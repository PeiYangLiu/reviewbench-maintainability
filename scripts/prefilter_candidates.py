#!/usr/bin/env python3
"""Automatically prefilter raw ReviewBench candidates for annotation and oracle curation."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict, deque
import csv
import json
from pathlib import Path
import re
from typing import Any


AUTO_STATIC_CATEGORIES = {"complexity", "duplication", "dead_code"}
BOT_AUTHOR_PATTERNS = (
    "[bot]",
    "bot",
    "copilot",
    "coderabbit",
    "reviewdog",
    "dependabot",
    "renovate",
    "pre-commit-ci",
    "github-actions",
    "codecov",
    "sonarcloud",
)
TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|__tests__|spec|specs|testdata|fixtures?)/"
    r"|(_test\.|\.test\.|\.spec\.|Test\.|Tests\.)",
    re.IGNORECASE,
)
DOC_PATH_RE = re.compile(r"(^|/)(docs?|website|site|examples?)/|(\.mdx?|\.rst|\.adoc)$", re.IGNORECASE)
GENERATED_PATH_RE = re.compile(
    r"(^|/)(dist|build|generated|vendor|third_party|node_modules)/"
    r"|(lock$|\.lock$|package-lock\.json$|pnpm-lock\.yaml$|yarn\.lock$)",
    re.IGNORECASE,
)


EXTENSION_LANGUAGE = {
    ".py": "Python",
    ".pyi": "Python",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".c": "C",
    ".h": "C/C++",
    ".cc": "C++",
    ".cpp": "C++",
    ".cxx": "C++",
    ".hpp": "C++",
    ".cs": "C#",
    ".rb": "Ruby",
    ".php": "PHP",
    ".swift": "Swift",
    ".scala": "Scala",
    ".sh": "Shell",
    ".bash": "Shell",
    ".zsh": "Shell",
    ".rs.in": "Rust",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--sample-size", type=int, default=500)
    parser.add_argument("--max-pr-files-high-priority", type=int, default=80)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    candidates = load_jsonl(args.candidates)
    for item in candidates:
        enrich_candidate(item, args.max_pr_files_high_priority)

    high_priority = [item for item in candidates if item["_prefilter"]["bucket"] == "high_priority"]
    oracle_candidates = [item for item in candidates if item["_prefilter"]["bucket"] == "oracle_candidate"]
    needs_review = [item for item in candidates if item["_prefilter"]["bucket"] == "needs_review"]
    rejected = [item for item in candidates if item["_prefilter"]["bucket"] == "rejected"]
    high_priority_auto_static = [
        item
        for item in high_priority
        if item.get("gate_type") == "auto_static" or item.get("category") in AUTO_STATIC_CATEGORIES
    ]
    sample = diversity_sample(high_priority, args.sample_size)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(args.out_dir / "high_priority.jsonl", high_priority)
    write_jsonl(args.out_dir / "high_priority_auto_static.jsonl", high_priority_auto_static)
    write_jsonl(args.out_dir / "oracle_candidates.jsonl", oracle_candidates)
    write_jsonl(args.out_dir / "needs_review.jsonl", needs_review)
    write_jsonl(args.out_dir / "rejected.jsonl", rejected)
    write_jsonl(args.out_dir / "annotation_sample_high_priority.jsonl", sample)
    write_manifest(args.out_dir / "high_priority_manifest.csv", high_priority)
    write_manifest(args.out_dir / "annotation_sample_high_priority.csv", sample)
    write_report(
        args.out_dir / "prefilter_report.json",
        candidates,
        high_priority,
        high_priority_auto_static,
        oracle_candidates,
        needs_review,
        rejected,
        sample,
    )
    print(
        f"Prefiltered {len(candidates)} candidates: "
        f"{len(high_priority)} high-priority, "
        f"{len(high_priority_auto_static)} high-priority auto-static, "
        f"{len(oracle_candidates)} oracle candidates, "
        f"{len(needs_review)} needs-review, "
        f"{len(rejected)} rejected.",
    )
    return 0


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def enrich_candidate(item: dict[str, Any], max_pr_files_high_priority: int) -> None:
    locality = item.get("locality") or {}
    comment = item.get("comment") or {}
    evidence = item.get("addressed_evidence") or {}
    changed_files = [str(path) for path in item.get("changed_files", []) if path]
    test_paths = [path for path in changed_files if TEST_PATH_RE.search(path)]
    source_paths = [
        path
        for path in changed_files
        if not TEST_PATH_RE.search(path) and not DOC_PATH_RE.search(path) and not GENERATED_PATH_RE.search(path)
    ]
    locality_path = str(locality.get("path") or "")
    author = str(comment.get("author") or "")
    category = str(item.get("category") or "")

    reasons: list[str] = []
    reject_reasons: list[str] = []
    missing = [
        field
        for field, value in (
            ("id", item.get("id")),
            ("repo", item.get("repo")),
            ("pr_number", item.get("pr_number")),
            ("task_instruction", item.get("task_instruction")),
            ("category", category),
            ("locality.path", locality_path),
            ("comment.url", comment.get("url")),
        )
        if value in (None, "")
    ]
    if missing:
        reject_reasons.append("missing_required_fields:" + ",".join(missing))
    if is_bot_author(author):
        reject_reasons.append("bot_or_automation_author")
    if item.get("contains_suggestion") is True:
        reasons.append("contains_suggestion_block")
    if DOC_PATH_RE.search(locality_path):
        reasons.append("doc_locality")
    if GENERATED_PATH_RE.search(locality_path):
        reject_reasons.append("generated_or_vendor_locality")
    if len(str(item.get("task_instruction") or "").strip()) < 12:
        reject_reasons.append("instruction_too_short")

    thread_resolved = evidence.get("thread_resolved") is True
    region_touched = evidence.get("region_touched_after_comment") is True
    file_touched = evidence.get("file_touched_after_comment") is True
    if thread_resolved:
        reasons.append("thread_resolved")
    if region_touched:
        reasons.append("region_touched")
    if file_touched:
        reasons.append("file_touched")
    if test_paths:
        reasons.append("pr_has_test_changes")
    if category in AUTO_STATIC_CATEGORIES or item.get("gate_type") == "auto_static":
        reasons.append("auto_static_category")
    if not thread_resolved and not region_touched:
        reject_reasons.append("weak_addressed_evidence")
    if not file_touched:
        reject_reasons.append("commented_file_not_touched")

    oracle_strength = "no_correctness_proxy"
    if test_paths and (thread_resolved or region_touched):
        oracle_strength = "pr_tests_plus_review_evidence"
    elif test_paths:
        oracle_strength = "pr_tests_only"
    elif thread_resolved and region_touched:
        oracle_strength = "strong_review_evidence_no_tests"
    elif thread_resolved or region_touched:
        oracle_strength = "review_evidence_only"

    high_priority_conditions = (
        not reject_reasons
        and item.get("contains_suggestion") is not True
        and thread_resolved
        and region_touched
        and bool(test_paths)
        and len(changed_files) <= max_pr_files_high_priority
    )
    oracle_candidate_conditions = (
        not reject_reasons
        and item.get("contains_suggestion") is not True
        and bool(test_paths)
        and (thread_resolved or region_touched)
    )

    if reject_reasons:
        bucket = "rejected"
    elif high_priority_conditions:
        bucket = "high_priority"
    elif oracle_candidate_conditions:
        bucket = "oracle_candidate"
    else:
        bucket = "needs_review"

    score = priority_score(
        category=category,
        gate_type=str(item.get("gate_type") or ""),
        thread_resolved=thread_resolved,
        region_touched=region_touched,
        file_touched=file_touched,
        has_tests=bool(test_paths),
        contains_suggestion=item.get("contains_suggestion") is True,
        changed_file_count=len(changed_files),
        doc_locality=DOC_PATH_RE.search(locality_path) is not None,
    )

    item["inferred_language"] = infer_language(locality_path)
    item["correctness_oracle_proxy"] = {
        "oracle_strength": oracle_strength,
        "has_pr_test_changes": bool(test_paths),
        "test_change_paths": test_paths,
        "source_change_paths": source_paths,
        "changed_file_count": len(changed_files),
        "needs_command_curation": True,
    }
    item["_prefilter"] = {
        "bucket": bucket,
        "priority_score": score,
        "reasons": reasons,
        "reject_reasons": reject_reasons,
    }


def priority_score(
    category: str,
    gate_type: str,
    thread_resolved: bool,
    region_touched: bool,
    file_touched: bool,
    has_tests: bool,
    contains_suggestion: bool,
    changed_file_count: int,
    doc_locality: bool,
) -> int:
    score = 0
    if thread_resolved:
        score += 20
    if region_touched:
        score += 25
    if file_touched:
        score += 10
    if has_tests:
        score += 25
    if category in AUTO_STATIC_CATEGORIES or gate_type == "auto_static":
        score += 10
    if contains_suggestion:
        score -= 25
    if doc_locality:
        score -= 10
    if changed_file_count > 80:
        score -= 10
    if changed_file_count > 150:
        score -= 15
    return score


def is_bot_author(author: str) -> bool:
    lowered = author.lower()
    return any(pattern in lowered for pattern in BOT_AUTHOR_PATTERNS)


def infer_language(path: str) -> str:
    lowered = path.lower()
    for suffix, language in sorted(EXTENSION_LANGUAGE.items(), key=lambda pair: -len(pair[0])):
        if lowered.endswith(suffix):
            return language
    return "Other"


def diversity_sample(items: list[dict[str, Any]], sample_size: int) -> list[dict[str, Any]]:
    ordered = sorted(
        items,
        key=lambda item: (-int((item.get("_prefilter") or {}).get("priority_score", 0)), item.get("id", "")),
    )
    groups: dict[tuple[str, str, str], deque[dict[str, Any]]] = defaultdict(deque)
    for item in ordered:
        key = (
            item.get("inferred_language", "Other"),
            item.get("category", "unknown"),
            item.get("repo", "unknown"),
        )
        groups[key].append(item)
    keys = sorted(groups, key=lambda key: (key[0] == "Other", key[0], key[1], key[2]))
    sample: list[dict[str, Any]] = []
    while keys and len(sample) < sample_size:
        next_keys = []
        for key in keys:
            queue = groups[key]
            if queue and len(sample) < sample_size:
                sample.append(queue.popleft())
            if queue:
                next_keys.append(key)
        keys = next_keys
    return sample


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
        "pr_number",
        "category",
        "gate_type",
        "language",
        "priority_score",
        "oracle_strength",
        "changed_file_count",
        "test_change_count",
        "path",
        "comment_author",
        "comment_url",
        "task_instruction",
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for item in items:
            locality = item.get("locality") or {}
            comment = item.get("comment") or {}
            proxy = item.get("correctness_oracle_proxy") or {}
            prefilter = item.get("_prefilter") or {}
            writer.writerow(
                {
                    "id": item.get("id", ""),
                    "repo": item.get("repo", ""),
                    "pr_number": item.get("pr_number", ""),
                    "category": item.get("category", ""),
                    "gate_type": item.get("gate_type", ""),
                    "language": item.get("inferred_language", ""),
                    "priority_score": prefilter.get("priority_score", ""),
                    "oracle_strength": proxy.get("oracle_strength", ""),
                    "changed_file_count": proxy.get("changed_file_count", ""),
                    "test_change_count": len(proxy.get("test_change_paths", [])),
                    "path": locality.get("path", ""),
                    "comment_author": comment.get("author", ""),
                    "comment_url": comment.get("url", ""),
                    "task_instruction": item.get("task_instruction", ""),
                }
            )


def write_report(
    path: Path,
    candidates: list[dict[str, Any]],
    high_priority: list[dict[str, Any]],
    high_priority_auto_static: list[dict[str, Any]],
    oracle_candidates: list[dict[str, Any]],
    needs_review: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
    sample: list[dict[str, Any]],
) -> None:
    report = {
        "total_candidates": len(candidates),
        "high_priority": summarize(high_priority),
        "high_priority_auto_static": summarize(high_priority_auto_static),
        "oracle_candidates": summarize(oracle_candidates),
        "needs_review": summarize(needs_review),
        "rejected": summarize(rejected),
        "annotation_sample_high_priority": summarize(sample),
        "reject_reasons": dict(
            Counter(
                reason
                for item in rejected
                for reason in (item.get("_prefilter") or {}).get("reject_reasons", [])
            ).most_common()
        ),
        "oracle_strengths": dict(
            Counter((item.get("correctness_oracle_proxy") or {}).get("oracle_strength", "") for item in candidates)
            .most_common()
        ),
    }
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(items),
        "auto_static": sum(1 for item in items if item.get("gate_type") == "auto_static"),
        "categories": dict(Counter(item.get("category", "unknown") for item in items).most_common()),
        "languages": dict(Counter(item.get("inferred_language", "Other") for item in items).most_common()),
        "repos": dict(Counter(item.get("repo", "unknown") for item in items).most_common(20)),
    }


if __name__ == "__main__":
    raise SystemExit(main())
