#!/usr/bin/env python3
"""Generate model patches for a versioned ReviewBench release.

The solver receives only the reviewer instruction and source at start_sha.
Reference patches, quality judgments, target source, and oracle results are
never included in the prompt.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import difflib
import hashlib
import json
from pathlib import Path
import re
import subprocess
import threading
import time
from typing import Any
import urllib.error
import urllib.request


MODEL_SPECS = {
    "claude-opus-4.8": {"provider": "chat", "reasoning_effort": None},
    "gemini-3.5-flash": {"provider": "chat", "reasoning_effort": "medium"},
    "gemini-3.6-flash": {"provider": "chat", "reasoning_effort": "medium"},
    "gpt-5.5": {
        "provider": "responses",
        "reasoning_effort": "medium",
        "response_models": {"gpt-5.5", "gpt-5.5-2026-04-23"},
    },
    "gpt-5.6-luna": {"provider": "responses", "reasoning_effort": "medium"},
    "gpt-5.6-sol": {"provider": "responses", "reasoning_effort": "medium"},
    "gpt-5.6-terra": {"provider": "responses", "reasoning_effort": "medium"},
}
SOLVER_INSTRUCTIONS = """You are an expert software engineer addressing one code review request.

Make a focused, production-quality maintainability improvement while preserving
all existing behavior and public contracts. Do not add tests, change unrelated
code, or merely describe a solution.

Return one or more edits and nothing else, each in this exact format:
<<<<<<< SEARCH
exact source text to replace
=======
replacement source text
>>>>>>> REPLACE

