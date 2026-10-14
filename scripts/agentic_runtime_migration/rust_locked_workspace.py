"""Original locked workspace variants, selected only by complete Cargo metadata."""
import hashlib
import json
from pathlib import Path
import shlex
import tempfile
import tomllib

from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, atomic_json, canonical_hash, file_hash,
)
from reviewbench_maintainability.agentic.snapshots import tree_files, start_archive, extract_tar
from . import rust_image_entry as entry, rust_vendor
from .rust_build_entry import relative_path
from .rust_lock_assets import reference_trees
from .rust_overlay import session_bytes

OPTION = "rust_original_locked_workspaces"
SPEC = "/opt/reviewbench-assets/locked-workspace.json"
PRIVATE = "/opt/reviewbench-assets/locked-workspace-private.json"
SOURCES = "/opt/reviewbench-assets/locked-workspace-sources.toml"
TOOL = "/opt/reviewbench-assets/rust-build-tools/locked_workspace.py"
PROFILES = {
    "github__fedimint__fedimint__pull_235__comment_917377808__testability": {
        "index": 36, "repo": "fedimint/fedimint",
        "start": "1ab604d027aefa81d386351f48db43181ea47bfa",
        "plan": "41225be63fd27a05af369a461057c7d993239b8c8f180145efcff696e7da7659",
        "golden": "aaf2f9331c91280a609a4e67857c09bbd7de585e168eaaa5be7acab26a6f7fc9",
        "locks": ["a964480dc8f0418910a3284ee1b2f5ef3622aa0c14198385965446304ef9fb7f",
                  "1bc67a05da545e695fce345db147c2709b33acd3f64835bc75615e4bb95ed9e6"],
        "toolchain": "1.64.0", "manifest": "client/client-lib/Cargo.toml", "package": "mint-client",
    },
    "github__informalsystems__hermes__pull_1989__comment_880826714__complexity": {
        "index": 50, "repo": "informalsystems/hermes",
        "start": "80be0a88db70d4e2c540722fd848d7016c0e6020",
        "plan": "b38c27a6f459d96e35c92bf0a6970050e65156de77c14cfad57ff55fe72ad571",
        "golden": "2f68542fffd3637b04f485c435ac3247d0b73e80ea20924ae57240ecbb22eda0",
        "locks": ["602b54e5b090d6fe54f626977cb627794f0334f58401d8c879c5b73d751bcc8d",
                  "945ebde6cf2cc0858aad0f823449c64c963b33517a3d94ccf591acb514cefec0"],
        "toolchain": "1.60.0", "manifest": "modules/Cargo.toml", "package": "ibc",
    },
    "github__getsentry__relay__pull_2862__comment_1440311244__modularity": {
        "index": 40, "repo": "getsentry/relay",
        "start": "6aa7f5c27c69cb2857a5451de4c343aed38b4a83",
        "plan": "ffa1b373a2740f10679cc4ca10f63cc4f4ef962ba293e32ffce409f6d3568790",
        "golden": "c822eb7f8af63fd18d29b5f433df51efc540cbc91dc8d91049b44c41c8fb0000",
        "locks": ["3a13c663af205c46f39629266d28bd64aa842428c87f9f6c2efe4108ab60f6ff",
                  "689a11e22bb87f2700f6ccba9d89bc069155e472d7f48230792823cbfb844cd6"],
        "toolchain": "1.75.0", "manifest": "relay/Cargo.toml", "package": "relay",
        "environment": {"RUST_BACKTRACE": "1", "RELAY_RELEASE": "reviewbench-oracle"},
        "production_test_named_paths": ["relay-server/src/actors/test_store.rs"],
        "resource": {
            "path": "relay-ua/uap-core/regexes.yaml", "source": "session/040/uap-regexes.yaml",
            "sha256": "23ec70f1e68276c230fdd92f14944d90a3fdc80729e44ae788cd055abbdc583a",
            "module": "relay-ua/uap-core", "commit": "bbd43aed9a623486191a33c3af9e463e89c85f7a",
            "relative": "regexes.yaml",
        },
    },
}


def require(value, message):
    if not value:
        raise ContractError("original locked workspace: " + message)


def referenced(row, *, document=False):
    path = Path(row["path"])
    require(path.is_absolute() and path.is_file() and not path.is_symlink(), "missing regular evidence")
    require(path.stat().st_size <= 32 * 1024**2 and file_hash(path) == row["sha256"], "evidence hash changed")
    data = path.read_bytes()
    require(hashlib.sha256(data).hexdigest() == row["sha256"], "evidence changed during read")
    return json.loads(data) if document else data


