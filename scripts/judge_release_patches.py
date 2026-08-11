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
    judge_model: str,
    max_context_chars: int,
    max_output_tokens: int | None,
) -> str:
    payload = {
        "prompt_version": JUDGE_PROMPT_VERSION,
        "rubric": rubric,
        "rubric_instructions": RUBRICS[rubric],
        "judge_model": judge_model,
        "max_context_chars": max_context_chars,
        "max_output_tokens": max_output_tokens,
    }
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
    parser.add_argument("--judge-model", default="claude-opus-4.8")
    parser.add_argument("--rubric", choices=("v1", "v2"), default="v1")
    parser.add_argument(
        "--chat-endpoint",
        default="http://127.0.0.1:4399/v1/chat/completions",
    )
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--max-context-chars", type=int, default=30_000)
    parser.add_argument("--max-output-tokens", type=int, default=2048)
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args()


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
) -> tuple[str, dict[str, Any]]:
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
    if actual_model and actual_model != model:
        raise RuntimeError(
            f"judge model mismatch: requested {model}, gateway returned "
            f"{actual_model}; refusing a silent fallback"
        )
    return generator.chat_text(data), data.get("usage") or {}


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
    )
    base = {
        "id": task["id"],
        "repo": task.get("repo"),
        "solver_model": solver_model,
        "judge_model": args.judge_model,
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

    raw_path = (
        args.raw_dir
        / generator.safe_component(solver_model)
        / f"{task['id']}.txt"
    )
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    last_error = ""
    attempt_prompt = prompt
    for attempt in range(1, 4):
        text = ""
        try:
            text, usage = invoke_judge(
                args.judge_model,
                attempt_prompt,
                args.chat_endpoint,
                args.max_output_tokens,
                args.rubric,
            )
            raw_path.write_text(text, encoding="utf-8")
            raw_path.with_name(
                f"{raw_path.stem}.attempt-{attempt}{raw_path.suffix}"
            ).write_text(text, encoding="utf-8")
            verdict = parse_verdict(text, args.rubric)
            passed = verdict_passes(verdict, args.rubric)
            return {
                **base,
                "status": "judged",
                "maintainability_pass": passed,
                "attempt": attempt,
                "patch_file": str(patch_path),
                "raw_file": str(raw_path),
                "usage": usage,
                "verdict": verdict,
            }
        except Exception as exc:
            last_error = str(exc)[:500]
            if text:
                attempt_prompt = build_repair_prompt(
                    prompt,
                    text,
                    last_error,
                    args.rubric,
                )
    return {
        **base,
        "status": "judge_error",
        "maintainability_pass": False,
        "error": last_error,
        "raw_file": str(raw_path),
    }


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
        in {"judge_error", "input_error", "missing_patch"}
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
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if all(item["errors"] == 0 for item in summary.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
