#!/usr/bin/env python3
"""Judge the teacher rollouts the SFT data is built from.

The task pool behind those rollouts is screened hard -- every task has
`strict_pass` and `decision: pass` from the GPT quality judge, and none are
rated easy. What is never checked is the teacher's own answer: a rollout is
kept when the teacher edited something and produced a non-empty diff, with no
test run, no comparison against the golden patch and no judge.

So this runs the published panel over the teacher's patches. Any demonstration
the panel rejects is one the student is being taught to imitate.

Trajectories carry their own file contents in `initial_prompt`, so no repository
checkout is needed -- which is what makes this affordable at all, since the
rollouts span 2,266 repositories against the 332 the oracles cover.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import importlib.util
import json
import random
import re
import sys
import threading
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

SPEC = importlib.util.spec_from_file_location(
    "judge_release_patches", REPO / "scripts" / "judge_release_patches.py"
)
JUDGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(JUDGE)

CONCERN_RE = re.compile(r"REVIEWER CONCERN:\n(.*?)\n\nINITIAL EXCERPT", re.DOTALL)
EXCERPT_RE = re.compile(r"INITIAL EXCERPT \(lines \d+-(\d+)\):\n(.*)", re.DOTALL)
ANCHOR_RE = re.compile(r"REVIEW ANCHOR: line (\d+)")


def build_prompt(record: dict[str, Any], maximum_chars: int) -> str | None:
    """Rebuild the judge prompt from a self-contained trajectory.

    Field names and section order match `judge_release_patches.build_prompt`
    exactly; a difference here would make these verdicts incomparable with the
    board's.
    """
    prompt = record.get("initial_prompt") or ""
    concern = CONCERN_RE.search(prompt)
    excerpt = EXCERPT_RE.search(prompt)
    if not concern or not excerpt:
        return None
    anchor = ANCHOR_RE.search(prompt)
    body = excerpt.group(2)
    # The rollout numbers every line; the board shows the raw file, so strip
    # the prefix rather than judging a differently formatted excerpt.
    lines = []
    for line in body.splitlines():
        head, sep, rest = line.partition(": ")
        lines.append(rest if sep and head.strip().isdigit() else line)
    source = "\n".join(lines)
    first, last = 1, len(lines)
    if len(source) > maximum_chars:
        source = source[:maximum_chars]
    return (
        f"REPOSITORY: {record.get('repo')}\n"
        f"FILE: {record.get('path')}\n"
        f"REVIEWER CONCERN:\n{concern.group(1).strip()}\n\n"
        f"ORIGINAL SOURCE (lines {first}-{last}):\n{source}\n\n"
        f"CANDIDATE DIFF:\n{record.get('patch') or ''}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rollouts", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--judge-model", required=True)
    parser.add_argument("--sample", type=int, default=0)
    parser.add_argument("--seed", type=int, default=20260901)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--rubric", default="v2")
    parser.add_argument("--max-context-chars", type=int, default=30_000)
    parser.add_argument("--max-output-tokens", type=int, default=2048)
    parser.add_argument(
        "--chat-endpoint", default="http://127.0.0.1:4000/v1/chat/completions"
    )
    args = parser.parse_args()

    records = [
        json.loads(line)
        for line in args.rollouts.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if args.sample and args.sample < len(records):
        random.Random(args.seed).shuffle(records)
        records = records[: args.sample]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if args.out.exists():
        for line in args.out.read_text(encoding="utf-8").splitlines():
            try:
                done.add(json.loads(line)["id"])
            except (json.JSONDecodeError, KeyError):
                continue
    pending = [r for r in records if r["id"] not in done]
    print(f"{len(records)} rollouts, {len(done)} already judged, {len(pending)} to go")

    lock = threading.Lock()
    handle = args.out.open("a", encoding="utf-8")
    counts = {"pass": 0, "fail": 0, "error": 0}

    def work(record: dict[str, Any]) -> None:
        prompt = build_prompt(record, args.max_context_chars)
        if prompt is None:
            with lock:
                counts["error"] += 1
            return
        try:
            text, usage, _ = JUDGE.invoke_judge(
                args.judge_model,
                prompt,
                args.chat_endpoint,
                args.max_output_tokens,
                args.rubric,
            )
            verdict = JUDGE.parse_verdict(text, args.rubric)
            passed = JUDGE.verdict_passes(verdict, args.rubric)
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            with lock:
                counts["error"] += 1
                handle.write(
                    json.dumps(
                        {"id": record["id"], "error": str(exc)[:300]},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                handle.flush()
            return
        with lock:
            counts["pass" if passed else "fail"] += 1
            handle.write(
                json.dumps(
                    {
                        "id": record["id"],
                        "repo": record.get("repo"),
                        "judge_model": args.judge_model,
                        "rubric": args.rubric,
                        "teacher_model": record.get("teacher_model"),
                        "maintainability_pass": passed,
                        "verdict": verdict,
                        "usage": usage,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            handle.flush()
            total = sum(counts.values())
            if total % 25 == 0:
                rate = counts["pass"] / max(1, counts["pass"] + counts["fail"])
                print(f"  {total}/{len(pending)}  pass_rate={100 * rate:.1f}%")

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(work, pending))
    handle.close()
    scored = counts["pass"] + counts["fail"]
    print(
        json.dumps(
            {
                **counts,
                "pass_rate": round(counts["pass"] / max(1, scored), 4),
                "scored": scored,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
