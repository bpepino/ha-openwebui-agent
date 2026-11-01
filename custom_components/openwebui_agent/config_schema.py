"""Translated selectors shared by first setup and options."""

from typing import Any

import voluptuous as vol

from homeassistant.data_entry_flow import section
from homeassistant.helpers.selector import (
    DurationSelector,
    DurationSelectorConfig,
    EntitySelector,
    EntitySelectorConfig,
    LanguageSelector,
    LanguageSelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .client import Resource
from .const import (
    CONF_COMPLETION_TIMEOUT,
    CONF_DOOR_OPEN_AFTER,
    CONF_EVENTS,
    CONF_EVENTS_TOKEN,
    CONF_FALLBACK,
    CONF_GATEWAY_TOKEN,
    CONF_GATEWAY_URL,
    CONF_GENERIC_STATE_CHANGES,
    CONF_LANGUAGE,
    CONF_MODEL,
    CONF_PRESENCE,
    CONF_TTS_FORMAT,
    CONF_TTS_VOICE,
    CONF_UNAVAILABLE_AFTER,
    FEATURES,
    HOUSEHOLD_SECTION,
    LANGUAGES,
    NAME,
    TTS_FORMATS,
)

PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


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
    """Direct Open WebUI connection (deprecated) with a password field for the key."""
    return vol.Schema(
        {
            vol.Required("service_name", default=values.get("service_name", NAME)): str,
            vol.Required("base_url", default=values.get("base_url", "")): str,
            vol.Required("api_key"): PASSWORD,
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


def gateway_schema(values: dict) -> vol.Schema:
    """Ask for the gateway URL, an optional token (open mode needs none), a language."""
    return vol.Schema(
        {
            vol.Required(
                CONF_GATEWAY_URL, default=values.get(CONF_GATEWAY_URL, "")
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
            vol.Optional(
                CONF_GATEWAY_TOKEN,
                description={"suggested_value": values.get(CONF_GATEWAY_TOKEN)},
            ): PASSWORD,
            vol.Required(
                CONF_LANGUAGE, default=values[CONF_LANGUAGE]
            ): LanguageSelector(LanguageSelectorConfig(languages=list(LANGUAGES))),
        }
    )


def token_schema() -> vol.Schema:
    """Reauthentication asks only for a new token."""
    return vol.Schema({vol.Optional(CONF_GATEWAY_TOKEN): PASSWORD})


def household_schema(values: dict) -> vol.Schema:
    """Choose the household events pushed to the gateway (collapsed in Configure)."""
    duration = DurationSelector(DurationSelectorConfig(enable_day=False))
    return vol.Schema(
        {
            vol.Required(CONF_EVENTS, default=values[CONF_EVENTS]): bool,
            vol.Optional(CONF_PRESENCE, default=values[CONF_PRESENCE]): EntitySelector(
                EntitySelectorConfig(domain="person", multiple=True)
            ),
            vol.Required(
                CONF_DOOR_OPEN_AFTER, default=values[CONF_DOOR_OPEN_AFTER]
            ): duration,
            vol.Required(
                CONF_UNAVAILABLE_AFTER, default=values[CONF_UNAVAILABLE_AFTER]
            ): duration,
            vol.Required(
                CONF_GENERIC_STATE_CHANGES, default=values[CONF_GENERIC_STATE_CHANGES]
            ): bool,
            vol.Optional(
                CONF_EVENTS_TOKEN,
                description={"suggested_value": values[CONF_EVENTS_TOKEN] or None},
            ): PASSWORD,
        }
    )


def gateway_options_schema(values: dict) -> vol.Schema:
    """Gateway options: the wait, the voice, the fallback and the household events."""
    return vol.Schema(
        {
            vol.Required(
                CONF_COMPLETION_TIMEOUT, default=values[CONF_COMPLETION_TIMEOUT]
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=3600)),
            vol.Optional(
                CONF_TTS_VOICE,
                description={"suggested_value": values[CONF_TTS_VOICE] or None},
            ): str,
            vol.Required(CONF_TTS_FORMAT, default=values[CONF_TTS_FORMAT]): select(
                list(TTS_FORMATS), translation_key="tts_format"
            ),
            vol.Required(CONF_FALLBACK, default=values[CONF_FALLBACK]): bool,
            vol.Required(HOUSEHOLD_SECTION): section(
                household_schema(values), {"collapsed": True}
            ),
        }
    )


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
