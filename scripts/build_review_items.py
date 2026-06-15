"""Build rendered-style review items from scored (flat) candidate records.

The difficulty scorer and the candidate pools store flat task records, but the
review-page renderer expects items wrapped with ``original_pr`` and
``original_review_comment``. This script samples records by difficulty tier and
fetches the missing PR title/body and the raw review comment (body + diff hunk)
so the same rich page can be rendered for, e.g., a sample of hard tasks.

Output is a JSON list of items: {rank, difficulty, task, original_pr,
original_review_comment, why_kept}. Pipe it through ``enrich_cases`` and
``summarize_cases`` before ``render_review_page``.
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any

import enrich_cases


def fetch_pr(repo: str, number: int) -> dict[str, Any]:
    try:
        pr = enrich_cases.gh_json(f"/repos/{repo}/pulls/{number}") or {}
    except Exception as exc:  # noqa: BLE001
        return {"fetch_error": str(exc)[:200]}
    return {
        "title": pr.get("title"),
        "body": pr.get("body"),
        "author": (pr.get("user") or {}).get("login"),
        "created_at": pr.get("created_at"),
        "merged_at": pr.get("merged_at"),
        "url": pr.get("html_url"),
        "fetch_error": None,
    }


def fetch_comment(repo: str, comment_id: int, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        c = enrich_cases.gh_json(f"/repos/{repo}/pulls/comments/{comment_id}") or {}
    except Exception as exc:  # noqa: BLE001
        return {
            "author": fallback.get("author"),
            "body": "",
            "diff_hunk": (fallback.get("locality") or {}).get("diff_hunk"),
            "path": (fallback.get("locality") or {}).get("path"),
            "line": (fallback.get("locality") or {}).get("line"),
            "original_line": (fallback.get("locality") or {}).get("line"),
            "url": (fallback.get("comment") or {}).get("url"),
            "fetch_error": str(exc)[:200],
        }
    return {
        "author": (c.get("user") or {}).get("login"),
        "body": c.get("body") or "",
        "diff_hunk": c.get("diff_hunk"),
        "path": c.get("path"),
        "line": c.get("line"),
        "original_line": c.get("original_line"),
        "url": c.get("html_url"),
        "fetch_error": None,
    }


def build_why_kept(task: dict[str, Any], difficulty: dict[str, Any]) -> list[str]:
    ann = task.get("human_annotation", {})
    ev = task.get("addressed_evidence", {})
    why = []
    if difficulty.get("tier") and difficulty.get("tier") != "unknown":
        why.append(
            f"难度={difficulty['tier']}（score={difficulty.get('score')}）："
            f"修法 +{difficulty.get('added')}/-{difficulty.get('deleted')} 行，"
            f"hunks={difficulty.get('hunks')}，新增函数/类={difficulty.get('new_defs')}，"
            f"纯删除={difficulty.get('pure_deletion')}。"
        )
    why.append(
        f"人工验收 keep={ann.get('label_keep_for_benchmark')}，质量分={ann.get('label_quality_score_1_to_5')}/5。"
    )
    why.append(
        f"maintainability={ann.get('label_is_maintainability_comment')}，"
        f"任务清楚={ann.get('label_task_is_clear')}，局部定位={ann.get('label_task_localized_to_comment')}，"
        f"no-leak={ann.get('label_no_answer_leakage_after_sanitization')}。"
    )
    why.append(
        f"addressed: thread_resolved={ev.get('thread_resolved')}, "
        f"file_touched={ev.get('file_touched_after_comment')}, region_touched={ev.get('region_touched_after_comment')}。"
    )
    return why


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="inputs", type=Path, nargs="+", required=True, help="Scored JSONL file(s).")
    parser.add_argument("--out", type=Path, required=True, help="Output rendered-style JSON list.")
    parser.add_argument("--tier", choices=("easy", "medium", "hard"), help="Filter to this difficulty tier.")
    parser.add_argument("--no-suggestion", action="store_true", help="Keep only leak-free tasks (no suggestion block).")
    parser.add_argument("--n", type=int, default=10, help="Sample size.")
    parser.add_argument("--seed", type=int, default=20260613)
    parser.add_argument("--sort-by-score", action="store_true", help="Take highest-score instead of random sample.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    pool: dict[str, dict[str, Any]] = {}
    for path in args.inputs:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                tid = record.get("id") or record.get("task", {}).get("id") or json.dumps(record)[:80]
                pool[tid] = record  # dedup by id; later files win
    records = list(pool.values())
    if args.tier:
        records = [r for r in records if r.get("difficulty", {}).get("tier") == args.tier]
    if args.no_suggestion:
        records = [r for r in records if not r.get("difficulty", {}).get("contains_suggestion")]

    if args.sort_by_score:
        records.sort(key=lambda r: -r.get("difficulty", {}).get("score", 0))
        sample = records[: args.n]
    else:
        rng = random.Random(args.seed)
        sample = rng.sample(records, min(args.n, len(records)))
        sample.sort(key=lambda r: -r.get("difficulty", {}).get("score", 0))

    items = []
    for rank, record in enumerate(sample, 1):
        repo = record.get("repo")
        number = record.get("pr_number")
        comment_id = (record.get("comment") or {}).get("id")
        difficulty = record.get("difficulty", {})
        print(f"[{rank}/{len(sample)}] {repo}#{number} ({difficulty.get('tier')}) ...", flush=True)
        original_pr = fetch_pr(repo, number) if repo and number else {}
        original_comment = fetch_comment(repo, comment_id, record) if repo and comment_id else {}
        items.append(
            {
                "rank": rank,
                "difficulty": difficulty,
                "task": record,
                "original_pr": original_pr,
                "original_review_comment": original_comment,
                "why_kept": build_why_kept(record, difficulty),
            }
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(items)} items to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
