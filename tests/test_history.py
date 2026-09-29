"""Exercise chat retention without contacting a live server or deleting user data."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from owui_protocol.exceptions import OpenWebUIError, NotFoundError
from owui_protocol.history import ChatHistoryCleaner, IDLE_SECONDS
import owui_protocol.history as history_module


@pytest.fixture
def cleanup(monkeypatch):
    """Use a controlled clock and only a fake chat-deletion endpoint."""
    clock = [1000.0]
    monkeypatch.setattr(history_module, "time", lambda: clock[0])
    client = AsyncMock()
    client.async_get_chat_tasks.return_value = []
    save = AsyncMock()
    return ChatHistoryCleaner(client, save), client, save, clock


async def test_followups_extend_idle_deadline(cleanup):
    """An active or recently continued conversation must retain its context."""
    cleaner, client, save, clock = cleanup
    await cleaner.async_acquire("owned")
    clock[0] += IDLE_SECONDS + 1
    await cleaner.async_cleanup()
    client.async_delete_chat.assert_not_awaited()
    await cleaner.async_release("owned")
    clock[0] += IDLE_SECONDS - 1
    await cleaner.async_cleanup()
    client.async_delete_chat.assert_not_awaited()
    await cleaner.async_acquire("owned")
    await cleaner.async_release("owned")
    clock[0] += IDLE_SECONDS
    await cleaner.async_cleanup()
    client.async_delete_chat.assert_awaited_once_with("owned")
    assert cleaner.pending_count == 0
    assert save.call_args.args == ({"chats": {}},)


async def test_restart_resumes_only_owned_cleanup(cleanup):
    """Persisted IDs resume cleanup without enumerating any other user's chats."""
    cleaner, client, save, clock = cleanup
    await cleaner.async_acquire("owned")
    await cleaner.async_release("owned")
    restored = ChatHistoryCleaner(client, save, save.call_args.args[0])
    clock[0] += IDLE_SECONDS
    await restored.async_cleanup()
    client.async_delete_chat.assert_awaited_once_with("owned")


async def test_pending_task_is_not_deleted(cleanup):
    """An uncertain remote action must finish before its chat is removed."""
    cleaner, client, _, clock = cleanup
    await cleaner.async_acquire("owned")
    await cleaner.async_release("owned")
    clock[0] += IDLE_SECONDS
    client.async_get_chat_tasks.return_value = ["still-running"]
    await cleaner.async_cleanup()
    client.async_delete_chat.assert_not_awaited()
    client.async_get_chat_tasks.return_value = []
    clock[0] += 60
    await cleaner.async_cleanup()
    client.async_delete_chat.assert_awaited_once()


async def test_cleanup_failure_is_retried_later(cleanup):
    """A transient failure retains the record; already-deleted chats are forgotten."""
    cleaner, client, _, clock = cleanup
    await cleaner.async_acquire("owned")
    await cleaner.async_release("owned")
    clock[0] += IDLE_SECONDS
    client.async_delete_chat.side_effect = OpenWebUIError()
    await cleaner.async_cleanup()
    assert cleaner.pending_count == 1
    client.async_delete_chat.side_effect = NotFoundError()
    clock[0] += 60
    await cleaner.async_cleanup()
    assert cleaner.pending_count == 0


async def test_followup_cannot_race_deletion(cleanup):
    """Serialize acquiring context with expiry to prevent deletion during a turn."""
    cleaner, client, _, clock = cleanup
    await cleaner.async_acquire("owned")
    await cleaner.async_release("owned")
    clock[0] += IDLE_SECONDS
    queried, proceed = asyncio.Event(), asyncio.Event()

    async def tasks(*args):
        queried.set()
        await proceed.wait()
        return []

    client.async_get_chat_tasks.side_effect = tasks
    sweep = asyncio.create_task(cleaner.async_cleanup())
    await queried.wait()
    acquire = asyncio.create_task(cleaner.async_acquire("owned"))
    await asyncio.sleep(0)
    assert not acquire.done()
    proceed.set()
    await sweep
    await acquire
    # A following chat read will see 404 and use the existing recovery path.
    await cleaner.async_cleanup()
    client.async_delete_chat.assert_awaited_once()
    await cleaner.async_release("owned")


async def test_close_keeps_cleanup_metadata(cleanup):
    """Unload leaves a durable deadline and performs no late remote deletion."""
    cleaner, client, save, clock = cleanup
    await cleaner.async_acquire("owned")
    await cleaner.async_release("owned")
    await cleaner.async_close()
    clock[0] += IDLE_SECONDS
    await cleaner.async_cleanup()
    client.async_delete_chat.assert_not_awaited()
    assert "owned" in save.call_args.args[0]["chats"]
