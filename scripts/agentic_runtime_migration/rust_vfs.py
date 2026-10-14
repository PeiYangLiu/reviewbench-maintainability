"""Exact VFS61 integration-harness admission; no production/build-lock rewrite."""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import shutil

from reviewbench_maintainability.agentic.contracts import (
    ContractError, atomic_json, canonical_hash, file_hash,
)
from . import rust_vfs_entry as entry
from .rust_overlay import session_bytes

TASK = entry.TASK
PROFILE = entry.PROFILE
PLAN = entry.PLAN
START = "53b09441738c7a47bc08cb543605b9fbb1265418"
GOLDEN = "15cd4e4c4b9596b47c3136964857a19afaca30ecec102d977e581f06053aa868"
ORIGINAL = "28bf4adafd969a1254dc0b13e5ea8799bc1efa14e22f1b9fd21c0a809167da97"
ARTIFACT = "artifacts/061-manuel-woelker__rust-vfs-pr54-c1341909533"
TOOLS = "/opt/reviewbench-assets/rust-vfs-tools"
SPEC = "/opt/reviewbench-assets/rust-vfs-spec.json"
LOGS = (
    "ed9637d3d255bcae0d45b808f21d4c7cf7d935c239fca64afb7b6d90da9cbf8e",
    "9dea23f3e6b84cbf07c8edfe9fd7a8c19f855259d5693774868863f8330de224",
    "701973921cf3df39f9788645a9d90d969e61d516e58e12ee4fb46d20f9b13550",
    "229cb49d1a4470a299256336edf1ceab6fa84ef92c3ec86f42fbc7a6d965281e",
    "1f7994e2c1fe925e66e2f7691047e94b2d233eb9f6bc696771765f183af4405f",
    "c2db30eac280a699221e54787dcea9b4dce02ae7e4814d363d1a5ae0ba1ed7a3",
)


def applies(task, plan, oracle):
    return (task.get("id") == TASK or oracle.get("id") == TASK
            or plan.get("input_index") == 61)


def specification(ctx, task, plan, oracle):
    import json
    if (task.get("id") != TASK or task.get("repo") != "manuel-woelker/rust-vfs"
            or task.get("start_sha") != START or oracle.get("id") != TASK
            or oracle.get("start_sha") != START or oracle.get("input_index") != 61
            or canonical_hash(plan) != PLAN or oracle.get("golden_patch_sha256") != GOLDEN
            or oracle.get("pass_to_pass_tests") != list(entry.EXPECTED)):
        raise ContractError("unknown or altered VFS61 original API profile")
    original = ctx.legacy / "rust" / ARTIFACT / "result.json"
    patch = original.parent / "source-only-golden.patch"
    if file_hash(original) != ORIGINAL or file_hash(patch) != GOLDEN:
        raise ContractError("VFS61 original Oracle/golden receipt changed")
    if {k: v for k, v in oracle.items() if k != "lane_results_source"} != json.loads(original.read_text()):
        raise ContractError("VFS61 executable Oracle differs from its original record")
    files = []
    contents = []
    for row in plan["session_files"]:
        data = session_bytes(ctx, plan, oracle, row)
        if hashlib.sha256(data).hexdigest() != entry.HARNESSES.get(row["path"]):
            raise ContractError("VFS61 original harness/assertions changed")
        files.append({"path": row["path"], "sha256": entry.HARNESSES[row["path"]]})
        contents.append(data)
    if (len(contents) != 2 or contents[0].replace(b"MemoryFS", b"AsyncMemoryFS").replace(
            b"VfsPath", b"AsyncVfsPath") != contents[1]):
        raise ContractError("VFS61 forms differ beyond the two original public API names")
    for index, (variant, run) in enumerate((v, n) for v in ("start", "golden") for n in (1, 2, 3)):
        record = oracle["runs"][variant][run - 1]
        name = ARTIFACT + f"/logs/{variant}-run{run}-attempt1.log"
        if (record["log"] != name or file_hash(ctx.legacy / "rust" / name) != LOGS[index]
                or record["passed_tests"] != list(entry.EXPECTED)
                or record["failed_tests"] or any(a["exit_code"] != 0 for a in record["attempts"])):
            raise ContractError("VFS61 original six-run evidence changed")
    spec = {
        "schema_version": entry.SCHEMA, "profile": PROFILE, "task_id": TASK,
        "plan_sha256": PLAN, "toolchain": "1.93.0", "workdir": ".",
        "files": files, "expected_tests": list(entry.EXPECTED),
        "forms": [{"name": name, "target": name, "harness": f"tests/{name}.rs",
                   "arguments": entry.arguments(name)} for name in entry.NAMES],
        "max_log_bytes": 32 * 1024**2, "cpus": 4, "timeout_seconds": 1800,
        "selection": entry.guard.SELECTION,
    }
    atomic_json(ctx.output / "rust-vfs-admission" / (canonical_hash(spec) + ".json"), spec)
    return spec


def binding(ctx, spec, cargo_prefix):
    # Reuse the existing pinned Python-3.10 TOML toolkit. Do not depend on an
    # incidental shared mount: the VFS tool archive contains its own copy.
    from .rust_overlay import build_tools
    value = copy.deepcopy(spec)
    for form in value["forms"]:
        form["command"] = ["cargo", *cargo_prefix, *form.pop("arguments")]
    entry.validate(value)
    build_tools(ctx)
    root = ctx.output / "rust-vfs-tools"
    shutil.copytree(ctx.output / "rust-build-tools", root, dirs_exist_ok=True)
    for name in ("rust_vfs_entry.py", "rust_test_compat_entry.py", "rust_api_entry.py"):
        shutil.copyfile(Path(__file__).with_name(name), root / name)
    tools = ctx.bundle(root, role="grader")
    path = ctx.output / "rust-vfs-specs" / (canonical_hash(value) + ".json")
    atomic_json(path, value)
    assets = [
        {**tools, "kind": "archive", "visibility": "grader", "target": TOOLS},
        {**ctx.copy_asset(path, "rust-vfs-spec"), "kind": "file",
         "visibility": "grader", "target": SPEC},
    ]
    return ["python3", TOOLS + "/rust_vfs_entry.py", "--spec", SPEC], assets
