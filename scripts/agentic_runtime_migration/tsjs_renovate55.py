"""Original Renovate55 no-reinstall workflow with current local source packages."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shlex
import tempfile

from reviewbench_maintainability.agentic.assets import asset_descriptor
from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, atomic_json, canonical_hash, file_hash,
)
from reviewbench_maintainability.agentic.snapshots import git
from .tsjs_fixed_install import SOURCE, SCHEMA
from .tsjs_pnpm import NODE, MANAGER, PAYLOAD, bind_oracle, jest_pattern

ID = "github__renovatebot__renovate__pull_33058__comment_1882885567__testability"
START = "af464f952d90770a814f401b46718cb73c62015a"
CASE = "eaec1d9d4788e7da4b4c56537eb465dc225e00b60cc498684dab25caef38ef56"
REFERENCE = "0b508b3d2f3da5a65dd2a1bcff13baaf9eab8656077f47aa512eb3b4a439d6ce"
TRANSPORT = "/opt/reviewbench-assets/pnpm-original-fixed-install"


def checked(reference, label):
    if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
        raise ContractError("Renovate original input reference missing: " + label)
    path = Path(reference["path"])
    if (not path.is_absolute() or path.is_symlink() or not path.is_file()
            or file_hash(path) != reference["sha256"]):
        raise ContractError("Renovate original input changed: " + label)
    return json.loads(path.read_text())


def command(argv):
    if argv[:2] != ["corepack", "pnpm@9.14.4"]:
        raise ContractError("Renovate original command/manager changed")
    return shlex.join(["node", MANAGER + "/bin/pnpm.cjs", *argv[2:]])


def migrate(ctx, task, oracle, case):
    if task.get("id") != ID:
        return None
    if (task.get("repo") != "renovatebot/renovate" or task.get("start_sha") != START
            or oracle.get("index") != 55 or canonical_hash(case) != CASE
            or case.get("build") != ["corepack", "pnpm@9.14.4", "generate"]
            or case.get("reinstall_golden") is not False or oracle.get("pass_to_pass") != 16):
        raise ContractError("Renovate requires its complete original task/build/test contract")
    logs = bind_oracle(case, oracle)
    references = ctx.config.get("migration", {}).get("renovate55_original_inputs", {})
    exported = checked(references.get("dependencies"), "source-free original install")
    readback = checked(references.get("readback"), "independent dependency readback")
    inputs = checked(references.get("inputs"), "original task/tools/logs")
    if (exported.get("status") != "original_Renovate55_external_dependencies_exported"
            or exported.get("task_id") != ID or exported.get("start_sha") != START
            or exported.get("case_fingerprint") != CASE
            or exported.get("original_local_code_files_exported") != 0
            or exported.get("new_dependency_installations") != 0
            or exported.get("new_project_build_test_model_judge_calls") != 0
            or exported.get("layout", {}).get("workspace_production_files_exported") != 0):
        raise ContractError("Renovate dependency export does not bind the original source-free install")
    if (readback.get("status") != "independent_Blob_component_readback_verified"
            or readback.get("asset") != exported["dependencies"]
            or not readback.get("all_paths_bytes_full_modes_verified")
            or not readback.get("gzip_crc_and_eof_verified")):
        raise ContractError("Renovate original dependency closure has not been independently restored")
    if (inputs.get("status") != "original_Renovate55_build_inputs_and_reused_tools_Blob_verified"
            or inputs.get("task_id") != ID or inputs.get("case_fingerprint") != CASE
            or not inputs.get("original_six_logs_verified") or inputs.get("original_test_count") != 16
            or inputs.get("node") != exported["node"] or inputs.get("manager") != exported["manager"]):
        raise ContractError("Renovate original tools/logs differ from their retained evidence")
    for name, role in (("node", "toolchain"), ("manager", "toolchain"), ("dependencies", "dependency")):
        if asset_descriptor(exported[name], ctx.output)["source_kind"] != role:
            raise ContractError("Renovate public asset contains unbound source/reference material")
    directory = ctx.legacy / "tsjs" / oracle["recovery_artifact"]
    for name in logs:
        path = directory / name
        if not path.is_file() or not re.search(jest_pattern(oracle["start_signature"]), path.read_text(), re.MULTILINE):
            raise ContractError("Renovate original sixteen-test completion differs")
    from .tsjs_lane import IMAGE, node_worker_environment, reference_patch
    image, audit = ctx.image(IMAGE)
    reference = reference_patch(ctx, task, oracle)
    if reference["sha256"] != REFERENCE:
        raise ContractError("Renovate complete original source-only reference changed")
    build, test = command(case["build"]), command(case["test"])
    with tempfile.TemporaryDirectory(prefix="pnpm-fixed-install-", dir=ctx.scratch) as temporary:
        root = Path(temporary)
        (root / "transport.cjs").write_text(SOURCE)
        transport = ctx.bundle(root, role="toolchain")
    names = git(ctx.repo(task), "ls-tree", "-r", "--name-only", task["start_sha"]).decode().splitlines()
    immutable = set(ctx.immutable_tests(task)) | set(case["test_paths"])
    immutable.update(name for name in names if re.search(
        r"(^|/)(__tests__|__mocks__|__snapshots__|tools/jest)(/|$)|\.(spec|test)\.[cm]?[jt]sx?$|\.snap$", name))
    immutable.update(name for name in names if name in {
        "jest.config.ts", "jest.config.js", "jest.config.cjs", "tsconfig.spec.json",
    })
    assets = [
        {**exported[name], "kind": "archive", "visibility": "developer", "target": target}
        for name, target in (("node", NODE), ("manager", MANAGER), ("dependencies", PAYLOAD))
    ] + [{**transport, "kind": "archive", "visibility": "developer", "target": TRANSPORT}]
    recipe = RuntimeRecipe(
        task_id=ID, repo=task["repo"], start_sha=START, lane="tsjs",
        image=image, image_audit=audit, cpus=2, memory_mb=2048, build_scratch_mb=4096,
        command_timeout_seconds=case["build_timeout"], workdir="/workspace",
        submodules=ctx.modules(task), assets=assets, scratch_paths=["node_modules"],
        env={"PATH": NODE + "/bin:" + MANAGER + "/bin:/usr/local/bin:/usr/bin:/bin",
             "HOME": "/workspace/.reviewbench/home", "TMPDIR": "/workspace/.reviewbench/tmp",
             "NODE_OPTIONS": "--max-old-space-size=1536", "CI": "true", "HUSKY": "0", "FORCE_COLOR": "0",
             "COREPACK_ENABLE_NETWORK": "0", "COREPACK_ENABLE_PROJECT_SPEC": "0",
             "npm_config_offline": "true", **node_worker_environment(2)},
        bootstrap=[
            "mkdir -p /workspace/.reviewbench/tmp /workspace/.reviewbench/home",
            'test "$(node --version)" = v20.19.5 && test "$(node ' + MANAGER + '/bin/pnpm.cjs --version)" = 9.14.4',
            "node " + TRANSPORT + "/transport.cjs " + PAYLOAD + " /workspace",
        ],
        dev_checks=[build],
        grading={"multifile_build": True, "immutable_paths": sorted(immutable),
                 "golden_apply_ignore_space_change": True, "golden_patch": reference,
                 "bootstrap": [build], "commands": [{
                     "command": test, "parser": "regex",
                     "expected_tests": oracle.get("pass_to_pass_tests") or [ID + "::frozen-" + oracle["start_signature"]],
                     "success_pattern": jest_pattern(oracle["start_signature"]),
                 }]},
    )
    recipe.validate()
    atomic_json(ctx.output / "renovate55-original-binding.json", {
        "id": ID, "full_scope": 402, "input_references": references, "case_fingerprint": CASE,
        "original_build_argv": case["build"], "original_test_argv": case["test"],
        "effective_build_command": build, "effective_test_command": test,
        "original_tests": 16, "test_selector_changes": [], "reference_sha256": REFERENCE,
        "fixed_original_install": True, "candidate_manifest_lock_not_rewritten": True,
        "old_local_source_in_dependencies": False, "current_local_source_binding": "tools/eslint",
        "transport_schema": SCHEMA, "new_package_installs_model_judge_calls": 0,
    })
    return recipe
