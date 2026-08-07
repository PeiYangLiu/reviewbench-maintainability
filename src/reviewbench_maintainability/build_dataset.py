"""CLI for mining maintainability benchmark candidate tasks."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any

from .github_client import (
    GitHubClient,
    GitHubClientError,
    PullRequestMaterials,
    resolution_by_comment_id,
)
from .heuristics import Classification, classify_comment
from .models import CandidateTask, PullRequestRef, ReviewComment, candidate_id


HUNK_RE = re.compile(r"^@@ -(?P<old_start>\d+)(?:,(?P<old_len>\d+))? \+(?P<new_start>\d+)(?:,(?P<new_len>\d+))? @@", re.MULTILINE)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--pr", action="append", default=[], help="PR ref as owner/repo#123. Repeatable.")
    source.add_argument("--from-snapshot", type=Path, help="Read PR snapshots from this directory.")
    parser.add_argument("--repo", help="Restrict search query to owner/repo.")
    parser.add_argument("--org", help="Restrict search query to a GitHub org.")
    parser.add_argument("--query", default="is:pr is:merged refactor", help="GitHub issue search query.")
    parser.add_argument("--limit", type=int, default=25, help="Maximum PRs to fetch when searching.")
    parser.add_argument("--out", type=Path, required=True, help="Output JSONL path.")
    parser.add_argument("--report", type=Path, help="Output JSON report path.")
    parser.add_argument("--snapshot-dir", type=Path, help="Save raw API payloads here.")
    parser.add_argument("--gh", default="gh", help="GitHub CLI binary.")
    parser.add_argument(
        "--addressed-policy",
        default="resolved-or-region-touched",
        choices=(
            "resolved",
            "region-touched",
            "file-touched",
            "resolved-or-region-touched",
            "resolved-or-file-touched",
            "none",
        ),
        help="How to decide that a review comment was addressed.",
    )
    parser.add_argument("--include-replies", action="store_true", help="Include replies to review comments.")
    parser.add_argument("--include-golden-patch", action="store_true", help="Embed localized compare patch text.")
    parser.add_argument("--no-review-threads", action="store_true", help="Skip GraphQL review thread resolution fetch.")
    parser.add_argument("--fetch-issue-comments", action="store_true", help="Fetch issue comments; not needed for candidate mining.")
    parser.add_argument("--fetch-commits", action="store_true", help="Fetch PR commits; not needed for candidate mining.")
    parser.add_argument(
        "--patch-source",
        default="compare",
        choices=("compare", "pr-files"),
        help="Use exact start_sha..head compare patches, or faster PR file patches for touched evidence.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        materials = load_materials(args)
        candidates, report = build_dataset(args, materials)
    except (GitHubClientError, ValueError) as exc:
        print(f"reviewbench-build: {exc}", file=sys.stderr)
        return 2

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        "\n".join(candidate.to_json() for candidate in candidates) + ("\n" if candidates else ""),
        encoding="utf-8",
    )
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(f"Wrote {len(candidates)} candidates to {args.out}")
    return 0


def load_materials(args: argparse.Namespace) -> list[PullRequestMaterials]:
    if args.from_snapshot:
        directories = sorted(path for path in args.from_snapshot.iterdir() if path.is_dir())
        return [PullRequestMaterials.read_snapshot(directory) for directory in directories]

    client = GitHubClient(args.gh)
    refs = [PullRequestRef.parse(value) for value in args.pr]
    if not refs:
        query = scoped_query(args.query, repo=args.repo, org=args.org)
        refs = client.search_merged_prs(query, args.limit)

    materials: list[PullRequestMaterials] = []
    skipped: list[dict[str, str]] = []
    for ref in refs:
        try:
            item = client.fetch_pull_request(
                ref,
                include_threads=not args.no_review_threads,
                include_issue_comments=args.fetch_issue_comments,
                include_commits=args.fetch_commits,
            )
        except GitHubClientError as error:
            # A single pathological pull request must not abort the batch.
            # GitHub refuses to generate diffs for very large pull requests
            # ("this diff is taking too long to generate", HTTP 422), and one
            # such pull request used to fail the whole mining batch, which then
            # never completed and was retried forever.
            skipped.append({"pr": ref.full_name + f"#{ref.number}", "error": str(error)[:300]})
            continue
        if args.snapshot_dir:
            item.write_snapshot(args.snapshot_dir)
        materials.append(item)
    if skipped:
        print(
            f"skipped {len(skipped)} unreachable pull request(s): "
            + "; ".join(item["pr"] for item in skipped[:10]),
            file=sys.stderr,
            flush=True,
        )
    args.skipped_pull_requests = skipped
    return materials


def scoped_query(query: str, repo: str | None, org: str | None) -> str:
    pieces = [query]
    if repo and f"repo:{repo}" not in query:
        pieces.append(f"repo:{repo}")
    if org and f"org:{org}" not in query:
        pieces.append(f"org:{org}")
    return " ".join(pieces)


def build_dataset(
    args: argparse.Namespace,
    all_materials: list[PullRequestMaterials],
) -> tuple[list[CandidateTask], dict[str, Any]]:
    client = GitHubClient(args.gh)
    candidates: list[CandidateTask] = []
    report = {
        "prs_seen": 0,
        "review_comments_seen": 0,
        "comments_classified": 0,
        "candidates_kept": 0,
        "drop_reasons": {},
    }

    for materials in all_materials:
        report["prs_seen"] += 1
        pull = materials.pull
        if not pull:
            bump(report, "missing_pull_payload")
            continue
        if not pull.get("merged_at"):
            bump(report, "not_merged")
            continue
        compare_cache: dict[tuple[str, str], dict[str, Any]] = {}
        for raw_comment in materials.review_comments:
            report["review_comments_seen"] += 1
            comment = ReviewComment.from_api(raw_comment)
            task = build_candidate_for_comment(
                args=args,
                client=client,
                materials=materials,
                pull=pull,
                comment=comment,
                compare_cache=compare_cache,
                report=report,
            )
            if task:
                candidates.append(task)

    report["candidates_kept"] = len(candidates)
    report["skipped_pull_requests"] = getattr(args, "skipped_pull_requests", [])
    return candidates, report


def build_candidate_for_comment(
    args: argparse.Namespace,
    client: GitHubClient,
    materials: PullRequestMaterials,
    pull: dict[str, Any],
    comment: ReviewComment,
    compare_cache: dict[tuple[str, str], dict[str, Any]],
    report: dict[str, Any],
) -> CandidateTask | None:
    if comment.in_reply_to_id and not args.include_replies:
        bump(report, "reply_comment")
        return None
    if not comment.path:
        bump(report, "missing_path")
        return None

    classifications = classify_comment(comment.body, comment.author)
    if not classifications:
        bump(report, "not_maintainability_comment")
        return None
    report["comments_classified"] += 1
    classification = classifications[0]

    head_sha = nested_get(pull, ["head", "sha"])
    base_sha = nested_get(pull, ["base", "sha"])
    start_sha = comment.original_commit_id or comment.commit_id
    if not start_sha or not head_sha:
        bump(report, "missing_sha")
        return None
    if start_sha == head_sha:
        bump(report, "comment_on_final_head")
        return None

    patch_info = compare_comment_to_head(
        args=args,
        client=client,
        materials=materials,
        start_sha=start_sha,
        head_sha=head_sha,
        comment=comment,
        compare_cache=compare_cache,
    )
    resolved_map = resolution_by_comment_id(materials.review_threads)
    thread_resolved = resolved_map.get(comment.id)
    addressed_evidence = {
        "thread_resolved": thread_resolved,
        "file_touched_after_comment": patch_info["file_touched"],
        "region_touched_after_comment": patch_info["region_touched"],
        "addressed_policy": args.addressed_policy,
    }
    if not passes_addressed_policy(args.addressed_policy, addressed_evidence):
        bump(report, "addressed_policy_not_met")
        return None

    repo = materials.ref.full_name
    changed_files = sorted({item.get("filename", "") for item in materials.files if item.get("filename")})
    task = CandidateTask(
        id=candidate_id(repo, materials.ref.number, comment.id, classification.category),
        repo=repo,
        pr_number=materials.ref.number,
        pr_url=pull.get("html_url") or f"https://github.com/{repo}/pull/{materials.ref.number}",
        task_instruction=classification.sanitized_body,
        category=classification.category,
        gate_type=classification.gate_type,
        confidence=final_confidence(classification, addressed_evidence, patch_info),
        start_sha=start_sha,
        target_sha=head_sha,
        merge_commit_sha=pull.get("merge_commit_sha"),
        base_sha=base_sha,
        matched_terms=classification.matched_terms,
        contains_suggestion=classification.contains_suggestion,
        locality={
            "path": comment.path,
            "line": comment.anchor_line,
            "start_line": comment.anchor_start_line,
            "diff_hunk": comment.diff_hunk,
        },
        addressed_evidence=addressed_evidence,
        changed_files=changed_files,
        comment={
            "id": comment.id,
            "url": comment.html_url,
            "author": comment.author,
            "created_at": comment.created_at,
            "updated_at": comment.updated_at,
        },
        oracles={
            "correctness": "run original project tests; curate per-repo commands before scored release",
            "maintainability": classification.gate_type,
            "static_metrics_scope": "commented file/region when available",
        },
        golden_patch=patch_info["patch"] if args.include_golden_patch else None,
    )
    return task


def compare_comment_to_head(
    args: argparse.Namespace,
    client: GitHubClient,
    materials: PullRequestMaterials,
    start_sha: str,
    head_sha: str,
    comment: ReviewComment,
    compare_cache: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    key = (start_sha, head_sha)
    if args.from_snapshot or args.patch_source == "pr-files":
        # Snapshots store PR-level file patches, not arbitrary start_sha..head compares.
        compare_payload = {"files": materials.files}
    else:
        if key not in compare_cache:
            compare_cache[key] = client.compare(materials.ref, start_sha, head_sha)
        compare_payload = compare_cache[key]

    files = compare_payload.get("files", []) if isinstance(compare_payload, dict) else []
    file_patch = None
    file_touched = False
    for item in files:
        if item.get("filename") == comment.path or item.get("previous_filename") == comment.path:
            file_touched = True
            file_patch = item.get("patch")
            break

    region_touched = False
    if file_patch and comment.anchor_line is not None:
        region_touched = patch_touches_line(file_patch, comment.anchor_line)

    return {
        "file_touched": file_touched,
        "region_touched": region_touched,
        "patch": file_patch,
    }


def patch_touches_line(patch: str, line: int) -> bool:
    for match in HUNK_RE.finditer(patch):
        old_start = int(match.group("old_start"))
        old_len = int(match.group("old_len") or "1")
        old_end = old_start + max(old_len, 1) - 1
        if old_start <= line <= old_end:
            return True
    return False


def passes_addressed_policy(policy: str, evidence: dict[str, Any]) -> bool:
    resolved = evidence["thread_resolved"] is True
    region = evidence["region_touched_after_comment"] is True
    file_touched = evidence["file_touched_after_comment"] is True
    if policy == "none":
        return True
    if policy == "resolved":
        return resolved
    if policy == "region-touched":
        return region
    if policy == "file-touched":
        return file_touched
    if policy == "resolved-or-region-touched":
        return resolved or region
    if policy == "resolved-or-file-touched":
        return resolved or file_touched
    raise ValueError(f"Unknown addressed policy: {policy}")


def final_confidence(
    classification: Classification,
    evidence: dict[str, Any],
    patch_info: dict[str, Any],
) -> float:
    confidence = classification.confidence
    if evidence["thread_resolved"] is True:
        confidence += 0.1
    if patch_info["region_touched"]:
        confidence += 0.08
    elif patch_info["file_touched"]:
        confidence += 0.03
    return round(min(0.99, confidence), 3)


def nested_get(payload: dict[str, Any], path: list[str]) -> Any:
    value: Any = payload
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def bump(report: dict[str, Any], reason: str) -> None:
    drop_reasons = report.setdefault("drop_reasons", {})
    drop_reasons[reason] = drop_reasons.get(reason, 0) + 1


if __name__ == "__main__":
    raise SystemExit(main())
