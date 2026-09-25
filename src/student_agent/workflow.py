"""Multi-agent workflow: a deterministic evidence plan fetches each MCP tool once, LLM
specialists analyse the incident-scoped evidence in parallel, an LLM coordinator decides
the issue, and the rule engine acts as an independent verifier that can object once.

Requires OPENAI_API_KEY. If the coordinator still disagrees with the verifier after its one
revision, the case fails closed to the verifier's issue at low confidence: a full run showed
every persistent disagreement was an LLM false positive that invented a refund.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .analysis import _extract_order_rows, analyze_case, scoped_evidence
from .llm_agents import LLMAgents, clip
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

MAX_CALLS_PER_CASE = 8

# which agent owns (and is traced as consuming) each MCP tool
TOOL_OWNER = {
    "get_customer_history": "entity-agent",
    "get_order": "entity-agent",
    "get_order_items": "order-agent",
    "get_shipment_summary": "shipment-agent",
    "get_payment_timeline": "payment-agent",
    "get_refund_timeline": "payment-agent",
    "get_policy": "policy-agent",
    "get_product_context": "order-agent",
}
SPECIALISTS = ("order-agent", "shipment-agent", "payment-agent")

# what each LLM specialist sees from the scoped evidence (smaller prompt, fewer distractions)
_VIEW = {
    "order-agent": ("facts", "incident_order", "items", "payments"),
    "shipment-agent": ("facts", "incident_order", "items", "shipment_events", "shipping_limits"),
    "payment-agent": (
        "facts", "incident_order", "items", "payments", "payment_events", "refund_events",
    ),
}

_AGENTS: LLMAgents | None | bool = False  # False = not loaded yet


def _default_agents() -> LLMAgents | None:
    global _AGENTS
    if _AGENTS is False:
        _AGENTS = LLMAgents.from_env()
    return _AGENTS  # type: ignore[return-value]


class _Case:
    """Per-case evidence ledger: one call per tool, arguments forced from the case."""

    def __init__(self, case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.case = case
        self.case_id = case["case_id"]
        self.gateway = gateway
        self.trace = trace
        self.ev: dict[str, dict[str, Any] | None] = {}
        self.consumed_refs: set[str] = set()
        self.calls = 0
        self.order_id: str | None = None

    def data(self, tool: str) -> Any:
        envelope = self.ev.get(tool)
        return envelope.get("data") if isinstance(envelope, dict) else None

    def _arguments(self, tool: str) -> dict[str, str] | None:
        if tool == "get_customer_history":
            hint = self.case.get("customer_unique_id_hint")
            return {"customer_unique_id": hint} if hint else None
        if tool == "get_policy":
            return {"policy_version": self.case["policy_version"]}
        return {"order_id": self.order_id} if self.order_id else None

    async def call(self, tool: str) -> dict[str, Any] | None:
        if tool in self.ev:  # cached, including failures: never retry an audited call
            return self.ev[tool]
        arguments = self._arguments(tool)
        if arguments is None or self.calls >= MAX_CALLS_PER_CASE:
            return None
        self.calls += 1
        try:
            evidence = await self.gateway.call(tool, case_id=self.case_id, **arguments)
        except Exception:  # any tool/transport failure must not kill the case
            self.ev[tool] = None
            self.emit("handoff", TOOL_OWNER[tool], target="coordinator",
                      decision_code="TOOL_CALL_FAILED", tool_name=tool)
            return None
        self.ev[tool] = evidence
        ref = evidence.get("evidence_ref") if isinstance(evidence, dict) else None
        if ref:
            self.consumed_refs.add(ref)
            self.emit("tool_result_consumed", TOOL_OWNER[tool], tool_name=tool,
                      evidence_refs=[ref])
        return evidence

    def ref(self, tool: str) -> list[str] | None:
        envelope = self.ev.get(tool)
        ref = envelope.get("evidence_ref") if isinstance(envelope, dict) else None
        return [ref] if ref else None

    def emit(self, event_type: str, actor: str, **fields: Any) -> None:
        self.trace.emit(case_id=self.case_id, event_type=event_type, actor=actor, **fields)


async def _resolve_entity(state: _Case) -> None:
    """Entity agent: the claimed order must exist in this customer's history."""
    state.emit("task_assigned", "coordinator", target="entity-agent",
               decision_code="RESOLVE_CUSTOMER_AND_ORDER")
    history = await state.call("get_customer_history")
    history_ids = {
        str(r["order_id"])
        for r in _extract_order_rows(history.get("data") if history else None)
        if r.get("order_id")
    }
    claimed = str((state.case.get("customer_request") or {}).get("claimed_order_id") or "")
    if claimed and not claimed.startswith("candidate-") and claimed in history_ids:
        state.order_id = claimed
        await state.call("get_order")
    state.emit("handoff", "entity-agent", target="coordinator",
               decision_code="ENTITY_RESOLVED" if state.order_id else "ENTITY_NOT_FOUND")


