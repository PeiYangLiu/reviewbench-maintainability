"""Freeze a diverse release from strictly quality-gated, executable tasks."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict, deque
import hashlib
import json
from pathlib import Path
import re
from typing import Any


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, required=True, help="Quality-gated task JSONL.")
    parser.add_argument("--evaluations", type=Path, nargs="+", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--target", type=int, default=100)
    parser.add_argument("--max-per-repo", type=int, default=5)
    parser.add_argument("--min-pass-to-pass", type=int, default=1)
    parser.add_argument(
        "--allow-uncovered",
        action="store_true",
        help="Permit tasks whose stable tests do not cover changed lines.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    tasks = load_jsonl_by_id(args.tasks)
    evaluations = load_evaluations(args.evaluations)
    eligible = []
    failures: Counter[str] = Counter()
    for task_id, task in tasks.items():
        reasons = release_failure_reasons(
            task,
            evaluations.get(task_id),
            min_pass_to_pass=args.min_pass_to_pass,
            require_coverage=not args.allow_uncovered,
        )
        if reasons:
            failures.update(reasons)
            continue
        eligible.append(build_release_record(task, evaluations[task_id]))

    selected = select_diverse(
        eligible,
        target=args.target,
        max_per_repo=args.max_per_repo,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in selected)
        + ("\n" if selected else ""),
        encoding="utf-8",
    )
    report = release_report(
        tasks=tasks,
        evaluations=evaluations,
        eligible=eligible,
        selected=selected,
        failures=failures,
        target=args.target,
        max_per_repo=args.max_per_repo,
        min_pass_to_pass=args.min_pass_to_pass,
        require_coverage=not args.allow_uncovered,
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if len(selected) == args.target else 1


def load_jsonl_by_id(path: Path) -> dict[str, dict[str, Any]]:
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        task_id = str(item.get("id") or "")
        if task_id:
            result[task_id] = item
    return result


def load_evaluations(paths: list[Path]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for path in paths:
        text = path.read_text(encoding="utf-8")
        try:
            value = json.loads(text)
            records = value if isinstance(value, list) else [value]
        except json.JSONDecodeError:
            records = [
                json.loads(line) for line in text.splitlines() if line.strip()
            ]
        for record in records:
            if not isinstance(record, dict):
                continue
            task_id = str(record.get("id") or record.get("task_id") or "")
            if not task_id:
                continue
            current = result.get(task_id)
            if current is None:
                result[task_id] = record
            else:
                result[task_id] = merge_evaluation_records(current, record)
    return result


def release_failure_reasons(
    task: dict[str, Any],
    evaluation: dict[str, Any] | None,
    min_pass_to_pass: int,
    require_coverage: bool = True,
) -> list[str]:
    reasons = []
    if (task.get("quality_gate") or {}).get("passed") is not True:
        reasons.append("quality_gate_failed")
    if judged_difficulty(task) not in {"medium", "hard"}:
        reasons.append("difficulty_below_medium")
    if evaluation is None:
        reasons.append("missing_evaluation")
        return reasons
    if evaluation.get("setup_error"):
        reasons.append("setup_failed")
    golden = golden_evaluation(evaluation)
    if not golden:
        reasons.append("missing_golden_evaluation")
    elif golden.get("correctness_ok") is not True:
        reasons.append("golden_correctness_failed")
    if pass_to_pass_count(evaluation) < min_pass_to_pass:
        reasons.append("insufficient_pass_to_pass")
    if require_coverage and coverage_result(evaluation) is not True:
        reasons.append("changed_lines_not_covered")
    return reasons


def judged_difficulty(task: dict[str, Any]) -> str:
    difficulties = [
        str((judgment.get("verdict") or {}).get("intrinsic_difficulty") or "")
        for judgment in task.get("quality_judgments") or []
        if isinstance(judgment, dict)
    ]
    if not difficulties:
        return ""
    if all(value == "hard" for value in difficulties):
        return "hard"
    if all(value in {"medium", "hard"} for value in difficulties):
        return "medium"
    return "easy"


def golden_evaluation(evaluation: dict[str, Any]) -> dict[str, Any] | None:
    if type(evaluation.get("golden_correctness_ok")) is bool:
        return {"correctness_ok": evaluation["golden_correctness_ok"]}
    for item in evaluation.get("evals") or []:
        if item.get("mode") == "golden":
            return item
    gates = evaluation.get("gates") or {}
    correctness = gates.get("correctness") or {}
    if correctness:
        return {"correctness_ok": correctness.get("passed") is True}
    return None


def build_release_record(
    task: dict[str, Any],
    evaluation: dict[str, Any],
) -> dict[str, Any]:
    item = dict(task)
    item["release_validation"] = {
        "judged_difficulty": judged_difficulty(task),
        "pass_to_pass": pass_to_pass_count(evaluation),
        "golden_correctness": True,
        "changed_lines_covered": coverage_result(evaluation),
        "quality_gate_version": (task.get("quality_gate") or {}).get("version"),
    }
    return item


def select_diverse(
    records: list[dict[str, Any]],
    target: int,
    max_per_repo: int,
) -> list[dict[str, Any]]:
    ordered = sorted(
        records,
        key=lambda item: (
            0 if judged_difficulty(item) == "hard" else 1,
            -_minimum_quality_score(item),
            str(item.get("id") or ""),
        ),
    )
    by_repo: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    for item in ordered:
        by_repo[str(item.get("repo") or "")].append(item)

    selected = []
    repo_counts: Counter[str] = Counter()
    fingerprints: set[str] = set()
    active = sorted(by_repo)
    while active and len(selected) < target:
        next_active = []
        for repo in active:
            queue = by_repo[repo]
            while queue and task_fingerprint(queue[0]) in fingerprints:
                queue.popleft()
            if queue and repo_counts[repo] < max_per_repo and len(selected) < target:
                item = queue.popleft()
                selected.append(item)
                fingerprints.add(task_fingerprint(item))
                repo_counts[repo] += 1
            if queue and repo_counts[repo] < max_per_repo:
                next_active.append(repo)
        active = next_active
    return selected


def task_fingerprint(task: dict[str, Any]) -> str:
    evidence = task.get("quality_evidence") or {}
    patch = str(evidence.get("patch") or "")
    if not patch:
        return str(task.get("id") or "")
    normalized = re.sub(r"^@@.*?@@", "@@", patch, flags=re.MULTILINE)
    normalized = "\n".join(line.rstrip() for line in normalized.splitlines())
    identity = "\n".join(
        (
            str(task.get("repo") or ""),
            str(task.get("pr_number") or ""),
            str((task.get("locality") or {}).get("path") or ""),
            normalized,
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def release_report(
    tasks: dict[str, dict[str, Any]],
    evaluations: dict[str, dict[str, Any]],
    eligible: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    failures: Counter[str],
    target: int,
    max_per_repo: int,
    min_pass_to_pass: int,
    require_coverage: bool,
) -> dict[str, Any]:
    return {
        "target": target,
        "complete": len(selected) == target,
        "quality_gated_tasks": len(tasks),
        "evaluation_records": len(evaluations),
        "eligible": len(eligible),
        "selected": len(selected),
        "failure_reasons": dict(failures.most_common()),
        "selection": {
            "max_per_repo": max_per_repo,
            "min_pass_to_pass": min_pass_to_pass,
            "require_changed_line_coverage": require_coverage,
        },
        "tiers": dict(Counter(judged_difficulty(item) for item in selected)),
        "languages": dict(Counter(item.get("inferred_language") for item in selected)),
        "categories": dict(
            Counter(item.get("human_category") or item.get("category") for item in selected)
        ),
        "repositories": len({item.get("repo") for item in selected}),
    }


def evaluation_richness(record: dict[str, Any]) -> tuple[int, int]:
    return (
        1 if record.get("evals") else 0,
        len(record.get("evals") or []),
    )


def merge_evaluation_records(
    left: dict[str, Any],
    right: dict[str, Any],
) -> dict[str, Any]:
    if evaluation_richness(right) >= evaluation_richness(left):
        primary, secondary = right, left
    else:
        primary, secondary = left, right
    merged = dict(secondary)
    merged.update(primary)
    for key in ("coverage", "changed_lines_covered", "pass_to_pass"):
        if key not in primary and key in secondary:
            merged[key] = secondary[key]
    if (
        primary.get("golden_correctness_ok") is True
        and pass_to_pass_count(primary) > 0
    ):
        merged["setup_error"] = primary.get("setup_error")
    return merged


def coverage_result(evaluation: dict[str, Any]) -> bool | None:
    value = evaluation.get("changed_lines_covered")
    if type(value) is bool:
        return value
    coverage = evaluation.get("coverage") or {}
    covered = coverage.get("covered") if isinstance(coverage, dict) else None
    return covered if type(covered) is bool else None


def pass_to_pass_count(evaluation: dict[str, Any]) -> int:
    value = evaluation.get("pass_to_pass")
    if isinstance(value, dict):
        value = value.get("count", 0)
    elif isinstance(value, list):
        return len(value)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _minimum_quality_score(task: dict[str, Any]) -> int:
    scores = [
        int((judgment.get("verdict") or {}).get("quality_score_1_to_5") or 0)
        for judgment in task.get("quality_judgments") or []
        if isinstance(judgment, dict)
    ]
    return min(scores, default=0)


if __name__ == "__main__":
    raise SystemExit(main())
