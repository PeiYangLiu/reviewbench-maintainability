"""Original RECS compilers selected by public declarations, with explicit new lock provenance."""
import hashlib
import json
from pathlib import Path
import shlex
import tomllib

from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, atomic_json, canonical_hash, file_hash,
)
from . import rust_project_toolchain_entry as entry, rust_vendor
from .native_submodules import readonly_git
from .rust_lock_assets import reference_trees
from .rust_locked_workspace import referenced
from .rust_manifest_service import attest_vendor
from .rust_overlay import session_bytes

ID = "github__martinjonsson01__recs__pull_104__comment_1161283022__modularity"
OPTION = "rust_original_project_toolchains"
PROFILE = {
    "repo": "martinjonsson01/recs", "start": "865ef70070c5e4d27df26ea493dbcad6169dfc56",
    "plan": "ece7baa4df01830320d3f0c5f1632b29244617187b8d6d03f3af34ca9234b392",
    "golden": "71ca9226f3d0989d51f0834749ea929ae2e0e06df4e8b67e1980443d6d7ac814",
    "harness": "f0b7906c06375ef054a882a225b049bc3fb80f056bdb6316a8eb2b92cce06935",
    "locks": ["94057444edd6f0cc526c72a6a8f14544cf0828e68ce0fb1271452eaf58bdb72f",
              "c2fe79bb2de6e4daea68f8eb5237b73fdeff90e42a3f569b722fad77f1ff2971"],
    "index_commit": "16d7a2eec788c4b0785ccc66a535cd6520c46712",
    "index_tree": "3f1cba40c6cae128415448515c2743b24179e771",
    "compilers": {
        "1.68.2": {"cargo": "c5a319a8a89701dffdc98de38a438a2cb63066b5f8fe46ce1e824da0700f0198",
                   "rustc": "14dd17635036dced85899bef48f89b4c93c6a6203f0deeec34df7d533946cca7"},
        "nightly-2023-05-04": {"cargo": "c3294a433baec696211175d0a8b1c9acede0fe134d660ba9324636d5646c9706",
                              "rustc": "e1a5d787b49ecdbb28c3c5125bf49b11feb4071f47308247cd608383c0e3774a"},
    },
}
TEST = "zero_parameter_system_runs_once"
HARNESS = "crates/ecs/tests/oracle_session.rs"
MANIFEST = "crates/ecs/Cargo.toml"
SPEC = "/opt/reviewbench-assets/project-toolchains.json"
PRIVATE = "/opt/reviewbench-assets/project-toolchains-private.json"
SOURCES = "/opt/reviewbench-assets/recs-sources.toml"
TOOL = "/opt/reviewbench-assets/rust-build-tools/project_toolchain.py"


def require(value, message):
    if not value:
        raise ContractError("original project compilers: " + message)


def requested(ctx, task):
    values = getattr(ctx, "config", {}).get("migration", {}).get(OPTION, {})
    require(isinstance(values, dict) and set(values) <= {ID}, "unsupported original project")
    return task.get("id") in values


