"""Gateway mode with real Home Assistant fixtures and respx at the HTTP layer."""

import asyncio
from datetime import timedelta
import json
from unittest.mock import patch

import httpx
import pytest
import respx

from homeassistant import config_entries
from homeassistant.components import conversation, stt, tts
from homeassistant.components.conversation.chat_log import DATA_CHAT_LOGS
from homeassistant.components.homeassistant.exposed_entities import (
    async_expose_entity,
)
from homeassistant.components.tts.media_source import generate_media_source_id
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    intent,
    issue_registry as ir,
)
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.openwebui_agent import events, gateway_client
from custom_components.openwebui_agent.const import DOMAIN
from custom_components.openwebui_agent.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.openwebui_agent.oikos import DATA_PUSHERS

URL = "http://gateway.test:8000"
TOKEN = "fake-gateway-token"
EVENTS_TOKEN = "fake-events-token"
DATA = {
    "backend": "gateway",
    "gateway_url": URL,
    "gateway_token": "",
    "language": "el",
}
OPTIONS = {"completion_timeout": 60, "tts_voice": "", "tts_format": "mp3"}
DEFAULT_OPTIONS = {
    "completion_timeout": 120,
    "tts_voice": "",
    "tts_format": "mp3",
    "fallback_to_home_assistant": False,
    "events": True,
    "presence": [],
    "door_open_after": {"hours": 0, "minutes": 10, "seconds": 0},
    "unavailable_after": {"hours": 0, "minutes": 2, "seconds": 0},
    "generic_state_changes": False,
    "gateway_events_token": "",
}
HOUSEHOLD = {
    "events": True,
    "presence": [],
    "door_open_after": {"hours": 0, "minutes": 10, "seconds": 0},
    "unavailable_after": {"hours": 0, "minutes": 2, "seconds": 0},
    "generic_state_changes": False,
}
HEALTH = {
    "status": "ok",
    "version": "18.0.0",
    "checks": {"stt": {"status": "ok", "url": "http://speaches.local/v1/models"}},
}
NOT_FOUND = {"error": {"code": "conversation_not_found", "message": "Unknown."}}
UNAUTHORIZED = {"error": {"code": "unauthorized", "message": "Missing bearer token"}}
FORBIDDEN = {"error": {"code": "forbidden", "message": "Token lacks scope"}}
REPLY = {
    "conversation_id": "gw-conv-1",
    # Plain already: Home Assistant shows it as it is (no Markdown pass).
    "display_text": "Done: the *office* light is on.",
    "speech_text": "Done.",
    "expects_reply": False,
    "new_chat": True,
    "identity": {"confidence": "device-default"},
    "timings": {"total_ms": 812},
}
STILL_RUNNING = {
    "error": {
        "code": "timeout",
        "message": "The assistant is still working on that.",
        "still_running": True,
        "turn_id": "t-1",
        "conversation_id": "gw-conv-1",
    }
}
ACCEPTED = {"accepted": True, "event_id": "x", "reason": "reasoning_queued"}
OFFLINE = "Assistant offline, basic commands only."


class FakeGateway:
    """respx routes for every gateway endpoint the integration calls."""

    def __init__(self, mock: respx.MockRouter) -> None:
        self.require_tokens = False
        # A token without the assistant:turns scope (403 in both modes).
        self.forbid_token = False
        self.health = mock.get("/health").respond(json=HEALTH)
        self.probe = mock.get("/assistant/v1/conversations/ha-auth-probe")
        self.probe.side_effect = self._probe
        self.turns = mock.post("/assistant/v1/turns").respond(json=REPLY)
        self.poll = mock.route(method="GET", path__regex=r"^/assistant/v1/turns/.+$")
        self.stt = mock.post("/v1/audio/transcriptions").respond(
            json={
                "text": "Alex: anapse to fos",
                "plain_text": "anapse to fos",
                "fixed_greek": "άναψε το φως",
            }
        )
        self.tts = mock.post("/v1/audio/speech").respond(
            content=b"ID3-fake-mp3", headers={"Content-Type": "audio/mpeg"}
        )
        self.events = mock.post("/household/v1/events").respond(202, json=ACCEPTED)
        self.mock = mock

    def _probe(self, request: httpx.Request) -> httpx.Response:
        """Open mode accepts anything; otherwise only the known token."""
        token = request.headers.get("Authorization")
        if self.forbid_token and token:
            return httpx.Response(403, json=FORBIDDEN)
        if self.require_tokens and token != f"Bearer {TOKEN}":
            return httpx.Response(401, json=UNAUTHORIZED)
        return httpx.Response(404, json=NOT_FOUND)

    @property
    def requests(self) -> list[httpx.Request]:
        return [call.request for call in self.mock.calls]

    def event_bodies(self) -> list[dict]:
        return [json.loads(call.request.content) for call in self.events.calls]


@pytest.fixture
def gateway():
    """Serve the gateway's HTTP API through respx."""
    with respx.mock(base_url=URL, assert_all_called=False) as mock:
        yield FakeGateway(mock)


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch):
    """Poll a still-running turn without waiting two seconds."""
    monkeypatch.setattr(gateway_client, "POLL_INTERVAL_S", 0)


