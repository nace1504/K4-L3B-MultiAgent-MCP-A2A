"""Host-side evidence handling: case-scoped evidence store, order-version split, facts.

Nothing in this module calls an LLM. The LLM agents only ever see the *views* built
here and cite evidence by local id (E1, E2, ...); real ``evidence_ref`` values stay on
the host and are mapped back when the output is assembled.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from itertools import combinations
from typing import Any

from mcp.shared.exceptions import MCPError

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

ORDER_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
TRANSIENT_RETRIES = 2

# Tool name -> the single argument the LLM may pass (case_id is always injected by host).
MCP_TOOLS: dict[str, tuple[str, str]] = {
    "get_order": ("order_id", "Return the authoritative order row for one order."),
    "get_customer_history": (
        "customer_unique_id",
        "Return order history for one scoped customer identity.",
    ),
    "get_order_items": ("order_id", "Return item and seller rows belonging to one order."),
    "get_order_payments": ("order_id", "Return raw payment rows belonging to one order."),
    "get_payment_timeline": (
        "order_id",
        "Return base payments and authoritative payment lifecycle events.",
    ),
    "get_refund_timeline": ("order_id", "Return authoritative refund lifecycle events."),
    "get_shipment_summary": (
        "order_id",
        "Return delivery timestamps, seller handoff limits and shipment events.",
    ),
    "get_sellers": ("order_id", "Return seller records associated with an order's items."),
    "get_product_context": ("order_id", "Return products and categories of an order."),
    "get_policy": ("policy_version", "Return the machine-readable policy for a version."),
}


# --------------------------------------------------------------------------- primitives


def ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def money(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def to_float(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01")))


def amount(event: dict[str, Any]) -> Decimal:
    return money(event.get("amount_brl"))


# --------------------------------------------------------------------------- evidence store


@dataclass
class StoredEvidence:
    local_id: str
    tool: str
    arguments: dict[str, str]
    evidence_ref: str
    domain: str
    data: Any


class EvidenceStore:
    """Case-scoped cache of MCP evidence. One instance per case, never shared."""

    def __init__(self, case_id: str, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.case_id = case_id
        self._gateway = gateway
        self._trace = trace
        self._by_key: dict[tuple[str, tuple[tuple[str, str], ...]], StoredEvidence | None] = {}
        self._by_local: dict[str, StoredEvidence] = {}
        self._consumed: set[tuple[str, str]] = set()
        self._lock = asyncio.Lock()
        self.mcp_calls = 0

    # -- access -------------------------------------------------------------
    def get(self, local_id: str) -> StoredEvidence | None:
        return self._by_local.get(local_id)

    def find(self, tool: str, **arguments: str) -> StoredEvidence | None:
        return self._by_key.get((tool, tuple(sorted(arguments.items()))))

    def all(self) -> list[StoredEvidence]:
        return list(self._by_local.values())

    def refs(self, local_ids: list[str]) -> list[str]:
        """Map local ids to real evidence refs; unknown ids are silently dropped."""
        out: list[str] = []
        for local_id in local_ids:
            item = self._by_local.get(local_id)
            if item and item.evidence_ref not in out:
                out.append(item.evidence_ref)
        return out

    def unknown(self, local_ids: list[str]) -> list[str]:
        return [x for x in local_ids if x not in self._by_local]

    # -- fetch --------------------------------------------------------------
    async def fetch(self, actor: str, tool: str, **arguments: str) -> StoredEvidence | None:
        """Real MCP call (at most once per tool+arguments per case)."""
        key = (tool, tuple(sorted(arguments.items())))
        async with self._lock:
            if key not in self._by_key:
                self._by_key[key] = await self._call(tool, arguments)
            item = self._by_key[key]
        if item is not None and (actor, item.local_id) not in self._consumed:
            self._consumed.add((actor, item.local_id))
            self._trace.emit(
                case_id=self.case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=tool,
                evidence_refs=[item.evidence_ref],
                attributes={"domain": item.domain, "local_id": item.local_id},
            )
        return item

    async def _call(self, tool: str, arguments: dict[str, str]) -> StoredEvidence | None:
        for attempt in range(TRANSIENT_RETRIES + 1):
            try:
                self.mcp_calls += 1
                evidence = await self._gateway.call(tool, case_id=self.case_id, **arguments)
            except RuntimeError as exc:  # deterministic tool error: never retried
                if "failed" in str(exc):
                    return None
                raise
            except MCPError:
                # JSON-RPC error response from the server: transient in practice. Retry,
                # then let the caller redo the whole case instead of dropping evidence.
                if attempt >= TRANSIENT_RETRIES:
                    raise
                await asyncio.sleep(2.0 * (attempt + 1))
                continue
            except (TimeoutError, OSError):
                if attempt >= TRANSIENT_RETRIES:
                    return None
                await asyncio.sleep(1.0)
                continue
            local_id = f"E{len(self._by_local) + 1}"
            item = StoredEvidence(
                local_id,
                tool,
                dict(arguments),
                evidence["evidence_ref"],
                evidence["domain"],
                evidence.get("data"),
            )
            self._by_local[local_id] = item
            return item
        return None


# --------------------------------------------------------------------------- order versions


@dataclass
class Scenario:
    """One consistent version of the order, anchored on a distinct order row."""

    version_id: str
    order: dict[str, Any]
    merged: bool = False  # identical history rows: versions not separable by time
    items: list[dict[str, Any]] = field(default_factory=list)
    captures: list[dict[str, Any]] = field(default_factory=list)
    payment_events: list[dict[str, Any]] = field(default_factory=list)
    refund_events: list[dict[str, Any]] = field(default_factory=list)
    shipment_events: list[dict[str, Any]] = field(default_factory=list)
    shipping_limits: list[dict[str, Any]] = field(default_factory=list)

    @property
    def purchase(self) -> datetime | None:
        return ts(self.order.get("order_purchase_timestamp"))

    @property
    def estimated(self) -> datetime | None:
        return ts(self.order.get("order_estimated_delivery_date"))

    @property
    def order_total(self) -> Decimal:
        return sum(
            (money(i.get("price")) + money(i.get("freight_value")) for i in self.items),
            Decimal("0"),
        )

    @property
    def captured_total(self) -> Decimal:
        return sum((amount(e) for e in self.captures), Decimal("0"))

    def add(self, bucket: str, record: dict[str, Any]) -> None:
        rows = getattr(self, bucket)
        if record not in rows:  # identical rows from duplicated versions are one record
            rows.append(record)


@dataclass
class VersionContext:
    order_id: str
    scenarios: list[Scenario]
    selected: Scenario
    basis: str

    @property
    def inseparable(self) -> bool:
        return self.basis == "inseparable_versions"

    def owner(self, when: Any) -> Scenario | None:
        """A dated record belongs to the latest version purchased at or before it."""
        if len(self.scenarios) == 1:
            return self.scenarios[0]
        moment = ts(when)
        if moment is None:
            return None
        owners = [s for s in self.scenarios if s.purchase is not None and s.purchase <= moment]
        return max(owners, key=lambda s: s.purchase) if owners else None  # type: ignore[arg-type,return-value]

    def describe(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "versions": [
                {
                    "version_id": s.version_id,
                    "selected": s is self.selected,
                    "order_status": s.order.get("order_status"),
                    "purchase": s.order.get("order_purchase_timestamp"),
                    "estimated_delivery": s.order.get("order_estimated_delivery_date"),
                }
                for s in self.scenarios
            ],
            "selection_basis": self.basis,
            "inseparable_versions": self.inseparable,
        }


def build_versions(
    order_id: str,
    order_row: dict[str, Any],
    history_orders: list[dict[str, Any]],
    opened_at: Any,
) -> VersionContext:
    """Deterministic version choice, made by host code before any LLM sees the data.

    A complaint can only concern a version that existed when the case was opened.
    Preference: versions already due (purchase and estimated delivery at or before
    ``opened_at``), then versions merely placed before it; latest purchase wins.
    Without a usable ``opened_at`` the authoritative get_order row wins.
    """
    rows: list[dict[str, Any]] = []
    duplicated = False
    for row in history_orders:
        if row.get("order_id") != order_id:
            continue
        if row in rows:
            duplicated = True
        else:
            rows.append(row)
    if not any(
        r.get("order_purchase_timestamp") == order_row.get("order_purchase_timestamp") for r in rows
    ):
        rows.insert(0, order_row)
    scenarios = [Scenario(f"V{i}", r, merged=duplicated) for i, r in enumerate(rows, 1)]
    authoritative = next(
        (
            s
            for s in scenarios
            if s.order.get("order_purchase_timestamp") == order_row.get("order_purchase_timestamp")
        ),
        scenarios[0],
    )
    opened = ts(opened_at)
    placed = [s for s in scenarios if opened and s.purchase and s.purchase <= opened]
    due = [s for s in placed if opened and s.estimated and s.estimated <= opened]
    if len(scenarios) == 1:
        selected, basis = scenarios[0], "single_version"
    elif due:
        selected = max(due, key=lambda s: s.purchase)  # type: ignore[arg-type,return-value]
        basis = "unique_due_version" if len(due) == 1 else "latest_due_version"
    elif placed:
        selected = max(placed, key=lambda s: s.purchase)  # type: ignore[arg-type,return-value]
        basis = "latest_placed_version"
    else:
        selected, basis = authoritative, "authoritative_fallback"
    if duplicated and len(scenarios) == 1:
        basis = "inseparable_versions"
    return VersionContext(order_id, scenarios, selected, basis)


def absorb(ctx: VersionContext, tool: str, data: Any) -> None:
    """Attach dated rows of a tool result to the version that owns them."""
    if not isinstance(data, (dict, list)):
        return

    def put(bucket: str, record: dict[str, Any], when: Any) -> None:
        owner = ctx.owner(when)
        if owner:
            owner.add(bucket, record)

    if tool == "get_order_items":
        for item in data if isinstance(data, list) else []:
            put("items", item, item.get("shipping_limit_date"))
    elif tool == "get_payment_timeline" and isinstance(data, dict):
        for event in data.get("events") or []:
            put("payment_events", event, event.get("event_at"))
            if event.get("event_type") == "captured" and event.get("status", "confirmed") == (
                "confirmed"
            ):
                put("captures", event, event.get("event_at"))
    elif tool == "get_refund_timeline" and isinstance(data, dict):
        for event in data.get("events") or []:
            put("refund_events", event, event.get("event_at"))
    elif tool == "get_shipment_summary" and isinstance(data, dict):
        for limit in data.get("shipping_limits") or []:
            put("shipping_limits", limit, limit.get("shipping_limit_at"))
        for event in data.get("events") or []:
            put("shipment_events", event, event.get("event_at"))


def _split(rows: list[dict[str, Any]], ctx: VersionContext, date_key: str) -> tuple[list, list]:
    keep: list[dict[str, Any]] = []
    other: list[dict[str, Any]] = []
    for row in rows:
        owner = ctx.owner(row.get(date_key))
        target = (
            keep if owner is ctx.selected or owner is None and len(ctx.scenarios) == 1 else other
        )
        if row not in target:
            target.append(row)
    return keep, other


def view_for_llm(ctx: VersionContext | None, tool: str, data: Any) -> dict[str, Any]:
    """Selected-version view of a tool result plus the rows excluded by the version rule."""
    if ctx is None or not isinstance(data, (dict, list)):
        return {"data": data}
    sel = ctx.selected.order
    if tool == "get_order" and isinstance(data, dict):
        same = data.get("order_purchase_timestamp") == sel.get("order_purchase_timestamp")
        return {"data": sel, "excluded_other_version_rows": [] if same else [data]}
    if tool == "get_customer_history" and isinstance(data, dict):
        orders = data.get("orders") or []
        other = [o for o in orders if o.get("order_id") == ctx.order_id and o != sel]
        keep = [o for o in orders if o not in other]
        deduped = [o for i, o in enumerate(keep) if o not in keep[:i]]
        return {"data": {**data, "orders": deduped}, "excluded_other_version_rows": other}
    if tool == "get_order_items" and isinstance(data, list):
        keep, other = _split(data, ctx, "shipping_limit_date")
        return {"data": keep, "excluded_other_version_rows": other}
    if tool in {"get_payment_timeline", "get_refund_timeline"} and isinstance(data, dict):
        keep, other = _split(data.get("events") or [], ctx, "event_at")
        view = {**data, "events": keep}
        if tool == "get_payment_timeline":
            view["payments"] = _payments_for(ctx, data.get("payments") or [])
        return {"data": view, "excluded_other_version_rows": other}
    if tool == "get_order_payments" and isinstance(data, list):
        return {"data": _payments_for(ctx, data)}
    if tool == "get_shipment_summary" and isinstance(data, dict):
        limits, other_limits = _split(data.get("shipping_limits") or [], ctx, "shipping_limit_at")
        events, other_events = _split(data.get("events") or [], ctx, "event_at")
        view = {
            **data,
            "order_status": sel.get("order_status"),
            "delivered_carrier_at": sel.get("order_delivered_carrier_date"),
            "delivered_customer_at": sel.get("order_delivered_customer_date"),
            "estimated_delivery_at": sel.get("order_estimated_delivery_date"),
            "shipping_limits": limits,
            "events": events,
        }
        top_other = data.get("delivered_customer_at") != sel.get("order_delivered_customer_date")
        return {
            "data": view,
            "excluded_other_version_rows": other_limits
            + other_events
            + ([{"top_level_timestamps_from": "other_version"}] if top_other else []),
        }
    return {"data": data}


def _payments_for(ctx: VersionContext, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Undated payment rows linked to the selected version by captured amount."""
    pool = list(rows)
    linked: list[dict[str, Any]] = []
    for capture in ctx.selected.captures:
        for idx, row in enumerate(pool):
            if row is not None and money(row.get("payment_value")) == amount(capture):
                linked.append(row)
                pool[idx] = None  # type: ignore[call-overload]
                break
    return linked or rows


