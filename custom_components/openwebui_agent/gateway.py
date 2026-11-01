"""Home Assistant glue for gateway mode: the client and the shared device."""

from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.httpx_client import get_async_client

from .const import (
    CONF_GATEWAY_TOKEN,
    CONF_GATEWAY_URL,
    DOMAIN,
    GATEWAY_DEFAULT_OPTIONS,
)
from .gateway_client import GatewayClient


def create_gateway_client(
    hass: HomeAssistant, data: Mapping[str, Any], options: Mapping[str, Any]
) -> GatewayClient:
    """Use HA's shared httpx client; 2.1.0 entries may have saved SSL/timeout."""
    options = {**GATEWAY_DEFAULT_OPTIONS, **options}
    return GatewayClient(
        data[CONF_GATEWAY_URL],
        data.get(CONF_GATEWAY_TOKEN),
        get_async_client(hass, verify_ssl=options["verify_ssl"]),
        options["timeout"],
    )


def gateway_device_info(entry: ConfigEntry, client: GatewayClient) -> DeviceInfo:
    """One service device groups the agent, speech-to-text and text-to-speech."""
    status = client.last_status
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="Home AI gateway",
        model="Assistant and voice API",
        sw_version=status.version if status else None,
        entry_type=DeviceEntryType.SERVICE,
        configuration_url=f"{client.base_url}/ui",
    )
