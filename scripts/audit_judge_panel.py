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


def verdict_shape(pattern: str) -> dict:
    """Distribution of each judge's raw verdict fields.

    Separates a judge that is not reading from one that reads with the wrong
    scale, which need opposite fixes. A near-duplicate reason rate or verdict
    fields that contradict each other point at the first; a collapsed middle
    grade points at the second. gemini reads (0.4% duplicate reasons) but
    awards 5/5 on 57% of patches against opus-5's 3%, and uses the middle
    "partial" grade on 8% against the others' 32-34%.
    """
    per: dict = collections.defaultdict(
        lambda: {
            "n": 0,
            "quality": collections.Counter(),
            "addressed": collections.Counter(),
            "reasons": collections.Counter(),
            "contradict": 0,
        }
    )
    for path in sorted(glob.glob(pattern)):
        who = Path(path).parent.name.split("__")[0]
        for line in open(path, encoding="utf-8"):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            v = record.get("verdict")
            if not isinstance(v, dict):
                continue
            d = per[who]
            d["n"] += 1
            d["quality"][v.get("quality_1_to_5")] += 1
            addressed = str(v.get("addressed"))
            d["addressed"][addressed] += 1
            d["reasons"][str(v.get("reason") or "")[:60]] += 1
            q = v.get("quality_1_to_5")
            if isinstance(q, int) and (
                (addressed == "yes" and q <= 2) or (addressed == "no" and q >= 4)
            ):
                d["contradict"] += 1
    return per


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


def would_dissent(votes: dict, candidate: dict, incumbents: tuple[str, str]) -> dict:
    """How often a candidate judge rejects what both incumbents accepted.

    This is the question a substitute has to answer, and the verdict
    distribution cannot answer it. Distribution shape describes a judge's own
    habits; solo vetoes describe its relationship to the others. deepseek-v3.2
    passes 13 points more often than gpt-5.6-sol overall and still vetoed 20 of
    the 161 tasks sol and opus-5 both approved, against gemini's 1.
    """
    both_ok = [
        task
        for (solver, task), d in votes.items()
        if all(d.get(j) for j in incumbents)
    ]
    covered = [t for t in both_ok if t in candidate]
    vetoed = [t for t in covered if candidate[t] is False]
    return {"both_ok": len(both_ok), "covered": len(covered), "vetoed": len(vetoed)}


def self_favoritism(own: dict, others: dict) -> dict:
    """Self-favoritism as a difference-in-differences against a neutral judge.

    ``own`` and ``others`` map a solver to ``(judge_votes, reference_votes)``
    for the same tasks, where the reference is a judge with no stake in the
    board -- not in the evaluated set and neither is its family.

    The obvious control is the same-family model, and it is the wrong one. It
    holds style fixed but not patch quality, and quality is what the benchmark
    measures: a judge vetoes its own patches less partly because they really are
    better. Measured against a neutral reference, claude-opus-5 came out at
    +2.8pp (p=0.44, CI [-3.7, +9.2]), excluding the 13.8pp that the same-family
    control had implied.
    """

    def gap(pairs):
        n = sum(len(j) for j, _ in pairs.values())
        if not n:
            raise ValueError("no overlapping judged tasks")
        judge = sum(sum(j.values()) for j, _ in pairs.values())
        ref = sum(sum(r[t] for t in j) for j, r in pairs.values())
        return (judge - ref) / n, n

    own_gap, own_n = gap(own)
    other_gap, other_n = gap(others)
    return {
        "own": own_gap,
        "others": other_gap,
        "favoritism": own_gap - other_gap,
        "n_own": own_n,
        "n_others": other_n,
    }


def each_judge_ranks_first(scores: dict) -> dict:
    """Who each judge puts at the top, judging alone.

    ``scores`` maps judge -> {solver: pass_count}. Asking how many points of
    self-favoritism a judge has needs an agreed-on neutral yardstick, and three
    different yardsticks gave three different answers here. Asking who each
    judge ranks first needs no yardstick: claude-opus-5 ranked itself first and
    gpt-5.6-sol ranked itself first, while the neutral judge scored them 233 to
    228 (paired p=0.74). Two out of two judges picked themselves, and the top
    spot was a tie.
    """
    picks = {}
    for judge, board in scores.items():
        if not board:
            raise ValueError(f"{judge} scored nothing")
        top = max(board.items(), key=lambda kv: (kv[1], kv[0] == judge))
        picks[judge] = {"top": top[0], "score": top[1], "picked_self": top[0] == judge}
    contestants = [j for j in scores if j in next(iter(scores.values()))]
    selfies = [j for j in contestants if picks[j]["picked_self"]]
    return {
        "picks": picks,
        "judges_on_the_board": len(contestants),
        "picked_themselves": len(selfies),
    }


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

    shape = verdict_shape(args.judgements)
    if shape:
        print("\n判定分布(区分「没在读」和「读了但刻度不对」):\n")
        print(f"{'judge':26s} {'满分5占比':>9s} {'中间档':>7s} {'理由重复':>8s} {'自相矛盾':>8s}")
        for who in sorted(shape):
            d = shape[who]
            n = max(d["n"], 1)
            top = d["reasons"].most_common(1)[0][1] if d["reasons"] else 0
            mid = d["addressed"].get("partial", 0)
            note = ""
            if 100 * d["quality"].get(5, 0) / n > 40 and 100 * mid / n < 15:
                note = "  <-- 中间档塌缩,刻度问题而非部署问题"
            print(
                f"{who:26s} {100*d['quality'].get(5,0)/n:8.0f}% {100*mid/n:6.0f}% "
                f"{100*top/n:7.1f}% {100*d['contradict']/n:7.1f}%{note}"
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
