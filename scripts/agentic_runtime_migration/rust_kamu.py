"""Bind Kamu's original source overlay and same-target API pair to shared execution."""
import hashlib
import json
from pathlib import Path
import shlex
import tomllib

from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, atomic_json, canonical_hash, file_hash,
)
from . import rust_overlay_api_entry as entry, rust_vendor
from .rust_build_entry import manifest_operations
from .rust_composable import referenced
from .rust_local_registry import graph_signature

OPTION = "rust_original_kamu_inputs"
SCHEMA = "reviewbench-original-kamu53-inputs/v1"
PROFILE = "kamu53-original-v1"
TASK = "github__kamu-data__kamu-cli__pull_941__comment_1843087682__api_design"
START = "a60560a5558f17b025596053de8b1b04ddf12105"
PLAN = "dced9d34f4f4bd4e868f6f2e3cd56b5166241aa4699d369b100c705101403404"
GOLDEN = "0e1326f96a12e869c36da77cc3c30188f7e6a15a96f4b39382f61dfc6667b919"
SEED = "b9a4826297faa82ec59862b13c7f6992d3c5699e7c5c9d59634baa47d1112bb0"
LOCK = "aae81ba55cbbb6d152c023713e4f7dec5f198ced7a60e15520db0b456d0fa3e7"
TOOLCHAIN = "nightly-2024-07-19"
CARGO = "cf576623f39744b3346220b88269ccc09921826d3803ec762e0423ad6d81e615"
RUSTC = "344ad71973938c988e99e79e839d3b91e209634d01708bfd33b962ee08c77842"
HARNESS = "src/domain/core/tests/oracle.rs"
TEST = "sync_ref_is_local_classifies_local_and_remote"
MANIFESTS = ["Cargo.toml", "src/domain/core/Cargo.toml"]
HARNESS_HASHES = {
    "start": "6d11325a1cb3be3afc9e2d855f5b63ce897d442720b5e7ebd1a0e4ddb9895b2c",
    "golden": "a7e46363783def2d16c2fd99bcaaafa5249bf9f71d713f484bdeb278fde2c29d",
}
COMMAND = (
    "cargo +nightly-2024-07-19 test -p kamu-core --test oracle "
    "sync_ref_is_local_classifies_local_and_remote -- --nocapture"
)
SPEC = "/opt/reviewbench-assets/kamu-public.json"
PRIVATE = "/opt/reviewbench-assets/kamu-private.json"
API_TOOL = "/opt/reviewbench-assets/kamu-private-api.py"
TOOL = "/opt/reviewbench-assets/rust-build-tools/overlay_api.py"


def require(value, message):
    if not value:
        raise ContractError("original Kamu53: " + message)


def requested(ctx, task):
    options = getattr(ctx, "config", {}).get("migration", {}).get(OPTION, {})
    require(isinstance(options, dict) and set(options) <= {TASK}, "unknown profile")
    return task.get("id") == TASK and TASK in options