# --------------------------------------------------------------------------- facts


def split_group(s: Scenario) -> list[dict[str, Any]]:
    """Captures (>=2) that together reconcile exactly to the order total."""
    total = s.order_total
    if not total or len(s.captures) < 2:
        return []
    for size in range(len(s.captures), 1, -1):
        for group in combinations(s.captures, size):
            if sum((amount(e) for e in group), Decimal("0")) == total:
                return list(group)
    return []


def duplicate_group(s: Scenario) -> list[dict[str, Any]]:
    """Repeated captures of one amount that do not reconcile to the order total."""
    by_amount: dict[Decimal, list[dict[str, Any]]] = {}
    for event in s.captures:
        by_amount.setdefault(amount(event), []).append(event)
    for group in by_amount.values():
        if len(group) >= 2 and sum((amount(e) for e in group), Decimal("0")) != s.order_total:
            return group
    return []


def late_party(s: Scenario) -> str | None:
    delivered = ts(s.order.get("order_delivered_customer_date"))
    if not (delivered and s.estimated and delivered > s.estimated):
        return None
    carrier = ts(s.order.get("order_delivered_carrier_date"))
    limits = [x for x in (ts(v.get("shipping_limit_at")) for v in s.shipping_limits) if x]
    limits = limits or [x for x in (ts(i.get("shipping_limit_date")) for i in s.items) if x]
    if carrier and limits:
        return "seller" if carrier > min(limits) else "logistics_provider"
    actors = {(e.get("actor") or "") for e in s.shipment_events}
    return "seller" if "seller" in actors else "logistics_provider"


