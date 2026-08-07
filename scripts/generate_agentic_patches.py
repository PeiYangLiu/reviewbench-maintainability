#!/usr/bin/env python3
"""Generate patches by letting a model edit a scoped Git worktree.

Unlike the SEARCH/REPLACE baseline, the model operates through line-oriented
tools. The host applies valid tool actions to the current file, then Git
computes the final unified diff from the resulting worktree.
"""

from __future__ import annotations

import argparse
import ast
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from typing import Any
import warnings

import generate_release_patches as api


AGENT_INSTRUCTIONS = """You are an expert software engineer editing a real Git worktree.

Address the reviewer concern with the smallest complete, behavior-preserving
maintainability change. You may inspect repository files, but may edit only the
anchored production file. Do not add tests or modify unrelated code.

Return exactly one JSON object per turn, with one of these actions:

{"action":"read","path":"path/to/file","start_line":1,"end_line":200}
{"action":"find","path":"path/to/file","query":"literal text"}
{"action":"edit","edits":[
  {"start_line":10,"end_line":20,"content":"replacement text"},
  {"start_line":40,"end_line":39,"content":"text inserted before line 40"}
]}
{"action":"finish"}

Line numbers are 1-based and end_line is inclusive. For insertion before a
line, set end_line=start_line-1. Empty content deletes a non-empty range.
Multiple edits in one action must not overlap. After an edit, line numbers may
change; use read again when needed.

Do not return Markdown, prose, SEARCH/REPLACE blocks, or a unified diff."""

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--repo-cache", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument(
        "--models",
        nargs="+",
        choices=sorted(api.MODEL_SPECS),
        default=list(api.MODEL_SPECS),
    )
    parser.add_argument("--ids", nargs="+")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--max-rounds", type=int, default=32)
    parser.add_argument("--max-output-tokens", type=int, default=8192)
    parser.add_argument("--request-timeout", type=int, default=300)
    parser.add_argument("--request-retries", type=int, default=6)
    parser.add_argument("--initial-context-chars", type=int, default=30_000)
    parser.add_argument(
        "--chat-endpoint",
        default="http://127.0.0.1:4399/v1/chat/completions",
    )
    parser.add_argument(
        "--responses-endpoint",
        default="https://api.githubcopilot.com/responses",
    )
    parser.add_argument(
        "--token-endpoint",
        default="http://127.0.0.1:4399/token",
    )
    parser.add_argument("--force-chat", action="store_true")
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args()


def object_candidates(text: str) -> list[str]:
    """Extract balanced object literals, including valid nested objects."""
    candidates: list[tuple[int, str]] = []
    starts = []
    quote = False
    escaped = False
    for index, character in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quote = False
            continue
        if character == '"':
            quote = True
        elif character == "{":
            starts.append(index)
        elif character == "}" and starts:
            start = starts.pop()
            candidates.append((start, text[start : index + 1]))
    return [candidate for _, candidate in sorted(candidates)]


