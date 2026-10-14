"""Original complete gRPC workspace, recursive native sources and26 loopback RPC tests."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import tomllib

from reviewbench_maintainability.agentic.assets import ASSET_SCHEMA
from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, atomic_json, canonical_hash, file_hash,
)
from reviewbench_maintainability.agentic.snapshots import start_archive, extract_tar, tree_hash, tree_files
from . import rust_grpc_entry as entry
from .rust_local_registry import checked_line, index_path

ID = "github__tikv__grpc-rs__pull_615__comment_1205702187__modularity"
OPTION = "rust_original_grpc"
PROFILE = {
    "repo": "tikv/grpc-rs", "start": "8609072ad5b5c99a6f8f6737b69d42629c8d1dff",
    "plan": "2e9141c2357f119d2465a94dffe02498b429866f19abcdebf4261d5de813485b",
    "golden": "2122fc7136f35a0eb2e33623626a81e802ffe6659e13aa40db5fb53171922831",
    "lock": "56cae4e3886938d447ce0d95d41a2e808b51cb05a0e50699f8d79f70d5814ed5",
    "cargo": "bfc78d62e9e3e80adf4dca159f012ca21cb1e0fdf8bdf63ff5d25d5c75ada76d",
    "rustc": "c1bd86d51ed89c344fa4b394dcb116d48f5e3eeb121498c083b9b516624f55cd",
    "metadata_cargo": "9548937d530bf439ff1ba47a3b2bd26eeb9c3aff1961c20c01798613de922578",
    "native_commit": "996605a5e62f3f00043ac8d3ebca84523bc2dd76",
    "native_files": 32736,
    "native_tree": "189a63279cbda19e236cef700d38cc0daa09d72faed593df447c7b2368344c79",
    "registry_tree": "d5d4e2556747c1964ab2bb35cadef309c063bca9f9fc6548d6aebbdeda295b96",
    "compiler_asset": "6c0dd63cfacc1693a60b3b56614b35321cd4820cf5f5d5c23a545086f91fa629",
}
PREPARE = "git config --global --add safe.directory '*' && git submodule update --init --recursive grpc-sys/grpc"
COMMAND = ["cargo", "+1.77.2", "test", "-p", "interop", "--test", "tests", "--", "--test-threads=1"]
SPEC = "/opt/reviewbench-assets/grpc-public.json"
PRIVATE = "/opt/reviewbench-assets/grpc-private.json"
SOURCES = "/opt/reviewbench-assets/grpc-sources.toml"
REGISTRY = "/opt/reviewbench-assets/grpc-registry"
TOOL = "/opt/reviewbench-assets/rust-build-tools/grpc.py"


def require(value, message):
    if not value:
        raise ContractError("original gRPC: " + message)


def referenced(row, *, document=False):
    path = Path(row["path"])
    require(path.is_absolute() and path.is_file() and not path.is_symlink()
            and path.stat().st_size <= 32 * 1024**2 and file_hash(path) == row["sha256"],
            "fixed preparation evidence changed")
    data = path.read_bytes()
    require(hashlib.sha256(data).hexdigest() == row["sha256"], "evidence changed during read")
    return json.loads(data) if document else data


def requested(ctx, task):
    options = getattr(ctx, "config", {}).get("migration", {}).get(OPTION, {})
    require(isinstance(options, dict) and set(options) <= {ID}, "unknown original profile")
    return task.get("id") in options


def original_inputs(ctx, task, oracle, plan, patch):
    require(task["id"] == oracle["id"] == ID and task["repo"] == oracle["repo"] == PROFILE["repo"]
            and task["start_sha"] == oracle["start_sha"] == PROFILE["start"], "task changed")
    require(canonical_hash(plan) == PROFILE["plan"] and plan["input_index"] == oracle["input_index"] == 105
            and plan["prepare_command"] == PREPARE and plan["toolchain"] == "1.77.2"
            and plan["build_jobs"] == 16 and plan["timeout_seconds"] == 3600
            and shlex.split(plan["command"]) == COMMAND, "original execution plan changed")
    require(patch["sha256"] == oracle["golden_patch_sha256"] == PROFILE["golden"]
            and file_hash(Path(patch["path"])) == PROFILE["golden"], "full original golden changed")
    require(not any(plan.get(k) for k in (
        "session_files", "environment_replacements", "environment_file_replacements")),
        "existing tests acquired a session overlay")
    bundle = referenced(ctx.config["migration"][OPTION][ID], document=True)
    prepared = referenced(bundle["graphs"], document=True)
    retained = referenced(prepared["retained_inputs"], document=True)
    native = referenced(prepared["native_inputs"], document=True)
    registry = referenced(prepared["registry_inputs"], document=True)
    require(prepared["status"] == "full_original_references_offline_verified"
            and prepared["task_id"] == retained["task_id"] == native["task_id"] == registry["task_id"] == ID
            and prepared["original_plan_sha256"] == PROFILE["plan"]
            and prepared["new_dependency_resolutions"] == prepared["new_SUT_model_judge_calls"] == 0,
            "incomplete original preparation")
    require(retained["lock_hash_is_recovery_pin_not_original_oracle_field"] is True
            and retained["original_command"] == plan["command"]
            and retained["original_prepare_command"] == PREPARE,
            "recovered lock provenance changed")
    lock = referenced(retained["lock"])
    require(hashlib.sha256(lock).hexdigest() == PROFILE["lock"], "recovered lock changed")
    require(referenced(prepared["golden"]) == Path(patch["path"]).read_bytes(), "prepared golden changed")
    sources = referenced(prepared["sources"], document=True)
    tests = referenced(prepared["test_files"], document=True)
    require(len(oracle["pass_to_pass_tests"]) == 26
            and prepared["expected_tests"] == oracle["pass_to_pass_tests"], "original26 test identities changed")
    for variant in ("start", "golden"):
        require(shlex.split(oracle["environment"]["variant_commands"][variant]) == COMMAND
                and oracle["environment"]["variant_toolchains"][variant] == "1.77.2",
                "original command/compiler changed")
        require(len(oracle["runs"][variant]) == 3, "original three repetitions missing")
        for run in oracle["runs"][variant]:
            text = (ctx.legacy / "rust" / run["log"]).read_text()
            require(run["ok"] and run["passed_tests"] == oracle["pass_to_pass_tests"]
                    and not run["failed_tests"] and all("test " + n + " ... ok" in text
                    for n in oracle["pass_to_pass_tests"]), "original test evidence changed")
        row = sources[variant]
        require(row["native_files"] == PROFILE["native_files"], "native scope changed")
        expected = row["project_snapshot"]
        require("Cargo.lock" not in expected, "missing-only lock gained a committed seed")
        with tempfile.TemporaryDirectory(prefix="grpc-original-project-", dir=ctx.scratch) as temporary:
            source = Path(temporary) / "source"
            with start_archive(ctx.repo(task), task["start_sha"]) as stream:
                extract_tar(stream, source)
            if variant == "golden":
                p = subprocess.run(["git", "apply", "--binary", "--whitespace=nowarn", patch["path"]],
                                   cwd=source, env={**os.environ, "GIT_CEILING_DIRECTORIES": temporary},
                                   capture_output=True, text=True)
                require(p.returncode == 0, "original golden failed to apply")
            require(entry.core.snapshot(source) == expected, "full original project was changed or narrowed")
        require({n: r for n, r in expected.items() if n.startswith("interop/tests/")} == tests,
                "original existing test closure changed")
        observation = prepared["observations"][variant]
        require(observation["exit_status"] == 0 and observation["source_unchanged"]
                and observation["command"] == [registry["metadata_cargo"]["path"], "--offline", "metadata",
                                               "--locked", "--format-version", "1", "--all-features"],
                "full original metadata preparation changed")
        graph = referenced(observation["stdout"], document=True)
        referenced(observation["stderr"])
        require(len(graph["packages"]) == len(graph["resolve"]["nodes"]) == 183
                and len(graph["workspace_members"]) == 8
                and {(p["name"], p["version"], p.get("source")) for p in graph["packages"]} ==
                {(p["name"], p["version"], p.get("source")) for p in tomllib.loads(lock.decode())["package"]},
                "original complete183-package graph changed")
    require(native["path"] == "grpc-sys/grpc" and native["commit"] == PROFILE["native_commit"]
            and native["files"] == PROFILE["native_files"] and len(native["modules"]) == 27
            and native["original_git_objects_verified"] and not native["copied_historical_build_outputs"],
            "full recursive native inputs changed")
    inventory = referenced(native["inventory"], document=True)
    require(len(inventory) == PROFILE["native_files"]
            and all(row["kind"] in {"file", "symlink"} for row in inventory.values()),
            "original native file inventory changed")
    native_files = {
        name: (["symlink", row["link"]] if row["kind"] == "symlink"
               else ["file", row["sha256"], bool(row["mode"] & 0o111)])
        for name, row in inventory.items()
    }
    require(entry.core.snapshot(Path(native["root"])) == native_files
            and entry.digest_object(native_files) == sources["start"]["native_snapshot_sha256"]
            == sources["golden"]["native_snapshot_sha256"], "native source bytes changed")
    native_asset = referenced(native["asset"], document=True)
    require(native_asset["schema_version"] == ASSET_SCHEMA and native_asset["source_kind"] == "submodule"
            and native_asset["git_commit"] == PROFILE["native_commit"]
            and native_asset["tree_sha256"] == PROFILE["native_tree"] == tree_hash(Path(native["root"])),
            "native archive binding changed")
    require(registry["status"] == "full_locked_local_registry_and_metadata_reader_verified"
            and registry["registry_packages"] == 175 and registry["full_metadata_packages"] == 183
            and registry["new_dependency_resolutions"] == registry["original_SUT_model_judge_calls"] == 0
            and not registry["manifest_or_index_fields_rewritten"], "full native registry changed")
    root = Path(registry["registry"])
    audit = referenced(registry["audit"], document=True)
    packages = [p for p in tomllib.loads(lock.decode())["package"] if p.get("source")]
    require({(r["name"], r["version"], r["checksum"]) for r in audit} ==
            {(p["name"], p["version"], p["checksum"]) for p in packages}
            and len(audit) == len(packages) == 175, "registry package inventory changed")
    names = set()
    for package in packages:
        path = root / (package["name"] + "-" + package["version"] + ".crate")
        index = root / "index" / index_path(package["name"])
        require(path.is_file() and not path.is_symlink() and file_hash(path) == package["checksum"],
                "original crate archive changed")
        lines = [line + b"\n" for line in index.read_bytes().splitlines()
                 if json.loads(line).get("vers") == package["version"]]
        require(len(lines) == 1, "locked index entry missing or repeated")
        checked_line(lines[0], package["name"], package["version"], package["checksum"])
        expected_line = next(r for r in audit if r["name"] == package["name"] and r["version"] == package["version"])
        require(hashlib.sha256(lines[0]).hexdigest() == expected_line["index_line_sha256"],
                "original index fields changed")
        names.update([path.relative_to(root).as_posix(), index.relative_to(root).as_posix()])
    require(set(tree_files(root)) == names, "registry closure added or removed files")
    registry_asset = referenced(registry["asset"], document=True)
    require(registry_asset["source_kind"] == "dependency"
            and registry_asset["tree_sha256"] == PROFILE["registry_tree"] == tree_hash(root),
            "registry archive changed")
    sources_config = tomllib.loads(referenced(registry["source_config"]).decode())
    require(sources_config == {
        "source": {"crates-io": {"replace-with": "reviewbench-locked"},
                   "reviewbench-locked": {"local-registry": REGISTRY}},
        "net": {"offline": True}}, "original source transport changed")
    for key, path in (("cargo", Path(retained["compiler_root"]) / "bin/cargo"),
                      ("rustc", Path(retained["compiler_root"]) / "bin/rustc"),
                      ("metadata_cargo", Path(registry["metadata_cargo"]["path"]))):
        require(path.is_file() and not path.is_symlink() and file_hash(path) == PROFILE[key],
                "fixed compiler/metadata binary changed")
    require(bundle["compiler_asset"]["sha256"] == PROFILE["compiler_asset"], "full original compiler asset changed")
    referenced(bundle["compiler_asset"], document=True)
    return bundle, retained, native, registry, sources, tests


def migrate(ctx, task, oracle):
    from . import rust_lane, rust_overlay
    plan = rust_lane.load_plan(ctx, oracle)
    patch = ctx.golden(task, oracle, "rust", ctx.task_directory(task, oracle, "rust"))
    bundle, retained, native, registry, sources, tests = original_inputs(ctx, task, oracle, plan, patch)
    image, image_audit = ctx.image(rust_lane.IMAGE)
    assets = []

    def asset(value, target, role="developer", kind="file"):
        assets.append({**value, "target": target, "visibility": role, "kind": kind})
        return {"path": target, "sha256": value["sha256"]}

    asset(bundle["compiler_asset"], "/opt/reviewbench-assets/rust", kind="archive")
    reader = Path(registry["metadata_cargo"]["path"])
    asset(ctx.bundle(reader.parent.parent, ["bin/cargo"], role="toolchain"),
          "/opt/reviewbench-assets/grpc-metadata", kind="archive")
    asset(registry["asset"], REGISTRY, kind="archive")
    source_config = asset(ctx.copy_asset(Path(registry["source_config"]["path"]), "grpc-sources"), SOURCES)
    lock = asset(ctx.copy_asset(Path(retained["lock"]["path"]), "grpc-locks"), "/opt/reviewbench-assets/grpc-lock")
    public = {
        "schema_version": entry.SCHEMA, "task_id": ID, "package": "interop",
        "package_manifest": "interop/Cargo.toml", "metadata_reader_role": "complete-locked-graph-only",
        "public_command": ["check", "--locked", "-p", "interop", "--message-format=json"],
        "cargo": {"path": "/opt/reviewbench-assets/rust/bin/cargo", "sha256": PROFILE["cargo"]},
        "rustc": {"path": "/opt/reviewbench-assets/rust/bin/rustc", "sha256": PROFILE["rustc"]},
        "metadata_cargo": {"path": "/opt/reviewbench-assets/grpc-metadata/bin/cargo", "sha256": PROFILE["metadata_cargo"]},
        "source_config": source_config, "lock": lock,
        "native": {"path": native["path"], "commit": native["commit"], "files": native["files"],
                   "snapshot_sha256": sources["start"]["native_snapshot_sha256"]},
        "cpus": 16, "timeout_seconds": 3600, "metadata_timeout_seconds": 180, "max_log_bytes": 32 * 1024**2,
    }
    private = {"task_id": ID, "command": COMMAND[2:], "harness": "interop/tests/tests.rs",
               "files": {n: row[1] for n, row in tests.items()},
               "expected_tests": oracle["pass_to_pass_tests"]}
    entry.validate(public, private)
    for name, value, target, role in (("public", public, SPEC, "developer"), ("private", private, PRIVATE, "grader")):
        path = ctx.output / ("grpc-" + name + ".json")
        atomic_json(path, value)
        asset(ctx.copy_asset(path, "grpc-specs"), target, role)
    tools = rust_overlay.build_tools(ctx, extra_entries={
        "grpc.py": Path(entry.__file__), "image_locks.py": Path(__file__).with_name("rust_image_entry.py")})
    asset(tools, rust_overlay.TOOLS, kind="archive")
    modules = ctx.modules(task)
    require(len(modules) == 1 and modules[0]["path"] == native["path"]
            and modules[0]["commit"] == native["commit"], "root native Git link changed")
    prefix = ["python3", TOOL, "--spec", SPEC, "--task-id", ID]
    recipe = RuntimeRecipe(
        task_id=ID, repo=task["repo"], start_sha=task["start_sha"], lane="rust",
        image=image, image_audit=image_audit, cpus=16, memory_mb=16384, build_scratch_mb=6144,
        command_timeout_seconds=3600, workdir="/workspace",
        env={"PATH": "/opt/reviewbench-assets/rust/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
             "RUSTC": "/opt/reviewbench-assets/rust/bin/rustc", "CARGO_HOME": "/opt/reviewbench-cache/cargo",
             "CARGO_TARGET_DIR": "/opt/reviewbench-cache/target", "CARGO_NET_OFFLINE": "true",
             "CARGO_BUILD_JOBS": "16", "CARGO_INCREMENTAL": "0", "CARGO_TERM_COLOR": "never",
             "CARGO_PROFILE_DEV_DEBUG": "0", "CARGO_PROFILE_TEST_DEBUG": "0", "RUSTFLAGS": "--cap-lints=allow",
             "RUST_BACKTRACE": "1", "PROTOC_INCLUDE": "/usr/include", "SKIP_WASM_BUILD": "1",
             "RUST_BUILD_DOCKER": "1", "GIT_COMMIT": "reviewbench-hard-oracle", "GIT_DIRTY": "false",
             "SOURCE_TIMESTAMP": "0",
             "HOME": "/tmp", "TMPDIR": "/workspace/.reviewbench/tmp"},
        assets=assets, cache_mounts={"cargo": "/opt/reviewbench-cache/cargo", "target": "/opt/reviewbench-cache/target",
                                    "rust-build": rust_overlay.BUILD_CACHE},
        bootstrap=["mkdir -p /workspace/.reviewbench/tmp"], dev_checks=[shlex.join(prefix)], scratch_paths=[],
        submodules=[{"path": native["path"], "commit": native["commit"], "asset": native["asset"]}],
        grading={"multifile_build": True, "immutable_paths": ctx.immutable_tests(task), "golden_patch": patch,
                 "bootstrap": [], "commands": [{"command": shlex.join([*prefix, "--private", PRIVATE]),
                                                "parser": "rust-test", "expected_tests": private["expected_tests"]}]},
    )
    recipe.validate()
    return recipe