def requested(ctx, task):
    options = getattr(ctx, "config", {}).get("migration", {}).get(OPTION, {})
    require(isinstance(options, dict) and set(options) <= set(PROFILES), "unsupported opt-in profile")
    return task.get("id") in options


def resource_module(ctx, task, plan, oracle, prepared, profile):
    """Bind the complete original submodule, not a replacement resource stub."""
    wanted = profile.get("resource")
    if wanted is None:
        require("resource_module" not in prepared, "old profile acquired an undeclared resource module")
        return None
    from .native_submodules import readonly_git, readonly_links
    require(plan.get("env") == profile["environment"], "original release environment changed")
    recorded = plan["environment_file_replacements"]
    require(len(recorded) == 2 and recorded[0]["path"] == wanted["path"]
            and recorded[0]["source"] == wanted["source"] and recorded[0]["source_sha256"] == wanted["sha256"]
            and recorded[0]["allow_missing"] is True and recorded[1]["path"] == "Cargo.lock",
            "original resource/lock preparation changed")
    descriptor = referenced(prepared["resource_module"], document=True)
    require((descriptor["path"], descriptor["commit"]) == (wanted["module"], wanted["commit"])
            and descriptor["resource"] == {"path": wanted["relative"], "sha256": wanted["sha256"]},
            "prepared resource identity changed")
    links = readonly_links(ctx.repo(task), task["start_sha"])
    require({"path": wanted["module"], "commit": wanted["commit"]} in links,
            "resource module is not the original Git pin")
    repository, source = Path(descriptor["git_repository"]), Path(descriptor["source"])
    require(repository.is_absolute() and source.is_absolute() and not source.is_symlink(),
            "resource source is not a regular private snapshot")
    require(readonly_links(repository, wanted["commit"]) == [], "resource acquired undeclared nested modules")
    blob = readonly_git(repository, "show", wanted["commit"] + ":" + wanted["relative"])
    require(hashlib.sha256(blob).hexdigest() == wanted["sha256"]
            and blob == (ctx.legacy / "rust" / wanted["source"]).read_bytes(),
            "resource differs from the locked Git object or original Oracle asset")
    files = referenced(descriptor["files"], document=True)
    require(entry.snapshot(source) == files, "resource module source files changed")
    with tempfile.TemporaryDirectory(prefix="locked-resource-attestation-", dir=ctx.scratch) as tmp:
        root = Path(tmp) / "source"
        with start_archive(repository, wanted["commit"]) as stream:
            extract_tar(stream, root)
        require(entry.snapshot(root) == files, "prepared resource omits or replaces original module files")
    for variant in ("start", "golden"):
        rows = oracle["environment_replacements"][variant]
        require(len(rows) == 2 and rows[0]["path"] == wanted["path"]
                and rows[0]["sha256"] == wanted["sha256"] and rows[0]["source"] == wanted["source"],
                "resource differs from original per-variant environment evidence")
    return descriptor


def immutable_paths(ctx, task, plan, oracle, profile):
    """Keep the original plan's production actor distinct from actual test files."""
    ordinary = ctx.immutable_tests(task)
    production = profile.get("production_test_named_paths", [])
    require(plan.get("production_test_named_paths", []) == production,
            "production/test classification differs from the original plan")
    for name in production:
        evidence = oracle.get("inline_test_preservation", {}).get(name, {})
        audit = oracle.get("source_only_audit", {}).get(name, {})
        require(name in plan["source_paths"] and name in oracle["source_changed"]
                and evidence.get("start_items") == 0 and evidence.get("target_items") == 0
                and audit.get("production_matches_target_after_test_stripping") is True
                and audit.get("final_patch_changes_start_test_content") is False,
                "named production path lacks its original source-only/test-preservation evidence")
    if not production and not profile.get("resource"):
        return ordinary
    result = set(ordinary) - set(production)
    if profile.get("resource"):
        result.add(profile["resource"]["path"])
    return sorted(result)


