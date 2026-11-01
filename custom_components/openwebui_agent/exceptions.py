"""Sanitized protocol failures; credentials never enter errors."""


class OpenWebUIError(Exception):
    """Base client error with a Home Assistant translation key."""

    key = "cannot_connect"
    # Gateway mode: the envelope's short English message, used only when no
    # translation exists for ``key`` (never credentials or user text), and the
    # conversation the failure belongs to.
    server_message: str | None = None
    conversation_id: str | None = None
    # Gateway mode: the gateway itself could not be reached (a connection or DNS
    # failure, or a 5xx without the gateway's error envelope, from a proxy).
    # Never set for a 4xx or a 504 still_running: that turn may still act.
    unreachable: bool = False


class AuthenticationError(OpenWebUIError):
    """The API key was rejected."""

    key = "invalid_auth"


class PermissionDenied(OpenWebUIError):
    """The API user lacks access."""

    key = "permission_denied"


class NotFoundError(OpenWebUIError):
    """The requested resource is missing."""

    key = "not_found"


class ProtocolError(OpenWebUIError):
    """Unexpected JSON or message tree."""

    key = "invalid_response"


class UnsupportedAPIError(OpenWebUIError):
    """Required native agent API behavior is unavailable."""

    key = "unsupported_api"


class CompletionTimeout(OpenWebUIError):
    """A request or agent run exceeded its deadline."""

    key = "timeout"
    still_running = False


class TurnStillRunning(CompletionTimeout):
    """Gateway: the deadline passed after the turn was accepted; it may still act."""

    key = "still_running"
    still_running = True
    # The id for ``GET /assistant/v1/turns/{turn_id}`` from the gateway's 504.
    turn_id: str | None = None


class ModelMissingError(OpenWebUIError):
    """The configured model is not visible to this API user."""

    key = "model_missing"


class ResourceUnavailable(OpenWebUIError):
    """A resource disappeared or needs browser authorization."""

    key = "resource_unavailable"


class ToolUnavailable(ResourceUnavailable):
    """Selected IDs are absent from the API user's discovery response."""

    key = "tool_unavailable"

    def __init__(self, resource_ids: list[str]) -> None:
        """Expose only selected identifiers, never the server response or credentials."""
        super().__init__(self.key)
        self.translation_placeholders = {"resources": ", ".join(resource_ids)}


class ToolAuthorizationRequired(ToolUnavailable):
    """A discovered tool explicitly requires the API user's OAuth login."""

    key = "tool_authorization_required"


class BrowserToolUnsupported(ToolUnavailable):
    """A browser-local connection cannot be used by the server-side agent."""

    key = "browser_tool_unsupported"


class TerminalUnavailable(ToolUnavailable):
    """A selected terminal is absent or is unavailable in chat contexts."""

    key = "terminal_unavailable"


class TerminalDisabled(ResourceUnavailable):
    """The selected model explicitly disables terminal access."""

    key = "terminal_disabled"


class TerminalRequired(ResourceUnavailable):
    """Specific-terminal mode has no selected terminal."""

    key = "terminal_required"


class AgentFailedError(OpenWebUIError):
    """The server task failed or did not persist a final answer."""

    key = "agent_failed"


class UnexecutedToolCall(AgentFailedError):
    """The final answer contains an unexecuted tool protocol."""

    key = "unexecuted_tool"


# Gateway mode. Upstream (Open WebUI) failures reported by the gateway reuse the
# keys above; these describe the gateway itself or the request sent to it.


class GatewayUnreachable(OpenWebUIError):
    """The gateway could not be reached; nothing was submitted."""

    key = "gateway_unreachable"


class NotGatewayError(ProtocolError):
    """The URL answered, but not like the gateway's assistant API."""

    key = "not_gateway"


class GatewayUnauthorized(AuthenticationError):
    """The gateway rejected this entry's API client token."""

    key = "gateway_unauthorized"


class TokenRequired(GatewayUnauthorized):
    """The gateway requires tokens and this entry has none."""

    key = "token_required"


class GatewayForbidden(PermissionDenied):
    """The API client token lacks the scope for this request."""

    key = "gateway_forbidden"


class InvalidRequestError(OpenWebUIError):
    """The gateway rejected the request body."""

    key = "invalid_request"


class NonSpeechError(OpenWebUIError):
    """The gateway found no speech in the transcript."""

    key = "non_speech"


class VoiceUnavailableError(OpenWebUIError):
    """The gateway has no voice pipeline configured."""

    key = "voice_unavailable"


class ConversationBusyError(OpenWebUIError):
    """The conversation is still answering a previous request."""

    key = "conversation_busy"


class GatewayConfigurationError(OpenWebUIError):
    """The gateway is not configured for this client (profile, user key)."""

    key = "gateway_configuration"


class GatewayError(OpenWebUIError):
    """An internal or otherwise unrecognized gateway failure."""

    key = "gateway_error"
