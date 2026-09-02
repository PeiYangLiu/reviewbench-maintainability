#!/usr/bin/env python3
"""Audit a judge panel for rubber-stamp members and self-preference.

Two failures a panel can have that its own output never reveals:

A member with no marginal influence. gemini-3.6-flash vetoes 4 tasks its
co-judges both passed, against 627 for claude-opus-5, so removing it moves the
unanimous count by 0.3%. It is a third of the panel by name and nothing by
effect, which quietly turns three-judge agreement into two-judge agreement.

A member grading its own patches. Comparing its pass rate on itself against its
pass rate on others proves nothing, because judges are drawn from the same pool
as the solvers and a strong model is strong at both. The comparison has to be
against co-judges on the same solver; then against the judge's own model family
to separate self-preference from shared house style. opus-5 vetoes itself at
half the rate it vetoes its siblings, which is self-preference. sol is gentler
on gpt-5.5 than on itself, which is not.
"""
from __future__ import annotations

import argparse
import collections
import glob
import importlib.util
import json
import statistics
from pathlib import Path

_JUDGE_SCRIPT = Path(__file__).resolve().parent / "judge_release_patches.py"


def load_judge_module():
    spec = importlib.util.spec_from_file_location("judge_release_patches", _JUDGE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_votes(pattern: str) -> dict:
    judge = load_judge_module()
    votes: dict = collections.defaultdict(dict)
    for path in sorted(glob.glob(pattern)):
        who = Path(path).parent.name.split("__")[0]
        for line in open(path, encoding="utf-8"):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            verdict = record.get("verdict")
            if isinstance(verdict, dict):
                votes[(record.get("solver_model"), record.get("id"))][who] = (
                    judge.verdict_passes(verdict, record.get("rubric") or "v2")
                )
    return votes


def marginal_influence(votes: dict, panel_size: int = 3) -> dict:
    """Unanimous count with each judge removed, plus its solo vetoes.

    A judge whose removal barely changes the count is not supplying a second
    opinion, whatever the panel size claims.
    """
    full = [k for k, d in votes.items() if len(d) == panel_size]
    judges = sorted({j for d in votes.values() for j in d})
    unanimous = sum(1 for k in full if all(votes[k].values()))
    out = {"unanimous": unanimous, "panel": len(full), "judges": {}}
    for j in judges:
        without = sum(1 for k in full if all(v for jj, v in votes[k].items() if jj != j))
        solo = sum(
            1 for k in full if not votes[k][j] and all(v for jj, v in votes[k].items() if jj != j)
        )
        out["judges"][j] = {"without": without, "delta": without - unanimous, "solo_veto": solo}
    return out


def board_without(votes: dict, judge: str, panel_size: int = 3) -> collections.Counter:
    """Per-solver unanimous count with one judge removed.

    Solo-veto counts say how often a judge dissents; this says whether that
    dissent reaches the board. gemini dissents 4 times and moves no model's
    rank, so its third of the judging budget buys nothing -- a fact available
    from a recount, which nobody had run.
    """
    out: collections.Counter = collections.Counter()
    for (solver, _), d in votes.items():
        if len(d) != panel_size:
            continue
        if all(v for j, v in d.items() if j != judge):
            out[solver] += 1
    return out


def rank_shift(before: collections.Counter, after: collections.Counter) -> int:
    def order(c):
        return [m for m, _ in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))]

    base, moved = order(before), order(after)
    return sum(1 for i, m in enumerate(base) if moved.index(m) != i)


def veto_rates(votes: dict, judge: str, panel_size: int = 3) -> dict:
    """Per-solver veto rate, counted only where the co-judges agreed to pass.

    Restricting to tasks the others passed takes the solver's own quality out of
    the comparison: every counted task is one the rest of the panel approved.
    """
    chance: collections.Counter = collections.Counter()
    veto: collections.Counter = collections.Counter()
    for (solver, _), d in votes.items():
        if len(d) != panel_size or judge not in d:
            continue
        if all(v for j, v in d.items() if j != judge):
            chance[solver] += 1
            if not d[judge]:
                veto[solver] += 1
    return {s: (veto[s], chance[s]) for s in chance}


