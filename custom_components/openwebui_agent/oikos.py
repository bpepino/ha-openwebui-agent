"""``GET /api/oikos``: the Assist-exposed entities, read by the gateway.

Ported from the gateway's former Home Assistant component with the same
query parameters and response shape: the gateway's household reviews call
``?area=1&caps=1&assistant=conversation`` and read ``entities``; ``push`` is the
event push's status. Read-only; it needs a Home Assistant token.

Views cannot be unregistered, so the view is registered once per Home
Assistant instance and reads the current event pusher on every request:
reloading or removing the entry needs no new view.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web

from homeassistant.components.homeassistant.exposed_entities import (
    KNOWN_ASSISTANTS,
    async_should_expose,
)
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    service as ha_service,
)

from .const import DOMAIN, LOGGER

DATA_VIEW = f"{DOMAIN}_oikos_view"
# entry_id -> EventPusher of each loaded gateway entry (see push.py)
DATA_PUSHERS = f"{DOMAIN}_pushers"
DEFAULT_ASSISTANT = "conversation"

READ_ONLY_DOMAINS = {
    "sensor",
    "binary_sensor",
    "device_tracker",
    "person",
}

CAPS_KEYS = [
    "device_class",
    "unit_of_measurement",
    "state_class",
    "icon",
    "supported_features",
    # light
    "supported_color_modes",
    "color_mode",
    "brightness",
    "effect",
    "effect_list",
    "color_temp",
    "color_temp_kelvin",
    "min_color_temp_kelvin",
    "max_color_temp_kelvin",
    # fan
    "percentage",
    "percentage_step",
    "preset_mode",
    "preset_modes",
    "oscillating",
    # climate
    "hvac_mode",
    "hvac_modes",
    "temperature",
    "current_temperature",
    "target_temp_high",
    "target_temp_low",
    "target_temp_step",
    "min_temp",
    "max_temp",
    "fan_mode",
    "fan_modes",
    "swing_mode",
    "swing_modes",
    "humidity",
    "target_humidity",
    "min_humidity",
    "max_humidity",
    # cover
    "current_position",
    "current_tilt_position",
    # media_player info only
    "media_title",
    "media_artist",
    "media_album_name",
    "volume_level",
    "is_volume_muted",
    "source",
    "source_list",
]


@callback
def async_register_view(hass: HomeAssistant) -> None:
    """Register ``/api/oikos`` once per Home Assistant instance."""
    if hass.data.get(DATA_VIEW):
        return
    hass.http.register_view(OikosView(hass))
    hass.data[DATA_VIEW] = True
    LOGGER.debug("Exposed-entity view registered at %s", OikosView.url)


def push_status(hass: HomeAssistant) -> dict[str, Any]:
    """Return the first running event push's status, else a disabled block."""
    for pusher in hass.data.get(DATA_PUSHERS, {}).values():
        return pusher.status()
    return {
        "enabled": False,
        "sent": 0,
        "failed": 0,
        "rate_limited": 0,
        "filtered": 0,
        "last_error": None,
    }


def assistant_key(value: str | None) -> str | None:
    """Return the exposure key of an assistant; None when there is no such one.

    Home Assistant's Assist agent is ``conversation.home_assistant`` while its
    exposure is stored under ``conversation``. Unknown names are refused because
    Home Assistant records a default for every assistant it is asked about.
    """
    value = (value or "").strip() or DEFAULT_ASSISTANT
    if value == "conversation.home_assistant":
        value = DEFAULT_ASSISTANT
    return value if value in KNOWN_ASSISTANTS else None


@callback
def is_exposed(hass: HomeAssistant, entity_id: str, assistant: str) -> bool:
    """Home Assistant's own exposure decision (Settings → Voice assistants → Expose)."""
    return async_should_expose(hass, assistant, entity_id)


def _truthy_qs(request: web.Request, key: str, default: bool = False) -> bool:
    if key not in request.query:
        return default
    val = request.query.get(key)
    return val in (None, "", "1", "true", "yes", "on")


