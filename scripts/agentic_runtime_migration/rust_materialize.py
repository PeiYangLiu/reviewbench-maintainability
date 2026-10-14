"""Bind the three original Materialize workspaces to shared same-target API execution."""
import hashlib
import json
from pathlib import Path
import shlex
import tomllib

from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, atomic_json, canonical_hash, file_hash,
)
from . import rust_overlay_api_entry as entry
from .rust_composable import referenced
from .rust_build_entry import manifest_operations

OPTION = "rust_original_materialize_apis"
PROFILES = json.loads(Path(__file__).with_name("rust_materialize_profiles.json").read_text())
SPEC = "/opt/reviewbench-assets/materialize-public.json"
PRIVATE = "/opt/reviewbench-assets/materialize-private.json"
API_TOOL = "/opt/reviewbench-assets/materialize-private-api.py"
TOOL = "/opt/reviewbench-assets/rust-build-tools/overlay_api.py"
REGISTRY_ASSET = "6b760b73d32d63f06f53e363833ed989573edbeb959d2a3711c00ae0221fbe41"
GIT_ASSET = "9e3b357c5c539345a190d54a21e51d047eb5ba7eed48ef44de2920f480563abc"
NPM_TABLE_SHA256 = "c85a0fe50bdba12c4cbba90ef6bd8686299425096b29460c781ee4b22621d643"


def require(value, message):
    if not value:
        raise ContractError("original Materialize: " + message)


def requested(ctx, task):
    choices = getattr(ctx, "config", {}).get("migration", {}).get(OPTION, {})
    require(isinstance(choices, dict) and set(choices) <= set(PROFILES), "unsupported original profile")
    return task.get("id") in choices


