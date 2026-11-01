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
        # Server-sent events answered instead of turn_reply (a streamed turn).
        self.turn_events: list[tuple[str, dict]] | None = None
        self.polls: list[tuple[int, object]] = []
        self.health_reply: tuple[int, object] = (200, HEALTH)
        self.probe_reply: tuple[int, object] | None = None
        self.token_probe_reply: tuple[int, object] | None = None
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
        if self.turn_events is not None:
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                content=sse(self.turn_events),
            )
        return self.respond(self.turn_reply)

    def poll(self, request: httpx.Request) -> httpx.Response:
        self.record(request, None)
        reply = self.polls.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return self.respond(reply)

    def health(self, request: httpx.Request) -> httpx.Response:
        self.record(request, None)
        return self.respond(self.health_reply)

    def probe(self, request: httpx.Request) -> httpx.Response:
        """Open mode accepts any request; otherwise only the known token."""
        self.record(request, None)
        if self.probe_reply:
            return self.respond(self.probe_reply)
        if self.token_probe_reply and request.headers.get("Authorization"):
            return self.respond(self.token_probe_reply)
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
        if isinstance(self.speech_reply, tuple):
            return self.respond(self.speech_reply)
        return httpx.Response(
            200, content=self.speech_reply, headers={"Content-Type": "audio/mpeg"}
        )


def sse(events: list[tuple[str, dict]]) -> bytes:
    """Encode events like the gateway (``event:``, one JSON ``data:`` line)."""
    return "".join(
        f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        for event, data in events
    ).encode()


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
    """Poll without waiting two seconds."""
    monkeypatch.setattr(gateway_module, "POLL_INTERVAL_S", 0.01)


def still_running(turn_id: str | None = "t-1", **fields) -> tuple[int, dict]:
    """Answer like the gateway when an accepted turn passed its deadline (504)."""
    error = {
        "code": "timeout",
        "message": "The assistant is still working on that.",
        "still_running": True,
        **fields,
    }
    if turn_id:
        error["turn_id"] = turn_id
    return 504, {"error": error}


RUNNING = (202, {"turn_id": "t-1", "status": "running"})


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


async def test_still_running_turn_is_followed_up(client, gateway, fast_polling):
    """The 504 envelope's turn_id is polled at /turns/{turn_id} until it is done."""
    gateway.turn_reply = still_running("t/1", conversation_id="gw-9")
    gateway.polls = [
        (202, {"turn_id": "t/1", "status": "running"}),
        (200, {"turn_id": "t/1", "status": "done", "result": REPLY}),
    ]
    result = await client.async_turn("Run the long task", conversation_id=CONVERSATION)
    assert result.display_text == REPLY["display_text"]
    assert result.speech_text == REPLY["speech_text"]
    assert result.expects_reply is True
    assert client.conversations[CONVERSATION] == CONVERSATION
    paths = [r["path"] for r in gateway.requests]
    assert paths == [
        "/assistant/v1/turns",
        "/assistant/v1/turns/t%2F1",
        "/assistant/v1/turns/t%2F1",
    ]
    assert gateway.requests[-1]["headers"]["Authorization"] == f"Bearer {TOKEN}"


async def test_followed_up_answer_without_conversation(client, gateway, fast_polling):
    """A result without conversation_id keeps the one the 504 envelope named."""
    gateway.turn_reply = still_running(conversation_id="gw-9")
    final = {key: value for key, value in REPLY.items() if key != "conversation_id"}
    gateway.polls = [(200, {"status": "done", "result": final})]
    result = await client.async_turn("Run the long task", conversation_id="ha-1")
    assert result.conversation_id == client.conversations["ha-1"] == "gw-9"


async def test_polling_stops_after_the_turn_timeout(gateway, http, fast_polling):
    """Polling lasts at most the turn timeout; the turn is never resubmitted."""
    client = GatewayClient(gateway.url, TOKEN, http, timeout=5)
    gateway.turn_reply = still_running()
    gateway.polls = [RUNNING] * 1000
    with pytest.raises(TurnStillRunning) as caught:
        await client.async_turn("Open the garage", conversation_id="c-9", timeout_s=0.1)
    assert caught.value.conversation_id == "c-9"
    assert 1 <= 1000 - len(gateway.polls) < 1000
    assert [r["path"] for r in gateway.requests].count("/assistant/v1/turns") == 1


async def test_still_running_without_turn_id(client, gateway, fast_polling):
    """Nothing to poll: the accepted turn may still act, so say so."""
    gateway.turn_reply = still_running(None, conversation_id="c-2")
    with pytest.raises(TurnStillRunning) as caught:
        await client.async_turn("Hi", conversation_id="c-2")
    assert caught.value.turn_id is None
    assert [r["path"] for r in gateway.requests] == ["/assistant/v1/turns"]


