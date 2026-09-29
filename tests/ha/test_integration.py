"""Real Home Assistant config, conversation, migration and lifecycle tests."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant import config_entries
from homeassistant.components.conversation import ConversationInput
from homeassistant.core import Context
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.openwebui_conversation import async_migrate_entry
from custom_components.openwebui_conversation.client import (
    AgentResult,
    ConversationState,
    Resource,
)
from custom_components.openwebui_conversation.const import DEFAULT_OPTIONS, DOMAIN
from custom_components.openwebui_conversation.conversation import OpenWebUIAgent
from custom_components.openwebui_conversation.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.openwebui_conversation.exceptions import (
    AuthenticationError,
    CompletionTimeout,
    PermissionDenied,
    ToolUnavailable,
)

CONNECTION = {
    "service_name": "Test",
    "base_url": "https://example.test",
    "api_key": "fake-key",
}
MODELS = [{"id": "custom-model", "name": "My Agent"}]


@pytest.fixture
def client():
    """Mock only network I/O; run Home Assistant's actual flows."""
    with patch(
        "custom_components.openwebui_conversation.config_flow.OpenWebUIClient",
        autospec=True,
    ) as cls:
        obj = cls.return_value
        obj.async_get_models.return_value = MODELS
        obj.async_get_version.return_value = "0.11.4"
        obj.async_get_tools.return_value = [Resource("server:mcp:exact", "MCP", "mcp")]
        obj.async_get_terminals.return_value = []
        yield obj


async def test_setup_flow(hass, client):
    """First run validates auth and dynamically populates the model selector."""
    with patch(
        "custom_components.openwebui_conversation.async_setup_entry", return_value=True
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["step_id"] == "user"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**CONNECTION, "timeout": 30, "verify_ssl": True}
        )
        assert result["step_id"] == "agent"
        schema = result["data_schema"].schema
        selector = next(v for k, v in schema.items() if k.schema == "chat_model")
        assert selector.config["options"] == [
            {"value": "custom-model", "label": "My Agent"}
        ]
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                **{
                    k: v
                    for k, v in DEFAULT_OPTIONS.items()
                    if k not in ("tool_ids", "tool_mode", "terminal_mode")
                },
                "chat_model": "custom-model",
            },
        )
        assert result["step_id"] == "tools"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"tool_mode": "custom", "tool_ids": ["server:mcp:exact"]}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"terminal_mode": "none"}
        )
        assert result["type"] == "create_entry"
        assert result["data"]["api_key"] == "fake-key"
        assert result["options"]["tool_ids"] == ["server:mcp:exact"]


async def test_invalid_auth(hass, client):
    """Translate a rejected key rather than reporting a generic connection failure."""
    client.async_get_models.side_effect = AuthenticationError()
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
        data={**CONNECTION, "timeout": 30, "verify_ssl": True},
    )
    assert result["errors"] == {"base": "invalid_auth"}


async def test_options_preserve_missing_resources(hass, client):
    """Refresh tools and terminals, preserving old IDs after discovery failure."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=CONNECTION,
        options={
            **DEFAULT_OPTIONS,
            "chat_model": "custom-model",
            "tool_ids": ["missing-tool"],
        },
        version=2,
    )
    entry.add_to_hass(hass)
    client.async_get_tools.side_effect = PermissionDenied()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "agent"
    client.async_get_models.assert_awaited()
    client.async_get_tools.assert_awaited()
    client.async_get_terminals.assert_awaited()
    options = {
        k: v
        for k, v in entry.options.items()
        if k not in ("tool_mode", "tool_ids", "terminal_mode")
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], options
    )
    assert result["step_id"] == "tools"
    assert result["errors"] == {"base": "discovery_partial"}


async def test_entry_migration(hass):
    """Migrate a real config entry in place with credentials and identity intact."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=CONNECTION,
        options={
            "chat_model": "custom-model",
            "search_enabled": True,
            "search_sentences": "find {query}",
        },
        version=1,
    )
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    assert entry.version == 2 and entry.data == CONNECTION
    assert (
        entry.options["web_search"]
        and entry.options["search_sentences"] == "find {query}"
    )


