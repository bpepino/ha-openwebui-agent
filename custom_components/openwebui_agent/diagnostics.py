"""Diagnostics intentionally omit URLs, identifiers, prompts and secrets."""

from dataclasses import asdict

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant

from .const import (
    BACKEND_GATEWAY,
    BACKEND_OPENWEBUI,
    CONF_LANGUAGE,
    DEFAULT_OPTIONS,
    DOMAIN,
    FEATURES,
    GATEWAY_DEFAULT_OPTIONS,
    VERSION,
    entry_backend,
)
from .exceptions import OpenWebUIError


async def _async_gateway_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict:
    """Gateway mode: a live check (allowlisted fields) and local settings only."""
    client = hass.data[DOMAIN][entry.entry_id]
    options = {**GATEWAY_DEFAULT_OPTIONS, **entry.options}
    try:
        gateway = asdict(await client.async_check())
    except OpenWebUIError as err:
        gateway = {"error": err.key}
    return {
        "integration_version": VERSION,
        "home_assistant_version": HA_VERSION,
        "backend": BACKEND_GATEWAY,
        "gateway": gateway,
        "token_configured": client.has_token,
        "language": entry.data.get(CONF_LANGUAGE),
        "completion_timeout": options["completion_timeout"],
        "tts_format": options["tts_format"],
        "tts_voice_configured": bool(options["tts_voice"]),
        "conversation_mode": options["conversation_mode"],
        "strip_markdown": options["strip_markdown"],
        "timeout": options["timeout"],
        "verify_ssl": options["verify_ssl"],
        "mapped_conversation_count": len(client.conversations),
        "last_identity_confidence": client.last_confidence,
        "last_turn_timing_ms": client.last_timings,
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict:
    """Return an allowlist; never serialize entry data or server message objects."""
    if entry_backend(entry.data) == BACKEND_GATEWAY:
        return await _async_gateway_diagnostics(hass, entry)
    manager = hass.data[DOMAIN][entry.entry_id]
    options = {**DEFAULT_OPTIONS, **entry.options}
    return {
        "integration_version": VERSION,
        "home_assistant_version": HA_VERSION,
        "backend": BACKEND_OPENWEBUI,
        "openwebui_version": manager.client.server_version,
        "features": {key: options[key] for key in FEATURES},
        "tool_mode": options["tool_mode"],
        "selected_tool_count": len(options["tool_ids"]),
        "terminal_mode": options["terminal_mode"],
        "thinking_mode": options["thinking_mode"],
        "conversation_mode": options["conversation_mode"],
        "keep_chat_history": options["keep_chat_history"],
        "pending_chat_cleanup_count": manager.cleaner.pending_count
        if manager.cleaner
        else 0,
        "timeout": options["timeout"],
        "completion_timeout": options["completion_timeout"],
        "poll_interval": options["poll_interval"],
        "verify_ssl": options["verify_ssl"],
        "active_conversation_count": len(manager.states),
        "conversation_persistence": False,
        "last_turn_timing_seconds": manager.last_timings,
    }
