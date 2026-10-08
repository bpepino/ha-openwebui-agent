"""Integration identity and configuration defaults."""

from logging import getLogger

LOGGER = getLogger(__package__)
DOMAIN = "openwebui_agent"
NAME = "Open WebUI Agent"
VERSION = "2.1.0"
CONF_SERVICE_NAME = "service_name"
CONF_BASE_URL = "base_url"
CONF_API_KEY = "api_key"
CONF_MODEL = "chat_model"
CONF_TIMEOUT = "timeout"
CONF_VERIFY_SSL = "verify_ssl"
CONF_STRIP_MARKDOWN = "strip_markdown"
CONF_TOOL_MODE = "tool_mode"
CONF_TOOL_IDS = "tool_ids"
CONF_TERMINAL_MODE = "terminal_mode"
CONF_TERMINAL_ID = "terminal_id"
CONF_COMPLETION_TIMEOUT = "completion_timeout"
CONF_POLL_INTERVAL = "poll_interval"
CONF_CONVERSATION_MODE = "conversation_mode"
# Connection backend. Entries without the key predate 2.1.0 and talk to Open WebUI.
CONF_BACKEND = "backend"
BACKEND_OPENWEBUI = "openwebui"
BACKEND_GATEWAY = "gateway"
BACKENDS = (BACKEND_OPENWEBUI, BACKEND_GATEWAY)
CONF_GATEWAY_URL = "gateway_url"
CONF_GATEWAY_TOKEN = "gateway_token"
FEATURES = ("memory", "web_search", "code_interpreter", "image_generation")
DEFAULT_OPTIONS = {
    CONF_TIMEOUT: 30,
    CONF_VERIFY_SSL: True,
    CONF_STRIP_MARKDOWN: True,
    CONF_COMPLETION_TIMEOUT: 120,
    CONF_POLL_INTERVAL: 2,
    CONF_TOOL_MODE: "model",
    CONF_TOOL_IDS: [],
    CONF_TERMINAL_MODE: "none",
    "thinking_mode": "model",
    "conversation_mode": "questions",
    "keep_chat_history": False,
    "memory": True,
    "web_search": True,
    "code_interpreter": False,
    "image_generation": False,
}
# The gateway owns model, tools, features, thinking, history and polling.
GATEWAY_DEFAULT_OPTIONS = {
    CONF_TIMEOUT: 30,
    CONF_VERIFY_SSL: True,
    CONF_STRIP_MARKDOWN: True,
    CONF_COMPLETION_TIMEOUT: 120,
    CONF_CONVERSATION_MODE: "questions",
}


def entry_backend(data: dict) -> str:
    """Return the connection backend stored in a config entry's data."""
    return data.get(CONF_BACKEND, BACKEND_OPENWEBUI)
