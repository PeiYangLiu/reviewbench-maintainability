#!/usr/bin/env python3
"""Annotate ReviewBench CSV rows with a local Copilot OpenAI-compatible API.

Each annotation must also include ``case_summary``: a 1-3 sentence Chinese
summary of what the PR/case is doing (the reviewer's maintainability concern,
where the before-code has the problem, and how the PR fixed it). This is a
required deliverable for every annotation task so reviewers can understand a
case at a glance; it is recorded verbatim and not used for keep/reject gating.
"""

from __future__ import annotations

import argparse
import csv
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import json
from pathlib import Path
import random
import re
import threading
import time
from typing import Any
import urllib.error
import urllib.request


LABEL_COLUMNS = (
    "label_is_maintainability_comment",
    "label_category_correct",
    "label_best_category",
    "label_task_is_clear",
    "label_task_localized_to_comment",
    "label_comment_was_addressed",
    "label_no_answer_leakage_after_sanitization",
    "label_auto_static_gate_valid",
    "label_correctness_oracle_usable",
    "label_keep_for_benchmark",
    "label_quality_score_1_to_5",
    "annotator_id",
    "case_summary",
    "notes",
)

# Free-text columns are recorded verbatim and are not validated against an
# allowed value set or used to decide whether a row is already labeled.
FREE_TEXT_COLUMNS = {"case_summary", "notes"}

CATEGORIES = {
    "complexity",
    "duplication",
    "dead_code",
    "modularity",
    "readability",
    "naming",
    "testability",
    "api_design",
    "other",
}

ALLOWED = {
    "label_is_maintainability_comment": {"yes", "no", "unclear"},
    "label_category_correct": {"yes", "no", "partial", "unclear"},
    "label_best_category": CATEGORIES,
    "label_task_is_clear": {"yes", "no", "unclear"},
    "label_task_localized_to_comment": {"yes", "no", "unclear"},
    "label_comment_was_addressed": {"yes", "no", "unclear"},
    "label_no_answer_leakage_after_sanitization": {"yes", "no", "unclear"},
    "label_auto_static_gate_valid": {"yes", "partial", "no", "not_applicable"},
    "label_correctness_oracle_usable": {"yes", "partial", "no", "unclear"},
    "label_keep_for_benchmark": {"yes", "maybe", "no"},
    "label_quality_score_1_to_5": {"1", "2", "3", "4", "5"},
}

