"""One config flow: the home AI gateway, or (deprecated) Open WebUI directly.

Gateway: the URL, an optional token and the language, then a test step that
shows the gateway's version and whether it runs in open mode. Options: the turn
timeout, the text-to-speech voice and format, the fallback to Home Assistant's
own agent while the gateway is unreachable, and the household events pushed to
the gateway. The gateway owns the model, tools, features, identity, chat
history and the voice pipeline.

Direct Open WebUI mode keeps its discovery and agent steps for one more release.
"""

import asyncio
from copy import deepcopy

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import OpenWebUIClient, normalize_url
from .config_schema import (
    connection_schema,
    gateway_options_schema,
    gateway_schema,
    model_schema,
    terminal_schema,
    token_schema,
    tools_schema,
)
from .const import (
    BACKEND_GATEWAY,
    BACKEND_OPENWEBUI,
    BACKENDS,
    CONF_BACKEND,
    CONF_EVENTS_TOKEN,
    CONF_GATEWAY_TOKEN,
    CONF_GATEWAY_URL,
    CONF_LANGUAGE,
    CONF_MODEL,
    CONF_TTS_VOICE,
    DEFAULT_OPTIONS,
    DOMAIN,
    GATEWAY_DEFAULT_OPTIONS,
    GATEWAY_OPTIONS,
    GATEWAY_TITLE,
    HOUSEHOLD_SECTION,
    NAME,
    default_language,
    entry_backend,
)
from .exceptions import OpenWebUIError
from .gateway import create_gateway_client
from .gateway_client import GatewayStatus


class _FormError(Exception):
    """A form error key raised by a connection helper."""

    def __init__(self, key: str) -> None:
        """Keep only the translation key."""
        super().__init__(key)
        self.key = key


def gateway_placeholders(status: GatewayStatus) -> dict[str, str]:
    """Describe a gateway check for the test step (no URL or token)."""
    health = status.status
    if status.degraded:
        health = f"{health}; unavailable: {', '.join(status.degraded)}"
    return {
        "version": status.version or "unknown",
        "status": health,
        "access": "open mode, no token needed on your network"
        if status.open_mode
        else "tokens required, and this token was accepted",
    }


