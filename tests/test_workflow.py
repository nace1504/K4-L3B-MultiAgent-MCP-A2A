from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case


class FakeGateway:
    def __init__(self, responses: dict[str, dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses = responses or {}

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, {"case_id": case_id, **arguments}))
        if tool_name in self.responses:
            resp = self.responses[tool_name]
            if isinstance(resp, Exception):
                raise resp
            return resp
        raise RuntimeError(f"Unexpected tool call: {tool_name}")


@pytest.fixture
def contracts() -> Contracts:
    root = Path(__file__).resolve().parents[1]
    return Contracts(root / "contracts" / "schemas")


@pytest.fixture
def base_case() -> dict[str, Any]:
    return {
        "case_id": "L3B_CASE_001",
        "opened_at": "2018-01-01T09:00:00-03:00",
        "customer_request": {
            "language": "vi",
            "message": "Điều tra đơn hàng.",
            "claimed_order_id": "af0bbb47f125381ce9f3597dc70ef07b",
            "claims": [
                {"claim_id": "claim-001-a", "topic": "late_delivery_logistics"},
                {"claim_id": "claim-001-b", "topic": "requested_full_refund"},
            ],
        },
        "policy_version": "EC_POLICY_V2",
        "candidate_order_ids": [
            "af0bbb47f125381ce9f3597dc70ef07b",
            "candidate-001",
        ],
        "investigation_scope": {
            "include_customer_history": True,
            "include_product_context": True,
            "require_independent_verification": True,
        },
        "customer_unique_id_hint": "customer-597dc70ef07b",
    }


@pytest.fixture
def fake_evidence() -> dict[str, dict[str, Any]]:
    return {
        "get_customer_history": {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": "ev_cust_hist_01234567890123456",
            "result_hash": f"sha256:{'a' * 64}",
            "domain": "customer",
            "data": {
                "orders": [
                    {
                        "order_id": "af0bbb47f125381ce9f3597dc70ef07b",
                        "order_status": "delivered",
                        "order_purchase_timestamp": "2017-12-01T10:00:00-03:00",
                        "order_delivered_carrier_date": "2017-12-03T10:00:00-03:00",
                        "order_delivered_customer_date": "2018-01-04T10:00:00-03:00",
                        "order_estimated_delivery_date": "2017-12-20T10:00:00-03:00",
                    }
                ]
            },
        },
        "get_order": {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": "ev_order_decoy_012345678901234",
            "result_hash": f"sha256:{'b' * 64}",
            "domain": "order",
            "data": {
                "order_id": "af0bbb47f125381ce9f3597dc70ef07b",
                "order_status": "delivered",
                "order_purchase_timestamp": "2017-12-01T10:00:00-03:00",
                "order_delivered_carrier_date": "2017-12-03T10:00:00-03:00",
                "order_delivered_customer_date": "2018-01-04T10:00:00-03:00",
                "order_estimated_delivery_date": "2017-12-20T10:00:00-03:00",
            },
        },
        "get_order_items": {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": "ev_items_01234567890123456789",
            "result_hash": f"sha256:{'c' * 64}",
            "domain": "item",
            "data": [
                {
                    "order_item_id": "item-001",
                    "seller_id": "seller-001",
                    "shipping_limit_date": "2017-12-05T10:00:00-03:00",
                    "price": "16.00",
                    "freight_value": "0.00",
                }
            ],
        },
        "get_product_context": {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": "ev_product_0123456789012345678",
            "result_hash": f"sha256:{'d' * 64}",
            "domain": "product",
            "data": {"product_category_name": "eletronicos"},
        },
        "get_shipment_summary": {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": "ev_ship_012345678901234567890",
            "result_hash": f"sha256:{'e' * 64}",
            "domain": "shipment",
            "data": {
                "delivered_carrier_at": "2017-12-03T10:00:00-03:00",
                "delivered_customer_at": "2018-01-04T10:00:00-03:00",
                "estimated_delivery_at": "2017-12-20T10:00:00-03:00",
                "events": [
                    {
                        "event_at": "2018-01-04T10:00:00-03:00",
                        "event_type": "delivered_late",
                        "actor": "logistics",
                        "status": "confirmed",
                    }
                ],
            },
        },
        "get_payment_timeline": {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": "ev_pay_0123456789012345678901",
            "result_hash": f"sha256:{'f' * 64}",
            "domain": "payment",
            "data": {
                "payments": [
                    {
                        "payment_sequential": 1,
                        "payment_type": "credit_card",
                        "payment_value": "16.00",
                    }
                ],
                "events": [
                    {
                        "event_at": "2017-12-01T10:05:00-03:00",
                        "event_type": "capture",
                        "amount_brl": "16.00",
                        "status": "confirmed",
                    }
                ],
            },
        },
        "get_policy": {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": "ev_pol_0123456789012345678901",
            "result_hash": f"sha256:{'0' * 64}",
            "domain": "policy",
            "data": {
                "rules": {
                    "late_delivery_logistics": {
                        "case_status": "action_required",
                        "recommended_action": "refund_shipping_or_order",
                        "refund_brl": "16.00",
                        "responsible_parties": [
                            {"party_type": "logistics_provider", "party_id": "logistics"}
                        ],
                    }
                }
            },
        },
    }


