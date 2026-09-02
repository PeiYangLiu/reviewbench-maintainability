"""Four-gate board from judgements plus normalized correctness.

The maintainability rule is imported from the judge script rather than
reimplemented: a local reimplementation that dropped `quality_1_to_5 >= 4`
inflated every model by 40-70 tasks and looked like a real jump.
"""
import collections
import glob
import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, "/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability/scripts")
REPO = Path("/data/v-peiyangliu/reviewbench-maintainability/reviewbench-maintainability")
spec = importlib.util.spec_from_file_location(
    "judge_release_patches", REPO / "scripts" / "judge_release_patches.py"
)
JUDGE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(JUDGE)


def load_votes(pattern, dropped=None):
    """Collect per-judge verdicts, optionally recording lost judgements.

    A record with no verdict is either the solver never producing a patch
    (a real failure, correctly worth zero) or the judge refusing to look at
    one it did produce (data loss). Only the second kind lands in `dropped`,
    because a task can silently leave the panel that way and the board would
    under-report it without saying so.
    """
    votes = collections.defaultdict(dict)
    for path in sorted(glob.glob(pattern)):
        for line in open(path, encoding="utf-8"):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = (record.get("solver_model"), record.get("id"))
            verdict = record.get("verdict")
            if not isinstance(verdict, dict):
                if dropped is not None and record.get("generation_error") is None:
                    dropped[key] = record.get("error") or "no verdict"
                continue
            votes[key][record.get("judge_model")] = JUDGE.verdict_passes(
                verdict, record.get("rubric") or "v2"
            )
    return votes


def load_correctness(dirs):
    status = {}
    for directory in dirs:
        for path in glob.glob(f"{directory}/correctness-normalized/*.jsonl"):
            for line in open(path, encoding="utf-8"):
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                model = record.get("generation_model") or record.get("model")
                status[(model, record.get("id"))] = record.get("status")
    return status


def board(votes, correctness, panel_size=3, decision="unanimous"):
    per = collections.defaultdict(collections.Counter)
    for (model, task), judges in votes.items():
        if len(judges) < panel_size:
            per[model]["short_panel"] += 1
            continue
        per[model]["judged"] += 1
        needed = panel_size if decision == "unanimous" else panel_size // 2 + 1
        passes = sum(judges.values())
        maintainable = passes >= needed
        if maintainable:
            per[model]["maint"] += 1
        # One vote short of the threshold. The ratio of these to scoring tasks
        # says how stable `final` is: above 100% the score swings widely on
        # resampling, which is the whole of P104.
        if passes == needed - 1:
            per[model]["boundary"] += 1
        if (model, task) in correctness:
            per[model]["scored"] += 1
        if correctness.get((model, task)) == "pass":
            per[model]["correct"] += 1
            per[model]["votes"] += passes
            if maintainable:
                per[model]["final"] += 1
    return per


def report_correctness_coverage(per):
    """Warn when arms were scored over different numbers of tasks.

    A lane that never ran shrinks every arm's denominator together, so the
    ordering and the relative gaps all survive and the board looks normal.
    The only visible trace is the count of tasks that reached the
    correctness stage, which is why it has to be asserted rather than shown.
    """
    scored = {model: counts["scored"] for model, counts in per.items()}
    if len(set(scored.values())) <= 1:
        return
    print("\nWARNING: arms were scored over different task counts, so their")
    print("totals are not comparable. Check correctness.complete.json.")
    for model, count in sorted(scored.items(), key=lambda kv: kv[1]):
        print(f"  {model:40s} {count:5d}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--judgements", default="judge-8k/*/judgements.jsonl")
    parser.add_argument("--correctness", action="append", required=True)
    parser.add_argument(
        "--decision",
        choices=("unanimous", "majority"),
        default="unanimous",
        help=(
            "the published board is unanimous; switching this silently moves "
            "every model by 40-70 tasks and reads as a real jump"
        ),
    )
    args = parser.parse_args()
    dropped = {}
    votes = load_votes(args.judgements, dropped)
    correctness = load_correctness(args.correctness)
    per = board(votes, correctness, decision=args.decision)
    print(
        f"{'model':40s} {'judged':>7s} {'Maint':>6s} {'Correct':>8s} "
        f"{'FINAL':>6s} {'votes':>6s} {'bndry':>6s}"
    )
    print("-" * 86)
    for final, model, counts in sorted(
        ((c["final"], m, c) for m, c in per.items()), reverse=True
    ):
        ratio = counts["boundary"] / counts["maint"] if counts["maint"] else 0.0
        print(
            f"{model:40s} {counts['judged']:7d} {counts['maint']:6d} "
            f"{counts['correct']:8d} {counts['final']:6d} "
            f"{counts['votes']:6d} {100 * ratio:5.0f}%"
        )
    # A refusal that a retry later covered leaves a full panel, so compare
    # against the verdicts that actually landed rather than trusting the
    # refusal record. Zero surviving judges is the worst case, not a skip.
    gaps = [k for k in dropped if len(votes.get(k, {})) < 3]
    if gaps:
        by_model = collections.Counter(model for model, _ in gaps)
        print(
            f"\nWARNING: {len(gaps)} task(s) lost a judge and are scored by an "
            f"incomplete panel:"
        )
        for model, count in by_model.most_common():
            print(f"  {model:40s} {count:4d}")
        print("  Verify these cannot change the ranking before publishing.")
    report_correctness_coverage(per)
