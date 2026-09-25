import json
from typing import Any

import pytest

from student_agent.llm_agents import LLMAgents


class _Reply:
    def __init__(self, content: dict[str, Any]) -> None:
        self._content = content

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return {"choices": [{"message": {"content": json.dumps(self._content)}}]}


class DeferringLLM:
    """Offline stand-in: specialists report nothing, coordinator adopts the verifier objection."""

    async def post(self, url: str, headers: dict[str, str], json: dict[str, Any]) -> _Reply:
        import json as _json

        payload = _json.loads(json["messages"][1]["content"])
        return _Reply({"primary_issue": payload.get("verifier_objection"), "issue": None})


@pytest.fixture(autouse=True)
def _no_real_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    # tests must never reach OpenAI
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        "student_agent.workflow._AGENTS", LLMAgents("sk-test", "fake", client=DeferringLLM())
    )
