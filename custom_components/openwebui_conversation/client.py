"""Open WebUI's saved-chat, server-owned native agent protocol.

No tool definitions or executors live here. A completion is accepted asynchronously;
only the persisted assistant message is a response to the caller.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field
import json
import re
from time import monotonic, time
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import uuid4

import aiohttp

from .const import LOGGER
from .exceptions import (
    AgentFailedError,
    AuthenticationError,
    BrowserToolUnsupported,
    CompletionTimeout,
    ModelMissingError,
    NotFoundError,
    OpenWebUIError,
    PermissionDenied,
    ProtocolError,
    TerminalDisabled,
    TerminalRequired,
    TerminalUnavailable,
    ToolAuthorizationRequired,
    ToolUnavailable,
    UnexecutedToolCall,
    UnsupportedAPIError,
)

DISCOVERY_CACHE_SECONDS = 60


@dataclass(frozen=True)
class Resource:
    """A resource ID as returned by Open WebUI, never synthesized from its name."""

    id: str
    name: str
    category: str
    description: str = ""
    authenticated: bool = True


@dataclass
class ConversationState:
    """Only remote references; chat and memory contents stay in Open WebUI."""

    chat_id: str
    session_id: str
    last_assistant_message_id: str
    model_id: str


@dataclass
class AgentResult:
    """Final prose and the original persisted message for future UI support."""

    text: str
    message: dict[str, Any]
    state: ConversationState
    timings: dict[str, float] = field(default_factory=dict)


def normalize_url(value: str) -> str:
    """Normalize HTTP URLs and reject embedded credentials, query and fragment."""
    parts = urlsplit(value.strip())
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ValueError("Invalid base URL")
    try:
        _ = parts.port
    except ValueError:
        raise ValueError("Invalid base URL") from None
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def _mapping(value: Any) -> dict:
    """Validate optional metadata without letting malformed JSON crash Assist."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProtocolError("Invalid metadata object")
    return value


