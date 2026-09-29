"""Diagnostics intentionally omit URLs, identifiers, prompts and secrets."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant

from .const import DEFAULT_OPTIONS, DOMAIN, FEATURES, VERSION


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict:
    """Return an allowlist; never serialize entry data or server message objects."""
    manager = hass.data[DOMAIN][entry.entry_id]
    options = {**DEFAULT_OPTIONS, **entry.options}
    return {
        "integration_version": VERSION,
        "home_assistant_version": HA_VERSION,
        "openwebui_version": manager.client.server_version,
        "features": {key: options[key] for key in FEATURES},
        "tool_mode": options["tool_mode"],
        "selected_tool_count": len(options["tool_ids"]),
        "terminal_mode": options["terminal_mode"],
        "timeout": options["timeout"],
        "completion_timeout": options["completion_timeout"],
        "poll_interval": options["poll_interval"],
        "verify_ssl": options["verify_ssl"],
        "active_conversation_count": len(manager.states),
        "conversation_persistence": False,
    }
