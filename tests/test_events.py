"""Household event push: the pure mapping in events.py.

Ported from the gateway's own tests of this module. The
gateway's ingest contract (``gateway/household/models.py``) and its event-rule
names are copied below so these tests run without the gateway package.
"""

from datetime import UTC, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re

import pytest

from owui_protocol import events as ev

PATH = Path(__file__).parents[1] / "custom_components/openwebui_agent/events.py"
# sha256 of the gateway repository's archived copy of this module
ARCHIVED_SHA256 = "f2e00922c5405b60827b4e6ddb88bfb850342ecb7cd69a6872971607f962a71d"

# gateway/household/models.py
EVENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]*$")
TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9_.\-]*$")
ENTITY_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:\-/]*$")
MAX_EVENT_DATA_BYTES = 8 * 1024
MAX_EVENT_AGE_S = 7 * 86400
MAX_EVENT_AHEAD_S = 300
SOURCES = {"home_assistant", "frigate", "calendar", "scheduler", "system", "manual"}
PRIORITIES = {"low", "normal", "high", "urgent"}
# gateway/household/settings.py: the types that can start a household review
# (LEGACY_REASONING_TYPES; "auto" resolves to a subset of them).
REASONING_TYPES = (
    "person_arrived_home",
    "person_left_home",
    "door_open_long",
    "device_unavailable",
    "frigate_person",
    "water_leak",
    "smoke_detected",
)

T0 = datetime(2026, 10, 31, 18, 0, 0, tzinfo=UTC)


def snap(entity_id, state, at=T0, **attrs):
    """Build a mapper input."""
    return ev.Snap(entity_id=entity_id, state=state, attributes=attrs, last_changed=at)


def mapper(**kw):
    """Build a mapper with these settings."""
    return ev.EventMapper(ev.PushConfig(**kw))


def validate_ingest_body(body: dict, now: datetime) -> None:
    """Apply the gateway's ``Event`` model rules to one ingest body."""
    assert body["source"] in SOURCES
    assert len(body["type"]) <= 64 and TYPE_RE.match(body["type"])
    assert len(body["entity_id"]) <= 128 and ENTITY_RE.match(body["entity_id"])
    assert len(body["event_id"]) <= 128 and EVENT_ID_RE.match(body["event_id"])
    assert body["priority"] in PRIORITIES
    assert isinstance(body["data"], dict)
    size = len(
        json.dumps(body["data"], ensure_ascii=False, separators=(",", ":")).encode()
    )
    assert size <= MAX_EVENT_DATA_BYTES
    stamp = datetime.fromisoformat(body["timestamp"])
    assert stamp.tzinfo is not None
    assert now - timedelta(seconds=MAX_EVENT_AGE_S) <= stamp
    assert stamp <= now + timedelta(seconds=MAX_EVENT_AHEAD_S)


def test_module_is_the_archived_copy():
    """events.py is kept byte-identical to the gateway's archived module."""
    assert hashlib.sha256(PATH.read_bytes()).hexdigest() == ARCHIVED_SHA256


def test_module_has_no_homeassistant_import():
    """The pure module never imports Home Assistant."""
    source = PATH.read_text(encoding="utf-8")
    assert "homeassistant" not in source.replace("Home Assistant", "")


# ------------------------------------------------------------------ presence
def test_person_arrives_and_leaves():
    """Arrival and departure of a tracked person."""
    m = mapper()
    d = m.on_state_change(
        snap("person.alex", "not_home"),
        snap("person.alex", "home", source="device_tracker.p"),
        exposed=True,
        now=T0,
    )
    assert [e["type"] for e in d.events] == ["person_arrived_home"]
    e = d.events[0]
    assert e["source"] == "home_assistant"
    assert e["entity_id"] == "person.alex" and e["priority"] == "normal"
    assert e["data"] == {
        "state": "home",
        "previous": "not_home",
        "source": "device_tracker.p",
    }
    assert e["timestamp"] == T0.isoformat()

    later = T0 + timedelta(hours=2)
    d = m.on_state_change(
        snap("person.alex", "home"),
        snap("person.alex", "work", later),
        exposed=True,
        now=later,
    )
    assert [e["type"] for e in d.events] == ["person_left_home"]
    assert d.events[0]["data"]["state"] == "work"
    assert d.events[0]["data"]["previous"] == "home"


