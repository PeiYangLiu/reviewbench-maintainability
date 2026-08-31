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


def load_votes(pattern):
    votes = collections.defaultdict(dict)
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
        maintainable = sum(judges.values()) >= needed
        if maintainable:
            per[model]["maint"] += 1
        if correctness.get((model, task)) == "pass":
            per[model]["correct"] += 1
            if maintainable:
                per[model]["final"] += 1
    return per


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
    votes = load_votes(args.judgements)
    correctness = load_correctness(args.correctness)
    per = board(votes, correctness, decision=args.decision)
    print(f"{'model':40s} {'judged':>7s} {'Maint':>6s} {'Correct':>8s} {'FINAL':>6s}")
    print("-" * 72)
    for final, model, counts in sorted(
        ((c["final"], m, c) for m, c in per.items()), reverse=True
    ):
        print(
            f"{model:40s} {counts['judged']:7d} {counts['maint']:6d} "
            f"{counts['correct']:8d} {counts['final']:6d}"
        )
