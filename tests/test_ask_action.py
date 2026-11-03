"""``openwebui_agent.ask_action``: the fields are checked and the body forwarded to the gateway.

The gateway's contract (``gateway/household/ask_action.py``): ``POST /household/v1/ask_action`` with the
gateway token, body ``{question, steps: [{service: domain.service, entity_ids, data}], satellite?, area?,
context?, integration_version, request_id?}``, answer ``{status: asked | skipped | refused, reason, ...}``.
"""

import json

import httpx
import pytest
import respx

from owui_protocol import ask_action as aa
from owui_protocol.exceptions import InvalidRequestError
from owui_protocol.gateway_client import GatewayClient

URL = "http://gateway.test:8000"
TOKEN = "fake-gateway-token"
CALL = {
    "question": "water_heater_long",
    "satellite": "assist_satellite.kitchen",
    "steps": [
        {"service": "turn_off", "entity_id": ["switch.boiler", "water_heater.tank"]}
    ],
    "context": {"hours": 2, "why": "on too long"},
}


def test_the_fields_become_the_gateways_body():
    """A bare service is split per domain; optional fields are left out when empty."""
    body = aa.build_request(CALL, version="3.3.0", request_id="01JABC")
    assert body == {
        "question": "water_heater_long",
        "steps": [
            {"service": "switch.turn_off", "entity_ids": ["switch.boiler"], "data": {}},
            {
                "service": "water_heater.turn_off",
                "entity_ids": ["water_heater.tank"],
                "data": {},
            },
        ],
        "integration_version": "3.3.0",
        "satellite": "assist_satellite.kitchen",
        "context": {"hours": 2, "why": "on too long"},
        "request_id": "01JABC",
    }
    one = aa.build_request(
        {
            "question": "bedtime_sweep",
            "steps": [{"service": "light.turn_off", "entity_id": "light.a, light.b"}],
        },
        version="3.3.0",
    )
    assert one["steps"] == [
        {"service": "light.turn_off", "entity_ids": ["light.a", "light.b"], "data": {}}
    ]
    assert "satellite" not in one and "context" not in one


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"question": "Bedtime Sweep!"}, "question"),
        ({"steps": []}, "at least one step"),
        (
            {"steps": [{"service": "light.turn_off", "entity_id": ["switch.boiler"]}]},
            "not a light entity",
        ),
        (
            {"steps": [{"service": "{{ svc }}", "entity_id": ["switch.boiler"]}]},
            "domain.service",
        ),
        (
            {"steps": [{"service": "turn_off", "entity_id": ["{{ x }}"]}]},
            "not a plain entity id",
        ),
        (
            {"steps": [{"service": "turn_off", "entity_id": "switch.a", "target": {}}]},
            "unknown key",
        ),
        (
            {
                "steps": [
                    {"service": "turn_off", "entity_id": "switch.a", "data": {"x": [1]}}
                ]
            },
            "plain values",
        ),
        (
            {
                "steps": [
                    {
                        "service": "turn_off",
                        "entity_id": [f"switch.s{i}" for i in range(41)],
                    }
                ]
            },
            "at most 40",
        ),
        ({"satellite": "media_player.kitchen"}, "assist_satellite"),
        (
            {"context": {"why": "{{ states('sensor.x') }}"}},
            "templates are not accepted",
        ),
    ],
)
def test_bad_fields_are_refused_before_anything_is_sent(change, why):
    """Every field outside the contract stops the call in Home Assistant."""
    with pytest.raises(aa.AskActionInvalid, match=why):
        aa.build_request({**CALL, **change}, version="3.3.0")


async def test_the_client_forwards_with_the_token_and_returns_the_decision():
    """The body goes to the gateway with the gateway token; only a refusal is an error."""
    seen = []

    def answer(request):
        seen.append((request.headers.get("Authorization"), json.loads(request.content)))
        return httpx.Response(
            200, json={"status": "asked", "pending_id": "pend-1", "level": 2}
        )

    with respx.mock(base_url=URL) as mock:
        mock.post("/household/v1/ask_action").side_effect = answer
        async with httpx.AsyncClient() as http:
            client = GatewayClient(URL, TOKEN, http, timeout=5)
            out = await client.async_ask_action(aa.build_request(CALL, version="3.3.0"))
    assert out["status"] == "asked" and aa.outcome_error(out) is None
    assert (
        seen[0][0] == f"Bearer {TOKEN}"
        and seen[0][1]["question"] == "water_heater_long"
    )
    assert (
        aa.outcome_error({"status": "refused", "reason": "not_exposed"})
        == "not_exposed"
    )
    assert aa.outcome_error({"status": "skipped", "reason": "quiet_hours"}) is None


async def test_a_body_the_gateway_rejects_is_an_error():
    """A 400 envelope maps to the invalid-request error."""
    with respx.mock(base_url=URL) as mock:
        mock.post("/household/v1/ask_action").respond(
            400,
            json={
                "error": {
                    "code": "invalid_request",
                    "message": "ask_action validation failed",
                }
            },
        )
        async with httpx.AsyncClient() as http:
            client = GatewayClient(URL, None, http, timeout=5)
            with pytest.raises(InvalidRequestError):
                await client.async_ask_action({"question": "x", "steps": []})
