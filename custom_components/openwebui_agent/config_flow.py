"""Connection, discovery and agent options for Open WebUI Agent.

Two backends: Open WebUI directly (discovery and native agent options here), or
the home AI gateway (URL, API client token and voice timing only; the gateway
owns model, tools, features, identity and chat history).
"""

import asyncio
from copy import deepcopy

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import OpenWebUIClient, normalize_url
from .config_schema import (
    connection_schema,
    gateway_schema,
    model_schema,
    terminal_schema,
    tools_schema,
)
from .const import (
    BACKEND_GATEWAY,
    BACKEND_OPENWEBUI,
    CONF_BACKEND,
    CONF_GATEWAY_TOKEN,
    CONF_GATEWAY_URL,
    CONF_MODEL,
    DEFAULT_OPTIONS,
    DOMAIN,
    GATEWAY_DEFAULT_OPTIONS,
    NAME,
    entry_backend,
)
from .exceptions import OpenWebUIError
from .gateway_client import GatewayClient


class _FormError(Exception):
    """A form error key raised by a connection helper."""

    def __init__(self, key: str) -> None:
        """Keep only the translation key."""
        super().__init__(key)
        self.key = key


def gateway_status(health: dict) -> str:
    """Summarize a gateway health answer for the form (no URLs or secrets)."""
    owui = health["openwebui"]
    if not owui.get("reachable"):
        return "reachable, but it cannot reach Open WebUI right now"
    version = owui.get("version") or "version unknown"
    return f"{health['status']}, Open WebUI {version}"


