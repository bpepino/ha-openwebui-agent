"""Home Assistant lifecycle for Open WebUI Agent."""

from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .client import OpenWebUIClient
from .const import (
    BACKEND_GATEWAY,
    CONF_EVENTS,
    DEFAULT_OPTIONS,
    DOMAIN,
    GATEWAY_DEFAULT_OPTIONS,
    LOGGER,
    VERSION,
    entry_backend,
)
from .exceptions import AuthenticationError, GatewayForbidden, OpenWebUIError
from .gateway import create_gateway_client
from .history import ChatHistoryCleaner
from .migration import migrate_options
from .oikos import DATA_PUSHERS, async_register_view
from .push import EventPusher
from .services import async_setup_services
from .state import ConversationManager

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# The gateway serves the whole Assist pipeline: agent, speech-to-text and
# text-to-speech. Direct Open WebUI mode (deprecated) is a conversation agent.
GATEWAY_PLATFORMS = (Platform.CONVERSATION, Platform.STT, Platform.TTS)
OPENWEBUI_PLATFORMS = (Platform.CONVERSATION,)
DOCS_URL = (
    "https://github.com/bpepino/ha-openwebui-agent#direct-open-webui-mode-deprecated"
)


def _platforms(entry: ConfigEntry) -> tuple[Platform, ...]:
    if entry_backend(entry.data) == BACKEND_GATEWAY:
        return GATEWAY_PLATFORMS
    return OPENWEBUI_PLATFORMS


def _deprecation_issue_id(entry: ConfigEntry) -> str:
    return f"direct_mode_deprecated_{entry.entry_id}"


async def _async_setup_gateway(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Gateway mode: check the URL and access; the gateway owns everything else."""
    client = create_gateway_client(hass, entry.data, entry.options)
    try:
        status = await client.async_check()
    except (AuthenticationError, GatewayForbidden) as err:
        # 403: the token lacks the assistant:turns scope; retrying cannot help.
        raise ConfigEntryAuthFailed(
            "The gateway requires a token, rejected this one or does not allow it"
        ) from err
    except OpenWebUIError as err:
        raise ConfigEntryNotReady("The AI gateway is unavailable") from err
    if status.degraded:
        # Turns report failures themselves; the gateway may recover on its own.
        LOGGER.warning(
            "The AI gateway reports unavailable subsystems: %s",
            ", ".join(status.degraded),
        )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = client
    try:
        await hass.config_entries.async_forward_entry_setups(entry, GATEWAY_PLATFORMS)
    except Exception:
        await client.async_close()
        hass.data[DOMAIN].pop(entry.entry_id, None)
        raise
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    # The gateway's household reviews read /api/oikos and receive these events.
    async_register_view(hass)
    _async_start_push(hass, entry, client.base_url)
    LOGGER.debug(
        "Open WebUI Agent %s initialized in gateway mode (gateway %s, open mode %s)",
        VERSION,
        status.version,
        status.open_mode,
    )
    return True


def _async_start_push(hass: HomeAssistant, entry: ConfigEntry, base_url: str) -> None:
    """Push household events unless the owner turned them off."""
    options = {**GATEWAY_DEFAULT_OPTIONS, **entry.options}
    if not options[CONF_EVENTS]:
        return
    pusher = EventPusher(
        hass,
        base_url,
        options,
        get_async_client(hass, verify_ssl=options["verify_ssl"]),
    )
    hass.data.setdefault(DATA_PUSHERS, {})[entry.entry_id] = pusher
    pusher.start(
        lambda target, name: entry.async_create_background_task(hass, target, name)
    )

    def _stop() -> None:
        pusher.stop()
        hass.data[DATA_PUSHERS].pop(entry.entry_id, None)

    entry.async_on_unload(_stop)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the services (``ask_action`` needs a loaded gateway entry)."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Validate authentication before creating the entities."""
    if entry_backend(entry.data) == BACKEND_GATEWAY:
        return await _async_setup_gateway(hass, entry)
    ir.async_create_issue(
        hass,
        DOMAIN,
        _deprecation_issue_id(entry),
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="direct_mode_deprecated",
        translation_placeholders={"title": entry.title},
        learn_more_url=DOCS_URL,
    )
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
        await hass.config_entries.async_forward_entry_setups(entry, OPENWEBUI_PLATFORMS)
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
    LOGGER.warning(
        "Direct Open WebUI mode is deprecated and will be removed in a future release; "
        "add the AI gateway instead"
    )
    LOGGER.debug("Open WebUI Agent %s initialized", VERSION)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the entities and cancel pending local requests."""
    if not await hass.config_entries.async_unload_platforms(entry, _platforms(entry)):
        return False
    manager = hass.data[DOMAIN].pop(entry.entry_id)
    await manager.async_close()
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Drop the deprecation notice of a removed direct entry."""
    ir.async_delete_issue(hass, DOMAIN, _deprecation_issue_id(entry))


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply changed options using Home Assistant's reload lifecycle."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Upgrade older option schemas within this domain, preserving entry identity."""
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