def original_inputs(ctx, task, oracle, plan, patch):
    require(task["id"] in PROFILES and oracle["id"] == task["id"], "original task changed")
    profile = PROFILES[task["id"]]
    require(task["repo"] == oracle["repo"] == profile["repo"]
            and task["start_sha"] == oracle["start_sha"] == profile["start"], "original source identity changed")
    require(canonical_hash(plan) == profile["plan"] and plan["input_index"] == profile["index"]
            and plan["command"] == profile["original_command"], "original plan/argv changed")
    require(patch["sha256"] == oracle["golden_patch_sha256"] == profile["golden"]
            and file_hash(Path(patch["path"])) == profile["golden"], "source-only golden changed")
    options = ctx.config["migration"][OPTION][task["id"]]
    require(set(options) - {"native_assets"} == {"inputs", "registry", "git", "graphs", "readback", "overlay"},
            "preparation binding changed")
    inputs, registry, git, graphs, readback, overlay = [
        referenced(options[key], document=True) for key in ("inputs", "registry", "git", "graphs", "readback", "overlay")]
    for name, value in (("task", task), ("oracle", oracle), ("plan", plan)):
        require(referenced(inputs[name], document=True) == value, "original " + name + " differs")
    require(inputs["full_scope"] == graphs["full_scope"] == readback["full_scope"] == 402
            and inputs["new_resolution_SUT_model_judge_calls"] == 0 and graphs["all_complete"]
            and graphs["matching_full_graphs"] == 12, "original graph preparation incomplete")
    require(readback["status"] == "original_dependency_components_independently_verified"
            and all(row["paths_bytes_modes_verified"] for row in readback["components"]),
            "dependency archive readback incomplete")
    require(registry["asset"]["sha256"] == REGISTRY_ASSET and git["asset"]["sha256"] == GIT_ASSET
            and registry["registry_packages"] == 989 and git["git_packages"] == 82
            and git["git_sources"] == 39 and git["per_case_selector_isolation"],
            "original dependency union changed")
    for component in (registry, git):
        referenced(component["asset"], document=True)
    source_config = git["source_configs"][str(profile["index"])]
    referenced(source_config)
    sources = referenced(inputs["source_preparation"], document=True)
    logs = referenced(inputs["original_logs"], document=True)
    observations = referenced(graphs["observations"], document=True)
    originals = []
    for variant in ("start", "golden"):
        source = sources[variant]
        require(entry.core.snapshot(Path(source["source"])) == source["source_snapshot"]
                and entry.core.snapshot(Path(source["declared_environment"])) == source["environment_snapshot"],
                "complete original source/environment changed")
        require(source["source_snapshot"]["Cargo.lock"][1] == profile["seed"]
                and inputs["retained_locks"][variant]["head"] == profile["start"]
                and inputs["retained_locks"][variant]["recovery_pin_not_original_oracle_lock_field"],
                "retained lock provenance changed")
        require(hashlib.sha256(referenced(inputs["retained_locks"][variant]["lock"])).hexdigest() == profile["lock"],
                "original retained lock bytes changed")
        require(oracle["environment"]["variant_toolchains"][variant] == profile["toolchain"]
                and shlex.split(oracle["environment"]["variant_commands"][variant]) ==
                shlex.split(profile["original_command"]), "original variant compiler/argv changed")
        require(len(oracle["runs"][variant]) == len(logs[variant]) == 3, "original six observations missing")
        for original, row in zip(oracle["runs"][variant], logs[variant]):
            text = referenced(row).decode()
            require(original["ok"] and not original["failed_tests"] and original["passed_tests"] == profile["tests"]
                    and Path(row["path"]).resolve() == (ctx.legacy / "rust" / original["log"]).resolve()
                    and all("test " + name + " ... ok" in text for name in profile["tests"]),
                    "original assertion evidence changed")
        harness = source["session_files"][profile["harness"]]
        data = referenced(harness)
        require(hashlib.sha256(data).hexdigest() == profile["harness_sha256"][variant],
                "original private harness changed")
        declared = plan["session_files"][0]
        content = declared.get(variant + "_content", declared.get("content"))
        if content is None:
            name = declared.get(variant + "_source", declared.get("source"))
            content = (ctx.legacy / "rust" / name).read_text()
        require(content.encode() == data, "prepared harness is not the original declared variant")
        operations = [{"path": name, "operations": manifest_operations(
            tomllib.loads((Path(source["source"]) / name).read_text()),
            tomllib.loads((Path(source["declared_environment"]) / name).read_text()))}
                      for name in profile["overlay_paths"]]
        originals.append(operations)
        for role in ("public", "private"):
            found = [row for row in observations.values() if row["case"] == profile["index"]
                     and row["variant"] == variant and row["role"] == role]
            require(len(found) == 1 and found[0]["exit_status"] == 0
                    and found[0]["packages"] == profile["packages"]
                    and found[0]["workspace_members"] == profile["workspace_members"]
                    and found[0]["full_dependency_edges_and_features_unchanged"]
                    and found[0]["original_git_cache_mounted"] is False, "full offline graph changed")
            referenced(found[0]["stdout"])
            referenced(found[0]["stderr"])
    require(originals[0] == originals[1] == overlay["manifests"]
            and overlay["task_id"] == task["id"] and overlay["locks"] == [] and overlay["private_members"] == [],
            "original symmetric environment operations changed")
    modules = inputs["original_uninitialized_gitlinks"]
    require({(row["path"], row["commit"]) for row in modules} ==
            {(row["path"], row["commit"]) for row in ctx.modules(task)}
            and all(row["omit"] for row in modules), "original empty gitlink state changed")
    require(plan["build_jobs"] == profile["cpus"] and plan["timeout_seconds"] == profile["timeout_seconds"]
            and plan.get("env", {}) == profile["env"] and oracle["pass_to_pass_tests"] == profile["tests"],
            "original resources/environment/assertions changed")
    return profile, inputs, registry, git, sources, modules


