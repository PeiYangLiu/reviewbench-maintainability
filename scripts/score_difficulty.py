"""Score the difficulty of ReviewBench candidate tasks from their reference fix.

Maintainability candidates skew easy: many are pure deletions (remove a
duplicate line, drop an unused import) or are fully determined by a reviewer
suggestion block. To see how many trivial vs. substantive tasks a pool contains,
this script fetches the reference fix for each task (the focused start..head /
base..head diff for the commented file) and derives objective shape metrics:

- added / deleted lines, net added, hunk count near the anchor;
- new definitions introduced (extract function / class / method);
- whether the fix is a pure deletion.

These combine with the category and whether the comment carried a suggestion
into a difficulty score and an easy / medium / hard tier. The output is an
annotated JSONL plus a summary (tier distribution and tier x category cross-tab).

Reuses ``fetch_fix`` and the GitHub helpers from ``enrich_cases``.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import enrich_cases


HARD_CATEGORIES = {"complexity", "modularity", "api_design", "testability"}
MECHANICAL_CATEGORIES = {"dead_code", "duplication"}

NEW_DEF_RE = re.compile(
    r"^\+\s*(?:async\s+)?(?:def|func|function|fn|class|interface|struct|impl|trait|type|enum)\b"
    r"|^\+\s*(?:public|private|protected|internal)\b[^=;]*\([^;{]*\)\s*[:{]?\s*$"
)
HUNK_MARK_RE = re.compile(r"^@@ ", re.MULTILINE)


def coerce_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def fix_metrics(patch_text: str) -> dict[str, Any]:
    added = deleted = new_defs = 0
    for line in patch_text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
            if NEW_DEF_RE.match(line):
                new_defs += 1
        elif line.startswith("-"):
            deleted += 1
    hunks = len(HUNK_MARK_RE.findall(patch_text))
    total = added + deleted
    return {
        "added": added,
        "deleted": deleted,
        "total": total,
        "net_added": added - deleted,
        "hunks": hunks,
        "new_defs": new_defs,
        "pure_deletion": added <= 1 and deleted >= 1,
    }


def difficulty_score(metrics: dict[str, Any], category: str, contains_suggestion: bool) -> dict[str, Any]:
    total = metrics["total"]
    if total <= 3:
        base = 0
    elif total <= 10:
        base = 1
    elif total <= 30:
        base = 2
    else:
        base = 3

    structure = min(metrics["new_defs"], 2)
    structure += 1 if metrics["hunks"] >= 3 else 0
    structure += 1 if metrics["net_added"] >= 8 else 0

    cat_adj = 1 if category in HARD_CATEGORIES else (-1 if category in MECHANICAL_CATEGORIES else 0)

    spec_adj = 0
    if contains_suggestion:
        spec_adj -= 1
    if metrics["pure_deletion"]:
        spec_adj -= 1

    score = max(0, min(7, base + structure + cat_adj + spec_adj))
    tier = "easy" if score <= 1 else ("medium" if score <= 3 else "hard")
    return {
        "score": score,
        "tier": tier,
        "components": {"base_size": base, "structure": structure, "category": cat_adj, "spec": spec_adj},
    }


def task_view(record: dict[str, Any]) -> dict[str, Any]:
    """Accept either a flat task record or a wrapped {'task': ...} item."""

    if "task" in record and isinstance(record["task"], dict):
        return record["task"]
    return record


def anchor_line(task: dict[str, Any]) -> int | None:
    locality = task.get("locality") or {}
    comment = task.get("comment") or {}
    return locality.get("line") or comment.get("original_line") or locality.get("start_line")


def score_record(record: dict[str, Any], compare_cache: dict[tuple[str, str, str], Any]) -> dict[str, Any]:
    task = task_view(record)
    repo = task.get("repo")
    path = (task.get("locality") or {}).get("path")
    start_sha = task.get("start_sha")
    target_sha = task.get("target_sha")
    base_sha = task.get("base_sha")
    category = task.get("human_category") or task.get("category") or "other"
    contains_suggestion = coerce_bool(task.get("contains_suggestion"))
    anchor = anchor_line(task)

    diff: dict[str, Any] = {"category": category, "contains_suggestion": contains_suggestion}
    if not (repo and path and start_sha and target_sha and anchor):
        diff.update({"tier": "unknown", "error": "missing repo/path/sha/anchor"})
        record["difficulty"] = diff
        return record

    try:
        fix = enrich_cases.fetch_fix(repo, path, start_sha, target_sha, base_sha, int(anchor), compare_cache)
    except Exception as exc:  # noqa: BLE001
        diff.update({"tier": "unknown", "error": str(exc)[:200]})
        record["difficulty"] = diff
        return record

    text = fix.get("text") or ""
    if not text.strip():
        diff.update({"tier": "unknown", "error": fix.get("note") or "empty fix"})
        record["difficulty"] = diff
        return record

    metrics = fix_metrics(text)
    scored = difficulty_score(metrics, category, contains_suggestion)
    diff.update(metrics)
    diff.update(scored)
    diff["fix_source"] = fix.get("source")
    record["difficulty"] = diff
    return record


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    tiers = Counter(r["difficulty"]["tier"] for r in records)
    scored = [r for r in records if r["difficulty"]["tier"] != "unknown"]
    cross = Counter(
        (r["difficulty"]["tier"], r["difficulty"]["category"]) for r in scored
    )
    cross_tab: dict[str, dict[str, int]] = {}
    for (tier, cat), count in cross.items():
        cross_tab.setdefault(tier, {})[cat] = count
    n = len(scored) or 1
    return {
        "total": len(records),
        "scored": len(scored),
        "tier_counts": dict(tiers.most_common()),
        "tier_pct": {tier: round(100 * tiers.get(tier, 0) / len(records), 1) for tier in ("easy", "medium", "hard", "unknown")},
        "tier_by_category": {tier: dict(sorted(cats.items(), key=lambda x: -x[1])) for tier, cats in cross_tab.items()},
        "pure_deletion_rate": round(sum(1 for r in scored if r["difficulty"].get("pure_deletion")) / n, 3),
        "suggestion_rate": round(sum(1 for r in scored if r["difficulty"].get("contains_suggestion")) / n, 3),
        "median_total_changed": _median([r["difficulty"]["total"] for r in scored]),
        "mean_total_changed": round(sum(r["difficulty"]["total"] for r in scored) / n, 1),
    }


def _median(values: list[int]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    mid = len(s) // 2
    return float(s[mid]) if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def print_summary(summary: dict[str, Any]) -> None:
    print(f"\nscored {summary['scored']}/{summary['total']} tasks")
    print("tier distribution:")
    for tier in ("easy", "medium", "hard", "unknown"):
        c = summary["tier_counts"].get(tier, 0)
        print(f"  {tier:<8} {c:>4}  ({summary['tier_pct'].get(tier, 0)}%)")
    print(f"pure-deletion rate: {summary['pure_deletion_rate']}  suggestion rate: {summary['suggestion_rate']}")
    print(f"changed lines (focused fix): median={summary['median_total_changed']} mean={summary['mean_total_changed']}")
    print("tier x category:")
    for tier in ("hard", "medium", "easy"):
        cats = summary["tier_by_category"].get(tier, {})
        if cats:
            inline = ", ".join(f"{k}:{v}" for k, v in cats.items())
            print(f"  {tier:<7} {inline}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="input", type=Path, required=True, help="Candidate JSONL (flat task records).")
    parser.add_argument("--out", type=Path, required=True, help="Annotated JSONL output.")
    parser.add_argument("--report", type=Path, help="Summary JSON output.")
    parser.add_argument("--resume", action="store_true", help="Skip records already scored in --out.")
    parser.add_argument("--limit", type=int, help="Score only the first N records.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    records = load_jsonl(args.input)
    if args.limit:
        records = records[: args.limit]

    done: dict[str, dict[str, Any]] = {}
    if args.resume and args.out.exists():
        for prior in load_jsonl(args.out):
            tid = task_view(prior).get("id")
            if tid and prior.get("difficulty", {}).get("tier") not in (None, "unknown"):
                done[tid] = prior

    compare_cache: dict[tuple[str, str, str], Any] = {}
    out: list[dict[str, Any]] = []
    for i, record in enumerate(records, 1):
        tid = task_view(record).get("id", "")
        if tid in done:
            out.append(done[tid])
            continue
        repo = task_view(record).get("repo")
        print(f"[{i}/{len(records)}] {repo} ...", flush=True)
        out.append(score_record(record, compare_cache))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in out) + ("\n" if out else ""),
        encoding="utf-8",
    )
    summary = summarize(out)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print_summary(summary)
    print(f"\nWrote {len(out)} scored tasks to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
