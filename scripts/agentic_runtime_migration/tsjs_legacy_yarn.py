"""Reuse both original Yarn graphs without exposing reference inputs to an agent."""
from __future__ import annotations

import ast
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
from .tsjs_yarn1 import checked, source_names, test_pattern
from .tsjs_yarn1_transport import SOURCE as TRANSPORT_SOURCE, original_contract

OPOSSUM = "github__opossum-tool__OpossumUI__pull_987__comment_972768731__duplication"
NODE = "/opt/reviewbench-assets/node"
MANAGER = "/opt/reviewbench-assets/yarn-manager"
BASE = "/opt/reviewbench-assets/legacy-yarn-start"
PRIVATE = "/opt/reviewbench-assets/legacy-yarn-golden"
INSTALL = ["corepack", "yarn@1.22.22", "install", "--frozen-lockfile", "--ignore-scripts"]
ARGV = [
    "react-scripts", "test", "--watchAll=false",
    "src/Frontend/Components/ProjectStatisticsPopup/__tests__/ProjectStatisticsPopup.test.tsx",
    "--runInBand", "--setupFilesAfterEnv=./setupTests.ts", "--coverage",
    "--collectCoverageFrom=src/Frontend/Components/ProjectStatisticsPopup/project-statistics-popup-helpers.ts",
]
SIGNATURE = "jest:suites=1,tests=2"

# This file and the target graph are grader-only. Public development always
# receives the original start installation, never target metadata or packages.
SELECTOR_SOURCE = r"""
'use strict';
const fs = require('fs'), path = require('path'), crypto = require('crypto');
const cp = require('child_process');
try {
  if (process.argv.length !== 7) throw new Error('original graph selection needs five arguments');
  const [startTool, startPayload, goldenTool, goldenPayload, workspace] = process.argv.slice(2);
  const expected = JSON.parse(fs.readFileSync(path.join(__dirname, 'golden-inputs.json'), 'utf8'));
  const names = Object.keys(expected).sort();
  if (JSON.stringify(names) !== JSON.stringify(['package.json', 'yarn.lock']))
    throw new Error('original target graph inputs differ');
  let golden = true;
  for (const name of names) {
    const filename = path.join(workspace, name), st = fs.lstatSync(filename);
    if (!st.isFile() || st.isSymbolicLink()) throw new Error('original graph input is redirected');
    const digest = crypto.createHash('sha256').update(fs.readFileSync(filename)).digest('hex');
    golden = golden && digest === expected[name];
  }
  console.log('LEGACY_YARN_GRAPH phase=' + (golden ? 'golden' : 'start') +
              ' source_metadata_rewritten=false');
  const result = cp.spawnSync(process.execPath,
    [golden ? goldenTool : startTool, golden ? goldenPayload : startPayload, workspace],
    {stdio: 'inherit', shell: false});
  if (result.error) throw result.error;
  process.exit(result.status === null ? 2 : result.status);
} catch (error) {
  console.error('LEGACY_YARN_GRAPH_ERROR ' + error.message);
  process.exit(2);
}
"""


def pinned_file(pointer, label):
    if not isinstance(pointer, dict) or set(pointer) != {"path", "sha256"}:
        raise ContractError("legacy Yarn needs an exact file pointer: " + label)
    path = Path(pointer["path"])
    if (not path.is_absolute() or not path.is_file() or path.is_symlink()
            or file_hash(path) != pointer["sha256"]):
        raise ContractError("legacy Yarn original file changed: " + label)
    return path


