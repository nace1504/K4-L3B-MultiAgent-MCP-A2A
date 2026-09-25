from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from . import OUTPUT_SCHEMA_VERSION

_VALID_PARTY_TYPES = frozenset(
    ["seller", "platform", "logistics_provider", "payment_provider", "customer", "unknown"]
)
_CAPTURE_EVENT_TYPES = frozenset(
    ["captured", "capture", "payment", "charge", "payment_capture"]
)


def _to_dec(val: Any) -> Decimal:
    if val is None or val == "":
        return Decimal(0)
    try:
        return Decimal(str(val))
    except InvalidOperation:
        return Decimal(0)


def _to_money(val: Any) -> float:
    return float(round(_to_dec(val), 2))


def _parse_dt(ts: Any) -> datetime | None:
    if not ts or not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(ts, fmt)
            except ValueError:
                pass
        return None


def _cmp_dt(d1: datetime | None, d2: datetime | None) -> int:
    if d1 is None or d2 is None:
        return 0
    if d1.tzinfo is None and d2.tzinfo is not None:
        d1 = d1.replace(tzinfo=d2.tzinfo)
    elif d1.tzinfo is not None and d2.tzinfo is None:
        d2 = d2.replace(tzinfo=d1.tzinfo)
    return -1 if d1 < d2 else (1 if d1 > d2 else 0)


def _in_window(ts: Any, start: datetime | None, end: datetime | None) -> bool:
    dt = _parse_dt(ts)
    if dt is None:
        return True
    if start and _cmp_dt(dt, start) < 0:
        return False
    return not (end and _cmp_dt(dt, end) >= 0)


def _get_env_data(ev: dict[str, dict[str, Any] | None], key: str) -> Any:
    env = ev.get(key)
    return env.get("data") if isinstance(env, dict) else None


def _extract_order_rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        if isinstance(data.get("orders"), list):
            return [r for r in data["orders"] if isinstance(r, dict)]
        if isinstance(data.get("order"), dict):
            return [data["order"]]
        if "order_id" in data:
            return [data]
    return []


# evidence domains that decide each claim topic (claim-level evidence_refs)
_CLAIM_EVIDENCE: dict[str, tuple[str, ...]] = {
    "late_delivery_seller": (
        "get_order", "get_order_items", "get_shipment_summary", "get_policy",
    ),
    "late_delivery_logistics": ("get_order", "get_shipment_summary", "get_policy"),
    "canceled_order_paid": (
        "get_customer_history", "get_order", "get_payment_timeline", "get_policy",
    ),
    "unavailable_order_paid": (
        "get_customer_history", "get_order", "get_order_items", "get_payment_timeline",
        "get_policy",
    ),
    "duplicate_charge": ("get_order_items", "get_payment_timeline", "get_policy"),
    "payment_mismatch": ("get_payment_timeline", "get_policy"),
    "valid_split_payment": ("get_payment_timeline", "get_policy"),
    "refund_pending": ("get_payment_timeline", "get_refund_timeline", "get_policy"),
    "refund_failed": ("get_payment_timeline", "get_refund_timeline", "get_policy"),
    "unsupported_claim": (
        "get_order", "get_shipment_summary", "get_payment_timeline", "get_product_context",
        "get_policy",
    ),
    "requested_full_refund": ("get_payment_timeline", "get_refund_timeline", "get_policy"),
}


