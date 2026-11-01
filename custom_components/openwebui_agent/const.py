"""Integration identity and configuration defaults."""

from logging import getLogger

LOGGER = getLogger(__package__)
DOMAIN = "openwebui_agent"
NAME = "Open WebUI Agent"
VERSION = "3.2.0"
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
BACKENDS = (BACKEND_GATEWAY, BACKEND_OPENWEBUI)
CONF_GATEWAY_URL = "gateway_url"
# Optional: the gateway's open mode (its default) needs no token on the LAN.
CONF_GATEWAY_TOKEN = "gateway_token"
CONF_LANGUAGE = "language"
CONF_TTS_VOICE = "tts_voice"
CONF_TTS_FORMAT = "tts_format"
# Speak each sentence as soon as the gateway streams it (MP3 per sentence).
# Off: one text-to-speech call with the whole answer.
CONF_STREAM_SPEECH = "stream_speech"
STREAM_FORMAT = "mp3"
# Off by default: Home Assistant's own agent acts outside the gateway's policy.
CONF_FALLBACK = "fallback_to_home_assistant"
# A degraded answer: the gateway's own (assistant upstream offline), or Home
# Assistant's agent while the gateway is unreachable.
EVENT_DEGRADED = "openwebui_agent_degraded"
DEGRADED_GATEWAY = "gateway"
DEGRADED_HOME_ASSISTANT = "home_assistant"
# Text-to-speech containers the gateway produces, and HA's file extension for each.
TTS_FORMATS = {"mp3": "mp3", "wav": "wav", "opus": "ogg"}
GATEWAY_TITLE = "AI gateway"
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
# Household events pushed to the gateway (push.py).
CONF_EVENTS = "events"
CONF_PRESENCE = "presence"
CONF_DOOR_OPEN_AFTER = "door_open_after"
CONF_UNAVAILABLE_AFTER = "unavailable_after"
CONF_GENERIC_STATE_CHANGES = "generic_state_changes"
# An events client token, needed only when the gateway requires tokens.
CONF_EVENTS_TOKEN = "gateway_events_token"
HOUSEHOLD_SECTION = "household"
HOUSEHOLD_OPTIONS = (
    CONF_EVENTS,
    CONF_PRESENCE,
    CONF_DOOR_OPEN_AFTER,
    CONF_UNAVAILABLE_AFTER,
    CONF_GENERIC_STATE_CHANGES,
    CONF_EVENTS_TOKEN,
)
# The gateway owns model, tools, features, thinking, history and voice.
# Only GATEWAY_OPTIONS are shown; the rest keep values saved by 2.1.0 entries.
GATEWAY_OPTIONS = (
    CONF_COMPLETION_TIMEOUT,
    CONF_TTS_VOICE,
    CONF_TTS_FORMAT,
    CONF_STREAM_SPEECH,
    CONF_FALLBACK,
    *HOUSEHOLD_OPTIONS,
)
GATEWAY_DEFAULT_OPTIONS = {
    CONF_COMPLETION_TIMEOUT: 120,
    CONF_TTS_VOICE: "",
    CONF_TTS_FORMAT: "mp3",
    CONF_STREAM_SPEECH: True,
    CONF_FALLBACK: False,
    CONF_EVENTS: True,
    # Empty: every person exposed to Assist.
    CONF_PRESENCE: [],
    CONF_DOOR_OPEN_AFTER: {"hours": 0, "minutes": 10, "seconds": 0},
    CONF_UNAVAILABLE_AFTER: {"hours": 0, "minutes": 2, "seconds": 0},
    CONF_GENERIC_STATE_CHANGES: False,
    CONF_EVENTS_TOKEN: "",
    CONF_TIMEOUT: 30,
    CONF_VERIFY_SSL: True,
}
# Languages the gateway's Whisper-based speech recognition understands.
LANGUAGES = (
    "af", "am", "ar", "as", "az", "ba", "be", "bg", "bn", "bo", "br", "bs", "ca",
    "cs", "cy", "da", "de", "el", "en", "es", "et", "eu", "fa", "fi", "fo", "fr",
    "gl", "gu", "ha", "he", "hi", "hr", "ht", "hu", "hy", "id", "is", "it", "ja",
    "jv", "ka", "kk", "km", "kn", "ko", "la", "lb", "ln", "lo", "lt", "lv", "mg",
    "mi", "mk", "ml", "mn", "mr", "ms", "mt", "my", "ne", "nl", "nn", "no", "oc",
    "pa", "pl", "ps", "pt", "ro", "ru", "sa", "sd", "si", "sk", "sl", "sn", "so",
    "sq", "sr", "su", "sv", "sw", "ta", "te", "tg", "th", "tk", "tl", "tr", "tt",
    "uk", "ur", "uz", "vi", "yi", "yo", "zh",
)  # fmt: skip


def entry_backend(data: dict) -> str:
    """Return the connection backend stored in a config entry's data."""
    return data.get(CONF_BACKEND, BACKEND_OPENWEBUI)


def default_language(language: str | None) -> str:
    """Map HA's configured language (``en-GB``) onto a supported one."""
    for candidate in (language or "", (language or "").split("-")[0]):
        if candidate in LANGUAGES:
            return candidate
    return "en"
