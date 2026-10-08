"""Gateway mode with real Home Assistant fixtures: config, Assist, lifecycle."""

from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

import pytest

from homeassistant import config_entries
from homeassistant.components.conversation import ConversationInput
from homeassistant.components.conversation import chat_log as chat_log_module
from homeassistant.core import Context
from homeassistant.helpers import area_registry as ar, device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.openwebui_agent.const import DOMAIN, GATEWAY_DEFAULT_OPTIONS
from custom_components.openwebui_agent.conversation import OpenWebUIAgent
from custom_components.openwebui_agent.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.openwebui_agent.exceptions import (
    GatewayUnauthorized,
    GatewayUnreachable,
    NonSpeechError,
    TurnStillRunning,
)
from custom_components.openwebui_agent.gateway_client import TurnResult

GATEWAY_DATA = {
    "backend": "gateway",
    "service_name": "Gateway",
    "gateway_url": "http://gateway.test:8000",
    "gateway_token": "fake-gateway-token",
}
CONNECTION_FORM = {
    "backend": "gateway",
    "service_name": "Gateway",
    "base_url": "http://gateway.test:8000/",
    "api_key": "fake-gateway-token",
    "timeout": 20,
    "verify_ssl": False,
}
HEALTH = {
    "status": "ok",
    "openwebui": {
        "reachable": True,
        "version": "0.11.4",
        "supported_version": "0.11.4",
        "version_ok": True,
    },
    "voice_pipeline": True,
    "retention_running": True,
}


@pytest.fixture
def gateway():
    """Mock only the gateway's network I/O in the config flow."""
    with (
        patch(
            "custom_components.openwebui_agent.config_flow.GatewayClient",
            autospec=True,
        ) as cls,
        patch(
            "custom_components.openwebui_agent.config_flow.OpenWebUIClient",
            autospec=True,
        ) as owui,
    ):
        cls.return_value.async_health.return_value = HEALTH
        yield cls
        owui.assert_not_called()


def _fields(result) -> set[str]:
    return {key.schema for key in result["data_schema"].schema}


async def test_gateway_setup_flow(hass, gateway):
    """URL and token on the connection step, then only voice timing settings."""
    with patch(
        "custom_components.openwebui_agent.async_setup_entry", return_value=True
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["step_id"] == "user"
        assert "backend" in _fields(result)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CONNECTION_FORM
        )
        assert result["step_id"] == "gateway"
        assert _fields(result) == {
            "completion_timeout",
            "conversation_mode",
            "strip_markdown",
        }
        assert "0.11.4" in result["description_placeholders"]["status"]
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "completion_timeout": 60,
                "conversation_mode": "questions",
                "strip_markdown": True,
            },
        )
    assert result["type"] == "create_entry"
    assert result["data"] == GATEWAY_DATA
    assert result["options"] == {
        "timeout": 20,
        "verify_ssl": False,
        "strip_markdown": True,
        "completion_timeout": 60,
        "conversation_mode": "questions",
    }
    gateway.assert_called_with(
        "http://gateway.test:8000", "fake-gateway-token", ANY, 20, False
    )


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (GatewayUnauthorized(), "gateway_unauthorized"),
        (GatewayUnreachable(), "gateway_unreachable"),
    ],
)
async def test_gateway_connection_errors(hass, gateway, error, key):
    """A rejected token or unreachable gateway stays on the connection form."""
    gateway.return_value.async_health.side_effect = error
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
        data=CONNECTION_FORM,
    )
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": key}


async def test_gateway_options_hide_open_webui_settings(hass, gateway):
    """The gateway owns model, tools, features, thinking and history."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=GATEWAY_DATA,
        options=dict(GATEWAY_DEFAULT_OPTIONS),
        version=2,
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "gateway"
    assert _fields(result) == {
        "verify_ssl",
        "timeout",
        "completion_timeout",
        "conversation_mode",
        "strip_markdown",
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "verify_ssl": True,
            "timeout": 30,
            "completion_timeout": 45,
            "conversation_mode": "always",
            "strip_markdown": False,
        },
    )
    assert result["type"] == "create_entry"
    assert entry.options["completion_timeout"] == 45
    assert entry.options["conversation_mode"] == "always"
    assert "chat_model" not in entry.options and "tool_ids" not in entry.options


async def test_gateway_reauth_replaces_token(hass, gateway):
    """Reauthentication keeps the backend and stores the new token."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=GATEWAY_DATA,
        options=dict(GATEWAY_DEFAULT_OPTIONS),
        version=2,
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=GATEWAY_DATA,
    )
    assert result["step_id"] == "user"
    assert "backend" not in _fields(result)
    with patch.object(hass.config_entries, "async_schedule_reload"):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "service_name": "Gateway",
                "base_url": "http://gateway.test:8000",
                "api_key": "replacement-gateway-token",
                "timeout": 30,
                "verify_ssl": True,
            },
        )
    assert result["type"] == "abort" and result["reason"] == "reauth_successful"
    assert entry.data["backend"] == "gateway"
    assert entry.data["gateway_token"] == "replacement-gateway-token"
    assert "api_key" not in entry.data