async def _setup(hass, data=None, options=None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="AI gateway",
        data={**DATA, **(data or {})},
        options={**OPTIONS, **(options or {})},
        version=2,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _wait_for(condition) -> None:
    """Let the event push's background sender run until ``condition`` holds."""
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met")


def _entity_id(hass, platform: str, unique_id: str) -> str:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)


def _fields(result) -> set[str]:
    return {key.schema for key in result["data_schema"].schema}


async def _start_gateway_flow(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == "menu"
    assert result["menu_options"] == ["gateway", "openwebui"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "gateway"}
    )
    assert result["step_id"] == "gateway"
    return result


async def test_setup_flow_open_mode(hass, gateway):
    """URL and language only; the test step shows the version and open mode."""
    result = await _start_gateway_flow(hass)
    assert _fields(result) == {"gateway_url", "gateway_token", "language"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"gateway_url": f"{URL}/", "language": "el"}
    )
    assert result["step_id"] == "test"
    assert result["description_placeholders"] == {
        "version": "18.0.0",
        "status": "ok",
        "access": "open mode, no token needed on your network",
    }
    assert gateway.health.called and gateway.probe.called
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == "create_entry"
    assert result["title"] == "AI gateway"
    assert result["data"] == {**DATA, "gateway_token": ""}
    assert result["options"] == DEFAULT_OPTIONS
    await hass.async_block_till_done()
    # Open mode: no request ever carried a token.
    assert gateway.requests
    assert all("Authorization" not in r.headers for r in gateway.requests)


async def test_setup_flow_requires_a_valid_token(hass, gateway):
    """Tokens required: a missing and a rejected token stay on the form."""
    gateway.require_tokens = True
    gateway.health.respond(
        json={
            **HEALTH,
            "status": "degraded",
            "checks": {"tts": {"status": "error", "error": "down"}},
        }
    )
    result = await _start_gateway_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"gateway_url": URL, "language": "en"}
    )
    assert result["errors"] == {"base": "token_required"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"gateway_url": URL, "gateway_token": "wrong", "language": "en"},
    )
    assert result["errors"] == {"base": "gateway_unauthorized"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"gateway_url": URL, "gateway_token": f" {TOKEN} ", "language": "en"},
    )
    assert result["step_id"] == "test"
    assert result["description_placeholders"]["access"] == (
        "tokens required, and this token was accepted"
    )
    assert result["description_placeholders"]["status"] == "degraded; unavailable: tts"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["data"]["gateway_token"] == TOKEN
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("url", "route", "key"),
    [
        ("ftp://gateway.test", None, "invalid_url"),
        (URL, httpx.ConnectError("refused"), "gateway_unreachable"),
        (URL, httpx.Response(404, text="<html>Open WebUI</html>"), "not_gateway"),
        (URL, httpx.Response(200, json={"detail": "x"}), "not_gateway"),
    ],
)
async def test_setup_flow_errors(hass, gateway, url, route, key):
    """A wrong URL, an unreachable gateway and a non-gateway are reported apart."""
    if isinstance(route, Exception):
        gateway.health.side_effect = route
    elif route is not None:
        gateway.health.return_value = route
    result = await _start_gateway_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"gateway_url": url, "language": "en"}
    )
    assert result["step_id"] == "gateway"
    assert result["errors"] == {"base": key}


@pytest.mark.parametrize("open_mode", [True, False])
async def test_setup_flow_reads_open_mode_from_health(hass, gateway, open_mode):
    """Health says whether a token is needed; one probe checks the access."""
    gateway.require_tokens = not open_mode
    gateway.health.respond(json={**HEALTH, "open_mode": open_mode})
    result = await _start_gateway_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"gateway_url": URL, "language": "en"}
    )
    assert gateway.probe.call_count == 1
    if open_mode:
        assert result["step_id"] == "test"
        assert result["description_placeholders"]["access"] == (
            "open mode, no token needed on your network"
        )
    else:
        assert result["errors"] == {"base": "token_required"}


async def test_same_gateway_twice_aborts(hass, gateway):
    """One entry per gateway URL."""
    MockConfigEntry(domain=DOMAIN, data=DATA, options=OPTIONS).add_to_hass(hass)
    result = await _start_gateway_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"gateway_url": f"{URL}/", "language": "en"}
    )
    assert result["type"] == "abort" and result["reason"] == "already_configured"