def test_solve_case_happy_path(
    tmp_path: Path,
    contracts: Contracts,
    base_case: dict[str, Any],
    fake_evidence: dict[str, dict[str, Any]],
) -> None:
    trace_path = tmp_path / "trace.jsonl"
    trace = TraceWriter(trace_path, contracts)
    gateway = FakeGateway(fake_evidence)

    output = asyncio.run(solve_case(base_case, gateway, trace))  # type: ignore[arg-type]

    contracts.validate_output(output, "L3B_CASE_001")
    assert output["assessment"]["primary_issue"] == "late_delivery_logistics"
    assert output["assessment"]["case_status"] == "action_required"

    # Tool calls check: max 8, never get_sellers or get_order_payments
    called_tools = [name for name, _ in gateway.calls]
    assert len(called_tools) <= 8
    assert "get_sellers" not in called_tools
    assert "get_order_payments" not in called_tools
    # get_refund_timeline not called: no refund claim, not canceled, no refund events
    assert "get_refund_timeline" not in called_tools

    # Read and inspect trace events
    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    event_types = [e["event_type"] for e in events]

    # Required sequence events
    assert "task_assigned" in event_types
    assert "tool_result_consumed" in event_types
    assert "handoff" in event_types
    assert "policy_decided" in event_types
    assert "verification_completed" in event_types

    # Verifier completed PASS
    verif_events = [e for e in events if e["event_type"] == "verification_completed"]
    assert [e["decision_code"] for e in verif_events] == ["ISSUE_MISMATCH", "PASS"]

    # Policy decided code matches primary_issue
    policy_events = [e for e in events if e["event_type"] == "policy_decided"]
    assert policy_events[-1]["decision_code"] == "late_delivery_logistics"

    # Entity resolved handoff
    entity_handoff = [
        e for e in events if e["event_type"] == "handoff" and e["actor"] == "entity-agent"
    ]
    assert len(entity_handoff) == 1
    assert entity_handoff[0]["decision_code"] == "ENTITY_RESOLVED"


def test_solve_case_entity_not_found(
    tmp_path: Path,
    contracts: Contracts,
    base_case: dict[str, Any],
    fake_evidence: dict[str, dict[str, Any]],
) -> None:
    # Change claimed_order_id to an unknown id not in history
    base_case["customer_request"]["claimed_order_id"] = "order-unknown-999"

    trace_path = tmp_path / "trace.jsonl"
    trace = TraceWriter(trace_path, contracts)
    gateway = FakeGateway(fake_evidence)

    output = asyncio.run(solve_case(base_case, gateway, trace))  # type: ignore[arg-type]

    contracts.validate_output(output, "ENTITY_NOT_FOUND_CASE")
    called_tools = [name for name, _ in gateway.calls]

    # get_order must NOT be called for unknown order
    assert "get_order" not in called_tools

    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    entity_handoff = [
        e for e in events if e["event_type"] == "handoff" and e["actor"] == "entity-agent"
    ]
    assert len(entity_handoff) == 1
    assert entity_handoff[0]["decision_code"] == "ENTITY_NOT_FOUND"


def test_solve_case_refund_timeline_called_when_needed(
    tmp_path: Path,
    contracts: Contracts,
    base_case: dict[str, Any],
    fake_evidence: dict[str, dict[str, Any]],
) -> None:
    # Add a refund claim topic
    base_case["customer_request"]["claims"].append(
        {"claim_id": "claim-001-c", "topic": "refund_pending"}
    )
    fake_evidence["get_refund_timeline"] = {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": "ev_refund_01234567890123456789",
        "result_hash": f"sha256:{'9' * 64}",
        "domain": "refund",
        "data": {"events": []},
    }

    trace_path = tmp_path / "trace.jsonl"
    trace = TraceWriter(trace_path, contracts)
    gateway = FakeGateway(fake_evidence)

    output = asyncio.run(solve_case(base_case, gateway, trace))  # type: ignore[arg-type]

    called_tools = [name for name, _ in gateway.calls]
    assert "get_refund_timeline" in called_tools
    contracts.validate_output(output, "REFUND_CALLED_CASE")


def test_solve_case_handles_tool_failure_gracefully(
    tmp_path: Path,
    contracts: Contracts,
    base_case: dict[str, Any],
    fake_evidence: dict[str, dict[str, Any]],
) -> None:
    # Simulate get_order_items failing with RuntimeError
    fake_evidence["get_order_items"] = RuntimeError("Items DB timeout")  # type: ignore[assignment]

    trace_path = tmp_path / "trace.jsonl"
    trace = TraceWriter(trace_path, contracts)
    gateway = FakeGateway(fake_evidence)

    output = asyncio.run(solve_case(base_case, gateway, trace))  # type: ignore[arg-type]

    contracts.validate_output(output, "FAILURE_HANDLED_CASE")
    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    consumed_tools = [
        e.get("tool_name") for e in events if e["event_type"] == "tool_result_consumed"
    ]
    # Failed tool should not have emitted tool_result_consumed
    assert "get_order_items" not in consumed_tools
    failed = [e for e in events if e.get("decision_code") == "TOOL_CALL_FAILED"]
    assert [e["tool_name"] for e in failed] == ["get_order_items"]
