"""Bind the original Sentry41 session, symmetric overlays and two unchanged locks."""
import hashlib
import json
from pathlib import Path
import shlex
import tomllib

from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, atomic_json, canonical_hash, file_hash,
)
from reviewbench_maintainability.agentic.snapshots import tree_files
from . import rust_session_overlay_entry as entry, rust_vendor
from .rust_build_entry import manifest_operations
from .rust_lock_assets import reference_trees
from .rust_overlay import reference_inputs, session_bytes, lock_prepare

TASK = "github__getsentry__sentry-rust__pull_479__comment_908614480__duplication"
REPO = "getsentry/sentry-rust"
START = "d29e807d617c07742ab7096d71225921e08f7371"
PLAN = "922e928c55a16e6856c17f740861af5de860bd79e69daccfe0377bdd60c95515"
GOLDEN = "7843d01ed4b137e4109a33f050dc05c01360fe0f79b4359e351de3b910930865"
LOCKS = {
    "start": "c9f135dc3e868f103d335aec83a46565701d4ef4aaa568bc7ace7df32fb6d0e8",
    "golden": "005bb7dc23ba518d514d9662d6a619c9e7ae252f890e7b74011291a2891c1344",
}
OPTION = "rust_original_session_overlays"
SPEC = "/opt/reviewbench-assets/sentry-session.json"
PRIVATE = "/opt/reviewbench-assets/sentry-session-private.json"
OVERLAY = "/opt/reviewbench-assets/sentry-session-overlay.json"
SOURCES = "/opt/reviewbench-assets/sentry-session-sources.toml"
TOOL = "/opt/reviewbench-assets/rust-build-tools/session_overlay.py"
ROOT_MANIFEST = "tests/oracle-session/Cargo.toml"


def require(value, message):
    if not value:
        raise ContractError("original Sentry session: " + message)


def referenced(row, *, document=False):
    path = Path(row["path"])
    require(path.is_absolute() and path.is_file() and not path.is_symlink()
            and path.stat().st_size <= 32 * 1024**2 and file_hash(path) == row["sha256"],
            "input file/hash changed")
    data = path.read_bytes()
    require(hashlib.sha256(data).hexdigest() == row["sha256"], "input changed during read")
    return json.loads(data) if document else data


def requested(ctx, task):
    values = getattr(ctx, "config", {}).get("migration", {}).get(OPTION, {})
    require(isinstance(values, dict) and not (set(values) - {TASK}), "unsupported opt-in")
    return task.get("id") == TASK and TASK in values