async def test_options_flow(hass, gateway):
    """Options: wait, voice, format, and the household events in one section."""
    entry = await _setup(hass, options={"tts_voice": "maria"})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    assert _fields(result) == {
        "completion_timeout",
        "tts_voice",
        "tts_format",
        "fallback_to_home_assistant",
        "household",
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "completion_timeout": 45,
            "tts_format": "opus",
            "household": {
                **HOUSEHOLD,
                "presence": ["person.alex"],
                "door_open_after": {"hours": 0, "minutes": 5, "seconds": 0},
                "gateway_events_token": f" {EVENTS_TOKEN} ",
            },
        },
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.options["completion_timeout"] == 45
    assert entry.options["tts_format"] == "opus"
    # A cleared voice field falls back to the gateway's speech profile voice.
    assert entry.options["tts_voice"] == ""
    assert entry.options["presence"] == ["person.alex"]
    assert entry.options["door_open_after"] == {"hours": 0, "minutes": 5, "seconds": 0}
    assert entry.options["gateway_events_token"] == EVENTS_TOKEN
    assert entry.options["fallback_to_home_assistant"] is False
    assert "household" not in entry.options and "chat_model" not in entry.options
    # The reload after saving started a new event push; the view is not re-added.
    assert entry.state is config_entries.ConfigEntryState.LOADED
    assert hass.data[DATA_PUSHERS][entry.entry_id].has_token

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"completion_timeout": 45, "tts_format": "opus", "household": HOUSEHOLD},
    )
    await hass.async_block_till_done()
    # A cleared events token sends events without one (open mode).
    assert entry.options["gateway_events_token"] == ""


async def test_reauth_replaces_token(hass, gateway):
    """A gateway that starts requiring tokens triggers reauth; a token fixes it."""
    gateway.require_tokens = True
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, options=OPTIONS, version=2)
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is config_entries.ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]
    result = await hass.config_entries.flow.async_configure(flows[0]["flow_id"])
    assert result["step_id"] == "reauth_confirm"
    assert _fields(result) == {"gateway_token"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"gateway_token": "wrong"}
    )
    assert result["errors"] == {"base": "gateway_unauthorized"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"gateway_token": TOKEN}
    )
    assert result["type"] == "abort" and result["reason"] == "reauth_successful"
    await hass.async_block_till_done()
    assert entry.data == {**DATA, "gateway_token": TOKEN}
    assert entry.state is config_entries.ConfigEntryState.LOADED


async def test_forbidden_token_starts_reauth(hass, gateway):
    """A 403 at setup (token without assistant:turns) asks for a token, not retries."""
    gateway.forbid_token = True
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**DATA, "gateway_token": TOKEN},
        options=OPTIONS,
        version=2,
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is config_entries.ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]
    result = await hass.config_entries.flow.async_configure(flows[0]["flow_id"])
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"gateway_token": TOKEN}
    )
    assert result["errors"] == {"base": "gateway_forbidden"}
    # Open mode: an empty token is a valid answer.
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == "abort" and result["reason"] == "reauth_successful"
    await hass.async_block_till_done()
    assert entry.state is config_entries.ConfigEntryState.LOADED


async def test_reconfigure(hass, gateway):
    """Change URL, token or language; the test step runs before saving."""
    entry = await _setup(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    with respx.mock(base_url="http://other.test:8000", assert_all_called=False) as m:
        m.get("/health").respond(json=HEALTH)
        m.get("/assistant/v1/conversations/ha-auth-probe").respond(404, json=NOT_FOUND)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"gateway_url": "http://other.test:8000", "language": "en"},
        )
        assert result["step_id"] == "test"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()
    assert result["type"] == "abort" and result["reason"] == "reconfigure_successful"
    assert entry.data["gateway_url"] == "http://other.test:8000"
    assert entry.data["language"] == "en"


async def test_reconfigure_direct_entry_aborts(hass):
    """Direct entries are deprecated; they are not reconfigured into the gateway."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"service_name": "Test", "base_url": "https://x.test", "api_key": "k"},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] == "abort" and result["reason"] == "reconfigure_direct"


async def test_setup_creates_three_entities_on_one_device(hass, gateway):
    """Agent, speech-to-text and text-to-speech share the gateway's device."""
    entry = await _setup(hass)
    ids = {
        _entity_id(hass, "conversation", entry.entry_id),
        _entity_id(hass, "stt", f"{entry.entry_id}-stt"),
        _entity_id(hass, "tts", f"{entry.entry_id}-tts"),
    }
    assert ids == {
        "conversation.ai_gateway",
        "stt.ai_gateway_speech_to_text",
        "tts.ai_gateway_text_to_speech",
    }
    device = dr.async_get(hass).async_get_device({(DOMAIN, entry.entry_id)})
    assert device.sw_version == "18.0.0"
    assert device.configuration_url == f"{URL}/ui"
    registry = er.async_get(hass)
    assert {registry.async_get(i).device_id for i in ids} == {device.id}
    assert not ir.async_get(hass).issues  # no deprecation notice for the gateway
    assert entry.entry_id in hass.data[DATA_PUSHERS]
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.entry_id not in hass.data[DOMAIN]
    assert entry.entry_id not in hass.data[DATA_PUSHERS]


async def test_setup_retries_while_unreachable(hass, gateway):
    """An unreachable gateway is retried by Home Assistant, not reauthenticated."""
    gateway.health.side_effect = httpx.ConnectError("refused")
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, options=OPTIONS, version=2)
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is config_entries.ConfigEntryState.SETUP_RETRY


