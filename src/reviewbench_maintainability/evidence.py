"""Extract review-local before/reference evidence from cached public git repos."""

from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import difflib
import json
from pathlib import Path
import re
import shutil
import subprocess
import threading
from typing import Any


HUNK_RE = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@",
    re.MULTILINE,
)
HUNK_WINDOW = 30
BEFORE_CONTEXT = 30
MAX_LOCAL_PATCH_CHARS = 20_000
MAX_FULL_PATCH_CHARS = 60_000
QUALITY_EVIDENCE_VERSION = "reviewbench-quality-evidence/v2"


def two_dot_file_diff(before: str | None, after: str | None) -> str:
    before_lines = (before or "").splitlines()
    after_lines = (after or "").splitlines()
    diff = difflib.unified_diff(before_lines, after_lines, lineterm="", n=3)
    return "\n".join(
        line for line in diff if not line.startswith(("--- ", "+++ "))
    )


def split_hunks(patch: str) -> list[tuple[int, int, str]]:
    matches = list(HUNK_RE.finditer(patch))
    hunks = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(patch)
        hunks.append(
            (
                int(match.group(1)),
                int(match.group(3)),
                patch[match.start() : end].rstrip("\n"),
            )
        )
    return hunks


def focus_patch(patch: str, anchor: int, window: int = HUNK_WINDOW) -> dict[str, Any]:
    hunks = split_hunks(patch)
    if not hunks:
        return {
            "text": patch[:MAX_LOCAL_PATCH_CHARS].strip(),
            "focused": False,
            "hunk_count": 0,
            "complete": len(patch) <= MAX_LOCAL_PATCH_CHARS,
            "dropped_hunks": 0,
            "truncated": len(patch) > MAX_LOCAL_PATCH_CHARS,
        }

    kept = []
    for old_start, new_start, text in hunks:
        body = text.splitlines()[1:]
        old_length = sum(1 for line in body if not line.startswith("+"))
        new_length = sum(1 for line in body if not line.startswith("-"))
        near = (
            abs(old_start - anchor) <= window
            or abs(new_start - anchor) <= window
            or old_start <= anchor <= old_start + old_length
            or new_start <= anchor <= new_start + new_length
        )
        if near:
            kept.append(text)
    focused = bool(kept)
    if not kept:
        kept = [hunk[2] for hunk in hunks[:2]]
    text = "\n".join(kept)
    return {
        "text": text[:MAX_LOCAL_PATCH_CHARS],
        "focused": focused,
        "hunk_count": len(hunks),
        "complete": len(kept) == len(hunks) and len(text) <= MAX_LOCAL_PATCH_CHARS,
        "dropped_hunks": len(hunks) - len(kept),
        "truncated": len(text) > MAX_LOCAL_PATCH_CHARS,
    }


def before_window(text: str, anchor: int, context: int = BEFORE_CONTEXT) -> str:
    lines = text.splitlines()
    if not lines:
        return ""
    start = max(1, anchor - context)
    end = min(len(lines), anchor + context)
    return "\n".join(f"{number}: {lines[number - 1]}" for number in range(start, end + 1))


