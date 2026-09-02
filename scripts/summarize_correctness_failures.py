#!/usr/bin/env python3
"""Group correctness failures by what actually went wrong.

`correctness` is a single pass/fail gate, which says a model is the
bottleneck without saying why. Splitting the failures answers a different
question: a model that never submits needs more budget, a model that
submits patches which break passing tests needs execution feedback, and
those two look identical on the gate.

The `regressed` bucket is the one that matters. It means the same tests
passed before the patch and failed after, so the task was solvable in that
environment and the patch is what broke it.
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os

# Ordered: the first match wins, so the more specific reason has to come
# first. `no_patch` before `apply_fail` because a run that never produced a
# patch also has no patch to apply, and reporting it as an apply failure
# would send someone to debug diff formatting instead of generation.
BUCKETS = (
    "passed",
    "regressed",
    "tests_failed",
    "no_patch",
    "apply_fail",
    "infra",
    "unclassified",
)

_BASELINE_CLEARED = ("baseline passed", "diagnostic passed")


def classify(record: dict) -> str:
    if record.get("status") == "pass":
        return "passed"
    cause = str(record.get("failure_root_cause") or "")
    if "did not finish with a patch" in cause:
        return "no_patch"
    if record.get("patch_apply") is False:
        return "apply_fail"
    if "prepare" in cause:
        return "infra"
    # The baseline clause is what separates "the patch broke it" from "it was
    # already broken", so a bare oracle failure must not count as a regression.
    if "exact-start" in cause and any(c in cause for c in _BASELINE_CLEARED):
        return "regressed"
    if "P2P" in cause:
        return "tests_failed"
    return "unclassified"


def tally(paths) -> dict:
    per: dict = collections.defaultdict(collections.Counter)
    for path in paths:
        for line in open(path, encoding="utf-8"):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            per[record.get("model")][classify(record)] += 1
    return per


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="+")
    parser.add_argument("--glob", default="correctness-normalized/*.jsonl")
    args = parser.parse_args()
    paths = [p for root in args.roots for p in sorted(glob.glob(os.path.join(root, args.glob)))]
    if not paths:
        raise SystemExit(f"no files matched {args.glob!r} under {args.roots}")
    per = tally(paths)

    print(f"{'model':34s}" + "".join(f"{b:>14s}" for b in BUCKETS) + f"{'n':>7s}")
    print("-" * (34 + 14 * len(BUCKETS) + 7))
    for model in sorted(per, key=lambda m: -per[m]["passed"]):
        counts = per[model]
        total = sum(counts.values())
        print(
            f"{str(model):34s}"
            + "".join(f"{100 * counts[b] / total:13.1f}%" for b in BUCKETS)
            + f"{total:7d}"
        )

    print("\nshare of failures that are the patch breaking passing tests:")
    for model in sorted(per, key=lambda m: -per[m]["passed"]):
        counts = per[model]
        failed = sum(counts.values()) - counts["passed"]
        if not failed:
            continue
        broke = counts["regressed"] + counts["tests_failed"]
        print(
            f"  {str(model):34s} {100 * broke / failed:5.1f}%"
            f"   (baseline-confirmed {100 * counts['regressed'] / failed:.1f}%)"
        )
    print(
        "\nunclassified is mostly raw build output that the root-cause matcher\n"
        "did not recognise, not a distinct failure mode, so the share above is\n"
        "a lower bound."
    )


if __name__ == "__main__":
    main()
