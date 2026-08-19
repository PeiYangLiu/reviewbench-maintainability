#!/usr/bin/env python3
"""Blindly judge whether generated patches resolve release task concerns."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import threading
from typing import Any

import generate_agentic_patches as agentic
import generate_release_patches as generator


JUDGE_INSTRUCTIONS = """You are a strict code-review judge for software maintainability.

You receive a real reviewer concern, source code before the change, and one
candidate diff. The candidate model identity is intentionally hidden.

Return yes only if the diff fully and substantively resolves the specific
concern in the right code. Reject incomplete, cosmetic, unrelated, over-broad,
or solution-shaped-but-wrong changes. A candidate that changes behavior or a
public contract without the concern requiring it does not pass, even if tests
might pass. New maintainability debt of comparable severity also does not pass.

The candidate is intentionally restricted to the anchored production file.
Repository tests are evaluated by a separate correctness oracle. Do not reject
solely because the candidate diff does not add or modify tests, even when the
reviewer also requested a test; judge whether the production-code
maintainability concern is fully resolved.

Return exactly one JSON object:
{
  "addressed": "yes" | "partial" | "no",
  "quality_1_to_5": 1 | 2 | 3 | 4 | 5,
  "behavior_preserved": true | false,
  "focused": true | false,
  "reason": "one concise sentence"
}"""

RUBRIC_V2_INSTRUCTIONS = """You are a code-review judge for software maintainability.

You receive a real reviewer concern, source code before the change, and one
candidate diff. The candidate identity is intentionally hidden, and the
candidate may be a human maintainer's own change.

Judge one question: does this diff substantively resolve that specific
maintainability concern in the right code, and is the resulting code better?

Do NOT judge whether runtime behavior is preserved. A separate executable
oracle already runs the repository's tests against this exact diff, so
speculating about behavior from the diff text is out of scope. Real fixes
routinely adjust behavior, signatures, or public contracts when the concern
requires it.

Do NOT reject a diff merely because it also contains changes beyond the minimal
fix. Extra work is normal in a real commit. Reject on scope only when the
concern itself is left unresolved, or when the diff introduces new
maintainability debt of comparable severity to the debt it removes.

Do not reject solely because the candidate diff does not add or modify tests,
even when the reviewer also requested a test.

