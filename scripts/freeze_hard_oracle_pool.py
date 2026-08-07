#!/usr/bin/env python3
"""Freeze a semantic-unique hard-candidate pool for Oracle engineering.

Review comments in one pull request can point to the same anchor file and the
same human fix. Those are distinct task IDs but not distinct benchmark
solutions, so validating every ID would duplicate expensive environment work.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--released", type=Path, action="append", default=[])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--exclusions", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def solution_fingerprint(task: dict[str, Any]) -> str:
    evidence = task.get("quality_evidence") or {}
    patch = str(evidence.get("full_patch") or evidence.get("patch") or "")
    patch = re.sub(r"^@@.*?@@", "@@", patch, flags=re.MULTILINE)
    patch = "\n".join(line.rstrip() for line in patch.splitlines())
    identity = "\n".join(
        (
            str(task.get("repo") or ""),
            str(task.get("pr_number") or ""),
            str((task.get("locality") or {}).get("path") or ""),
            patch,
        )
    )
    return hashlib.sha256(identity.encode()).hexdigest()


def preference(task: dict[str, Any]) -> tuple[Any, ...]:
    selection = task.get("hard_selection") or {}
    prefilter = task.get("_prefilter") or {}
    return (
        selection.get("gpt_hard") is True
        and selection.get("claude_hard") is True,
        selection.get("claude_strict_pass") is True,
        selection.get("claude_verdict_available") is True,
        int(prefilter.get("priority_score") or 0),
        str(task.get("id") or ""),
    )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    args = parse_args()
    candidates = load_jsonl(args.input)
    released: dict[str, str] = {}
    for path in args.released:
        for task in load_jsonl(path):
            released[solution_fingerprint(task)] = task["id"]

    groups: dict[str, list[dict[str, Any]]] = {}
    for task in candidates:
        groups.setdefault(solution_fingerprint(task), []).append(task)

    selected = []
    exclusions = []
    for fingerprint, tasks in sorted(groups.items()):
        if fingerprint in released:
            for task in tasks:
                exclusions.append(
                    {
                        "id": task["id"],
                        "reason": "overlaps_released_solution",
                        "representative": released[fingerprint],
                        "solution_fingerprint": fingerprint,
                    }
                )
            continue
        representative = max(tasks, key=preference)
        selected.append(representative)
        for task in tasks:
            if task["id"] != representative["id"]:
                exclusions.append(
                    {
                        "id": task["id"],
                        "reason": "duplicate_solution_in_pool",
                        "representative": representative["id"],
                        "solution_fingerprint": fingerprint,
                    }
                )

    selected.sort(key=lambda task: task["id"])
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w") as stream:
        for task in selected:
            stream.write(json.dumps(task, ensure_ascii=False, sort_keys=True) + "\n")
    args.exclusions.write_text(
        json.dumps(exclusions, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    )
    manifest = {
        "schema_version": "reviewbench-hard-oracle-pool/v1",
        "input": str(args.input),
        "input_records": len(candidates),
        "semantic_unique_records": len(selected),
        "excluded_records": len(exclusions),
        "released_sources": [str(path) for path in args.released],
        "output": str(args.out),
        "output_sha256": sha256(args.out),
        "exclusions": str(args.exclusions),
        "exclusions_sha256": sha256(args.exclusions),
        "dedup_key": "repo + pr_number + anchor_path + normalized full same-file reference diff",
    }
    args.manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