def original_inputs(ctx, task, oracle, plan, patch):
    require(task["id"] == oracle["id"] == ID and task["repo"] == oracle["repo"] == PROFILE["repo"]
            and task["start_sha"] == oracle["start_sha"] == PROFILE["start"], "task identity changed")
    require(canonical_hash(plan) == PROFILE["plan"] and plan["input_index"] == oracle["input_index"] == 62,
            "original plan changed")
    require(patch["sha256"] == oracle["golden_patch_sha256"] == PROFILE["golden"]
            and file_hash(Path(patch["path"])) == PROFILE["golden"], "original golden changed")
    prepared = referenced(ctx.config["migration"][OPTION][ID], document=True)
    require(prepared["schema_version"] == "reviewbench-recs62-explicit-resolution-inputs/v1"
            and prepared["task_id"] == ID and prepared["complete"]
            and prepared["original_final_golden_lock_recovered"] is False
            and prepared["new_SUT_model_judge_calls"] == 0, "new resolution was misclassified")
    for name, value in (("task", task), ("oracle", oracle), ("plan", plan)):
        require(referenced(prepared[name], document=True) == value, "prepared " + name + " changed")
    require(hashlib.sha256(referenced(prepared["golden"])).hexdigest() == PROFILE["golden"], "prepared golden changed")
    require(len(plan["session_files"]) == 1 and oracle["pass_to_pass_tests"] == [TEST], "original test inventory changed")
    harness = session_bytes(ctx, plan, oracle, plan["session_files"][0])
    require(harness == referenced(prepared["harness"]) and hashlib.sha256(harness).hexdigest() == PROFILE["harness"],
            "original assertion source changed")
    references = reference_trees(ctx, task, patch, {"Cargo.lock"})
    snapshots = referenced(prepared["source_preparation"], document=True)
    locks = []
    for number, (variant, version) in enumerate(zip(("start", "golden"), entry.VERSIONS)):
        require(oracle["environment"]["variant_toolchains"][variant] == version,
                "original compiler version changed")
        command = oracle["environment"]["variant_commands"][variant]
        require(shlex.split(command) == ["cargo", "+" + version, "test", "-p", "ecs",
                                        "--test", "oracle_session", "--", "--nocapture"],
                "original unlocked test arguments changed")
        manifests, seed = references[variant]
        require(hashlib.sha256(seed["Cargo.lock"]).hexdigest() == PROFILE["locks"][0],
                "original committed seed changed")
        require(manifests == {n: v[1] for n, v in snapshots[variant]["snapshot"].items()
                              if Path(n).name == "Cargo.toml"}, "complete source manifests changed")
        source = Path(snapshots[variant]["source"])
        require(entry.core.snapshot(source) == snapshots[variant]["snapshot"], "prepared original source changed")
        selected, declaration = entry.project_toolchain(source, entry.VERSIONS[0])
        require(selected == version, "original public compiler declaration differs from the original command")
        if variant == "start":
            require(declaration["path"] is None, "original start acquired a toolchain override")
        else:
            require(tomllib.loads((source / "rust-toolchain.toml").read_text()) ==
                    {"toolchain": {"channel": version}}, "original dated declaration changed")
        lock_bytes = referenced(prepared["locks"][variant])
        require(hashlib.sha256(lock_bytes).hexdigest() == PROFILE["locks"][number], "sealed lock changed")
        lock = tomllib.loads(lock_bytes.decode())
        locks.append(lock)
        require(len(lock["package"]) == (370 if variant == "start" else 375), "full reference graph changed")
        require(len(oracle["runs"][variant]) == 3, "six original observations missing")
        for run in oracle["runs"][variant]:
            path = ctx.legacy / "rust" / run["log"]
            require(run["ok"] and not run["failed_tests"] and run["passed_tests"] == [TEST]
                    and "test " + TEST + " ... ok" in path.read_text(), "original observation changed")
        for role in ("public", "private"):
            record = prepared["offline_observations"][role + "-" + variant]
            require(record["exit_status"] == 0 and record["source_unchanged"], "offline reference metadata failed")
            graph = referenced(record["stdout"], document=True)
            referenced(record["stderr"])
            require({(p["name"], p["version"], p.get("source")) for p in graph["packages"]} ==
                    {(p["name"], p["version"], p.get("source")) for p in lock["package"]}
                    and len(graph["resolve"]["nodes"]) == len(graph["packages"]),
                    "offline reference graph was narrowed")
        for name in ("cargo", "rustc"):
            row = prepared["original_compilers"][version][name]
            require(row["sha256"] == PROFILE["compilers"][version][name] == file_hash(Path(row["path"])),
                    "original compiler binary changed")
    index = referenced(prepared["fixed_full_index"], document=True)
    require(index["commit"] == PROFILE["index_commit"] and index["tree"] == PROFILE["index_tree"]
            and index["full_index_not_filtered"] is True and index["original_final_lock_recovered"] is False,
            "full frozen index provenance changed")
    repository = Path(index["source"])
    require(readonly_git(repository, "rev-parse", index["commit"] + "^{tree}").decode().strip() == index["tree"],
            "retained index object changed")
    transport = referenced(prepared["transport_record"], document=True)
    require(transport["full_original_index"] == index and transport["transport_only_config_dl_rewrite"],
            "index transport changed")
    for name, sha in transport["index_files"].items():
        require(hashlib.sha256(readonly_git(repository, "show", index["commit"] + ":" + name)).hexdigest() == sha,
                "served index file differs from the complete frozen index")
    intent = referenced(prepared["first_new_resolution"], document=True)
    require(intent["variant"] == "golden" and intent["toolchain"] == entry.VERSIONS[1]
            and intent["committed_seed_sha256"] == PROFILE["locks"][0]
            and intent["source_snapshot"] == snapshots["golden"]["snapshot"]
            and intent["index"] == index and intent["max_resolution_attempts"] == 1
            and intent["private_tests_used_for_selection"] is False and intent["original_final_lock_recovered"] is False,
            "first-new environment admission changed")
    records = referenced(prepared["resolution_observations"], document=True)
    initial, resolved = records["golden-original-seed-locked"], records["golden-first-new-metadata"]
    require(initial["exit_status"] == 101 and resolved["exit_status"] == 0
            and resolved["changed_paths"] == ["Cargo.lock"]
            and "--locked" in initial["command"] and "--locked" not in resolved["command"],
            "original missing resolution or first new command changed")
    expected = {**snapshots["golden"]["snapshot"], HARNESS: ["file", PROFILE["harness"], False]}
    require(resolved["source_before"] == expected and resolved["source_after"] ==
            {**expected, "Cargo.lock": ["file", PROFILE["locks"][1], False]},
            "new resolution changed original source/assertions")
    attest_vendor(prepared, locks)
    return prepared, harness


