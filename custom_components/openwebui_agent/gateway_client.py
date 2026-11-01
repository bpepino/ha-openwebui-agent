"""Thin client for the home AI gateway: health, assistant turns, speech in and out.

In gateway mode the gateway owns the Open WebUI protocol, the model/tool
profile, identity, conversation continuation, chat retention and the voice
pipeline. This client submits requests and maps the replies, or the gateway's
error envelope, onto this integration's exception keys so the existing
translations keep working.

The token is optional: in the gateway's open mode (its default) requests need
none, so the ``Authorization`` header is sent only when a token is configured.

No retries: an accepted turn may act on the real world.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib
import re
from time import monotonic
from typing import Any
from urllib.parse import quote

import httpx

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
POLL_INTERVAL_S = 1.0
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
    # A returned result is never still running: polling waits for the answer,
    # and a deadline that passes after acceptance raises TurnStillRunning.
    still_running: bool = False
    confidence: str | None = None
    timings: dict[str, float] = field(default_factory=dict)


@dataclass
class GatewayStatus:
    """What the setup test step shows: version, health and access mode."""

    status: str
    version: str | None
    # Names of the gateway's failing checks when ``status`` is ``degraded``.
    degraded: list[str] = field(default_factory=list)
    # Requests without a token are accepted (the gateway's default).
    open_mode: bool = False


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
    body["response"] = {"speech": False, "stream": False}
    return body


def health_status(data: Any) -> GatewayStatus | None:
    """Allowlist ``GET /health``; None when the body is not a gateway health."""
    if not isinstance(data, dict) or not isinstance(data.get("status"), str):
        return None
    version = data.get("version")
    checks = data.get("checks")
    return GatewayStatus(
        status=data["status"],
        version=version if isinstance(version, str) else None,
        degraded=sorted(
            name
            for name, check in (checks.items() if isinstance(checks, dict) else ())
            if isinstance(check, dict) and check.get("status") == "error"
        ),
    )


def transcript_text(data: Any) -> str:
    """Return the repaired transcript when the gateway made one, else the text."""
    if not isinstance(data, dict):
        raise NotGatewayError("Transcription reply is not a JSON object")
    for key in ("fixed_greek", "text"):
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
        return _error_from_status(
            status, submitted=submitted, conversation_id=conversation_id
        )
    code = envelope["code"]
    cls = ERROR_CLASSES.get(code)
    if code == "timeout" and envelope.get("still_running") is True:
        err: OpenWebUIError = TurnStillRunning("Accepted turn exceeded its deadline")
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
    return TurnResult(
        display_text=display,
        speech_text=speech,
        expects_reply=data.get("expects_reply") is True,
        conversation_id=conversation_id
        if isinstance(conversation_id, str) and conversation_id
        else sent_id,
        new_chat=data.get("new_chat") is True,
        confidence=confidence if isinstance(confidence, str) else None,
        timings={
            key: value
            for key, value in (timings.items() if isinstance(timings, dict) else ())
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        },
    )


def _still_running(conversation_id: str | None, reason: str) -> TurnStillRunning:
    err = TurnStillRunning(reason)
    err.conversation_id = conversation_id
    return err


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
            raise GatewayUnreachable("Gateway connection failed") from None
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

        In open mode the gateway accepts any token, so there is nothing more to
        validate. Otherwise a missing token and a rejected one are reported
        separately; both start Home Assistant's reauthentication.
        """
        status = await self.async_health()
        status.open_mode = await self._async_accepted(token=False)
        if not status.open_mode:
            if not self._token:
                raise TokenRequired("The gateway requires a token")
            if not await self._async_accepted(token=True):
                raise GatewayUnauthorized("The gateway rejected the token")
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
    ) -> TurnResult:
        """Run one turn for HA's ``conversation_id``; poll while it is still running."""
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
        )
        LOGGER.debug(
            "Gateway turn: device=%s area=%s token=%s",
            "yes" if "device_id" in body else "no",
            "yes" if "area" in body else "no",
            "yes" if self._token else "no",
        )
        sent_id = body.get("conversation_id")
        # The gateway enforces timeout_s itself; allow transport slack so its
        # own still_running answer arrives first.
        deadline = monotonic() + body.get("timeout_s", DEFAULT_TURN_TIMEOUT_S)
        deadline += self.timeout
        task = asyncio.current_task()
        if task:
            self._tasks.add(task)
        turn_id = None
        try:
            data = await self._async_turn_reply(
                "POST", TURNS_PATH, sent_id, deadline - monotonic(), json=body
            )
            if isinstance(data.get("conversation_id"), str):
                sent_id = data["conversation_id"] or sent_id
            while data.get("still_running") is True:
                # Later poll answers may leave out what the first one named.
                turn_id = data.get("turn_id") or turn_id
                data = await self._async_poll(turn_id, sent_id, deadline)
        finally:
            if task:
                self._tasks.discard(task)
        result = _turn_result(data, sent_id)
        if conversation_id:
            self._remember(conversation_id, result)
        self.last_timings = result.timings
        self.last_confidence = result.confidence
        LOGGER.debug(
            "Gateway turn done: new_chat=%s expects_reply=%s confidence=%s timings=%s",
            result.new_chat,
            result.expects_reply,
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

    async def _async_poll(
        self, turn_id: Any, sent_id: str | None, deadline: float
    ) -> dict[str, Any]:
        """Wait one interval, then read the accepted turn's state again."""
        if not isinstance(turn_id, str) or not turn_id:
            raise _still_running(sent_id, "Still running without a turn_id")
        if monotonic() + POLL_INTERVAL_S >= deadline:
            raise _still_running(sent_id, "Accepted turn exceeded the local deadline")
        await asyncio.sleep(POLL_INTERVAL_S)
        LOGGER.debug("Gateway turn still running; polling")
        try:
            return await self._async_turn_reply(
                "GET",
                f"{TURNS_PATH}/{quote(turn_id, safe='')}",
                sent_id,
                min(self.timeout, deadline - monotonic()),
            )
        except GatewayUnreachable:
            # The turn was accepted; a failed poll does not mean it stopped.
            raise _still_running(sent_id, "Polling failed") from None

    async def async_transcribe(
        self,
        audio: bytes,
        *,
        language: str | None,
        filename: str = "audio.wav",
        content_type: str = "audio/wav",
    ) -> str:
        """``POST /v1/audio/transcriptions``: the repaired transcript, else the text."""
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