async def test_failed_turn_raises_its_error(client, gateway, fast_polling):
    """A followed-up turn that failed raises the gateway's error."""
    gateway.turn_reply = still_running()
    gateway.polls = [
        RUNNING,
        (
            200,
            {
                "turn_id": "t-1",
                "status": "failed",
                "error": {"code": "agent_failed", "message": "No answer."},
            },
        ),
    ]
    with pytest.raises(AgentFailedError) as caught:
        await client.async_turn("Hi", conversation_id="c-3")
    assert caught.value.server_message == "No answer."
    gateway.turn_reply = still_running()
    gateway.polls = [(200, {"status": "failed"})]
    with pytest.raises(GatewayError):
        await client.async_turn("Hi", conversation_id="c-3")


@pytest.mark.parametrize(
    "poll",
    [
        (404, {"error": {"code": "turn_not_found", "message": "Expired."}}),
        httpx.ConnectError("refused"),
        (502, "<html>Bad Gateway</html>"),
        (401, UNAUTHORIZED),
        (200, {"status": "something new"}),
    ],
)
async def test_polling_not_possible_is_still_running(
    client, gateway, fast_polling, poll
):
    """An unknown or expired turn, or a failed follow-up: it may still act."""
    gateway.turn_reply = still_running(conversation_id="c-4")
    gateway.polls = [poll]
    with pytest.raises(TurnStillRunning) as caught:
        await client.async_turn("Hi", conversation_id="c-4")
    assert caught.value.conversation_id == "c-4"
    assert [r["path"] for r in gateway.requests].count("/assistant/v1/turns") == 1


async def test_degraded_answer(client, gateway):
    """The gateway's degraded flag is kept; a normal answer has none."""
    result = await client.async_turn("Hi", conversation_id="c-1")
    assert result.degraded is False
    gateway.turn_reply = (
        200,
        {
            **REPLY,
            "speech_text": "Assistant offline, basic commands only. Turned on the light.",
            "degraded": True,
        },
    )
    result = await client.async_turn("Turn on the light", conversation_id="c-1")
    assert result.degraded is True
    assert result.speech_text.startswith("Assistant offline")
    assert sum(r["path"] == "/assistant/v1/turns" for r in gateway.requests) == 2


@pytest.mark.parametrize(
    ("reply", "unreachable"),
    [
        (None, True),  # connection refused or DNS failure
        ((502, "<html>Bad Gateway</html>"), True),  # a proxy
        ((503, "Service Unavailable"), True),
        ((500, "Internal Server Error"), True),
        ((502, {"error": {"code": "cannot_connect", "message": "x"}}), False),
        ((500, {"error": {"code": "internal_error", "message": "x"}}), False),
        ((503, {"error": {"code": "brand_new_code", "message": "x"}}), False),
        ((401, UNAUTHORIZED), False),
        ((404, "<html>Not Found</html>"), False),
        ((504, "<html>Gateway Timeout</html>"), False),  # may still act
        ((504, {"error": {"code": "timeout", "still_running": True}}), False),
    ],
)
async def test_unreachable_is_marked(client, gateway, reply, unreachable):
    """Only a gateway that cannot be reached is marked unreachable (fallback)."""
    if reply is None:
        gateway.down = True
    else:
        gateway.turn_reply = reply
    with pytest.raises(OpenWebUIError) as caught:
        await client.async_turn("Hi", conversation_id="c-1")
    assert caught.value.unreachable is unreachable


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
    assert caught.value.turn_id is None

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
    # A gateway whose health does not say whether it runs in open mode is probed.
    health, token_probe, probe = gateway.requests
    assert health["path"] == "/health"
    assert (
        probe["path"]
        == token_probe["path"]
        == ("/assistant/v1/conversations/ha-auth-probe")
    )
    assert "Authorization" not in health["headers"]
    assert "Authorization" not in probe["headers"]
    # A configured token is checked in open mode too (its scope may be missing).
    assert token_probe["headers"]["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize(
    ("open_mode", "token", "probes", "error"),
    [
        (True, TOKEN, [TOKEN], None),
        (True, "", [None], None),
        (False, TOKEN, [TOKEN], None),
        (False, "", [None], TokenRequired),
    ],
)
async def test_check_reads_open_mode_from_health(
    gateway, http, open_mode, token, probes, error
):
    """Health says whether a token is needed; one probe checks the access."""
    gateway.require_tokens = not open_mode
    gateway.health_reply = (
        200,
        {
            **HEALTH,
            "open_mode": open_mode,
            "degraded_mode": {"enabled": True, "offline": False, "reason": "x"},
        },
    )
    client = GatewayClient(gateway.url, token, http)
    if error:
        with pytest.raises(error):
            await client.async_check()
    else:
        status = await client.async_check()
        assert status.open_mode is open_mode
        # Only the allowlisted degraded-mode fields are kept.
        assert status.degraded_mode == {"enabled": True, "offline": False}
    sent = [
        r["headers"].get("Authorization", "").removeprefix("Bearer ") or None
        for r in gateway.requests[1:]
    ]
    assert sent == probes


