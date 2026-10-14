"""Strict task-level quality-improvement gate for benchmark curation."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import sys
import time
from typing import Any
import urllib.error
import urllib.request

from .jsonl import read_jsonl, write_jsonl


QUALITY_GATE_VERSION = "reviewbench-quality-improvement/v1"
ALLOWED_PRIMARY_KINDS = {
    "maintainability",
    "functional_bug",
    "feature",
    "security",
    "performance",
    "style",
    "documentation",
    "test_only",
    "mixed",
}
ALLOWED_DIFFICULTIES = {"easy", "medium", "hard"}
REQUIRED_TRUE_CRITERIA = (
    "evidence_sufficient",
    "explicit_code_smell",
    "golden_materially_improves",
    "improvement_is_substantive",
    "reference_fix_is_attributable",
    "not_style_only",
    "no_obvious_quality_regression",
)
REQUIRED_FALSE_CRITERIA = ("primary_goal_is_behavior_change",)
INLINE_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

QUALITY_JUDGE_INSTRUCTIONS = """You are curating a benchmark whose core claim is:
every released task requires a clear, substantive improvement to code quality or
maintainability.

You receive a real review comment, the original code near the comment, the
anchor-local human fix, and the complete same-file reference diff. Be
conservative. Passing tests or satisfying a reviewer is not enough.

A task may PASS only when all of the following are true:
1. The original code has a concrete maintainability problem visible in the evidence.
2. The reference patch materially reduces that problem.
3. Maintainability is the primary goal, rather than fixing behavior, adding a
   feature, security, performance, documentation, or tests.
4. The improvement is substantive. Reject import ordering, formatting, trivial
   style preferences, and weak cosmetic consistency nits.
5. The patch does not merely follow a preference while making structure,
   readability, coupling, API clarity, or complexity obviously worse.
6. The evidence is sufficient to compare before and after.
7. The task itself is at least medium difficulty. Judge intrinsic reasoning
   difficulty, not the size of unrelated PR changes:
   - easy: mechanical import ordering, formatting, one-line deletion, direct
     rename, or an essentially specified replacement;
   - medium: requires local semantic understanding and a non-trivial,
     behavior-preserving refactor across meaningful code;
   - hard: requires cross-function/module reasoning, abstraction design, or
     substantial behavior-preserving restructuring.
8. The relevant quality fix is clearly attributable in the reference patch;
   unrelated bundled changes must not be what creates the apparent difficulty.

This is a comment-local benchmark task. Judge the requested maintainability
change primarily from ANCHOR-LOCAL FIX HUNKS. Use COMPLETE SAME-FILE DIFF to
find linked helper definitions and regressions caused by that same fix. Do not
reject merely because the PR contains unrelated bundled changes when the
review-local fix is clearly separable. Mark "mixed" only when the requested fix
itself combines maintainability with behavior change.

Naming and API-design tasks may pass when they materially improve clarity or
encapsulation; they fail when they are merely subjective style. A mixed
functional-and-maintainability task fails because the benchmark must make an
unambiguous maintainability claim.

