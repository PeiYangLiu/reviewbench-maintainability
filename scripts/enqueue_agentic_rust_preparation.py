#!/usr/bin/env python3
"""Fill the bounded queue from unmeasured original Rust inputs; never run a model/test."""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, os.environ.get("REVIEWBENCH_ENGINE_SRC", str(Path(__file__).resolve().parents[1] / "src")))
from agentic_runtime_migration.rust_lane import load_plan
from reviewbench_maintainability.agentic.cli import load_config
from reviewbench_maintainability.agentic.contracts import (
    ContractError, atomic_json, canonical_hash, file_hash, read_jsonl, selected_tasks,
)
from reviewbench_maintainability.agentic.resources import file_lock
from run_agentic_prepare_queue import validate


def require(value, message):
    if not value:
        raise ContractError("preparation feeder: " + message)


def captured_tasks(jobs):
    """An existing failed/active/succeeded attempt always wins over automatic replay."""
    result = {}
    for job in jobs:
        command = job["command"]
        if not any(Path(arg).name == "capture_agentic_rust_inputs.py" for arg in command):
            continue
        require(command.count("--task-id") == 1, "ambiguous existing capture command")
        at = command.index("--task-id")
        require(at + 1 < len(command), "existing capture has no task identity")
        result.setdefault(command[at + 1], []).append(job["id"])
    return result


def pending_inputs(tasks, oracles, rows, jobs, legacy, *, limit, loader=load_plan):
    """Classify the complete inventory before selecting a bounded batch."""
    require(type(limit) is int and 1 <= limit <= 16, "batch limit must be 1..16")
    require(len(rows) == len(tasks) and {r["id"] for r in rows} == set(tasks),
            "publication must contain exactly one result for every original task")
    existing = captured_tasks(jobs)
    selected, decisions = [], []
    for row in sorted(rows, key=lambda r: r["id"]):
        ident = row["id"]
        item = {"task_id": ident}
        task, oracle = tasks[ident], oracles[ident]
        if row.get("missing_reason") != "generation_not_measured" or row.get("status") != "incomplete":
            item["status"] = "measurement_preserved"
        elif task.get("inferred_language", "").lower() != "rust":
            item["status"] = "other_lane"
        elif ident in existing:
            item.update(status="existing_attempt_preserved", jobs=existing[ident])
        elif "/rust/results.jsonl" not in (oracle.get("lane_results_source") or ""):
            item["status"] = "requires_original_lane_adapter"
        else:
            try:
                original = loader(type("Context", (), {"legacy": legacy})(), oracle)
                require(bool(original.get("command")), "original command is absent")
                if len(selected) < limit:
                    selected.append((task, oracle, original))
                    item.update(status="selected", original_command=original["command"])
                else:
                    item["status"] = "next_batch"
            except (ContractError, OSError, ValueError, KeyError) as error:
                item.update(status="input_diagnosis_required", error=f"{type(error).__name__}: {error}")
        decisions.append(item)
    return selected, decisions


def build_job(task, oracle, original, *, config_path, config, legacy, publication,
              output_root, script, python):
    bindings = {"task_sha256": canonical_hash(task), "oracle_sha256": canonical_hash(oracle),
                "plan_sha256": canonical_hash(original)}
    key = canonical_hash(bindings)[:20]
    output = output_root / ("original-rust-" + key)
    require(not output.exists(), "unowned output already exists; diagnose rather than overwrite")
    inputs = {script, config_path, Path(config["tasks"]), Path(config["oracles"]),
              Path(config["scope_ids"]), legacy / "rust/plans.jsonl"}
    inputs = [{"path": str(p.resolve()), "sha256": file_hash(p)} for p in sorted(inputs)]
    proof = str(output / "capture.json")
    return {
        "id": "capture-pending-rust-" + key + "-v1", "pool": "prepare",
        "command": [python, "-B", str(script), "--config", str(config_path),
                    "--legacy-root", str(legacy), "--task-id", task["id"],
                    "--out-dir", str(output), "--publication", str(publication)],
        "cwd": str(script.parent.parent), "timeout_seconds": 300,
        "admission_timeout_seconds": 3600, "resource_owner": "queue",
        "resources": {"cpus": 1, "memory_bytes": 256 * 1024**2, "scratch_bytes": 256 * 1024**2},
        "inputs": inputs, "outputs": [proof], "needs": [],
        "success_when": [
            {"path": proof, "keys": ["status"], "equals": "original_inputs_captured"},
            *[{"path": proof, "keys": [key], "equals": value} for key, value in bindings.items()],
        ],
    }


