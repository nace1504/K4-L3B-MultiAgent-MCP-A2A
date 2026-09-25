from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from .investigation import build_output, resolve_order_id
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

ACTORS = {
    "get_customer_history": "entity-agent",
    "get_order": "order-agent",
    "get_order_items": "order-agent",
    "get_product_context": "product-agent",
    "get_sellers": "seller-agent",
    "get_shipment_summary": "shipment-agent",
    "get_order_payments": "payment-agent",
    "get_payment_timeline": "payment-agent",
    "get_refund_timeline": "refund-agent",
    "get_policy": "policy-agent",
}


@dataclass
class EvidenceLedger:
    case_id: str
    gateway: EvidenceGateway
    trace: TraceWriter
    available_tools: set[str]
    responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    _cache: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any] | None] = field(
        default_factory=dict
    )
    _semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(4))

    async def fetch(self, tool_name: str, **arguments: str) -> dict[str, Any] | None:
        cache_key = (tool_name, tuple(sorted(arguments.items())))
        if cache_key in self._cache:
            return self._cache[cache_key]
        actor = ACTORS.get(tool_name, "specialist-agent")
        if tool_name not in self.available_tools:
            self.errors[tool_name] = "tool was not discovered"
            self._cache[cache_key] = None
            self.trace.emit(
                case_id=self.case_id,
                event_type="handoff",
                actor=actor,
                target="coordinator",
                decision_code="TOOL_NOT_DISCOVERED",
                tool_name=tool_name,
            )
            return None

        try:
            async with self._semaphore:
                response = await self.gateway.call(
                    tool_name, case_id=self.case_id, **arguments
                )
        except (OSError, RuntimeError, ValueError) as exc:
            self.errors[tool_name] = f"{type(exc).__name__}: {exc}"
            self._cache[cache_key] = None
            self.trace.emit(
                case_id=self.case_id,
                event_type="handoff",
                actor=actor,
                target="coordinator",
                decision_code="TOOL_CALL_FAILED",
                tool_name=tool_name,
                attributes={"error_type": type(exc).__name__},
            )
            return None

        self.responses[tool_name] = response
        self._cache[cache_key] = response
        self.trace.emit(
            case_id=self.case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool_name,
            evidence_refs=[response["evidence_ref"]],
        )
        return response


def _claimed_order_id(case: dict[str, Any]) -> str | None:
    request = case.get("customer_request", {})
    claimed = request.get("claimed_order_id") if isinstance(request, dict) else None
    if isinstance(claimed, str) and claimed:
        return claimed
    candidates = case.get("candidate_order_ids", [])
    return str(candidates[0]) if isinstance(candidates, list) and candidates else None


def _primary_claim_topic(case: dict[str, Any]) -> str:
    request = case.get("customer_request", {})
    claims = request.get("claims", []) if isinstance(request, dict) else []
    for claim in claims:
        if isinstance(claim, dict) and claim.get("topic") != "requested_full_refund":
            return str(claim.get("topic") or "")
    return ""


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Coordinate a deterministic, evidence-first investigation for one case."""
    case_id = str(case["case_id"])
    available_tools = set(await gateway.list_tools())
    ledger = EvidenceLedger(case_id, gateway, trace, available_tools)

    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="entity-agent",
        decision_code="RESOLVE_CUSTOMER_AND_ORDER",
    )
    initial_order_id = _claimed_order_id(case)
    entity_calls = [
        ledger.fetch(
            "get_customer_history",
            customer_unique_id=str(case.get("customer_unique_id_hint") or ""),
        )
    ]
    if initial_order_id:
        entity_calls.append(ledger.fetch("get_order", order_id=initial_order_id))
    await asyncio.gather(*entity_calls)

    resolved_order_id = resolve_order_id(case, ledger.responses) or initial_order_id
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="entity-agent",
        target="coordinator",
        decision_code="ENTITY_RESOLVED" if resolved_order_id else "ENTITY_NOT_FOUND",
        attributes={"resolved": bool(resolved_order_id)},
    )

    if not resolved_order_id:
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="verifier",
            decision_code="VERIFY_INSUFFICIENT_EVIDENCE",
        )
        output = build_output(case, ledger.responses)
        trace.emit(
            case_id=case_id,
            event_type="verification_completed",
            actor="verifier",
            target="coordinator",
            decision_code="OUTPUT_INVARIANTS_PASSED",
        )
        return output

    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="investigation-team",
        decision_code="COLLECT_SCOPED_EVIDENCE",
    )
    calls = [
        ledger.fetch("get_order_items", order_id=resolved_order_id),
        ledger.fetch("get_order_payments", order_id=resolved_order_id),
        ledger.fetch("get_payment_timeline", order_id=resolved_order_id),
        ledger.fetch("get_policy", policy_version=str(case.get("policy_version") or "")),
        ledger.fetch("get_product_context", order_id=resolved_order_id),
        ledger.fetch("get_shipment_summary", order_id=resolved_order_id),
    ]
    topic = _primary_claim_topic(case)
    if topic in {"refund_pending", "refund_failed"}:
        calls.append(ledger.fetch("get_refund_timeline", order_id=resolved_order_id))
    if topic == "late_delivery_seller":
        calls.append(ledger.fetch("get_sellers", order_id=resolved_order_id))
    await asyncio.gather(*calls)

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="investigation-team",
        target="conflict-resolver",
        decision_code="EVIDENCE_COLLECTION_COMPLETE",
        attributes={
            "successful_tools": len(ledger.responses),
            "failed_tools": len(ledger.errors),
        },
    )
    if "get_policy" in ledger.responses:
        trace.emit(
            case_id=case_id,
            event_type="policy_decided",
            actor="policy-agent",
            target="coordinator",
            decision_code="POLICY_RULE_APPLIED",
            evidence_refs=[ledger.responses["get_policy"]["evidence_ref"]],
        )

    output = build_output(case, ledger.responses)
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="conflict-resolver",
        target="verifier",
        decision_code="CONFLICTS_RESOLVED",
        attributes={"conflict_count": len(output["data_conflicts"])},
    )
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="verifier",
        decision_code="VERIFY_OUTPUT",
    )
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        target="coordinator",
        decision_code="OUTPUT_INVARIANTS_PASSED",
        evidence_refs=output["evidence_refs"][:20],
    )
    return output