def test_person_zone_change_and_restart_artifacts_are_not_events():
    """Zone moves and restarts send nothing."""
    m = mapper()
    assert not m.on_state_change(
        snap("person.b", "work"), snap("person.b", "school"), exposed=True
    ).events
    # no real state known yet (fresh start): unknown -> home is not an arrival
    assert not m.on_state_change(
        snap("person.c", "unknown"), snap("person.c", "home"), exposed=True
    ).events
    # home -> unavailable -> home: a restart, not a movement
    assert not m.on_state_change(
        snap("person.c", "home"), snap("person.c", "unavailable"), exposed=True
    ).events
    assert not m.on_state_change(
        snap("person.c", "unavailable"), snap("person.c", "home"), exposed=True
    ).events
    assert not m.on_state_change(None, snap("person.c", "home"), exposed=True).events
    assert not m.on_state_change(
        snap("person.c", "home"), snap("person.c", "home"), exposed=True
    ).events


def test_person_moves_through_unknown_are_not_lost():
    """Compare against the last real state."""
    m = mapper()
    assert not m.on_state_change(None, snap("person.d", "home"), exposed=True).events
    assert not m.on_state_change(
        snap("person.d", "home"), snap("person.d", "unknown"), exposed=True
    ).events
    d = m.on_state_change(
        snap("person.d", "unknown"), snap("person.d", "not_home"), exposed=True
    )
    assert [e["type"] for e in d.events] == ["person_left_home"]
    assert d.events[0]["data"]["previous"] == "home"
    # not_home -> unavailable -> unknown -> home is an arrival
    assert not m.on_state_change(
        snap("person.d", "not_home"), snap("person.d", "unavailable"), exposed=True
    ).events
    assert not m.on_state_change(
        snap("person.d", "unavailable"), snap("person.d", "unknown"), exposed=True
    ).events
    d = m.on_state_change(
        snap("person.d", "unknown"), snap("person.d", "home"), exposed=True
    )
    assert [e["type"] for e in d.events] == ["person_arrived_home"]
    assert d.events[0]["data"]["previous"] == "not_home"
    # a removed person forgets its last state
    m.on_state_change(snap("person.d", "home"), None, exposed=True)
    assert not m.on_state_change(
        snap("person.d", "unknown"), snap("person.d", "not_home"), exposed=True
    ).events


def test_person_needs_exposure_unless_listed():
    """A presence list tracks unexposed persons and limits the rest."""
    old, new = snap("person.anna", "not_home"), snap("person.anna", "home")
    assert not mapper().on_state_change(old, new, exposed=False).events
    listed = mapper(presence=("person.anna",))
    assert [
        e["type"] for e in listed.on_state_change(old, new, exposed=False).events
    ] == ["person_arrived_home"]
    # a presence list restricts the persons too
    assert not listed.on_state_change(
        snap("person.bob", "not_home"), snap("person.bob", "home"), exposed=True
    ).events


# ---------------------------------------------------------------------- doors
def test_door_timer_start_cancel():
    """An open door starts a timer; closing cancels it."""
    m = mapper(door_open_after_s=600)
    d = m.on_state_change(
        snap("binary_sensor.balcony", "off", device_class="door"),
        snap("binary_sensor.balcony", "on", device_class="door"),
        exposed=True,
        now=T0,
    )
    assert d.events == []
    assert d.start == [ev.TimerStart("door", "binary_sensor.balcony", 600.0)]
    assert ("door", "binary_sensor.balcony") in m.pending()

    closed = T0 + timedelta(minutes=3)
    d = m.on_state_change(
        snap("binary_sensor.balcony", "on", device_class="door"),
        snap("binary_sensor.balcony", "off", closed, device_class="door"),
        exposed=True,
        now=closed,
    )
    assert d.cancel == [("door", "binary_sensor.balcony")] and not d.events
    assert m.pending() == set()