Return exactly one JSON object with this schema:
{
  "decision": "pass" | "reject" | "insufficient",
  "primary_kind": "maintainability" | "functional_bug" | "feature" | "security" |
                  "performance" | "style" | "documentation" | "test_only" | "mixed",
  "evidence_sufficient": true | false,
  "explicit_code_smell": true | false,
  "golden_materially_improves": true | false,
  "improvement_is_substantive": true | false,
  "reference_fix_is_attributable": true | false,
  "primary_goal_is_behavior_change": true | false,
  "not_style_only": true | false,
  "no_obvious_quality_regression": true | false,
  "quality_score_1_to_5": 1 | 2 | 3 | 4 | 5,
  "intrinsic_difficulty": "easy" | "medium" | "hard",
  "confidence": <number from 0 to 1>,
  "code_smell": "<specific problem, one sentence>",
  "improvement": "<specific quality improvement, one sentence>",
  "difficulty_reason": "<why the task is easy, medium, or hard>",
  "reason": "<concise decisive reason>"
}"""


@dataclass(frozen=True)
class QualityVerdict:
    decision: str
    primary_kind: str
    evidence_sufficient: bool
    explicit_code_smell: bool
    golden_materially_improves: bool
    improvement_is_substantive: bool
    reference_fix_is_attributable: bool
    primary_goal_is_behavior_change: bool
    not_style_only: bool
    no_obvious_quality_regression: bool
    quality_score_1_to_5: int
    intrinsic_difficulty: str
    confidence: float
    code_smell: str
    improvement: str
    difficulty_reason: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_verdict(text: str) -> QualityVerdict:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        match = INLINE_JSON_RE.search(text)
        if not match:
            # A verdict cut off by the output-token cap has an opening brace but
            # never closes it, so it must be reported as a truncation rather
            # than silently recorded as a failed judgement.
            if text.lstrip().lstrip("`").lstrip("json").lstrip().startswith("{"):
                raise ValueError(
                    "quality judge response was truncated before the JSON "
                    "object closed; raise --max-output-tokens"
                )
            raise ValueError("quality judge did not return a JSON object")
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("quality judge response is not an object")

    decision = str(value.get("decision", "")).strip().lower()
    if decision not in {"pass", "reject", "insufficient"}:
        raise ValueError(f"invalid quality decision: {decision!r}")
    primary_kind = str(value.get("primary_kind", "")).strip().lower()
    if primary_kind not in ALLOWED_PRIMARY_KINDS:
        raise ValueError(f"invalid primary kind: {primary_kind!r}")
    intrinsic_difficulty = str(value.get("intrinsic_difficulty", "")).strip().lower()
    if intrinsic_difficulty not in ALLOWED_DIFFICULTIES:
        raise ValueError(f"invalid intrinsic difficulty: {intrinsic_difficulty!r}")

    score = _bounded_int(value.get("quality_score_1_to_5"), 1, 5, "quality_score_1_to_5")
    confidence = _bounded_float(value.get("confidence"), 0.0, 1.0, "confidence")
    booleans = {
        key: _strict_bool(value.get(key), key)
        for key in (*REQUIRED_TRUE_CRITERIA, *REQUIRED_FALSE_CRITERIA)
    }
    return QualityVerdict(
        decision=decision,
        primary_kind=primary_kind,
        intrinsic_difficulty=intrinsic_difficulty,
        quality_score_1_to_5=score,
        confidence=confidence,
        code_smell=_short_text(value.get("code_smell")),
        improvement=_short_text(value.get("improvement")),
        difficulty_reason=_short_text(value.get("difficulty_reason")),
        reason=_short_text(value.get("reason")),
        **booleans,
    )


def verdict_passes(verdict: QualityVerdict, min_confidence: float = 0.8) -> bool:
    return (
        verdict.decision == "pass"
        and verdict.primary_kind == "maintainability"
        and verdict.quality_score_1_to_5 >= 4
        and verdict.intrinsic_difficulty in {"medium", "hard"}
        and verdict.confidence >= min_confidence
        and all(getattr(verdict, key) is True for key in REQUIRED_TRUE_CRITERIA)
        and all(getattr(verdict, key) is False for key in REQUIRED_FALSE_CRITERIA)
    )


def aggregate_verdicts(
    judgments: list[dict[str, Any]],
    min_judges: int = 2,
    min_confidence: float = 0.8,
) -> dict[str, Any]:
    parsed: list[tuple[str, QualityVerdict]] = []
    errors = []
    for judgment in judgments:
        try:
            verdict = parse_verdict(json.dumps(judgment["verdict"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            errors.append(str(exc))
            continue
        model = str(judgment.get("model") or "").strip()
        if not model:
            errors.append("judgment is missing model identity")
            continue
        parsed.append((model, verdict))

    distinct_models = {model for model, _ in parsed}
    enough = len(distinct_models) >= min_judges
    all_judges_valid = len(parsed) == len(judgments) and not errors
    unanimous = (
        enough
        and all_judges_valid
        and all(verdict_passes(item, min_confidence) for _, item in parsed)
    )
    if unanimous:
        status = "passed"
    elif not all_judges_valid:
        status = "judge_error"
    elif enough:
        status = "rejected"
    else:
        status = "insufficient_judgments"
    return {
        "version": QUALITY_GATE_VERSION,
        "passed": unanimous,
        "status": status,
        "judge_count": len(parsed),
        "distinct_judge_count": len(distinct_models),
        "required_judges": min_judges,
        "unanimous_strict_pass": unanimous,
        "errors": errors,
    }


def build_prompt(
    task: dict[str, Any],
    before: str,
    patch: str,
    full_patch: str | None = None,
) -> str:
    locality = task.get("locality") or {}
    difficulty = task.get("difficulty") or {}
    return (
        f"TASK ID: {task.get('id')}\n"
        f"REPOSITORY: {task.get('repo')}\n"
        f"LANGUAGE: {task.get('inferred_language')}\n"
        "HEURISTIC CATEGORY (unverified; classify independently): "
        f"{task.get('human_category') or task.get('category')}\n"
        "HEURISTIC DIFFICULTY (unverified; assess independently): "
        f"{difficulty.get('tier')} (score={difficulty.get('score')})\n"
        f"FILE: {locality.get('path')}:{locality.get('line')}\n\n"
        f"REVIEW COMMENT:\n{task.get('task_instruction') or ''}\n\n"
        f"ORIGINAL CODE NEAR THE COMMENT:\n{before or '(missing)'}\n\n"
        f"ANCHOR-LOCAL FIX HUNKS:\n{patch or '(missing)'}\n\n"
        f"COMPLETE SAME-FILE REFERENCE DIFF:\n{full_patch or patch or '(missing)'}\n"
    )


def evidence_from_record(record: dict[str, Any]) -> tuple[str, str, str]:
    quality_evidence = record.get("quality_evidence") or {}
    before = str(quality_evidence.get("before") or "")
    patch = str(quality_evidence.get("patch") or "")
    full_patch = str(quality_evidence.get("full_patch") or patch)
    if before and patch:
        return before, patch, full_patch

    evidence = record.get("evidence") or {}
    before_window = evidence.get("before_window") or {}
    lines = before_window.get("lines") or []
    if not before and lines:
        before = "\n".join(f"{line[0]}: {line[1]}" for line in lines if len(line) >= 2)
    fix_patch = evidence.get("fix_patch") or {}
    if not patch:
        patch = str(fix_patch.get("text") or "")
    return before, patch, patch


def evidence_is_complete(record: dict[str, Any]) -> bool:
    quality_evidence = record.get("quality_evidence")
    if isinstance(quality_evidence, dict) and quality_evidence:
        return (
            quality_evidence.get("status") == "ready"
            and quality_evidence.get("patch_complete") is True
            and quality_evidence.get("patch_truncated") is not True
            and bool(quality_evidence.get("full_patch"))
        )
    evidence = record.get("evidence") or {}
    fix_patch = evidence.get("fix_patch") or {}
    return bool(fix_patch.get("text")) and fix_patch.get("truncated") is not True


def post_responses(
    endpoint: str,
    model: str,
    prompt: str,
    retries: int = 5,
    timeout_seconds: int = 180,
) -> str:
    payload = json.dumps(
        {
            "model": model,
            "stream": False,
            "instructions": QUALITY_JUDGE_INSTRUCTIONS,
            "input": prompt,
        }
    ).encode("utf-8")
    delay = 2.0
    for attempt in range(retries):
        request = urllib.request.Request(
            endpoint,
            data=payload,
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
            parts = [
                str(content["text"])
                for item in data.get("output") or []
                for content in item.get("content") or []
                if content.get("text")
            ]
            if not parts and data.get("output_text"):
                parts.append(str(data["output_text"]))
            text = "\n".join(parts).strip()
            if not text:
                raise RuntimeError("quality judge returned empty output")
            return text
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            retryable = not isinstance(exc, urllib.error.HTTPError) or exc.code in {
                429,
                500,
                502,
                503,
                504,
            }
            if not retryable or attempt == retries - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 30)
    raise RuntimeError("quality judge request failed")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="input", type=Path, required=True, help="Enriched task JSONL.")
    parser.add_argument("--out", type=Path, required=True, help="Judged task JSONL.")
    parser.add_argument("--endpoint", help="OpenAI-compatible /v1/responses endpoint.")
    parser.add_argument("--model", action="append", default=[], help="Independent judge model; repeatable.")
    parser.add_argument(
        "--judgments",
        type=Path,
        nargs="+",
        help="External JSONL judgments with id, model, and verdict fields.",
    )
    parser.add_argument("--min-judges", type=int, default=2)
    parser.add_argument("--min-confidence", type=float, default=0.8)
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.judgments and not (args.endpoint and args.model):
        print(
            "reviewbench-quality: provide --judgments or --endpoint with --model",
            file=sys.stderr,
        )
        return 2
    if args.model and not args.endpoint:
        print("reviewbench-quality: --model requires --endpoint", file=sys.stderr)
        return 2

    records = list(read_jsonl(args.input))
    completed: dict[str, dict[str, Any]] = {}
    if args.resume and args.out.exists():
        for item in read_jsonl(args.out):
            completed[str(item.get("id") or "")] = item
    external = load_external_judgments(args.judgments or [])

    output: list[dict[str, Any]] = []
    for index, record in enumerate(records, start=1):
        task_id = str(record.get("id") or "")
        prior = completed.get(task_id)
        if prior and len(prior.get("quality_judgments") or []) >= len(args.model):
            output.append(prior)
            continue

        before, patch, full_patch = evidence_from_record(record)
        judgments: list[dict[str, Any]] = list(external.get(task_id, []))
        complete_evidence = bool(before and patch and evidence_is_complete(record))
        if complete_evidence and args.endpoint:
            prompt = build_prompt(record, before, patch, full_patch)
            for model in args.model:
                if any(item.get("model") == model for item in judgments):
                    continue
                try:
                    raw = post_responses(args.endpoint, model, prompt)
                    verdict = parse_verdict(raw)
                    judgments.append(
                        {
                            "model": model,
                            "verdict": verdict.to_dict(),
                            "strict_pass": verdict_passes(verdict, args.min_confidence),
                            "error": None,
                        }
                    )
                except Exception as exc:  # noqa: BLE001 - persist per-judge failure
                    judgments.append(
                        {
                            "model": model,
                            "verdict": None,
                            "strict_pass": False,
                            "error": str(exc)[:300],
                        }
                    )

        item = dict(record)
        item["quality_judgments"] = judgments
        item["quality_gate"] = aggregate_verdicts(
            judgments,
            min_judges=args.min_judges,
            min_confidence=args.min_confidence,
        )
        if not complete_evidence:
            item["quality_gate"]["status"] = "missing_evidence"
            item["quality_gate"]["passed"] = False
        output.append(item)
        _write_jsonl(args.out, output)
        print(
            f"[{index}/{len(records)}] {task_id}: {item['quality_gate']['status']}",
            flush=True,
        )
    return 0


def load_external_judgments(paths: list[Path]) -> dict[str, list[dict[str, Any]]]:
    by_task: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[str, str]] = set()
    for path in paths:
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            if not line.strip():
                continue
            value = json.loads(line)
            task_id = str(value.get("id") or "").strip()
            model = str(value.get("model") or "").strip()
            if not task_id or not model or not isinstance(value.get("verdict"), dict):
                raise ValueError(
                    f"{path}:{line_number}: expected id, model, and verdict object"
                )
            key = (task_id, model)
            if key in seen:
                raise ValueError(
                    f"{path}:{line_number}: duplicate judgment for {task_id}/{model}"
                )
            seen.add(key)
            by_task.setdefault(task_id, []).append(
                {"model": model, "verdict": value["verdict"], "error": None}
            )
    return by_task


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    write_jsonl(path, records, atomic=True)


def _strict_bool(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name} must be a JSON boolean")
    return value


def _bounded_int(value: Any, minimum: int, maximum: int, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _bounded_float(value: Any, minimum: float, maximum: float, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _short_text(value: Any, limit: int = 500) -> str:
    return " ".join(str(value or "").split())[:limit]


if __name__ == "__main__":
    raise SystemExit(main())