def original_inputs(ctx, task, oracle, plan, patch):
    profile = PROFILES.get(task.get("id"))
    require(profile is not None, "unrecognized task")
    require((task.get("repo"), task.get("start_sha")) == (profile["repo"], profile["start"])
            and (oracle.get("id"), oracle.get("repo"), oracle.get("start_sha")) ==
            (task["id"], profile["repo"], profile["start"]), "task identity changed")
    require(canonical_hash(plan) == profile["plan"] and plan["input_index"] == oracle["input_index"] ==
            profile["index"] and plan["toolchain"] == profile["toolchain"], "original plan/compiler changed")
    require(patch["sha256"] == oracle["golden_patch_sha256"] == profile["golden"]
            and file_hash(Path(patch["path"])) == profile["golden"], "original golden changed")
    options = ctx.config["migration"][OPTION][task["id"]]
    require(set(options) == {"path", "sha256"}, "prepared inputs need an independent hash pin")
    prepared = referenced(options, document=True)
    require(prepared["schema_version"] == "reviewbench-original-locked-workspace-inputs/v1"
            and prepared["task_id"] == task["id"] and prepared["complete"]
            and prepared["new_SUT_tests_model_judges"] == 0, "incomplete original graph preparation")
    require(referenced(prepared["original_task"], document=True) == task
            and referenced(prepared["original_oracle"], document=True) == oracle
            and referenced(prepared["original_plan"], document=True) == plan,
            "prepared inputs differ from current original task/Oracle/plan")
    require(hashlib.sha256(referenced(prepared["golden"])).hexdigest() == profile["golden"],
            "prepared golden differs")
    require(len(plan["session_files"]) == 1, "original test inventory changed")
    harness = session_bytes(ctx, plan, oracle, plan["session_files"][0])
    require(harness == referenced(prepared["harness"]), "original assertion bytes changed")
    references = reference_trees(ctx, task, patch, {"Cargo.lock"})
    sources = referenced(prepared["source_preparation"], document=True)
    resource_module(ctx, task, plan, oracle, prepared, profile)
    if profile.get("production_test_named_paths"):
        immutable_paths(ctx, task, plan, oracle, profile)
    locks = []
    for variant, digest in zip(("start", "golden"), profile["locks"]):
        content = referenced(prepared["locks"][variant])
        require(hashlib.sha256(content).hexdigest() == digest, "original variant lock changed")
        lock = tomllib.loads(content.decode())
        locks.append(lock)
        original = oracle["environment_replacements"][variant]
        require(len(original) == (2 if profile.get("resource") else 1)
                and original[-1]["path"] == "Cargo.lock" and original[-1]["sha256"] == digest,
                "variant lock is not the frozen original environment input")
        require(shlex.split(oracle["environment"]["variant_commands"][variant]) == shlex.split(plan["command"])
                and oracle["environment"]["variant_toolchains"][variant] == profile["toolchain"],
                "original commands/toolchains differ between variants")
        manifests, original_locks = references[variant]
        require(hashlib.sha256(original_locks["Cargo.lock"]).hexdigest() == profile["locks"][0],
                "original source-only reference seed changed")
        require(manifests == {n: row[1] for n, row in sources[variant]["snapshot"].items()
                             if Path(n).name == "Cargo.toml"}, "full original manifest tree changed")
        require(len(prepared["original_logs"][variant]) == 3
                and len(oracle["runs"][variant]) == 3, "six original observations are missing")
        for row, historical in zip(prepared["original_logs"][variant], oracle["runs"][variant]):
            output = referenced(row).decode()
            require(Path(row["path"]).resolve() == (ctx.legacy / "rust" / historical["log"]).resolve()
                    and historical["ok"] and not historical["failed_tests"]
                    and historical["passed_tests"] == oracle["pass_to_pass_tests"]
                    and all("test " + test + " ... ok" in output for test in oracle["pass_to_pass_tests"]),
                    "original successful test evidence changed")
        for field in ("metadata", "offline_metadata"):
            record = prepared[field][variant]
            require(record["exit_status"] == 0 and record["stop_reason"] is None,
                    "original graph metadata did not finish successfully")
            data = referenced(record["stdout"], document=True)
            referenced(record["stderr"])
            require({(p["name"], p["version"], p.get("source")) for p in data["packages"]} ==
                    {(p["name"], p["version"], p.get("source")) for p in lock["package"]},
                    "original complete dependency graph was narrowed")
    require(prepared["cargo"]["sha256"] == file_hash(Path(prepared["cargo"]["path"]))
            and prepared["rustc"]["sha256"] == file_hash(Path(prepared["rustc"]["path"])),
            "prepared original compiler changed")
    expected = rust_vendor.locked_packages(locks)
    audit = referenced(prepared["vendor_audit"], document=True)["packages"]
    require({(r["name"], r["version"], r["source"]): r["package_checksum"] for r in audit} == expected,
            "vendor is not the union of the two complete original locks")
    vendor = Path(prepared["vendor_root"])
    require(vendor.is_absolute() and vendor.is_dir() and not vendor.is_symlink(), "missing original vendor")
    expected_files = set()
    for row in audit:
        directory = vendor / relative_path(row["directory"])
        require(directory.is_dir() and not directory.is_symlink()
                and directory.resolve().is_relative_to(vendor.resolve()), "vendor package path escaped")
        if row.get("format") == "locked-local-registry/v1":
            from .rust_local_registry import checked_line, index_path
            require(row["source"] == rust_vendor.CRATES_IO
                    and row["archive"] == row["name"] + "-" + row["version"] + ".crate"
                    and row["index_path"] == "index/" + index_path(row["name"]),
                    "native registry path/identity changed")
            archive = directory / row["archive"]
            index = directory / row["index_path"]
            require(not archive.is_symlink() and not index.is_symlink()
                    and file_hash(archive) == row["archive_sha256"] == row["package_checksum"]
                    and archive.stat().st_size == row["archive_bytes"], "native original archive changed")
            lines = [line + b"\n" for line in index.read_bytes().splitlines()
                     if json.loads(line).get("vers") == row["version"]]
            require(len(lines) == 1 and hashlib.sha256(lines[0]).hexdigest() == row["index_line_sha256"],
                    "native original index entry changed")
            checked_line(lines[0], row["name"], row["version"], row["package_checksum"])
            expected_files.update(p.relative_to(vendor).as_posix() for p in (archive, index))
            continue
        check = json.loads((directory / ".cargo-checksum.json").read_text())
        require(check["package"] == row["package_checksum"] and len(check["files"]) == row["files"],
                "vendor package checksum inventory changed")
        require(all(file_hash(directory / name) == sha for name, sha in check["files"].items()),
                "vendored source bytes changed")
        expected_files.update((directory / relative_path(name)).relative_to(vendor).as_posix()
                              for name in check["files"])
        expected_files.add((directory / ".cargo-checksum.json").relative_to(vendor).as_posix())
    require(set(tree_files(vendor)) == expected_files, "vendor files added or removed")
    referenced(prepared["source_config"])
    return profile, prepared, harness