def test_door_timer_fires_with_high_priority_and_stable_id():
    """door_open_long is high priority with a stable id."""
    m = mapper(door_open_after_s=600)
    open_state = snap(
        "binary_sensor.garage", "on", device_class="garage_door", friendly_name="Garage"
    )
    m.on_state_change(
        snap("binary_sensor.garage", "off", device_class="garage_door"),
        open_state,
        exposed=True,
        now=T0,
    )
    fired_at = T0 + timedelta(minutes=10)
    e = m.on_timer("door", "binary_sensor.garage", open_state, now=fired_at)
    assert e["type"] == "door_open_long" and e["priority"] == "high"
    assert e["data"] == {
        "state": "on",
        "device_class": "garage_door",
        "name": "Garage",
        "open_since": T0.isoformat(),
        "open_s": 600,
    }
    assert e["timestamp"] == fired_at.isoformat()
    assert e["event_id"] == ev.event_id_for(
        "door_open_long", "binary_sensor.garage", T0
    )
    assert m.pending() == set()
    # a second fire of the same key does nothing
    assert m.on_timer("door", "binary_sensor.garage", open_state, now=fired_at) is None


def test_door_timer_does_not_fire_when_the_state_moved_on():
    """A reopened or closed door does not fire the old timer."""
    m = mapper()
    m.on_state_change(
        snap("binary_sensor.w", "off", device_class="window"),
        snap("binary_sensor.w", "on", device_class="window"),
        exposed=True,
        now=T0,
    )
    reopened = snap(
        "binary_sensor.w", "on", T0 + timedelta(minutes=5), device_class="window"
    )
    assert (
        m.on_timer("door", "binary_sensor.w", reopened, now=T0 + timedelta(minutes=10))
        is None
    )
    m.on_state_change(
        snap("binary_sensor.w", "off", device_class="window"),
        snap("binary_sensor.w", "on", device_class="window"),
        exposed=True,
        now=T0,
    )
    closed = snap("binary_sensor.w", "off", device_class="window")
    assert (
        m.on_timer("door", "binary_sensor.w", closed, now=T0 + timedelta(minutes=10))
        is None
    )


def test_door_open_at_startup_uses_the_remaining_time():
    """A door open at start-up is timed from last_changed."""
    m = mapper(door_open_after_s=600)
    open_state = snap("binary_sensor.front", "on", device_class="opening")
    assert m.is_relevant(None, open_state)
    d = m.on_state_change(None, open_state, exposed=True, now=T0 + timedelta(minutes=4))
    assert d.start == [ev.TimerStart("door", "binary_sensor.front", 360.0)]
    # the same open door seen again (scan + state_changed) is not re-timed
    assert not m.on_state_change(
        None, open_state, exposed=True, now=T0 + timedelta(minutes=5)
    )
    # overdue: fire at once
    d = mapper(door_open_after_s=600).on_state_change(
        None, open_state, exposed=True, now=T0 + timedelta(hours=1)
    )
    assert d.start[0].delay_s == 0.0


def test_non_door_classes_and_unexposed_doors_are_ignored():
    """Only exposed door-like binary sensors count."""
    m = mapper()
    assert not m.on_state_change(
        snap("binary_sensor.motion", "off", device_class="motion"),
        snap("binary_sensor.motion", "on", device_class="motion"),
        exposed=True,
    )
    assert not m.on_state_change(
        snap("binary_sensor.d", "off", device_class="door"),
        snap("binary_sensor.d", "on", device_class="door"),
        exposed=False,
    )
    assert not m.on_state_change(
        snap("sensor.door", "off", device_class="door"),
        snap("sensor.door", "on", device_class="door"),
        exposed=True,
    )


def test_removed_entity_cancels_its_timers():
    """A removed entity cancels its timers."""
    m = mapper()
    old = snap("binary_sensor.d", "on", device_class="door")
    m.on_state_change(
        snap("binary_sensor.d", "off", device_class="door"), old, exposed=True, now=T0
    )
    assert m.is_relevant(old, None)
    d = m.on_state_change(old, None, exposed=False, now=T0)
    assert d.cancel == [("door", "binary_sensor.d")] and m.pending() == set()