def issue_holds(issue: str, s: Scenario) -> bool:
    """Evidence predicate per primary issue — used by the verifier, never shown as a label."""
    status = (s.order.get("order_status") or "").lower()
    refund_status = {(e.get("status") or "").lower() for e in s.refund_events}
    checks = {
        "canceled_order_paid": lambda: status == "canceled" and bool(s.captures),
        "unavailable_order_paid": lambda: status == "unavailable" and bool(s.captures),
        "refund_failed": lambda: "failed" in refund_status,
        "refund_pending": lambda: bool(refund_status & {"pending", "requested", "processing"}),
        "payment_mismatch": lambda: any(
            e.get("event_type") == "reconciliation_mismatch" for e in s.payment_events
        ),
        "duplicate_charge": lambda: bool(duplicate_group(s)),
        "late_delivery_seller": lambda: late_party(s) == "seller",
        "late_delivery_logistics": lambda: late_party(s) == "logistics_provider",
        "valid_split_payment": lambda: bool(split_group(s)),
    }
    if issue == "unsupported_claim":
        return not any(check() for check in checks.values())
    check = checks.get(issue)
    return bool(check and check())


def supported_issues(s: Scenario) -> list[str]:
    return [i for i in PRIMARY_ISSUES if i != "insufficient_evidence" and issue_holds(i, s)]