@pytest.mark.parametrize("token", ["", TOKEN])
async def test_assist_turn(hass, gateway, token):
    """Satellite context, the 504 envelope, its follow-up and the mapping."""
    gateway.require_tokens = bool(token)
    entry = await _setup(hass, data={"gateway_token": token})
    agent_id = _entity_id(hass, "conversation", entry.entry_id)
    area = ar.async_get(hass).async_create("Living Room")
    satellite = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", "satellite")}
    )
    dr.async_get(hass).async_update_device(satellite.id, area_id=area.id)
    er.async_get(hass).async_get_or_create(
        "assist_satellite",
        "test",
        "living-room",
        device_id=satellite.id,
        suggested_object_id="living_room",
    )
    # Accepted, past its deadline (504 with turn_id), running, then done.
    gateway.turns.respond(504, json=STILL_RUNNING)
    gateway.poll.side_effect = [
        httpx.Response(202, json={"turn_id": "t-1", "status": "running"}),
        httpx.Response(
            200,
            json={
                "turn_id": "t-1",
                "status": "done",
                "result": {**REPLY, "expects_reply": True, "turn_id": "t-1"},
            },
        ),
    ]
    result = await conversation.async_converse(
        hass,
        "Turn on the light",
        None,
        Context(),
        language="el",
        agent_id=agent_id,
        satellite_id="assist_satellite.living_room",
        extra_system_prompt="Keep it short.",
    )
    assert result.response.speech["plain"]["speech"] == "Done."
    assert result.continue_conversation is True
    assert gateway.turns.call_count == 1  # never resubmitted
    assert gateway.poll.call_count == 2
    assert gateway.poll.calls.last.request.url.path == "/assistant/v1/turns/t-1"
    # The display text is already plain: shown as it is, no second Markdown pass.
    chat_log = hass.data[DATA_CHAT_LOGS][result.conversation_id]
    assert chat_log.content[-1].content == REPLY["display_text"]
    body = json.loads(gateway.turns.calls.last.request.content)
    assert body == {
        "conversation_id": result.conversation_id,
        "text": "Turn on the light",
        "language": "el",
        "device_id": satellite.id,
        "area": "Living Room",
        "extra_instructions": "Keep it short.",
        "timeout_s": 60,
        "response": {"speech": False, "stream": False},
    }
    # The follow-up goes to the gateway conversation the reply named.
    gateway.turns.respond(json={**REPLY, "new_chat": False})
    result = await conversation.async_converse(
        hass,
        "Thanks",
        result.conversation_id,
        Context(),
        language="el",
        agent_id=agent_id,
        device_id=satellite.id,
    )
    assert result.continue_conversation is False
    body = json.loads(gateway.turns.calls.last.request.content)
    assert body["conversation_id"] == "gw-conv-1"
    assert body["area"] == "Living Room"
    # Open mode never sends a token; a configured token is always sent.
    for request in gateway.requests:
        if request.url.path.startswith("/assistant/v1/turns"):
            assert request.headers.get("Authorization") == (
                f"Bearer {token}" if token else None
            )


@pytest.mark.parametrize(
    ("poll", "spoken"),
    [
        (
            httpx.Response(
                200,
                json={
                    "turn_id": "t-1",
                    "status": "failed",
                    "error": {"code": "agent_failed", "message": "No answer."},
                },
            ),
            "Open WebUI did not save a completed text answer",
        ),
        (
            httpx.Response(
                404, json={"error": {"code": "turn_not_found", "message": "Gone."}}
            ),
            "still working",
        ),
    ],
)
async def test_followed_up_turn_failed_or_expired(hass, gateway, poll, spoken):
    """A failed turn speaks its error; 404 keeps "still working"."""
    entry = await _setup(hass)
    agent_id = _entity_id(hass, "conversation", entry.entry_id)
    gateway.turns.respond(504, json=STILL_RUNNING)
    gateway.poll.side_effect = [poll]
    result = await conversation.async_converse(
        hass, "Open the garage", None, Context(), language="en", agent_id=agent_id
    )
    assert spoken in result.response.speech["plain"]["speech"]
    assert result.continue_conversation is False
    assert gateway.turns.call_count == 1 and gateway.poll.call_count == 1


async def test_assist_turn_errors_are_spoken(hass, gateway):
    """A deadline after acceptance without a turn_id says it is still working."""
    entry = await _setup(hass)
    agent_id = _entity_id(hass, "conversation", entry.entry_id)
    gateway.turns.respond(
        504,
        json={
            "error": {
                "code": "timeout",
                "message": "Still working.",
                "still_running": True,
            }
        },
    )
    result = await conversation.async_converse(
        hass, "Open the garage", None, Context(), language="en", agent_id=agent_id
    )
    assert "still working" in result.response.speech["plain"]["speech"]
    assert result.continue_conversation is False
    assert gateway.turns.call_count == 1
    assert not gateway.poll.called