# --------------------------------------------------------------- availability
def test_unavailable_then_available():
    """device_unavailable, then device_available."""
    m = mapper(unavailable_after_s=120)
    gone = snap("light.kitchen", "unavailable", T0, friendly_name="Kitchen")
    d = m.on_state_change(snap("light.kitchen", "on"), gone, exposed=True, now=T0)
    assert d.start == [ev.TimerStart("unavailable", "light.kitchen", 120.0)]
    assert not d.events

    e = m.on_timer("unavailable", "light.kitchen", gone, now=T0 + timedelta(minutes=2))
    assert e["type"] == "device_unavailable" and e["priority"] == "normal"
    assert e["data"] == {
        "state": "unavailable",
        "previous": "on",
        "name": "Kitchen",
        "domain": "light",
        "since": T0.isoformat(),
        "unavailable_s": 120,
    }
    assert m.reported_unavailable() == {"light.kitchen"}

    back_at = T0 + timedelta(minutes=30)
    d = m.on_state_change(
        gone, snap("light.kitchen", "off", back_at), exposed=True, now=back_at
    )
    assert [x["type"] for x in d.events] == ["device_available"]
    a = d.events[0]
    assert a["priority"] == "low" and a["data"]["unavailable_s"] == 1800
    assert a["data"]["state"] == "off"
    assert m.reported_unavailable() == set()


def test_short_unavailability_is_not_reported():
    """A short outage sends nothing; unknown is not available."""
    m = mapper(unavailable_after_s=120)
    gone = snap("switch.pump", "unavailable", T0)
    m.on_state_change(snap("switch.pump", "on"), gone, exposed=True, now=T0)
    back = T0 + timedelta(seconds=30)
    d = m.on_state_change(gone, snap("switch.pump", "on", back), exposed=True, now=back)
    assert d.cancel == [("unavailable", "switch.pump")] and not d.events
    # unknown also ends the unavailable timer, but is not "available" yet
    m2 = mapper()
    m2.on_state_change(snap("switch.pump", "on"), gone, exposed=True, now=T0)
    m2.on_timer("unavailable", "switch.pump", gone, now=T0 + timedelta(minutes=3))
    d = m2.on_state_change(
        gone, snap("switch.pump", "unknown", back), exposed=True, now=back
    )
    assert not d.events and m2.reported_unavailable() == {"switch.pump"}
    later = back + timedelta(minutes=1)
    d = m2.on_state_change(
        snap("switch.pump", "unknown", back),
        snap("switch.pump", "on", later),
        exposed=True,
        now=later,
    )
    assert [x["type"] for x in d.events] == ["device_available"]


def test_unexposed_entities_get_no_availability_events():
    """Unexposed entities are not watched."""
    m = mapper()
    assert not m.on_state_change(
        snap("light.x", "on"), snap("light.x", "unavailable"), exposed=False
    )


# -------------------------------------------------------------------- generic
def test_generic_state_changes_off_by_default():
    """Generic state changes are off by default."""
    m = mapper()
    old, new = snap("light.lamp", "off"), snap("light.lamp", "on")
    assert not m.is_relevant(old, new)
    assert not m.on_state_change(old, new, exposed=True).events


def test_generic_on_off():
    """state_changed with trimmed attributes."""
    m = mapper(generic_state_changes=True)
    old = snap("light.lamp", "off")
    new = snap(
        "light.lamp",
        "on",
        brightness=200,
        friendly_name="Lamp",
        entity_picture="/api/x?token=abc",
        effect_list=["a", "b"],
        access_token="secret",
    )
    assert m.is_relevant(old, new)
    d = m.on_state_change(old, new, exposed=True, now=T0)
    assert [e["type"] for e in d.events] == ["state_changed"]
    e = d.events[0]
    assert e["priority"] == "low"
    assert e["data"] == {
        "state": "on",
        "previous": "off",
        "domain": "light",
        "name": "Lamp",
        "attributes": {"brightness": 200},
    }
    # attribute-only updates and transitions through unavailable are not sent
    same = snap("light.lamp", "on", brightness=10)
    assert not m.is_relevant(new, same)
    changes = m.on_state_change(new, snap("light.lamp", "unavailable"), exposed=True)
    assert not [x for x in changes.events if x["type"] == "state_changed"]
    assert (
        not mapper(generic_state_changes=True)
        .on_state_change(old, new, exposed=False)
        .events
    )


def test_generic_and_presence_events_together():
    """A person change can send both events."""
    m = mapper(generic_state_changes=True)
    d = m.on_state_change(
        snap("person.b", "not_home"), snap("person.b", "home"), exposed=True, now=T0
    )
    assert sorted(e["type"] for e in d.events) == [
        "person_arrived_home",
        "state_changed",
    ]
    assert len({e["event_id"] for e in d.events}) == 2


