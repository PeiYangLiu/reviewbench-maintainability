"""Enrich rendered benchmark cases with the "before" code and the reference fix.

For each case we add two pieces of evidence that make the task understandable:

1. ``before_window`` - the source around the commented line at ``start_sha``
   (the code the reviewer is complaining about), with real line numbers and the
   anchor / referenced lines marked.
2. ``fix_patch`` - the diff from ``start_sha`` to ``target_sha`` for the
   commented file, filtered to the hunks near the anchor line so unrelated PR
   changes are dropped. This is how the PR actually addressed the comment.

Both are fetched with the GitHub CLI (``gh``). Results are written back as an
``evidence`` field on each case so the renderer can display a clear
concern -> before -> fix story.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from reviewbench_maintainability.evidence import focus_patch, two_dot_file_diff


REFERENCED_LINE_RE = re.compile(r"\blines?\s+(\d+)", re.IGNORECASE)
CONTEXT_BEFORE = 16
CONTEXT_AFTER = 8


def gh_json(endpoint: str, jq: str | None = None, retries: int = 4) -> Any:
    args = ["gh", "api", endpoint]
    if jq:
        args += ["--jq", jq]
    delay = 3.0
    last_err = ""
    for attempt in range(retries):
        proc = subprocess.run(args, capture_output=True, text=True)
        if proc.returncode == 0:
            out = proc.stdout.strip()
            if not out:
                return None
            return out if jq else json.loads(out)
        last_err = (proc.stderr or proc.stdout).strip()
        # Retry on transient rate-limit / abuse / server errors.
        if any(tok in last_err for tok in ("rate limit", "was submitted too quickly", "abuse", "HTTP 403", "HTTP 429", "HTTP 502", "HTTP 503")):
            if attempt < retries - 1:
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
        break
    raise RuntimeError(last_err)


class MissingRefError(RuntimeError):
    """The commit/ref itself is unreachable (e.g. force-pushed away)."""


def fetch_file_text(repo: str, path: str, ref: str) -> str | None:
    """Return file text at a ref.

    Returns None when the file is absent at an otherwise-valid commit (added or
    deleted file). Raises MissingRefError when the commit itself is gone, so
    callers can mark the case invalid instead of treating ``before`` as empty
    (which would render a real file as spurious all-additions).
    """
    try:
        raw = gh_json(f"/repos/{repo}/contents/{path}?ref={ref}", jq=".content")
    except RuntimeError as exc:
        msg = str(exc)
        if "No commit found for the ref" in msg or "No commit found" in msg:
            raise MissingRefError(ref) from exc
        if "Not Found" in msg or "HTTP 404" in msg:
            return None
        raise
    if not raw:
        return None
    return base64.b64decode(raw).decode("utf-8", errors="replace")


def fetch_fix(
    repo: str,
    path: str,
    start_sha: str,
    target_sha: str,
    base_sha: str | None,
    anchor: int,
    compare_cache: dict[tuple[str, str, str], Any],
) -> dict[str, Any]:
    """Return the focused reference fix: the true diff of the commented file
    between ``start_sha`` and ``target_sha``.

    Computed from the actual file blobs at both commits (two-dot), so it is
    immune to rebases/force-pushes that make GitHub's three-dot ``compare`` show
    code already present at ``start_sha`` as spurious additions. Falls back to
    the compare API only when a blob cannot be fetched (e.g. renamed file).
    """

    try:
        before = fetch_file_text(repo, path, start_sha)
        after = fetch_file_text(repo, path, target_sha)
    except MissingRefError as exc:
        return {
            "text": "",
            "focused": False,
            "hunk_count": 0,
            "source": "invalid",
            "missing_ref": str(exc),
            "note": f"commit {str(exc)[:12]} 不可达（已被 force-push/删除），无法构造 before/after，此 case 应剔除",
        }
    if before is not None or after is not None:
        text = two_dot_file_diff(before, after)
        if not text.strip():
            return {
                "text": "",
                "focused": False,
                "hunk_count": 0,
                "source": "start..target(blob)",
                "note": "start..target 该文件无变化（评论可能在最终版上，或经 thread 解决）",
            }
        focused = focus_patch(text, int(anchor))
        focused["source"] = "start..target(blob)"
        return focused

    # Fallback: file blob unavailable at both commits (e.g. rename); use compare.
    file_patch = file_patch_from_compare(repo, start_sha, target_sha, path, compare_cache)
    source = "comment..head(compare)"
    if not file_patch and base_sha:
        file_patch = file_patch_from_compare(repo, base_sha, target_sha, path, compare_cache)
        source = "base..head(compare)"
    if not file_patch:
        return {"text": "", "focused": False, "hunk_count": 0, "note": "file not in compare"}
    focused = focus_patch(file_patch, int(anchor))
    focused["source"] = source
    return focused


def fetch_before_window(repo: str, path: str, ref: str, anchor: int, extra_lines: list[int]) -> dict[str, Any]:
    raw = gh_json(f"/repos/{repo}/contents/{path}?ref={ref}", jq=".content")
    if not raw:
        raise RuntimeError("no content")
    text = base64.b64decode(raw).decode("utf-8", errors="replace")
    lines = text.splitlines()
    targets = [n for n in [anchor, *extra_lines] if n]
    lo = max(1, min(targets) - CONTEXT_BEFORE) if targets else 1
    hi = min(len(lines), (max(targets) if targets else 1) + CONTEXT_AFTER)
    window = [[n, lines[n - 1]] for n in range(lo, hi + 1)]
    return {
        "start_line": lo,
        "end_line": hi,
        "lines": window,
        "anchor": anchor,
        "referenced_lines": extra_lines,
        "total_lines": len(lines),
    }


def file_patch_from_compare(
    repo: str,
    base: str,
    head: str,
    path: str,
    compare_cache: dict[tuple[str, str, str], Any],
) -> str | None:
    key = (repo, base, head)
    if key not in compare_cache:
        compare_cache[key] = gh_json(f"/repos/{repo}/compare/{base}...{head}")
    compare = compare_cache[key] or {}
    for f in compare.get("files", []):
        if f.get("filename") == path or f.get("previous_filename") == path:
            return f.get("patch")
    return None


def enrich_case(item: dict[str, Any], compare_cache: dict[tuple[str, str, str], Any]) -> dict[str, Any]:
    task = item.get("task", {})
    rc = item.get("original_review_comment", {})
    repo = task.get("repo")
    path = (task.get("locality") or {}).get("path") or rc.get("path")
    start_sha = task.get("start_sha")
    target_sha = task.get("target_sha")
    anchor = rc.get("original_line") or rc.get("line") or (task.get("locality") or {}).get("line")
    body = rc.get("body") or ""
    referenced = sorted({int(n) for n in REFERENCED_LINE_RE.findall(body)} - {anchor})

    evidence: dict[str, Any] = {"fetch_error": None}

    if not (repo and path and start_sha and target_sha and anchor):
        evidence["fetch_error"] = "missing repo/path/sha/anchor"
        item["evidence"] = evidence
        return item

    try:
        evidence["before_window"] = fetch_before_window(repo, path, start_sha, int(anchor), referenced)
    except Exception as exc:  # noqa: BLE001 - record and continue
        evidence["before_error"] = str(exc)[:200]

    try:
        evidence["fix_patch"] = fetch_fix(
            repo, path, start_sha, target_sha, task.get("base_sha"), int(anchor), compare_cache
        )
    except Exception as exc:  # noqa: BLE001
        evidence["fix_error"] = str(exc)[:200]

    item["evidence"] = evidence
    return item


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="input", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    items = json.loads(args.input.read_text(encoding="utf-8"))
    compare_cache: dict[tuple[str, str, str], Any] = {}
    for i, item in enumerate(items, 1):
        repo = item.get("task", {}).get("repo")
        print(f"[{i}/{len(items)}] enriching {repo} ...", flush=True)
        enrich_case(item, compare_cache)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote enriched cases to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
