"""The ``openwebui_agent.ask_action`` service: Home Assistant proposes, the gateway asks.

The fields are checked by ``ask_action.build_request`` (pure); the body goes to
the gateway over the loaded gateway entry's client (its token, its URL). The
gateway checks the plan, asks on the satellite and runs it only on a yes; Home
Assistant never runs it. A refusal raises (the automation's trace shows the
reason); ``asked`` and ``skipped`` return the gateway's answer.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from .ask_action import (
    SERVICE_ASK_ACTION,
    AskActionInvalid,
    build_request,
    outcome_error,
)
from .const import BACKEND_GATEWAY, DOMAIN, LOGGER, VERSION, entry_backend
from .exceptions import OpenWebUIError
from .gateway_client import GatewayClient

ASK_ACTION_SCHEMA = vol.Schema(
    {
        vol.Required("question"): cv.string,
        vol.Required("steps"): vol.All(cv.ensure_list, [dict]),
        vol.Optional("satellite"): cv.string,
        vol.Optional("area"): cv.string,
        vol.Optional("context"): dict,
    }
)


def _gateway_client(hass: HomeAssistant) -> GatewayClient:
    """Return the first loaded gateway entry's client."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is not ConfigEntryState.LOADED:
            continue
        if entry_backend(entry.data) != BACKEND_GATEWAY:
            continue
        client = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if isinstance(client, GatewayClient):
            return client
    raise ServiceValidationError("No AI gateway entry is loaded")


async def _async_ask_action(call: ServiceCall) -> ServiceResponse:
    client = _gateway_client(call.hass)
    try:
        body = build_request(call.data, version=VERSION, request_id=call.context.id)
    except AskActionInvalid as err:
        raise ServiceValidationError(str(err)) from err
    try:
        answer = await client.async_ask_action(body)
    except OpenWebUIError as err:
        detail = getattr(err, "server_message", None) or type(err).__name__
        raise HomeAssistantError(
            f"The gateway did not take the question: {detail}"
        ) from err
    if reason := outcome_error(answer):
        raise HomeAssistantError(f"The gateway refused the question: {reason}")
    LOGGER.debug(
        "ask_action %s: %s %s",
        body["question"],
        answer.get("status"),
        answer.get("reason") or "",
    )
    return answer if call.return_response else None


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the integration's services once (they work with a gateway entry)."""
    if hass.services.has_service(DOMAIN, SERVICE_ASK_ACTION):
        return
    hass.services.async_register(
        DOMAIN,
        SERVICE_ASK_ACTION,
        _async_ask_action,
        schema=ASK_ACTION_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