def test_events_disabled_does_nothing():
    """Events off: nothing is relevant."""
    m = mapper(events=False, generic_state_changes=True)
    assert not m.is_relevant(snap("person.b", "not_home"), snap("person.b", "home"))
    assert not m.on_state_change(
        snap("person.b", "not_home"), snap("person.b", "home"), exposed=True
    )


# ------------------------------------------------------------------- event ids
def test_event_id_is_deterministic_and_valid():
    """The same change always gives the same id."""
    a = ev.event_id_for("person_arrived_home", "person.alex", T0)
    assert a == ev.event_id_for("person_arrived_home", "person.alex", T0.isoformat())
    assert a == ev.event_id_for(
        "person_arrived_home",
        "person.alex",
        T0.astimezone(timezone(timedelta(hours=2))),
    )
    assert a != ev.event_id_for("person_left_home", "person.alex", T0)
    assert a != ev.event_id_for("person_arrived_home", "person.anna", T0)
    assert a != ev.event_id_for(
        "person_arrived_home", "person.alex", T0 + timedelta(seconds=1)
    )
    assert a.startswith("ha-person_arrived_home-") and len(a) <= 128
    assert EVENT_ID_RE.match(a)

    def arrive():
        return (
            mapper()
            .on_state_change(
                snap("person.alex", "not_home"),
                snap("person.alex", "home"),
                exposed=True,
                now=T0,
            )
            .events[0]
        )

    assert arrive() == arrive()  # the same change -> the identical body


def test_generic_event_ids_differ_per_change():
    """Each state change has its own id."""
    m = mapper(generic_state_changes=True)
    e1 = m.on_state_change(
        snap("light.l", "off"), snap("light.l", "on", T0), exposed=True
    ).events[0]
    e2 = m.on_state_change(
        snap("light.l", "on", T0),
        snap("light.l", "off", T0 + timedelta(seconds=5)),
        exposed=True,
    ).events[0]
    assert e1["event_id"] != e2["event_id"]


# --------------------------------------------------------------- attributes
def test_trim_attributes_strips_secrets_pictures_and_noise():
    """Secrets, pictures and option lists are dropped."""
    attrs = {
        "brightness": 128,
        "unit_of_measurement": "W",
        "entity_picture": "/local/p.png",
        "access_token": "t",
        "api_key": "k",
        "stream_source": "rtsp://x",
        "media_image_url": "http://x",
        "friendly_name": "F",
        "source_list": ["a"],
        "supported_features": 3,
        "icon": "mdi:x",
        "password": "p",
        "when": T0,
        "ratio": float("nan"),
        "blob": b"\x00",
        "nested": {"a": 1, "b": {"deep": 1}, "token": "x"},
        "items": list(range(50)),
        "long": "x" * 1000,
    }
    out = ev.trim_attributes(attrs)
    assert set(out) == {
        "brightness",
        "unit_of_measurement",
        "when",
        "ratio",
        "nested",
        "items",
        "long",
    }
    assert out["when"] == T0.isoformat() and out["ratio"] == "nan"
    assert out["nested"] == {"a": 1}
    assert out["items"] == list(range(10))
    assert len(out["long"]) <= ev.MAX_STRING + 3
    json.dumps(out, allow_nan=False)


def test_trim_attributes_drops_location_and_network_data():
    """Location and network identity never leave."""
    attrs = {
        "latitude": 37.9,
        "longitude": 23.7,
        "gps_accuracy": 5,
        "gps": [1, 2],
        "ip": "10.0.0.2",
        "ip_address": "10.0.0.3",
        "local_ip": "10.0.0.4",
        "ipv4": "10.0.0.5",
        "mac": "aa:bb",
        "mac_address": "aa:cc",
        "host_name": "phone",
        "hostname": "phone2",
        "ssid": "Home",
        "bssid": "aa:dd",
        "wifi_ssid": "Home",
        "nested": {"latitude": 1, "ok": 2},
        # look-alikes that are not location/network data stay
        "zip_code": "123",
        "description": "d",
        "machine_state": "idle",
        "equipment": "e",
        "battery": 80,
    }
    out = ev.trim_attributes(attrs)
    assert set(out) == {
        "nested",
        "zip_code",
        "description",
        "machine_state",
        "equipment",
        "battery",
    }
    assert out["nested"] == {"ok": 2}
    m = mapper(generic_state_changes=True)
    e = m.on_state_change(
        snap("device_tracker.p", "home"),
        snap("device_tracker.p", "not_home", **attrs),
        exposed=True,
        now=T0,
    ).events[0]
    sent = json.dumps(e)
    for leaked in ("37.9", "10.0.0", "aa:bb", "phone", 'Home"'):
        assert leaked not in sent


