"""Consume one successful, original Scylla minimum-version prepare sequence.

This is an explicitly new environment lock, not invented historical lock bytes.
The producer's full command and both precise updates remain mandatory. No
resolver, compiler, model, or network request is started by this reader.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import shlex
import tomllib

from reviewbench_maintainability.agentic.contracts import file_hash, repo_path
from reviewbench_maintainability.agentic.snapshots import tree_files

TASK = "github__scylladb__scylla-rust-driver__pull_509__comment_962978126__api_design"
PREPARE = (
    "rm -f Cargo.lock && cargo +nightly-2022-12-20 -Z minimal-versions generate-lockfile >/dev/null"
    " && cargo +1.64.0 update -p twox-hash --precise 1.6.3 >/dev/null"
    " && cargo +1.64.0 update -p thiserror --precise 1.0.35 >/dev/null"
)
SCHEMA = "reviewbench-scylla-original-prepare-admission/v1"
PINS = {"twox-hash": "1.6.3", "thiserror": "1.0.35"}


def validate_metadata_command(record):
    """Keep the raw argv while admitting the resolved, absolute Cargo shim."""
    from .rust_first_resolution import require
    expected = ["cargo", "+1.64.0", "metadata", "--format-version", "1",
                "--locked", "--all-features"]
    argv = record.get("argv")
    require(isinstance(argv, list) and len(argv) == len(expected)
            and isinstance(argv[0], str) and Path(argv[0]).name == "cargo"
            and (argv[0] == "cargo" or Path(argv[0]).is_absolute())
            and argv[1:] == expected[1:]
            and ("requested_argv" not in record or record["requested_argv"] == expected),
            "Scylla metadata command changed its Cargo shim, toolchain, or complete locked graph flags")


def validate_pins(lock):
    from .rust_first_resolution import require
    rows = lock.get("package")
    require(isinstance(rows, list), "Scylla final lock lacks packages")
    for name, version in PINS.items():
        observed = [row["version"] for row in rows if row.get("name") == name]
        require(observed == [version], "Scylla original precise pin changed: " + name)


def prepare_record(record, decision, lock_sha256):
    """Validate a normalized record whose raw source remains hash-bound."""
    from .rust_first_resolution import referenced, require
    require(record["command"] == PREPARE and record["exit_status"] == 0
            and record["natural_exit"] is True and record["timed_out"] is False,
            "Scylla original full prepare did not complete")
    require(record["source_before_lock_sha256"] is None
            and record["final_lock_sha256"] == lock_sha256,
            "Scylla final lock is not the missing-only original prepare result")
    require(record["completed_steps"] == [
        ["cargo", "+nightly-2022-12-20", "-Z", "minimal-versions", "generate-lockfile"],
        ["cargo", "+1.64.0", "update", "-p", "twox-hash", "--precise", "1.6.3"],
        ["cargo", "+1.64.0", "update", "-p", "thiserror", "--precise", "1.0.35"],
    ], "Scylla original prepare step order or pins changed")
    require(datetime.fromisoformat(decision["recorded_at"])
            < datetime.fromisoformat(record["started_at"])
            <= datetime.fromisoformat(record["completed_at"]),
            "Scylla decision was not fixed before original preparation")
    raw = referenced(record["raw_record"], json_data=True)
    require(raw["exit_status"] == 0 and raw.get("natural_exit", True) is True,
            "Scylla raw prepare process was not successful")
    command = raw.get("argv", raw.get("command"))
    require(command == record["raw_argv"] and isinstance(command, list)
            and command[-2:] == ["-c", PREPARE], "normalized prepare lost its exact raw shell command")
    for stream in ("stdout", "stderr"):
        contents = referenced(record[stream])
        digest = raw.get(stream + "_sha256")
        if digest is None and isinstance(raw.get(stream), dict):
            digest = raw[stream]["sha256"]
        require(hashlib.sha256(contents).hexdigest() == digest,
                "Scylla original prepare stream changed")
    if "raw_child_records" in record:
        before = None
        for expected, reference in zip(record["completed_steps"], record["raw_child_records"]):
            child = referenced(reference, json_data=True)
            argv = child["argv"]
            require(Path(argv[0]).name == "cargo" and ["cargo", *argv[1:]] == expected
                    and child["exit_status"] == 0 and child.get("stop_reason") is None,
                    "Scylla recorded Cargo child did not execute the original step")
            require(child["lock_before_sha256"] == before,
                    "Scylla intermediate lock chain is discontinuous")
            before = child["lock_after_sha256"]
        require(len(record["raw_child_records"]) == 3 and before == lock_sha256,
                "Scylla final lock does not follow both original precise updates")
    return raw


def prepared_original_sequence(envelope, profile):
    """Read a parent-sealed, raw-record-bound view of the original sequence."""
    from .rust_first_resolution import (
        BINDINGS, graph_summary, referenced, reference_packages, require, CRATES_IO,
    )
    require(envelope["task_id"] == TASK and profile["task_id"] == TASK
            and profile["prepare_command"] == PREPARE, "unknown Scylla original prepare profile")
    root = Path(envelope["root"])
    require(root.is_absolute() and root.is_dir(), "Scylla input root is absent")

    def read(name, *, json_data=True):
        name = repo_path(name)
        require(name in envelope["files"], "unsealed Scylla preparation input: " + name)
        return referenced({"path": str(root / name), **envelope["files"][name]}, json_data=json_data)

    view = read("original-prepare-admission.json")
    require(view["schema_version"] == SCHEMA and view["task_id"] == TASK
            and view["historical_lock_restoration"] is False
            and view["test_outcome_based_selection"] is False
            and view["original_prepare_successes"] == 1 and view["new_model_or_SUT_calls"] == 0,
            "Scylla original first-success prepare admission changed")
    decision = read("first-prepare-decision.json")
    projection = read("original-source-projection.json")
    require(decision["task_id"] == projection["task_id"] == TASK
            and decision["original_prepare_command_verbatim"] == PREPARE
            and decision["reference_prepare_successes_max"] == 1
            and decision["required_pins"] == PINS
            and decision["historical_lock_restoration"] is False
            and decision["test_outcome_based_selection"] is False,
            "Scylla original first-prepare policy changed")
    require(decision["source_projection"]["sha256"]
            == envelope["files"]["original-source-projection.json"]["sha256"]
            and decision["original_plan_sha256"] == projection["plan_sha256"] == BINDINGS[TASK]["plan_sha256"]
            and projection["original_golden"]["sha256"] == profile["golden"],
            "Scylla preparation is not bound to its original inputs")
    require(projection["complete_manifest_trees_byte_identical"] is True
            and profile["defaults"]["start"]["manifests"] == profile["defaults"]["golden"]["manifests"],
            "Scylla shared lock lacks identical complete original manifests")
    toolchain = referenced(view["toolchain_binding"], json_data=True)
    require(toolchain["version"] == "1.64.0", "Scylla execution toolchain changed")
    for binary in ("cargo", "rustc"):
        require(file_hash(Path(toolchain[binary]["path"])) == toolchain[binary]["sha256"],
                "Scylla execution compiler bytes changed")
    data = referenced(view["final_lock"])
    lock_sha256 = hashlib.sha256(data).hexdigest()
    lock = reference_packages(data)
    validate_pins(lock)
    require(isinstance(view["prepare"].get("raw_child_records"), list)
            and len(view["prepare"]["raw_child_records"]) == 3,
            "Scylla prepare lacks its three original Cargo child receipts")
    raw_prepare = prepare_record(view["prepare"], decision, lock_sha256)
    require(view["first_success"]["final_lock_sha256"] == lock_sha256
            and view["first_success"]["all_original_steps_completed"] is True
            and datetime.fromisoformat(raw_prepare["completed_at"])
            <= datetime.fromisoformat(view["first_success"]["recorded_at"]),
            "Scylla final lock was frozen before the full original sequence completed")
    locks, graph_audits, graphs = {}, {}, {}
    cache_roots = set()
    for variant in ("start", "golden"):
        stage = view["variants"][variant]
        source = Path(stage["source_path"])
        expected = projection["sources"][variant]
        actual = tree_files(source)
        require(set(actual) == set(expected["files"]) | {"Cargo.lock"},
                "Scylla preparation added or removed original source")
        for name, record in expected["files"].items():
            require(not actual[name].is_symlink() and file_hash(actual[name]) == record["sha256"]
                    and actual[name].stat().st_mode & 0o777 == record["mode"],
                    "Scylla original source bytes/modes changed")
        require(file_hash(source / "Cargo.lock") == lock_sha256, "Scylla variants do not share the frozen lock")
        metadata = referenced(stage["metadata_record"], json_data=True)
        validate_metadata_command(metadata)
        require(metadata["exit_status"] == 0 and isinstance(metadata.get("completed_at"), str)
                and metadata.get("natural_exit", True) is True
                and metadata.get("stop_reason") is None and metadata["cwd"] == str(source)
                and metadata["environment"].get("CARGO_NET_OFFLINE") == "true"
                and metadata["lock_before_sha256"] == metadata["lock_after_sha256"] == lock_sha256,
                "Scylla metadata did not validate the complete immutable offline lock")
        require(datetime.fromisoformat(view["first_success"]["recorded_at"])
                < datetime.fromisoformat(metadata["started_at"]),
                "Scylla lock was not fixed before metadata validation")
        for stream in ("stdout", "stderr"):
            contents = referenced(stage[stream])
            digest = metadata.get(stream + "_sha256")
            if digest is None and isinstance(metadata.get(stream), dict):
                digest = metadata[stream]["sha256"]
            require(hashlib.sha256(contents).hexdigest() == digest,
                    "Scylla metadata stream changed")
        stderr = referenced(stage["stderr"])
        from .rust_first_resolution import metadata_stderr_allowed
        require(metadata_stderr_allowed(stderr, virtual=True), "unaccounted Scylla metadata diagnostic")
        graph = json.loads(referenced(stage["stdout"]))
        summary = graph_summary(graph, lock, source, profile["defaults"][variant]["manifests"],
                                local_packages=4, virtual=True)
        graph_audits[variant], graphs[variant] = summary, graph
        locks[variant] = {"path": str(source / "Cargo.lock"), "sha256": lock_sha256, "data": lock}
        cache_roots.add(metadata["environment"]["CARGO_HOME"])
    require(graph_audits["start"]["semantic_graph_sha256"] == graph_audits["golden"]["semantic_graph_sha256"]
            and len(cache_roots) == 1, "Scylla shared graph is not complete and symmetric")
    inventory = referenced(view["registry_archives"], json_data=True)
    require(inventory["missing"] == [], "Scylla final registry closure is incomplete")
    from .rust_vendor import locked_packages
    expected_packages = locked_packages([lock])
    archives = {}
    for row in inventory["rows"]:
        key = row["name"], row["version"], row["package_identity"]["source"]
        require(key[2] == CRATES_IO and key not in archives
                and expected_packages.get(key) == row["checksum"]
                and row["retained"]["sha256"] == row["checksum"],
                "Scylla archive identity/checksum mismatch")
        referenced(row["retained"], limit=256 * 1024**2)
        archives[key] = row["retained"]
    require(set(archives) == set(expected_packages), "Scylla final archive union has missing/extra sources")
    return {
        "envelope": envelope, "root": root, "locks": locks, "archives": archives,
        "graph_audits": graph_audits, "graphs": graphs, "profile": profile,
        "cargo_cache": Path(next(iter(cache_roots))), "git_packages": {}, "receipt": view,
        "receipt_sha256": envelope["files"]["original-prepare-admission.json"]["sha256"],
        "cargo_sha256": toolchain["cargo"]["sha256"], "rustc_sha256": toolchain["rustc"]["sha256"],
    }
