"""OpenAI tool-calling loop with host-executed MCP tools and structured outputs.

The LLM chooses *which* allowed tool to call and with which argument; the host executes
the real MCP call (always with the host-held case_id), stores the evidence and returns a
version-filtered view tagged with a local id. The LLM never sees or writes evidence_ref.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from .config import Settings
from .evidence import MCP_TOOLS

TEMPERATURE = 0
MAX_ITERATIONS = 4  # tool rounds per agent before it must answer
REQUEST_TIMEOUT_S = 90.0

_client: AsyncOpenAI | None = None
_model: str = "gpt-4o-mini"


def client() -> tuple[AsyncOpenAI, str]:
    global _client, _model
    if _client is None:
        settings = Settings.load(Path.cwd())
        settings.require_openai()
        _client = AsyncOpenAI(
            api_key=settings.openai_api_key, timeout=REQUEST_TIMEOUT_S, max_retries=3
        )
        _model = settings.openai_model
    return _client, _model


def tool_specs(names: frozenset[str]) -> list[dict[str, Any]]:
    specs = []
    for name in sorted(names):
        arg, description = MCP_TOOLS[name]
        specs.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "strict": True,
                    "parameters": {
                        "type": "object",
                        "properties": {arg: {"type": "string"}},
                        "required": [arg],
                        "additionalProperties": False,
                    },
                },
            }
        )
    return specs


@dataclass
class Usage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    per_agent: dict[str, int] = field(default_factory=dict)

    def add(self, actor: str, usage: Any) -> None:
        self.calls += 1
        self.per_agent[actor] = self.per_agent.get(actor, 0) + 1
        if usage is not None:
            self.prompt_tokens += usage.prompt_tokens or 0
            self.completion_tokens += usage.completion_tokens or 0


ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


async def run_agent(
    *,
    actor: str,
    system: str,
    user: dict[str, Any],
    schema_name: str,
    schema: dict[str, Any],
    usage: Usage,
    tools: frozenset[str] = frozenset(),
    execute: ToolExecutor | None = None,
    require_tool_first: bool = False,
    max_iterations: int = MAX_ITERATIONS,
) -> dict[str, Any] | None:
    """Run one agent to a schema-valid JSON answer; None when it cannot answer in budget."""
    openai, model = client()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False, default=str)},
    ]
    response_format = {
        "type": "json_schema",
        "json_schema": {"name": schema_name, "strict": True, "schema": schema},
    }
    specs = tool_specs(tools) if tools else []
    for iteration in range(max_iterations + 1):
        kwargs: dict[str, Any] = {
            "model": model,
            "temperature": TEMPERATURE,
            "messages": messages,
            "response_format": response_format,
        }
        if specs:
            kwargs["tools"] = specs
            if iteration == max_iterations:
                kwargs["tool_choice"] = "none"  # budget exhausted: answer now
            elif iteration == 0 and require_tool_first:
                kwargs["tool_choice"] = "required"
            else:
                kwargs["tool_choice"] = "auto"
        response = await openai.chat.completions.create(**kwargs)
        usage.add(actor, response.usage)
        message = response.choices[0].message
        if message.tool_calls and specs and execute is not None:
            messages.append(message.model_dump(exclude_none=True))
            for call in message.tool_calls:
                name = call.function.name
                try:
                    arguments = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                if name not in tools:  # never execute a tool outside the allow-list
                    result: dict[str, Any] = {"error": "tool_not_permitted"}
                else:
                    result = await execute(name, arguments)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(result, ensure_ascii=False, default=str),
                    }
                )
            continue
        if message.refusal or not message.content:
            return None
        try:
            return json.loads(message.content)
        except json.JSONDecodeError:
            return None
    return None