def test_trim_attributes_respects_the_budget():
    """Attributes fit the byte budget."""
    attrs = {f"k{i}": "v" * 150 for i in range(100)}
    attrs["small"] = 1
    out = ev.trim_attributes(attrs, budget=2048)
    assert len(json.dumps(out, separators=(",", ":")).encode()) <= 2048
    assert out["small"] == 1
    assert ev.trim_attributes(None) == {}


def test_generic_event_data_stays_under_the_gateway_limit():
    """Event data stays under the gateway's 8 KiB."""
    big = {f"attr_{i}": "y" * 199 for i in range(200)}
    m = mapper(generic_state_changes=True)
    e = m.on_state_change(
        snap("sensor.big", "1"), snap("sensor.big", "2", **big), exposed=True, now=T0
    ).events[0]
    size = len(json.dumps(e["data"], separators=(",", ":")).encode())
    assert size <= MAX_EVENT_DATA_BYTES
    assert e["data"]["state"] == "2"


def test_ensure_data_size_drops_largest_keys():
    """Oversized data loses its largest keys."""
    data = {"a": "x" * 9000, "b": 1}
    assert ev.ensure_data_size(data) == {"b": 1}


# ------------------------------------------------- gateway contract and names
def test_type_names_match_the_gateway_rules():
    """The first four types can start a household review; the last two are stored."""
    for t in (
        ev.PERSON_ARRIVED,
        ev.PERSON_LEFT,
        ev.DOOR_OPEN_LONG,
        ev.DEVICE_UNAVAILABLE,
    ):
        assert t in REASONING_TYPES
    assert ev.DEVICE_AVAILABLE not in REASONING_TYPES
    assert ev.STATE_CHANGED not in REASONING_TYPES
    for t in ev.EVENT_TYPES:
        assert TYPE_RE.match(t)


@pytest.mark.parametrize(
    "kind", ["person", "door", "unavailable", "available", "generic"]
)
def test_every_emitted_event_validates_against_the_ingest_contract(kind):
    """Every event passes the gateway's ingest rules."""
    m = mapper(generic_state_changes=True)
    if kind == "person":
        e = m.on_state_change(
            snap("person.b", "not_home"), snap("person.b", "home"), exposed=True, now=T0
        ).events[0]
    elif kind == "door":
        s = snap("binary_sensor.d", "on", device_class="door")
        m.on_state_change(None, s, exposed=True, now=T0)
        e = m.on_timer("door", "binary_sensor.d", s, now=T0 + timedelta(minutes=10))
    elif kind in ("unavailable", "available"):
        s = snap("light.x", "unavailable")
        m.on_state_change(None, s, exposed=True, now=T0)
        e = m.on_timer("unavailable", "light.x", s, now=T0 + timedelta(minutes=2))
        if kind == "available":
            back = T0 + timedelta(minutes=5)
            e = m.on_state_change(
                s, snap("light.x", "on", back), exposed=True, now=back
            ).events[0]
    else:
        e = m.on_state_change(
            snap("light.x", "off"),
            snap("light.x", "on", brightness=3),
            exposed=True,
            now=T0,
        ).events[0]
    body = json.loads(json.dumps(e))
    validate_ingest_body(body, T0 + timedelta(minutes=10))
    assert body["source"] == "home_assistant"


