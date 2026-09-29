"""Bounded, per-entry conversation mappings and serialized turns."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
from time import monotonic
from typing import Any

from .client import AgentResult, ConversationState, OpenWebUIClient
from .const import CONF_MODEL, DEFAULT_OPTIONS, FEATURES, LOGGER
from .exceptions import NotFoundError, OpenWebUIError
from .history import ChatHistoryCleaner


class ConversationManager:
    """Own conversation references, not model context or a second agent loop.

    Mappings are intentionally ephemeral. Restart/reload or model changes start
    fresh saved chats. Failed/uncertain runs are never automatically replayed.
    """

    def __init__(
        self,
        client: OpenWebUIClient,
        capacity: int = 128,
        cleaner: ChatHistoryCleaner | None = None,
    ) -> None:
        """Create one state container per Home Assistant config entry."""
        self.client = client
        self.states: OrderedDict[str, ConversationState] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}
        self._users: dict[str, int] = {}
        self._tasks: set[asyncio.Task] = set()
        self._closed = False
        self._capacity = capacity
        self.last_timings: dict[str, float] | None = None
        self.cleaner = cleaner

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
        """Resolve model defaults and submit a single turn with measured phases."""
        started = monotonic()
        settings = {**DEFAULT_OPTIONS, **options}
        async with self._conversation(conversation_id):
            queued = monotonic()
            acquired: list[str] = []

            async def track_chat(chat_id: str) -> None:
                if self.cleaner and chat_id not in acquired:
                    # Add before the await so cancellation still releases protection.
                    acquired.append(chat_id)
                    await self.cleaner.async_acquire(chat_id)

            try:
                model = await self.client.async_get_model(settings.get(CONF_MODEL, ""))
                discovered = monotonic()
                tools, terminal = await self.client.async_resolve_resources(
                    model, settings
                )
                resolved = monotonic()
                state = self.states.get(conversation_id)
                if state and state.model_id != model["id"]:
                    self.states.pop(conversation_id)
                    state = None
                chat = None
                if state:
                    await track_chat(state.chat_id)
                    try:
                        chat = await self.client.async_get_chat(state.chat_id)
                    except NotFoundError:
                        LOGGER.info(
                            "Saved Open WebUI chat was deleted; starting a new chat"
                        )
                        self.states.pop(conversation_id)
                        state = None
                loaded = monotonic()
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
                    thinking_mode=settings["thinking_mode"],
                    on_chat_created=track_chat,
                )
            except (OpenWebUIError, asyncio.CancelledError):
                # A remote run may still be active. Do not attach another request to it.
                self.states.pop(conversation_id, None)
                self.client.clear_discovery_cache()
                raise
            finally:
                if self.cleaner:
                    for chat_id in acquired:
                        await self.cleaner.async_release(chat_id)
            result.timings.update(
                queue_s=queued - started,
                model_discovery_s=discovered - queued,
                resource_discovery_s=resolved - discovered,
                history_load_s=loaded - resolved,
                total_s=monotonic() - started,
            )
            self.last_timings = {
                key: round(value, 3) for key, value in result.timings.items()
            }
            LOGGER.debug("Open WebUI turn timing (seconds): %s", self.last_timings)
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
        if self.cleaner:
            await self.cleaner.async_close()
        self.states.clear()