class AgentFlow:
    """Shared configuration-only steps; no protocol or tool execution."""

    # The options flow edits the gateway's SSL/timeout settings on the gateway
    # step; first setup asks for them on the connection step.
    _gateway_connection_fields = False

    def _initialize(self, data: dict, options: dict) -> None:
        """Set working copies without modifying a saved entry."""
        self.connection = dict(data)
        defaults = (
            GATEWAY_DEFAULT_OPTIONS
            if entry_backend(data) == BACKEND_GATEWAY
            else DEFAULT_OPTIONS
        )
        self.options = {**deepcopy(defaults), **options}
        self.models = []
        self.tools = []
        self.terminals = []
        self.discovery_errors = {}
        self.loaded = False
        self.gateway_status: str | None = None

    def _client(self) -> OpenWebUIClient:
        """Reuse Home Assistant's session with the current connection settings."""
        return OpenWebUIClient(
            self.connection["base_url"],
            self.connection["api_key"],
            async_get_clientsession(self.hass),
            self.options["timeout"],
            self.options["verify_ssl"],
        )

    def _gateway_client(self) -> GatewayClient:
        """Reuse Home Assistant's session with the current gateway settings."""
        return GatewayClient(
            self.connection[CONF_GATEWAY_URL],
            self.connection[CONF_GATEWAY_TOKEN],
            async_get_clientsession(self.hass),
            self.options["timeout"],
            self.options["verify_ssl"],
        )

    async def _async_check_gateway(self) -> str | None:
        """Validate the gateway URL and token; return an error key or None."""
        try:
            health = await self._gateway_client().async_health()
        except OpenWebUIError as err:
            return err.key
        self.gateway_status = gateway_status(health)
        return None

    async def async_step_gateway(self, user_input: dict | None = None):
        """Gateway mode: timeouts and listening behavior only."""
        errors = {}
        if user_input is not None:
            self.options.update(user_input)
            if (error := await self._async_check_gateway()) is None:
                return self._finish()
            errors["base"] = error
        elif self.gateway_status is None and (
            error := await self._async_check_gateway()
        ):
            errors["base"] = error
        return self.async_show_form(
            step_id="gateway",
            data_schema=gateway_schema(
                self.options, connection=self._gateway_connection_fields
            ),
            errors=errors,
            description_placeholders={"status": self.gateway_status or "unknown"},
        )

    async def _discover(self) -> None:
        """Refresh all selectors each time a configuration/options flow opens."""
        client = self._client()
        self.discovery_errors = {}
        results = await asyncio.gather(
            client.async_get_models(),
            client.async_get_tools(),
            client.async_get_terminals(),
            return_exceptions=True,
        )
        for name, result in zip(("models", "tools", "terminals"), results, strict=True):
            if isinstance(result, OpenWebUIError):
                self.discovery_errors[name] = result.key
                setattr(self, name, [])
            elif isinstance(result, BaseException):
                raise result
            else:
                setattr(self, name, result)
        self.loaded = True

    async def async_step_agent(self, user_input: dict | None = None):
        """Select a discovered model and native feature flags."""
        if not self.loaded:
            await self._discover()
        errors = {}
        if user_input is not None:
            old_model = self.options.get(CONF_MODEL)
            self.options.update(user_input)
            if (
                self.options[CONF_MODEL] not in {m["id"] for m in self.models}
                and self.options[CONF_MODEL] != old_model
            ):
                errors["base"] = "model_missing"
            else:
                # Connection settings can be repaired in options before rediscovery.
                self.loaded = False
                await self._discover()
                if "models" not in self.discovery_errors:
                    return await self.async_step_tools()
                errors["base"] = self.discovery_errors["models"]
        elif "models" in self.discovery_errors:
            errors["base"] = self.discovery_errors["models"]
        elif not self.models:
            errors["base"] = "no_models"
        elif self.options.get(CONF_MODEL) and self.options[CONF_MODEL] not in {
            m["id"] for m in self.models
        }:
            errors["base"] = "model_missing"
        return self.async_show_form(
            step_id="agent",
            data_schema=model_schema(self.options, self.models),
            errors=errors,
        )

    async def async_step_tools(self, user_input: dict | None = None):
        """Configure model tools or explicit server-side tool IDs."""
        if user_input is not None:
            self.options.update(user_input)
            return await self.async_step_terminal()
        missing = set(self.options.get("tool_ids", [])) - {t.id for t in self.tools}
        warning = (
            "tools" in self.discovery_errors
            or missing
            or any(not t.authenticated for t in self.tools)
        )
        return self.async_show_form(
            step_id="tools",
            data_schema=tools_schema(self.options, self.tools),
            errors={"base": "discovery_partial"} if warning else {},
        )

    async def async_step_terminal(self, user_input: dict | None = None):
        """Complete setup, retaining unavailable saved IDs with a visible warning."""
        errors = {}
        if user_input is not None:
            self.options.update(user_input)
            if self.options["terminal_mode"] == "custom" and not self.options.get(
                "terminal_id"
            ):
                errors["base"] = "terminal_required"
            else:
                return self._finish()
        elif "terminals" in self.discovery_errors or (
            self.options.get("terminal_id")
            and self.options["terminal_id"] not in {t.id for t in self.terminals}
        ):
            errors["base"] = "discovery_partial"
        return self.async_show_form(
            step_id="terminal",
            data_schema=terminal_schema(self.options, self.terminals),
            errors=errors,
        )


