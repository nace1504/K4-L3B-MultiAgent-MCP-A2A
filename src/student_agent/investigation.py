from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from itertools import combinations
from typing import Any

PRIMARY_ISSUES = {
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
    "valid_split_payment",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
    "unsupported_claim",
}

CAUSE_CODES = {
    "canceled_order_paid": "CANCELED_ORDER_CAPTURED",
    "unavailable_order_paid": "UNAVAILABLE_ORDER_CAPTURED",
    "late_delivery_seller": "SELLER_HANDOFF_DELAY",
    "late_delivery_logistics": "LOGISTICS_DELIVERY_DELAY",
    "valid_split_payment": "VALID_SPLIT_PAYMENT",
    "payment_mismatch": "PAYMENT_RECONCILIATION_MISMATCH",
    "duplicate_charge": "DUPLICATE_PAYMENT_CAPTURE",
    "refund_pending": "REFUND_PENDING",
    "refund_failed": "REFUND_FAILED",
    "unsupported_claim": "CLAIM_NOT_SUPPORTED",
    "insufficient_evidence": "INSUFFICIENT_EVIDENCE",
}

DEFAULT_STATUS = {
    "canceled_order_paid": "action_required",
    "unavailable_order_paid": "action_required",
    "late_delivery_seller": "action_required",
    "late_delivery_logistics": "action_required",
    "valid_split_payment": "no_action",
    "payment_mismatch": "action_required",
    "duplicate_charge": "action_required",
    "refund_pending": "needs_investigation",
    "refund_failed": "action_required",
    "unsupported_claim": "no_action",
    "insufficient_evidence": "needs_investigation",
}

