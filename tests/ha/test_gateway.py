"""Gateway mode with real Home Assistant fixtures and respx at the HTTP layer."""

import json

import httpx
import pytest
import respx

from homeassistant import config_entries
from homeassistant.components import conversation, stt, tts
from homeassistant.components.tts.media_source import generate_media_source_id
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.openwebui_agent import gateway_client
from custom_components.openwebui_agent.const import DOMAIN
from custom_components.openwebui_agent.diagnostics import (
    async_get_config_entry_diagnostics,
)

URL = "http://gateway.test:8000"
TOKEN = "fake-gateway-token"
DATA = {
    "backend": "gateway",
    "gateway_url": URL,
    "gateway_token": "",
    "language": "el",
}
OPTIONS = {"completion_timeout": 60, "tts_voice": "", "tts_format": "mp3"}
HEALTH = {
    "status": "ok",
    "version": "18.0.0",
    "checks": {"stt": {"status": "ok", "url": "http://speaches.local/v1/models"}},
}
NOT_FOUND = {"error": {"code": "conversation_not_found", "message": "Unknown."}}
UNAUTHORIZED = {"error": {"code": "unauthorized", "message": "Missing bearer token"}}
REPLY = {
    "conversation_id": "gw-conv-1",
    "display_text": "**Done.** See [the log](https://example.test).",
    "speech_text": "Done.",
    "expects_reply": False,
    "new_chat": True,
    "identity": {"confidence": "device-default"},
    "timings": {"total_ms": 812},
}


class FakeGateway:
    """respx routes for every gateway endpoint the integration calls."""

    def __init__(self, mock: respx.MockRouter) -> None:
        self.require_tokens = False
        self.health = mock.get("/health").respond(json=HEALTH)
        self.probe = mock.get("/assistant/v1/conversations/ha-auth-probe")
        self.probe.side_effect = self._probe
        self.turns = mock.post("/assistant/v1/turns").respond(json=REPLY)
        self.poll = mock.route(method="GET", path__regex=r"^/assistant/v1/turns/.+$")
        self.stt = mock.post("/v1/audio/transcriptions").respond(
            json={"text": "Billo: anapse to fos", "fixed_greek": "άναψε το φως"}
        )
        self.tts = mock.post("/v1/audio/speech").respond(
            content=b"ID3-fake-mp3", headers={"Content-Type": "audio/mpeg"}
        )
        self.mock = mock

    def _probe(self, request: httpx.Request) -> httpx.Response:
        """Open mode accepts anything; otherwise only the known token."""
        if self.require_tokens and (
            request.headers.get("Authorization") != f"Bearer {TOKEN}"
        ):
            return httpx.Response(401, json=UNAUTHORIZED)
        return httpx.Response(404, json=NOT_FOUND)

    @property
    def requests(self) -> list[httpx.Request]:
        return [call.request for call in self.mock.calls]


@pytest.fixture
def gateway():
    """Serve the gateway's HTTP API through respx."""
    with respx.mock(base_url=URL, assert_all_called=False) as mock:
        yield FakeGateway(mock)


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch):
    """Poll a still-running turn without waiting a second."""
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
    assert result["options"] == {
        "completion_timeout": 120,
        "tts_voice": "",
        "tts_format": "mp3",
    }
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


async def test_same_gateway_twice_aborts(hass, gateway):
    """One entry per gateway URL."""
    MockConfigEntry(domain=DOMAIN, data=DATA, options=OPTIONS).add_to_hass(hass)
    result = await _start_gateway_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"gateway_url": f"{URL}/", "language": "en"}
    )
    assert result["type"] == "abort" and result["reason"] == "already_configured"


