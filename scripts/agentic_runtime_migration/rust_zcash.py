"""Original librustzcash114 graphs, symmetric compatibility and SQLite wallet test."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import tomllib

from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, atomic_json, canonical_hash, file_hash,
)
from reviewbench_maintainability.agentic.snapshots import start_archive, extract_tar, tree_hash
from . import rust_zcash_entry as entry

ID = "github__zcash__librustzcash__pull_2065__comment_2615041570__modularity"
OPTION = "rust_original_zcash_wallet"
PROFILE = {
    "repo": "zcash/librustzcash", "start": "cb1a6a6a7b51abf5972a40f44b48c68727625189",
    "plan": "257702eaed35da7b9c05778585cc326a882a5a53c36d93820205bc3c13604d43",
    "golden": "885d45cc0807ae3ccce4d7a29884a9ec799a1e26737c50b1060360148b4b85f0",
    "locks": {"start": "7a5ade7ff63cff669e5df3a5294912662a34c6d7d53f9e9a55412a358fc60958",
              "golden": "6f9b9fb92c8f1a9bcdef3ab0a69085b8e74e1e5fc4e9565c4ce9c92af1831323"},
    "cargo": "87e9ff02f95ea17d877e7c4eeb0f365f50fe3827f5afec132131ff3b8031a57e",
    "rustc": "f8d033fd72e878ec60493788c8bffec2e6c154dcab6f0cb4c145af8ada26a7ed",
    "harness": "052d8c758f7b7f920fb08cdf4805a293a9124a322772c45712407b0e34f908f7",
    "vendor_tree": "2b28cbddcca6c85bcde20b2bf6df7e7cee56f7a07f3e2e94e7bc3404e830ba50",
    "vendor_asset": "c741171ba43dd35afbb176cfc40d05850b45e6c0d0a6c199922c6b41f6d27d5c",
}
TEST = "public_wallet_api_maintains_the_external_transparent_gap"
SPEC = "/opt/reviewbench-assets/wallet-public.json"
PRIVATE = "/opt/reviewbench-assets/wallet-private.json"
SOURCES = "/opt/reviewbench-assets/wallet-sources.toml"
TOOL = "/opt/reviewbench-assets/rust-build-tools/wallet.py"


def require(value, message):
    if not value:
        raise ContractError("original wallet: " + message)


def referenced(row, *, document=False):
    p = Path(row["path"])
    require(p.is_absolute() and p.is_file() and not p.is_symlink()
            and p.stat().st_size <= 32 * 1024**2 and file_hash(p) == row["sha256"], "prepared input changed")
    data = p.read_bytes()
    require(hashlib.sha256(data).hexdigest() == row["sha256"], "input changed during read")
    return json.loads(data) if document else data


def requested(ctx, task):
    choices = getattr(ctx, "config", {}).get("migration", {}).get(OPTION, {})
    require(isinstance(choices, dict) and set(choices) <= {ID}, "unknown wallet profile")
    return task.get("id") in choices


def original_inputs(ctx, task, oracle, plan, patch):
    require(task["id"] == oracle["id"] == ID and task["repo"] == oracle["repo"] == PROFILE["repo"]
            and task["start_sha"] == oracle["start_sha"] == PROFILE["start"], "task identity changed")
    require(canonical_hash(plan) == PROFILE["plan"] and plan["input_index"] == oracle["input_index"] == 114
            and shlex.split(plan["command"]) == ["cargo", "+1.85.1", *entry.COMMAND]
            and plan["toolchain"] == "1.85.1" and plan["build_jobs"] == 4 and plan["timeout_seconds"] == 3600,
            "original command/compiler changed")
    require(patch["sha256"] == oracle["golden_patch_sha256"] == PROFILE["golden"]
            and file_hash(Path(patch["path"])) == PROFILE["golden"], "complete source-only golden changed")
    require(oracle["pass_to_pass_tests"] == [TEST] and len(plan["session_files"]) == 1
            and plan["session_files"][0]["path"] == entry.HARNESS
            and not plan.get("prepare_command") and not plan.get("environment_replacements"),
            "original session or preparation changed")
    prepared = referenced(ctx.config["migration"][OPTION][ID], document=True)
    require(prepared["status"] == "original_compatibility_and_unique_graphs_verified"
            and prepared["task_id"] == ID and prepared["new_resolution_SUT_model_judge_calls"] == 0
            and prepared["original_plan_sha256"] == PROFILE["plan"], "original preparation unbound")
    for name, value in (("task", task), ("oracle", oracle), ("plan", plan)):
        require(referenced(prepared[name], document=True) == value, "original " + name + " differs")
    inputs = referenced(prepared["original_inputs"], document=True)
    registry = referenced(prepared["registry"], document=True)
    sources = referenced(prepared["source_preparation"], document=True)
    transformations = referenced(prepared["transformations"], document=True)
    observations = referenced(prepared["observations"], document=True)
    cross = referenced(prepared["cross_lock_observations"], document=True)
    harness = referenced(inputs["harness"])
    require(hashlib.sha256(harness).hexdigest() == PROFILE["harness"], "original assertion changed")
    for variant, digest in PROFILE["locks"].items():
        lock = referenced(inputs["variants"][variant]["lock"])
        require(hashlib.sha256(lock).hexdigest() == digest
                and len(tomllib.loads(lock.decode())["package"]) == 664, "original complete lock changed")
        require(oracle["environment"]["variant_commands"][variant] == plan["command"]
                and oracle["environment"]["variant_toolchains"][variant] == "1.85.1"
                and len(oracle["environment_replacements"][variant]) == 1
                and oracle["environment_replacements"][variant][0]["path"] == "Cargo.lock"
                and oracle["environment_replacements"][variant][0]["sha256"] == digest,
                "original lock/command evidence changed")
        require(len(oracle["runs"][variant]) == 3, "original repetitions missing")
        for row in oracle["runs"][variant]:
            log = (ctx.legacy / "rust" / row["log"]).read_text()
            require(row["ok"] and row["passed_tests"] == [TEST] and not row["failed_tests"]
                    and "test " + TEST + " ... ok" in log, "original successful evidence changed")
        require(sources[variant]["original_lock_sha256"] == PROFILE["locks"]["start"],
                "reference source seed changed")
        with tempfile.TemporaryDirectory(prefix="wallet-original-source-", dir=ctx.scratch) as temporary:
            root = Path(temporary) / "source"
            with start_archive(ctx.repo(task), task["start_sha"]) as stream:
                extract_tar(stream, root)
            if variant == "golden":
                p = subprocess.run(["git", "apply", "--binary", "--whitespace=nowarn", patch["path"]],
                                   cwd=root, env={**os.environ, "GIT_CEILING_DIRECTORIES": temporary},
                                   capture_output=True, text=True)
                require(p.returncode == 0, "original full golden does not apply")
            require(entry.core.snapshot(root) == sources[variant]["snapshot"], "full original source changed")
            changes, operations = entry.compatibility(root)
            require(len(operations) == len(transformations[variant]) == 2, "compatibility operations changed")
            for number, operation in enumerate(plan["test_compatibility_replacements"]):
                before = (root / operation["path"]).read_bytes()
                require(before.count(operation["old"].encode()) == 1, "original compatibility anchor changed")
                after = before.replace(operation["old"].encode(), operation["new"].encode(), 1)
                require(hashlib.sha256(after).hexdigest() ==
                        oracle["test_compatibility_replacements"][variant][number]["sha256"],
                        "original intermediate compatibility hash changed")
                require(referenced(transformations[variant][number]["original_after"]) == after,
                        "prepared original compatibility differs")
                if operation["path"] == entry.MANIFEST:
                    require(tomllib.loads(after.decode()) == tomllib.loads(changes[entry.MANIFEST].decode()),
                            "manifest operation changed other semantics")
                else:
                    require(changes[entry.LIB] == after, "import-only operation changed source bodies")
        for role in ("public", "private"):
            row = observations[variant + "-" + role]
            require(row["exit_status"] == 0 and row["source_unchanged"] and row["lock_sha256"] == digest,
                    "original complete graph observation failed")
            graph = referenced(row["stdout"], document=True)
            referenced(row["stderr"])
            require(len(graph["packages"]) == len(graph["resolve"]["nodes"]) == 664
                    and len(graph["workspace_members"]) == 17
                    and {(p["name"], p["version"], p.get("source")) for p in graph["packages"]} ==
                    {(p["name"], p["version"], p.get("source")) for p in tomllib.loads(lock.decode())["package"]},
                    "original complete graph was narrowed")
        row = cross[variant]
        require(row["exit_status"] == 101 and row["full_source_unchanged"], "opposite lock was not incompatible")
        diagnostic = {"returncode": row["exit_status"], "command": row["command"],
                      "cwd": observations[variant + "-public"]["cwd"], "stderr": referenced(row["stderr"]).decode()}
        require(entry.core.lock_incompatible(diagnostic, original_locked=True), "unrelated cross-lock failure")
    require(registry["packages"] == 647 and registry["new_dependency_resolutions"] == 0
            and registry["new_original_SUT_model_judge_calls"] == 0
            and registry["asset"]["sha256"] == PROFILE["vendor_asset"],
            "original vendor package closure changed")
    descriptor = referenced(registry["asset"], document=True)
    require(descriptor["tree_sha256"] == PROFILE["vendor_tree"] == tree_hash(Path(registry["source"])),
            "original vendor bytes changed")
    for key in ("cargo", "rustc"):
        row = inputs["compiler"][key]
        require(row["sha256"] == PROFILE[key] == file_hash(Path(row["path"])), "original compiler changed")
    require(ctx.modules(task) == [], "original workspace gained a Git submodule")
    return prepared, inputs, registry, harness


def migrate(ctx, task, oracle):
    from . import rust_lane, rust_overlay, rust_vendor
    plan = rust_lane.load_plan(ctx, oracle)
    patch = ctx.golden(task, oracle, "rust", ctx.task_directory(task, oracle, "rust"))
    prepared, inputs, registry, harness = original_inputs(ctx, task, oracle, plan, patch)
    image, image_audit = ctx.image(rust_lane.IMAGE)
    assets = []

    def asset(value, target, role="developer", kind="file"):
        assets.append({**value, "target": target, "visibility": role, "kind": kind})
        return {"path": target, "sha256": value["sha256"]}

    compiler = rust_lane.compiler(ctx, "1.85.1")
    asset(compiler, "/opt/reviewbench-assets/rust", kind="archive")
    asset(registry["asset"], rust_vendor.TARGET, kind="archive")
    sources = asset(ctx.copy_asset(Path(registry["source_config"]["path"]), "wallet-sources"), SOURCES)
    locks = []
    for number, variant in enumerate(("start", "golden")):
        locks.append(asset(ctx.copy_asset(Path(inputs["variants"][variant]["lock"]["path"]), "wallet-locks"),
                           "/opt/reviewbench-assets/wallet-lock-" + str(number)))
    public = {
        "schema_version": entry.SCHEMA, "task_id": ID, "public_command": entry.PUBLIC,
        "selection": "unique-full-current-locked-metadata-no-test-outcomes",
        "compatibility_policy": "recorded-dev-feature-and-import-only-current-mirror",
        "reference_seed_sha256": PROFILE["locks"]["start"], "locks": locks, "source_config": sources,
        "cargo": {"path": "/opt/reviewbench-assets/rust/bin/cargo", "sha256": PROFILE["cargo"]},
        "rustc": {"path": "/opt/reviewbench-assets/rust/bin/rustc", "sha256": PROFILE["rustc"]},
        "cpus": 4, "timeout_seconds": 3600, "metadata_timeout_seconds": 180, "max_log_bytes": 32 * 1024**2,
    }
    private = {"task_id": ID, "command": entry.COMMAND,
               "harness": {"path": entry.HARNESS, "sha256": PROFILE["harness"]}, "expected_tests": [TEST]}
    entry.validate(public, private)
    for name, data, target, visibility in (("public", public, SPEC, "developer"), ("private", private, PRIVATE, "grader")):
        path = ctx.output / ("wallet-" + name + ".json")
        atomic_json(path, data)
        asset(ctx.copy_asset(path, "wallet-specs"), target, visibility)
    tools = rust_overlay.build_tools(ctx, extra_entries={
        "wallet.py": Path(entry.__file__), "image_locks.py": Path(__file__).with_name("rust_image_entry.py")})
    asset(tools, rust_overlay.TOOLS, kind="archive")
    original_test = asset(ctx.copy_asset(Path(inputs["harness"]["path"]), "wallet-original-tests"),
                          "/opt/reviewbench-assets/wallet-original-test.rs", "grader")
    destination = "/workspace/" + entry.HARNESS
    prefix = ["python3", TOOL, "--spec", SPEC, "--task-id", ID]
    recipe = RuntimeRecipe(
        task_id=ID, repo=task["repo"], start_sha=task["start_sha"], lane="rust", image=image, image_audit=image_audit,
        workdir="/workspace", cpus=4, memory_mb=8192, build_scratch_mb=4096, command_timeout_seconds=3600,
        env={"PATH": "/opt/reviewbench-assets/rust/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
             "RUSTC": "/opt/reviewbench-assets/rust/bin/rustc", "CARGO_HOME": "/opt/reviewbench-cache/cargo",
             "CARGO_TARGET_DIR": "/opt/reviewbench-cache/target", "CARGO_NET_OFFLINE": "true",
             "CARGO_BUILD_JOBS": "4", "CARGO_INCREMENTAL": "0", "CARGO_TERM_COLOR": "never",
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
                 "commands": [{"command": shlex.join([*prefix, "--private", PRIVATE]),
                               "parser": "rust-test", "expected_tests": [TEST]}]},
    )
    recipe.validate()
    return recipe
