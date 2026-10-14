"""Bind RPGP's committed seed and explicitly derived original-log golden graph."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import shlex
import tomllib

from reviewbench_maintainability.agentic.contracts import ContractError, RuntimeRecipe, atomic_json, canonical_hash, file_hash
from reviewbench_maintainability.agentic.snapshots import tree_files
from . import rust_image_entry as entry, rust_vendor
from .rust_lock_assets import reference_trees
from .rust_overlay import session_bytes
from .native_submodules import readonly_git, readonly_links

TASK = "github__rpgp__rpgp__pull_662__comment_2980868075__modularity"
REPO = "rpgp/rpgp"
START = "33672c7c2da2d8a500c6ccf77eaf0bcfb91cb604"
GOLDEN = "7259a1269688c3192fcaa84b926ab5acd8c4b4fa7be029a1caae51ab94bcabba"
PLAN = "244a66f8170aef20f4ea022dc7b0f764f80ae2026351881fa62886be8ab12412"
SEED = "64e74f2f3feb62b5c949caf4c77c7b8f6f4b3c2da145e140ebd8b2012fdf59e9"
DERIVED = "78ff8e2c1134e7936ff33357f08fab5f044c98d4122504e7062db0f90f3222cd"
MODULE = "5677ea606ef95dc9bd476c2c6de46a11b15d2faa"
HARNESS = "tests/oracle_session.rs"
HARNESS_SHA = "973ef7d5b5ef7f24a7debcf63180b79809121508201a8f49dc93f1cda4ad3473"
EXPECTED = ["password_protected_seipdv1_roundtrips"]
COMMAND = ["cargo", "+1.93.0", "test", "-p", "pgp", "--test", "oracle_session", "--", "--nocapture"]
CARGO_SHA = "9548937d530bf439ff1ba47a3b2bd26eeb9c3aff1961c20c01798613de922578"
RUSTC_SHA = "d32249a7c3bfcfc67b471460386e46323accae7125e344567a12d5664d99bb57"
OPTION = "rust_rpgp_reference_seed"
SPEC = "/opt/reviewbench-assets/rpgp-reference-seed.json"
PRIVATE = "/opt/reviewbench-assets/rpgp-reference-seed-private.json"
SOURCES = "/opt/reviewbench-assets/rpgp-cargo-sources.toml"
TOOL = "/opt/reviewbench-assets/rust-build-tools/rpgp_locks.py"
LOCK_PREFIX = "/opt/reviewbench-assets/rpgp-environment-lock-"
RUST = "/opt/reviewbench-assets/rust"


def require(value, message):
    if not value:
        raise ContractError("RPGP original reference seed: " + message)


def referenced(row, *, json_data=False):
    path = Path(row["path"])
    require(path.is_absolute() and path.is_file() and not path.is_symlink(), "missing regular input")
    require(path.stat().st_size <= 32 * 1024**2 and file_hash(path) == row["sha256"], "input bytes changed")
    data = path.read_bytes()
    require(hashlib.sha256(data).hexdigest() == row["sha256"], "input changed during read")
    return json.loads(data) if json_data else data


def requested(ctx, task):
    if task.get("id") != TASK:
        return False
    options = getattr(ctx, "config", {}).get(OPTION, {})
    require(isinstance(options, dict) and not (set(options) - {TASK}), "invalid task opt-in")
    return TASK in options


def expected_snapshot(files):
    return {name: (["symlink", row["link_target"]] if "link_target" in row
                   else ["file", row["sha256"], bool(row["mode"] & 0o111)])
            for name, row in files.items()}


def original_inputs(ctx, task, oracle, plan, patch):
    require((task.get("id"), task.get("repo"), task.get("start_sha")) == (TASK, REPO, START)
            and (oracle.get("id"), oracle.get("repo"), oracle.get("start_sha")) == (TASK, REPO, START),
            "wrong task/repo/start")
    require(canonical_hash(plan) == PLAN and plan["input_index"] == oracle["input_index"] == 88
            and shlex.split(plan["command"]) == COMMAND and plan["toolchain"] == "1.93.0"
            and plan["timeout_seconds"] == 2400 and plan["build_jobs"] == 4
            and plan["features"] == [] and plan["workdir"] == ".", "original command/plan changed")
    require(patch["sha256"] == oracle["golden_patch_sha256"] == GOLDEN
            and file_hash(Path(patch["path"])) == GOLDEN and oracle["pass_to_pass_tests"] == EXPECTED,
            "original golden or test changed")
    for variant in ("start", "golden"):
        require(shlex.split(oracle["environment"]["variant_commands"][variant]) == COMMAND
                and oracle["environment"]["variant_toolchains"][variant] == "1.93.0"
                and oracle["environment_replacements"][variant] == [], "variant contract changed")
    require(len(plan["session_files"]) == 1 and plan["session_files"][0]["path"] == HARNESS
            and hashlib.sha256(session_bytes(ctx, plan, oracle, plan["session_files"][0])).hexdigest() == HARNESS_SHA,
            "original roundtrip harness changed")
    path = Path(ctx.config[OPTION][TASK])
    require(path.is_absolute() and path.is_file() and not path.is_symlink(), "invalid prepared input")
    prepared = json.loads(path.read_text())
    require(prepared["schema_version"] == "reviewbench-rpgp88-reference-seed-inputs/v1"
            and prepared["task_id"] == TASK
            and prepared["new_golden_resolution_is_derived_not_historical_attestation"] is True,
            "derived golden was misrepresented as historical recovery")
    require(referenced(prepared["original_task"], json_data=True) == task
            and referenced(prepared["original_oracle"], json_data=True) == oracle
            and referenced(prepared["original_plan"], json_data=True) == plan,
            "prepared selection differs from original inputs")
    old_recipe = referenced(prepared["original_recipe"], json_data=True)
    require((old_recipe["task_id"], old_recipe["repo"], old_recipe["start_sha"]) == (TASK, REPO, START)
            and old_recipe["grading"]["golden_patch"]["sha256"] == GOLDEN
            and old_recipe["grading"]["commands"][0]["expected_tests"] == EXPECTED,
            "original recipe changed")
    source = referenced(prepared["source_preparation"], json_data=True)
    init = referenced(prepared["golden_initialization"], json_data=True)
    diagnostics = referenced(prepared["original_diagnostics"], json_data=True)
    logged = referenced(prepared["original_logged_updates"], json_data=True)
    raw_log = referenced(logged["original_golden_log"]).decode()
    updates = [{"name": n, "from": a, "to": b} for n, a, b in re.findall(
        r"^\s*Updating ([A-Za-z0-9_-]+) v([0-9A-Za-z.+-]+) -> v([0-9A-Za-z.+-]+)", raw_log, re.M)]
    require(len(updates) == 13 and updates == logged["updates"] == init["logged_version_updates"],
            "initialization does not follow the thirteen original logged versions")
    require(init["old_sha256"] == SEED and init["new_sha256"] == DERIVED
            and init["new_golden_resolution_calls"] == 1 and init["original_start_resolution_calls"] == 0
            and init["historical_golden_lock_hash_claimed"] is False
            and init["original_sources_and_start_lock_unchanged"], "golden initialization provenance changed")
    require(diagnostics["original_command"] == plan["command"]
            and diagnostics["original_SUT_diagnostic_calls"] == 2
            and diagnostics["new_model_judge_calls"] == 0, "original command diagnostics changed")
    references = reference_trees(ctx, task, patch, {"Cargo.lock"})
    lockfiles = {}
    for variant, lock_sha in (("start", SEED), ("golden", DERIVED)):
        files = source["variants"][variant]["files"]
        complete = Path(source["variants"][variant]["complete_source"])
        work = Path(source["variants"][variant]["work"])
        require(len(files) == 730 and entry.snapshot(complete) == expected_snapshot(files),
                "full original source/submodule changed")
        require(file_hash(complete / "Cargo.lock") == SEED, "committed source lock replaced")
        manifests, locks = references[variant]
        require(set(locks) == {"Cargo.lock"} and hashlib.sha256(locks["Cargo.lock"]).hexdigest() == SEED
                and manifests == {n: row["sha256"] for n, row in files.items() if Path(n).name == "Cargo.toml"},
                "original source-only reference manifests/lock changed")
        expected = expected_snapshot(files)
        expected["Cargo.lock"] = ["file", lock_sha, False]
        expected[HARNESS] = ["file", HARNESS_SHA, False]
        require(entry.snapshot(work) == expected and file_hash(work / "Cargo.lock") == lock_sha,
                "prepared complete source/lock/harness changed")
        d = diagnostics["rows"][variant]
        require(d["exit_status"] == 0 and d["source_files"] == 731 and d["lock_sha256"] == lock_sha
                and d["offline_container"], "original diagnostic narrowed source or environment")
        actual = referenced(d["command_result"], json_data=True)
        require(actual["original_command"] == COMMAND and actual["exit_status"] == 0
                and actual["source_unchanged"] and actual["stop_reason"] is None,
                "original diagnostic was not a natural unchanged run")
        stdout = referenced(actual["stdout"]).decode()
        referenced(actual["stderr"])
        require("test password_protected_seipdv1_roundtrips ... ok" in stdout
                and "1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out;" in stdout,
                "original roundtrip did not pass")
        lockfiles[variant] = {"path": str(work / "Cargo.lock"), "sha256": lock_sha}
    require(readonly_links(ctx.repo(task), START) == [{"path": "tests/tests", "commit": MODULE}]
            and readonly_git(Path(prepared["module_git"]), "rev-parse", MODULE + "^{commit}").decode().strip() == MODULE,
            "required module pin changed")
    module = Path(prepared["module_source"])
    module_files = {n[len("tests/tests/"):]: row for n, row in source["variants"]["start"]["files"].items()
                    if n.startswith("tests/tests/")}
    require(len(module_files) == 13 and entry.snapshot(module) == expected_snapshot(module_files),
            "required submodule source incomplete")
    package_locks = [tomllib.loads(Path(row["path"]).read_text()) for row in lockfiles.values()]
    expected_packages = rust_vendor.locked_packages(package_locks)
    require(len(expected_packages) == 271 and all(k[2] == rust_vendor.CRATES_IO for k in expected_packages),
            "original/derived lock union changed")
    hydration = referenced(prepared["archive_hydration"], json_data=True)
    require(not hydration["missing"] and hydration["registry_packages"] == 271
            and {(r["name"], r["version"], r["source"]): r["checksum"] for r in hydration["rows"]} == expected_packages,
            "archive identity union changed")
    vendor = referenced(prepared["vendor_audit"], json_data=True)
    vendor_root = Path(prepared["vendor_root"])
    expected_files = set()
    require(vendor["packages"] == 271 and vendor["new_Cargo_resolutions"] == 0, "vendor was re-resolved")
    require({(r["name"], r["version"], r["source"]): r["package_checksum"] for r in vendor["source_audit"]}
            == expected_packages, "vendor graph differs from the bound two locks")
    for row in vendor["source_audit"]:
        directory = vendor_root / row["directory"]
        check = json.loads((directory / ".cargo-checksum.json").read_text())
        require(check["package"] == row["package_checksum"] and len(check["files"]) == row["files"],
                "vendor checksum metadata changed")
        for name, checksum in check["files"].items():
            require(file_hash(directory / name) == checksum, "vendored source changed")
            expected_files.add((Path(row["directory"]) / name).as_posix())
        expected_files.add((Path(row["directory"]) / ".cargo-checksum.json").as_posix())
    require(set(tree_files(vendor_root)) == expected_files, "vendor files added or removed")
    return prepared, old_recipe, lockfiles, vendor_root, module


def migrate(ctx, task, oracle):
    from . import rust_lane, rust_overlay
    plan = rust_lane.load_plan(ctx, oracle)
    directory = ctx.task_directory(task, oracle, "rust")
    patch = ctx.golden(task, oracle, "rust", directory)
    prepared, original, lockfiles, vendor_root, module = original_inputs(ctx, task, oracle, plan, patch)
    registry = Path(ctx.config.get("rust_toolchains", ctx.output.parent / "rust-toolchains/toolchains.json"))
    compiler_root = Path(json.loads(registry.read_text())["1.93.0"]["root"])
    require(file_hash(compiler_root / "bin/cargo") == CARGO_SHA
            and file_hash(compiler_root / "bin/rustc") == RUSTC_SHA, "original compiler changed")
    after = deepcopy(original)
    after["grading"]["golden_patch"] = patch
    vendor_asset = ctx.bundle(vendor_root)
    after["assets"] = [
        {**vendor_asset, "kind": "archive", "visibility": "developer", "target": rust_vendor.TARGET}
        if row["target"] == rust_vendor.TARGET else row for row in original["assets"]
    ]
    source_text, _ = rust_vendor.source_configuration([rust_vendor.CRATES_IO])
    source_file = ctx.output / "rpgp-cargo-sources.toml"
    source_file.write_text(source_text)
    source_asset = ctx.copy_asset(source_file, "rpgp-source-config")
    after["assets"].append({**source_asset, "kind": "file", "visibility": "developer", "target": SOURCES})
    locks = []
    for index, row in enumerate(sorted(lockfiles.values(), key=lambda r: r["sha256"])):
        asset = ctx.copy_asset(Path(row["path"]), "rpgp-environment-lock")
        require(asset["sha256"] == row["sha256"], "copied environment lock changed")
        target = LOCK_PREFIX + str(index)
        after["assets"].append({**asset, "kind": "file", "visibility": "developer", "target": target})
        locks.append({"path": target, "sha256": asset["sha256"]})
    public = {
        "schema_version": entry.REFERENCE_SCHEMA, "task_id": TASK, "selection": entry.SELECTION,
        "workdir": ".", "lock_path": "Cargo.lock", "locks": locks,
        "reference_seed_sha256": SEED, "reference_seed_policy": "replace-only-identical-committed-seed-in-private-mirror",
        "package": "pgp", "public_command": ["check", "--locked", "-p", "pgp"],
        "cargo": {"path": RUST + "/bin/cargo", "sha256": CARGO_SHA},
        "rustc": {"path": RUST + "/bin/rustc", "sha256": RUSTC_SHA},
        "source_config": {"path": SOURCES, "sha256": source_asset["sha256"]},
        "cpus": 4, "timeout_seconds": 2400, "metadata_timeout_seconds": 120, "max_log_bytes": 16 * 1024**2,
    }
    private = {
        "schema_version": entry.PRIVATE_SCHEMA, "task_id": TASK, "original_unlocked": True,
        "harness": {"path": HARNESS, "sha256": HARNESS_SHA}, "command": COMMAND[2:], "expected_tests": EXPECTED,
    }
    entry.validate(public, private)
    for name, content, target, visibility in (
        ("rpgp-reference-seed.json", public, SPEC, "developer"),
        ("rpgp-private.json", private, PRIVATE, "grader"),
    ):
        path = ctx.output / name
        atomic_json(path, content)
        after["assets"].append({**ctx.copy_asset(path, "rpgp-spec"), "kind": "file",
                                "visibility": visibility, "target": target})
    tools = rust_overlay.build_tools(ctx, extra_entries={
        "rpgp_locks.py": Path(__file__).with_name("rust_image_entry.py"),
    })
    after["assets"].append({**tools, "kind": "archive", "visibility": "developer", "target": rust_overlay.TOOLS})
    after["cache_mounts"]["rust-build"] = rust_overlay.BUILD_CACHE
    after["submodules"] = [{"path": "tests/tests", "commit": MODULE,
                            "asset": ctx.bundle(module, role="submodule", git_commit=MODULE)}]
    prefix = ["python3", TOOL, "--spec", SPEC, "--task-id", TASK]
    after["dev_checks"] = [shlex.join(prefix)]
    after["grading"]["commands"][0]["command"] = shlex.join([*prefix, "--private", PRIVATE])
    require(after["grading"]["immutable_paths"] == original["grading"]["immutable_paths"]
            and after["grading"]["bootstrap"] == original["grading"]["bootstrap"]
            and after["env"] == original["env"] and after["bootstrap"] == original["bootstrap"],
            "original source/test/environment contract drifted")
    recipe = RuntimeRecipe.from_dict(after)
    atomic_json(ctx.output / "rpgp-input-audit.json", {
        "task_id": TASK, "prepared_input_sha256": file_hash(Path(ctx.config[OPTION][TASK])),
        "original_committed_seed": SEED, "derived_golden_lock": DERIVED,
        "golden_lock_is_historical_attestation": False, "new_dependency_resolutions": 0,
        "original_command": COMMAND, "required_submodule": MODULE, "runtime_fingerprint": recipe.fingerprint,
    })
    return recipe
