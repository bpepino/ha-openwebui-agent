"""Exercise the gateway client against a simulated ``/assistant/v1`` HTTP server."""

import asyncio
import json
from pathlib import Path
import socket

import aiohttp
from aiohttp import test_utils, web
import pytest

from owui_protocol.exceptions import (
    AgentFailedError,
    AuthenticationError,
    BrowserToolUnsupported,
    CompletionTimeout,
    ConversationBusyError,
    GatewayConfigurationError,
    GatewayError,
    GatewayForbidden,
    GatewayUnauthorized,
    GatewayUnreachable,
    InvalidRequestError,
    ModelMissingError,
    NonSpeechError,
    NotFoundError,
    NotGatewayError,
    OpenWebUIError,
    PermissionDenied,
    ProtocolError,
    ResourceUnavailable,
    ToolAuthorizationRequired,
    ToolUnavailable,
    TurnStillRunning,
    UnexecutedToolCall,
    UnsupportedAPIError,
    VoiceUnavailableError,
)
from owui_protocol.gateway_client import (
    CONVERSATION_ID_RE,
    ERROR_CLASSES,
    GatewayClient,
    TurnResult,
    build_turn_body,
    turn_channel,
)

TOKEN = "test-gateway-token-not-real"
CONVERSATION = "01JHAEXAMPLE0000000000000"
REPLY = {
    "conversation_id": CONVERSATION,
    "display_text": "The office light is on. Do you want the hallway light too?",
    "speech_text": "The office light is on. Do you want the hallway light too?",
    "expects_reply": True,
    "identity": {"user_id": "maria", "display_name": "Maria", "confidence": "verified"},
    "timings": {"queue_ms": 0, "submit_ms": 41, "wait_ms": 2310, "total_ms": 2352},
    "expires_in_s": 900,
    "new_chat": True,
}
HEALTH = {
    "status": "ok",
    "openwebui": {
        "reachable": True,
        "version": "0.11.4",
        "supported_version": "0.11.4",
        "version_ok": True,
    },
    "voice_pipeline": True,
    "retention_running": True,
}
COMPONENT = Path(__file__).parents[1] / "custom_components/openwebui_agent"


class Gateway:
    """Record requests; answer with a configurable status and JSON or text body."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.turn_reply: tuple[int, object] = (200, REPLY)
        self.health_reply: tuple[int, object] = (200, HEALTH)
        self.hold = False
        self.release = asyncio.Event()
        self.url = ""

    @staticmethod
    def respond(reply: tuple[int, object]) -> web.StreamResponse:
        status, body = reply
        if status in (301, 302, 307):
            return web.Response(status=status, headers={"Location": "/elsewhere"})
        if isinstance(body, str):
            return web.Response(status=status, text=body, content_type="text/html")
        return web.json_response(body, status=status)

    async def turns(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(
            {
                "path": request.path,
                "headers": dict(request.headers),
                "body": await request.json(),
            }
        )
        if self.hold:
            await self.release.wait()
        return self.respond(self.turn_reply)

    async def health(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(
            {"path": request.path, "headers": dict(request.headers), "body": None}
        )
        return self.respond(self.health_reply)


@pytest.fixture
async def gateway():
    """Serve the simulated gateway on a local port."""
    simulated = Gateway()
    app = web.Application()
    app.router.add_post("/assistant/v1/turns", simulated.turns)
    app.router.add_get("/assistant/v1/health", simulated.health)
    server = test_utils.TestServer(app)
    await server.start_server()
    simulated.url = str(server.make_url("/"))
    yield simulated
    simulated.release.set()
    await server.close()


@pytest.fixture
async def session():
    """Open a real aiohttp session, like Home Assistant's shared one."""
    async with aiohttp.ClientSession() as http:
        yield http


@pytest.fixture
def client(gateway, session):
    """Pass real HTTP through the gateway client."""
    return GatewayClient(gateway.url, TOKEN, session, timeout=5)


