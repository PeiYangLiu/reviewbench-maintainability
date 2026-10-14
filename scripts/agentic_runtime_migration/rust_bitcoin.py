"""Bind original bitcoin71 Git preparation and both unchanged public-API harnesses."""
import hashlib
import json
from pathlib import Path
import shlex
import tomllib

from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, atomic_json, canonical_hash, file_hash,
)
from reviewbench_maintainability.agentic.snapshots import tree_hash
from . import rust_bitcoin_entry as entry
from .rust_overlay import reference_inputs, session_bytes
from .rust_vendor import locked_packages

ID = "github__p2pderivatives__rust-bitcoin-coin-selection__pull_23__comment_1281482075__api_design"
OPTION = "rust_original_bitcoin_git"
PROFILE = {
    "repo": "p2pderivatives/rust-bitcoin-coin-selection",
    "start": "5b2dca769fbadacfe476ca3f4f91226c4234efce",
    "plan": "448240f399a37e8a2f1aceafebb28c6565539715760d6e27c356eb6d9c39083d",
    "golden": "f58c52844e07349e6b38dea3beafd665470c8112ec688b6fa236e284ce517db9",
    "locks": {
        "start": "7e99f69425d99857e1a2be121512b312b5e3ccae97923afd86d554c63dbcb50d",
        "golden": "11e6029b1d79d05c890352999dd84c0f5754d845a21e14c909435bcb3b7ae108",
    },
    "cargo": "b34acb7e9d9da98cac5882b98d47ddb7654c8650ae82905ffb1c33c1a00b6a0f",
    "rustc": "464d1dc87e7edf5a740af344dcdffb1f8e4cbf958367514f47cebdc0c4e72e17",
    "index": "16d7a2eec788c4b0785ccc66a535cd6520c46712",
}
PINS = [
    {"url": "https://github.com/yancyribbens/rust-bitcoin", "branch": "add-option-param-to-predict_weight",
     "commit": "1b009b809ba2f282d88153cc32e390f1bbd4773c"},
    {"url": "https://github.com/rust-bitcoin/rust-bitcoin", "branch": "master",
     "commit": "7fd9b89e82bdbae42e5a25e82602feab995715c8"},
]
HARNESSES = {
    "tests/oracle_start.rs": "96265ec82f8a5c0a30049427ba7cbb07807bf69b7dfe00b1d255c215dde5f9a2",
    "tests/oracle_golden.rs": "925302c7eb5639983c2cda5a8e2827e1569dd4a3a9e72e70a0ce317d09622e8b",
}
TEST = "fallback_selection_funds_the_requested_target"
PREPARE = "cargo +1.73.0 update -p hex-conservative --precise 0.1.1 >/dev/null"
SPEC = "/opt/reviewbench-assets/bitcoin-git-public.json"
PRIVATE = "/opt/reviewbench-assets/bitcoin-git-private.json"
SOURCES = "/opt/reviewbench-assets/bitcoin-git-sources.toml"
ENTRY = "/opt/reviewbench-assets/rust-build-tools/bitcoin_git.py"


def require(value, message):
    if not value:
        raise ContractError("original bitcoin71: " + message)


def referenced(row, document=False):
    path = Path(row["path"])
    require(path.is_absolute() and path.is_file() and not path.is_symlink()
            and file_hash(path) == row["sha256"], "prepared asset changed")
    data = path.read_bytes()
    return json.loads(data) if document else data


def requested(ctx, task):
    choices = getattr(ctx, "config", {}).get("migration", {}).get(OPTION, {})
    require(isinstance(choices, dict) and set(choices) <= {ID}, "unknown profile")
    return task.get("id") in choices


