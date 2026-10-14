"""Bind Gear37's two original compiler/manifest/lock/WASM environments."""
import hashlib
import json
from pathlib import Path
import shlex
import tomllib

from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, atomic_json, canonical_hash, file_hash,
)
from . import rust_gear_entry as entry, rust_vendor
from .rust_composable import referenced
from .rust_local_registry import graph_signature

OPTION = "rust_original_gear_inputs"
SCHEMA = "reviewbench-original-gear37-inputs/v1"
TASK = entry.TASK
START = "01cd8e8efb22563fdbed27b4c2c36cb2b147b109"
PLAN = "d4f9530b10d984af00e846a2b26ada69458b50670d06101e6fea65652400a200"
GOLDEN = "e3016e1e6b34c2aed3ea01f9f51d9bc88f0f6aed5d68f2dd104f188dd7037502"
SEED = "c4b1576fb8b77140eaa5b7fbd9315e38a37c3f702c611b0bb50fb73aa43754e3"
PINS = {
    "start": {
        "cargo": "40ba5673f2486657fa4a2da75ff1b446bae3a3cf10907f85fa3174ab3653b2da",
        "rustc": "3a4a6a0357002064638ce20291fc6503582befac77c7c82eb455e701f4ae1d9c",
        "public_manifest": "a0ee67d8d82aeb303bfd7d78b03953ec2994680c12bec08dda1765ed2d648e70",
        "private_manifest": "aa0d81469d7fe4703afd6106bb8785c1f89dce53c19613c9b871c04ba6947bdc",
        "public_lock": SEED,
        "private_lock": "efb453d146045bce2fd8a0bec23bb9576795daa0b2edfb5e7d3486c5cdfc862d",
        "wasm": "072f929cff5005c07b2c55c7e3d4b403c90ab509f81648120ee1e8a13299df1e",
    },
    "golden": {
        "cargo": "0a51f7c5b4a61ab4bbfe80a496a71d1d36327d8d6a1ea3a1a37a894e946f7fd4",
        "rustc": "7c508a0768c7a8f36163b9eaa630379a947d154d905e89725380bfd61c074479",
        "public_manifest": "220e7b3be3dada94e59f60a32aa8badcdbe51fed8a931295c92c68b64763dae5",
        "private_manifest": "7890f9363c49d948bdc303e4ad70964466774009da27fd9d9395a3c7cef25ca9",
        "public_lock": "13476cf1d570aabbbc5a5aa4feb8d29c3879f84a6bf5ffdee8194a6fa24f3132",
        "private_lock": "0d5b536c27fed9f350796bb8ca842c1ec4f95cb22b59fdd56ddcfc04282b34be",
        "wasm": "788d32d3930eb9d68347360c872037a6df096823601b4b4ce42d8e24b4f4df93",
    },
}
SESSION_HASHES = {
    entry.SESSION_PATHS[0]: "a0c1e631170fc091a938d4050e8a0e2832790672891ebf83379d33b905bbf5f8",
    entry.HARNESS: "e70780360c3d59617b4f8666ceb4ddf18d48e02180adf8ad89e14804f5e6fdfd",
}
SPEC = "/opt/reviewbench-assets/gear-public.json"
PRIVATE = "/opt/reviewbench-assets/gear-private.json"
TOOL = "/opt/reviewbench-assets/rust-build-tools/gear.py"


def require(condition, message):
    if not condition:
        raise ContractError("original Gear37: " + message)


def requested(ctx, task):
    settings = getattr(ctx, "config", {}).get("migration", {}).get(OPTION, {})
    require(isinstance(settings, dict) and set(settings) <= {TASK}, "unknown profile")
    return task.get("id") == TASK and TASK in settings


def readback(component, proof):
    descriptor = referenced(component["asset"], document=True)
    require(proof["status"] == "independent_Blob_component_readback_verified"
            and proof["asset"] == component["asset"] and proof["all_paths_bytes_full_modes_verified"]
            and proof["gzip_crc_and_eof_verified"] and proof["tree_sha256"] == descriptor["tree_sha256"],
            "independent original component restoration incomplete")
    return descriptor


def isolated_sources(lock):
    git, _ = rust_vendor.source_configuration(
        {p["source"] for p in lock["package"] if p.get("source", "").startswith("git+")},
        target="/opt/reviewbench-assets/vendor-git")
    registry, _ = rust_vendor.source_configuration(
        {entry.core.CRATES_IO}, target="/opt/reviewbench-assets/vendor-registry")
    return {"source": {**tomllib.loads(registry)["source"], **tomllib.loads(git)["source"]},
            "net": {"offline": True, "retry": 0}}


