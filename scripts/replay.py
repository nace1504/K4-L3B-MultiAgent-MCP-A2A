"""Replay analysis against real debug dumps and optionally write outputs."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from student_agent.analysis import analyze_case  # noqa: E402
from student_agent.cases import load_case_set  # noqa: E402
from student_agent.contracts import Contracts  # noqa: E402


def load_ev(debug_dir: Path, case_id: str) -> dict:
    path = debug_dir / f"{case_id}.jsonl"
    ev: dict = {}
    if not path.exists():
        return ev
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        tool = rec["tool"]
        if rec.get("ok"):
            ev[tool] = rec["evidence"]
        else:
            ev[tool] = None
    return ev


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    case_set = load_case_set(ROOT)
    contracts = Contracts(ROOT / "contracts" / "schemas")
    debug_dir = ROOT / "debug"
    out_dir = ROOT / "outputs"

    counter: Counter[str] = Counter()
    rows: list[tuple[str, str, str]] = []

    for case_id in case_set.case_ids:
        case = case_set.cases[case_id]
        ev = load_ev(debug_dir, case_id)
        result = analyze_case(case, ev)
        issue = result["assessment"]["primary_issue"]

        try:
            contracts.validate_output(result, case_id)
            valid = "OK"
        except Exception as exc:
            valid = f"ERR: {exc}"

        claim_topics = [
            c.get("topic", "?")
            for c in (case.get("customer_request") or {}).get("claims", [])
            if isinstance(c, dict)
        ]
        topic = claim_topics[0] if claim_topics else "?"

        counter[issue] += 1
        rows.append((case_id, topic, issue, valid))

    # Per-case table
    print(f"{'case_id':<18} {'claim_topic':<28} {'primary_issue':<28} {'schema'}")
    print("-" * 100)
    for case_id, topic, issue, valid in rows:
        match = "==" if topic == issue else "!="
        print(f"{case_id:<18} {topic:<28} {issue:<28} {match}  {valid}")

    print()
    print("Distribution:")
    for issue, cnt in sorted(counter.items(), key=lambda x: -x[1]):
        print(f"  {issue}: {cnt}")

    if args.write:
        out_dir.mkdir(exist_ok=True)
        for case_id in case_set.case_ids:
            case = case_set.cases[case_id]
            ev = load_ev(debug_dir, case_id)
            result = analyze_case(case, ev)
            out_path = out_dir / f"{case_id}.json"
            out_path.write_text(
                json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        print(f"\nWrote {len(case_set.case_ids)} outputs to {out_dir}/")


if __name__ == "__main__":
    main()
