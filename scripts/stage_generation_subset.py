#!/usr/bin/env python3
"""Stage an exact task subset from completed generation artifacts."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any

import generate_agentic_patches as agentic
import merge_generation_retries as merge


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--oracles", type=Path, required=True)
    parser.add_argument("--repo-cache", type=Path, required=True)
    parser.add_argument("--ids-file", type=Path, required=True)
    parser.add_argument(
        "--generation",
        action="append",
        required=True,
        metavar="MODEL=GENERATION_RESULTS_JSONL",
    )
    parser.add_argument("--out-dir", type=Path, required=True)
    return parser.parse_args()


def parse_generation_args(values: list[str]) -> dict[str, Path]:
    result = {}
    for value in values:
        model, separator, path = value.partition("=")
        if not separator or not model or not path:
            raise ValueError(f"invalid --generation value: {value!r}")
        if model in result:
            raise ValueError(f"duplicate generation model: {model}")
        result[model] = Path(path)
    return result


def stage_subset(
    *,
    tasks_path: Path,
    oracles_path: Path,
    repo_cache: Path,
    ids: list[str],
    generation_paths: dict[str, Path],
    output: Path,
) -> dict[str, Any]:
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"output directory is not empty: {output}")
    if len(ids) != len(set(ids)):
        raise ValueError("subset IDs contain duplicates")
    tasks = merge.load_jsonl(tasks_path)
    oracles = merge.load_jsonl(oracles_path)
    tasks_by_id = merge.index_unique(tasks, label="tasks")
    oracles_by_id = merge.index_unique(oracles, label="oracles")
    unknown = set(ids) - set(tasks_by_id)
    if unknown:
        raise ValueError(f"unknown subset IDs: {sorted(unknown)[:5]}")

    ordered_ids = [task["id"] for task in tasks if task["id"] in set(ids)]
    subset_tasks = [tasks_by_id[task_id] for task_id in ordered_ids]
    subset_oracles = [oracles_by_id[task_id] for task_id in ordered_ids]
    output.mkdir(parents=True, exist_ok=True)
    staged_tasks = output / "tasks.jsonl"
    merge.write_jsonl(staged_tasks, subset_tasks)
    merge.write_jsonl(output / "oracles.jsonl", subset_oracles)

    models = {}
    for model, source_path in generation_paths.items():
        source = merge.index_unique(
            merge.load_jsonl(source_path),
            label=f"{model} generation",
        )
        missing = set(ordered_ids) - set(source)
        if missing:
            raise ValueError(
                f"{model} generation is missing IDs: {sorted(missing)[:5]}"
            )
        model_root = (
            output / "models" / agentic.api.safe_component(model)
        )
        records = {
            task_id: merge.materialize_record(
                source[task_id],
                tasks_by_id[task_id],
                source_path,
                model_root,
                "subset",
                0,
                repo_cache,
            )
            for task_id in ordered_ids
        }
        results_path = model_root / "generation_results.jsonl"
        merge.write_jsonl(
            results_path,
            [records[task_id] for task_id in ordered_ids],
        )
        agentic.write_completion_marker(
            results_path,
            model,
            {task_id: tasks_by_id[task_id] for task_id in ordered_ids},
            records,
            merge.file_sha256(staged_tasks),
        )
        models[model] = {
            "records": len(records),
            "generated": sum(
                record.get("apply_ok") is True for record in records.values()
            ),
            "results": str(results_path.resolve()),
            "results_sha256": merge.file_sha256(results_path),
        }

    manifest = {
        "schema_version": "reviewbench-generation-subset/v1",
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "tasks": len(ordered_ids),
        "task_ids_sha256": hashlib.sha256(
            "".join(f"{task_id}\n" for task_id in ordered_ids).encode()
        ).hexdigest(),
        "models": models,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    merge.write_checksums(output)
    return manifest


def main() -> int:
    args = parse_args()
    ids = [
        line.strip()
        for line in args.ids_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    result = stage_subset(
        tasks_path=args.tasks,
        oracles_path=args.oracles,
        repo_cache=args.repo_cache,
        ids=ids,
        generation_paths=parse_generation_args(args.generation),
        output=args.out_dir,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