def read_inputs(task, oracle, pointer):
    value = checked(pointer, "legacy Yarn two-graph inputs")
    if (task["id"] != OPOSSUM or value.get("id") != task["id"]
            or value.get("schema_version") != "reviewbench-legacy-yarn-two-graphs/v1"
            or value.get("status") != "both_original_dependency_graphs_ready"
            or value.get("full_scope") != 402):
        raise ContractError("legacy Yarn original two-graph identity differs")
    base = checked(value["base_inputs"], "original published inputs")
    execution = checked(value["execution_binding"], "original execution binding")
    if (base.get("task_fingerprint") != canonical_hash(task)
            or base.get("oracle_fingerprint") != canonical_hash(oracle)
            or base.get("node_version") != oracle.get("historical_node")
            or execution.get("status") != "original_runner_accepted_argv_and_two_install_graphs_bound"
            or execution.get("id") != task["id"] or execution.get("full_scope") != 402
            or execution.get("original_node_version") != "18.20.8"
            or execution.get("published_node_version") != "16.20.2"
            or execution.get("published_node_version") != oracle.get("historical_node")
            or execution.get("accepted_test_argv") != ARGV
            or execution.get("signature") != SIGNATURE
            or execution.get("original_install_argv") != INSTALL
            or execution.get("original_reinstall_golden") is not True
            or execution.get("new_install_calibration_model_judge_calls") != 0):
        raise ContractError("legacy Yarn original command, version, or install policy differs")
    runner = checked(execution["runner_binding"], "original runner binding")
    script = pinned_file(runner["runner"], "original runner")
    values = {}
    for node in ast.parse(script.read_text()).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {"CONFIG", "PM"}:
                    values[target.id] = ast.literal_eval(node.value)
    if (list(values.get("CONFIG", {}).get(23, ()))[:3] != ["18.20.8", "yarn1", ""]
            or list(values.get("PM", {}).get("yarn1", ())) != ["yarn", "1.22.22"]):
        raise ContractError("legacy Yarn recorded runner does not select the original Node/Yarn")
    logs = execution.get("original_logs", [])
    if [(row["phase"], row["run"]) for row in logs] != [
            (phase, number) for phase in ("start", "golden") for number in (1, 2, 3)]:
        raise ContractError("legacy Yarn needs all six original accepted replay identities")
    for row in logs:
        path = pinned_file({k: row[k] for k in ("path", "sha256")}, "accepted replay")
        text = path.read_text()
        commands = [shlex.split(line[2:]) for line in text.splitlines() if line.startswith("$ ")]
        if (len(commands) != 1 or Path(commands[0][0]).name != ARGV[0]
                or commands[0][1:] != ARGV[1:]
                or not re.search(test_pattern(SIGNATURE), text, re.M)):
            raise ContractError("legacy Yarn original accepted argv or two-test signature differs")
    preparations = {}
    for phase in ("start", "golden"):
        original = execution["original_installations"][phase]
        path = pinned_file({k: original[k] for k in ("path", "sha256")}, phase + " original install")
        lines = path.read_text().splitlines()
        if (len(lines) < 2 or shlex.split(lines[0][2:]) != INSTALL
                or not re.fullmatch(r"exit=0 duration=[0-9.]+s", lines[1])):
            raise ContractError("legacy Yarn original successful installation receipt differs")
        prepared = checked(value["preparations"][phase], phase + " prepared dependencies")
        if (prepared.get("id") != task["id"] or prepared.get("full_scope") != 402
                or prepared.get("status") != "original_" + phase + "_dependencies_Blob_verified"
                or prepared.get("original_node") != "18.20.8"
                or prepared.get("original_manager") != "yarn@1.22.22"
                or prepared.get("original_install_argv") != INSTALL
                or prepared.get("install", {}).get("original_install_argv") != INSTALL
                or prepared["install"].get("exit_status") != 0
                or prepared.get("original_sources_unchanged") is not True
                or prepared.get("workspace_production_files_exported") != 0
                or prepared.get("calibration_model_judge_calls") != 0
                or prepared.get("input_hashes") != execution["input_hashes"][phase]
                or prepared.get("module_roots") != ["node_modules"]
                or prepared.get("workspace_links") != []):
            raise ContractError("legacy Yarn prepared graph is not the exact original successful install")
        preparations[phase] = prepared
    if any(preparations["start"][key] != preparations["golden"][key] for key in ("node", "manager")):
        raise ContractError("legacy Yarn original graphs use different toolchains")
    pinned_file(execution["source_only_reference"], "original source-only reference")
    return value, execution, preparations


def check_assets(ctx, prepared):
    for name, role in (("node", "toolchain"), ("manager", "toolchain"), ("dependencies", "dependency")):
        descriptor = asset_descriptor(prepared[name], ctx.output)
        if descriptor["source_kind"] != role:
            raise ContractError("legacy Yarn asset role differs")
        if name == "dependencies":
            proof = prepared["independent_readback"]
            if (proof.get("status") != "independent_Blob_bytes_modes_links_crc_verified"
                    or proof.get("files") != descriptor["files"]
                    or proof.get("bytes") != descriptor["archive"]["compressed_bytes"]
                    or len(proof.get("receipts", [])) != len(descriptor["archive"]["parts"])):
                raise ContractError("legacy Yarn graph lacks complete independent Blob readback")
            for receipt, part in zip(proof["receipts"], descriptor["archive"]["parts"]):
                if any(receipt.get(key) != part[key] for key in ("sha256", "size_bytes", "md5_base64")):
                    raise ContractError("legacy Yarn independent readback bytes differ")


