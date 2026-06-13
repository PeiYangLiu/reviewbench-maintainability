#!/usr/bin/env python3
"""Apply human annotations to candidate JSONL and export benchmark splits."""

from __future__ import annotations

import argparse
import csv
from collections import Counter
import json
from pathlib import Path
from typing import Any


LABEL_COLUMNS = (
    "label_is_maintainability_comment",
    "label_category_correct",
    "label_best_category",
    "label_task_is_clear",
    "label_task_localized_to_comment",
    "label_comment_was_addressed",
    "label_no_answer_leakage_after_sanitization",
    "label_auto_static_gate_valid",
    "label_correctness_oracle_usable",
    "label_keep_for_benchmark",
    "label_quality_score_1_to_5",
    "annotator_id",
    "notes",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, help="Original candidate JSONL. Optional when annotation CSV has base fields.")
    parser.add_argument("--annotations", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    annotations = load_annotations(args.annotations)
    if args.candidates and args.candidates.exists():
        candidates = load_jsonl(args.candidates)
        labeled = merge_annotations(candidates, annotations)
    else:
        labeled = items_from_annotations(annotations)

    strict_verified = [item for item in labeled if is_strict_verified(item)]
    relaxed_pool = [item for item in labeled if is_relaxed_candidate(item)]
    strict_ids = {item.get("id") for item in strict_verified}
    relaxed_extra = [item for item in relaxed_pool if item.get("id") not in strict_ids]
    needs_review = [
        item
        for item in labeled
        if item not in strict_verified
        and item not in relaxed_pool
        and not is_rejected(item)
    ]
    rejected = [item for item in labeled if is_rejected(item)]

    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(args.out_dir / "verified_strict.jsonl", strict_verified)
    write_jsonl(args.out_dir / "review_pool_relaxed.jsonl", relaxed_pool)
    write_jsonl(args.out_dir / "review_pool_relaxed_extra.jsonl", relaxed_extra)
    write_jsonl(args.out_dir / "needs_review.jsonl", needs_review)
    write_jsonl(args.out_dir / "rejected.jsonl", rejected)
    write_manifest(args.out_dir / "verified_strict.csv", strict_verified)
    write_manifest(args.out_dir / "review_pool_relaxed.csv", relaxed_pool)
    write_manifest(args.out_dir / "review_pool_relaxed_extra.csv", relaxed_extra)
    write_manifest(args.out_dir / "needs_review.csv", needs_review)
    write_manifest(args.out_dir / "rejected.csv", rejected)

    report = build_report(labeled, strict_verified, relaxed_pool, relaxed_extra, needs_review, rejected)
    (args.out_dir / "annotation_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        f"Exported {len(strict_verified)} strict verified, "
        f"{len(relaxed_pool)} relaxed ({len(relaxed_extra)} beyond strict), "
        f"{len(needs_review)} needs-review, "
        f"{len(rejected)} rejected to {args.out_dir}"
    )
    return 0


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_annotations(path: Path) -> dict[str, dict[str, str]]:
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    annotations: dict[str, dict[str, str]] = {}
    for row in rows:
        case_id = (row.get("case_id") or "").strip()
        if not case_id:
            continue
        annotations[case_id] = {
            key: (value or "").strip()
            for key, value in row.items()
            if key is not None
        }
    return annotations


def items_from_annotations(annotations: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    items = []
    for case_id, row in annotations.items():
        labels = {column: row.get(column, "") for column in LABEL_COLUMNS}
        category = row.get("category_pred", "")
        gate_type = row.get("gate_type", "") or infer_gate_type(category)
        item = {
            "id": case_id,
            "repo": row.get("repo", ""),
            "pr_number": int(row["pr_number"]) if row.get("pr_number", "").isdigit() else row.get("pr_number", ""),
            "pr_url": row.get("pr_url", ""),
            "task_instruction": row.get("task_instruction", ""),
            "category": category,
            "gate_type": gate_type,
            "confidence": parse_float(row.get("confidence", "")),
            "inferred_language": row.get("language", "Other"),
            "matched_terms": [term for term in row.get("matched_terms", "").split("|") if term],
            "contains_suggestion": parse_bool(row.get("contains_suggestion", "")),
            "locality": {
                "path": row.get("path", ""),
                "line": parse_int(row.get("line", "")),
            },
            "addressed_evidence": {
                "thread_resolved": parse_bool_or_none(row.get("thread_resolved", "")),
                "region_touched_after_comment": parse_bool(row.get("region_touched", "")),
                "file_touched_after_comment": parse_bool(row.get("file_touched", "")),
            },
            "comment": {
                "url": row.get("comment_url", ""),
            },
            "human_annotation": labels,
        }
        item["human_category"] = human_category(item, labels)
        item["human_gate_type"] = infer_gate_type(item["human_category"])
        items.append(item)
    return items


def infer_gate_type(category: str) -> str:
    return "auto_static" if category in {"complexity", "duplication", "dead_code"} else "manual_or_llm_calibrated"


def merge_annotations(
    candidates: list[dict[str, Any]],
    annotations: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    labeled = []
    for candidate in candidates:
        case_id = candidate.get("id")
        item = dict(candidate)
        annotation = annotations.get(case_id, {})
        item["human_annotation"] = annotation
        item["human_category"] = human_category(candidate, annotation)
        item["human_gate_type"] = infer_gate_type(item["human_category"])
        labeled.append(item)
    return labeled


def human_category(candidate: dict[str, Any], annotation: dict[str, str]) -> str:
    predicted = candidate.get("category", "unknown")
    best = annotation.get("label_best_category", "")
    correctness = annotation.get("label_category_correct", "")
    if correctness == "yes" or not best:
        return predicted
    return best


def parse_bool(value: str) -> bool:
    return str(value).strip().lower() == "true"


def parse_bool_or_none(value: str) -> bool | None:
    normalized = str(value).strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    return None


def parse_int(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_float(value: str) -> float | str:
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


def quality(item: dict[str, Any]) -> int:
    value = item.get("human_annotation", {}).get("label_quality_score_1_to_5", "")
    try:
        return int(value)
    except ValueError:
        return 0


def auto_static_gate_ok(item: dict[str, Any]) -> bool:
    if item.get("human_gate_type", item.get("gate_type")) != "auto_static":
        return True
    return item.get("human_annotation", {}).get("label_auto_static_gate_valid") in {"yes", "partial"}


def correctness_oracle_ok(item: dict[str, Any]) -> bool:
    return item.get("human_annotation", {}).get("label_correctness_oracle_usable") in {"yes", "partial"}


def is_strict_verified(item: dict[str, Any]) -> bool:
    labels = item.get("human_annotation", {})
    return (
        labels.get("label_is_maintainability_comment") == "yes"
        and labels.get("label_task_is_clear") == "yes"
        and labels.get("label_task_localized_to_comment") == "yes"
        and labels.get("label_comment_was_addressed") == "yes"
        and labels.get("label_no_answer_leakage_after_sanitization") == "yes"
        and labels.get("label_keep_for_benchmark") == "yes"
        and quality(item) >= 4
        and auto_static_gate_ok(item)
        and correctness_oracle_ok(item)
    )


def is_relaxed_candidate(item: dict[str, Any]) -> bool:
    labels = item.get("human_annotation", {})
    return (
        labels.get("label_is_maintainability_comment") == "yes"
        and labels.get("label_task_is_clear") == "yes"
        and labels.get("label_task_localized_to_comment") == "yes"
        and labels.get("label_comment_was_addressed") in {"yes", "unclear"}
        and labels.get("label_no_answer_leakage_after_sanitization") == "yes"
        and labels.get("label_keep_for_benchmark") in {"yes", "maybe"}
        and quality(item) >= 3
        and auto_static_gate_ok(item)
        and correctness_oracle_ok(item)
    )


def is_rejected(item: dict[str, Any]) -> bool:
    labels = item.get("human_annotation", {})
    return (
        labels.get("label_keep_for_benchmark") == "no"
        or labels.get("label_is_maintainability_comment") == "no"
        or labels.get("label_no_answer_leakage_after_sanitization") == "no"
        or quality(item) <= 2
    )


def build_report(
    labeled: list[dict[str, Any]],
    strict_verified: list[dict[str, Any]],
    relaxed_pool: list[dict[str, Any]],
    relaxed_extra: list[dict[str, Any]],
    needs_review: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "total_labeled": len(labeled),
        "strict_verified": summarize_items(strict_verified),
        "review_pool_relaxed": summarize_items(relaxed_pool),
        "review_pool_relaxed_extra": summarize_items(relaxed_extra),
        "needs_review": summarize_items(needs_review),
        "rejected": summarize_items(rejected),
        "label_distributions": {
            column: dict(Counter(item.get("human_annotation", {}).get(column, "") for item in labeled).most_common())
            for column in LABEL_COLUMNS
        },
        "precision_estimates": {
            "maintainability_yes_rate": rate(labeled, "label_is_maintainability_comment", "yes"),
            "keep_yes_rate": rate(labeled, "label_keep_for_benchmark", "yes"),
            "keep_yes_or_maybe_rate": sum(
                1
                for item in labeled
                if item.get("human_annotation", {}).get("label_keep_for_benchmark") in {"yes", "maybe"}
            )
            / len(labeled)
            if labeled
            else 0.0,
            "correctness_oracle_yes_or_partial_rate": sum(
                1
                for item in labeled
                if item.get("human_annotation", {}).get("label_correctness_oracle_usable") in {"yes", "partial"}
            )
            / len(labeled)
            if labeled
            else 0.0,
            "strict_verified_rate": len(strict_verified) / len(labeled) if labeled else 0.0,
            "relaxed_candidate_rate": len(relaxed_pool) / len(labeled) if labeled else 0.0,
        },
    }


def summarize_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(items),
        "auto_static": sum(1 for item in items if item.get("human_gate_type", item.get("gate_type")) == "auto_static"),
        "languages": dict(Counter(item.get("inferred_language", "Other") for item in items).most_common()),
        "predicted_categories": dict(Counter(item.get("category", "unknown") for item in items).most_common()),
        "human_categories": dict(Counter(item.get("human_category", "unknown") for item in items).most_common()),
        "repos": dict(Counter(item.get("repo", "unknown") for item in items).most_common()),
    }


def rate(items: list[dict[str, Any]], column: str, value: str) -> float:
    return (
        sum(1 for item in items if item.get("human_annotation", {}).get(column) == value) / len(items)
        if items
        else 0.0
    )


def write_jsonl(path: Path, items: list[dict[str, Any]]) -> None:
    path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in items)
        + ("\n" if items else ""),
        encoding="utf-8",
    )


def write_manifest(path: Path, items: list[dict[str, Any]]) -> None:
    columns = (
        "id",
        "repo",
        "inferred_language",
        "category",
        "human_category",
        "gate_type",
        "human_gate_type",
        "confidence",
        "quality",
        "path",
        "comment_url",
        "task_instruction",
        "keep",
        "maintainability",
        "addressed",
        "auto_static_gate_valid",
        "correctness_oracle_usable",
        "oracle_strength",
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for item in items:
            labels = item.get("human_annotation", {})
            locality = item.get("locality", {})
            comment = item.get("comment", {})
            oracle_proxy = item.get("correctness_oracle_proxy", {})
            writer.writerow(
                {
                    "id": item.get("id", ""),
                    "repo": item.get("repo", ""),
                    "inferred_language": item.get("inferred_language", ""),
                    "category": item.get("category", ""),
                    "human_category": item.get("human_category", ""),
                    "gate_type": item.get("gate_type", ""),
                    "human_gate_type": item.get("human_gate_type", ""),
                    "confidence": item.get("confidence", ""),
                    "quality": labels.get("label_quality_score_1_to_5", ""),
                    "path": locality.get("path", ""),
                    "comment_url": comment.get("url", ""),
                    "task_instruction": item.get("task_instruction", ""),
                    "keep": labels.get("label_keep_for_benchmark", ""),
                    "maintainability": labels.get("label_is_maintainability_comment", ""),
                    "addressed": labels.get("label_comment_was_addressed", ""),
                    "auto_static_gate_valid": labels.get("label_auto_static_gate_valid", ""),
                    "correctness_oracle_usable": labels.get("label_correctness_oracle_usable", ""),
                    "oracle_strength": oracle_proxy.get("oracle_strength", ""),
                }
            )


if __name__ == "__main__":
    raise SystemExit(main())