def decode_object(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as json_error:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                return ast.literal_eval(text)
        except (SyntaxError, ValueError):
            raise json_error


def parse_action(
    text: str,
    *,
    prefer_edit: bool = False,
) -> dict[str, Any]:
    stripped = text.strip()
    try:
        direct = decode_object(stripped)
    except (json.JSONDecodeError, SyntaxError, ValueError):
        direct = None
    if isinstance(direct, dict):
        nested = direct.get("assistant_action")
        if not direct.get("action") and isinstance(nested, dict):
            direct = nested
        if str(direct.get("action") or "") in {"read", "find", "edit", "finish"}:
            return direct

    candidates = object_candidates(text)
    errors = []
    seen = set()
    selected = None
    selected_edit = None
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            value = decode_object(candidate)
        except (json.JSONDecodeError, SyntaxError, ValueError) as exc:
            errors.append(str(exc))
            continue
        if not isinstance(value, dict):
            errors.append("action must be a JSON object")
            continue
        nested = value.get("assistant_action")
        if not value.get("action") and isinstance(nested, dict):
            value = nested
        action = str(value.get("action") or "")
        if action in {"read", "find", "edit", "finish"}:
            selected = value
            if action == "edit":
                selected_edit = value
            continue
        errors.append(f"unsupported action: {action!r}")
    if selected is not None:
        if prefer_edit and selected.get("action") == "finish" and selected_edit:
            return selected_edit
        return selected
    detail = errors[-1] if errors else "no object literal found"
    raise ValueError(f"model did not return a valid JSON action: {detail}")


def numbered_excerpt(
    lines: list[str],
    anchor_line: int,
    maximum_chars: int,
) -> tuple[str, int, int]:
    source = "\n".join(lines) + "\n"
    excerpt, first, last = api.source_excerpt(source, anchor_line, maximum_chars)
    numbered = "\n".join(
        f"{line_number}: {text}"
        for line_number, text in enumerate(
            excerpt.splitlines(),
            start=first,
        )
    )
    return numbered, first, last


def validate_repo_path(path: str) -> str:
    value = Path(path)
    if value.is_absolute() or ".." in value.parts or not value.parts:
        raise ValueError(f"invalid repository path: {path!r}")
    return value.as_posix()


def read_at_start(
    task: dict[str, Any],
    repo_cache: Path,
    path: str,
) -> str:
    path = validate_repo_path(path)
    completed = subprocess.run(
        ["git", "show", f"{task['start_sha']}:{path}"],
        cwd=api.repo_dir(repo_cache, str(task["repo"])),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0:
        raise ValueError(f"file unavailable at start_sha: {path}")
    return completed.stdout


def read_lines(
    text: str,
    start_line: int,
    end_line: int,
    maximum_lines: int = 400,
) -> dict[str, Any]:
    lines = text.splitlines()
    if start_line < 1 or end_line < start_line:
        raise ValueError("invalid read range")
    end_line = min(end_line, len(lines), start_line + maximum_lines - 1)
    if start_line > len(lines):
        raise ValueError("read starts after end of file")
    return {
        "path_lines": len(lines),
        "start_line": start_line,
        "end_line": end_line,
        "content": "\n".join(
            f"{number}: {lines[number - 1]}"
            for number in range(start_line, end_line + 1)
        ),
    }


def find_text(text: str, query: str, limit: int = 20) -> dict[str, Any]:
    if not query:
        raise ValueError("find query is empty")
    matches = [
        {"line": number, "text": line}
        for number, line in enumerate(text.splitlines(), start=1)
        if query in line
    ][:limit]
    return {"query": query, "matches": matches, "truncated": len(matches) == limit}


def apply_line_edits(text: str, edits: Any) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(edits, list) or not edits:
        raise ValueError("edit action requires a non-empty edits list")
    lines = text.splitlines()
    had_final_newline = text.endswith("\n")
    parsed = []
    for value in edits:
        if not isinstance(value, dict):
            raise ValueError("each edit must be an object")
        start = value.get("start_line")
        end = value.get("end_line")
        content = value.get("content")
        if type(start) is not int or type(end) is not int or not isinstance(content, str):
            raise ValueError("edit requires integer line bounds and string content")
        if start < 1 or start > len(lines) + 1:
            raise ValueError(f"edit start out of range: {start}")
        if end < start - 1 or end > len(lines):
            raise ValueError(f"edit end out of range: {end}")
        parsed.append((start, end, content))

    ascending = sorted(parsed)
    previous_end = 0
    previous_start = None
    for start, end, _ in ascending:
        effective_end = max(end, start - 1)
        if start <= previous_end or start == previous_start:
            raise ValueError("edit ranges overlap")
        previous_start = start
        previous_end = effective_end

    applied = []
    for start, end, content in sorted(parsed, reverse=True):
        replacement = content.splitlines()
        start_index = start - 1
        end_index = end if end >= start else start_index
        lines[start_index:end_index] = replacement
        applied.append(
            {
                "start_line": start,
                "end_line": end,
                "replacement_lines": len(replacement),
            }
        )
    output = "\n".join(lines)
    if had_final_newline and lines:
        output += "\n"
    return output, list(reversed(applied))


def prepare_line_edit(
    text: str,
    edits: Any,
) -> tuple[str, dict[str, Any]]:
    candidate, applied = apply_line_edits(text, edits)
    first = min(item["start_line"] for item in applied)
    line_count = len(candidate.splitlines())
    updated_excerpt = (
        read_lines(
            candidate,
            max(1, first - 10),
            min(line_count, first + 30),
        )
        if line_count
        else {
            "path_lines": 0,
            "start_line": 0,
            "end_line": 0,
            "content": "",
        }
    )
    return candidate, {
        "applied": applied,
        "current_file_lines": line_count,
        "updated_excerpt": updated_excerpt,
    }


def edit_action_is_valid(action: dict[str, Any], editable_path: str) -> bool:
    requested_path = action.get("path")
    if requested_path:
        try:
            if validate_repo_path(str(requested_path)) != editable_path:
                return False
        except ValueError:
            return False
    insertion_points = [
        edit.get("start_line")
        for edit in action.get("edits") or []
        if isinstance(edit, dict)
        and edit.get("end_line") == edit.get("start_line", 0) - 1
    ]
    return len(insertion_points) == len(set(insertion_points))


def resume_agent_state(
    before: str,
    editable_path: str,
    previous: dict[str, Any] | None,
    max_rounds: int,
) -> tuple[str, list[dict[str, Any]], bool, list[dict[str, Any]]] | None:
    if not previous or previous.get("apply_ok") is True:
        return None
    rounds = list(previous.get("rounds") or [])
    if not rounds or len(rounds) > max_rounds:
        return None

    current = before
    transcript = []
    edited = False
    for round_result in rounds:
        action = round_result.get("action")
        tool_result = round_result.get("tool_result")
        if round_result.get("error"):
            transcript.append({"tool_error": str(round_result["error"])[:500]})
            continue
        if not isinstance(action, dict) or tool_result is None:
            return None
        if action.get("action") == "edit":
            if not edit_action_is_valid(action, editable_path):
                return None
            try:
                current, _ = apply_line_edits(current, action.get("edits"))
            except ValueError:
                return None
            edited = True
        transcript.append({"assistant_action": action, "tool_result": tool_result})
    return current, transcript, edited, rounds


def git_diff(path: str, before: str, after: str) -> str:
    with tempfile.TemporaryDirectory(prefix="reviewbench-agentic-") as directory:
        root = Path(directory)
        subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "reviewbench@example.invalid"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "ReviewBench"], cwd=root, check=True)
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(before, encoding="utf-8")
        subprocess.run(["git", "add", "--", path], cwd=root, check=True)
        subprocess.run(["git", "commit", "--quiet", "-m", "start"], cwd=root, check=True)
        target.write_text(after, encoding="utf-8")
        completed = subprocess.run(
            ["git", "diff", "--binary", "--", path],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
        return completed.stdout


def build_initial_prompt(
    task: dict[str, Any],
    source: str,
    maximum_chars: int,
) -> str:
    locality = task.get("locality") or {}
    anchor = int(locality.get("line") or locality.get("start_line") or 1)
    lines = source.splitlines()
    excerpt, first, last = numbered_excerpt(lines, anchor, maximum_chars)
    return (
        f"REPOSITORY: {task['repo']}\n"
        f"EDITABLE FILE: {locality.get('path')}\n"
        f"TOTAL LINES: {len(lines)}\n"
        f"REVIEW ANCHOR: line {anchor}\n"
        f"REVIEWER CONCERN:\n{task.get('task_instruction') or ''}\n\n"
        f"INITIAL EXCERPT (lines {first}-{last}):\n{excerpt}\n\n"
        "Choose the next JSON tool action."
    )


def invoke(
    model: str,
    prompt: str,
    args: argparse.Namespace,
) -> tuple[str, dict[str, Any]]:
    invoke_args = argparse.Namespace(
        chat_endpoint=args.chat_endpoint,
        responses_endpoint=args.responses_endpoint,
        token_endpoint=args.token_endpoint,
        max_output_tokens=args.max_output_tokens,
        request_timeout=args.request_timeout,
        request_retries=args.request_retries,
        force_chat=args.force_chat,
    )
    return api.invoke_model(model, AGENT_INSTRUCTIONS, prompt, invoke_args)


def generate_one(
    task: dict[str, Any],
    model: str,
    args: argparse.Namespace,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    task_id = str(task["id"])
    editable_path = validate_repo_path(str((task.get("locality") or {}).get("path") or ""))
    model_root = args.out_dir / api.safe_component(model)
    patch_root = model_root / "patches"
    trace_root = model_root / "traces"
    patch_root.mkdir(parents=True, exist_ok=True)
    trace_root.mkdir(parents=True, exist_ok=True)
    patch_path = patch_root / f"{task_id}.patch"
    patch_path.unlink(missing_ok=True)
    result: dict[str, Any] = {
        "id": task_id,
        "repo": task.get("repo"),
        "path": editable_path,
        "start_sha": task.get("start_sha"),
        "model": model,
        "protocol": "agentic-line-edit/v1",
        "status": "failed",
        "apply_ok": False,
        "rounds": [],
    }
    try:
        before = read_at_start(task, args.repo_cache, editable_path)
    except Exception as exc:
        result["error"] = f"source: {exc}"[:500]
        result["duration_seconds"] = round(time.monotonic() - started, 3)
        return result

    resumed = resume_agent_state(
        before,
        editable_path,
        previous,
        args.max_rounds,
    )
    if resumed is None:
        current = before
        transcript = []
        edited = False
        first_round = 1
    else:
        current, transcript, edited, prior_rounds = resumed
        result["rounds"] = prior_rounds
        result["resumed_from_round"] = len(prior_rounds)
        first_round = len(prior_rounds) + 1

    for round_number in range(first_round, args.max_rounds + 1):
        prompt = build_initial_prompt(task, before, args.initial_context_chars)
        if transcript:
            prompt += "\n\nTOOL TRANSCRIPT:\n" + "\n".join(
                json.dumps(event, ensure_ascii=False, sort_keys=True)
                for event in transcript
            )
        prompt += "\n\nReturn the next JSON action only."
        round_result: dict[str, Any] = {"round": round_number}
        text = ""
        try:
            text, usage = invoke(model, prompt, args)
            action = parse_action(text, prefer_edit=not edited)
            round_result.update({"action": action, "usage": usage, "response_chars": len(text)})
            object_count = len(object_candidates(text))
            round_result["response_object_count"] = object_count
            if object_count > 1:
                round_result["raw_response_tail"] = text[-20_000:]
            name = action["action"]
            if name == "read":
                path = validate_repo_path(str(action.get("path") or editable_path))
                source = current if path == editable_path else read_at_start(task, args.repo_cache, path)
                tool_result = read_lines(
                    source,
                    int(action.get("start_line")),
                    int(action.get("end_line")),
                )
            elif name == "find":
                path = validate_repo_path(str(action.get("path") or editable_path))
                source = current if path == editable_path else read_at_start(task, args.repo_cache, path)
                tool_result = find_text(source, str(action.get("query") or ""))
            elif name == "edit":
                if not edit_action_is_valid(action, editable_path):
                    raise ValueError("edit action targets a non-editable file")
                candidate, tool_result = prepare_line_edit(
                    current,
                    action.get("edits"),
                )
                current = candidate
                edited = True
            else:
                if not edited or current == before:
                    tool_result = {"ok": False, "error": "finish rejected: no file changes"}
                else:
                    patch = git_diff(editable_path, before, current)
                    if not patch.strip():
                        tool_result = {"ok": False, "error": "finish rejected: empty Git diff"}
                    else:
                        patch_path.write_text(patch, encoding="utf-8")
                        result.update(
                            {
                                "status": "generated",
                                "apply_ok": True,
                                "patch_file": str(patch_path),
                                "changed_lines": api.changed_line_count(patch),
                                "successful_round": round_number,
                            }
                        )
                        round_result["tool_result"] = {"ok": True, "patch_file": str(patch_path)}
                        result["rounds"].append(round_result)
                        break
            round_result["tool_result"] = tool_result
            transcript.append({"assistant_action": action, "tool_result": tool_result})
        except Exception as exc:
            round_result["error"] = str(exc)[:500]
            if text:
                round_result["raw_response"] = text[:20_000]
            transcript.append({"tool_error": round_result["error"]})
        result["rounds"].append(round_result)

    if not result["apply_ok"] and edited and current != before:
        patch = git_diff(editable_path, before, current)
        if patch.strip():
            patch_path.write_text(patch, encoding="utf-8")
            result.update(
                {
                    "status": "generated",
                    "apply_ok": True,
                    "patch_file": str(patch_path),
                    "changed_lines": api.changed_line_count(patch),
                    "successful_round": len(result["rounds"]),
                    "finish_mode": "round_limit",
                }
            )

    trace_path = trace_root / f"{task_id}.json"
    trace_path.write_text(
        json.dumps(result["rounds"], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    result["trace_file"] = str(trace_path)
    if not result["apply_ok"]:
        result["error"] = result["rounds"][-1].get("error") or "agent did not finish with a patch"
    prior_duration = float(previous.get("duration_seconds") or 0) if resumed else 0
    result["duration_seconds"] = round(
        prior_duration + time.monotonic() - started,
        3,
    )
    return result


def load_results(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    return {
        item["id"]: item
        for item in (
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    }


def reusable_result(item: dict[str, Any]) -> bool:
    if (
        item.get("apply_ok") is not True
        or item.get("protocol") != "agentic-line-edit/v1"
        or not Path(str(item.get("patch_file") or "")).is_file()
    ):
        return False
    editable_path = str(item.get("path") or "")
    for round_result in item.get("rounds") or []:
        if round_result.get("error"):
            continue
        action = round_result.get("action") or {}
        if action.get("action") != "edit":
            continue
        if not edit_action_is_valid(action, editable_path):
            return False
    return True


def write_results(
    path: Path,
    order: dict[str, int],
    results: dict[str, dict[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(results.values(), key=lambda item: order[item["id"]])
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in ordered),
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    args = parse_args()
    all_tasks = api.load_tasks(args.tasks, None, None)
    tasks = api.load_tasks(args.tasks, set(args.ids) if args.ids else None, args.limit)
    order = {task["id"]: index for index, task in enumerate(all_tasks)}
    paths = {
        model: args.out_dir / api.safe_component(model) / "generation_results.jsonl"
        for model in args.models
    }
    results = {
        model: load_results(path) if args.resume else {}
        for model, path in paths.items()
    }
    jobs = [
        (task, model)
        for model in args.models
        for task in tasks
        if not reusable_result(results[model].get(task["id"], {}))
    ]
    lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                generate_one,
                task,
                model,
                args,
                results[model].get(task["id"]),
            ): (task, model)
            for task, model in jobs
        }
        for future in as_completed(futures):
            task, model = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {
                    "id": task["id"],
                    "repo": task.get("repo"),
                    "model": model,
                    "protocol": "agentic-line-edit/v1",
                    "status": "failed",
                    "apply_ok": False,
                    "error": str(exc)[:500],
                }
            with lock:
                results[model][task["id"]] = result
                write_results(paths[model], order, results[model])
            print(
                f"[{model}] {task['id']}: {result.get('status')} "
                f"rounds={len(result.get('rounds') or [])} "
                f"lines={result.get('changed_lines')}",
                flush=True,
            )

    summary = {
        model: {
            "tasks": len(items),
            "generated": sum(item.get("apply_ok") is True for item in items.values()),
            "failed": sum(item.get("apply_ok") is not True for item in items.values()),
        }
        for model, items in results.items()
    }
    (args.out_dir / "generation_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if all(item["failed"] == 0 for item in summary.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