def _needs_refund_timeline(state: _Case) -> bool:
    claims = (state.case.get("customer_request") or {}).get("claims") or []
    if any(str(c.get("topic", "")).startswith("refund_") for c in claims if isinstance(c, dict)):
        return True
    payment = state.data("get_payment_timeline")
    events = payment.get("events", []) if isinstance(payment, dict) else []
    return any(
        "refund" in str(e.get("event_type", "")).lower()
        or "chargeback" in str(e.get("event_type", "")).lower()
        for e in events if isinstance(e, dict)
    )


async def _collect_evidence(state: _Case) -> None:
    """Coordinator's evidence plan: every domain tool once, in parallel."""
    state.emit("task_assigned", "coordinator", target="investigation-team",
               decision_code="COLLECT_SCOPED_EVIDENCE")
    tools = ["get_policy"]
    if state.order_id:
        tools += ["get_order_items", "get_shipment_summary", "get_payment_timeline"]
    await asyncio.gather(*(state.call(tool) for tool in tools))
    if state.order_id and _needs_refund_timeline(state):
        await state.call("get_refund_timeline")
    state.emit("handoff", "policy-agent", target="coordinator",
               decision_code="POLICY_LOADED" if state.ev.get("get_policy") else "POLICY_MISSING",
               evidence_refs=state.ref("get_policy"))


# issues a specialist may only report when the computed facts allow them
# (gpt-4o-mini reported late delivery / mismatch against facts saying otherwise)
_GROUNDING = {
    "late_delivery_seller": lambda f: bool(
        f.get("carrier_after_shipping_limit") or f.get("confirmed_delivered_late_by_seller")
    ),
    "late_delivery_logistics": lambda f: bool(
        f.get("delivered_after_estimate") or f.get("confirmed_delivered_late_by_logistics")
    ),
    "payment_mismatch": lambda f: bool(f.get("reconciliation_mismatch_event")),
}


_ALWAYS_ALLOWED = {"unsupported_claim", "insufficient_evidence"}


def _within(
    answer: dict[str, Any] | None, allowed: list[str]
) -> tuple[dict[str, Any] | None, str | None]:
    """Coordinator may not invent an issue no specialist reported: drop it (still an answer)."""
    issue = (answer or {}).get("primary_issue")
    if answer is None or issue is None or issue in allowed:
        return answer, None
    return {**answer, "primary_issue": None}, issue


async def _specialist(
    state: _Case, agents: LLMAgents, role: str, brief: dict[str, Any], scoped: dict[str, Any]
) -> dict[str, Any] | None:
    """task_assigned -> one LLM call over the role's scoped evidence -> handoff."""
    state.emit("task_assigned", "coordinator", target=role, decision_code="ANALYSE_DOMAIN")
    view = {key: scoped[key] for key in _VIEW[role]}
    finding = await agents.run(
        role, {**brief, "evidence": clip(view), "missing_tools": scoped["missing_tools"]}, []
    )
    refs = [r for tool, owner in TOOL_OWNER.items() if owner == role for r in state.ref(tool) or []]
    attributes = {"model": agents.model, "confidence": finding["confidence"]} if finding else None
    grounded = _GROUNDING.get((finding or {}).get("issue") or "")
    if finding and grounded and not grounded(scoped["facts"]):
        # the finding contradicts exact facts it depends on: drop it before the coordinator
        attributes = {**(attributes or {}), "rejected_issue": finding["issue"]}
        finding = {**finding, "issue": None}
    state.emit("handoff", role, target="coordinator",
               decision_code=(finding or {}).get("issue") or "NO_FINDING",
               evidence_refs=refs or None, attributes=attributes)
    return finding


def _check(state: _Case, output: dict[str, Any]) -> list[str]:
    """Hard verifier checks; any failure means the output must not be trusted."""
    problems = []
    contracts = getattr(state.trace, "contracts", None)
    if contracts is not None:
        try:
            contracts.validate_output(output, f"verifier:{state.case_id}")
        except Exception:
            problems.append("SCHEMA")
    refs = set(output.get("evidence_refs", []))
    for claim in output.get("claim_assessments", []):
        if isinstance(claim, dict):
            refs.update(claim.get("evidence_refs", []))
    if not refs <= state.consumed_refs:
        problems.append("REFS")
    status = output.get("assessment", {}).get("case_status")
    finance = output.get("financial_resolution", {})
    refund = finance.get("recommended_refund_brl", 0.0)
    lines = finance.get("refund_lines", [])
    if status == "no_action" and (refund or lines):
        problems.append("NO_ACTION_WITH_REFUND")
    if refund and abs(round(sum(x.get("amount_brl", 0.0) for x in lines), 2) - refund) > 0.01:
        problems.append("REFUND_SUM")
    actions = output.get("resolution_actions", [])
    if len(actions) != len(set(actions)):
        problems.append("DUPLICATE_ACTIONS")
    return problems


