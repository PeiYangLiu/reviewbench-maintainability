#!/usr/bin/env python3
"""Materialize the human golden fix as a judgeable baseline.

The maintainability gate in `judge_release_patches.py` is an LLM judge, so the
absolute Resolved@1 numbers are only interpretable against a ceiling. The real
merged human fix is that ceiling: it is the change a maintainer actually landed
in response to the same reviewer comment.

This command extracts, for every released task, the human diff restricted to the
single anchor production file that solver models were allowed to edit, and emits
it in the exact `generation_results.jsonl` shape the judge already consumes. The
judge can then be run unchanged, so the golden patch and the model patches are
scored by an identical prompt and an identical gate.

    python scripts/build_golden_baseline.py \
      --tasks data/releases/reserves/20260728/cumulative_preview.jsonl \
      --repo-cache /path/to/repos \
      --out-dir /path/to/golden-baseline

    python scripts/judge_release_patches.py \
      --tasks data/releases/reserves/20260728/cumulative_preview.jsonl \
      --repo-cache /path/to/repos \
      --generation golden-human=/path/to/golden-baseline/generation_results.jsonl \
      --out /path/to/golden-baseline/judgments.jsonl \
      --raw-dir /path/to/golden-baseline/judge_raw
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

PROTOCOL = "human-golden/anchor-file-v1"
LOCAL_PROTOCOL = "human-golden/review-local-v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--repo-cache", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument(
        "--mode",
        choices=("review-local", "anchor-file"),
        default="review-local",
        help=(
            "review-local uses the reviewer-comment-scoped hunk that the two "
            "quality judges certified; anchor-file uses the whole human change "
            "to the anchor file across the entire pull request, which also "
            "carries unrelated work and is therefore a lower bound."
        ),
    )
    parser.add_argument(
        "--model-name",
        default="golden-human",
        help="Solver name recorded for the human baseline.",
    )
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def repo_dir(cache: Path, repo: str) -> Path:
    return cache / repo.replace("/", "__")


def anchor_diff(repo: Path, start: str, target: str, path: str) -> str:
    """Human change to the anchor file only, as a normal git patch."""
    result = subprocess.run(
        [
            "git", "-C", str(repo), "diff", "--no-color", "--binary",
            "--find-renames", start, target, "--", path,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip()[:300] or "git diff failed")
    return result.stdout


def review_local_patch(task: dict[str, Any], path: str) -> str:
    """Reviewer-comment-scoped human fix, as a normal git patch.

    `quality_evidence.patch` is the localized reference hunk the two quality
    judges saw when they certified the task. It is stored without a git header,
    so one is added to keep the format identical to a solver patch.
    """
    evidence = task.get("quality_evidence") or {}
    hunks = evidence.get("patch") or ""
    if not hunks.strip():
        return ""
    header = f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
    return header + hunks if not hunks.startswith("diff --git ") else hunks


def changed_lines(patch: str) -> int:
    return sum(
        1
        for line in patch.splitlines()
        if (line.startswith("+") or line.startswith("-"))
        and not line.startswith(("+++", "---"))
    )


def main() -> int:
    args = parse_args()
    tasks = load_jsonl(args.tasks)
    patches = args.out_dir / "patches"
    patches.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    empty: list[str] = []
    failed: list[dict[str, str]] = []
    protocol = LOCAL_PROTOCOL if args.mode == "review-local" else PROTOCOL
    for task in tasks:
        path = (task.get("locality") or {}).get("path")
        repo = repo_dir(args.repo_cache, task["repo"])
        record: dict[str, Any] = {
            "id": task["id"],
            "model": args.model_name,
            "repo": task["repo"],
            "protocol": protocol,
            "path": path,
            "start_sha": task["start_sha"],
            "target_sha": task["target_sha"],
        }
        try:
            if args.mode == "review-local":
                patch = review_local_patch(task, path)
            else:
                patch = anchor_diff(repo, task["start_sha"], task["target_sha"], path)
        except Exception as error:
            failed.append({"id": task["id"], "error": str(error)})
            record.update(status="diff_failed", apply_ok=False, error=str(error)[:300])
            rows.append(record)
            continue
        if not patch.strip():
            # Nothing scoped to the reviewer comment, or the anchor file itself
            # is unchanged between the two commits.
            empty.append(task["id"])
            record.update(status="empty_golden_patch", apply_ok=False)
            rows.append(record)
            continue
        patch_file = patches / f"{task['id']}.patch"
        patch_file.write_text(patch, encoding="utf-8")
        record.update(
            status="generated",
            apply_ok=True,
            patch_file=str(patch_file),
            changed_lines=changed_lines(patch),
        )
        rows.append(record)

    output = args.out_dir / "generation_results.jsonl"
    with output.open("w") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary = {
        "schema_version": "reviewbench-golden-baseline/v1",
        "mode": args.mode,
        "protocol": protocol,
        "tasks": len(tasks),
        "generated": sum(row["apply_ok"] for row in rows),
        "empty_golden_patch": empty,
        "diff_failed": failed,
        "generation_results": str(output),
    }
    (args.out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    print(
        f"generated {summary['generated']}/{len(tasks)} golden anchor patches; "
        f"empty={len(empty)} failed={len(failed)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
