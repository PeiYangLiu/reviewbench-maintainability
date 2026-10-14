"""Explicit published-legacy Fuel197 runtime, retaining all original77 repository tests."""
import hashlib
import json
from pathlib import Path
import re
import shlex
import tomllib

from reviewbench_maintainability.agentic.contracts import ContractError, RuntimeRecipe, atomic_json, canonical_hash, file_hash
from . import rust_fuel_entry as entry, rust_vendor
from .rust_composable import referenced
from .rust_local_registry import graph_signature

TASK = entry.TASK
OPTION = "rust_original_fuel_inputs"
SCHEMA = "reviewbench-published-legacy-fuel4-inputs/v1"
START = "30d88423ac0e00f2d0a54c314b4dc59ef38238c1"
TASK_HASH = "df573d766bdb00b93ba8bee643c7ea177644bab771a478da60fed9d5167dbc83"
ORACLE_HASH = "ed53be209adbb6ef7d3b31dcab6bf0c5d26370089424f9f20c47bac3cbaf45b1"
GOLDEN = "03169f84206ccc9c0758aecf79cad047c9da8fe450935219ee5a3e1b0ca6d213"
CARGO = "9548937d530bf439ff1ba47a3b2bd26eeb9c3aff1961c20c01798613de922578"
RUSTC = "d32249a7c3bfcfc67b471460386e46323accae7125e344567a12d5664d99bb57"
LOCKS = {"start": "50ac67209069a5cdbff2287603aac7a497e5beed5751c700a4e99d0508e2a91d",
         "golden": "cba137902afcd9990e4ecfddd6193c83bcdd79f84417ccec3996d74921d98cc6"}
SPEC = "/opt/reviewbench-assets/fuel-public.json"
PRIVATE = "/opt/reviewbench-assets/fuel-private.json"
TOOL = "/opt/reviewbench-assets/rust-build-tools/fuel.py"
COMMAND = "cargo +1.93.0 test -p fuel-tx --all-features -- --nocapture"


def require(value, message):
    if not value:
        raise ContractError("original published Fuel: " + message)


def requested(ctx, task):
    settings = getattr(ctx, "config", {}).get("migration", {}).get(OPTION, {})
    require(isinstance(settings, dict) and set(settings) <= {TASK}, "unknown original task profile")
    return task.get("id") == TASK and TASK in settings


