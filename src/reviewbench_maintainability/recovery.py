"""Create a persistent recovery queue for quality tasks that are not yet runnable."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from typing import Any

from .release import (
    coverage_result,
    golden_evaluation,
    load_evaluations,
    load_jsonl_by_id,
    pass_to_pass_count,
)


RECOVERY_SCHEMA_VERSION = "reviewbench-recovery/v1"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--evaluations", type=Path, nargs="+", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    tasks = load_jsonl_by_id(args.tasks)
    evaluations = load_evaluations(args.evaluations)
    queue = []
    final_count = 0
    for task_id, task in tasks.items():
        if (task.get("quality_gate") or {}).get("passed") is not True:
            continue
        evaluation = evaluations.get(task_id)
        status, detail = recovery_status(evaluation)
        if status == "strict_pass":
            final_count += 1
            continue
        queue.append(
            {
                "schema_version": RECOVERY_SCHEMA_VERSION,
                "id": task_id,
                "repo": task.get("repo"),
                "language": task.get("inferred_language"),
                "difficulty": [
                    (judgment.get("verdict") or {}).get("intrinsic_difficulty")
                    for judgment in task.get("quality_judgments") or []
                ],
                "recovery_status": status,
                "detail": detail,
                "attempt_count": 0,
                "resolved": False,
            }
        )
    queue.sort(key=lambda item: (recovery_priority(item["recovery_status"]), item["id"]))
    write_jsonl(args.out, queue)
    report = {
        "quality_tasks": sum(
            (task.get("quality_gate") or {}).get("passed") is True
            for task in tasks.values()
        ),
        "strict_pass": final_count,
        "queued": len(queue),
        "statuses": dict(Counter(item["recovery_status"] for item in queue).most_common()),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def recovery_status(
    evaluation: dict[str, Any] | None,
) -> tuple[str, str | None]:
    if not evaluation:
        return "not_validated", None
    error = str(evaluation.get("setup_error") or "")
    lowered = error.lower()
    if error:
        if "test_anchor" in lowered:
            return "structurally_ineligible", error
        if "baseline_no_pass" in lowered:
            return "baseline_recovery", error
        if "no_pass_to_pass" in lowered:
            return "p2p_recovery", error
        if "coverage" in lowered:
            return "coverage_recovery", error
        if any(
            term in lowered
            for term in (
                "env:",
                "build",
                "compile",
                "fetch",
                "clone",
                "toolchain",
                "dependency",
                "install",
            )
        ):
            return "environment_recovery", error
        return "oracle_recovery", error

    golden = golden_evaluation(evaluation)
    if not golden or golden.get("correctness_ok") is not True:
        return "golden_recovery", None
    if pass_to_pass_count(evaluation) < 1:
        return "p2p_recovery", None
    if coverage_result(evaluation) is not True:
        return "coverage_recovery", None
    return "strict_pass", None


def recovery_priority(status: str) -> int:
    order = {
        "environment_recovery": 0,
        "baseline_recovery": 1,
        "coverage_recovery": 2,
        "p2p_recovery": 3,
        "golden_recovery": 4,
        "oracle_recovery": 5,
        "not_validated": 6,
        "structurally_ineligible": 7,
    }
    return order.get(status, 99)


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in records)
        + ("\n" if records else ""),
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
