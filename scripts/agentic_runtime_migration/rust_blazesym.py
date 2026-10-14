"""The original Blazesym test with a complete frozen, indexed local registry."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import tomllib

import tomli_w

from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, atomic_json, canonical_hash, file_hash, repo_path,
)
from reviewbench_maintainability.agentic.snapshots import tree_files
from .rust_first_resolution import referenced, reference_packages, graph_summary
from .rust_lock_assets import BINDINGS, attested_bytes, reference_trees

TASK = "github__libbpf__blazesym__pull_70__comment_1134655780__complexity"
PROFILE = "blazesym56-local-registry-native-v1"
OPTION = "rust_blazesym_local_registry"
SCHEMA = "reviewbench-blazesym-local-registry-inputs/v1"
REGISTRY_SOURCE = "registry+https://github.com/rust-lang/crates.io-index"
LOCK_SHA = "07d2557d972b952f7828d46ff6e374741b90418dff392e3d13f0cacca83b032a"
GOLDEN_SHA = "6fdee7e0a1265a6b257b452233890622678ccc6f3305cdcdbcb81710f8ebcfb1"
HARNESS = "tests/blazesym.rs"
HARNESS_SHA = "e62189d11fda38a6fcbd04f995fe18d36a67746858d5edb3cab13fcdd08db658"
TESTS = ["symbolize_dwarf", "symbolize_gsym"]
COMMAND = ["test", "--test", "blazesym", "--", "--nocapture"]
MANIFESTS = {
    "start": {"Cargo.toml": "40bb1055ae061d9e0e7d4631c037a422f7e8d23cff1efe13b4a42f4ceef5f40f"},
    "golden": {"Cargo.toml": "b048c5fbdf17d862a9dd7f74fbd06deffac6b31f1d6e3230b11f479bb990c425"},
}
FIXTURES = {
    "data/test.c": "4675050e64b9a0ce3fb1c68070d4ef05dfe7fd9568c523746f618a2de47ea2cd",
    "data/test-stable-addresses.c": "d89f28e1b29056b145f9997603ae373b4c70ca0efdb88081a8abe60ef9d10730",
    "data/test-stable-addresses.ld": "62683fcc1a8f0aefeba8faa85491bb96319db63b07133aba3de3aa462d4b6bab",
}
GENERATED = [
    "data/test-no-debug.bin", "data/test-dwarf-v4.bin", "data/test-stable-addresses.bin",
    "data/test.gsym", "data/test-dwarf.bin",
]
TOOLS = "/opt/reviewbench-assets/blazesym-tools"
SPEC = "/opt/reviewbench-assets/blazesym.json"
PRIVATE = "/opt/reviewbench-assets/blazesym-private.json"
REGISTRY = "/opt/reviewbench-assets/vendor"
REGISTRY_MANIFEST = "/opt/reviewbench-assets/blazesym-registry-manifest.json"
SOURCE_CONFIG = "/opt/reviewbench-assets/cargo-sources.toml"
DEFAULT_LOCK = "/opt/reviewbench-assets/rust-new-lock-0"


def require(value, message):
    if not value:
        raise ContractError("Blazesym fixed local registry: " + message)


def requested(ctx, task):
    configured = getattr(ctx, "config", {}).get("migration", {}).get(OPTION, {})
    require(isinstance(configured, dict) and set(configured) <= {TASK}, "unknown opt-in task")
    return task.get("id") == TASK and TASK in configured


def metadata_stderr(data, graph, registry):
    allowed = {
        f"   Unpacking {row['name']} v{row['version']} (registry `{registry}`)"
        for row in graph["packages"] if row.get("source")
    }
    lines = data.decode().splitlines()
    return len(lines) == len(set(lines)) and all(line in allowed for line in lines)


def prepared_inputs(reference):
    envelope = referenced(reference, json_data=True)
    require(envelope["schema_version"] == SCHEMA and envelope["task_id"] == TASK,
            "wrong preparation envelope")
    projection = referenced(envelope["projection"], json_data=True)
    decision = referenced(envelope["lock_decision"], json_data=True)
    transport = referenced(envelope["transport_decision"], json_data=True)
    require(projection["task_id"] == decision["task_id"] == TASK
            and projection["plan_sha256"] == decision["original_plan_sha256"] == BINDINGS[TASK]["plan_sha256"],
            "original plan/task binding changed")
    require(decision["reference_prepare_successes_max_per_variant"] == 1
            and decision["first_lock_command"] == ["cargo", "+1.68.2", "generate-lockfile"]
            and decision["historical_lock_or_native_restoration"] is False
            and decision["test_outcome_based_selection"] is False,
            "first-new resolution policy changed")
    require(transport["lock_sha256"] == LOCK_SHA
            and transport["version_selection_or_lock_changes"] is False,
            "transport changes the frozen lock")
    referenced(envelope["transport_producer"])
    container = referenced(envelope["transport_container"], json_data=True)
    require(container["run"]["exit_status"] == 0 and "--network=none" in container["run"]["argv"],
            "transport observations did not complete offline")
    locks, parsed, audits = {}, None, {}
    for variant in ("start", "golden"):
        receipt = referenced(envelope["lock_receipts"][variant], json_data=True)
        generate = referenced(receipt["generate_record"], json_data=True)
        data = referenced(receipt["lock"])
        require(hashlib.sha256(data).hexdigest() == LOCK_SHA and receipt["generate_invocations"] == 1
                and receipt["generate_exit_status"] == generate["exit_status"] == 0
                and generate["natural_exit"] is True and generate["stop_reason"] is None
                and generate["lock_before_sha256"] is None and generate["lock_after_sha256"] == LOCK_SHA
                and generate["requested_argv"] == ["cargo", "+1.68.2", "generate-lockfile"],
                "first successful original-tool lock changed")
        require(receipt["decision"]["sha256"] == envelope["lock_decision"]["sha256"]
                and Path(generate["argv"][0]).name == "cargo"
                and generate["argv"][1:] == ["+1.68.2", "generate-lockfile"],
                "first resolution lost its actual command or prior decision")
        require(datetime.fromisoformat(decision["recorded_at"]) < datetime.fromisoformat(generate["started_at"])
                <= datetime.fromisoformat(generate["completed_at"])
                <= datetime.fromisoformat(receipt["recorded_at"])
                < datetime.fromisoformat(transport["recorded_at"]),
                "lock was not frozen before transport observation")
        for stream in ("stdout", "stderr"):
            referenced(generate[stream])
        lock = reference_packages(data)
        require(len(lock["package"]) == 56 and (parsed is None or parsed == lock),
                "reference locks do not share all56 exact packages")
        parsed = lock
        locks[variant] = receipt["lock"]
        source = Path(envelope["sources"][variant])
        actual = tree_files(source)
        original = projection["sources"][variant]["files"]
        require(set(actual) == set(original) | {"Cargo.lock"} and len(original) == 34,
                "complete original source was pruned or modified")
        for name, row in original.items():
            require(not actual[name].is_symlink() and file_hash(actual[name]) == row["sha256"]
                    and actual[name].stat().st_mode & 0o777 == row["mode"],
                    "original source bytes/modes changed")
        require(file_hash(source / "Cargo.lock") == LOCK_SHA
                and all(path not in original for path in GENERATED), "invalid derived output boundary")
        graphs = {}
        for mode, version in (("default", "1.68.2"), ("all", "1.93.0")):
            record = referenced(envelope["metadata"][variant][mode], json_data=True)
            expected = [envelope["tools"][version]["cargo"]["path"], "--offline",
                        "--config", 'source.crates-io.replace-with="reviewbench-locked-local"',
                        "--config", 'source.reviewbench-locked-local.local-registry="' + envelope["registry_root"] + '"',
                        "metadata", "--format-version", "1", "--locked"]
            if mode == "all":
                expected.append("--all-features")
            require(record["argv"] == expected and record["exit_status"] == 0
                    and record["natural_exit"] is True and record["stop_reason"] is None
                    and record["cwd"] == str(source)
                    and record["environment"]["CARGO_NET_OFFLINE"] == "true"
                    and record["environment"]["RUSTC"] == envelope["tools"][version]["rustc"]["path"],
                    "wrong metadata tool/features/transport/outcome")
            graph = json.loads(referenced(record["stdout"]))
            require(metadata_stderr(referenced(record["stderr"]), graph, envelope["registry_root"]),
                    "unaccounted metadata diagnostic")
            subset = {(p["name"], p["version"], p.get("source")) for p in graph["packages"]}
            selected_lock = (lock if mode == "all" else {
                **lock, "package": [p for p in lock["package"]
                                   if (p["name"], p["version"], p.get("source")) in subset]})
            summary = graph_summary(graph, selected_lock, source, MANIFESTS[variant], local_packages=1)
            require(summary["packages"] == (56 if mode == "all" else 19),
                    "reference metadata closure changed")
            if mode == "default":
                root = next(n for n in graph["resolve"]["nodes"] if n["id"] == graph["resolve"]["root"])
                require(root["features"] == ["generate-test-files"],
                        "original default self-dev fixture feature changed")
            graphs[mode] = summary
        audits[variant] = graphs
    native = referenced(envelope["native"], json_data=True)
    require(native["source_projection"]["sha256"] == envelope["projection"]["sha256"],
            "native tools refer to another source projection")
    for version, tools in envelope["tools"].items():
        require(version in {"1.68.2", "1.93.0"}, "unknown compiler")
        for tool in tools.values():
            referenced(tool, limit=128 * 1024**2)
    manifest = referenced(envelope["registry_manifest"], json_data=True)
    runtime_manifest = referenced(envelope["runtime_registry_manifest"], json_data=True)
    registry = Path(envelope["registry_root"])
    files = tree_files(registry)
    require(set(files) == set(manifest["files"]) and runtime_manifest["files"] == manifest["files"],
            "local registry omits/adds source files")
    require(runtime_manifest == {
        "schema_version": "reviewbench-locked-local-registry/v1", "source": REGISTRY_SOURCE,
        "files": manifest["files"],
        "packages": [{key: row[key] for key in (
            "name", "version", "cksum", "index_path", "original_line_sha256")}
            for row in manifest["packages"]],
    }, "runtime registry manifest differs from its complete original transport")
    for name, row in manifest["files"].items():
        require(not files[name].is_symlink() and file_hash(files[name]) == row["sha256"]
                and files[name].stat().st_size == row["bytes"]
                and files[name].stat().st_mode & 0o777 == row["mode"], "local registry file changed")
    expected_packages = {(p["name"], p["version"]): p["checksum"]
                         for p in parsed["package"] if p.get("source")}
    require(len(expected_packages) == len(manifest["packages"]) == 55,
            "full locked registry closure changed")
    seen = set()
    for row in manifest["packages"]:
        key = row["name"], row["version"]
        require(key not in seen and expected_packages.get(key) == row["cksum"],
                "registry index has an unbound package")
        seen.add(key)
        archive = registry / (row["name"] + "-" + row["version"] + ".crate")
        require(file_hash(archive) == row["cksum"], "crate differs from frozen checksum")
        line_hash = row["original_line_sha256"]
        original_lines = referenced(row["original_index_file"]).splitlines(keepends=True)
        local_lines = (registry / "index" / repo_path(row["index_path"])).read_bytes().splitlines(keepends=True)
        selected = [line for line in local_lines if json.loads(line)["vers"] == row["version"]]
        require(len(selected) == 1 and selected[0] in original_lines
                and hashlib.sha256(selected[0]).hexdigest() == line_hash,
                "local registry index rewrites original dependency metadata")
    require(seen == set(expected_packages), "missing complete locked registry identity")
    indexed = [json.loads(line) for name, path in files.items() if name.startswith("index/")
               for line in path.read_bytes().splitlines()]
    require(len(indexed) == len(expected_packages) and {
        (row["name"], row["vers"]): row["cksum"] for row in indexed
    } == expected_packages, "registry index contains extra or missing locked versions")
    # The failed old full-feature query stays an explicit different observation.
    failed = referenced(envelope["original_failed_full_168_metadata"], json_data=True)
    require(failed["exit_status"] == 101, "lost original old-Cargo full-feature failure")
    referenced(envelope["old_cache_failed_modern_observation"], json_data=True)
    return {"envelope": envelope, "projection": projection, "locks": locks, "lock": parsed,
            "registry": registry, "runtime_manifest": runtime_manifest,
            "native": native, "graphs": audits, "receipt_sha256": reference["sha256"]}


def original_inputs(ctx, task, oracle, patch, checked):
    from .rust_lane import load_plan
    plan = load_plan(ctx, oracle)
    pin = BINDINGS[TASK]
    projection = checked["projection"]
    require(task["id"] == oracle["id"] == TASK
            and all(task.get(k) == oracle.get(k) == pin[k] for k in ("repo", "start_sha"))
            and canonical_hash(plan) == pin["plan_sha256"], "original task/plan changed")
    original = json.loads(attested_bytes(ctx.legacy / "rust", pin["artifacts"] + "/result.json",
                                         pin["oracle_file_sha256"]))
    require({k: v for k, v in oracle.items() if k != "lane_results_source"} == original,
            "original oracle changed")
    require(patch["sha256"] == GOLDEN_SHA == file_hash(Path(patch["path"]))
            and shlex.split(plan["command"]) == ["cargo", "+1.68.2", *COMMAND]
            and not plan.get("prepare_command") and not plan.get("session_files")
            and plan["timeout_seconds"] == 1800 and plan["build_jobs"] == 4
            and oracle["pass_to_pass_tests"] == TESTS,
            "original command/test/prepare/resource boundary changed")
    trees = reference_trees(ctx, task, patch, {"Cargo.lock"})
    for variant in ("start", "golden"):
        require(trees[variant][0] == MANIFESTS[variant] and trees[variant][1]["Cargo.lock"] is None,
                "original full manifests or missing locks changed")
        source = Path(projection["sources"][variant]["path"])
        require(file_hash(source / HARNESS) == HARNESS_SHA
                and all(file_hash(source / name) == digest for name, digest in FIXTURES.items()),
                "original tests or native fixture inputs changed")
        runs = original["runs"][variant]
        require(len(runs) == 3, "original six runs missing")
        for index, run in enumerate(runs, 1):
            log = next(r for r in projection["prior_six_log_proofs_reused"]
                       if r["variant"] == variant and r["run"] == index)
            require(run["passed_tests"] == TESTS and not run["failed_tests"]
                    and all(a["exit_code"] == 0 and not a["timed_out"] for a in run["attempts"]),
                    "original six-pass oracle changed")
            referenced(log["original"])
    return plan


def specifications(checked):
    """Public transport/identity data is separate from the fixed grading inputs."""
    envelope = checked["envelope"]
    config = tomli_w.dumps({
        "source": {
            "crates-io": {"replace-with": "reviewbench-locked-local"},
            "reviewbench-locked-local": {"local-registry": REGISTRY},
        },
        "net": {"offline": True},
    })
    tools = envelope["tools"]
    runtime = {
        "schema_version": "reviewbench-blazesym-locked-local-registry/v1",
        "task_id": TASK, "profile": PROFILE, "toolchain": "1.68.2", "metadata_toolchain": "1.93.0",
        "cargo_sha256": tools["1.68.2"]["cargo"]["sha256"],
        "rustc_sha256": tools["1.68.2"]["rustc"]["sha256"],
        "metadata_cargo_sha256": tools["1.93.0"]["cargo"]["sha256"],
        "metadata_rustc_sha256": tools["1.93.0"]["rustc"]["sha256"],
        "cpus": 4, "max_log_bytes": 32 * 1024**2, "metadata_timeout_seconds": 120,
        "command_timeout_seconds": 1800, "receipt_sha256": checked["receipt_sha256"],
        "default_lock": {"asset_target": DEFAULT_LOCK, "sha256": LOCK_SHA},
        "registry_manifest_sha256": envelope["runtime_registry_manifest"]["sha256"],
        "source_config_sha256": hashlib.sha256(config.encode()).hexdigest(),
        "native_tools": {
            name: {"path": row["requested_path"], "sha256": row["sha256"]}
            for name, row in checked["native"]["tools"].items()
        },
    }
    private = {
        "schema_version": "reviewbench-blazesym-fixed-oracle/v1", "task_id": TASK,
        "command": COMMAND, "harness": {"path": HARNESS, "sha256": HARNESS_SHA},
        "expected_tests": TESTS, "fixtures": FIXTURES,
    }
    return runtime, private, config


def migrate(ctx, task, oracle):
    from . import rust_blazesym_entry as entry
    from .rust_lane import compiler, IMAGE
    from .rust_overlay import build_tools

    require(requested(ctx, task), "explicit preparation reference is required")
    checked = prepared_inputs(ctx.config["migration"][OPTION][TASK])
    directory = ctx.task_directory(task, oracle, "rust")
    patch = ctx.golden(task, oracle, "rust", directory)
    plan = original_inputs(ctx, task, oracle, patch, checked)
    runtime, private, source_config = specifications(checked)
    entry.validate(runtime, private)
    require(checked["native"]["image_id"] == IMAGE, "native tools belong to another image")
    old_tool, metadata_tool = compiler(ctx, "1.68.2"), compiler(ctx, "1.93.0")
    registry = ctx.bundle(checked["registry"])
    image, audit = ctx.image(IMAGE)
    # Reuse the existing TOML bridge and logging/guard modules, unchanged.
    build_tools(ctx)
    tools = ctx.output / "blazesym-tools"
    shutil.copytree(ctx.output / "rust-build-tools", tools)
    for name in ("rust_first_resolution_entry.py", "rust_test_compat_entry.py",
                 "rust_api_entry.py", "rust_blazesym_entry.py"):
        shutil.copyfile(Path(__file__).with_name(name), tools / name)
    runtime_path = ctx.output / "blazesym-spec.json"
    private_path = ctx.output / "blazesym-private.json"
    config_path = ctx.output / "blazesym-cargo-sources.toml"
    atomic_json(runtime_path, runtime)
    atomic_json(private_path, private)
    config_path.write_text(source_config)
    require(file_hash(config_path) == runtime["source_config_sha256"], "source configuration changed")
    assets = [
        {**old_tool, "kind": "archive", "visibility": "developer", "target": "/opt/reviewbench-assets/rust"},
        {**metadata_tool, "kind": "archive", "visibility": "developer",
         "target": "/opt/reviewbench-assets/rust-metadata"},
        {**registry, "kind": "archive", "visibility": "developer", "target": REGISTRY},
        {**ctx.bundle(tools, role="toolchain"), "kind": "archive", "visibility": "developer", "target": TOOLS},
    ]
    for path, role, visibility, target in (
        (Path(checked["locks"]["start"]["path"]), "blazesym-first-lock", "developer", DEFAULT_LOCK),
        (Path(checked["envelope"]["runtime_registry_manifest"]["path"]),
         "blazesym-registry-manifest", "developer", REGISTRY_MANIFEST),
        (runtime_path, "blazesym-spec", "developer", SPEC),
        (private_path, "blazesym-private", "grader", PRIVATE),
        (config_path, "blazesym-cargo-config", "developer", SOURCE_CONFIG),
    ):
        assets.append({**ctx.copy_asset(path, role), "kind": "file", "visibility": visibility, "target": target})

    def wrap(command, private=False):
        prefix = ["python3", TOOLS + "/rust_blazesym_entry.py", "--source", "/workspace",
                  "--build-root", "/opt/reviewbench-cache/rust-build", "--spec", SPEC, "--cwd", "."]
        if private:
            prefix += ["--private-spec", PRIVATE]
        return shlex.join([*prefix, "--", "cargo", "--offline", "--config", SOURCE_CONFIG, *command])

    environment = {
        "PATH": "/opt/reviewbench-assets/rust/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "RUSTC": "/opt/reviewbench-assets/rust/bin/rustc",
        "CARGO_HOME": "/opt/reviewbench-cache/cargo",
        "CARGO_TARGET_DIR": "/opt/reviewbench-cache/target",
        "CARGO_BUILD_JOBS": "4", "CARGO_TERM_COLOR": "never", "CARGO_INCREMENTAL": "0",
        "CARGO_NET_OFFLINE": "true", "CARGO_PROFILE_DEV_DEBUG": "0", "CARGO_PROFILE_TEST_DEBUG": "0",
        "RUSTFLAGS": plan.get("rustflags", "--cap-lints=allow"), "RUST_BACKTRACE": "1",
        "TMPDIR": "/workspace/.reviewbench/tmp", "LLVM_GSYMUTIL": "/usr/bin/llvm-gsymutil-14",
        "RUSTUP_AUTO_INSTALL": "0",
    }
    recipe = RuntimeRecipe(
        task_id=TASK, repo=task["repo"], start_sha=task["start_sha"], lane="rust",
        image=image, image_audit=audit, workdir="/workspace",
        cpus=4, memory_mb=8192, build_scratch_mb=24576, command_timeout_seconds=1800,
        env=environment, cache_mounts={
            "cargo": "/opt/reviewbench-cache/cargo", "cargo-metadata": "/opt/reviewbench-cache/cargo-metadata",
            "target": "/opt/reviewbench-cache/target", "rust-build": "/opt/reviewbench-cache/rust-build",
        },
        bootstrap=["mkdir -p /workspace/.reviewbench/tmp"], dev_checks=[wrap(["check"])],
        scratch_paths=list(GENERATED), assets=assets, submodules=ctx.modules(task),
        grading={
            "multifile_build": True,
            "immutable_paths": sorted(set(ctx.immutable_tests(task)) | {HARNESS, *FIXTURES}),
            "golden_patch": patch, "bootstrap": [],
            "commands": [{"command": wrap(COMMAND, private=True), "parser": "rust-test", "expected_tests": TESTS}],
        },
    )
    recipe.validate()
    atomic_json(ctx.output / "blazesym-admission.json", {
        "task_id": TASK, "historical_restoration": False, "first_lock_sha256": LOCK_SHA,
        "metadata_graphs": checked["graphs"], "complete_registry_packages": 55,
        "original_SUT_toolchain": "1.68.2", "observational_full_metadata_toolchain": "1.93.0",
        "new_generate_update_download_model_calls": 0, "source_or_fixture_rewrites": [],
        "derived_scratch_paths": GENERATED, "native_tools": runtime["native_tools"],
    })
    return recipe
