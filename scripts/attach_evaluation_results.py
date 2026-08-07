#!/usr/bin/env python3
"""Attach normalized model-evaluation results to rendered review items."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def model_result(row: dict[str, Any]) -> dict[str, Any]:
    verdict = row.get("judge_verdict") or {}
    return {
        "resolved": row.get("resolved") is True,
        "correctness_ok": row.get("correctness_ok") is True,
        "scope_ok": row.get("scope_ok") is True,
        "maintainability": verdict.get("addressed"),
        "maintainability_quality_1_to_5": verdict.get("quality_1_to_5"),
        "judge_reason": verdict.get("reason"),
        "generation_apply": row.get("generation_apply") is True,
        "changed_lines": row.get("changed_lines"),
        "generation_protocol": row.get("generation_protocol"),
        "failure_reasons": list(row.get("failure_reasons") or []),
    }


def attach_results(
    items: list[dict[str, Any]],
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        task_id = str(row.get("id") or "")
        model = str(row.get("model") or "")
        if not task_id or not model:
            raise ValueError("every result requires id and model")
        model_key = f"model:{model}"
        task_results = by_id.setdefault(task_id, {})
        if model_key in task_results:
            raise ValueError(f"duplicate result for {(task_id, model)}")
        task_results[model_key] = model_result(row)

    item_ids = {str((item.get("task") or {}).get("id") or "") for item in items}
    result_ids = set(by_id)
    if "" in item_ids:
        raise ValueError("every review item requires task.id")
    if item_ids != result_ids:
        raise ValueError(
            f"item/result task mismatch: missing={sorted(item_ids - result_ids)} "
            f"unknown={sorted(result_ids - item_ids)}"
        )

    for item in items:
        task = item["task"]
        task_id = str(task["id"])
        task["model_results"] = by_id[task_id]
        task["evaluation_protocols"] = sorted(
            {
                str(result.get("generation_protocol") or "")
                for result in by_id[task_id].values()
                if result.get("generation_protocol")
            }
        )
    return items


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--items", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    items = json.loads(args.items.read_text(encoding="utf-8"))
    if not isinstance(items, list):
        raise ValueError("--items must contain a JSON list")
    attached = attach_results(items, load_jsonl(args.results))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.out.with_suffix(args.out.suffix + ".tmp")
    temporary.write_text(
        json.dumps(attached, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    temporary.replace(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
