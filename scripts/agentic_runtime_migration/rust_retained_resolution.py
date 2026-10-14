"""Bind existing Ariel/Tock post-preparation locks to complete retained inputs.

The original Oracles do not declare lock digests. The separate JSON digests are
explicit recovery pins, not invented historical fields. Complete manifest trees,
recorded environment changes, both retained locks and all six original successful
run logs are checked before the default is offered to a current-candidate mirror.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from reviewbench_maintainability.agentic.contracts import (
    ContractError, atomic_json, canonical_hash, file_hash, repo_path,
)
from reviewbench_maintainability.agentic.snapshots import git
from .rust_lock_assets import _validate_lock, attested_bytes, reference_trees

BINDINGS = json.loads(Path(__file__).with_name("rust_retained_locks.json").read_text())


def add_retained_resolution(ctx, task, plan, oracle, patch, spec, defaults):
    pin = BINDINGS.get(task["id"])
    if pin is None:
        return
    if (any(task.get(key) != pin[key] or oracle.get(key) != pin[key] for key in ("repo", "start_sha"))
            or oracle.get("id") != task["id"] or plan.get("input_index") != pin["input_index"]
            or oracle.get("input_index") != pin["input_index"]
            or canonical_hash(plan) != pin["plan_sha256"]):
        raise ContractError("retained Cargo resolution has a different task/start/plan")
    root = ctx.legacy / "rust"
    original = json.loads(attested_bytes(root, pin["artifacts"] + "/result.json", pin["oracle_file_sha256"]))
    if canonical_hash(original) != canonical_hash({key: value for key, value in oracle.items()
                                                 if key != "lane_results_source"}):
        raise ContractError("retained Cargo resolution differs from the original Oracle")
    if (patch.get("sha256") != pin["patch_sha256"] or oracle.get("golden_patch_sha256") != pin["patch_sha256"]
            or file_hash(Path(patch["path"])) != pin["patch_sha256"]):
        raise ContractError("retained Cargo resolution accepted patch changed")
    if (plan.get("prepare_command") or plan.get("environment_file_replacements")
            or plan.get("test_compatibility_replacements")
            or any(Path(item["path"]).name == "Cargo.toml" for item in plan.get("session_files", []))):
        raise ContractError("retained Cargo resolution has unaccounted preparation/session manifests")
    if defaults or spec["locks"]:
        raise ContractError("retained Cargo resolution conflicts with another lock default")
    trees = reference_trees(ctx, task, patch, {"Cargo.lock"})
    changed_paths = {repo_path(item["path"]) for item in plan.get("environment_replacements", [])}
    if changed_paths != {entry["path"] for entry in spec["manifests"]}:
        raise ContractError("retained Cargo resolution does not account for every build overlay")
    from .rust_overlay import reference_inputs
    from .rust_build_entry import manifest_operations
    import tomllib
    reference_build_inputs = reference_inputs(ctx, task, patch, sorted(changed_paths))
    operations = {entry["path"]: entry["operations"] for entry in spec["manifests"]}
    checked_logs, manifest_evidence, lock_source = {}, {}, None
    for variant, (manifests, locks) in trees.items():
        before = locks["Cargo.lock"]
        before_sha = hashlib.sha256(before).hexdigest() if before is not None else None
        if before_sha != pin["reference_lock_sha256"]:
            raise ContractError("retained Cargo resolution has a different reference lock")
        snapshot = root / pin["artifacts"] / variant
        if snapshot.is_symlink() or git(snapshot, "rev-parse", "HEAD").decode().strip() != task["start_sha"]:
            raise ContractError("retained Cargo source snapshot is redirected or has a different start")
        names = {name.decode() for name in git(snapshot, "ls-files", "-z", "--cached", "--others",
                                               "--exclude-standard", "--", "*Cargo.toml").split(b"\0")
                 if name and Path(name.decode()).name == "Cargo.toml"}
        expected_manifests = dict(manifests)
        extra = {}
        evidence = oracle.get("environment_replacements", {}).get(variant, [])
        replacements = plan.get("environment_replacements", [])
        if len(evidence) != len(replacements):
            raise ContractError("retained Cargo source has extra/missing environment evidence")
        for item, observed in zip(replacements, evidence):
            name = repo_path(item["path"])
            if observed["path"] != name:
                raise ContractError("retained Cargo source environment paths changed")
            (expected_manifests if Path(name).name == "Cargo.toml" else extra)[name] = observed["sha256"]
        if names != set(expected_manifests):
            raise ContractError("retained Cargo source has a different complete manifest tree")
        for name, digest in {**expected_manifests, **extra}.items():
            attested_bytes(snapshot, name, digest)
        for name in changed_paths:
            before_build = reference_build_inputs[variant][name]
            if before_build is None:
                raise ContractError("retained Cargo build input was absent from its reference")
            after_build = attested_bytes(snapshot, name, {**expected_manifests, **extra}[name])
            observed_operations = manifest_operations(tomllib.loads(before_build.decode()),
                                                     tomllib.loads(after_build.decode()))
            if canonical_hash(observed_operations) != canonical_hash(operations[name]):
                raise ContractError("retained Cargo build overlay operations differ from the frozen inputs")
        data = attested_bytes(snapshot, "Cargo.lock", pin["lock_sha256"])
        _validate_lock(data)
        lock_source = lock_source or snapshot / "Cargo.lock"
        manifest_evidence[variant] = {**expected_manifests, **extra}
        runs = original.get("runs", {}).get(variant, [])
        if len(runs) != 3:
            raise ContractError("retained Cargo resolution needs three original runs per reference")
        for run in runs:
            name = repo_path(run["log"])
            digest = pin["logs"].get(name)
            attempts = run.get("attempts", [])
            if (not name.startswith(pin["artifacts"] + "/logs/") or not digest
                    or run.get("ok") is not True or run.get("failed") != 0 or run.get("failed_tests")
                    or run.get("passed_tests") != original["pass_to_pass_tests"]
                    or not attempts or attempts[-1].get("exit_code") != 0
                    or attempts[-1].get("timed_out") is not False or attempts[-1].get("log") != name):
                raise ContractError("retained Cargo resolution lacks its complete original successful run")
            attested_bytes(root, name, digest)
            checked_logs[name] = digest
    if len(checked_logs) != 6 or checked_logs != pin["logs"]:
        raise ContractError("retained Cargo resolution log bindings are incomplete")
    asset = ctx.copy_asset(lock_source, "rust-retained-locks")
    if asset["sha256"] != pin["lock_sha256"] or file_hash(Path(asset["path"])) != pin["lock_sha256"]:
        raise ContractError("copied retained Cargo resolution lock changed")
    defaults["Cargo.lock"] = asset
    record = {
        "path": "Cargo.lock", "asset_target": "/opt/reviewbench-assets/rust-lock-0",
        "sha256": asset["sha256"], "allow_missing": pin["reference_lock_sha256"] is None,
        "replace_if_sha256": sorted({digest for digest in
                                    (pin["reference_lock_sha256"], pin["lock_sha256"]) if digest is not None}),
    }
    spec["locks"].append(record)
    spec["retained_lock_resolution"] = {
        "lock_sha256": pin["lock_sha256"], "original_oracle_declares_lock_sha256": False,
        "runtime_policy": "only absent/original/default locks; preserve genuinely changed candidate locks",
    }
    atomic_json(ctx.output / "rust-retained-resolution" / (pin["lock_sha256"] + ".json"), {
        "task_id": task["id"], "basis": "explicit recovery pins; retained complete manifest trees and six original logs",
        "original_oracle_declares_lock_sha256": False, "pins": pin,
        "reference_and_environment_manifests": manifest_evidence,
        "lock": asset, "default_policy": record, "candidate_source_replacements": [],
        "candidate_test_replacements": [], "scoring_result": False,
    })
