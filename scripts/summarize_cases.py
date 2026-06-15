"""Generate a grounded Chinese summary ("这个 case 在干啥") for each case.

The summary is produced by an LLM (TRAPI / Azure OpenAI) from the evidence we
already collected: the reviewer comment, the code before the change, and the
focused reference fix. It is written back as ``summary_zh`` so the renderer can
show a plain-language explanation at the top of every case.

Auth reuses the TRAPI bearer-token pattern (AzureCliCredential). The model is
controlled by ``TRAPI_MODEL`` (default ``gpt-5.5_2026-04-24``). Calls retry on
rate limits, and cases already summarized are skipped unless ``--force``.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any
import urllib.error
import urllib.request


INSTRUCTIONS = """你是资深代码审查助手。下面是一个从真实 GitHub PR 挖掘出来的「可维护性」benchmark 任务。
请只依据给定信息，用中文写 2-4 句话，总结「这个 case 在干啥」，让人快速看懂。需要讲清楚：
1) reviewer 指出了什么可维护性问题（结合类别）；
2) 改前代码具体哪里有这个问题（尽量点到函数名/行号/变量）；
3) PR 实际是怎么改的（参考 diff）。
要求：平实、具体、就事论事；不要编造给定信息里没有的内容；不要长篇大论；不要原样照抄整段代码；输出纯文本，不要 markdown 标题。"""


def build_client():
    from azure.identity import (
        AzureCliCredential,
        ChainedTokenCredential,
        ManagedIdentityCredential,
        get_bearer_token_provider,
    )
    from openai import AzureOpenAI

    scope = os.environ.get("TRAPI_SCOPE", "api://trapi/.default")
    apipath = os.environ.get("TRAPI_APIPATH", "gcr/shared")
    api_version = os.environ.get("TRAPI_API_VERSION", "2025-04-01-preview")
    endpoint = os.environ.get("TRAPI_ENDPOINT", f"https://trapi.research.microsoft.com/{apipath}")
    credential = get_bearer_token_provider(
        ChainedTokenCredential(AzureCliCredential(), ManagedIdentityCredential()), scope
    )
    return AzureOpenAI(azure_endpoint=endpoint, azure_ad_token_provider=credential, api_version=api_version)


def truncate(text: str | None, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "\n…(截断)"


def build_prompt(item: dict[str, Any]) -> str:
    task = item.get("task", {})
    rc = item.get("original_review_comment", {})
    ev = item.get("evidence", {}) or {}
    bw = ev.get("before_window") or {}
    fix = ev.get("fix_patch") or {}

    before_lines = "\n".join(f"{n}: {text}" for n, text in bw.get("lines", []))
    fix_src = {
        "start..target(blob)": "start→target 的真实改动（两点 diff）",
        "comment..head": "评论之后的增量改动",
        "comment..head(compare)": "评论之后的增量改动",
        "base..head": "PR 对该文件的整体改动（评论后该文件未再单独改动）",
        "base..head(compare)": "PR 对该文件的整体改动（评论后该文件未再单独改动）",
    }.get(fix.get("source"), "PR 改动")

    return (
        f"类别: {task.get('category')}\n"
        f"文件:行: {(task.get('locality') or {}).get('path')}:{(task.get('locality') or {}).get('line')}\n"
        f"reviewer 评论:\n{truncate(rc.get('body'), 1200)}\n\n"
        f"改前代码（start_sha 附近，行号: 内容）:\n{truncate(before_lines, 2600)}\n\n"
        f"参考修法 diff（{fix_src}）:\n{truncate(fix.get('text'), 2600)}\n"
    )


def summarize_trapi(client, model: str, prompt: str, retries: int = 6) -> str:
    from openai import RateLimitError

    delay = 2.0
    for attempt in range(retries):
        try:
            response = client.responses.create(
                model=model,
                input=[
                    {"role": "developer", "content": [{"type": "input_text", "text": INSTRUCTIONS}]},
                    {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
                ],
            )
            return (getattr(response, "output_text", None) or "").strip()
        except RateLimitError:
            if attempt == retries - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 30)
    return ""


def summarize_responses(endpoint: str, model: str, prompt: str, retries: int = 6) -> str:
    """Summarize via an OpenAI-compatible /v1/responses HTTP endpoint (e.g. local Copilot)."""

    payload = json.dumps(
        {"model": model, "stream": False, "instructions": INSTRUCTIONS, "input": prompt}
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
        except urllib.error.HTTPError as exc:
            last_err = f"HTTP {exc.code}"
            if exc.code in (429, 500, 502, 503) and attempt < retries - 1:
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            raise
        except urllib.error.URLError as exc:
            last_err = str(exc)
            if attempt < retries - 1:
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            raise
    raise RuntimeError(last_err or "responses endpoint failed")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="input", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--backend",
        choices=("trapi", "responses"),
        default="trapi",
        help="trapi = Azure OpenAI (TRAPI); responses = OpenAI-compatible /v1/responses HTTP endpoint (e.g. local Copilot).",
    )
    parser.add_argument("--endpoint", default="http://127.0.0.1:4143/v1/responses", help="Used when --backend responses.")
    parser.add_argument("--model", help="Model name. Defaults per backend.")
    parser.add_argument("--force", action="store_true", help="Re-summarize even if summary_zh exists.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    items = json.loads(args.input.read_text(encoding="utf-8"))

    if args.backend == "responses":
        model = args.model or "claude-opus-4.7"
        run = lambda prompt: summarize_responses(args.endpoint, model, prompt)  # noqa: E731
    else:
        model = args.model or os.environ.get("TRAPI_MODEL", "gpt-5.5_2026-04-24")
        client = build_client()
        run = lambda prompt: summarize_trapi(client, model, prompt)  # noqa: E731

    def checkpoint() -> None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.out.with_suffix(args.out.suffix + ".tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(args.out)

    for i, item in enumerate(items, 1):
        repo = item.get("task", {}).get("repo")
        if item.get("summary_zh") and not args.force:
            print(f"[{i}/{len(items)}] skip {repo} (already summarized)", flush=True)
            continue
        print(f"[{i}/{len(items)}] summarizing {repo} via {args.backend}:{model} ...", flush=True)
        try:
            item["summary_zh"] = run(build_prompt(item))
            item.pop("summary_error", None)
        except Exception as exc:  # noqa: BLE001
            item["summary_zh"] = ""
            item["summary_error"] = str(exc)[:200]
            print(f"    ! error: {exc}", flush=True)
        # Checkpoint after every case so a timeout/kill never loses progress
        # and --resume can continue from here.
        checkpoint()
    print(f"Wrote summaries to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
