"""Original Alacritty X11 command with public-only Glutin form selection.

This opt-in adapter supplies build inputs, not a reference implementation. It
keeps both complete original graphs, compiles the current candidate against
both compatible forms, then conjunctively executes the original service test.
"""
import hashlib
import json
from pathlib import Path
import shlex
import tomllib

from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, atomic_json, canonical_hash, file_hash,
)
from reviewbench_maintainability.agentic.snapshots import tree_files
from . import rust_manifest_service_entry as entry, rust_vendor
from .rust_build_entry import manifest_operations, relative_path
from .rust_lock_assets import reference_trees
from .rust_locked_workspace import referenced
from .rust_overlay import session_bytes

OPTION = "rust_original_manifest_services"
ID = "github__alacritty__alacritty__pull_6390__comment_994007978__api_design"
PROFILE = {
    "repo": "alacritty/alacritty", "index": 17,
    "start": "a0ffe556b529897cc29c772761da097bed508b89",
    "plan": "c9bd4b47bf40edda21fe7640919c0d9936b58e683e8087b7bcb0a02419a0371e",
    "golden": "38164e0b1388600d6701ab2a3dc10d7949731ee8825a67e042ca4d139c5f1a66",
    "harness": "e6bfe60aecc378ba157520403629ea79ca89a69abccc1de8cb22ad2ca8ebf5d5",
    "manifests": ["b11f305decd189dd12be4f2e85503c4ae7adca0a7d3664bed18f1a640e0f2510",
                  "8fc483e27b2f22bc7f9831537f81c030ad3c9c1da60bcd34cfeac6b8c499aac4"],
    "locks": ["bae703aaf895514d4b79cdd718f4f16a6c4118cd16f01020a8fe3acb485bae27",
              "f07851c4156ea63abe58c8ced841179c0bc1b169f43b71ad3119b97bbeef3e94"],
    "cargo": "f795aeb807da391bf3a17b20765660b401b6346575f5d63647c5639e319d63d3",
    "rustc": "0ffe4e2f3eccbc18631d2a59357dbf5fabd86c2d0cc8422156601dda0fda6168",
    "dockerfile": "b8ef1fb2a44c17320cadacbffe3afc8d63ee51a6ca273d9f7116d4ca5f8dcf45",
}
SPEC = "/opt/reviewbench-assets/manifest-service.json"
PRIVATE = "/opt/reviewbench-assets/manifest-service-private.json"
SOURCES = "/opt/reviewbench-assets/manifest-service-sources.toml"
TOOL = "/opt/reviewbench-assets/rust-build-tools/manifest_service.py"
BUILD_PACKAGES = ["libfontconfig1-dev", "libfreetype6-dev", "libxkbcommon-dev"]


def supported_build_packages(packages):
    # xcb's original native link needs the distribution's development package,
    # not a manually substituted linker stub. Older records remain readable;
    # each resulting runtime still needs all six original calibration replays.
    return packages == BUILD_PACKAGES or packages == [*BUILD_PACKAGES, "libxcb1-dev"]


def require(value, message):
    if not value:
        raise ContractError("original manifest service: " + message)


def requested(ctx, task):
    options = getattr(ctx, "config", {}).get("migration", {}).get(OPTION, {})
    require(isinstance(options, dict) and set(options) <= {ID}, "unsupported opt-in profile")
    return task.get("id") in options


def attest_vendor(prepared, locks):
    expected = rust_vendor.locked_packages(locks)
    audit = referenced(prepared["vendor_audit"], document=True)["packages"]
    require(len(audit) == len(expected)
            and {(r["name"], r["version"], r["source"]): r["package_checksum"] for r in audit} == expected,
            "vendor is not the union of both full original graphs")
    root = Path(prepared["vendor_root"])
    require(root.is_absolute() and root.is_dir() and not root.is_symlink(), "missing original vendor")
    names = set()
    for record in audit:
        directory = root / relative_path(record["directory"])
        require(not directory.is_symlink() and directory.resolve().is_relative_to(root.resolve()),
                "vendor directory escaped")
        checksum = directory / ".cargo-checksum.json"
        require(checksum.is_file() and not checksum.is_symlink(), "missing vendor inventory")
        check = json.loads(checksum.read_text())
        require(check["package"] == record["package_checksum"] and len(check["files"]) == record["files"],
                "vendor checksum inventory changed")
        for name, sha in check["files"].items():
            path = directory / relative_path(name)
            require(path.is_file() and path.resolve().is_relative_to(directory.resolve())
                    and file_hash(path) == sha, "vendored source changed")
            names.add(path.relative_to(root).as_posix())
        names.add(checksum.relative_to(root).as_posix())
    require(set(tree_files(root)) == names, "vendor files added or removed")
    text, _ = rust_vendor.source_configuration(k[2] for k in expected)
    require(referenced(prepared["source_config"]) == text.encode(), "offline source configuration changed")


