#!/usr/bin/env python3
"""Run only the original public check, seal external dependencies, emit a warm recipe."""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

sys.path.insert(0, os.environ.get("REVIEWBENCH_ENGINE_SRC", str(Path(__file__).resolve().parents[1] / "src")))
from agentic_runtime_migration import public_build_cache as seeds
from reviewbench_maintainability.agentic.assets import ASSET_SCHEMA, asset_descriptor
from reviewbench_maintainability.agentic.cli import load_config
from reviewbench_maintainability.agentic.contracts import (
    ContractError, atomic_json, canonical_hash, component, file_hash, selected_tasks, write_jsonl,
)
from reviewbench_maintainability.agentic.environment import ReviewBenchDockerEnvironment
from reviewbench_maintainability.agentic.execution import ExecutionServices
from reviewbench_maintainability.agentic.preparation import load_recipes
from reviewbench_maintainability.agentic.resources import CURRENT_JOB, job_directory
from reviewbench_maintainability.agentic.retention import archive_files
from reviewbench_maintainability.agentic.snapshots import extract_start, initialize_git, tree_hash, tree_files


def source_state(workspace, scratch_paths):
    ignored = {".git", ".reviewbench", *scratch_paths}
    result = {}
    for name, path in tree_files(workspace).items():
        if any(name == item or name.startswith(item + "/") for item in ignored):
            continue
        result[name] = (["symlink", os.readlink(path)] if path.is_symlink() else
                        ["file", file_hash(path), bool(path.stat().st_mode & 0o111)])
    return result


def metadata_from_records(directory, records):
    candidates = []
    grouped = []
    for record in records:
        path = directory / record["stdout_file"]
        for line in path.read_text(errors="replace").splitlines():
            if not line.startswith("ReviewBench ") or ": " not in line:
                continue
            try:
                evidence = json.loads(line.split(": ", 1)[1])
            except ValueError:
                continue
            if (evidence.get("role") != "public" or evidence.get("source_unchanged") is not True
                    or evidence.get("exit_status") != 0):
                continue
            if evidence.get("schema_version") in {"reviewbench-original-multi-manifest-variants/v1",
                                                 "reviewbench-original-root-manifest-variants/v1",
                                                 "reviewbench-original-asymmetric-gear-environments/v1"}:
                grouped.append(variant_metadata(evidence))
                continue
            for probe in evidence.get("probes", []):
                result = probe.get("command_result", {})
                if result.get("returncode") == 0 and probe.get("lock_sha256") == evidence.get(
                        "selected_lock_sha256"):
                    value = json.loads(result["stdout"])
                    if "packages" in value and "workspace_members" in value:
                        candidates.append(value)
    if grouped:
        if len(grouped) != 1 or candidates:
            raise ContractError("public cache has ambiguous public metadata evidence")
        return grouped[0]
    if len(candidates) != 1:
        raise ContractError("public cache needs exactly one successful complete public metadata graph")
    return candidates[0]


def variant_metadata(evidence):
    """Capture dependency outputs for every publicly checked original form.

    A form rejected by a typed error in current production source can still
    have compiled external dependencies. The successful public adapter checks
    this classification before emitting completed evidence; no private test
    or workspace artifact is used to produce the seed.
    """
    if (evidence.get("status") != "completed" or evidence.get("test_results") != {}
            or evidence.get("selection_uses_test_results") is not False
            or evidence.get("new_lock_generation_commands") != 0
            or not evidence.get("source_before")
            or evidence.get("source_before") != evidence.get("source_after")):
        raise ContractError("unbound public manifest-variant evidence")
    groups, forms, compatible = {}, set(), []
    for compilation in evidence.get("public_compiles", []):
        form, namespace = compilation["form"], compilation["target_cache"]
        command = compilation["command_result"]
        if (form in forms or namespace in groups or command.get("limit")
                or command.get("returncode") not in (0, 101)):
            raise ContractError("ambiguous or incomplete public form compilation")
        matches = [p for p in evidence["probes"] if p.get("form") == form
                   and p.get("command_result", {}).get("returncode") == 0]
        if len(matches) != 1:
            raise ContractError("public form needs exactly one complete locked graph")
        probe = matches[0]["command_result"]
        graph = json.loads(probe["stdout"])
        if (probe.get("limit") or probe["cwd"] != command["cwd"]
                or not {"packages", "workspace_members", "resolve"} <= graph.keys()
                or not graph["packages"] or not graph["workspace_members"]):
            raise ContractError("public form graph is incomplete or belongs to another workspace")
        groups[namespace] = graph
        forms.add(form)
        if command["returncode"] == 0:
            compatible.append(form)
    if not compatible or compatible != evidence.get("compatible_forms"):
        raise ContractError("public form compilation/compatibility inventory changed")
    result = {"schema_version": seeds.GRAPH_SCHEMA, "cache_groups": groups}
    seeds.metadata_groups(result)
    return result