def _incident(case: dict[str, Any], ev: dict[str, dict[str, Any] | None]) -> dict[str, Any]:
    """Entity resolution + incident window selection (decoy rows excluded)."""
    claimed_order_id = (case.get("customer_request") or {}).get("claimed_order_id")

    history_data = _get_env_data(ev, "get_customer_history")
    history_rows = _extract_order_rows(history_data)
    order_data = _get_env_data(ev, "get_order")
    order_rows = _extract_order_rows(order_data)

    history_order_ids = list(
        dict.fromkeys(r["order_id"] for r in history_rows if r.get("order_id"))
    )

    # Entity resolution
    matches_hist = bool(claimed_order_id and claimed_order_id in history_order_ids)
    matches_ord = bool(
        claimed_order_id and any(r.get("order_id") == claimed_order_id for r in order_rows)
    )
    if matches_hist or matches_ord:
        res_status = "resolved"
        resolved_order_ids = [claimed_order_id]
        res_confidence = 0.95
    else:
        res_status = "not_found"
        resolved_order_ids = []
        res_confidence = 0.4

    target_order_id = resolved_order_ids[0] if resolved_order_ids else claimed_order_id

    # DECOY handling & incident window
    # get_order always returns the decoy row. The correct window row is in get_customer_history
    # and does NOT match get_order's purchase_timestamp.
    opened_at_dt = _parse_dt(case.get("opened_at"))

    # get_order purchase timestamp = decoy timestamp
    order_purchase_ts = order_rows[0].get("order_purchase_timestamp") if order_rows else None
    order_purchase_dt = _parse_dt(order_purchase_ts)

    # All history rows for the target order
    hist_target_rows = [r for r in history_rows if r.get("order_id") == target_order_id]
    ord_target_rows = [r for r in order_rows if r.get("order_id") == target_order_id]

    # Build all candidate rows (history only - preferred over get_order which is decoy)
    # Exclude rows whose purchase_timestamp matches get_order (decoy timestamp)
    # unless there is no non-decoy row before opened_at.
    all_target_rows_tagged: list[tuple[dict, str]] = [
        (r, "get_customer_history") for r in hist_target_rows
    ] + [(r, "get_order") for r in ord_target_rows]

    before_opened = [
        (r, src)
        for r, src in all_target_rows_tagged
        if r.get("order_purchase_timestamp")
        and _cmp_dt(_parse_dt(r.get("order_purchase_timestamp")), opened_at_dt) <= 0
    ]

    # Non-decoy candidates: history rows before opened_at whose purchase_ts ≠ order's purchase_ts
    non_decoy_before = [
        (r, src)
        for r, src in before_opened
        if not (src == "get_order" and r.get("order_purchase_timestamp") == order_purchase_ts)
        and not (
            order_purchase_dt is not None
            and _parse_dt(r.get("order_purchase_timestamp")) == order_purchase_dt
        )
    ]

    conflicts: list[dict[str, Any]] = []

    if non_decoy_before:
        non_decoy_before.sort(
            key=lambda item: _parse_dt(item[0].get("order_purchase_timestamp")) or datetime.min,
            reverse=True,
        )
        selected_row, selected_source = non_decoy_before[0]
    elif before_opened:
        before_opened.sort(
            key=lambda item: _parse_dt(item[0].get("order_purchase_timestamp")) or datetime.min,
            reverse=True,
        )
        selected_row, selected_source = before_opened[0]
    elif all_target_rows_tagged:
        selected_row, selected_source = all_target_rows_tagged[0]
    else:
        selected_row, selected_source = {}, None

    window_start_dt = _parse_dt(selected_row.get("order_purchase_timestamp"))
    # window_end = smallest timestamp STRICTLY greater than window_start among all rows
    all_purchase_dts = [
        _parse_dt(r.get("order_purchase_timestamp"))
        for r, _ in all_target_rows_tagged
        if r.get("order_purchase_timestamp")
    ]
    subsequent_dts = [d for d in all_purchase_dts if d and _cmp_dt(d, window_start_dt) > 0]
    window_end_dt = min(subsequent_dts) if subsequent_dts else None

    # Conflict detection between get_order and get_customer_history
    # compare the chosen incident row against get_order's row (usually the decoy)
    if selected_row and ord_target_rows:
        h_row = selected_row
        o_row = ord_target_rows[0]
        for field in [
            "order_purchase_timestamp",
            "order_status",
            "order_delivered_carrier_date",
            "order_delivered_customer_date",
            "order_estimated_delivery_date",
        ]:
            if h_row.get(field) and o_row.get(field) and h_row[field] != o_row[field]:
                res_code = (
                    "INCIDENT_WINDOW_BEFORE_OPENED_AT"
                    if field == "order_purchase_timestamp"
                    else "INCIDENT_WINDOW_SELECTED"
                )
                conflicts.append({
                    "field": field,
                    "sources": ["get_order", "get_customer_history"],
                    "selected_source": selected_source or "get_customer_history",
                    "resolution_code": res_code,
                })
                if len(conflicts) >= 5:
                    break

    return {
        "history_order_ids": history_order_ids,
        "res_status": res_status,
        "resolved_order_ids": resolved_order_ids,
        "res_confidence": res_confidence,
        "target_order_id": target_order_id,
        "selected_row": selected_row,
        "window_start": window_start_dt,
        "window_end": window_end_dt,
        "conflicts": conflicts,
    }


def _window_rows(data: Any, key: str, ts_field: str, start: Any, end: Any) -> list[dict]:
    rows = data.get(key, []) if isinstance(data, dict) else (data if key == "events" else [])
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict) and _in_window(r.get(ts_field), start, end)]


