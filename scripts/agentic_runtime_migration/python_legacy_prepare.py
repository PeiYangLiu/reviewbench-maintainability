"""Bounded nine-task Python closure, using the unmodified full402 verifier.

Run as a module with the explicitly frozen engine on PYTHONPATH. This independent
Python controller does not bypass the shared migration CLI's legacy-input gate:
it records the missing original inputs, and emits recipes only for the newly
implemented, evidence-complete QDK route. No shared module is patched.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import datetime
import json
from pathlib import Path

from reviewbench_maintainability.agentic.assets import AssetPool
from reviewbench_maintainability.agentic.cli import load_config
from reviewbench_maintainability.agentic.contracts import (
    ContractError, atomic_json, canonical_hash, component, file_hash,
    lane_for, read_jsonl, selected_tasks, write_jsonl,
)
from reviewbench_maintainability.agentic.execution import ExecutionServices
from reviewbench_maintainability.agentic.models import engine_identity
from reviewbench_maintainability.agentic.preparation import prepare, verify_recipe
from reviewbench_maintainability.agentic.resources import CURRENT_JOB, GiB, ResourceRequest
from reviewbench_maintainability.agentic.scheduling import output_lock
from .common import MigrationContext
from .python_automatic_prepare import container_record, recorded
from .python_candidate import external_site, normalized_name
from .python_lane import PYTHON_IMAGE, migrate
from .python_legacy import (
    COHORT, DEPENDENCY_SCHEMA, LEGACY_INPUTS, MIND, QDK, QDK_DIRECTORY,
    QDK_REQUIRED, legacy_preflight, mindscape_boundary, qdk_contract,
)

ENGINE = "5142d1fec94eb6c5a6177a66ee7ce8533a26f0a2578280075a6947a399c01ca3"


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def prepare_qdk(ctx, task, contract, directory):
    """One source-free snapshot from the existing venv, no pip/network restore."""
    directory.mkdir(parents=True, exist_ok=False)
    inventory, names = external_site(ctx.legacy / QDK_DIRECTORY / "venv",
                                     owned_distributions=("deq",), owned_packages=("deq",))
    atomic_json(directory / "dependency-inventory.json", inventory)
    observed = {normalized_name(d["name"]): d["version"] for d in inventory["distributions"]}
    if any(observed.get(k) != v for k, v in QDK_REQUIRED.items()):
        raise ContractError("QDK exact published dependency versions differ")
    if inventory["input_bytes"] * 3 + GiB > 8 * GiB:
        raise ContractError("QDK dependency snapshot exceeds the admitted 8 GiB preparation bound")
    if not inventory["python_version"].startswith("3.11."):
        raise ContractError("QDK retained dependency Python ABI differs")
    image = json.loads(recorded(["docker", "image", "inspect", PYTHON_IMAGE],
                                directory, "image-inspect"))[0]["Id"]
    version = container_record(image, ["python3", "-I", "-B", "-c",
                                      "import platform; print(platform.python_version())"],
                               directory, "interpreter").strip()
    if not version.startswith("3.11."):
        raise ContractError("QDK pinned developer image Python ABI differs")
    # This is a new image audit, not relabelled historical interpreter evidence.
    scan = ("find / -xdev \\( -name .git -o -name '*source-only-golden.patch' "
            "-o -name '*golden*.patch' -o -name oracles.jsonl -o -name generation_results.jsonl \\) -print")
    scanned = container_record(image, ["/bin/bash", "-c", scan], directory, "image-scan",
                               audit_rootfs=True)
    if scanned.strip():
        raise ContractError("QDK developer toolchain image contains repository/reference assets")
    history = recorded(["docker", "history", "--no-trunc", "--format", "{{json .}}", image],
                       directory, "image-history")
    audit = directory / "image-audit.json"
    atomic_json(audit, {"schema_version": "reviewbench-developer-image-audit/v1",
                       "image": image, "expected_image_id": image, "python_version": version,
                       "no_reference_assets": True, "no_repository_history": True,
                       "rootfs_scan_command": scan, "rootfs_scan_output": scanned,
                       "history": [json.loads(line) for line in history.splitlines()]})
    site_asset = ctx.bundle(Path(inventory["site"]), names, role="dependency")
    descriptor = json.loads(Path(site_asset["path"]).read_text())
    # Probe the actual filtered archive, not the unfiltered retained site.
    job = CURRENT_JOB.get()
    pool = AssetPool(ctx.cache, ctx.scratch, job["lease"], ctx.output)
    try:
        site = pool.materialize(site_asset)
        probe = ("import importlib,importlib.util,sys; sys.path.insert(0,'/dependency-site'); "
                 "[importlib.import_module(n) for n in "
                 "['grpc_tools.protoc','google.protobuf','stim','binar','paulimer','numpy','lark','mako']]; "
                 "assert 'deq' not in sys.modules; "
                 "assert importlib.util.find_spec('deq') is None; "
                 "print('FILTERED_EXTERNAL_DEPENDENCIES: PASS')")
        container_record(image, ["python3", "-I", "-B", "-c", probe], directory, "dependency-imports",
                         mounts=[(site, "/dependency-site")], timeout=300)
    finally:
        pool.close()
    value = {"schema_version": DEPENDENCY_SCHEMA, "status": "prepared",
             "task_id": task["id"], "start_sha": task["start_sha"], "contract_sha256": canonical_hash(contract),
             "image": image, "image_audit": ctx.copy_asset(audit, "image-audit"),
             "python_version": version, "retained_python_version": inventory["python_version"],
             "interpreter_binding": "same CPython 3.11 ABI; new immutable image microversion, not historical replay",
             "site_asset": site_asset, "site_tree_sha256": descriptor["tree_sha256"],
             "owned_packages": ["deq"], "owned_distributions": ["deq"],
             "inventory": {"path": str(directory / "dependency-inventory.json"),
                           "sha256": file_hash(directory / "dependency-inventory.json")},
             "runtime_verified": False}
    atomic_json(directory / "preparation.json", value)
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--legacy-root", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--migration-ids", type=Path, required=True)
    parser.add_argument("--prior-mindscape-package", type=Path, required=True)
    parser.add_argument("--dependency-root", type=Path)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--workers", type=int, choices=[1], default=1)
    args = parser.parse_args(argv)
    if args.prepare and args.dependency_root:
        parser.error("prepare and reuse-dependency-root are mutually exclusive")
    config = load_config(args.config)
    identity = engine_identity()["fingerprint"]
    if identity != ENGINE or config.get("migration", {}).get("verification_engine_fingerprint") != ENGINE:
        raise ContractError("Python closure must use the attested frozen engine")
    tasks = selected_tasks(Path(config["tasks"]), Path(config["scope_ids"]))
    selected = args.migration_ids.read_text().splitlines()
    if len(tasks) != 402 or len(selected) != len(COHORT) or set(selected) != set(COHORT):
        raise ContractError("Python closure must retain the complete 402 scope and nine-task cohort")
    if not set(selected) <= tasks.keys():
        raise ContractError("Python closure has foreign task IDs")
    oracles = read_jsonl(Path(config["oracles"]))
    out = args.out_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if (out / "migration-summary.json").exists():
        raise ContractError("Python closure output already exists; use a new version directory")
    dependencies = args.dependency_root.resolve() if args.dependency_root else out / "dependencies"
    config = {**config, "asset_root": str(out), "runtime_recipes": str(out / "runtime-recipes.jsonl"),
              "out_dir": str(out / "execution"),
              "migration": {**config["migration"], "python_legacy_dependency_root": str(dependencies)}}
    import yaml
    (out / "experiment.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    services = ExecutionServices(config)
    ctx = MigrationContext(config, tasks, oracles, args.legacy_root.resolve(), out)
    summary = {"created_at": now(), "scope_size": 402, "selected_ids": selected,
               "engine_fingerprint": identity, "workers": 1, "models_called": 0,
               "tasks_fingerprint": canonical_hash(tasks), "config_sha256": file_hash(args.config),
               "migration_ids_sha256": file_hash(args.migration_ids),
               "recipes": {}, "tasks": {}, "registered": False}
    recipes = {}
    with output_lock(out):
        for ident in selected:
            task, oracle = tasks[ident], oracles[ident]
            try:
                if ident in LEGACY_INPUTS:
                    row = legacy_preflight(task, oracle, ctx.legacy)
                elif ident == MIND:
                    row = mindscape_boundary(ctx, task, oracle, args.prior_mindscape_package.resolve())
                else:
                    contract = qdk_contract(ctx, task, oracle)
                    atomic_json(out / "qdk-contract.json", contract)
                    if args.prepare:
                        request = ResourceRequest("python-legacy-dependencies:" + ident, cpus=2,
                                                  memory_bytes=4 * GiB, scratch_bytes=8 * GiB)
                        with services.gate.acquire(request, uses_containers=True, timeout=3600):
                            prepare_qdk(ctx, task, contract, dependencies / "qdk-ec105")
                    if args.prepare or args.dependency_root:
                        with services.gate.acquire(ResourceRequest("python-legacy-migrate:" + ident,
                                                                  cpus=1, memory_bytes=512 * 1024**2,
                                                                  scratch_bytes=GiB)):
                            recipe = migrate(ctx, task, oracle)
                        recipes[ident] = recipe
                        summary["recipes"][ident] = recipe.fingerprint
                        row = {"task_id": ident, "status": "configured", "runtime_verified": False}
                    else:
                        row = {"task_id": ident, "status": "input-bound", "runtime_verified": False}
                summary["tasks"][ident] = row
            except Exception as exc:
                summary["tasks"][ident] = {"task_id": ident, "status": "blocked",
                                           "error": f"{type(exc).__name__}: {exc}", "runtime_verified": False}
            print(json.dumps(summary["tasks"][ident]), flush=True)
            atomic_json(out / "cohort-status.json", summary)
        # The denominator is never a filtered copy of the release. Recipe
        # readiness below is issued only by the original frozen verifier.
        write_jsonl(out / "runtime-recipes.jsonl", [asdict(r) for r in recipes.values()])
        inventory = [
            {"id": i, "repo": t["repo"], "lane": lane_for(t, oracles[i]),
             "status": summary["tasks"].get(i, {}).get("status", "pending"),
             "reason": summary["tasks"].get(i, {}).get("cause", "outside selected Python batch")
                       if i not in recipes else None}
            for i, t in tasks.items()
        ]
        write_jsonl(out / "migration-inventory.jsonl", inventory)
        report = prepare(tasks, oracles, recipes, Path(config["repo_cache"]), out, out / "execution/preparation")
        if args.verify:
            for ident, recipe in recipes.items():
                task, oracle = tasks[ident], oracles[ident]
                directory = out / "execution/preparation/verified" / component(ident)
                try:
                    with services.job(task, recipe, ctx.repo(task), "verify"):
                        proof = verify_recipe(task, recipe, oracle, ctx.repo(task), out, directory)
                except Exception as exc:
                    proof = {"id": ident, "status": "blocked", "error": f"{type(exc).__name__}: {exc}",
                             "task_fingerprint": canonical_hash(task), "oracle_fingerprint": canonical_hash(oracle),
                             "runtime_fingerprint": recipe.fingerprint, "engine_fingerprint": identity, "runs": []}
                    atomic_json(directory / "verification.json", proof)
                summary["tasks"][ident] = {
                    "task_id": ident, "status": proof["status"], "error": proof.get("error"),
                    "runtime_verified": proof["status"] == "ready", "proof": str(directory / "verification.json"),
                    "successful_replays": sum(bool(r["tests"]) and all(r["tests"].values()) for r in proof["runs"]),
                }
                print(json.dumps(summary["tasks"][ident]), flush=True)
                atomic_json(out / "cohort-status.json", summary)
        report["execution_verified"] = sum(r.get("runtime_verified") is True for r in summary["tasks"].values())
        report["complete"] = report["execution_verified"] == len(tasks)
        summary.update(completed_at=now(), execution_verified=report["execution_verified"],
                       complete=report["complete"], cli_exit=0 if report["complete"] else 2)
        atomic_json(out / "execution/preparation/readiness.json", report)
        atomic_json(out / "resource-usage.json", services.gate.status())
        atomic_json(out / "migration-summary.json", summary)
        atomic_json(out / "cohort-status.json", summary)
    print(json.dumps({"expected_tasks": 402, "selected_tasks": 9, "ready": report["execution_verified"],
                      "cli_exit": summary["cli_exit"]}), flush=True)
    return summary["cli_exit"]


if __name__ == "__main__":
    raise SystemExit(main())