def detected_conflicts(ctx: VersionContext) -> list[dict[str, Any]]:
    """Source disagreements found by the host while splitting versions."""
    if len(ctx.scenarios) == 1 and not ctx.inseparable:
        return []
    auth_is_selected = ctx.basis == "authoritative_fallback"
    chosen = (
        None if ctx.inseparable else ("get_order" if auth_is_selected else "get_customer_history")
    )
    found = [
        {
            "field": "order.version",
            "sources": ["get_order", "get_customer_history"],
            "host_selected_source": chosen,
            "host_basis": ctx.basis,
        }
    ]
    others = [s for s in ctx.scenarios if s is not ctx.selected]
    per_tool = [
        ("order_items.shipping_limit_date", "get_order_items", any(o.items for o in others)),
        (
            "payment.captured_amount_brl",
            "get_payment_timeline",
            any(o.captures or o.payment_events for o in others),
        ),
        ("refund.events", "get_refund_timeline", any(o.refund_events for o in others)),
        ("shipment.events", "get_shipment_summary", any(o.shipment_events for o in others)),
    ]
    for field_name, tool, present in per_tool:
        if present:
            found.append(
                {
                    "field": field_name,
                    "sources": [tool, "get_customer_history"],
                    "host_selected_source": tool,
                    "host_basis": "rows_linked_to_selected_order_version",
                }
            )
    return found