async def test_voice_turn_request_and_reply(client, gateway):
    """Send only TurnBody fields with the token; map the reply."""
    result = await client.async_turn(
        "Turn on the office light",
        conversation_id=CONVERSATION,
        language="en",
        user_id="ha-user-id",
        device_id="9a8b7c6d5e4f",
        satellite_id="assist_satellite.office",
        area_id="Office",
        extra_instructions="Keep it short.",
        timeout_s=120,
    )
    request = gateway.requests[-1]
    assert request["path"] == "/assistant/v1/turns"
    assert request["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert request["headers"]["Content-Type"].startswith("application/json")
    assert request["body"] == {
        "conversation_id": CONVERSATION,
        "text": "Turn on the office light",
        "language": "en",
        "device_id": "9a8b7c6d5e4f",
        "area": "Office",
        "extra_instructions": "Keep it short.",
        "timeout_s": 120,
        "response": {"speech": False, "stream": False},
    }
    assert result == TurnResult(
        display_text=REPLY["display_text"],
        speech_text=REPLY["speech_text"],
        expects_reply=True,
        conversation_id=CONVERSATION,
        new_chat=True,
        still_running=False,
        confidence="verified",
        timings=REPLY["timings"],
    )
    assert client.last_timings == REPLY["timings"]
    assert client.last_confidence == "verified"


async def test_text_turn_omits_absent_fields(client, gateway):
    """Typed text has no device or area; nothing empty is sent."""
    gateway.turn_reply = (
        200,
        {"display_text": "Hello.", "conversation_id": "c-1", "expects_reply": False},
    )
    result = await client.async_turn("  Hello  ", conversation_id="c-1")
    assert gateway.requests[-1]["body"] == {
        "conversation_id": "c-1",
        "text": "  Hello  ",
        "response": {"speech": False, "stream": False},
    }
    # Missing speech_text falls back to the display text.
    assert result.speech_text == result.display_text == "Hello."
    assert result.expects_reply is False and result.new_chat is False
    assert turn_channel(None, None) == "text"
    assert turn_channel("device", None) == turn_channel(None, "sat") == "voice"


async def test_satellite_names_the_device_when_device_id_is_missing(client, gateway):
    """A satellite entity ID is the best device identifier HA has then."""
    await client.async_turn("Hi", satellite_id="assist_satellite.kitchen")
    assert gateway.requests[-1]["body"]["device_id"] == "assist_satellite.kitchen"


def test_body_fits_gateway_limits():
    """Normalize values the gateway would otherwise reject with a 400."""
    body = build_turn_body(
        "Hi",
        conversation_id="not a valid id!",
        language="en_US",
        area="A" * 100,
        extra_instructions="x" * 2500,
        timeout_s=5000,
    )
    assert CONVERSATION_ID_RE.match(body["conversation_id"])
    assert (
        body["conversation_id"]
        == build_turn_body("Hi", conversation_id="not a valid id!")["conversation_id"]
    )
    assert body["language"] == "en-US"
    assert len(body["area"]) == 64
    assert len(body["extra_instructions"]) == 2000
    assert body["timeout_s"] == 3600
    assert "language" not in build_turn_body("Hi", language="*")
    assert "area" not in build_turn_body("Hi", area="   ")


# Every code in the gateway's ERROR_MESSAGES (gateway/assistant/errors.py), the
# HTTP status the gateway sends with it, the exception and its translation key.
ERROR_CASES = [
    ("cannot_connect", 502, OpenWebUIError, "cannot_connect"),
    ("invalid_auth", 502, AuthenticationError, "invalid_auth"),
    ("permission_denied", 502, PermissionDenied, "permission_denied"),
    ("not_found", 502, NotFoundError, "not_found"),
    ("invalid_response", 502, ProtocolError, "invalid_response"),
    ("unsupported_api", 502, UnsupportedAPIError, "unsupported_api"),
    ("timeout", 504, CompletionTimeout, "timeout"),
    ("model_missing", 502, ModelMissingError, "model_missing"),
    ("resource_unavailable", 502, ResourceUnavailable, "resource_unavailable"),
    ("tool_unavailable", 502, ToolUnavailable, "tool_unavailable"),
    (
        "tool_authorization_required",
        502,
        ToolAuthorizationRequired,
        "tool_authorization_required",
    ),
    (
        "browser_tool_unsupported",
        502,
        BrowserToolUnsupported,
        "browser_tool_unsupported",
    ),
    ("agent_failed", 502, AgentFailedError, "agent_failed"),
    ("unexecuted_tool", 502, UnexecutedToolCall, "unexecuted_tool"),
    ("invalid_request", 400, InvalidRequestError, "invalid_request"),
    ("unauthorized", 401, GatewayUnauthorized, "gateway_unauthorized"),
    ("forbidden", 403, GatewayForbidden, "gateway_forbidden"),
    ("conversation_not_found", 404, NotFoundError, "not_found"),
    ("stt_not_found", 404, NotFoundError, "not_found"),
    ("conversation_busy", 409, ConversationBusyError, "conversation_busy"),
    ("non_speech", 422, NonSpeechError, "non_speech"),
    ("configuration_error", 500, GatewayConfigurationError, "gateway_configuration"),
    ("internal_error", 500, GatewayError, "gateway_error"),
    ("stt_failed", 502, GatewayError, "gateway_error"),
    ("voice_unavailable", 503, VoiceUnavailableError, "voice_unavailable"),
]


def test_every_gateway_code_is_mapped():
    """No gateway code falls through to a guess."""
    assert set(ERROR_CLASSES) == {case[0] for case in ERROR_CASES}


@pytest.mark.parametrize(("code", "status", "error", "key"), ERROR_CASES)
async def test_error_envelope_mapping(client, gateway, code, status, error, key):
    """Map each envelope code to the integration's exception and key."""
    gateway.turn_reply = (
        status,
        {"error": {"code": code, "message": "Short English text."}},
    )
    with pytest.raises(OpenWebUIError) as caught:
        await client.async_turn("Hi", conversation_id="c-1")
    assert type(caught.value) is error
    assert caught.value.key == key
    assert caught.value.server_message == "Short English text."
    assert caught.value.conversation_id == "c-1"
    assert TOKEN not in str(caught.value)


async def test_tool_errors_name_the_tools(client, gateway):
    """The details.resources placeholder reaches the translated message."""
    gateway.turn_reply = (
        502,
        {
            "error": {
                "code": "tool_unavailable",
                "message": "A tool the assistant needs is unavailable.",
                "details": {"resources": "home_assistant, weather"},
            }
        },
    )
    with pytest.raises(ToolUnavailable) as caught:
        await client.async_turn("Hi")
    assert caught.value.translation_placeholders == {
        "resources": "home_assistant, weather"
    }


async def test_timeout_after_acceptance_is_still_running(client, gateway):
    """The gateway's 504 with still_running must not read as 'nothing happened'."""
    gateway.turn_reply = (
        504,
        {
            "error": {
                "code": "timeout",
                "message": "The assistant is still working on that.",
                "still_running": True,
                "conversation_id": "g-7d1c",
            }
        },
    )
    with pytest.raises(TurnStillRunning) as caught:
        await client.async_turn("Turn on the heater", conversation_id="g-7d1c")
    assert isinstance(caught.value, CompletionTimeout)
    assert caught.value.key == "still_running" and caught.value.still_running
    assert caught.value.conversation_id == "g-7d1c"

    gateway.turn_reply = (504, {"error": {"code": "timeout", "message": "Slow."}})
    with pytest.raises(CompletionTimeout) as caught:
        await client.async_turn("Hi")
    assert type(caught.value) is CompletionTimeout
    assert caught.value.still_running is False


async def test_local_deadline_after_submission_is_still_running(gateway, session):
    """No reply before timeout_s + transport slack: the turn may still act."""
    client = GatewayClient(gateway.url, TOKEN, session, timeout=0.2)
    gateway.hold = True
    with pytest.raises(TurnStillRunning) as caught:
        await client.async_turn("Open the garage", conversation_id="c-9", timeout_s=0.2)
    assert caught.value.conversation_id == "c-9"
    assert len(gateway.requests) == 1  # never retried


async def test_unreachable_gateway(session):
    """Nothing was submitted when the connection fails."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    client = GatewayClient(f"http://127.0.0.1:{port}", TOKEN, session, timeout=1)
    with pytest.raises(GatewayUnreachable):
        await client.async_turn("Hi")
    with pytest.raises(GatewayUnreachable):
        await client.async_health()


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        ((502, "<html>Bad Gateway</html>"), GatewayUnreachable),
        ((503, "Service Unavailable"), GatewayUnreachable),
        ((504, "<html>Gateway Timeout</html>"), TurnStillRunning),
        ((500, "Internal Server Error"), GatewayError),
        ((401, "Unauthorized"), GatewayUnauthorized),
        ((403, "Forbidden"), GatewayForbidden),
        ((413, "Too large"), InvalidRequestError),
        ((404, "<html>Not Found</html>"), NotGatewayError),
        ((302, None), NotGatewayError),
        ((200, "<html>Open WebUI</html>"), NotGatewayError),
        ((200, ["not", "an", "object"]), NotGatewayError),
        ((200, {"status": True}), ProtocolError),
        ((200, {"display_text": "   "}), AgentFailedError),
    ],
)
async def test_answers_without_envelope(client, gateway, reply, error):
    """Proxies, wrong URLs and malformed replies map to sensible keys."""
    gateway.turn_reply = reply
    with pytest.raises(error) as caught:
        await client.async_turn("Hi", conversation_id="c-1")
    assert type(caught.value) is error


async def test_unknown_code_uses_status_and_keeps_message(client, gateway):
    """A newer gateway code still yields a key and a speakable fallback."""
    gateway.turn_reply = (
        503,
        {"error": {"code": "brand_new_code", "message": "Try again soon."}},
    )
    with pytest.raises(GatewayUnreachable) as caught:
        await client.async_turn("Hi")
    assert caught.value.server_message == "Try again soon."


async def test_health_validates_token_and_allowlists_fields(client, gateway):
    """Health sends the token and keeps only the documented fields."""
    gateway.health_reply = (200, {**HEALTH, "debug": {"internal": "value"}})
    assert await client.async_health() == HEALTH
    request = gateway.requests[-1]
    assert request["path"] == "/assistant/v1/health"
    assert request["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert client.last_health == HEALTH


async def test_health_when_open_webui_is_down(client, gateway):
    """503 with a health body: the gateway and token are fine."""
    down = {**HEALTH, "status": "ok", "openwebui": {"reachable": False}}
    gateway.health_reply = (503, down)
    health = await client.async_health()
    assert health["openwebui"]["reachable"] is False


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        (
            (401, {"error": {"code": "unauthorized", "message": "Unknown token"}}),
            GatewayUnauthorized,
        ),
        ((200, {"status": True}), NotGatewayError),
        ((200, "<html>Open WebUI</html>"), NotGatewayError),
        ((503, ""), GatewayUnreachable),
        ((404, {"detail": "Not Found"}), NotGatewayError),
    ],
)
async def test_health_errors(client, gateway, reply, error):
    """A rejected token asks for reauth; a non-gateway URL is named as such."""
    gateway.health_reply = reply
    with pytest.raises(error) as caught:
        await client.async_health()
    assert type(caught.value) is error
    assert isinstance(caught.value, AuthenticationError) == (
        error is GatewayUnauthorized
    )


async def test_close_cancels_waiting_turn(client, gateway):
    """Unload stops local waiting; later turns are refused locally."""
    gateway.hold = True
    task = asyncio.create_task(client.async_turn("Hi"))
    while not gateway.requests:
        await asyncio.sleep(0.01)
    await client.async_close()
    assert task.cancelled()
    with pytest.raises(GatewayUnreachable):
        await client.async_turn("Hi")
    assert len(gateway.requests) == 1


def test_translations_cover_gateway_keys():
    """Every key the gateway client raises has an English message."""
    strings = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))
    english = json.loads(
        (COMPONENT / "translations/en.json").read_text(encoding="utf-8")
    )
    assert strings == english
    keys = {cls.key for cls in ERROR_CLASSES.values()} | {
        TurnStillRunning.key,
        GatewayUnreachable.key,
        NotGatewayError.key,
    }
    assert keys <= set(strings["exceptions"])
    form_keys = {"gateway_unreachable", "gateway_unauthorized", "not_gateway"}
    for section in ("config", "options"):
        assert form_keys <= set(strings[section]["error"])