async def test_assist_final_response_and_translated_error(hass):
    """Exercise HA's managed ChatLog, including its stable conversation ID."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=CONNECTION,
        options={**DEFAULT_OPTIONS, "chat_model": "custom-model"},
    )
    entry.add_to_hass(hass)
    manager = SimpleNamespace(
        async_process=AsyncMock(
            return_value=AgentResult(
                "**Done.**",
                {},
                ConversationState("chat", "session", "assistant", "custom-model"),
            )
        )
    )
    hass.data[DOMAIN] = {entry.entry_id: manager}
    agent = OpenWebUIAgent(hass, entry)
    agent.entity_id = "conversation.openwebui_agent"
    user = ConversationInput(
        text="Hello",
        context=Context(),
        conversation_id=None,
        device_id=None,
        satellite_id=None,
        language="en",
        agent_id=agent.entity_id,
    )
    result = await agent.async_process(user)
    assert result.response.speech["plain"]["speech"] == "Done."
    assert result.conversation_id
    assert manager.async_process.call_args.args[0] == result.conversation_id
    conversation_id = result.conversation_id
    manager.async_process.return_value.text = "Which light?"
    user.conversation_id = conversation_id
    result = await agent.async_process(user)
    assert result.continue_conversation is True
    assert result.conversation_id == conversation_id
    assert manager.async_process.call_args.args[0] == conversation_id
    manager.async_process.return_value.text = "Okay."
    user.text = "Let's talk!"
    result = await agent.async_process(user)
    assert result.continue_conversation is True
    user.text = "Tell me something"
    result = await agent.async_process(user)
    assert result.continue_conversation is True
    user.conversation_id = None
    result = await agent.async_process(user)
    assert result.continue_conversation is False
    user.conversation_id = conversation_id
    user.text = "End conversation."
    result = await agent.async_process(user)
    assert result.continue_conversation is False
    user.text = "Hello"
    result = await agent.async_process(user)
    assert result.continue_conversation is False
    hass.config_entries.async_update_entry(
        entry, options={"conversation_mode": "always"}
    )
    result = await agent.async_process(user)
    assert result.continue_conversation is True
    manager.async_process.side_effect = CompletionTimeout()
    result = await agent.async_process(user)
    assert "timed out" in result.response.speech["plain"]["speech"]
    assert result.continue_conversation is False
    manager.async_process.side_effect = ToolUnavailable(["missing-tool"])
    result = await agent.async_process(user)
    assert "missing-tool" in result.response.speech["plain"]["speech"]
    assert "OAuth" not in result.response.speech["plain"]["speech"]


async def test_diagnostics_allowlist(hass):
    """Credentials, server URL and user-selected identifiers never enter diagnostics."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=CONNECTION,
        options={
            **DEFAULT_OPTIONS,
            "tool_ids": ["private-tool"],
            "chat_model": "private-model",
        },
    )
    hass.data[DOMAIN] = {
        entry.entry_id: SimpleNamespace(
            client=SimpleNamespace(server_version="0.11.4"),
            states={"private-conversation": "private-chat"},
            last_timings={"total_s": 12.5, "completion_wait_s": 12.1},
            cleaner=None,
        )
    }
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["active_conversation_count"] == 1
    assert result["last_turn_timing_seconds"] == {
        "total_s": 12.5,
        "completion_wait_s": 12.1,
    }
    assert all(
        secret not in str(result) for secret in ("fake-key", "example.test", "private-")
    )


