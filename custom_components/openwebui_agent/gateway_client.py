"""Thin client for the home AI gateway's assistant API (``/assistant/v1``).

In gateway mode the gateway owns the Open WebUI protocol, the model/tool
profile, identity, conversation continuation and chat retention. This client
submits one turn and maps the reply, or the gateway's error envelope, onto this
integration's exception keys so the existing translations keep working.

No retries: an accepted turn may act on the real world.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import re
from typing import Any

import aiohttp

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
    ToolAuthorizationRequired,
    ToolUnavailable,
    TurnStillRunning,
    UnexecutedToolCall,
    UnsupportedAPIError,
    VoiceUnavailableError,
)

API_PREFIX = "/assistant/v1"
TURNS_PATH = f"{API_PREFIX}/turns"
HEALTH_PATH = f"{API_PREFIX}/health"

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
    # A completed turn is never still running; a timed-out accepted turn
    # raises TurnStillRunning instead. Kept for forward compatibility.
    still_running: bool = False
    confidence: str | None = None
    timings: dict[str, float] = field(default_factory=dict)


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


def turn_channel(device_id: str | None, satellite_id: str | None) -> str:
    """Voice when Assist names a device or satellite, otherwise typed text."""
    return "voice" if device_id or satellite_id else "text"


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
    satellite_id: str | None = None,
    area: str | None = None,
    extra_instructions: str | None = None,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """Build a ``POST /turns`` body with only the fields the gateway accepts.

    Absent values are omitted. The text is sent unchanged: the gateway matches
    it against the voice proxy's transcript to identify the speaker.
    """
    if extra_instructions and len(extra_instructions.strip()) > MAX_INSTRUCTIONS_CHARS:
        LOGGER.debug(
            "Extra instructions clipped to %s characters", MAX_INSTRUCTIONS_CHARS
        )
    fields: dict[str, Any] = {
        "conversation_id": gateway_conversation_id(conversation_id),
        "text": text,
        "language": gateway_language(language),
        "device_id": _clip(device_id or satellite_id, MAX_DEVICE_ID_CHARS),
        "area": _clip(area, MAX_AREA_CHARS),
        "extra_instructions": _clip(extra_instructions, MAX_INSTRUCTIONS_CHARS),
        "timeout_s": min(timeout_s, MAX_TIMEOUT_S) if timeout_s else None,
    }
    body = {key: value for key, value in fields.items() if value is not None}
    body["response"] = {"speech": False, "stream": False}
    return body


def _scalar(value: Any) -> str | bool | int | float | None:
    return value if isinstance(value, (str, bool, int, float)) else None


def health_summary(data: Any) -> dict[str, Any] | None:
    """Allowlist the health fields; None when the body is not a gateway health."""
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("status"), str)
        or not isinstance(data.get("openwebui"), dict)
    ):
        return None
    owui = data["openwebui"]
    return {
        "status": data["status"],
        "openwebui": {
            key: _scalar(owui.get(key))
            for key in ("reachable", "version", "supported_version", "version_ok")
        },
        "voice_pipeline": _scalar(data.get("voice_pipeline")),
        "retention_running": _scalar(data.get("retention_running")),
    }


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
        if isinstance(conversation_id, str)
        else sent_id,
        new_chat=data.get("new_chat") is True,
        still_running=data.get("still_running") is True,
        confidence=confidence if isinstance(confidence, str) else None,
        timings={
            key: value
            for key, value in (timings.items() if isinstance(timings, dict) else ())
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        },
    )


class GatewayClient:
    """Async gateway client using Home Assistant's shared, externally owned session."""

    def __init__(
        self,
        base_url: str,
        token: str,
        session: aiohttp.ClientSession,
        timeout: float = 30,
        verify_ssl: bool = True,
    ) -> None:
        """Initialize connection settings without logging the token."""
        self.base_url = normalize_url(base_url)
        self._token = token
        self._session = session
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.last_health: dict[str, Any] | None = None
        self.last_timings: dict[str, float] | None = None
        self.last_confidence: str | None = None
        self._tasks: set[asyncio.Task] = set()
        self._closed = False

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        total: float | None = None,
        ok_statuses: tuple[int, ...] = (200,),
        submitted: bool = False,
        conversation_id: str | None = None,
    ) -> tuple[int, Any]:
        """Return ``(status, json)`` for an accepted status; raise mapped errors."""
        try:
            async with self._session.request(
                method,
                f"{self.base_url}{path}",
                json=body,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/json",
                },
                timeout=aiohttp.ClientTimeout(
                    total=total or self.timeout, sock_connect=self.timeout
                ),
                ssl=self.verify_ssl,
                allow_redirects=False,
            ) as response:
                try:
                    data = await response.json(content_type=None)
                except ValueError:
                    data = None
                if response.status not in ok_statuses:
                    raise error_from_response(
                        response.status,
                        data,
                        submitted=submitted,
                        conversation_id=conversation_id,
                    )
                return response.status, data
        except aiohttp.ConnectionTimeoutError:
            raise GatewayUnreachable("Gateway connection timed out") from None
        except TimeoutError:
            if submitted:
                # The turn left Home Assistant and may be running: never "retry".
                err = TurnStillRunning("No gateway reply before the deadline")
                err.conversation_id = conversation_id
                raise err from None
            raise GatewayUnreachable("Gateway did not answer in time") from None
        except aiohttp.ClientError:
            raise GatewayUnreachable("Gateway connection failed") from None

    async def async_health(self) -> dict[str, Any]:
        """Validate URL and token; 503 still answers when Open WebUI is down."""
        status, data = await self._request("GET", HEALTH_PATH, ok_statuses=(200, 503))
        health = health_summary(data)
        if health is None:
            if status == 503:
                raise GatewayUnreachable("Gateway unavailable")
            raise NotGatewayError("Not a gateway health response")
        self.last_health = health
        return health

    async def async_turn(
        self,
        text: str,
        conversation_id: str | None = None,
        language: str | None = None,
        user_id: str | None = None,
        device_id: str | None = None,
        satellite_id: str | None = None,
        area_id: str | None = None,
        extra_instructions: str | None = None,
        channel: str | None = None,
        timeout_s: float | None = None,
    ) -> TurnResult:
        """Run one turn. ``area_id`` is sent as the gateway's ``area`` (a room name).

        ``user_id`` and ``channel`` are not part of the gateway's request model
        yet, so they are only logged; identity comes from the API client token
        and the gateway's own speaker correlation.
        """
        if self._closed:
            raise GatewayUnreachable("Integration is unloading")
        body = build_turn_body(
            text,
            conversation_id=conversation_id,
            language=language,
            device_id=device_id,
            satellite_id=satellite_id,
            area=area_id,
            extra_instructions=extra_instructions,
            timeout_s=timeout_s,
        )
        LOGGER.debug(
            "Gateway turn: channel=%s ha_user=%s device=%s area=%s",
            channel or turn_channel(device_id, satellite_id),
            "yes" if user_id else "no",
            "yes" if "device_id" in body else "no",
            "yes" if "area" in body else "no",
        )
        sent_id = body.get("conversation_id")
        task = asyncio.current_task()
        if task:
            self._tasks.add(task)
        try:
            _, data = await self._request(
                "POST",
                TURNS_PATH,
                body=body,
                # The gateway enforces timeout_s itself; allow transport slack
                # so its own 504 still_running answer arrives first.
                total=body.get("timeout_s", DEFAULT_TURN_TIMEOUT_S) + self.timeout,
                submitted=True,
                conversation_id=sent_id,
            )
        finally:
            if task:
                self._tasks.discard(task)
        if not isinstance(data, dict):
            raise NotGatewayError("Turn reply is not a JSON object")
        result = _turn_result(data, sent_id)
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

    async def async_close(self) -> None:
        """Cancel local waits on unload; the gateway may finish accepted work."""
        self._closed = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
