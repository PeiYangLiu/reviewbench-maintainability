"""Attest alternative frozen Rust integration harnesses for the current API."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import shlex

from reviewbench_maintainability.agentic.contracts import (
    ContractError, atomic_json, canonical_hash, repo_path,
)
from .rust_overlay import session_bytes

ENTRY = "/opt/reviewbench-assets/rust-api-selector.py"
SPEC = "/opt/reviewbench-assets/rust-api-selection.json"
SCHEMA = "reviewbench-rust-api-selection/v1"


def _arguments(command, version):
    words = shlex.split(command)
    if words[:2] != ["cargo", "+" + version] or words[2:3] != ["test"]:
        raise ContractError("Rust API alternatives require the same explicit pinned Cargo toolchain")
    arguments = words[2:]
    cargo_options = arguments[:arguments.index("--")] if "--" in arguments else arguments
    if (cargo_options.count("--test") != 1
            or any(option in cargo_options for option in
                   ("--no-run", "--message-format", "--lib", "--bins", "--tests", "--all-targets", "--doc"))):
        raise ContractError("Rust API alternative must preserve one original integration-test target")
    at = cargo_options.index("--test")
    if at + 1 >= len(cargo_options) or not re.fullmatch(r"[A-Za-z0-9_-]+", cargo_options[at + 1]):
        raise ContractError("Rust API alternative has no literal integration-test identity")
    return arguments, at, cargo_options[at + 1]


def specification(ctx, task, plan, oracle):
    """Only a test-target change is admitted here, never a toolchain/graph guess."""
    from . import rust_vfs
    if rust_vfs.applies(task, plan, oracle):
        return rust_vfs.specification(ctx, task, plan, oracle)
    commands = {variant: plan.get(variant + "_command") or plan["command"]
                for variant in ("start", "golden")}
    if shlex.split(commands["start"]) == shlex.split(commands["golden"]):
        return None
    environment = oracle.get("environment", {})
    versions = environment.get("variant_toolchains", {})
    if any(versions.get(variant, plan["toolchain"]) != plan["toolchain"]
           for variant in ("start", "golden")):
        raise ContractError("Rust variant toolchains require a separate candidate toolchain adapter")
    forms, normalized = [], []
    files = []
    for entry in plan.get("session_files", []):
        data = session_bytes(ctx, plan, oracle, entry)
        files.append({"path": repo_path(entry["path"]), "sha256": hashlib.sha256(data).hexdigest()})
    for variant, command in commands.items():
        if shlex.split(environment.get("variant_commands", {}).get(variant, "")) != shlex.split(command):
            raise ContractError("Rust API command differs from its frozen variant evidence")
        arguments, at, target = _arguments(command, plan["toolchain"])
        shape = list(arguments)
        shape[at + 1] = "<integration-target>"
        normalized.append(shape)
        paths = [row["path"] for row in files if Path(row["path"]).suffix == ".rs"
                 and Path(row["path"]).stem == target]
        if len(paths) != 1:
            raise ContractError("Rust API target has no unique frozen integration harness")
        forms.append({"name": target, "target": target, "harness": paths[0], "arguments": arguments})
    if normalized[0] != normalized[1] or len({form["target"] for form in forms}) != 2:
        raise ContractError("Rust API alternatives need identical arguments except their harness target")
    expected = oracle.get("pass_to_pass_tests")
    if (not isinstance(expected, list) or not expected or len(set(expected)) != len(expected)
            or any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_:]+", name) for name in expected)):
        raise ContractError("Rust API alternatives need exact nonempty fixed test identities")
    spec = {
        "schema_version": SCHEMA, "task_id": task["id"], "plan_sha256": canonical_hash(plan),
        "oracle_sha256": canonical_hash(oracle), "toolchain": plan["toolchain"],
        "workdir": plan.get("workdir", "."), "forms": forms, "files": files,
        "expected_tests": expected, "max_log_bytes": 32 * 1024**2, "cpus": 4,
        "timeout_seconds": plan.get("timeout_seconds", 1800),
        "selection": "compile-only-current-candidate; execute-all-compilable-forms; conjunctive-results",
    }
    atomic_json(ctx.output / "rust-api-admission" / (canonical_hash(spec) + ".json"), spec)
    return spec


def binding(ctx, spec, cargo_prefix):
    from . import rust_vfs
    if spec.get("profile") == rust_vfs.PROFILE or spec.get("task_id") == rust_vfs.TASK:
        return rust_vfs.binding(ctx, spec, cargo_prefix)
    value = {**spec, "forms": [
        {key: item for key, item in form.items() if key != "arguments"}
        | {"command": ["cargo", *cargo_prefix, *form["arguments"]]}
        for form in spec["forms"]
    ]}
    path = ctx.output / "rust-api-specs" / (canonical_hash(value) + ".json")
    atomic_json(path, value)
    assets = [
        {**ctx.copy_asset(Path(__file__).with_name("rust_api_entry.py"), "rust-api-tools"),
         "kind": "file", "visibility": "grader", "target": ENTRY},
        {**ctx.copy_asset(path, "rust-api-spec"), "kind": "file", "visibility": "grader", "target": SPEC},
    ]
    return ["python3", ENTRY, "--spec", SPEC], assets
