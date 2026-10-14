"""Admit only FROST106's already-recorded, rename-only public test repairs."""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path

from reviewbench_maintainability.agentic.contracts import (
    ContractError, atomic_json, canonical_hash, file_hash,
)
from .rust_overlay import reference_inputs
from .rust_test_compat_entry import SCHEMA, SELECTION, OPERATIONS, transformed

TASK = "github__ZcashFoundation__frost__pull_106__comment_964209322__naming"
START = "d2e70b94916555ffd26356e110196ac2c17e4824"
PLAN = "5943a4a8417a21285bb4eda0a107b8de60a3b4e3b90259900b3f85d85703413b"
COMMAND = "cargo +1.64.0 test --locked -p frost-core --features test-impl --test tests -- --nocapture"
GOLDEN = "4b556d7612acce3398eca87bfc5d55c246f455f5623b46f764de1b8db2dc271c"
LOCK = "80f5989710229a536ec886835e7f53faa32494563205a81a1228dc6d14c5f2a7"
EXPECTED = ["check_share_generation_ristretto255_sha512", "check_sign_with_test_vectors",
            "common::ciphersuite::use_parameterized_types"]
ORIGINAL = {
    "frost-core/src/tests.rs": "f10b5fbad54b0ab725188149dc4c5b341088a6860f9203de47971720e9bf456f",
    "frost-core/src/tests/vectors.rs": "fead02b877f64adeed7e3e32f395d586b85701d64a0072734ff3d7d9a5461bb9",
}
RENAMED = [
    "3d56319d94604318e9b893d977958a083d8be08ed19aa96775484f463b264f92",
    "871e55559ac315bab7f15980a044be59786c79aa75b6d7dc22f86737a91ff7b3",
    "13ffea9bc4124834bb50e0f606794ac3bad08f1bb4b9ac9734a36e06ba2fdcbe",
]
RUN_HASHES = {
    "start": ["60cb005521c6a334b0bc053e0797d9f99affa471d37e1aab51f55f0f527fb7cb",
              "47e0ebaddc04cfea6af5561feec022cb01c313d381d1973998e03e8260df8752",
              "47e0ebaddc04cfea6af5561feec022cb01c313d381d1973998e03e8260df8752"],
    "golden": ["67dd25d386c04f87dd9e2ae235c5c856b0363490c774c4ea8f55edc6d653a297",
               "a6132301bea928ed2ad5507372eb1eeee734f350e9ae6e6e913bd6800fea2b78",
               "a6132301bea928ed2ad5507372eb1eeee734f350e9ae6e6e913bd6800fea2b78"],
}
TOOLS = "/opt/reviewbench-assets/rust-test-compat"
PUBLIC_SPEC = "/opt/reviewbench-assets/rust-test-compat-public.json"
GRADER_SPEC = "/opt/reviewbench-assets/rust-test-compat-grading.json"


def validate_profile(task, plan, oracle):
    from . import rust_wash_compat
    if task.get("id") == rust_wash_compat.TASK:
        return rust_wash_compat.validate_profile(task, plan, oracle)
    if (task.get("id") != TASK or task.get("repo") != "ZcashFoundation/frost"
            or task.get("start_sha") != START or oracle.get("id") != TASK
            or oracle.get("start_sha") != START or oracle.get("input_index") != 15
            or canonical_hash(plan) != PLAN or oracle.get("pass_to_pass_tests") != EXPECTED
            or oracle.get("golden_patch_sha256") != GOLDEN):
        raise ContractError("unknown or altered Rust test_compatibility_replacements profile")
    for variant in ("start", "golden"):
        environment = oracle.get("environment", {})
        if (environment.get("variant_commands", {}).get(variant) != COMMAND
                or environment.get("variant_toolchains", {}).get(variant) != "1.64.0"):
            raise ContractError("FROST compatibility changed original Cargo/toolchain")
        rows = oracle.get("test_compatibility_replacements", {}).get(variant, [])
        if len(rows) != 3:
            raise ContractError("FROST compatibility lacks all intermediate hash receipts")
        for i, row in enumerate(rows):
            operation = plan["test_compatibility_replacements"][i]
            expected = ORIGINAL[operation["path"]] if variant == "start" else RENAMED[i]
            if (row.get("path") != operation["path"] or row.get("sha256") != expected
                    or row.get("reason") != operation["reason"] or row.get("variant") != variant):
                raise ContractError("FROST original/intermediate/final helper hash changed")
        environment_rows = oracle.get("environment_replacements", {}).get(variant, [])
        if [(row.get("path"), row.get("sha256")) for row in environment_rows] != [
            ("frost-core/Cargo.toml", "24053ac637e59c22437a8047778295814af3bde68926506ee9a9944581e14979"),
            ("Cargo.lock", LOCK),
        ]:
            raise ContractError("FROST manifest/lock contract changed")