def original_inputs(ctx, task, oracle, plan, patch):
    require(task["id"] == oracle["id"] == TASK and task["repo"] == oracle["repo"] == "gear-tech/gear"
            and task["start_sha"] == oracle["start_sha"] == START, "original task/source changed")
    require(canonical_hash(plan) == PLAN and plan["input_index"] == oracle["input_index"] == 37
            and plan["build_jobs"] == 4 and plan["timeout_seconds"] == 3600
            and plan["workdir"] == entry.SESSION and plan.get("features", []) == []
            and not plan.get("env"), "original plan/resources changed")
    require(oracle["pass_to_pass_tests"] == [entry.TEST]
            and [r["path"] for r in plan["session_files"]] ==
            [entry.SESSION_PATHS[0], entry.HARNESS, entry.SESSION_PATHS[2]],
            "original assertion inventory changed")
    require(patch["sha256"] == oracle["golden_patch_sha256"] == GOLDEN
            and file_hash(Path(patch["path"])) == GOLDEN, "original source-only golden changed")
    prepared = referenced(ctx.config["migration"][OPTION][TASK], document=True)
    require(prepared["schema_version"] == SCHEMA and prepared["full_scope"] == 402
            and prepared["task_id"] == TASK, "unbound original preparation")
    for key, value in (("task", task), ("oracle", oracle), ("plan", plan)):
        require(referenced(prepared[key], document=True) == value, "original " + key + " changed")
    original = referenced(prepared["original_sources"], document=True)
    sources = referenced(original["source_preparation"], document=True)
    logs = referenced(original["original_logs"], document=True)
    native = referenced(prepared["original_graphs"], document=True)
    expanded = referenced(prepared["expanded_graphs"], document=True)
    require(native["complete"] and native["successful_full_graph_pairs"] == 4
            and expanded["complete"] and native["full_scope"] == expanded["full_scope"] == 402,
            "original full graph verification incomplete")
    graphs = referenced(native["observations"], document=True)
    expanded_graphs = referenced(expanded["observations"], document=True)
    require(set(graphs) == set(expanded_graphs) == {
        name + "-" + role for name in entry.NAMES for role in ("public", "private")},
        "original four graph views changed")
    for index, name in enumerate(entry.NAMES):
        source, form, pins = sources[name], prepared["forms"][name], PINS[name]
        require(entry.core.snapshot(Path(source["source"])) == source["source_snapshot"]
                and entry.core.snapshot(Path(source["declared_environment"])) == source["environment_snapshot"],
                "original complete source/environment changed")
        require(source["source_lock"]["sha256"] == SEED, "source lock seed changed")
        command = ["cargo", "+" + entry.TOOLCHAINS[index], *entry.COMMAND]
        require(form["toolchain"] == entry.TOOLCHAINS[index]
                and shlex.split(source["original_command"]) == command
                and shlex.split(oracle["environment"]["variant_commands"][name]) == command,
                "original compiler/argv changed")
        for key in ("cargo", "rustc"):
            require(form[key]["sha256"] == pins[key] == file_hash(Path(form[key]["path"])),
                    "original compiler bytes changed")
        for path, row in source["session_files"].items():
            required = pins["wasm"] if path.endswith(".wasm") else SESSION_HASHES[path]
            require(hashlib.sha256(referenced(row)).hexdigest() == required, "original session bytes changed")
        require(len(logs[name]) == len(oracle["runs"][name]) == 3, "original six logs missing")
        for proof, run in zip(logs[name], oracle["runs"][name]):
            text = referenced(proof).decode()
            require(Path(proof["path"]).resolve() == (ctx.legacy / "rust" / run["log"]).resolve()
                    and run["ok"] and run["passed_tests"] == [entry.TEST] and not run["failed_tests"]
                    and "test " + entry.TEST + " ... ok" in text, "original test/log changed")
        for role in ("public", "private"):
            key = name + "-" + role
            row, full = graphs[key], expanded_graphs[key]
            require(row["complete"] and row["historical_log_exact_additions"]
                    and not row["missing_historical_added"] and not row["unexpected_added"]
                    and row["original_source_except_lock_unchanged"]
                    and row["transport_source_and_observed_lock_unchanged"]
                    and full["exit_status"] == 0 and full["source_and_lock_unchanged"]
                    and full["all_six_original_source_gitlinks_restored"]
                    and full["full_graph_edges_features_identical"],
                    "original graph or complete source transport changed")
            actual = referenced(full["stdout"], document=True)
            directory = referenced(row["directory"]["stdout"], document=True)
            native_graph = referenced(row["native"]["stdout"], document=True)
            require(graph_signature(actual) == graph_signature(directory) == graph_signature(native_graph)
                    == row["native_graph_signature"], "full graph edges/features changed")
            count = (1626 if name == "start" else 1654) + (role == "private")
            require(len(actual["packages"]) == len(actual["resolve"]["nodes"]) == count
                    and len(actual["workspace_members"]) == 167 + (role == "private"),
                    "original workspace graph narrowed")
            for field in ("manifest", "lock"):
                ref = form["manifests" if field == "manifest" else "locks"][role]
                require(hashlib.sha256(referenced(ref)).hexdigest() == pins[role + "_" + field],
                        "original manifest/observed-lock pair changed")
        referenced(form["source_config"])
    registry, git = [referenced(prepared[key], document=True) for key in ("registry", "git")]
    require(registry["registry_packages"] == 1464 and git["git_packages"] == 354 and git["git_sources"] == 10,
            "full original external inventory changed")
    for name, component in (("registry", registry), ("git", git)):
        readback(component, referenced(prepared["readbacks"][name], document=True))
    # Cargo cannot merge a single Git selector pointing at two original commits.
    # Revalidate each complete source configuration independently.
    for name in entry.NAMES:
        lock = tomllib.loads(referenced(prepared["forms"][name]["locks"]["public"]).decode())
        expected = isolated_sources(lock)
        require(tomllib.loads(referenced(prepared["forms"][name]["source_config"]).decode()) == expected,
                "per-variant Git selectors were merged or changed")
    modules = []
    for item in prepared["modules"]:
        component = referenced(item["result"], document=True)
        descriptor = readback(component, referenced(item["readback"], document=True))
        require(component["path"] == item["path"] and component["commit"] == descriptor["git_commit"] == item["commit"]
                and descriptor["source_kind"] == "submodule", "original source gitlink changed")
        modules.append({"path": item["path"], "commit": item["commit"], "asset": component["asset"]})
    require(len(modules) == 6 and {p["path"]: p["commit"] for p in modules}
            == {p["path"]: p["commit"] for p in ctx.modules(task)}, "full source gitlink inventory changed")
    return prepared, sources, registry, git, modules


