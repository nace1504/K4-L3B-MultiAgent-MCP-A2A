"""Replay the full LLM workflow over saved MCP dumps (debug/*.jsonl): no MCP calls.

Usage (needs OPENAI_API_KEY in .env; outputs/ and traces/ are not touched):
    python scripts/replay_llm.py                       # all dumped cases
    python scripts/replay_llm.py L3B_CASE_002 L3B_CASE_007
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from student_agent.cases import load_case_set  # noqa: E402
from student_agent.contracts import Contracts  # noqa: E402
from student_agent.trace import TraceWriter  # noqa: E402
from student_agent.workflow import solve_case  # noqa: E402


class ReplayGateway:
    """Serves the recorded envelope (or recorded failure) for each tool of one case."""

    def __init__(self, dump: Path) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        for line in dump.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                self.records[record["tool"]] = record

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        record = self.records.get(tool_name)
        if record is None:
            raise RuntimeError(f"{tool_name} was not recorded for {case_id}")
        if not record.get("ok"):
            raise RuntimeError(record.get("error", "recorded failure"))
        return record["evidence"]


async def main(selected: list[str]) -> None:
    load_dotenv(ROOT / ".env")
    case_set = load_case_set(ROOT)
    contracts = Contracts(ROOT / "contracts" / "schemas")
    dumps = ROOT / "debug"
    case_ids = selected or [c for c in case_set.case_ids if (dumps / f"{c}.jsonl").exists()]
    trace_path = Path(tempfile.mkdtemp()) / "trace.jsonl"
    trace = TraceWriter(trace_path, contracts)
    limit = asyncio.Semaphore(4)

    async def one(case_id: str) -> tuple[str, str, float]:
        async with limit:
            gateway = ReplayGateway(dumps / f"{case_id}.jsonl")
            output = await solve_case(case_set.cases[case_id], gateway, trace)  # type: ignore[arg-type]
        return case_id, output["assessment"]["primary_issue"], output["assessment"]["confidence"]

    results = await asyncio.gather(*(one(c) for c in case_ids))
    verdicts = {}
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event["event_type"] == "verification_completed":
            verdicts[event["case_id"]] = event["decision_code"]  # last one wins
    for case_id, issue, confidence in results:
        print(f"{case_id:<16} {issue:<26} {confidence:<5} {verdicts.get(case_id, '')}")
    print("\nissues:", dict(Counter(issue for _, issue, _ in results)))
    print("verifier:", dict(Counter(verdicts.values())))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
