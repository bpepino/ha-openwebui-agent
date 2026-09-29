"""Connection, discovery and native agent options for Open WebUI Agent."""

import asyncio
from copy import deepcopy

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import OpenWebUIClient, normalize_url
from .config_schema import (
    connection_schema,
    model_schema,
    terminal_schema,
    tools_schema,
)
from .const import CONF_MODEL, DEFAULT_OPTIONS, DOMAIN, NAME
from .exceptions import OpenWebUIError


class AgentFlow:
    """Shared configuration-only steps; no protocol or tool execution."""

    def _initialize(self, data: dict, options: dict) -> None:
        """Set working copies without modifying a saved entry."""
        self.connection = dict(data)
        self.options = {**deepcopy(DEFAULT_OPTIONS), **options}
        self.models = []
        self.tools = []
        self.terminals = []
        self.discovery_errors = {}
        self.loaded = False

    def _client(self) -> OpenWebUIClient:
        """Reuse Home Assistant's session with the current connection settings."""
        return OpenWebUIClient(
            self.connection["base_url"],
            self.connection["api_key"],
            async_get_clientsession(self.hass),
            self.options["timeout"],
            self.options["verify_ssl"],
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
        """Validate credentials through authenticated model discovery."""
        errors = {}
        if user_input is not None:
            try:
                self.connection = {
                    key: user_input[key]
                    for key in ("service_name", "base_url", "api_key")
                }
                self.connection["base_url"] = normalize_url(user_input["base_url"])
                self.options.update(
                    {key: user_input[key] for key in ("timeout", "verify_ssl")}
                )
                client = self._client()
                self.models = await client.async_get_models()
                await client.async_get_version()
                if not self.models:
                    errors["base"] = "no_models"
                elif self._reauth_entry:
                    return self.async_update_reload_and_abort(
                        self._reauth_entry,
                        data_updates=self.connection,
                        options=self.options,
                    )
                elif any(
                    e.data.get("service_name") == self.connection["service_name"]
                    for e in self._async_current_entries()
                ):
                    return self.async_abort(reason="already_configured")
                else:
                    return await self.async_step_agent()
            except ValueError:
                errors["base"] = "invalid_url"
            except OpenWebUIError as err:
                errors["base"] = err.key
        return self.async_show_form(
            step_id="user",
            data_schema=connection_schema({**self.options, **self.connection}),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict):
        """Replace an expired/revoked key without deleting the existing entry."""
        self._reauth_entry = self._get_reauth_entry()
        self._initialize(entry_data, dict(self._reauth_entry.options))
        return await self.async_step_user()

    def _finish(self):
        """Save connection data separately from agent options."""
        return self.async_create_entry(
            title=f"{NAME} - {self.connection['service_name']}",
            data=self.connection,
            options=self.options,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        """Return fresh discovery for every options session."""
        return OpenWebUIOptionsFlow(config_entry)


class OpenWebUIOptionsFlow(AgentFlow, config_entries.OptionsFlow):
    """Edit all agent settings while preserving legacy and unknown options."""

    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        """Copy entry data; never assign the read-only config_entry property."""
        self._initialize(dict(entry.data), dict(entry.options))

    async def async_step_init(self, user_input: dict | None = None):
        """Open the refreshed model and feature settings."""
        return await self.async_step_agent(user_input)

    def _finish(self):
        """Persist options and trigger the entry's registered reload listener."""
        return self.async_create_entry(title="", data=self.options)
