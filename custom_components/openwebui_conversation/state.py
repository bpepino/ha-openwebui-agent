"""Bounded, per-entry conversation mappings and serialized turns."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Any

from .client import AgentResult, ConversationState, OpenWebUIClient
from .const import CONF_MODEL, DEFAULT_OPTIONS, FEATURES, LOGGER
from .exceptions import NotFoundError, OpenWebUIError


class ConversationManager:
    """Own conversation references, not model context or a second agent loop.

    Mappings are intentionally ephemeral. Restart/reload or model changes start
    fresh saved chats. Failed/uncertain runs are never automatically replayed.
    """

    def __init__(self, client: OpenWebUIClient, capacity: int = 128) -> None:
        """Create one state container per Home Assistant config entry."""
        self.client = client
        self.states: OrderedDict[str, ConversationState] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}
        self._users: dict[str, int] = {}
        self._tasks: set[asyncio.Task] = set()
        self._closed = False
        self._capacity = capacity

    @asynccontextmanager
    async def _conversation(self, conversation_id: str):
        """Serialize a conversation, retaining locks while callers wait."""
        if self._closed:
            raise OpenWebUIError("Integration is unloading")
        task = asyncio.current_task()
        self._tasks.add(task)
        lock = self._locks.setdefault(conversation_id, asyncio.Lock())
        self._users[conversation_id] = self._users.get(conversation_id, 0) + 1
        try:
            async with lock:
                yield
        finally:
            self._tasks.discard(task)
            self._users[conversation_id] -= 1
            if not self._users[conversation_id]:
                self._users.pop(conversation_id)
                self._locks.pop(conversation_id)
            for key in list(self.states):
                if len(self.states) <= self._capacity:
                    break
                if key not in self._users:
                    self.states.pop(key)

    async def async_process(
        self, conversation_id: str, prompt: str, options: dict[str, Any]
    ) -> AgentResult:
        """Resolve fresh model defaults and submit a single turn."""
        settings = {**DEFAULT_OPTIONS, **options}
        async with self._conversation(conversation_id):
            try:
                model = await self.client.async_get_model(settings.get(CONF_MODEL, ""))
                tools, terminal = await self.client.async_resolve_resources(
                    model, settings
                )
                state = self.states.get(conversation_id)
                if state and state.model_id != model["id"]:
                    self.states.pop(conversation_id)
                    state = None
                chat = None
                if state:
                    try:
                        chat = await self.client.async_get_chat(state.chat_id)
                    except NotFoundError:
                        LOGGER.info(
                            "Saved Open WebUI chat was deleted; starting a new chat"
                        )
                        self.states.pop(conversation_id)
                        state = None
                result = await self.client.async_send_message(
                    prompt,
                    model,
                    {key: settings[key] for key in FEATURES},
                    tools,
                    terminal,
                    settings["completion_timeout"],
                    settings["poll_interval"],
                    state,
                    chat,
                )
            except (OpenWebUIError, asyncio.CancelledError):
                # A remote run may still be active. Do not attach another request to it.
                self.states.pop(conversation_id, None)
                raise
            self.states[conversation_id] = result.state
            self.states.move_to_end(conversation_id)
            return result

    async def async_close(self) -> None:
        """Cancel local waits on unload; the server may continue already accepted work."""
        self._closed = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.states.clear()
