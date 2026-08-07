#!/usr/bin/env python3
"""Merge per-release model evaluation results into one cumulative leaderboard.

Each input is a `results.jsonl` produced by `summarize_release_evaluation.py`:
one row per (task, model) with the four gate outcomes already decided. This
command only aggregates; it never re-decides a gate, so a frozen release
evaluation keeps exactly the numbers it was published with.

Example:

    python scripts/merge_evaluation_reports.py \
      --results data/releases/reserves/20260721/evaluations/agentic-three-model-20260725/results.jsonl \
      --results data/releases/reserves/20260728/evaluations/agentic-three-model-20260729/results.jsonl \
      --out /tmp/cumulative_results.jsonl \
      --report /tmp/cumulative_report.json
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any

SUMMARIZER = Path(__file__).resolve().parent / "summarize_release_evaluation.py"


def load_summarizer():
    spec = importlib.util.spec_from_file_location("summarize_release", SUMMARIZER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", type=Path, action="append", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    return parser.parse_args()


def load_rows(paths: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for path in paths:
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            key = (row["id"], row["model"])
            if key in seen:
                raise ValueError(f"duplicate result for {key} in {path}")
            seen.add(key)
            rows.append(row)
    return rows


def main() -> int:
    args = parse_args()
    module = load_summarizer()
    rows = load_rows(args.results)
    models = sorted({row["model"] for row in rows})
    task_ids = {row["id"] for row in rows}
    incomplete = [
        task_id
        for task_id in sorted(task_ids)
        if len({row["model"] for row in rows if row["id"] == task_id}) != len(models)
    ]
    rows.sort(key=lambda row: (row["id"], row["model"]))
    with args.out.open("w") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    report = {
        "schema_version": "reviewbench-model-evaluation/cumulative-v1",
        "sources": [str(path) for path in args.results],
        "tasks": len(task_ids),
        "task_model_pairs": len(rows),
        "models": models,
        "tasks_missing_a_model": incomplete,
        "leaderboard": {
            model: module.summarize_model(rows, model) for model in models
        },
        "pairwise": module.pairwise(rows, models),
    }
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    for model in models:
        entry = report["leaderboard"][model]
        print(
            f"{model:20s} resolved={entry['resolved']:4d}/{entry['tasks']:<4d} "
            f"rate={entry['resolved_rate']:.4f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