def migrate(ctx, task, oracle, pointer):
    _, execution, preparations = read_inputs(task, oracle, pointer)
    from .tsjs_lane import IMAGE, node_worker_environment, typescript_public_check
    excluded = sorted(set(execution["original_restored_tests"])
                      | set(execution["original_target_added_test_absences"]))
    reference = execution["source_only_reference"]
    expected = git(ctx.repo(task), "diff", "--binary", "--full-index", task["start_sha"],
                   task["target_sha"], "--", ".", *[":(exclude)" + repo_path(name) for name in excluded])
    if pinned_file(reference, "original source-only reference").read_bytes() != expected:
        raise ContractError("legacy Yarn complete original source-only rule differs")
    names = source_names(ctx, task)
    if ARGV[3] not in names or "setupTests.ts" not in names:
        raise ContractError("legacy Yarn original tracked test/setup is absent")
    if git(ctx.repo(task), "show", task["start_sha"] + ":" + ARGV[3]) != git(
            ctx.repo(task), "show", task["target_sha"] + ":" + ARGV[3]):
        raise ContractError("legacy Yarn accepted original test changed between source trees")
    transports = {}
    for phase, commit in (("start", task["start_sha"]), ("golden", task["target_sha"])):
        prepared = preparations[phase]
        check_assets(ctx, prepared)
        files = {name: git(ctx.repo(task), "show", commit + ":" + repo_path(name))
                 for name in prepared["input_hashes"]}
        contract = original_contract(
            {**prepared, "source_manifest_or_lock_rewritten": False, "yarn_version": "1.22.22"}, files)
        with tempfile.TemporaryDirectory(prefix="legacy-yarn-" + phase + "-", dir=ctx.scratch) as temporary:
            root = Path(temporary)
            (root / "transport.cjs").write_text(TRANSPORT_SOURCE)
            atomic_json(root / "original-install.json", contract)
            if phase == "golden":
                (root / "select.cjs").write_text(SELECTOR_SOURCE)
                atomic_json(root / "golden-inputs.json", prepared["input_hashes"])
            transports[phase] = ctx.bundle(root, role="toolchain" if phase == "start" else "grader")
    image, audit = ctx.image(IMAGE)
    assets = [
        {**preparations["start"][name], "kind": "archive", "visibility": "developer", "target": target}
        for name, target in (("node", NODE), ("manager", MANAGER), ("dependencies", BASE + "/payload"))
    ]
    assets += [
        {**transports["start"], "kind": "archive", "visibility": "developer", "target": BASE + "/tool"},
        {**preparations["golden"]["dependencies"], "kind": "archive",
         "visibility": "grader", "target": PRIVATE + "/payload"},
        {**transports["golden"], "kind": "archive", "visibility": "grader", "target": PRIVATE + "/tool"},
    ]
    start = ["node", BASE + "/tool/transport.cjs", BASE + "/payload", "/workspace"]
    select = ["node", PRIVATE + "/tool/select.cjs", BASE + "/tool/transport.cjs", BASE + "/payload",
              PRIVATE + "/tool/transport.cjs", PRIVATE + "/payload", "/workspace"]
    bootstrap = ("if test -f " + PRIVATE + "/tool/select.cjs; then " + shlex.join(select)
                 + "; else " + shlex.join(start) + "; fi")
    immutable = set(ctx.immutable_tests(task)) | {ARGV[3], "setupTests.ts"}
    immutable.update(name for name in names if re.search(
        r"(^|/)(__tests__|__mocks__|__snapshots__)(/|$)|\.(spec|test)\.[cm]?[jt]sx?$|\.snap$", name))
    if not immutable <= set(names):
        raise ContractError("legacy Yarn immutable paths must be original tracked files")
    recipe = RuntimeRecipe(
        task_id=task["id"], repo=task["repo"], start_sha=task["start_sha"], lane="tsjs",
        image=image, image_audit=audit, cpus=2, memory_mb=2048, build_scratch_mb=2048,
        command_timeout_seconds=600, workdir="/workspace", assets=assets,
        submodules=ctx.modules(task), scratch_paths=["node_modules", "coverage"],
        env={"PATH": NODE + "/bin:" + MANAGER + "/bin:/workspace/node_modules/.bin:/usr/local/bin:/usr/bin:/bin",
             "NODE_PATH": "/workspace/node_modules", "CI": "true", "HUSKY": "0", "FORCE_COLOR": "0",
             "HOME": "/workspace/.reviewbench/home", "TMPDIR": "/workspace/.reviewbench/tmp",
             "NODE_OPTIONS": "--max-old-space-size=1536", "COREPACK_ENABLE_NETWORK": "0",
             "COREPACK_ENABLE_PROJECT_SPEC": "0", **node_worker_environment(2)},
        bootstrap=["mkdir -p /workspace/.reviewbench/tmp /workspace/.reviewbench/home",
                   'test "$(node --version)" = v18.20.8 && test "$(node '
                   + MANAGER + '/bin/yarn.js --version)" = 1.22.22', bootstrap],
        dev_checks=[typescript_public_check(task["locality"]["path"])],
        grading={"multifile_build": True, "immutable_paths": sorted(immutable),
                 "golden_apply_ignore_space_change": True, "golden_patch": reference,
                 "bootstrap": [], "commands": [{
                     "command": shlex.join(["/workspace/node_modules/.bin/react-scripts", *ARGV[1:]]),
                     "parser": "regex", "expected_tests": [task["id"] + "::frozen-" + SIGNATURE],
                     "success_pattern": test_pattern(SIGNATURE),
                 }]},
    )
    recipe.validate()
    atomic_json(ctx.output / "legacy-yarn-bindings" / (component(task["id"]) + ".json"), {
        "id": task["id"], "inputs": pointer, "runtime_fingerprint": recipe.fingerprint,
        "accepted_original_argv": ARGV, "original_install_argv": INSTALL,
        "original_target_reinstallation_reused": True, "target_assets_grader_only": True,
        "developer_graph": "original-start", "current_complete_source_used": True,
        "new_installs_calibration_model_judge_calls": 0,
    })
    return recipe