async def test_options_flow(hass, gateway):
    """Options: turn timeout, voice and audio format only."""
    entry = await _setup(hass, options={"tts_voice": "maria"})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    assert _fields(result) == {"completion_timeout", "tts_voice", "tts_format"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"completion_timeout": 45, "tts_format": "opus"}
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.options["completion_timeout"] == 45
    assert entry.options["tts_format"] == "opus"
    # A cleared voice field falls back to the gateway's speech profile voice.
    assert entry.options["tts_voice"] == ""
    assert "chat_model" not in entry.options


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
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.entry_id not in hass.data[DOMAIN]


async def test_setup_retries_while_unreachable(hass, gateway):
    """An unreachable gateway is retried by Home Assistant, not reauthenticated."""
    gateway.health.side_effect = httpx.ConnectError("refused")
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, options=OPTIONS, version=2)
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is config_entries.ConfigEntryState.SETUP_RETRY


@pytest.mark.parametrize("token", ["", TOKEN])
async def test_assist_turn(hass, gateway, token):
    """Satellite device and area, polling, expects_reply and conversation mapping."""
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
    # Accepted, still running, then finished with a question.
    gateway.turns.respond(
        json={"still_running": True, "turn_id": "turn-1", "conversation_id": "c"}
    )
    gateway.poll.side_effect = [
        httpx.Response(200, json={"still_running": True, "turn_id": "turn-1"}),
        httpx.Response(200, json={**REPLY, "expects_reply": True, "turn_id": "turn-1"}),
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
    assert gateway.poll.call_count == 2
    assert gateway.poll.calls.last.request.url.path == "/assistant/v1/turns/turn-1"
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


async def test_assist_turn_errors_are_spoken(hass, gateway):
    """A deadline after acceptance says it is still working; never retried."""
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


async def test_speech_to_text(hass, gateway):
    """WAV upload in the configured language; the repaired transcript wins."""
    entry = await _setup(hass)
    entity = stt.async_get_speech_to_text_entity(
        hass, _entity_id(hass, "stt", f"{entry.entry_id}-stt")
    )
    assert "el" in entity.supported_languages

    async def audio():
        yield b"\x01\x02" * 800
        yield b"\x03\x04" * 800

    metadata = stt.SpeechMetadata(
        language="en",
        format=stt.AudioFormats.WAV,
        codec=stt.AudioCodecs.PCM,
        bit_rate=stt.AudioBitRates.BITRATE_16,
        sample_rate=stt.AudioSampleRates.SAMPLERATE_16000,
        channel=stt.AudioChannels.CHANNEL_MONO,
    )
    result = await entity.async_process_audio_stream(metadata, audio())
    assert result == stt.SpeechResult("άναψε το φως", stt.SpeechResultState.SUCCESS)
    request = gateway.stt.calls.last.request
    assert request.headers["Content-Type"].startswith("multipart/form-data")
    content = request.content
    assert b'name="language"\r\n\r\nel\r\n' in content  # configured, not "en"
    assert b'name="model"\r\n\r\nwhisper-1\r\n' in content
    assert b'filename="audio.wav"' in content
    assert b"RIFF" in content and b"WAVEfmt " in content
    assert "Authorization" not in request.headers

    gateway.stt.respond(json={"text": "plain transcript"})
    result = await entity.async_process_audio_stream(metadata, audio())
    assert result.text == "plain transcript"
    gateway.stt.respond(502, json={"detail": "Speaches failed"})
    result = await entity.async_process_audio_stream(metadata, audio())
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


async def test_diagnostics(hass, gateway):
    """Live check and settings, never the URL or the token."""
    gateway.require_tokens = True
    entry = await _setup(hass, data={"gateway_token": TOKEN})
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["backend"] == "gateway"
    assert result["gateway"] == {
        "status": "ok",
        "version": "18.0.0",
        "degraded": [],
        "open_mode": False,
    }
    assert result["token_configured"] is True
    assert result["language"] == "el"
    assert all(secret not in str(result) for secret in (TOKEN, "gateway.test"))
    gateway.health.side_effect = httpx.ConnectError("refused")
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["gateway"] == {"error": "gateway_unreachable"}