Every SEARCH block must match the supplied source exactly, including whitespace.
Use the smallest complete change that fully addresses the reviewer concern."""
SEARCH_REPLACE_RE = re.compile(
    r"<<<<<<< SEARCH\n(.*?)\n=======\n(.*?)\n>>>>>>> REPLACE",
    re.DOTALL,
)
RETRYABLE_HTTP_CODES = {408, 409, 429, 500, 502, 503, 504}
TOKEN_LOCK = threading.Lock()
TOKEN_CACHE: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--repo-cache", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument(
        "--models",
        nargs="+",
        default=list(MODEL_SPECS),
        help=(
            "Model IDs. Known Copilot models use their configured provider; "
            "unknown IDs default to an OpenAI-compatible chat endpoint."
        ),
    )
    parser.add_argument("--ids", nargs="+")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--max-context-chars", type=int, default=100_000)
    parser.add_argument("--max-output-tokens", type=int, default=8192)
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
    parser.add_argument(
        "--force-chat",
        action="store_true",
        help="Send every model through the OpenAI-compatible chat endpoint.",
    )
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args()


def load_tasks(path: Path, ids: set[str] | None, limit: int | None) -> list[dict[str, Any]]:
    tasks = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if ids is not None:
        tasks = [task for task in tasks if str(task.get("id")) in ids]
        missing = ids - {str(task.get("id")) for task in tasks}
        if missing:
            raise ValueError(f"unknown task IDs: {sorted(missing)}")
    return tasks[:limit] if limit is not None else tasks


def repo_dir(repo_cache: Path, repo: str) -> Path:
    return repo_cache / repo.replace("/", "__")


def source_at_start(task: dict[str, Any], repo_cache: Path) -> str:
    path = str((task.get("locality") or {}).get("path") or "")
    start_sha = str(task.get("start_sha") or "")
    repository = repo_dir(repo_cache, str(task.get("repo") or "")).resolve()
    completed = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={repository}",
            "show",
            f"{start_sha}:{path}",
        ],
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip()[:300] or "git show failed")
    return completed.stdout


def source_excerpt(source: str, anchor_line: int, maximum_chars: int) -> tuple[str, int, int]:
    lines = source.splitlines(keepends=True)
    if len(source) <= maximum_chars:
        return source, 1, len(lines)

    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    anchor_index = min(max(anchor_line - 1, 0), max(len(lines) - 1, 0))
    anchor_offset = offsets[anchor_index]
    half = maximum_chars // 2
    start_offset = max(0, anchor_offset - half)
    end_offset = min(len(source), start_offset + maximum_chars)
    if end_offset == len(source):
        start_offset = max(0, end_offset - maximum_chars)

    start_line = 0
    while start_line < len(lines) and offsets[start_line + 1] <= start_offset:
        start_line += 1
    end_line = start_line
    while end_line < len(lines) and offsets[end_line] < end_offset:
        end_line += 1
    return "".join(lines[start_line:end_line]), start_line + 1, end_line


def build_prompt(
    task: dict[str, Any],
    source: str,
    maximum_chars: int,
) -> tuple[str, dict[str, Any]]:
    locality = task.get("locality") or {}
    anchor_line = int(locality.get("line") or locality.get("start_line") or 1)
    excerpt, first_line, last_line = source_excerpt(source, anchor_line, maximum_chars)
    total_lines = len(source.splitlines())
    context_kind = "full_file" if first_line == 1 and last_line == total_lines else "excerpt"
    prompt = (
        f"REPOSITORY: {task.get('repo')}\n"
        f"FILE: {locality.get('path')}\n"
        f"REVIEWER CONCERN:\n{task.get('task_instruction') or ''}\n\n"
        f"SOURCE AT START_SHA ({context_kind}, lines {first_line}-{last_line} "
        f"of {total_lines}):\n{excerpt}"
    )
    return prompt, {
        "context_kind": context_kind,
        "context_first_line": first_line,
        "context_last_line": last_line,
        "source_total_lines": total_lines,
        "prompt_chars": len(prompt),
    }


def apply_search_replace(original: str, response: str) -> tuple[str | None, str]:
    blocks = SEARCH_REPLACE_RE.findall(response)
    if not blocks:
        return None, "no_search_replace_blocks"
    content = original
    for index, (search, replace) in enumerate(blocks, start=1):
        if search in content:
            content = content.replace(search, replace, 1)
            continue
        content_lines = content.split("\n")
        search_lines = search.split("\n")
        normalized = [line.rstrip() for line in content_lines]
        wanted = [line.rstrip() for line in search_lines]
        found = -1
        for offset in range(len(normalized) - len(wanted) + 1):
            if normalized[offset : offset + len(wanted)] == wanted:
                found = offset
                break
        if found < 0:
            return None, f"search_block_{index}_not_found"
        content = "\n".join(
            content_lines[:found]
            + replace.split("\n")
            + content_lines[found + len(search_lines) :]
        )
    if content == original:
        return None, "no_op"
    return content, "ok"


def unified_patch(path: str, before: str, after: str) -> str:
    patch = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=3,
    )
    text = "".join(patch)
    return text if text.endswith("\n") else text + "\n"


def changed_line_count(patch: str) -> int:
    return sum(
        1
        for line in patch.splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    )


def post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: int = 300,
    retries: int = 6,
) -> dict[str, Any]:
    delay = 2.0
    for attempt in range(retries):
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"content-type": "application/json", **headers},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code not in RETRYABLE_HTTP_CODES or attempt == retries - 1:
                raise RuntimeError(f"HTTP {exc.code}: {body[:500]}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == retries - 1:
                raise RuntimeError(str(exc)) from exc
        time.sleep(delay)
        delay = min(delay * 2, 30)
    raise RuntimeError("request failed")


def copilot_token(endpoint: str, refresh: bool = False) -> str:
    global TOKEN_CACHE
    with TOKEN_LOCK:
        if TOKEN_CACHE and not refresh:
            return TOKEN_CACHE
        with urllib.request.urlopen(endpoint, timeout=30) as response:
            value = json.loads(response.read().decode("utf-8"))
        token = str(value.get("token") or "")
        if not token:
            raise RuntimeError("token endpoint returned no token")
        TOKEN_CACHE = token
        return token


def response_text(data: dict[str, Any]) -> str:
    parts = [
        str(content["text"])
        for item in data.get("output") or []
        for content in item.get("content") or []
        if content.get("text")
    ]
    if not parts and data.get("output_text"):
        parts.append(str(data["output_text"]))
    return "\n".join(parts).strip()


def chat_text(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    content = (choices[0].get("message") or {}).get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict)
        ).strip()
    return ""


def invoke_model_envelope(
    model: str,
    instructions: str,
    prompt: str,
    args: argparse.Namespace,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    spec = MODEL_SPECS.get(
        model,
        {"provider": "chat", "reasoning_effort": None},
    )
    accepted_response_models = set(spec.get("response_models") or {model})
    if spec["provider"] == "chat" or getattr(args, "force_chat", False):
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": args.max_output_tokens,
            "stream": False,
        }
        if spec["reasoning_effort"]:
            payload["reasoning_effort"] = spec["reasoning_effort"]
        data = post_json(
            args.chat_endpoint,
            payload,
            {},
            timeout=getattr(args, "request_timeout", 300),
            retries=getattr(args, "request_retries", 6),
        )
        actual_model = str(data.get("model") or "")
        if actual_model not in accepted_response_models:
            raise RuntimeError(
                f"solver model mismatch: requested {model}, gateway returned "
                f"{actual_model or '<missing>'}; refusing an unattested "
                f"or silent fallback; accepted identities are "
                f"{sorted(accepted_response_models)}"
            )
        return chat_text(data), data.get("usage") or {}, data

    payload = {
        "model": model,
        "stream": False,
        "instructions": instructions,
        "input": prompt,
        "max_output_tokens": args.max_output_tokens,
        "reasoning": {"effort": spec["reasoning_effort"]},
    }
    for auth_attempt in range(2):
        token = copilot_token(args.token_endpoint, refresh=auth_attempt > 0)
        headers = {
            "Authorization": f"Bearer {token}",
            "copilot-integration-id": "vscode-chat",
            "editor-version": "vscode/1.100.0",
            "editor-plugin-version": "copilot-chat/0.26.7",
            "x-github-api-version": "2025-04-01",
        }
        try:
            data = post_json(
                args.responses_endpoint,
                payload,
                headers,
                timeout=getattr(args, "request_timeout", 300),
                retries=getattr(args, "request_retries", 6),
            )
            break
        except RuntimeError as exc:
            if auth_attempt == 0 and (
                "HTTP 401:" in str(exc) or "HTTP 403:" in str(exc)
            ):
                continue
            raise
    actual_model = str(data.get("model") or "")
    if actual_model not in accepted_response_models:
        raise RuntimeError(
            f"solver model mismatch: requested {model}, gateway returned "
            f"{actual_model or '<missing>'}; refusing an unattested or "
            f"silent fallback; accepted identities are "
            f"{sorted(accepted_response_models)}"
        )
    return response_text(data), data.get("usage") or {}, data


def invoke_model(
    model: str,
    instructions: str,
    prompt: str,
    args: argparse.Namespace,
) -> tuple[str, dict[str, Any]]:
    text, usage, _ = invoke_model_envelope(
        model,
        instructions,
        prompt,
        args,
    )
    return text, usage


def safe_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)


def generate_one(
    task: dict[str, Any],
    model: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    started = time.monotonic()
    task_id = str(task["id"])
    path = str((task.get("locality") or {}).get("path") or "")
    model_root = args.out_dir / safe_component(model)
    raw_root = model_root / "raw" / safe_component(task_id)
    patch_root = model_root / "patches"
    raw_root.mkdir(parents=True, exist_ok=True)
    patch_root.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "id": task_id,
        "repo": task.get("repo"),
        "path": path,
        "start_sha": task.get("start_sha"),
        "model": model,
        "provider": MODEL_SPECS.get(
            model,
            {"provider": "chat"},
        )["provider"],
        "status": "failed",
        "apply_ok": False,
        "attempts": [],
    }
    try:
        before = source_at_start(task, args.repo_cache)
        prompt, context = build_prompt(task, before, args.max_context_chars)
        result.update(context)
        result["source_sha256"] = hashlib.sha256(before.encode("utf-8")).hexdigest()
    except Exception as exc:
        result["error"] = f"source: {exc}"[:500]
        result["duration_seconds"] = round(time.monotonic() - started, 3)
        return result

    for attempt in range(1, args.max_attempts + 1):
        raw_path = raw_root / f"attempt-{attempt}.txt"
        attempt_result: dict[str, Any] = {"attempt": attempt, "raw_file": str(raw_path)}
        try:
            text, usage = invoke_model(model, SOLVER_INSTRUCTIONS, prompt, args)
            raw_path.write_text(text, encoding="utf-8")
            after, note = apply_search_replace(before, text)
            attempt_result.update(
                {
                    "response_chars": len(text),
                    "usage": usage,
                    "apply_note": note,
                }
            )
            if after is None:
                result["attempts"].append(attempt_result)
                continue
            patch = unified_patch(path, before, after)
            patch_path = patch_root / f"{task_id}.patch"
            patch_path.write_text(patch, encoding="utf-8")
            attempt_result["apply_ok"] = True
            result["attempts"].append(attempt_result)
            result.update(
                {
                    "status": "generated",
                    "apply_ok": True,
                    "successful_attempt": attempt,
                    "patch_file": str(patch_path),
                    "changed_lines": changed_line_count(patch),
                    "response_chars": len(text),
                }
            )
            break
        except Exception as exc:
            attempt_result["error"] = str(exc)[:500]
            result["attempts"].append(attempt_result)

    if not result["apply_ok"]:
        result["error"] = result["attempts"][-1].get("error") or result["attempts"][-1].get(
            "apply_note", "generation failed"
        )
    result["duration_seconds"] = round(time.monotonic() - started, 3)
    return result


def read_results(path: Path) -> dict[str, dict[str, Any]]:
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


def write_results(
    path: Path,
    task_order: dict[str, int],
    results: dict[str, dict[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(results.values(), key=lambda item: task_order[item["id"]])
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in ordered),
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    args = parse_args()
    tasks = load_tasks(args.tasks, set(args.ids) if args.ids else None, args.limit)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    jobs = [(task, model) for model in args.models for task in tasks]
    result_paths = {
        model: args.out_dir / safe_component(model) / "generation_results.jsonl"
        for model in args.models
    }
    task_order = {str(task["id"]): index for index, task in enumerate(tasks)}
    all_results = {
        model: read_results(path) if args.resume else {}
        for model, path in result_paths.items()
    }
    jobs = [
        (task, model)
        for task, model in jobs
        if all_results[model].get(str(task["id"]), {}).get("apply_ok") is not True
    ]

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(generate_one, task, model, args): (task, model)
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
                    "status": "failed",
                    "apply_ok": False,
                    "error": str(exc)[:500],
                }
            all_results[model][str(task["id"])] = result
            write_results(result_paths[model], task_order, all_results[model])
            print(
                f"[{model}] {task['id']}: {result.get('status')} "
                f"attempts={len(result.get('attempts') or [])} "
                f"lines={result.get('changed_lines')}",
                flush=True,
            )

    summary = {
        model: {
            "tasks": len(results),
            "generated": sum(item.get("apply_ok") is True for item in results.values()),
            "failed": sum(item.get("apply_ok") is not True for item in results.values()),
        }
        for model, results in all_results.items()
    }
    (args.out_dir / "generation_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if all(item["failed"] == 0 for item in summary.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