def original_inputs(ctx, task, oracle, plan, patch):
    require(task["id"] == oracle["id"] == ID
            and task["repo"] == oracle["repo"] == PROFILE["repo"]
            and task["start_sha"] == oracle["start_sha"] == PROFILE["start"], "task identity changed")
    require(canonical_hash(plan) == PROFILE["plan"] and plan["input_index"] == oracle["input_index"] == 17
            and plan["toolchain"] == "1.65.0", "original plan/compiler changed")
    require(patch["sha256"] == oracle["golden_patch_sha256"] == PROFILE["golden"]
            and file_hash(Path(patch["path"])) == PROFILE["golden"], "original golden changed")
    options = ctx.config["migration"][OPTION][ID]
    require(set(options) == {"inputs", "system_image"}, "inputs and system image need independent hash pins")
    prepared = referenced(options["inputs"], document=True)
    require(prepared["schema_version"] == "reviewbench-original-variant-manifest-inputs/v1"
            and prepared["task_id"] == ID and prepared["complete"] is True
            and prepared["new_SUT_tests_model_judges"] == 0, "incomplete original preparation")
    for field, value in (("original_task", task), ("original_oracle", oracle), ("original_plan", plan)):
        require(referenced(prepared[field], document=True) == value, "prepared " + field + " changed")
    require(hashlib.sha256(referenced(prepared["golden"])).hexdigest() == PROFILE["golden"],
            "prepared golden changed")
    require(len(plan["session_files"]) == 1 and oracle["pass_to_pass_tests"] == [entry.TEST],
            "original test inventory changed")
    harness = session_bytes(ctx, plan, oracle, plan["session_files"][0])
    require(harness == referenced(prepared["harness"])
            and hashlib.sha256(harness).hexdigest() == PROFILE["harness"], "original harness changed")
    references = reference_trees(ctx, task, patch, {"Cargo.lock"})
    sources = referenced(prepared["source_preparation"], document=True)
    locks, manifests = [], []
    for index, variant in enumerate(("start", "golden")):
        lock_bytes = referenced(prepared["locks"][variant])
        manifest_bytes = referenced(prepared["manifests"][variant])
        require(hashlib.sha256(lock_bytes).hexdigest() == PROFILE["locks"][index]
                and hashlib.sha256(manifest_bytes).hexdigest() == PROFILE["manifests"][index],
                "original variant manifest/lock changed")
        lock = tomllib.loads(lock_bytes.decode())
        locks.append(lock)
        manifests.append(tomllib.loads(manifest_bytes.decode()))
        original = oracle["environment_replacements"][variant]
        require([(r["path"], r["sha256"]) for r in original] == [
            ("alacritty/Cargo.toml", PROFILE["manifests"][index]), ("Cargo.lock", PROFILE["locks"][index])],
            "variant build inputs differ from original environment")
        require(oracle["environment"]["variant_commands"][variant] == plan["command"]
                and oracle["environment"]["variant_toolchains"][variant] == "1.65.0",
                "original commands/compiler differ")
        original_manifests, original_locks = references[variant]
        require(original_manifests["alacritty/Cargo.toml"] == PROFILE["manifests"][0]
                and hashlib.sha256(original_locks["Cargo.lock"]).hexdigest() == PROFILE["locks"][0],
                "source-only reference seed changed")
        require(original_manifests == {n: r[1] for n, r in sources[variant]["snapshot"].items()
                                      if Path(n).name == "Cargo.toml"}, "complete original manifest tree changed")
        require(len(prepared["original_logs"][variant]) == len(oracle["runs"][variant]) == 3,
                "six original observations missing")
        for row, historical in zip(prepared["original_logs"][variant], oracle["runs"][variant]):
            output = referenced(row).decode()
            require(Path(row["path"]).resolve() == (ctx.legacy / "rust" / historical["log"]).resolve()
                    and historical["ok"] and not historical["failed_tests"]
                    and historical["passed_tests"] == [entry.TEST]
                    and "test " + entry.TEST + " ... ok" in output, "original observation changed")
        for field in ("metadata", "offline_metadata"):
            record = prepared[field][variant]
            require(record["exit_status"] == 0 and record["stop_reason"] is None,
                    "original metadata did not succeed")
            graph = referenced(record["stdout"], document=True)
            referenced(record["stderr"])
            require(len(graph["packages"]) == len(lock["package"]) == 217
                    and {(p["name"], p["version"], p.get("source")) for p in graph["packages"]} ==
                    {(p["name"], p["version"], p.get("source")) for p in lock["package"]},
                    "original complete graph narrowed")
    operations = manifest_operations(*manifests)
    require(operations == [
        {"kind": "remove", "key": ["dependencies", "glutin", "git"],
         "old": "https://github.com/rust-windowing/glutin"},
        {"kind": "add", "key": ["dependencies", "glutin", "version"], "value": "0.30.0"},
    ], "original forms changed non-source dependency fields")
    for name in ("cargo", "rustc"):
        require(prepared[name]["sha256"] == PROFILE[name] == file_hash(Path(prepared[name]["path"])),
                "prepared pinned compiler changed")
    attest_vendor(prepared, locks)
    return prepared, operations