def original_inputs(ctx, task, oracle, plan, patch):
    require(task["id"] == oracle["id"] == ID and task["repo"] == PROFILE["repo"]
            and task["start_sha"] == oracle["start_sha"] == PROFILE["start"], "task changed")
    require(canonical_hash(plan) == PROFILE["plan"] and oracle["input_index"] == 71
            and plan["prepare_command"] == PREPARE and plan["toolchain"] == "1.73.0", "original preparation changed")
    require(patch["sha256"] == oracle["golden_patch_sha256"] == PROFILE["golden"] == file_hash(Path(patch["path"])),
            "original source-only golden changed")
    bundle = referenced(ctx.config["migration"][OPTION][ID], True)
    prepared = referenced(bundle["prepared"], True)
    vendor = referenced(bundle["vendor"], True)
    require(prepared["task_id"] == vendor["task_id"] == ID
            and prepared["complete"] and prepared["new_original_prepare_resolutions"] == 2
            and prepared["original_final_lock_recovered"] is False
            and prepared["new_SUT_model_judge_calls"] == vendor["new_SUT_model_judge_calls"] == 0
            and vendor["new_resolutions"] == 0, "unbound first-success environment")
    require(prepared["original_plan_sha256"] == PROFILE["plan"]
            and referenced(prepared["golden"]) == Path(patch["path"]).read_bytes(), "first-success inputs changed")
    index = referenced(prepared["index_binding"], True)
    require(index["commit"] == PROFILE["index"] and index["full_index_not_filtered"]
            and index["original_final_lock_recovered"] is False, "fixed complete index changed")
    roots = referenced(prepared["sources"], True)
    manifests = reference_inputs(ctx, task, patch, ["Cargo.toml", "Cargo.lock"])
    session = {row["path"]: session_bytes(ctx, plan, oracle, row) for row in plan["session_files"]}
    require({n: hashlib.sha256(data).hexdigest() for n, data in session.items()} == HARNESSES,
            "original harness bytes changed")
    require(oracle["pass_to_pass_tests"] == [TEST] and len(plan["session_files"]) == 2,
            "original test inventory changed")
    locks = []
    for i, variant in enumerate(("start", "golden")):
        require(manifests[variant]["Cargo.lock"] is None, "original source gained a committed lock")
        require(entry.core.snapshot(Path(roots[variant]["source"])) == roots[variant]["snapshot"]
                and roots[variant]["snapshot"]["Cargo.toml"][1] ==
                hashlib.sha256(manifests[variant]["Cargo.toml"]).hexdigest(), "prepared source changed")
        content = manifests[variant]["Cargo.toml"]
        original = tomllib.loads(content.decode())
        for n, operation in enumerate(plan["environment_replacements"]):
            old, new = operation[variant + "_old"], operation[variant + "_new"]
            require(content.decode().count(old) == operation.get("count", 1), "original overlay occurrence changed")
            content = content.decode().replace(old, new, operation.get("count", 1)).encode()
            require(hashlib.sha256(content).hexdigest() == oracle["environment_replacements"][variant][n]["sha256"],
                    "original environment intermediate hash changed")
        require(entry.pin_manifest(original, {"git_pins": PINS}) == tomllib.loads(content.decode()),
                "runtime Git pins differ from the original preparation")
        row = prepared["variants"][variant]
        lock = referenced(row["lock"])
        require(row["lock"]["sha256"] == PROFILE["locks"][variant], "first-success lock changed")
        value = tomllib.loads(lock.decode())
        require(len(value["package"]) == row["packages"] == (26 if variant == "start" else 28),
                "first-success complete graph changed")
        require(next(p for p in value["package"] if p["name"] == "hex-conservative")["version"] == "0.1.1",
                "original precise package pin changed")
        locks.append(value)
        directory = Path(row["lock"]["path"]).parent
        intent = json.loads((directory / (variant + "-first-new-resolution-intent.json")).read_text())
        receipt = json.loads((directory / (variant + "-first-success.json")).read_text())
        observed = referenced(receipt["original_prepare"], True)
        require(intent["original_prepare_command"] == PREPARE and intent["max_original_prepare_attempts"] == 1
                and receipt["historical_lock_recovered"] is False and receipt["lock"] == row["lock"]
                and observed["command"] == [prepared["cargo"]["path"], "update", "-p", "hex-conservative", "--precise", "0.1.1"]
                and observed["exit_status"] == 0 and observed["changed_paths"] == ["Cargo.lock"],
                "original first-success preparation receipt changed")
        for stream in ("stdout", "stderr"):
            referenced(observed[stream])
        require(observed["source_after"]["Cargo.lock"][1] == row["lock"]["sha256"]
                and all(observed["source_after"].get(n) == r for n, r in observed["source_before"].items()),
                "original preparation changed non-lock source")
        expected_command = ["cargo", "+1.73.0", "test", "--features", "rand", "--test", "oracle_" + variant,
                            "--", "--nocapture"]
        require(shlex.split(plan[variant + "_command"]) == expected_command
                and shlex.split(oracle["environment"]["variant_commands"][variant]) == expected_command
                and oracle["environment"]["variant_toolchains"][variant] == "1.73.0",
                "original test command/compiler changed")
        require(len(oracle["runs"][variant]) == 3, "original repetitions missing")
        for run in oracle["runs"][variant]:
            require(run["ok"] and run["passed_tests"] == [TEST] and not run["failed_tests"], "original behavior changed")
            text = (ctx.legacy / "rust" / run["log"]).read_text()
            require("test " + TEST + " ... ok" in text, "original test log changed")
        for role in ("public", "private"):
            observation = vendor["observations"][role + "-" + variant]
            require(observation["exit_status"] == 0 and observation["source_unchanged"], "offline closure failed")
            graph = referenced(observation["stdout"]).decode()
            referenced(observation["stderr"])
            workspace = Path(observation["cwd"])
            checked = entry.core.graph_check(graph, workspace, entry.core.snapshot(workspace), lock, locked_git=True)
            require(checked["packages"] == row["packages"], "offline full graph changed")
    source_audit = referenced(vendor["audit"], True)
    require({(r["name"], r["version"], r["source"]): r["package_checksum"] for r in source_audit}
            == locked_packages(locks), "vendor identity set changed")
    descriptor = referenced(vendor["vendor_asset"], True)
    require(tree_hash(Path(vendor["vendor"])) == descriptor["tree_sha256"], "vendored source tree changed")
    referenced(vendor["source_config"])
    require(all(prepared[key]["sha256"] == PROFILE[key] == file_hash(Path(prepared[key]["path"]))
                for key in ("cargo", "rustc")), "original Rust1.73 binaries changed")
    referenced(bundle["compiler_asset"], True)
    return bundle, prepared, vendor, session