class AgentFlow:
    """Shared configuration-only steps; no protocol or tool execution."""

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
        self.gateway_status: GatewayStatus | None = None

    def _client(self) -> OpenWebUIClient:
        """Reuse Home Assistant's session with the current connection settings."""
        return OpenWebUIClient(
            self.connection["base_url"],
            self.connection["api_key"],
            async_get_clientsession(self.hass),
            self.options["timeout"],
            self.options["verify_ssl"],
        )

    async def _async_test_gateway(self) -> str | None:
        """Check health and access with the working settings; an error key or None."""
        client = create_gateway_client(self.hass, self.connection, self.options)
        try:
            self.gateway_status = await client.async_check()
        except OpenWebUIError as err:
            return err.key
        return None

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
    """Configure the gateway (recommended) or the deprecated direct mode."""

    VERSION = 2
    MINOR_VERSION = 1

    def __init__(self) -> None:
        """Initialize setup state."""
        self._initialize({}, {})
        self._reauth_entry = None

    async def async_step_user(self, user_input: dict | None = None):
        """Choose the gateway, or Open WebUI directly for one more release."""
        return self.async_show_menu(step_id="user", menu_options=list(BACKENDS))

    async def async_step_gateway(self, user_input: dict | None = None):
        """Ask for the gateway's URL, an optional token and the language."""
        return await self._async_gateway_form("gateway", user_input)

    async def async_step_reconfigure(self, user_input: dict | None = None):
        """Change the gateway URL, token or language without losing the entry."""
        entry = self._get_reconfigure_entry()
        if entry_backend(entry.data) != BACKEND_GATEWAY:
            return self.async_abort(reason="reconfigure_direct")
        if user_input is None:
            self._initialize(dict(entry.data), dict(entry.options))
        return await self._async_gateway_form("reconfigure", user_input)

    async def _async_gateway_form(self, step_id: str, user_input: dict | None):
        """Validate the gateway, then show what the test found."""
        errors = {}
        if user_input is not None:
            try:
                url = normalize_url(user_input[CONF_GATEWAY_URL])
            except ValueError:
                errors["base"] = "invalid_url"
            else:
                self._async_abort_entries_match({CONF_GATEWAY_URL: url})
                self.connection = {
                    CONF_BACKEND: BACKEND_GATEWAY,
                    CONF_GATEWAY_URL: url,
                    CONF_GATEWAY_TOKEN: user_input.get(CONF_GATEWAY_TOKEN, "").strip(),
                    CONF_LANGUAGE: user_input[CONF_LANGUAGE],
                }
                if (error := await self._async_test_gateway()) is None:
                    return await self.async_step_test()
                errors["base"] = error
        values = {
            CONF_LANGUAGE: default_language(self.hass.config.language),
            **self.connection,
            **(user_input or {}),
        }
        return self.async_show_form(
            step_id=step_id,
            data_schema=gateway_schema(values),
            errors=errors,
            # Translation strings may not contain URLs.
            description_placeholders={"example": "http://192.168.1.20:8000"},
        )

    async def async_step_test(self, user_input: dict | None = None):
        """Show the gateway's version and access mode; submitting saves the entry."""
        if user_input is None:
            return self.async_show_form(
                step_id="test",
                description_placeholders=gateway_placeholders(self.gateway_status),
            )
        if self.source == config_entries.SOURCE_RECONFIGURE:
            return self.async_update_reload_and_abort(
                self._get_reconfigure_entry(), data=self.connection
            )
        return self.async_create_entry(
            title=GATEWAY_TITLE,
            data=self.connection,
            options={key: GATEWAY_DEFAULT_OPTIONS[key] for key in GATEWAY_OPTIONS},
        )

    async def async_step_openwebui(self, user_input: dict | None = None):
        """Connect directly (deprecated): the URL and API key must list a model."""
        errors = {}
        if user_input is not None:
            try:
                return await self._async_connect_openwebui(user_input)
            except ValueError:
                errors["base"] = "invalid_url"
            except (OpenWebUIError, _FormError) as err:
                errors["base"] = err.key
        return self.async_show_form(
            step_id="openwebui",
            data_schema=connection_schema({**self.options, **self.connection}),
            errors=errors,
        )

    def _name_taken(self) -> bool:
        """Instance names stay unique across both backends."""
        return any(
            e.data.get("service_name") == self.connection["service_name"]
            for e in self._async_current_entries()
        )

    async def _async_connect_openwebui(self, user_input: dict):
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

    async def async_step_reauth(self, entry_data: dict):
        """Replace an expired/revoked key or token without deleting the entry."""
        self._reauth_entry = self._get_reauth_entry()
        self._initialize(entry_data, dict(self._reauth_entry.options))
        if entry_backend(entry_data) == BACKEND_GATEWAY:
            return await self.async_step_reauth_confirm()
        return await self.async_step_openwebui()

    async def async_step_reauth_confirm(self, user_input: dict | None = None):
        """Ask for a token: the gateway now requires one, or rejected the saved one."""
        errors = {}
        if user_input is not None:
            token = user_input.get(CONF_GATEWAY_TOKEN, "").strip()
            self.connection[CONF_GATEWAY_TOKEN] = token
            if (error := await self._async_test_gateway()) is None:
                return self.async_update_reload_and_abort(
                    self._reauth_entry, data_updates={CONF_GATEWAY_TOKEN: token}
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=token_schema(), errors=errors
        )

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
    """Edit settings while preserving legacy and unknown options."""

    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        """Copy entry data; never assign the read-only config_entry property."""
        self._initialize(dict(entry.data), dict(entry.options))

    async def async_step_init(self, user_input: dict | None = None):
        """Gateway: wait, voice and household events. Direct: model and features."""
        if entry_backend(self.connection) == BACKEND_OPENWEBUI:
            return await self.async_step_agent(user_input)
        if user_input is not None:
            household = dict(user_input[HOUSEHOLD_SECTION])
            self.options.update(
                {k: v for k, v in user_input.items() if k != HOUSEHOLD_SECTION}
            )
            self.options.update(household)
            # A cleared voice field means the gateway's speech profile voice,
            # a cleared events token sends events without one (open mode).
            self.options[CONF_TTS_VOICE] = user_input.get(CONF_TTS_VOICE, "").strip()
            self.options[CONF_EVENTS_TOKEN] = household.get(
                CONF_EVENTS_TOKEN, ""
            ).strip()
            return self._finish()
        return self.async_show_form(
            step_id="init", data_schema=gateway_options_schema(self.options)
        )

    def _finish(self):
        """Persist options and trigger the entry's registered reload listener."""
        return self.async_create_entry(title="", data=self.options)
