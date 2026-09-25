from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from student_agent.llm_agents import LLMAgents
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case
from test_workflow import FakeGateway, base_case, contracts, fake_evidence  # noqa: F401

ORDER = "af0bbb47f125381ce9f3597dc70ef07b"

class FakeResponse:
    def __init__(self, message: dict[str, Any]) -> None:
        self._message = message

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return {"choices": [{"message": self._message}]}

class FakeClient:
    """Scripted OpenAI: per role, a list of replies (tool calls or final JSON)."""

    def __init__(self, script: dict[str, list[Any]]) -> None:
        self.script = {role: list(replies) for role, replies in script.items()}
        self.bodies: list[dict[str, Any]] = []

    async def post(self, url: str, headers: dict[str, str], json: dict[str, Any]) -> FakeResponse:
        self.bodies.append(json)
        system = json["messages"][0]["content"]
        role = next(r for r in self.script if f"the {r.split('-')[0]}" in system)
        reply = self.script[role].pop(0) if self.script[role] else {"issue": None}
        if isinstance(reply, list):  # tool calls
            calls = [
                {"id": f"c{i}", "function": {"name": n, "arguments": _json(a)}}
                for i, (n, a) in enumerate(reply)
            ]
            return FakeResponse({"content": None, "tool_calls": calls})
        return FakeResponse({"content": _json(reply)})

def _json(value: Any) -> str:
    return json.dumps(value)

def _run(tmp_path: Path, contracts, case, evidence, script):  # noqa: F811
    trace_path = tmp_path / "trace.jsonl"
    gateway = FakeGateway(evidence)
    client = FakeClient(script)
    agents = LLMAgents("sk-test", "gpt-4o-mini", client=client)
    output = asyncio.run(solve_case(case, gateway, TraceWriter(trace_path, contracts), agents))
    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    return output, gateway, client, events

def _script(coordinator: list[Any]) -> dict[str, list[Any]]:
    return {
        "entity-agent": [
            [("get_customer_history", {"customer_unique_id": "evil"})],
            [("get_order", {"order_id": "candidate-001"}), ("get_order", {"order_id": ORDER})],
            {"resolved_order_id": ORDER, "confidence": 0.9},
        ],
        "order-agent": [[("get_order_items", {"order_id": "x"})], {"issue": None}],
        "shipment-agent": [{"issue": "late_delivery_logistics", "confidence": 0.8}],
        "payment-agent": [{"issue": None}],
        "policy-agent": [[("get_policy", {})], {"issue": None}],
        "coordinator": coordinator,
    }

def test_llm_agreement(tmp_path, contracts, base_case, fake_evidence) -> None:  # noqa: F811
    output, gateway, client, events = _run(
        tmp_path, contracts, base_case, fake_evidence,
        _script([{"primary_issue": "late_delivery_logistics", "confidence": 0.9}]),
    )
    assert output["assessment"]["primary_issue"] == "late_delivery_logistics"
    assert output["assessment"]["confidence"] == 0.96  # agreed with verifier + 1 specialist vote
    # guardrails: forced identity, no candidate ids, pinned order, no cross-role tools
    for name, args in gateway.calls:
        assert args["case_id"] == base_case["case_id"]
        if name == "get_customer_history":
            assert args["customer_unique_id"] == base_case["customer_unique_id_hint"]
        if "order_id" in args:
            assert args["order_id"] == ORDER
    names = [n for n, _ in gateway.calls]
    assert len(names) == len(set(names)) <= 9
    assert names.count("get_policy") == 1
    # case_id never offered to the LLM
    for body in client.bodies:
        for tool in body.get("tools", []):
            assert "case_id" not in tool["function"]["parameters"]["properties"]
    refs = set(output["evidence_refs"])
    consumed = {r for e in events for r in e.get("evidence_refs", [])}
    assert refs <= consumed
    assert len({e["actor"] for e in events}) >= 5
    verdicts = [e["decision_code"] for e in events if e["event_type"] == "verification_completed"]
    assert verdicts == ["PASS"]
    # one LLM call per specialist (3, run in parallel) + coordinator; no LLM tool loops
    assert len(client.bodies) == 4
    assert all("tools" not in body for body in client.bodies)
    assert "get_product_context" not in names

def test_persistent_disagreement_fails_closed(tmp_path, contracts, base_case, fake_evidence) -> None:  # noqa: F811,E501
    output, _, _, events = _run(
        tmp_path, contracts, base_case, fake_evidence,
        _script([{"primary_issue": "duplicate_charge"}, {"primary_issue": "duplicate_charge"}]),
    )
    # an LLM issue the verifier rejects twice never drives the refund
    assert output["assessment"]["primary_issue"] == "late_delivery_logistics"
    assert output["assessment"]["confidence"] == 0.55
    verdicts = [e["decision_code"] for e in events if e["event_type"] == "verification_completed"]
    assert verdicts == ["ISSUE_MISMATCH", "PASS_RULES_OVER_LLM"]
    escalations = [e for e in events if e.get("decision_code") == "ESCALATED_TO_VERIFIER"]
    assert escalations[0]["attributes"] == {"llm_issue": "duplicate_charge"}


def test_specialist_out_of_domain_issue_dropped() -> None:
    from student_agent.llm_agents import _normalise

    assert _normalise({"issue": "payment_mismatch"}, "shipment-agent")["issue"] is None
    assert _normalise({"issue": "late_delivery_seller"}, "shipment-agent")["issue"] == (
        "late_delivery_seller"
    )
    assert _normalise({"primary_issue": "payment_mismatch"}, "coordinator")["primary_issue"] == (
        "payment_mismatch"
    )

