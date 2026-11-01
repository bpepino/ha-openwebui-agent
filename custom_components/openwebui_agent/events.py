"""assistd event push: the pure part (no Home Assistant imports).

``__init__.py`` wires this module to Home Assistant: it converts HA ``State``
objects into ``Snap``, asks the component's exposure logic whether an entity is
exposed, feeds ``EventMapper`` and executes what it returns (send events,
start or cancel timers). Everything that decides *what* is sent lives here so
it can be unit tested without Home Assistant.

Events follow the gateway's ingest contract (``POST /household/v1/events``,
docs/gateway/HOUSEHOLD.md section 6). Each one carries a deterministic
``event_id`` derived from (type, entity_id, the state change time), so a retry
of the same event is a duplicate at the gateway, never a second row.

Emitted types (names match the gateway's default ``event_rules``):

* ``person_arrived_home`` / ``person_left_home``: a tracked ``person.*`` goes
  to / leaves ``home``. ``unavailable``/``unknown`` are not states: the mapper
  compares against the person's last REAL state, so ``home -> unknown ->
  not_home`` is still a departure, while ``home -> unknown -> home`` is nothing.
* ``door_open_long`` (priority high): an exposed ``binary_sensor`` of device
  class door / window / garage_door / opening stays ``on`` for
  ``door_open_after_s``.
* ``device_unavailable``: an exposed entity stays ``unavailable`` for
  ``unavailable_after_s``; ``device_available`` (priority low) when it comes
  back, only after a ``device_unavailable`` was emitted for it.
* ``state_changed`` (priority low, only with ``generic_state_changes``): the
  state value of an exposed entity changed (attribute-only updates and
  changes from or to ``unavailable``/``unknown`` are not sent).
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

SOURCE = "home_assistant"
EVENTS_PATH = "/household/v1/events"

PERSON_ARRIVED = "person_arrived_home"
PERSON_LEFT = "person_left_home"
DOOR_OPEN_LONG = "door_open_long"
DEVICE_UNAVAILABLE = "device_unavailable"
DEVICE_AVAILABLE = "device_available"
STATE_CHANGED = "state_changed"
EVENT_TYPES = (PERSON_ARRIVED, PERSON_LEFT, DOOR_OPEN_LONG, DEVICE_UNAVAILABLE, DEVICE_AVAILABLE, STATE_CHANGED)

PRIORITY = {
    PERSON_ARRIVED: "normal",
    PERSON_LEFT: "normal",
    DOOR_OPEN_LONG: "high",
    DEVICE_UNAVAILABLE: "normal",
    DEVICE_AVAILABLE: "low",
    STATE_CHANGED: "low",
}

DOOR_CLASSES = frozenset({"door", "window", "garage_door", "opening"})
UNAVAILABLE = "unavailable"
UNKNOWN = "unknown"
NOT_REAL = frozenset({UNAVAILABLE, UNKNOWN})

TIMER_DOOR = "door"
TIMER_UNAVAILABLE = "unavailable"

MAX_DATA_BYTES = 8 * 1024        # the gateway rejects a larger ``data``
ATTRIBUTE_BUDGET = 4 * 1024      # attributes inside a ``state_changed`` event
MAX_STRING = 200
MAX_LIST_ITEMS = 10
MAX_NAME = 64

# Attribute keys never sent: secrets, pictures/streams/URLs, and noisy option lists.
_SENSITIVE_KEY = re.compile(
    r"(token|password|passwd|secret|api_?key|access|auth|credential|cookie|session|"
    r"picture|image|thumbnail|stream|url|camera|code_format|code_arm|"
    # location and network identity (device trackers, routers, phones)
    r"latitude|longitude|gps|host_?name|b?ssid|"
    r"(?:^|_)(?:ip|ipv4|ipv6|mac)(?:$|_|addr)|ip_?address|mac_?address)", re.IGNORECASE)
_NOISY_KEY = re.compile(r"(_list$|_modes$|^supported_|^attribution$|^icon$|^friendly_name$|^editable$|"
                        r"^device_trackers$|^user_id$|^id$|^entity_id$)", re.IGNORECASE)


# ====================================================================== inputs
@dataclass(frozen=True)
class Snap:
    """The part of a Home Assistant ``State`` the mapper needs."""
    entity_id: str
    state: str
    attributes: Mapping[str, Any] = field(default_factory=dict)
    last_changed: Optional[datetime] = None

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]

    @property
    def device_class(self) -> str:
        dc = self.attributes.get("device_class") if isinstance(self.attributes, Mapping) else None
        return dc if isinstance(dc, str) else ""

    @property
    def name(self) -> str:
        n = self.attributes.get("friendly_name") if isinstance(self.attributes, Mapping) else None
        return n[:MAX_NAME] if isinstance(n, str) and n else ""


@dataclass(frozen=True)
class PushConfig:
    events: bool = True
    presence: Optional[Tuple[str, ...]] = None    # None: every exposed person.*
    door_open_after_s: float = 600.0
    unavailable_after_s: float = 120.0
    generic_state_changes: bool = False


@dataclass(frozen=True)
class TimerStart:
    kind: str           # TIMER_DOOR or TIMER_UNAVAILABLE
    entity_id: str
    delay_s: float

    @property
    def key(self) -> Tuple[str, str]:
        return (self.kind, self.entity_id)


@dataclass
class Decision:
    events: List[Dict[str, Any]] = field(default_factory=list)
    start: List[TimerStart] = field(default_factory=list)
    cancel: List[Tuple[str, str]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.events or self.start or self.cancel)


# ===================================================================== helpers
def _utc(dt: Optional[datetime]) -> datetime:
    if dt is None:
        return datetime.now(timezone.utc)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def iso(dt: Optional[datetime]) -> str:
    return _utc(dt).isoformat()


def event_id_for(event_type: str, entity_id: str, stamp: Any) -> str:
    """Deterministic id: ``ha-<type>-<24 hex>`` from (type, entity, stamp).

    ``stamp`` is the time of the state change the event is about (a datetime
    or an ISO string). Fits the gateway's ``event_id`` rules (<= 128 chars,
    letters, digits, ``. _ : -``)."""
    if isinstance(stamp, datetime):
        stamp = iso(stamp)
    digest = hashlib.sha256(f"{event_type}|{entity_id}|{stamp}".encode("utf-8")).hexdigest()[:24]
    return f"ha-{event_type}-{digest}"


def build_event(event_type: str, entity_id: str, *, stamp: Any, at: Optional[datetime],
                data: Mapping[str, Any], priority: Optional[str] = None) -> Dict[str, Any]:
    """One ingest body. ``stamp`` feeds the event id, ``at`` is the event time."""
    body = {k: v for k, v in dict(data).items() if v not in (None, "")}
    body = ensure_data_size(body)
    return {
        "source": SOURCE,
        "type": event_type,
        "entity_id": entity_id,
        "timestamp": iso(at),
        "data": body,
        "priority": priority or PRIORITY.get(event_type, "normal"),
        "event_id": event_id_for(event_type, entity_id, stamp),
    }


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _scalar(value: Any) -> Any:
    """A JSON-safe scalar, or ``_DROP``."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return value if len(value) <= MAX_STRING else value[:MAX_STRING] + "..."
    if isinstance(value, (datetime, date, dtime)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, (bytes, bytearray)):
        return _DROP
    text = str(value)
    return text if len(text) <= MAX_STRING else text[:MAX_STRING] + "..."


