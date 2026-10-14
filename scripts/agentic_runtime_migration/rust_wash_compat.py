"""Attest wash569's original inline test import and conditional dependency lock."""
from __future__ import annotations

import copy
from pathlib import Path
import shutil
import tomllib

from reviewbench_maintainability.agentic.contracts import (
    ContractError, atomic_json, canonical_hash, file_hash,
)
from . import rust_wash_compat_entry as entry
from .rust_overlay import reference_inputs

TASK = entry.TASK
START = "a9979a590fd7269a696948e88f349635d061e477"
PLAN = "c8e9d1684e159f34da58a6b1e8266e9a07ede0415be226621b7aed1cddaa3f18"
COMMAND = "cargo +1.65.0 test --locked --bin wash test_ctl_comprehensive -- --nocapture"
EXPECTED = ["ctl::test::test_ctl_comprehensive"]
GOLDEN = "cb607d5bd90cac810dcd63d228cbaf3f89bcfd653ca013a1770cad5ea1309331"
LOCKS = {
    "start": "75b58537283d9f1d13d4057c027830d2562b31dceb79738461aad41565a1014c",
    "golden": "e9ed7e27c35d6094ac018d64483f082421f811d36302ce51c8fef650de5e422f",
}
SOURCE = {
    "start": "d44b3e07649e72164d2a4bdd3228207102fbccf685c731a0d2594d4d3c70abe4",
    "golden": "e8feb7987a471341cd394e68f2d40784a0c282ca185e357728a1f6ac9768e76f",
}
REPAIRED = "391d2770549e00ac900ea2387788546004c8b160468b6a452d537c3b94f474e9"
TEST_BLOCK = {"path": entry.PATH, "bytes": 13649,
              "sha256": "0c8130556070d539323e5055046def18fe38a72e630ccfac82130825589d31b1"}
RUN_HASHES = {
    "start": ["aea49fe03c1325335964d632121a25fa84326377defb3d18934334c984641f31",
              "928b4656e5b6aa435ccd5f353a5802c4e21240f97075149a403e051618c92651",
              "928b4656e5b6aa435ccd5f353a5802c4e21240f97075149a403e051618c92651"],
    "golden": ["7a6559a11dc022095f86123a443709f49c056bbfbef652d17cf6302d4c2f1e97",
               "4000b5aa723bd70c0071cbc6a14089cb29147f1512ba8b0097e5621e82ce718f",
               "c0b2ae56817ff3fe16bd0cd2122cb0b6c25eb16bb743983bdb1872a081e62824"],
}
TOOLS = "/opt/reviewbench-assets/rust-wash-compat"
PUBLIC_SPEC = "/opt/reviewbench-assets/rust-wash-public.json"
GRADER_SPEC = "/opt/reviewbench-assets/rust-wash-grading.json"
LOCK_WHEN = [
    {"path": "Cargo.toml", "key": ["package", "name"], "value": "wash-cli"},
    {"path": "Cargo.toml", "key": ["package", "version"], "value": "0.18.0-alpha.2"},
    {"path": "crates/wash-lib/Cargo.toml", "key": ["package", "version"], "value": "0.9.0-alpha.2"},
    {"path": "Cargo.toml", "key": ["workspace", "dependencies", "wash-lib"],
     "value": {"version": "0.9.0-alpha.2", "path": "./crates/wash-lib"}},
]


def validate_profile(task, plan, oracle):
    if (task.get("id") != TASK or task.get("repo") != "wasmCloud/wash-archive"
            or task.get("start_sha") != START or oracle.get("id") != TASK
            or oracle.get("start_sha") != START or oracle.get("input_index") != 113
            or canonical_hash(plan) != PLAN or oracle.get("pass_to_pass_tests") != EXPECTED
            or oracle.get("golden_patch_sha256") != GOLDEN
            or oracle.get("toolchain") != "1.65.0"):
        raise ContractError("unknown or altered wash569 original compatibility profile")
    for variant in ("start", "golden"):
        env = oracle.get("environment", {})
        if (env.get("variant_commands", {}).get(variant) != COMMAND
                or env.get("variant_toolchains", {}).get(variant) != "1.65.0"):
            raise ContractError("wash original Cargo/toolchain/test selection changed")
        expected = SOURCE["start"] if variant == "start" else REPAIRED
        repairs = oracle.get("test_compatibility_replacements", {}).get(variant, [])
        if (len(repairs) != 1 or repairs[0].get("path") != entry.PATH
                or repairs[0].get("sha256") != expected or repairs[0].get("variant") != variant
                or repairs[0].get("reason") != plan["test_compatibility_replacements"][0]["reason"]):
            raise ContractError("wash original import-only receipt changed")
        locks = oracle.get("environment_replacements", {}).get(variant, [])
        source = "session/113/Cargo.lock." + variant
        if (len(locks) != 1 or locks[0].get("path") != "Cargo.lock"
                or locks[0].get("sha256") != LOCKS[variant] or locks[0].get("source") != source):
            raise ContractError("wash exact dependency-lock receipt changed")


