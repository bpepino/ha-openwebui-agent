"""``openwebui_agent.ask_action``: the pure part (no Home Assistant imports).

A question recipe (the gateway store's bedtime sweep, window open while heating or AC runs, water heater on too
long) proposes a plan; Home Assistant never runs it. The service checks the fields, builds the gateway's body and
``__init__`` sends it to ``POST /household/v1/ask_action`` with the gateway token (the same API client and link
as the assistant turns). The gateway checks the plan like any other (levels, budgets, undo), asks on the
satellite with ``assist_satellite.ask_question`` and runs it only on a yes.

Service fields:

* ``question`` (required): a short id for the audit, ``bedtime_sweep`` (lowercase letters, digits, ``_``);
* ``steps`` (required, 1 to 12): ``{service, entity_id, data?}``. ``service`` is ``domain.service``, or a bare
  ``service`` (``turn_off``) that applies to each entity's own domain; ``entity_id`` one id or a list (at most
  40 in all); ``data`` optional plain values;
* ``satellite`` (optional): the ``assist_satellite`` entity that asks; ``area`` (optional): an area id;
* ``context`` (optional): up to 8 plain values kept in the gateway's audit, never spoken.

Nothing that still looks like a template (``{{``, ``{%``, ``{#``) is sent: Home Assistant renders a template
before the service runs, so one left over is a mistake.
"""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

ASK_ACTION_PATH = "/household/v1/ask_action"
SERVICE_ASK_ACTION = "ask_action"
MAX_STEPS = 12
MAX_ENTITIES = 40
MAX_DATA_KEYS = 10
MAX_CONTEXT_KEYS = 8
MAX_STRING = 200

WORD_RE = re.compile(r"^[a-z0-9_]{1,64}$")
SERVICE_RE = re.compile(r"^(?:([a-z0-9_]{1,64})\.)?([a-z0-9_]{1,64})$")
ENTITY_RE = re.compile(r"^([a-z0-9_]{1,64})\.[a-z0-9_]{1,128}$")
SATELLITE_RE = re.compile(r"^assist_satellite\.[a-z0-9_]{1,128}$")
TEMPLATE_RE = re.compile(r"\{\{|\{%|\{#")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class AskActionInvalid(ValueError):
    """A field is not what the gateway's contract accepts (the message says which)."""


def _scalar(value: Any, where: str) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        if value != value or value in (float("inf"), float("-inf")):
            raise AskActionInvalid(f"{where}: not a finite number")
        return value
    if isinstance(value, str):
        if TEMPLATE_RE.search(value):
            raise AskActionInvalid(f"{where}: templates are not accepted")
        if CONTROL_RE.search(value):
            raise AskActionInvalid(f"{where}: control characters are not accepted")
        if len(value) > MAX_STRING:
            raise AskActionInvalid(f"{where}: at most {MAX_STRING} characters")
        return value
    raise AskActionInvalid(f"{where}: only plain values (text, numbers, true/false)")


def _plain_map(value: Any, where: str, limit: int) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise AskActionInvalid(f"{where}: must be a mapping")
    if len(value) > limit:
        raise AskActionInvalid(f"{where}: at most {limit} keys")
    out: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not WORD_RE.match(key):
            raise AskActionInvalid(f"{where}: {str(key)[:40]!r} is not a plain key")
        item = _scalar(item, f"{where}.{key}")
        if item is not None:
            out[key] = item
    return out


def _entity_ids(value: Any, where: str) -> list[str]:
    if isinstance(value, str):
        value = [v.strip() for v in value.split(",")]
    if not isinstance(value, (list, tuple)) or not value:
        raise AskActionInvalid(f"{where}: one entity id or a list of them")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not ENTITY_RE.match(item.strip()):
            raise AskActionInvalid(
                f"{where}: {str(item)[:60]!r} is not a plain entity id"
            )
        if item.strip() not in out:
            out.append(item.strip())
    return out


def build_steps(steps: Any) -> list[dict[str, Any]]:
    """Turn the service's ``steps`` into ``[{service: domain.service, entity_ids, data}]``."""
    if not isinstance(steps, (list, tuple)) or not steps:
        raise AskActionInvalid("steps: at least one step")
    out: list[dict[str, Any]] = []
    total = 0
    for i, step in enumerate(steps):
        where = f"steps[{i}]"
        if not isinstance(step, Mapping):
            raise AskActionInvalid(f"{where}: must be a mapping")
        unknown = set(step) - {"service", "action", "entity_id", "data"}
        if unknown:
            raise AskActionInvalid(f"{where}: unknown key {sorted(unknown)[0]!r}")
        service = step.get("service", step.get("action"))
        match = SERVICE_RE.match(service) if isinstance(service, str) else None
        if match is None:
            raise AskActionInvalid(
                f"{where}.service: a plain domain.service (or a service like turn_off)"
            )
        domain, name = match.group(1), match.group(2)
        ids = _entity_ids(step.get("entity_id"), f"{where}.entity_id")
        data = _plain_map(step.get("data"), f"{where}.data", MAX_DATA_KEYS)
        total += len(ids)
        by_domain: dict[str, list[str]] = {}
        for eid in ids:
            own = eid.split(".", 1)[0]
            if domain and own != domain:
                raise AskActionInvalid(f"{where}: {eid} is not a {domain} entity")
            by_domain.setdefault(own, []).append(eid)
        for own, group in by_domain.items():
            out.append(
                {"service": f"{own}.{name}", "entity_ids": group, "data": dict(data)}
            )
    if total > MAX_ENTITIES:
        raise AskActionInvalid(f"steps: at most {MAX_ENTITIES} entities in all")
    if len(out) > MAX_STEPS:
        raise AskActionInvalid(f"steps: at most {MAX_STEPS} steps")
    return out


def build_request(
    data: Mapping[str, Any], *, version: str, request_id: str | None = None
) -> dict[str, Any]:
    """Build the gateway's ``ask_action`` body; raise ``AskActionInvalid`` for a bad field."""
    question = data.get("question")
    if not isinstance(question, str) or not WORD_RE.match(question.strip()):
        raise AskActionInvalid("question: a short id (lowercase letters, digits and _)")
    body: dict[str, Any] = {
        "question": question.strip(),
        "steps": build_steps(data.get("steps")),
        "integration_version": version,
    }
    satellite = data.get("satellite")
    if satellite not in (None, ""):
        if not isinstance(satellite, str) or not SATELLITE_RE.match(satellite.strip()):
            raise AskActionInvalid("satellite: an assist_satellite entity")
        body["satellite"] = satellite.strip()
    area = data.get("area")
    if area not in (None, ""):
        if not isinstance(area, str) or not WORD_RE.match(area.strip()):
            raise AskActionInvalid("area: an area id")
        body["area"] = area.strip()
    context = _plain_map(data.get("context"), "context", MAX_CONTEXT_KEYS)
    if context:
        body["context"] = context
    if request_id:
        body["request_id"] = request_id[:128]
    return body


def outcome_error(answer: Any) -> str | None:
    """Return the reason of a ``refused`` gateway answer, else None.

    ``asked`` and ``skipped`` (quiet hours, nobody home, busy, ...) are normal
    answers; a refusal is raised in the automation.
    """
    if isinstance(answer, Mapping) and answer.get("status") == "refused":
        return str(answer.get("reason") or "refused")[:120]
    return None
