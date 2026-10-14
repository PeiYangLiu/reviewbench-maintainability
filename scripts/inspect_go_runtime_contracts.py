#!/usr/bin/env python3
"""Inventory Go configuration gaps without fetching, containers, or live registration."""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, os.environ.get("REVIEWBENCH_ENGINE_SRC", str(Path(__file__).resolve().parents[1] / "src")))
from reviewbench_maintainability.agentic.contracts import atomic_json, file_hash
from agentic_runtime_migration.go_contracts import load_contract, transport_wrapper
from agentic_runtime_migration.go_dependencies import frozen_inputs
from agentic_runtime_migration.go_lane import compiler_version, find_compiler


def inventory(gaps, tasks, oracles, ctx):
    rows = []
    for gap in gaps["gaps"]:
        task = tasks[gap["id"]]
        if (gap["lane"] != "gopy" or gap.get("legacy")
                or task.get("inferred_language", "").lower() != "go"
                or gap["latest"]["kind"] != "configuration"):
            continue
        oracle = oracles[task["id"]]
        directory = ctx.task_directory(task, oracle, "gopy")
        row = {
            "id": task["id"], "repo": task["repo"], "original_gap": gap["latest"],
            "original_directory": str(directory), "published_p2p_count": oracle.get("pass_to_pass"),
            "original_session_spec_exists": (directory / "session-spec.json").is_file(),
            "runtime_verified": False, "runtime_status": "not-prepared",
        }
        try:
            contract = load_contract(ctx, task, oracle, directory)
            inputs = frozen_inputs(ctx, task, directory, contract.module_cwd, oracle=oracle, contract=contract)
            row.update({
                "contract_status": "supported", "adapter": contract.kind,
                "command": contract.command, "cwd": contract.cwd, "module_cwd": contract.module_cwd,
                "public_target": contract.public_target, "expected_tests": contract.expected,
                "go_version": compiler_version(ctx, contract.spec, task, contract.module_cwd),
                "module_pins": contract.module_pins, "locked_versions": len(inputs.modules),
                "manifest_inputs": inputs.manifests, "candidate_local_modules": sorted(inputs.local_modules),
                "grader_files": contract.grader_files, "support_files": contract.support_files,
                "contract_evidence": contract.provenance,
                "transport_changes": transport_wrapper(contract)[1] if contract.wrapper else [],
                "next_step": "stage exact compiler/proxy/tools; frozen-engine whole-candidate verify still required",
            })
            configured = ctx.config.get("migration", {}).get("go_toolchains", {}).get(row["go_version"])
            if configured:
                from agentic_runtime_migration.go_public_inputs import pinned_file
                _, reference = pinned_file(configured, "pinned-go-toolchain")
                row["compiler_status"] = "pinned-archive-needs-runtime-version-check"
                row["compiler_evidence"] = reference
            else:
                try:
                    source, observed = find_compiler(ctx, directory, contract.spec, row["go_version"])
                    row["compiler_status"] = "available"
                    row["compiler_evidence"] = {"path": str(source / "bin/go"), "version_output": observed}
                except Exception as exc:
                    row["compiler_status"] = "pending"
                    row["compiler_error"] = f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            row.update({"contract_status": "pending", "reason": f"{type(exc).__name__}: {exc}"})
        rows.append(row)
    return {
        "schema_version": "reviewbench-go-configuration-inventory/v1",
        "scope": gaps["scope"], "remaining_input": gaps["remaining"],
        "input_gap_ids": [g["id"] for g in gaps["gaps"]],
        "modern_go_configuration_tasks": len(rows),
        "contract_status_counts": dict(Counter(row["contract_status"] for row in rows)),
        "adapter_counts": dict(Counter(row.get("adapter", "original-evidence-pending") for row in rows)),
        "runtime_verified": 0, "denominator_changed": False, "rows": rows,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("gaps", "tasks", "oracles", "oracle-root", "repo-cache", "out"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    config = {}
    if args.config:
        import yaml
        config = yaml.safe_load(args.config.read_text())
    tasks = {row["id"]: row for row in map(json.loads, args.tasks.read_text().splitlines()) if row}
    oracles = {row["id"]: row for row in map(json.loads, args.oracles.read_text().splitlines()) if row}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    ctx = SimpleNamespace(
        config=config, legacy=args.oracle_root.resolve(), scratch=args.out.parent.resolve(),
        repo=lambda task: args.repo_cache / task["repo"].replace("/", "__"),
        task_directory=lambda task, oracle, lane: args.oracle_root / lane / oracle["artifacts"]["directory"],
    )
    report = inventory(json.loads(args.gaps.read_text()), tasks, oracles, ctx)
    report["input_files"] = [{"path": str(p.resolve()), "sha256": file_hash(p)}
                             for p in (args.gaps, args.tasks, args.oracles)]
    atomic_json(args.out, report)
    print(json.dumps({k: report[k] for k in (
        "scope", "remaining_input", "modern_go_configuration_tasks", "contract_status_counts",
        "runtime_verified", "denominator_changed",
    )}, sort_keys=True))
    return 2 if report["contract_status_counts"].get("pending") else 0


if __name__ == "__main__":
    raise SystemExit(main())