def original_inputs(ctx, task, oracle, plan, patch):
    require((task.get("id"), task.get("repo"), task.get("start_sha")) == (TASK, REPO, START)
            and (oracle.get("id"), oracle.get("repo"), oracle.get("start_sha")) == (TASK, REPO, START),
            "task identity changed")
    require(canonical_hash(plan) == PLAN and plan["input_index"] == oracle["input_index"] == 41
            and plan["toolchain"] == "1.60.0", "original plan/compiler changed")
    require(patch["sha256"] == oracle["golden_patch_sha256"] == GOLDEN
            and file_hash(Path(patch["path"])) == GOLDEN, "original golden changed")
    setting = ctx.config["migration"][OPTION][TASK]
    require(set(setting) == {"path", "sha256"}, "prepared inputs need an independent SHA pin")
    prepared = referenced(setting, document=True)
    require(prepared["schema_version"] == "reviewbench-original-sentry41-inputs/v1"
            and prepared["task_id"] == TASK and prepared["original_SUT_model_judge_calls"] == 0,
            "unbound original preparation")
    require(referenced(prepared["task"], document=True) == task
            and referenced(prepared["oracle"], document=True) == oracle
            and referenced(prepared["plan"], document=True) == plan
            and hashlib.sha256(referenced(prepared["golden"])).hexdigest() == GOLDEN,
            "prepared task/Oracle/plan/golden differs")
    harness = {row["path"]: session_bytes(ctx, plan, oracle, row) for row in plan["session_files"]}
    require(len(harness) == 4 and ROOT_MANIFEST in harness
            and "tests/oracle-session/src/lib.rs" in harness, "private session inventory changed")
    interface = tomllib.loads(harness[ROOT_MANIFEST].decode())
    require(interface["package"]["name"] == "sentry-oracle-session"
            and interface["package"]["version"] == "0.0.0"
            and interface["dependencies"]["sentry-core"] ==
            {"path": "../../sentry-core", "features": ["client", "profiling", "test"]},
            "original public API/features changed")
    references = reference_trees(ctx, task, patch, {"Cargo.lock"})
    snapshots = referenced(prepared["source_reference"], document=True)
    overlays = {}
    names = sorted({row["path"] for row in plan["environment_replacements"]})
    inputs = reference_inputs(ctx, task, patch, names)
    locks = []
    for variant, checksum in LOCKS.items():
        require(shlex.split(oracle["environment"]["variant_commands"][variant]) == shlex.split(plan["command"])
                and oracle["environment"]["variant_toolchains"][variant] == "1.60.0",
                "original per-variant command/compiler changed")
        manifests, original_locks = references[variant]
        require(original_locks == {"Cargo.lock": None}, "original root acquired a committed lock")
        require(manifests == {n: row[1] for n, row in snapshots[variant]["snapshot"].items()
                             if Path(n).name == "Cargo.toml"}, "complete reference manifests changed")
        outputs = dict(inputs[variant])
        for index, row in enumerate(plan["environment_replacements"]):
            name, old, new = row["path"], row["old"], row["new"]
            content = outputs[name].decode()
            require(content.count(old) == row.get("count", 1), "original overlay occurrence count changed")
            outputs[name] = content.replace(old, new, row.get("count", 1)).encode()
            evidence = oracle["environment_replacements"][variant][index]
            require(evidence["path"] == name
                    and evidence["sha256"] == hashlib.sha256(outputs[name]).hexdigest(),
                    "original intermediate manifest hash changed")
        overlays[variant] = [
            {"path": name, "operations": manifest_operations(
                tomllib.loads(inputs[variant][name].decode()), tomllib.loads(outputs[name].decode()))}
            for name in names]
        operation = lock_prepare(plan[variant + "_prepare_command"])
        content = referenced(prepared["locks"][variant])
        require(operation["path"] == "Cargo.lock" and operation["sha256"] == checksum
                and harness[operation["source"]] == content
                and hashlib.sha256(content).hexdigest() == checksum, "original checked-copy lock changed")
        lock = tomllib.loads(content.decode())
        locks.append(lock)
        require(len(prepared["original_logs"][variant]) == len(oracle["runs"][variant]) == 3,
                "six original named observations missing")
        for row, original in zip(prepared["original_logs"][variant], oracle["runs"][variant]):
            text = referenced(row).decode()
            require(Path(row["path"]).resolve() == (ctx.legacy / "rust" / original["log"]).resolve()
                    and original["ok"] and original["passed_tests"] == oracle["pass_to_pass_tests"]
                    and not original["failed_tests"]
                    and all("test " + n + " ... ok" in text for n in oracle["pass_to_pass_tests"]),
                    "original named behavior evidence changed")
        for role in ("private", "public"):
            observed = prepared["observations"][role + "-" + variant]
            require(observed["exit_status"] == 0 and observed["source_unchanged"] and observed["locked"],
                    "original offline graph was not verified unchanged")
            data = referenced(observed["stdout"], document=True)
            referenced(observed["stderr"])
            expected = {(p["name"], p["version"], p.get("source")) for p in lock["package"]}
            if role == "public":
                expected.remove(("sentry-oracle-session", "0.0.0", None))
            require({(p["name"], p["version"], p.get("source")) for p in data["packages"]} == expected,
                    "public/private graph changed beyond the private workspace member")
    require(overlays["start"] == overlays["golden"], "original build overlays are not symmetric")
    overlay = referenced(prepared["overlay"], document=True)
    require(overlay["manifests"] == overlays["start"] and overlay["locks"] == []
            and overlay["private_members"] == ["tests/oracle-session"]
            and overlay["task_id"] == TASK, "prepared semantic overlays changed")
    expected = rust_vendor.locked_packages(locks)
    audit = referenced(prepared["vendor_audit"], document=True)["packages"]
    require({(p["name"], p["version"], p["source"]): p["package_checksum"] for p in audit} == expected,
            "vendor is not the two complete original lock union")
    vendor = Path(prepared["vendor_root"])
    require(vendor.is_absolute() and vendor.is_dir() and not vendor.is_symlink(), "missing vendor")
    files = set()
    for row in audit:
        root = vendor / row["directory"]
        require(root.resolve().is_relative_to(vendor.resolve()) and not root.is_symlink(), "vendor path escapes")
        checks = json.loads((root / ".cargo-checksum.json").read_text())
        require(checks["package"] == row["package_checksum"] and len(checks["files"]) == row["files"],
                "vendor identity inventory changed")
        for name, checksum in checks["files"].items():
            require(file_hash(root / name) == checksum, "vendored original bytes changed")
            files.add((root / name).relative_to(vendor).as_posix())
        files.add((root / ".cargo-checksum.json").relative_to(vendor).as_posix())
    require(set(tree_files(vendor)) == files, "vendor files added or removed")
    referenced(prepared["source_config"])
    return prepared, harness, overlay


