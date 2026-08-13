#!/usr/bin/env python3
"""Combine generation, oracle replay, and blind-judge results into a leaderboard."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument(
        "--generation",
        action="append",
        required=True,
        help="MODEL=path/to/generation_results.jsonl",
    )
    parser.add_argument("--correctness-dir", type=Path, required=True)
    parser.add_argument("--judgments", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--max-changed-lines", type=int, default=400)
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def parse_model_paths(values: list[str]) -> dict[str, Path]:
    result = {}
    for value in values:
        model, separator, path = value.partition("=")
        if not separator or not model or not path:
            raise ValueError(f"invalid model path: {value!r}")
        result[model] = Path(path)
    return result


def load_correctness(directory: Path) -> dict[tuple[str, str], dict[str, Any]]:
    result = {}
    for path in sorted(directory.glob("*.jsonl")):
        for record in load_jsonl(path):
            model_results = (
                record.get("models")
                or record.get("results")
                or record.get("model_results")
                or []
            )
            if not model_results and record.get("model"):
                model_results = [record]
            if isinstance(model_results, dict):
                model_results = list(model_results.values())
            for model_result in model_results:
                key = (str(record.get("id") or ""), str(model_result.get("model") or ""))
                if not all(key):
                    continue
                if key in result:
                    raise ValueError(f"duplicate correctness result for {key}")
                apply_result = model_result.get("apply") or {}
                tests = model_result.get("tests") or {}
                result[key] = {
                    **model_result,
                    "patch_apply": (
                        model_result.get("patch_apply") is True
                        or model_result.get("patch_applied") is True
                        or model_result.get("patch_apply_ok") is True
                        or model_result.get("apply_ok") is True
                        or apply_result.get("applied") is True
                    ),                    "correctness_ok": (
                        model_result.get("correctness_ok") is True
                        or model_result.get("correct") is True
                        or model_result.get("correctness_pass") is True
                        or model_result.get("correctness") == "pass"
                        or model_result.get("correctness_result") == "pass"
                        or model_result.get("status") == "correctness_pass"
                        or tests.get("correctness_pass") is True
                    ),
                    "pass_to_pass_total": (
                        model_result.get("pass_to_pass_total")
                        if model_result.get("pass_to_pass_total") is not None
                        else model_result.get("expected_pass_to_pass")
                        if model_result.get("expected_pass_to_pass") is not None
                        else model_result.get("p2p_total")
                        if model_result.get("p2p_total") is not None
                        else model_result.get("actual_p2p")
                        if model_result.get("actual_p2p") is not None
                        else tests.get("pass_to_pass_total")
                        if tests.get("pass_to_pass_total") is not None
                        else tests.get("total")
                        if tests.get("total") is not None
                        else (record.get("oracle") or {}).get("pass_to_pass")
                    ),
                    "pass_to_pass_passed": (
                        model_result.get("pass_to_pass_passed")
                        if model_result.get("pass_to_pass_passed") is not None
                        else model_result.get("observed_pass_to_pass")
                        if model_result.get("observed_pass_to_pass") is not None
                        else model_result.get("p2p_passed")
                        if model_result.get("p2p_passed") is not None
                        else model_result.get("actual_p2p")
                        if model_result.get("correctness_pass") is True
                        else tests.get("pass_to_pass_passed")
                        if tests.get("pass_to_pass_passed") is not None
                        else tests.get("passed")
                    ),
                    "failed_tests": model_result.get("failed_tests")
                    or tests.get("failed_tests")
                    or (
                        [str(model_result.get("failure_summary"))]
                        if model_result.get("failure_summary")
                        else []
                    )
                    or (
                        [str(model_result.get("reason"))]
                        if model_result.get("correct") is False
                        and model_result.get("reason")
                        else []
                    )
                    or [],
                    "command": model_result.get("command") or tests.get("command"),
                    "oracle_replayed": (
                        record.get("oracle_replayed") is True
                        or (
                            record.get("schema_version")
                            == "reviewbench-open-correctness-normalized/v1"
                            and model_result.get("status") in {"pass", "fail"}
                        )
                        or model_result.get("correctness_result")
                        in {"pass", "fail"}
                        or model_result.get("status")
                        in {"correctness_pass", "correctness_fail"}
                        or model_result.get("pair_status") in {"pass", "fail"}
                        or isinstance(record.get("oracle_reused"), dict)
                        or (
                            record.get("evaluation_mode") == "direct"
                            and (record.get("policy") or {}).get(
                                "same_oracle_all_models"
                            )
                            is True
                        )
                        or (
                            (record.get("policy") or {}).get(
                                "same_oracle_all_models"
                            )
                            is True
                            and (record.get("policy") or {}).get(
                                "golden_or_target_applied"
                            )
                            is False
                        )
                        or (
                            (record.get("policy") or {}).get(
                                "start_reset_each_model"
                            )
                            is True
                            and (record.get("policy") or {}).get(
                                "target_or_golden_applied"
                            )
                            is False
                        )
                        or (
                            (record.get("policy") or {}).get(
                                "start_reset_per_model"
                            )
                            is True
                            and (record.get("policy") or {}).get(
                                "target_or_golden_applied"
                            )
                            is False
                        )
                        or (
                            isinstance(record.get("environment_reused"), dict)
                            and (record.get("policy") or {}).get(
                                "same_oracle_all_models"
                            )
                            is True
                            and (record.get("policy") or {}).get(
                                "target_applied"
                            )
                            is False
                            and (record.get("policy") or {}).get(
                                "golden_applied"
                            )
                            is False
                        )
                        or (
                            str(record.get("schema_version") or "").startswith(
                                "reviewbench-model-correctness/"
                            )
                            and (record.get("patch_policy") or {}).get(
                                "target_or_golden_applied"
                            )
                            is False
                        )
                    ),
                    "correctness_source": path.name,
                }
    return result


def difficulty(task: dict[str, Any]) -> str:
    return str((task.get("release_validation") or {}).get("judged_difficulty") or "")


def summarize_model(rows: list[dict[str, Any]], model: str) -> dict[str, Any]:
    model_rows = [row for row in rows if row["model"] == model]
    resolved = [row for row in model_rows if row["resolved"]]
    return {
        "tasks": len(model_rows),
        "generation_apply": sum(row["generation_apply"] for row in model_rows),
        "scope_pass": sum(row["scope_ok"] for row in model_rows),
        "correctness_pass": sum(row["correctness_ok"] for row in model_rows),
        "maintainability_pass": sum(row["maintainability_pass"] for row in model_rows),
        "resolved": len(resolved),
        "resolved_rate": round(len(resolved) / len(model_rows), 4) if model_rows else 0.0,
        "by_difficulty": breakdown(model_rows, "difficulty"),
        "by_language": breakdown(model_rows, "language"),
        "by_category": breakdown(model_rows, "category"),
        "failure_reasons": dict(
            Counter(
                reason
                for row in model_rows
                for reason in row["failure_reasons"]
            ).most_common()
        ),
    }


def breakdown(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    result = {}
    for value in sorted({str(row[key]) for row in rows}):
        group = [row for row in rows if str(row[key]) == value]
        count = sum(row["resolved"] for row in group)
        result[value] = {
            "resolved": count,
            "tasks": len(group),
            "rate": round(count / len(group), 4) if group else 0.0,
        }
    return result


def pairwise(rows: list[dict[str, Any]], models: list[str]) -> dict[str, Any]:
    by_key = {(row["id"], row["model"]): row["resolved"] for row in rows}
    ids = sorted({row["id"] for row in rows})
    result = {}
    for left_index, left in enumerate(models):
        for right in models[left_index + 1 :]:
            values = Counter()
            for task_id in ids:
                left_pass = by_key.get((task_id, left), False)
                right_pass = by_key.get((task_id, right), False)
                if left_pass and right_pass:
                    values["both"] += 1
                elif left_pass:
                    values["left_only"] += 1
                elif right_pass:
                    values["right_only"] += 1
                else:
                    values["neither"] += 1
            result[f"{left}__vs__{right}"] = dict(values)
    return result


def build_evaluation(
    tasks: list[dict[str, Any]],
    model_paths: dict[str, Path],
    correctness_dir: Path,
    judgments_path: Path,
    max_changed_lines: int = 400,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    generations = {
        (item["id"], model): item
        for model, path in model_paths.items()
        for item in load_jsonl(path)
    }
    correctness = load_correctness(correctness_dir)
    judgments = {
        (item["id"], item["solver_model"]): item
        for item in load_jsonl(judgments_path)
    }

    rows = []
    for task in tasks:
        task_id = task["id"]
        for model in model_paths:
            generation = generations.get((task_id, model)) or {}
            correct = correctness.get((task_id, model)) or {}
            judgment = judgments.get((task_id, model)) or {}
            generation_apply = generation.get("apply_ok") is True
            scope_ok = (
                generation_apply
                and type(generation.get("changed_lines")) is int
                and 0 < generation["changed_lines"] <= max_changed_lines
                and generation.get("path") == (task.get("locality") or {}).get("path")
            )
            correctness_ok = (
                correct.get("oracle_replayed") is True
                and correct.get("patch_apply") is True
                and correct.get("correctness_ok") is True
            )
            maintainability_pass = judgment.get("maintainability_pass") is True
            resolved = generation_apply and scope_ok and correctness_ok and maintainability_pass
            failures = []
            if not generation_apply:
                failures.append("generation_or_apply")
            if generation_apply and not scope_ok:
                failures.append("scope")
            if not correctness_ok:
                failures.append("correctness")
            if not maintainability_pass:
                failures.append("maintainability")
            rows.append(
                {
                    "id": task_id,
                    "repo": task.get("repo"),
                    "model": model,
                    "language": task.get("inferred_language"),
                    "category": task.get("human_category") or task.get("category"),
                    "difficulty": difficulty(task),
                    "generation_apply": generation_apply,
                    "scope_ok": scope_ok,
                    "correctness_ok": correctness_ok,
                    "maintainability_pass": maintainability_pass,
                    "resolved": resolved,
                    "failure_reasons": failures,
                    "changed_lines": generation.get("changed_lines"),
                    "generation_protocol": generation.get("protocol", "search-replace/v1"),
                    "generation_status": generation.get("status", "missing"),
                    "correctness_status": (
                        "passed"
                        if correctness_ok
                        else correct.get("error")
                        or ("missing" if not correct else "failed")
                    ),
                    "pass_to_pass_total": correct.get("pass_to_pass_total"),
                    "pass_to_pass_passed": correct.get("pass_to_pass_passed"),
                    "failed_tests": correct.get("failed_tests") or [],
                    "judge_status": judgment.get("status", "missing"),
                    "judge_verdict": judgment.get("verdict"),
                }
            )

    models = list(model_paths)
    expected = len(tasks) * len(models)
    generation_protocols = sorted(
        {
            str(item.get("protocol") or "search-replace/v1")
            for item in generations.values()
        }
    )
    report = {
        "schema_version": "reviewbench-model-evaluation/v1",
        "complete": (
            len(generations) == expected
            and len(correctness) == expected
            and len(judgments) == expected
        ),
        "tasks": len(tasks),
        "models": models,
        "expected_task_model_pairs": expected,
        "generation_records": len(generations),
        "generation_protocols": generation_protocols,
        "correctness_records": len(correctness),
        "judgment_records": len(judgments),
        "leaderboard": {
            model: summarize_model(rows, model)
            for model in models
        },
        "pairwise": pairwise(rows, models),
    }
    return rows, report


def main() -> int:
    args = parse_args()
    tasks = load_jsonl(args.tasks)
    model_paths = parse_model_paths(args.generation)
    rows, report = build_evaluation(
        tasks,
        model_paths,
        args.correctness_dir,
        args.judgments,
        args.max_changed_lines,
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
