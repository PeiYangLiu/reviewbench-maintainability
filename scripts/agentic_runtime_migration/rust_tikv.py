"""Reuse the complete original TiKV106 inputs with the existing Cargo overlay."""
import hashlib
import json
from pathlib import Path
import shlex
import tomllib

from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, atomic_json, canonical_hash, file_hash,
)
from . import rust_image_entry as core, rust_vendor
from .rust_local_registry import graph_signature
from .rust_locked_workspace import referenced

OPTION = "rust_original_tikv_inputs"
SCHEMA = "reviewbench-original-tikv106-inputs/v1"
TASK = "github__tikv__tikv__pull_12938__comment_915750622__api_design"
REPO = "tikv/tikv"
START = "fb73a0472d0c1e5e314fd43de3ca1c7e62171af0"
PLAN = "c0d796706433d1e70cc8151291d53780b41f69a2627f86446173023daeee4f9d"
GOLDEN = "784035f3e08cf8623cd6cb70523f30bfc1ed560966ab97fe47a74ad73a2d2ed8"
LOCK = "9ee8353f82d2d7b821d58954e528616518ef5bc6e13b00c7091c8a58a489495b"
TOOLCHAIN = "nightly-2022-05-01"
CARGO = "291b2e2c4c311afd230b7f52c868f666bc57488359b9aef262723eedd9813099"
RUSTC = "e7547417fb26ad411989ea1aeae33dbe971e1e2127f7114a4dad84f4d90aef37"
HARNESS = "components/engine_rocks/tests/oracle_raw_mvcc.rs"
TEST = "raw_factory_collects_live_and_deleted_values"
ORIGINAL_COMMAND = (
    "cargo +nightly-2022-05-01 test --locked -p engine_rocks --test oracle_raw_mvcc "
    "raw_factory_collects_live_and_deleted_values -- --nocapture --test-threads=1"
)
ENV = {"CXXFLAGS": "-Wno-error=array-bounds -Wno-error=stringop-overflow"}
SOURCES = "/opt/reviewbench-assets/tikv-sources.toml"
COMPILER = "/opt/reviewbench-assets/rust"


def require(value, message):
    if not value:
        raise ContractError("original TiKV106: " + message)


def requested(ctx, task):
    choices = getattr(ctx, "config", {}).get("migration", {}).get(OPTION, {})
    require(isinstance(choices, dict) and set(choices) <= {TASK}, "unknown input profile")
    return task.get("id") == TASK and TASK in choices


def commands(plan):
    require(plan["command"] == ORIGINAL_COMMAND and plan["toolchain"] == TOOLCHAIN
            and plan.get("env", {}) == ENV and plan.get("features", []) == [],
            "original command, compiler, flags or features changed")
    private = [COMPILER + "/bin/cargo", *shlex.split(ORIGINAL_COMMAND)[2:]]
    public = [COMPILER + "/bin/cargo", "build", "--locked", "-p", "engine_rocks", "--lib"]
    return public, private


def verified_graphs(document, expected_packages):
    require(document["status"] == "all_original_offline_graphs_verified"
            and document["complete"] and document["full_scope"] == 402
            and document["successful_graphs"] == document["matching_full_graphs"] == 4,
            "original offline graph verification incomplete")
    observations = referenced(document["observations"], document=True)
    require(set(observations) == {v + "-" + r for v in ("start", "golden") for r in ("public", "private")},
            "original four graph views missing")
    for name, row in observations.items():
        require(row["exit_status"] == 0 and row["source_and_lock_unchanged"]
                and row["empty_cargo_home"] and row["original_git_cache_mounted"] is False
                and row["packages"] == row["nodes"] == 596 and row["workspace_members"] == 74
                and row["complete_original_lock_match"] and row["full_edges_features_identical"],
                "full source/native graph changed: " + name)
        graph = referenced(row["stdout"], document=True)
        native = referenced(row["original_graph"], document=True)
        referenced(row["stderr"])
        require({(p["name"], p["version"], p.get("source")) for p in graph["packages"]} == expected_packages
                and graph_signature(graph) == graph_signature(native) == row["graph_signature"],
                "dependency identities, edges or features changed")