Return exactly one JSON object:
{
  "addressed": "yes" | "partial" | "no",
  "quality_1_to_5": 1 | 2 | 3 | 4 | 5,
  "introduces_new_debt": true | false,
  "reason": "one concise sentence"
}"""
RUBRICS = {"v1": JUDGE_INSTRUCTIONS, "v2": RUBRIC_V2_INSTRUCTIONS}
JUDGE_PROMPT_VERSION = "reviewbench-maintainability-judge/v2"


def judge_protocol_fingerprint(
    rubric: str,
    judge_model: str | list[str],
    max_context_chars: int,
    max_output_tokens: int | None,
    max_output_token_ceiling: int | None = None,
) -> str:
    panel = [judge_model] if isinstance(judge_model, str) else list(judge_model)
    payload = {
        "prompt_version": JUDGE_PROMPT_VERSION,
        "rubric": rubric,
        "rubric_instructions": RUBRICS[rubric],
        # A single judge keeps its historical fingerprint shape; a panel is
        # recorded as the ordered member list plus its decision rule so a
        # unanimous verdict can never be confused with a single-judge one.
        "judge_model": panel[0] if len(panel) == 1 else panel,
        "judge_panel": panel,
        "judge_decision_rule": "unanimous" if len(panel) > 1 else "single",
        "max_context_chars": max_context_chars,
        "max_output_tokens": max_output_tokens,
    }
    if max_output_token_ceiling is not None:
        # The escalation ceiling changes how a truncated judge is resolved, so
        # it is part of the protocol rather than a tuning knob.
        payload["max_output_token_ceiling"] = max_output_token_ceiling
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
INLINE_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--repo-cache", type=Path, required=True)
    parser.add_argument(
        "--generation",
        action="append",
        required=True,
        help="MODEL=path/to/generation_results.jsonl; repeat for each solver.",
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--raw-dir", type=Path, required=True)
    parser.add_argument(
        "--judge-model",
        action="append",
        default=None,
        help="Judge model; repeat to form a panel. A panel only passes a "
        "candidate when every member passes it (unanimous).",
    )
    parser.add_argument("--rubric", choices=("v1", "v2"), default="v1")
    parser.add_argument(
        "--chat-endpoint",
        default="http://127.0.0.1:4399/v1/chat/completions",
    )
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--max-context-chars", type=int, default=30_000)
    parser.add_argument("--max-output-tokens", type=int, default=2048)
    parser.add_argument(
        "--max-output-token-ceiling",
        type=int,
        default=16384,
        help="Upper bound when doubling the budget after a truncated judge "
        "response.",
    )
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if not args.judge_model:
        args.judge_model = ["claude-opus-4.8"]
    if len(set(args.judge_model)) != len(args.judge_model):
        raise ValueError(
            f"duplicate judge in panel: {args.judge_model}; a repeated member "
            "would give one model multiple votes"
        )
    return args


def parse_generation_args(values: list[str]) -> dict[str, Path]:
    result = {}
    for value in values:
        model, separator, path = value.partition("=")
        if not separator or not model or not path:
            raise ValueError(f"invalid --generation value: {value!r}")
        result[model] = Path(path)
    return result


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def parse_verdict(text: str, rubric: str = "v1") -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        match = INLINE_JSON_RE.search(text)
        value = json.loads(match.group(0)) if match else {}
    addressed = str(value.get("addressed") or "").strip().lower()
    if addressed not in {"yes", "partial", "no"}:
        raise ValueError(f"invalid addressed value: {addressed!r}")
    score = value.get("quality_1_to_5")
    if type(score) is not int or not 1 <= score <= 5:
        raise ValueError("quality_1_to_5 must be an integer from 1 to 5")
    reason = str(value.get("reason") or "").strip()
    if not reason:
        raise ValueError("reason is required")
    if rubric == "v2":
        new_debt = value.get("introduces_new_debt")
        if type(new_debt) is not bool:
            raise ValueError("introduces_new_debt must be a boolean")
        return {
            "addressed": addressed,
            "quality_1_to_5": score,
            "introduces_new_debt": new_debt,
            "reason": reason[:500],
        }
    behavior_preserved = value.get("behavior_preserved")
    focused = value.get("focused")
    if type(behavior_preserved) is not bool or type(focused) is not bool:
        raise ValueError("behavior_preserved and focused must be booleans")
    return {
        "addressed": addressed,
        "quality_1_to_5": score,
        "behavior_preserved": behavior_preserved,
        "focused": focused,
        "reason": reason[:500],
    }


def verdict_passes(verdict: dict[str, Any], rubric: str) -> bool:
    if rubric == "v2":
        # Behaviour preservation is delegated to the executable correctness
        # oracle, which runs the repository tests against this exact diff.
        return (
            verdict["addressed"] == "yes"
            and verdict["quality_1_to_5"] >= 4
            and verdict["introduces_new_debt"] is False
        )
    return (
        verdict["addressed"] == "yes"
        and verdict["quality_1_to_5"] >= 4
        and verdict["behavior_preserved"] is True
        and verdict["focused"] is True
    )


def build_prompt(
    task: dict[str, Any],
    source: str,
    patch: str,
    maximum_chars: int,
) -> str:
    locality = task.get("locality") or {}
    anchor_line = int(locality.get("line") or locality.get("start_line") or 1)
    excerpt, first_line, last_line = generator.source_excerpt(
        source,
        anchor_line,
        maximum_chars,
    )
    return (
        f"REPOSITORY: {task.get('repo')}\n"
        f"FILE: {locality.get('path')}\n"
        f"REVIEWER CONCERN:\n{task.get('task_instruction') or ''}\n\n"
        f"ORIGINAL SOURCE (lines {first_line}-{last_line}):\n{excerpt}\n\n"
        f"CANDIDATE DIFF:\n{patch}"
    )


def invoke_judge(
    model: str,
    prompt: str,
    endpoint: str,
    maximum_tokens: int,
    rubric: str = "v1",
) -> tuple[str, dict[str, Any], str]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": RUBRICS[rubric]},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": maximum_tokens,
        "stream": False,
    }
    data = generator.post_json(endpoint, payload, {})
    actual_model = str(data.get("model") or "")
    if actual_model != model:
        raise RuntimeError(
            f"judge model mismatch: requested {model}, gateway returned "
            f"{actual_model or '<missing>'}; refusing an unattested or "
            "silent fallback"
        )
    return (
        generator.chat_text(data),
        data.get("usage") or {},
        str(((data.get("choices") or [{}])[0]).get("finish_reason") or ""),
    )


CONTENT_FILTER_FINISH_REASONS = frozenset({"content_filter", "safety", "blocked"})


def response_was_truncated(text: str, finish_reason: str) -> bool:
    """Whether the judge ran out of output budget rather than disagreeing.

    Reasoning judges spend budget before emitting any visible text, so an
    exhausted budget shows up as ``finish_reason == "length"`` and/or an empty
    body. Retrying such a call at the same budget can only fail again, and
    silently recording it would drop exactly the hardest patches -- the ones
    that need the most deliberation -- from the panel.

    A safety refusal also returns an empty body, but more budget cannot fix it,
    so it is deliberately excluded here and handled as its own terminal state.
    """
    if finish_reason in CONTENT_FILTER_FINISH_REASONS:
        return False
    return finish_reason == "length" or not text.strip()


def response_was_content_filtered(text: str, finish_reason: str) -> bool:
    """Whether the judge refused to evaluate this patch at all."""
    return finish_reason in CONTENT_FILTER_FINISH_REASONS


def build_repair_prompt(
    prompt: str,
    invalid_response: str,
    error: str,
    rubric: str,
) -> str:
    required = (
        "addressed, quality_1_to_5, introduces_new_debt, reason"
        if rubric == "v2"
        else (
            "addressed, quality_1_to_5, behavior_preserved, focused, "
            "reason"
        )
    )
    return (
        f"{prompt}\n\n"
        "SCHEMA REPAIR: Your previous response was invalid. Return the "
        f"corrected JSON object only, with every required field: {required}.\n"
        f"VALIDATION ERROR: {error}\n"
        f"INVALID RESPONSE:\n{invalid_response[:2_000]}"
    )


def judge_with_one_model(
    judge_model: str,
    prompt: str,
    raw_path: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Obtain one judge's verdict, retrying only its own malformed output.

    A truncated response is retried at a doubled budget rather than re-asked at
    the same one: the same budget would truncate again, so the pair would be
    dropped for being hard rather than for being wrong.
    """
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    last_error = ""
    attempt_prompt = prompt
    budget = args.max_output_tokens
    budgets_used = []
    for attempt in range(1, 4):
        text = ""
        budgets_used.append(budget)
        try:
            text, usage, finish_reason = invoke_judge(
                judge_model,
                attempt_prompt,
                args.chat_endpoint,
                budget,
                args.rubric,
            )
            raw_path.write_text(text, encoding="utf-8")
            raw_path.with_name(
                f"{raw_path.stem}.attempt-{attempt}{raw_path.suffix}"
            ).write_text(text, encoding="utf-8")
            if response_was_content_filtered(text, finish_reason):
                # A safety refusal is terminal for this judge: neither a larger
                # budget nor a reworded repair prompt changes the decision, and
                # retrying would only burn attempts. It is surfaced as its own
                # status so scope decisions can be made explicitly rather than
                # by silently scoring the pair as a failure.
                return {
                    "judge_model": judge_model,
                    "status": "content_filtered",
                    "maintainability_pass": False,
                    "attempt": attempt,
                    "raw_file": str(raw_path),
                    "usage": usage,
                    "finish_reason": finish_reason,
                    "output_token_budgets": budgets_used,
                    "error": (
                        f"judge refused to evaluate this patch "
                        f"(finish_reason={finish_reason})"
                    ),
                }
            if response_was_truncated(text, finish_reason):
                raise ValueError(
                    f"judge response truncated at {budget} output tokens "
                    f"(finish_reason={finish_reason or '<missing>'})"
                )
            verdict = parse_verdict(text, args.rubric)
            return {
                "judge_model": judge_model,
                "status": "judged",
                "maintainability_pass": verdict_passes(verdict, args.rubric),
                "attempt": attempt,
                "raw_file": str(raw_path),
                "usage": usage,
                "output_token_budget": budget,
                "output_token_budgets": budgets_used,
                "verdict": verdict,
            }
        except Exception as exc:
            last_error = str(exc)[:500]
            truncated = "truncated at" in last_error
            if truncated:
                budget = min(budget * 2, args.max_output_token_ceiling)
                attempt_prompt = prompt
            elif text:
                attempt_prompt = build_repair_prompt(
                    prompt,
                    text,
                    last_error,
                    args.rubric,
                )
    return {
        "judge_model": judge_model,
        "status": "judge_error",
        "maintainability_pass": False,
        "error": last_error,
        "raw_file": str(raw_path),
        "output_token_budgets": budgets_used,
    }