SYSTEM_PROMPT = """You are an expert human annotator for ReviewBench maintainability benchmark curation.

Use only the provided metadata and diff hunk. Do not browse. If evidence is insufficient, use "unclear" or "partial"; do not invent facts.

Goal: decide whether a review comment can form a reliable maintainability benchmark task: human review signal, clear/local task, no near-complete answer leakage, addressed evidence, and useful correctness oracle from PR test/build/lint changes.

Allowed values:
- label_is_maintainability_comment: yes/no/unclear. Maintainability includes complexity, duplication, dead code, modularity, readability, naming, testability, API design. Exclude pure functionality requests, docs-only discussion, CI logistics, broad design debates, and pure style preferences.
- label_category_correct: yes/no/partial/unclear.
- label_best_category: complexity/duplication/dead_code/modularity/readability/naming/testability/api_design/other.
- label_task_is_clear: yes/no/unclear. "yes" means an implementer would know the requested local change.
- label_task_localized_to_comment: yes/no/unclear. "yes" means the path/line/diff hunk anchor the task well enough.
- label_comment_was_addressed: yes/no/unclear. Resolved thread plus file/region touched is strong evidence, unless the text contradicts it.
- label_no_answer_leakage_after_sanitization: yes/no/unclear. Mark "no" for suggestion blocks or near-complete replacement code. A natural-language instruction such as "remove unused X" is normally not leakage by itself.
- label_auto_static_gate_valid: yes/partial/no/not_applicable. For complexity/duplication/dead_code auto_static, decide whether static maintainability evidence plausibly matches the comment. Use not_applicable for other gate types/categories.
- label_correctness_oracle_usable: yes/partial/no/unclear. "yes" means changed tests/build/lint/typecheck files look relevant and usable; "partial" means plausible but broad/indirect; "no" means absent or unrelated.
- label_keep_for_benchmark: yes/maybe/no. Use yes only when strict keep is satisfied: maintainability yes, clear yes, localized yes, addressed yes, no leakage yes, quality >=4, oracle yes/partial, and valid auto_static when applicable.
- label_quality_score_1_to_5: 1-5. 4-5 means strong benchmark candidate; 3 means maybe; 1-2 means reject.
- case_summary: 用 1-3 句中文，平实地概述「这个 case 在干啥」，方便后续人快速理解。要点：reviewer 指出的可维护性问题是什么、改前代码哪里有这个问题（尽量点到函数名/行号/变量）、PR 实际是怎么改的。只依据给定信息，不要编造，不要照抄整段代码。

Return exactly one JSON object with these keys:
label_is_maintainability_comment, label_category_correct, label_best_category, label_task_is_clear, label_task_localized_to_comment, label_comment_was_addressed, label_no_answer_leakage_after_sanitization, label_auto_static_gate_valid, label_correctness_oracle_usable, label_keep_for_benchmark, label_quality_score_1_to_5, case_summary, notes.

case_summary is a 1-3 sentence Chinese summary of what this PR/case is doing (concern -> before -> fix). The notes must be one concise English sentence explaining the decisive reason. Do not include markdown."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-csv", type=Path, required=True)
    parser.add_argument("--source-jsonl", type=Path, required=True)
    parser.add_argument("--out-csv", type=Path, required=True)
    parser.add_argument("--checkpoint-jsonl", type=Path, required=True)
    parser.add_argument("--endpoint", default="http://127.0.0.1:4143/v1/responses")
    parser.add_argument("--model", default="claude-opus-4.7")
    parser.add_argument("--workers", type=int, default=30)
    parser.add_argument("--temperature", type=float, default=0)
    parser.add_argument("--max-retries", type=int, default=4)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--annotator-prefix", default="opus47")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rows, fieldnames = load_csv(args.input_csv)
    source_by_id = load_source(args.source_jsonl)
    completed = load_completed(args.checkpoint_jsonl)

    pending = []
    for index, row in enumerate(rows):
        case_id = row["case_id"]
        if already_labeled(row):
            continue
        if case_id in completed:
            continue
        pending.append((index, row))
    if args.limit is not None:
        pending = pending[: args.limit]

    args.checkpoint_jsonl.parent.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    start = time.time()
    done_count = 0
    error_count = 0

    def run_one(index: int, row: dict[str, str]) -> dict[str, Any]:
        source = source_by_id.get(row["case_id"], {})
        payload = {
            "model": args.model,
            "stream": False,
            "temperature": args.temperature,
            "instructions": SYSTEM_PROMPT,
            "input": build_user_prompt(row, source),
        }
        last_error = ""
        for attempt in range(args.max_retries + 1):
            try:
                response = post_json(args.endpoint, payload)
                text = extract_text(response)
                annotation = parse_annotation(text, row)
                annotation["annotator_id"] = f"{args.annotator_prefix}_worker_{index % args.workers:02d}"
                return {"case_id": row["case_id"], "annotation": annotation, "raw_text": text}
            except Exception as exc:  # noqa: BLE001 - keep runner resilient and resumable.
                last_error = str(exc)
                if attempt >= args.max_retries:
                    break
                sleep_s = min(45.0, 2.0 * (2**attempt)) + random.random()
                time.sleep(sleep_s)
        fallback = fallback_annotation(row, last_error)
        fallback["annotator_id"] = f"{args.annotator_prefix}_fallback"
        return {"case_id": row["case_id"], "annotation": fallback, "error": last_error}

    with ThreadPoolExecutor(max_workers=args.workers) as executor, args.checkpoint_jsonl.open(
        "a", encoding="utf-8"
    ) as checkpoint:
        future_to_item = {executor.submit(run_one, index, row): (index, row) for index, row in pending}
        while future_to_item:
            done, _ = wait(future_to_item, return_when=FIRST_COMPLETED)
            for future in done:
                index, row = future_to_item.pop(future)
                result = future.result()
                with lock:
                    checkpoint.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
                    checkpoint.flush()
                done_count += 1
                if result.get("error"):
                    error_count += 1
                if done_count == 1 or done_count % 25 == 0 or done_count == len(pending):
                    elapsed = max(time.time() - start, 1.0)
                    rate = done_count / elapsed
                    remaining = len(pending) - done_count
                    eta = remaining / rate if rate > 0 else 0
                    print(
                        f"progress {done_count}/{len(pending)} "
                        f"errors={error_count} rate={rate:.2f}/s eta={eta/60:.1f}m "
                        f"last={row['case_id']}",
                        flush=True,
                    )

    all_completed = load_completed(args.checkpoint_jsonl)
    merge_annotations(rows, all_completed)
    # Ensure every annotation column (e.g. a newly added case_summary) is present
    # in the output header even if the input template predates it.
    output_fieldnames = list(fieldnames) + [c for c in LABEL_COLUMNS if c not in fieldnames]
    write_csv(args.out_csv, output_fieldnames, rows)
    print(f"wrote {args.out_csv} with {len(all_completed)} checkpoint annotations")
    return 0


def load_csv(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = [normalize_case_id_name(name) for name in (reader.fieldnames or [])]
        rows = []
        for raw in reader:
            row = {normalize_case_id_name(key): value for key, value in raw.items() if key is not None}
            rows.append(row)
    return rows, fieldnames


def normalize_case_id_name(name: str) -> str:
    return name.lstrip("\ufeff") if name else name


def load_source(path: Path) -> dict[str, dict[str, Any]]:
    by_id = {}
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            item = json.loads(line)
            case_id = item.get("id")
            if case_id:
                by_id[case_id] = item
    return by_id


def load_completed(path: Path) -> dict[str, dict[str, str]]:
    completed = {}
    if not path.exists():
        return completed
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("error"):
                continue
            case_id = item.get("case_id")
            annotation = item.get("annotation")
            if case_id and isinstance(annotation, dict):
                completed[case_id] = {column: str(annotation.get(column, "")).strip() for column in LABEL_COLUMNS}
    return completed


def already_labeled(row: dict[str, str]) -> bool:
    return all((row.get(column) or "").strip() for column in LABEL_COLUMNS if column not in FREE_TEXT_COLUMNS)


def build_user_prompt(row: dict[str, str], source: dict[str, Any]) -> str:
    locality = source.get("locality") or {}
    evidence = source.get("addressed_evidence") or {}
    oracle = source.get("correctness_oracle_proxy") or {}
    prefilter = source.get("_prefilter") or {}
    comment = source.get("comment") or {}

    context = {
        "case_id": row.get("case_id", ""),
        "repo": row.get("repo", ""),
        "pr_number": row.get("pr_number", ""),
        "pr_url": row.get("pr_url", ""),
        "comment_url": row.get("comment_url", "") or comment.get("url", ""),
        "comment_author": row.get("comment_author", "") or comment.get("author", ""),
        "comment_created_at": row.get("comment_created_at", "") or comment.get("created_at", ""),
        "category_pred": row.get("category_pred", "") or source.get("category", ""),
        "gate_type": row.get("gate_type", "") or source.get("gate_type", ""),
        "language": row.get("language", "") or source.get("inferred_language", ""),
        "priority_score": row.get("priority_score", "") or prefilter.get("priority_score", ""),
        "oracle_strength": row.get("oracle_strength", "") or oracle.get("oracle_strength", ""),
        "changed_file_count": row.get("changed_file_count", "") or oracle.get("changed_file_count", ""),
        "test_change_count": row.get("test_change_count", ""),
        "path": row.get("path", "") or locality.get("path", ""),
        "line": row.get("line", "") or locality.get("line", ""),
        "start_line": row.get("start_line", "") or locality.get("start_line", ""),
        "task_instruction": row.get("task_instruction", "") or source.get("task_instruction", ""),
        "diff_hunk": trim_text(str(locality.get("diff_hunk", "")), 2400),
        "matched_terms": split_cell(row.get("matched_terms", "")) or source.get("matched_terms", []),
        "contains_suggestion": row.get("contains_suggestion", "") or source.get("contains_suggestion", ""),
        "thread_resolved": row.get("thread_resolved", "") or evidence.get("thread_resolved", ""),
        "file_touched": row.get("file_touched", "") or evidence.get("file_touched_after_comment", ""),
        "region_touched": row.get("region_touched", "") or evidence.get("region_touched_after_comment", ""),
        "prefilter_bucket": row.get("prefilter_bucket", "") or prefilter.get("bucket", ""),
        "prefilter_reasons": split_cell(row.get("prefilter_reasons", "")) or prefilter.get("reasons", []),
        "prefilter_reject_reasons": split_cell(row.get("prefilter_reject_reasons", ""))
        or prefilter.get("reject_reasons", []),
        "test_change_paths": limit_list(
            split_cell(row.get("test_change_paths", "")) or oracle.get("test_change_paths", []), 35
        ),
        "source_change_paths_sample": limit_list(
            split_cell(row.get("source_change_paths_sample", "")) or oracle.get("source_change_paths", []), 20
        ),
        "changed_files_sample": limit_list(
            split_cell(row.get("changed_files_sample", "")) or source.get("changed_files", []), 35
        ),
    }
    return (
        "Annotate this single ReviewBench candidate. Return only the JSON object described in the system instructions.\n\n"
        + json.dumps(context, ensure_ascii=False, indent=2, sort_keys=True)
    )


def split_cell(value: str) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in str(value).split("|") if part.strip()]


def limit_list(items: list[Any], limit: int) -> list[str]:
    return [str(item) for item in items[:limit]]


def trim_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]"


def post_json(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=data,
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body[:1000]}") from exc


def extract_text(response: dict[str, Any]) -> str:
    output = response.get("output") or []
    parts = []
    for item in output:
        for content in item.get("content") or []:
            text = content.get("text")
            if text:
                parts.append(text)
    if not parts and "output_text" in response:
        parts.append(str(response["output_text"]))
    if not parts:
        raise ValueError(f"response had no text: {response}")
    return "\n".join(parts).strip()


def parse_annotation(text: str, row: dict[str, str]) -> dict[str, str]:
    data = parse_json_object(text)
    annotation = {}
    category_pred = (row.get("category_pred") or "other").strip()
    for column in LABEL_COLUMNS:
        if column == "annotator_id":
            continue
        if column == "notes":
            annotation[column] = clean_note(str(data.get(column, "")))
            continue
        if column == "case_summary":
            annotation[column] = clean_summary(str(data.get(column, "")))
            continue
        value = normalize_label_value(column, data.get(column, ""))
        if column == "label_quality_score_1_to_5":
            value = value[:1]
        if column == "label_best_category" and not value:
            value = category_pred if category_pred in CATEGORIES else "other"
        if column == "label_auto_static_gate_valid" and not value:
            value = "not_applicable"
        if value not in ALLOWED[column]:
            raise ValueError(f"invalid {column}={value!r} in {text[:500]!r}")
        annotation[column] = value
    enforce_consistency(annotation, row)
    return annotation


def normalize_label_value(column: str, value: Any) -> str:
    text = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    synonyms = {
        "true": "yes",
        "false": "no",
        "y": "yes",
        "n": "no",
        "unknown": "unclear",
        "not_sure": "unclear",
        "n/a": "not_applicable",
        "na": "not_applicable",
        "not applicable": "not_applicable",
        "testing": "testability",
        "tests": "testability",
        "unused_code": "dead_code",
        "unreachable_code": "dead_code",
        "code_duplication": "duplication",
        "duplicate": "duplication",
        "api": "api_design",
        "design": "api_design",
        "maintainability": "other",
        "style": "readability",
    }
    text = synonyms.get(text, text)
    if column in {
        "label_is_maintainability_comment",
        "label_task_is_clear",
        "label_task_localized_to_comment",
        "label_comment_was_addressed",
        "label_no_answer_leakage_after_sanitization",
    } and text == "partial":
        return "unclear"
    if column == "label_keep_for_benchmark" and text == "unclear":
        return "maybe"
    return text


def parse_json_object(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"no JSON object in response: {text[:500]!r}")
    data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError(f"JSON response is not an object: {text[:500]!r}")
    return data


def clean_note(note: str) -> str:
    note = " ".join(note.replace("\n", " ").split())
    if len(note) > 360:
        note = note[:357].rstrip() + "..."
    return note


def clean_summary(summary: str) -> str:
    summary = " ".join(summary.replace("\n", " ").split())
    if len(summary) > 600:
        summary = summary[:597].rstrip() + "..."
    return summary


def enforce_consistency(annotation: dict[str, str], row: dict[str, str]) -> None:
    category = annotation.get("label_best_category") or row.get("category_pred") or "other"
    if category not in CATEGORIES:
        annotation["label_best_category"] = "other"
    gate_type = (row.get("gate_type") or "").strip()
    pred = (row.get("category_pred") or "").strip()
    best = annotation.get("label_best_category", "")
    auto_static = gate_type == "auto_static" or pred in {"complexity", "duplication", "dead_code"} or best in {
        "complexity",
        "duplication",
        "dead_code",
    }
    if not auto_static:
        annotation["label_auto_static_gate_valid"] = "not_applicable"

    strict_ok = (
        annotation.get("label_is_maintainability_comment") == "yes"
        and annotation.get("label_task_is_clear") == "yes"
        and annotation.get("label_task_localized_to_comment") == "yes"
        and annotation.get("label_comment_was_addressed") == "yes"
        and annotation.get("label_no_answer_leakage_after_sanitization") == "yes"
        and annotation.get("label_correctness_oracle_usable") in {"yes", "partial"}
        and annotation.get("label_auto_static_gate_valid") in {"yes", "partial", "not_applicable"}
        and int(annotation.get("label_quality_score_1_to_5") or "0") >= 4
    )
    if annotation.get("label_keep_for_benchmark") == "yes" and not strict_ok:
        annotation["label_keep_for_benchmark"] = "maybe"
    if annotation.get("label_is_maintainability_comment") == "no" or annotation.get(
        "label_no_answer_leakage_after_sanitization"
    ) == "no":
        annotation["label_keep_for_benchmark"] = "no"
        if int(annotation.get("label_quality_score_1_to_5") or "5") > 2:
            annotation["label_quality_score_1_to_5"] = "2"


def fallback_annotation(row: dict[str, str], error: str) -> dict[str, str]:
    category = row.get("category_pred") if row.get("category_pred") in CATEGORIES else "other"
    is_auto_static = row.get("gate_type") == "auto_static" or category in {"complexity", "duplication", "dead_code"}
    return {
        "label_is_maintainability_comment": "unclear",
        "label_category_correct": "unclear",
        "label_best_category": category,
        "label_task_is_clear": "unclear",
        "label_task_localized_to_comment": "unclear",
        "label_comment_was_addressed": "unclear",
        "label_no_answer_leakage_after_sanitization": "unclear",
        "label_auto_static_gate_valid": "partial" if is_auto_static else "not_applicable",
        "label_correctness_oracle_usable": "unclear",
        "label_keep_for_benchmark": "maybe",
        "label_quality_score_1_to_5": "3",
        "case_summary": "",
        "notes": clean_note(f"Fallback after API/parse failure; requires manual review. Error: {error}"),
    }


def merge_annotations(rows: list[dict[str, str]], annotations: dict[str, dict[str, str]]) -> None:
    for row in rows:
        annotation = annotations.get(row["case_id"])
        if not annotation:
            continue
        for column in LABEL_COLUMNS:
            row[column] = annotation.get(column, row.get(column, ""))


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    raise SystemExit(main())