def system_image(ctx):
    from .rust_lane import IMAGE
    record = referenced(ctx.config["migration"][OPTION][ID]["system_image"], document=True)
    require(record["status"] == "ready_reconstructed_system_image"
            and record["classification"] == "fixed-snapshot reconstruction"
            and record["historical_image_bytes_recovered"] is False
            and record["snapshot"] == "20260805T000000Z" and record["base_image"] == IMAGE
            and record["original_dockerfile_sha256"] == PROFILE["dockerfile"]
            and all(record[k] == 0 for k in ("exit_status", "build_exit_status", "download_exit_status",
                                             "probe_exit_status", "model_calls", "original_SUT_commands")),
            "system image lacks its explicit reconstruction evidence")
    original = ctx.legacy / "rust/cache/alacritty-xvfb-image/Dockerfile"
    require(file_hash(original) == PROFILE["dockerfile"], "original system recipe changed")
    require(record["declared_runtime_packages"] == [
        "xvfb", "xauth", "libgl1-mesa-dri", "libglx-mesa0", "libegl1-mesa", "libxcursor1",
        "libxi6", "libxrandr2", "libxinerama1", "libxkbcommon-x11-0", "libxrender1", "libxfixes3"]
        and supported_build_packages(record["additional_build_packages"])
        and record["probe_stdout"].endswith("XVFB_AND_ORIGINAL_FONT_REQUIREMENTS_READY\n"),
        "original runtime/font prerequisites changed")
    packages = referenced(record["packages"], document=True)
    require(packages and len({p["path"] for p in packages}) == len(packages), "missing system package inputs")
    for package in packages:
        path = Path(package["path"])
        require(path.is_absolute() and path.is_file() and not path.is_symlink()
                and path.stat().st_size == package["bytes"] and file_hash(path) == package["sha256"],
                "fixed-snapshot package changed")
    return record["image"]


