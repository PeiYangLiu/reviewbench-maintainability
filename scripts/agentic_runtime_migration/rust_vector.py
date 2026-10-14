"""Vector111's original root-manifest pairs and Datadog configuration test."""
import hashlib
import json
from pathlib import Path
import shlex
import tomllib

from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, atomic_json, canonical_hash, file_hash,
)
from reviewbench_maintainability.agentic.snapshots import tree_hash
from . import rust_manifest_variants_entry as entry
from .rust_composable import referenced
from .rust_lock_assets import reference_trees
from .rust_overlay import session_bytes

ID = "github__vectordotdev__vector__pull_18929__comment_1372273555__naming"
OPTION = "rust_original_vector_root"
PROFILE = {
    "repo": "vectordotdev/vector", "start": "6ff4a8bf7b9b5f3f447410a791fb37ae202987d3",
    "plan": "dd05875de6eb0c7a061e76acfda22e42b601f444d1e08e36dec2b5558e232c57",
    "golden": "a014dedb9b7791f67e1cac2add5e64cd5a29ad847269d03696beca52954e5a77",
    "harness": "9658639ad65ecbdd1ca09ed2265b1af5bb54c5242091c1042b1c057231de6cb0",
    "cargo": "1e52169612ce3862d20f502cc6e2ea7040666bee5b2deb2136fa6312489d59f4",
    "rustc": "4ac14945459bba59f532e1174751547782710f7a13eef5cf54bdef6ad0de5f2a",
    "manifests": {
        "start": "76c0d6474c3ab5f0a0dbeea00cd243dce431365b81c0b21e51554e65f630b4a3",
        "golden": "7d49a7665d44e25cb2b47b85aec8ed1f2ea9ccb6871af4838792bd0fb0344c4f",
    },
    "locks": {
        "start": "99604395560ee61787fac9f6f03c74d373a47b68a15ee5d92517c7cb4ecba930",
        "golden": "3eb96b1622483db607745d0054a2602d5a4ca6bf996b1a420789794692ed8086",
    },
    "registry_asset": "cf0d1e4bd3d989fce51b2433ab9fc081f7317a41e6c565de79f779689a4c4c27",
    "registry_tree": "db76cec69cf81b484c2cb4bd57c23dc230d9792697b052a8fdab218fd958dc5b",
    "git_asset": "2287f222d4700027448602f72e1f2d0660760b14ec30a5efc0a63bb29d83e18c",
    "git_tree": "41db91538f7e81f972707f54377deffb6925c2ddb91a152120d71750750da076",
    "sources": "12cea84749b651c6b92571fd3da80bf3f0f903632dac01183cd6ea7ac3978ddc",
}
TOOLCHAIN = "1.72.1"
HARNESS = "tests/oracle_datadog.rs"
TEST = "generated_datadog_logs_configuration_deserializes"
FEATURES = ["--no-default-features", "--features", "sinks-datadog_logs"]
COMMAND = ["test", "--locked", *FEATURES, "--test", "oracle_datadog", TEST, "--", "--nocapture"]
PUBLIC = ["build", "--locked", *FEATURES, "--lib", "--message-format=json"]
SPEC = "/opt/reviewbench-assets/vector-root-public.json"
PRIVATE = "/opt/reviewbench-assets/vector-root-private.json"
SOURCES = "/opt/reviewbench-assets/vector-root-sources.toml"
TOOL = "/opt/reviewbench-assets/rust-build-tools/manifest_variants.py"


def require(value, message):
    if not value:
        raise ContractError("original Vector root: " + message)


def requested(ctx, task):
    settings = getattr(ctx, "config", {}).get("migration", {}).get(OPTION, {})
    require(isinstance(settings, dict) and set(settings) <= {ID}, "unsupported root profile")
    return task.get("id") in settings