_DROP = object()


def _value(value: Any) -> Any:
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_scalar(v) for v in list(value)[:MAX_LIST_ITEMS] if not isinstance(v, (dict, list, tuple, set))]
        return [v for v in items if v is not _DROP]
    if isinstance(value, Mapping):
        out: Dict[str, Any] = {}
        for k, v in list(value.items())[:MAX_LIST_ITEMS]:
            if not isinstance(k, str) or _SENSITIVE_KEY.search(k) or isinstance(v, (dict, list, tuple, set)):
                continue
            s = _scalar(v)
            if s is not _DROP:
                out[k] = s
        return out
    return _scalar(value)


def trim_attributes(attrs: Optional[Mapping[str, Any]], budget: int = ATTRIBUTE_BUDGET) -> Dict[str, Any]:
    """A compact, JSON-safe copy of ``attrs`` that serializes to at most ``budget`` bytes.

    Drops secrets, pictures, URLs and option lists; truncates long strings and
    lists; flattens nested values one level; then removes the largest keys
    until the whole dict fits."""
    if not isinstance(attrs, Mapping):
        return {}
    out: Dict[str, Any] = {}
    for key, value in attrs.items():
        if not isinstance(key, str) or _SENSITIVE_KEY.search(key) or _NOISY_KEY.search(key):
            continue
        v = _value(value)
        if v is _DROP:
            continue
        out[key] = v
    while out and _json_size(out) > budget:
        largest = max(out, key=lambda k: _json_size({k: out[k]}))
        del out[largest]
    return out