async def test_check_in_open_mode_without_token(gateway, http):
    """Open mode without a token: health and one tokenless probe."""
    status = await GatewayClient(gateway.url, "", http).async_check()
    assert status.open_mode is True
    assert len(gateway.requests) == 2
    assert all("Authorization" not in r["headers"] for r in gateway.requests)


async def test_check_token_without_scope_is_forbidden(client, gateway):
    """Open mode accepts no token, but a token without assistant:turns is a 403."""
    gateway.token_probe_reply = (403, {"error": {"code": "forbidden"}})
    with pytest.raises(GatewayForbidden):
        await client.async_check()


async def test_check_with_required_tokens(client, gateway):
    """Tokens required: the configured token is validated by a second probe."""
    gateway.require_tokens = True
    status = await client.async_check()
    assert status.open_mode is False
    assert gateway.requests[1]["headers"]["Authorization"] == f"Bearer {TOKEN}"


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
            {
                "text": "Alex: anapse to fos tis kouzinas",
                "plain_text": "anapse to fos tis kouzinas",
                "fixed_greek": "άναψε το φως της κουζίνας",
            },
            "anapse to fos tis kouzinas",
        ),
        (
            {"text": "turn on the light", "fixed_greek": "άναψε το φως"},
            "turn on the light",
        ),
        ({"text": "x", "plain_text": "  "}, "x"),
        ({"text": "  turn on the light  "}, "turn on the light"),
        ({"text": "", "fixed_greek": "άναψε"}, ""),
    ],
)
async def test_transcription(client, gateway, reply, text):
    """Multipart upload; plain_text, then text, never the repaired fixed_greek."""
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


FINAL = {
    **REPLY,
    "display_text": "It is sunny. Twenty degrees. Anything else?",
    "speech_text": "It is sunny. Twenty degrees. Anything else?",
    "continue_conversation": True,
    "path": "direct",
    "turn_id": "t-9",
}
ACCEPTED = (
    "status",
    {
        "phase": "accepted",
        "conversation_id": "gw-9",
        "turn_id": "t-9",
        "path": "direct",
    },
)


async def test_streamed_turn(client, gateway):
    """Each delta is passed on as it arrives; final is the result."""
    gateway.turn_events = [
        ACCEPTED,
        (
            "delta",
            {
                "conversation_id": "gw-9",
                "turn_id": "t-9",
                "index": 0,
                "text": "It is sunny. ",
            },
        ),
        (
            "delta",
            {
                "conversation_id": "gw-9",
                "turn_id": "t-9",
                "index": 1,
                "text": "Twenty degrees. ",
            },
        ),
        ("final", FINAL),
    ]
    deltas: list[str] = []
    result = await client.async_turn(
        "What's the weather?", conversation_id=CONVERSATION, on_delta=deltas.append
    )
    assert deltas == ["It is sunny. ", "Twenty degrees. "]
    assert result.display_text == FINAL["display_text"]
    assert result.continue_conversation is True and result.path == "direct"
    assert client.last_path == "direct"
    request = gateway.requests[-1]
    assert request["body"]["response"] == {"speech": False, "stream": True}
    assert request["headers"]["Accept"].startswith("text/event-stream")
    assert request["headers"]["Authorization"] == f"Bearer {TOKEN}"


async def test_streamed_turn_without_deltas(client, gateway):
    """The Open WebUI path streams only status events, then final."""
    gateway.turn_events = [
        ("status", {"phase": "accepted", "conversation_id": "gw-9"}),
        ("status", {"phase": "waiting", "conversation_id": "gw-9", "elapsed_ms": 2000}),
        ("final", {**REPLY, "path": "openwebui", "continue_conversation": False}),
    ]
    deltas: list[str] = []
    result = await client.async_turn(
        "Hi", conversation_id="c-1", on_delta=deltas.append
    )
    assert deltas == []
    assert result.path == "openwebui" and result.continue_conversation is False


