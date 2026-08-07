#!/usr/bin/env python3
"""Label how many production files the reviewer-scoped human fix actually spans.

Solver models may only edit the single anchor production file, so a task is only
fairly solvable if the human fix is expressible in that one file. The whole
pull-request diff is a bad proxy for this: a merged PR bundles unrelated work
and its production-file count is dominated by noise.

This uses a tighter and purely mechanical definition: the fix is the set of
commits between `start_sha` and `target_sha` that actually touch the anchor
file, and its scope is the union of production files those commits change.

    python scripts/label_fix_scope.py \
      --tasks data/releases/reserves/20260728/cumulative_preview.jsonl \
      --repo-cache /path/to/repos \
      --out /path/to/fix_scope.jsonl
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

TEST_PATH = re.compile(
    r"(^|/)(tests?|spec|specs|__tests__|testdata|test_data|e2e|fixtures?)(/|$)"
    r"|(^|/)test_[^/]*$"
    r"|[._-]test\.[A-Za-z0-9]+$"
    r"|[._-]spec\.[A-Za-z0-9]+$"
    r"|Tests?\.(java|kt|cs|scala|swift)$",
    re.IGNORECASE,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--repo-cache", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return result.stdout if result.returncode == 0 else ""


def fix_commits(
    repo: Path, start: str, target: str, path: str, since: str | None
) -> list[str]:
    """The commit that answers the review comment, plus its immediate follow-ups.

    A pull request usually keeps evolving after a review comment is posted, so
    the union of every commit touching the anchor file overstates the fix. The
    response to the comment is the first commit after it that touches the anchor
    file; later commits are separate work.
    """
    revs = ["rev-list", "--reverse", f"{start}..{target}"]
    if since:
        revs += [f"--since={since}"]
    output = git(repo, *revs, "--", path)
    commits = output.split()
    return commits[:1]


def commit_files(repo: Path, commit: str) -> list[str]:
    output = git(
        repo, "show", "--pretty=format:", "--name-only", "--no-renames", commit
    )
    return [line for line in output.split("\n") if line.strip()]


def main() -> int:
    args = parse_args()
    tasks = load_jsonl(args.tasks)
    records = []
    for task in tasks:
        anchor = (task.get("locality") or {}).get("path")
        repo = args.repo_cache / task["repo"].replace("/", "__")
        since = (task.get("comment") or {}).get("created_at")
        commits = fix_commits(repo, task["start_sha"], task["target_sha"], anchor, since)
        touched: set[str] = set()
        for commit in commits:
            touched.update(commit_files(repo, commit))
        production = sorted(
            path for path in touched if path and not TEST_PATH.search(path)
        )
        records.append(
            {
                "id": task["id"],
                "repo": task["repo"],
                "anchor_path": anchor,
                "fix_commits": len(commits),
                "fix_commit": commits[0] if commits else None,
                "comment_created_at": since,
                "production_files": len(production),
                "production_paths": production[:50],
                "single_file_expressible": production == [anchor],
                "anchor_in_scope": anchor in production,
                "status": "ok" if commits else "no_fix_commit_found",
            }
        )
    with args.out.open("w") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    total = len(records)
    single = sum(record["single_file_expressible"] for record in records)
    unresolved = sum(record["status"] != "ok" for record in records)
    print(
        f"labelled {total} tasks: single_file_expressible={single} "
        f"({single / total * 100:.1f}%), no_fix_commit={unresolved}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