async def test_degraded_gateway_answer(hass, gateway):
    """Spoken as usual, never listening on, exposed as attribute, event and diagnostics."""
    entry = await _setup(hass)
    agent_id = _entity_id(hass, "conversation", entry.entry_id)
    assert hass.states.get(agent_id).attributes["degraded"] is False
    degraded = async_capture_events(hass, "openwebui_agent_degraded")
    gateway.turns.respond(
        json={
            **REPLY,
            "speech_text": f"{OFFLINE} Turned on the light.",
            "expects_reply": True,
            "degraded": True,
        }
    )
    result = await conversation.async_converse(
        hass,
        "Turn on the light",
        None,
        Context(),
        language="en",
        agent_id=agent_id,
        device_id="satellite-device",
    )
    assert (
        result.response.speech["plain"]["speech"] == f"{OFFLINE} Turned on the light."
    )
    assert result.continue_conversation is False
    assert gateway.turns.call_count == 1  # never retried
    assert hass.states.get(agent_id).attributes["degraded"] is True
    assert [(e.data["source"], e.data["device_id"]) for e in degraded] == [
        ("gateway", "satellite-device")
    ]
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["degraded_answers"] == {
        "fallback_to_home_assistant": False,
        "last_answer": "gateway",
        "count": 1,
    }
    # The next normal answer clears the attribute.
    gateway.turns.respond(json=REPLY)
    await conversation.async_converse(
        hass, "Thanks", None, Context(), language="en", agent_id=agent_id
    )
    assert hass.states.get(agent_id).attributes["degraded"] is False
    assert len(degraded) == 1


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError("refused"),
        httpx.Response(502, text="<html>Bad Gateway</html>"),
    ],
)
async def test_fallback_to_home_assistant(hass, gateway, failure):
    """Unreachable gateway and the option on: Home Assistant's agent answers."""
    entry = await _setup(hass, options={"fallback_to_home_assistant": True})
    agent_id = _entity_id(hass, "conversation", entry.entry_id)
    degraded = async_capture_events(hass, "openwebui_agent_degraded")
    if isinstance(failure, Exception):
        gateway.turns.side_effect = failure
    else:
        gateway.turns.return_value = failure
    with patch(
        "homeassistant.components.conversation.async_converse",
        wraps=conversation.async_converse,
    ) as converse:
        result = await conversation.async_converse(
            hass,
            "What time is it?",
            None,
            Context(),
            language="en",
            agent_id=agent_id,
            device_id="satellite-device",
        )
    speech = result.response.speech["plain"]["speech"]
    assert speech.startswith(f"{OFFLINE} ") and len(speech) > len(OFFLINE) + 1
    # A real intent ran (the current time), not an error.
    assert result.response.response_type is intent.IntentResponseType.ACTION_DONE
    assert result.continue_conversation is False
    assert result.conversation_id
    # The original turn is never retried; Home Assistant's agent got the same input.
    assert gateway.turns.call_count == 1
    inner = converse.call_args_list[1]
    assert inner.args[1] == "What time is it?"
    assert inner.args[2] == result.conversation_id
    assert inner.kwargs["agent_id"] == "conversation.home_assistant"
    assert inner.kwargs["device_id"] == "satellite-device"
    assert inner.kwargs["language"] == "en"
    assert hass.states.get(agent_id).attributes["degraded"] is True
    assert [e.data["source"] for e in degraded] == ["home_assistant"]
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["degraded_answers"]["last_answer"] == "home_assistant"


@pytest.mark.parametrize(
    ("fallback", "reply", "spoken"),
    [
        (False, httpx.ConnectError("refused"), "I can't reach the assistant gateway"),
        (
            True,
            httpx.Response(401, json=UNAUTHORIZED),
            "rejected this Home Assistant token",
        ),
        (
            True,
            httpx.Response(
                504,
                json={"error": {"code": "timeout", "still_running": True}},
            ),
            "still working",
        ),
        (
            True,
            httpx.Response(
                502, json={"error": {"code": "cannot_connect", "message": "x"}}
            ),
            "Cannot connect to Open WebUI",
        ),
    ],
)
async def test_no_fallback(hass, gateway, fallback, reply, spoken):
    """Option off, a 4xx, a 504 still_running or a gateway error: no fallback."""
    entry = await _setup(hass, options={"fallback_to_home_assistant": fallback})
    agent_id = _entity_id(hass, "conversation", entry.entry_id)
    degraded = async_capture_events(hass, "openwebui_agent_degraded")
    if isinstance(reply, Exception):
        gateway.turns.side_effect = reply
    else:
        gateway.turns.return_value = reply
    with patch(
        "homeassistant.components.conversation.async_converse",
        wraps=conversation.async_converse,
    ) as converse:
        result = await conversation.async_converse(
            hass, "Open the garage", None, Context(), language="en", agent_id=agent_id
        )
    assert spoken in result.response.speech["plain"]["speech"]
    assert OFFLINE not in result.response.speech["plain"]["speech"]
    assert converse.call_count == 1  # only this test's own call
    assert not degraded
    assert hass.states.get(agent_id).attributes["degraded"] is False


