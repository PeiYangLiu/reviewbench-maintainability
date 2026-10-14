"""Opt-in admission of VFS61's first NEW unlocked-prepare receipt, never resolve.

The original task had no lock in either tree. This module consumes the parent's
explicit first-resolution decision and Gauss's completed metadata/byte evidence.
It neither executes Cargo nor infers a dependency choice from replay outcomes.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil

from reviewbench_maintainability.agentic.contracts import (
    ContractError, atomic_json, canonical_hash, file_hash,
)
from . import rust_vfs_resolution_entry as entry

TASK = entry.TASK
OPTION = "rust_vfs61_first_resolution"
INPUT = "c318a08e6f57b8e5efa0d4f115cf903da55c66344a38be7f641602c9de17fa39"
GOLDEN = "15cd4e4c4b9596b47c3136964857a19afaca30ecec102d977e581f06053aa868"
START = "53b09441738c7a47bc08cb543605b9fbb1265418"
STATUS = "first_new_environment_lock_and_full_metadata_prepared"
PROVENANCE = "new environment resolution, never historical-lock recovery"


def require(condition, message):
    if not condition:
        raise ContractError("VFS61 first-resolution receipt: " + message)


def requested(ctx, task):
    # A full402 migration may carry this one opt-in. Other tasks never enter it.
    if task.get("id") != TASK:
        return False
    option = getattr(ctx, "config", {}).get("migration", {}).get(OPTION)
    if option is None:
        return False
    require(isinstance(option, dict) and type(option.get("enabled")) is bool, "invalid opt-in")
    return option["enabled"]


def reference(record, *, json_data=True, limit=4 * 1024**2):
    require(isinstance(record, dict) and isinstance(record.get("path"), str)
            and isinstance(record.get("sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", record["sha256"]), "unbound retained file")
    path = Path(record["path"])
    require(path.is_absolute() and path.is_file() and not path.is_symlink(), "missing retained file")
    require(path.stat().st_size <= limit, "retained file exceeds byte bound")
    data = path.read_bytes()
    require(hashlib.sha256(data).hexdigest() == record["sha256"]
            and ("bytes" not in record or record["bytes"] == len(data)), "retained file hash/size changed")
    return entry.strict_json(data) if json_data else data


def fields(record, expected, label):
    require(isinstance(record, dict), label + " has invalid shape")
    require(all(type(record.get(k)) is type(v) and record[k] == v for k, v in expected.items()),
            label + " binding changed")


def command_record(row, argv, before):
    record = reference(row["result"])
    fields(record, {
        "argv": argv, "exit_status": 0, "natural_exit": True, "stop_reason": None,
        "lock_before_sha256": before, "lock_after_sha256": entry.LOCK,
        "source_before_matches_original": True, "source_after_matches_original": True,
    }, "successful original Cargo command")
    for name in ("argv", "cwd", "exit_status", "natural_exit", "environment",
                 "started_at", "completed_at", "command_timeout_seconds"):
        require(row[name] == record[name], "summary differs from raw command: " + name)
    streams = {}
    for name in ("stdout", "stderr"):
        require(record[name] == row[name]["path"] and record[name + "_sha256"] == row[name]["sha256"],
                "command stream binding changed")
        streams[name] = reference(row[name], json_data=False)
    require(not entry.guard.process_failure({
        "returncode": record["exit_status"], "stdout": "", "stderr": streams["stderr"].decode(),
    }), "prepare/metadata infrastructure failure")
    return record, streams


def read_receipt(config):
    """Verify raw command/lock/graph/archive evidence, not just a ready boolean."""
    option = config.get("migration", {}).get(OPTION)
    require(isinstance(option, dict) and set(option) == {
        "enabled", "task_id", "receipt", "receipt_sha256", "decision", "decision_sha256"
    } and option["enabled"] is True and option["task_id"] == TASK, "explicit exact-task opt-in missing")
    decision = reference({"path": option["decision"], "sha256": option["decision_sha256"]})
    fields(decision, {
        "schema": "reviewbench-original-unlocked-preparation-decision/v1", "task_id": TASK,
        "original_manifest_sha256": entry.MANIFEST, "original_prepare_command": entry.PREPARE,
        "original_task_input_sha256": INPUT, "original_committed_lock_absent": True,
        "original_golden_does_not_add_lock": True, "start_and_golden_manifests_identical": True,
        "model_calls_by_decision": 0, "before_first_model_generation": True, "provenance": PROVENANCE,
    }, "parent decision")
    receipt = reference({"path": option["receipt"], "sha256": option["receipt_sha256"]})
    fields(receipt, {
        "schema_version": "vfs61-first-unlocked-protocol-resolution/v1", "status": STATUS,
        "task_id": TASK, "input_index": 61, "original_start_sha": START,
        "original_plan_sha256": entry.PLAN, "original_golden_sha256": GOLDEN,
        "original_prepare_command_verbatim": entry.PREPARE, "manifest_sha256": entry.MANIFEST,
        "manifest_bytes": 1168, "manifest_pair_byte_identical": True,
        "historical_restoration": False, "historical_lock_absence_verified": True,
        "new_update_invocations": 1, "selection_uses_test_results": False,
        "model_calls": 0, "sut_builds_checks_tests": 0, "public_checks": 0, "calibrations": 0,
        "metadata_has_full_lock_graph": True, "metadata_all_features_validation_only": True,
        "source_bytes_unchanged_except_new_Cargo_lock": True,
        "required_tokio_stream": "0.1.14", "observed_tokio_stream": "0.1.14",
        "lock_packages": 102, "registry_packages": 101, "git_packages": 0,
        "start_golden_normalized_graph_sha256": entry.GRAPH,
    }, "new environment receipt")
    # All linked small evidence files remain hash-bound, including preservation
    # and resource records. This never traverses an unrelated cache or Blob tree.
    for value in receipt.values():
        if isinstance(value, dict) and {"path", "sha256"} <= set(value):
            reference(value, json_data=False)
    require(receipt["original_task_input_json"]["sha256"] == INPUT, "original task input changed")
    inputs = reference(receipt["original_task_input_json"])
    require(canonical_hash(reference(receipt["original_plan_json"])) == entry.PLAN
            and canonical_hash(inputs["resolved_plan"]) == entry.PLAN, "original prepare/plan changed")
    require(receipt["original_test_commands"] == {
        v: inputs["resolved_plan"][v + "_command"] for v in ("start", "golden")
    } and receipt["original_test_commands_unchanged_and_unlocked"] is True,
        "receipt changed original unlocked test commands")
    first = reference(receipt["first_success"])
    fields(first, {
        "origin": "first-successful-new-environment-original-prepare",
        "first_success_frozen": True, "historical_restoration": False,
        "prepare_command_verbatim": entry.PREPARE, "update_exit_status": 0,
        "lock_sha256": entry.LOCK, "lock_bytes": 24634,
        "manifest_sha256": entry.MANIFEST, "original_plan_sha256": entry.PLAN,
        "tokio_stream_versions": ["0.1.14"], "pin_valid": True,
        "source_after_matches_original": True, "new_update_invocations": 1, "selection_uses_tests": False,
    }, "first successful prepare")
    require(receipt["new_lock"]["sha256"] == entry.LOCK, "not the first frozen lock")
    packages = entry.lock_packages(reference(receipt["new_lock"], json_data=False))
    original = reference(receipt["original_input_proof"])
    fields(original, {
        "task_id": TASK, "input_index": 61, "start_sha": START,
        "task_inputs_sha256": INPUT, "original_plan_sha256": entry.PLAN,
        "golden_patch_sha256": GOLDEN, "prepare_command_verbatim": entry.PREPARE,
        "manifests_byte_identical": True, "version_selection_uses_test_results": False,
    }, "original input proof")
    require(original["historical_lock_present"] is False,
            "original tree already had a lock")
    source_before = reference(receipt["source_before"])
    bindings = reference(receipt["source_bindings"])
    from .rust_vfs import entry as vfs
    for variant in ("start", "golden"):
        names = original["complete_pre_harness_source_files"][variant]
        require({n: r["sha256"] for n, r in names.items() if Path(n).name in {"Cargo.toml", "Cargo.lock"}}
                == {"Cargo.toml": entry.MANIFEST}, "original manifest/lock tree changed")
        expected_source = {**names, **{
            p: {"sha256": h, "bytes": len(next(r["content"].encode()
                                              for r in inputs["resolved_plan"]["session_files"] if r["path"] == p))}
            for p, h in vfs.HARNESSES.items()
        }}
        require(source_before[variant] == expected_source
                and bindings["sources"][variant]["source_tree_excluding_new_lock_sha256"]
                == canonical_hash(expected_source)
                and bindings["sources"][variant]["before_and_after_equal"] is True,
                "complete original source/harness preservation changed")
    runs = receipt["cargo_runs"]
    require(isinstance(runs, list) and [r["label"] for r in runs] == [
        "update", "metadata-start", "metadata-golden"], "missing/repeated original prepare or graph command")
    update, output = command_record(runs[0], ["/bin/bash", "--noprofile", "--norc", "-c", entry.PREPARE], None)
    require(output["stdout"] == b"" and first["update_result"] == runs[0]["result"]["path"],
            "first-success/raw prepare binding changed")
    complete = reference(receipt["metadata"])
    fields(complete, {
        "historical_restoration": False, "new_update_invocations": 1,
        "same_lock_and_complete_graph": True, "sut_builds_checks_tests": 0,
        "public_calibrations_models_registrations": 0,
    }, "full graph receipt")
    require(len(complete["proofs"]) == 2, "both original manifest graphs are required")
    for i, variant in enumerate(("start", "golden"), 1):
        argv = ["cargo", "+1.93.0", "metadata", "--format-version", "1", "--locked",
                "--all-features", *(["--offline"] if variant == "golden" else [])]
        record, output = command_record(runs[i], argv, entry.LOCK)
        require(record["cwd"] == str(Path(update["cwd"]).parent / variant), "graph source binding changed")
        normalized = entry.normalized_graph(entry.strict_json(output["stdout"]), packages, Path(record["cwd"]))
        proof = complete["proofs"][i - 1]
        fields(proof, {
            "variant": variant, "packages": 102, "resolve_nodes": 102, "workspace_members": 1,
            "exit_status": 0, "lock_sha256": entry.LOCK,
            "normalized_complete_graph_sha256": normalized,
            "stdout": runs[i]["stdout"]["path"], "stdout_sha256": runs[i]["stdout"]["sha256"],
            "result": runs[i]["result"]["path"], "all_ids_and_edges_closed": True,
            "all_lock_packages_equal_metadata_packages": True, "all_features_metadata_only": True,
            "source_unchanged_except_new_lock": True,
        }, "raw complete graph")
    toolchain = reference(receipt["toolchain_binding"])
    fields(toolchain["original_retained_receipt"], {"status": "ready", "version": "1.93.0", "exit_status": 0},
           "original toolchain")
    for name, digest in (("cargo", entry.CARGO_SHA), ("rustc", entry.RUSTC_SHA)):
        require(toolchain[name]["sha256"] == digest and file_hash(Path(toolchain[name]["path"])) == digest,
                "original pinned " + name + " changed")
    archive_record = reference(receipt["archive_checksums"])
    fields(archive_record, {
        "schema_version": "vfs61-new-lock-archive-checksums/v1", "lock_sha256": entry.LOCK,
        "registry_packages": 101, "git_packages": 0, "missing": [],
    }, "retained dependency archives")
    rows = archive_record["rows"]
    require(len(rows) == 101, "missing/extra dependency archive")
    archives = {}
    expected = {(p["name"], p["version"], p["checksum"]) for p in packages if p.get("source")}
    for row in rows:
        identity = row["name"], row["version"], row["sha256"]
        require(identity in expected and identity not in archives
                and row["source"] == "registry+https://github.com/rust-lang/crates.io-index"
                and row["filename"] == row["name"] + "-" + row["version"] + ".crate",
                "unbound/duplicate dependency archive")
        reference(row, json_data=False, limit=32 * 1024**2)
        archives[identity] = Path(row["path"])
    require(set(archives) == expected, "first graph has missing dependency payloads")
    return receipt, entry.marker(option["receipt_sha256"], option["decision_sha256"]), archives


def build_specification(ctx, task, plan, oracle, patch):
    from . import rust_vfs
    from .rust_overlay import reference_inputs
    require(requested(ctx, task), "profile is not enabled for this exact task")
    # Preserve all existing original Oracle, golden, harness and six-log guards.
    rust_vfs.specification(ctx, task, plan, oracle)
    require(patch["sha256"] == GOLDEN and file_hash(Path(patch["path"])) == GOLDEN, "actual golden changed")
    inputs = reference_inputs(ctx, task, patch, ["Cargo.lock", "Cargo.toml"])
    for variant in ("start", "golden"):
        require(inputs[variant]["Cargo.lock"] is None
                and isinstance(inputs[variant]["Cargo.toml"], bytes)
                and hashlib.sha256(inputs[variant]["Cargo.toml"]).hexdigest() == entry.MANIFEST,
                "original tree has an old lock or mismatched manifest")
    try:
        receipt, marker, _ = read_receipt(ctx.config)
    except (entry.OverlayError, OSError, ValueError, KeyError, TypeError) as exc:
        raise ContractError("invalid VFS61 first-resolution evidence: " + str(exc)) from exc
    registry = Path(ctx.config.get("rust_toolchains", ctx.output.parent / "rust-toolchains/toolchains.json"))
    tool = entry.strict_json(registry.read_bytes())["1.93.0"]
    require(tool["status"] == "ready" and all(
        file_hash(Path(tool["root"]) / "bin" / name) == digest
        for name, digest in (("cargo", entry.CARGO_SHA), ("rustc", entry.RUSTC_SHA))),
        "configured compiler is not the receipt's toolchain")
    spec = {
        "schema_version": "reviewbench-cargo-build-overlay/v1", "task_id": TASK,
        "manifests": [], "private_members": [], "cpus": 4,
        "locks": [{"path": "Cargo.lock", "asset_target": "/opt/reviewbench-assets/rust-lock-0",
                   "sha256": entry.LOCK, "allow_missing": True, "replace_if_sha256": []}],
        entry.MARKER: marker,
    }
    entry.validate(spec)
    locks = {"Cargo.lock": ctx.copy_asset(Path(receipt["new_lock"]["path"]), "rust-first-new-lock")}
    atomic_json(ctx.output / "rust-vfs-first-resolution" / (canonical_hash(spec) + ".json"), {
        "task_id": TASK, "historical_restoration": False, "runtime_ready": False,
        "receipt_sha256": marker["receipt_sha256"], "decision_sha256": marker["decision_sha256"],
        "new_resolver_invocations": 0, "new_downloads": 0,
        "lock_sha256": entry.LOCK, "complete_graph_sha256": entry.GRAPH,
        "public_or_sut_runs": 0, "model_calls": 0,
    })
    return spec, locks


def retained_archives(ctx, spec, locks):
    entry.validate(spec)
    require(set(locks) == {"Cargo.lock"} and locks["Cargo.lock"]["sha256"] == entry.LOCK
            and file_hash(Path(locks["Cargo.lock"]["path"])) == entry.LOCK,
            "copied first-lock asset changed before dependency packaging")
    try:
        _, marker, archives = read_receipt(ctx.config)
    except (entry.OverlayError, OSError, ValueError, KeyError, TypeError) as exc:
        raise ContractError("VFS61 retained dependency closure changed: " + str(exc)) from exc
    require(marker == spec[entry.MARKER], "vendoring receipt differs from admitted build spec")
    return archives


def binding(ctx, spec, execute, public):
    """Same wrapper, separate commands; no private harness/spec in public assets."""
    entry.validate(spec)
    root = ctx.output / "rust-vfs-resolution-tools"
    # build_tools has already installed the byte-pinned Python3.10 TOML helpers.
    shutil.copytree(ctx.output / "rust-build-tools", root, dirs_exist_ok=True)
    for name in ("rust_vfs_resolution_entry.py", "rust_test_compat_entry.py", "rust_api_entry.py"):
        shutil.copyfile(Path(__file__).with_name(name), root / name)
    (root / "bin").mkdir(exist_ok=True)
    shim = root / "bin/cargo"
    shim.write_text("#!/bin/sh\nexec python3 " + entry.TOOLS
                    + '/rust_vfs_resolution_entry.py --cargo-shim "$@"\n')
    shim.chmod(0o755)
    asset = {**ctx.bundle(root, role="toolchain"), "kind": "archive", "visibility": "developer",
             "target": entry.TOOLS}
    def wrap(command):
        return shlex.join(["python3", entry.TOOLS + "/rust_vfs_resolution_entry.py",
                           "--spec", entry.SPEC, "--cwd", ".", "--", *command])
    return wrap(execute), wrap(public), [asset]