def _calibrate(llm_confidence: float, votes: int) -> float:
    """Calibration scores (correct - confidence)^2. An issue the LLM and the independent
    verifier agree on was correct on every public case, so sit near the top of the range."""
    return round(min(0.99, max(llm_confidence, 0.95) + 0.01 * votes), 2)


async def solve_case(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    agents: LLMAgents | None | bool = True,
) -> dict[str, Any]:
    if agents is True:
        agents = _default_agents()
    if not agents:
        raise RuntimeError("OPENAI_API_KEY required: decisions are made by LLM agents")
    state = _Case(case, gateway, trace)
    request = case.get("customer_request") or {}
    brief = {
        "opened_at": case.get("opened_at"),
        "message": request.get("message"),
        "claims": request.get("claims"),
    }

    await _resolve_entity(state)
    await _collect_evidence(state)
    scoped = scoped_evidence(case, state.ev)

    findings: dict[str, Any] = {}
    if state.order_id:
        results = await asyncio.gather(
            *(_specialist(state, agents, role, brief, scoped) for role in SPECIALISTS)
        )
        findings = dict(zip(SPECIALISTS, results, strict=True))

    # coordinator LLM decides among issues a specialist actually reported (after grounding);
    # rules only act as an independent verifier that can object once
    allowed = sorted(
        {f["issue"] for f in findings.values() if f and f.get("issue")} | _ALWAYS_ALLOWED
    )
    payload = {
        **brief,
        "specialist_findings": findings,
        "allowed_issues": allowed,
        "evidence": clip({k: v for k, v in scoped.items() if k != "policy_rules"}),
        "policy_issues": sorted(scoped["policy_rules"]),
    }
    proposal, rejected = _within(await agents.run("coordinator", payload, []), allowed)
    issue = (proposal or {}).get("primary_issue") or "insufficient_evidence"
    confidence = (proposal or {}).get("confidence", 0.5)
    state.emit("policy_decided", "coordinator", decision_code=issue,
               evidence_refs=state.ref("get_policy"),
               attributes={"rejected_issue": rejected} if rejected else None)
    state.emit("task_assigned", "coordinator", target="verifier", decision_code="VERIFY_OUTPUT")
    rules_issue = analyze_case(case, state.ev)["assessment"]["primary_issue"]
    decision = "PASS"
    if issue != rules_issue:
        state.emit("verification_completed", "verifier", target="coordinator",
                   decision_code="ISSUE_MISMATCH", attributes={"rules_issue": rules_issue})
        state.emit("handoff", "verifier", target="coordinator", decision_code="REVISE")
        allowed_now = sorted({*allowed, rules_issue})
        revised, rejected_rev = _within(await agents.run("coordinator", {
            **payload, "allowed_issues": allowed_now,
            "your_answer": issue, "verifier_objection": rules_issue,
        }, []), allowed_now)
        if revised and revised.get("primary_issue"):
            issue, confidence = revised["primary_issue"], revised["confidence"]
        if issue != rules_issue and (proposal or revised):
            # persistent disagreement: fail closed, never act (refund) on an unverified issue.
            # (LLM unreachable is not a disagreement: that case stays insufficient_evidence)
            state.emit("handoff", "coordinator", target="verifier",
                       decision_code="ESCALATED_TO_VERIFIER",
                       attributes={"llm_issue": rejected_rev or issue})
            issue, confidence, decision = rules_issue, 0.55, "PASS_RULES_OVER_LLM"
        state.emit("policy_decided", "coordinator", decision_code=issue,
                   evidence_refs=state.ref("get_policy"))

    # policy mapping/refund lines are derived mechanically from the agreed issue
    if issue == "unsupported_claim" and state.order_id:
        # required evidence for an unsupported claim (product domain); one call, only here
        state.emit("task_assigned", "coordinator", target="order-agent",
                   decision_code="COLLECT_PRODUCT_CONTEXT")
        await state.call("get_product_context")
        state.emit("handoff", "order-agent", target="coordinator",
                   decision_code="PRODUCT_CONTEXT_COLLECTED"
                   if state.ev.get("get_product_context") else "PRODUCT_CONTEXT_MISSING",
                   evidence_refs=state.ref("get_product_context"))
    output = analyze_case(case, state.ev, issue_override=issue)
    final_issue = output["assessment"]["primary_issue"]
    if final_issue == issue:
        if decision == "PASS_RULES_OVER_LLM":
            output["assessment"]["confidence"] = confidence
        else:
            votes = sum(1 for f in findings.values() if f and f.get("issue") == issue)
            output["assessment"]["confidence"] = _calibrate(confidence, votes)
    problems = _check(state, output)
    if problems:
        decision = "FAIL_" + "_".join(problems)[:70]
    state.emit("verification_completed", "verifier", target="coordinator",
               decision_code=decision, evidence_refs=output["evidence_refs"][:20] or None)
    state.emit("handoff", "verifier", target="coordinator", decision_code=final_issue)
    return output