def migrate(ctx, task, oracle):
    from . import rust_lane, rust_overlay, rust_api_entry, rust_protoc
    plan = rust_lane.load_plan(ctx, oracle)
    patch = ctx.golden(task, oracle, "rust", ctx.task_directory(task, oracle, "rust"))
    profile, inputs, registry, git, sources, modules = original_inputs(ctx, task, oracle, plan, patch)
    toolchain = json.loads(Path(ctx.config["rust_toolchains"]).read_text())[profile["toolchain"]]
    require(all(file_hash(Path(toolchain["root"]) / "bin" / name) == profile[name]
                for name in ("cargo", "rustc")), "original compiler registry changed")
    image, image_audit = ctx.image(rust_lane.IMAGE)
    assets = []

    def asset(value, target, visibility="developer", kind="file"):
        assets.append({**value, "target": target, "visibility": visibility, "kind": kind})
        return {"path": target, "sha256": value["sha256"]}

    asset(rust_lane.compiler(ctx, profile["toolchain"]), "/opt/reviewbench-assets/rust", kind="archive")
    asset(registry["asset"], "/opt/reviewbench-assets/vendor-registry", kind="archive")
    asset(git["asset"], "/opt/reviewbench-assets/vendor-git", kind="archive")
    config = git["source_configs"][str(profile["index"])]
    source_config = asset(ctx.copy_asset(Path(config["path"]), "materialize-sources"),
                          "/opt/reviewbench-assets/materialize-sources.toml")
    option = ctx.config["migration"][OPTION][task["id"]]
    original_overlay = asset(ctx.copy_asset(Path(option["overlay"]["path"]), "materialize-overlay"),
                             "/opt/reviewbench-assets/materialize-overlay.json")
    lock = asset(ctx.copy_asset(Path(inputs["retained_locks"]["start"]["lock"]["path"]), "materialize-lock"),
                 "/opt/reviewbench-assets/materialize-original.lock")
    cargo = {"path": "/opt/reviewbench-assets/rust/bin/cargo", "sha256": profile["cargo"]}
    command = [cargo["path"], *shlex.split(profile["original_command"])[2:]]
    forms = []
    for variant in ("start", "golden"):
        data = sources[variant]["session_files"][profile["harness"]]
        original = asset(ctx.copy_asset(Path(data["path"]), "materialize-original-harness"),
                         "/opt/reviewbench-assets/materialize-" + variant + ".rs", "grader")
        forms.append({"name": variant, "harness": profile["harness"], "target": "oracle",
                      "asset": original, "command": command})
    private = {
        "schema_version": rust_api_entry.SAME_TARGET_SCHEMA, "task_id": task["id"],
        "selection": "compile-only-current-candidate; execute-all-compilable-forms; conjunctive-results",
        "workdir": ".", "package": profile["package"], "package_manifest": profile["manifest"],
        "cargo": cargo, "forms": forms, "expected_tests": profile["tests"],
        "cpus": profile["cpus"], "timeout_seconds": profile["timeout_seconds"], "max_log_bytes": 32 * 1024**2,
    }
    rust_api_entry.validate(private)
    path = ctx.output / "materialize-private.json"
    atomic_json(path, private)
    private_spec = asset(ctx.copy_asset(path, "materialize-private-spec"), PRIVATE, "grader")
    private_tool = asset(ctx.copy_asset(Path(rust_api_entry.__file__), "materialize-private-selector"), API_TOOL, "grader")
    public = {
        "schema_version": entry.SAME_TARGET_SCHEMA, "task_id": task["id"], "selection": entry.core.SELECTION,
        "package": profile["package"], "package_manifest": profile["manifest"],
        "feature_args": profile["feature_args"], "locks": [lock], "reference_seed_sha256": profile["seed"],
        "cargo": cargo, "rustc": {"path": "/opt/reviewbench-assets/rust/bin/rustc", "sha256": profile["rustc"]},
        "source_config": source_config, "overlay": original_overlay, "overlay_paths": profile["overlay_paths"],
        "private_spec_sha256": private_spec["sha256"], "private_tool_sha256": private_tool["sha256"],
        "public_command": ["build", "--locked", "-p", profile["package"], *profile["feature_args"],
                           "--lib", "--message-format=json"],
        "cpus": profile["cpus"], "timeout_seconds": profile["timeout_seconds"],
        "metadata_timeout_seconds": 180, "max_log_bytes": 32 * 1024**2,
    }
    if profile["index"] == 10:
        # The original integration-test command unifies clap's dev-dependency
        # feature `env`. Derive the public-only equivalent from the CURRENT
        # manifest, without adding reference features to the private command.
        public["public_test_feature_dependencies"] = ["clap", "mz-ore"]
    if profile["index"] == 11:
        # The original integration-test dev-dependency mz-persist enables
        # transitive Tokio features missing from a repr-only library build.
        public["public_test_workspace_dependencies"] = ["mz-persist"]
    entry.validate(public)
    path = ctx.output / "materialize-public.json"
    atomic_json(path, public)
    asset(ctx.copy_asset(path, "materialize-public-spec"), SPEC)
    tools = rust_overlay.build_tools(ctx, extra_entries={
        "overlay_api.py": Path(entry.__file__), "image_locks.py": Path(__file__).with_name("rust_image_entry.py")})
    asset(tools, rust_overlay.TOOLS, kind="archive")
    asset(rust_protoc.build_asset(ctx), rust_protoc.TARGET, kind="archive")
    if "native_assets" in option:
        native = referenced(option["native_assets"], document=True)
        require(native["status"] == "original_cached_npm_assets_verified"
                and native["original_package_table_sha256"] == NPM_TABLE_SHA256
                and len(native["packages"]) == 7 and len(native["files"]) == 16,
                "original static build-input inventory changed")
        for variant in ("start", "golden"):
            module = Path(sources[variant]["source"]) / "src/npm/src/lib.rs"
            text = module.read_text()
            block = text[text.index("const NPM_PACKAGES"):text.index("impl NpmPackage")]
            require(hashlib.sha256(block.encode()).hexdigest() == NPM_TABLE_SHA256,
                    "original npm package/checksum table changed")
        source_root = Path(native["root"])
        for row in native["files"]:
            source = source_root / row["relative_path"]
            require(source.resolve() == Path(row["source"]["path"]).resolve()
                    and file_hash(source) == row["source"]["sha256"], "original npm bytes changed")
        native_root = "/opt/reviewbench-assets/materialize-native-npm"
        asset(ctx.bundle(source_root), native_root, kind="archive")
        crates = ["src/prof", "src/materialized" if profile["index"] == 8 else "src/environmentd"]
        public["build_inputs"] = [
            {"path": str(Path(crate) / row["relative_path"]),
             "source": {"path": str(Path(native_root) / row["relative_path"]), "sha256": row["source"]["sha256"]},
             "mode": row["mode"]}
            for crate in crates for row in native["files"]]
        entry.validate(public)
        # Replace only this recipe's small public spec; original dependency
        # bodies and private harnesses remain unchanged and reusable.
        path = ctx.output / "materialize-public-with-native.json"
        atomic_json(path, public)
        assets[:] = [row for row in assets if row["target"] != SPEC]
        asset(ctx.copy_asset(path, "materialize-public-spec"), SPEC)
    prefix = ["python3", TOOL, "--spec", SPEC, "--task-id", task["id"]]
    recipe = RuntimeRecipe(
        task_id=task["id"], repo=task["repo"], start_sha=task["start_sha"], lane="rust",
        image=image, image_audit=image_audit, workdir="/workspace", cpus=profile["cpus"], memory_mb=8192,
        build_scratch_mb=12288, command_timeout_seconds=profile["timeout_seconds"],
        env={"PATH": "/opt/reviewbench-assets/rust/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
             "RUSTC": "/opt/reviewbench-assets/rust/bin/rustc", "CARGO_HOME": "/opt/reviewbench-cache/cargo",
             "CARGO_TARGET_DIR": "/opt/reviewbench-cache/target", "CARGO_NET_OFFLINE": "true",
             "CARGO_BUILD_JOBS": str(profile["cpus"]), "CARGO_INCREMENTAL": "0", "CARGO_TERM_COLOR": "never",
             "CARGO_PROFILE_DEV_DEBUG": "0", "CARGO_PROFILE_TEST_DEBUG": "0", "RUSTFLAGS": "--cap-lints=allow",
             "RUST_BACKTRACE": "1", "PROTOC": rust_protoc.TARGET + "/bin/protoc",
             "PROTOC_INCLUDE": rust_protoc.TARGET + "/include", "SKIP_WASM_BUILD": "1",
             "RUST_BUILD_DOCKER": "1", "GIT_COMMIT": "reviewbench-hard-oracle", "GIT_DIRTY": "false",
             "SOURCE_TIMESTAMP": "0", "HOME": "/tmp", "TMPDIR": "/workspace/.reviewbench/tmp", **profile["env"]},
        assets=assets, cache_mounts={"cargo": "/opt/reviewbench-cache/cargo", "target": "/opt/reviewbench-cache/target",
                                    "rust-build": rust_overlay.BUILD_CACHE},
        bootstrap=["mkdir -p /workspace/.reviewbench/tmp",
                   'test "$(' + rust_protoc.TARGET + '/bin/protoc --version)" = "libprotoc 3.19.2"'],
        dev_checks=[shlex.join(prefix)], scratch_paths=[], submodules=modules,
        grading={"multifile_build": True, "immutable_paths": ctx.immutable_tests(task), "golden_patch": patch,
                 "bootstrap": [], "commands": [{
                     "command": shlex.join([*prefix, "--private", PRIVATE, "--private-tool", API_TOOL]),
                     "parser": "rust-test", "expected_tests": profile["tests"]}]},
    )
    recipe.validate()
    return recipe