def migrate(ctx, task, oracle):
    from . import rust_lane, rust_overlay
    plan = rust_lane.load_plan(ctx, oracle)
    patch = ctx.golden(task, oracle, "rust", ctx.task_directory(task, oracle, "rust"))
    prepared, harness = original_inputs(ctx, task, oracle, plan, patch)
    registry = json.loads(Path(ctx.config["rust_toolchains"]).read_text())
    image, image_audit = ctx.image(rust_lane.IMAGE)
    assets, tools = [], {}

    def asset(value, target, role="developer", kind="file"):
        assets.append({**value, "target": target, "visibility": role, "kind": kind})
        return {"path": target, "sha256": value["sha256"]}

    for version in entry.VERSIONS:
        root = Path(registry[version]["root"])
        require(all(file_hash(root / ("bin/" + n)) == PROFILE["compilers"][version][n] for n in ("cargo", "rustc")),
                "compiler registry changed")
        target = "/opt/reviewbench-assets/rust-" + version
        asset(rust_lane.compiler(ctx, version), target, kind="archive")
        tools[version] = {n: {"path": target + "/bin/" + n, "sha256": PROFILE["compilers"][version][n]}
                          for n in ("cargo", "rustc")}
    asset(ctx.bundle(Path(prepared["vendor_root"])), rust_vendor.TARGET, kind="archive")
    sources = asset(ctx.copy_asset(Path(prepared["source_config"]["path"]), "project-rust-sources"), SOURCES)
    locks = [asset(ctx.copy_asset(Path(prepared["locks"][v]["path"]), "project-rust-lock"),
                   "/opt/reviewbench-assets/project-rust-lock-" + str(i)) for i, v in enumerate(("start", "golden"))]
    base = {
        "schema_version": entry.core.WORKSPACE_REFERENCE_SCHEMA, "task_id": ID, "selection": entry.core.SELECTION,
        "workdir": ".", "lock_path": "Cargo.lock", "locks": locks, "reference_seed_sha256": PROFILE["locks"][0],
        "reference_seed_policy": "replace-only-identical-committed-seed-in-private-mirror",
        "package": "ecs", "package_manifest": MANIFEST, "public_command": ["check", "--locked", "-p", "ecs"],
        "source_config": sources, "cpus": 4, "timeout_seconds": 1800, "metadata_timeout_seconds": 120,
        "max_log_bytes": 16 * 1024**2,
    }
    public = {"schema_version": entry.SCHEMA, "task_id": ID, "default_toolchain": entry.VERSIONS[0],
              "toolchains": tools, "cargo_contract": base}
    private = {"schema_version": entry.core.WORKSPACE_PRIVATE_SCHEMA, "task_id": ID, "original_unlocked": True,
               "package_manifest": MANIFEST, "harness": {"path": HARNESS, "sha256": PROFILE["harness"]},
               "command": ["test", "-p", "ecs", "--test", "oracle_session", "--", "--nocapture"],
               "expected_tests": [TEST]}
    for version in entry.VERSIONS:
        entry.core.validate({**base, **tools[version]}, private)
    for name, data, target, role in (("public", public, SPEC, "developer"), ("private", private, PRIVATE, "grader")):
        path = ctx.output / ("project-toolchain-" + name + ".json")
        atomic_json(path, data)
        asset(ctx.copy_asset(path, "project-toolchain-spec"), target, role)
    package = rust_overlay.build_tools(ctx, extra_entries={
        "project_toolchain.py": Path(entry.__file__),
        "image_locks.py": Path(__file__).with_name("rust_image_entry.py")})
    asset(package, rust_overlay.TOOLS, kind="archive")
    forward_root = ctx.output / "cargo-project-bin"
    forward_root.mkdir(exist_ok=True)
    forward = forward_root / "cargo"
    forward.write_text("#!/usr/bin/env sh\nexec " + shlex.join(
        ["python3", TOOL, "--spec", SPEC, "--task-id", ID, "--forward"]) + ' "$@"\n')
    forward.chmod(0o755)
    asset(ctx.bundle(forward_root), "/opt/reviewbench-assets/project-bin", kind="archive")
    test = asset(ctx.copy_asset(Path(prepared["harness"]["path"]), "project-original-test"),
                 "/opt/reviewbench-assets/project-original-test.rs", "grader")
    prefix = ["python3", TOOL, "--spec", SPEC, "--task-id", ID]
    destination = "/workspace/" + HARNESS
    recipe = RuntimeRecipe(
        task_id=ID, repo=task["repo"], start_sha=task["start_sha"], lane="rust",
        image=image, image_audit=image_audit, workdir="/workspace", cpus=4, memory_mb=8192, build_scratch_mb=12288,
        command_timeout_seconds=1800,
        env={"PATH": "/opt/reviewbench-assets/project-bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
             "CARGO_HOME": "/opt/reviewbench-cache/cargo", "CARGO_TARGET_DIR": "/opt/reviewbench-cache/target",
             "CARGO_NET_OFFLINE": "true", "CARGO_NET_RETRY": "0", "RUSTUP_AUTO_INSTALL": "0",
             "CARGO_INCREMENTAL": "0", "CARGO_BUILD_JOBS": "4", "CARGO_TERM_COLOR": "never",
             "CARGO_PROFILE_DEV_DEBUG": "0", "CARGO_PROFILE_TEST_DEBUG": "0", "RUSTFLAGS": "--cap-lints=allow",
             "RUST_BACKTRACE": "1", "HOME": "/tmp", "TMPDIR": "/workspace/.reviewbench/tmp"},
        cache_mounts={"cargo": "/opt/reviewbench-cache/cargo", "target": "/opt/reviewbench-cache/target",
                      "rust-build": rust_overlay.BUILD_CACHE},
        bootstrap=["mkdir -p /workspace/.reviewbench/tmp && cp " + SOURCES + " /opt/reviewbench-cache/cargo/config.toml"],
        dev_checks=[shlex.join(prefix)], assets=assets, submodules=ctx.modules(task), scratch_paths=[],
        grading={"multifile_build": True, "immutable_paths": ctx.immutable_tests(task), "golden_patch": patch,
                 "bootstrap": ["mkdir -p " + shlex.quote(str(Path(destination).parent)) + " && cp "
                               + shlex.quote(test["path"]) + " " + shlex.quote(destination)],
                 "commands": [{"command": shlex.join([*prefix, "--private", PRIVATE]),
                               "parser": "rust-test", "expected_tests": [TEST]}]},
    )
    recipe.validate()
    return recipe
