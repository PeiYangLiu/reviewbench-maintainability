"""Pinned Yarn1 runtime with original private assertions and no-reinstall semantics."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shlex
import tempfile

from reviewbench_maintainability.agentic.assets import asset_descriptor
from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, atomic_json, canonical_hash, component, file_hash, repo_path,
)
from reviewbench_maintainability.agentic.snapshots import git
from .node_yarn_prepare import NODE, MANAGER, SCHEMA as PREPARATION_SCHEMA, validate_case
from .tsjs_pnpm import bind_oracle, jest_pattern
from .tsjs_yarn1_transport import SOURCE, original_contract

PAYLOAD = "/opt/reviewbench-assets/yarn1-dependencies"
TRANSPORT = "/opt/reviewbench-assets/yarn1-original-transport"
HARNESS = "/opt/reviewbench-assets/yarn1-private-tests"


def checked(pointer, label):
    if not isinstance(pointer, dict) or set(pointer) != {"path", "sha256"}:
        raise ContractError("Yarn1 needs a hash-bound original input: " + label)
    path = Path(pointer["path"])
    if (not path.is_absolute() or path.is_symlink() or not path.is_file()
            or file_hash(path) != pointer["sha256"]):
        raise ContractError("Yarn1 original input reference changed: " + label)
    return json.loads(path.read_text())


def test_command(case):
    validate_case(case)
    return shlex.join(["node", MANAGER + "/bin/yarn.js", *case["test"][2:]])


def test_pattern(signature):
    if signature.startswith("jest:"):
        return jest_pattern(signature)
    match = re.fullmatch(r"vitest:files=(\d+),tests=(\d+)", signature)
    if not match or min(map(int, match.groups())) < 1:
        raise ContractError("Yarn1 original complete test signature is unsupported")
    files, tests = match.groups()
    space = r"(?:[ \t\r]|\x1b\[[0-9;]*m)"
    prefix = rf"^(?:\[log\]{space}*)?{space}*"
    rows = [rf"{prefix}Test{space}+Files{space}+{files}{space}+passed{space}*\({files}\){space}*$",
            rf"{prefix}Tests{space}+{tests}{space}+passed{space}*\({tests}\){space}*$"]
    return "(?s)" + "".join("(?=.*" + row + ")" for row in rows) + ".*"


def source_names(ctx, task):
    # Without -z Git C-quotes non-ASCII paths. Those display strings are not
    # repository paths and must never enter the immutable-test inventory.
    values = git(ctx.repo(task), "ls-tree", "-r", "--name-only", "-z", task["start_sha"])
    return [repo_path(name.decode("utf-8")) for name in values.split(b"\0") if name]


def read_descriptor(ctx, task, oracle, case, pointer):
    validate_case(case)
    logs = bind_oracle(case, oracle)
    record = checked(pointer, "prepared original dependencies")
    if (record.get("schema_version") != PREPARATION_SCHEMA
            or record.get("status") != "prepared_not_calibrated"
            or record.get("id") != task["id"] or record.get("start_sha") != task["start_sha"]
            or record.get("case_fingerprint") != canonical_hash(case)
            or record.get("node_version") != case["node"] or record.get("yarn_version") != "1.22.22"
            or record.get("workspace_production_files_exported") != 0
            or record.get("source_manifest_or_lock_rewritten") is not False
            or record.get("source_files_unchanged") is not True
            or record.get("install", {}).get("exit_status") != 0
            or record["install"].get("original_install_argv") != case["install"]):
        raise ContractError("Yarn1 dependencies do not bind the successful original unchanged install")
    if [row["name"] for row in record["original_logs"]] != logs:
        raise ContractError("Yarn1 dependency record lost an original calibration log")
    pattern = test_pattern(oracle["start_signature"])
    if oracle["golden_signature"] != oracle["start_signature"]:
        raise ContractError("Yarn1 original start/golden test signature differs")
    for row in record["original_logs"]:
        path = ctx.legacy / "tsjs" / oracle["recovery_artifact"] / row["name"]
        if (not path.is_file() or path.is_symlink() or file_hash(path) != row["sha256"]
                or not re.search(pattern, path.read_text(), re.M)):
            raise ContractError("Yarn1 original calibration evidence changed")
    for name, role in (("node", "toolchain"), ("manager", "toolchain"), ("dependencies", "dependency")):
        if asset_descriptor(record[name], ctx.output)["source_kind"] != role:
            raise ContractError("Yarn1 asset role differs from the original dependency preparation")
    proof = record["dependency_readback"]
    descriptor = asset_descriptor(record["dependencies"], ctx.output)
    if (proof.get("status") != "independent_Blob_bytes_modes_links_crc_verified"
            or proof.get("bytes") != descriptor["archive"]["compressed_bytes"]
            or proof.get("files") != descriptor["files"]
            or len(proof.get("receipts", [])) != len(descriptor["archive"]["parts"])):
        raise ContractError("Yarn1 dependencies lack their complete independent Blob readback")
    for receipt, part in zip(proof["receipts"], descriptor["archive"]["parts"]):
        if any(receipt.get(key) != part[key] for key in ("sha256", "size_bytes", "md5_base64")):
            raise ContractError("Yarn1 readback does not bind the published dependency bytes")
    return record


def migrate(ctx, task, oracle, case, pointer):
    record = read_descriptor(ctx, task, oracle, case, pointer)
    files = {name: git(ctx.repo(task), "show", task["start_sha"] + ":" + repo_path(name))
             for name in record["input_hashes"]}
    contract = original_contract(record, files)
    inject = case.get("session_files", [])
    original_private = oracle.get("session_files", [])
    if inject != [{k: row[k] for k in ("source", "destination")} for row in original_private]:
        raise ContractError("Yarn1 original private test mapping changed")
    for row in original_private:
        path = ctx.legacy / "tsjs" / repo_path(row["source"])
        if path.is_symlink() or not path.is_file() or file_hash(path) != row["sha256"]:
            raise ContractError("Yarn1 original private assertion bytes changed")
    from .tsjs_lane import IMAGE, node_worker_environment, reference_patch, typescript_public_check
    image, audit = ctx.image(IMAGE)
    with tempfile.TemporaryDirectory(prefix="yarn1-original-transport-", dir=ctx.scratch) as temporary:
        root = Path(temporary)
        (root / "transport.cjs").write_text(SOURCE)
        atomic_json(root / "original-install.json", contract)
        transport = ctx.bundle(root, role="toolchain")
    assets = [
        {**record[name], "kind": "archive", "visibility": "developer", "target": target}
        for name, target in (("node", NODE), ("manager", MANAGER), ("dependencies", PAYLOAD))
    ] + [{**transport, "kind": "archive", "visibility": "developer", "target": TRANSPORT}]
    grade = []
    if inject:
        harness = ctx.bundle(ctx.legacy / "tsjs", [repo_path(row["source"]) for row in inject], role="grader")
        assets.append({**harness, "kind": "archive", "visibility": "grader", "target": HARNESS})
        for row in inject:
            destination = "/workspace/" + repo_path(row["destination"])
            grade.append("mkdir -p " + shlex.quote(str(Path(destination).parent)) + " && cp "
                         + shlex.quote(HARNESS + "/" + repo_path(row["source"])) + " " + shlex.quote(destination))
    names = source_names(ctx, task)
    immutable = set(ctx.immutable_tests(task)) | set(case["test_paths"])
    immutable.update(name for name in names if re.search(
        r"(^|/)(__tests__|__mocks__|__snapshots__)(/|$)|\.(spec|test)\.[cm]?[jt]sx?$|\.snap$", name))
    immutable.update(name for name in names if re.search(
        r"(^|/)(jest\.config(?:\.[^/]+)?|vitest\.config(?:\.[^/]+)?|tsconfig\.(?:test|spec)\.json)$", name))
    command = test_command(case)
    recipe = RuntimeRecipe(
        task_id=task["id"], repo=task["repo"], start_sha=task["start_sha"], lane="tsjs",
        image=image, image_audit=audit, cpus=2, memory_mb=2048, build_scratch_mb=2048,
        command_timeout_seconds=case["test_timeout"], workdir="/workspace",
        submodules=ctx.modules(task), assets=assets, scratch_paths=record["module_roots"],
        env={"PATH": NODE + "/bin:" + MANAGER + "/bin:/usr/local/bin:/usr/bin:/bin",
             "NODE_PATH": "/workspace/node_modules",
             "HOME": "/workspace/.reviewbench/home", "TMPDIR": "/workspace/.reviewbench/tmp",
             "NODE_OPTIONS": "--max-old-space-size=1536", "CI": "true", "HUSKY": "0", "FORCE_COLOR": "0",
             "COREPACK_ENABLE_NETWORK": "0", "COREPACK_ENABLE_PROJECT_SPEC": "0",
             "npm_config_offline": "true", "SOURCE_ROOT": "/workspace", **node_worker_environment(2)},
        bootstrap=[
            "mkdir -p /workspace/.reviewbench/tmp /workspace/.reviewbench/home",
            'test "$(node --version)" = v' + record["node_version"]
            + ' && test "$(node ' + MANAGER + '/bin/yarn.js --version)" = 1.22.22',
            "node " + TRANSPORT + "/transport.cjs " + PAYLOAD + " /workspace",
        ],
        dev_checks=[typescript_public_check(task["locality"]["path"])],
        grading={"multifile_build": True, "immutable_paths": sorted(immutable),
                 "golden_apply_ignore_space_change": True, "golden_patch": reference_patch(ctx, task, oracle),
                 "bootstrap": grade, "commands": [{
                     "command": command, "parser": "regex",
                     "expected_tests": oracle.get("pass_to_pass_tests") or [task["id"] + "::frozen-" + oracle["start_signature"]],
                     "success_pattern": test_pattern(oracle["start_signature"]),
                 }]},
    )
    recipe.validate()
    atomic_json(ctx.output / "yarn1-original-bindings" / (component(task["id"]) + ".json"), {
        "id": task["id"], "case_fingerprint": canonical_hash(case), "dependencies": pointer,
        "original_install_argv": case["install"], "original_test_argv": case["test"],
        "transport_test_command": command, "original_private_tests": original_private,
        "original_reinstall_golden": False, "source_manifest_or_lock_rewritten": False,
        "workspace_production_files_exported": 0, "workspace_links": record["workspace_links"],
        "runtime_fingerprint": recipe.fingerprint, "new_installs_builds_tests_model_judge_calls": 0,
    })
    return recipe
