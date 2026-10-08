"""Translated selectors shared by first setup and options."""

from typing import Any

import voluptuous as vol

from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .client import Resource
from .const import (
    BACKEND_OPENWEBUI,
    BACKENDS,
    CONF_BACKEND,
    CONF_MODEL,
    FEATURES,
    NAME,
)


def select(
    options: list, *, multiple: bool = False, translation_key: str | None = None
) -> SelectSelector:
    """Create a dropdown which never requires manually entering identifiers."""
    config = SelectSelectorConfig(
        options=options,
        multiple=multiple,
        custom_value=False,
        mode=SelectSelectorMode.DROPDOWN,
    )
    if translation_key:
        config["translation_key"] = translation_key
    return SelectSelector(config)


def connection_schema(values: dict, *, backend_choice: bool = True) -> vol.Schema:
    """Build a connection form with a password selector for the API key.

    The URL and key fields serve both backends: the Open WebUI URL and API key,
    or the gateway URL and its API client token. Reauthentication keeps the
    entry's backend, so it hides the choice.
    """
    schema: dict[Any, Any] = {}
    if backend_choice:
        schema[
            vol.Required(
                CONF_BACKEND, default=values.get(CONF_BACKEND, BACKEND_OPENWEBUI)
            )
        ] = select(list(BACKENDS), translation_key="backend")
    return vol.Schema(
        {
            **schema,
            vol.Required("service_name", default=values.get("service_name", NAME)): str,
            vol.Required("base_url", default=values.get("base_url", "")): str,
            vol.Required("api_key"): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
            vol.Required("verify_ssl", default=values.get("verify_ssl", True)): bool,
            vol.Required("timeout", default=values.get("timeout", 30)): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=300)
            ),
        }
    )


def model_schema(values: dict, models: list[dict]) -> vol.Schema:
    """Model, native features and voice/request settings."""
    options = [
        {"value": item["id"], "label": item.get("name") or item["id"]}
        for item in models
    ]
    selected = values.get(CONF_MODEL)
    if selected and selected not in {item["id"] for item in models}:
        options.append({"value": selected, "label": selected})
    model_field = (
        vol.Required(CONF_MODEL, default=selected)
        if selected
        else vol.Required(CONF_MODEL)
    )
    schema: dict[Any, Any] = {model_field: select(options)}
    schema[vol.Required("thinking_mode", default=values["thinking_mode"])] = select(
        ["model", "disabled"], translation_key="thinking_mode"
    )
    schema[vol.Required("conversation_mode", default=values["conversation_mode"])] = (
        select(["questions", "always"], translation_key="conversation_mode")
    )
    for key in (*FEATURES, "strip_markdown", "verify_ssl", "keep_chat_history"):
        schema[vol.Required(key, default=values[key])] = bool
    for key, maximum in (
        ("timeout", 300),
        ("completion_timeout", 3600),
        ("poll_interval", 30),
    ):
        schema[vol.Required(key, default=values[key])] = vol.All(
            vol.Coerce(int), vol.Range(min=1, max=maximum)
        )
    return vol.Schema(schema)


def gateway_schema(values: dict, *, connection: bool) -> vol.Schema:
    """Gateway mode: only how HA waits and listens; the gateway owns the agent."""
    schema: dict[Any, Any] = {}
    if connection:
        schema[vol.Required("verify_ssl", default=values["verify_ssl"])] = bool
        schema[vol.Required("timeout", default=values["timeout"])] = vol.All(
            vol.Coerce(int), vol.Range(min=1, max=300)
        )
    schema[vol.Required("completion_timeout", default=values["completion_timeout"])] = (
        vol.All(vol.Coerce(int), vol.Range(min=1, max=3600))
    )
    schema[vol.Required("conversation_mode", default=values["conversation_mode"])] = (
        select(["questions", "always"], translation_key="conversation_mode")
    )
    schema[vol.Required("strip_markdown", default=values["strip_markdown"])] = bool
    return vol.Schema(schema)


def resource_options(resources: list[Resource], saved: list[str]) -> list[dict]:
    """Keep saved IDs selectable during temporary discovery/access failures."""
    options = [{"value": item.id, "label": item.name} for item in resources]
    known = {item.id for item in resources}
    options.extend(
        {"value": item, "label": item} for item in saved if item not in known
    )
    return options


def tools_schema(values: dict, resources: list[Resource]) -> vol.Schema:
    """Separate model defaults from explicit selection without OpenAI tools."""
    return vol.Schema(
        {
            vol.Required("tool_mode", default=values.get("tool_mode", "model")): select(
                ["model", "custom"], translation_key="tool_mode"
            ),
            vol.Required("tool_ids", default=values.get("tool_ids", [])): select(
                resource_options(resources, values.get("tool_ids", [])), multiple=True
            ),
        }
    )


def terminal_schema(values: dict, resources: list[Resource]) -> vol.Schema:
    """Offer none, model default or an accessible server terminal."""
    saved = [values["terminal_id"]] if values.get("terminal_id") else []
    schema = {
        vol.Required(
            "terminal_mode", default=values.get("terminal_mode", "none")
        ): select(["none", "model", "custom"], translation_key="terminal_mode"),
    }
    if resources or saved:
        schema[
            vol.Optional(
                "terminal_id",
                description={"suggested_value": values.get("terminal_id")},
            )
        ] = select(resource_options(resources, saved))
    return vol.Schema(schema)
