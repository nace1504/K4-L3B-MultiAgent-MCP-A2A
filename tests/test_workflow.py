from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from student_agent.contracts import Contracts
from student_agent.investigation import build_output
from student_agent.mcp_gateway import EvidenceGateway
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case

TOOLS = {
    "get_customer_history",
    "get_order",
    "get_order_items",
    "get_order_payments",
    "get_payment_timeline",
    "get_policy",
    "get_product_context",
    "get_refund_timeline",
    "get_sellers",
    "get_shipment_summary",
}


def evidence(tool_name: str, domain: str, data: Any) -> dict[str, Any]:
    suffix = f"{sum(map(ord, tool_name)):024d}"
    return {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": f"ev_{suffix}",
        "result_hash": f"sha256:{'0' * 64}",
        "domain": domain,
        "data": data,
    }


def valid_split_fixture() -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    case = {
        "case_id": "L3B_CASE_TEST",
        "opened_at": "2018-02-02T09:00:00-03:00",
        "customer_request": {
            "claimed_order_id": "order-1",
            "claims": [
                {"claim_id": "claim-a", "topic": "valid_split_payment"},
                {"claim_id": "claim-b", "topic": "requested_full_refund"},
            ],
        },
        "candidate_order_ids": ["order-1", "order-decoy"],
        "customer_unique_id_hint": "customer-1",
        "policy_version": "EC_POLICY_V2",
        "investigation_scope": {
            "include_customer_history": True,
            "include_product_context": True,
            "require_independent_verification": True,
        },
    }
    order = {
        "order_id": "order-1",
        "customer_id": "customer-row-1",
        "order_status": "delivered",
        "order_purchase_timestamp": "2018-01-21T09:00:00-03:00",
        "order_approved_at": "2018-01-21T10:00:00-03:00",
        "order_delivered_carrier_date": "2018-01-23T09:00:00-03:00",
        "order_delivered_customer_date": "2018-01-30T09:00:00-03:00",
        "order_estimated_delivery_date": "2018-01-31T09:00:00-03:00",
    }
    payments = [
        {
            "order_id": "order-1",
            "payment_sequential": "1",
            "payment_type": "credit_card",
            "payment_value": "44.50",
        },
        {
            "order_id": "order-1",
            "payment_sequential": "2",
            "payment_type": "voucher",
            "payment_value": "44.50",
        },
    ]
    responses = {
        "get_customer_history": evidence(
            "get_customer_history",
            "customer",
            {"customer_unique_id": "customer-1", "orders": [order]},
        ),
        "get_order": evidence("get_order", "order", order),
        "get_order_items": evidence(
            "get_order_items",
            "item",
            [
                {
                    "order_id": "order-1",
                    "order_item_id": "item-1",
                    "product_id": "product-1",
                    "seller_id": "seller-1",
                    "shipping_limit_date": "2018-01-24T09:00:00-03:00",
                    "price": "79.00",
                    "freight_value": "10.00",
                }
            ],
        ),
        "get_order_payments": evidence("get_order_payments", "payment", payments),
        "get_payment_timeline": evidence(
            "get_payment_timeline",
            "payment",
            {
                "order_id": "order-1",
                "payments": payments,
                "events": [
                    {
                        "order_id": "order-1",
                        "event_at": "2018-01-21T10:00:00-03:00",
                        "event_type": "captured",
                        "amount_brl": "44.50",
                        "status": "confirmed",
                    },
                    {
                        "order_id": "order-1",
                        "event_at": "2018-01-21T11:00:00-03:00",
                        "event_type": "captured",
                        "amount_brl": "44.50",
                        "status": "confirmed",
                    },
                ],
            },
        ),
        "get_policy": evidence(
            "get_policy",
            "policy",
            {
                "currency": "BRL",
                "policy_version": "EC_POLICY_V2",
                "rules": {
                    "valid_split_payment": {
                        "case_status": "no_action",
                        "recommended_action": "document_no_action",
                        "refund_brl": 0,
                        "responsible_parties": [
                            {"party_type": "customer", "party_id": None}
                        ],
                    }
                },
            },
        ),
        "get_product_context": evidence(
            "get_product_context",
            "product",
            [{"order_item_id": "item-1", "product_id": "product-1"}],
        ),
        "get_shipment_summary": evidence(
            "get_shipment_summary",
            "shipment",
            {
                "order_id": "order-1",
                "order_status": "delivered",
                "events": [],
            },
        ),
    }
    return case, responses


