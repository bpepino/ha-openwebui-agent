"""Exercise the gateway client against a simulated gateway at the HTTP layer (respx)."""

import asyncio
from email.parser import BytesParser
from email.policy import HTTP
import json
from pathlib import Path

import httpx
import pytest
import respx

from owui_protocol import gateway_client as gateway_module
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
    TokenRequired,
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
    GatewayStatus,
    TurnResult,
    build_turn_body,
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
    "version": "18.0.0",
    "checked_at": "2026-11-01T10:00:00+00:00",
    "checks": {
        "config": {"status": "ok"},
        "stt": {"status": "ok", "url": "http://speaches.local/v1/models"},
        "llm": {"status": "ok", "url": "http://ninfer.local/v1/models"},
    },
}
NOT_FOUND = {
    "error": {"code": "conversation_not_found", "message": "Unknown conversation."}
}
UNAUTHORIZED = {"error": {"code": "unauthorized", "message": "Missing bearer token"}}
COMPONENT = Path(__file__).parents[1] / "custom_components/openwebui_agent"


URL = "http://gateway.test:8000"


def multipart_fields(request: httpx.Request) -> dict:
    """Decode a multipart/form-data body into {name: value or (filename, type, bytes)}."""
    header = f"Content-Type: {request.headers['Content-Type']}\r\n\r\n"
    message = BytesParser(policy=HTTP).parsebytes(header.encode() + request.content)
    fields = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        payload = part.get_payload(decode=True)
        if part.get_filename():
            fields[name] = (part.get_filename(), part.get_content_type(), payload)
        else:
            fields[name] = payload.decode()
    return fields


class Gateway:
    """Record requests; answer with configurable statuses and bodies."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.turn_reply: tuple[int, object] = (200, REPLY)
        self.polls: list[tuple[int, object]] = []
        self.health_reply: tuple[int, object] = (200, HEALTH)
        self.probe_reply: tuple[int, object] | None = None
        self.require_tokens = False
        self.stt_reply: tuple[int, object] = (200, {"text": "hello"})
        self.speech_reply = b"ID3-fake-mp3"
        self.down = False
        self.hold = False
        self.release = asyncio.Event()
        self.url = URL

    @staticmethod
    def respond(reply: tuple[int, object]) -> httpx.Response:
        status, body = reply
        if status in (301, 302, 307):
            return httpx.Response(status, headers={"Location": "/elsewhere"})
        if isinstance(body, str):
            return httpx.Response(
                status, text=body, headers={"Content-Type": "text/html"}
            )
        return httpx.Response(status, json=body)

    def record(self, request: httpx.Request, body: object) -> None:
        if self.down:
            raise httpx.ConnectError("Connection refused", request=request)
        self.requests.append(
            {
                "path": request.url.raw_path.decode(),
                "headers": request.headers,
                "body": body,
            }
        )

    async def turns(self, request: httpx.Request) -> httpx.Response:
        self.record(request, json.loads(request.content))
        if self.hold:
            await self.release.wait()
        return self.respond(self.turn_reply)

    def poll(self, request: httpx.Request) -> httpx.Response:
        self.record(request, None)
        return self.respond(self.polls.pop(0))

    def health(self, request: httpx.Request) -> httpx.Response:
        self.record(request, None)
        return self.respond(self.health_reply)

    def probe(self, request: httpx.Request) -> httpx.Response:
        """Open mode accepts any request; otherwise only the known token."""
        self.record(request, None)
        if self.probe_reply:
            return self.respond(self.probe_reply)
        if self.require_tokens and (
            request.headers.get("Authorization") != f"Bearer {TOKEN}"
        ):
            return self.respond((401, UNAUTHORIZED))
        return self.respond((404, NOT_FOUND))

    def transcriptions(self, request: httpx.Request) -> httpx.Response:
        self.record(request, multipart_fields(request))
        return self.respond(self.stt_reply)

    def speech(self, request: httpx.Request) -> httpx.Response:
        self.record(request, json.loads(request.content))
        return httpx.Response(
            200, content=self.speech_reply, headers={"Content-Type": "audio/mpeg"}
        )


@pytest.fixture
def gateway():
    """Serve the simulated gateway through respx; no sockets are opened."""
    simulated = Gateway()
    with respx.mock(base_url=URL, assert_all_called=False) as mock:
        mock.get("/health").side_effect = simulated.health
        mock.post("/assistant/v1/turns").side_effect = simulated.turns
        mock.route(
            method="GET", path__regex=r"^/assistant/v1/turns/.+$"
        ).side_effect = simulated.poll
        mock.get(
            "/assistant/v1/conversations/ha-auth-probe"
        ).side_effect = simulated.probe
        mock.post("/v1/audio/transcriptions").side_effect = simulated.transcriptions
        mock.post("/v1/audio/speech").side_effect = simulated.speech
        yield simulated
    simulated.release.set()


@pytest.fixture
async def http():
    """Use a real httpx client, like Home Assistant's shared one."""
    async with httpx.AsyncClient() as shared:
        yield shared