def prepare(config_path, package, task_id, output, *, live_run=None, metadata_command=None):
    config = load_config(config_path)
    tasks = selected_tasks(Path(config["tasks"]), Path(config["scope_ids"]) if config.get("scope_ids") else None)
    if task_id not in tasks:
        raise ContractError("cache task is not in the original scope")
    if live_run:
        for pair in (live_run / "pairs").glob("*/" + component(task_id)):
            record = pair / "pair-result.json"
            value = json.loads(record.read_text()) if record.exists() else {}
            if value.get("generation_started") or value.get("generation") or list(pair.glob("episode*/trajectory.json")):
                raise ContractError("a measured/admitted task is not a cache-prewarming experiment")
    recipe = load_recipes(package / "runtime-recipes.jsonl")[task_id]
    if recipe.lane != "rust" or "target" not in recipe.cache_mounts:
        raise ContractError("the current prewarmer requires a Rust target cache")
    seeds.validate_public_image_inputs(recipe)
    if output.exists():
        raise ContractError("cache package output already exists")
    output.mkdir(parents=True)
    services = ExecutionServices(config)
    if services.cache is None:
        raise ContractError("cache publication requires configured immutable object storage")
    task = tasks[task_id]
    repository = Path(config["repo_cache"]) / task["repo"].replace("/", "__")
    binding = seeds.public_binding(recipe)
    record = {"status": "preparing", "task_id": task_id, "public_runtime_binding": binding,
              "original_runtime_fingerprint": recipe.fingerprint, "full_scope": len(tasks),
              "model_calls": 0, "judge_calls": 0, "private_test_calls": 0,
              "started_at": datetime.now().astimezone().isoformat()}
    atomic_json(output / "cache-preparation.json", record)
    try:
        input_bytes = sum(asset_descriptor(a, Path(config["asset_root"]))["archive"]["input_bytes"]
                          for a in recipe.assets if a["visibility"] == "developer" and a.get("kind") == "archive")
        admission_recipe = replace(recipe, build_scratch_mb=recipe.build_scratch_mb
                                   + (2 * input_bytes + 1024**2 - 1) // 1024**2)
        with services.job(task, admission_recipe, repository, "prewarm-public-dependencies"):
            recipe, image_record = seeds.bake_public_inputs(recipe, CURRENT_JOB.get()["asset_pool"], output)
            binding = seeds.public_binding(recipe)
            record.update(public_runtime_binding=binding, immutable_input_image=image_record)
            atomic_json(output / "cache-preparation.json", record)
            with job_directory(prefix="public-cache-prewarm-") as directory:
                workspace = Path(directory) / "workspace"
                extract_start(repository, task["start_sha"], workspace)
                initialize_git(workspace)
                environment = ReviewBenchDockerEnvironment(
                    recipe, workspace, output / "public-commands", Path(config["asset_root"]), grader=False)
                try:
                    for command in recipe.bootstrap:
                        result = environment.execute({"command": command})
                        if result["returncode"] != 0:
                            raise ContractError("original public bootstrap failed during cache preparation")
                    original_source = source_state(workspace, recipe.scratch_paths)
                    for command in recipe.dev_checks:
                        result = environment.execute({"command": command})
                        if result["returncode"] != 0:
                            raise ContractError("original public check failed during cache preparation")
                    if metadata_command:
                        result = environment.execute({"command": metadata_command})
                        if result["returncode"] != 0:
                            raise ContractError("public metadata command failed")
                        command_record = environment.records[-1]
                        metadata = json.loads((output / "public-commands" / command_record["stdout_file"]).read_text())
                    else:
                        metadata = metadata_from_records(output / "public-commands", environment.records)
                    if source_state(workspace, recipe.scratch_paths) != original_source:
                        raise ContractError("public check changed source; its compiled artifacts are not a clean seed")
                    if any(p.get("source") is not None and Path(p["manifest_path"]).is_relative_to("/workspace")
                           for _, graph in seeds.metadata_groups(metadata) for p in graph["packages"]):
                        raise ContractError("workspace-owned dependency sources are not external immutable cache inputs")
                finally:
                    environment.close()
                pool = CURRENT_JOB.get()["asset_pool"]
                source = pool.mutable_cache("target")
                staged = Path(directory) / "seed"
                receipt = {"producer": "clean-start-public-check", "grader_assets_visible": False,
                           "public_check_exit_status": 0, "task_id": task_id, "start_sha": task["start_sha"],
                           "source_sha256_before_and_after": canonical_hash(original_source),
                           "public_commands": [*recipe.bootstrap, *recipe.dev_checks],
                           "command_records": environment.records}
                cache_manifest = seeds.capture(source, metadata, staged, binding, producer_receipt=receipt,
                                               runtime_target=recipe.cache_mounts["target"])
                names = [p.relative_to(staged).as_posix() for p in staged.rglob("*")
                         if p.is_file() or p.is_symlink()]
                archive, receipts = archive_files(staged, names, services.cache, Path(directory) / "compression")
                descriptor = output / "public-cache-asset.json"
                atomic_json(descriptor, {"schema_version": ASSET_SCHEMA, "source_kind": "dependency",
                                         "tree_sha256": tree_hash(staged), "archive": archive, "receipts": receipts})
                seed = {"public_runtime_binding": binding,
                        "asset": {"path": str(descriptor.resolve()), "sha256": file_hash(descriptor)},
                        "files": len(cache_manifest["files"]), "bytes": cache_manifest["bytes"],
                        "excluded_workspace_packages": cache_manifest["excluded_workspace_packages"]}
                importer = output / "cache_seed_entry.py"
                shutil.copy2(Path(seeds.__file__).with_name("cache_seed_entry.py"), importer)
                warmed = seeds.bind(recipe, seed, {"path": str(importer.resolve()), "sha256": file_hash(importer)})
                write_jsonl(output / "runtime-recipes.jsonl", [asdict(warmed)])
                atomic_json(output / "cache-seed.json", seed)
                record.update(status="prepared_needs_original_calibration", exit_status=0,
                              runtime_fingerprint=warmed.fingerprint, seed=seed,
                              completed_at=datetime.now().astimezone().isoformat())
    except Exception as error:
        record.update(status="failed", exit_status=1, error=f"{type(error).__name__}: {error}",
                      completed_at=datetime.now().astimezone().isoformat())
        atomic_json(output / "cache-preparation.json", record)
        raise
    atomic_json(output / "cache-preparation.json", record)
    print("PUBLIC_DEPENDENCY_CACHE_PREPARED " + json.dumps({
        "files": seed["files"], "bytes": seed["bytes"], "full_scope": len(tasks),
        "private_test_calls": 0, "model_calls": 0, "runtime_requires_calibration": True,
    }, sort_keys=True))
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--live-run", type=Path)
    parser.add_argument("--metadata-command")
    args = parser.parse_args()
    prepare(args.config, args.package, args.task_id, args.out_dir,
            live_run=args.live_run, metadata_command=args.metadata_command)


if __name__ == "__main__":
    main()
