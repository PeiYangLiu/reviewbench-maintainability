"""Build a deduplicated, runnable-oriented medium/hard curation pool."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
from typing import Any

from .jsonl import read_jsonl, write_jsonl


TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|testing|__tests__|spec|specs)(/|$)"
    r"|(^|/)(test_[^/]+|[^/]+_test)\.(py|go)$",
    re.IGNORECASE,
)
DOC_PATH_RE = re.compile(
    r"(^|/)(docs?|examples?|website|site)(/|$)|\.(md|mdx|rst|adoc)$",
    re.IGNORECASE,
)
GENERATED_PATH_RE = re.compile(
    r"(^|/)(vendor|third_party|generated|dist|build|node_modules)(/|$)",
    re.IGNORECASE,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="inputs", type=Path, nargs="+", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--tiers", nargs="+", default=["hard", "medium"])
    parser.add_argument(
        "--allow-unscored",
        action="store_true",
        help="Let the strict quality judges assign intrinsic difficulty.",
    )
    parser.add_argument("--languages", nargs="+", default=["Go", "Python"])
    parser.add_argument(
        "--buckets",
        nargs="+",
        default=["high_priority", "oracle_candidate", "needs_review"],
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument("--max-per-repo", type=int)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    records, duplicate_count = merge_records(args.inputs)
    kept = []
    drop_reasons: Counter[str] = Counter()
    for record in records:
        reasons = exclusion_reasons(
            record,
            tiers=set(args.tiers),
            languages=set(args.languages),
            buckets=set(args.buckets),
            allow_unscored=args.allow_unscored,
        )
        if reasons:
            drop_reasons.update(reasons)
        else:
            kept.append(record)
    eligible_count = len(kept)
    kept.sort(key=ranking_key)
    if args.max_per_repo:
        kept = cap_per_repo(kept, args.max_per_repo)
    if args.limit:
        kept = kept[: args.limit]

    write_jsonl(args.out, kept)
    report = {
        "input_files": [str(path) for path in args.inputs],
        "input_rows": sum(_line_count(path) for path in args.inputs),
        "unique_tasks": len(records),
        "duplicate_rows": duplicate_count,
        "kept": len(kept),
        "eligible_before_selection": eligible_count,
        "drop_reasons": dict(drop_reasons.most_common()),
        "tiers": dict(Counter(_difficulty(item).get("tier") for item in kept)),
        "languages": dict(Counter(item.get("inferred_language") for item in kept)),
        "categories": dict(
            Counter(item.get("human_category") or item.get("category") for item in kept)
        ),
        "repositories": len({item.get("repo") for item in kept}),
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def merge_records(paths: list[Path]) -> tuple[list[dict[str, Any]], int]:
    merged: dict[str, dict[str, Any]] = {}
    rows = 0
    for path in paths:
        for record in read_jsonl(path):
            rows += 1
            task_id = str(record.get("id") or "")
            if not task_id:
                continue
            current = merged.get(task_id)
            merged[task_id] = _prefer_record(current, record)
    return list(merged.values()), rows - len(merged)


def exclusion_reasons(
    record: dict[str, Any],
    tiers: set[str],
    languages: set[str],
    buckets: set[str],
    allow_unscored: bool = False,
) -> list[str]:
    reasons = []
    path = str((record.get("locality") or {}).get("path") or "")
    tier = _difficulty(record).get("tier")
    if tier not in tiers and not (allow_unscored and not tier):
        reasons.append("difficulty_not_selected")
    if record.get("inferred_language") not in languages:
        reasons.append("language_not_selected")
    if (record.get("_prefilter") or {}).get("bucket") not in buckets:
        reasons.append("prefilter_bucket_not_selected")
    if record.get("contains_suggestion") is True:
        reasons.append("answer_leakage")
    if TEST_PATH_RE.search(path):
        reasons.append("test_anchor")
    if DOC_PATH_RE.search(path):
        reasons.append("documentation_anchor")
    if GENERATED_PATH_RE.search(path):
        reasons.append("generated_anchor")
    if not record.get("start_sha") or not record.get("target_sha"):
        reasons.append("missing_commit")
    if not path or (record.get("locality") or {}).get("line") is None:
        reasons.append("missing_locality")
    return reasons


def ranking_key(record: dict[str, Any]) -> tuple[Any, ...]:
    difficulty = _difficulty(record)
    annotation = record.get("human_annotation") or {}
    annotation_pass = (
        annotation.get("label_is_maintainability_comment") == "yes"
        and annotation.get("label_keep_for_benchmark") == "yes"
        and _annotation_quality(annotation) >= 4
    )
    return (
        {"hard": 0, "medium": 1}.get(str(difficulty.get("tier")), 2),
        0 if annotation_pass else 1,
        0 if _has_test_change(record) else 1,
        -substantive_hint_score(record),
        -int(difficulty.get("score") or 0),
        str(record.get("repo") or ""),
        str(record.get("id") or ""),
    )


def cap_per_repo(
    records: list[dict[str, Any]],
    maximum: int,
) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    selected = []
    for record in records:
        repo = str(record.get("repo") or "")
        if counts[repo] >= maximum:
            continue
        selected.append(record)
        counts[repo] += 1
    return selected


def substantive_hint_score(record: dict[str, Any]) -> int:
    instruction = str(record.get("task_instruction") or "").lower()
    category = str(record.get("human_category") or record.get("category") or "")
    score = 0
    for term in (
        "extract",
        "split",
        "refactor",
        "duplicate",
        "duplication",
        "complex",
        "decouple",
        "separation of concerns",
        "responsibility",
        "abstraction",
        "reusable",
        "reuse",
        "hard to follow",
        "hard to understand",
        "simplif",
        "nested",
        "testable",
    ):
        if term in instruction:
            score += 2
    for term in (
        "nit",
        "typo",
        "move this import",
        "move import",
        "unused import",
        "add a comment",
        "add a docstring",
        "rename",
    ):
        if term in instruction:
            score -= 2
    if category in {"complexity", "modularity", "duplication", "testability"}:
        score += 2
    if category in {"naming", "dead_code"}:
        score -= 1
    return score


def _prefer_record(
    current: dict[str, Any] | None,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    if current is None:
        return candidate
    current_annotation = bool(current.get("human_annotation"))
    candidate_annotation = bool(candidate.get("human_annotation"))
    if candidate_annotation and not current_annotation:
        return candidate
    current_difficulty = bool(current.get("difficulty"))
    candidate_difficulty = bool(candidate.get("difficulty"))
    if candidate_difficulty and not current_difficulty:
        return candidate
    return current


def _annotation_quality(annotation: dict[str, Any]) -> int:
    try:
        return int(annotation.get("label_quality_score_1_to_5") or 0)
    except (TypeError, ValueError):
        return 0


def _has_test_change(record: dict[str, Any]) -> bool:
    proxy = record.get("correctness_oracle_proxy") or {}
    return bool(proxy.get("test_change_paths"))


def _difficulty(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("difficulty")
    return value if isinstance(value, dict) else {}


def _line_count(path: Path) -> int:
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


if __name__ == "__main__":
    raise SystemExit(main())
