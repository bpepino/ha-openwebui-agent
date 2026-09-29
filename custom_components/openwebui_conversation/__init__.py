"""Home Assistant lifecycle for Open WebUI Agent."""

from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .client import OpenWebUIClient
from .const import DEFAULT_OPTIONS, DOMAIN, LOGGER, VERSION
from .exceptions import AuthenticationError, OpenWebUIError
from .history import ChatHistoryCleaner
from .migration import migrate_options
from .state import ConversationManager

PLATFORMS = (Platform.CONVERSATION,)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Validate authentication before creating the conversation entity."""
    options = {**DEFAULT_OPTIONS, **entry.options}
    client = OpenWebUIClient(
        entry.data["base_url"],
        entry.data["api_key"],
        async_get_clientsession(hass),
        options["timeout"],
        options["verify_ssl"],
    )
    try:
        await client.async_get_models()
        await client.async_get_version()
    except AuthenticationError as err:
        raise ConfigEntryAuthFailed("Open WebUI API key was rejected") from err
    except OpenWebUIError as err:
        raise ConfigEntryNotReady(
            "Open WebUI connection or API is unavailable"
        ) from err
    store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.chat_cleanup")
    cleaner = None
    if options["keep_chat_history"]:
        # Enabling retention cancels pending deletion as well as future tracking.
        await store.async_remove()
    else:
        cleaner = ChatHistoryCleaner(client, store.async_save, await store.async_load())
    manager = ConversationManager(client, cleaner=cleaner)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = manager
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await manager.async_close()
        hass.data[DOMAIN].pop(entry.entry_id, None)
        raise
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    if cleaner:
        entry.async_on_unload(
            async_track_time_interval(
                hass, cleaner.async_cleanup, timedelta(seconds=60)
            )
        )
        entry.async_create_background_task(
            hass, cleaner.async_cleanup(), "openwebui_chat_cleanup"
        )
    LOGGER.debug("Open WebUI Agent %s initialized", VERSION)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the entity and cancel pending local requests."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    manager = hass.data[DOMAIN].pop(entry.entry_id)
    await manager.async_close()
    return True


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply changed options using Home Assistant's reload lifecycle."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Keep the upstream domain, credentials, title and entity identity."""
    if entry.version > 2:
        return False
    if entry.version == 1:
        hass.config_entries.async_update_entry(
            entry,
            options=migrate_options(dict(entry.data), dict(entry.options)),
            version=2,
            minor_version=1,
        )
        LOGGER.info(
            "Migrated to native Web Search; legacy search phrases are retained but inactive"
        )
    return True