@pytest.fixture
def client(gateway, http):
    """Pass real HTTP through the gateway client."""
    return GatewayClient(gateway.url, TOKEN, http, timeout=5)


@pytest.fixture
def fast_polling(monkeypatch):
    """Poll without waiting a whole second."""
    monkeypatch.setattr(gateway_module, "POLL_INTERVAL_S", 0.01)


async def test_turn_request_and_reply(client, gateway):
    """Send only TurnBody fields with the token; map the reply."""
    result = await client.async_turn(
        "Turn on the office light",
        conversation_id=CONVERSATION,
        language="en",
        device_id="9a8b7c6d5e4f",
        area="Office",
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


async def test_open_mode_sends_no_token(gateway, http):
    """Without a configured token no Authorization header is sent at all."""
    client = GatewayClient(gateway.url, "  ", http, timeout=5)
    assert not client.has_token
    await client.async_turn("Hi", conversation_id="c-1")
    await client.async_transcribe(b"RIFF", language="en")
    await client.async_speech("Hi", voice=None, response_format="mp3")
    status = await client.async_check()
    assert status.open_mode is True
    assert all("Authorization" not in r["headers"] for r in gateway.requests)


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


def test_body_fits_gateway_limits():
    """Normalize values the gateway would otherwise reject with a 400."""
    body = build_turn_body(
        "Hi",
        conversation_id="not a valid id!",
        language="en_US",
        device_id="d" * 200,
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
    assert len(body["device_id"]) == 128
    assert len(body["area"]) == 64
    assert len(body["extra_instructions"]) == 2000
    assert body["timeout_s"] == 3600
    assert "language" not in build_turn_body("Hi", language="*")
    assert "area" not in build_turn_body("Hi", area="   ")


async def test_still_running_turn_is_polled(client, gateway, fast_polling):
    """A 200 with still_running is polled at /turns/{turn_id} until it finishes."""
    gateway.turn_reply = (
        200,
        {"still_running": True, "turn_id": "t/1", "conversation_id": "gw-9"},
    )
    # Later answers may leave out the turn and conversation the first one named.
    final = {key: value for key, value in REPLY.items() if key != "conversation_id"}
    gateway.polls = [
        (200, {"still_running": True}),
        (200, {**final, "still_running": False}),
    ]
    result = await client.async_turn("Run the long task", conversation_id=CONVERSATION)
    assert result.display_text == REPLY["display_text"]
    assert result.still_running is False
    assert result.conversation_id == client.conversations[CONVERSATION] == "gw-9"
    paths = [r["path"] for r in gateway.requests]
    assert paths == [
        "/assistant/v1/turns",
        "/assistant/v1/turns/t%2F1",
        "/assistant/v1/turns/t%2F1",
    ]
    assert gateway.requests[-1]["headers"]["Authorization"] == f"Bearer {TOKEN}"


async def test_polling_stops_at_the_local_deadline(gateway, http, fast_polling):
    """Past timeout_s plus transport slack the turn is reported as still running."""
    client = GatewayClient(gateway.url, TOKEN, http, timeout=0.05)
    running = (200, {"still_running": True, "turn_id": "t1"})
    gateway.turn_reply = running
    gateway.polls = [running] * 100
    with pytest.raises(TurnStillRunning) as caught:
        await client.async_turn("Open the garage", conversation_id="c-9", timeout_s=0.1)
    assert caught.value.conversation_id == "c-9"
    assert 1 <= len(gateway.polls) < 100  # polled, then gave up; never resubmitted
    assert [r["path"] for r in gateway.requests].count("/assistant/v1/turns") == 1


async def test_still_running_without_turn_id(client, gateway, fast_polling):
    """Nothing to poll: the accepted turn may still act, so say so."""
    gateway.turn_reply = (200, {"still_running": True, "conversation_id": "c-2"})
    with pytest.raises(TurnStillRunning):
        await client.async_turn("Hi", conversation_id="c-2")


async def test_failed_poll_turns_into_mapped_error(client, gateway, fast_polling):
    """A turn that fails while polled raises the gateway's error."""
    gateway.turn_reply = (200, {"still_running": True, "turn_id": "t1"})
    gateway.polls = [(502, {"error": {"code": "agent_failed", "message": "No."}})]
    with pytest.raises(AgentFailedError):
        await client.async_turn("Hi", conversation_id="c-3")


async def test_conversation_mapping_and_new_chat(client, gateway):
    """HA's ID maps to the gateway's; new_chat replaces the mapping."""
    gateway.turn_reply = (
        200,
        {**REPLY, "conversation_id": "gw-1", "new_chat": True},
    )
    await client.async_turn("One", conversation_id="ha-1")
    assert gateway.requests[-1]["body"]["conversation_id"] == "ha-1"
    gateway.turn_reply = (200, {**REPLY, "conversation_id": "gw-1", "new_chat": False})
    await client.async_turn("Two", conversation_id="ha-1")
    assert gateway.requests[-1]["body"]["conversation_id"] == "gw-1"
    # A follow-up that is not a new chat never moves the mapping.
    gateway.turn_reply = (200, {**REPLY, "conversation_id": "gw-x", "new_chat": False})
    await client.async_turn("Three", conversation_id="ha-1")
    assert client.conversations["ha-1"] == "gw-1"
    gateway.turn_reply = (200, {**REPLY, "conversation_id": "gw-2", "new_chat": True})
    await client.async_turn("Four", conversation_id="ha-1")
    await client.async_turn("Five", conversation_id="ha-1")
    assert gateway.requests[-1]["body"]["conversation_id"] == "gw-2"
    # Other HA conversations are isolated.
    await client.async_turn("Elsewhere", conversation_id="ha-2")
    assert gateway.requests[-1]["body"]["conversation_id"] == "ha-2"


async def test_mapping_is_bounded(client, gateway, monkeypatch):
    """Old conversations are forgotten first."""
    monkeypatch.setattr(gateway_module, "MAX_CONVERSATIONS", 2)
    for ha_id in ("a", "b", "c"):
        await client.async_turn("Hi", conversation_id=ha_id)
    assert list(client.conversations) == ["b", "c"]


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


async def test_local_deadline_after_submission_is_still_running(gateway, http):
    """No reply before timeout_s + transport slack: the turn may still act."""
    client = GatewayClient(gateway.url, TOKEN, http, timeout=0.2)
    gateway.hold = True
    with pytest.raises(TurnStillRunning) as caught:
        await client.async_turn("Open the garage", conversation_id="c-9", timeout_s=0.2)
    assert caught.value.conversation_id == "c-9"
    assert len(gateway.requests) == 1  # never retried


async def test_unreachable_gateway(client, gateway):
    """Nothing was submitted when the connection fails."""
    gateway.down = True
    with pytest.raises(GatewayUnreachable):
        await client.async_turn("Hi")
    with pytest.raises(GatewayUnreachable):
        await client.async_check()
    with pytest.raises(GatewayUnreachable):
        await client.async_transcribe(b"RIFF", language="en")
    assert not gateway.requests


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


async def test_check_in_open_mode(client, gateway):
    """Health is read without a token; the probe without a token is accepted."""
    gateway.health_reply = (
        200,
        {
            **HEALTH,
            "status": "degraded",
            "checks": {**HEALTH["checks"], "tts": {"status": "error", "error": "x"}},
        },
    )
    status = await client.async_check()
    assert status == GatewayStatus(
        status="degraded", version="18.0.0", degraded=["tts"], open_mode=True
    )
    assert client.last_status == status
    health, probe = gateway.requests
    assert health["path"] == "/health"
    assert probe["path"] == "/assistant/v1/conversations/ha-auth-probe"
    assert "Authorization" not in health["headers"]
    assert "Authorization" not in probe["headers"]


async def test_check_with_required_tokens(client, gateway):
    """Tokens required: the configured token is validated by a second probe."""
    gateway.require_tokens = True
    status = await client.async_check()
    assert status.open_mode is False
    assert gateway.requests[-1]["headers"]["Authorization"] == f"Bearer {TOKEN}"


async def test_check_rejects_missing_and_wrong_tokens(gateway, http):
    """A missing token and a rejected one both ask for reauthentication."""
    gateway.require_tokens = True
    with pytest.raises(TokenRequired) as caught:
        await GatewayClient(gateway.url, "", http).async_check()
    assert isinstance(caught.value, AuthenticationError)
    assert caught.value.key == "token_required"
    with pytest.raises(GatewayUnauthorized) as caught:
        await GatewayClient(gateway.url, "wrong-token", http).async_check()
    assert type(caught.value) is GatewayUnauthorized


@pytest.mark.parametrize(
    ("health", "probe", "error"),
    [
        ((200, {"status": True}), None, NotGatewayError),
        ((200, "<html>Open WebUI</html>"), None, NotGatewayError),
        ((404, {"detail": "Not Found"}), None, NotGatewayError),
        ((503, ""), None, GatewayUnreachable),
        ((200, HEALTH), (404, {"detail": "Not Found"}), NotGatewayError),
        ((200, HEALTH), (403, {"error": {"code": "forbidden"}}), GatewayForbidden),
    ],
)
async def test_check_errors(client, gateway, health, probe, error):
    """A non-gateway URL is named as such; a scope problem is not 'unreachable'."""
    gateway.health_reply = health
    gateway.probe_reply = probe
    with pytest.raises(error) as caught:
        await client.async_check()
    assert type(caught.value) is error


@pytest.mark.parametrize(
    ("reply", "text"),
    [
        (
            {"text": "Billo: ανάψε το φως", "fixed_greek": "άναψε το φως"},
            "άναψε το φως",
        ),
        ({"text": "turn on the light", "fixed_greek": ""}, "turn on the light"),
        ({"text": "  turn on the light  "}, "turn on the light"),
        ({"text": ""}, ""),
    ],
)
async def test_transcription(client, gateway, reply, text):
    """Multipart upload; the repaired transcript wins over the plain text."""
    gateway.stt_reply = (200, reply)
    result = await client.async_transcribe(
        b"RIFF-audio", language="el", filename="audio.wav", content_type="audio/wav"
    )
    assert result == text
    request = gateway.requests[-1]
    assert request["path"] == "/v1/audio/transcriptions"
    assert request["headers"]["Content-Type"].startswith("multipart/form-data")
    assert request["body"] == {
        "model": "whisper-1",
        "response_format": "json",
        "language": "el",
        "file": ("audio.wav", "audio/wav", b"RIFF-audio"),
    }


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        ((400, {"detail": "Unsupported response_format."}), InvalidRequestError),
        ((502, {"detail": "Speaches failed"}), GatewayUnreachable),
        ((200, ["no"]), NotGatewayError),
    ],
)
async def test_transcription_errors(client, gateway, reply, error):
    """The voice routes answer FastAPI errors without the assistant envelope."""
    gateway.stt_reply = reply
    with pytest.raises(error):
        await client.async_transcribe(b"RIFF", language=None)
    assert "language" not in gateway.requests[-1]["body"]


async def test_speech(client, gateway):
    """OpenAI JSON in, audio bytes out; an empty voice is left to the gateway."""
    audio = await client.async_speech("Hello", voice="", response_format="opus")
    assert audio == b"ID3-fake-mp3"
    request = gateway.requests[-1]
    assert request["path"] == "/v1/audio/speech"
    assert request["body"] == {
        "model": "tts-1",
        "input": "Hello",
        "response_format": "opus",
    }
    await client.async_speech("Hello", voice="maria", response_format="mp3")
    assert gateway.requests[-1]["body"]["voice"] == "maria"
    gateway.speech_reply = b""
    with pytest.raises(GatewayError):
        await client.async_speech("Hello", voice=None, response_format="mp3")


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
        TokenRequired.key,
    }
    assert keys <= set(strings["exceptions"])
    form_keys = {
        "gateway_unreachable",
        "gateway_unauthorized",
        "gateway_forbidden",
        "not_gateway",
        "token_required",
        "invalid_url",
    }
    assert form_keys <= set(strings["config"]["error"])