async def test_speech_to_text(hass, gateway):
    """WAV upload in the pipeline's language; plain_text, never fixed_greek."""
    entry = await _setup(hass)
    entity = stt.async_get_speech_to_text_entity(
        hass, _entity_id(hass, "stt", f"{entry.entry_id}-stt")
    )
    assert "el" in entity.supported_languages

    async def audio():
        yield b"\x01\x02" * 800
        yield b"\x03\x04" * 800

    def metadata(language: str) -> stt.SpeechMetadata:
        return stt.SpeechMetadata(
            language=language,
            format=stt.AudioFormats.WAV,
            codec=stt.AudioCodecs.PCM,
            bit_rate=stt.AudioBitRates.BITRATE_16,
            sample_rate=stt.AudioSampleRates.SAMPLERATE_16000,
            channel=stt.AudioChannels.CHANNEL_MONO,
        )

    result = await entity.async_process_audio_stream(metadata("en-US"), audio())
    assert result == stt.SpeechResult("anapse to fos", stt.SpeechResultState.SUCCESS)
    request = gateway.stt.calls.last.request
    assert request.headers["Content-Type"].startswith("multipart/form-data")
    content = request.content
    assert b'name="language"\r\n\r\nen\r\n' in content  # the pipeline's base code
    assert b'name="model"\r\n\r\nwhisper-1\r\n' in content
    assert b'filename="audio.wav"' in content
    assert b"RIFF" in content and b"WAVEfmt " in content
    assert "Authorization" not in request.headers

    # Without a pipeline language the configured one is sent.
    gateway.stt.respond(json={"text": "plain transcript", "fixed_greek": "x"})
    result = await entity.async_process_audio_stream(metadata(""), audio())
    assert result.text == "plain transcript"
    assert b'name="language"\r\n\r\nel\r\n' in gateway.stt.calls.last.request.content
    gateway.stt.respond(502, json={"detail": "Speaches failed"})
    result = await entity.async_process_audio_stream(metadata("el"), audio())
    assert result.result is stt.SpeechResultState.ERROR


async def test_text_to_speech(hass, gateway):
    """The configured voice and format reach the gateway; audio comes back."""
    entry = await _setup(hass, options={"tts_voice": "maria"})
    engine = _entity_id(hass, "tts", f"{entry.entry_id}-tts")
    extension, audio = await tts.async_get_media_source_audio(
        hass,
        generate_media_source_id(hass, "Καλημέρα", engine=engine, cache=False),
    )
    assert (extension, audio) == ("mp3", b"ID3-fake-mp3")
    assert json.loads(gateway.tts.calls.last.request.content) == {
        "model": "tts-1",
        "input": "Καλημέρα",
        "response_format": "mp3",
        "voice": "maria",
    }
    assert "Authorization" not in gateway.tts.calls.last.request.headers

    gateway.tts.respond(503, json={"detail": "no TTS"})
    with pytest.raises(HomeAssistantError):
        await tts.async_get_media_source_audio(
            hass,
            generate_media_source_id(hass, "Again", engine=engine, cache=False),
        )


async def test_text_to_speech_opus_is_ogg(hass, gateway):
    """Opus arrives in an Ogg container; HA converts it for satellites as needed."""
    entry = await _setup(hass, options={"tts_format": "opus"})
    entity = hass.data[tts.DATA_COMPONENT].get_entity(
        _entity_id(hass, "tts", f"{entry.entry_id}-tts")
    )
    assert entity.default_language == "el"
    assert entity.default_options == {"response_format": "opus"}
    extension, _ = await entity.async_get_tts_audio(
        "Hello", "el", dict(entity.default_options)
    )
    assert extension == "ogg"
    body = json.loads(gateway.tts.calls.last.request.content)
    assert body["response_format"] == "opus"


async def test_text_to_speech_defaults_to_gateway_voice(hass, gateway):
    """No voice configured: the gateway's speech profile voice is used."""
    entry = await _setup(hass)
    engine = _entity_id(hass, "tts", f"{entry.entry_id}-tts")
    extension, _ = await tts.async_get_media_source_audio(
        hass, generate_media_source_id(hass, "Hello", engine=engine, cache=False)
    )
    assert extension == "mp3"
    assert "voice" not in json.loads(gateway.tts.calls.last.request.content)


