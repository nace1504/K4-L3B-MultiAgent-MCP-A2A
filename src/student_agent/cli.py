from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path

from .analysis import analyze_case
from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .llm_agents import LLMAgents
from .mcp_gateway import connect_gateway
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case


def _root(value: str) -> Path:
    return Path(value).resolve()


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool in await gateway.list_tools():
            print(tool)


async def _run(root: Path) -> None:
    settings = Settings.load(root)
    if LLMAgents.from_env() is None:
        raise RuntimeError("OPENAI_API_KEY missing: the workflow is decided by LLM agents")
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    # DAY09_RESUME=1 keeps finalized cases so a dropped connection doesn't re-spend audited calls
    done: set[str] = set()
    if os.environ.get("DAY09_RESUME") and trace_path.exists():
        events = []
        for line in trace_path.read_text(encoding="utf-8").splitlines():
            with contextlib.suppress(ValueError):  # half-written last line after a crash
                events.append(json.loads(line))
        done = {
            e["case_id"] for e in events
            if e["event_type"] == "case_finalized"
            and (output_root / f"{e['case_id']}.json").exists()
        }
        kept = [json.dumps(e, ensure_ascii=False) for e in events if e["case_id"] in done]
        trace_path.write_text("".join(line + "\n" for line in kept), encoding="utf-8")
    else:
        trace_path.unlink(missing_ok=True)
    for stale in output_root.glob("*.json"):
        if stale.stem not in done:
            stale.unlink()
    trace = TraceWriter(trace_path, contracts)

    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        discovered_tools = await gateway.list_tools()
        if not discovered_tools:
            raise RuntimeError("MCP Gateway returned no tools")
        only = {c for c in os.environ.get("DAY09_CASES", "").split(",") if c}
        # keep raw evidence so prompts can be iterated offline (scripts/replay_llm.py)
        # without spending audited MCP calls; debug/ is gitignored and never packaged
        dump_dir = Path(os.environ.setdefault("DAY09_DUMP_DIR", str(root / "debug")))
        # cases are independent (evidence never shared), so run a bounded number at once
        limit = asyncio.Semaphore(max(1, int(os.environ.get("DAY09_CONCURRENCY", "4"))))
        pending = [
            case_id for case_id in case_set.case_ids
            if case_id not in done and not (only and case_id not in only)
        ]
        for case_id in pending:  # dumps append, so drop stale ones of cases being re-run
            (dump_dir / f"{case_id}.jsonl").unlink(missing_ok=True)

        async def solve_one(case_id: str) -> None:
            case = case_set.cases[case_id]
            trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
            try:
                async with limit:
                    output = await solve_case(case, gateway, trace)
                contracts.validate_output(output, f"outputs/{case_id}.json")
            except Exception as exc:  # one bad case must not sink the other 99
                print(f"WARN: {case_id} fell back: {exc!r}", file=sys.stderr)
                output = analyze_case(case, {})
                contracts.validate_output(output, f"outputs/{case_id}.json")
            if output.get("case_id") != case_id:
                raise ValueError(f"solver returned a mismatched case_id for {case_id}")
            target = output_root / f"{case_id}.json"
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            temporary.replace(target)
            trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")

        await asyncio.gather(*(solve_one(case_id) for case_id in pending))

def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3B student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    commands.add_parser("run", help="run the implemented workflow for all cases")
    commands.add_parser("validate", help="validate outputs and observable trace")
    package = commands.add_parser("package", help="validate and build the submission ZIP")
    package.add_argument("--output", default="dist/submission.zip")
    return result


def main() -> None:
    args = parser().parse_args()
    root = _root(args.root)
    try:
        if args.command == "validate-inputs":
            case_set = load_case_set(root)
            print(
                f"OK: {case_set.variant_id} / {case_set.version} / "
                f"{len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root))
        elif args.command == "run":
            asyncio.run(_run(root))
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
            _, trace = validate_artifacts(root, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "package":
            destination = package_submission(root, root / args.output)
            print(f"OK: {destination}")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
