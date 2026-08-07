#!/usr/bin/env python3
"""Merge local and remote Oracle lane results without losing better evidence."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def state(row: dict[str, Any]) -> str:
    oracle = str(row.get("oracle_result") or "")
    status = str(row.get("status") or "")
    disposition = str(row.get("disposition") or "")
    if oracle == "PASS_TO_PASS" or status == "strict_pass" or disposition == "accept":
        return "pass"
    if (
        oracle == "STRUCTURAL_REJECT"
        or status.startswith("structural")
        or disposition == "structural"
    ):
        return "structural"
    if (
        oracle == "BLOCKED"
        or status.startswith("blocked")
        or disposition in {"blocked", "environment_blocked"}
    ):
        return "blocked"
    return "recoverable"


def evidence_score(row: dict[str, Any]) -> int:
    score = 0
    for key in (
        "golden_correctness_ok",
        "changed_lines_covered",
        "stable_p2p",
        "triple_run_stable",
        "stable_start_pass",
        "stable_golden_pass",
        "release_eligible",
    ):
        score += 10 if row.get(key) is True else 0
    score += min(int(row.get("pass_to_pass") or 0), 20)
    score += len(row.get("pass_to_pass_tests") or [])
    score += len(row.get("runs") or {})
    score += len(row.get("artifacts") or {})
    return score


def preference(row: dict[str, Any]) -> tuple[int, int, int]:
    rank = {"recoverable": 1, "blocked": 2, "structural": 3, "pass": 4}
    return rank[state(row)], evidence_score(row), len(json.dumps(row, sort_keys=True))


def main() -> int:
    args = parse_args()
    merged: dict[str, dict[str, Any]] = {}
    provenance: dict[str, str] = {}
    for path in args.input:
        for row in load_jsonl(path):
            task_id = str(row.get("id") or "")
            if not task_id:
                continue
            if task_id not in merged or preference(row) > preference(merged[task_id]):
                merged[task_id] = row
                provenance[task_id] = str(path)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w") as stream:
        for task_id in sorted(merged):
            row = {**merged[task_id], "merged_results_source": provenance[task_id]}
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    counts = {}
    for row in merged.values():
        counts[state(row)] = counts.get(state(row), 0) + 1
    print(json.dumps({"records": len(merged), "states": counts}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
