"""Use real Home Assistant fixtures when its Linux test runtime is installed."""

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")


@pytest.fixture(autouse=True)
def enable_integrations(enable_custom_integrations):
    """Permit loading this repository's custom integration."""
    yield


@pytest.fixture(autouse=True)
async def setup_homeassistant(hass):
    """Initialize the core registries used by the conversation dependency."""
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "homeassistant", {})
