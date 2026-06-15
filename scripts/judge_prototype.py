"""Maintainability LLM-as-judge prototype + discriminative calibration.

A maintainability benchmark has no test oracle, so the scored signal must come
from a judge that decides whether a candidate change addresses the reviewer's
specific concern. Before trusting such a judge we must show it can discriminate:

For each case we present the judge (blind, one at a time) with three candidates:
  - reference : the real fix (start->target diff)  -> expect "addressed"
  - noop      : an empty change                    -> expect "not addressed"
  - distractor: another case's real fix            -> expect "not addressed"
                (a real change that is irrelevant to THIS concern)

If the judge cannot tell the real fix from doing nothing, or rewards an
irrelevant change, it is unusable. We report discrimination accuracy and the
false-positive rates for noop/distractor.

The judge runs on an OpenAI-compatible /v1/responses endpoint (e.g. the local
Copilot proxy). Results are checkpointed so the run is resumable.
"""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


JUDGE_INSTRUCTIONS = """You are a strict, fair code-review judge for software MAINTAINABILITY.

You are given:
- CONCERN: a real reviewer's maintainability request on a pull request.
- ORIGINAL CODE: the code region before any change.
- CANDIDATE CHANGE: a unified diff that supposedly addresses the concern (it may be empty, or may change unrelated code).

Decide ONLY whether the CANDIDATE CHANGE actually resolves THE SPECIFIC concern stated.
Rules:
- An empty change, or a change that does not touch the code the concern is about, does NOT address it.
- A change to unrelated code, even if reasonable, does NOT address this concern.
- Reward a change only if it plausibly makes the requested maintainability improvement to the right code.
- Judge the change itself; do not assume intent.

Return ONLY a JSON object:
{"addressed": "yes" | "partial" | "no", "quality_1_5": <int 1-5>, "reason": "<=1 sentence"}"""

