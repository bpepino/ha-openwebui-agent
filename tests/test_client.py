"""Exercise the saved-chat protocol against a simulated Open WebUI HTTP server."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import aiohttp
import pytest

from owui_protocol.client import OpenWebUIClient, final_text, normalize_url
from owui_protocol.exceptions import (
    AgentFailedError,
    AuthenticationError,
    BrowserToolUnsupported,
    CompletionTimeout,
    ModelMissingError,
    NotFoundError,
    OpenWebUIError,
    PermissionDenied,
    ProtocolError,
    ResourceUnavailable,
    TerminalDisabled,
    TerminalRequired,
    TerminalUnavailable,
    ToolAuthorizationRequired,
    ToolUnavailable,
    UnexecutedToolCall,
    UnsupportedAPIError,
)
from owui_protocol.state import ConversationManager
from owui_protocol.history import ChatHistoryCleaner
import owui_protocol.client as client_module
import owui_protocol.state as state_module


class Response:
    """Minimal aiohttp response, including deterministic cleanup."""

    def __init__(self, data, status=200):
        self.data, self.status, self.closed = data, status, False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return deepcopy(self.data)


class Server:
    """Persist trees and finish a remote task after configurable polling rounds."""

    def __init__(self):
        self.calls, self.responses, self.chats = [], [], {}
        self.models = [
            {"id": "workspace-agent", "name": "Home Agent", "info": {"meta": {}}}
        ]
        self.tools = [
            {"id": "server:mcp:exact-id", "name": "Home MCP"},
            {"id": "server:api", "name": "OpenAPI"},
            {"id": "helper", "name": "Workspace helper"},
        ]
        self.terminals = [
            {
                "id": "terminal-one",
                "name": "Terminal",
                "contexts": {"chat": {"context_id": "chat_id"}},
            }
        ]
        self.pending = {}
        self.rounds = 2
        self.answer = "The office light is on."
        self.output = None
        self.overrides = {}

    def request(self, method, url, **kwargs):
        path = url.split("https://example.test", 1)[-1]
        self.calls.append((method, path, deepcopy(kwargs)))
        override = self.overrides.get((method, path))
        if isinstance(override, Exception):
            raise override
        if override is not None:
            response = Response(*override)
        else:
            response = Response(self.dispatch(method, path, kwargs.get("json")))
        self.responses.append(response)
        return response

    def dispatch(self, method, path, body):
        if path == "/api/models":
            return {"data": self.models}
        if path == "/api/version":
            return {"version": "0.11.4"}
        if path == "/api/v1/tools/":
            return self.tools
        if path == "/api/v1/terminals/":
            return self.terminals
        if path == "/api/v1/chats/new":
            chat_id = f"chat-{len(self.chats) + 1}"
            self.chats[chat_id] = deepcopy(body["chat"])
            return {"id": chat_id}
        if path == "/api/chat/completions":
            self.pending[body["chat_id"]] = [self.rounds, body["id"]]
            return {"status": True, "task_ids": ["task"], "chat_id": body["chat_id"]}
        if path.startswith("/api/tasks/chat/"):
            chat_id = path.rsplit("/", 1)[-1]
            pending = self.pending[chat_id]
            if pending[0]:
                pending[0] -= 1
                return {"task_ids": ["task"]}
            message = self.chats[chat_id]["history"]["messages"][pending[1]]
            message.update(content=self.answer, done=True, sources=[{"id": "source"}])
            if self.output:
                message["output"] = self.output
            return {"task_ids": []}
        if path.startswith("/api/v1/chats/"):
            chat_id = path.rsplit("/", 1)[-1]
            if method == "DELETE":
                self.chats.pop(chat_id, None)
                return True
            if method == "POST":
                self.chats[chat_id] = deepcopy(body["chat"])
            return {"chat": self.chats[chat_id]}
        raise AssertionError((method, path))


@pytest.fixture
def server():
    """Create an isolated mocked server."""
    return Server()


@pytest.fixture
def client(server):
    """Pass the mocked session through the real HTTP client."""
    return OpenWebUIClient(
        "https://example.test/", "test-secret-not-a-real-key", server
    )


@pytest.fixture
def manager(client):
    """Create a fresh conversation manager."""
    return ConversationManager(client)


OPTIONS = {"chat_model": "workspace-agent", "poll_interval": 0.001}


async def test_native_request_and_final_answer(manager, server):
    """The HTTP acceptance metadata never becomes the spoken answer."""
    result = await manager.async_process("ha-one", "Turn on the office light", OPTIONS)
    assert result.text == server.answer
    assert result.message["sources"] == [{"id": "source"}]
    payload = next(
        call[2]["json"] for call in server.calls if call[1] == "/api/chat/completions"
    )
    assert "tools" not in payload
    assert payload["stream"] is True
    assert payload["chat_id"] and payload["id"] and payload["session_id"]
    assert payload["features"] == {
        "memory": True,
        "web_search": True,
        "code_interpreter": False,
        "image_generation": False,
    }
    assert payload["params"] == {"function_calling": "native"}
    assert not any(payload["background_tasks"].values())
    assert all(r.closed for r in server.responses)


async def test_multiturn_and_isolation(manager, server):
    """Continue the exact branch, but keep HA conversations independent."""
    first = await manager.async_process("one", "Hello", OPTIONS)
    second = await manager.async_process("one", "What did I say?", OPTIONS)
    other = await manager.async_process("two", "Hello", OPTIONS)
    assert first.state.chat_id == second.state.chat_id != other.state.chat_id
    assert first.state.session_id == second.state.session_id != other.state.session_id
    assert (
        first.state.last_assistant_message_id != second.state.last_assistant_message_id
    )
    tree = server.chats[first.state.chat_id]["history"]["messages"]
    user_id = tree[second.state.last_assistant_message_id]["parentId"]
    assert tree[user_id]["parentId"] == first.state.last_assistant_message_id
    assert tree[first.state.last_assistant_message_id]["childrenIds"] == [user_id]
    calls = [c[2]["json"] for c in server.calls if c[1] == "/api/chat/completions"]
    assert [m["role"] for m in calls[1]["messages"]] == ["user", "assistant", "user"]


async def test_request_prefix_remains_stable(manager, server):
    """Do not inject dates, IDs or changing system text into model messages."""
    settings = {**OPTIONS, "tool_mode": "custom", "tool_ids": ["helper", "server:api"]}
    for conversation_id in ("one", "one", "one", "two"):
        await manager.async_process(conversation_id, "Hello", settings)
    requests = [c[2]["json"] for c in server.calls if c[1] == "/api/chat/completions"]
    assert (
        requests[0]["messages"]
        == requests[3]["messages"]
        == [{"role": "user", "content": "Hello"}]
    )
    for earlier, later in zip(requests[:2], requests[1:3], strict=True):
        assert later["messages"][: len(earlier["messages"])] == earlier["messages"]
    for key in ("features", "params", "tool_ids", "model"):
        assert all(request[key] == requests[0][key] for request in requests)


async def test_disable_thinking_preserves_native_tools(manager, server):
    """Send the documented NInfer/OpenAI-compatible off setting through Open WebUI."""
    await manager.async_process(
        "one", "Hello", {**OPTIONS, "thinking_mode": "disabled"}
    )
    payload = next(
        c[2]["json"] for c in server.calls if c[1] == "/api/chat/completions"
    )
    assert payload["params"] == {
        "function_calling": "native",
        "reasoning_effort": "none",
    }
    assert payload["stream"] is True and "tools" not in payload


async def test_cleanup_tracks_failed_submission(manager, client, server):
    """A chat created before a failed submission still has a cleanup deadline."""
    save = AsyncMock()
    cleaner = ChatHistoryCleaner(client, save)
    manager.cleaner = cleaner
    server.overrides[("POST", "/api/chat/completions")] = ({}, 403)
    with pytest.raises(PermissionDenied):
        await manager.async_process("one", "Sensitive test prompt", OPTIONS)
    persisted = save.call_args.args[0]
    assert list(persisted["chats"]) == ["chat-1"]
    assert "Sensitive" not in str(persisted)
    assert cleaner.pending_count == 1
    assert not manager.states
    await manager.async_close()


async def test_turns_reuse_discovery_but_not_execution(manager, server):
    """Remove repeated catalogue calls while every turn still reaches the server."""
    settings = {**OPTIONS, "tool_mode": "custom", "tool_ids": ["helper"]}
    await manager.async_process("one", "Hello", settings)
    await manager.async_process("one", "Again", settings)
    paths = [call[1] for call in server.calls]
    assert paths.count("/api/models") == paths.count("/api/v1/tools/") == 1
    assert paths.count("/api/chat/completions") == 2
    assert paths.count("/api/v1/chats/chat-1") == 4


async def test_discovery_expiry_and_explicit_refresh(client, server, monkeypatch):
    """Refresh within a minute and immediately when configuration requests it."""
    clock = [0.0]
    monkeypatch.setattr(client_module, "monotonic", lambda: clock[0])
    first = await client.async_get_models(refresh=False)
    first[0]["name"] = "Local mutation"
    server.models[0]["name"] = "Changed on server"
    clock[0] = 59.0
    assert (await client.async_get_models(refresh=False))[0]["name"] == "Home Agent"
    clock[0] = 60.0
    assert (await client.async_get_models(refresh=False))[0][
        "name"
    ] == "Changed on server"
    server.models[0]["name"] = "Changed again"
    assert (await client.async_get_models())[0]["name"] == "Changed again"
    assert len(server.calls) == 3


async def test_concurrent_discovery_is_shared(client, monkeypatch):
    """Concurrent voice conversations do not each reload the same catalogue."""

    async def discovery(*args):
        await asyncio.sleep(0)
        return {"data": [{"id": "model"}]}

    request = AsyncMock(side_effect=discovery)
    monkeypatch.setattr(client, "_request", request)
    await asyncio.gather(*(client.async_get_models(refresh=False) for _ in range(3)))
    request.assert_awaited_once()


async def test_failed_refresh_never_uses_stale_discovery(client, server, monkeypatch):
    """A revoked credential or failed refresh must not fall back to old access."""
    clock = [0.0]
    monkeypatch.setattr(client_module, "monotonic", lambda: clock[0])
    await client.async_get_models(refresh=False)
    server.overrides[("GET", "/api/models")] = ({}, 401)
    clock[0] = 60.0
    with pytest.raises(AuthenticationError):
        await client.async_get_models(refresh=False)
    with pytest.raises(AuthenticationError):
        await client.async_get_models(refresh=False)


async def test_failed_turn_refreshes_discovery_without_replay(manager, server):
    """Failure clears cached metadata; only an explicit next turn submits again."""
    server.overrides[("POST", "/api/chat/completions")] = ({}, 403)
    with pytest.raises(PermissionDenied):
        await manager.async_process("one", "Hello", OPTIONS)
    assert sum(c[1] == "/api/chat/completions" for c in server.calls) == 1
    server.overrides.clear()
    await manager.async_process("one", "Try again", OPTIONS)
    assert sum(c[1] == "/api/models" for c in server.calls) == 2


async def test_turn_timings_separate_discovery_from_agent(manager, client, monkeypatch):
    """Measure all phases with a fake clock, without real-time performance assertions."""
    clock = [0.0]
    monkeypatch.setattr(client_module, "monotonic", lambda: clock[0])
    monkeypatch.setattr(state_module, "monotonic", lambda: clock[0])
    original = client._request

    async def request(*args, **kwargs):
        clock[0] += 1.0
        return await original(*args, **kwargs)

    async def sleep(interval):
        clock[0] += interval

    monkeypatch.setattr(client, "_request", request)
    monkeypatch.setattr(client_module.asyncio, "sleep", sleep)
    result = await manager.async_process(
        "one",
        "Hello",
        {**OPTIONS, "poll_interval": 2, "tool_mode": "custom", "tool_ids": ["helper"]},
    )
    assert (
        result.timings
        == manager.last_timings
        == {
            "queue_s": 0.0,
            "model_discovery_s": 1.0,
            "resource_discovery_s": 1.0,
            "history_load_s": 0.0,
            "chat_save_s": 1.0,
            "submission_s": 1.0,
            "completion_wait_s": 7.0,
            "answer_read_s": 1.0,
            "total_s": 12.0,
        }
    )


async def test_deleted_chat_recovery(manager, server):
    """Only a missing previously saved chat causes a single fresh-chat attempt."""
    first = await manager.async_process("one", "Hello", OPTIONS)
    server.overrides[("GET", f"/api/v1/chats/{first.state.chat_id}")] = ({}, 404)
    second = await manager.async_process("one", "Again", OPTIONS)
    assert first.state.chat_id != second.state.chat_id


async def test_model_change_and_reload(manager, server, client):
    """Never reuse a saved chat with a different selected model."""
    first = await manager.async_process("one", "Hello", OPTIONS)
    server.models.append({"id": "other"})
    changed = await manager.async_process(
        "one", "Hello", {**OPTIONS, "chat_model": "other"}
    )
    restarted = await ConversationManager(client).async_process("one", "Hello", OPTIONS)
    assert (
        len({first.state.chat_id, changed.state.chat_id, restarted.state.chat_id}) == 3
    )


@pytest.mark.parametrize("rounds", [0, 1, 4])
async def test_server_owns_all_tool_rounds(manager, server, rounds):
    """Represent search, MCP and multi-step work as server tasks only."""
    server.rounds = rounds
    server.output = [
        {
            "type": "reasoning",
            "content": [{"type": "reasoning_text", "text": "private"}],
        },
        {
            "type": "message",
            "content": [{"type": "output_text", "text": "I will check."}],
        },
        {"type": "function_call", "name": "arbitrary_tool", "arguments": "secret"},
        {"type": "function_call_output", "output": "secret"},
        {
            "type": "message",
            "content": [{"type": "output_text", "text": "Final answer"}],
        },
    ]
    result = await manager.async_process(
        "one",
        "Do the work",
        {**OPTIONS, "tool_mode": "custom", "tool_ids": ["server:mcp:exact-id"]},
    )
    assert result.text == "Final answer"
    await manager.async_process("one", "Continue", OPTIONS)
    payload = [c[2]["json"] for c in server.calls if c[1] == "/api/chat/completions"][
        -1
    ]
    assert payload["messages"][1]["output"] == server.output


async def test_discovery_and_defaults(client, server):
    """Use exact IDs and model metadata, including terminal chat access."""
    tools = await client.async_get_tools()
    assert [r.category for r in tools] == ["mcp", "openapi", "workspace"]
    model = server.models[0]
    model["info"]["meta"] = {
        "toolIds": ["server:mcp:exact-id", "helper", "helper"],
        "terminalId": "terminal-one",
    }
    assert await client.async_resolve_resources(model, {"terminal_mode": "model"}) == (
        ["server:mcp:exact-id", "helper"],
        "terminal-one",
    )
    assert await client.async_resolve_resources(
        model, {"tool_mode": "custom", "tool_ids": []}
    ) == ([], None)
    server.terminals[0]["contexts"]["chat"] = False
    assert await client.async_get_terminals() == []
    with pytest.raises(ResourceUnavailable):
        await client.async_resolve_resources(model, {"terminal_mode": "model"})


async def test_oauth_access_and_missing_model(client, server):
    """Do not silently omit explicitly selected resources."""
    server.tools[0]["authenticated"] = False
    with pytest.raises(ToolAuthorizationRequired) as raised:
        await client.async_resolve_resources(
            server.models[0],
            {"tool_mode": "custom", "tool_ids": [server.tools[0]["id"]]},
        )
    assert raised.value.translation_placeholders == {"resources": "server:mcp:exact-id"}
    with pytest.raises(ModelMissingError):
        await client.async_get_model("missing")


async def test_stale_model_defaults_use_accessible_intersection(client, server, caplog):
    """Match browser selection when an unrelated stale default remains attached."""
    model = server.models[0]
    model["info"]["meta"]["toolIds"] = ["removed", "helper", "server:mcp:exact-id"]
    assert await client.async_resolve_resources(model, {}) == (
        ["helper", "server:mcp:exact-id"],
        None,
    )
    assert "Skipping 1 unavailable" in caplog.text


@pytest.mark.parametrize("mode", ["model", "custom"])
async def test_no_accessible_selected_tools_is_actionable(client, server, mode):
    """Never silently run without tools when every selected ID is unavailable."""
    model = server.models[0]
    model["info"]["meta"]["toolIds"] = ["removed"]
    with pytest.raises(ToolUnavailable) as raised:
        await client.async_resolve_resources(
            model, {"tool_mode": mode, "tool_ids": ["removed"]}
        )
    assert raised.value.key == "tool_unavailable"
    assert raised.value.translation_placeholders == {"resources": "removed"}


async def test_custom_selection_does_not_silently_drop_missing_tools(client, server):
    """Explicit custom selection stays exact even if some tools remain accessible."""
    with pytest.raises(ToolUnavailable):
        await client.async_resolve_resources(
            server.models[0], {"tool_mode": "custom", "tool_ids": ["helper", "removed"]}
        )


async def test_browser_local_tool_has_own_error(client, server):
    """A browser-local connection should not be misdiagnosed as OAuth failure."""
    with pytest.raises(BrowserToolUnsupported) as raised:
        await client.async_resolve_resources(
            server.models[0], {"tool_mode": "custom", "tool_ids": ["direct_server:0"]}
        )
    assert raised.value.translation_placeholders == {"resources": "direct_server:0"}


async def test_terminal_none_skips_default_and_discovery(client, server):
    """An unused model terminal cannot block a tools-only HA conversation."""
    server.models[0]["info"]["meta"]["terminalId"] = "removed-terminal"
    assert await client.async_resolve_resources(
        server.models[0], {"terminal_mode": "none"}
    ) == ([], None)
    assert not any(call[1] == "/api/v1/terminals/" for call in server.calls)


async def test_terminal_errors_are_distinct(client, server):
    """Report missing selection, model capability and discovery separately."""
    with pytest.raises(TerminalRequired):
        await client.async_resolve_resources(
            server.models[0], {"terminal_mode": "custom"}
        )
    with pytest.raises(TerminalUnavailable) as raised:
        await client.async_resolve_resources(
            server.models[0], {"terminal_mode": "custom", "terminal_id": "missing"}
        )
    assert raised.value.translation_placeholders == {"resources": "missing"}
    server.models[0]["info"]["meta"]["capabilities"] = {"terminal": False}
    with pytest.raises(TerminalDisabled):
        await client.async_resolve_resources(
            server.models[0], {"terminal_mode": "custom", "terminal_id": "terminal-one"}
        )


@pytest.mark.parametrize(
    "status,error",
    [
        (401, AuthenticationError),
        (403, PermissionDenied),
        (404, NotFoundError),
        (405, UnsupportedAPIError),
        (500, OpenWebUIError),
        (302, OpenWebUIError),
    ],
)
async def test_http_errors_and_no_secret_logging(client, server, caplog, status, error):
    """Never propagate external response bodies, auth headers or exception details."""
    server.overrides[("GET", "/api/models")] = (
        {"detail": "test-secret-not-a-real-key"},
        status,
    )
    with pytest.raises(error) as raised:
        await client.async_get_models()
    assert "test-secret" not in str(raised.value) + caplog.text
    assert server.responses[-1].closed


@pytest.mark.parametrize(
    "value", [None, {}, {"data": None}, {"data": [{}]}, ValueError("secret")]
)
async def test_malformed_json(client, server, value):
    """Malformed server responses are actionable protocol errors."""
    server.overrides[("GET", "/api/models")] = (value, 200)
    with pytest.raises(ProtocolError):
        await client.async_get_models()


@pytest.mark.parametrize(
    "error,expected",
    [
        (aiohttp.ClientConnectionError("secret"), OpenWebUIError),
        (TimeoutError("secret"), CompletionTimeout),
    ],
)
async def test_network_failures(client, server, error, expected):
    """Network errors are sanitized and typed."""
    server.overrides[("GET", "/api/models")] = error
    with pytest.raises(expected) as raised:
        await client.async_get_models()
    assert "secret" not in str(raised.value)


async def test_timeout_drops_uncertain_mapping(manager, server):
    """Time out without replaying an action-taking completion."""
    server.rounds = 100000
    with pytest.raises(CompletionTimeout):
        await manager.async_process(
            "one", "Work", {**OPTIONS, "completion_timeout": 0.02}
        )
    assert not manager.states
    assert sum(c[1] == "/api/chat/completions" for c in server.calls) == 1


async def test_unload_cancels_waits(manager, server):
    """Unload is cancellation-safe and leaves no local polling tasks."""
    server.rounds = 100000
    task = asyncio.create_task(manager.async_process("one", "Work", OPTIONS))
    await asyncio.sleep(0.01)
    await manager.async_close()
    assert task.cancelled()
    assert not manager.states and not manager._tasks and not manager._locks


async def test_same_conversation_serialized(manager, server):
    """Concurrent callers cannot overwrite the same message tree."""
    await asyncio.gather(
        *(manager.async_process("one", str(i), OPTIONS) for i in range(3))
    )
    assert len(server.chats) == 1
    assert len(next(iter(server.chats.values()))["history"]["messages"]) == 6


async def test_bounded_mapping_count(client):
    """Evict old references without deleting saved chats."""
    manager = ConversationManager(client, capacity=2)
    for i in range(3):
        await manager.async_process(str(i), "Hi", OPTIONS)
    assert list(manager.states) == ["1", "2"]
    assert not manager._locks


@pytest.mark.parametrize(
    "text",
    [
        "<tool_call>private</tool_call>",
        "<function=some_tool>",
        '{"tool_calls": []}',
        '{"function_call": {}}',
    ],
)
def test_raw_tool_output_is_rejected(text):
    """Diagnostic protection never attempts to execute the model output."""
    with pytest.raises(UnexecutedToolCall):
        final_text({"content": text})


def test_reasoning_and_nontext():
    """Strip protocol decorations without exposing reasoning or raw media JSON."""
    assert (
        final_text(
            {"content": "<think>secret</think><details>tool</details>Hello<|im_end|>"}
        )
        == "Hello"
    )
    with pytest.raises(AgentFailedError):
        final_text({"content": [{"type": "image"}]})


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp",
        "https://u:secret@host",
        "https://host?q=secret",
        "https://host#fragment",
        "http://host:bad",
    ],
)
def test_invalid_urls(url):
    """Forbid credentials and ambiguous request URLs."""
    with pytest.raises(ValueError):
        normalize_url(url)


def test_url_normalization():
    """Preserve reverse-proxy prefixes while trimming duplicate trailing slashes."""
    assert normalize_url(" https://host/prefix/// ") == "https://host/prefix"


async def test_invalid_tasks_and_acceptance(manager, server):
    """Never mistake missing task metadata or a normal completion for final text."""
    server.overrides[("POST", "/api/chat/completions")] = ({"choices": []}, 200)
    with pytest.raises(UnsupportedAPIError):
        await manager.async_process("one", "Hi", OPTIONS)
    server.overrides.clear()
    server.overrides[("GET", "/api/tasks/chat/chat-2")] = ({}, 200)
    with pytest.raises(ProtocolError):
        await manager.async_process("two", "Hi", OPTIONS)


async def test_failed_task_and_broken_tree(manager, server, client):
    """Reject stopped/error messages and cycles in parent links."""
    first = await manager.async_process("one", "Hi", OPTIONS)
    tree = server.chats[first.state.chat_id]["history"]["messages"]
    tree[first.state.last_assistant_message_id]["parentId"] = (
        first.state.last_assistant_message_id
    )
    with pytest.raises(ProtocolError):
        await manager.async_process("one", "Again", OPTIONS)
    client.async_get_chat_tasks = AsyncMock(return_value=[])
    tree[first.state.last_assistant_message_id]["done"] = False
    with pytest.raises(AgentFailedError):
        await client.async_wait_for_completion(first.state, 0.001)


async def test_version_and_model_stream_override(client, server):
    """Allow newer versions but reject known incompatible model settings."""
    assert await client.async_get_version() == "0.11.4"
    server.overrides[("GET", "/api/version")] = ({"version": "99.0.0"}, 200)
    assert await client.async_get_version() == "99.0.0"
    server.models[0]["info"]["params"] = {"stream_response": False}
    with pytest.raises(UnsupportedAPIError):
        await client.async_resolve_resources(server.models[0], {})


@pytest.mark.parametrize("metadata", ["invalid", [], 42])
async def test_malformed_model_metadata(client, metadata):
    """Unexpected metadata raises a typed error that Assist can translate."""
    with pytest.raises(ProtocolError):
        await client.async_resolve_resources({"id": "model", "info": metadata}, {})


def test_no_final_message_after_tool():
    """Do not speak an intermediate promise when the tool has no final answer."""
    with pytest.raises(AgentFailedError):
        final_text(
            {
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "I will do it."}],
                    },
                    {"type": "function_call", "name": "tool"},
                ]
            }
        )
    with pytest.raises(UnexecutedToolCall):
        final_text({"content": '[{"name": "tool", "arguments": {}}]'})
    with pytest.raises(AgentFailedError):
        final_text({"content": "<|channel|>analysis private reasoning"})


def test_cancelled_remote_output_is_not_final():
    """A remote cancellation can persist done=True with incomplete output."""
    with pytest.raises(AgentFailedError):
        final_text(
            {
                "done": True,
                "output": [
                    {
                        "type": "message",
                        "status": "incomplete",
                        "content": [{"type": "output_text", "text": "Partial"}],
                    }
                ],
            }
        )