def migrate(ctx, task, oracle):
    from . import rust_lane, rust_overlay, rust_api
    plan = rust_lane.load_plan(ctx, oracle)
    patch = ctx.golden(task, oracle, "rust", ctx.task_directory(task, oracle, "rust"))
    bundle, prepared, vendor, session = original_inputs(ctx, task, oracle, plan, patch)
    image, image_audit = ctx.image(rust_lane.IMAGE)
    assets = []

    def asset(value, target, role="developer", kind="file"):
        assets.append({**value, "kind": kind, "target": target, "visibility": role})
        return {"path": target, "sha256": value["sha256"]}

    asset(bundle["compiler_asset"], "/opt/reviewbench-assets/rust", kind="archive")
    asset(vendor["vendor_asset"], "/opt/reviewbench-assets/vendor", kind="archive")
    source_config = asset(vendor["source_config"], SOURCES)
    defaults = []
    for number, variant in enumerate(("start", "golden")):
        defaults.append({"git": PINS[number]["url"], "commit": PINS[number]["commit"],
                         "asset": asset(prepared["variants"][variant]["lock"],
                                        "/opt/reviewbench-assets/bitcoin-lock-" + str(number))})
    public = {
        "schema_version": entry.SCHEMA, "task_id": ID, "selection": entry.SELECTION,
        "git_pins": PINS, "locks": defaults, "source_config": source_config,
        "cargo": {"path": "/opt/reviewbench-assets/rust/bin/cargo", "sha256": PROFILE["cargo"]},
        "rustc": {"path": "/opt/reviewbench-assets/rust/bin/rustc", "sha256": PROFILE["rustc"]},
        "public_command": ["check", "--locked", "--features", "rand", "--message-format=json"],
        "cpus": 4, "timeout_seconds": 1800, "metadata_timeout_seconds": 120, "max_log_bytes": 32 * 1024**2,
    }
    api_spec = rust_api.specification(ctx, task, plan, oracle)
    require(api_spec is not None, "original two-target API specification missing")
    api_spec["forms"] = [
        {k: v for k, v in row.items() if k != "arguments"} | {"command": ["cargo", *row["arguments"]]}
        for row in api_spec["forms"]]
    private = {"task_id": ID, "api": api_spec}
    entry.validate(public, private)
    for name, value, target, role in (("public", public, SPEC, "developer"), ("private", private, PRIVATE, "grader")):
        path = ctx.output / ("bitcoin-git-" + name + ".json")
        atomic_json(path, value)
        asset(ctx.copy_asset(path, "bitcoin-git-specs"), target, role)
    tools = rust_overlay.build_tools(ctx, extra_entries={
        "bitcoin_git.py": Path(entry.__file__),
        "image_locks.py": Path(__file__).with_name("rust_image_entry.py"),
        "original_api.py": Path(__file__).with_name("rust_api_entry.py"),
    })
    asset(tools, rust_overlay.TOOLS, kind="archive")
    private_bootstrap = []
    for name, data in session.items():
        path = Path(prepared["original_session"][name]["path"])
        require(path.read_bytes() == data, "session asset changed")
        value = asset(ctx.copy_asset(path, "bitcoin-original-session"),
                      "/opt/reviewbench-assets/original-" + Path(name).name, "grader")
        private_bootstrap.append("mkdir -p /workspace/tests && cp " + shlex.quote(value["path"])
                                 + " " + shlex.quote("/workspace/" + name))
    prefix = ["python3", ENTRY, "--spec", SPEC, "--task-id", ID]
    recipe = RuntimeRecipe(
        task_id=ID, repo=task["repo"], start_sha=task["start_sha"], lane="rust",
        image=image, image_audit=image_audit, workdir="/workspace", cpus=4, memory_mb=4096,
        build_scratch_mb=4096, command_timeout_seconds=1800, submodules=ctx.modules(task),
        env={"PATH": "/opt/reviewbench-assets/rust/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
             "RUSTC": "/opt/reviewbench-assets/rust/bin/rustc", "CARGO_HOME": "/opt/reviewbench-cache/cargo",
             "CARGO_TARGET_DIR": "/opt/reviewbench-cache/target", "CARGO_NET_OFFLINE": "true",
             "CARGO_BUILD_JOBS": "4", "CARGO_INCREMENTAL": "0", "CARGO_TERM_COLOR": "never",
             "CARGO_PROFILE_DEV_DEBUG": "0", "CARGO_PROFILE_TEST_DEBUG": "0", "RUSTFLAGS": "--cap-lints=allow",
             "HOME": "/tmp", "TMPDIR": "/workspace/.reviewbench/tmp"},
        assets=assets, cache_mounts={"cargo": "/opt/reviewbench-cache/cargo", "target": "/opt/reviewbench-cache/target",
                                    "rust-build": rust_overlay.BUILD_CACHE},
        bootstrap=["mkdir -p /workspace/.reviewbench/tmp"], dev_checks=[shlex.join(prefix)], scratch_paths=[],
        grading={"multifile_build": True, "immutable_paths": ctx.immutable_tests(task), "golden_patch": patch,
                 "bootstrap": private_bootstrap, "commands": [{"command": shlex.join([*prefix, "--private", PRIVATE]),
                                                               "parser": "rust-test", "expected_tests": [TEST]}]},
    )
    recipe.validate()
    return recipe
