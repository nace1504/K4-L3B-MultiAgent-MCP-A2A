"""LLM agents: OpenAI chat completions with function calling, over httpx2 (no extra dependency).

Each agent only gets its role's tools. It never sees or supplies case_id, and its text is never
used as an evidence_ref: the workflow executor owns every MCP call and every ref.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from typing import Any

import httpx2

ISSUES = [
    "late_delivery_seller",
    "late_delivery_logistics",
    "duplicate_charge",
    "payment_mismatch",
    "valid_split_payment",
    "refund_pending",
    "refund_failed",
    "canceled_order_paid",
    "unavailable_order_paid",
    "unsupported_claim",
    "insufficient_evidence",
]

_TOOL_DESC = {
    "get_customer_history": "Order history for the case's customer (identity is fixed).",
    "get_order": "Authoritative order row for one order_id from the customer's history.",
    "get_order_items": "Item and seller rows of the resolved order.",
    "get_product_context": "Products and categories of the resolved order.",
    "get_shipment_summary": "Delivery timestamps, seller shipping limits and shipment events.",
    "get_payment_timeline": "Payment rows and payment lifecycle events (captures, refunds).",
    "get_refund_timeline": "Refund lifecycle events. Only useful if a refund was requested/issued.",
    "get_policy": "Machine-readable refund/resolution policy for the case.",
}

_SCOPED = (
    " The evidence is already fetched and restricted to the incident window (decoy snapshots "
    "removed); missing_tools lists tools that failed. Confirmed lifecycle events override base "
    "rows. `facts` are exact computations over that evidence: trust them over your own date or "
    "amount arithmetic. The customer's claim is only a hint. Most cases have no problem in your "
    "domain: answer issue null unless the evidence clearly shows one."
)
# each specialist may only report issues of its own domain; anything else is dropped
DOMAIN_ISSUES = {
    "order-agent": ["canceled_order_paid", "unavailable_order_paid"],
    "shipment-agent": ["late_delivery_seller", "late_delivery_logistics"],
    "payment-agent": [
        "duplicate_charge", "payment_mismatch", "valid_split_payment",
        "refund_pending", "refund_failed",
    ],
}


def _finding(role: str) -> str:
    return (
        ' Reply JSON: {"issue": one of ' + json.dumps(DOMAIN_ISSUES[role])
        + ' or null, "confidence": 0..1, "finding": "<=200 chars"}.'
    )


_ROLES = {
    "order-agent": "You are the order agent. Report canceled_order_paid / unavailable_order_paid "
    "only when the incident order status is canceled / unavailable AND a payment was captured "
    "(facts.order_status, facts.captured_total_brl)." + _SCOPED + _finding("order-agent"),
    "shipment-agent": "You are the shipment agent. late_delivery_seller: the seller handed the "
    "parcel to the carrier after its shipping limit (facts.carrier_after_shipping_limit or a "
    "confirmed delivered_late event with actor seller). late_delivery_logistics: delivered after "
    "the estimate (facts.delivered_after_estimate or a confirmed delivered_late event with actor "
    "logistics_provider). Otherwise null." + _SCOPED + _finding("shipment-agent"),
    "payment-agent": "You are the payment agent. duplicate_charge: the same payment "
    "(sequential, amount) captured twice. payment_mismatch: a reconciliation_mismatch event. "
    "valid_split_payment: several captures of different payment types, each unique, no "
    "mismatch — a legitimate split, not a problem to refund. refund_pending / refund_failed: "
    "latest status of a refund." + _SCOPED + _finding("payment-agent"),
    "coordinator": "You are the coordinator. Pick exactly one primary issue from the specialist "
    "findings and the scoped evidence. primary_issue MUST be one of allowed_issues: the issues "
    "specialists verified, plus unsupported_claim and insufficient_evidence; any other answer "
    "is discarded. A specialist reporting null means its domain is fine. "
    "Priority when several hold: canceled/unavailable paid order > refund_failed > "
    "refund_pending > duplicate_charge > payment_mismatch > valid_split_payment > late delivery. "
    "If nothing is supported answer unsupported_claim; if required evidence is missing answer "
    "insufficient_evidence. If a verifier_objection is present, re-check both against `facts` "
    "and answer the one the facts support. "
    'Reply JSON: {"primary_issue": one of ISSUES, "confidence": 0..1, "finding": "<=200 chars"}.',
}

_MAX_TEXT = 6000

def clip(value: Any) -> str:
    # ponytail: hard clip keeps prompts cheap; summarise per domain if clipping hurts accuracy
    return json.dumps(value, ensure_ascii=False, default=str)[:_MAX_TEXT]

def _tool_spec(name: str) -> dict[str, Any]:
    props = (
        {} if name in ("get_customer_history", "get_policy")
        else {"order_id": {"type": "string"}}
    )
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": _TOOL_DESC[name],
            "parameters": {"type": "object", "properties": props, "required": list(props)},
        },
    }

Executor = Callable[[str, dict[str, Any]], Awaitable[str]]

class LLMAgents:
    def __init__(
        self,
        api_key: str,
        model: str,
        client: Any | None = None,
        base_url: str = "https://api.openai.com/v1",
    ) -> None:
        self._api_key = api_key
        self.model = model
        self._url = base_url.rstrip("/") + "/chat/completions"
        # ponytail: client lives for the process; close it if agents become long-lived
        self._client = client or httpx2.AsyncClient(timeout=60.0)

    @classmethod
    def from_env(cls) -> LLMAgents | None:
        # any OpenAI-compatible endpoint works (vLLM/Ollama/OpenRouter) -> pick a <10B model there
        key = os.getenv("OPENAI_API_KEY", "").strip()
        if not key:
            return None
        return cls(
            key,
            os.getenv("OPENAI_MODEL", "").strip() or "gpt-4o-mini",
            base_url=os.getenv("OPENAI_BASE_URL", "").strip() or "https://api.openai.com/v1",
        )

    async def _chat(
        self, messages: list[dict[str, Any]], tools: list[str], final: bool
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": messages,
        }
        if tools:
            body["tools"] = [_tool_spec(name) for name in tools]
            if final:
                body["tool_choice"] = "none"
        for attempt in range(3):
            response = await self._client.post(
                self._url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=body,
            )
            # cases run concurrently: back off on rate limits / transient server errors
            if getattr(response, "status_code", 200) in (429, 500, 502, 503) and attempt < 2:
                await asyncio.sleep(2 ** attempt)
                continue
            response.raise_for_status()
            return response.json()["choices"][0]["message"]
        raise RuntimeError("unreachable")

    async def run(
        self,
        role: str,
        payload: dict[str, Any],
        tools: list[str],
        executor: Executor | None = None,
        max_steps: int = 4,
    ) -> dict[str, Any] | None:
        """Tool-use loop. Returns the agent's normalised JSON answer, or None on any failure."""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _ROLES[role] + " ISSUES = " + json.dumps(ISSUES)},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
        ]
        try:
            for step in range(max_steps + 1):
                message = await self._chat(messages, tools, final=step == max_steps)
                calls = message.get("tool_calls") or []
                if not calls or executor is None:
                    return _normalise(json.loads(message.get("content") or "{}"), role)
                messages.append(
                    {"role": "assistant", "content": message.get("content"), "tool_calls": calls}
                )
                for call in calls:
                    try:
                        arguments = json.loads(call["function"].get("arguments") or "{}")
                    except ValueError:
                        arguments = {}
                    result = await executor(call["function"]["name"], arguments)
                    messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
        except Exception:
            return None
        return None

def _normalise(answer: Any, role: str = "") -> dict[str, Any] | None:
    if not isinstance(answer, dict):
        return None
    for key in ("issue", "primary_issue"):
        if key in answer and answer[key] not in DOMAIN_ISSUES.get(role, ISSUES):
            answer[key] = None
    try:
        answer["confidence"] = min(1.0, max(0.0, float(answer.get("confidence", 0.5))))
    except (TypeError, ValueError):
        answer["confidence"] = 0.5
    answer["finding"] = str(answer.get("finding", ""))[:200]
    return answer