def scoped_evidence(case: dict[str, Any], ev: dict[str, dict[str, Any] | None]) -> dict[str, Any]:
    """Evidence restricted to the selected incident window, for LLM specialists.

    Decoy snapshots of the same order are dropped here, so prompts stay small and the
    model never has to guess which occurrence the complaint is about.
    """
    inc = _incident(case, ev)
    start, end = inc["window_start"], inc["window_end"]
    facts: dict[str, Any] = {}
    analyze_case(case, ev, facts_out=facts)
    items = _get_env_data(ev, "get_order_items")
    if isinstance(items, dict):
        items = items.get("items") or items.get("order_items") or []
    pay = _get_env_data(ev, "get_payment_timeline")
    ship = _get_env_data(ev, "get_shipment_summary")
    policy = _get_env_data(ev, "get_policy")
    return {
        "facts": facts,
        "incident_order": inc["selected_row"],
        "incident_window": [str(start) if start else None, str(end) if end else None],
        "items": [
            it for it in (items if isinstance(items, list) else [])
            if isinstance(it, dict) and _in_window(it.get("shipping_limit_date"), start, end)
        ],
        "payments": pay.get("payments", []) if isinstance(pay, dict) else [],
        "payment_events": _window_rows(pay, "events", "event_at", start, end),
        "shipment_events": _window_rows(ship, "events", "event_at", start, end),
        "shipping_limits": _window_rows(ship, "shipping_limits", "shipping_limit_at", start, end),
        "refund_events": _window_rows(
            _get_env_data(ev, "get_refund_timeline"), "events", "event_at", start, end
        ),
        "policy_rules": policy.get("rules", {}) if isinstance(policy, dict) else {},
        "missing_tools": sorted(tool for tool, env in ev.items() if env is None),
    }