class FakeGateway:
    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    async def list_tools(self) -> list[str]:
        return sorted(TOOLS)

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        del case_id, arguments
        self.calls.append(tool_name)
        return self.responses[tool_name]


def test_valid_split_workflow_is_schema_valid_and_traced(tmp_path: Path) -> None:
    case, responses = valid_split_fixture()
    gateway = FakeGateway(responses)
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    trace.emit(case_id=case["case_id"], event_type="case_received", actor="coordinator")

    output = asyncio.run(solve_case(case, gateway, trace))  # type: ignore[arg-type]
    contracts.validate_output(output, "test output")
    trace.emit(case_id=case["case_id"], event_type="case_finalized", actor="coordinator")

    assert output["assessment"]["primary_issue"] == "valid_split_payment"
    assert output["assessment"]["case_status"] == "no_action"
    assert output["payment_analysis"]["captured_total_brl"] == 89.0
    assert output["financial_resolution"]["recommended_refund_brl"] == 0.0
    assert set(gateway.calls) == {
        "get_customer_history",
        "get_order",
        "get_order_items",
        "get_order_payments",
        "get_payment_timeline",
        "get_policy",
        "get_shipment_summary",
    }
    assert "get_product_context" not in gateway.calls

    event_types = {
        __import__("json").loads(line)["event_type"]
        for line in (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    }
    assert {
        "case_received",
        "task_assigned",
        "tool_result_consumed",
        "handoff",
        "policy_decided",
        "verification_completed",
        "case_finalized",
    }.issubset(event_types)


def test_gateway_accepts_snake_case_mcp_sdk_result() -> None:
    response = evidence("get_order", "order", {"order_id": "order-1"})

    class Session:
        async def call_tool(self, tool_name: str, arguments: dict[str, str]) -> Any:
            del tool_name, arguments
            return SimpleNamespace(
                is_error=False,
                structured_content=response,
                content=[],
            )

    class ContractStub:
        def validate_evidence(self, value: Any, label: str) -> None:
            assert value is response
            assert label == "MCP tool get_order"

    gateway = EvidenceGateway(Session(), ContractStub())  # type: ignore[arg-type]
    result = asyncio.run(gateway.call("get_order", case_id="CASE_001", order_id="order-1"))
    assert result == response


def test_temporal_customer_scope_beats_conflicting_direct_order() -> None:
    case, responses = valid_split_fixture()
    case["customer_request"]["claims"][0]["topic"] = "canceled_order_paid"
    selected = responses["get_customer_history"]["data"]["orders"][0]
    selected["order_status"] = "canceled"
    selected["order_delivered_carrier_date"] = None
    selected["order_delivered_customer_date"] = None
    responses["get_order"]["data"] = {
        **selected,
        "order_status": "delivered",
        "order_purchase_timestamp": "2018-04-21T09:00:00-03:00",
        "order_delivered_customer_date": "2018-04-30T09:00:00-03:00",
    }
    responses["get_customer_history"]["data"]["orders"].append(
        responses["get_order"]["data"]
    )
    responses["get_shipment_summary"]["data"]["events"] = [
        {
            "order_id": "order-1",
            "event_at": "2018-04-30T09:00:00-03:00",
            "event_type": "delivered_late",
            "actor": "seller",
            "status": "confirmed",
        }
    ]
    responses["get_policy"]["data"]["rules"] = {
        "canceled_order_paid": {
            "case_status": "action_required",
            "recommended_action": "issue_refund",
            "refund_brl": 89,
            "responsible_parties": [{"party_type": "platform", "party_id": None}],
        }
    }

    output = build_output(case, responses)

    assert output["assessment"]["primary_issue"] == "canceled_order_paid"
    assert output["shipment_analysis"]["verdict"] == "insufficient_evidence"
    assert output["financial_resolution"]["recommended_refund_brl"] == 89.0
    assert output["data_conflicts"][0]["field"] == "order_snapshot"
    assert output["data_conflicts"][0]["selected_source"] == "get_customer_history"


def test_seller_delay_uses_case_seller_as_responsible_party() -> None:
    case, responses = valid_split_fixture()
    case["customer_request"]["claims"][0]["topic"] = "late_delivery_seller"
    responses["get_shipment_summary"]["data"]["events"] = [
        {
            "order_id": "order-1",
            "event_at": "2018-01-25T09:00:00-03:00",
            "event_type": "seller_handoff_late",
            "actor": "seller",
            "status": "confirmed",
        }
    ]
    responses["get_policy"]["data"]["rules"] = {
        "late_delivery_seller": {
            "case_status": "action_required",
            "recommended_action": "refund_freight",
            "refund_brl": 10,
            "responsible_parties": [
                {"party_type": "seller", "party_id": "seller-policy-placeholder"}
            ],
        }
    }

    output = build_output(case, responses)

    assert output["shipment_analysis"]["late_seller_ids"] == ["seller-1"]
    assert output["root_cause_analysis"]["responsible_parties"] == [
        {"party_type": "seller", "party_id": "seller-1"}
    ]


def test_failed_refund_uses_authoritative_refund_lifecycle() -> None:
    case, responses = valid_split_fixture()
    case["customer_request"]["claims"][0]["topic"] = "refund_failed"
    responses["get_refund_timeline"] = evidence(
        "get_refund_timeline",
        "refund",
        {
            "order_id": "order-1",
            "events": [
                {
                    "order_id": "order-1",
                    "event_at": "2018-02-01T09:00:00-03:00",
                    "event_type": "refund_requested",
                    "amount_brl": "52.00",
                    "status": "failed",
                }
            ],
        },
    )
    responses["get_policy"]["data"]["rules"] = {
        "refund_failed": {
            "case_status": "action_required",
            "recommended_action": "retry_refund",
            "refund_brl": 52,
            "responsible_parties": [
                {"party_type": "payment_provider", "party_id": None}
            ],
        }
    }

    output = build_output(case, responses)

    assert output["assessment"]["primary_issue"] == "refund_failed"
    assert output["payment_analysis"]["verdict"] == "refund_failed"
    assert output["financial_resolution"]["recommended_refund_brl"] == 52.0
    assert output["resolution_actions"] == ["retry_refund"]


def test_valid_split_selects_matching_capture_subset_from_noisy_timeline() -> None:
    case, responses = valid_split_fixture()
    noisy_payment = {
        "order_id": "order-1",
        "payment_sequential": "1",
        "payment_type": "credit_card",
        "payment_value": "52.00",
    }
    noisy_capture = {
        "order_id": "order-1",
        "event_at": "2018-01-21T10:00:00-03:00",
        "event_type": "captured",
        "amount_brl": "52.00",
        "status": "confirmed",
    }
    responses["get_order_payments"]["data"].insert(0, noisy_payment)
    responses["get_payment_timeline"]["data"]["payments"].insert(0, noisy_payment)
    responses["get_payment_timeline"]["data"]["events"].insert(0, noisy_capture)

    output = build_output(case, responses)

    assert output["assessment"]["primary_issue"] == "valid_split_payment"
    assert output["payment_analysis"]["verdict"] == "reconciled"
    assert output["payment_analysis"]["captured_total_brl"] == 89.0