class GitRepoCache:
    def __init__(self, root: Path, timeout_seconds: int = 900) -> None:
        self.root = root
        self.timeout_seconds = timeout_seconds
        self.root.mkdir(parents=True, exist_ok=True)

    def evidence_for(self, task: dict[str, Any]) -> dict[str, Any]:
        repo = str(task.get("repo") or "")
        path = str((task.get("locality") or {}).get("path") or "")
        start = str(task.get("start_sha") or "")
        target = str(task.get("target_sha") or "")
        anchor = int((task.get("locality") or {}).get("line") or 0)
        if not all((repo, path, start, target, anchor)):
            return {"status": "missing_task_fields"}

        try:
            repo_dir = self._ensure_clone(repo)
            self._ensure_commit(repo_dir, start)
            self._ensure_commit(repo_dir, target)
            before = self._show_file(repo_dir, start, path)
            after = self._show_file(repo_dir, target, path)
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            return {"status": "git_error", "error": str(exc)[:500]}
        if before is None:
            return {"status": "missing_before_file"}
        if after is None:
            return {"status": "missing_after_file"}

        patch = two_dot_file_diff(before, after)
        if not patch.strip():
            return {"status": "empty_reference_patch"}
        focused = focus_patch(patch, anchor)
        return {
            "version": QUALITY_EVIDENCE_VERSION,
            "status": "ready",
            "before": before_window(before, anchor),
            "patch": focused["text"],
            "full_patch": patch[:MAX_FULL_PATCH_CHARS],
            "patch_focused": focused["focused"],
            "patch_complete": len(patch) <= MAX_FULL_PATCH_CHARS,
            "patch_hunk_count": focused["hunk_count"],
            "patch_dropped_hunks": focused["dropped_hunks"],
            "patch_truncated": len(patch) > MAX_FULL_PATCH_CHARS,
            "local_patch_truncated": focused.get("truncated", False),
        }

    def _ensure_clone(self, repo: str) -> Path:
        destination = self.root / _repo_slug(repo)
        git_dir = destination / ".git"
        if git_dir.exists():
            return destination
        if destination.exists():
            shutil.rmtree(destination)
        destination.mkdir(parents=True)
        initialized = _run(
            ["git", "init", "--quiet"],
            cwd=destination,
            timeout=120,
        )
        if initialized.returncode != 0:
            raise RuntimeError(
                f"git init failed for {repo}: "
                f"{(initialized.stderr or initialized.stdout)[-300:]}"
            )
        completed = _run(
            [
                "git",
                "remote",
                "add",
                "origin",
                f"https://github.com/{repo}.git",
            ],
            cwd=destination,
            timeout=120,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"remote setup failed for {repo}: "
                f"{(completed.stderr or completed.stdout)[-300:]}"
            )
        return destination

    def _ensure_commit(self, repo_dir: Path, commit: str) -> None:
        present = _run(
            ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
            cwd=repo_dir,
            timeout=120,
        )
        if present.returncode == 0:
            return
        fetched = _run(
            [
                "git",
                "fetch",
                "--quiet",
                "--filter=blob:none",
                "--no-tags",
                "origin",
                commit,
            ],
            cwd=repo_dir,
            timeout=self.timeout_seconds,
        )
        if fetched.returncode != 0:
            raise RuntimeError(
                f"fetch failed for {commit[:12]}: "
                f"{(fetched.stderr or fetched.stdout)[-300:]}"
            )

    def _show_file(self, repo_dir: Path, commit: str, path: str) -> str | None:
        completed = _run(
            ["git", "show", f"{commit}:{path}"],
            cwd=repo_dir,
            timeout=self.timeout_seconds,
        )
        if completed.returncode != 0:
            return None
        return completed.stdout


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="input", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repo-cache", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    records = [
        json.loads(line)
        for line in args.input.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if args.limit:
        records = records[: args.limit]
    prior: dict[str, dict[str, Any]] = {}
    if args.resume and args.out.exists():
        for line in args.out.read_text(encoding="utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                prior[str(item.get("id") or "")] = item

    by_repo: dict[str, list[dict[str, Any]]] = defaultdict(list)
    output: dict[str, dict[str, Any]] = {}
    for record in records:
        task_id = str(record.get("id") or "")
        prior_evidence = (prior.get(task_id) or {}).get("quality_evidence") or {}
        if (
            task_id in prior
            and prior_evidence.get("status") == "ready"
            and prior_evidence.get("patch_complete") is True
            and prior_evidence.get("version") == QUALITY_EVIDENCE_VERSION
        ):
            output[task_id] = prior[task_id]
        else:
            by_repo[str(record.get("repo") or "")].append(record)

    lock = threading.Lock()

    def process_repo(repo_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cache = GitRepoCache(args.repo_cache)
        processed = []
        for record in repo_records:
            item = dict(record)
            item["quality_evidence"] = cache.evidence_for(record)
            processed.append(item)
        return processed

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(process_repo, repo_records): repo
            for repo, repo_records in by_repo.items()
        }
        for future in as_completed(futures):
            repo = futures[future]
            try:
                processed = future.result()
            except Exception as exc:  # noqa: BLE001 - persist whole-repo failure
                processed = []
                for record in by_repo[repo]:
                    item = dict(record)
                    item["quality_evidence"] = {
                        "status": "repo_error",
                        "error": str(exc)[:500],
                    }
                    processed.append(item)
            with lock:
                for item in processed:
                    output[str(item.get("id") or "")] = item
                _write_ordered(args.out, records, output)
            ready = sum(
                (item.get("quality_evidence") or {}).get("status") == "ready"
                for item in processed
            )
            print(f"{repo}: {ready}/{len(processed)} evidence-ready", flush=True)
    return 0


def _write_ordered(
    path: Path,
    records: list[dict[str, Any]],
    output: dict[str, dict[str, Any]],
) -> None:
    items = [
        output[str(record.get("id") or "")]
        for record in records
        if str(record.get("id") or "") in output
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in items)
        + ("\n" if items else ""),
        encoding="utf-8",
    )
    temporary.replace(path)


def _repo_slug(repo: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "__", repo)


def _run(
    command: list[str],
    cwd: Path | None,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )


if __name__ == "__main__":
    raise SystemExit(main())