class OpenWebUIConfigFlow(AgentFlow, config_entries.ConfigFlow, domain=DOMAIN):
    """Configure the preserved integration domain with native-agent options."""

    VERSION = 2
    MINOR_VERSION = 1

    def __init__(self) -> None:
        """Initialize setup state."""
        self._initialize({}, {})
        self._reauth_entry = None

    async def async_step_user(self, user_input: dict | None = None):
        """Validate credentials: model discovery, or the gateway's health check."""
        errors = {}
        if user_input is not None:
            backend = (
                entry_backend(self._reauth_entry.data)
                if self._reauth_entry
                else user_input.get(CONF_BACKEND, BACKEND_OPENWEBUI)
            )
            try:
                if backend == BACKEND_GATEWAY:
                    return await self._async_connect_gateway(user_input)
                return await self._async_connect_openwebui(user_input)
            except ValueError:
                errors["base"] = "invalid_url"
            except (OpenWebUIError, _FormError) as err:
                errors["base"] = err.key
        values = {**self.options, **self.connection}
        if CONF_GATEWAY_URL in self.connection:
            values["base_url"] = self.connection[CONF_GATEWAY_URL]
        return self.async_show_form(
            step_id="user",
            data_schema=connection_schema(
                values, backend_choice=self._reauth_entry is None
            ),
            errors=errors,
        )

    def _name_taken(self) -> bool:
        """Instance names stay unique across both backends."""
        return any(
            e.data.get("service_name") == self.connection["service_name"]
            for e in self._async_current_entries()
        )

    async def _async_connect_openwebui(self, user_input: dict):
        """Direct mode: the URL and API key must list at least one model."""
        self.connection = {
            key: user_input[key] for key in ("service_name", "base_url", "api_key")
        }
        self.connection["base_url"] = normalize_url(user_input["base_url"])
        self.options.update({key: user_input[key] for key in ("timeout", "verify_ssl")})
        client = self._client()
        self.models = await client.async_get_models()
        await client.async_get_version()
        if not self.models:
            raise _FormError("no_models")
        if self._reauth_entry:
            return self.async_update_reload_and_abort(
                self._reauth_entry,
                data_updates=self.connection,
                options=self.options,
            )
        if self._name_taken():
            return self.async_abort(reason="already_configured")
        return await self.async_step_agent()

    async def _async_connect_gateway(self, user_input: dict):
        """Gateway mode: the URL is the gateway's, the key its API client token."""
        self.connection = {
            CONF_BACKEND: BACKEND_GATEWAY,
            "service_name": user_input["service_name"],
            CONF_GATEWAY_URL: normalize_url(user_input["base_url"]),
            CONF_GATEWAY_TOKEN: user_input["api_key"],
        }
        self.options.update({key: user_input[key] for key in ("timeout", "verify_ssl")})
        if error := await self._async_check_gateway():
            raise _FormError(error)
        if self._reauth_entry:
            return self.async_update_reload_and_abort(
                self._reauth_entry,
                data_updates=self.connection,
                options=self._entry_options(),
            )
        if self._name_taken():
            return self.async_abort(reason="already_configured")
        return await self.async_step_gateway()

    def _entry_options(self) -> dict:
        """Gateway entries keep only the settings this integration still owns."""
        if entry_backend(self.connection) == BACKEND_GATEWAY:
            return {key: self.options[key] for key in GATEWAY_DEFAULT_OPTIONS}
        return self.options

    async def async_step_reauth(self, entry_data: dict):
        """Replace an expired/revoked key or token without deleting the entry."""
        self._reauth_entry = self._get_reauth_entry()
        self._initialize(entry_data, dict(self._reauth_entry.options))
        return await self.async_step_user()

    def _finish(self):
        """Save connection data separately from agent options."""
        return self.async_create_entry(
            title=f"{NAME} - {self.connection['service_name']}",
            data=self.connection,
            options=self._entry_options(),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        """Return fresh discovery for every options session."""
        return OpenWebUIOptionsFlow(config_entry)


class OpenWebUIOptionsFlow(AgentFlow, config_entries.OptionsFlow):
    """Edit all agent settings while preserving legacy and unknown options."""

    _gateway_connection_fields = True

    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        """Copy entry data; never assign the read-only config_entry property."""
        self._initialize(dict(entry.data), dict(entry.options))

    async def async_step_init(self, user_input: dict | None = None):
        """Open the refreshed model and feature settings, or the gateway settings."""
        if entry_backend(self.connection) == BACKEND_GATEWAY:
            return await self.async_step_gateway(user_input)
        return await self.async_step_agent(user_input)

    def _finish(self):
        """Persist options and trigger the entry's registered reload listener."""
        return self.async_create_entry(title="", data=self.options)