def original_inputs(ctx, task, oracle, plan, patch):
    require(task["id"] == oracle["id"] == ID and task["repo"] == oracle["repo"] == PROFILE["repo"]
            and task["start_sha"] == oracle["start_sha"] == PROFILE["start"], "task/start changed")
    require(canonical_hash(plan) == PROFILE["plan"] and plan["input_index"] == oracle["input_index"] == 111,
            "original plan changed")
    require(plan["toolchain"] == TOOLCHAIN and shlex.split(plan["command"]) ==
            ["cargo", "+" + TOOLCHAIN, *COMMAND] and plan["build_jobs"] == 4
            and plan["timeout_seconds"] == 3600 and plan["workdir"] == ".", "original execution changed")
    require(patch["sha256"] == oracle["golden_patch_sha256"] == PROFILE["golden"]
            and file_hash(Path(patch["path"])) == PROFILE["golden"], "source-only golden changed")
    refs = ctx.config["migration"][OPTION][ID]
    require(set(refs) == {"inputs", "registry", "git", "offline", "task", "oracle", "plan"},
            "preparation binding changed")
    for name, value in (("task", task), ("oracle", oracle), ("plan", plan)):
        require(referenced(refs[name], document=True) == value, "prepared " + name + " changed")
    inputs, registry, git, offline = [referenced(refs[key], document=True)
                                     for key in ("inputs", "registry", "git", "offline")]
    require(all(row["task_id"] == ID and row["full_scope"] == 402
                for row in (inputs, registry, git, offline)), "prepared task scope changed")
    require(inputs["original_plan_sha256"] == PROFILE["plan"] and inputs["new_resolution_SUT_model_judge_calls"] == 0
            and registry["new_SUT_model_judge_calls"] == registry["new_dependency_resolutions"] == 0
            and git["new_resolution_SUT_model_judge_calls"] == offline["new_resolution_SUT_model_judge_calls"] == 0
            and offline["all_complete"] is True, "original preparation is incomplete")
    require(hashlib.sha256(referenced(inputs["golden"])).hexdigest() == PROFILE["golden"],
            "prepared golden changed")
    require(oracle["pass_to_pass_tests"] == [TEST] and len(plan["session_files"]) == 1
            and plan["session_files"][0]["path"] == HARNESS, "original test inventory changed")
    harness = session_bytes(ctx, plan, oracle, plan["session_files"][0])
    require(harness == referenced(inputs["harness"])
            and hashlib.sha256(harness).hexdigest() == PROFILE["harness"], "original assertion changed")
    require(file_hash(ctx.legacy / "rust" / plan["source_paths_file"]) == plan["source_paths_file_sha256"],
            "full original production closure changed")
    snapshots = referenced(inputs["source_preparation"], document=True)
    references = reference_trees(ctx, task, patch, {"Cargo.lock"})
    logs = referenced(inputs["original_logs"], document=True)
    observations = referenced(offline["observations"], document=True)
    expected_packages = {}
    for variant, count, members in (("start", 964, 28), ("golden", 977, 29)):
        require(oracle["environment"]["variant_toolchains"][variant] == TOOLCHAIN
                and shlex.split(oracle["environment"]["variant_commands"][variant]) ==
                ["cargo", "+" + TOOLCHAIN, *COMMAND], "original variant argv changed")
        source = snapshots[variant]
        require(entry.core.snapshot(Path(source["source"])) == source["source_snapshot"]
                and entry.core.snapshot(Path(source["original_work"])) == source["original_work_snapshot"],
                "complete prepared source changed")
        manifests, locks = references[variant]
        require(manifests == {name: row[1] for name, row in source["source_snapshot"].items()
                              if Path(name).name == "Cargo.toml"}
                and hashlib.sha256(locks["Cargo.lock"]).hexdigest() == PROFILE["locks"]["start"],
                "original source manifest/lock seed changed")
        pair = inputs["variants"][variant]
        for name, key in (("Cargo.toml", "manifest"), ("Cargo.lock", "lock")):
            data = referenced(pair[key])
            pin = PROFILE["manifests" if key == "manifest" else "locks"][variant]
            declared = next(row for row in plan["environment_file_replacements"] if row["path"] == name)
            field = "golden_source" if variant == "golden" else "source"
            require(hashlib.sha256(data).hexdigest() == pin == declared[field + "_sha256"]
                    and data == (ctx.legacy / "rust" / declared[field]).read_bytes()
                    and source["original_work_snapshot"][name][1] == pin,
                    "original manifest/lock replacement changed")
            require(source["source_snapshot"][name][1] == declared["old_sha256"],
                    "original replacement preimage changed")
        lock = tomllib.loads(referenced(pair["lock"]).decode())
        identities = {(p["name"], p["version"], p.get("source")) for p in lock["package"]}
        require(len(identities) == len(lock["package"]) == count, "original dependency graph size changed")
        expected_packages.update({(p["name"], p["version"], p["source"]): p.get("checksum")
                                  for p in lock["package"] if p.get("source")})
        require(len(oracle["runs"][variant]) == len(logs[variant]) == 3, "original six observations missing")
        for original, row in zip(oracle["runs"][variant], logs[variant]):
            text = referenced(row).decode()
            require(Path(row["path"]).resolve() == (ctx.legacy / "rust" / original["log"]).resolve()
                    and original["ok"] and original["failed_tests"] == [] and original["passed_tests"] == [TEST]
                    and "test " + TEST + " ... ok" in text, "original behavior evidence changed")
        for role in ("public", "private"):
            observation = observations[variant + "-" + role]
            require(observation["exit_status"] == 0 and observation["source_and_lock_unchanged"]
                    and observation["empty_cargo_home"] and observation["no_original_git_cache_mounted"],
                    "original empty-home offline check failed")
            graph = referenced(observation["stdout"], document=True)
            require(referenced(observation["stderr"]) == b""
                    and len(graph["packages"]) == len(graph["resolve"]["nodes"]) == count
                    and len(graph["workspace_members"]) == members
                    and {(p["name"], p["version"], p.get("source")) for p in graph["packages"]} == identities,
                    "complete original offline graph was narrowed")
    for key, component, count in (("registry", registry, 1233), ("git", git, 46)):
        descriptor = referenced(component["asset"], document=True)
        require(component["asset"]["sha256"] == PROFILE[key + "_asset"]
                and descriptor["tree_sha256"] == PROFILE[key + "_tree"] == tree_hash(Path(component["source"])),
                "original component archive/source changed")
        rows = referenced(component["vendor_audit"], document=True)
        require(len(rows) == count and all(
            (row["name"], row["version"], row["source"]) in expected_packages
            and expected_packages[(row["name"], row["version"], row["source"])] == row["package_checksum"]
            for row in rows), "original vendored source identities changed")
    require(registry["registry_packages"] == 1233 and git["git_packages"] == 46
            and git["git_source_identities"] == 9, "original complete source union changed")
    require(hashlib.sha256(referenced(git["source_config"])).hexdigest() == PROFILE["sources"],
            "original offline source configuration changed")
    for key in ("cargo", "rustc"):
        row = inputs["compiler"][key]
        require(row["sha256"] == PROFILE[key] == file_hash(Path(row["path"])), "fixed compiler changed")
    require(ctx.modules(task) == [], "original source gained a submodule")
    return inputs, registry, git


