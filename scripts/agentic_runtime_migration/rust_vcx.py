"""Original VCX workspace overlays, retained locks and two public API harnesses."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import tomllib

from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, atomic_json, canonical_hash, file_hash,
)
from . import rust_overlay_api_entry as entry, rust_vendor
from .rust_build_entry import manifest_operations
from .rust_lock_assets import reference_trees
from .rust_locked_workspace import referenced
from .rust_manifest_service import attest_vendor
from .rust_overlay import reference_inputs, session_bytes

ID = "github__openwallet-foundation__vcx__pull_1085__comment_1489542112__duplication"
OPTION = "rust_original_overlay_apis"
PROFILE = {
    "repo": "openwallet-foundation/vcx", "index": 69,
    "start": "5afbe287a105b579d131536028e8d1ed60884f80",
    "plan": "c4ada0a1404289a91698be43df7895f01bbbc0731e4b9ced482366d30966531b",
    "golden": "41ac5d33ea82b5e167ae6ba6f00a943d596b9e165a542426e64558df7a53a6c7",
    "seed": "9e4d99abbf1458b0d9698d1b52bcfd24e82a5a3abd91ac813f2fa68f8032f3ce",
    "locks": ["ec6900a448ee7504d913b42a27d16950dccfe892e3e65a19c34fb4b519e591cb",
              "28fecd63973f3979dd04cdfc1b13e59d777630012020e107b63c0a68c03ba6cd"],
    "harnesses": ["d137d920488453fc09ef762606ec430098e701dc8420a724db79606b1d8d342e",
                  "d999c7b39b1b549eb948061a1c62d46e4eef1f9b2e484429a766f6dd6f4c449c"],
    "cargo": "0146fa3496118cbf4ed3f7c4c1bdcb07dd8de371d1e654f6ddce7e50bcbfcceb",
    "rustc": "da4ab574eaf49f8a2b98b6ee2a2bf673400893314b41db60685d3c98c4181547",
}
PREFIX = ("apt-get update >/dev/null 2>&1 && apt-get install -y --no-install-recommends "
          "libsodium-dev libzmq3-dev pkg-config >/dev/null 2>&1 && ")
PACKAGE = "aries_vcx_core"
MANIFEST = "aries/aries_vcx_core/Cargo.toml"
TEST = "anoncrypt_pack_then_unpack_recovers_plaintext"
SPEC = "/opt/reviewbench-assets/original-overlay-api.json"
OVERLAY = "/opt/reviewbench-assets/original-vcx-overlay.json"
SOURCES = "/opt/reviewbench-assets/original-vcx-sources.toml"
API_SPEC = "/opt/reviewbench-assets/original-vcx-api.json"
API_TOOL = "/opt/reviewbench-assets/original-vcx-api.py"
TOOL = "/opt/reviewbench-assets/rust-build-tools/overlay_api.py"


def require(value, message):
    if not value:
        raise ContractError("original VCX: " + message)


def requested(ctx, task):
    values = getattr(ctx, "config", {}).get("migration", {}).get(OPTION, {})
    require(isinstance(values, dict) and set(values) <= {ID}, "unsupported original API profile")
    return task.get("id") in values


def original_commands(plan, oracle):
    result = []
    for variant, target in (("start", "oracle_start"), ("golden", "oracle_golden")):
        command = plan[variant + "_command"]
        require(command.startswith(PREFIX + "cargo +1.79.0 ")
                and command == oracle["environment"]["variant_commands"][variant],
                "original apt/Cargo command changed")
        arguments = shlex.split(command[len(PREFIX):])
        require(arguments == ["cargo", "+1.79.0", "test", "-p", PACKAGE, "--features", "askar_wallet",
                              "--test", target, TEST, "--", "--nocapture"],
                "original unlocked Cargo test argv changed")
        result.append(["cargo", *arguments[2:]])
    return result


def original_inputs(ctx, task, oracle, plan, patch):
    require(task["id"] == oracle["id"] == ID and task["repo"] == oracle["repo"] == PROFILE["repo"]
            and task["start_sha"] == oracle["start_sha"] == PROFILE["start"], "task identity changed")
    require(plan["input_index"] == oracle["input_index"] == 69 and canonical_hash(plan) == PROFILE["plan"]
            and plan["toolchain"] == oracle["toolchain"] == "1.79.0", "original plan/compiler changed")
    require(patch["sha256"] == oracle["golden_patch_sha256"] == PROFILE["golden"]
            and file_hash(Path(patch["path"])) == PROFILE["golden"], "original source-only golden changed")
    original_commands(plan, oracle)
    options = ctx.config["migration"][OPTION][ID]
    require(set(options) == {"inputs", "system_image"}, "prepared input/image hashes are required")
    prepared = referenced(options["inputs"], document=True)
    require(prepared["schema_version"] == "reviewbench-original-vcx69-inputs/v1"
            and prepared["task_id"] == ID and prepared["complete"] is True
            and prepared["new_original_SUT_model_judge_calls"] == 0, "unbound original preparation")
    for field, value in (("task", task), ("oracle", oracle), ("plan", plan)):
        require(referenced(prepared[field], document=True) == value, "prepared " + field + " changed")
    require(hashlib.sha256(referenced(prepared["golden"])).hexdigest() == PROFILE["golden"], "prepared golden changed")
    require(oracle["pass_to_pass_tests"] == [TEST] and len(plan["session_files"]) == 2,
            "original test inventory changed")
    harness = {}
    for row, sha in zip(plan["session_files"], PROFILE["harnesses"]):
        data = session_bytes(ctx, plan, oracle, row)
        require(hashlib.sha256(data).hexdigest() == sha and data == referenced(prepared["harness"][row["path"]]),
                "original assertion bytes changed")
        harness[row["path"]] = data
    refs = reference_trees(ctx, task, patch, {"Cargo.lock"})
    snapshots = referenced(prepared["source_preparation"], document=True)
    names = sorted({r["path"] for r in plan["environment_replacements"]})
    require(names == ["Cargo.toml", MANIFEST], "original overlay paths changed")
    inputs = reference_inputs(ctx, task, patch, names)
    overlays, locks = {}, []
    for number, variant in enumerate(("start", "golden")):
        manifests, original_locks = refs[variant]
        require(hashlib.sha256(original_locks["Cargo.lock"]).hexdigest() == PROFILE["seed"],
                "original committed lock seed changed")
        require(manifests == {n: row[1] for n, row in snapshots[variant]["snapshot"].items()
                              if Path(n).name == "Cargo.toml"}, "full original manifest tree changed")
        changed = dict(inputs[variant])
        for index, row in enumerate(plan["environment_replacements"]):
            value = changed[row["path"]].decode()
            require(value.count(row["old"]) == row.get("count", 1), "original overlay count changed")
            changed[row["path"]] = value.replace(row["old"], row["new"], row.get("count", 1)).encode()
            old = oracle["environment_replacements"][variant][index]
            require(old["path"] == row["path"]
                    and hashlib.sha256(changed[row["path"]]).hexdigest() == old["sha256"],
                    "original intermediate environment hash changed")
        overlays[variant] = [{"path": name, "operations": manifest_operations(
            tomllib.loads(inputs[variant][name].decode()), tomllib.loads(changed[name].decode()))} for name in names]
        content = referenced(prepared["retained_locks"][variant])
        require(hashlib.sha256(content).hexdigest() == PROFILE["locks"][number], "retained lock changed")
        lock = tomllib.loads(content.decode())
        locks.append(lock)
        retained = ctx.task_directory(task, oracle, "rust") / variant
        require(file_hash(retained / "Cargo.lock") == PROFILE["locks"][number]
                and entry.core.snapshot(retained) == entry.core.snapshot(
                    Path(prepared["source_preparation"]["path"]).parent / ("prepared-" + variant)),
                "complete retained source/preparation snapshot changed")
        require(len(oracle["runs"][variant]) == len(prepared["original_logs"][variant]) == 3,
                "six original observations missing")
        for original, row in zip(oracle["runs"][variant], prepared["original_logs"][variant]):
            text = referenced(row).decode()
            require(Path(row["path"]).resolve() == (ctx.legacy / "rust" / original["log"]).resolve()
                    and original["ok"] and not original["failed_tests"] and original["passed_tests"] == [TEST]
                    and "test " + TEST + " ... ok" in text, "original successful test evidence changed")
        for role in ("public", "private"):
            row = prepared["observations"][role + "-" + variant]
            require(row["exit_status"] == 0 and row["source_unchanged"], "original graph did not succeed unchanged")
            graph = referenced(row["stdout"], document=True)
            referenced(row["stderr"])
            require(len(graph["packages"]) == len(graph["resolve"]["nodes"]) == len(lock["package"]) == 402
                    and {(p["name"], p["version"], p.get("source")) for p in graph["packages"]} ==
                    {(p["name"], p["version"], p.get("source")) for p in lock["package"]},
                    "original complete graph narrowed or altered")
    require(overlays["start"] == overlays["golden"], "original overlays are not symmetric")
    overlay = referenced(prepared["overlay"], document=True)
    require(overlay == {"schema_version": "reviewbench-cargo-build-overlay/v1", "task_id": ID,
                        "manifests": overlays["start"], "locks": [], "private_members": []},
            "prepared overlay differs from the original environment")
    attest_vendor(prepared, locks)
    require(all(prepared[n]["sha256"] == PROFILE[n] == file_hash(Path(prepared[n]["path"]))
                for n in ("cargo", "rustc")), "prepared compiler changed")
    return prepared, harness


def system_image(ctx, oracle):
    from .rust_lane import IMAGE
    value = referenced(ctx.config["migration"][OPTION][ID]["system_image"], document=True)
    require(value["status"] == "ready_reconstructed_system_image"
            and value["classification"] == "fixed-snapshot reconstruction"
            and value["historical_image_bytes_recovered"] is False
            and value["base_image"] == IMAGE and value["snapshot"] == "20260807T000000Z"
            and value["original_image_reference"] == oracle["environment"]["docker_image"]
            and value["original_apt_prefix"] == PREFIX
            and value["original_packages"] == ["libsodium-dev", "libzmq3-dev", "pkg-config"],
            "original native prefix reconstruction changed")
    require(all(value[k] == 0 for k in ("exit_status", "build_exit_status", "download_exit_status",
                                        "probe_exit_status", "new_original_SUT_model_judge_calls"))
            and value["probe_stdout"].endswith("VCX_ORIGINAL_NATIVE_PREFIX_READY\n"),
            "original native link/init checks did not succeed")
    require(value["runtime_account"]["uid"] == os.getuid() and value["runtime_account"]["gid"] == os.getgid(),
            "prepared runtime account differs from the current nonroot executor")
    packages = referenced(value["packages"], document=True)
    require(packages and len({r["path"] for r in packages}) == len(packages), "missing native package inventory")
    for row in packages:
        path = Path(row["path"])
        require(path.is_absolute() and path.is_file() and not path.is_symlink()
                and path.stat().st_size == row["bytes"] and file_hash(path) == row["sha256"],
                "fixed native package bytes changed")
    return value["image"]


def migrate(ctx, task, oracle):
    from . import rust_lane, rust_overlay, rust_api_entry
    plan = rust_lane.load_plan(ctx, oracle)
    patch = ctx.golden(task, oracle, "rust", ctx.task_directory(task, oracle, "rust"))
    prepared, harness = original_inputs(ctx, task, oracle, plan, patch)
    image, image_audit = ctx.image(system_image(ctx, oracle))
    registry = json.loads(Path(ctx.config["rust_toolchains"]).read_text())
    tool = Path(registry["1.79.0"]["root"])
    require(all(file_hash(tool / ("bin/" + n)) == PROFILE[n] for n in ("cargo", "rustc")), "compiler registry changed")
    assets = []

    def asset(value, target, role="developer", kind="file"):
        assets.append({**value, "target": target, "visibility": role, "kind": kind})
        return {"path": target, "sha256": value["sha256"]}

    asset(rust_lane.compiler(ctx, "1.79.0"), "/opt/reviewbench-assets/rust", kind="archive")
    asset(ctx.bundle(Path(prepared["vendor_root"])), rust_vendor.TARGET, kind="archive")
    sources = asset(ctx.copy_asset(Path(prepared["source_config"]["path"]), "vcx-sources"), SOURCES)
    overlay = asset(ctx.copy_asset(Path(prepared["overlay"]["path"]), "vcx-overlay"), OVERLAY)
    locks = [asset(ctx.copy_asset(Path(prepared["retained_locks"][v]["path"]), "vcx-original-lock"),
                   "/opt/reviewbench-assets/vcx-lock-" + str(i)) for i, v in enumerate(("start", "golden"))]
    commands = original_commands(plan, oracle)
    forms = []
    for command, name in zip(commands, harness):
        target = Path(name).stem
        forms.append({"name": target, "target": target, "harness": name, "command": command})
    private = {
        "schema_version": rust_api_entry.SCHEMA, "task_id": ID,
        "selection": "compile-only-current-candidate; execute-all-compilable-forms; conjunctive-results",
        "forms": forms, "files": [{"path": n, "sha256": hashlib.sha256(b).hexdigest()} for n, b in harness.items()],
        "expected_tests": [TEST], "workdir": ".", "max_log_bytes": 16 * 1024**2,
        "timeout_seconds": plan["timeout_seconds"], "cpus": 4,
        "original_commands": [plan[v + "_command"] for v in ("start", "golden")],
        "preparation_prefix_moved_to_verified_image": PREFIX,
    }
    rust_api_entry.validate(private)
    private_file = ctx.output / "vcx-original-api.json"
    atomic_json(private_file, private)
    private_asset = asset(ctx.copy_asset(private_file, "vcx-api-spec"), API_SPEC, "grader")
    private_tool = asset(ctx.copy_asset(Path(rust_api_entry.__file__), "vcx-api-tool"), API_TOOL, "grader")
    public = {
        "schema_version": entry.SCHEMA, "task_id": ID, "selection": entry.core.SELECTION,
        "package": PACKAGE, "package_manifest": MANIFEST, "locks": locks, "reference_seed_sha256": PROFILE["seed"],
        "cargo": {"path": "/opt/reviewbench-assets/rust/bin/cargo", "sha256": PROFILE["cargo"]},
        "rustc": {"path": "/opt/reviewbench-assets/rust/bin/rustc", "sha256": PROFILE["rustc"]},
        "source_config": sources, "overlay": overlay, "private_spec_sha256": private_asset["sha256"],
        "private_tool_sha256": private_tool["sha256"],
        "public_command": ["check", "--locked", "-p", PACKAGE, "--features", "askar_wallet", "--message-format=json"],
        "cpus": 4, "timeout_seconds": plan["timeout_seconds"], "metadata_timeout_seconds": 120,
        "max_log_bytes": 32 * 1024**2,
    }
    entry.validate(public)
    path = ctx.output / "vcx-public.json"
    atomic_json(path, public)
    asset(ctx.copy_asset(path, "vcx-public-spec"), SPEC)
    tools = rust_overlay.build_tools(ctx, extra_entries={
        "overlay_api.py": Path(entry.__file__), "image_locks.py": Path(__file__).with_name("rust_image_entry.py")})
    asset(tools, rust_overlay.TOOLS, kind="archive")
    bootstrap = []
    for name, content in harness.items():
        target = "/opt/reviewbench-assets/vcx-original-" + Path(name).name
        value = asset(ctx.copy_asset(Path(prepared["harness"][name]["path"]), "vcx-original-test"), target, "grader")
        destination = "/workspace/" + name
        bootstrap.append("mkdir -p " + shlex.quote(str(Path(destination).parent)) + " && cp "
                         + shlex.quote(value["path"]) + " " + shlex.quote(destination))
    prefix = ["python3", TOOL, "--spec", SPEC, "--task-id", ID]
    recipe = RuntimeRecipe(
        task_id=ID, repo=task["repo"], start_sha=task["start_sha"], lane="rust",
        image=image, image_audit=image_audit, workdir="/workspace", cpus=4, memory_mb=8192,
        build_scratch_mb=12288, command_timeout_seconds=plan["timeout_seconds"],
        env={
            "PATH": "/opt/reviewbench-assets/rust/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "RUSTC": "/opt/reviewbench-assets/rust/bin/rustc", "CARGO_HOME": "/opt/reviewbench-cache/cargo",
            "CARGO_TARGET_DIR": "/opt/reviewbench-cache/target", "CARGO_BUILD_JOBS": "4", "CARGO_TERM_COLOR": "never",
            "CARGO_INCREMENTAL": "0", "CARGO_NET_OFFLINE": "true", "CARGO_PROFILE_DEV_DEBUG": "0",
            "CARGO_PROFILE_TEST_DEBUG": "0", "RUSTFLAGS": "--cap-lints=allow", "RUST_BACKTRACE": "1",
            "SKIP_WASM_BUILD": "1", "RUST_BUILD_DOCKER": "1", "GIT_COMMIT": "reviewbench-hard-oracle",
            "GIT_DIRTY": "false", "SOURCE_TIMESTAMP": "0", "TMPDIR": "/workspace/.reviewbench/tmp",
            "PROTOC_INCLUDE": "/usr/include",
        },
        cache_mounts={"cargo": "/opt/reviewbench-cache/cargo", "target": "/opt/reviewbench-cache/target",
                      "rust-build": rust_overlay.BUILD_CACHE},
        bootstrap=["mkdir -p /workspace/.reviewbench/tmp"], dev_checks=[shlex.join(prefix)],
        assets=assets, scratch_paths=[], submodules=ctx.modules(task),
        grading={"multifile_build": True, "immutable_paths": ctx.immutable_tests(task), "golden_patch": patch,
                 "bootstrap": bootstrap, "commands": [{
                     "command": shlex.join([*prefix, "--private", API_SPEC, "--private-tool", API_TOOL]),
                     "parser": "rust-test", "expected_tests": [TEST]}]},
    )
    recipe.validate()
    return recipe
