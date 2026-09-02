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
import re

# Compiler and interpreter diagnostics. Test names routinely contain words like
# "Expected", so a bare keyword match misfiles them as build breaks -- the
# pattern has to look for the diagnostic's own shape.
_DIAGNOSTIC = re.compile(
    r"(^\s*(error|fatal error):)|(Parse error:)|(SyntaxError)|(IndentationError)"
    r"|(was not declared)|(cannot find symbol)|(no matching function)"
    r"|(unexpected token)|(invalid syntax)|(undefined reference)",
    re.I,
)
_TEST_NAME = re.compile(r"^[\w.]+#\w+$|^test[\w_]*$", re.I)

EXPECTED_LANES = ("native", "gopy", "jvm", "scripted", "tsjs", "rust", "csharp")


def broke_the_build(record: dict) -> bool:
    """True when the patch stopped the code compiling rather than failing tests.

    Worth separating because the fix differs by an order of magnitude: a build
    break is caught by a compiler in seconds with no test run, no oracle and no
    judge. In this data it is 94% of C/C++ regressions and 68% of PHP ones,
    while Go, Python and Java never break the build and fail on logic instead.
    """
    for entry in record.get("failed_tests") or []:
        text = str(entry)
        if _DIAGNOSTIC.search(text) and not _TEST_NAME.match(text.strip()):
            return True
    return False
import os

# Ordered: the first match wins, so the more specific reason has to come
# first. `no_patch` before `apply_fail` because a run that never produced a
# patch also has no patch to apply, and reporting it as an apply failure
# would send someone to debug diff formatting instead of generation.
BUCKETS = (
    "passed",
    "regressed",
    "build_broken",
    "tests_failed",
    "out_of_scope",
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
    # Editing outside the anchor is the model misreading the task, not the
    # patch failing to apply. Both set patch_apply false, and folding them
    # together sends the reader to diff tooling for a capability problem.
    if record.get("scope_ok") is False or ("anchor" in cause and "400" in cause):
        return "out_of_scope"
    if record.get("patch_apply") is False:
        return "apply_fail"
    if "prepare" in cause:
        return "infra"
    # The baseline clause is what separates "the patch broke it" from "it was
    # already broken", so a bare oracle failure must not count as a regression.
    if "exact-start" in cause and any(c in cause for c in _BASELINE_CLEARED):
        return "build_broken" if broke_the_build(record) else "regressed"
    if "P2P" in cause:
        return "tests_failed"
    # The legacy lane records the failed tests and the pass-to-pass counts but
    # writes no root cause, so falling through would file a plain test failure
    # under "we did not look".
    if record.get("failed_tests") and record.get("pass_to_pass_total"):
        return "tests_failed"
    return "unclassified"


def tally(paths, lanes=None) -> dict:
    per: dict = collections.defaultdict(collections.Counter)
    for path in paths:
        for line in open(path, encoding="utf-8"):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            per[record.get("model")][classify(record)] += 1
            if lanes is not None:
                lanes[record.get("lane")] += 1
    return per


def report_lane_coverage(lanes, expected=EXPECTED_LANES) -> None:
    """Warn when a lane is absent, because every percentage here is relative.

    Failure classes are not spread evenly across languages: build breaks are
    94% of C/C++ regressions and 0% of Go, Python and Java ones. Drop the
    native lane and build_broken falls from 20.6% to 8.1% with no other sign
    that anything changed, which reads as the model writing better code.
    """
    missing = [lane for lane in expected if not lanes.get(lane)]
    if not missing:
        return
    print(
        f"\nWARNING: no rows for lane(s) {', '.join(missing)}. Failure classes are"
        "\nlanguage-specific, so these percentages are not comparable against a run"
        "\nthat has them. Check correctness.complete.json."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="+")
    parser.add_argument("--glob", default="correctness-normalized/*.jsonl")
    args = parser.parse_args()
    paths = [p for root in args.roots for p in sorted(glob.glob(os.path.join(root, args.glob)))]
    if not paths:
        raise SystemExit(f"no files matched {args.glob!r} under {args.roots}")
    lanes: collections.Counter = collections.Counter()
    per = tally(paths, lanes)

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
    report_lane_coverage(lanes)


if __name__ == "__main__":
    main()
