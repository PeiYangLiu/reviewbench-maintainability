"""Image49's two original declared locks, selected only by current Cargo metadata."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import shlex
import tomllib

from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, atomic_json, canonical_hash, file_hash,
)
from reviewbench_maintainability.agentic.snapshots import tree_files
from . import rust_image_entry as entry
from .rust_lock_assets import reference_trees
from .rust_overlay import session_bytes
from . import rust_vendor

TASK = "github__image-rs__image__pull_2672__comment_2602759902__api_design"
REPO = "image-rs/image"
START = "cdc0363a939fb9b8ee3f9e53018d07955aac4473"
GOLDEN = "591f782067bc9855184bd22c29d62926d5487017c311260a0eb808f81b1a15c0"
PLAN = "ca5931b6c3d5210c2cdffcb1feaa82b1b032d3465a183fd1429646022a33def2"
OPTION = "rust_image_retained_locks"
INPUT_SCHEMA = "reviewbench-image49-original-retained-lock-inputs/v1"
HARNESS = "tests/oracle_pnm.rs"
HARNESS_SHA = "3e9796cde2abf3b2847156585ee8d4b04782d034f3039aeba3d4da0527df309c"
EXPECTED = ["public_reader_decodes_a_pnm_pixel"]
COMMAND = ["cargo", "+1.88.0", "test", "--locked", "--test", "oracle_pnm",
           "public_reader_decodes_a_pnm_pixel", "--", "--nocapture", "--test-threads=1"]
LOCKS = {
    "start": "da0a9651c99781c40b4bbc2932f495fcfc7a18d737e0723b4a6b6b94e926f09e",
    "golden": "e8171b8dfcd0a0e75f32a9e2113710518f090a94e1e40c333dca27763ba0af32",
}
COUNTS = {"start": 197, "golden": 207}
CARGO_SHA = "de9be4299324f5c1f5d12103792d35e8b8ab195df3a374194b8ffc33562c7e3d"
RUSTC_SHA = "ac3e92e45fe15c789939deedf7614f4f9578cc9609cb9ead39c9b1808a316c5b"
SPEC = "/opt/reviewbench-assets/image-retained-locks.json"
PRIVATE = "/opt/reviewbench-assets/image-retained-locks-private.json"
SOURCES = "/opt/reviewbench-assets/image-cargo-sources.toml"
TOOL = "/opt/reviewbench-assets/rust-build-tools/image_locks.py"
LOCK_PREFIX = "/opt/reviewbench-assets/image-original-lock-"
RUST = "/opt/reviewbench-assets/rust"


def require(condition, message):
    if not condition:
        raise ContractError("Image original retained locks: " + message)


def referenced(row, *, json_data=False):
    path = Path(row["path"])
    require(path.is_absolute() and path.is_file() and not path.is_symlink(),
            "missing/redirected prepared input")
    data = path.read_bytes()
    require(len(data) <= 32 * 1024**2 and hashlib.sha256(data).hexdigest() == row["sha256"],
            "prepared input bytes changed")
    return json.loads(data) if json_data else data


def requested(ctx, task):
    if task.get("id") != TASK:
        return False
    options = getattr(ctx, "config", {}).get(OPTION, {})
    require(isinstance(options, dict) and not (set(options) - {TASK}), "invalid task opt-in")
    return TASK in options


def _tree_matches(path, records):
    observed = tree_files(path)
    require(set(observed) == set(records), "complete original source file set changed")
    for name, record in records.items():
        file = observed[name]
        if "link_target" in record:
            require(file.is_symlink() and file.readlink().as_posix() == record["link_target"],
                    "original source link changed")
        else:
            require(not file.is_symlink() and file_hash(file) == record["sha256"]
                    and bool(file.stat().st_mode & 0o111) == bool(record["mode"] & 0o111),
                    "original source bytes/mode changed: " + name)


def original_inputs(ctx, task, oracle, plan, patch):
    require((task.get("id"), task.get("repo"), task.get("start_sha")) == (TASK, REPO, START)
            and (oracle.get("id"), oracle.get("repo"), oracle.get("start_sha")) == (TASK, REPO, START),
            "wrong original task/repo/start")
    require(canonical_hash(plan) == PLAN and plan["input_index"] == oracle["input_index"] == 49
            and plan["toolchain"] == "1.88.0" and shlex.split(plan["command"]) == COMMAND,
            "original plan changed")
    require(plan["features"] == ["default"] and plan["build_jobs"] == 4
            and plan["timeout_seconds"] == 1800 and plan["workdir"] == ".",
            "original features/workdir/resources changed")
    require(oracle["pass_to_pass_tests"] == EXPECTED
            and oracle["golden_patch_sha256"] == patch["sha256"] == GOLDEN
            and file_hash(Path(patch["path"])) == GOLDEN, "original patch/tests changed")
    for variant in ("start", "golden"):
        require(shlex.split(oracle["environment"]["variant_commands"][variant]) == COMMAND
                and oracle["environment"]["variant_toolchains"][variant] == "1.88.0",
                "original variant compiler/command changed")
        rows = oracle["environment_replacements"][variant]
        require(len(rows) == 1 and rows[0]["path"] == "Cargo.lock"
                and rows[0]["sha256"] == LOCKS[variant]
                and rows[0]["source"] == f"session/049/Cargo.{variant}.lock",
                "original declared lock changed")
    require(len(plan["session_files"]) == 1 and plan["session_files"][0]["path"] == HARNESS
            and hashlib.sha256(session_bytes(ctx, plan, oracle, plan["session_files"][0])).hexdigest()
            == HARNESS_SHA, "original harness changed")
    path = Path(ctx.config[OPTION][TASK])
    require(path.is_absolute() and path.is_file() and not path.is_symlink(), "prepared opt-in is not a local file")
    prepared = json.loads(path.read_text())
    require(prepared["schema_version"] == INPUT_SCHEMA and prepared["task_id"] == TASK
            and prepared["original_declared_locks_not_new_resolutions"] is True,
            "wrong retained-input manifest")
    require(referenced(prepared["original_task"], json_data=True) == task
            and referenced(prepared["original_oracle"], json_data=True) == oracle
            and referenced(prepared["original_plan"], json_data=True) == plan,
            "prepared inputs differ from current original selection")
    sources = referenced(prepared["source_preparation"], json_data=True)
    hydration = referenced(prepared["archive_hydration"], json_data=True)
    metadata = referenced(prepared["metadata"], json_data=True)
    matrix = referenced(prepared["lock_compatibility"], json_data=True)
    diagnostics = referenced(prepared["original_diagnostics"], json_data=True)
    require(sources["task_id"] == TASK and sources["original_plan"] == plan
            and sources["original_declared_locks_not_new_resolutions"] is True,
            "source preparation changed original locks")
    require(metadata["ready"] and metadata["new_Cargo_resolutions"] == 0
            and matrix["compatibility"] == {"start": {"start": True, "golden": False},
                                          "golden": {"start": False, "golden": True}},
            "original dependency-compatibility observations absent")
    require(diagnostics["both_original_variants_passed"] and diagnostics["test_calls"] == 2
            and diagnostics["new_dependency_resolutions"] == 0, "original full-source diagnostics absent")
    trees = reference_trees(ctx, task, patch, {"Cargo.lock"})
    locks = {}
    for variant in ("start", "golden"):
        manifests, committed_locks = trees[variant]
        source = sources["variants"][variant]
        require(committed_locks == {"Cargo.lock": None} and manifests == source["manifest_sha256"]
                and len(manifests) == 3, "complete original manifests/root lock changed")
        require(source["file_count"] == (916 if variant == "start" else 917), "original source set narrowed")
        _tree_matches(Path(source["source"]), source["files"])
        original_lock = ctx.legacy / "rust" / f"session/049/Cargo.{variant}.lock"
        require(file_hash(original_lock) == source["lock"]["sha256"] == LOCKS[variant],
                "original retained lock bytes changed")
        require(referenced(source["lock"]) == original_lock.read_bytes(), "prepared lock differs from original declaration")
        require(source["harness"]["sha256"] == HARNESS_SHA
                and hashlib.sha256(referenced(source["harness"])).hexdigest() == HARNESS_SHA,
                "prepared harness differs")
        expected_work = dict(source["files"])
        expected_work["Cargo.lock"] = {"sha256": LOCKS[variant], "mode": 0o644}
        expected_work[HARNESS] = {"sha256": HARNESS_SHA, "mode": 0o644}
        _tree_matches(Path(source["work"]), expected_work)
        m = metadata["rows"][variant]
        require(m["exit_status"] == 0 and m["source_and_original_lock_unchanged"]
                and m["locked_graph_exact"] and m["packages"] == m["nodes"] == COUNTS[variant],
                "incomplete original locked metadata")
        graph = json.loads(referenced(m["stdout"]))
        require(len(graph["packages"]) == len(graph["resolve"]["nodes"]) == COUNTS[variant]
                and len(graph["workspace_members"]) == 1
                and Path(graph["workspace_root"]) == Path(source["work"]), "original metadata not full-source")
        actual = {(p["name"], p["version"], p.get("source")) for p in graph["packages"]}
        parsed_lock = tomllib.loads(original_lock.read_text())
        require(actual == {(p["name"], p["version"], p.get("source")) for p in parsed_lock["package"]},
                "original metadata does not cover retained lock")
        d = diagnostics["rows"][variant]
        require(d["original_command"] == COMMAND and d["exit_status"] == 0
                and d["source_unchanged"] and d["stop_reason"] is None
                and d["environment"]["CARGO_BUILD_JOBS"] == "4", "original test diagnostic altered")
        stdout = referenced(d["stdout"]).decode()
        referenced(d["stderr"])
        require("test public_reader_decodes_a_pnm_pixel ... ok" in stdout
                and "1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out;" in stdout,
                "original behavior test did not actually pass")
        locks[variant] = {"path": str(original_lock), "sha256": LOCKS[variant]}
    require(not hydration["missing"] and hydration["registry_packages"] == 212
            and hydration["new_Cargo_resolutions"] == 0, "original archive union incomplete")
    expected = rust_vendor.locked_packages([tomllib.loads(Path(r["path"]).read_text()) for r in locks.values()])
    archive_rows = {}
    for row in hydration["rows"]:
        key = (row["name"], row["version"], row["source"])
        require(key not in archive_rows and row["retained"]["sha256"] == row["checksum"]
                and hashlib.sha256(referenced(row["retained"])).hexdigest() == row["checksum"],
                "exact archive was changed or repeated")
        archive_rows[key] = row["checksum"]
    require(archive_rows == expected, "retained archive union differs from original two locks")
    vendor = referenced(prepared["vendor_audit"], json_data=True)
    vendor_root = Path(prepared["vendor_root"])
    require(vendor["packages"] == 212 and vendor["new_resolver_calls"] == 0,
            "vendor was resolved/pruned instead of transported")
    audit_rows = {(r["name"], r["version"], r["source"]): r for r in vendor["source_audit"]}
    require(len(audit_rows) == 212 and {k: r["package_checksum"] for k, r in audit_rows.items()} == expected,
            "vendor package/checksum set differs from original lock union")
    expected_files = set()
    for row in audit_rows.values():
        package = vendor_root / row["directory"]
        check = json.loads((package / ".cargo-checksum.json").read_text())
        require(check["package"] == row["package_checksum"]
                and len(check["files"]) == row["files"], "vendor checksum metadata changed")
        for name, checksum in check["files"].items():
            require(file_hash(package / name) == checksum, "vendored original payload changed")
            expected_files.add((Path(row["directory"]) / name).as_posix())
        expected_files.add((Path(row["directory"]) / ".cargo-checksum.json").as_posix())
    require(set(tree_files(vendor_root)) == expected_files, "vendor payload set changed")
    return prepared, locks, vendor_root


def migrate(ctx, task, oracle):
    from . import rust_lane, rust_overlay
    plan = rust_lane.load_plan(ctx, oracle)
    directory = ctx.task_directory(task, oracle, "rust")
    patch = ctx.golden(task, oracle, "rust", directory)
    prepared, original_locks, vendor_root = original_inputs(ctx, task, oracle, plan, patch)
    registry = Path(ctx.config.get("rust_toolchains", ctx.output.parent / "rust-toolchains/toolchains.json"))
    toolchain = Path(json.loads(registry.read_text())["1.88.0"]["root"])
    require(file_hash(toolchain / "bin/cargo") == CARGO_SHA and file_hash(toolchain / "bin/rustc") == RUSTC_SHA,
            "original Rust1.88 compiler bytes changed")
    compiler = rust_lane.compiler(ctx, "1.88.0")
    image, image_audit = ctx.image(rust_lane.IMAGE)
    require(image == prepared["native_image"] == rust_lane.IMAGE, "original diagnostic native image changed")
    assets = [
        {**compiler, "kind": "archive", "target": RUST, "visibility": "developer"},
        {**ctx.bundle(vendor_root), "kind": "archive", "target": rust_vendor.TARGET, "visibility": "developer"},
    ]
    config, _ = rust_vendor.source_configuration([rust_vendor.CRATES_IO])
    config_path = ctx.output / "image-cargo-sources.toml"
    config_path.write_text(config)
    source_config = ctx.copy_asset(config_path, "image-source-config")
    assets.append({**source_config, "kind": "file", "target": SOURCES, "visibility": "developer"})
    lock_assets = []
    for index, record in enumerate(sorted(original_locks.values(), key=lambda r: r["sha256"])):
        copied = ctx.copy_asset(Path(record["path"]), "image-original-locks")
        require(copied["sha256"] == record["sha256"], "copied original lock changed")
        target = LOCK_PREFIX + str(index)
        assets.append({**copied, "kind": "file", "target": target, "visibility": "developer"})
        lock_assets.append({"path": target, "sha256": copied["sha256"]})
    public = {
        "schema_version": entry.SCHEMA, "task_id": TASK, "selection": entry.SELECTION,
        "workdir": ".", "lock_path": "Cargo.lock", "locks": lock_assets,
        "cargo": {"path": RUST + "/bin/cargo", "sha256": CARGO_SHA},
        "rustc": {"path": RUST + "/bin/rustc", "sha256": RUSTC_SHA},
        "source_config": {"path": SOURCES, "sha256": source_config["sha256"]},
        "cpus": 4, "timeout_seconds": 1800, "metadata_timeout_seconds": 120,
        "max_log_bytes": 16 * 1024**2,
    }
    private = {
        "schema_version": entry.PRIVATE_SCHEMA, "task_id": TASK,
        "harness": {"path": HARNESS, "sha256": HARNESS_SHA},
        "command": COMMAND[2:], "expected_tests": EXPECTED,
    }
    entry.validate(public, private)
    public_file, private_file = ctx.output / "image-retained-locks.json", ctx.output / "image-private.json"
    atomic_json(public_file, public)
    atomic_json(private_file, private)
    assets.extend([
        {**ctx.copy_asset(public_file, "image-public-spec"), "kind": "file", "target": SPEC, "visibility": "developer"},
        {**ctx.copy_asset(private_file, "image-private-spec"), "kind": "file", "target": PRIVATE, "visibility": "grader"},
    ])
    tools = rust_overlay.build_tools(ctx, extra_entries={
        "image_locks.py": Path(__file__).with_name("rust_image_entry.py"),
    })
    assets.append({**tools, "kind": "archive", "target": rust_overlay.TOOLS, "visibility": "developer"})
    harness = ctx.legacy / "rust/session/049/oracle_pnm.rs"
    require(file_hash(harness) == HARNESS_SHA, "original session harness changed")
    harness_asset = ctx.copy_asset(harness, "image-original-harness")
    harness_target = "/opt/reviewbench-assets/image-oracle-pnm.rs"
    assets.append({**harness_asset, "kind": "file", "target": harness_target, "visibility": "grader"})
    prefix = ["python3", TOOL, "--spec", SPEC, "--task-id", TASK]
    require(ctx.modules(task) == [], "original source unexpectedly gained submodules")
    recipe = RuntimeRecipe(
        task_id=TASK, repo=REPO, start_sha=START, lane="rust",
        image=image, image_audit=image_audit, workdir="/workspace",
        cpus=4, memory_mb=8192, build_scratch_mb=24576, command_timeout_seconds=1800,
        env={
            "PATH": RUST + "/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "RUSTC": RUST + "/bin/rustc", "CARGO_HOME": "/opt/reviewbench-cache/cargo",
            "CARGO_TARGET_DIR": "/opt/reviewbench-cache/target", "CARGO_BUILD_JOBS": "4",
            "CARGO_TERM_COLOR": "never", "CARGO_NET_OFFLINE": "true", "CARGO_NET_RETRY": "0",
            "CARGO_INCREMENTAL": "0", "CARGO_PROFILE_DEV_DEBUG": "0", "CARGO_PROFILE_TEST_DEBUG": "0",
            "RUSTFLAGS": "--cap-lints=allow", "RUST_BACKTRACE": "1", "TMPDIR": "/workspace/.reviewbench/tmp",
        },
        cache_mounts={"cargo": "/opt/reviewbench-cache/cargo", "target": "/opt/reviewbench-cache/target",
                      "rust-build": rust_overlay.BUILD_CACHE},
        bootstrap=["mkdir -p /workspace/.reviewbench/tmp"],
        dev_checks=[shlex.join(prefix)], assets=assets, submodules=[],
        grading={
            "multifile_build": True, "immutable_paths": sorted(set(ctx.immutable_tests(task)) | {HARNESS}),
            "golden_patch": patch,
            "bootstrap": ["mkdir -p /workspace/tests && cp " + harness_target + " /workspace/" + HARNESS],
            "commands": [{"command": shlex.join([*prefix, "--private", PRIVATE]),
                          "parser": "rust-test", "expected_tests": EXPECTED}],
        },
    )
    recipe.validate()
    atomic_json(ctx.output / "image-original-input-audit.json", {
        "task_id": TASK, "prepared_input_sha256": file_hash(Path(ctx.config[OPTION][TASK])),
        "original_lock_sha256": LOCKS, "selection": entry.SELECTION,
        "source_default_features_original_harness_preserved": True,
        "new_dependency_resolutions": 0, "original_command": COMMAND, "runtime": recipe.fingerprint,
    })
    return recipe