def migrate(ctx, task, oracle):
    from . import rust_lane, rust_overlay, rust_gear_output_entry
    plan = rust_lane.load_plan(ctx, oracle)
    patch = ctx.golden(task, oracle, "rust", ctx.task_directory(task, oracle, "rust"))
    prepared, sources, registry, git, modules = original_inputs(ctx, task, oracle, plan, patch)
    image, image_audit = ctx.image(rust_lane.IMAGE)
    assets = []

    def asset(value, target, visibility="developer", kind="file"):
        assets.append({**value, "target": target, "visibility": visibility, "kind": kind})
        return {"path": target, "sha256": value["sha256"]}

    asset(registry["asset"], "/opt/reviewbench-assets/vendor-registry", kind="archive")
    asset(git["asset"], "/opt/reviewbench-assets/vendor-git", kind="archive")
    public_forms, private_forms = [], []
    for index, name in enumerate(entry.NAMES):
        inputs = prepared["forms"][name]
        compiler = "/opt/reviewbench-assets/rust-" + name
        asset(rust_lane.compiler(ctx, entry.TOOLCHAINS[index]), compiler, kind="archive")
        item = {"name": name, "toolchain": entry.TOOLCHAINS[index],
                "cargo": {"path": compiler + "/bin/cargo", "sha256": PINS[name]["cargo"]},
                "rustc": {"path": compiler + "/bin/rustc", "sha256": PINS[name]["rustc"]}}
        item["source_config"] = asset(
            ctx.copy_asset(Path(inputs["source_config"]["path"]), "gear-sources"),
            "/opt/reviewbench-assets/gear-sources-" + name + ".toml")
        hidden = {"name": name, "session_files": []}
        for role in ("public", "private"):
            bound = {}
            for field in ("manifest", "lock"):
                reference = inputs["manifests" if field == "manifest" else "locks"][role]
                bound[field] = asset(ctx.copy_asset(Path(reference["path"]), "gear-original-" + field),
                                     "/opt/reviewbench-assets/gear-" + role + "-" + field + "-" + name,
                                     "developer" if role == "public" else "grader")
            if role == "public":
                item.update(manifests=[{"path": "Cargo.toml", "asset": bound["manifest"]}], lock=bound["lock"])
            else:
                hidden.update(bound)
        for number, path in enumerate(entry.SESSION_PATHS):
            reference = sources[name]["session_files"][path]
            original = asset(ctx.copy_asset(Path(reference["path"]), "gear-session"),
                             "/opt/reviewbench-assets/gear-session-" + name + "-" + str(number), "grader")
            hidden["session_files"].append({"path": path, "asset": original})
        public_forms.append(item)
        private_forms.append(hidden)
    private = {"schema_version": entry.PRIVATE_SCHEMA, "task_id": TASK, "workdir": entry.SESSION,
               "expected_tests": [entry.TEST], "command": entry.COMMAND, "forms": private_forms}
    public = {"schema_version": entry.SCHEMA, "task_id": TASK, "selection": entry.forms.SELECTION,
              "package": "gtest", "package_manifest": "Cargo.toml", "public_command": entry.PUBLIC,
              "forms": public_forms, "reference_seed_sha256": SEED,
              "manifests": [{"path": "Cargo.toml", "reference": public_forms[0]["manifests"][0]["asset"]}],
              "cpus": 4, "timeout_seconds": 3600, "metadata_timeout_seconds": 180,
              "max_log_bytes": 32 * 1024**2, "private_fingerprint": entry.fingerprint(private)}
    entry.validate(public, private)
    for name, value, target, role in (("public", public, SPEC, "developer"), ("private", private, PRIVATE, "grader")):
        path = ctx.output / ("gear-" + name + ".json")
        atomic_json(path, value)
        asset(ctx.copy_asset(path, "gear-specs"), target, role)
    tools = rust_overlay.build_tools(ctx, extra_entries={
        "gear.py": Path(entry.__file__), "manifest_variants.py": Path(entry.forms.__file__),
        "image_locks.py": Path(entry.core.__file__),
    })
    asset(tools, rust_overlay.TOOLS, kind="archive")
    prefix = ["python3", TOOL, "--spec", SPEC, "--task-id", TASK]
    output_tool = "/opt/reviewbench-assets/gear-grader-output.py"
    asset(ctx.copy_asset(Path(rust_gear_output_entry.__file__), "gear-grader-output"), output_tool, "grader")
    grading = ["python3", output_tool, "--spec", SPEC, "--task-id", TASK, "--private", PRIVATE,
               "--core-sha256", file_hash(Path(entry.__file__))]
    recipe = RuntimeRecipe(
        task_id=TASK, repo=task["repo"], start_sha=START, lane="rust", image=image, image_audit=image_audit,
        workdir="/workspace", cpus=4, memory_mb=16384, build_scratch_mb=12288, command_timeout_seconds=3600,
        env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
             "CARGO_TARGET_DIR": "/opt/reviewbench-cache/target", "CARGO_NET_OFFLINE": "true",
             "CARGO_BUILD_JOBS": "4", "CARGO_INCREMENTAL": "0", "CARGO_TERM_COLOR": "never",
             "CARGO_PROFILE_DEV_DEBUG": "0", "CARGO_PROFILE_TEST_DEBUG": "0", "RUSTFLAGS": "--cap-lints=allow",
             "RUST_BACKTRACE": "1", "SKIP_WASM_BUILD": "1", "RUST_BUILD_DOCKER": "1",
             "GIT_COMMIT": "reviewbench-hard-oracle", "GIT_DIRTY": "false", "SOURCE_TIMESTAMP": "0",
             "HOME": "/tmp", "TMPDIR": "/workspace/.reviewbench/tmp"},
        assets=assets, cache_mounts={"target": "/opt/reviewbench-cache/target", "rust-build": rust_overlay.BUILD_CACHE},
        bootstrap=["mkdir -p /workspace/.reviewbench/tmp"], dev_checks=[shlex.join(prefix)], scratch_paths=[],
        submodules=modules, grading={
            "multifile_build": True, "immutable_paths": ctx.immutable_tests(task), "golden_patch": patch,
            "bootstrap": [], "commands": [{"command": shlex.join(grading),
                                         "parser": "rust-test", "expected_tests": [entry.TEST]}],
        })
    recipe.validate()
    return recipe
