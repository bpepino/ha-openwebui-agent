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
from .const import CONF_MODEL, FEATURES, NAME


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


def connection_schema(values: dict) -> vol.Schema:
    """Build a connection form with a password selector for the API key."""
    return vol.Schema(
        {
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
    for key in (*FEATURES, "strip_markdown", "verify_ssl"):
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