async def test_gateway_assist_turn(hass):
    """Forward HA's context; speak speech_text, log display_text, honor expects_reply."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=GATEWAY_DATA, options=dict(GATEWAY_DEFAULT_OPTIONS)
    )
    entry.add_to_hass(hass)
    area = ar.async_get(hass).async_create("Living Room")
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "satellite")}
    )
    dr.async_get(hass).async_update_device(device.id, area_id=area.id)
    client = SimpleNamespace(
        async_turn=AsyncMock(
            return_value=TurnResult(
                display_text="**Done.** See [the log](https://example.test).",
                speech_text="Done.",
                expects_reply=True,
                conversation_id="ignored",
                new_chat=True,
            )
        )
    )
    hass.data[DOMAIN] = {entry.entry_id: client}
    agent = OpenWebUIAgent(hass, entry)
    agent.entity_id = "conversation.openwebui_agent"
    user = ConversationInput(
        text="Turn on the light",
        context=Context(user_id="ha-user-1"),
        conversation_id=None,
        device_id=device.id,
        satellite_id="assist_satellite.living_room",
        language="en",
        agent_id=agent.entity_id,
        extra_system_prompt="Keep it short.",
    )
    logged: list[str] = []
    original = chat_log_module.ChatLog.async_add_assistant_content_without_tools

    def capture(self, content):
        logged.append(content.content)
        return original(self, content)

    with patch.object(
        chat_log_module.ChatLog, "async_add_assistant_content_without_tools", capture
    ):
        result = await agent.async_process(user)
    assert result.response.speech["plain"]["speech"] == "Done."
    assert logged == ["Done. See the log."]
    # The reply does not end in a question; the gateway's expects_reply decides.
    assert result.continue_conversation is True
    assert client.async_turn.call_args.args == ("Turn on the light",)
    assert client.async_turn.call_args.kwargs == {
        "conversation_id": result.conversation_id,
        "language": "en",
        "user_id": "ha-user-1",
        "device_id": device.id,
        "satellite_id": "assist_satellite.living_room",
        "area_id": "Living Room",
        "extra_instructions": "Keep it short.",
        "timeout_s": 120,
    }

    user.conversation_id = result.conversation_id
    client.async_turn.return_value = TurnResult("Okay.", "Okay.", False, None)
    result = await agent.async_process(user)
    assert result.continue_conversation is False
    assert client.async_turn.call_args.kwargs["conversation_id"] == (
        user.conversation_id
    )

    user.text = "That's all"
    client.async_turn.return_value = TurnResult(
        "Anything else?", "Anything else?", True, None
    )
    result = await agent.async_process(user)
    assert result.continue_conversation is False  # stop phrases win

    client.async_turn.side_effect = TurnStillRunning()
    result = await agent.async_process(user)
    assert "still working" in result.response.speech["plain"]["speech"]
    assert result.continue_conversation is False
    client.async_turn.side_effect = NonSpeechError()
    result = await agent.async_process(user)
    assert result.response.speech["plain"]["speech"] == "I didn't catch any speech."


async def test_gateway_setup_unload(hass):
    """Gateway entries never construct the Open WebUI client or chat cleanup."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=GATEWAY_DATA,
        options=dict(GATEWAY_DEFAULT_OPTIONS),
        version=2,
    )
    entry.add_to_hass(hass)
    with (
        patch("custom_components.openwebui_agent.GatewayClient", autospec=True) as cls,
        patch("custom_components.openwebui_agent.OpenWebUIClient") as owui,
    ):
        cls.return_value.async_health.return_value = HEALTH
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert hass.data[DOMAIN][entry.entry_id] is cls.return_value
        owui.assert_not_called()
        assert await hass.config_entries.async_unload(entry.entry_id)
        cls.return_value.async_close.assert_awaited()
        assert entry.entry_id not in hass.data[DOMAIN]


async def test_gateway_setup_auth_failure_requests_reauth(hass):
    """A revoked API client token enters HA's authentication failure state."""
    entry = MockConfigEntry(domain=DOMAIN, data=GATEWAY_DATA, version=2)
    entry.add_to_hass(hass)
    with patch("custom_components.openwebui_agent.GatewayClient", autospec=True) as cls:
        cls.return_value.async_health.side_effect = GatewayUnauthorized()
        assert not await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is config_entries.ConfigEntryState.SETUP_ERROR
        assert entry.entry_id not in hass.data.get(DOMAIN, {})


async def test_gateway_diagnostics(hass):
    """Backend and live health, never the token or URL."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=GATEWAY_DATA, options=dict(GATEWAY_DEFAULT_OPTIONS)
    )
    client = SimpleNamespace(
        async_health=AsyncMock(return_value=HEALTH),
        last_timings={"total_ms": 2352},
        last_confidence="verified",
    )
    hass.data[DOMAIN] = {entry.entry_id: client}
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["backend"] == "gateway"
    assert result["gateway_health"] == HEALTH
    assert result["last_turn_timing_ms"] == {"total_ms": 2352}
    assert all(
        secret not in str(result) for secret in ("fake-gateway-token", "gateway.test")
    )
    client.async_health.side_effect = GatewayUnreachable()
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["gateway_health"] == {"error": "gateway_unreachable"}