def original_inputs(ctx, task, oracle):
    require(canonical_hash(task) == TASK_HASH and canonical_hash(oracle) == ORACLE_HASH,
            "original release task/oracle changed")
    require(task["id"] == oracle["id"] == TASK and task["start_sha"] == START
            and task["repo"] == "FuelLabs/fuel-tx" and not oracle.get("lane_results_source"),
            "this is not the published-legacy Fuel task")
    require(oracle["workspace_command"] == COMMAND and oracle["toolchain"] == "1.93.0"
            and oracle["pass_to_pass"] == len(set(oracle["pass_to_pass_tests"])) == 77
            and oracle["test_harness"]["kind"] == "unchanged repository tests",
            "original command/compiler/tests changed")
    prepared = referenced(ctx.config["migration"][OPTION][TASK], document=True)
    require(prepared["schema_version"] == SCHEMA and prepared["task_id"] == TASK
            and prepared["full_scope"] == 402, "unbound preparation")
    require(referenced(prepared["task"], document=True) == task
            and referenced(prepared["oracle"], document=True) == oracle, "prepared source records changed")
    require(hashlib.sha256(referenced(prepared["expanded_patch"])).hexdigest() == GOLDEN,
            "expanded source-only golden changed")
    sources = referenced(prepared["sources"], document=True)
    original_logs = referenced(prepared["original_logs"], document=True)
    blocks = referenced(prepared["inline_test_blocks"], document=True)
    files = referenced(prepared["original_test_files"], document=True)
    require(len(blocks) == 7 and len(files) == 23, "original repository assertion inputs changed")
    for variant in ("start", "golden"):
        row = sources[variant]
        root = Path(row["source"])
        require(entry.core.snapshot(root) == row["snapshot"] and len(row["snapshot"]) == 64
                and "Cargo.lock" not in row["snapshot"] and root.name == "fuel-tx",
                "original full source or missing-lock contract changed")
        for block in blocks:
            data = (root / block["source_path"]).read_bytes()
            marker = block["marker"].encode()
            require(data.count(marker) == 1, "original test marker changed")
            tail = data[data.index(marker):]
            require(len(tail) == block["bytes"] and hashlib.sha256(tail).hexdigest() == block["sha256"],
                    "original inline test block changed")
        entry.inline.verify_blocks(root, blocks)
        for item in files:
            require(file_hash(root / item["path"]) == item["sha256"], "original external test/snapshot changed")
        require(len(original_logs[variant]) == len(oracle["runs"][variant]) == 3, "six original observations missing")
        for proof, original in zip(original_logs[variant], oracle["runs"][variant]):
            text = referenced(proof).decode()
            passed = re.findall(r"^test ([^\r\n]+?) \.\.\. ok$", text, re.M)
            require(original["ok"] and original["passed"] == 77 and original["failed"] == 0
                    and len(passed) == 77 and set(passed) == set(oracle["pass_to_pass_tests"]),
                    "original77 test results changed")
    native = referenced(prepared["native_graphs"], document=True)
    compact = referenced(prepared["compact_registry"], document=True)
    readback = referenced(prepared["registry_readback"], document=True)
    offline = referenced(prepared["offline_graphs"], document=True)
    require(native["status"] == "both_complete_native_Fuel_graphs_derived"
            and native["original_lock_recovered"] is False
            and compact["registry_packages"] == 149 and compact["original_lock_recovered"] is False,
            "newly derived environment was mislabeled or narrowed")
    descriptor = referenced(compact["asset"], document=True)
    require(readback["status"] == "independent_Blob_component_readback_verified"
            and readback["asset"] == compact["asset"] and readback["all_paths_bytes_full_modes_verified"]
            and readback["gzip_crc_and_eof_verified"] and readback["tree_sha256"] == descriptor["tree_sha256"],
            "independent dependency readback incomplete")
    require(offline["status"] == "both_complete_Fuel_offline_graphs_verified", "offline transport incomplete")
    locks = []
    for row in offline["observations"]:
        variant = row["variant"]
        require(row["exit_status"] == 0 and row["full_edges_features_identical"]
                and row["source_and_lock_unchanged"] and row["empty_Cargo_home"]
                and row["packages"] == row["nodes"] == (150 if variant == "start" else 151)
                and row["workspace_members"] == 1, "full native graph changed")
        require(row["lock"]["sha256"] == LOCKS[variant], "newly derived lock changed")
        locks.append(tomllib.loads(referenced(row["lock"]).decode()))
        require(graph_signature(referenced(row["stdout"], document=True))
                == graph_signature(referenced(row["original_graph"], document=True)),
                "full graph edges/features changed")
    inventory = referenced(compact["package_inventory"], document=True)
    require({(r["name"], r["version"], r["source"]): r["checksum"] for r in inventory}
            == rust_vendor.locked_packages(locks), "compact transport dropped a locked dependency")
    for key, checksum in (("cargo", CARGO), ("rustc", RUSTC)):
        require(prepared["compiler"][key]["sha256"] == file_hash(Path(prepared["compiler"][key]["path"])) == checksum,
                "original compiler changed")
    require(ctx.modules(task) == [], "source submodule policy changed")
    return prepared, compact, offline, blocks, files