def migrate(ctx, task, oracle):
    from . import rust_lane, rust_overlay
    plan = rust_lane.load_plan(ctx, oracle)
    directory = ctx.task_directory(task, oracle, "rust")
    patch = ctx.golden(task, oracle, "rust", directory)
    prepared, harness, overlay = original_inputs(ctx, task, oracle, plan, patch)
    registry = json.loads(Path(ctx.config["rust_toolchains"]).read_text())
    tool = Path(registry["1.60.0"]["root"])
    require(file_hash(tool / "bin/cargo") == prepared["cargo"]["sha256"]
            and file_hash(tool / "bin/rustc") == prepared["rustc"]["sha256"], "pinned compiler changed")
    compiler = rust_lane.compiler(ctx, "1.60.0")
    vendor = ctx.bundle(Path(prepared["vendor_root"]))
    source = ctx.copy_asset(Path(prepared["source_config"]["path"]), "sentry-session-sources")
    overlay_file = ctx.copy_asset(Path(prepared["overlay"]["path"]), "sentry-session-overlay")
    image, image_audit = ctx.image(rust_lane.IMAGE)
    assets = [
        {**compiler, "kind": "archive", "visibility": "developer", "target": "/opt/reviewbench-assets/rust"},
        {**vendor, "kind": "archive", "visibility": "developer", "target": rust_vendor.TARGET},
        {**source, "kind": "file", "visibility": "developer", "target": SOURCES},
        {**overlay_file, "kind": "file", "visibility": "developer", "target": OVERLAY},
    ]
    locks = []
    for index, row in enumerate(prepared["locks"].values()):
        target = "/opt/reviewbench-assets/sentry-session-lock-" + str(index)
        asset = ctx.copy_asset(Path(row["path"]), "sentry-session-lock")
        assets.append({**asset, "kind": "file", "visibility": "developer", "target": target})
        locks.append({"path": target, "sha256": asset["sha256"]})
    public = {
        "schema_version": entry.SCHEMA, "task_id": TASK, "selection": entry.core.SELECTION,
        "cargo": {"path": "/opt/reviewbench-assets/rust/bin/cargo", "sha256": prepared["cargo"]["sha256"]},
        "rustc": {"path": "/opt/reviewbench-assets/rust/bin/rustc", "sha256": prepared["rustc"]["sha256"]},
        "source_config": {"path": SOURCES, "sha256": source["sha256"]},
        "overlay": {"path": OVERLAY, "sha256": overlay_file["sha256"]}, "locks": locks,
        "session": {"manifest": ROOT_MANIFEST, "package": "sentry-oracle-session", "version": "0.0.0"},
        "public_command": ["check", "--locked", "-p", "sentry-core", "--features", ",".join(plan["features"])],
        "cpus": 4, "timeout_seconds": plan["timeout_seconds"], "metadata_timeout_seconds": 120,
        "max_log_bytes": 16 * 1024**2,
    }
    private = {
        "schema_version": entry.PRIVATE_SCHEMA, "task_id": TASK, "command": shlex.split(plan["command"])[2:],
        "session_files": [{"path": name, "sha256": hashlib.sha256(data).hexdigest()}
                          for name, data in sorted(harness.items())],
        "expected_tests": oracle["pass_to_pass_tests"],
    }
    entry.validate(public, private)
    for name, value, target, visibility in (("public", public, SPEC, "developer"),
                                            ("private", private, PRIVATE, "grader")):
        path = ctx.output / ("sentry-session-" + name + ".json")
        atomic_json(path, value)
        assets.append({**ctx.copy_asset(path, "sentry-session-spec"), "kind": "file",
                       "visibility": visibility, "target": target})
    tools = rust_overlay.build_tools(ctx, extra_entries={
        "image_locks.py": Path(__file__).with_name("rust_image_entry.py"),
        "session_overlay.py": Path(__file__).with_name("rust_session_overlay_entry.py"),
    })
    assets.append({**tools, "kind": "archive", "visibility": "developer", "target": rust_overlay.TOOLS})
    root = ctx.output / "sentry-original-session"
    for name, data in harness.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    session_asset = ctx.bundle(root, role="grader")
    session_target = "/opt/reviewbench-assets/sentry-original-session"
    assets.append({**session_asset, "kind": "archive", "visibility": "grader", "target": session_target})
    grade = []
    for name in sorted(harness):
        source_path, destination = session_target + "/" + name, "/workspace/" + name
        grade.append("mkdir -p " + shlex.quote(str(Path(destination).parent))
                     + " && if [ -e " + shlex.quote(destination) + " ] || [ -L " + shlex.quote(destination)
                     + " ]; then cmp -s " + shlex.quote(source_path) + " " + shlex.quote(destination)
                     + "; else cp " + shlex.quote(source_path) + " " + shlex.quote(destination) + "; fi")
    prefix = ["python3", TOOL, "--spec", SPEC, "--task-id", TASK]
    recipe = RuntimeRecipe(
        task_id=TASK, repo=REPO, start_sha=START, lane="rust", image=image, image_audit=image_audit,
        workdir="/workspace", cpus=4, memory_mb=8192, build_scratch_mb=24576,
        command_timeout_seconds=plan["timeout_seconds"], env={
            "PATH": "/opt/reviewbench-assets/rust/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "RUSTC": "/opt/reviewbench-assets/rust/bin/rustc", "CARGO_HOME": "/opt/reviewbench-cache/cargo",
            "CARGO_TARGET_DIR": "/opt/reviewbench-cache/target", "CARGO_BUILD_JOBS": "4",
            "CARGO_TERM_COLOR": "never", "CARGO_INCREMENTAL": "0", "CARGO_NET_OFFLINE": "true",
            "CARGO_PROFILE_DEV_DEBUG": "0", "CARGO_PROFILE_TEST_DEBUG": "0",
            "RUSTFLAGS": "--cap-lints=allow", "RUST_BACKTRACE": "1", "SKIP_WASM_BUILD": "1",
            "RUST_BUILD_DOCKER": "1", "GIT_COMMIT": "reviewbench-hard-oracle", "GIT_DIRTY": "false",
            "SOURCE_TIMESTAMP": "0", "TMPDIR": "/workspace/.reviewbench/tmp", "PROTOC_INCLUDE": "/usr/include",
        }, cache_mounts={"cargo": "/opt/reviewbench-cache/cargo", "target": "/opt/reviewbench-cache/target",
                         "rust-build": rust_overlay.BUILD_CACHE},
        bootstrap=["mkdir -p /workspace/.reviewbench/tmp"], dev_checks=[shlex.join(prefix)],
        assets=assets, scratch_paths=[], submodules=ctx.modules(task),
        grading={"multifile_build": True, "immutable_paths": ctx.immutable_tests(task), "golden_patch": patch,
                 "bootstrap": grade, "commands": [{"command": shlex.join([*prefix, "--private", PRIVATE]),
                 "parser": "rust-test", "expected_tests": oracle["pass_to_pass_tests"]}]},
    )
    recipe.validate()
    return recipe