def combine_panel(panel: list[dict[str, Any]]) -> tuple[str, bool]:
    """Combine per-judge verdicts under the unanimous rule.

    A candidate passes only when every judge returned a valid verdict *and*
    every one of those verdicts passes. An unresolved judge therefore never
    yields a pass, and is reported as ``judge_error`` so the completion marker
    forces it to be resolved rather than silently scored as a failure.

    A safety refusal is reported distinctly: it cannot be resolved by retrying,
    so the pair needs an explicit scope decision instead of an endless rerun.
    """
    if any(member["status"] == "content_filtered" for member in panel):
        return "content_filtered", False
    if any(member["status"] != "judged" for member in panel):
        return "judge_error", False
    return "judged", all(member["maintainability_pass"] for member in panel)


def judge_one(
    task: dict[str, Any],
    generation: dict[str, Any],
    solver_model: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    fingerprint = generation_fingerprint(generation)
    protocol_fingerprint = judge_protocol_fingerprint(
        args.rubric,
        args.judge_model,
        args.max_context_chars,
        args.max_output_tokens,
        args.max_output_token_ceiling,
    )
    judges = list(args.judge_model)
    base = {
        "id": task["id"],
        "repo": task.get("repo"),
        "solver_model": solver_model,
        "judge_model": judges[0] if len(judges) == 1 else judges,
        "judge_panel": judges,
        "judge_decision_rule": "unanimous" if len(judges) > 1 else "single",
        "rubric": args.rubric,
        "judge_protocol_fingerprint": protocol_fingerprint,
        "task_fingerprint": agentic.task_fingerprint(task),
        "generation_fingerprint": fingerprint,
    }
    if generation.get("apply_ok") is not True:
        return {
            **base,
            "status": "generation_failed",
            "maintainability_pass": False,
            "generation_error": generation.get("error"),
        }
    patch_path = Path(str(generation["patch_file"]))
    if not patch_path.exists():
        return {
            **base,
            "status": "missing_patch",
            "maintainability_pass": False,
        }
    try:
        source = generator.source_at_start(task, args.repo_cache)
        patch = patch_path.read_text(encoding="utf-8")
        prompt = build_prompt(task, source, patch, args.max_context_chars)
    except Exception as exc:
        return {
            **base,
            "status": "input_error",
            "maintainability_pass": False,
            "error": str(exc)[:500],
        }

    raw_root = args.raw_dir / generator.safe_component(solver_model)
    panel = []
    for judge_model in judges:
        suffix = (
            "" if len(judges) == 1 else f".{generator.safe_component(judge_model)}"
        )
        panel.append(
            judge_with_one_model(
                judge_model,
                prompt,
                raw_root / f"{task['id']}{suffix}.txt",
                args,
            )
        )
    status, passed = combine_panel(panel)
    result = {
        **base,
        "status": status,
        "maintainability_pass": passed,
        "patch_file": str(patch_path),
        "panel": panel,
        "panel_passes": {
            member["judge_model"]: member["maintainability_pass"]
            for member in panel
        },
    }
    if len(judges) == 1:
        # Preserve the historical single-judge record shape.
        member = panel[0]
        for key in ("attempt", "raw_file", "usage", "verdict", "error"):
            if key in member:
                result[key] = member[key]
    else:
        errors = [
            f"{member['judge_model']}: {member.get('error', '')}"
            for member in panel
            if member["status"] != "judged"
        ]
        if errors:
            result["error"] = "; ".join(errors)[:500]
    return result


def write_results(
    path: Path,
    results: dict[tuple[str, str], dict[str, Any]],
    task_order: dict[str, int],
    model_order: dict[str, int],
) -> None:
    ordered = sorted(
        results.values(),
        key=lambda item: (
            task_order[item["id"]],
            model_order[item["solver_model"]],
        ),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in ordered),
        encoding="utf-8",
    )
    temporary.replace(path)