async def test_oikos_view(hass, gateway, hass_client, hass_client_no_auth):
    """Exposed entities only, with area names, in the shape the gateway reads."""
    await _setup(hass)
    areas, devices, entities = (
        ar.async_get(hass),
        dr.async_get(hass),
        er.async_get(hass),
    )
    kitchen, hall = areas.async_create("Kitchen"), areas.async_create("Hall")
    lamp_device = devices.async_get_or_create(
        config_entry_id=hass.config_entries.async_entries(DOMAIN)[0].entry_id,
        identifiers={("test", "lamp")},
    )
    devices.async_update_device(lamp_device.id, area_id=kitchen.id)
    lamp = entities.async_get_or_create(
        "light", "test", "lamp", device_id=lamp_device.id, suggested_object_id="lamp"
    )
    entities.async_update_entity(lamp.entity_id, aliases=["ceiling light"])
    hass.states.async_set(
        "light.lamp",
        "on",
        {
            "friendly_name": "Kitchen lamp",
            "brightness": 200,
            "supported_color_modes": ["brightness"],
            "entity_picture": "/local/lamp.png",
        },
    )
    sensor = entities.async_get_or_create(
        "sensor", "test", "temp", suggested_object_id="temp"
    )
    entities.async_update_entity(sensor.entity_id, area_id=hall.id)
    hass.states.async_set(
        "sensor.temp",
        "21.5",
        {"friendly_name": "Hall temperature", "unit_of_measurement": "°C"},
    )
    entities.async_get_or_create(
        "switch", "test", "hidden", suggested_object_id="hidden"
    )
    hass.states.async_set("switch.hidden", "off")
    async_expose_entity(hass, "conversation", "light.lamp", True)
    async_expose_entity(hass, "conversation", "sensor.temp", True)
    async_expose_entity(hass, "conversation", "switch.hidden", False)

    client = await hass_client()
    response = await client.get("/api/oikos?area=1&caps=1&assistant=conversation")
    assert response.status == 200
    body = await response.json()
    assert body["assistant"] == "conversation"
    by_id = {item["entity_id"]: item for item in body["entities"]}
    assert "switch.hidden" not in by_id
    assert body["count"] == len(body["entities"])
    assert {k: v for k, v in by_id["light.lamp"].items() if k != "services"} == {
        "entity_id": "light.lamp",
        "name": "Kitchen lamp",
        "aliases": ["ceiling light"],
        "domain": "light",
        "state": "on",
        "available": True,
        "area_name": "Kitchen",
        "capabilities": {"brightness": 200, "supported_color_modes": ["brightness"]},
        "actions": ["turn_on", "turn_off", "toggle", "set_brightness"],
    }
    assert isinstance(by_id["light.lamp"]["services"], list)
    assert by_id["sensor.temp"]["area_name"] == "Hall"  # the entity's own area
    assert by_id["sensor.temp"]["actions"] == ["read_state"]
    assert by_id["sensor.temp"]["services"] == []
    assert body["push"]["enabled"] is True
    assert body["push"]["sent"] == 0

    response = await client.get("/api/oikos?slim=1&area=1")
    slim = {item["entity_id"]: item for item in (await response.json())["entities"]}
    assert slim["light.lamp"] == {
        "entity_id": "light.lamp",
        "name": "Kitchen lamp",
        "aliases": ["ceiling light"],
        "domain": "light",
        "area_name": "Kitchen",
    }
    response = await client.get("/api/oikos?all=1")
    everything = {
        item["entity_id"]: item for item in (await response.json())["entities"]
    }
    assert everything["switch.hidden"]["exposed"] is False
    assert (await client.get("/api/oikos?assistant=other")).status == 400
    assert (
        await client.get("/api/oikos?assistant=conversation.home_assistant")
    ).status == 200
    assert (await (await hass_client_no_auth()).get("/api/oikos")).status == 401


@pytest.mark.parametrize("events_token", ["", EVENTS_TOKEN])
async def test_person_arrival_is_pushed(hass, gateway, events_token):
    """person_arrived_home with its deterministic id; a token only when set."""
    hass.states.async_set("person.alex", "not_home")
    hass.states.async_set("person.anna", "not_home")
    entry = await _setup(
        hass,
        options={"presence": ["person.alex"], "gateway_events_token": events_token},
    )
    hass.states.async_set("person.anna", "home")  # not listed: not tracked
    hass.states.async_set(
        "person.alex", "home", {"friendly_name": "Alex", "source": "device_tracker.p"}
    )
    changed = hass.states.get("person.alex").last_changed
    await _wait_for(lambda: gateway.events.called)
    assert gateway.events.call_count == 1
    request = gateway.events.calls.last.request
    assert request.url == f"{URL}/household/v1/events"
    assert request.headers.get("Authorization") == (
        f"Bearer {events_token}" if events_token else None
    )
    body = json.loads(request.content)
    assert body == {
        "source": "home_assistant",
        "type": "person_arrived_home",
        "entity_id": "person.alex",
        "timestamp": changed.isoformat(),
        "data": {
            "state": "home",
            "previous": "not_home",
            "name": "Alex",
            "source": "device_tracker.p",
        },
        "priority": "normal",
        "event_id": events.event_id_for("person_arrived_home", "person.alex", changed),
    }
    pusher = hass.data[DATA_PUSHERS][entry.entry_id]
    assert pusher.status()["sent"] == 1
    # The diagnostics show the counts and whether a token is set, never the token.
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    household = diagnostics["household_events"]
    assert household["push"]["sent"] == 1
    assert household["events_token_configured"] is bool(events_token)
    assert household["presence_count"] == 1
    assert EVENTS_TOKEN not in str(diagnostics)


async def test_exposed_person_is_tracked_without_a_presence_list(hass, gateway):
    """Empty presence: every person exposed to Assist."""
    hass.states.async_set("person.alex", "home")
    hass.states.async_set("person.anna", "home")
    async_expose_entity(hass, "conversation", "person.alex", True)
    async_expose_entity(hass, "conversation", "person.anna", False)
    await _setup(hass)
    hass.states.async_set("person.anna", "not_home")
    hass.states.async_set("person.alex", "not_home")
    await _wait_for(lambda: gateway.events.called)
    await asyncio.sleep(0.05)
    assert [(b["type"], b["entity_id"]) for b in gateway.event_bodies()] == [
        ("person_left_home", "person.alex")
    ]


