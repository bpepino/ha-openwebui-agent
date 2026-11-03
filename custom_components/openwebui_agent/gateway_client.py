"""Thin client for the home AI gateway: health, assistant turns, speech in and out.

In gateway mode the gateway owns the Open WebUI protocol, the model/tool
profile, identity, conversation continuation, chat retention and the voice
pipeline. This client submits requests and maps the replies, or the gateway's
error envelope, onto this integration's exception keys so the existing
translations keep working.

The token is optional: in the gateway's open mode (its default) requests need
none, so the ``Authorization`` header is sent only when a token is configured.

No retries: an accepted turn may act on the real world. A turn whose
deadline passed after the gateway accepted it (504 ``still_running`` with a
``turn_id``) is followed up at ``GET /assistant/v1/turns/{turn_id}``.

Streamed turns (``response.stream``) answer server-sent events: ``status``,
one ``delta`` per finished sentence (the gateway's direct path only), then one
``final`` (the full answer) or ``error`` (the error envelope).
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import dataclass, field
import hashlib
import json
import re
from time import monotonic
from typing import Any
from urllib.parse import quote

import httpx

from .ask_action import ASK_ACTION_PATH
from .client import normalize_url
from .const import LOGGER
from .exceptions import (
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

HEALTH_PATH = "/health"
API_PREFIX = "/assistant/v1"
TURNS_PATH = f"{API_PREFIX}/turns"
# Reading a conversation nobody owns is a side-effect-free authentication probe:
# 404 means the request was accepted, 401 that the gateway requires a token.
PROBE_PATH = f"{API_PREFIX}/conversations/ha-auth-probe"
TRANSCRIPTIONS_PATH = "/v1/audio/transcriptions"
SPEECH_PATH = "/v1/audio/speech"
# The gateway picks its own engines; these names only satisfy the OpenAI format.
STT_MODEL = "whisper-1"
TTS_MODEL = "tts-1"
VOICE_TIMEOUT_S = 60
POLL_INTERVAL_S = 2.0
MAX_CONVERSATIONS = 256

# Limits of the gateway's TurnBody. Values outside them would turn a whole
# voice request into a 400, so they are normalized here instead.
CONVERSATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{1,8})*$")
MAX_DEVICE_ID_CHARS = 128
MAX_AREA_CHARS = 64
MAX_INSTRUCTIONS_CHARS = 2000
MAX_TIMEOUT_S = 3600
DEFAULT_TURN_TIMEOUT_S = 120

# Gateway error codes -> this integration's exceptions (and translation keys).
ERROR_CLASSES: dict[str, type[OpenWebUIError]] = {
    # Open WebUI failures reported by the gateway: same keys as direct mode.
    "cannot_connect": OpenWebUIError,
    "invalid_auth": AuthenticationError,
    "permission_denied": PermissionDenied,
    "not_found": NotFoundError,
    "invalid_response": ProtocolError,
    "unsupported_api": UnsupportedAPIError,
    "timeout": CompletionTimeout,
    "model_missing": ModelMissingError,
    "resource_unavailable": ResourceUnavailable,
    "tool_unavailable": ToolUnavailable,
    "tool_authorization_required": ToolAuthorizationRequired,
    "browser_tool_unsupported": BrowserToolUnsupported,
    "agent_failed": AgentFailedError,
    "unexecuted_tool": UnexecutedToolCall,
    # The gateway itself.
    "invalid_request": InvalidRequestError,
    "unauthorized": GatewayUnauthorized,
    "forbidden": GatewayForbidden,
    "conversation_not_found": NotFoundError,
    "stt_not_found": NotFoundError,
    "conversation_busy": ConversationBusyError,
    "non_speech": NonSpeechError,
    "voice_unavailable": VoiceUnavailableError,
    "configuration_error": GatewayConfigurationError,
    "stt_failed": GatewayError,
    "internal_error": GatewayError,
}


@dataclass
class TurnResult:
    """A completed gateway turn."""

    display_text: str
    speech_text: str
    expects_reply: bool
    conversation_id: str | None
    new_chat: bool = False
    # The gateway answered in degraded mode: Home Assistant's own agent spoke.
    degraded: bool = False
    # Listen again without the wake word; None when the gateway does not say.
    continue_conversation: bool | None = None
    # How the gateway answered: "direct" or "openwebui" (informational).
    path: str | None = None
    confidence: str | None = None
    timings: dict[str, float] = field(default_factory=dict)


@dataclass
class GatewayStatus:
    """What the setup test step shows: version, health and access mode."""

    status: str
    version: str | None
    # Names of the gateway's failing checks when ``status`` is ``degraded``.
    degraded: list[str] = field(default_factory=list)
    # Requests without a token are accepted (the gateway's default). Read from
    # ``GET /health``; older gateways leave it out and are probed instead.
    open_mode: bool | None = None
    # The gateway's degraded mode: ``enabled`` by the owner, assistant ``offline``.
    degraded_mode: dict[str, bool] | None = None


def gateway_conversation_id(value: str | None) -> str | None:
    """Pass HA's ULIDs unchanged; map anything else to a stable valid ID."""
    if not value:
        return None
    if CONVERSATION_ID_RE.match(value):
        return value
    return "ha-" + hashlib.sha256(value.encode()).hexdigest()[:40]