def ensure_data_size(data: Dict[str, Any], limit: int = MAX_DATA_BYTES) -> Dict[str, Any]:
    """Keep ``data`` under the gateway's limit: shrink ``attributes`` first, then drop the largest keys."""
    if _json_size(data) <= limit:
        return data
    data = dict(data)
    if isinstance(data.get("attributes"), dict):
        rest = _json_size({k: v for k, v in data.items() if k != "attributes"})
        data["attributes"] = trim_attributes(data["attributes"], max(0, limit - rest - 32))
        if not data["attributes"]:
            del data["attributes"]
    while data and _json_size(data) > limit:
        largest = max(data, key=lambda k: _json_size({k: data[k]}))
        del data[largest]
    return data


def events_url(base_url: str) -> str:
    """``http://gw:8000`` (or the full ingest URL) -> ``http://gw:8000/household/v1/events``."""
    base = (base_url or "").strip().rstrip("/")
    if base.endswith(EVENTS_PATH):
        return base
    return base + EVENTS_PATH


def auth_header(token: str) -> str:
    """The raw events token -> ``Bearer <token>`` (a pasted ``Bearer ...`` is accepted too)."""
    tok = (token or "").strip()
    if tok.lower().startswith("bearer "):
        tok = tok[7:].strip()
    return f"Bearer {tok}"


