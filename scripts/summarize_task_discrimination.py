#!/usr/bin/env python3
"""Which tasks separate models, and which are simply broken?

A task every model passes and a task every model fails cost the same to run and
neither orders anything. They are not the same problem, though: one is
saturated, one may be impossible, and a task nobody passes splits again into
hard and broken.

The split that matters is whether the oracle passes at the unpatched start. If
it does not, no patch can succeed and the task is measuring nothing -- three
tasks from one PR failed this way for all 11 models, and for the SFT and RL
arms too. If it does, every model genuinely failed and the task marks the
current ceiling, which is worth keeping.
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os

BASELINE_FAILED = "baseline failed"

# status has five values and only one means the harness failed rather than the
# model: infra_retry. Scoring it as a failure turns "we never measured this"
# into "every model failed", which is how three tasks whose oracle needs an
# unavailable container came to look like the hardest in the set.
# candidate_missing and scope_error stay failures -- the model genuinely did
# not produce a usable patch.
NO_VERDICT = ("infra_retry",)


def load(root: str, pattern: str = "correctness-normalized/*.jsonl"):
    status: dict = collections.defaultdict(dict)
    rows: dict = collections.defaultdict(list)
    lane: dict = {}
    for path in sorted(glob.glob(os.path.join(root, pattern))):
        for line in open(path, encoding="utf-8"):
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            task = r.get("id")
            raw = r.get("status")
            status[task][r.get("model")] = None if raw in NO_VERDICT else raw == "pass"
            rows[task].append(r)
            lane[task] = r.get("lane")
    return status, rows, lane


def classify(models: dict, min_models: int) -> str:
    scored = {m: v for m, v in models.items() if v is not None}
    if not scored:
        return "no_verdict"
    if len(scored) < min_models:
        return "under_covered"
    passes = sum(scored.values())
    if passes == 0:
        return "none_passed"
    if passes == len(scored):
        return "all_passed"
    return "discriminating"


def baseline_failure_rate(records: list) -> float:
    """Share of models whose run died before the patch could matter."""
    if not records:
        return 0.0
    bad = sum(1 for r in records if BASELINE_FAILED in str(r.get("failure_root_cause") or ""))
    return bad / len(records)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root")
    parser.add_argument("--min-models", type=int, default=8)
    parser.add_argument(
        "--broken-threshold",
        type=float,
        default=0.5,
        help="share of models hitting a failed baseline before a task counts as broken",
    )
    args = parser.parse_args()
    status, rows, lane = load(args.root)
    if not status:
        raise SystemExit(f"no correctness rows under {args.root}")

    kinds: collections.Counter = collections.Counter()
    by_lane: dict = collections.defaultdict(collections.Counter)
    none_passed = []
    for task, models in status.items():
        kind = classify(models, args.min_models)
        kinds[kind] += 1
        by_lane[lane.get(task)][kind] += 1
        if kind == "none_passed":
            none_passed.append(task)

    total = sum(kinds.values())
    print(f"{args.root}: {total} tasks\n")
    for kind in ("discriminating", "all_passed", "none_passed", "under_covered", "no_verdict"):
        if kinds[kind]:
            print(f"  {kind:16s} {kinds[kind]:4d}  {100*kinds[kind]/total:5.1f}%")

    print(f"\n{'lane':10s} {'discrim':>8s} {'none':>6s} {'all':>5s}")
    for name, counts in sorted(by_lane.items(), key=lambda kv: -sum(kv[1].values())):
        print(
            f"{str(name):10s} {counts['discriminating']:8d} "
            f"{counts['none_passed']:6d} {counts['all_passed']:5d}"
        )

    broken = [t for t in none_passed if baseline_failure_rate(rows[t]) > args.broken_threshold]
    if broken:
        print(f"\nBROKEN -- oracle fails at the unpatched start, no patch can pass:")
        for task in broken:
            rate = baseline_failure_rate(rows[task])
            print(f"  [{lane.get(task)}] {task}  ({100*rate:.0f}% of models)")
        print("  These measure nothing. Fix the oracle or drop them.")
    hard = len(none_passed) - len(broken)
    if hard:
        print(f"\n{hard} task(s) nobody passed with a working baseline -- genuinely hard, keep.")


if __name__ == "__main__":
    main()