def test_llm_revision_accepted(tmp_path, contracts, base_case, fake_evidence) -> None:  # noqa: F811
    output, _, _, _ = _run(
        tmp_path, contracts, base_case, fake_evidence,
        _script(
            [{"primary_issue": "duplicate_charge"}, {"primary_issue": "late_delivery_logistics"}]
        ),
    )
    assert output["assessment"]["primary_issue"] == "late_delivery_logistics"

def test_llm_down_gives_insufficient(tmp_path, contracts, base_case, fake_evidence) -> None:  # noqa: F811
    class Broken:
        async def post(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("openai down")

    trace_path = tmp_path / "trace.jsonl"
    gateway = FakeGateway(fake_evidence)
    agents = LLMAgents("sk-test", "gpt-4o-mini", client=Broken())
    output = asyncio.run(
        solve_case(base_case, gateway, TraceWriter(trace_path, contracts), agents)
    )
    # no LLM decision -> never silently use the rules' answer
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert "get_shipment_summary" in [n for n, _ in gateway.calls]


def test_finding_contradicting_facts_is_dropped() -> None:
    from student_agent.workflow import _GROUNDING

    on_time = {"delivered_after_estimate": False, "confirmed_delivered_late_by_logistics": False}
    assert not _GROUNDING["late_delivery_logistics"](on_time)
    assert _GROUNDING["late_delivery_logistics"]({**on_time, "delivered_after_estimate": True})
    assert not _GROUNDING["payment_mismatch"]({"reconciliation_mismatch_event": False})


def test_gateway_retries_transient_tool_error(monkeypatch) -> None:
    from types import SimpleNamespace

    from student_agent import mcp_gateway

    envelope = {"evidence_ref": "ev_x", "data": {}}
    replies = [
        SimpleNamespace(is_error=True, content=[SimpleNamespace(text="Error executing tool")]),
        SimpleNamespace(is_error=False, content=[], structured_content=envelope),
    ]

    class Session:
        calls = 0

        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            Session.calls += 1
            return replies.pop(0)

    async def no_sleep(_: float) -> None:
        pass

    monkeypatch.setattr(mcp_gateway.asyncio, "sleep", no_sleep)
    no_schema = SimpleNamespace(validate_evidence=lambda *a: None)
    gateway = mcp_gateway.EvidenceGateway(Session(), no_schema)  # type: ignore[arg-type]
    assert asyncio.run(gateway.call("get_policy", case_id="C1", policy_version="v")) == envelope
    assert Session.calls == 2


def test_coordinator_cannot_invent_unreported_issue(tmp_path, contracts, base_case, fake_evidence) -> None:  # noqa: F811,E501
    # no specialist reported duplicate_charge: the coordinator's pick is discarded, and on
    # revision it may only take the verifier's issue or unsupported/insufficient
    output, _, client, events = _run(
        tmp_path, contracts, base_case, fake_evidence,
        _script(
            [{"primary_issue": "duplicate_charge"}, {"primary_issue": "late_delivery_logistics"}]
        ),
    )
    assert output["assessment"]["primary_issue"] == "late_delivery_logistics"
    decided = [e for e in events if e["event_type"] == "policy_decided"]
    assert decided[0]["decision_code"] == "insufficient_evidence"
    assert decided[0]["attributes"] == {"rejected_issue": "duplicate_charge"}
    coordinator_bodies = [b for b in client.bodies if "coordinator" in b["messages"][0]["content"]]
    first = json.loads(coordinator_bodies[0]["messages"][1]["content"])
    assert first["allowed_issues"] == [
        "insufficient_evidence", "late_delivery_logistics", "unsupported_claim",
    ]


def test_claim_refs_cite_only_deciding_domains(tmp_path, contracts, base_case, fake_evidence) -> None:  # noqa: F811,E501
    output, _, _, _ = _run(
        tmp_path, contracts, base_case, fake_evidence,
        _script([{"primary_issue": "late_delivery_logistics", "confidence": 0.9}]),
    )
    claims = {c["claim_id"]: c["evidence_refs"] for c in output["claim_assessments"]}
    assert claims["claim-001-a"] == [
        "ev_order_decoy_012345678901234", "ev_ship_012345678901234567890",
        "ev_pol_0123456789012345678901",
    ]
    assert claims["claim-001-b"] == [
        "ev_pay_0123456789012345678901", "ev_pol_0123456789012345678901",
    ]
    assert "ev_product_0123456789012345678" not in output["evidence_refs"]


def test_unsupported_claim_fetches_product_context(tmp_path, contracts, base_case, fake_evidence) -> None:  # noqa: F811,E501
    base_case["customer_request"]["claims"][0]["topic"] = "unsupported_claim"
    on_time = "2017-12-10T10:00:00-03:00"  # before the 12-20 estimate: nothing is late
    fake_evidence["get_customer_history"]["data"]["orders"][0]["order_delivered_customer_date"] = (
        on_time
    )
    fake_evidence["get_order"]["data"]["order_delivered_customer_date"] = on_time
    fake_evidence["get_shipment_summary"]["data"]["events"] = []
    output, gateway, _, _ = _run(
        tmp_path, contracts, base_case, fake_evidence,
        _script([{"primary_issue": "unsupported_claim", "confidence": 0.9}]),
    )
    assert output["assessment"]["primary_issue"] == "unsupported_claim"
    assert [n for n, _ in gateway.calls].count("get_product_context") == 1
    assert "ev_product_0123456789012345678" in output["evidence_refs"]