def settle_basis(ctx: VersionContext) -> None:
    """Identical duplicated rows are only a real conflict if they support >1 issue."""
    if ctx.inseparable and len(supported_issues(ctx.selected)) <= 1:
        ctx.basis = "single_version_duplicated_rows"


PRIMARY_ISSUES = (
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
    "insufficient_evidence",
)


def relevant_captures(issue: str, s: Scenario) -> list[dict[str, Any]]:
    """Captures that belong to the chosen issue when versions are inseparable."""
    if not s.merged:
        return s.captures
    if issue == "valid_split_payment":
        return split_group(s) or s.captures
    if issue == "duplicate_charge":
        return duplicate_group(s) or s.captures
    if issue in {"refund_failed", "refund_pending"}:
        wanted = {amount(e) for e in s.refund_events}
    elif issue == "payment_mismatch":
        wanted = {
            amount(e) for e in s.payment_events if e.get("event_type") == "reconciliation_mismatch"
        }
    else:
        return s.captures
    return [e for e in s.captures if amount(e) in wanted] or s.captures


def shipment_facts(s: Scenario) -> dict[str, Any]:
    delivered = ts(s.order.get("order_delivered_customer_date"))
    carrier = ts(s.order.get("order_delivered_carrier_date"))
    limits = [x for x in (ts(v.get("shipping_limit_at")) for v in s.shipping_limits) if x]
    return {
        "order_status": s.order.get("order_status"),
        "delivered_after_estimate": bool(delivered and s.estimated and delivered > s.estimated),
        "carrier_handoff_after_shipping_limit": bool(carrier and limits and carrier > min(limits)),
        "timeline_complete": all(
            s.order.get(k)
            for k in (
                "order_purchase_timestamp",
                "order_approved_at",
                "order_delivered_carrier_date",
                "order_delivered_customer_date",
                "order_estimated_delivery_date",
            )
        ),
        "seller_ids": sorted(
            {x.get("seller_id") for x in s.shipping_limits + s.items if x.get("seller_id")}
        ),
    }


def payment_facts(ctx: VersionContext) -> dict[str, Any]:
    s = ctx.selected
    split = split_group(s)
    dup = duplicate_group(s)
    return {
        "captured_amounts_brl": [to_float(amount(e)) for e in s.captures],
        "captured_total_brl": to_float(s.captured_total),
        "order_total_brl": to_float(s.order_total) if s.items else None,
        "capture_group_reconciling_to_order_total_brl": [to_float(amount(e)) for e in split],
        "repeated_capture_amounts_brl": [to_float(amount(e)) for e in dup],
        "reconciliation_mismatch_open": any(
            e.get("event_type") == "reconciliation_mismatch" for e in s.payment_events
        ),
        "refund_events": [
            {"amount_brl": e.get("amount_brl"), "status": e.get("status")} for e in s.refund_events
        ],
        "inseparable_versions": ctx.inseparable,
    }