def migrate(ctx, task, oracle):
    from . import rust_lane, rust_overlay
    prepared, compact, offline, blocks, files = original_inputs(ctx, task, oracle)
    image, audit = ctx.image(rust_lane.IMAGE)
    assets = []

    def asset(value, target, visibility="developer", kind="file"):
        assets.append({**value, "target": target, "visibility": visibility, "kind": kind})
        return {"path": target, "sha256": value["sha256"]}

    asset(rust_lane.compiler(ctx, "1.93.0"), "/opt/reviewbench-assets/rust", kind="archive")
    asset(compact["asset"], "/opt/reviewbench-assets/fuel-registry", kind="archive")
    source_config = ctx.output / "fuel-sources.toml"
    source_config.write_text(
        '[source.crates-io]\nreplace-with="fuel-fixed-native-registry"\n'
        '[source.fuel-fixed-native-registry]\nlocal-registry="/opt/reviewbench-assets/fuel-registry"\n'
        '[net]\noffline=true\nretry=0\n')
    config = asset(ctx.copy_asset(source_config, "fuel-sources"), "/opt/reviewbench-assets/fuel-sources.toml")
    locks = []
    for variant in ("start", "golden"):
        row = next(r for r in offline["observations"] if r["variant"] == variant)
        locks.append(asset(ctx.copy_asset(Path(row["lock"]["path"]), "fuel-derived-locks"),
                           "/opt/reviewbench-assets/fuel-" + variant + ".lock"))
    private = {"schema_version": entry.PRIVATE_SCHEMA, "task_id": TASK, "command": entry.COMMAND,
               "expected_tests": oracle["pass_to_pass_tests"], "test_targets": entry.TARGETS,
               "inline_test_blocks": blocks, "files": files}
    public = {"schema_version": entry.SCHEMA, "task_id": TASK, "selection": entry.SELECTION,
              "mirror_basename": "fuel-tx", "public_command": entry.PUBLIC, "locks": locks,
              "original_lock_recovered": False, "derivation": "original logged versions and native Cargo-selected platform inputs",
              "cargo": {"path": "/opt/reviewbench-assets/rust/bin/cargo", "sha256": CARGO},
              "rustc": {"path": "/opt/reviewbench-assets/rust/bin/rustc", "sha256": RUSTC},
              "source_config": config, "cpus": 16, "timeout_seconds": 3600,
              "metadata_timeout_seconds": 180, "max_log_bytes": 16 * 1024**2,
              "private_fingerprint": canonical_hash(private)}
    entry.validate(public, private)
    for name, value, target, role in (("public", public, SPEC, "developer"), ("private", private, PRIVATE, "grader")):
        path = ctx.output / ("fuel-" + name + ".json")
        atomic_json(path, value)
        asset(ctx.copy_asset(path, "fuel-specs"), target, role)
    tools = rust_overlay.build_tools(ctx, extra_entries={
        "fuel.py": Path(entry.__file__), "image_locks.py": Path(entry.core.__file__),
        **{name: Path(__file__).with_name(name) for name in (
            "rust_libsbp_entry.py", "rust_wash_compat_entry.py", "rust_test_compat_entry.py", "rust_api_entry.py")}})
    asset(tools, rust_overlay.TOOLS, kind="archive")
    patch = ctx.copy_asset(Path(prepared["expanded_patch"]["path"]), "fuel-original-expanded-golden")
    prefix = ["python3", TOOL, "--spec", SPEC, "--task-id", TASK]
    recipe = RuntimeRecipe(
        task_id=TASK, repo=task["repo"], start_sha=START, lane="rust", image=image, image_audit=audit,
        cpus=16, memory_mb=16384, build_scratch_mb=4096, command_timeout_seconds=3600, workdir="/workspace",
        env={"PATH": "/opt/reviewbench-assets/rust/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
             "RUSTC": "/opt/reviewbench-assets/rust/bin/rustc", "CARGO_TARGET_DIR": "/opt/reviewbench-cache/target",
             "CARGO_NET_OFFLINE": "true", "CARGO_BUILD_JOBS": "16", "CARGO_INCREMENTAL": "0",
             "CARGO_TERM_COLOR": "never", "CARGO_PROFILE_DEV_DEBUG": "0", "CARGO_PROFILE_TEST_DEBUG": "0",
             "RUSTFLAGS": "--cap-lints=allow", "RUST_BACKTRACE": "1", "PROTOC_INCLUDE": "/usr/include",
             "RUST_BUILD_DOCKER": "1", "GIT_COMMIT": "reviewbench-oracle", "GIT_DIRTY": "false",
             "SOURCE_TIMESTAMP": "0", "SKIP_WASM_BUILD": "1", "HOME": "/tmp", "TMPDIR": "/workspace/.reviewbench/tmp"},
        assets=assets, cache_mounts={"target": "/opt/reviewbench-cache/target", "rust-build": rust_overlay.BUILD_CACHE},
        bootstrap=["mkdir -p /workspace/.reviewbench/tmp"], dev_checks=[shlex.join(prefix)],
        scratch_paths=[], submodules=[],
        grading={"multifile_build": True, "immutable_paths": sorted(set(ctx.immutable_tests(task)) | {r["path"] for r in files}),
                 "golden_patch": patch, "bootstrap": [],
                 "commands": [{"command": shlex.join([*prefix, "--private", PRIVATE]),
                               "parser": "rust-test", "expected_tests": oracle["pass_to_pass_tests"]}]})
    recipe.validate()
    return recipe
