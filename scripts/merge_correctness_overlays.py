#!/usr/bin/env python3
"""Merge correctness reruns while enforcing canonical patch identity."""

from __future__ import annotations

import argparse
from collections import Counter
import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any


LANES = ("gopy", "scripted", "tsjs", "csharp", "jvm", "rust", "native")
GENERATED_FINAL_STATUSES = {"pass", "fail", "scope_error"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ids-file", type=Path, required=True)
    parser.add_argument("--expected-models-file", type=Path, required=True)
    parser.add_argument(
        "--generation",
        action="append",
        required=True,
        metavar="MODEL=GENERATION_RESULTS_JSONL",
    )
    parser.add_argument("--base-dir", action="append", type=Path, required=True)
    parser.add_argument("--overlay-dir", action="append", type=Path, default=[])
    parser.add_argument("--out-dir", type=Path, required=True)
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def pair_set_sha256(pairs: set[tuple[str, str]]) -> str:
    return hashlib.sha256(
        "".join(f"{task_id}\0{model}\n" for task_id, model in sorted(pairs)).encode()
    ).hexdigest()


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def write_json(path: Path, value: Any) -> None:
    atomic_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    atomic_text(
        path,
        "".join(
            json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
            for row in rows
        ),
    )


def parse_generation_paths(values: list[str]) -> dict[str, Path]:
    result = {}
    for value in values:
        model, separator, raw_path = value.partition("=")
        if not separator or not model or not raw_path:
            raise ValueError(f"invalid --generation value: {value!r}")
        if model in result:
            raise ValueError(f"duplicate generation model: {model}")
        result[model] = Path(raw_path)
    return result


def load_generations(
    paths: dict[str, Path],
    ids: set[str],
) -> tuple[
    dict[tuple[str, str], dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    records = {}
    manifest = {}
    for model, path in paths.items():
        by_id = {}
        for row in load_jsonl(path):
            task_id = str(row.get("id") or "")
            if task_id not in ids:
                continue
            if row.get("model") != model:
                raise ValueError(
                    f"{path}: {task_id} model {row.get('model')!r} != {model!r}"
                )
            if task_id in by_id:
                raise ValueError(f"{path}: duplicate generation ID {task_id}")
            by_id[task_id] = row
            records[(task_id, model)] = row
        missing = ids - set(by_id)
        if missing:
            raise ValueError(
                f"{model} generation missing IDs: {sorted(missing)[:5]}"
            )
        manifest[model] = {
            "path": str(path.resolve()),
            "sha256": sha256(path),
            "records": len(by_id),
            "generated": sum(
                row.get("apply_ok") is True for row in by_id.values()
            ),
        }
    return records, manifest


def load_directory(
    directory: Path,
    expected_ids: set[str],
    expected_models: set[str],
) -> tuple[
    dict[tuple[str, str], dict[str, Any]],
    dict[tuple[str, str], str],
    dict[str, Any],
]:
    records = {}
    sources = {}
    files = {}
    filtered = 0
    for path in sorted(directory.glob("*.jsonl")):
        files[path.name] = sha256(path)
        for row in load_jsonl(path):
            task_id = str(row.get("id") or "")
            model = str(row.get("model") or "")
            if task_id not in expected_ids or model not in expected_models:
                filtered += 1
                continue
            key = (task_id, model)
            if key in records:
                raise ValueError(f"{directory}: duplicate correctness pair {key}")
            records[key] = row
            sources[key] = str(path.resolve())
    return records, sources, {
        "path": str(directory.resolve()),
        "files": files,
        "records": len(records),
        "filtered_records": filtered,
    }


def validate_pair(
    key: tuple[str, str],
    generation: dict[str, Any],
    correctness: dict[str, Any],
) -> None:
    status = correctness.get("status")
    if generation.get("apply_ok") is True:
        generation_sha = generation.get("patch_sha256")
        if not generation_sha:
            raise ValueError(f"{key}: generated candidate has no patch SHA-256")
        if correctness.get("patch_sha256") != generation_sha:
            raise ValueError(f"{key}: correctness patch does not match generation")
        if status not in GENERATED_FINAL_STATUSES:
            raise ValueError(
                f"{key}: generated candidate has non-final correctness status "
                f"{status!r}"
            )
    elif status != "candidate_missing":
        raise ValueError(
            f"{key}: non-generated candidate has correctness status {status!r}"
        )


def merge(
    *,
    ids: list[str],
    expected_models: list[str],
    generation_paths: dict[str, Path],
    base_directories: list[Path],
    overlay_directories: list[Path],
    output: Path,
) -> dict[str, Any]:
    if len(ids) != len(set(ids)):
        raise ValueError("IDs file contains duplicates")
    if len(expected_models) != len(set(expected_models)):
        raise ValueError("expected models file contains duplicates")
    if set(generation_paths) != set(expected_models):
        raise ValueError(
            "generation model set does not match expected models: "
            f"expected={sorted(expected_models)} "
            f"actual={sorted(generation_paths)}"
        )
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)

    id_set = set(ids)
    model_set = set(expected_models)
    id_order = {task_id: index for index, task_id in enumerate(ids)}
    generations, generation_manifest = load_generations(
        generation_paths,
        id_set,
    )
    expected_pairs = {
        (task_id, model) for task_id in id_set for model in model_set
    }

    correctness = {}
    provenance = {}
    base_manifest = []
    for directory in base_directories:
        rows, sources, details = load_directory(
            directory,
            id_set,
            model_set,
        )
        duplicates = set(correctness) & set(rows)
        if duplicates:
            raise ValueError(
                f"{directory}: duplicate base pairs: {sorted(duplicates)[:5]}"
            )
        correctness.update(rows)
        provenance.update(
            {
                key: {"source": sources[key], "kind": "base", "precedence": 0}
                for key in rows
            }
        )
        base_manifest.append(details)
    if set(correctness) != expected_pairs:
        missing = expected_pairs - set(correctness)
        extra = set(correctness) - expected_pairs
        raise ValueError(
            "base correctness pair set mismatch: "
            f"missing={sorted(missing)[:5]} extra={sorted(extra)[:5]}"
        )

    overlay_manifest = []
    for precedence, directory in enumerate(overlay_directories, start=1):
        rows, sources, details = load_directory(
            directory,
            id_set,
            model_set,
        )
        unknown = set(rows) - expected_pairs
        if unknown:
            raise ValueError(
                f"{directory}: unknown overlay pairs: {sorted(unknown)[:5]}"
            )
        for key, row in rows.items():
            validate_pair(key, generations[key], row)
            correctness[key] = row
            provenance[key] = {
                "source": sources[key],
                "kind": "overlay",
                "precedence": precedence,
            }
        details["applied_records"] = len(rows)
        details["precedence"] = precedence
        overlay_manifest.append(details)

    for key in sorted(expected_pairs):
        validate_pair(key, generations[key], correctness[key])

    by_lane = {lane: [] for lane in LANES}
    status_counts = Counter()
    for key, row in correctness.items():
        lane = row.get("lane")
        if lane not in by_lane:
            raise ValueError(f"{key}: unknown correctness lane {lane!r}")
        by_lane[lane].append(row)
        status_counts[str(row.get("status"))] += 1

    lane_manifest = {}
    for lane, rows in by_lane.items():
        rows.sort(key=lambda row: (id_order[row["id"]], row["model"]))
        path = output / f"{lane}.jsonl"
        write_jsonl(path, rows)
        lane_manifest[lane] = {
            "records": len(rows),
            "sha256": sha256(path),
            "status_counts": dict(
                sorted(Counter(row["status"] for row in rows).items())
            ),
        }

    provenance_rows = [
        {
            "id": task_id,
            "model": model,
            **provenance[(task_id, model)],
        }
        for task_id, model in sorted(expected_pairs)
    ]
    provenance_path = output / "provenance" / "records.jsonl"
    write_jsonl(provenance_path, provenance_rows)

    manifest = {
        "schema_version": "reviewbench-correctness-overlay/v1",
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "tasks": len(id_set),
        "models": expected_models,
        "records": len(correctness),
        "pair_set_sha256": pair_set_sha256(set(correctness)),
        "ids_sha256": hashlib.sha256(
            "".join(f"{task_id}\n" for task_id in ids).encode()
        ).hexdigest(),
        "generations": generation_manifest,
        "base_inputs": base_manifest,
        "overlay_inputs": overlay_manifest,
        "lanes": lane_manifest,
        "status_counts": dict(sorted(status_counts.items())),
        "provenance_sha256": sha256(provenance_path),
        "canonical_patch_mismatches": 0,
        "infra_records": 0,
    }
    write_json(output / "manifest.json", manifest)
    manifest["manifest_sha256"] = sha256(output / "manifest.json")
    return manifest


def main() -> int:
    args = parse_args()
    ids = [
        line.strip()
        for line in args.ids_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    expected_models = [
        line.strip()
        for line in args.expected_models_file.read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip()
    ]
    result = merge(
        ids=ids,
        expected_models=expected_models,
        generation_paths=parse_generation_paths(args.generation),
        base_directories=args.base_dir,
        overlay_directories=args.overlay_dir,
        output=args.out_dir,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