def gateway_language(value: str | None) -> str | None:
    """Return a BCP-47-ish tag the gateway accepts, or None (gateway default)."""
    if not value:
        return None
    value = value.strip().replace("_", "-")
    return value if LANGUAGE_RE.match(value) else None


def _clip(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()[:limit].strip()
    return text or None


def build_turn_body(
    text: str,
    *,
    conversation_id: str | None = None,
    language: str | None = None,
    device_id: str | None = None,
    area: str | None = None,
    extra_instructions: str | None = None,
    timeout_s: float | None = None,
    stream: bool = False,
) -> dict[str, Any]:
    """Build a ``POST /turns`` body with only the fields the gateway accepts.

    Absent values are omitted. The text is sent unchanged: the gateway matches
    it against the voice pipeline's transcript to identify the speaker.
    """
    if extra_instructions and len(extra_instructions.strip()) > MAX_INSTRUCTIONS_CHARS:
        LOGGER.debug(
            "Extra instructions clipped to %s characters", MAX_INSTRUCTIONS_CHARS
        )
    fields: dict[str, Any] = {
        "conversation_id": gateway_conversation_id(conversation_id),
        "text": text,
        "language": gateway_language(language),
        "device_id": _clip(device_id, MAX_DEVICE_ID_CHARS),
        "area": _clip(area, MAX_AREA_CHARS),
        "extra_instructions": _clip(extra_instructions, MAX_INSTRUCTIONS_CHARS),
        "timeout_s": min(timeout_s, MAX_TIMEOUT_S) if timeout_s else None,
    }
    body = {key: value for key, value in fields.items() if value is not None}
    body["response"] = {"speech": False, "stream": stream}
    return body


def health_status(data: Any) -> GatewayStatus | None:
    """Allowlist ``GET /health``; None when the body is not a gateway health."""
    if not isinstance(data, dict) or not isinstance(data.get("status"), str):
        return None
    version = data.get("version")
    checks = data.get("checks")
    open_mode = data.get("open_mode")
    mode = data.get("degraded_mode")
    return GatewayStatus(
        status=data["status"],
        version=version if isinstance(version, str) else None,
        degraded=sorted(
            name
            for name, check in (checks.items() if isinstance(checks, dict) else ())
            if isinstance(check, dict) and check.get("status") == "error"
        ),
        open_mode=open_mode if isinstance(open_mode, bool) else None,
        degraded_mode={
            key: mode[key]
            for key in ("enabled", "offline")
            if isinstance(mode.get(key), bool)
        }
        if isinstance(mode, dict)
        else None,
    )


def transcript_text(data: Any) -> str:
    """Return ``plain_text``, else ``text``; never ``fixed_greek``.

    The gateway identifies the speaker and picks the answer language by
    matching the turn text against ``text``/``plain_text``; ``fixed_greek`` is
    the repaired household-language text and would break that match.
    """
    if not isinstance(data, dict):
        raise NotGatewayError("Transcription reply is not a JSON object")
    for key in ("plain_text", "text"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _error_from_status(
    status: int, *, submitted: bool, conversation_id: str | None
) -> OpenWebUIError:
    """Classify a failure without an error envelope (proxy, wrong URL, ...)."""
    if status == 401:
        return GatewayUnauthorized(f"HTTP {status}")
    if status == 403:
        return GatewayForbidden(f"HTTP {status}")
    if status == 504:
        # A proxy in front of the gateway stopped waiting; the turn may run on.
        if submitted:
            err = TurnStillRunning(f"HTTP {status}")
            err.conversation_id = conversation_id
            return err
        return CompletionTimeout(f"HTTP {status}")
    if status in (502, 503):
        return GatewayUnreachable(f"HTTP {status}")
    if status >= 500:
        return GatewayError(f"HTTP {status}")
    if status in (400, 413, 422):
        return InvalidRequestError(f"HTTP {status}")
    # Redirects, 404/405 and other answers: not the gateway's assistant API.
    return NotGatewayError(f"HTTP {status}")


def error_from_response(
    status: int,
    data: Any,
    *,
    submitted: bool = False,
    conversation_id: str | None = None,
) -> OpenWebUIError:
    """Map ``{"error": {"code", "message", "still_running"?, ...}}`` to an exception."""
    envelope = data.get("error") if isinstance(data, dict) else None
    if not isinstance(envelope, dict) or not isinstance(envelope.get("code"), str):
        LOGGER.debug("Gateway HTTP failure without error envelope: status=%s", status)
        err = _error_from_status(
            status, submitted=submitted, conversation_id=conversation_id
        )
        # A 5xx without the envelope came from a proxy: the gateway is unreachable.
        err.unreachable = status >= 500 and not isinstance(err, TurnStillRunning)
        return err
    code = envelope["code"]
    cls = ERROR_CLASSES.get(code)
    if code == "timeout" and envelope.get("still_running") is True:
        err: OpenWebUIError = TurnStillRunning("Accepted turn exceeded its deadline")
        turn_id = envelope.get("turn_id")
        if isinstance(turn_id, str) and turn_id:
            err.turn_id = turn_id
    elif cls is None:
        LOGGER.debug("Unknown gateway error code %s (HTTP %s)", code, status)
        err = _error_from_status(
            status, submitted=submitted, conversation_id=conversation_id
        )
    elif issubclass(cls, ToolUnavailable):
        details = envelope.get("details")
        resources = details.get("resources") if isinstance(details, dict) else None
        err = cls([resources] if isinstance(resources, str) and resources else [])
    else:
        err = cls(f"Gateway error {code}")
    message = envelope.get("message")
    if isinstance(message, str) and message.strip():
        err.server_message = message.strip()[:300]
    returned_id = envelope.get("conversation_id")
    err.conversation_id = (
        returned_id if isinstance(returned_id, str) else conversation_id
    )
    return err


def _turn_result(data: dict[str, Any], sent_id: str | None) -> TurnResult:
    display = data.get("display_text")
    if not isinstance(display, str):
        raise ProtocolError("Turn reply has no display_text")
    if not display.strip():
        raise AgentFailedError("Gateway returned an empty answer")
    speech = data.get("speech_text")
    if not isinstance(speech, str) or not speech.strip():
        speech = display
    conversation_id = data.get("conversation_id")
    identity = data.get("identity")
    confidence = identity.get("confidence") if isinstance(identity, dict) else None
    timings = data.get("timings")
    follow_up = data.get("continue_conversation")
    path = data.get("path")
    return TurnResult(
        display_text=display,
        speech_text=speech,
        expects_reply=data.get("expects_reply") is True,
        conversation_id=conversation_id
        if isinstance(conversation_id, str) and conversation_id
        else sent_id,
        new_chat=data.get("new_chat") is True,
        degraded=data.get("degraded") is True,
        continue_conversation=follow_up if isinstance(follow_up, bool) else None,
        path=path if isinstance(path, str) else None,
        confidence=confidence if isinstance(confidence, str) else None,
        timings={
            key: value
            for key, value in (timings.items() if isinstance(timings, dict) else ())
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        },
    )


def _still_running(
    conversation_id: str | None, reason: str, turn_id: str | None = None
) -> TurnStillRunning:
    err = TurnStillRunning(reason)
    err.conversation_id = conversation_id
    err.turn_id = turn_id
    return err


def _unreachable(reason: str) -> GatewayUnreachable:
    err = GatewayUnreachable(reason)
    err.unreachable = True
    return err


async def _sse_events(lines: AsyncIterator[str]) -> AsyncIterator[tuple[str, Any]]:
    """Parse server-sent events into ``(event, JSON data)`` pairs."""
    event, data = "message", []
    async for line in lines:
        if not line:
            if data:
                try:
                    yield event, json.loads("\n".join(data))
                except ValueError:
                    LOGGER.debug("Gateway sent an event without JSON data: %s", event)
            event, data = "message", []
        elif not line.startswith(":"):
            name, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if name == "event":
                event = value
            elif name == "data":
                data.append(value)


class GatewayClient:
    """Async gateway client using Home Assistant's shared, externally owned client."""

    def __init__(
        self,
        base_url: str,
        token: str | None,
        http: httpx.AsyncClient,
        timeout: float = 30,
    ) -> None:
        """Initialize connection settings without logging the token."""
        self.base_url = normalize_url(base_url)
        self._token = (token or "").strip()
        self._http = http
        self.timeout = timeout
        self.last_status: GatewayStatus | None = None
        self.last_timings: dict[str, float] | None = None
        self.last_confidence: str | None = None
        self.last_path: str | None = None
        # The source of the last answer when it was degraded ("gateway" or
        # "home_assistant"), else None; and how many were (diagnostics).
        self.last_degraded: str | None = None
        self.degraded_answers = 0
        # HA conversation ID -> gateway conversation ID, most recent last.
        self.conversations: OrderedDict[str, str] = OrderedDict()
        self._tasks: set[asyncio.Task] = set()
        self._closed = False

    @property
    def has_token(self) -> bool:
        """Whether requests carry an API client token."""
        return bool(self._token)

    def _headers(self, accept: str, *, token: bool = True) -> dict[str, str]:
        headers = {"Accept": accept}
        if token and self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def _send(
        self,
        method: str,
        path: str,
        *,
        accept: str = "application/json",
        total: float | None = None,
        ok_statuses: tuple[int, ...] = (200,),
        submitted: bool = False,
        conversation_id: str | None = None,
        token: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        """Return the response for an accepted status; raise mapped errors."""
        total = total or self.timeout
        try:
            async with asyncio.timeout(total):
                response = await self._http.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=self._headers(accept, token=token),
                    timeout=httpx.Timeout(total, connect=min(total, self.timeout)),
                    follow_redirects=False,
                    **kwargs,
                )
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise _unreachable("Gateway connection failed") from None
        except (TimeoutError, httpx.HTTPError):
            if submitted:
                # The turn left Home Assistant and may be running: never "retry".
                raise _still_running(
                    conversation_id, "No gateway reply before the deadline"
                ) from None
            raise GatewayUnreachable("Gateway did not answer") from None
        if response.status_code not in ok_statuses:
            raise error_from_response(
                response.status_code,
                _json(response),
                submitted=submitted,
                conversation_id=conversation_id,
            )
        return response

    async def async_health(self) -> GatewayStatus:
        """``GET /health``: the gateway's version and subsystem status, no token."""
        response = await self._send("GET", HEALTH_PATH, token=False)
        status = health_status(_json(response))
        if status is None:
            raise NotGatewayError("Not a gateway health response")
        return status

    async def _async_accepted(self, *, token: bool) -> bool:
        """Whether an assistant request is accepted, with or without the token."""
        try:
            response = await self._send(
                "GET", PROBE_PATH, ok_statuses=(200, 404), token=token
            )
        except GatewayUnauthorized:
            return False
        if response.status_code == 404 and _error_code(response) != (
            "conversation_not_found"
        ):
            raise NotGatewayError("The assistant API is missing")
        return True

    async def async_check(self) -> GatewayStatus:
        """Health plus access: open mode, or a token the gateway accepts.

        ``GET /health`` says whether the gateway runs in open mode; a gateway
        that does not say is probed without a token. A configured token is
        always tried, also in open mode: a token without the ``assistant:turns``
        scope answers 403 (``GatewayForbidden``). A missing token and a rejected
        one are reported separately; all three start Home Assistant's
        reauthentication.
        """
        status = await self.async_health()
        if self._token:
            if not await self._async_accepted(token=True):
                raise GatewayUnauthorized("The gateway rejected the token")
            if status.open_mode is None:
                status.open_mode = await self._async_accepted(token=False)
        else:
            accepted = await self._async_accepted(token=False)
            if status.open_mode is None:
                status.open_mode = accepted
            if not accepted:
                raise TokenRequired("The gateway requires a token")
        self.last_status = status
        return status

    def _remember(self, ha_id: str, result: TurnResult) -> None:
        """Map HA's conversation to the gateway's; a new chat replaces the mapping."""
        if not result.conversation_id:
            return
        if result.new_chat or ha_id not in self.conversations:
            self.conversations[ha_id] = result.conversation_id
        self.conversations.move_to_end(ha_id)
        while len(self.conversations) > MAX_CONVERSATIONS:
            self.conversations.popitem(last=False)

    async def async_turn(
        self,
        text: str,
        conversation_id: str | None = None,
        language: str | None = None,
        device_id: str | None = None,
        area: str | None = None,
        extra_instructions: str | None = None,
        timeout_s: float | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> TurnResult:
        """Run one turn for HA's ``conversation_id``; follow it up after a 504.

        With ``on_delta`` the turn is streamed and each finished sentence is
        passed to it as it arrives; the result is the gateway's ``final``.
        """
        if self._closed:
            raise GatewayUnreachable("Integration is unloading")
        body = build_turn_body(
            text,
            conversation_id=self.conversations.get(conversation_id or "")
            or conversation_id,
            language=language,
            device_id=device_id,
            area=area,
            extra_instructions=extra_instructions,
            timeout_s=timeout_s,
            stream=on_delta is not None,
        )
        LOGGER.debug(
            "Gateway turn: device=%s area=%s token=%s",
            "yes" if "device_id" in body else "no",
            "yes" if "area" in body else "no",
            "yes" if self._token else "no",
        )
        sent_id = body.get("conversation_id")
        turn_timeout = body.get("timeout_s", DEFAULT_TURN_TIMEOUT_S)
        task = asyncio.current_task()
        if task:
            self._tasks.add(task)
        try:
            try:
                # The gateway enforces timeout_s itself; allow transport slack
                # so its own 504 still_running answer arrives first.
                total = turn_timeout + self.timeout
                if on_delta is None:
                    data = await self._async_turn_reply(
                        "POST", TURNS_PATH, sent_id, total, json=body
                    )
                else:
                    data = await self._async_stream_turn(body, sent_id, total, on_delta)
            except TurnStillRunning as err:
                if not err.turn_id:
                    raise
                # The gateway keeps waiting for the accepted turn; ask for its
                # answer for at most the turn timeout.
                sent_id = err.conversation_id or sent_id
                data = await self._async_poll(
                    err.turn_id, sent_id, monotonic() + turn_timeout
                )
        finally:
            if task:
                self._tasks.discard(task)
        result = _turn_result(data, sent_id)
        if conversation_id:
            self._remember(conversation_id, result)
        self.last_timings = result.timings
        self.last_confidence = result.confidence
        self.last_path = result.path
        LOGGER.debug(
            "Gateway turn done: path=%s new_chat=%s continue=%s confidence=%s timings=%s",
            result.path,
            result.new_chat,
            result.continue_conversation,
            result.confidence,
            result.timings,
        )
        return result

    async def _async_turn_reply(
        self, method: str, path: str, sent_id: str | None, total: float, **kwargs: Any
    ) -> dict[str, Any]:
        response = await self._send(
            method,
            path,
            total=total,
            submitted=True,
            conversation_id=sent_id,
            **kwargs,
        )
        data = _json(response)
        if not isinstance(data, dict):
            raise NotGatewayError("Turn reply is not a JSON object")
        return data

    async def _async_stream_turn(
        self,
        body: dict[str, Any],
        sent_id: str | None,
        total: float,
        on_delta: Callable[[str], None],
    ) -> dict[str, Any]:
        """POST a streamed turn: pass each ``delta`` on and return ``final``.

        ``error`` raises like an error answer (a ``still_running`` one carries
        the ``turn_id``). A stream that breaks or ends without either belonged
        to a submitted turn that may still act: it is still running, with the
        ``turn_id`` its ``status`` event named. A gateway that answers JSON
        instead of events is read like an unstreamed turn.

        The gateway ends the stream right after ``final`` or ``error``; it is
        read to its end so no reader is left suspended.
        """
        turn_id: str | None = None
        final: dict[str, Any] | None = None
        error: OpenWebUIError | None = None
        try:
            async with (
                asyncio.timeout(total),
                self._http.stream(
                    "POST",
                    f"{self.base_url}{TURNS_PATH}",
                    headers=self._headers("text/event-stream, application/json"),
                    json=body,
                    timeout=httpx.Timeout(total, connect=min(total, self.timeout)),
                    follow_redirects=False,
                ) as response,
            ):
                if response.status_code != 200 or not response.headers.get(
                    "content-type", ""
                ).startswith("text/event-stream"):
                    await response.aread()
                    data = _json(response)
                    if response.status_code != 200:
                        raise error_from_response(
                            response.status_code,
                            data,
                            submitted=True,
                            conversation_id=sent_id,
                        )
                    if not isinstance(data, dict):
                        raise NotGatewayError("Turn reply is not a JSON object")
                    return data
                async with (
                    aclosing(response.aiter_lines()) as lines,
                    aclosing(_sse_events(lines)) as events,
                ):
                    async for event, data in events:
                        if not isinstance(data, dict):
                            continue
                        if event == "status":
                            turn_id = data.get("turn_id") or turn_id
                            if isinstance(data.get("conversation_id"), str):
                                sent_id = data["conversation_id"] or sent_id
                        elif event == "delta":
                            if isinstance(text := data.get("text"), str) and text:
                                on_delta(text)
                        elif event == "final" and final is None and error is None:
                            final = data
                        elif event == "error" and final is None and error is None:
                            error = error_from_response(
                                500, data, submitted=True, conversation_id=sent_id
                            )
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise _unreachable("Gateway connection failed") from None
        except (TimeoutError, httpx.HTTPError):
            LOGGER.debug("Gateway turn stream broke after submission")
        if error is not None:
            raise error
        if final is not None:
            return final
        raise _still_running(
            sent_id,
            "Turn stream ended without an answer",
            turn_id if isinstance(turn_id, str) else None,
        )

    async def _async_poll(
        self, turn_id: str, sent_id: str | None, deadline: float
    ) -> dict[str, Any]:
        """Ask for an accepted turn's answer every ``POLL_INTERVAL_S`` until ``deadline``.

        202 ``running`` keeps polling; 200 ``done`` returns the answer and 200
        ``failed`` raises the turn's error. Anything else (404: unknown or
        expired turn, a lost connection, a proxy error) ends polling: the turn
        may still act, so it is reported as still running.
        """
        path = f"{TURNS_PATH}/{quote(turn_id, safe='')}"
        while monotonic() + POLL_INTERVAL_S < deadline:
            await asyncio.sleep(POLL_INTERVAL_S)
            LOGGER.debug("Gateway turn still running; polling")
            try:
                response = await self._send(
                    "GET",
                    path,
                    total=max(0.1, min(self.timeout, deadline - monotonic())),
                    ok_statuses=(200, 202),
                    conversation_id=sent_id,
                )
            except OpenWebUIError:
                raise _still_running(sent_id, "Polling stopped") from None
            data = _json(response)
            state = data.get("status") if isinstance(data, dict) else None
            if response.status_code == 202 or state == "running":
                continue
            if state == "done" and isinstance(data.get("result"), dict):
                return data["result"]
            if state == "failed":
                envelope = data.get("error")
                if not isinstance(envelope, dict) or not isinstance(
                    envelope.get("code"), str
                ):
                    raise GatewayError("The turn failed without an error code")
                raise error_from_response(
                    response.status_code,
                    {"error": envelope},
                    submitted=True,
                    conversation_id=sent_id,
                )
            raise _still_running(sent_id, "Unexpected follow-up answer")
        raise _still_running(sent_id, "Accepted turn exceeded the local deadline")

    async def async_transcribe(
        self,
        audio: bytes,
        *,
        language: str | None,
        filename: str = "audio.wav",
        content_type: str = "audio/wav",
    ) -> str:
        """``POST /v1/audio/transcriptions``: ``plain_text``, else ``text``."""
        form = {"model": STT_MODEL, "response_format": "json"}
        if language := gateway_language(language):
            form["language"] = language
        response = await self._send(
            "POST",
            TRANSCRIPTIONS_PATH,
            total=VOICE_TIMEOUT_S,
            data=form,
            files={"file": (filename, audio, content_type)},
        )
        return transcript_text(_json(response))

    async def async_speech(
        self, text: str, *, voice: str | None, response_format: str
    ) -> bytes:
        """``POST /v1/audio/speech``: audio in the requested container."""
        body = {"model": TTS_MODEL, "input": text, "response_format": response_format}
        if voice:
            body["voice"] = voice
        response = await self._send(
            "POST", SPEECH_PATH, accept="audio/*", total=VOICE_TIMEOUT_S, json=body
        )
        if not response.content:
            raise GatewayError("The gateway returned no audio")
        return response.content

    async def async_speech_stream(
        self, text: str, *, voice: str | None, response_format: str
    ) -> AsyncIterator[bytes]:
        """``POST /v1/audio/speech`` and yield the audio as it arrives."""
        body = {"model": TTS_MODEL, "input": text, "response_format": response_format}
        if voice:
            body["voice"] = voice
        received = False
        try:
            async with self._http.stream(
                "POST",
                f"{self.base_url}{SPEECH_PATH}",
                headers=self._headers("audio/*"),
                json=body,
                timeout=httpx.Timeout(VOICE_TIMEOUT_S, connect=self.timeout),
                follow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    await response.aread()
                    raise error_from_response(response.status_code, _json(response))
                async with aclosing(response.aiter_bytes()) as chunks:
                    async for chunk in chunks:
                        received = received or bool(chunk)
                        yield chunk
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise _unreachable("Gateway connection failed") from None
        except httpx.HTTPError:
            raise GatewayUnreachable("Gateway speech stream failed") from None
        if not received:
            raise GatewayError("The gateway returned no audio")

    async def async_ask_action(self, body: dict[str, Any]) -> dict[str, Any]:
        """``POST /household/v1/ask_action``: hand the gateway a plan to ask about.

        The gateway answers 200 with its decision (``asked``, ``skipped`` or
        ``refused``); nothing runs in Home Assistant. No retries: an accepted
        request may already be a question on a satellite.
        """
        response = await self._send("POST", ASK_ACTION_PATH, json=body)
        data = _json(response)
        if not isinstance(data, dict) or not isinstance(data.get("status"), str):
            raise ProtocolError("Unexpected ask_action answer")
        return data

    async def async_close(self) -> None:
        """Cancel local waits on unload; the gateway may finish accepted work."""
        self._closed = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _error_code(response: httpx.Response) -> str | None:
    data = _json(response)
    envelope = data.get("error") if isinstance(data, dict) else None
    return envelope.get("code") if isinstance(envelope, dict) else None