def generation_fingerprint(generation: dict[str, Any]) -> str:
    record = json.dumps(
        generation,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    patch = Path(str(generation.get("patch_file") or ""))
    patch_bytes = patch.read_bytes() if patch.is_file() else b""
    return hashlib.sha256(record + b"\0" + patch_bytes).hexdigest()


def generation_set_digests(
    generations: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, str]:
    return {
        model: hashlib.sha256(
            "".join(
                f"{task_id}\t{generation_fingerprint(record)}\n"
                for task_id, record in sorted(records.items())
            ).encode()
        ).hexdigest()
        for model, records in sorted(generations.items())
    }


def completion_marker(path: Path) -> Path:
    return path.with_name(f"{path.stem}.complete.json")


def write_completion_marker(
    path: Path,
    task_fingerprints: dict[str, str],
    models: set[str],
    results: dict[tuple[str, str], dict[str, Any]],
    tasks_sha256: str,
    generation_sha256: dict[str, str],
    generation_fingerprints: dict[tuple[str, str], str],
    protocol_fingerprint: str,
    rubric: str,
    judge_model: str,
    max_context_chars: int,
    max_output_tokens: int | None,
    attestation_mode: str = "runtime",
) -> None:
    expected = {
        (task_id, model)
        for task_id in task_fingerprints
        for model in models
    }
    if set(results) != expected:
        completion_marker(path).unlink(missing_ok=True)
        return
    unresolved = [
        key
        for key, result in results.items()
        if result.get("status")
        in {"judge_error", "input_error", "missing_patch", "content_filtered"}
    ]
    unresolved.extend(
        key
        for key, result in results.items()
        if result.get("task_fingerprint")
        != task_fingerprints.get(key[0])
    )
    unresolved.extend(
        key
        for key, result in results.items()
        if result.get("judge_protocol_fingerprint")
        != protocol_fingerprint
    )
    unresolved.extend(
        key
        for key, result in results.items()
        if result.get("generation_fingerprint")
        != generation_fingerprints.get(key)
    )
    if unresolved:
        completion_marker(path).unlink(missing_ok=True)
        return
    marker = {
        "schema_version": "reviewbench-judgments-complete/v1",
        "attestation_mode": attestation_mode,
        "tasks": len(task_fingerprints),
        "models": sorted(models),
        "task_model_pairs": len(results),
        "tasks_sha256": tasks_sha256,
        "generation_sha256": generation_sha256,
        "judge_protocol_fingerprint": protocol_fingerprint,
        "rubric": rubric,
        "judge_model": judge_model,
        "judge_panel": (
            [judge_model] if isinstance(judge_model, str) else list(judge_model)
        ),
        "judge_decision_rule": (
            "single"
            if isinstance(judge_model, str) or len(judge_model) == 1
            else "unanimous"
        ),
        "max_context_chars": max_context_chars,
        "max_output_tokens": max_output_tokens,
        "results_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "pair_ids_sha256": hashlib.sha256(
            "".join(
                f"{task_id}\t{model}\n"
                for task_id, model in sorted(expected)
            ).encode()
        ).hexdigest(),
        "statuses": dict(
            Counter(result.get("status") for result in results.values())
        ),
        "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    completion_marker(path).write_text(
        json.dumps(marker, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    args = parse_args()
    tasks = load_jsonl(args.tasks)
    tasks_by_id = {task["id"]: task for task in tasks}
    generation_paths = parse_generation_args(args.generation)
    generations = {
        model: {item["id"]: item for item in load_jsonl(path)}
        for model, path in generation_paths.items()
    }
    missing = {
        model: set(tasks_by_id) - set(items)
        for model, items in generations.items()
        if set(tasks_by_id) - set(items)
    }
    if missing:
        raise ValueError(f"incomplete generation files: {missing}")

    results: dict[tuple[str, str], dict[str, Any]] = {}
    if args.resume and args.out.exists():
        results = {
            (item["id"], item["solver_model"]): item
            for item in load_jsonl(args.out)
        }
    jobs = [
        (task, generations[model][task["id"]], model)
        for task in tasks
        for model in generation_paths
        if (
            (task["id"], model) not in results
            or results[(task["id"], model)].get("generation_fingerprint")
            != generation_fingerprint(generations[model][task["id"]])
            or results[(task["id"], model)].get("task_fingerprint")
            != agentic.task_fingerprint(task)
            or results[(task["id"], model)].get(
                "judge_protocol_fingerprint"
            )
            != judge_protocol_fingerprint(
                args.rubric,
                args.judge_model,
                args.max_context_chars,
                args.max_output_tokens,
                args.max_output_token_ceiling,
            )
            or (
                generations[model][task["id"]].get("apply_ok") is True
                and results[(task["id"], model)].get("status")
                in {
                    "generation_failed",
                    "missing_patch",
                    "judge_error",
                    "input_error",
                }
            )
        )
    ]
    if jobs:
        completion_marker(args.out).unlink(missing_ok=True)
    task_order = {task["id"]: index for index, task in enumerate(tasks)}
    model_order = {model: index for index, model in enumerate(generation_paths)}
    lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(judge_one, task, generation, model, args): (task, model)
            for task, generation, model in jobs
        }
        for future in as_completed(futures):
            task, model = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {
                    "id": task["id"],
                    "repo": task.get("repo"),
                    "solver_model": model,
                    "judge_model": args.judge_model,
                    "status": "judge_error",
                    "maintainability_pass": False,
                    "error": str(exc)[:500],
                }
            with lock:
                results[(task["id"], model)] = result
                write_results(args.out, results, task_order, model_order)
            print(
                f"[{model}] {task['id']}: {result.get('status')} "
                f"pass={result.get('maintainability_pass')}",
                flush=True,
            )

    summary = {
        model: {
            "tasks": sum(item["solver_model"] == model for item in results.values()),
            "passed": sum(
                item["solver_model"] == model
                and item.get("maintainability_pass") is True
                for item in results.values()
            ),
            "errors": sum(
                item["solver_model"] == model
                and item.get("status")
                in {"judge_error", "input_error", "missing_patch"}
                for item in results.values()
            ),
            "content_filtered": sum(
                item["solver_model"] == model
                and item.get("status") == "content_filtered"
                for item in results.values()
            ),
        }
        for model in generation_paths
    }
    write_completion_marker(
        args.out,
        {
            task_id: agentic.task_fingerprint(task)
            for task_id, task in tasks_by_id.items()
        },
        set(generation_paths),
        results,
        hashlib.sha256(args.tasks.read_bytes()).hexdigest(),
        generation_set_digests(generations),
        {
            (task_id, model): generation_fingerprint(record)
            for model, records in generations.items()
            for task_id, record in records.items()
        },
        judge_protocol_fingerprint(
            args.rubric,
            args.judge_model,
            args.max_context_chars,
            args.max_output_tokens,
        ),
        args.rubric,
        args.judge_model,
        args.max_context_chars,
        args.max_output_tokens,
        args.max_output_token_ceiling,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if all(item["errors"] == 0 for item in summary.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
