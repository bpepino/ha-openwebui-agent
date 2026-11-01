"""Push household events to the gateway (``POST /household/v1/events``).

Ported from the gateway's former Home Assistant component. What is
sent and when lives in ``events.py`` (pure, no Home Assistant imports); this
module feeds it Home Assistant's state changes, runs its timers and delivers
the events from a small in-memory queue: one POST per event, 5 s timeout, up
to 3 attempts on timeouts, connection errors, 408, 429 and 5xx, and one retry
after 60 s for a 2xx that says the event was rate limited. Problems are
logged at most once a minute.

Only entities exposed to Assist are watched (the same decision as
``/api/oikos``); persons listed in ``presence`` are tracked even when not
exposed. In the gateway's open mode no token is needed: the ``Authorization``
header is sent only when an events token is configured.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import timedelta
import time
from typing import Any

import httpx

from homeassistant.const import EVENT_HOMEASSISTANT_STOP, EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.start import async_at_started
from homeassistant.util import dt as dt_util

from . import events as push_events
from .const import (
    CONF_DOOR_OPEN_AFTER,
    CONF_EVENTS_TOKEN,
    CONF_GENERIC_STATE_CHANGES,
    CONF_PRESENCE,
    CONF_UNAVAILABLE_AFTER,
    LOGGER,
)
from .oikos import DEFAULT_ASSISTANT, is_exposed

PUSH_TIMEOUT_S = 5
PUSH_QUEUE_MAX = 200
EXPOSE_CACHE_S = 60.0


def duration_s(value: Any, default: float) -> float:
    """Convert a duration selector value (``{"minutes": 10}``) or seconds to seconds."""
    if isinstance(value, Mapping):
        try:
            return timedelta(**{k: float(v) for k, v in value.items()}).total_seconds()
        except (TypeError, ValueError):
            return default
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return default


def push_config(options: Mapping[str, Any]) -> push_events.PushConfig:
    """Options -> the mapper's settings; an empty presence list means every exposed person."""
    presence = options.get(CONF_PRESENCE) or None
    return push_events.PushConfig(
        events=True,
        presence=push_events.presence_tuple(presence) if presence else None,
        door_open_after_s=duration_s(options.get(CONF_DOOR_OPEN_AFTER), 600.0),
        unavailable_after_s=duration_s(options.get(CONF_UNAVAILABLE_AFTER), 120.0),
        generic_state_changes=bool(options.get(CONF_GENERIC_STATE_CHANGES, False)),
    )


def _snap(state: Any) -> push_events.Snap | None:
    """Home Assistant ``State`` -> ``events.Snap`` (None stays None)."""
    if state is None:
        return None
    attrs = getattr(state, "attributes", None)
    if not hasattr(attrs, "get"):
        attrs = {}
    return push_events.Snap(
        entity_id=state.entity_id,
        state=str(state.state),
        attributes=attrs,
        last_changed=getattr(state, "last_changed", None),
    )