def component_assets(prepared, locks):
    registry = referenced(prepared["registry"], document=True)
    git = referenced(prepared["git"], document=True)
    require(registry["registry_packages"] == 490 and git["git_packages"] == 32
            and git["git_sources"] == 17 and registry["registry_downloads"] == 0
            and git["new_dependency_resolutions"] == 0, "complete original components changed")
    view = referenced(registry["view"], document=True)
    rows = [*view["selection"], *referenced(git["vendor_audit"], document=True)]
    require(len(rows) == 522 and {(p["name"], p["version"], p["source"]): p["package_checksum"] for p in rows}
            == rust_vendor.locked_packages(locks), "vendor does not contain the complete original external graph")
    for kind, component in (("registry", registry), ("git", git)):
        asset = referenced(component["asset"], document=True)
        proof = referenced(prepared["readbacks"][kind], document=True)
        require(proof["status"] == "independent_Blob_component_readback_verified"
                and proof["asset"] == component["asset"] and proof["all_paths_bytes_full_modes_verified"]
                and proof["gzip_crc_and_eof_verified"] and proof["tree_sha256"] == asset["tree_sha256"],
                "independent " + kind + " archive verification incomplete")
    sources = tomllib.loads(referenced(git["source_config"]).decode())
    require(sources.get("net", {}).get("offline") is True, "source configuration allows online resolution")
    expected_sources = {}
    for packages, target in (
        ({p["source"] for p in view["selection"]}, "/opt/reviewbench-assets/vendor-registry"),
        ({p["source"] for p in rows if p["source"].startswith("git+")}, "/opt/reviewbench-assets/vendor-git"),
    ):
        text, _ = rust_vendor.source_configuration(packages, target=target)
        expected_sources.update(tomllib.loads(text)["source"])
    require(sources["source"] == expected_sources, "original source identities or destinations changed")
    return registry, git


def original_inputs(ctx, task, oracle, plan, patch):
    require((task["id"], task["repo"], task["start_sha"]) == (TASK, REPO, START)
            and (oracle["id"], oracle["repo"], oracle["start_sha"]) == (TASK, REPO, START),
            "task identity changed")
    require(canonical_hash(plan) == PLAN and plan["input_index"] == oracle["input_index"] == 106
            and plan["build_jobs"] == 4 and plan["timeout_seconds"] == 3600,
            "original plan or resources changed")
    commands(plan)
    require(patch["sha256"] == oracle["golden_patch_sha256"] == GOLDEN
            and file_hash(Path(patch["path"])) == GOLDEN, "original source-only golden changed")
    require(plan["session_files"][0]["path"] == HARNESS and len(plan["session_files"]) == 1
            and oracle["pass_to_pass_tests"] == [TEST], "original assertion target changed")
    prepared = referenced(ctx.config["migration"][OPTION][TASK], document=True)
    require(prepared["schema_version"] == SCHEMA and prepared["task_id"] == TASK
            and prepared["new_SUT_model_judge_calls"] == 0 and prepared["full_scope"] == 402,
            "unbound preparation")
    for key, original in (("task", task), ("oracle", oracle), ("plan", plan)):
        require(referenced(prepared[key], document=True) == original, "prepared " + key + " changed")
    inputs = referenced(prepared["original_inputs"], document=True)
    require(inputs["task_id"] == TASK and inputs["gitlinks"] == []
            and inputs["golden"]["sha256"] == GOLDEN, "original source binding changed")
    source_rows = referenced(inputs["source_preparation"], document=True)
    logs = referenced(inputs["original_logs"], document=True)
    locks, harness = [], None
    for variant in ("start", "golden"):
        row = source_rows[variant]
        require(core.snapshot(Path(row["source"])) == row["source_snapshot"]
                and core.snapshot(Path(row["declared_environment"])) == row["environment_snapshot"],
                "original source/environment copy changed")
        require(row["source_lock"]["sha256"] == row["environment_lock"]["sha256"] == LOCK,
                "original lock seed changed")
        locks.append(tomllib.loads(referenced(row["environment_lock"]).decode()))
        data = referenced(row["session_files"][HARNESS])
        require(harness in (None, data), "original harness differs across variants")
        harness = data
        require(row["original_command"] == oracle["environment"]["variant_commands"][variant] == ORIGINAL_COMMAND
                and row["original_toolchain"] == oracle["environment"]["variant_toolchains"][variant] == TOOLCHAIN,
                "original per-variant command/compiler changed")
        require(len(logs[variant]) == len(oracle["runs"][variant]) == 3, "six original observations missing")
        for reference, run in zip(logs[variant], oracle["runs"][variant]):
            text = referenced(reference).decode()
            require(Path(reference["path"]).resolve() == (ctx.legacy / "rust" / run["log"]).resolve()
                    and run["ok"] and run["passed_tests"] == [TEST] and not run["failed_tests"]
                    and "test " + TEST + " ... ok" in text, "original test evidence changed")
    from .rust_overlay import session_bytes
    require(harness == session_bytes(ctx, plan, oracle, plan["session_files"][0]),
            "prepared harness bytes differ from original input")
    expected = {(p["name"], p["version"], p.get("source")) for p in locks[0]["package"]}
    require(locks[0] == locks[1] and len(expected) == 596, "original complete lock changed")
    verified_graphs(referenced(prepared["offline_graphs"], document=True), expected)
    registry, git = component_assets(prepared, locks)
    return prepared, registry, git, harness