class OpenWebUIClient:
    """Async client using Home Assistant's shared, externally owned session."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        session: aiohttp.ClientSession,
        timeout: float = 30,
        verify_ssl: bool = True,
    ) -> None:
        """Initialize connection settings without logging credentials."""
        self.base_url = normalize_url(base_url)
        self._api_key = api_key
        self._session = session
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.server_version: str | None = None
        self._discovery_cache: dict[str, tuple[float, list[dict]]] = {}
        self._discovery_locks: dict[str, asyncio.Lock] = {}

    def clear_discovery_cache(self) -> None:
        """Recheck metadata after a failed turn without replaying the completion."""
        self._discovery_cache.clear()

    async def _discovery_items(
        self, path: str, *, refresh: bool, wrapped: bool = False
    ) -> list[dict]:
        """Reuse short-lived discovery results; never cache chats or execution."""
        async with self._discovery_locks.setdefault(path, asyncio.Lock()):
            cached = self._discovery_cache.get(path)
            if (
                not refresh
                and cached
                and monotonic() - cached[0] < DISCOVERY_CACHE_SECONDS
            ):
                return deepcopy(cached[1])
            # Never reuse an expired result if its refresh fails.
            self._discovery_cache.pop(path, None)
            data = await self._request("GET", path)
            if wrapped:
                data = data.get("data") if isinstance(data, dict) else None
            items = self._items(data)
            self._discovery_cache[path] = (monotonic(), deepcopy(items))
            return items

    async def _request(self, method: str, path: str, body: dict | None = None) -> Any:
        """Read JSON, release connections, and sanitize every external error."""
        try:
            async with self._session.request(
                method,
                f"{self.base_url}{path}",
                json=body,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                ssl=self.verify_ssl,
                allow_redirects=False,
            ) as response:
                if response.status >= 300:
                    LOGGER.debug("Open WebUI HTTP failure: status=%s", response.status)
                    error = {
                        401: AuthenticationError,
                        403: PermissionDenied,
                        404: NotFoundError,
                        405: UnsupportedAPIError,
                    }.get(response.status, OpenWebUIError)
                    raise error(f"HTTP {response.status}")
                try:
                    return await response.json()
                except (ValueError, aiohttp.ContentTypeError):
                    raise ProtocolError("Expected a JSON response") from None
        except TimeoutError:
            raise CompletionTimeout("API request timed out") from None
        except aiohttp.ClientError:
            raise OpenWebUIError("Open WebUI connection failed") from None

    async def async_get_version(self) -> str | None:
        """Best-effort version detection; never reject unknown or future versions."""
        try:
            data = await self._request("GET", "/api/version")
        except (NotFoundError, PermissionDenied):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("version"), str):
            raise ProtocolError("Invalid version response")
        self.server_version = data["version"]
        # No unverified numeric minimum: endpoint/payload checks are authoritative.
        LOGGER.debug("Open WebUI server version=%s", self.server_version)
        return self.server_version

    async def async_get_models(self, *, refresh: bool = True) -> list[dict[str, Any]]:
        """Discover base and Workspace models visible to the API-key user."""
        return await self._discovery_items("/api/models", refresh=refresh, wrapped=True)

    async def async_get_model(self, model_id: str) -> dict[str, Any]:
        """Use the same model metadata as the browser's models store."""
        for model in await self.async_get_models(refresh=False):
            if model["id"] == model_id:
                return model
        # A newly selected model may have appeared since the cached catalogue.
        for model in await self.async_get_models():
            if model["id"] == model_id:
                return model
        raise ModelMissingError("Configured model is not available")

    @staticmethod
    def _items(data: Any) -> list[dict[str, Any]]:
        """Validate discovery response shape before exposing it to UI code."""
        if not isinstance(data, list) or any(
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not item["id"]
            for item in data
        ):
            raise ProtocolError("Invalid discovery response")
        return data

    async def async_get_tools(self, *, refresh: bool = True) -> list[Resource]:
        """Discover Workspace, MCP and OpenAPI servers through the unified endpoint."""
        data = await self._discovery_items("/api/v1/tools/", refresh=refresh)
        return [
            Resource(
                id=item["id"],
                name=item.get("name") or item["id"],
                category=(
                    "mcp"
                    if item["id"].startswith("server:mcp:")
                    else "openapi"
                    if item["id"].startswith("server:")
                    else "workspace"
                ),
                description=_mapping(item.get("meta")).get("description", ""),
                authenticated=item.get("authenticated") is not False,
            )
            for item in data
            if not item["id"].startswith("direct_server:")
        ]

    async def async_get_terminals(self, *, refresh: bool = True) -> list[Resource]:
        """Discover terminals allowed in chat, including saved-chat scoped ones."""
        data = await self._discovery_items("/api/v1/terminals/", refresh=refresh)
        return [
            Resource(item["id"], item.get("name") or item["id"], "terminal")
            for item in data
            if _mapping(item.get("contexts")).get("chat") is not False
        ]

    async def async_resolve_resources(
        self, model: dict, options: dict
    ) -> tuple[list[str], str | None]:
        """Reproduce browser model defaults, with explicit failure for lost access."""
        info = _mapping(model.get("info"))
        meta = _mapping(info.get("meta"))
        params = _mapping(info.get("params"))
        if (
            params.get("function_calling") == "legacy"
            or params.get("stream_response") is False
        ):
            raise UnsupportedAPIError("Model must allow native streaming")
        ids = (
            meta.get("toolIds", [])
            if options.get("tool_mode", "model") == "model"
            else options.get("tool_ids", [])
        )
        if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
            raise ProtocolError("Invalid model tool defaults")
        ids = list(dict.fromkeys(ids))
        if ids:
            browser_ids = [i for i in ids if i.startswith("direct_server:")]
            if browser_ids:
                raise BrowserToolUnsupported(browser_ids)
            available = {
                tool.id: tool for tool in await self.async_get_tools(refresh=False)
            }
            missing = [i for i in ids if i not in available]
            if missing:
                LOGGER.debug(
                    "Tool lookup failed; mode=%s selected=%d discovered=%d missing=%r",
                    options.get("tool_mode", "model"),
                    len(ids),
                    len(available),
                    missing,
                )
                if options.get("tool_mode", "model") != "model" or len(missing) == len(
                    ids
                ):
                    raise ToolUnavailable(missing)
                # The browser intersects model defaults with its accessible tools.
                # A stale, unrelated default must not block remaining valid tools.
                LOGGER.warning(
                    "Skipping %d unavailable model-default tool(s); using %d accessible tool(s). Review the model's tool assignments in Open WebUI",
                    len(missing),
                    len(ids) - len(missing),
                )
                ids = [i for i in ids if i in available]
            unauthorized = [i for i in ids if not available[i].authenticated]
            if unauthorized:
                raise ToolAuthorizationRequired(unauthorized)
        mode = options.get("terminal_mode", "none")
        terminal = (
            meta.get("terminalId")
            if mode == "model"
            else options.get("terminal_id")
            if mode == "custom"
            else None
        )
        if mode == "custom" and not terminal:
            raise TerminalRequired()
        if terminal:
            if _mapping(meta.get("capabilities")).get("terminal") is False:
                raise TerminalDisabled()
            if terminal not in {
                item.id for item in await self.async_get_terminals(refresh=False)
            }:
                raise TerminalUnavailable([terminal])
        return ids, terminal

    async def async_create_chat(self, chat: dict) -> str:
        """Persist the initial tree, including the empty assistant placeholder."""
        try:
            data = await self._request("POST", "/api/v1/chats/new", {"chat": chat})
        except NotFoundError:
            raise UnsupportedAPIError("Saved chat API is unavailable") from None
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("id"), str)
            or not data["id"]
        ):
            raise ProtocolError("Missing created chat ID")
        return data["id"]

    async def async_get_chat(self, chat_id: str) -> dict:
        """Read the saved chat; 404 is handled only by the state layer."""
        data = await self._request("GET", f"/api/v1/chats/{quote(chat_id, safe='')}")
        if data is None:
            raise NotFoundError("Chat was deleted")
        if not isinstance(data, dict) or not isinstance(data.get("chat"), dict):
            raise ProtocolError("Missing chat object")
        self._tree(data["chat"])
        return data["chat"]

    async def async_update_chat(self, chat_id: str, chat: dict) -> None:
        """Persist a follow-up branch before starting the native completion."""
        await self._request(
            "POST", f"/api/v1/chats/{quote(chat_id, safe='')}", {"chat": chat}
        )

    async def async_delete_chat(self, chat_id: str) -> None:
        """Delete one known integration-owned chat, never the user's chat list."""
        deleted = await self._request(
            "DELETE", f"/api/v1/chats/{quote(chat_id, safe='')}"
        )
        if deleted is not True:
            raise ProtocolError("Chat deletion was not confirmed")

    @staticmethod
    def _tree(chat: dict) -> dict:
        """Reject malformed trees rather than silently overwriting a conversation."""
        history = chat.get("history")
        if not isinstance(history, dict) or not isinstance(
            history.get("messages"), dict
        ):
            raise ProtocolError("Invalid chat history")
        return history["messages"]

    @classmethod
    def _append_turn(
        cls, chat: dict, model: dict, prompt: str, parent: str | None
    ) -> tuple[str, list[dict]]:
        """Append two linked nodes and build the active branch's model context."""
        tree = cls._tree(chat)
        messages = []
        cursor, seen = parent, set()
        while cursor:
            if (
                cursor in seen
                or cursor not in tree
                or not isinstance(tree[cursor], dict)
            ):
                raise ProtocolError("Broken parent chain")
            seen.add(cursor)
            node = tree[cursor]
            if node.get("role") not in ("user", "assistant", "system"):
                raise ProtocolError("Invalid history role")
            if node["role"] == "assistant" and node.get("output"):
                messages.append(
                    {
                        "role": "assistant",
                        "model": node.get("model", model["id"]),
                        "output": node["output"],
                    }
                )
            else:
                messages.append(
                    {"role": node["role"], "content": node.get("content", "")}
                )
            cursor = node.get("parentId")
        messages.reverse()
        user_id, assistant_id = str(uuid4()), str(uuid4())
        now = int(time())
        if parent:
            children = tree[parent].get("childrenIds")
            if not isinstance(children, list):
                raise ProtocolError("Invalid child links")
            children.append(user_id)
        tree[user_id] = {
            "id": user_id,
            "parentId": parent,
            "childrenIds": [assistant_id],
            "role": "user",
            "content": prompt,
            "models": [model["id"]],
            "timestamp": now,
        }
        tree[assistant_id] = {
            "id": assistant_id,
            "parentId": user_id,
            "childrenIds": [],
            "role": "assistant",
            "content": "",
            "model": model["id"],
            "modelName": model.get("name", model["id"]),
            "modelIdx": 0,
            "done": False,
            "timestamp": now + 1,
        }
        chat["history"]["currentId"] = assistant_id
        messages.append({"role": "user", "content": prompt})
        return assistant_id, messages

    async def async_get_chat_tasks(self, chat_id: str) -> list[str]:
        """Validate task metadata; missing task_ids never means finished."""
        try:
            data = await self._request(
                "GET", f"/api/tasks/chat/{quote(chat_id, safe='')}"
            )
        except NotFoundError:
            raise UnsupportedAPIError("Chat task polling is unavailable") from None
        if not isinstance(data, dict) or not isinstance(data.get("task_ids"), list):
            raise ProtocolError("Missing task IDs")
        return data["task_ids"]

    async def async_wait_for_completion(
        self,
        state: ConversationState,
        poll_interval: float,
        timings: dict[str, float] | None = None,
    ) -> dict:
        """Wait asynchronously and read the exact placeholder we created."""
        started = monotonic()
        polls = 0
        while await self.async_get_chat_tasks(state.chat_id):
            polls += 1
            if polls == 1 or polls % 10 == 0:
                LOGGER.debug("Open WebUI agent still running; polls=%d", polls)
            await asyncio.sleep(poll_interval)
        finished = monotonic()
        chat = await self.async_get_chat(state.chat_id)
        if timings is not None:
            timings["completion_wait_s"] = finished - started
            timings["answer_read_s"] = monotonic() - finished
        message = self._tree(chat).get(state.last_assistant_message_id)
        if (
            not isinstance(message, dict)
            or message.get("role") != "assistant"
            or message.get("error")
        ):
            raise AgentFailedError("Final assistant message missing or failed")
        if message.get("done") is not True:
            raise AgentFailedError("Server task stopped without a completed answer")
        return message

    async def async_send_message(
        self,
        prompt: str,
        model: dict,
        features: dict[str, bool],
        tool_ids: list[str],
        terminal_id: str | None,
        completion_timeout: float,
        poll_interval: float,
        state: ConversationState | None = None,
        chat: dict | None = None,
        *,
        thinking_mode: str = "model",
        on_chat_created: Callable[[str], Awaitable[None]] | None = None,
    ) -> AgentResult:
        """Run one native turn without retrying potentially action-taking requests."""
        started = monotonic()
        timings: dict[str, float] = {}
        if completion_timeout <= 0 or poll_interval <= 0:
            raise ProtocolError("Timeouts and polling interval must be positive")
        params = {"function_calling": "native"}
        if thinking_mode == "disabled":
            params["reasoning_effort"] = "none"
        elif thinking_mode != "model":
            raise ProtocolError("Unknown thinking mode")
        try:
            async with asyncio.timeout(completion_timeout):
                if state:
                    chat = deepcopy(
                        chat
                        if chat is not None
                        else await self.async_get_chat(state.chat_id)
                    )
                    parent = state.last_assistant_message_id
                else:
                    chat = {
                        "title": "Home Assistant",
                        "models": [model["id"]],
                        "history": {"currentId": None, "messages": {}},
                    }
                    parent = None
                assistant_id, messages = self._append_turn(chat, model, prompt, parent)
                if state:
                    await self.async_update_chat(state.chat_id, chat)
                    next_state = ConversationState(
                        state.chat_id, state.session_id, assistant_id, model["id"]
                    )
                else:
                    next_state = ConversationState(
                        await self.async_create_chat(chat),
                        str(uuid4()),
                        assistant_id,
                        model["id"],
                    )
                    if on_chat_created is not None:
                        await on_chat_created(next_state.chat_id)
                saved = monotonic()
                timings["chat_save_s"] = saved - started
                tree = self._tree(chat)
                user_message = tree[tree[assistant_id]["parentId"]]
                payload = {
                    "model": model["id"],
                    "messages": messages,
                    "stream": True,
                    "chat_id": next_state.chat_id,
                    "id": assistant_id,
                    # The current backend recreates the assistant placeholder from
                    # user_message.id. Omitting this severs its saved parent link.
                    "user_message": user_message,
                    "parent_id": parent,
                    "session_id": next_state.session_id,
                    "features": features,
                    "params": params,
                    "tool_ids": tool_ids,
                    "background_tasks": {
                        "title_generation": False,
                        "tags_generation": False,
                        "follow_up_generation": False,
                    },
                }
                if terminal_id:
                    payload["terminal_id"] = terminal_id
                LOGGER.debug(
                    "Starting native agent; existing=%s chat=%s model=%s features=%s tools=%d terminal=%s",
                    state is not None,
                    next_state.chat_id[:8],
                    model["id"],
                    features,
                    len(tool_ids),
                    terminal_id is not None,
                )
                accepted = await self._request("POST", "/api/chat/completions", payload)
                timings["submission_s"] = monotonic() - saved
                if (
                    not isinstance(accepted, dict)
                    or accepted.get("status") is not True
                    or not isinstance(accepted.get("task_ids"), list)
                    or accepted.get("chat_id", next_state.chat_id) != next_state.chat_id
                ):
                    raise UnsupportedAPIError(
                        "Expected asynchronous native task acceptance"
                    )
                message = await self.async_wait_for_completion(
                    next_state, poll_interval, timings
                )
                text = final_text(message)
                LOGGER.debug("Native agent finished in %.1fs", monotonic() - started)
                return AgentResult(text, message, next_state, timings)
        except TimeoutError:
            raise CompletionTimeout("Agent completion timed out") from None


