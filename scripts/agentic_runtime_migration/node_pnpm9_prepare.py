"""Bounded pnpm9/v3 preparation with immutable Node reuse and no task execution."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.environ.get("REVIEWBENCH_ENGINE_SRC", str(Path(__file__).resolve().parents[2] / "src")))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reviewbench_maintainability.agentic.assets import AssetPool, asset_descriptor
from reviewbench_maintainability.agentic.cli import load_config
from reviewbench_maintainability.agentic.contracts import (
    ContractError, atomic_json, canonical_hash, component, file_hash, read_jsonl, selected_tasks,
)
from reviewbench_maintainability.agentic.execution import ExecutionServices
from reviewbench_maintainability.agentic.models import engine_identity
from reviewbench_maintainability.agentic.resources import GiB, ResourceRequest
from reviewbench_maintainability.agentic.snapshots import extract_tar, start_archive
from agentic_runtime_migration.common import MigrationContext
from agentic_runtime_migration.node_pnpm_prepare import ENGINE, retained_manager, install, sri
from agentic_runtime_migration.tsjs_pnpm_payload import input_plan, check_inputs
from agentic_runtime_migration.tsjs_pnpm9 import validate_case
from agentic_runtime_migration.tsjs_pnpm9_transport import SCHEMA, build_payload


def seed_store_v3(packages, retained, destination):
    """Use only exact lock-SRI v3 indexes; never scan/copy a whole store."""
    retained, destination = Path(retained), Path(destination)
    result = {"packages_considered": len(packages), "indexes_reused": 0, "files_reused": 0,
              "bytes_reused": 0, "side_effects_reused": False, "indexes": []}
    for package, value in sorted(packages.items()):
        _, digest = sri(value["resolution"]["integrity"])
        relative = Path("files") / digest[:2] / (digest[2:] + "-index.json")
        index = retained / relative
        if index.is_symlink() or not index.is_file():
            continue
        record = json.loads(index.read_text())
        if package != str(record.get("name")) + "@" + str(record.get("version")):
            raise ContractError("pnpm9 v3 cache index package identity differs")
        files = []
        for entry in record.get("files", {}).values():
            algorithm, digest = sri(entry["integrity"])
            relative_file = Path("files") / digest[:2] / (
                digest[2:] + ("-exec" if entry["mode"] & 0o111 else ""))
            source = retained / relative_file
            if source.is_symlink() or not source.is_file():
                files = []
                break
            if hashlib.new(algorithm, source.read_bytes()).hexdigest() != digest:
                raise ContractError("pnpm9 retained content differs from its SRI")
            target = destination / relative_file
            if target.exists() and (target.is_symlink() or
                                   hashlib.new(algorithm, target.read_bytes()).hexdigest() != digest):
                raise ContractError("pnpm9 seeded content differs from its SRI")
            files.append((source, target))
        if not files:
            continue
        for source, target in files:
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                result["files_reused"] += 1
                result["bytes_reused"] += source.stat().st_size
        clean = {key: value for key, value in record.items() if key != "sideEffects"}
        target = destination / relative
        atomic_json(target, clean)
        result["indexes"].append({"package": package, "source": str(index),
                                  "source_sha256": file_hash(index), "seed_sha256": file_hash(target)})
        result["indexes_reused"] += 1
    return result


def reused_node(path, digest, version, output):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or file_hash(path) != digest:
        raise ContractError("pnpm9 existing Node preparation receipt changed")
    record = json.loads(path.read_text())
    if record.get("status") != "prepared" or record.get("node_version") != version:
        raise ContractError("pnpm9 existing Node receipt has a different version/status")
    asset = record["node"]
    if asset_descriptor(asset, output)["source_kind"] != "toolchain":
        raise ContractError("pnpm9 Node asset must be the existing toolchain, not a candidate build")
    return asset


def prepare(ctx, services, task, oracle, case, output, manager_root, retained_store,
            node_record, node_record_sha256, *, manager_archive=None, additional_v3_stores=()):
    version, log_names = validate_case(case, oracle)
    if output.exists():
        raise ContractError("pnpm9 requires a new attempt directory; no implicit install retry")
    output.mkdir(parents=True)
    record = {"schema_version": SCHEMA, "id": task["id"], "start_sha": task["start_sha"],
              "case_fingerprint": canonical_hash(case), "node_version": case["node"],
              "pnpm_version": version, "status": "waiting_for_gate", "started_at": time.time(),
              "model_calls": 0, "evaluation_completion": False}
    atomic_json(output / "process.json", record)
    with services.gate.acquire(
        ResourceRequest("pnpm9-prepare:" + task["id"], cpus=2, memory_bytes=2 * GiB,
                        scratch_bytes=12 * GiB),
        uses_containers=True, timeout=ctx.config["execution"].get("admission_timeout_seconds", 14400),
    ) as lease:
        record.update(status="preparing", lease=lease["lease"])
        atomic_json(output / "process.json", record)
        pool = AssetPool(services.cache, services.scratch, lease["lease"], ctx.output)
        try:
            record["node"] = reused_node(node_record, node_record_sha256, case["node"], ctx.output)
            node_root = pool.materialize(record["node"])
            actual = subprocess.check_output([str(node_root / "bin/node"), "--version"],
                                             text=True, timeout=10).strip()
            if actual != "v" + case["node"]:
                raise ContractError("pnpm9 reused immutable Node executable version differs")
            record["node_reuse"] = {"receipt": str(node_record), "receipt_sha256": node_record_sha256,
                                   "observed_version": actual, "new_node_archive": False}
            archive = output / ("pnpm-" + version + ".tgz")
            if manager_archive is not None:
                if manager_archive.is_symlink() or not manager_archive.is_file():
                    raise ContractError("pnpm9 reused manager archive is not a regular file")
                # retained_manager independently checks the historical full SRI
                # and every retained executable byte, also on this reuse path.
                shutil.copyfile(manager_archive, archive)
            manager_proof = retained_manager(manager_root, version, archive)
            atomic_json(output / "manager-provenance.json", manager_proof)
            record["manager"] = ctx.bundle(Path(manager_root), role="toolchain")
            record["manager_provenance"] = {"path": str(output / "manager-provenance.json"),
                                             "sha256": file_hash(output / "manager-provenance.json")}
            # This retained exact-start install is never itself a runtime asset.
            source = output / "installed"
            with start_archive(ctx.repo(task), task["start_sha"]) as stream:
                extract_tar(stream, source)
            plan = input_plan(source)
            record["input_hashes"] = plan["input_hashes"]
            store = output / "store"
            store.mkdir()
            seed = seed_store_v3(plan["packages"], Path(retained_store) / "v3", store / "v3")
            atomic_json(output / "store-reuse.json", seed)
            for n, extra in enumerate(additional_v3_stores):
                extra_seed = seed_store_v3(plan["packages"], extra, store / "v3")
                atomic_json(output / ("additional-store-reuse-" + str(n) + ".json"), extra_seed)
            ctx._pnpm_node_version = case["node"]
            record["install"] = install(ctx, source, store, node_root, Path(manager_root),
                                          output / "install.log", version)
            check_inputs(source, plan["input_hashes"])
            record.update(status="installed_not_exported", retained_install=str(source))
            atomic_json(output / "process.json", record)
            layout, index, parser = build_payload(source, output / "payload", plan)
            atomic_json(output / "external-index.json", index)
            record.update({name: layout[name] for name in
                           ("module_roots", "workspace_links", "external_bytes",
                            "external_index_sha256", "workspace_production_files_exported")})
            record["yaml_parser"] = parser
            record["dependencies"] = ctx.bundle(output / "payload", role="dependency")
            directory = ctx.legacy / "tsjs" / oracle["recovery_artifact"]
            record["original_logs"] = [{"name": name, "sha256": file_hash(directory / name)} for name in log_names]
            record.update(status="prepared", ended_at=time.time())
            atomic_json(output / "dependencies.json", record)
        except Exception as exc:
            record.update(status="blocked", error=f"{type(exc).__name__}: {exc}", ended_at=time.time())
            raise
        finally:
            atomic_json(output / "process.json", record)
            pool.close()
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("config", "legacy-root", "out-dir", "cohort-ids", "manager-root", "retained-store", "node-record"):
        parser.add_argument("--" + flag, type=Path, required=True)
    parser.add_argument("--node-record-sha256", required=True)
    parser.add_argument("--manager-archive", type=Path)
    parser.add_argument("--additional-v3-store", type=Path, action="append", default=[])
    args = parser.parse_args()
    config = load_config(args.config)
    if engine_identity()["fingerprint"] != ENGINE:
        raise ContractError("pnpm9 preparation requires the original frozen engine")
    tasks = selected_tasks(Path(config["tasks"]), Path(config["scope_ids"]))
    ids = args.cohort_ids.read_text().splitlines()
    if len(tasks) != 402 or len(ids) != 1 or ids[0] not in tasks:
        raise ContractError("pnpm9 preparation requires one ID within the unchanged full402 scope")
    ident = ids[0]
    oracles = read_jsonl(Path(config["oracles"]))
    cases = json.loads((args.legacy_root / "tsjs/cases.json").read_text())["cases"]
    ctx = MigrationContext(config, tasks, oracles, args.legacy_root, args.out_dir)
    services = ExecutionServices(config)
    try:
        value = prepare(ctx, services, tasks[ident], oracles[ident], cases[ident],
                        args.out_dir / component(ident), args.manager_root, args.retained_store,
                        args.node_record, args.node_record_sha256,
                        manager_archive=args.manager_archive, additional_v3_stores=args.additional_v3_store)
        summary = {"id": ident, "status": value["status"], "expected_tasks": 402,
                   "dependencies": str(args.out_dir / component(ident) / "dependencies.json"),
                   "external_bytes": value["external_bytes"], "workspace_links": len(value["workspace_links"]),
                   "evaluation_completion": False}
        code = 0
    except Exception as exc:
        summary = {"id": ident, "status": "blocked", "expected_tasks": 402,
                   "error": f"{type(exc).__name__}: {exc}", "evaluation_completion": False}
        code = 2
    atomic_json(args.out_dir / "summary.json", summary)
    print(json.dumps(summary), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