# ---------------------------------------------------------- sending helpers
def test_events_url_and_auth_header():
    """The ingest URL and the bearer header."""
    assert ev.events_url("http://gw:8000") == "http://gw:8000/household/v1/events"
    assert ev.events_url("http://gw:8000/") == "http://gw:8000/household/v1/events"
    assert (
        ev.events_url("http://gw:8000/household/v1/events")
        == "http://gw:8000/household/v1/events"
    )
    assert ev.auth_header("abc") == "Bearer abc"
    assert ev.auth_header(" Bearer abc ") == "Bearer abc"


def test_status_classification_and_backoff():
    """Which answers are retried, and when."""
    assert ev.classify_status(200) == "ok" and ev.classify_status(202) == "ok"
    for s in (400, 401, 403, 404, 422):
        assert ev.classify_status(s) == "drop"
    for s in (408, 429, 500, 502, 503):
        assert ev.classify_status(s) == "retry"
    assert ev.MAX_ATTEMPTS == 3
    assert [ev.retry_delay(i) for i in (1, 2, 3)] == [2.0, 8.0, None]


def test_classify_answer_reads_the_2xx_body():
    """A 2xx body can say rate limited or filtered."""
    assert (
        ev.classify_answer(
            202, '{"accepted": true, "event_id": "x", "reason": "reasoning_queued"}'
        )
        == "ok"
    )
    assert ev.classify_answer(202, '{"accepted": true, "duplicate": true}') == "ok"
    assert ev.classify_answer(200, "") == "ok"
    assert ev.classify_answer(200, "not json") == "ok"
    assert ev.classify_answer(200, "[1]") == "ok"
    assert (
        ev.classify_answer(
            202, '{"accepted": false, "rate_limited": true, "reason": "rate_limited"}'
        )
        == "rate_limited"
    )
    assert ev.classify_answer(202, {"rate_limited": True}) == "rate_limited"
    assert (
        ev.classify_answer(202, '{"accepted": false, "reason": "store_error"}')
        == "rate_limited"
    )
    assert (
        ev.classify_answer(202, '{"accepted": false, "reason": "type_filtered"}')
        == "filtered"
    )
    # non-2xx keep the status verdict whatever the body says
    assert ev.classify_answer(429, '{"accepted": true}') == "retry"
    assert ev.classify_answer(401, '{"rate_limited": true}') == "drop"


def test_rate_limit_retry_is_single_and_delayed():
    """One retry, 60 s after a rate-limited answer."""
    assert ev.rate_limit_retry_delay(False) == 60.0 == ev.RATE_LIMIT_RETRY_S
    assert ev.rate_limit_retry_delay(True) is None


def ingest_result(**fields) -> dict:
    """Build the gateway's ``IngestResult.to_dict()`` (gateway/household/events.py)."""
    return {
        "accepted": False,
        "event_id": "e",
        "duplicate": False,
        "rate_limited": False,
        "queued_reasoning": False,
        "reason": None,
        **fields,
    }


def test_ingest_result_shape_matches_classify_answer():
    """The gateway's IngestResult bodies map to the verdicts the push expects."""
    rl = ingest_result(rate_limited=True, reason="rate_limited")
    assert ev.classify_answer(202, json.dumps(rl)) == "rate_limited"
    filt = ingest_result(reason="type_filtered")
    assert ev.classify_answer(202, json.dumps(filt)) == "filtered"
    ok = ingest_result(accepted=True, reason="reasoning_recorded")
    assert ev.classify_answer(202, json.dumps(ok)) == "ok"


def test_push_wiring_source():
    """push.py needs Home Assistant, so its use of the policy is checked by source."""
    src = (PATH.parent / "push.py").read_text(encoding="utf-8")
    assert "push_events.classify_answer(status, body)" in src
    assert '"rate_limited": self.rate_limited' in src
    assert "push_events.rate_limit_retry_delay(already_retried)" in src
    assert "push_events.retry_delay(attempt)" in src


def test_warn_throttle_once_per_minute():
    """At most one warning a minute."""
    t = ev.WarnThrottle(60)
    assert t.allow(0.0) == (True, 0)
    assert t.allow(10.0) == (False, 0)
    assert t.allow(59.0) == (False, 0)
    assert t.allow(60.0) == (True, 2)
    assert t.allow(61.0) == (False, 0)


def test_presence_tuple():
    """Presence entries are normalized."""
    assert ev.presence_tuple(None) is None
    assert ev.presence_tuple([" Person.Alex ", ""]) == ("person.alex",)