def final_text(message: dict) -> str:
    """Extract final prose, excluding structured reasoning/tool events and artifacts."""
    output = message.get("output")
    if isinstance(output, list) and output:
        if any(
            isinstance(item, dict)
            and item.get("status") in ("in_progress", "incomplete", "failed")
            for item in output
        ):
            raise AgentFailedError("Server output is incomplete or failed")
        last_message = max(
            (
                i
                for i, item in enumerate(output)
                if isinstance(item, dict) and item.get("type") == "message"
            ),
            default=-1,
        )
        if any(
            isinstance(item, dict)
            and item.get("type")
            in (
                "function_call",
                "function_call_output",
                "web_search_call",
                "open_webui:code_interpreter",
            )
            for item in output[last_message + 1 :]
        ):
            raise AgentFailedError("No final assistant message after tool execution")
        # Only the last assistant message after tool rounds belongs in Assist/TTS.
        prose = [
            item
            for item in output
            if isinstance(item, dict)
            and item.get("type") == "message"
            and item.get("role", "assistant") == "assistant"
        ]
        parts = prose[-1].get("content", []) if prose else []
        text = (
            "\n".join(
                part["text"]
                for part in parts
                if isinstance(part, dict)
                and part.get("type") in ("text", "output_text")
                and isinstance(part.get("text"), str)
            )
            if isinstance(parts, list)
            else ""
        )
    else:
        text = message.get("content", "")
        if not isinstance(text, str):
            text = ""
    # Diagnostic detection only: never parse function arguments or execute anything.
    if (
        message.get("tool_calls")
        or message.get("function_call")
        or re.search(r"<\s*(?:tool_call|function[= >])|\[TOOL_CALLS\]", text, re.I)
    ):
        LOGGER.warning(
            "Open WebUI returned an unexecuted tool call; check native mode and server tool configuration"
        )
        raise UnexecutedToolCall("Unexecuted tool protocol")
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        value = None
    candidates = value if isinstance(value, list) else [value]
    if any(
        isinstance(item, dict)
        and (
            "tool_calls" in item
            or "function_call" in item
            or item.get("type") == "function_call"
            or ("name" in item and "arguments" in item)
        )
        for item in candidates
    ):
        raise UnexecutedToolCall("Structured tool protocol")
    if re.search(r"<\|(?:channel|start_header_id|analysis)\|>", text):
        raise AgentFailedError("Unprocessed model channel protocol")
    text = re.sub(
        r"<(think|analysis|details)\b[^>]*>.*?</\1\s*>", "", text, flags=re.I | re.S
    )
    text = re.sub(r"<(think|analysis|details)\b[^>]*>.*$", "", text, flags=re.I | re.S)
    text = re.sub(r"<\|[^|]+\|>", "", text).strip()
    if not text:
        raise AgentFailedError("No final text; inspect the saved Open WebUI chat")
    return text