def migrate(ctx, task, oracle):
    from . import rust_lane, rust_overlay
    plan = rust_lane.load_plan(ctx, oracle)
    patch = ctx.golden(task, oracle, "rust", ctx.task_directory(task, oracle, "rust"))
    prepared, registry, git, harness = original_inputs(ctx, task, oracle, plan, patch)
    toolchains = json.loads(Path(ctx.config["rust_toolchains"]).read_text())
    compiler = Path(toolchains[TOOLCHAIN]["root"])
    require(file_hash(compiler / "bin/cargo") == CARGO and file_hash(compiler / "bin/rustc") == RUSTC,
            "original compiler bytes changed")
    build_spec, defaults = rust_overlay.build_specification(ctx, task, plan, oracle, patch)
    require(defaults == {} and build_spec["locks"] == [] and build_spec["private_members"] == []
            and [r["path"] for r in build_spec["manifests"]] == ["components/tikv_util/Cargo.toml"],
            "symmetric environment overlay acquired unrelated inputs")
    image, audit = ctx.image(rust_lane.IMAGE)
    assets = [
        {**rust_lane.compiler(ctx, TOOLCHAIN), "kind": "archive", "visibility": "developer", "target": COMPILER},
        {**registry["asset"], "kind": "archive", "visibility": "developer",
         "target": "/opt/reviewbench-assets/vendor-registry"},
        {**git["asset"], "kind": "archive", "visibility": "developer",
         "target": "/opt/reviewbench-assets/vendor-git"},
        {**ctx.copy_asset(Path(git["source_config"]["path"]), "tikv-sources"), "kind": "file",
         "visibility": "developer", "target": SOURCES},
        {**rust_overlay.build_tools(ctx), "kind": "archive", "visibility": "developer",
         "target": rust_overlay.TOOLS},
    ]
    spec = ctx.output / "tikv-build-overlay.json"
    atomic_json(spec, build_spec)
    assets.append({**ctx.copy_asset(spec, "tikv-overlay"), "kind": "file",
                   "visibility": "developer", "target": rust_overlay.SPEC})
    private = ctx.output / "tikv-original-oracle.rs"
    private.write_bytes(harness)
    test_asset = "/opt/reviewbench-assets/tikv-original-oracle.rs"
    assets.append({**ctx.copy_asset(private, "tikv-original-test"), "kind": "file",
                   "visibility": "grader", "target": test_asset})
    public, command = commands(plan)
    env = {
        "PATH": COMPILER + "/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "RUSTC": COMPILER + "/bin/rustc", "CARGO_HOME": "/opt/reviewbench-cache/cargo",
        "CARGO_TARGET_DIR": "/opt/reviewbench-cache/target", "CARGO_BUILD_JOBS": "4",
        "CARGO_TERM_COLOR": "never", "CARGO_INCREMENTAL": "0", "CARGO_NET_OFFLINE": "true",
        "CARGO_PROFILE_DEV_DEBUG": "0", "CARGO_PROFILE_TEST_DEBUG": "0",
        "RUSTFLAGS": "--cap-lints=allow", "RUST_BACKTRACE": "1", "SKIP_WASM_BUILD": "1",
        "RUST_BUILD_DOCKER": "1", "GIT_COMMIT": "reviewbench-hard-oracle",
        "GIT_DIRTY": "false", "SOURCE_TIMESTAMP": "0", "TMPDIR": "/workspace/.reviewbench/tmp",
        "PROTOC_INCLUDE": "/usr/include", **ENV,
    }
    destination = "/workspace/" + HARNESS
    recipe = RuntimeRecipe(
        task_id=TASK, repo=REPO, start_sha=START, lane="rust", image=image, image_audit=audit,
        workdir="/workspace", cpus=4, memory_mb=8192, build_scratch_mb=24576,
        command_timeout_seconds=3600, env=env,
        cache_mounts={"cargo": "/opt/reviewbench-cache/cargo", "target": "/opt/reviewbench-cache/target",
                      "rust-build": rust_overlay.BUILD_CACHE},
        bootstrap=["mkdir -p /workspace/.reviewbench/tmp /opt/reviewbench-cache/cargo",
                   "cp " + SOURCES + " /opt/reviewbench-cache/cargo/config.toml"],
        dev_checks=[rust_overlay.command_wrapper(public, ".")], scratch_paths=[], assets=assets,
        submodules=[],
        grading={"multifile_build": True, "immutable_paths": ctx.immutable_tests(task), "golden_patch": patch,
                 "bootstrap": ["mkdir -p " + shlex.quote(str(Path(destination).parent))
                               + " && cp " + test_asset + " " + shlex.quote(destination)],
                 "commands": [{"command": rust_overlay.command_wrapper(command, "."),
                               "parser": "rust-test", "expected_tests": [TEST]}]},
    )
    recipe.validate()
    return recipe