def original_inputs(ctx, task, oracle, plan, patch):
    require((task["id"], task["repo"], task["start_sha"]) == (TASK, "kamu-data/kamu-cli", START)
            and (oracle["id"], oracle["repo"], oracle["start_sha"]) == (TASK, "kamu-data/kamu-cli", START),
            "original task/source changed")
    require(canonical_hash(plan) == PLAN and plan["input_index"] == oracle["input_index"] == 53
            and plan["command"] == COMMAND and plan["toolchain"] == TOOLCHAIN
            and plan["build_jobs"] == 16 and plan["timeout_seconds"] == 3600
            and plan.get("features", []) == [] and plan.get("env", {}) == {}
            and plan.get("workdir", ".") == ".", "original plan/command/resources changed")
    require(patch["sha256"] == oracle["golden_patch_sha256"] == GOLDEN
            and file_hash(Path(patch["path"])) == GOLDEN, "source-only golden changed")
    require(oracle["pass_to_pass_tests"] == [TEST] and len(plan["session_files"]) == 1
            and plan["session_files"][0]["path"] == HARNESS, "original assertion inventory changed")
    prepared = referenced(ctx.config["migration"][OPTION][TASK], document=True)
    require(prepared["schema_version"] == SCHEMA and prepared["full_scope"] == 402
            and prepared["task_id"] == TASK, "unbound original preparation")
    for key, value in (("task", task), ("oracle", oracle), ("plan", plan)):
        require(referenced(prepared[key], document=True) == value, "original " + key + " changed")
    inputs = referenced(prepared["original_inputs"], document=True)
    require(inputs["gitlinks"] == [] and inputs["task_id"] == TASK, "original source closure changed")
    sources = referenced(inputs["source_preparation"], document=True)
    logs = referenced(inputs["original_logs"], document=True)
    overlay = referenced(prepared["overlay"], document=True)
    observed_lock = referenced(prepared["lock"])
    require(hashlib.sha256(observed_lock).hexdigest() == LOCK, "original-seed resolved lock changed")
    seed = None
    for variant, row in sources.items():
        require(variant in HARNESS_HASHES
                and entry.core.snapshot(Path(row["source"])) == row["source_snapshot"]
                and entry.core.snapshot(Path(row["declared_environment"])) == row["environment_snapshot"],
                "complete source/environment changed")
        require(row["source_lock"]["sha256"] == row["environment_lock"]["sha256"] == SEED,
                "original dependency seed changed")
        seed_bytes = referenced(row["environment_lock"])
        require(seed is None or seed == seed_bytes, "asymmetric original seed")
        seed = seed_bytes
        harness = referenced(row["session_files"][HARNESS])
        require(hashlib.sha256(harness).hexdigest() == HARNESS_HASHES[variant]
                and harness == plan["session_files"][0][variant + "_content"].encode(),
                "original same-path harness changed")
        require(row["original_command"] == oracle["environment"]["variant_commands"][variant] == COMMAND
                and row["original_toolchain"] == oracle["environment"]["variant_toolchains"][variant] == TOOLCHAIN,
                "original variant compiler/argv changed")
        require(len(logs[variant]) == len(oracle["runs"][variant]) == 3, "original six runs missing")
        for proof, run in zip(logs[variant], oracle["runs"][variant]):
            text = referenced(proof).decode()
            require(run["ok"] and run["passed_tests"] == [TEST] and not run["failed_tests"]
                    and "test " + TEST + " ... ok" in text
                    and Path(proof["path"]).resolve() == (ctx.legacy / "rust" / run["log"]).resolve(),
                    "original assertion evidence changed")
        operations = [{
            "path": name, "operations": manifest_operations(
                tomllib.loads((Path(row["source"]) / name).read_text()),
                tomllib.loads((Path(row["declared_environment"]) / name).read_text())),
        } for name in MANIFESTS]
        require(operations == overlay["manifests"], "original environment operations changed")
    require(set(sources) == {"start", "golden"} and overlay["task_id"] == TASK
            and overlay["schema_version"] == "reviewbench-cargo-build-overlay/v1"
            and overlay["locks"] == [] and overlay["private_members"] == [],
            "original symmetric overlay changed")
    registry = referenced(prepared["registry"], document=True)
    git = referenced(prepared["git"], document=True)
    require(registry["registry_packages"] == 879 and git["git_packages"] == 8
            and git["git_sources"] == 2 and git["original_unused_git_patch_preserved"],
            "complete original external input inventory changed")
    rows = [
        *referenced(registry["vendor_audit"], document=True),
        *referenced(git["vendor_audit"], document=True),
    ]
    require({(p["name"], p["version"], p["source"]): p["package_checksum"] for p in rows}
            == rust_vendor.locked_packages([tomllib.loads(seed.decode())]),
            "transport dropped or replaced original locked packages")
    for kind, component in (("registry", registry), ("git", git)):
        descriptor = referenced(component["asset"], document=True)
        proof = referenced(prepared["readbacks"][kind], document=True)
        require(proof["status"] == "independent_Blob_component_readback_verified"
                and proof["asset"] == component["asset"]
                and proof["all_paths_bytes_full_modes_verified"] and proof["gzip_crc_and_eof_verified"]
                and proof["tree_sha256"] == descriptor["tree_sha256"],
                "independent " + kind + " readback incomplete")
    native = referenced(prepared["native_graphs"], document=True)
    require(native["complete"] and native["full_scope"] == 402, "original seed graph incomplete")
    native_rows = referenced(native["observations"], document=True)
    require(len(native_rows) == 4 and all(
        p["exit_status"] == 0 and p["source_except_lock_unchanged"]
        and p["original_external_versions_preserved"] and p["external_added"] == []
        and p["observed_lock"]["sha256"] == LOCK and p["packages"] == p["nodes"] == 520
        and p["workspace_members"] == 17 for p in native_rows.values()),
        "original-seed resolution changed external identities")
    offline = referenced(prepared["offline_graphs"], document=True)
    require(offline["complete"] and offline["successful_graphs"] == offline["matching_full_graphs"] == 4,
            "offline source transport incomplete")
    offline_rows = referenced(offline["observations"], document=True)
    require(set(offline_rows) == {v + "-" + r for v in ("start", "golden") for r in ("public", "private")},
            "four source/role graph views missing")
    for row in offline_rows.values():
        require(row["exit_status"] == 0 and row["source_and_lock_unchanged"]
                and row["empty_cargo_home"] and not row["original_git_cache_mounted"]
                and row["full_edges_features_identical"] and row["packages"] == row["nodes"] == 520
                and row["workspace_members"] == 17, "offline graph changed")
        actual, original = [referenced(row[k], document=True) for k in ("stdout", "original_graph")]
        require(graph_signature(actual) == graph_signature(original) == row["graph_signature"],
                "complete graph edges/features changed")
    referenced(git["source_config"])
    return prepared, sources, registry, git