def migrate(ctx, task, oracle):
    from . import rust_lane, rust_overlay
    plan = rust_lane.load_plan(ctx, oracle)
    directory = ctx.task_directory(task, oracle, "rust")
    patch = ctx.golden(task, oracle, "rust", directory)
    profile, prepared, harness = original_inputs(ctx, task, oracle, plan, patch)
    submodules = ctx.modules(task)
    if profile.get("resource"):
        descriptor = referenced(prepared["resource_module"], document=True)
        found = [row for row in submodules if row["path"] == descriptor["path"]]
        require(len(found) == 1 and found[0]["commit"] == descriptor["commit"],
                "runtime module list differs from attested resource")
        asset = ctx.bundle(Path(descriptor["source"]), role="submodule", git_commit=descriptor["commit"])
        submodules = [
            {"path": row["path"], "commit": row["commit"], "asset": asset}
            if row["path"] == descriptor["path"] else row for row in submodules
        ]
    registry = json.loads(Path(ctx.config["rust_toolchains"]).read_text())
    tool = Path(registry[profile["toolchain"]]["root"])
    require(file_hash(tool / "bin/cargo") == prepared["cargo"]["sha256"]
            and file_hash(tool / "bin/rustc") == prepared["rustc"]["sha256"], "compiler registry changed")
    compiler = rust_lane.compiler(ctx, profile["toolchain"])
    vendor = ctx.bundle(Path(prepared["vendor_root"]))
    source = ctx.copy_asset(Path(prepared["source_config"]["path"]), "locked-workspace-sources")
    image, image_audit = ctx.image(rust_lane.IMAGE)
    assets = [
        {**compiler, "kind": "archive", "visibility": "developer", "target": "/opt/reviewbench-assets/rust"},
        {**vendor, "kind": "archive", "visibility": "developer", "target": rust_vendor.TARGET},
        {**source, "kind": "file", "visibility": "developer", "target": SOURCES},
    ]
    locks = []
    for index, row in enumerate(prepared["locks"].values()):
        target = "/opt/reviewbench-assets/locked-workspace-lock-" + str(index)
        asset = ctx.copy_asset(Path(row["path"]), "locked-workspace-lock")
        assets.append({**asset, "kind": "file", "visibility": "developer", "target": target})
        locks.append({"path": target, "sha256": asset["sha256"]})
    public = {
        "schema_version": entry.LOCKED_WORKSPACE_SCHEMA, "task_id": task["id"], "selection": entry.SELECTION,
        "workdir": ".", "lock_path": "Cargo.lock", "locks": locks,
        "reference_seed_sha256": profile["locks"][0],
        "reference_seed_policy": "replace-only-identical-committed-seed-in-private-mirror",
        "package": profile["package"], "package_manifest": profile["manifest"],
        "public_command": ["check", "--locked", "-p", profile["package"]],
        "cargo": {"path": "/opt/reviewbench-assets/rust/bin/cargo", "sha256": prepared["cargo"]["sha256"]},
        "rustc": {"path": "/opt/reviewbench-assets/rust/bin/rustc", "sha256": prepared["rustc"]["sha256"]},
        "source_config": {"path": SOURCES, "sha256": source["sha256"]},
        "source_config_mode": "isolated-cargo-home", "git_dependencies": "full-locked-source-identities",
        "cpus": 4, "timeout_seconds": plan["timeout_seconds"],
        "metadata_timeout_seconds": 120, "max_log_bytes": 16 * 1024**2,
    }
    private = {
        "schema_version": entry.LOCKED_WORKSPACE_PRIVATE_SCHEMA, "task_id": task["id"], "original_locked": True,
        "package_manifest": profile["manifest"],
        "harness": {"path": plan["session_files"][0]["path"], "sha256": hashlib.sha256(harness).hexdigest()},
        "command": shlex.split(plan["command"])[2:], "expected_tests": oracle["pass_to_pass_tests"],
    }
    entry.validate(public, private)
    for name, value, target, visibility in (
        ("public", public, SPEC, "developer"), ("private", private, PRIVATE, "grader"),
    ):
        path = ctx.output / (name + "-" + str(profile["index"]) + ".json")
        atomic_json(path, value)
        assets.append({**ctx.copy_asset(path, "locked-workspace-spec"), "kind": "file",
                       "visibility": visibility, "target": target})
    tools = rust_overlay.build_tools(ctx, extra_entries={
        "locked_workspace.py": Path(__file__).with_name("rust_image_entry.py"),
    })
    assets.append({**tools, "kind": "archive", "visibility": "developer", "target": rust_overlay.TOOLS})
    h = ctx.copy_asset(Path(prepared["harness"]["path"]), "locked-workspace-tests")
    target = "/opt/reviewbench-assets/locked-workspace-original-test.rs"
    assets.append({**h, "kind": "file", "visibility": "grader", "target": target})
    prefix = ["python3", TOOL, "--spec", SPEC, "--task-id", task["id"]]
    destination = "/workspace/" + private["harness"]["path"]
    recipe = RuntimeRecipe(
        task_id=task["id"], repo=task["repo"], start_sha=task["start_sha"], lane="rust",
        image=image, image_audit=image_audit, workdir="/workspace", cpus=4, memory_mb=8192,
        build_scratch_mb=24576, command_timeout_seconds=plan["timeout_seconds"],
        env={
            "PATH": "/opt/reviewbench-assets/rust/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "RUSTC": "/opt/reviewbench-assets/rust/bin/rustc", "CARGO_HOME": "/opt/reviewbench-cache/cargo",
            "CARGO_TARGET_DIR": "/opt/reviewbench-cache/target", "CARGO_BUILD_JOBS": "4",
            "CARGO_TERM_COLOR": "never", "CARGO_INCREMENTAL": "0", "CARGO_NET_OFFLINE": "true",
            "CARGO_PROFILE_DEV_DEBUG": "0", "CARGO_PROFILE_TEST_DEBUG": "0",
            "RUSTFLAGS": plan.get("rustflags", "--cap-lints=allow"), "RUST_BACKTRACE": "1",
            "SKIP_WASM_BUILD": "1", "RUST_BUILD_DOCKER": "1", "GIT_COMMIT": "reviewbench-hard-oracle",
            "GIT_DIRTY": "false", "SOURCE_TIMESTAMP": "0", "TMPDIR": "/workspace/.reviewbench/tmp",
            "PROTOC_INCLUDE": "/usr/include",
            **profile.get("environment", {}),
        },
        cache_mounts={"cargo": "/opt/reviewbench-cache/cargo", "target": "/opt/reviewbench-cache/target",
                      "rust-build": rust_overlay.BUILD_CACHE},
        bootstrap=["mkdir -p /workspace/.reviewbench/tmp"], dev_checks=[shlex.join(prefix)],
        scratch_paths=[], assets=assets, submodules=submodules,
        grading={
            "multifile_build": True, "immutable_paths": immutable_paths(ctx, task, plan, oracle, profile),
            "golden_patch": patch,
            "bootstrap": ["mkdir -p " + shlex.quote(str(Path(destination).parent)) + " && cp " +
                          shlex.quote(target) + " " + shlex.quote(destination)],
            "commands": [{"command": shlex.join([*prefix, "--private", PRIVATE]), "parser": "rust-test",
                          "expected_tests": oracle["pass_to_pass_tests"]}],
        },
    )
    recipe.validate()
    return recipe