def enqueue(config_path, legacy, publication, plan_path, output, *, limit=8, apply=False):
    for path in (config_path, legacy, publication, plan_path, output):
        require(path.is_absolute(), "paths must be absolute")
    require(not output.exists(), "use a fresh audit output; old attempts stay immutable")
    config = load_config(config_path)
    tasks = selected_tasks(Path(config["tasks"]), Path(config["scope_ids"]))
    oracles = read_jsonl(Path(config["oracles"]))
    require(len(tasks) == 402, "retain the original402 denominator")
    script = Path(__file__).resolve().with_name("capture_agentic_rust_inputs.py")
    # The same lock also serializes independent feeder processes. Reread inside it.
    with file_lock(plan_path.parent / "plan-update.lock"):
        before = plan_path.read_bytes()
        plan = json.loads(before)
        validate(plan)
        require(plan["scope"]["expected_tasks"] == len(tasks), "queue scope mismatch")
        scope_inputs = {Path(row["path"]).resolve(): row["sha256"]
                        for row in plan["scope"].get("inputs", [])}
        require(all(Path(config[key]).resolve() in scope_inputs for key in ("tasks", "oracles", "scope_ids")),
                "configuration inputs are not bound by the queue scope")
        for row in plan["scope"].get("inputs", []):
            require(file_hash(Path(row["path"])) == row["sha256"], "queue scope input changed")
        epoch = publication.resolve()
        result_path = epoch / "results.jsonl"
        rows = [json.loads(line) for line in result_path.read_text().splitlines()]
        selected, decisions = pending_inputs(tasks, oracles, rows, plan["jobs"], legacy, limit=limit)
        jobs = [build_job(task, oracle, original, config_path=config_path, config=config,
                         legacy=legacy, publication=publication, output_root=plan_path.parent / "queue-work",
                         script=script, python=sys.executable) for task, oracle, original in selected]
        updated = {**plan, "jobs": [*plan["jobs"], *jobs]}
        validate(updated)
        output.mkdir(parents=True)
        (output / "queue-plan-before.json").write_bytes(before)
        atomic_json(output / "proposed-jobs.json", jobs)
        atomic_json(output / "decisions.json", decisions)
        if apply and jobs:
            require(plan_path.read_bytes() == before, "another writer changed the plan")
            atomic_json(plan_path, updated)
        record = {
            "status": "appended" if apply else "planned", "full_scope": 402,
            "publication": str(epoch), "results_sha256": file_hash(result_path),
            "selected": len(jobs), "appended": len(jobs) if apply else 0,
            "decisions": dict(Counter(row["status"] for row in decisions)),
            "job_ids": [job["id"] for job in jobs],
            "old_job_definitions_unchanged": updated["jobs"][:len(plan["jobs"])] == plan["jobs"],
            "plan_before_sha256": canonical_hash(plan), "plan_after_sha256": canonical_hash(updated if apply else plan),
            "new_model_calls": 0, "new_original_SUT_commands": 0, "runtime_ready": False,
        }
        atomic_json(output / "result.json", record)
    print(f"PREPARATION_BATCH selected={record['selected']} appended={record['appended']} "
          "full_scope=402 prior_attempts_preserved=true new_API_SUT_calls=0 runtime_ready=false")
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--legacy-root", required=True, type=Path)
    parser.add_argument("--publication", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    enqueue(args.config.absolute(), args.legacy_root.absolute(), args.publication.absolute(),
            args.plan.absolute(), args.out_dir.absolute(), limit=args.limit, apply=args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
