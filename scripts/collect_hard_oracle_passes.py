#!/usr/bin/env python3
"""Collect strict Oracle passes from independently engineered language lanes."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--lane-results", type=Path, action="append", required=True)
    parser.add_argument("--out-tasks", type=Path, required=True)
    parser.add_argument("--out-oracles", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def strict_pass(oracle: dict[str, Any]) -> bool:
    def stable(value: Any) -> bool:
        if value is True:
            return True
        if type(value) is int:
            return value >= 3
        if isinstance(value, list):
            return len(value) >= 3 and all(
                not isinstance(item, dict) or item.get("exit_code") == 0
                for item in value
            )
        return False

    start_stable = (
        stable(oracle.get("start_runs"))
        or oracle.get("stable_start_pass") is True
        or oracle.get("stable_start") is True
        or oracle.get("start_stable") is True
    )
    golden_stable = (
        stable(oracle.get("golden_runs"))
        or stable(oracle.get("source_only_golden_runs"))
        or oracle.get("stable_golden_pass") is True
        or oracle.get("stable_source_only_golden") is True
        or oracle.get("golden_stable") is True
    )
    explicitly_triple = oracle.get("triple_run_stable") is True
    stable_p2p = (
        oracle.get("stable_p2p") is True
        or explicitly_triple
        or (start_stable and golden_stable)
    )
    passed_state = (
        oracle.get("oracle_result") == "PASS_TO_PASS"
        or oracle.get("status") == "strict_pass"
        or oracle.get("disposition") == "accept"
    )
    return (
        passed_state
        and oracle.get("golden_correctness_ok") is True
        and oracle.get("stable_p2p") is not False
        and (explicitly_triple or (start_stable and golden_stable))
        and stable_p2p
        and type(oracle.get("pass_to_pass")) is int
        and oracle["pass_to_pass"] > 0
        and oracle.get("changed_lines_covered") is True
    )


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    args = parse_args()
    tasks = {row["id"]: row for row in load_jsonl(args.tasks)}
    oracles: dict[str, dict[str, Any]] = {}
    statuses: Counter[str] = Counter()
    unknown = []
    duplicates = []
    for path in args.lane_results:
        for oracle in load_jsonl(path):
            task_id = str(oracle.get("id") or "")
            if task_id not in tasks:
                unknown.append({"id": task_id, "source": str(path)})
                continue
            if task_id in oracles:
                duplicates.append({"id": task_id, "source": str(path)})
                continue
            oracle = {**oracle, "lane_results_source": str(path)}
            oracles[task_id] = oracle
            statuses[str(oracle.get("oracle_result") or "MISSING_STATUS")] += 1

    passed_ids = sorted(task_id for task_id, row in oracles.items() if strict_pass(row))
    passed_tasks = [tasks[task_id] for task_id in passed_ids]
    passed_oracles = [oracles[task_id] for task_id in passed_ids]
    write_jsonl(args.out_tasks, passed_tasks)
    write_jsonl(args.out_oracles, passed_oracles)
    report = {
        "schema_version": "reviewbench-hard-oracle-collection/v1",
        "input_tasks": len(tasks),
        "oracle_records": len(oracles),
        "missing_oracle_records": len(tasks) - len(oracles),
        "strict_pass": len(passed_ids),
        "statuses": dict(statuses),
        "unknown_records": unknown,
        "duplicate_records": duplicates,
        "tasks_output": str(args.out_tasks),
        "tasks_sha256": digest(args.out_tasks),
        "oracles_output": str(args.out_oracles),
        "oracles_sha256": digest(args.out_oracles),
    }
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if not unknown and not duplicates else 1


if __name__ == "__main__":
    raise SystemExit(main())