async def test_streamed_turn_still_running_is_followed_up(
    client, gateway, fast_polling
):
    """An error event with still_running and a turn_id is polled like the 504."""
    gateway.turn_events = [
        ACCEPTED,
        ("delta", {"index": 0, "text": "Working on it. "}),
        (
            "error",
            {
                "error": {
                    "code": "timeout",
                    "message": "Still working.",
                    "still_running": True,
                    "turn_id": "t-9",
                    "conversation_id": "gw-9",
                }
            },
        ),
    ]
    gateway.polls = [RUNNING, (200, {"status": "done", "result": FINAL})]
    deltas: list[str] = []
    result = await client.async_turn(
        "Hi", conversation_id="c-1", on_delta=deltas.append
    )
    assert deltas == ["Working on it. "]
    assert result.display_text == FINAL["display_text"]
    assert [r["path"] for r in gateway.requests] == [
        "/assistant/v1/turns",
        "/assistant/v1/turns/t-9",
        "/assistant/v1/turns/t-9",
    ]


async def test_streamed_turn_error_event(client, gateway):
    """An error event raises the gateway's error."""
    gateway.turn_events = [
        ACCEPTED,
        ("error", {"error": {"code": "agent_failed", "message": "No answer."}}),
    ]
    with pytest.raises(AgentFailedError) as caught:
        await client.async_turn("Hi", conversation_id="c-1", on_delta=[].append)
    assert caught.value.server_message == "No answer."
    assert caught.value.unreachable is False
    gateway.turn_events = [("error", {"error": {"code": "brand_new", "message": "x"}})]
    with pytest.raises(GatewayError):
        await client.async_turn("Hi", conversation_id="c-1", on_delta=[].append)


async def test_stream_that_ends_without_an_answer(client, gateway, fast_polling):
    """It may still act: followed up with its turn_id, else still running."""
    gateway.turn_events = [ACCEPTED, ("delta", {"text": "Half. "})]
    gateway.polls = [(200, {"status": "done", "result": FINAL})]
    result = await client.async_turn("Hi", conversation_id="c-1", on_delta=[].append)
    assert result.display_text == FINAL["display_text"]
    gateway.turn_events = [("status", {"phase": "accepted"})]
    with pytest.raises(TurnStillRunning) as caught:
        await client.async_turn("Hi", conversation_id="c-1", on_delta=[].append)
    assert caught.value.turn_id is None
    assert [r["path"] for r in gateway.requests].count("/assistant/v1/turns") == 2


async def test_stream_request_answered_like_before(client, gateway):
    """A gateway that answers JSON, or an error status, is read as before."""
    deltas: list[str] = []
    result = await client.async_turn(
        "Hi", conversation_id="c-1", on_delta=deltas.append
    )
    assert result.display_text == REPLY["display_text"] and deltas == []
    # REPLY has no continue_conversation: an older gateway.
    assert result.continue_conversation is None and result.path is None
    gateway.turn_reply = (401, UNAUTHORIZED)
    with pytest.raises(GatewayUnauthorized):
        await client.async_turn("Hi", conversation_id="c-1", on_delta=deltas.append)
    gateway.turn_reply = (502, "<html>Bad Gateway</html>")
    with pytest.raises(GatewayUnreachable) as caught:
        await client.async_turn("Hi", conversation_id="c-1", on_delta=deltas.append)
    assert caught.value.unreachable is True
    gateway.down = True
    with pytest.raises(GatewayUnreachable) as caught:
        await client.async_turn("Hi", conversation_id="c-1", on_delta=deltas.append)
    assert caught.value.unreachable is True


async def test_continue_conversation_is_read_as_sent(client, gateway):
    """Only a boolean counts; anything else means the gateway did not say."""
    gateway.turn_reply = (200, {**REPLY, "continue_conversation": False})
    assert (await client.async_turn("Hi")).continue_conversation is False
    gateway.turn_reply = (200, {**REPLY, "continue_conversation": "yes"})
    assert (await client.async_turn("Hi")).continue_conversation is None


async def test_speech_stream(client, gateway):
    """One sentence per request; the audio is yielded as it arrives."""
    audio = b"".join(
        [
            chunk
            async for chunk in client.async_speech_stream(
                "It is sunny.", voice="maria", response_format="mp3"
            )
        ]
    )
    assert audio == b"ID3-fake-mp3"
    assert gateway.requests[-1]["body"] == {
        "model": "tts-1",
        "input": "It is sunny.",
        "response_format": "mp3",
        "voice": "maria",
    }
    gateway.speech_reply = b""
    with pytest.raises(GatewayError):
        async for _ in client.async_speech_stream(
            "x", voice=None, response_format="mp3"
        ):
            pass
    gateway.speech_reply = (503, "Service Unavailable")
    with pytest.raises(GatewayUnreachable):
        async for _ in client.async_speech_stream(
            "x", voice=None, response_format="mp3"
        ):
            pass


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