def specification(ctx, task, plan, oracle, patch):
    from . import rust_wash_compat
    if task.get("id") == rust_wash_compat.TASK:
        return rust_wash_compat.specification(ctx, task, plan, oracle, patch)
    validate_profile(task, plan, oracle)
    if patch["sha256"] != GOLDEN or file_hash(Path(patch["path"])) != GOLDEN:
        raise ContractError("FROST original source-only golden changed")
    inputs = reference_inputs(ctx, task, patch, ORIGINAL)
    chain, current = [], dict(inputs["start"])
    for name, digest in ORIGINAL.items():
        if (current[name] is None or current[name] != inputs["golden"][name]
                or hashlib.sha256(current[name]).hexdigest() != digest):
            raise ContractError("FROST golden changed preserved public test-helper content")
    for index, operation in enumerate(OPERATIONS):
        path, old, new = operation
        before = hashlib.sha256(current[path]).hexdigest()
        if current[path].count(old.encode()) != 1:
            raise ContractError("FROST rename occurrence count changed")
        current[path] = current[path].replace(old.encode(), new.encode(), 1)
        after = hashlib.sha256(current[path]).hexdigest()
        if after != RENAMED[index]:
            raise ContractError("FROST rename changed an assertion or unrecorded source byte")
        chain.append({"path": path, "old": old, "new": new, "before": before, "after": after})
    logs = []
    for variant in ("start", "golden"):
        rows = oracle.get("runs", {}).get(variant, [])
        if len(rows) != 3:
            raise ContractError("FROST requires three original runs per input")
        for index, row in enumerate(rows):
            path = ctx.legacy / "rust" / row["log"]
            if (file_hash(path) != RUN_HASHES[variant][index] or row.get("run") != index + 1
                    or row.get("passed_tests") != EXPECTED or row.get("passed") != 3
                    or row.get("failed") != 0 or row.get("ok") is not True):
                raise ContractError("FROST original three-test run evidence changed")
            logs.append({"path": str(path), "sha256": file_hash(path)})
    result = {"schema_version": SCHEMA, "profile": "frost106-public-helper-renames-v1",
              "task_id": TASK, "toolchain": "1.64.0", "selection": SELECTION,
              "files": [{"path": name, "sha256": digest} for name, digest in ORIGINAL.items()],
              "chain": chain, "cpus": 4, "timeout_seconds": plan["timeout_seconds"],
              "max_log_bytes": 32 * 1024**2}
    # These are the two complete forms in the original contract, not newly
    # invented combinations of intermediate replacements.
    transformed(inputs["start"], result, "renamed")
    atomic_json(ctx.output / "rust-test-compat-admission" / (canonical_hash(result) + ".json"), {
        "public_spec": result, "plan_sha256": PLAN, "oracle_sha256": canonical_hash(oracle),
        "golden_sha256": GOLDEN, "lock_sha256": LOCK, "original_runs": logs,
        "runtime_selection_inputs": "complete current candidate and compile-only diagnostics",
    })
    return result


def binding(ctx, spec, execute, public):
    from . import rust_wash_compat
    if spec.get("profile") == rust_wash_compat.entry.PROFILE:
        return rust_wash_compat.binding(ctx, spec, execute, public)
    directory = ctx.output / "rust-test-compat-tools"
    directory.mkdir(parents=True, exist_ok=True)
    import shutil
    for name in ("rust_test_compat_entry.py", "rust_api_entry.py"):
        shutil.copyfile(Path(__file__).with_name(name), directory / name)
    tool = ctx.bundle(directory, role="toolchain")
    assets = [{**tool, "kind": "archive", "visibility": "developer", "target": TOOLS}]
    commands = []
    for mode, command, target in (("public", public, PUBLIC_SPEC), ("grading", execute, GRADER_SPEC)):
        value = copy.deepcopy(spec) | {"mode": mode, "command": command}
        if mode == "grading":
            value["expected_tests"] = list(EXPECTED)
        path = ctx.output / "rust-test-compat-specs" / (canonical_hash(value) + ".json")
        atomic_json(path, value)
        assets.append({**ctx.copy_asset(path, "rust-test-compat-spec"), "kind": "file",
                       "visibility": "developer" if mode == "public" else "grader", "target": target})
        commands.append(["python3", TOOLS + "/rust_test_compat_entry.py", "--spec", target])
    return commands[1], commands[0], assets
