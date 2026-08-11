#!/usr/bin/env python3
"""Merge targeted generation retries without replacing successful samples."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any

import generate_agentic_patches as agentic


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--oracles", type=Path, required=True)
    parser.add_argument("--repo-cache", type=Path, required=True)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--retry", type=Path, action="append", default=[])
    parser.add_argument("--out-dir", type=Path, required=True)
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def index_unique(
    rows: list[dict[str, Any]],
    *,
    label: str,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        task_id = str(row.get("id") or "")
        if not task_id:
            raise ValueError(f"{label} contains a record without an ID")
        if task_id in result:
            raise ValueError(f"{label} contains duplicate ID {task_id}")
        result[task_id] = row
    return result


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def record_sha256(record: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def resolve_artifact(
    record: dict[str, Any],
    source_results: Path,
    field: str,
    directory: str,
    suffix: str,
) -> Path | None:
    configured = Path(str(record.get(field) or ""))
    if configured.is_file():
        return configured
    candidate = source_results.parent / directory / f"{record['id']}{suffix}"
    return candidate if candidate.is_file() else None


def patch_applies(
    task: dict[str, Any],
    patch: Path,
    repo_cache: Path,
) -> bool:
    before = agentic.read_at_start(
        task,
        repo_cache,
        str((task.get("locality") or {})["path"]),
    )
    with tempfile.TemporaryDirectory(
        prefix="reviewbench-retry-apply-"
    ) as directory:
        root = Path(directory)
        subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "reviewbench@example.invalid"],
            cwd=root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "ReviewBench"],
            cwd=root,
            check=True,
        )
        target = root / str(task["locality"]["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(before, encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(
            ["git", "commit", "--quiet", "-m", "start"],
            cwd=root,
            check=True,
        )
        completed = subprocess.run(
            ["git", "apply", "--check", str(patch.resolve())],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        return completed.returncode == 0


def repair_patch_line_endings(
    task: dict[str, Any],
    record: dict[str, Any],
    repo_cache: Path,
) -> str:
    editable_path = str((task.get("locality") or {})["path"])
    before = agentic.read_at_start(task, repo_cache, editable_path)
    previous = {**record, "apply_ok": False}
    state = agentic.resume_agent_state(
        before,
        editable_path,
        previous,
        max(1, len(record.get("rounds") or [])),
    )
    if state is None:
        raise ValueError(f"{record['id']} patch cannot be replayed from trace")
    current, _, edited, _ = state
    if not edited or current == before:
        raise ValueError(f"{record['id']} trace contains no effective edit")
    patch = agentic.git_diff(editable_path, before, current)
    if not patch.strip():
        raise ValueError(f"{record['id']} repaired patch is empty")
    return patch


def materialize_record(
    record: dict[str, Any],
    task: dict[str, Any],
    source_results: Path,
    output: Path,
    source_kind: str,
    source_index: int,
    repo_cache: Path | None,
) -> dict[str, Any]:
    result = copy.deepcopy(record)
    result["canonical_provenance"] = {
        "source_kind": source_kind,
        "source_index": source_index,
        "source_results_sha256": file_sha256(source_results),
        "source_record_sha256": record_sha256(record),
    }
    patch = resolve_artifact(
        record,
        source_results,
        "patch_file",
        "patches",
        ".patch",
    )
    if record.get("apply_ok") is True and patch is None:
        raise ValueError(f"{record['id']} generated record has no patch")
    if patch is not None:
        destination = output / "patches" / f"{record['id']}.patch"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(patch, destination)
        result["patch_file"] = str(destination.resolve())
        result["patch_sha256"] = file_sha256(destination)
        if (
            record.get("apply_ok") is True
            and repo_cache is not None
            and not patch_applies(task, destination, repo_cache)
        ):
            original_sha256 = result["patch_sha256"]
            destination.write_text(
                repair_patch_line_endings(task, record, repo_cache),
                encoding="utf-8",
            )
            if not patch_applies(task, destination, repo_cache):
                raise ValueError(
                    f"{record['id']} repaired patch still does not apply"
                )
            result["patch_sha256"] = file_sha256(destination)
            result["canonical_provenance"]["patch_repaired"] = (
                "preserved-source-line-endings"
            )
            result["canonical_provenance"][
                "original_patch_sha256"
            ] = original_sha256
    else:
        result["patch_file"] = None

    trace = resolve_artifact(
        record,
        source_results,
        "trace_file",
        "traces",
        ".json",
    )
    if trace is not None:
        destination = output / "traces" / f"{record['id']}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(trace, destination)
        result["trace_file"] = str(destination.resolve())
    elif record.get("trace_file"):
        raise ValueError(f"{record['id']} record references a missing trace")
    return result


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def write_checksums(root: Path) -> None:
    entries = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.name == "SHA256SUMS":
            continue
        entries.append(f"{file_sha256(path)}  {path.relative_to(root)}\n")
    (root / "SHA256SUMS").write_text("".join(entries), encoding="utf-8")


def merge_retries(
    *,
    tasks_path: Path,
    oracles_path: Path,
    base_path: Path,
    retry_paths: list[Path],
    output: Path,
    repo_cache: Path | None = None,
) -> dict[str, Any]:
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)

    tasks = load_jsonl(tasks_path)
    oracles = load_jsonl(oracles_path)
    tasks_by_id = index_unique(tasks, label="tasks")
    oracles_by_id = index_unique(oracles, label="oracles")
    if set(tasks_by_id) != set(oracles_by_id):
        raise ValueError("task and Oracle ID sets differ")

    base = index_unique(load_jsonl(base_path), label="base generation")
    if set(base) != set(tasks_by_id):
        raise ValueError("base generation does not match the task ID set")
    model_values = {str(record.get("model") or "") for record in base.values()}
    if len(model_values) != 1 or not next(iter(model_values)):
        raise ValueError(f"base generation has inconsistent models: {model_values}")
    model = next(iter(model_values))

    selected: dict[str, tuple[dict[str, Any], Path, str, int]] = {
        task_id: (record, base_path, "base", 0)
        for task_id, record in base.items()
    }
    overlay_manifest = []
    touched: set[str] = set()
    for index, retry_path in enumerate(retry_paths, start=1):
        retry = index_unique(
            load_jsonl(retry_path),
            label=f"retry generation {index}",
        )
        unexpected = set(retry) - set(base)
        if unexpected:
            raise ValueError(
                f"retry {index} contains unknown IDs: {sorted(unexpected)[:5]}"
            )
        wrong_models = {
            str(record.get("model") or "")
            for record in retry.values()
            if str(record.get("model") or "") != model
        }
        if wrong_models:
            raise ValueError(
                f"retry {index} model mismatch: {sorted(wrong_models)}"
            )
        for task_id, record in retry.items():
            current = selected[task_id][0]
            if current.get("apply_ok") is True:
                raise ValueError(
                    f"retry {index} attempts to replace successful ID {task_id}"
                )
            selected[task_id] = (record, retry_path, "retry", index)
            touched.add(task_id)
        overlay_manifest.append(
            {
                "index": index,
                "records": len(retry),
                "results_sha256": file_sha256(retry_path),
                "generated": sum(
                    row.get("apply_ok") is True for row in retry.values()
                ),
                "failed": sum(
                    row.get("apply_ok") is not True for row in retry.values()
                ),
            }
        )

    canonical = {
        task_id: materialize_record(
            record,
            tasks_by_id[task_id],
            source_path,
            output,
            source_kind,
            source_index,
            repo_cache,
        )
        for task_id, (record, source_path, source_kind, source_index)
        in selected.items()
    }
    for task_id, record in canonical.items():
        if record.get("task_fingerprint") != agentic.task_fingerprint(
            tasks_by_id[task_id]
        ):
            raise ValueError(f"{task_id} task fingerprint mismatch")

    ordered = [canonical[task["id"]] for task in tasks]
    results_path = output / "generation_results.jsonl"
    write_jsonl(results_path, ordered)
    agentic.write_completion_marker(
        results_path,
        model,
        tasks_by_id,
        canonical,
        file_sha256(tasks_path),
    )

    recovered_ids = [
        task["id"]
        for task in tasks
        if task["id"] in touched
        and base[task["id"]].get("apply_ok") is not True
        and canonical[task["id"]].get("apply_ok") is True
    ]
    recovered_root = output / "recovered"
    recovered_tasks = [tasks_by_id[task_id] for task_id in recovered_ids]
    recovered_oracles = [oracles_by_id[task_id] for task_id in recovered_ids]
    recovered_records = {
        task_id: canonical[task_id] for task_id in recovered_ids
    }
    recovered_tasks_path = recovered_root / "tasks.jsonl"
    recovered_results_path = recovered_root / "generation_results.jsonl"
    write_jsonl(recovered_tasks_path, recovered_tasks)
    write_jsonl(recovered_root / "oracles.jsonl", recovered_oracles)
    write_jsonl(
        recovered_results_path,
        [recovered_records[task_id] for task_id in recovered_ids],
    )
    agentic.write_completion_marker(
        recovered_results_path,
        model,
        {task_id: tasks_by_id[task_id] for task_id in recovered_ids},
        recovered_records,
        file_sha256(recovered_tasks_path),
    )

    evaluation_ids = [
        task["id"]
        for task in tasks
        if task["id"] in recovered_ids
        or (
            canonical[task["id"]]
            .get("canonical_provenance", {})
            .get("patch_repaired")
        )
    ]
    evaluation_root = output / "evaluation_delta"
    evaluation_tasks = [tasks_by_id[task_id] for task_id in evaluation_ids]
    evaluation_oracles = [
        oracles_by_id[task_id] for task_id in evaluation_ids
    ]
    evaluation_records = {
        task_id: canonical[task_id] for task_id in evaluation_ids
    }
    evaluation_tasks_path = evaluation_root / "tasks.jsonl"
    evaluation_results_path = evaluation_root / "generation_results.jsonl"
    write_jsonl(evaluation_tasks_path, evaluation_tasks)
    write_jsonl(evaluation_root / "oracles.jsonl", evaluation_oracles)
    write_jsonl(
        evaluation_results_path,
        [evaluation_records[task_id] for task_id in evaluation_ids],
    )
    agentic.write_completion_marker(
        evaluation_results_path,
        model,
        {task_id: tasks_by_id[task_id] for task_id in evaluation_ids},
        evaluation_records,
        file_sha256(evaluation_tasks_path),
    )

    manifest = {
        "schema_version": "reviewbench-generation-retry-merge/v1",
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "model": model,
        "tasks": len(tasks),
        "base_results_sha256": file_sha256(base_path),
        "retry_overlays": overlay_manifest,
        "touched": len(touched),
        "recovered": len(recovered_ids),
        "evaluation_delta": len(evaluation_ids),
        "generated": sum(
            row.get("apply_ok") is True for row in canonical.values()
        ),
        "failed": sum(
            row.get("apply_ok") is not True for row in canonical.values()
        ),
        "results_sha256": file_sha256(results_path),
        "recovered_task_ids_sha256": hashlib.sha256(
            "".join(f"{task_id}\n" for task_id in recovered_ids).encode()
        ).hexdigest(),
        "evaluation_task_ids_sha256": hashlib.sha256(
            "".join(f"{task_id}\n" for task_id in evaluation_ids).encode()
        ).hexdigest(),
    }
    (output / "merge_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_checksums(output)
    return manifest


def main() -> int:
    args = parse_args()
    result = merge_retries(
        tasks_path=args.tasks,
        oracles_path=args.oracles,
        repo_cache=args.repo_cache,
        base_path=args.base,
        retry_paths=args.retry,
        output=args.out_dir,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