def _pick(attrs: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    return {k: attrs[k] for k in keys if k in attrs}


def _aliases(entry: er.RegistryEntry | None) -> list[str]:
    """Return the entity's own aliases; the computed-name marker is not one."""
    return [a for a in entry.aliases if isinstance(a, str)] if entry else []


def _has_any(attrs: dict[str, Any], keys: tuple[str, ...]) -> bool:
    return any(k in attrs for k in keys)


def infer_actions(domain: str, state: str, attrs: dict[str, Any]) -> list[str]:  # noqa: C901 - ported as is
    """Compact action hints for the planner.

    These are semantic hints, not guaranteed raw HA service names.
    Exact executable calls come from 'services'.
    """
    if state in ("unavailable", "unknown"):
        return ["unavailable"]

    if domain in READ_ONLY_DOMAINS:
        return ["read_state"]

    if not isinstance(attrs, dict):
        attrs = {}

    actions: list[str] = []

    # Light has its own branch below so its capability-specific hints are
    # reachable. These domains share the basic on/off/toggle set.
    if domain in ("switch", "fan", "input_boolean", "humidifier", "siren"):
        actions += ["turn_on", "turn_off", "toggle"]

    if domain == "light":
        actions += ["turn_on", "turn_off", "toggle"]
        if _has_any(attrs, ("brightness", "supported_color_modes")):
            actions.append("set_brightness")
        if _has_any(
            attrs,
            (
                "color_temp",
                "color_temp_kelvin",
                "min_color_temp_kelvin",
                "max_color_temp_kelvin",
            ),
        ):
            actions.append("set_color_temp")
        if _has_any(
            attrs,
            ("rgb_color", "hs_color", "xy_color", "rgbw_color", "rgbww_color"),
        ):
            actions.append("set_color")
        if "effect_list" in attrs:
            actions.append("set_effect")

    elif domain == "cover":
        actions += ["open_cover", "close_cover", "stop_cover"]
        if _has_any(attrs, ("current_position", "position")):
            actions.append("set_cover_position")
        if _has_any(attrs, ("current_tilt_position", "tilt_position")):
            actions.append("set_cover_tilt_position")

    elif domain == "fan":
        if _has_any(attrs, ("percentage", "speed")):
            actions.append("set_percentage")
        if "preset_modes" in attrs:
            actions.append("set_preset_mode")
        if "oscillating" in attrs:
            actions.append("oscillate")

    elif domain == "media_player":
        actions += ["turn_on", "turn_off", "play", "pause", "stop"]
        if "volume_level" in attrs:
            actions.append("set_volume")
        if "is_volume_muted" in attrs:
            actions.append("mute")
        if "source_list" in attrs:
            actions.append("select_source")

    elif domain == "lock":
        actions += ["lock", "unlock"]

    elif domain == "vacuum":
        actions += ["start", "stop", "return_to_base"]

    elif domain == "alarm_control_panel":
        actions += ["arm_home", "arm_away", "arm_night", "disarm"]

    elif domain == "climate":
        actions += ["turn_on", "turn_off"]
        actions.append("set_hvac_mode")
        if _has_any(
            attrs,
            (
                "temperature",
                "target_temp_high",
                "target_temp_low",
                "min_temp",
                "max_temp",
            ),
        ):
            actions.append("set_temperature")
        if "fan_modes" in attrs:
            actions.append("set_fan_mode")
        if "preset_modes" in attrs:
            actions.append("set_preset_mode")
        if "swing_modes" in attrs:
            actions.append("set_swing_mode")
        if _has_any(
            attrs,
            ("humidity", "target_humidity", "min_humidity", "max_humidity"),
        ):
            actions.append("set_humidity")

    return list(dict.fromkeys(actions))


def _target_includes_domain(target: Any, entity_domain: str) -> bool:
    """Handle common Home Assistant service target shapes."""
    if not isinstance(target, dict):
        return False

    ent = target.get("entity")
    if ent is None:
        return False

    def domains_from_obj(obj: Any) -> list[str]:
        if isinstance(obj, dict):
            d = obj.get("domain")
            if isinstance(d, str):
                return [d]
            if isinstance(d, list):
                return [x for x in d if isinstance(x, str)]
        return []

    if isinstance(ent, dict):
        return entity_domain in domains_from_obj(ent)

    if isinstance(ent, list):
        return any(entity_domain in domains_from_obj(item) for item in ent)

    return False


def _service_allowed_by_capabilities(
    entity_domain: str, service_name: str, attrs: dict[str, Any]
) -> bool:
    """Trim obviously irrelevant services to reduce LLM noise."""

    if entity_domain in READ_ONLY_DOMAINS:
        return False

    if entity_domain == "climate":
        if service_name == "set_fan_mode" and "fan_modes" not in attrs:
            return False
        if service_name == "set_preset_mode" and "preset_modes" not in attrs:
            return False
        if service_name == "set_swing_mode" and "swing_modes" not in attrs:
            return False
        if service_name == "set_humidity" and not _has_any(
            attrs, ("humidity", "target_humidity", "min_humidity", "max_humidity")
        ):
            return False

    if entity_domain == "fan":
        if service_name == "set_preset_mode" and "preset_modes" not in attrs:
            return False
        if service_name in ("increase_speed", "decrease_speed") and not _has_any(
            attrs, ("percentage", "speed")
        ):
            return False

    if entity_domain == "cover":
        if service_name in (
            "set_cover_tilt_position",
            "open_cover_tilt",
            "close_cover_tilt",
            "stop_cover_tilt",
            "toggle_cover_tilt",
        ) and not _has_any(attrs, ("current_tilt_position", "tilt_position")):
            return False
        if service_name == "set_cover_position" and not _has_any(
            attrs, ("current_position", "position")
        ):
            return False

    if entity_domain == "media_player":
        if service_name == "select_source" and "source_list" not in attrs:
            return False
        if service_name == "select_sound_mode" and "sound_mode_list" not in attrs:
            return False
        if service_name in ("volume_set", "volume_up", "volume_down") and not _has_any(
            attrs, ("volume_level", "is_volume_muted")
        ):
            return False

    return True


def _compact_service_fields(fields: Any) -> dict[str, Any]:
    """Keep only the useful bits when services_full=1."""
    out: dict[str, Any] = {}
    if not isinstance(fields, dict):
        return out

    for fname, fmeta in fields.items():
        if not isinstance(fname, str):
            continue
        item: dict[str, Any] = {}
        if isinstance(fmeta, dict):
            if "required" in fmeta:
                item["required"] = bool(fmeta.get("required"))
            selector = fmeta.get("selector")
            if isinstance(selector, dict):
                item["selector"] = selector
            if "example" in fmeta:
                item["example"] = fmeta.get("example")
            if "default" in fmeta:
                item["default"] = fmeta.get("default")
        out[fname] = item

    return out


class OikosView(HomeAssistantView):
    """The entities exposed to Assist, in the shape the gateway reads."""

    url = "/api/oikos"
    name = "api:openwebui_agent:oikos"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        """Keep Home Assistant; the event pusher is looked up per request."""
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:  # noqa: C901 - ported as is
        """Return the exposed entities.

        Query params:
          - assistant=<name>   default: conversation
          - all=1             include non-exposed too (debug)
          - slim=1            minimal identity index: entity_id, name,
                              friendly_name, aliases, domain, area_name,
                              device_class. No state/caps/actions/services.
          - caps=1            include compact capabilities (default on)
          - attrs=1           include full attrs instead of compact caps
          - actions=1         include action hints (default on)
          - services=1        include executable service names (default on)
          - services_full=1   include service descriptions + minimal fields
          - area=1            include area_name (entity area, falling back
                              to the device's area)
          - debug_expose=1    include each entity's exposure decision
        """
        assistant = assistant_key(request.query.get("assistant"))
        if assistant is None:
            return self.json_message(
                f"Unknown assistant; use one of {', '.join(KNOWN_ASSISTANTS)}", 400
            )

        include_all = _truthy_qs(request, "all", default=False)
        slim = _truthy_qs(request, "slim", default=False)
        include_attrs = _truthy_qs(request, "attrs", default=False)
        include_caps = _truthy_qs(request, "caps", default=not include_attrs)
        include_actions = _truthy_qs(request, "actions", default=True)
        include_services_full = _truthy_qs(request, "services_full", default=False)
        include_services = include_services_full or _truthy_qs(
            request, "services", default=True
        )
        include_area = _truthy_qs(request, "area", default=False)
        debug_expose = _truthy_qs(request, "debug_expose", default=False)

        if slim:
            include_attrs = False
            include_caps = False
            include_actions = False
            include_services = False
            include_services_full = False

        hass = self._hass
        ent_reg = er.async_get(hass)
        area_reg = ar.async_get(hass) if include_area else None
        dev_reg = dr.async_get(hass) if include_area else None

        service_desc: dict[str, dict[str, Any]] = {}
        if include_services:
            service_desc = await ha_service.async_get_all_descriptions(hass)

        def area_name(area_id: str) -> str:
            if not include_area or not area_reg or not area_id:
                return ""
            area_entry = area_reg.async_get_area(area_id)
            return area_entry.name if area_entry else ""

        def entity_area_id(entry: er.RegistryEntry | None) -> str:
            """Return the entity's area, else its device's area.

            Most entities inherit their area from the device in Home Assistant.
            """
            if not entry:
                return ""
            if entry.area_id:
                return entry.area_id
            if dev_reg and entry.device_id:
                device = dev_reg.async_get(entry.device_id)
                if device and device.area_id:
                    return device.area_id
            return ""

        # The catalog scan depends only on the entity's domain, so do it once
        # per domain instead of once per entity.
        domain_candidates: dict[str, list[tuple[str, str]]] = {}

        def candidates_for(entity_domain: str) -> list[tuple[str, str]]:
            cached = domain_candidates.get(entity_domain)
            if cached is not None:
                return cached
            matches: set[tuple[str, str]] = set()
            for s_domain, s_services in service_desc.items():
                if not isinstance(s_services, dict):
                    continue
                for s_name, desc in s_services.items():
                    if not isinstance(desc, dict):
                        continue
                    if _target_includes_domain(desc.get("target"), entity_domain):
                        matches.add((s_domain, s_name))
            if entity_domain in service_desc:
                for s_name in service_desc[entity_domain]:
                    matches.add((entity_domain, s_name))
            result = sorted(matches)
            domain_candidates[entity_domain] = result
            return result

        def services_for_entity(entity_domain: str, attrs: dict[str, Any]) -> list[Any]:
            if not include_services or entity_domain in READ_ONLY_DOMAINS:
                return []

            out: list[Any] = []
            for d, n in candidates_for(entity_domain):
                if not _service_allowed_by_capabilities(entity_domain, n, attrs):
                    continue
                key = f"{d}.{n}"
                if include_services_full:
                    desc = (
                        service_desc.get(d, {}).get(n, {})
                        if isinstance(service_desc.get(d), dict)
                        else {}
                    )
                    out.append(
                        {
                            "service": key,
                            "name": desc.get("name", ""),
                            "fields": _compact_service_fields(desc.get("fields", {})),
                        }
                    )
                else:
                    out.append(key)

            return out

        entities_out: list[dict[str, Any]] = []

        for st in hass.states.async_all():
            entity_id = st.entity_id
            entry = ent_reg.async_get(entity_id)

            exposed = is_exposed(hass, entity_id, assistant)
            if not include_all and not exposed:
                continue

            domain = entity_id.split(".", 1)[0]
            attrs = dict(st.attributes)

            name = (
                (entry.name if entry and entry.name else None)
                or (entry.original_name if entry and entry.original_name else None)
                or attrs.get("friendly_name")
                or entity_id
            )
            friendly = attrs.get("friendly_name")

            if slim:
                item: dict[str, Any] = {
                    "entity_id": entity_id,
                    "name": name,
                    "aliases": _aliases(entry),
                    "domain": domain,
                }
                if isinstance(friendly, str) and friendly and friendly != name:
                    item["friendly_name"] = friendly
                if include_area and (an := area_name(entity_area_id(entry))):
                    item["area_name"] = an
                device_class = attrs.get("device_class")
                if isinstance(device_class, str) and device_class:
                    item["device_class"] = device_class
                if include_all or debug_expose:
                    item["exposed"] = exposed
                entities_out.append(item)
                continue

            item = {
                "entity_id": entity_id,
                "name": name,
                "aliases": _aliases(entry),
                "domain": domain,
                "state": st.state,
                "available": st.state not in ("unavailable", "unknown"),
            }
            if isinstance(friendly, str) and friendly and friendly != name:
                item["friendly_name"] = friendly

            if include_area and (area_id := entity_area_id(entry)):
                item["area_name"] = area_name(area_id)

            if include_caps:
                item["capabilities"] = _pick(attrs, CAPS_KEYS)

            if include_attrs:
                item["attributes"] = attrs

            if include_actions:
                item["actions"] = infer_actions(domain, st.state, attrs)

            if include_services:
                item["services"] = services_for_entity(domain, attrs)

            if include_all or debug_expose:
                item["exposed"] = exposed

            entities_out.append(item)

        return self.json(
            {
                "assistant": assistant,
                "count": len(entities_out),
                "entities": entities_out,
                "push": push_status(hass),
            }
        )
