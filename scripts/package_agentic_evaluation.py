#!/usr/bin/env python3
"""Package a completed agentic evaluation into a portable release directory."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any

import summarize_release_evaluation as summarizer


GENERATION_FIELDS = (
    "id",
    "repo",
    "path",
    "start_sha",
    "model",
    "protocol",
    "status",
    "apply_ok",
    "changed_lines",
    "successful_round",
    "finish_mode",
    "resumed_from_round",
    "duration_seconds",
    "task_fingerprint",
    "patch_sha256",
    "error",
)


def parse_model_paths(values: list[str]) -> dict[str, Path]:
    return summarizer.parse_model_paths(values)


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )


def parse_replacements(values: list[str]) -> dict[str, str]:
    replacements = {}
    for value in values:
        label, separator, path = value.partition("=")
        if not separator or not label or not path:
            raise ValueError(f"invalid --sanitize-root value: {value!r}")
        source = path.rstrip("/")
        replacement = f"${{{label}}}"
        replacements[source] = replacement
        if source.startswith("/"):
            replacements[source[1:]] = replacement
    return dict(
        sorted(
            replacements.items(),
            key=lambda item: len(item[0]),
            reverse=True,
        )
    )


def sanitize(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        for source, replacement in replacements.items():
            value = value.replace(source, replacement)
        return value
    if isinstance(value, list):
        return [sanitize(item, replacements) for item in value]
    if isinstance(value, dict):
        return {
            key: sanitize(item, replacements)
            for key, item in value.items()
        }
    return value


def copy_generation(
    model_paths: dict[str, Path],
    out: Path,
    task_order: dict[str, int],
    replacements: dict[str, str],
) -> list[dict[str, Any]]:
    records = []
    for model, source_path in model_paths.items():
        for item in summarizer.load_jsonl(source_path):
            if item["id"] not in task_order:
                continue
            rounds = list(item.get("rounds") or [])
            record = {
                key: item[key]
                for key in GENERATION_FIELDS
                if key in item
            }
            response_models = [
                str(raw["model"])
                for round_record in rounds
                if isinstance(
                    raw := round_record.get("raw_api_response"),
                    dict,
                )
                and raw.get("model")
            ]
            record["round_count"] = len(rounds)
            record["response_rounds_with_model"] = len(response_models)
            record["response_models"] = sorted(set(response_models))
            record["response_model_attested"] = (
                bool(rounds) and len(response_models) == len(rounds)
            )
            patch_source = Path(str(item.get("patch_file") or ""))
            if patch_source.is_file():
                relative = Path("patches") / model / f"{item['id']}.patch"
                destination = out / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(patch_source, destination)
                record["patch_file"] = relative.as_posix()
            trace_source = Path(str(item.get("trace_file") or ""))
            if trace_source.is_file():
                relative = Path("traces") / model / f"{item['id']}.json"
                destination = out / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                trace = json.loads(trace_source.read_text(encoding="utf-8"))
                destination.write_text(
                    json.dumps(
                        sanitize(trace, replacements),
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                record["trace_file"] = relative.as_posix()
            records.append(sanitize(record, replacements))
    model_order = {model: index for index, model in enumerate(model_paths)}
    return sorted(
        records,
        key=lambda item: (task_order[item["id"]], model_order[item["model"]]),
    )


def normalize_correctness(
    directory: Path,
    task_order: dict[str, int],
    model_order: dict[str, int],
) -> list[dict[str, Any]]:
    normalized = summarizer.load_correctness(directory)
    records = [
        {
            "id": task_id,
            "model": model,
            "schema_version": value.get("schema_version"),
            "lane": value.get("lane"),
            "source_kind": value.get("source_kind"),
            "status": value.get("status"),
            "patch_sha256": value.get("patch_sha256"),
            "patch_apply": value.get("patch_apply") is True,
            "correctness_ok": value.get("correctness_ok") is True,
            "oracle_replayed": value.get("oracle_replayed") is True,
            "scope_ok": value.get("scope_ok"),
            "retryable": value.get("retryable") is True,
            "pass_to_pass_total": value.get("pass_to_pass_total"),
            "pass_to_pass_passed": value.get("pass_to_pass_passed"),
            "failed_tests": list(value.get("failed_tests") or []),
            "command": value.get("command"),
            "error": value.get("error"),
        }
        for (task_id, model), value in normalized.items()
    ]
    return sorted(
        records,
        key=lambda item: (task_order[item["id"]], model_order[item["model"]]),
    )


def copy_judgments(
    source: Path,
    out: Path,
    task_order: dict[str, int],
    model_order: dict[str, int],
) -> list[dict[str, Any]]:
    records = []
    for item in summarizer.load_jsonl(source):
        if (
            item["id"] not in task_order
            or item["solver_model"] not in model_order
        ):
            continue
        record = dict(item)
        record.pop("patch_file", None)
        raw_source = Path(str(record.pop("raw_file", "") or ""))
        if raw_source.is_file():
            model = str(record["solver_model"])
            relative = Path("judge_raw") / model / f"{record['id']}.txt"
            destination = out / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(raw_source, destination)
            record["raw_file"] = relative.as_posix()
        records.append(record)
    return sorted(
        records,
        key=lambda item: (
            task_order[item["id"]],
            model_order[item["solver_model"]],
        ),
    )


def sha256sums(root: Path) -> str:
    lines = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.name == "SHA256SUMS":
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.relative_to(root).as_posix()}")
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--generation", action="append", required=True)
    parser.add_argument("--correctness-dir", type=Path, required=True)
    parser.add_argument("--judgments", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--run-config", type=Path)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--evaluation-id", required=True)
    parser.add_argument(
        "--sanitize-root",
        action="append",
        default=[],
        metavar="LABEL=PATH",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.out_dir.exists() and any(args.out_dir.iterdir()):
        raise ValueError(f"output directory is not empty: {args.out_dir}")
    args.out_dir.mkdir(parents=True, exist_ok=True)

    tasks = summarizer.load_jsonl(args.tasks)
    task_order = {item["id"]: index for index, item in enumerate(tasks)}
    model_paths = parse_model_paths(args.generation)
    model_order = {model: index for index, model in enumerate(model_paths)}
    expected = len(tasks) * len(model_paths)
    replacements = parse_replacements(args.sanitize_root)

    generation = copy_generation(
        model_paths,
        args.out_dir,
        task_order,
        replacements,
    )
    correctness = sanitize(
        normalize_correctness(
            args.correctness_dir,
            task_order,
            model_order,
        ),
        replacements,
    )
    judgments = sanitize(
        copy_judgments(
            args.judgments,
            args.out_dir,
            task_order,
            model_order,
        ),
        replacements,
    )
    results = sanitize(summarizer.load_jsonl(args.results), replacements)
    report = json.loads(args.report.read_text(encoding="utf-8"))
    run_config = (
        json.loads(args.run_config.read_text(encoding="utf-8"))
        if args.run_config
        else {}
    )
    counts = {
        "generation": len(generation),
        "correctness": len(correctness),
        "judgments": len(judgments),
        "results": len(results),
    }
    if any(value != expected for value in counts.values()) or not report.get("complete"):
        raise ValueError(
            f"incomplete evaluation: expected={expected} counts={counts} "
            f"report_complete={report.get('complete')}"
        )

    write_jsonl(args.out_dir / "generation.jsonl", generation)
    write_jsonl(args.out_dir / "correctness.jsonl", correctness)
    write_jsonl(args.out_dir / "judgments.jsonl", judgments)
    write_jsonl(args.out_dir / "results.jsonl", results)
    shutil.copy2(args.report, args.out_dir / "report.json")
    manifest = {
        "schema_version": "reviewbench-agentic-evaluation/v1",
        "evaluation_id": args.evaluation_id,
        "benchmark": {
            "tasks": len(tasks),
            "hard": sum(summarizer.difficulty(task) == "hard" for task in tasks),
            "medium": sum(summarizer.difficulty(task) == "medium" for task in tasks),
        },
        "models": list(model_paths),
        "generation_protocols": report.get("generation_protocols") or [],
        "policy": run_config.get("policy") or {},
        "records": {
            **counts,
            "patches": sum(item.get("apply_ok") is True for item in generation),
            "traces": sum("trace_file" in item for item in generation),
            "model_identity_attested": sum(
                item.get("response_model_attested") is True
                for item in generation
            ),
        },
        "leaderboard": report.get("leaderboard") or {},
        "complete": True,
    }
    (args.out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (args.out_dir / "SHA256SUMS").write_text(
        sha256sums(args.out_dir),
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
