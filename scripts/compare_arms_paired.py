#!/usr/bin/env python3
"""Compare two arms gate by gate, paired and directional.

Marginal counts are not enough and nearly cost this line a correct conclusion.
Three runs of one unchanged adapter score 3, 1 and 1 on maintainability while a
trained arm scores 5 against its base's 0 -- the same magnitude, so a marginal
comparison calls both noise. Paired, the replicates disagree in both directions
(7 vs 3) while the trained arm wins every discordant task (11 vs 1, p=0.006).

So the output is always `(only A, only B, p)`, and the marginal counts are a
footnote (P99).
"""
from __future__ import annotations

import argparse
import collections
import glob
import importlib.util
import json
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def load_judge_module():
    sys.path.insert(0, str(REPO / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "judge_release_patches", REPO / "scripts" / "judge_release_patches.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_maintainability(
    pattern: str, panel: int = 3, metric: str = "unanimous"
) -> dict[str, dict[str, int]]:
    """Panel verdicts keyed by arm then task.

    `unanimous` reproduces the published board. `votes` returns how many judges
    passed the patch, and that difference decides whether an effect is visible
    at all: binarising at "all three" throws away every task one vote short,
    which at 14B is 15 tasks against 4 that score. Three identical runs then
    swing between 1 and 9 (CV 0.71) while the vote count holds at 127-137
    (CV 0.03), and the same comparison moves from p=0.21 to p=1e-6.
    """
    judge = load_judge_module()
    votes: dict[tuple[str, str], dict[str, bool]] = collections.defaultdict(dict)
    for path in sorted(glob.glob(pattern)):
        for line in open(path, encoding="utf-8"):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            verdict = record.get("verdict")
            if not isinstance(verdict, dict):
                continue
            key = (record.get("solver_model"), record.get("id"))
            votes[key][record.get("judge_model")] = judge.verdict_passes(
                verdict, record.get("rubric") or "v2"
            )
    result: dict[str, dict[str, int]] = collections.defaultdict(dict)
    for (arm, task), panel_votes in votes.items():
        # A short panel is missing data, not a rejection.
        if len(panel_votes) < panel:
            continue
        passes = sum(panel_votes.values())
        result[arm][task] = passes if metric == "votes" else int(passes >= panel)
    return result


def load_correctness(roots: list[str]) -> dict[str, dict[str, bool]]:
    lanes = ("gopy", "scripted", "tsjs", "csharp", "jvm", "rust", "native")
    result: dict[str, dict[str, bool]] = collections.defaultdict(dict)
    for root in roots:
        for lane in lanes:
            path = Path(root) / f"{lane}.jsonl"
            if not path.exists():
                continue
            for line in open(path, encoding="utf-8"):
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                status = str(record.get("status") or "").lower()
                passed = status == "pass" or record.get("correctness_ok") is True
                result[record.get("model")][record.get("id")] = passed
    return result


def exact_two_sided(only_a: int, only_b: int) -> float:
    """Exact binomial p under H0: a discordant task favours either arm equally.

    With five discordant tasks all one way the smallest attainable p is 0.0625,
    so a non-significant p here means underpowered, not absent -- which is why
    the counts are printed next to it rather than just the verdict.
    """
    total = only_a + only_b
    if total == 0:
        return 1.0
    tail = sum(math.comb(total, k) for k in range(min(only_a, only_b) + 1))
    return min(1.0, 2 * tail / 2**total)


def compare(a: dict[str, int], b: dict[str, int]) -> tuple[int, int, int, int, float]:
    """Totals and the paired directional test, for binary or graded scores."""
    shared = set(a) & set(b)
    only_a = sum(1 for task in shared if a[task] > b[task])
    only_b = sum(1 for task in shared if b[task] > a[task])
    return (
        sum(a[task] for task in shared),
        sum(b[task] for task in shared),
        only_a,
        only_b,
        exact_two_sided(only_a, only_b),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--judgements", default="judge-sftrl/*/judgements.jsonl")
    parser.add_argument(
        "--metric",
        choices=("unanimous", "votes"),
        default="unanimous",
        help=(
            "unanimous matches the published board; votes grades 0-3 and is "
            "what iteration should use -- it cut the replicate variance 23x"
        ),
    )
    parser.add_argument("--correctness", action="append", default=[])
    parser.add_argument(
        "--pair",
        action="append",
        required=True,
        metavar="ARM_A:ARM_B",
        help="two arm names to compare, before and after",
    )
    args = parser.parse_args()

    maint = load_maintainability(args.judgements, metric=args.metric)
    corr = load_correctness(args.correctness) if args.correctness else {}

    header = (
        f"{'comparison':38s} {'gate':6s} {'A':>4s} {'B':>4s} "
        f"{'onlyA':>6s} {'onlyB':>6s} {'p':>8s}"
    )
    print(header)
    print("-" * len(header))
    for spec in args.pair:
        left, _, right = spec.partition(":")
        rows = [("maint", maint.get(left, {}), maint.get(right, {}))]
        if corr:
            rows.append(("correct", corr.get(left, {}), corr.get(right, {})))
            final = {}
            for arm in (left, right):
                shared = set(maint.get(arm, {})) & set(corr.get(arm, {}))
                final[arm] = {t: maint[arm][t] and corr[arm][t] for t in shared}
            rows.append(("FINAL", final[left], final[right]))
        for gate, first, second in rows:
            if not first or not second:
                continue
            count_a, count_b, only_a, only_b, p = compare(first, second)
            mark = "**" if p < 0.05 else "  "
            print(
                f"{spec:38s} {gate:6s} {count_a:4d} {count_b:4d} "
                f"{only_a:6d} {only_b:6d} {p:8.4f}{mark}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
