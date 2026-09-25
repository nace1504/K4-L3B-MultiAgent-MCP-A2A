from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx2
from mcp.shared.exceptions import MCPError

from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
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


async def _run(root: Path, resume: bool = False) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    if resume:
        # Keep finished cases (output + case_finalized); drop partial trace of the rest.
        done = {p.stem for p in output_root.glob("*.json")} & _finalized_cases(trace_path)
        for case_id in case_set.case_ids:
            if case_id not in done:
                (output_root / f"{case_id}.json").unlink(missing_ok=True)
                _drop_case_events(trace_path, case_id)
        pending = [c for c in case_set.case_ids if c not in done]
        print(f"resume: {len(done)} done, {len(pending)} pending", file=sys.stderr)
    else:
        for stale in output_root.glob("*.json"):
            stale.unlink()
        trace_path.unlink(missing_ok=True)
        pending = list(case_set.case_ids)
    trace = TraceWriter(trace_path, contracts)

    attempts: dict[str, int] = {}
    while pending:
        try:
            async with connect_gateway(
                settings.mcp_endpoint, settings.team_api_key, contracts
            ) as gateway:
                discovered_tools = await gateway.list_tools()
                if not discovered_tools:
                    raise RuntimeError("MCP Gateway returned no tools")
                while pending:
                    case_id = pending[0]
                    case = case_set.cases[case_id]
                    trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
                    output = await solve_case(case, gateway, trace)
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
                    pending.pop(0)
        except Exception as exc:
            # A dropped MCP connection aborts the whole session: reconnect and redo only the
            # interrupted case, after removing its partial trace events.
            case_id = pending[0]
            attempts[case_id] = attempts.get(case_id, 0) + 1
            if not _is_transport_error(exc) or attempts[case_id] > MAX_CASE_RECONNECTS:
                raise
            _drop_case_events(trace_path, case_id)
            print(f"WARN: MCP connection lost during {case_id}; reconnecting", file=sys.stderr)
            await asyncio.sleep(2.0 * attempts[case_id])


MAX_CASE_RECONNECTS = 3


def _is_transport_error(exc: BaseException) -> bool:
    if isinstance(exc, BaseExceptionGroup):
        return any(_is_transport_error(inner) for inner in exc.exceptions)
    if isinstance(exc, (httpx2.TransportError, OSError, TimeoutError, MCPError)):
        return True
    cause = exc.__cause__ or exc.__context__
    return cause is not None and cause is not exc and _is_transport_error(cause)


def _finalized_cases(trace_path: Path) -> set[str]:
    if not trace_path.exists():
        return set()
    return {
        event["case_id"]
        for line in trace_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and (event := json.loads(line)).get("event_type") == "case_finalized"
    }


def _drop_case_events(trace_path: Path, case_id: str) -> None:
    if not trace_path.exists():
        return
    kept = [
        line
        for line in trace_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line).get("case_id") != case_id
    ]
    trace_path.write_text("".join(f"{line}\n" for line in kept), encoding="utf-8")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3B student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    run = commands.add_parser("run", help="run the implemented workflow for all cases")
    run.add_argument(
        "--resume", action="store_true", help="keep finished cases, run only missing ones"
    )
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
            asyncio.run(_run(root, resume=args.resume))
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
