"""Bind both original Cargo-contract workspaces to their retained lock graphs."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shlex
import tomllib

from reviewbench_maintainability.agentic.contracts import ContractError, RuntimeRecipe, atomic_json, canonical_hash, file_hash
from reviewbench_maintainability.agentic.snapshots import tree_files
from . import rust_image_entry as entry, rust_vendor
from .rust_lock_assets import reference_trees
from .rust_overlay import session_bytes

REPO = "use-ink/cargo-contract"
OPTION = "rust_cargo_contract_retained_locks"
HARNESS = "crates/cargo-contract/tests/oracle.rs"
MANIFEST = "crates/cargo-contract/Cargo.toml"
CARGO_SHA = "4c9a2415cb329832c05bc36edb15d62b7a9ad9b7224dd50aa0fd03eeea32a513"
RUSTC_SHA = "5633b4115dbdbdfe202e8b17b027c1eee27b554d8980e4fc006df8d4c6005583"
COMMAND = ["cargo", "+1.70.0", "test", "-p", "cargo-contract", "--test", "oracle", "--", "--nocapture"]
TARGET_LOCK = "5da41ca4fb491d7a8b5ceb89219cf39e970e80715ab8176aa832e4decd31c080"
TARGET_PREPARE = "git config --global --add safe.directory '*' && git show 358ed0fdcd51e3c6a5f7edee8c96c21b96caff0d:Cargo.lock > Cargo.lock"
TASKS = {
    "github__use-ink__cargo-contract__pull_1213__comment_1291334340__api_design": {
        "index": 108, "start": "ac6ef0995a65ba6f61d532491c17dab1710afdfa",
        "golden": "5a7544f06b69c426653de3d56af8a456c33617137a666dbea459da0b102cfe05",
        "plan": "da1a5853799b1b6ecb86a1f54d9b692b6bf7206a9c14af250ef2001bccac6a95",
        "seed": "a02b56b1ac35a8ffc493c6aa3a525e6cfa42317951ff3b9ff578c28d4c0af5e4",
        "retained_golden": TARGET_LOCK,
        "harness": "c5310a518a597483d1f1d2da6e5dbf795980d7c2aedd7e38b7f8b77b2705e269",
        "expected": "instantiate_salt_is_parsed_then_reports_error",
    },
    "github__use-ink__cargo-contract__pull_1213__comment_1299911820__modularity": {
        "index": 109, "start": "24eb4d7240977613a550e7b388c96e12dea220d6",
        "golden": "341b859e9279068da2296263837ce13176b22dd8829817722ae2f526969dc7de",
        "plan": "683c1457f9890a2e26ce5c6fe27713822424d1eb0d37735a28168dfa79c8e068",
        "seed": "a76ab51bfcd40d4e3ec37ef9c6328c269e3edd89bf6bab8b7ef46387c7b1c335",
        "retained_golden": "53d61a21b7397daa76594375833ec62a06ef66a4a5d249b194a5dd6338dee719",
        "harness": "52fcb44269e79ddcfb8b7ab9901149b91f2fb60edc861db742ea991b2e9bf41c",
        "expected": "upload_without_valid_artifact_reports_error",
    },
}
SPEC = "/opt/reviewbench-assets/cargo-contract-reference.json"
PRIVATE = "/opt/reviewbench-assets/cargo-contract-reference-private.json"
SOURCES = "/opt/reviewbench-assets/cargo-contract-sources.toml"
RUST = "/opt/reviewbench-assets/rust"


def require(value, message):
    if not value:
        raise ContractError("Cargo-contract original retained inputs: " + message)


def referenced(row, *, decode=True):
    path = Path(row["path"])
    require(path.is_absolute() and path.is_file() and not path.is_symlink(), "missing regular prepared input")
    require(path.stat().st_size <= 32 * 1024**2 and file_hash(path) == row["sha256"], "prepared input changed")
    data = path.read_bytes()
    require(hashlib.sha256(data).hexdigest() == row["sha256"], "prepared input changed during read")
    return json.loads(data) if decode else data


def requested(ctx, task):
    if task.get("id") not in TASKS:
        return False
    options = getattr(ctx, "config", {}).get(OPTION, {})
    require(isinstance(options, dict) and not (set(options) - set(TASKS)), "invalid original task opt-in")
    return task["id"] in options


def expected_snapshot(files):
    return {name: (["symlink", row["link_target"]] if "link_target" in row else
                   ["file", row["sha256"], bool(row["mode"] & 0o111)]) for name, row in files.items()}


def original_inputs(ctx, task, oracle, plan, patch):
    pin = TASKS.get(task.get("id"))
    require(pin is not None and task.get("repo") == oracle.get("repo") == REPO
            and task.get("id") == oracle.get("id")
            and task.get("start_sha") == oracle.get("start_sha") == pin["start"], "wrong task/repo/start")
    require(canonical_hash(plan) == pin["plan"] and plan["input_index"] == oracle["input_index"] == pin["index"]
            and shlex.split(plan["command"]) == COMMAND and plan["toolchain"] == "1.70.0"
            and plan["build_jobs"] == 16 and plan["timeout_seconds"] == 3600
            and plan["features"] == [] and plan["workdir"] == ".", "original command/plan changed")
    require(plan.get("golden_prepare_command") == (TARGET_PREPARE if pin["index"] == 108 else None),
            "task109 cannot inherit task108's explicit golden preparation")
    require(patch["sha256"] == oracle["golden_patch_sha256"] == pin["golden"]
            and file_hash(Path(patch["path"])) == pin["golden"]
            and oracle["pass_to_pass_tests"] == [pin["expected"]], "original patch/test identity changed")
    require(len(plan["session_files"]) == 1 and plan["session_files"][0]["path"] == HARNESS
            and hashlib.sha256(session_bytes(ctx, plan, oracle, plan["session_files"][0])).hexdigest() == pin["harness"],
            "original CLI assertion/harness changed")
    for variant in ("start", "golden"):
        require(oracle["environment"]["variant_commands"][variant] == plan["command"]
                and oracle["environment"]["variant_toolchains"][variant] == "1.70.0"
                and oracle["environment_replacements"][variant] == []
                and oracle["test_compatibility_replacements"][variant] == [], "original variant contract changed")
    prepared = json.loads(Path(ctx.config[OPTION][task["id"]]).read_text())
    require(prepared["schema_version"] == "reviewbench-cargo-contract108109-retained-inputs/v1"
            and set(prepared["cases"]) == set(TASKS)
            and prepared["retained_lock_pins_are_current_recovery_pins_not_original_oracle_fields"] is True,
            "unbound retained preparation or misrepresented historical lock digest")
    selected = prepared["cases"][task["id"]]
    require(referenced(selected["task"]) == task and referenced(selected["oracle"]) == oracle
            and referenced(selected["plan"]) == plan, "prepared original objects differ")
    original = referenced(selected["original_recipe"])
    require((original["task_id"], original["repo"], original["start_sha"]) == (task["id"], REPO, pin["start"])
            and original["cpus"] == 4 and original["env"]["CARGO_BUILD_JOBS"] == "4"
            and original["command_timeout_seconds"] == 3600 and original["submodules"] == [],
            "original migrated execution bounds changed")
    for asset in [original["image_audit"], *original["assets"], original["grading"]["golden_patch"]]:
        require(file_hash(Path(asset["path"])) == asset["sha256"], "retained original runtime asset changed")
    source = referenced(prepared["source_preparation"])["cases"][str(pin["index"])]
    retained = referenced(prepared["retained_lock_audit"])["cases"][str(pin["index"])]
    references = reference_trees(ctx, task, patch, {"Cargo.lock"})
    locks = {}
    for variant, lock_hash in (("start", pin["seed"]), ("golden", pin["retained_golden"])):
        row = source["variants"][variant]
        complete = Path(row["source"])
        require(entry.snapshot(complete) == expected_snapshot(row["files"]), "complete reference source changed")
        manifests, original_locks = references[variant]
        require(set(original_locks) == {"Cargo.lock"} and hashlib.sha256(original_locks["Cargo.lock"]).hexdigest() == pin["seed"]
                and file_hash(complete / "Cargo.lock") == pin["seed"], "source-only reference lock was replaced")
        require(manifests == {name: f["sha256"] for name, f in row["files"].items() if Path(name).name == "Cargo.toml"},
                "complete reference manifest tree changed")
        lock = retained["variants"][variant]["lock"]
        require(lock["sha256"] == lock_hash, "wrong retained lock variant")
        referenced(lock, decode=False)
        legacy = Path(retained["variants"][variant]["retained_snapshot"])
        expected = expected_snapshot(row["files"])
        expected["Cargo.lock"] = ["file", lock_hash, False]
        expected[HARNESS] = ["file", pin["harness"], False]
        require(entry.snapshot(legacy) == expected, "retained whole source/lock/harness has extra or changed files")
        runs = retained["variants"][variant]["logs"]
        require(len(runs) == 3, "three original successful run logs are required")
        for log in runs:
            text = referenced(log, decode=False).decode()
            require("test " + pin["expected"] + " ... ok" in text
                    and "1 passed; 0 failed; 0 ignored;" in text, "retained original CLI execution is incomplete")
        preflight = source["metadata_preflight"][variant]
        require(preflight["source_unchanged"] and preflight["selected_lock_sha256"] == lock_hash
                and preflight["error"] == "BeforeSUT: PREFLIGHT_STOP_BEFORE_ORIGINAL_SUT"
                and sorted(p["returncode"] for p in preflight["probes"]) == [0, 101],
                "full original Cargo1.70 graph is not uniquely bound")
        locks[variant] = {"path": lock["path"], "sha256": lock_hash}
    vendor_audit = referenced(prepared["shared_vendor_audit"])
    vendor = Path(vendor_audit["vendor"])
    package_locks = [tomllib.loads(referenced(row, decode=False).decode()) for row in vendor_audit["locks"]]
    expected_packages = rust_vendor.locked_packages(package_locks)
    require(len(expected_packages) == vendor_audit["registry_packages"] == 850
            and all(key[2] == rust_vendor.CRATES_IO for key in expected_packages)
            and vendor_audit["new_Cargo_resolution_commands"] == 0, "shared vendor was re-resolved")
    require({(r["name"], r["version"], r["source"]): r["package_checksum"] for r in vendor_audit["source_audit"]}
            == expected_packages, "shared vendor package identities differ")
    expected_files = set()
    for row in vendor_audit["source_audit"]:
        root = vendor / row["directory"]
        checks = json.loads((root / ".cargo-checksum.json").read_text())
        require(checks["package"] == row["package_checksum"] and len(checks["files"]) == row["files"],
                "vendor checksum metadata differs")
        for name, checksum in checks["files"].items():
            require(entry.file_digest(root / name) == checksum, "vendored bytes changed")
            expected_files.add((Path(row["directory"]) / name).as_posix())
        expected_files.add((Path(row["directory"]) / ".cargo-checksum.json").as_posix())
    require(set(tree_files(vendor)) == expected_files, "vendor source closure was narrowed")
    return pin, original, locks, vendor


def migrate(ctx, task, oracle):
    from . import rust_lane, rust_overlay
    plan = rust_lane.load_plan(ctx, oracle)
    patch = ctx.golden(task, oracle, "rust", ctx.task_directory(task, oracle, "rust"))
    pin, original, locks, vendor = original_inputs(ctx, task, oracle, plan, patch)
    registry = Path(ctx.config["rust_toolchains"])
    compiler = Path(json.loads(registry.read_text())["1.70.0"]["root"])
    require(file_hash(compiler / "bin/cargo") == CARGO_SHA and file_hash(compiler / "bin/rustc") == RUSTC_SHA,
            "original Rust1.70 compiler changed")
    after = deepcopy(original)
    after["grading"]["golden_patch"] = patch
    vendor_asset = ctx.bundle(vendor)
    after["assets"] = [
        {**vendor_asset, "kind": "archive", "visibility": "developer", "target": rust_vendor.TARGET}
        if asset["target"] == rust_vendor.TARGET else asset for asset in after["assets"]
    ]
    directory = ctx.output / ("cargo-contract" + str(pin["index"]))
    directory.mkdir()
    text, _ = rust_vendor.source_configuration([rust_vendor.CRATES_IO])
    (directory / "sources.toml").write_text(text)
    source_asset = ctx.copy_asset(directory / "sources.toml", "cargo-contract-config")
    after["assets"].append({**source_asset, "kind": "file", "visibility": "developer", "target": SOURCES})
    mounted_locks = []
    for index, lock in enumerate(sorted(locks.values(), key=lambda r: r["sha256"])):
        asset = ctx.copy_asset(Path(lock["path"]), "cargo-contract-environment-lock")
        require(asset["sha256"] == lock["sha256"], "copied retained lock changed")
        target = "/opt/reviewbench-assets/cargo-contract-lock-" + str(index)
        after["assets"].append({**asset, "kind": "file", "visibility": "developer", "target": target})
        mounted_locks.append({"path": target, "sha256": asset["sha256"]})
    public = {
        "schema_version": entry.WORKSPACE_REFERENCE_SCHEMA, "task_id": task["id"], "selection": entry.SELECTION,
        "workdir": ".", "lock_path": "Cargo.lock", "locks": mounted_locks,
        "reference_seed_sha256": pin["seed"], "reference_seed_policy": "replace-only-identical-committed-seed-in-private-mirror",
        "package": "cargo-contract", "package_manifest": MANIFEST,
        "public_command": ["check", "--locked", "-p", "cargo-contract"],
        "cargo": {"path": RUST + "/bin/cargo", "sha256": CARGO_SHA},
        "rustc": {"path": RUST + "/bin/rustc", "sha256": RUSTC_SHA},
        "source_config": {"path": SOURCES, "sha256": source_asset["sha256"]},
        "cpus": 4, "timeout_seconds": 3600, "metadata_timeout_seconds": 120, "max_log_bytes": 16 * 1024**2,
    }
    private = {
        "schema_version": entry.WORKSPACE_PRIVATE_SCHEMA, "task_id": task["id"], "original_unlocked": True,
        "package_manifest": MANIFEST, "harness": {"path": HARNESS, "sha256": pin["harness"]},
        "command": COMMAND[2:], "expected_tests": [pin["expected"]],
    }
    entry.validate(public, private)
    for name, value, target, visibility in (("public.json", public, SPEC, "developer"),
                                            ("private.json", private, PRIVATE, "grader")):
        atomic_json(directory / name, value)
        after["assets"].append({**ctx.copy_asset(directory / name, "cargo-contract-spec"), "kind": "file",
                                "target": target, "visibility": visibility})
    tool = "workspace_locks.py"
    tools = rust_overlay.build_tools(ctx, extra_entries={tool: Path(entry.__file__)})
    after["assets"].append({**tools, "kind": "archive", "visibility": "developer", "target": rust_overlay.TOOLS})
    after["cache_mounts"]["rust-build"] = rust_overlay.BUILD_CACHE
    prefix = ["python3", rust_overlay.TOOLS + "/" + tool, "--spec", SPEC, "--task-id", task["id"]]
    after["dev_checks"] = [shlex.join(prefix)]
    after["grading"]["commands"][0]["command"] = shlex.join([*prefix, "--private", PRIVATE])
    for name in ("env", "bootstrap", "scratch_paths", "submodules", "image", "image_audit", "cpus",
                 "memory_mb", "build_scratch_mb", "command_timeout_seconds", "pids_limit"):
        require(after[name] == original[name], "unrelated runtime field changed: " + name)
    for name in ("bootstrap", "immutable_paths", "multifile_build"):
        require(after["grading"][name] == original["grading"][name], "original grading input changed: " + name)
    recipe = RuntimeRecipe.from_dict(after)
    atomic_json(directory / "input-audit.json", {
        "task_id": task["id"], "original_command": COMMAND,
        "original_committed_seed": pin["seed"], "retained_original_golden_lock": pin["retained_golden"],
        "original_oracle_declares_golden_lock_digest": False,
        "explicit_original_golden_prepare": plan.get("golden_prepare_command"),
        "candidate_source_lock_harness_unchanged": True, "new_dependency_resolutions": 0,
        "runtime_fingerprint": recipe.fingerprint, "shared_vendor": vendor_asset,
    })
    return recipe
