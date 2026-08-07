#!/usr/bin/env python3
"""Run resumable strict quality-improvement judgments over an evidence pool."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import threading
from typing import Any

import generate_release_patches as api
from reviewbench_maintainability.quality_gate import (
    QUALITY_GATE_VERSION,
    QUALITY_JUDGE_INSTRUCTIONS,
    build_prompt,
    evidence_from_record,
    evidence_is_complete,
    parse_verdict,
    verdict_passes,
)


SUPPORTED_MODELS = {"gpt-5.6-sol", "claude-opus-4.8"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="input", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--model", choices=sorted(SUPPORTED_MODELS), required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--max-output-tokens", type=int, default=2048)
    parser.add_argument("--resume", action="store_true")
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
        help=(
            "Send every model through the OpenAI-compatible chat endpoint. "
            "Use when the gateway exposes /v1/chat/completions but not the "
            "Copilot /token route the responses API needs."
        ),
    )
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def load_completed(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    return {
        item["id"]: item
        for item in load_jsonl(path)
        if isinstance(item.get("verdict"), dict) and not item.get("error")
    }


def invoke(
    model: str,
    prompt: str,
    args: argparse.Namespace,
) -> tuple[str, dict[str, Any]]:
    if model == "claude-opus-4.8" or args.force_chat:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": QUALITY_JUDGE_INSTRUCTIONS},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": args.max_output_tokens,
            "stream": False,
        }
        data = api.post_json(args.chat_endpoint, payload, {})
        actual_model = str(data.get("model") or "")
        if actual_model and actual_model != model:
            raise RuntimeError(
                f"judge model mismatch: requested {model}, gateway returned "
                f"{actual_model}; refusing a silent fallback"
            )
        return api.chat_text(data), data.get("usage") or {}

    payload = {
        "model": model,
        "stream": False,
        "instructions": QUALITY_JUDGE_INSTRUCTIONS,
        "input": prompt,
        "max_output_tokens": args.max_output_tokens,
        "reasoning": {"effort": "medium"},
    }
    for auth_attempt in range(2):
        token = api.copilot_token(args.token_endpoint, refresh=auth_attempt > 0)
        headers = {
            "Authorization": f"Bearer {token}",
            "copilot-integration-id": "vscode-chat",
            "editor-version": "vscode/1.100.0",
            "editor-plugin-version": "copilot-chat/0.26.7",
            "x-github-api-version": "2025-04-01",
        }
        try:
            data = api.post_json(args.responses_endpoint, payload, headers)
            return api.response_text(data), data.get("usage") or {}
        except RuntimeError as exc:
            if auth_attempt == 0 and (
                "HTTP 401:" in str(exc) or "HTTP 403:" in str(exc)
            ):
                continue
            raise
    raise RuntimeError("quality judge authentication failed")


def judge_one(
    record: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    task_id = str(record.get("id") or "")
    result = {
        "id": task_id,
        "repo": record.get("repo"),
        "model": args.model,
        "schema_version": QUALITY_GATE_VERSION,
        "verdict": None,
        "strict_pass": False,
        "error": None,
    }
    if not evidence_is_complete(record):
        result["error"] = "incomplete quality evidence"
        return result
    try:
        before, patch, full_patch = evidence_from_record(record)
        prompt = build_prompt(record, before, patch, full_patch) + (
            "\n\nSTRICT JSON REMINDER: confidence must be a JSON number from 0 to 1 "
            "(for example 0.85), never a quoted string or a word."
        )
        text, usage = invoke(args.model, prompt, args)
        verdict = parse_verdict(text)
        result["verdict"] = verdict.to_dict()
        result["strict_pass"] = verdict_passes(verdict)
        result["usage"] = usage
    except Exception as exc:
        result["error"] = str(exc)[:500]
    return result


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
    records = load_jsonl(args.input)
    order = {record["id"]: index for index, record in enumerate(records)}
    results = load_completed(args.out) if args.resume else {}
    pending = [record for record in records if record["id"] not in results]
    lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(judge_one, record, args): record
            for record in pending
        }
        for future in as_completed(futures):
            record = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {
                    "id": record["id"],
                    "repo": record.get("repo"),
                    "model": args.model,
                    "schema_version": QUALITY_GATE_VERSION,
                    "verdict": None,
                    "strict_pass": False,
                    "error": str(exc)[:500],
                }
            with lock:
                results[result["id"]] = result
                write_results(args.out, order, results)
            print(
                f"[{len(results)}/{len(records)}] {result['id']}: "
                f"{'pass' if result.get('strict_pass') else 'reject'}"
                f"{' error=' + result['error'] if result.get('error') else ''}",
                flush=True,
            )

    errors = sum(bool(item.get("error")) for item in results.values())
    passed = sum(item.get("strict_pass") is True for item in results.values())
    print(
        json.dumps(
            {
                "model": args.model,
                "records": len(results),
                "strict_pass": passed,
                "errors": errors,
            },
            sort_keys=True,
        )
    )
    return 0 if errors == 0 and len(results) == len(records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
