"""Explicit hash-bound pnpm transport; the existing npm migration is unchanged."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shlex
import tempfile

from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, atomic_json, canonical_hash, component, file_hash, repo_path,
)
from reviewbench_maintainability.agentic.snapshots import git
from .tsjs_pnpm_payload import SCHEMA, SOURCE

NODE = "/opt/reviewbench-assets/node"
MANAGER = "/opt/reviewbench-assets/pnpm-manager"
PAYLOAD = "/opt/reviewbench-assets/pnpm-dependencies"
TRANSPORT = "/opt/reviewbench-assets/pnpm-transport"


def validate_case(case):
    manager = case.get("package_manager", "")
    if not re.fullmatch(r"pnpm@\d+\.\d+\.\d+", manager):
        raise ContractError("pnpm transport requires an exact package-manager version")
    if (case.get("status") != "ready" or case.get("build") not in (None, "SKIP")
            or case.get("install_cwd") != "." or case.get("reinstall_golden") is not False
            or case.get("session_files")):
        raise ContractError("unsupported pnpm build/install/session-file case")
    if case.get("install") != ["corepack", manager, "install", "--frozen-lockfile", "--ignore-scripts"]:
        raise ContractError("pnpm original frozen-lockfile/ignore-scripts install changed")
    test = case.get("test")
    if not isinstance(test, list) or test[:3] != ["corepack", manager, "exec"]:
        raise ContractError("pnpm requires the original pinned exec argv")
    repo_path(case["workspace"], allow_root=True)
    return manager.split("@", 1)[1]


def test_command(case):
    validate_case(case)
    return shlex.join(["node", MANAGER + "/bin/pnpm.cjs", *case["test"][2:]])


def bind_oracle(case, oracle):
    if (canonical_hash(case) != oracle.get("case_sha256")
            or case["test"] != oracle.get("test_command")
            or case["install"] != oracle.get("install_command")
            or case["workspace"] != oracle.get("workspace")
            or case["node"] != oracle.get("historical_node")
            or case["package_manager"] != oracle.get("package_manager")):
        raise ContractError("pnpm case differs from the original Oracle commands/versions")
    names = []
    for key in ("start_runs", "source_only_golden_runs"):
        rows = oracle.get(key, [])
        if len(rows) != 3 or [row.get("run") for row in rows] != [1, 2, 3]:
            raise ContractError("pnpm requires the original three start and three golden records")
        for row in rows:
            if row.get("exit_code") != 0 or row.get("signature") != oracle.get("start_signature"):
                raise ContractError("pnpm original replay signature/exit changed")
            names.append(Path(repo_path(row["log"])).name)
    if len(set(names)) != 6:
        raise ContractError("pnpm original replay paths are duplicated")
    return names


def jest_pattern(signature):
    match = re.fullmatch(r"jest:suites=(\d+),tests=(\d+)", signature)
    if not match or min(map(int, match.groups())) < 1:
        raise ContractError("pnpm original complete Jest counts are absent")
    suites, tests = match.groups()
    return (r"(?s)(?=.*^Test Suites:\s*" + suites + r" passed,\s*" + suites + r" total\s*$)"
            r"(?=.*^Tests:\s*" + tests + r" passed,\s*" + tests + r" total\s*$)"
            r"(?!.*^(?:Tests|Test Suites):[^\n]*(?:failed|skipped|todo)).*")


def read_descriptor(ctx, task, oracle, case, pointer):
    logs = bind_oracle(case, oracle)
    if set(pointer) != {"path", "sha256"} or not Path(pointer["path"]).is_absolute():
        raise ContractError("pnpm opt-in needs an absolute hash-bound descriptor")
    path = Path(pointer["path"])
    if path.is_symlink() or not path.is_file() or file_hash(path) != pointer["sha256"]:
        raise ContractError("pnpm dependency descriptor bytes changed")
    record = json.loads(path.read_text())
    if (record.get("schema_version") != SCHEMA or record.get("status") != "prepared"
            or record.get("id") != task["id"] or record.get("start_sha") != task["start_sha"]
            or record.get("case_fingerprint") != canonical_hash(case)
            or record.get("node_version") != case["node"]
            or record.get("pnpm_version") != validate_case(case)
            or record.get("workspace_production_files_exported") != 0):
        raise ContractError("pnpm descriptor does not bind the fixed task/case/versions")
    if ([row.get("name") for row in record.get("original_logs", [])] != logs
            or any(not re.fullmatch(r"[0-9a-f]{64}", row.get("sha256", ""))
                   for row in record.get("original_logs", []))):
        raise ContractError("pnpm descriptor does not bind all six original replay logs")
    for name in ("node", "manager", "dependencies"):
        asset = record[name]
        source = Path(asset["path"])
        if not source.is_absolute() or source.is_symlink() or file_hash(source) != asset["sha256"]:
            raise ContractError("pnpm immutable asset descriptor changed: " + name)
    for name, digest in record["input_hashes"].items():
        data = git(ctx.repo(task), "show", task["start_sha"] + ":" + repo_path(name))
        if __import__("hashlib").sha256(data).hexdigest() != digest:
            raise ContractError("pnpm dependency input differs from exact start: " + name)
    return record


def migrate(ctx, task, oracle, case, pointer):
    from .tsjs_lane import IMAGE, node_worker_environment, reference_patch
    record = read_descriptor(ctx, task, oracle, case, pointer)
    signature = oracle.get("start_signature")
    if signature != oracle.get("golden_signature"):
        raise ContractError("pnpm original start/golden signature differs")
    pattern = jest_pattern(signature)
    directory = ctx.legacy / "tsjs" / oracle["recovery_artifact"]
    for row in record["original_logs"]:
        p = directory / row["name"]
        if not p.is_file() or p.is_symlink() or file_hash(p) != row["sha256"]:
            raise ContractError("pnpm original replay evidence changed")
    image, audit = ctx.image(IMAGE)
    with tempfile.TemporaryDirectory(prefix="pnpm-transport-", dir=ctx.scratch) as temporary:
        root = Path(temporary)
        (root / "transport.cjs").write_text(SOURCE)
        transport = ctx.bundle(root, role="dependency")
    cwd = repo_path(case["workspace"], allow_root=True)
    assets = [
        {**record[name], "kind": "archive", "visibility": "developer", "target": target}
        for name, target in (("node", NODE), ("manager", MANAGER), ("dependencies", PAYLOAD))
    ] + [{**transport, "kind": "archive", "visibility": "developer", "target": TRANSPORT}]
    bootstrap = [
        "mkdir -p /workspace/.reviewbench/tmp /workspace/.reviewbench/home",
        "test \"$(node --version)\" = " + shlex.quote("v" + record["node_version"]) +
        " && test \"$(node " + MANAGER + "/bin/pnpm.cjs --version)\" = " + shlex.quote(record["pnpm_version"]),
        "node " + TRANSPORT + "/transport.cjs " + PAYLOAD + " /workspace",
    ]
    # This is the original public suite, not an invented anchor-only syntax test.
    # The original Jest command includes --runInBand and retains all fixed flags.
    command = test_command(case)
    recipe = RuntimeRecipe(
        task_id=task["id"], repo=task["repo"], start_sha=task["start_sha"], lane="tsjs",
        image=image, image_audit=audit, cpus=2, memory_mb=2048,
        build_scratch_mb=12288, command_timeout_seconds=case.get("test_timeout") or 600,
        workdir="/workspace" + ("/" + cwd if cwd != "." else ""),
        env={"PATH": NODE + "/bin:" + MANAGER + "/bin:/usr/local/bin:/usr/bin:/bin",
             "HOME": "/workspace/.reviewbench/home", "TMPDIR": "/workspace/.reviewbench/tmp",
             "NODE_OPTIONS": "--max-old-space-size=1536", "CI": "true", "HUSKY": "0",
             "FORCE_COLOR": "0", "COREPACK_ENABLE_NETWORK": "0",
             "COREPACK_ENABLE_PROJECT_SPEC": "0", "npm_config_offline": "true",
             "SOURCE_ROOT": "/workspace", **node_worker_environment(2)},
        assets=assets, bootstrap=bootstrap, dev_checks=[command], submodules=ctx.modules(task),
        scratch_paths=record["module_roots"],
        grading={"multifile_build": True, "immutable_paths": ctx.immutable_tests(task),
                 "golden_apply_ignore_space_change": True, "golden_patch": reference_patch(ctx, task, oracle),
                 "bootstrap": [], "commands": [{
                     "command": command, "parser": "regex",
                     "expected_tests": oracle.get("pass_to_pass_tests") or [task["id"] + "::frozen-" + signature],
                     "success_pattern": pattern,
                 }]},
    )
    recipe.validate()
    atomic_json(ctx.output / "pnpm-plans" / (component(task["id"]) + ".json"), {
        "id": task["id"], "descriptor": pointer, "case_fingerprint": canonical_hash(case),
        "original_test_argv": case["test"], "transport_test_command": command,
        "original_workdir": case["workspace"], "original_install_argv": case["install"],
        "runtime_fingerprint": recipe.fingerprint, "node_version": record["node_version"],
        "pnpm_version": record["pnpm_version"], "workspace_links": record["workspace_links"],
        "workspace_production_files_exported": 0,
        "readiness": "requires original public development check and current-engine start3/golden3",
    })
    return recipe