DEFAULT_ACTION = {
    "canceled_order_paid": "issue_refund",
    "unavailable_order_paid": "issue_refund",
    "late_delivery_seller": "refund_freight",
    "late_delivery_logistics": "refund_freight",
    "valid_split_payment": "document_no_action",
    "payment_mismatch": "reconcile_payment",
    "duplicate_charge": "refund_duplicate_charge",
    "refund_pending": "monitor_refund",
    "refund_failed": "retry_refund",
    "unsupported_claim": "document_no_action",
    "insufficient_evidence": "collect_additional_evidence",
}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(row) for row in value if isinstance(row, Mapping)]


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _money(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return Decimal("0.00")


def _number(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01")))


def _unique(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value is None:
            continue
        item = str(value)
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _evidence_data(evidence: Mapping[str, dict[str, Any]], tool_name: str) -> Any:
    response = evidence.get(tool_name)
    return response.get("data") if isinstance(response, Mapping) else None


def _evidence_ref(evidence: Mapping[str, dict[str, Any]], tool_name: str) -> str | None:
    response = evidence.get(tool_name)
    value = response.get("evidence_ref") if isinstance(response, Mapping) else None
    return value if isinstance(value, str) else None


def _refs_for(evidence: Mapping[str, dict[str, Any]], *tool_names: str) -> list[str]:
    return _unique(_evidence_ref(evidence, name) for name in tool_names)


def _select_order(
    case: Mapping[str, Any], evidence: Mapping[str, dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    customer_data = _mapping(_evidence_data(evidence, "get_customer_history"))
    history = _rows(customer_data.get("orders"))
    direct = _mapping(_evidence_data(evidence, "get_order"))
    request = _mapping(case.get("customer_request"))
    claimed_id = request.get("claimed_order_id")
    candidate_ids = _unique(case.get("candidate_order_ids", []))
    accepted_ids = set(candidate_ids)
    if isinstance(claimed_id, str):
        accepted_ids.add(claimed_id)

    candidates = [row for row in history if row.get("order_id") in accepted_ids]
    opened_at = _parse_time(case.get("opened_at"))

    topic = ""
    for claim in _rows(request.get("claims")):
        if claim.get("topic") != "requested_full_refund":
            topic = str(claim.get("topic") or "")
            break
    expected_status = {
        "canceled_order_paid": "canceled",
        "unavailable_order_paid": "unavailable",
    }.get(topic)
    status_matches = [
        row
        for row in candidates
        if str(row.get("order_status") or "").lower() == expected_status
    ]
    if status_matches:
        candidates = status_matches

    def purchase_time(row: Mapping[str, Any]) -> datetime | None:
        return _parse_time(row.get("order_purchase_timestamp"))

    selected: dict[str, Any] = {}
    dated = [(row, purchase_time(row)) for row in candidates]
    if opened_at:
        past = [(row, stamp) for row, stamp in dated if stamp and stamp <= opened_at]
        if past:
            selected = max(past, key=lambda pair: pair[1])[0]
        elif dated:
            with_dates = [(row, stamp) for row, stamp in dated if stamp]
            if with_dates:
                selected = min(
                    with_dates,
                    key=lambda pair: abs((pair[1] - opened_at).total_seconds()),
                )[0]
    if not selected and candidates:
        selected = candidates[0]
    if not selected and direct.get("order_id") in accepted_ids:
        selected = direct
    return selected, history, direct


def resolve_order_id(
    case: Mapping[str, Any], evidence: Mapping[str, dict[str, Any]]
) -> str | None:
    selected, _, _ = _select_order(case, evidence)
    value = selected.get("order_id")
    return str(value) if value else None


def _select_items(rows: list[dict[str, Any]], order: Mapping[str, Any]) -> list[dict[str, Any]]:
    order_id = order.get("order_id")
    matching = [row for row in rows if row.get("order_id") == order_id]
    purchased_at = _parse_time(order.get("order_purchase_timestamp"))
    if purchased_at:
        window_end = purchased_at + timedelta(days=45)
        scoped = [
            row
            for row in matching
            if (stamp := _parse_time(row.get("shipping_limit_date")))
            and purchased_at - timedelta(days=1) <= stamp <= window_end
        ]
        if scoped:
            matching = scoped

    unique_rows: dict[str, dict[str, Any]] = {}
    for row in matching:
        key = str(row.get("order_item_id") or len(unique_rows))
        unique_rows.setdefault(key, row)
    return list(unique_rows.values())


def _select_payment_events(
    events: list[dict[str, Any]], order: Mapping[str, Any], opened_at: datetime | None
) -> list[dict[str, Any]]:
    order_id = order.get("order_id")
    matching = [row for row in events if row.get("order_id") == order_id]
    approved_at = _parse_time(order.get("order_approved_at"))
    purchased_at = _parse_time(order.get("order_purchase_timestamp"))
    selected: list[dict[str, Any]] = []
    for event in matching:
        stamp = _parse_time(event.get("event_at"))
        event_type = event.get("event_type")
        if event_type == "captured" and approved_at and stamp:
            if abs((stamp - approved_at).total_seconds()) <= timedelta(days=2).total_seconds():
                selected.append(event)
        elif stamp and purchased_at:
            upper = (opened_at or purchased_at) + timedelta(days=14)
            if purchased_at - timedelta(days=1) <= stamp <= upper:
                selected.append(event)
    return selected


def _select_payment_rows(
    payments: list[dict[str, Any]], capture_events: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    remaining = list(payments)
    selected: list[dict[str, Any]] = []
    for event in capture_events:
        amount = _money(event.get("amount_brl"))
        match_index = next(
            (
                index
                for index, payment in enumerate(remaining)
                if _money(payment.get("payment_value")) == amount
            ),
            None,
        )
        if match_index is not None:
            selected.append(remaining.pop(match_index))
    return selected


def _select_split_captures(
    captures: list[dict[str, Any]],
    payments: list[dict[str, Any]],
    expected: Decimal,
) -> list[dict[str, Any]]:
    if expected <= 0 or len(captures) < 2:
        return captures
    for size in range(2, min(len(captures), 6) + 1):
        for subset in combinations(captures, size):
            if sum((_money(event.get("amount_brl")) for event in subset), Decimal("0")) != expected:
                continue
            matched = _select_payment_rows(payments, list(subset))
            references = {
                (row.get("payment_sequential"), row.get("payment_type")) for row in matched
            }
            if len(matched) == size and len(references) >= 2:
                return list(subset)
    return captures


def _select_refund_events(
    events: list[dict[str, Any]], order: Mapping[str, Any], opened_at: datetime | None
) -> list[dict[str, Any]]:
    purchased_at = _parse_time(order.get("order_purchase_timestamp"))
    order_id = order.get("order_id")
    selected: list[dict[str, Any]] = []
    for event in events:
        if event.get("order_id") != order_id:
            continue
        stamp = _parse_time(event.get("event_at"))
        if not purchased_at or not stamp:
            selected.append(event)
            continue
        upper = (opened_at or purchased_at) + timedelta(days=30)
        if purchased_at - timedelta(days=1) <= stamp <= upper:
            selected.append(event)
    return selected


def _select_shipment_events(
    events: list[dict[str, Any]], order: Mapping[str, Any], opened_at: datetime | None
) -> list[dict[str, Any]]:
    purchased_at = _parse_time(order.get("order_purchase_timestamp"))
    estimated_at = _parse_time(order.get("order_estimated_delivery_date"))
    order_id = order.get("order_id")
    upper_candidates = [stamp for stamp in (opened_at, estimated_at) if stamp]
    upper = max(upper_candidates) + timedelta(days=30) if upper_candidates else None
    selected: list[dict[str, Any]] = []
    for event in events:
        if event.get("order_id") != order_id:
            continue
        stamp = _parse_time(event.get("event_at"))
        in_window = bool(
            purchased_at
            and stamp
            and purchased_at - timedelta(days=1) <= stamp
            and (not upper or stamp <= upper)
        )
        if not purchased_at or not stamp or in_window:
            selected.append(event)
    return selected


def _shipment_analysis(
    order: Mapping[str, Any],
    items: list[dict[str, Any]],
    shipment_data: Mapping[str, Any],
    opened_at: datetime | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    events = _select_shipment_events(_rows(shipment_data.get("events")), order, opened_at)
    status = str(order.get("order_status") or "").lower()
    event_types = {str(event.get("event_type") or "").lower() for event in events}
    actors = {str(event.get("actor") or "").lower() for event in events}
    delivered_at = _parse_time(order.get("order_delivered_customer_date"))
    estimated_at = _parse_time(order.get("order_estimated_delivery_date"))

    if "lost" in event_types or status == "lost":
        verdict = "lost"
    elif "returned" in event_types or status == "returned":
        verdict = "returned"
    elif status in {"canceled", "unavailable"}:
        verdict = "insufficient_evidence"
    elif "seller" in actors:
        verdict = "seller_delay"
    elif "logistics_provider" in actors:
        verdict = "logistics_delay"
    elif delivered_at and estimated_at:
        verdict = "on_time" if delivered_at <= estimated_at else "logistics_delay"
    else:
        verdict = "insufficient_evidence"

    timeline_complete = bool(
        order.get("order_purchase_timestamp")
        and order.get("order_approved_at")
        and order.get("order_estimated_delivery_date")
        and (status != "delivered" or order.get("order_delivered_customer_date"))
    )
    late_sellers = _unique(
        item.get("seller_id") for item in items if verdict == "seller_delay"
    )
    return (
        {
            "verdict": verdict,
            "late_seller_ids": late_sellers,
            "timeline_complete": timeline_complete,
        },
        events,
    )


def _payment_analysis(
    order: Mapping[str, Any],
    items: list[dict[str, Any]],
    payment_data: Mapping[str, Any],
    raw_payments: list[dict[str, Any]],
    refund_data: Mapping[str, Any],
    opened_at: datetime | None,
    claim_topic: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], Decimal]:
    events = _select_payment_events(_rows(payment_data.get("events")), order, opened_at)
    captures = [event for event in events if event.get("event_type") == "captured"]
    expected = sum(
        (_money(item.get("price")) + _money(item.get("freight_value")) for item in items),
        Decimal("0"),
    )
    if claim_topic == "valid_split_payment":
        captures = _select_split_captures(captures, raw_payments, expected)
    selected_payments = _select_payment_rows(raw_payments, captures)
    refund_events = _select_refund_events(
        _rows(refund_data.get("events")), order, opened_at
    )

    captured = sum((_money(event.get("amount_brl")) for event in captures), Decimal("0"))
    completed_statuses = {"confirmed", "completed", "succeeded", "refunded"}
    refunded = sum(
        (
            _money(event.get("amount_brl"))
            for event in refund_events
            if str(event.get("status") or "").lower() in completed_statuses
        ),
        Decimal("0"),
    )
    event_types = {str(event.get("event_type") or "").lower() for event in events}
    refund_statuses = {str(event.get("status") or "").lower() for event in refund_events}

    if "failed" in refund_statuses:
        verdict = "refund_failed"
    elif "pending" in refund_statuses:
        verdict = "refund_pending"
    elif refunded > 0:
        verdict = "refunded"
    elif "reconciliation_mismatch" in event_types:
        verdict = "capture_mismatch"
    elif expected > 0 and captured > expected + Decimal("0.01") and len(captures) > 1:
        verdict = "duplicate_capture"
    elif captured > 0:
        verdict = "reconciled"
    else:
        verdict = "insufficient_evidence"

    analysis = {
        "verdict": verdict,
        "captured_total_brl": _number(captured) if captures else None,
        "refunded_total_brl": _number(refunded) if refund_events else 0.0,
        "refundable_total_brl": 0.0,
    }
    return analysis, selected_payments, refund_events, expected


def _claim_topic(case: Mapping[str, Any]) -> str:
    request = _mapping(case.get("customer_request"))
    for claim in _rows(request.get("claims")):
        topic = claim.get("topic")
        if topic in PRIMARY_ISSUES:
            return str(topic)
    return "insufficient_evidence"


def _issue_is_supported(
    topic: str,
    order: Mapping[str, Any],
    shipment: Mapping[str, Any],
    payment: Mapping[str, Any],
    payments: list[dict[str, Any]],
    expected_total: Decimal,
) -> bool:
    status = str(order.get("order_status") or "").lower()
    captured = _money(payment.get("captured_total_brl"))
    if topic == "canceled_order_paid":
        return status == "canceled" and captured > 0
    if topic == "unavailable_order_paid":
        return status == "unavailable" and captured > 0
    if topic == "late_delivery_seller":
        return shipment.get("verdict") == "seller_delay"
    if topic == "late_delivery_logistics":
        return shipment.get("verdict") == "logistics_delay"
    if topic == "payment_mismatch":
        return payment.get("verdict") == "capture_mismatch"
    if topic == "duplicate_charge":
        return payment.get("verdict") == "duplicate_capture"
    if topic == "refund_pending":
        return payment.get("verdict") == "refund_pending"
    if topic == "refund_failed":
        return payment.get("verdict") == "refund_failed"
    if topic == "valid_split_payment":
        references = {
            (row.get("payment_sequential"), row.get("payment_type")) for row in payments
        }
        return (
            len(references) >= 2
            and expected_total > 0
            and abs(captured - expected_total) <= Decimal("0.01")
            and payment.get("verdict") == "reconciled"
        )
    if topic == "unsupported_claim":
        return payment.get("verdict") in {"reconciled", "insufficient_evidence"} and shipment.get(
            "verdict"
        ) in {"on_time", "insufficient_evidence"}
    return False


def _policy_rule(
    evidence: Mapping[str, dict[str, Any]], primary_issue: str
) -> dict[str, Any]:
    policy = _mapping(_evidence_data(evidence, "get_policy"))
    rules = _mapping(policy.get("rules"))
    return _mapping(rules.get(primary_issue))


def _data_conflicts(
    selected_order: Mapping[str, Any],
    direct_order: Mapping[str, Any],
    selected_payments: list[dict[str, Any]],
    raw_payments: list[dict[str, Any]],
    shipment_data: Mapping[str, Any],
) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    order_fields = (
        "order_status",
        "order_purchase_timestamp",
        "order_delivered_customer_date",
    )
    if selected_order and direct_order and any(
        selected_order.get(field) != direct_order.get(field) for field in order_fields
    ):
        conflicts.append(
            {
                "field": "order_snapshot",
                "sources": ["get_order", "get_customer_history"],
                "selected_source": "get_customer_history",
                "resolution_code": "CUSTOMER_SCOPE_AND_OPENED_AT",
            }
        )
    if raw_payments and len(raw_payments) != len(selected_payments):
        conflicts.append(
            {
                "field": "payment_records",
                "sources": ["get_order_payments", "get_payment_timeline"],
                "selected_source": "get_payment_timeline",
                "resolution_code": "TEMPORAL_SCOPE_BY_APPROVAL",
            }
        )
    shipment_status = shipment_data.get("order_status")
    if shipment_status and shipment_status != selected_order.get("order_status"):
        conflicts.append(
            {
                "field": "shipment_order_status",
                "sources": ["get_shipment_summary", "get_customer_history"],
                "selected_source": "get_customer_history",
                "resolution_code": "CUSTOMER_SCOPE_AND_OPENED_AT",
            }
        )
    return conflicts[:5]


def build_output(
    case: Mapping[str, Any], evidence: Mapping[str, dict[str, Any]]
) -> dict[str, Any]:
    """Build one schema-valid, evidence-linked L3B assessment."""
    selected_order, history, direct_order = _select_order(case, evidence)
    case_id = str(case.get("case_id") or "")
    opened_at = _parse_time(case.get("opened_at"))
    resolved_order_id = selected_order.get("order_id")
    candidate_ids = _unique(case.get("candidate_order_ids", []))

    raw_items = _rows(_evidence_data(evidence, "get_order_items"))
    items = _select_items(raw_items, selected_order)
    raw_payments = _rows(_evidence_data(evidence, "get_order_payments"))
    payment_timeline = _mapping(_evidence_data(evidence, "get_payment_timeline"))
    refund_timeline = _mapping(_evidence_data(evidence, "get_refund_timeline"))
    shipment_data = _mapping(_evidence_data(evidence, "get_shipment_summary"))

    shipment, _ = _shipment_analysis(selected_order, items, shipment_data, opened_at)
    claimed_topic = _claim_topic(case)
    payment, selected_payments, refund_events, expected_total = _payment_analysis(
        selected_order,
        items,
        payment_timeline,
        raw_payments,
        refund_timeline,
        opened_at,
        claimed_topic,
    )

    supported = bool(selected_order) and _issue_is_supported(
        claimed_topic, selected_order, shipment, payment, selected_payments, expected_total
    )
    core_tools = {
        "get_customer_history",
        "get_order",
        "get_order_items",
        "get_order_payments",
        "get_payment_timeline",
        "get_policy",
        "get_shipment_summary",
    }
    has_core_evidence = len(core_tools.intersection(evidence)) >= 6
    if supported:
        primary_issue = claimed_topic
    elif has_core_evidence:
        primary_issue = "unsupported_claim"
    else:
        primary_issue = "insufficient_evidence"

    rule = _policy_rule(evidence, primary_issue)
    case_status = str(rule.get("case_status") or DEFAULT_STATUS[primary_issue])
    action = str(rule.get("recommended_action") or DEFAULT_ACTION[primary_issue])
    recommended_refund = _money(rule.get("refund_brl"))
    refunded_total = _money(payment.get("refunded_total_brl"))
    outstanding_refund = max(Decimal("0"), recommended_refund - refunded_total)
    payment["refundable_total_brl"] = _number(outstanding_refund)

    item_ids = _unique(item.get("order_item_id") for item in items)
    seller_ids = _unique(item.get("seller_id") for item in items)
    payment_references = _unique(
        payment_row.get("payment_sequential") for payment_row in selected_payments
    )
    related_order_ids = _unique(row.get("order_id") for row in history)

    responsible_parties = _rows(rule.get("responsible_parties"))
    case_seller_ids: list[str] = []
    if primary_issue == "late_delivery_seller":
        case_seller_ids = shipment["late_seller_ids"]
    elif primary_issue == "unavailable_order_paid":
        case_seller_ids = seller_ids
    if case_seller_ids:
        responsible_parties = [
            {"party_type": "seller", "party_id": seller_id}
            for seller_id in case_seller_ids[:5]
        ]
    elif not responsible_parties:
        responsible_parties = [{"party_type": "unknown", "party_id": None}]
    responsible_parties = [
        {
            "party_type": str(party.get("party_type") or "unknown"),
            "party_id": party.get("party_id"),
        }
        for party in responsible_parties[:5]
    ]

    rejected_candidates = [
        candidate for candidate in candidate_ids if candidate != resolved_order_id
    ]

    all_refs = _refs_for(evidence, *sorted(evidence))
    issue_tools = {
        "late_delivery_seller": ("get_shipment_summary", "get_order_items", "get_sellers"),
        "late_delivery_logistics": ("get_shipment_summary", "get_order"),
        "valid_split_payment": ("get_order_payments", "get_payment_timeline"),
        "payment_mismatch": ("get_order_payments", "get_payment_timeline"),
        "duplicate_charge": ("get_order_items", "get_order_payments", "get_payment_timeline"),
        "refund_pending": ("get_payment_timeline", "get_refund_timeline"),
        "refund_failed": ("get_payment_timeline", "get_refund_timeline"),
        "canceled_order_paid": ("get_customer_history", "get_payment_timeline"),
        "unavailable_order_paid": ("get_customer_history", "get_payment_timeline"),
        "unsupported_claim": ("get_order", "get_payment_timeline", "get_shipment_summary"),
        "insufficient_evidence": tuple(sorted(evidence)),
    }
    primary_refs = _refs_for(evidence, *issue_tools[primary_issue], "get_policy")
    refund_refs = _refs_for(
        evidence, "get_payment_timeline", "get_refund_timeline", "get_policy"
    )

    claims = _rows(_mapping(case.get("customer_request")).get("claims"))
    claim_assessments: list[dict[str, Any]] = []
    captured_total = _money(payment.get("captured_total_brl"))
    for claim in claims[:5]:
        topic = claim.get("topic")
        if topic == "requested_full_refund":
            if outstanding_refund <= 0:
                verdict = "unsupported"
            elif captured_total > 0 and outstanding_refund >= captured_total:
                verdict = "supported"
            else:
                verdict = "partially_supported"
            refs = refund_refs
        else:
            verdict = (
                "supported"
                if supported and topic == primary_issue
                else "insufficient_evidence"
                if primary_issue == "insufficient_evidence"
                else "unsupported"
            )
            refs = primary_refs
        claim_assessments.append(
            {
                "claim_id": str(claim.get("claim_id") or "unknown-claim"),
                "verdict": verdict,
                "confidence": 0.94 if verdict != "insufficient_evidence" else 0.45,
                "evidence_refs": refs,
            }
        )

    output: dict[str, Any] = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "secondary_issues": [],
            "case_status": case_status,
            "confidence": 0.94 if supported else 0.72 if has_core_evidence else 0.4,
        },
        "affected_entities": {
            "order_ids": [str(resolved_order_id)] if resolved_order_id else [],
            "item_ids": item_ids,
            "seller_ids": seller_ids,
            "payment_references": payment_references,
            "shipment_ids": [],
        },
        "claim_assessments": claim_assessments,
        "entity_resolution": {
            "status": "resolved" if resolved_order_id else "not_found",
            "resolved_order_ids": [str(resolved_order_id)] if resolved_order_id else [],
            "rejected_candidates": rejected_candidates,
            "confidence": (
                0.98 if resolved_order_id and history else 0.75 if resolved_order_id else 0.2
            ),
        },
        "customer_context": {
            "customer_unique_id": _mapping(
                _evidence_data(evidence, "get_customer_history")
            ).get("customer_unique_id"),
            "related_order_ids": related_order_ids,
        },
        "shipment_analysis": shipment,
        "payment_analysis": payment,
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": CAUSE_CODES[primary_issue], "rank": 1}],
            "responsible_parties": responsible_parties,
        },
        "evidence_refs": all_refs,
        "data_conflicts": _data_conflicts(
            selected_order,
            direct_order,
            selected_payments,
            raw_payments,
            shipment_data,
        ),
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": _number(outstanding_refund),
            "refund_lines": (
                [
                    {
                        "reason_code": primary_issue,
                        "amount_brl": _number(outstanding_refund),
                        "entity_id": str(resolved_order_id) if resolved_order_id else None,
                    }
                ]
                if outstanding_refund > 0
                else []
            ),
        },
        "resolution_actions": [action],
    }
    verify_output_invariants(output)
    return output


def verify_output_invariants(output: Mapping[str, Any]) -> None:
    """Fail closed on deterministic cross-field inconsistencies before finalization."""
    entity = _mapping(output.get("entity_resolution"))
    resolved = set(entity.get("resolved_order_ids", []))
    rejected = set(entity.get("rejected_candidates", []))
    if resolved.intersection(rejected):
        raise ValueError("resolved and rejected order IDs overlap")

    financial = _mapping(output.get("financial_resolution"))
    lines = _rows(financial.get("refund_lines"))
    line_total = sum((_money(line.get("amount_brl")) for line in lines), Decimal("0"))
    recommended = _money(financial.get("recommended_refund_brl"))
    if line_total != recommended:
        raise ValueError("refund line total does not match recommended refund")

    assessment = _mapping(output.get("assessment"))
    if assessment.get("case_status") == "no_action" and recommended != 0:
        raise ValueError("no_action case cannot recommend a refund")

    affected = _mapping(output.get("affected_entities"))
    shipment = _mapping(output.get("shipment_analysis"))
    if not set(shipment.get("late_seller_ids", [])).issubset(
        set(affected.get("seller_ids", []))
    ):
        raise ValueError("late sellers must be present in affected entities")

    if assessment.get("primary_issue") == "late_delivery_seller":
        responsible = _rows(_mapping(output.get("root_cause_analysis")).get("responsible_parties"))
        responsible_sellers = {
            party.get("party_id")
            for party in responsible
            if party.get("party_type") == "seller" and party.get("party_id")
        }
        if not set(shipment.get("late_seller_ids", [])).issubset(responsible_sellers):
            raise ValueError("late sellers must be identified as responsible parties")