def migrate(ctx, task, oracle):
    from . import rust_lane, rust_overlay, rust_api_entry
    plan = rust_lane.load_plan(ctx, oracle)
    patch = ctx.golden(task, oracle, "rust", ctx.task_directory(task, oracle, "rust"))
    prepared, sources, registry, git = original_inputs(ctx, task, oracle, plan, patch)
    toolchains = json.loads(Path(ctx.config["rust_toolchains"]).read_text())
    toolchain = Path(toolchains[TOOLCHAIN]["root"])
    require(file_hash(toolchain / "bin/cargo") == CARGO and file_hash(toolchain / "bin/rustc") == RUSTC,
            "pinned compiler changed")
    image, image_audit = ctx.image(rust_lane.IMAGE)
    assets = []

    def asset(value, target, visibility="developer", kind="file"):
        assets.append({**value, "target": target, "visibility": visibility, "kind": kind})
        return {"path": target, "sha256": value["sha256"]}

    asset(rust_lane.compiler(ctx, TOOLCHAIN), "/opt/reviewbench-assets/rust", kind="archive")
    asset(registry["asset"], "/opt/reviewbench-assets/vendor-registry", kind="archive")
    asset(git["asset"], "/opt/reviewbench-assets/vendor-git", kind="archive")
    configuration = asset(ctx.copy_asset(Path(git["source_config"]["path"]), "kamu-sources"),
                          "/opt/reviewbench-assets/kamu-sources.toml")
    overlay = asset(ctx.copy_asset(Path(prepared["overlay"]["path"]), "kamu-overlay"),
                    "/opt/reviewbench-assets/kamu-overlay.json")
    lock = asset(ctx.copy_asset(Path(prepared["lock"]["path"]), "kamu-original-seed-resolution"),
                 "/opt/reviewbench-assets/kamu-original-seed-resolved.lock")
    cargo = {"path": "/opt/reviewbench-assets/rust/bin/cargo", "sha256": CARGO}
    command = [cargo["path"], *shlex.split(COMMAND)[2:]]
    forms = []
    for variant in ("start", "golden"):
        reference = sources[variant]["session_files"][HARNESS]
        private_file = asset(ctx.copy_asset(Path(reference["path"]), "kamu-original-harness"),
                             "/opt/reviewbench-assets/kamu-" + variant + ".rs", "grader")
        forms.append({"name": variant, "harness": HARNESS, "target": "oracle",
                      "asset": private_file, "command": command})
    private = {
        "schema_version": rust_api_entry.SAME_TARGET_SCHEMA, "profile": PROFILE, "task_id": TASK,
        "selection": "compile-only-current-candidate; execute-all-compilable-forms; conjunctive-results",
        "workdir": ".", "package": "kamu-core", "package_manifest": MANIFESTS[1],
        "cargo": cargo, "forms": forms, "expected_tests": [TEST], "cpus": 16,
        "timeout_seconds": 3600, "max_log_bytes": 32 * 1024**2,
    }
    rust_api_entry.validate(private)
    path = ctx.output / "kamu-private.json"
    atomic_json(path, private)
    private_spec = asset(ctx.copy_asset(path, "kamu-private-spec"), PRIVATE, "grader")
    private_tool = asset(ctx.copy_asset(Path(rust_api_entry.__file__), "kamu-private-selector"),
                         API_TOOL, "grader")
    public = {
        "schema_version": entry.SAME_TARGET_SCHEMA, "profile": PROFILE, "task_id": TASK,
        "selection": entry.core.SELECTION, "package": "kamu-core", "package_manifest": MANIFESTS[1],
        "feature_args": [], "locks": [lock], "reference_seed_sha256": SEED, "cargo": cargo,
        "rustc": {"path": "/opt/reviewbench-assets/rust/bin/rustc", "sha256": RUSTC},
        "source_config": configuration, "overlay": overlay, "overlay_paths": MANIFESTS,
        "private_spec_sha256": private_spec["sha256"], "private_tool_sha256": private_tool["sha256"],
        "public_command": ["build", "--locked", "-p", "kamu-core", "--lib", "--message-format=json"],
        "cpus": 16, "timeout_seconds": 3600, "metadata_timeout_seconds": 180,
        "max_log_bytes": 32 * 1024**2,
    }
    entry.validate(public)
    path = ctx.output / "kamu-public.json"
    atomic_json(path, public)
    asset(ctx.copy_asset(path, "kamu-public-spec"), SPEC)
    tools = rust_overlay.build_tools(ctx, extra_entries={
        "overlay_api.py": Path(entry.__file__),
        "image_locks.py": Path(__file__).with_name("rust_image_entry.py"),
    })
    asset(tools, rust_overlay.TOOLS, kind="archive")
    prefix = ["python3", TOOL, "--spec", SPEC, "--task-id", TASK]
    recipe = RuntimeRecipe(
        task_id=TASK, repo=task["repo"], start_sha=START, lane="rust",
        image=image, image_audit=image_audit, workdir="/workspace", cpus=16, memory_mb=32768,
        build_scratch_mb=12288, command_timeout_seconds=3600, assets=assets,
        env={
            "PATH": "/opt/reviewbench-assets/rust/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "RUSTC": "/opt/reviewbench-assets/rust/bin/rustc", "CARGO_HOME": "/opt/reviewbench-cache/cargo",
            "CARGO_TARGET_DIR": "/opt/reviewbench-cache/target", "CARGO_NET_OFFLINE": "true",
            "CARGO_BUILD_JOBS": "16", "CARGO_INCREMENTAL": "0", "CARGO_TERM_COLOR": "never",
            "CARGO_PROFILE_DEV_DEBUG": "0", "CARGO_PROFILE_TEST_DEBUG": "0",
            "RUSTFLAGS": "--cap-lints=allow", "RUST_BACKTRACE": "1", "PROTOC_INCLUDE": "/usr/include",
            "SKIP_WASM_BUILD": "1", "RUST_BUILD_DOCKER": "1", "GIT_COMMIT": "reviewbench-hard-oracle",
            "GIT_DIRTY": "false", "SOURCE_TIMESTAMP": "0", "HOME": "/tmp",
            "TMPDIR": "/workspace/.reviewbench/tmp",
        },
        cache_mounts={"cargo": "/opt/reviewbench-cache/cargo", "target": "/opt/reviewbench-cache/target",
                      "rust-build": rust_overlay.BUILD_CACHE},
        bootstrap=["mkdir -p /workspace/.reviewbench/tmp"], dev_checks=[shlex.join(prefix)],
        scratch_paths=[], submodules=[],
        grading={
            "multifile_build": True, "immutable_paths": ctx.immutable_tests(task), "golden_patch": patch,
            "bootstrap": [], "commands": [{
                "command": shlex.join([*prefix, "--private", PRIVATE, "--private-tool", API_TOOL]),
                "parser": "rust-test", "expected_tests": [TEST],
            }],
        },
    )
    recipe.validate()
    return recipe
