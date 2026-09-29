"""Delete only integration-owned chats after their active conversation is idle."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import time

from .client import OpenWebUIClient
from .const import LOGGER
from .exceptions import NotFoundError, OpenWebUIError

IDLE_SECONDS = 15 * 60
RETRY_SECONDS = 60


@dataclass
class PendingChat:
    """Store an expiry and guard against deleting an in-use chat."""

    expires: float
    active: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ChatHistoryCleaner:
    """Persist only owned IDs and expiry times; never a second conversation history."""

    def __init__(
        self,
        client: OpenWebUIClient,
        save: Callable[[dict], Awaitable[None]],
        saved: dict | None = None,
    ) -> None:
        """Restore deadlines for chats previously created under auto-cleanup."""
        self.client = client
        self._save = save
        self._save_lock = asyncio.Lock()
        self._pending: dict[str, PendingChat] = {}
        self._sweeps: set[asyncio.Task] = set()
        self._closed = False
        for chat_id, expires in (saved or {}).get("chats", {}).items():
            if isinstance(chat_id, str) and isinstance(expires, (int, float)):
                self._pending[chat_id] = PendingChat(expires)

    @property
    def pending_count(self) -> int:
        """Expose a count, never identifiers, to diagnostics."""
        return len(self._pending)

    async def _persist(self) -> None:
        async with self._save_lock:
            await self._save(
                {
                    "chats": {
                        chat_id: item.expires for chat_id, item in self._pending.items()
                    }
                }
            )

    async def async_acquire(self, chat_id: str) -> None:
        """Record a chat before submission and protect it until the turn ends."""
        item = self._pending.setdefault(chat_id, PendingChat(time() + IDLE_SECONDS))
        async with item.lock:
            # A sweep may have finished deleting it while this caller waited.
            self._pending[chat_id] = item
            item.active = True
            item.expires = time() + IDLE_SECONDS
            await self._persist()

    async def async_release(self, chat_id: str) -> None:
        """Reset the idle period after either a successful or uncertain turn."""
        if item := self._pending.get(chat_id):
            async with item.lock:
                item.active = False
                item.expires = time() + IDLE_SECONDS
                await self._persist()

    async def async_cleanup(self, _now=None) -> None:
        """Retry unavailable servers later and never delete running remote tasks."""
        if self._closed or not self._pending:
            return
        task = asyncio.current_task()
        if self._sweeps:
            return
        self._sweeps.add(task)
        try:
            for chat_id, item in list(self._pending.items()):
                if item.active or item.expires > time():
                    continue
                async with item.lock:
                    if item.active or item.expires > time():
                        continue
                    try:
                        if await self.client.async_get_chat_tasks(chat_id):
                            item.expires = time() + RETRY_SECONDS
                            continue
                        await self.client.async_delete_chat(chat_id)
                    except NotFoundError:
                        pass  # Already removed manually.
                    except OpenWebUIError as err:
                        item.expires = time() + RETRY_SECONDS
                        LOGGER.warning("Chat cleanup deferred: %s", err.key)
                        continue
                    self._pending.pop(chat_id, None)
            await self._persist()
        finally:
            self._sweeps.discard(task)

    async def async_close(self) -> None:
        """Stop local cleanup; persisted deadlines resume on next setup."""
        self._closed = True
        tasks = list(self._sweeps)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._persist()