def leniency(votes: dict, panel_size: int = 3) -> dict:
    """Each judge's pass rate minus its co-judges' mean, per solver."""
    pair: dict = collections.defaultdict(collections.Counter)
    for (solver, _), d in votes.items():
        if len(d) != panel_size:
            continue
        for j, v in d.items():
            pair[(j, solver)]["n"] += 1
            pair[(j, solver)]["p"] += int(v)
    judges = sorted({j for j, _ in pair})
    solvers = sorted({s for _, s in pair})

    def rate(j, s):
        c = pair.get((j, s))
        return c["p"] / c["n"] if c and c["n"] else None

    out: dict = collections.defaultdict(dict)
    for j in judges:
        for s in solvers:
            mine = rate(j, s)
            peers = [x for x in (rate(o, s) for o in judges if o != j) if x is not None]
            if mine is not None and peers:
                out[j][s] = mine - statistics.mean(peers)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--judgements", default="judge-8k/*/judgements.jsonl")
    parser.add_argument(
        "--family",
        action="append",
        default=[],
        help="JUDGE=SIBLING,SIBLING -- same-vendor models, used to tell "
        "self-preference apart from house style. Repeatable.",
    )
    args = parser.parse_args()
    votes = load_votes(args.judgements)
    if not votes:
        raise SystemExit(f"no verdicts matched {args.judgements!r}")

    infl = marginal_influence(votes)
    print(f"完整面板 {infl['panel']} 对,全票 {infl['unanimous']}\n")
    print(f"{'judge':26s} {'移除后全票':>11s} {'变化':>7s} {'独自否决':>9s}")
    for j, d in sorted(infl["judges"].items(), key=lambda kv: -kv[1]["solo_veto"]):
        flag = (
            "   <-- 边际影响近零,面板实际少一人"
            if abs(d["delta"]) <= 0.01 * max(infl["unanimous"], 1)
            else ""
        )
        print(f"{j:26s} {d['without']:11d} {d['delta']:+7d} {d['solo_veto']:9d}{flag}")

    full: collections.Counter = collections.Counter()
    for (solver, _), d in votes.items():
        if len(d) == 3 and all(d.values()):
            full[solver] += 1
    print(f"\n移除某判官后,榜单的实际变化:\n")
    print(f"{'judge':26s} {'排名变化':>10s} {'总分':>8s} {'变化':>8s}")
    base_total = sum(full.values())
    for j in sorted(infl["judges"]):
        after = board_without(votes, j)
        moved = rank_shift(full, after)
        total = sum(after.values())
        flag = "   <-- 贡献为零,可直接替换" if moved == 0 else ""
        print(
            f"{j:26s} {moved:6d}/{len(full):<3d} {total:8d} "
            f"{100*(total-base_total)/max(base_total,1):+7.1f}%{flag}"
        )

    lenient = leniency(votes)
    print("\n宽松度(该判官通过率 - 另两人均值),已控制被判模型质量:\n")
    for j in sorted(lenient):
        own = lenient[j].get(j)
        others = [v for s, v in lenient[j].items() if s != j]
        if own is None:
            print(f"{j:26s} 不在被评测集合里")
            continue
        mean_o = statistics.mean(others)
        sd = statistics.stdev(others) if len(others) > 1 else 0.0
        z = (own - mean_o) / sd if sd else 0.0
        print(
            f"{j:26s} 判自己 {100*own:+6.1f}pp  判别人 {100*mean_o:+6.1f}pp "
            f"(sd {100*sd:4.1f})  z={z:+.2f}"
        )

    for spec in args.family:
        judge, _, siblings = spec.partition("=")
        family = [s for s in siblings.split(",") if s]
        rates = veto_rates(votes, judge)
        if judge not in rates:
            print(f"\n{judge}: 不在被评测集合里,跳过")
            continue
        sv, sc = rates[judge]
        fv = sum(rates.get(m, (0, 0))[0] for m in family)
        fc = sum(rates.get(m, (0, 0))[1] for m in family)
        print(f"\n{judge} 的否决率(仅在另两人都通过的题上):")
        print(f"  判自己       {100*sv/sc:5.1f}%  ({sv}/{sc})")
        if fc:
            print(f"  判同家族     {100*fv/fc:5.1f}%  ({fv}/{fc})   {', '.join(family)}")
            extra = (fv / fc - sv / sc) * sc
            verdict = (
                "自我偏袒:对自己比对同门宽松"
                if extra > 0
                else "家族效应:对同门不比对自己严,不构成自我偏袒"
            )
            print(f"  => {verdict};按同门标准会多否决 {extra:+.0f} 题")


if __name__ == "__main__":
    main()
