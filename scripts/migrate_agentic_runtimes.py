#!/usr/bin/env python3
"""Migrate the entire configured task inventory; verify ready recipes in bounded jobs."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, os.environ.get("REVIEWBENCH_ENGINE_SRC", str(Path(__file__).resolve().parents[1] / "src")))
from reviewbench_maintainability.agentic.cli import load_config
from reviewbench_maintainability.agentic.contracts import (
    atomic_json, canonical_hash, component, lane_for, read_jsonl, selected_tasks, write_jsonl, RuntimeRecipe,
)
from reviewbench_maintainability.agentic.execution import ExecutionServices
from reviewbench_maintainability.agentic.models import engine_identity
from reviewbench_maintainability.agentic.preparation import load_recipes, prepare, verify_recipe
from reviewbench_maintainability.agentic.resources import ResourceRequest
from reviewbench_maintainability.agentic.scheduling import completed_jobs, output_lock
from agentic_runtime_migration.common import MigrationContext
from agentic_runtime_migration import (
    python_lane, native_lane, go_lane, rust_lane, tsjs_lane, scripted_lane, csharp_lane, jvm_lane, rust_fuel, rust_anchor,
)
from agentic_runtime_migration.scheduler import classified_jobs
from agentic_runtime_migration.legacy_inputs import (
    LegacyInputError, load_inventory, load_legacy_inputs,
)


def import_existing(config: dict, tasks: dict) -> dict:
    recipes = load_recipes(Path(config["runtime_recipes"])) if config.get("runtime_recipes") else {}
    root = Path(config["asset_root"])
    result = {}
    for ident, recipe in recipes.items():
        if ident not in tasks:
            continue
        value = asdict(recipe)
        for asset in [value["image_audit"], *value["assets"], value["grading"]["golden_patch"]]:
            path = Path(asset["path"])
            asset["path"] = str(path if path.is_absolute() else root / path)
        result[ident] = RuntimeRecipe.from_dict(value)
    return result


def legacy_inventory_for_scope(tasks, oracles, path, digest):
    inventory = load_inventory(path, expected_sha256=digest)
    document = inventory.document
    expected = {ident for ident in tasks if not oracles[ident].get("lane_results_source")}
    captured = [row["id"] for row in document["rows"]]
    if (type(document["scope_size"]) is not int or document["scope_size"] != len(tasks)
            or len(captured) != len(expected) or set(captured) != expected):
        raise LegacyInputError("migration_scope_mismatch", "legacy inputs must retain the complete task scope")
    for ident in expected:
        inventory.for_task(tasks[ident], oracles[ident])
    return inventory


def verify_engine_binding(config):
    actual = engine_identity()["fingerprint"]
    expected = config.get("migration", {}).get("verification_engine_fingerprint")
    if expected is not None and expected != actual:
        raise ValueError("verification engine differs from the configured frozen experiment engine")
    return actual


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--legacy-root", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--refresh-lanes", nargs="*", default=[],
                        help="regenerate migrated recipes in these lanes; preserve imported original recipes")
    parser.add_argument("--refresh-failed-only", action="store_true",
                        help="keep previously successful task/runtime proofs while repairing failed recipes")
    parser.add_argument("--lanes", nargs="+", default=["gopy", "scripted", "tsjs", "csharp", "jvm", "rust", "native"])
    parser.add_argument("--languages", nargs="*", help="migration batch only; does not shrink the declared task scope")
    parser.add_argument("--verify-lanes", nargs="*", help="verify these lanes in this batch; full readiness still requires all tasks")
    parser.add_argument("--verify-languages", nargs="*", help="verify these languages in this batch")
    parser.add_argument("--maven-cache-root", type=Path,
                        help="prepared per-task dependency caches; preserves the declared task/model scope")
    parser.add_argument("--maven-base-package", type=Path,
                        help="reuse immutable base Maven archives; upload only task-specific dependency deltas")
    parser.add_argument("--migration-ids", type=Path,
                        help="IDs to prepare in this batch only; the declared evaluation scope remains unchanged")
    parser.add_argument("--node-dependency-root", type=Path,
                        help="pinned per-task npm preparation directory; does not change evaluation scope")
    parser.add_argument("--scripted-dependency-root", type=Path,
                        help="locked Composer/Bundler snapshots; preserves the declared task scope")
    parser.add_argument("--toolchains", type=Path, help="shared pinned developer toolchain registry")
    parser.add_argument("--jvm-shared-assets", type=Path,
                        help="shared immutable JVM descriptor index reused across output batches")
    parser.add_argument("--jvm-platforms", type=Path,
                        help="explicit per-task retained SDK/internal-network inputs; task scope remains unchanged")
    parser.add_argument("--gradle-cache-root", type=Path, help="prepared per-task Gradle download caches")
    parser.add_argument("--sbt-boot-root", type=Path, help="prepared exact-version sbt launcher/boot distributions")
    parser.add_argument("--sbt-dependency-root", type=Path, help="prepared per-task sbt dependency deltas")
    parser.add_argument("--legacy-inputs", type=Path, help="read-only published-legacy input inventory")
    parser.add_argument("--legacy-inputs-sha256", help="independent SHA256 pin for --legacy-inputs")
    args = parser.parse_args()
    if bool(args.legacy_inputs) != bool(args.legacy_inputs_sha256):
        parser.error("--legacy-inputs and --legacy-inputs-sha256 must be supplied together")
    original = load_config(args.config)
    if args.verify:
        verify_engine_binding(original)
    tasks = selected_tasks(Path(original["tasks"]), Path(original["scope_ids"]) if original.get("scope_ids") else None)
    migration_ids = set(args.migration_ids.read_text().splitlines()) if args.migration_ids else set(tasks)
    if not migration_ids or not migration_ids <= tasks.keys():
        raise ValueError("migration batch contains missing/foreign task IDs")
    oracles = read_jsonl(Path(original["oracles"]))
    legacy_inventory = (legacy_inventory_for_scope(
        tasks, oracles, args.legacy_inputs, args.legacy_inputs_sha256,
    ) if args.legacy_inputs else None)
    output = args.out_dir.resolve()
    config = {**original, "asset_root": str(output), "out_dir": str(output / "execution"),
              "runtime_recipes": str(output / "runtime-recipes.jsonl")}
    if args.maven_cache_root:
        config["migration"] = {**config.get("migration", {}),
                               "maven_cache_root": str(args.maven_cache_root.resolve())}
    if args.maven_base_package:
        config["migration"] = {**config.get("migration", {}),
                               "maven_base_package": str(args.maven_base_package.resolve())}
    if args.node_dependency_root:
        config["migration"] = {**config.get("migration", {}),
                               "node_dependency_root": str(args.node_dependency_root.resolve())}
    if args.scripted_dependency_root:
        config["migration"] = {**config.get("migration", {}),
                               "scripted_dependency_root": str(args.scripted_dependency_root.resolve())}
    if args.toolchains:
        config["migration"] = {**config.get("migration", {}),
                               "toolchain_registry": str(args.toolchains.resolve())}
    if args.jvm_shared_assets:
        config["migration"] = {**config.get("migration", {}),
                               "jvm_shared_assets": str(args.jvm_shared_assets.resolve())}
    if args.jvm_platforms:
        config["migration"] = {**config.get("migration", {}),
                               "jvm_platforms": str(args.jvm_platforms.resolve())}
    if args.gradle_cache_root:
        config["migration"] = {**config.get("migration", {}),
                               "gradle_cache_root": str(args.gradle_cache_root.resolve())}
    if args.sbt_boot_root:
        config["migration"] = {**config.get("migration", {}),
                               "sbt_boot_root": str(args.sbt_boot_root.resolve())}
    if args.sbt_dependency_root:
        config["migration"] = {**config.get("migration", {}),
                               "sbt_dependency_root": str(args.sbt_dependency_root.resolve())}
    with output_lock(output):
        recipes = import_existing(original, tasks)
        imported_ids = set(recipes)
        if Path(config["runtime_recipes"]).is_file():
            import shutil
            from reviewbench_maintainability.agentic.contracts import file_hash
            history = output / "history"
            history.mkdir(exist_ok=True)
            source = Path(config["runtime_recipes"])
            shutil.copyfile(source, history / ("recipes-" + file_hash(source)[:16] + ".jsonl"))
            recipes.update(load_recipes(Path(config["runtime_recipes"])))
        for ident in list(recipes):
            if (ident not in imported_ids and ident in migration_ids
                    and lane_for(tasks[ident], oracles[ident]) in args.refresh_lanes):
                proof_path = output/"execution/preparation/verified"/component(ident)/"verification.json"
                if args.refresh_failed_only and proof_path.is_file():
                    proof = json.loads(proof_path.read_text())
                    if (proof.get("status") == "ready"
                            and proof.get("runtime_fingerprint") == recipes[ident].fingerprint):
                        continue
                del recipes[ident]
        ctx = MigrationContext(config, tasks, oracles, args.legacy_root.resolve(), output)
        services = ExecutionServices(config)
        decisions = {}

        def migrate(ident):
            task, oracle = tasks[ident], oracles[ident]
            lane = lane_for(task, oracle)
            record = {"id": ident, "repo": task["repo"], "lane": lane, "status": "pending"}
            if ident in recipes:
                return record | {"status": "configured", "reused_recipe": True}, recipes[ident]
            if ident not in migration_ids:
                return record | {"reason": "task not selected for this migration batch"}, None
            if lane not in args.lanes:
                return record | {"reason": "lane not selected for this migration batch"}, None
            if args.languages and task.get("inferred_language", "").lower() not in {v.lower() for v in args.languages}:
                return record | {"reason": "language not selected for this migration batch"}, None
            try:
                # Explicit original-input adapters bind their published release,
                # complete parent sources, original assertions and prepared
                # graphs; legacy indices never select an unrelated hard case.
                original_explicit = lane == "rust" and (
                    rust_fuel.requested(ctx, task) or rust_anchor.requested(ctx, task))
                if not oracle.get("lane_results_source") and not original_explicit:
                    if legacy_inventory is None:
                        raise ValueError("published legacy runtime adapter pending")
                    inputs = load_legacy_inputs(task, oracle, inventory=legacy_inventory)
                    observed = inputs.row
                    record["legacy_inputs"] = {
                        "inventory_sha256": args.legacy_inputs_sha256,
                        "source_status": observed["source"]["status"],
                        "six_original_signals_bound": observed["replay"]["six_original_signals_bound"],
                        "required_lane_adapter": observed["required_lane_adapter"],
                        "migration_route": observed["migration_route"],
                        "runtime_verified": False,
                    }
                    oracle = inputs.require_existing_lane()
                scratch_bytes = (rust_lane.migration_scratch_bytes(ctx, task) if lane == "rust"
                                 else (1024**3 if lane != "jvm" else jvm_lane.migration_scratch_bytes(ctx, task)))
                if lane == "gopy" and task.get("inferred_language", "").lower() == "go":
                    scratch_bytes = go_lane.migration_scratch_bytes(ctx)
                with services.gate.acquire(ResourceRequest("migrate:" + ident, cpus=2 if lane == "rust" else 1,
                                                           memory_bytes=(1024 if lane == "rust" else 512) * 1024**2,
                                                           scratch_bytes=scratch_bytes)):
                    if lane == "gopy":
                        adapter = go_lane if task.get("inferred_language", "").lower() == "go" else python_lane
                        recipe = adapter.migrate(ctx, task, oracle)
                    elif lane == "native":
                        recipe = native_lane.migrate(ctx, task, oracle)
                    elif lane == "rust":
                        recipe = rust_lane.migrate(ctx, task, oracle)
                    elif lane == "tsjs":
                        recipe = tsjs_lane.migrate(ctx, task, oracle)
                    elif lane == "scripted":
                        recipe = scripted_lane.migrate(ctx, task, oracle)
                    elif lane == "csharp":
                        recipe = csharp_lane.migrate(ctx, task, oracle)
                    elif lane == "jvm":
                        recipe = jvm_lane.migrate(ctx, task, oracle)
                    else:
                        raise ValueError(f"{lane} runtime adapter pending")
                return record | {"status": "configured"}, recipe
            except Exception as exc:
                return record | {"reason": f"{type(exc).__name__}: {exc}"}, None

        for ident, (decision, recipe) in completed_jobs(migrate, tasks, args.workers):
            decisions[ident] = decision
            if recipe:
                recipes[ident] = recipe
            write_jsonl(output / "migration-inventory.jsonl", [decisions[k] for k in tasks if k in decisions])
            write_jsonl(Path(config["runtime_recipes"]), [asdict(recipes[k]) for k in tasks if k in recipes])
            print(json.dumps({"migration": ident, "status": decision["status"],
                              "configured": len(recipes), "expected": len(tasks),
                              "reason": decision.get("reason")}), flush=True)
        report = prepare(tasks, oracles, recipes, Path(config["repo_cache"]), output,
                         Path(config["out_dir"]) / "preparation")
        if args.verify:
            proofs = {}
            fingerprint = engine_identity()["fingerprint"]
            for ident, recipe in recipes.items():
                path = Path(config["out_dir"])/"preparation/verified"/component(ident)/"verification.json"
                if path.is_file():
                    value = json.loads(path.read_text())
                    if (value.get("runtime_fingerprint") == recipe.fingerprint
                            and value.get("task_fingerprint") == canonical_hash(tasks[ident])
                            and value.get("oracle_fingerprint") == canonical_hash(oracles[ident])
                            and value.get("engine_fingerprint") == fingerprint):
                        proofs[ident] = value
            def verify(ident):
                task, recipe, oracle = tasks[ident], recipes[ident], oracles[ident]
                directory = Path(config["out_dir"]) / "preparation/verified" / component(ident)
                existing = directory / "verification.json"
                if existing.is_file():
                    previous = json.loads(existing.read_text())
                    if (previous.get("status") == "ready" and previous.get("runtime_fingerprint") == recipe.fingerprint
                            and previous.get("task_fingerprint") == canonical_hash(task)
                            and previous.get("oracle_fingerprint") == canonical_hash(oracle)
                            and previous.get("engine_fingerprint") == fingerprint):
                        return previous
                    atomic_json(directory / ("verification-" + canonical_hash(previous)[:16] + ".json"), previous)
                try:
                    with services.job(task, recipe, ctx.repo(task), "verify"):
                        return verify_recipe(task, recipe, oracle, ctx.repo(task), output, directory)
                except Exception as exc:
                    record = {"id": ident, "status": "blocked", "error": f"{type(exc).__name__}: {exc}",
                              "runtime_fingerprint": recipe.fingerprint, "task_fingerprint": canonical_hash(task),
                              "oracle_fingerprint": canonical_hash(oracle), "engine_fingerprint": fingerprint}
                    atomic_json(existing, record)
                    return record
            selected = [i for i in recipes
                        if (not args.migration_ids or i in migration_ids)
                        and (not args.verify_lanes or recipes[i].lane in args.verify_lanes)
                        and (not args.verify_languages or tasks[i].get("inferred_language", "").lower()
                             in {v.lower() for v in args.verify_languages})]
            def heavy(ident):
                recipe = recipes[ident]
                return (recipe.lane in {"native", "rust", "jvm", "csharp"} or recipe.memory_mb > 4096
                        or services.source_bytes(tasks[ident], ctx.repo(tasks[ident])) > 1024**3)
            for ident, proof in classified_jobs(verify, selected, args.workers, heavy,
                                                services.gate.limits.heavy_tasks):
                proofs[ident] = proof
                report["execution_verified"] = sum(p["status"] == "ready" for p in proofs.values())
                report["verification_failures"] = {i: p.get("error") for i,p in proofs.items() if p["status"] != "ready"}
                report["complete"] = report["execution_verified"] == len(tasks) and report["blocked"] == 0
                atomic_json(Path(config["out_dir"]) / "preparation/readiness.json", report)
                print(json.dumps({"verified": ident, "status": proof["status"], "ready": report["execution_verified"],
                                  "expected": len(tasks), "error": proof.get("error")}), flush=True)
            report["execution_verified"] = sum(p["status"] == "ready" for p in proofs.values())
            report["verification_failures"] = {i:p.get("error") for i,p in proofs.items() if p["status"] != "ready"}
            report["complete"] = report["execution_verified"] == len(tasks) and report["blocked"] == 0
            atomic_json(Path(config["out_dir"])/"preparation/readiness.json", report)
        import yaml
        (output / "experiment.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
        atomic_json(output / "resource-usage.json", services.gate.status())
        atomic_json(output / "migration-summary.json", report)
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