async def test_setup_unload_and_auth_failure(hass):
    """Set up and unload the real platform without polling a health coordinator."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=CONNECTION,
        options={**DEFAULT_OPTIONS, "chat_model": "custom-model"},
        version=2,
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.openwebui_conversation.OpenWebUIClient", autospec=True
    ) as cls:
        cls.return_value.async_get_models.return_value = MODELS
        cls.return_value.async_get_version.return_value = "0.11.4"
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.entry_id in hass.data[DOMAIN]
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert entry.entry_id not in hass.data[DOMAIN]


async def test_reauth_preserves_entry(hass, client):
    """Replace credentials using HA's real reauth flow without losing options."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=CONNECTION,
        options={**DEFAULT_OPTIONS, "chat_model": "custom-model"},
        version=2,
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=CONNECTION,
    )
    with patch.object(hass.config_entries, "async_schedule_reload"):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                **CONNECTION,
                "api_key": "replacement-fake-key",
                "timeout": 42,
                "verify_ssl": False,
            },
        )
    assert result["type"] == "abort" and result["reason"] == "reauth_successful"
    assert entry.data["api_key"] == "replacement-fake-key"
    assert (
        entry.options["chat_model"] == "custom-model" and entry.options["timeout"] == 42
    )


async def test_options_save_retains_legacy_fields(hass, client):
    """Complete all options steps without discarding unrecognized saved values."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=CONNECTION,
        options={
            **DEFAULT_OPTIONS,
            "chat_model": "custom-model",
            "search_sentences": "legacy",
        },
        version=2,
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "chat_model": "custom-model",
            "memory": False,
            "web_search": True,
            "code_interpreter": False,
            "image_generation": False,
            "strip_markdown": True,
            "verify_ssl": True,
            "timeout": 30,
            "completion_timeout": 180,
            "poll_interval": 3,
            "thinking_mode": "disabled",
            "keep_chat_history": True,
        },
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tool_mode": "custom", "tool_ids": []}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"terminal_mode": "none"}
    )
    assert result["type"] == "create_entry"
    assert entry.options["search_sentences"] == "legacy"
    assert entry.options["thinking_mode"] == "disabled"
    assert entry.options["keep_chat_history"] is True
    assert (
        entry.options["memory"] is False and entry.options["completion_timeout"] == 180
    )


async def test_setup_auth_failure_requests_reauth(hass):
    """Invalid stored credentials enter HA's authentication failure state."""
    entry = MockConfigEntry(domain=DOMAIN, data=CONNECTION, version=2)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.openwebui_conversation.OpenWebUIClient", autospec=True
    ) as cls:
        cls.return_value.async_get_models.side_effect = AuthenticationError()
        assert not await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is config_entries.ConfigEntryState.SETUP_ERROR
        assert entry.entry_id not in hass.data.get(DOMAIN, {})


@pytest.mark.parametrize("keep", [False, True])
async def test_history_policy_restores_or_cancels_cleanup(hass, hass_storage, keep):
    """Use HA storage and lifecycle to restore deadlines or honor keep-history."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=CONNECTION,
        options={
            **DEFAULT_OPTIONS,
            "chat_model": "custom-model",
            "keep_chat_history": keep,
        },
        version=2,
    )
    entry.add_to_hass(hass)
    key = f"{DOMAIN}.{entry.entry_id}.chat_cleanup"
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {"chats": {"owned-chat": 9999999999.0}},
    }
    with patch(
        "custom_components.openwebui_conversation.OpenWebUIClient", autospec=True
    ) as cls:
        cls.return_value.async_get_models.return_value = MODELS
        cls.return_value.async_get_version.return_value = "0.11.4"
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        manager = hass.data[DOMAIN][entry.entry_id]
        if keep:
            assert manager.cleaner is None
            assert key not in hass_storage
        else:
            assert manager.cleaner.pending_count == 1
        cls.return_value.async_delete_chat.assert_not_awaited()
        assert await hass.config_entries.async_unload(entry.entry_id)
        if not keep:
            assert hass_storage[key]["data"]["chats"] == {"owned-chat": 9999999999.0}