def migrate(ctx, task, oracle):
    from . import rust_lane, rust_overlay, rust_protoc
    plan = rust_lane.load_plan(ctx, oracle)
    patch = ctx.golden(task, oracle, "rust", ctx.task_directory(task, oracle, "rust"))
    inputs, registry, git = original_inputs(ctx, task, oracle, plan, patch)
    image, image_audit = ctx.image(rust_lane.IMAGE)
    assets = []

    def asset(value, target, role="developer", kind="file"):
        assets.append({**value, "target": target, "visibility": role, "kind": kind})
        return {"path": target, "sha256": value["sha256"]}

    asset(rust_lane.compiler(ctx, TOOLCHAIN), "/opt/reviewbench-assets/rust", kind="archive")
    asset(registry["asset"], "/opt/reviewbench-assets/vendor-registry", kind="archive")
    asset(git["asset"], "/opt/reviewbench-assets/vendor-git", kind="archive")
    sources = asset(ctx.copy_asset(Path(git["source_config"]["path"]), "vector-root-sources"), SOURCES)
    forms = []
    for number, variant in enumerate(("start", "golden")):
        pair = inputs["variants"][variant]
        manifest = asset(ctx.copy_asset(Path(pair["manifest"]["path"]), "vector-root-manifests"),
                         "/opt/reviewbench-assets/vector-root-manifest-" + str(number))
        lock = asset(ctx.copy_asset(Path(pair["lock"]["path"]), "vector-root-locks"),
                     "/opt/reviewbench-assets/vector-root-lock-" + str(number))
        forms.append({"name": variant, "manifests": [{"path": "Cargo.toml", "asset": manifest}], "lock": lock})
    public = {
        "schema_version": entry.ROOT_SCHEMA, "selection": entry.SELECTION, "task_id": ID,
        "package": "vector", "package_manifest": "Cargo.toml", "feature_args": FEATURES, "forms": forms,
        "manifests": [{"path": "Cargo.toml", "reference": forms[0]["manifests"][0]["asset"]}],
        "reference_seed_sha256": PROFILE["locks"]["start"], "source_config": sources,
        "cargo": {"path": "/opt/reviewbench-assets/rust/bin/cargo", "sha256": PROFILE["cargo"]},
        "rustc": {"path": "/opt/reviewbench-assets/rust/bin/rustc", "sha256": PROFILE["rustc"]},
        "public_command": PUBLIC, "cpus": 4, "timeout_seconds": 3600,
        "metadata_timeout_seconds": 180, "max_log_bytes": 32 * 1024**2,
    }
    harness = {"path": HARNESS, "sha256": PROFILE["harness"]}
    private = {"task_id": ID, "package_manifest": "Cargo.toml", "harness": harness,
               "session_files": [harness], "test_filter": TEST, "expected_tests": [TEST], "command": COMMAND}
    entry.validate(public, private)
    for name, value, target, role in (("public", public, SPEC, "developer"), ("private", private, PRIVATE, "grader")):
        path = ctx.output / ("vector-root-" + name + ".json")
        atomic_json(path, value)
        asset(ctx.copy_asset(path, "vector-root-specs"), target, role)
    tools = rust_overlay.build_tools(ctx, extra_entries={
        "manifest_variants.py": Path(entry.__file__),
        "image_locks.py": Path(__file__).with_name("rust_image_entry.py"),
    })
    asset(tools, rust_overlay.TOOLS, kind="archive")
    test = asset(ctx.copy_asset(Path(inputs["harness"]["path"]), "vector-original-test"),
                 "/opt/reviewbench-assets/vector-original-test.rs", "grader")
    # This is an explicit pinned native-tool reconstruction, not a claim to
    # have recovered the absent historical image byte-for-byte.
    asset(rust_protoc.build_asset(ctx), rust_protoc.TARGET, kind="archive")
    prefix = ["python3", TOOL, "--spec", SPEC, "--task-id", ID]
    recipe = RuntimeRecipe(
        task_id=ID, repo=task["repo"], start_sha=task["start_sha"], lane="rust",
        image=image, image_audit=image_audit, workdir="/workspace", cpus=4, memory_mb=8192,
        build_scratch_mb=12288, command_timeout_seconds=3600,
        env={"PATH": "/opt/reviewbench-assets/rust/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
             "RUSTC": "/opt/reviewbench-assets/rust/bin/rustc", "CARGO_HOME": "/opt/reviewbench-cache/cargo",
             "CARGO_TARGET_DIR": "/opt/reviewbench-cache/target", "CARGO_NET_OFFLINE": "true",
             "CARGO_BUILD_JOBS": "4", "CARGO_INCREMENTAL": "0", "CARGO_TERM_COLOR": "never",
             "CARGO_PROFILE_DEV_DEBUG": "0", "CARGO_PROFILE_TEST_DEBUG": "0", "RUSTFLAGS": "--cap-lints=allow",
             "RUST_BACKTRACE": "1", "PROTOC": rust_protoc.TARGET + "/bin/protoc",
             "PROTOC_INCLUDE": rust_protoc.TARGET + "/include", "SKIP_WASM_BUILD": "1",
             "RUST_BUILD_DOCKER": "1", "GIT_COMMIT": "reviewbench-hard-oracle", "GIT_DIRTY": "false",
             "SOURCE_TIMESTAMP": "0", "HOME": "/tmp", "TMPDIR": "/workspace/.reviewbench/tmp"},
        assets=assets, cache_mounts={"cargo": "/opt/reviewbench-cache/cargo", "target": "/opt/reviewbench-cache/target",
                                    "rust-build": rust_overlay.BUILD_CACHE},
        bootstrap=["mkdir -p /workspace/.reviewbench/tmp",
                   'test "$(' + rust_protoc.TARGET + '/bin/protoc --version)" = "libprotoc 3.19.2"'],
        dev_checks=[shlex.join(prefix)], scratch_paths=[],
        grading={"multifile_build": True, "immutable_paths": ctx.immutable_tests(task), "golden_patch": patch,
                 "bootstrap": ["mkdir -p /workspace/tests && cp " + test["path"] + " /workspace/" + HARNESS],
                 "commands": [{"command": shlex.join([*prefix, "--private", PRIVATE]),
                               "parser": "rust-test", "expected_tests": [TEST]}]},
    )
    recipe.validate()
    return recipe
