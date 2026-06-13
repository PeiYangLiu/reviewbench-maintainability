#!/usr/bin/env python3
"""Merge, deduplicate, summarize, and diversity-sample candidate JSONL files."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict, deque
import glob
import json
from pathlib import Path
from typing import Any


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
    parser.add_argument("inputs", nargs="+", help="JSONL files or glob patterns.")
    parser.add_argument("--merged-jsonl", type=Path, required=True)
    parser.add_argument("--summary-json", type=Path, required=True)
    parser.add_argument("--sample-jsonl", type=Path)
    parser.add_argument("--sample-size", type=int, default=200)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    candidates = load_candidates(args.inputs)
    for candidate in candidates:
        candidate["inferred_language"] = infer_language(candidate)

    deduped = dedupe(candidates)
    deduped.sort(key=lambda item: (-float(item.get("confidence", 0.0)), item.get("id", "")))
    sample = diversity_sample(deduped, args.sample_size)
    summary = summarize(deduped, sample)

    write_jsonl(args.merged_jsonl, deduped)
    args.summary_json.parent.mkdir(parents=True, exist_ok=True)
    args.summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.sample_jsonl:
        write_jsonl(args.sample_jsonl, sample)
    print(
        f"Merged {summary['total_candidates']} candidates "
        f"from {summary['source_files']} files; sampled {summary['sample_size']}."
    )
    return 0


def load_candidates(patterns: list[str]) -> list[dict[str, Any]]:
    paths: list[Path] = []
    for pattern in patterns:
        matches = [Path(item) for item in glob.glob(pattern)]
        if matches:
            paths.extend(matches)
        else:
            paths.append(Path(pattern))

    candidates: list[dict[str, Any]] = []
    for path in sorted(set(paths)):
        if not path.exists():
            continue
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            item = json.loads(line)
            item["_source_file"] = str(path)
            item["_source_line"] = line_number
            candidates.append(item)
    return candidates


def dedupe(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for item in candidates:
        candidate_id = item.get("id")
        if not candidate_id:
            continue
        existing = by_id.get(candidate_id)
        if existing is None or float(item.get("confidence", 0.0)) > float(existing.get("confidence", 0.0)):
            by_id[candidate_id] = item
    return list(by_id.values())


def infer_language(candidate: dict[str, Any]) -> str:
    path = ((candidate.get("locality") or {}).get("path") or "").lower()
    for suffix, language in sorted(EXTENSION_LANGUAGE.items(), key=lambda pair: -len(pair[0])):
        if path.endswith(suffix):
            return language
    return "Other"


def diversity_sample(candidates: list[dict[str, Any]], sample_size: int) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], deque[dict[str, Any]]] = defaultdict(deque)
    for item in candidates:
        key = (
            item.get("inferred_language", "Other"),
            item.get("category", "unknown"),
            item.get("repo", "unknown"),
        )
        groups[key].append(item)

    ordered_keys = sorted(
        groups,
        key=lambda key: (
            key[0] == "Other",
            key[0],
            key[1],
            key[2],
        ),
    )
    sample: list[dict[str, Any]] = []
    while ordered_keys and len(sample) < sample_size:
        next_keys = []
        for key in ordered_keys:
            queue = groups[key]
            if queue and len(sample) < sample_size:
                sample.append(queue.popleft())
            if queue:
                next_keys.append(key)
        ordered_keys = next_keys
    return sample


def summarize(candidates: list[dict[str, Any]], sample: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "total_candidates": len(candidates),
        "sample_size": len(sample),
        "source_files": len({item.get("_source_file") for item in candidates}),
        "repos": dict(Counter(item.get("repo", "unknown") for item in candidates).most_common()),
        "languages": dict(Counter(item.get("inferred_language", "Other") for item in candidates).most_common()),
        "categories": dict(Counter(item.get("category", "unknown") for item in candidates).most_common()),
        "gate_types": dict(Counter(item.get("gate_type", "unknown") for item in candidates).most_common()),
        "auto_static_candidates": sum(1 for item in candidates if item.get("gate_type") == "auto_static"),
        "resolved_threads": sum(1 for item in candidates if (item.get("addressed_evidence") or {}).get("thread_resolved") is True),
        "region_touched": sum(1 for item in candidates if (item.get("addressed_evidence") or {}).get("region_touched_after_comment") is True),
        "file_touched": sum(1 for item in candidates if (item.get("addressed_evidence") or {}).get("file_touched_after_comment") is True),
        "top_candidates": [
            {
                "id": item.get("id"),
                "repo": item.get("repo"),
                "category": item.get("category"),
                "language": item.get("inferred_language"),
                "confidence": item.get("confidence"),
                "path": (item.get("locality") or {}).get("path"),
            }
            for item in candidates[:20]
        ],
    }


def write_jsonl(path: Path, items: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in items)
        + ("\n" if items else ""),
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
