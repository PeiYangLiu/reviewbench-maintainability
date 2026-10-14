"""Composable2's complete original workspace, patch catalog and airdrop test."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import tomllib

from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, canonical_hash, file_hash, atomic_json,
)
from reviewbench_maintainability.agentic.snapshots import start_archive, extract_tar, tree_hash
from . import rust_image_entry as entry, rust_vendor

ID = "github__ComposableFi__composable__pull_948__comment_899153377__modularity"
OPTION = "rust_original_composable_airdrop"
PROFILE = {
    "repo": "ComposableFi/composable", "start": "06ce3dae513543eb1e58c9ce8fb03f2ff4f806d9",
    "plan": "dde6c4b49a5ba932d2eb88021adfc3d8f690f33243d53fb7a414c443d881c059",
    "golden": "4e8cd3c0c9455761b084cac66cfb5818932f5f364c6e9547c8995b0454cc96be",
    "locks": {"start": "b378f25e648e9dadb2ab27cb42605810ca568653e020524751f860d71697ae6d",
              "golden": "7cb3675c6e71f556fe2bb048eb677f059016dbfce22df0c6df3785288501893b"},
    "cargo": "e8f1f270734aac106f29a01b8d4f8a0fead9a4a9f58d969f925f59d39214151d",
    "rustc": "9ee8579efb4542894018f5df69f01209f5bbfc4a0e5c5e67625a4d108c3a9956",
    "harness": "cbc43043698f13f4b2a375dfb25b0427a37cac8fc55d9ea83bd330e23542c413",
    "registry_asset": "7d418b7dcfee0415a6a7c3e7b548516e19dc0f7a3f5a36f52b036a417072ff3c",
    "registry_tree": "4c779fc6900d51a9afe3b370935bf2e179154e5bfb2c4a46fe01fa6bf5f920e8",
    "git_asset": "7b07cc031ba3e6e6d6d99447721ec5ef6d4e9ca5724a8386d64058a32ad28e6e",
    "git_tree": "dcd14f9bc2a07f5d68bf53d241890425f494b223616e023e2fb8096ddd115975",
}
TOOLCHAIN = "nightly-2022-12-20"
PACKAGE = "pallet-airdrop"
MANIFEST = "frame/airdrop/Cargo.toml"
HARNESS = "frame/airdrop/tests/oracle.rs"
TESTS = ["__construct_runtime_integrity_test::runtime_integrity_tests",
         "claim_with_invalid_ethereum_proof_is_rejected"]
COMMAND = ["test", "-p", PACKAGE, "--test", "oracle", "--", "--nocapture"]
# Produce external rlibs once, not only check-mode rmeta. The original grader
# harness is absent; the shared prewarmer still excludes all workspace outputs.
PUBLIC = ["build", "--locked", "-p", PACKAGE]
SPEC = "/opt/reviewbench-assets/airdrop-public.json"
PRIVATE = "/opt/reviewbench-assets/airdrop-private.json"
SOURCES = "/opt/reviewbench-assets/airdrop-sources.toml"
TOOL = "/opt/reviewbench-assets/rust-build-tools/airdrop.py"


def require(value, message):
    if not value:
        raise ContractError("original airdrop: " + message)


def referenced(row, *, document=False):
    p = Path(row["path"])
    require(p.is_absolute() and p.is_file() and not p.is_symlink()
            and p.stat().st_size <= 32 * 1024**2 and file_hash(p) == row["sha256"], "prepared input changed")
    data = p.read_bytes()
    require(hashlib.sha256(data).hexdigest() == row["sha256"], "input changed during read")
    return json.loads(data) if document else data


def requested(ctx, task):
    choices = getattr(ctx, "config", {}).get("migration", {}).get(OPTION, {})
    require(isinstance(choices, dict) and set(choices) <= {ID}, "unknown airdrop profile")
    return task.get("id") in choices


def original_inputs(ctx, task, oracle, plan, patch):
    require(task["id"] == oracle["id"] == ID and task["repo"] == oracle["repo"] == PROFILE["repo"]
            and task["start_sha"] == oracle["start_sha"] == PROFILE["start"], "task identity changed")
    require(canonical_hash(plan) == PROFILE["plan"] and plan["input_index"] == oracle["input_index"] == 2
            and shlex.split(plan["command"]) == ["cargo", "+" + TOOLCHAIN, *COMMAND]
            and plan["toolchain"] == TOOLCHAIN and plan["build_jobs"] == 8 and plan["timeout_seconds"] == 3600,
            "original command/compiler/resources changed")
    require(patch["sha256"] == oracle["golden_patch_sha256"] == PROFILE["golden"]
            and file_hash(Path(patch["path"])) == PROFILE["golden"], "full source-only golden changed")
    require(oracle["pass_to_pass_tests"] == TESTS and len(plan["session_files"]) == 1
            and plan["session_files"][0]["path"] == HARNESS
            and not any(plan.get(key) for key in ("prepare_command", "test_compatibility_replacements",
                                                 "environment_replacements", "environment_file_replacements")),
            "original session/preparation changed")
    aggregate = referenced(ctx.config["migration"][OPTION][ID], document=True)
    require(aggregate["status"] == "original_airdrop_inputs_bound" and aggregate["task_id"] == ID
            and aggregate["new_resolution_SUT_model_judge_calls"] == 0, "original preparation unbound")
    inputs = referenced(aggregate["original_inputs"], document=True)
    prepared = referenced(aggregate["graphs"], document=True)
    registry = referenced(aggregate["registry"], document=True)
    catalog = referenced(aggregate["git_catalog"], document=True)
    archive = referenced(aggregate["git_archive"], document=True)
    for name, value in (("task", task), ("oracle", oracle), ("plan", plan)):
        require(referenced(inputs[name], document=True) == value, "original " + name + " differs")
    require(prepared["status"] == "original_full_unique_graphs_verified" and prepared["all_complete"]
            and prepared["unique_lock_selection_verified"], "original full graphs have not been verified")
    source_rows = referenced(inputs["source_preparation"], document=True)
    observations = referenced(prepared["observations"], document=True)
    controls = referenced(prepared["cross_lock_controls"], document=True)
    harness = referenced(inputs["harness"])
    require(hashlib.sha256(harness).hexdigest() == PROFILE["harness"]
            and harness == plan["session_files"][0]["content"].encode(), "original assertion bytes changed")
    locks = []
    for variant, digest in PROFILE["locks"].items():
        retained = inputs["locks"][variant]
        require(retained["head"] == PROFILE["start"] and retained["lock_hash_is_recovery_pin_not_oracle_field"],
                "retained lock provenance was relabelled as an original Oracle field")
        data = referenced(retained["lock"])
        require(hashlib.sha256(data).hexdigest() == digest, "original retained lock changed")
        lock = tomllib.loads(data.decode())
        locks.append(lock)
        require(len(lock["package"]) == 1230 and source_rows[variant]["committed_lock_sha256"] ==
                PROFILE["locks"]["start"], "complete reference lock changed")
        require(oracle["environment"]["variant_commands"][variant] == plan["command"]
                and oracle["environment"]["variant_toolchains"][variant] == TOOLCHAIN,
                "original per-variant command/compiler changed")
        require(len(oracle["runs"][variant]) == 3, "original repetitions missing")
        for row in oracle["runs"][variant]:
            log = (ctx.legacy / "rust" / row["log"]).read_text()
            require(row["ok"] and row["passed_tests"] == TESTS and not row["failed_tests"]
                    and all("test " + test + " ... ok" in log for test in TESTS),
                    "original successful evidence changed")
        with tempfile.TemporaryDirectory(prefix="airdrop-original-source-", dir=ctx.scratch) as tmp:
            root = Path(tmp) / "source"
            with start_archive(ctx.repo(task), task["start_sha"]) as stream:
                extract_tar(stream, root)
            if variant == "golden":
                result = subprocess.run(["git", "apply", "--binary", "--whitespace=nowarn", patch["path"]],
                                        cwd=root, env={**os.environ, "GIT_CEILING_DIRECTORIES": tmp},
                                        capture_output=True, text=True)
                require(result.returncode == 0, "original full golden does not apply")
            require(entry.snapshot(root) == source_rows[variant]["snapshot"], "complete original source changed")
        for role in ("public", "private"):
            row = observations[variant + "-" + role]
            require(row["exit_status"] == 0 and row["source_and_lock_unchanged"]
                    and row["empty_cargo_home"] and row["original_cache_not_mounted"]
                    and row["lock_sha256"] == digest and row["full_original_graph_unchanged"],
                    "original offline graph failed")
            graph = referenced(row["stdout"], document=True)
            referenced(row["stderr"])
            require(len(graph["packages"]) == len(graph["resolve"]["nodes"]) == 1230
                    and len(graph["workspace_members"]) == 54
                    and {(p["name"], p["version"], p.get("source")) for p in graph["packages"]} ==
                    {(p["name"], p["version"], p.get("source")) for p in lock["package"]},
                    "original full graph was narrowed")
        row = controls[variant]
        require(row["exit_status"] == 101 and row["source_and_lock_unchanged"]
                and entry.catalog_lock_incompatible({"command": row["command"], "cwd": row["cwd"], "returncode": 101,
                                                     "stderr": referenced(row["stderr"]).decode()}),
                "opposite original lock is not uniquely incompatible")
    expected = rust_vendor.locked_packages(locks)
    registry_rows = referenced(registry["vendor_audit"], document=True)
    locked_rows = referenced(catalog["locked_audit"], document=True)
    unused_rows = referenced(catalog["unused_catalog_audit"], document=True)
    require({(r["name"], r["version"], r["source"]): r["package_checksum"]
             for r in [*registry_rows, *locked_rows]} == expected
            and len(registry_rows) == 888 and len(locked_rows) == 288 and len(unused_rows) == 57,
            "original dependency/catalog closure changed")
    require(not set(expected) & {(r["name"], r["version"], r["source"]) for r in unused_rows}
            and catalog["physical_git_packages"] == 345 and archive["status"] == "git_catalog_archived",
            "unused source candidates were converted into resolved dependencies")
    for key, component, asset in (("registry", registry, registry["asset"]),
                                  ("git", catalog, archive["asset"])):
        descriptor = referenced(asset, document=True)
        require(asset["sha256"] == PROFILE[key + "_asset"]
                and descriptor["tree_sha256"] == PROFILE[key + "_tree"] == tree_hash(Path(component["source"])),
                "original source asset bytes changed")
    for key in ("cargo", "rustc"):
        row = inputs["compiler"][key]
        require(row["sha256"] == PROFILE[key] == file_hash(Path(row["path"])), "original compiler changed")
    require(ctx.modules(task) == [], "original workspace gained a submodule")
    return inputs, registry, catalog, archive, harness


def migrate(ctx, task, oracle):
    from . import rust_lane, rust_overlay
    plan = rust_lane.load_plan(ctx, oracle)
    patch = ctx.golden(task, oracle, "rust", ctx.task_directory(task, oracle, "rust"))
    inputs, registry, catalog, archive, harness = original_inputs(ctx, task, oracle, plan, patch)
    image, image_audit = ctx.image(rust_lane.IMAGE)
    assets = []

    def asset(value, target, role="developer", kind="file"):
        assets.append({**value, "target": target, "visibility": role, "kind": kind})
        return {"path": target, "sha256": value["sha256"]}

    asset(rust_lane.compiler(ctx, TOOLCHAIN), "/opt/reviewbench-assets/rust", kind="archive")
    asset(registry["asset"], "/opt/reviewbench-assets/vendor-registry", kind="archive")
    asset(archive["asset"], "/opt/reviewbench-assets/vendor-git", kind="archive")
    config = asset(ctx.copy_asset(Path(catalog["source_config"]["path"]), "airdrop-sources"), SOURCES)
    locks = [asset(ctx.copy_asset(Path(inputs["locks"][variant]["lock"]["path"]), "airdrop-locks"),
                   "/opt/reviewbench-assets/airdrop-lock-" + str(number))
             for number, variant in enumerate(("start", "golden"))]
    public = {
        "schema_version": entry.CATALOG_WORKSPACE_SCHEMA, "task_id": ID, "selection": entry.SELECTION,
        "workdir": ".", "lock_path": "Cargo.lock", "locks": locks,
        "reference_seed_sha256": PROFILE["locks"]["start"],
        "reference_seed_policy": "replace-only-identical-committed-seed-in-private-mirror",
        "package": PACKAGE, "package_manifest": MANIFEST, "public_command": PUBLIC,
        "cargo": {"path": "/opt/reviewbench-assets/rust/bin/cargo", "sha256": PROFILE["cargo"]},
        "rustc": {"path": "/opt/reviewbench-assets/rust/bin/rustc", "sha256": PROFILE["rustc"]},
        "source_config": config, "source_config_mode": "isolated-cargo-home",
        "git_dependencies": "full-locked-source-identities",
        "cpus": 8, "timeout_seconds": 3600, "metadata_timeout_seconds": 180, "max_log_bytes": 32 * 1024**2,
    }
    private = {"schema_version": entry.CATALOG_WORKSPACE_PRIVATE_SCHEMA, "task_id": ID,
               "original_unlocked": True, "package_manifest": MANIFEST, "command": COMMAND,
               "harness": {"path": HARNESS, "sha256": PROFILE["harness"]}, "expected_tests": TESTS}
    entry.validate(public, private)
    for name, data, target, role in (("public", public, SPEC, "developer"), ("private", private, PRIVATE, "grader")):
        path = ctx.output / ("airdrop-" + name + ".json")
        atomic_json(path, data)
        asset(ctx.copy_asset(path, "airdrop-specs"), target, role)
    tools = rust_overlay.build_tools(ctx, extra_entries={"airdrop.py": Path(entry.__file__)})
    asset(tools, rust_overlay.TOOLS, kind="archive")
    original_test = asset(ctx.copy_asset(Path(inputs["harness"]["path"]), "airdrop-original-tests"),
                          "/opt/reviewbench-assets/airdrop-original-test.rs", "grader")
    prefix = ["python3", TOOL, "--spec", SPEC, "--task-id", ID]
    destination = "/workspace/" + HARNESS
    recipe = RuntimeRecipe(
        task_id=ID, repo=task["repo"], start_sha=task["start_sha"], lane="rust", image=image, image_audit=image_audit,
        workdir="/workspace", cpus=8, memory_mb=8192, build_scratch_mb=8192, command_timeout_seconds=3600,
        env={"PATH": "/opt/reviewbench-assets/rust/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
             "RUSTC": "/opt/reviewbench-assets/rust/bin/rustc", "CARGO_HOME": "/opt/reviewbench-cache/cargo",
             "CARGO_TARGET_DIR": "/opt/reviewbench-cache/target", "CARGO_NET_OFFLINE": "true",
             "CARGO_BUILD_JOBS": "8", "CARGO_INCREMENTAL": "0", "CARGO_TERM_COLOR": "never",
             "CARGO_PROFILE_DEV_DEBUG": "0", "CARGO_PROFILE_TEST_DEBUG": "0", "RUSTFLAGS": "--cap-lints=allow",
             "RUST_BACKTRACE": "1", "PROTOC_INCLUDE": "/usr/include", "SKIP_WASM_BUILD": "1",
             "RUST_BUILD_DOCKER": "1", "GIT_COMMIT": "reviewbench-hard-oracle", "GIT_DIRTY": "false",
             "SOURCE_TIMESTAMP": "0", "HOME": "/tmp", "TMPDIR": "/workspace/.reviewbench/tmp"},
        assets=assets, cache_mounts={"cargo": "/opt/reviewbench-cache/cargo", "target": "/opt/reviewbench-cache/target",
                                    "rust-build": rust_overlay.BUILD_CACHE},
        bootstrap=["mkdir -p /workspace/.reviewbench/tmp"], dev_checks=[shlex.join(prefix)], scratch_paths=[],
        grading={"multifile_build": True, "immutable_paths": ctx.immutable_tests(task), "golden_patch": patch,
                 "bootstrap": ["mkdir -p " + str(Path(destination).parent) + " && cp " +
                               original_test["path"] + " " + destination],
                 "commands": [{"command": shlex.join([*prefix, "--private", PRIVATE]), "parser": "rust-test",
                               "expected_tests": TESTS}]},
    )
    recipe.validate()
    return recipe