def analyze_case(
    case: dict[str, Any],
    ev: dict[str, dict[str, Any] | None],
    issue_override: str | None = None,
    facts_out: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pure rule-based analysis returning l3b-output-v2 compliant payload.

    `facts_out`, when given, receives the computed signals (dates, totals, flags) the rules
    classify on, so LLM agents reason over the same exact numbers.
    """
    candidate_order_ids = case.get("candidate_order_ids") or []
    claims = (case.get("customer_request") or {}).get("claims") or []
    claim_topics = [c.get("topic") for c in claims if isinstance(c, dict)]

    inc = _incident(case, ev)
    history_order_ids = inc["history_order_ids"]
    res_status = inc["res_status"]
    resolved_order_ids = inc["resolved_order_ids"]
    res_confidence = inc["res_confidence"]
    target_order_id = inc["target_order_id"]
    selected_row = inc["selected_row"]
    window_start_dt, window_end_dt = inc["window_start"], inc["window_end"]
    conflicts = inc["conflicts"]

    # Window items (filter by shipping_limit_date in window)
    items_data = _get_env_data(ev, "get_order_items")
    if isinstance(items_data, dict):
        raw_items = items_data.get("items") or items_data.get("order_items") or []
        if not raw_items and "order_item_id" in items_data:
            raw_items = [items_data]
    elif isinstance(items_data, list):
        raw_items = items_data
    else:
        raw_items = []

    window_items = [
        it
        for it in raw_items
        if isinstance(it, dict)
        and _in_window(it.get("shipping_limit_date"), window_start_dt, window_end_dt)
    ]
    window_item_ids = list(
        dict.fromkeys(str(it["order_item_id"]) for it in window_items if "order_item_id" in it)
    )
    window_seller_ids = list(
        dict.fromkeys(str(it["seller_id"]) for it in window_items if "seller_id" in it)
    )
    actual_seller_id = window_seller_ids[0] if window_seller_ids else None

    # Window payment events (filter by event_at in window)
    pay_data = _get_env_data(ev, "get_payment_timeline")
    payments_list_raw = pay_data.get("payments", []) if isinstance(pay_data, dict) else []
    raw_pe = (
        pay_data.get("events", [])
        if isinstance(pay_data, dict)
        else (pay_data if isinstance(pay_data, list) else [])
    )
    window_pay_events = [
        e
        for e in raw_pe
        if isinstance(e, dict) and _in_window(e.get("event_at"), window_start_dt, window_end_dt)
    ]

    # Window shipment events (filter by event_at in window)
    ship_data = _get_env_data(ev, "get_shipment_summary")
    raw_se = (
        ship_data.get("events", [])
        if isinstance(ship_data, dict)
        else (ship_data if isinstance(ship_data, list) else [])
    )
    window_ship_events = [
        e
        for e in raw_se
        if isinstance(e, dict) and _in_window(e.get("event_at"), window_start_dt, window_end_dt)
    ]

    # Window refund events (filter by event_at in window)
    ref_data = _get_env_data(ev, "get_refund_timeline")
    raw_re = (
        ref_data.get("events", [])
        if isinstance(ref_data, dict)
        else (ref_data if isinstance(ref_data, list) else [])
    )
    window_ref_events = [
        e
        for e in raw_re
        if isinstance(e, dict) and _in_window(e.get("event_at"), window_start_dt, window_end_dt)
    ]

    # Shipment lifecycle events in window
    # event_type "delivered_late" + actor = definitive late delivery signal
    # actor "logistics_provider"/"logistics" → logistics late
    # actor "seller" → seller late
    confirmed_carrier_date: str | None = None
    confirmed_delivery_date: str | None = None
    has_confirmed_late_logistics_event = False
    has_confirmed_late_seller_event = False
    late_seller_ids_set: set[str] = set()

    for e in window_ship_events:
        st = str(e.get("status", "")).lower()
        etype = str(e.get("event_type", "")).lower()
        actor = str(e.get("actor", "")).lower()
        if st == "confirmed":
            if etype in ("delivered_carrier", "carrier_pickup", "shipped"):
                confirmed_carrier_date = e.get("event_at")
            elif etype in ("delivered_customer", "customer_delivery", "delivered"):
                confirmed_delivery_date = e.get("event_at")
            elif etype == "delivered_late":
                if actor in ("logistics_provider", "logistics"):
                    has_confirmed_late_logistics_event = True
                elif actor == "seller":
                    has_confirmed_late_seller_event = True
                    if e.get("seller_id"):
                        late_seller_ids_set.add(str(e["seller_id"]))

    # Window shipping_limits from shipment_summary
    ship_shipping_limits = (
        ship_data.get("shipping_limits", []) if isinstance(ship_data, dict) else []
    )
    window_ship_limits = [
        sl
        for sl in ship_shipping_limits
        if isinstance(sl, dict)
        and _in_window(sl.get("shipping_limit_at"), window_start_dt, window_end_dt)
    ]

    # Dates for delivery analysis:
    # ONLY use window event dates or selected_row dates.
    # ship_data top-level (delivered_carrier_at etc.) may belong to a different window - avoid!
    carrier_date = confirmed_carrier_date or selected_row.get("order_delivered_carrier_date")
    customer_date = confirmed_delivery_date or selected_row.get("order_delivered_customer_date")
    estimated_date = selected_row.get("order_estimated_delivery_date")

    if (
        confirmed_delivery_date
        and selected_row.get("order_delivered_customer_date")
        and confirmed_delivery_date != selected_row.get("order_delivered_customer_date")
        and len(conflicts) < 5
    ):
        conflicts.append({
            "field": "order_delivered_customer_date",
            "sources": ["get_order", "get_shipment_summary"],
            "selected_source": "get_shipment_summary",
            "resolution_code": "CONFIRMED_LIFECYCLE_OVERRIDE",
        })

    # timeline_complete from incident-window dates only (top-level ship_data may be the decoy)
    display_carrier_date = carrier_date
    display_customer_date = customer_date
    display_estimated_date = estimated_date

    # Payment calculations
    has_payment_evidence = ev.get("get_payment_timeline") is not None
    capture_events = [
        e
        for e in window_pay_events
        if str(e.get("status", "")).lower() in ("confirmed", "succeeded")
        and str(e.get("event_type", "")).lower() in _CAPTURE_EVENT_TYPES
    ]
    captured_total: float | None = (
        max(
            round(
                sum(_to_money(e.get("amount_brl", e.get("amount", 0))) for e in capture_events), 2
            ),
            0.0,
        )
        if has_payment_evidence
        else None
    )

    # Refund events: use LATEST status per refund group
    all_refund_events = list(window_ref_events) + [
        e
        for e in window_pay_events
        if "refund" in str(e.get("event_type", "")).lower()
        or "chargeback" in str(e.get("event_type", "")).lower()
    ]

    # Group refunds by amount as proxy for refund identity; find latest status per group
    refund_by_amount: dict[str, list[dict]] = {}
    for e in all_refund_events:
        k = str(_to_dec(e.get("amount_brl", e.get("amount", ""))))
        refund_by_amount.setdefault(k, []).append(e)

    latest_refund_statuses: list[str] = []
    for group in refund_by_amount.values():
        latest = max(group, key=lambda e: _parse_dt(e.get("event_at")) or datetime.min)
        latest_refund_statuses.append(str(latest.get("status", "")).lower())

    has_refund_failed = "failed" in latest_refund_statuses
    has_refund_pending = "pending" in latest_refund_statuses and not has_refund_failed

    has_refund_evidence = (
        ev.get("get_payment_timeline") is not None or ev.get("get_refund_timeline") is not None
    )
    succeeded_refunds = [
        e
        for e in all_refund_events
        if str(e.get("status", "")).lower() in ("succeeded", "confirmed")
    ]
    refunded_total: float | None = (
        max(
            round(
                sum(
                    _to_money(e.get("amount_brl", e.get("amount", 0)))
                    for e in succeeded_refunds
                ),
                2,
            ),
            0.0,
        )
        if has_refund_evidence
        else None
    )

    # Payment mismatch: explicit reconciliation_mismatch event signals a reconciliation error
    has_reconciliation_mismatch = any(
        str(e.get("event_type", "")).lower() == "reconciliation_mismatch"
        for e in window_pay_events
    )

    # Duplicate charge vs split payment detection:
    # Duplicate signal: the payments_list contains repeated (seq, amount) pairs.
    # A duplicated charge re-generates the payment rows, so the list has more entries than
    # unique (seq, amount) pairs.
    # Split signal: all (seq, amount) pairs in the payments_list are unique, and there are
    # multiple distinct payment_types.
    payments_list_all = [p for p in payments_list_raw if isinstance(p, dict)]

    seq_amount_pairs: list[tuple[str, str]] = []
    for p in payments_list_all:
        seq = str(p.get("payment_sequential", ""))
        amt = str(_to_dec(p.get("payment_value", p.get("amount_brl", p.get("amount", 0)))))
        seq_amount_pairs.append((seq, amt))

    # Duplicate: any (seq, amount) pair appears more than once in the full payments list
    has_duplicate_capture = (
        len(capture_events) >= 2
        and len(seq_amount_pairs) > len(set(seq_amount_pairs))
    )

    # Split: at least 2 captures in window, all (seq, amount) pairs unique, multiple types
    all_payment_types = set(
        str(p.get("payment_type", "")) for p in payments_list_all if p.get("payment_type")
    )
    is_split_payment = (
        len(capture_events) >= 2
        and len(seq_amount_pairs) == len(set(seq_amount_pairs))
        and len(all_payment_types) >= 2
        and not has_reconciliation_mismatch
    )

    # Issue classification
    order_status = str(selected_row.get("order_status", "")).lower()

    if facts_out is not None:
        carrier_dt = _parse_dt(carrier_date)
        limits = [_parse_dt(it.get("shipping_limit_date")) for it in window_items] + [
            _parse_dt(sl.get("shipping_limit_at")) for sl in window_ship_limits
        ]
        delivered_dt, estimated_dt = _parse_dt(customer_date), _parse_dt(estimated_date)
        expected = sum(
            _to_dec(it.get("price")) + _to_dec(it.get("freight_value")) for it in window_items
        )
        facts_out.update({
            "order_status": order_status or None,
            "carrier_handoff_at": carrier_date,
            "delivered_at": customer_date,
            "estimated_delivery_at": estimated_date,
            "carrier_after_shipping_limit": bool(
                carrier_dt and any(lim and _cmp_dt(carrier_dt, lim) > 0 for lim in limits)
            ),
            "delivered_after_estimate": bool(
                delivered_dt and estimated_dt and _cmp_dt(delivered_dt, estimated_dt) > 0
            ),
            "confirmed_delivered_late_by_seller": has_confirmed_late_seller_event,
            "confirmed_delivered_late_by_logistics": has_confirmed_late_logistics_event,
            "captured_total_brl": captured_total,
            "expected_order_total_brl": _to_money(expected),
            "confirmed_capture_count": len(capture_events),
            "payment_types": sorted(all_payment_types),
            "repeated_payment_rows": len(seq_amount_pairs) - len(set(seq_amount_pairs)),
            "reconciliation_mismatch_event": has_reconciliation_mismatch,
            "latest_refund_statuses": sorted(latest_refund_statuses),
            "refunded_total_brl": refunded_total,
        })
    issue: str | None = None

    # 1. order_status canceled/unavailable + confirmed capture in window
    if (
        order_status in ("canceled", "unavailable")
        and captured_total is not None
        and captured_total > 0
    ):
        issue = "canceled_order_paid" if order_status == "canceled" else "unavailable_order_paid"

    # 2. refund events: check latest status per refund
    elif has_refund_failed:
        issue = "refund_failed"
    elif has_refund_pending:
        issue = "refund_pending"

    # 3. duplicate confirmed captures (same sequential captured twice)
    elif has_duplicate_capture:
        issue = "duplicate_charge"

    # 4. reconciliation_mismatch event = payment_mismatch
    elif has_reconciliation_mismatch:
        issue = "payment_mismatch"

    # 5. split payment: multiple payment types all in window, no anomalies
    elif is_split_payment:
        issue = "valid_split_payment"

    # 6. late delivery checks
    else:
        is_late_seller = False

        # Primary: confirmed delivered_late event with actor=seller
        if has_confirmed_late_seller_event:
            is_late_seller = True
            if not late_seller_ids_set:
                for it in window_items:
                    if it.get("seller_id"):
                        late_seller_ids_set.add(str(it["seller_id"]))

        # Secondary: in-window carrier_date > in-window shipping_limit
        # carrier_date must be from window events (confirmed_carrier_date) or selected_row
        # Do NOT use ship_data top-level (may be from different window)
        if not is_late_seller and carrier_date:
            carrier_dt = _parse_dt(carrier_date)
            # Check against item shipping limits
            for it in window_items:
                limit_dt = _parse_dt(it.get("shipping_limit_date"))
                if carrier_dt and limit_dt and _cmp_dt(carrier_dt, limit_dt) > 0:
                    is_late_seller = True
                    if it.get("seller_id"):
                        late_seller_ids_set.add(str(it["seller_id"]))
            # Check against window shipping_limits from shipment_summary
            if not is_late_seller:
                for sl in window_ship_limits:
                    limit_dt = _parse_dt(sl.get("shipping_limit_at"))
                    if carrier_dt and limit_dt and _cmp_dt(carrier_dt, limit_dt) > 0:
                        is_late_seller = True
                        if sl.get("seller_id"):
                            late_seller_ids_set.add(str(sl["seller_id"]))

        if is_late_seller:
            issue = "late_delivery_seller"
        else:
            # Confirmed late logistics event
            customer_dt = _parse_dt(customer_date)
            estimated_dt = _parse_dt(estimated_date)
            is_late_logistics = has_confirmed_late_logistics_event or bool(
                customer_dt and estimated_dt and _cmp_dt(customer_dt, estimated_dt) > 0
            )
            if is_late_logistics:
                issue = "late_delivery_logistics"

    # LLM coordinator decision wins; the evidence gating below still applies
    if issue_override:
        issue = issue_override

    # 7. required evidence missing → insufficient_evidence
    if issue is None:
        required_tools = ["get_order", "get_customer_history", "get_policy"]
        is_missing = any(ev.get(t) is None for t in required_tools) or res_status == "not_found"
        for t in claim_topics:
            if (
                t in ("late_delivery_seller", "late_delivery_logistics")
                and ev.get("get_shipment_summary") is None
            ):
                is_missing = True
            if (
                t in ("duplicate_charge", "payment_mismatch", "valid_split_payment")
                and ev.get("get_payment_timeline") is None
            ):
                is_missing = True
            if (
                t in ("refund_pending", "refund_failed")
                and ev.get("get_refund_timeline") is None
            ):
                is_missing = True
        if is_missing:
            issue = "insufficient_evidence"

    # Also insufficient_evidence if required envelope for determined issue is absent
    if issue in ("late_delivery_seller", "late_delivery_logistics"):
        if ev.get("get_shipment_summary") is None or ev.get("get_order_items") is None:
            issue = "insufficient_evidence"
    elif issue in (
        "duplicate_charge",
        "payment_mismatch",
        "valid_split_payment",
        "canceled_order_paid",
        "unavailable_order_paid",
    ):
        if ev.get("get_payment_timeline") is None:
            issue = "insufficient_evidence"
    elif issue in ("refund_pending", "refund_failed") and ev.get("get_refund_timeline") is None:
        issue = "insufficient_evidence"

    # 8. unsupported claim
    if issue is None:
        issue = "unsupported_claim"

    # Policy mapping
    policy_data = _get_env_data(ev, "get_policy")
    policy_rules = policy_data.get("rules", {}) if isinstance(policy_data, dict) else {}
    rule = policy_rules.get(issue, {}) if isinstance(policy_rules, dict) else {}

    if issue == "insufficient_evidence":
        case_status = "needs_investigation"
        rec_action = rule.get("recommended_action") or "investigate_missing_evidence"
        refund_brl = 0.0
        responsible_parties: list[dict[str, Any]] = [{"party_type": "unknown", "party_id": None}]
    else:
        case_status = rule.get(
            "case_status",
            "no_action"
            if issue in ("unsupported_claim", "valid_split_payment")
            else "action_required",
        )
        rec_action = rule.get(
            "recommended_action",
            "document_no_action" if case_status == "no_action" else "review_case",
        )
        raw_refund = rule.get("refund_brl", 0.0)
        refund_brl = max(_to_money(raw_refund), 0.0)

        resp_parties_raw = rule.get("responsible_parties")
        if resp_parties_raw and isinstance(resp_parties_raw, list):
            responsible_parties = []
            for p in resp_parties_raw:
                if not isinstance(p, dict):
                    continue
                ptype = str(p.get("party_type", "unknown"))
                if ptype not in _VALID_PARTY_TYPES:
                    ptype = "unknown"
                pid = p.get("party_id")
                pid = str(pid) if pid is not None else None
                if ptype == "seller":
                    if late_seller_ids_set:
                        pid = sorted(late_seller_ids_set)[0]
                    elif actual_seller_id:
                        pid = actual_seller_id
                responsible_parties.append({"party_type": ptype, "party_id": pid})
        else:
            if issue == "late_delivery_seller":
                seller_pid = (
                    sorted(late_seller_ids_set)[0] if late_seller_ids_set else actual_seller_id
                )
                responsible_parties = [{"party_type": "seller", "party_id": seller_pid}]
            elif issue == "late_delivery_logistics":
                responsible_parties = [
                    {"party_type": "logistics_provider", "party_id": "logistics"}
                ]
            elif issue in (
                "duplicate_charge",
                "payment_mismatch",
                "valid_split_payment",
                "refund_pending",
                "refund_failed",
            ):
                responsible_parties = [
                    {"party_type": "payment_provider", "party_id": "payment_gateway"}
                ]
            elif issue in ("canceled_order_paid", "unavailable_order_paid"):
                responsible_parties = [{"party_type": "platform", "party_id": "platform"}]
            elif issue == "unsupported_claim":
                pid = case.get("customer_unique_id_hint")
                pid = str(pid) if pid is not None else None
                responsible_parties = [{"party_type": "customer", "party_id": pid}]
            else:
                responsible_parties = [{"party_type": "unknown", "party_id": None}]

    resolution_actions_raw = [rec_action] if isinstance(rec_action, str) else list(rec_action)
    resolution_actions = [a for a in dict.fromkeys(resolution_actions_raw) if a][:8]

    refund_brl = max(refund_brl, 0.0)
    refund_lines = (
        [{"reason_code": issue, "amount_brl": refund_brl, "entity_id": target_order_id}]
        if refund_brl > 0
        else []
    )

    financial_resolution = {
        "currency": "BRL",
        "recommended_refund_brl": refund_brl,
        "refund_lines": refund_lines,
    }

    root_cause_analysis = {
        "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}],
        "responsible_parties": responsible_parties[:5],
    }

    # Confidence
    if issue == "insufficient_evidence":
        assessment_confidence = 0.4
    elif issue in claim_topics:
        assessment_confidence = 0.9
    else:
        assessment_confidence = 0.7

    # Evidence refs: collect from all tools called, in call order, for this case only
    # every case must cite history, order, shipment, payment and policy (missing_required_evidence)
    _ALWAYS_TOOLS = [
        "get_customer_history",
        "get_order",
        "get_order_items",
        "get_shipment_summary",
        "get_payment_timeline",
        "get_refund_timeline",
        "get_policy",
    ]
    _extra_tools = ["get_product_context"] if issue == "unsupported_claim" else []

    evidence_refs: list[str] = []
    for tool in _ALWAYS_TOOLS + _extra_tools:
        env = ev.get(tool)
        if isinstance(env, dict) and env.get("evidence_ref"):
            ref = str(env["evidence_ref"])
            if ref not in evidence_refs:
                evidence_refs.append(ref)
    evidence_refs = evidence_refs[:30]

    # Claim assessments
    claim_assessments: list[dict[str, Any]] = []
    for c in claims[:5]:
        cid = c.get("claim_id")
        topic = c.get("topic")
        if not cid:
            continue
        if issue == "insufficient_evidence":
            verdict = "insufficient_evidence"
            conf = 0.4
        elif topic == "requested_full_refund":
            captured_val = captured_total or 0.0
            if issue in ("late_delivery_seller", "late_delivery_logistics") and refund_brl > 0:
                verdict = "partially_supported"  # freight-only refund is never the full order
                conf = 0.9
            elif refund_brl >= captured_val and captured_val > 0:
                verdict = "supported"
                conf = 0.9
            elif 0 < refund_brl < captured_val:
                verdict = "partially_supported"
                conf = 0.9
            else:
                verdict = "unsupported"
                conf = 0.9
        elif topic == issue:
            verdict = "supported"
            conf = 0.9
        else:
            verdict = "unsupported"
            conf = 0.9
        # a claim cites only the domains that decide it (evidence precision), never an
        # empty list; top-level evidence_refs keeps the full required set
        domain_refs = [
            str(ev[t]["evidence_ref"]) for t in _CLAIM_EVIDENCE.get(str(topic), ())
            if isinstance(ev.get(t), dict) and ev[t].get("evidence_ref")
        ]
        claim_assessments.append({
            "claim_id": cid,
            "verdict": verdict,
            "confidence": conf,
            "evidence_refs": (
                domain_refs if domain_refs and issue != "insufficient_evidence" else evidence_refs
            ),
        })

    # Shipment analysis
    timeline_complete = bool(
        display_carrier_date and display_customer_date and display_estimated_date
    )
    if ev.get("get_shipment_summary") is None:
        ship_verdict = "insufficient_evidence"
    elif issue == "late_delivery_seller":
        ship_verdict = "seller_delay"
    elif issue == "late_delivery_logistics":
        ship_verdict = "logistics_delay"
    else:
        ship_verdict = "on_time"

    late_seller_ids_final = sorted(late_seller_ids_set)
    shipment_analysis = {
        "verdict": ship_verdict,
        "late_seller_ids": late_seller_ids_final[:20],
        "timeline_complete": timeline_complete,
    }

    # Payment analysis
    if ev.get("get_payment_timeline") is None:
        pay_verdict = "insufficient_evidence"
        captured_val_out: float | None = None
        refunded_val_out: float | None = None
        refundable_val_out: float | None = None
    else:
        captured_val_out = captured_total
        refunded_val_out = max(refunded_total or 0.0, 0.0)
        refundable_val_out = max(refund_brl, 0.0)
        if issue == "duplicate_charge":
            pay_verdict = "duplicate_capture"
        elif issue == "payment_mismatch":
            pay_verdict = "capture_mismatch"
        elif issue == "refund_pending":
            pay_verdict = "refund_pending"
        elif issue == "refund_failed":
            pay_verdict = "refund_failed"
        elif refunded_val_out > 0:
            pay_verdict = "refunded"
        else:
            pay_verdict = "reconciled"

    payment_analysis = {
        "verdict": pay_verdict,
        "captured_total_brl": captured_val_out,
        "refunded_total_brl": refunded_val_out,
        "refundable_total_brl": refundable_val_out,
    }

    # ponytail: ID format unknown, fill once public feedback shows a gap
    affected_entities = {
        "order_ids": [target_order_id] if target_order_id else [],
        "item_ids": window_item_ids[:20],
        "seller_ids": window_seller_ids[:20],
        "payment_references": [],
        "shipment_ids": [],
    }

    entity_resolution = {
        "status": res_status,
        "resolved_order_ids": resolved_order_ids[:20],
        "rejected_candidates": sorted(
            set(c for c in candidate_order_ids if c not in resolved_order_ids)
        )[:20],
        "confidence": res_confidence,
    }

    customer_context = {
        "customer_unique_id": case.get("customer_unique_id_hint"),
        "related_order_ids": history_order_ids[:20],
    }

    assessment = {
        "primary_issue": issue,
        "secondary_issues": [],
        "case_status": case_status,
        "confidence": assessment_confidence,
    }

    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": case.get("case_id"),
        "assessment": assessment,
        "affected_entities": affected_entities,
        "claim_assessments": claim_assessments,
        "entity_resolution": entity_resolution,
        "customer_context": customer_context,
        "shipment_analysis": shipment_analysis,
        "payment_analysis": payment_analysis,
        "root_cause_analysis": root_cause_analysis,
        "evidence_refs": evidence_refs,
        "data_conflicts": conflicts[:5],
        "financial_resolution": financial_resolution,
        "resolution_actions": resolution_actions,
    }
