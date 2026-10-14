"""Restore Clarity103's original fixed stacks-core preparation, with no resolver."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile

from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, atomic_json, canonical_hash, file_hash,
)
from reviewbench_maintainability.agentic.snapshots import start_archive, extract_tar, tree_files
from .native_submodules import object_view, readonly_git, readonly_links
from .rust_overlay import session_bytes, reference_inputs

TASK = "github__stx-labs__clarity-wasm__pull_98__comment_1364196603__complexity"
REPO = "stx-labs/clarity-wasm"
START = "0ce18dcafd3917cd9716cbf62a42bef2eaeaa530"
GOLDEN = "17ee68b7c31f867115930d0b01563ab4a6e48247dfae7a1b9064f9992108a0db"
PLAN = "f5f6d901d3682384b299ffc1d9c240ed4409119bb9439131dce00fda0d56083b"
MODULE = "d964d057ad8b5a2ae40934ecef775f50d6b1e979"
MODULE_PATH = "stacks-blockchain"
OPTION = "rust_clarity_original_submodule"
HARNESS_SHA = "ca968f715020e11985daeef5b0d59a9a529ef8f7e0189b6e48aa580f7a5cc798"
EXPECTED = ["print_round_trips_values_through_real_wasm_execution"]
WAT = "clar2wasm/src/standard/standard.wat"
WASM = "clar2wasm/src/standard/standard.wasm"
BUILD = "clar2wasm/build.rs"
BUILD_SHA = "f5385462a09155c2edb0eab0f1791ab3bdf1044fad754ca396b8c7e09f409e9d"
FRESH_BUILD = f"if test -f /workspace/{WAT}; then touch /workspace/{WAT}; fi"


def require(condition, message):
    if not condition:
        raise ContractError("Clarity original submodule: " + message)


def referenced(row):
    path = Path(row["path"])
    require(path.is_absolute() and path.is_file() and not path.is_symlink(),
            "prepared input must be a regular local file")
    require(path.stat().st_size <= 8 * 1024**2 and file_hash(path) == row["sha256"],
            "prepared input changed")
    return json.loads(path.read_text())


def requested(ctx, task):
    if task.get("id") != TASK:
        return False
    options = getattr(ctx, "config", {}).get(OPTION, {})
    require(isinstance(options, dict) and not (set(options) - {TASK}), "invalid task opt-in")
    return TASK in options


def replace_submodule(original, asset):
    """Keep every compiler/vendor/source/test/feature/resource field unchanged."""
    require((original["task_id"], original["repo"], original["start_sha"]) == (TASK, REPO, START),
            "original recipe has a different task")
    modules = original["submodules"]
    require(len(modules) == 1 and modules[0]["path"] == MODULE_PATH
            and modules[0]["commit"] == MODULE and modules[0].get("omit") is True,
            "original omitted-module policy differs")
    result = deepcopy(original)
    result["submodules"] = [{"path": MODULE_PATH, "commit": MODULE, "asset": asset}]
    require({k: v for k, v in result.items() if k != "submodules"} ==
            {k: v for k, v in original.items() if k != "submodules"}, "unrelated recipe change")
    return result


def fresh_workspace_policy(recipe):
    """Regenerate source-tree build output; do not freeze any candidate input."""
    result = deepcopy(recipe)
    require(result.get("workdir") == "/workspace", "fresh-workspace build has a different root")
    require(FRESH_BUILD not in result["bootstrap"], "fresh-workspace policy already bound")
    result["bootstrap"].append(FRESH_BUILD)
    result["scratch_paths"] = sorted(set(result.get("scratch_paths", [])) | {WASM})
    return result


def migrate(ctx, task, oracle):
    from .rust_lane import load_plan
    require((task.get("id"), task.get("repo"), task.get("start_sha")) == (TASK, REPO, START)
            and (oracle.get("id"), oracle.get("repo"), oracle.get("start_sha")) == (TASK, REPO, START),
            "wrong original task/repo/start")
    plan = load_plan(ctx, oracle)
    require(canonical_hash(plan) == PLAN and plan["input_index"] == oracle["input_index"] == 103
            and plan["toolchain"] == "1.73.0" and plan["build_jobs"] == 4
            and plan["features"] == ["developer-mode"] and plan["timeout_seconds"] == 3600,
            "original preparation/command/features/resources changed")
    require(oracle["golden_patch_sha256"] == GOLDEN and oracle["pass_to_pass_tests"] == EXPECTED,
            "original golden or fixed tests changed")
    require(len(plan["session_files"]) == 1
            and hashlib.sha256(session_bytes(ctx, plan, oracle, plan["session_files"][0])).hexdigest()
            == HARNESS_SHA, "original real-WASM test changed")
    path = Path(ctx.config[OPTION][TASK])
    require(path.is_absolute() and path.is_file() and not path.is_symlink(), "invalid preparation pointer")
    prepared = json.loads(path.read_text())
    require(prepared["schema_version"] == "reviewbench-clarity103-original-submodule-inputs/v1"
            and prepared["task_id"] == TASK and prepared["module_commit"] == MODULE
            and prepared["original_prepare_is_fixed_source_materialization"] is True,
            "wrong fixed submodule input")
    require(referenced(prepared["original_task"]) == task
            and referenced(prepared["original_oracle"]) == oracle
            and referenced(prepared["original_plan"]) == plan, "prepared original records differ")
    original = referenced(prepared["original_recipe"])
    require(original["grading"]["golden_patch"]["sha256"] == GOLDEN
            and original["grading"]["commands"][0]["expected_tests"] == EXPECTED
            and original["grading"]["commands"][0]["parser"] == "rust-test",
            "original recipe no longer binds its exact tests")
    for asset in [original["image_audit"], *original["assets"], original["grading"]["golden_patch"]]:
        require(file_hash(Path(asset["path"])) == asset["sha256"], "retained runtime asset changed")
    build_inputs = reference_inputs(ctx, task, original["grading"]["golden_patch"], [BUILD, WAT, WASM])
    for variant in ("start", "golden"):
        require(build_inputs[variant][BUILD] is not None
                and hashlib.sha256(build_inputs[variant][BUILD]).hexdigest() == BUILD_SHA
                and build_inputs[variant][WAT] is not None
                and build_inputs[variant][WASM] is None,
                "original generated-WASM build contract changed")
    repository = Path(prepared["module_repository"])
    require(repository.is_dir() and not repository.is_symlink(), "original cached stacks-core repository is absent")
    require(readonly_links(ctx.repo(task), START) == [{"path": MODULE_PATH, "commit": MODULE}],
            "superproject fixed gitlink changed")
    require(readonly_git(repository, "rev-parse", MODULE + "^{commit}").decode().strip() == MODULE
            and readonly_links(repository, MODULE) == [], "module commit or recursive source closure differs")
    raw = readonly_git(repository, "ls-tree", "-r", "-l", "-z", MODULE)
    files = {}
    for row in raw.split(b"\0"):
        if row:
            meta, name = row.split(b"\t", 1)
            mode, kind, oid, size = meta.decode().split()
            require(kind == "blob" and mode in {"100644", "100755", "120000"}, "unexpected module tree entry")
            files[name.decode()] = {"mode": mode, "git_oid": oid, "bytes": int(size)}
    require(len(files) == 643 and sum(row["bytes"] for row in files.values()) == 34325571,
            "original full module source inventory differs")
    with tempfile.TemporaryDirectory(prefix="clarity-original-stacks-", dir=ctx.scratch) as temporary:
        destination = Path(temporary) / "source"
        with object_view(repository) as view, start_archive(view, MODULE) as stream:
            extract_tar(stream, destination)
        actual = tree_files(destination)
        require(set(actual) == set(files), "module extraction narrowed original source")
        import os
        for name, target in actual.items():
            data = os.fsencode(os.readlink(target)) if target.is_symlink() else target.read_bytes()
            require(hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
                    == files[name]["git_oid"] and len(data) == files[name]["bytes"],
                    "module extraction changed original bytes")
            require(target.is_symlink() == (files[name]["mode"] == "120000"),
                    "module entry type changed")
            if not target.is_symlink():
                require(bool(target.stat().st_mode & 0o111) == (files[name]["mode"] == "100755"),
                        "module executable mode changed")
        require(file_hash(destination / "clarity/Cargo.toml") ==
                "22aadf11169c39a6e8ebd828e255d6d9bf9b46b0710f277a56a4d1b6c37b6432",
                "original clarity package changed")
        asset = ctx.bundle(destination, role="submodule", git_commit=MODULE)
    after = fresh_workspace_policy(replace_submodule(original, asset))
    recipe = RuntimeRecipe.from_dict(after)
    atomic_json(ctx.output / "clarity-original-module-audit.json", {
        "task_id": TASK, "original_plan": plan, "module_repository": str(repository),
        "module_commit": MODULE, "module_files": files, "module_asset": asset,
        "original_compiler_vendor_locks_source_tests_features_resources_unchanged": True,
        "changed_recipe_fields": ["submodules", "bootstrap", "scratch_paths"],
        "generated_output_policy": {
            "original_build_script_sha256": BUILD_SHA, "current_candidate_input": WAT,
            "generated_output_only": WASM, "fresh_workspace_bootstrap": FRESH_BUILD,
            "input_bytes_replaced": False, "whole_dependency_cache_cleaned": False,
        },
        "runtime_fingerprint": recipe.fingerprint,
        "new_dependency_resolutions": 0, "new_downloads": 0, "new_SUT_model_calls": 0,
        "prepare_equivalence": "exact source tree of original shared-clone/detached-checkout pin; no shared Git history mounted",
    })
    return recipe