# ================================================================ the mapper
class EventMapper:
    """State changes -> events and timer instructions. Holds the timer
    bookkeeping (which timers are pending, which entities were reported
    unavailable) but no clock and no I/O: callers pass ``now``."""

    def __init__(self, config: PushConfig):
        self.config = config
        # (kind, entity_id) -> (since, previous state) of a pending timer
        self._pending: Dict[Tuple[str, str], Tuple[datetime, str]] = {}
        # entity_id -> (unavailable since, previous state) once device_unavailable was emitted
        self._reported_unavailable: Dict[str, Tuple[datetime, str]] = {}
        # person entity_id -> last state that was not unavailable/unknown
        self._last_real_person: Dict[str, str] = {}

    # ------------------------------------------------------------ queries
    def pending(self) -> Set[Tuple[str, str]]:
        return set(self._pending)

    def reported_unavailable(self) -> Set[str]:
        return set(self._reported_unavailable)

    def is_person_tracked(self, entity_id: str, exposed: bool) -> bool:
        if not entity_id.startswith("person."):
            return False
        if self.config.presence is not None:
            return entity_id in self.config.presence
        return exposed

    @staticmethod
    def is_door(snap: Snap) -> bool:
        return snap.domain == "binary_sensor" and snap.device_class in DOOR_CLASSES

    def is_relevant(self, old: Optional[Snap], new: Optional[Snap]) -> bool:
        """Cheap pre-filter before the exposure lookup: removals, first states and
        state VALUE changes matter; attribute-only updates never do."""
        if not self.config.events:
            return False
        if new is None:
            if old is None:
                return False
            eid = old.entity_id
            return ((TIMER_DOOR, eid) in self._pending or (TIMER_UNAVAILABLE, eid) in self._pending
                    or eid in self._reported_unavailable)
        eid = new.entity_id
        if old is None:  # a first state: only an open door or an unavailable entity starts something
            return (self.is_door(new) and new.state == "on") or new.state == UNAVAILABLE
        if old.state == new.state:
            return False
        if self.config.generic_state_changes:
            return True
        return (eid.startswith("person.") or self.is_door(new) or new.state == UNAVAILABLE
                or (TIMER_DOOR, eid) in self._pending or (TIMER_UNAVAILABLE, eid) in self._pending
                or eid in self._reported_unavailable)

    # ------------------------------------------------------------ changes
    def on_state_change(self, old: Optional[Snap], new: Optional[Snap], *, exposed: bool,
                        now: Optional[datetime] = None) -> Decision:
        """``old`` None: first state seen (entity added, or the start-up scan);
        ``new`` None: entity removed."""
        out = Decision()
        if not self.config.events:
            return out
        now = _utc(now)
        if new is None:
            if old is not None:
                self._cancel(out, TIMER_DOOR, old.entity_id)
                self._cancel(out, TIMER_UNAVAILABLE, old.entity_id)
                self._reported_unavailable.pop(old.entity_id, None)
                self._last_real_person.pop(old.entity_id, None)
            return out
        if old is not None and old.state == new.state:
            return out
        eid = new.entity_id
        changed_at = _utc(new.last_changed or now)

        # presence: compared against the last REAL state, so a pass through
        # unknown/unavailable (home -> unknown -> not_home) is not lost.
        if self.is_person_tracked(eid, exposed):
            if old is not None and old.state not in NOT_REAL:
                previous: Optional[str] = old.state
            else:
                previous = self._last_real_person.get(eid)
            if new.state in NOT_REAL:
                if previous is not None:
                    self._last_real_person[eid] = previous
            else:
                self._last_real_person[eid] = new.state
                if previous is not None and previous != new.state:
                    data = {"state": new.state, "previous": previous, "name": new.name}
                    src = new.attributes.get("source") if isinstance(new.attributes, Mapping) else None
                    if isinstance(src, str) and src:
                        data["source"] = src[:128]
                    if new.state == "home":
                        out.events.append(build_event(PERSON_ARRIVED, eid, stamp=changed_at, at=changed_at,
                                                      data=data))
                    elif previous == "home":
                        out.events.append(build_event(PERSON_LEFT, eid, stamp=changed_at, at=changed_at,
                                                      data=data))

        if not exposed:
            # Not exposed (a listed person may still get here): no door/availability/generic events.
            # A pending timer of an entity that stopped being exposed is cancelled.
            self._cancel(out, TIMER_DOOR, eid)
            self._cancel(out, TIMER_UNAVAILABLE, eid)
            return out

        # door / window open too long (any other state, with or without device class, cancels)
        if self.is_door(new) and new.state == "on":
            self._start(out, TIMER_DOOR, eid, changed_at, old.state if old else "",
                        self.config.door_open_after_s, now)
        else:
            self._cancel(out, TIMER_DOOR, eid)

        # availability
        if new.state == UNAVAILABLE:
            prev = old.state if old is not None else ""
            self._start(out, TIMER_UNAVAILABLE, eid, changed_at, prev, self.config.unavailable_after_s, now)
        else:
            self._cancel(out, TIMER_UNAVAILABLE, eid)
            if new.state != UNKNOWN and eid in self._reported_unavailable:
                since, _prev = self._reported_unavailable.pop(eid)
                out.events.append(build_event(
                    DEVICE_AVAILABLE, eid, stamp=changed_at, at=changed_at,
                    data={"state": new.state, "name": new.name, "domain": new.domain,
                          "unavailable_since": iso(since),
                          "unavailable_s": int(max(0.0, (changed_at - since).total_seconds()))}))

        # generic
        if self.config.generic_state_changes and old is not None \
                and old.state not in NOT_REAL and new.state not in NOT_REAL:
            out.events.append(build_event(
                STATE_CHANGED, eid, stamp=f"{iso(changed_at)}|{new.state}", at=changed_at,
                data={"state": new.state[:MAX_STRING], "previous": old.state[:MAX_STRING], "domain": new.domain,
                      "name": new.name, "attributes": trim_attributes(new.attributes) or None}))
        return out

    # ------------------------------------------------------------- timers
    def on_timer(self, kind: str, entity_id: str, current: Optional[Snap], *,
                 now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
        """A timer fired. Returns the event when its condition still holds
        (same state, same ``last_changed`` as when the timer started)."""
        now = _utc(now)
        pending = self._pending.pop((kind, entity_id), None)
        if pending is None or current is None:
            return None
        since, prev = pending
        if current.last_changed is not None and _utc(current.last_changed) != since:
            return None
        held_s = int(max(0.0, (now - since).total_seconds()))
        if kind == TIMER_DOOR:
            if current.state != "on" or not self.is_door(current):
                return None
            return build_event(DOOR_OPEN_LONG, entity_id, stamp=since, at=now,
                               data={"state": "on", "device_class": current.device_class, "name": current.name,
                                     "open_since": iso(since), "open_s": held_s})
        if kind == TIMER_UNAVAILABLE:
            if current.state != UNAVAILABLE:
                return None
            self._reported_unavailable[entity_id] = (since, prev)
            return build_event(DEVICE_UNAVAILABLE, entity_id, stamp=since, at=now,
                               data={"state": UNAVAILABLE, "previous": prev, "name": current.name,
                                     "domain": current.domain, "since": iso(since), "unavailable_s": held_s})
        return None

    def clear(self) -> List[Tuple[str, str]]:
        """Forget every pending timer (shutdown); returns their keys."""
        keys = list(self._pending)
        self._pending.clear()
        return keys

    def _start(self, out: Decision, kind: str, entity_id: str, since: datetime, prev: str, after_s: float,
               now: datetime) -> None:
        key = (kind, entity_id)
        current = self._pending.get(key)
        if current is not None and current[0] == since:
            return  # the same condition is already being timed (idempotent start-up scan)
        if current is not None:
            out.cancel.append(key)
        self._pending[key] = (since, prev)
        remaining = max(0.0, float(after_s) - max(0.0, (now - since).total_seconds()))
        out.start.append(TimerStart(kind, entity_id, remaining))

    def _cancel(self, out: Decision, kind: str, entity_id: str) -> None:
        if self._pending.pop((kind, entity_id), None) is not None:
            out.cancel.append((kind, entity_id))


# ============================================================ sending policy
RETRY_DELAYS_S = (2.0, 8.0)        # 3 attempts in total
MAX_ATTEMPTS = len(RETRY_DELAYS_S) + 1


def classify_status(status: int) -> str:
    """``ok`` (2xx), ``retry`` (408, 429, 5xx) or ``drop`` (other answers: a bad body or token won't heal)."""
    if 200 <= status < 300:
        return "ok"
    if status in (408, 429) or status >= 500:
        return "retry"
    return "drop"


RATE_LIMIT_RETRY_S = 60.0          # one delayed retry after a rate-limited answer


def classify_answer(status: int, body: Any) -> str:
    """Verdict for one ingest answer: ``classify_status`` for the HTTP status,
    refined for a 2xx by its JSON body (the gateway answers 2xx even when it
    did not store the event):

    * ``rate_limited``: ``rate_limited: true``, or ``accepted: false`` for a
      reason that may heal (``store_error``); retried once after
      ``RATE_LIMIT_RETRY_S``;
    * ``filtered``: ``accepted: false`` with ``reason: type_filtered`` (a
      gateway rule; retrying will not help);
    * ``ok`` otherwise (a non-JSON or unknown 2xx body still counts as sent).

    ``body`` is the response text or an already parsed value."""
    verdict = classify_status(status)
    if verdict != "ok":
        return verdict
    if isinstance(body, (str, bytes, bytearray)):
        try:
            body = json.loads(body) if body else None
        except ValueError:
            return "ok"
    if not isinstance(body, Mapping):
        return "ok"
    if body.get("rate_limited") is True:
        return "rate_limited"
    if body.get("accepted") is False:
        return "filtered" if body.get("reason") == "type_filtered" else "rate_limited"
    return "ok"


def rate_limit_retry_delay(already_retried: bool) -> Optional[float]:
    """Seconds until the single retry of a rate-limited event; None once it was retried."""
    return None if already_retried else RATE_LIMIT_RETRY_S


def retry_delay(attempt: int) -> Optional[float]:
    """Seconds to wait after failed attempt ``attempt`` (1-based), None when no attempt is left."""
    if 1 <= attempt <= len(RETRY_DELAYS_S):
        return RETRY_DELAYS_S[attempt - 1]
    return None


class WarnThrottle:
    """At most one warning per ``interval_s``; counts what it suppressed."""

    def __init__(self, interval_s: float = 60.0):
        self.interval_s = float(interval_s)
        self._last: Optional[float] = None
        self.suppressed = 0

    def allow(self, now: float) -> Tuple[bool, int]:
        """``(log_now, suppressed_since_last_warning)``."""
        if self._last is None or now - self._last >= self.interval_s:
            self._last = now
            n, self.suppressed = self.suppressed, 0
            return True, n
        self.suppressed += 1
        return False, 0


def presence_tuple(values: Optional[Iterable[str]]) -> Optional[Tuple[str, ...]]:
    if values is None:
        return None
    return tuple(str(v).strip().lower() for v in values if str(v).strip())