def migrate(ctx, task, oracle):
    from . import rust_lane, rust_overlay
    plan = rust_lane.load_plan(ctx, oracle)
    patch = ctx.golden(task, oracle, "rust", ctx.task_directory(task, oracle, "rust"))
    prepared, operations = original_inputs(ctx, task, oracle, plan, patch)
    registry = json.loads(Path(ctx.config["rust_toolchains"]).read_text())
    tool = Path(registry["1.65.0"]["root"])
    require(all(file_hash(tool / ("bin/" + n)) == PROFILE[n] for n in ("cargo", "rustc")),
            "compiler registry changed")
    image, audit = ctx.image(system_image(ctx))
    assets = []

    def asset(value, target, visibility="developer", kind="file"):
        assets.append({**value, "target": target, "visibility": visibility, "kind": kind})
        return {"path": target, "sha256": value["sha256"]}

    asset(rust_lane.compiler(ctx, "1.65.0"), "/opt/reviewbench-assets/rust", kind="archive")
    asset(ctx.bundle(Path(prepared["vendor_root"])), rust_vendor.TARGET, kind="archive")
    source = asset(ctx.copy_asset(Path(prepared["source_config"]["path"]), "manifest-sources"), SOURCES)
    forms = []
    for index, (variant, name) in enumerate((("start", "git-beta"), ("golden", "registry-stable"))):
        form = {"name": name, "operations": [] if not index else operations}
        for field in ("manifest", "lock"):
            value = prepared[field + "s"][variant]
            form[field] = asset(ctx.copy_asset(Path(value["path"]), "manifest-original-inputs"),
                                "/opt/reviewbench-assets/manifest-form-" + str(index) + "-" + field)
        forms.append(form)
    public = {
        "schema_version": entry.SCHEMA, "task_id": ID,
        "selection": "full-candidate-metadata-and-public-compile; all-compatible-private-results",
        "manifest_path": "alacritty/Cargo.toml", "dependency_key": ["dependencies", "glutin"],
        "toolchain": "1.65.0", "package": "alacritty", "forms": forms,
        "cargo": {"path": "/opt/reviewbench-assets/rust/bin/cargo", "sha256": PROFILE["cargo"]},
        "rustc": {"path": "/opt/reviewbench-assets/rust/bin/rustc", "sha256": PROFILE["rustc"]},
        "source_config": source,
        "public_command": ["check", "--locked", "-p", "alacritty", "--no-default-features",
                           "--features", "x11", "--message-format=json"],
        "cpus": 4, "timeout_seconds": plan["timeout_seconds"],
        "metadata_timeout_seconds": 120, "max_log_bytes": 32 * 1024**2,
    }
    private = {
        "schema_version": entry.PRIVATE_SCHEMA, "task_id": ID, "package_manifest": public["manifest_path"],
        "harness": {"path": plan["session_files"][0]["path"], "sha256": PROFILE["harness"]},
        "command": entry.TEST_COMMAND, "expected_tests": [entry.TEST], "original_shell": plan["command"],
        "original_shell_sha256": hashlib.sha256(plan["command"].encode()).hexdigest(),
    }
    entry.validate(public, private)
    for name, spec, target, role in (("public", public, SPEC, "developer"), ("private", private, PRIVATE, "grader")):
        path = ctx.output / ("manifest-service-" + name + ".json")
        atomic_json(path, spec)
        asset(ctx.copy_asset(path, "manifest-service-specs"), target, role)
    tools = rust_overlay.build_tools(ctx, extra_entries={
        "manifest_service.py": Path(entry.__file__),
        "image_locks.py": Path(__file__).with_name("rust_image_entry.py"),
    })
    asset(tools, rust_overlay.TOOLS, kind="archive")
    test_target = "/opt/reviewbench-assets/manifest-service-original-test.rs"
    asset(ctx.copy_asset(Path(prepared["harness"]["path"]), "manifest-service-test"), test_target, "grader")
    prefix = ["python3", TOOL, "--spec", SPEC, "--task-id", ID]
    destination = "/workspace/" + private["harness"]["path"]
    recipe = RuntimeRecipe(
        task_id=ID, repo=task["repo"], start_sha=task["start_sha"], lane="rust",
        image=image, image_audit=audit, workdir="/workspace", cpus=4, memory_mb=8192,
        build_scratch_mb=24576, command_timeout_seconds=plan["timeout_seconds"],
        env={
            "PATH": "/opt/reviewbench-assets/rust/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "RUSTC": "/opt/reviewbench-assets/rust/bin/rustc", "CARGO_HOME": "/opt/reviewbench-cache/cargo",
            "CARGO_TARGET_DIR": "/opt/reviewbench-cache/target", "CARGO_BUILD_JOBS": "4",
            "CARGO_TERM_COLOR": "never", "CARGO_INCREMENTAL": "0", "CARGO_NET_OFFLINE": "true",
            "CARGO_PROFILE_DEV_DEBUG": "0", "CARGO_PROFILE_TEST_DEBUG": "0",
            "RUSTFLAGS": "--cap-lints=allow", "RUST_BACKTRACE": "1",
            "SKIP_WASM_BUILD": "1", "RUST_BUILD_DOCKER": "1", "GIT_COMMIT": "reviewbench-hard-oracle",
            "GIT_DIRTY": "false", "SOURCE_TIMESTAMP": "0", "TMPDIR": "/workspace/.reviewbench/tmp",
            "PROTOC_INCLUDE": "/usr/include",
        },
        cache_mounts={"cargo": "/opt/reviewbench-cache/cargo", "target": "/opt/reviewbench-cache/target",
                      "rust-build": rust_overlay.BUILD_CACHE},
        bootstrap=["mkdir -p /workspace/.reviewbench/tmp"], dev_checks=[shlex.join(prefix)],
        scratch_paths=[], assets=assets, submodules=ctx.modules(task),
        grading={"multifile_build": True, "immutable_paths": ctx.immutable_tests(task), "golden_patch": patch,
                 "bootstrap": ["mkdir -p " + shlex.quote(str(Path(destination).parent))
                               + " && cp " + shlex.quote(test_target) + " " + shlex.quote(destination)],
                 "commands": [{"command": shlex.join([*prefix, "--private", PRIVATE]),
                               "parser": "rust-test", "expected_tests": [entry.TEST]}]},
    )
    recipe.validate()
    return recipe
