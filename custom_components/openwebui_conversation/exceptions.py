"""Sanitized protocol failures; credentials never enter errors."""


class OpenWebUIError(Exception):
    """Base client error with a Home Assistant translation key."""

    key = "cannot_connect"


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