async def test_event_push_retries_server_errors(hass, gateway, monkeypatch):
    """5xx is retried (3 attempts in all); a rejected body is not."""
    monkeypatch.setattr(events, "RETRY_DELAYS_S", (0, 0))
    hass.states.async_set("person.alex", "not_home")
    entry = await _setup(hass, options={"presence": ["person.alex"]})
    pusher = hass.data[DATA_PUSHERS][entry.entry_id]
    gateway.events.side_effect = [
        httpx.Response(503, text="busy"),
        httpx.ConnectError("refused"),
        httpx.Response(202, json=ACCEPTED),
    ]
    hass.states.async_set("person.alex", "home")
    await _wait_for(lambda: pusher.sent == 1)
    assert gateway.events.call_count == 3
    ids = {b["event_id"] for b in gateway.event_bodies()}
    assert len(ids) == 1  # the same event every time: a duplicate at the gateway
    assert pusher.failed == 0

    gateway.events.side_effect = [httpx.Response(401, json={"error": "unauthorized"})]
    hass.states.async_set("person.alex", "not_home")
    await _wait_for(lambda: pusher.failed == 1)
    assert gateway.events.call_count == 4
    assert "no events token is set" in pusher.status()["last_error"]


async def test_door_open_long_and_clean_unload(hass, gateway, hass_client):
    """Timers fire while loaded; unload removes listeners, timers and the push."""
    hass.states.async_set(
        "binary_sensor.garage", "off", {"device_class": "garage_door"}
    )
    hass.states.async_set("binary_sensor.front", "off", {"device_class": "door"})
    hass.states.async_set("person.alex", "not_home")
    for entity_id in ("binary_sensor.garage", "binary_sensor.front"):
        async_expose_entity(hass, "conversation", entity_id, True)
    entry = await _setup(
        hass,
        options={
            "presence": ["person.alex"],
            "door_open_after": {"hours": 0, "minutes": 1, "seconds": 0},
        },
    )
    pusher = hass.data[DATA_PUSHERS][entry.entry_id]
    hass.states.async_set("binary_sensor.garage", "on", {"device_class": "garage_door"})
    opened = hass.states.get("binary_sensor.garage").last_changed
    assert pusher.status()["pending_timers"] == 1
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=2))
    await _wait_for(lambda: gateway.events.called)
    body = gateway.event_bodies()[-1]
    assert body["type"] == "door_open_long" and body["priority"] == "high"
    assert body["event_id"] == events.event_id_for(
        "door_open_long", "binary_sensor.garage", opened
    )

    hass.states.async_set("binary_sensor.front", "on", {"device_class": "door"})
    assert pusher.status()["pending_timers"] == 1
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert pusher.status() | {"enabled": None} == pusher.status() | {
        "enabled": None,
        "pending_timers": 0,
    }
    assert not pusher.status()["enabled"]
    assert DATA_PUSHERS not in hass.data or not hass.data[DATA_PUSHERS]
    calls = gateway.events.call_count
    hass.states.async_set("person.alex", "home")
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=10))
    await hass.async_block_till_done()
    await asyncio.sleep(0.05)
    assert gateway.events.call_count == calls
    # The view outlives the entry and reports the push as off.
    client = await hass_client()
    body = await (await client.get("/api/oikos")).json()
    assert body["push"]["enabled"] is False


async def test_events_off(hass, gateway):
    """Events off: nothing is watched or sent."""
    hass.states.async_set("person.alex", "not_home")
    entry = await _setup(hass, options={"events": False, "presence": ["person.alex"]})
    assert entry.entry_id not in hass.data.get(DATA_PUSHERS, {})
    hass.states.async_set("person.alex", "home")
    await hass.async_block_till_done()
    await asyncio.sleep(0.05)
    assert not gateway.events.called


async def test_diagnostics(hass, gateway):
    """Live check and settings, never the URL or the token."""
    gateway.require_tokens = True
    entry = await _setup(
        hass,
        data={"gateway_token": TOKEN},
        options={"gateway_events_token": EVENTS_TOKEN},
    )
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["backend"] == "gateway"
    assert result["gateway"] == {
        "status": "ok",
        "version": "18.0.0",
        "degraded": [],
        "open_mode": False,
        "degraded_mode": None,
        "token_needed": True,
    }
    assert result["degraded_answers"] == {
        "fallback_to_home_assistant": False,
        "last_answer": None,
        "count": 0,
    }
    assert result["token_configured"] is True
    assert result["language"] == "el"
    assert result["household_events"]["events_token_configured"] is True
    assert result["household_events"]["door_open_after_s"] == 600
    assert result["household_events"]["unavailable_after_s"] == 120
    assert all(
        secret not in str(result) for secret in (TOKEN, EVENTS_TOKEN, "gateway.test")
    )
    gateway.health.side_effect = httpx.ConnectError("refused")
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["gateway"] == {"error": "gateway_unreachable"}
