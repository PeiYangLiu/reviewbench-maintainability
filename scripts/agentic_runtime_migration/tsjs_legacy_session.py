"""Original legacy session-tool execution with complete current source checkouts."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shlex

from reviewbench_maintainability.agentic.assets import asset_descriptor
from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, atomic_json, canonical_hash, component, file_hash, repo_path,
)
from reviewbench_maintainability.agentic.snapshots import git
from .tsjs_yarn1 import test_pattern

DISCOURSE = "github__discourse__discourse__pull_37336__comment_2752215814__complexity"
NODE = "/opt/reviewbench-assets/node"
DEPENDENCIES = "/opt/reviewbench-assets/legacy-session-node-modules"
HARNESS = "/opt/reviewbench-assets/legacy-session-tests"
COMMAND = ["node", "/workspace/node_modules/vitest/vitest.mjs", "run",
           "--config", "session/vitest.config.js", "--coverage"]


def read_original_inputs(task, oracle, pointer):
    if not isinstance(pointer, dict) or set(pointer) != {"path", "sha256"}:
        raise ContractError("legacy JS needs an explicit hash-bound original input record")
    path = Path(pointer["path"])
    if (not path.is_absolute() or path.is_symlink() or not path.is_file()
            or file_hash(path) != pointer["sha256"]):
        raise ContractError("legacy JS original input record changed")
    record = json.loads(path.read_text())
    if (record.get("schema_version") != "reviewbench-original-legacy-js-inputs/v1"
            or record.get("status") != "original_runtime_inputs_ready_without_reinstall"
            or record.get("id") != task["id"] or task["id"] != DISCOURSE
            or record.get("full_scope") != 402
            or record.get("task_fingerprint") != canonical_hash(task)
            or record.get("oracle_fingerprint") != canonical_hash(oracle)
            or record.get("node_version") != oracle.get("historical_node")
            or record.get("node_version") != "20.19.5"
            or record.get("signature") != "vitest:files=1,tests=2"
            or record.get("effective_test_argv") != COMMAND
            or record.get("new_install_calibration_model_judge_calls") != 0):
        raise ContractError("legacy JS original task/test/tool bindings differ")
    rows = record.get("original_logs", [])
    if [(row["phase"], row["run"]) for row in rows] != [
            (phase, number) for phase in ("start", "golden") for number in (1, 2, 3)]:
        raise ContractError("legacy JS original six replay identities are incomplete")
    for row in rows:
        log = Path(row["path"])
        if (not log.is_file() or log.is_symlink() or file_hash(log) != row["sha256"]
                or not re.search(test_pattern(record["signature"]), log.read_text(), re.M)):
            raise ContractError("legacy JS original replay output changed")
    private = record["private_files"]
    if [row["destination"] for row in private] != [
            "session/checklist.behavior.test.js", "session/vitest.config.js"]:
        raise ContractError("legacy JS original private fixture/config mapping changed")
    for row in private:
        source = Path(row["path"])
        if not source.is_file() or source.is_symlink() or file_hash(source) != row["sha256"]:
            raise ContractError("legacy JS original private fixture/config bytes changed")
    return record


def migrate(ctx, task, oracle, pointer):
    record = read_original_inputs(task, oracle, pointer)
    for name, role in (("node", "toolchain"), ("dependencies", "dependency")):
        if asset_descriptor(record[name], ctx.output)["source_kind"] != role:
            raise ContractError("legacy JS public asset role differs")
    reference = record["source_only_reference"]
    path = Path(reference["path"])
    if not path.is_file() or path.is_symlink() or file_hash(path) != reference["sha256"]:
        raise ContractError("legacy JS original source-only reference changed")
    restored = [repo_path(name) for name in record["restored_tests"]]
    args = ["diff", "--binary", "--full-index", task["start_sha"], task["target_sha"], "--", "."]
    args += [":(exclude)" + name for name in restored]
    expected = git(ctx.repo(task), *args)
    if path.read_bytes() != expected:
        raise ContractError("legacy JS complete original source-only reference differs")
    from .tsjs_lane import IMAGE, node_worker_environment
    image, audit = ctx.image(IMAGE)
    assets = [
        {**record[name], "kind": "archive", "visibility": "developer", "target": target}
        for name, target in (("node", NODE), ("dependencies", DEPENDENCIES))
    ]
    grade = []
    for row in record["private_files"]:
        destination = "/workspace/" + repo_path(row["destination"])
        target = HARNESS + "/" + row["destination"]
        copied = ctx.copy_asset(Path(row["path"]), "legacy-session-tests")
        assets.append({**copied, "kind": "file", "visibility": "grader", "target": target})
        grade.append("mkdir -p " + shlex.quote(str(Path(destination).parent)) + " && cp "
                     + shlex.quote(target) + " " + shlex.quote(destination))
    names = {name.decode() for name in git(ctx.repo(task), "ls-tree", "-r", "--name-only", "-z",
                                          task["start_sha"]).split(b"\0") if name}
    # Original Git tests and fixed injected assets have different sources.
    # Do not claim a session-only fixture existed in the Git start. Its exact
    # grader-owned copy still overwrites any candidate-created file before tests.
    immutable = set(ctx.immutable_tests(task))
    immutable.update(name for name in names if re.search(
        r"(^|/)(spec|tests?|fixtures|__tests__|__snapshots__)(/|$)|\.(spec|test)\.[cm]?[jt]sx?$|_spec\.rb$", name))
    if not immutable <= names:
        raise ContractError("legacy JS original immutable test list contains a non-Git path")
    for scratch in ("node_modules", "coverage"):
        if any(name == scratch or name.startswith(scratch + "/") for name in names):
            raise ContractError("legacy JS scratch overlaps original tracked source")
    command = shlex.join(COMMAND)
    recipe = RuntimeRecipe(
        task_id=task["id"], repo=task["repo"], start_sha=task["start_sha"], lane="tsjs",
        image=image, image_audit=audit, cpus=2, memory_mb=2048, build_scratch_mb=1024,
        command_timeout_seconds=600, workdir="/workspace", assets=assets,
        submodules=ctx.modules(task), scratch_paths=["node_modules", "coverage"],
        env={"PATH": NODE + "/bin:/workspace/node_modules/.bin:/usr/local/bin:/usr/bin:/bin",
             "NODE_PATH": "/workspace/node_modules", "CI": "true", "HUSKY": "0",
             "HOME": "/workspace/.reviewbench/home", "TMPDIR": "/workspace/.reviewbench/tmp",
             "NODE_OPTIONS": "--max-old-space-size=1536", "FORCE_COLOR": "0",
             "COREPACK_ENABLE_NETWORK": "0", "COREPACK_ENABLE_PROJECT_SPEC": "0",
             **node_worker_environment(2)},
        bootstrap=[
            "mkdir -p /workspace/.reviewbench/tmp /workspace/.reviewbench/home",
            'test "$(node --version)" = v20.19.5',
            "mkdir -p /workspace/node_modules && cp -a " + DEPENDENCIES + "/. /workspace/node_modules",
        ],
        dev_checks=["node --input-type=module --check < " + shlex.quote("/workspace/" + task["locality"]["path"])],
        grading={"multifile_build": True, "immutable_paths": sorted(immutable),
                 "golden_apply_ignore_space_change": True, "golden_patch": reference,
                 "bootstrap": grade, "commands": [{
                     "command": command, "parser": "regex",
                     "expected_tests": [task["id"] + "::frozen-" + record["signature"]],
                     "success_pattern": test_pattern(record["signature"]),
                 }]},
    )
    recipe.validate()
    atomic_json(ctx.output / "legacy-session-bindings" / (component(task["id"]) + ".json"), {
        "id": task["id"], "original_inputs": pointer, "original_test_argv": COMMAND,
        "runtime_fingerprint": recipe.fingerprint, "original_signature": record["signature"],
        "private_test_bytes_preserved": True, "current_complete_source_used": True,
        "original_session_tool_installation_reused": True, "new_installs_model_judge_calls": 0,
        "retained_new_test_absences": record["original_retained_new_test_absences"],
    })
    return recipe
