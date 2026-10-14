"""Recover a recorded dependency resolution without changing candidate source.

Alpen's frozen reference patch adds a workspace dependency absent from the start
lock. Its recorded successful golden run resolved that dependency online. The
original Oracle names the log, but does NOT declare a lockfile digest: the log
and retained post-run lock hashes below are recovery pins, not original Oracle
fields. Their exact package delta, accepted patch and unchanged test input are
checked before exposing this build-only default and its vendor union.
"""
from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path

from reviewbench_maintainability.agentic.contracts import ContractError, atomic_json, file_hash, repo_path

TASK_ID = "github__alpenlabs__alpen__pull_568__comment_1899653893__modularity"
ARTIFACTS = "artifacts/018-alpenlabs__alpen-pr568-c1899653893"
MANIFEST = "bin/datatool/Cargo.toml"
SESSION = "crates/storage/tests/oracle_l2.rs"
TEST_NAME = "l2_manager_blocking_height_query_on_fresh_db"
CRATES_IO = "registry+https://github.com/rust-lang/crates.io-index"
PINS = {
    "start_sha": "b690acaaa92561bed7a9a4a0981931b428d67af5",
    "plan": "8d257dca0623d42c7695d62bac156e7667ae838389cd85ac6c465a5f85f2c4a8",
    "patch": "6ae5f8acb4add95314583ce0ac81028701a027957f31f65b47c3c71939037efb",
    "session": "1478c16bb83484cb8f712763250d9a8f221ccee2f9ba41b2a1e9731eb6a67392",
    "start_manifest": "90ae7da5665a9f2db1b5d86dd8670b5f30ef04880413df82360a554b95bc33c3",
    "resolved_manifest": "9992ffdb3f9e1e5f49e6e5465df26f276bbb86ff86114dc108a5dd44ffa02047",
    "start_lock": "99b6857410da69a3af62b7c304b70679198e8d0f417ea30258b6ddebd025fb01",
    "normalized_start_lock": "3c36951f6008e8bc6594eb7c75c4221c2f9dd817811db55c3593be33cdac4ff9",
    "resolved_lock": "72aa1d39654184d9552cd92c38562948def18309096945de08b3091c02bef410",
    "resolution_log": "088888be8a33f4e5ddc8f3082db4760cf5dc4ab9ac80a4d612c56403e95b15df",
}
ADDED = {
    ("bytemuck", "1.25.2"): "95832e849adfb21180ccb6826a99da14e5d266ae5c2e668e1602cf234f153797",
    ("bytemuck_derive", "1.12.0"): "fc0e56a716f1e132ff6bf4bdac1c944a3fcdc1cae65f70a4a2a1ac3b401d2d1f",
    ("syn", "3.0.3"): "53e9bae58849f64dfa4f5d5ae372c8341f7305f82a3868709269343628b659a3",
}
REMOVED = {("bytemuck", "1.18.0"), ("bytemuck_derive", "1.7.0")}
RESOLUTION_LINES = (
    "Updating bytemuck v1.18.0 -> v1.25.2",
    "Updating bytemuck_derive v1.7.0 -> v1.12.0",
    "Adding syn v3.0.3",
)


def validate_resolution_delta(before, after):
    from .rust_lane import external_lock_packages
    old, new = external_lock_packages(before), external_lock_packages(after)
    added = {key: value for key, value in new.items() if key not in old}
    removed = set(old) - set(new)
    expected = {(name, version, CRATES_IO): checksum for (name, version), checksum in ADDED.items()}
    if (added != expected or removed != {(name, version, CRATES_IO) for name, version in REMOVED}
            or any(old[key] != new[key] for key in set(old) & set(new))):
        raise ContractError("retained Cargo resolution differs from its recorded external package delta")
    return [{"name": key[0], "version": key[1], "source": key[2], "checksum": checksum}
            for key, checksum in sorted(added.items())]


