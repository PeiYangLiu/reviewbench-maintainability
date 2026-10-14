#!/usr/bin/env python3
"""Capture original pending-task inputs for queued preparation; never run an Oracle/model."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time

sys.path.insert(0, os.environ.get("REVIEWBENCH_ENGINE_SRC", str(Path(__file__).resolve().parents[1] / "src")))
from agentic_runtime_migration.rust_lane import load_plan
from reviewbench_maintainability.agentic.cli import load_config
from reviewbench_maintainability.agentic.contracts import (
    atomic_json, canonical_hash, file_hash, read_jsonl, repo_path, selected_tasks,
)


def capture(config_path, legacy, task_id, output, publication):
    started = time.time()
    config = load_config(config_path)
    tasks = selected_tasks(Path(config["tasks"]), Path(config["scope_ids"]))
    oracles = read_jsonl(Path(config["oracles"]))
    assert len(tasks) == 402 and task_id in tasks
    epoch = publication.resolve()
    rows = [json.loads(line) for line in (epoch / "results.jsonl").read_text().splitlines()]
    row = next(value for value in rows if value["id"] == task_id)
    assert row["missing_reason"] == "generation_not_measured", "completed/started measurements are not input-preparation jobs"
    task, oracle = tasks[task_id], oracles[task_id]
    plan = load_plan(type("Context", (), {"legacy": legacy})(), oracle)
    output.mkdir(parents=True)
    for name, value in (("task", task), ("oracle", oracle), ("plan", plan)):
        atomic_json(output / (name + ".json"), value)
    sources = set()
    for item in [*plan.get("session_files", []), *plan.get("environment_file_replacements", [])]:
        for key, value in item.items():
            if key in {"source", "start_source", "golden_source"} and isinstance(value, str):
                sources.add(repo_path(value))
    patch_name = oracle["artifacts"]["patch"]
    patch = legacy / "rust" / repo_path(patch_name)
    assert file_hash(patch) == oracle["golden_patch_sha256"]
    sources.add(patch_name)
    files = []
    for name in sorted(sources):
        source = legacy / "rust" / name
        assert source.is_file() and not source.is_symlink()
        destination = output / "original-files" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        assert source.read_bytes() == destination.read_bytes()
        files.append({"original": str(source), "path": str(destination),
                      "sha256": file_hash(source), "bytes": source.stat().st_size})
    for variant in ("start", "golden"):
        for item in plan.get("session_files", []):
            content = item.get(variant + "_content", item.get("content"))
            source = item.get(variant + "_source", item.get("source"))
            data = content.encode() if content is not None else (legacy / "rust" / repo_path(source)).read_bytes()
            evidence = [value for value in oracle["session_files"][variant] if value["path"] == item["path"]]
            assert len(evidence) == 1 and hashlib.sha256(data).hexdigest() == evidence[0]["sha256"]
            path = output / "original-session" / variant / repo_path(item["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    record = {
        "status": "original_inputs_captured", "task_id": task_id, "full_scope": 402,
        "task_sha256": canonical_hash(task), "oracle_sha256": canonical_hash(oracle),
        "plan_sha256": canonical_hash(plan), "golden_sha256": oracle["golden_patch_sha256"],
        "original_command": plan["command"], "files": files,
        "runtime_ready": False, "model_calls": 0, "original_SUT_commands": 0,
        "started_at": started, "completed_at": time.time(),
    }
    atomic_json(output / "capture.json", record)
    print("ORIGINAL_INPUTS_CAPTURED " + task_id + " files=" + str(len(files))
          + " full_scope=402 runtime_ready=false new_API_SUT_calls=0", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--legacy-root", required=True, type=Path)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--publication", required=True, type=Path)
    args = parser.parse_args()
    capture(args.config, args.legacy_root, args.task_id, args.out_dir, args.publication)