INLINE_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def post_responses(endpoint: str, model: str, instructions: str, prompt: str, retries: int = 6) -> str:
    payload = json.dumps(
        {"model": model, "stream": False, "instructions": instructions, "input": prompt}
    ).encode("utf-8")
    delay = 2.0
    last_err = ""
    for attempt in range(retries):
        request = urllib.request.Request(
            endpoint, data=payload, headers={"content-type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                data = json.loads(response.read().decode("utf-8"))
            parts = []
            for item in data.get("output") or []:
                for content in item.get("content") or []:
                    if content.get("text"):
                        parts.append(content["text"])
            if not parts and data.get("output_text"):
                parts.append(str(data["output_text"]))
            return "\n".join(parts).strip()
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            last_err = str(exc)
            if attempt < retries - 1:
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            raise
    raise RuntimeError(last_err or "judge endpoint failed")


def parse_verdict(text: str) -> dict[str, Any]:
    raw = text
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        m = INLINE_JSON_RE.search(text)
        obj = json.loads(m.group(0)) if m else {}
    addressed = str(obj.get("addressed", "")).strip().lower()
    if addressed not in {"yes", "partial", "no"}:
        addressed = "no"
    try:
        quality = int(obj.get("quality_1_5"))
    except (TypeError, ValueError):
        quality = 0
    return {"addressed": addressed, "quality_1_5": quality, "reason": str(obj.get("reason", ""))[:200], "raw": raw[:300]}


def before_text(item: dict[str, Any]) -> str:
    bw = (item.get("evidence") or {}).get("before_window") or {}
    lines = bw.get("lines") or []
    if not lines:
        return "(original code unavailable)"
    return "\n".join(f"{n}: {t}" for n, t in lines)


def build_prompt(concern: str, category: str, before: str, candidate_diff: str) -> str:
    diff = candidate_diff.strip() or "(no changes were made)"
    return (
        f"CONCERN (category: {category}):\n{concern}\n\n"
        f"ORIGINAL CODE (line: content):\n{before}\n\n"
        f"CANDIDATE CHANGE (unified diff):\n{diff}\n"
    )


def candidates_for(item: dict[str, Any], distractor_diff: str) -> dict[str, str]:
    fix = (item.get("evidence") or {}).get("fix_patch") or {}
    return {
        "reference": fix.get("text") or "",
        "noop": "",
        "distractor": distractor_diff or "",
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="input", type=Path, required=True, help="Enriched cases JSON (with evidence).")
    parser.add_argument("--out", type=Path, required=True, help="Per-judgment results JSONL.")
    parser.add_argument("--report", type=Path, help="Summary JSON.")
    parser.add_argument("--endpoint", default="http://127.0.0.1:4143/v1/responses")
    parser.add_argument("--model", default="claude-opus-4.7")
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    items = json.loads(args.input.read_text(encoding="utf-8"))
    items = [it for it in items if (it.get("evidence") or {}).get("fix_patch", {}).get("text", "").strip()]

    done: set[tuple[str, str]] = set()
    results: list[dict[str, Any]] = []
    if args.resume and args.out.exists():
        for line in args.out.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                results.append(r)
                done.add((r["id"], r["candidate"]))

    def checkpoint() -> None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.out.with_suffix(".tmp")
        tmp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in results) + "\n", encoding="utf-8")
        tmp.replace(args.out)

    n = len(items)
    for i, item in enumerate(items):
        task = item.get("task", {})
        cid = task.get("id", f"case_{i}")
        concern = task.get("task_instruction") or ""
        category = task.get("human_category") or task.get("category") or "?"
        before = before_text(item)
        distractor_diff = (items[(i + 1) % n].get("evidence") or {}).get("fix_patch", {}).get("text", "")
        cands = candidates_for(item, distractor_diff)
        for cand_name, cand_diff in cands.items():
            if (cid, cand_name) in done:
                continue
            print(f"[{i+1}/{n}] {task.get('repo')} :: {cand_name} ...", flush=True)
            try:
                text = post_responses(args.endpoint, args.model, JUDGE_INSTRUCTIONS, build_prompt(concern, category, before, cand_diff))
                verdict = parse_verdict(text)
                err = None
            except Exception as exc:  # noqa: BLE001
                verdict = {"addressed": "error", "quality_1_5": 0, "reason": "", "raw": ""}
                err = str(exc)[:200]
            results.append({"id": cid, "repo": task.get("repo"), "category": category, "candidate": cand_name, "error": err, **verdict})
            checkpoint()

    summary = summarize(results)
    if args.report:
        args.report.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print_summary(summary)
    print(f"\nWrote {len(results)} judgments to {args.out}")
    return 0


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_case: dict[str, dict[str, str]] = {}
    for r in results:
        by_case.setdefault(r["id"], {})[r["candidate"]] = r["addressed"]

    def rate(cand: str, values: set[str]) -> float:
        vals = [v.get(cand) for v in by_case.values() if cand in v]
        return round(sum(1 for x in vals if x in values) / len(vals), 3) if vals else 0.0

    # Per-case discrimination: reference addressed (yes/partial) AND noop=no AND distractor=no
    discrim_strict = 0
    discrim_lenient = 0
    complete = 0
    for v in by_case.values():
        if not all(k in v for k in ("reference", "noop", "distractor")):
            continue
        complete += 1
        if v["reference"] == "yes" and v["noop"] == "no" and v["distractor"] == "no":
            discrim_strict += 1
        if v["reference"] in {"yes", "partial"} and v["noop"] == "no" and v["distractor"] == "no":
            discrim_lenient += 1
    return {
        "cases": len(by_case),
        "complete_cases": complete,
        "reference_addressed_yes_rate": rate("reference", {"yes"}),
        "reference_addressed_yes_or_partial_rate": rate("reference", {"yes", "partial"}),
        "noop_false_positive_rate": rate("noop", {"yes", "partial"}),
        "distractor_false_positive_rate": rate("distractor", {"yes", "partial"}),
        "discrimination_strict": round(discrim_strict / complete, 3) if complete else 0.0,
        "discrimination_lenient": round(discrim_lenient / complete, 3) if complete else 0.0,
    }


def print_summary(s: dict[str, Any]) -> None:
    print("\n=== Judge discriminative calibration ===")
    print(f"cases: {s['cases']} (complete: {s['complete_cases']})")
    print(f"reference  addressed=yes        : {s['reference_addressed_yes_rate']}")
    print(f"reference  addressed=yes/partial : {s['reference_addressed_yes_or_partial_rate']}")
    print(f"noop       false-positive        : {s['noop_false_positive_rate']}   (want ~0)")
    print(f"distractor false-positive        : {s['distractor_false_positive_rate']}   (want ~0)")
    print(f"DISCRIMINATION (strict)          : {s['discrimination_strict']}")
    print(f"DISCRIMINATION (lenient)         : {s['discrimination_lenient']}")


if __name__ == "__main__":
    raise SystemExit(main())
