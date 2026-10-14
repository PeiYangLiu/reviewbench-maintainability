"""Relocate original Vue SFC oracles without exposing their assertions.

One pinned Node executable and one dependency archive serve the whole family.
The developer gets the native SFC compiler, not the original oracle's import
stubs, props, renderer, expected strings, or coverage tools. Grading executes
the original command against the complete current candidate checkout.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import shlex

from reviewbench_maintainability.agentic.assets import asset_descriptor
from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, file_hash, repo_path,
)
from .common import relocate

SCHEMA = "reviewbench-original-vue-inputs/v1"
NODE_VERSION = "25.6.1"
VUE_VERSION = "3.3.13"
VERSIONS = {
    "@vue/compiler-sfc": VUE_VERSION, "@vue/server-renderer": VUE_VERSION,
    "vue": VUE_VERSION, "esbuild": "0.19.12", "c8": "9.1.0",
}
ROOT = "/opt/reviewbench-assets/vue-runtime"
MODULES = ROOT + "/cache/vue-oracle/node_modules"
NODE = "/opt/reviewbench-assets/vue-node"
PUBLIC = "/opt/reviewbench-assets/vue-public"
STATE = "/workspace/.reviewbench/vue"
TOOLS = frozenset({
    "compile_vue_sfc.mjs", "render_vue_sfc.mjs", "run_graph_oracle.mjs",
    "run_largo_filter_oracle.mjs", "istanbul_to_json.py",
})


def read_pin(ref):
    """Read only a regular absolute file whose independently supplied hash matches."""
    if (not isinstance(ref, dict) or not isinstance(ref.get("path"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", str(ref.get("sha256", "")))):
        raise ContractError("Vue input requires a path and independent SHA256")
    path = Path(ref["path"])
    if not path.is_absolute() or path.is_symlink() or not path.is_file() or file_hash(path) != ref["sha256"]:
        raise ContractError("Vue pinned input is absent, redirected, or changed")
    return json.loads(path.read_text())


def case_target(task, oracle, case):
    if case.get("environment") != {
        "coverage": "c8/Istanbul", "node": NODE_VERSION, "vue": VUE_VERSION,
    }:
        raise ContractError("Vue adapter requires the original Node/Vue/coverage versions")
    if (case.get("setup_host_command") != "mkdir -p {task_dir}/artifacts"
            or case.get("timeout") != 300 or case.get("pass_to_pass") != 1
            or case.get("test_kind") != "session_observable_behavior"
            or case.get("session_files")):
        raise ContractError("Vue setup, timeout, or original test kind differs")
    selectors = oracle.get("pass_to_pass_tests") or oracle.get("test_selectors")
    if (not isinstance(selectors, list) or len(selectors) != 1
            or selectors != case.get("test_selectors") or oracle.get("pass_to_pass") != 1):
        raise ContractError("Vue original PASS_TO_PASS selectors differ")
    command = case.get("test_host_command", "")
    matches = re.findall(
        r"node \{root\}/tools/compile_vue_sfc\.mjs \{work\}/(\S+) "
        r"\{task_dir\}/artifacts/component-\{mode\}-\{run\}\.cjs( --script-only)?(?= && )",
        command,
    )
    if len(matches) != 1:
        raise ContractError("Vue original command needs one current-candidate SFC compilation")
    target, script_only = matches[0]
    target = repo_path(target)
    if target != task["locality"]["path"] or target != oracle.get("anchor_path") or not target.endswith(".vue"):
        raise ContractError("Vue compilation target differs from the original candidate path")
    names = set(re.findall(r"\{root\}/tools/([A-Za-z0-9_.-]+)", command))
    runners = names - {"compile_vue_sfc.mjs", "istanbul_to_json.py"}
    if (not names <= TOOLS or len(runners) != 1
            or not {"compile_vue_sfc.mjs", "istanbul_to_json.py"} <= names):
        raise ContractError("Vue original command has unsupported or missing oracle tools")
    return target, bool(script_only), sorted(names)


def relocated_command(case):
    command = relocate(case["test_host_command"], {
        "root": ROOT, "work": "/workspace", "task_dir": STATE,
        "mode": "candidate", "run": "1",
    })
    if re.search(r"\{(?:root|work|repo|task_dir|mode|run)\}|/(?:home|data)/", command):
        raise ContractError("Vue oracle still contains a host-only path")
    return shlex.join(["bash", "-c", command])


def _component(ctx, preparation, readback, *, status, role):
    if preparation.get("status") != status:
        raise ContractError("Vue shared component is not prepared")
    asset = preparation["asset"]
    if (readback.get("status") != "independent_Blob_component_readback_verified"
            or readback.get("asset") != asset
            or readback.get("all_paths_bytes_full_modes_verified") is not True
            or readback.get("gzip_crc_and_eof_verified") is not True):
        raise ContractError("Vue shared component has no matching independent Blob readback")
    descriptor = asset_descriptor(asset, ctx.output)
    if (descriptor["source_kind"] != role
            or descriptor.get("tree_sha256") != readback.get("tree_sha256")):
        raise ContractError("Vue shared component role/tree differs from verified readback")
    return ctx.copy_asset(Path(asset["path"]), "vue-components")


def original_inputs(ctx, task, oracle, case):
    document = read_pin(ctx.config.get("migration", {}).get("vue_original_inputs"))
    if document.get("schema_version") != SCHEMA:
        raise ContractError("Vue original-input manifest schema differs")
    capture = read_pin(document["capture"])
    if capture.get("status") != "all_eight_original_Vue_input_routes_inspected":
        raise ContractError("Vue input route inspection is incomplete")
    row = capture.get("tasks", {}).get(str(oracle["index"]))
    if not row:
        raise ContractError("Vue original source/input remains pending; no source link is rewritten")
    if (row.get("task_id") != task["id"] or read_pin(row["task"]) != task
            or read_pin(row["oracle"]) != oracle or read_pin(row["plan"]) != case):
        raise ContractError("Vue prepared task/oracle/plan differs from the original release")
    # The actual engine always checks out the full start and applies candidate
    # edits there. The prepared source record proves no target-only substitute.
    sources = read_pin(row["sources"])
    if set(sources) != {"start", "golden"} or any(not sources[v].get("snapshot") for v in sources):
        raise ContractError("Vue full start/source-only golden input evidence is absent")
    node, dependencies = read_pin(document["node"]), read_pin(document["dependencies"])
    if (node.get("node_version") != NODE_VERSION or node.get("exit_status") != 0
            or node.get("stdout") != "v" + NODE_VERSION + "\n"
            or not re.fullmatch(r"[0-9a-f]{64}", str(node.get("node_sha256", "")))
            or dependencies.get("versions") != VERSIONS):
        raise ContractError("Vue original executable or dependency versions differ")
    node_asset = _component(ctx, node, read_pin(document["node_readback"]),
        status="original_Node25_6_1_archived_and_base_compatible", role="toolchain")
    dependency_asset = _component(ctx, dependencies, read_pin(document["dependency_readback"]),
        status="original_Vue_dependencies_archived", role="dependency")
    # The historical six logs must remain exact inputs, not just an unpinned
    # success-pattern source. The reusable scripted parser then binds counts.
    logs = read_pin(row["original_logs"])
    for variant in ("start", "golden"):
        expected = [str((ctx.legacy / "scripted" / repo_path(run["log"])).resolve())
                    for run in oracle.get("runs", {}).get(variant, [])]
        refs = logs.get(variant, [])
        if len(expected) != 3 or [ref["path"] for ref in refs] != expected:
            raise ContractError("Vue original six log paths differ")
        for ref in refs:
            path = Path(ref["path"])
            if path.is_symlink() or not path.is_file() or file_hash(path) != ref["sha256"]:
                raise ContractError("Vue original log bytes changed")
    return row, capture["tools"], node, node_asset, dependency_asset


def migrate(ctx, task, oracle):
    from .scripted_lane import completion_pattern

    case = json.loads((ctx.legacy / "scripted/oracle_config.json").read_text())["tasks"][str(oracle["index"])]
    target, script_only, tools = case_target(task, oracle, case)
    row, captured_tools, node, node_asset, dependency_asset = original_inputs(ctx, task, oracle, case)
    image, audit = ctx.image(node["base_image"])
    if image != node["base_image"]:
        raise ContractError("Vue base image differs from original Node compatibility proof")
    original_tools = ctx.legacy / "scripted/tools"
    for name in tools:
        ref = captured_tools.get(name, {})
        path = original_tools / name
        if (ref.get("path") != str(path.resolve()) or path.is_symlink()
                or not path.is_file() or file_hash(path) != ref.get("sha256")):
            raise ContractError("Vue original grader tool bytes changed: " + name)
    grader = ctx.bundle(original_tools, tools, role="grader")
    public = ctx.bundle(Path(__file__).resolve().parent, ["vue_public_check.mjs"], role="toolchain")
    golden = ctx.golden(task, oracle, "scripted", ctx.task_directory(task, oracle, "scripted"))
    if golden["sha256"] != row["golden"]["sha256"]:
        raise ContractError("Vue source-only golden differs from captured original patch")
    command = ["node", PUBLIC + "/vue_public_check.mjs", "/workspace/" + target, MODULES]
    if script_only:
        command.append("--script-only")
    modules = ctx.modules(task)
    if modules:
        raise ContractError("Vue family needs explicit submodule inputs before admission")
    recipe = RuntimeRecipe(
        task_id=task["id"], repo=task["repo"], start_sha=task["start_sha"],
        lane="scripted", image=image, image_audit=audit, workdir="/workspace",
        cpus=2, memory_mb=2048, build_scratch_mb=2048, pids_limit=128,
        command_timeout_seconds=case["timeout"], submodules=[],
        assets=[
            {**node_asset, "kind": "archive", "visibility": "developer", "target": NODE},
            {**dependency_asset, "kind": "archive", "visibility": "developer", "target": MODULES},
            {**public, "kind": "archive", "visibility": "developer", "target": PUBLIC},
            {**grader, "kind": "archive", "visibility": "grader", "target": ROOT + "/tools"},
        ],
        env={"PATH": NODE + "/bin:/usr/local/bin:/usr/bin:/bin",
             "TMPDIR": "/workspace/.reviewbench/tmp", "CI": "true"},
        bootstrap=["mkdir -p /workspace/.reviewbench/tmp",
                   'test "$(node --version)" = v' + NODE_VERSION],
        dev_checks=[shlex.join(command)],
        grading={
            "multifile_build": True, "immutable_paths": ctx.immutable_tests(task),
            "golden_patch": golden, "bootstrap": ["mkdir -p " + STATE + "/artifacts"],
            "commands": [{
                "command": relocated_command(case), "parser": "regex",
                "expected_tests": case["test_selectors"],
                "success_pattern": completion_pattern(ctx, oracle, case),
            }],
        },
    )
    recipe.validate()
    return recipe