def add_recorded_resolution(ctx, task, plan, oracle, patch, spec, defaults):
    if task["id"] != TASK_ID:
        return
    from .rust_overlay import reference_inputs, session_bytes
    if (task.get("repo") != "alpenlabs/alpen" or task["start_sha"] != PINS["start_sha"]
            or oracle.get("golden_patch_sha256") != PINS["patch"]
            or file_hash(Path(patch["path"])) != PINS["patch"]
            or patch.get("sha256") != PINS["patch"]):
        raise ContractError("Alpen resolution is not bound to the frozen start and accepted patch")
    plan_file = ctx.legacy / "rust/plans.d/18.json"
    if file_hash(plan_file) != PINS["plan"]:
        raise ContractError("Alpen resolution plan changed")
    if plan.get("test_compatibility_replacements") or any(
            oracle.get("test_compatibility_replacements", {}).get(variant) for variant in ("start", "golden")):
        raise ContractError("recorded Alpen resolution must retain the unadapted frozen assertions")
    entries = [entry for entry in plan.get("session_files", []) if entry["path"] == SESSION]
    if (len(entries) != 1 or hashlib.sha256(session_bytes(ctx, plan, oracle, entries[0])).hexdigest() != PINS["session"]
            or oracle.get("pass_to_pass_tests") != [TEST_NAME]):
        raise ContractError("Alpen resolution has different mandatory test inputs")
    log_name = ARTIFACTS + "/logs/golden-run1-attempt1.log"
    runs = [run for run in oracle.get("runs", {}).get("golden", []) if run.get("log") == log_name]
    if (len(runs) != 1 or runs[0].get("ok") is not True or runs[0].get("passed_tests") != [TEST_NAME]
            or runs[0].get("passed") != 1 or runs[0].get("failed") != 0
            or runs[0].get("failed_tests") or not runs[0].get("attempts")
            or runs[0]["attempts"][-1].get("exit_code") != 0
            or runs[0]["attempts"][-1].get("log") != log_name
            or runs[0]["attempts"][-1].get("timed_out") is not False):
        raise ContractError("Alpen resolution lacks its frozen successful run record")
    log = ctx.legacy / "rust" / repo_path(log_name)
    if file_hash(log) != PINS["resolution_log"] or not all(line in log.read_text() for line in RESOLUTION_LINES):
        raise ContractError("Alpen recorded dependency-resolution log changed")
    inputs = reference_inputs(ctx, task, patch, [MANIFEST, "Cargo.lock"])
    for variant, pin in (("start", "start_manifest"), ("golden", "resolved_manifest")):
        data = inputs[variant][MANIFEST]
        if data is None or hashlib.sha256(data).hexdigest() != PINS[pin]:
            raise ContractError("Alpen dependency manifest differs from its accepted reference inputs")
    original = inputs["start"]["Cargo.lock"]
    if (original is None or hashlib.sha256(original).hexdigest() != PINS["start_lock"]
            or inputs["golden"]["Cargo.lock"] != original):
        raise ContractError("Alpen resolution is not a default for the unchanged reference lock")
    candidate_value = tomllib.loads(inputs["golden"][MANIFEST].decode())["dependencies"]["bytemuck"]
    if candidate_value != {"version": "1.21.0", "optional": True}:
        raise ContractError("Alpen resolution has a different public dependency selector")
    source = ctx.legacy / "rust" / ARTIFACTS / "golden/Cargo.lock"
    if file_hash(source) != PINS["resolved_lock"]:
        raise ContractError("retained Alpen resolution lock changed")
    added = validate_resolution_delta(tomllib.loads(original.decode()), tomllib.loads(source.read_text()))
    if "Cargo.lock" in defaults or any(record["path"] == "Cargo.lock" for record in spec["locks"]):
        raise ContractError("recorded Alpen resolution conflicts with another lock default")
    if defaults or spec["locks"]:
        raise ContractError("Alpen resolution needs an unambiguous lock asset target")
    asset = ctx.copy_asset(source, "rust-recorded-locks")
    if asset["sha256"] != PINS["resolved_lock"]:
        raise ContractError("copied Alpen resolution lock differs from its recovery pin")
    defaults["Cargo.lock"] = asset
    record = {
        "path": "Cargo.lock", "asset_target": "/opt/reviewbench-assets/rust-lock-0",
        "sha256": asset["sha256"], "allow_missing": False,
        "replace_if_sha256": [PINS["start_lock"], PINS["normalized_start_lock"], PINS["resolved_lock"]],
        "when": [{"path": MANIFEST, "key": ["dependencies", "bytemuck"], "value": candidate_value}],
    }
    spec["locks"].append(record)
    atomic_json(ctx.output / "rust-resolution-audits" / (PINS["resolved_lock"] + ".json"), {
        "task": task["id"], "basis": "frozen successful run record plus retained resolution log/lock",
        "oracle_declares_lock_sha256": False, "recovery_pins": PINS,
        "log": str(log), "lock": str(source), "selector": record["when"],
        "additional_registry_packages": added, "session_sha256": PINS["session"],
        "production_changes": [], "test_changes": [], "scoring_result": False,
    })