class EventPusher:
    """Turn Home Assistant's state changes into events and deliver them.

    Feeds ``state_changed`` into ``events.EventMapper``, runs its timers with
    ``async_call_later`` and delivers the events from a small in-memory queue
    (3 attempts with backoff, 5 s timeout each, no disk). Never raises into
    Home Assistant; delivery problems are logged at most once a minute.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        base_url: str,
        options: Mapping[str, Any],
        http: httpx.AsyncClient,
    ) -> None:
        """Prepare the mapper and the queue; nothing runs before ``start``."""
        self._hass = hass
        self._http = http
        self._assistant = DEFAULT_ASSISTANT
        self.url = push_events.events_url(base_url)
        token = str(options.get(CONF_EVENTS_TOKEN) or "").strip()
        # Open mode needs no token: never send "Bearer " with an empty one.
        self._headers = (
            {"Authorization": push_events.auth_header(token)} if token else {}
        )
        self._mapper = push_events.EventMapper(push_config(options))
        self._timers: dict[tuple[str, str], Callable[[], None]] = {}
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=PUSH_QUEUE_MAX)
        self._worker: asyncio.Task | None = None
        self._unsubs: list[Callable[[], None]] = []
        self._expose_cache: dict[str, tuple[float, bool]] = {}
        self._throttle = push_events.WarnThrottle(60.0)
        self._stopped = False
        self.sent = 0
        self.failed = 0
        # 2xx answers that did not store the event (see push_events.classify_answer)
        self.rate_limited = 0
        self.filtered = 0
        self.last_error: str | None = None
        # event_id -> cancel of its single delayed rate-limit retry (bounded)
        self._rate_retries: dict[str, Callable[[], None]] = {}
        # event_ids whose retry is queued (the next delivery is the last try)
        self._rate_retried: set[str] = set()

    @property
    def has_token(self) -> bool:
        """Whether events carry an events client token."""
        return bool(self._headers)

    # ------------------------------------------------------------ lifecycle
    @callback
    def start(self, create_task: Callable[..., asyncio.Task]) -> None:
        """Listen, scan once Home Assistant has started, and run the sender."""
        self._worker = create_task(self._run(), "openwebui_agent event push")
        self._unsubs.append(
            self._hass.bus.async_listen(EVENT_STATE_CHANGED, self._on_state_changed)
        )
        self._unsubs.append(
            self._hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, self._on_stop)
        )
        self._unsubs.append(async_at_started(self._hass, self._scan))

    @callback
    def _on_stop(self, _event: Event) -> None:
        self.stop()

    @callback
    def stop(self) -> None:
        """Remove the listeners, cancel timers and retries, stop the sender (idempotent)."""
        self._stopped = True
        for cancel in (
            *self._unsubs,
            *self._timers.values(),
            *self._rate_retries.values(),
        ):
            try:
                cancel()
            except Exception:  # noqa: BLE001 - shutdown must finish
                LOGGER.debug("Event push: cleanup failed", exc_info=True)
        self._unsubs.clear()
        self._timers.clear()
        self._rate_retries.clear()
        self._mapper.clear()
        if self._worker is not None:
            self._worker.cancel()

    def status(self) -> dict[str, Any]:
        """Return the ``push`` block of ``/api/oikos`` and diagnostics (no token)."""
        return {
            "enabled": not self._stopped,
            "sent": self.sent,
            "failed": self.failed,
            "rate_limited": self.rate_limited,
            "filtered": self.filtered,
            "retries_pending": len(self._rate_retries),
            "last_error": self.last_error,
            "queued": self._queue.qsize(),
            "pending_timers": len(self._timers),
        }

    # -------------------------------------------------------------- inputs
    def _is_exposed(self, entity_id: str) -> bool:
        now = time.monotonic()
        hit = self._expose_cache.get(entity_id)
        if hit is not None and now - hit[0] < EXPOSE_CACHE_S:
            return hit[1]
        try:
            value = is_exposed(self._hass, entity_id, self._assistant)
        except Exception:  # noqa: BLE001 - an unknown entity is not exposed
            value = False
        self._expose_cache[entity_id] = (now, value)
        return value

    @callback
    def _scan(self, _hass: Any = None) -> None:
        """Start-up: time doors that are already open and entities that are already unavailable."""
        if self._stopped:
            return
        try:
            now = dt_util.utcnow()
            for state in self._hass.states.async_all():
                snap = _snap(state)
                if snap is None or not self._mapper.is_relevant(None, snap):
                    continue
                exposed = self._is_exposed(snap.entity_id)
                self._apply(
                    self._mapper.on_state_change(None, snap, exposed=exposed, now=now)
                )
        except Exception:  # noqa: BLE001
            LOGGER.exception("Event push: start-up scan failed")

    @callback
    def _on_state_changed(self, event: Event) -> None:
        if self._stopped:
            return
        try:
            data = event.data
            old = _snap(data.get("old_state"))
            new = _snap(data.get("new_state"))
            if not self._mapper.is_relevant(old, new):
                return
            exposed = self._is_exposed(new.entity_id) if new is not None else False
            self._apply(
                self._mapper.on_state_change(
                    old, new, exposed=exposed, now=dt_util.utcnow()
                )
            )
        except Exception:  # noqa: BLE001 - never raise into the event bus
            LOGGER.debug("Event push: state_changed handling failed", exc_info=True)

    def _apply(self, decision: push_events.Decision) -> None:
        for key in decision.cancel:
            cancel = self._timers.pop(key, None)
            if cancel is not None:
                cancel()
        for timer in decision.start:
            previous = self._timers.pop(timer.key, None)
            if previous is not None:
                previous()
            self._timers[timer.key] = async_call_later(
                self._hass,
                timer.delay_s,
                self._timer_action(timer.kind, timer.entity_id),
            )
        for ev in decision.events:
            self._enqueue(ev)

    def _timer_action(self, kind: str, entity_id: str) -> Callable[[Any], None]:
        @callback
        def _fire(_now: Any) -> None:
            self._timers.pop((kind, entity_id), None)
            if self._stopped:
                return
            try:
                ev = self._mapper.on_timer(
                    kind,
                    entity_id,
                    _snap(self._hass.states.get(entity_id)),
                    now=dt_util.utcnow(),
                )
                if ev is not None:
                    self._enqueue(ev)
            except Exception:  # noqa: BLE001
                LOGGER.debug(
                    "Event push: timer %s/%s failed", kind, entity_id, exc_info=True
                )

        return _fire

    # ------------------------------------------------------------ delivery
    def _enqueue(self, ev: dict[str, Any]) -> None:
        if self._queue.full():
            try:
                dropped = self._queue.get_nowait()
                self._fail(dropped, "queue_full")
            except asyncio.QueueEmpty:
                pass
        try:
            self._queue.put_nowait(ev)
        except asyncio.QueueFull:
            self._fail(ev, "queue_full")

    async def _run(self) -> None:
        while True:
            ev = await self._queue.get()
            try:
                await self._deliver(ev)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self._fail(ev, type(e).__name__)

    async def _deliver(self, ev: dict[str, Any]) -> None:
        event_id = str(ev.get("event_id") or "")
        already_retried = event_id in self._rate_retried
        self._rate_retried.discard(event_id)
        error = "unknown"
        for attempt in range(1, push_events.MAX_ATTEMPTS + 1):
            try:
                async with asyncio.timeout(PUSH_TIMEOUT_S):
                    response = await self._http.post(
                        self.url,
                        json=ev,
                        headers=self._headers,
                        timeout=PUSH_TIMEOUT_S,
                        follow_redirects=False,
                    )
                status, body = response.status_code, response.text
                text = body[:300]
            except (httpx.HTTPError, TimeoutError) as e:
                verdict, error = "retry", type(e).__name__
            else:
                # A 2xx is not always "stored": the JSON body may say
                # rate_limited / accepted=false (counted apart from "sent").
                verdict = push_events.classify_answer(status, body)
                error = (
                    f"HTTP {status}"
                    if verdict not in ("ok", "rate_limited", "filtered")
                    else ""
                )
                if verdict == "ok":
                    self.sent += 1
                    LOGGER.debug(
                        "Event %s (%s %s) -> %s %s",
                        ev.get("event_id"),
                        ev.get("type"),
                        ev.get("entity_id"),
                        status,
                        text,
                    )
                    return
                if verdict == "filtered":
                    self.filtered += 1
                    LOGGER.debug(
                        "Event %s (%s %s) filtered by the gateway: %s",
                        ev.get("event_id"),
                        ev.get("type"),
                        ev.get("entity_id"),
                        text,
                    )
                    return
                if verdict == "rate_limited":
                    self._on_rate_limited(ev, text, already_retried)
                    return
                if text:
                    error = f"{error}: {text[:120]}"
                if status == 401 and not self._headers:
                    error += " (no events token is set; enter one in Configure)"
            if verdict == "drop":
                break
            delay = push_events.retry_delay(attempt)
            if delay is None:
                break
            await asyncio.sleep(delay)
        self._fail(ev, error)

    def _on_rate_limited(
        self, ev: dict[str, Any], text: str, already_retried: bool
    ) -> None:
        """Retry once after a 2xx that did not store the event, then fail it."""
        self.rate_limited += 1
        event_id = str(ev.get("event_id") or "")
        delay = push_events.rate_limit_retry_delay(already_retried)
        if (
            delay is None
            or self._stopped
            or not event_id
            or event_id in self._rate_retries
            or len(self._rate_retries) >= PUSH_QUEUE_MAX
        ):
            self._fail(ev, f"rate limited by the gateway: {text[:120]}")
            return
        log_now, suppressed = self._throttle.allow(time.monotonic())
        if log_now:
            LOGGER.warning(
                "Event %s (%s %s) rate limited by the gateway, one retry in %.0f s%s",
                ev.get("event_id"),
                ev.get("type"),
                ev.get("entity_id"),
                delay,
                f" ({suppressed} similar warnings suppressed)" if suppressed else "",
            )
        self._rate_retries[event_id] = async_call_later(
            self._hass, delay, self._rate_retry_action(event_id, ev)
        )

    def _rate_retry_action(
        self, event_id: str, ev: dict[str, Any]
    ) -> Callable[[Any], None]:
        @callback
        def _fire(_now: Any) -> None:
            self._rate_retries.pop(event_id, None)
            if self._stopped:
                return
            self._rate_retried.add(event_id)
            self._enqueue(ev)

        return _fire

    def _fail(self, ev: dict[str, Any], error: str) -> None:
        self._rate_retried.discard(str(ev.get("event_id") or ""))
        self.failed += 1
        self.last_error = error
        log_now, suppressed = self._throttle.allow(time.monotonic())
        if log_now:
            LOGGER.warning(
                "Event %s (%s %s) not delivered to the gateway: %s%s",
                ev.get("event_id"),
                ev.get("type"),
                ev.get("entity_id"),
                error,
                f" ({suppressed} similar warnings suppressed)" if suppressed else "",
            )