def specification(ctx, task, plan, oracle, patch):
    validate_profile(task, plan, oracle)
    if patch["sha256"] != GOLDEN or file_hash(Path(patch["path"])) != GOLDEN:
        raise ContractError("wash original golden changed")
    inputs = reference_inputs(ctx, task, patch, [entry.PATH])
    spec = {"schema_version": entry.SCHEMA, "profile": entry.PROFILE, "task_id": TASK,
            "toolchain": "1.65.0", "selection": entry.SELECTION, "files": [],
            "test_block": dict(TEST_BLOCK), "cpus": 2, "timeout_seconds": plan["timeout_seconds"],
            "max_log_bytes": 32 * 1024**2}
    for variant in ("start", "golden"):
        data = inputs[variant][entry.PATH]
        if data is None or entry.guard.digest(data) != SOURCE[variant]:
            raise ContractError("wash original mixed source/test input changed")
        entry.test_block(data, spec)
        if variant == "golden":
            if entry.guard.digest(entry.transformed(data, spec, "with-import")) != REPAIRED:
                raise ContractError("wash import changed unrecorded source/assertion bytes")
    logs = []
    for variant in ("start", "golden"):
        runs = oracle.get("runs", {}).get(variant, [])
        if len(runs) != 3:
            raise ContractError("wash needs all six original runs")
        for i, row in enumerate(runs):
            path = ctx.legacy / "rust" / row["log"]
            text = path.read_text()
            if (file_hash(path) != RUN_HASHES[variant][i] or row.get("run") != i + 1
                    or row.get("passed_tests") != EXPECTED or row.get("passed") != 1
                    or row.get("failed") != 0 or row.get("ok") is not True
                    or "test " + EXPECTED[0] + " ... ok" not in text):
                raise ContractError("wash original successful command evidence changed")
            logs.append({"path": str(path), "sha256": file_hash(path)})
    atomic_json(ctx.output / "rust-wash-admission" / (canonical_hash(spec) + ".json"), {
        "public_spec": spec, "plan_sha256": PLAN, "oracle_sha256": canonical_hash(oracle),
        "golden_sha256": GOLDEN, "original_runs": logs, "original_locks": LOCKS,
        "test_assertion_tokens_preserved": 74,
        "runtime_selection": "compile-only complete current candidate; no reference production hash",
        "mixed_production_file_is_not_whole_file_immutable": True,
    })
    return spec


def build_specification(ctx, task, plan, oracle, patch):
    validate_profile(task, plan, oracle)
    if patch["sha256"] != GOLDEN or file_hash(Path(patch["path"])) != GOLDEN:
        raise ContractError("wash dependency overlay golden changed")
    inputs = reference_inputs(ctx, task, patch, ["Cargo.lock", "Cargo.toml", "crates/wash-lib/Cargo.toml"])
    for variant in ("start", "golden"):
        path = ctx.legacy / "rust/session/113" / ("Cargo.lock." + variant)
        if file_hash(path) != LOCKS[variant]:
            raise ContractError("wash original fixed lock bytes changed")
        if inputs[variant]["Cargo.lock"] is None or entry.guard.digest(inputs[variant]["Cargo.lock"]) != LOCKS["start"]:
            raise ContractError("wash original committed lock changed")
    # These are dependency/package values from the CURRENT public manifests.
    # No production-source digest or replay label participates in selection.
    for condition in LOCK_WHEN:
        values = {}
        for variant in ("start", "golden"):
            value = tomllib.loads(inputs[variant][condition["path"]].decode())
            for key in condition["key"]:
                value = value[key]
            values[variant] = value
        if values["golden"] != condition["value"]:
            raise ContractError("wash attested dependency predicate changed")
    asset = ctx.copy_asset(ctx.legacy / "rust/session/113/Cargo.lock.golden", "rust-locks")
    if asset["sha256"] != LOCKS["golden"]:
        raise ContractError("wash lock asset identity changed")
    spec = {"schema_version": "reviewbench-cargo-build-overlay/v1", "task_id": TASK,
            "manifests": [], "private_members": [], "cpus": 2, "locks": [{
                "path": "Cargo.lock", "asset_target": "/opt/reviewbench-assets/rust-lock-0",
                "sha256": LOCKS["golden"], "replace_if_sha256": sorted(LOCKS.values()),
                "allow_missing": False, "when": copy.deepcopy(LOCK_WHEN),
            }]}
    return spec, {"Cargo.lock": asset}


def binding(ctx, spec, execute, public):
    directory = ctx.output / "rust-wash-compat-tools"
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("rust_wash_compat_entry.py", "rust_test_compat_entry.py", "rust_api_entry.py"):
        shutil.copyfile(Path(__file__).with_name(name), directory / name)
    tools = ctx.bundle(directory, role="toolchain")
    assets = [{**tools, "kind": "archive", "visibility": "developer", "target": TOOLS}]
    commands = []
    for mode, command, target in (("public", public, PUBLIC_SPEC), ("grading", execute, GRADER_SPEC)):
        value = copy.deepcopy(spec) | {"mode": mode, "command": list(command)}
        if mode == "grading":
            value["expected_tests"] = list(EXPECTED)
        entry.validate(value)
        path = ctx.output / "rust-wash-specs" / (canonical_hash(value) + ".json")
        atomic_json(path, value)
        assets.append({**ctx.copy_asset(path, "rust-wash-spec"), "kind": "file",
                       "visibility": "developer" if mode == "public" else "grader", "target": target})
        commands.append(["python3", TOOLS + "/rust_wash_compat_entry.py", "--spec", target])
    return commands[1], commands[0], assets
