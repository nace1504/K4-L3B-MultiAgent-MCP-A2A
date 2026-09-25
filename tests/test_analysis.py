from __future__ import annotations

from pathlib import Path

from student_agent.analysis import analyze_case
from student_agent.contracts import Contracts


def test_case_001_late_delivery_logistics_with_decoy() -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")

    case = {
        "case_id": "L3B_CASE_001",
        "opened_at": "2018-01-01T09:00:00-03:00",
        "customer_request": {
            "language": "vi",
            "message": "Điều tra đa nguồn: resolve đúng order.",
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

    ev = {
        "get_customer_history": {
            "evidence_ref": "ev_cust_hist_01234567890123456",
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
                    },
                    {
                        "order_id": "af0bbb47f125381ce9f3597dc70ef07b",
                        "order_status": "delivered",
                        "order_purchase_timestamp": "2018-05-15T10:00:00-03:00",
                        "order_delivered_carrier_date": "2018-05-17T10:00:00-03:00",
                        "order_delivered_customer_date": "2018-05-20T10:00:00-03:00",
                        "order_estimated_delivery_date": "2018-05-25T10:00:00-03:00",
                    },
                ]
            },
        },
        "get_order": {
            "evidence_ref": "ev_order_decoy_012345678901234",
            "domain": "order",
            "data": {
                "order_id": "af0bbb47f125381ce9f3597dc70ef07b",
                "order_status": "delivered",
                "order_purchase_timestamp": "2018-05-15T10:00:00-03:00",
                "order_delivered_carrier_date": "2018-05-17T10:00:00-03:00",
                "order_delivered_customer_date": "2018-05-20T10:00:00-03:00",
                "order_estimated_delivery_date": "2018-05-25T10:00:00-03:00",
            },
        },
        "get_order_items": {
            "evidence_ref": "ev_items_01234567890123456789",
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
        "get_shipment_summary": {
            "evidence_ref": "ev_ship_012345678901234567890",
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
            "evidence_ref": "ev_pay_0123456789012345678901",
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
            "evidence_ref": "ev_pol_0123456789012345678901",
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

    output = analyze_case(case, ev)

    assert output["assessment"]["primary_issue"] == "late_delivery_logistics"
    assert output["assessment"]["case_status"] == "action_required"
    assert output["financial_resolution"]["recommended_refund_brl"] == 16.0
    refund_lines = output["financial_resolution"]["refund_lines"]
    assert len(refund_lines) == 1
    assert refund_lines[0]["reason_code"] == "late_delivery_logistics"
    assert output["shipment_analysis"]["verdict"] == "logistics_delay"
    assert output["shipment_analysis"]["timeline_complete"] is True
    assert output["payment_analysis"]["verdict"] == "reconciled"
    assert output["payment_analysis"]["captured_total_brl"] == 16.0
    assert output["entity_resolution"]["status"] == "resolved"
    assert output["entity_resolution"]["resolved_order_ids"] == [
        "af0bbb47f125381ce9f3597dc70ef07b"
    ]
    assert output["entity_resolution"]["rejected_candidates"] == ["candidate-001"]

    # Verify conflict detected from decoy
    assert any(c["field"] == "order_purchase_timestamp" for c in output["data_conflicts"])

    # Full JSON Schema contract validation
    contracts.validate_output(output, "L3B_CASE_001")


def test_no_action_case_gives_refund_zero() -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")

    case = {
        "case_id": "L3B_CASE_002",
        "opened_at": "2018-01-01T09:00:00-03:00",
        "customer_request": {
            "language": "vi",
            "message": "Kiểm tra đơn hàng",
            "claimed_order_id": "order-12345",
            "claims": [
                {"claim_id": "claim-002-a", "topic": "late_delivery_seller"},
                {"claim_id": "claim-002-b", "topic": "requested_full_refund"},
            ],
        },
        "candidate_order_ids": ["order-12345"],
        "customer_unique_id_hint": "customer-12345",
    }

    ev = {
        "get_customer_history": {
            "evidence_ref": "ev_cust_hist_01234567890123456",
            "domain": "customer",
            "data": {
                "orders": [
                    {
                        "order_id": "order-12345",
                        "order_status": "delivered",
                        "order_purchase_timestamp": "2017-12-01T10:00:00-03:00",
                        "order_delivered_carrier_date": "2017-12-02T10:00:00-03:00",
                        "order_delivered_customer_date": "2017-12-08T10:00:00-03:00",
                        "order_estimated_delivery_date": "2017-12-15T10:00:00-03:00",
                    }
                ]
            },
        },
        "get_order": {
            "evidence_ref": "ev_order_012345678901234567890",
            "domain": "order",
            "data": {
                "order_id": "order-12345",
                "order_status": "delivered",
                "order_purchase_timestamp": "2017-12-01T10:00:00-03:00",
                "order_delivered_carrier_date": "2017-12-02T10:00:00-03:00",
                "order_delivered_customer_date": "2017-12-08T10:00:00-03:00",
                "order_estimated_delivery_date": "2017-12-15T10:00:00-03:00",
            },
        },
        "get_order_items": {
            "evidence_ref": "ev_items_01234567890123456789",
            "domain": "item",
            "data": [
                {
                    "order_item_id": "item-002",
                    "seller_id": "seller-002",
                    "shipping_limit_date": "2017-12-05T10:00:00-03:00",
                    "price": "20.00",
                    "freight_value": "5.00",
                }
            ],
        },
        "get_shipment_summary": {
            "evidence_ref": "ev_ship_012345678901234567890",
            "domain": "shipment",
            "data": {
                "delivered_carrier_at": "2017-12-02T10:00:00-03:00",
                "delivered_customer_at": "2017-12-08T10:00:00-03:00",
                "estimated_delivery_at": "2017-12-15T10:00:00-03:00",
                "events": [],
            },
        },
        "get_payment_timeline": {
            "evidence_ref": "ev_pay_0123456789012345678901",
            "domain": "payment",
            "data": {
                "payments": [
                    {
                        "payment_sequential": 1,
                        "payment_type": "credit_card",
                        "payment_value": "25.00",
                    }
                ],
                "events": [
                    {
                        "event_at": "2017-12-01T10:05:00-03:00",
                        "event_type": "capture",
                        "amount_brl": "25.00",
                        "status": "confirmed",
                    }
                ],
            },
        },
        "get_policy": {
            "evidence_ref": "ev_pol_0123456789012345678901",
            "domain": "policy",
            "data": {
                "rules": {
                    "unsupported_claim": {
                        "case_status": "no_action",
                        "recommended_action": "document_no_action",
                        "refund_brl": "0.00",
                        "responsible_parties": [
                            {"party_type": "customer", "party_id": "customer-12345"}
                        ],
                    }
                }
            },
        },
    }

    output = analyze_case(case, ev)

    assert output["assessment"]["primary_issue"] == "unsupported_claim"
    assert output["assessment"]["case_status"] == "no_action"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0.0
    assert output["financial_resolution"]["refund_lines"] == []
    assert output["resolution_actions"] == ["document_no_action"]

    contracts.validate_output(output, "NO_ACTION_CASE")


def test_insufficient_evidence_when_order_evidence_missing() -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")

    case = {
        "case_id": "L3B_CASE_003",
        "opened_at": "2018-01-01T09:00:00-03:00",
        "customer_request": {
            "language": "vi",
            "claimed_order_id": "order-missing",
            "claims": [{"claim_id": "claim-003", "topic": "late_delivery_seller"}],
        },
        "candidate_order_ids": ["order-missing"],
    }

    ev = {
        "get_customer_history": None,
        "get_order": None,
        "get_policy": {
            "evidence_ref": "ev_pol_0123456789012345678901",
            "domain": "policy",
            "data": {"rules": {}},
        },
    }

    output = analyze_case(case, ev)

    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["assessment"]["case_status"] == "needs_investigation"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0.0
    assert output["assessment"]["confidence"] == 0.4

    contracts.validate_output(output, "INSUFFICIENT_EVIDENCE")
