"""Original Relay39 standalone integration test and real Redis lifecycle."""
import hashlib
import json
from pathlib import Path
import shlex
import tomllib

from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, atomic_json, canonical_hash, file_hash,
)
from reviewbench_maintainability.agentic.snapshots import tree_files
from . import rust_standalone_service_entry as entry, rust_vendor
from .rust_lock_assets import reference_trees

TASK = "github__getsentry__relay__pull_2745__comment_1419051088__api_design"
REPO = "getsentry/relay"
START = "c6a90e9290d48afea4de85ca9ada708b81ec6fc5"
PLAN = "7fdb625c60c5b4447cab6ed81076e75aa784b4eb99c554b76718dad67f4f0271"
GOLDEN = "61469be164defc743a7b51e34bdbd05291be3e9d5cb5a3c378a069b2c2a422d3"
PUBLIC_SEED = "5addf72ca27445ca71c1fcb0a35cfc97049201db9f61e157bfb8ee5bbd9ab036"
PUBLIC_TARGET = "2a9f8fc7c9fdfb38284a6647d71ddd6798db1aeb06e9d433c3678ea35c1a56b5"
PRIVATE_LOCKS = {
    "start": "7a3333373e68aec9f106c9ca4e3ad184c2b3df59bd977a4a2b86d54e8419b1ab",
    "golden": "cd1f27c0cc97f25cac6cbcd3ecc1d8aad267104cac855247f7ff8794f0d1ae08",
}
OPTION = "rust_original_standalone_services"
SPEC = "/opt/reviewbench-assets/relay-service.json"
PRIVATE = "/opt/reviewbench-assets/relay-service-private.json"
SOURCES = "/opt/reviewbench-assets/relay-service-sources.toml"
TOOL = "/opt/reviewbench-assets/rust-build-tools/standalone_service.py"
TEST = "cardinality_check_persists_a_real_metric_scope_in_redis"
PRIVATE_CWD = "tests/oracle-relay"
TEST_COMMAND = ["test", "--locked", "--test", "oracle_cardinality", TEST, "--", "--nocapture"]
PUBLIC_COMMAND = ["check", "--locked", "-p", "relay-cardinality", "-p", "relay-common",
                  "-p", "relay-metrics", "-p", "relay-redis", "--features",
                  "relay-cardinality/redis,relay-redis/impl"]


def require(value, message):
    if not value:
        raise ContractError("original Relay service: " + message)


def referenced(row, *, document=False):
    path = Path(row["path"])
    require(path.is_absolute() and path.is_file() and not path.is_symlink()
            and path.stat().st_size <= 64 * 1024**2 and file_hash(path) == row["sha256"],
            "input reference/hash changed")
    data = path.read_bytes()
    require(hashlib.sha256(data).hexdigest() == row["sha256"], "input changed while reading")
    return json.loads(data) if document else data


def requested(ctx, task):
    settings = getattr(ctx, "config", {}).get("migration", {}).get(OPTION, {})
    require(isinstance(settings, dict) and not (set(settings) - {TASK}), "unsupported profile")
    return task.get("id") == TASK and TASK in settings


def original_inputs(ctx, task, oracle, plan, patch):
    require((task.get("id"), task.get("repo"), task.get("start_sha")) == (TASK, REPO, START)
            and (oracle.get("id"), oracle.get("repo"), oracle.get("start_sha")) == (TASK, REPO, START),
            "task identity changed")
    require(canonical_hash(plan) == PLAN and plan["toolchain"] == "1.73.0"
            and plan["workdir"] == PRIVATE_CWD and oracle["pass_to_pass_tests"] == [TEST],
            "original plan/toolchain/test changed")
    require(patch["sha256"] == oracle["golden_patch_sha256"] == GOLDEN
            and file_hash(Path(patch["path"])) == GOLDEN, "original source-only golden changed")
    settings = ctx.config["migration"][OPTION][TASK]
    require(set(settings) == {"inputs", "service"}, "profile requires two independently pinned input records")
    prepared = referenced(settings["inputs"], document=True)
    require(prepared["schema_version"] == "reviewbench-original-relay39-inputs/v1"
            and prepared["task_id"] == TASK and prepared["new_SUT_model_judge_calls"] == 0,
            "unbound original graph preparation")
    require(referenced(prepared["task"], document=True) == task
            and referenced(prepared["oracle"], document=True) == oracle
            and referenced(prepared["plan"], document=True) == plan
            and hashlib.sha256(referenced(prepared["golden"])).hexdigest() == GOLDEN,
            "prepared original inputs differ")
    harness = {}
    for row in plan["session_files"]:
        if row["path"].endswith("Cargo.lock"):
            continue
        data = (ctx.legacy / "rust" / row["source"]).read_bytes()
        for variant in ("start", "golden"):
            original = next(x for x in oracle["session_files"][variant] if x["path"] == row["path"])
            require(hashlib.sha256(data).hexdigest() == original["sha256"], "original session bytes changed")
        harness[row["path"]] = data
    require(set(harness) == {PRIVATE_CWD + "/Cargo.toml", PRIVATE_CWD + "/tests/oracle_cardinality.rs"},
            "original harness shape changed")
    interface = tomllib.loads(harness[PRIVATE_CWD + "/Cargo.toml"].decode())
    require(interface["workspace"] == {} and interface["package"]["name"] == "reviewbench-relay-cardinality-oracle"
            and interface["dependencies"] == {
                "relay-cardinality": {"path": "../../relay-cardinality", "features": ["redis"]},
                "relay-common": {"path": "../../relay-common"}, "relay-metrics": {"path": "../../relay-metrics"},
                "relay-redis": {"path": "../../relay-redis", "features": ["impl"]}},
            "original standalone production dependency declarations changed")
    refs = reference_trees(ctx, task, patch, {"Cargo.lock"})
    sources = referenced(prepared["source_reference"], document=True)
    locks = []
    for variant in ("start", "golden"):
        require(oracle["environment"]["variant_commands"][variant] == plan["command"]
                and oracle["environment"]["variant_toolchains"][variant] == "1.73.0"
                and oracle["environment_replacements"][variant] == [], "original environment contract changed")
        manifests, committed = refs[variant]
        require(hashlib.sha256(committed["Cargo.lock"]).hexdigest() == PUBLIC_SEED,
                "original public committed seed changed")
        require(manifests == {n: row[1] for n, row in sources[variant]["snapshot"].items()
                             if Path(n).name == "Cargo.toml"}, "full original manifest tree changed")
        for role in ("public", "private"):
            record = prepared["locks"][variant][role]
            data = referenced(record)
            wanted = PRIVATE_LOCKS[variant] if role == "private" else (
                PUBLIC_SEED if variant == "start" else PUBLIC_TARGET)
            require(hashlib.sha256(data).hexdigest() == wanted, "pinned role-specific lock changed")
            if role == "private":
                original = next(x for x in oracle["session_files"][variant] if x["path"] == PRIVATE_CWD + "/Cargo.lock")
                require(original["sha256"] == wanted, "private lock lacks original Oracle binding")
            lock = tomllib.loads(data.decode())
            locks.append(lock)
            receipt = prepared["observations"][role + "-" + variant]
            require(receipt["exit_status"] == 0 and receipt["source_unchanged"],
                    "public/private metadata failed or changed source")
            graph = referenced(receipt["stdout"], document=True)
            referenced(receipt["stderr"])
            require({(p["name"], p["version"], p.get("source")) for p in graph["packages"]} ==
                    {(p["name"], p["version"], p.get("source")) for p in lock["package"]},
                    "complete role-specific lock graph narrowed")
        require(len(prepared["original_logs"][variant]) == len(oracle["runs"][variant]) == 3,
                "original six test observations missing")
        for log, original in zip(prepared["original_logs"][variant], oracle["runs"][variant]):
            text = referenced(log).decode()
            require(Path(log["path"]).resolve() == (ctx.legacy / "rust" / original["log"]).resolve()
                    and original["ok"] and original["passed_tests"] == [TEST] and not original["failed_tests"]
                    and "test " + TEST + " ... ok" in text, "original Redis persistence observation changed")
    public_provenance = referenced(prepared["public_lock_provenance"], document=True)
    require(public_provenance["historical_private_oracle_lock_claimed"] is False
            and public_provenance["external_package_changes"] == 0,
            "retained public graph was misrepresented as historical private evidence")
    public_start = tomllib.loads(referenced(prepared["locks"]["start"]["public"]).decode())["package"]
    public_golden = tomllib.loads(referenced(prepared["locks"]["golden"]["public"]).decode())["package"]
    keyed = lambda ps: {(p["name"], p["version"], p.get("source")): p for p in ps}
    before, after = keyed(public_start), keyed(public_golden)
    require(before.keys() == after.keys() and all(after[k] == v for k, v in before.items() if k[2]),
            "public development graph changed external dependency identities/edges")
    require({k[0] for k, v in before.items() if after[k] != v} == {"relay-cardinality", "relay-metrics"},
            "public development graph changed unrelated local dependencies")
    expected = rust_vendor.locked_packages(locks)
    audit = referenced(prepared["vendor_audit"], document=True)["packages"]
    require({(p["name"], p["version"], p["source"]): p["package_checksum"] for p in audit} == expected,
            "vendor differs from complete four-lock union")
    root = Path(prepared["vendor_root"])
    require(root.is_absolute() and root.is_dir() and not root.is_symlink(), "vendor missing")
    names = set()
    for row in audit:
        directory = root / row["directory"]
        checks = json.loads((directory / ".cargo-checksum.json").read_text())
        require(checks["package"] == row["package_checksum"] and len(checks["files"]) == row["files"],
                "vendor package inventory changed")
        for name, checksum in checks["files"].items():
            require(file_hash(directory / name) == checksum, "vendored byte checksum changed")
            names.add((directory / name).relative_to(root).as_posix())
        names.add((directory / ".cargo-checksum.json").relative_to(root).as_posix())
    require(set(tree_files(root)) == names, "vendor added/removed files")
    service = referenced(settings["service"], document=True)
    require(service["schema_version"] == "reviewbench-relay39-reconstructed-redis/v1"
            and service["original_image_bytes_recovered"] is False
            and service["historical_binary_version_attested"] is False
            and service["snapshot"] == "20260805T000000Z", "service reconstruction provenance changed")
    build = referenced(service["build"], document=True)
    require(build["exit_status"] == 0 and build["image"] == service["image"]
            and build["snapshot"] == service["snapshot"]
            and build["historical_redis_binary_version_attested"] is False,
            "pinned reconstructed service build changed")
    controls = referenced(service["controls"], document=True)
    require([controls[n]["exit_status"] for n in ("BASELINE", "MODIFIED", "ROLLBACK")] == [127, 0, 127]
            and controls["new_SUT_model_judge_calls"] == 0, "real service control/rollback missing")
    binaries = referenced(service["binary_observation"], document=True)
    require(binaries["exit_status"] == 0, "service executable observation failed")
    for name, row in service["service_binaries"].items():
        require(name + " " + row["path"] in binaries["stdout"]
                and row["sha256"] + "  " + row["path"] in binaries["stdout"], "service binary hash changed")
    for package in referenced(service["packages"], document=True):
        require(file_hash(Path(package["path"])) == package["sha256"], "pinned service package changed")
    return prepared, service, harness


def migrate(ctx, task, oracle):
    from . import rust_lane, rust_overlay
    plan = rust_lane.load_plan(ctx, oracle)
    patch = ctx.golden(task, oracle, "rust", ctx.task_directory(task, oracle, "rust"))
    prepared, service, harness = original_inputs(ctx, task, oracle, plan, patch)
    registry = json.loads(Path(ctx.config["rust_toolchains"]).read_text())
    compiler = Path(registry["1.73.0"]["root"])
    require(file_hash(compiler / "bin/cargo") == prepared["cargo"]["sha256"]
            and file_hash(compiler / "bin/rustc") == prepared["rustc"]["sha256"], "original compiler changed")
    compiler_asset = rust_lane.compiler(ctx, "1.73.0")
    vendor = ctx.bundle(Path(prepared["vendor_root"]))
    source = ctx.copy_asset(Path(prepared["source_config"]["path"]), "relay-source-config")
    image, audit = ctx.image(service["image"])
    assets = [
        {**compiler_asset, "kind": "archive", "visibility": "developer", "target": "/opt/reviewbench-assets/rust"},
        {**vendor, "kind": "archive", "visibility": "developer", "target": rust_vendor.TARGET},
        {**source, "kind": "file", "visibility": "developer", "target": SOURCES},
    ]
    locks = {"public": [], "private": []}
    for role in locks:
        for variant in ("start", "golden"):
            original = prepared["locks"][variant][role]
            asset = ctx.copy_asset(Path(original["path"]), "relay-lock")
            target = "/opt/reviewbench-assets/relay-lock-" + role + "-" + str(len(locks[role]))
            assets.append({**asset, "kind": "file", "visibility": "developer", "target": target})
            locks[role].append({"path": target, "sha256": asset["sha256"]})
    public = {
        "schema_version": entry.SCHEMA, "task_id": TASK, "selection": entry.core.SELECTION,
        "cargo": {"path": "/opt/reviewbench-assets/rust/bin/cargo", "sha256": prepared["cargo"]["sha256"]},
        "rustc": {"path": "/opt/reviewbench-assets/rust/bin/rustc", "sha256": prepared["rustc"]["sha256"]},
        "source_config": {"path": SOURCES, "sha256": source["sha256"]}, "locks": locks,
        "toolchain": "1.73.0", "public_seed_sha256": PUBLIC_SEED, "private_workdir": PRIVATE_CWD,
        "public_command": PUBLIC_COMMAND, "cpus": 4, "timeout_seconds": 3600,
        "metadata_timeout_seconds": 120, "max_log_bytes": 16 * 1024**2,
    }
    private = {
        "schema_version": entry.PRIVATE_SCHEMA, "task_id": TASK,
        "session_files": [{"path": n, "sha256": hashlib.sha256(data).hexdigest()} for n, data in sorted(harness.items())],
        "test_path": PRIVATE_CWD + "/tests/oracle_cardinality.rs", "test_command": TEST_COMMAND,
        "original_shell": plan["command"], "original_shell_sha256": hashlib.sha256(plan["command"].encode()).hexdigest(),
        "expected_tests": [TEST], "service_binaries": service["service_binaries"],
    }
    entry.validate(public, private)
    for name, value, target, visibility in (("public", public, SPEC, "developer"),
                                           ("private", private, PRIVATE, "grader")):
        path = ctx.output / ("relay-" + name + ".json")
        atomic_json(path, value)
        assets.append({**ctx.copy_asset(path, "relay-spec"), "kind": "file", "visibility": visibility, "target": target})
    tools = rust_overlay.build_tools(ctx, extra_entries={
        "image_locks.py": Path(__file__).with_name("rust_image_entry.py"),
        "standalone_service.py": Path(__file__).with_name("rust_standalone_service_entry.py"),
    })
    assets.append({**tools, "kind": "archive", "visibility": "developer", "target": rust_overlay.TOOLS})
    original_tests = ctx.output / "relay-private-files"
    grade = []
    for name, data in harness.items():
        path = original_tests / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        destination = "/workspace/" + name
        origin = "/opt/reviewbench-assets/relay-private-files/" + name
        grade.append("mkdir -p " + shlex.quote(str(Path(destination).parent))
                     + " && if [ -e " + shlex.quote(destination) + " ] || [ -L " + shlex.quote(destination)
                     + " ]; then cmp -s " + shlex.quote(origin) + " " + shlex.quote(destination)
                     + "; else cp " + shlex.quote(origin) + " " + shlex.quote(destination) + "; fi")
    assets.append({**ctx.bundle(original_tests, role="grader"), "kind": "archive", "visibility": "grader",
                   "target": "/opt/reviewbench-assets/relay-private-files"})
    prefix = ["python3", TOOL, "--spec", SPEC, "--task-id", TASK]
    recipe = RuntimeRecipe(
        task_id=TASK, repo=REPO, start_sha=START, lane="rust", image=image, image_audit=audit,
        workdir="/workspace", cpus=4, memory_mb=8192, build_scratch_mb=24576, command_timeout_seconds=3600,
        env={"PATH": "/opt/reviewbench-assets/rust/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
             "RUSTC": "/opt/reviewbench-assets/rust/bin/rustc", "CARGO_HOME": "/opt/reviewbench-cache/cargo",
             "CARGO_TARGET_DIR": "/opt/reviewbench-cache/target", "CARGO_BUILD_JOBS": "4",
             "CARGO_TERM_COLOR": "never", "CARGO_INCREMENTAL": "0", "CARGO_NET_OFFLINE": "true",
             "CARGO_PROFILE_DEV_DEBUG": "0", "CARGO_PROFILE_TEST_DEBUG": "0", "RUSTFLAGS": "--cap-lints=allow",
             "RUST_BACKTRACE": "1", "SKIP_WASM_BUILD": "1", "RUST_BUILD_DOCKER": "1",
             "GIT_COMMIT": "reviewbench-hard-oracle", "GIT_DIRTY": "false", "SOURCE_TIMESTAMP": "0",
             "TMPDIR": "/workspace/.reviewbench/tmp", "PROTOC_INCLUDE": "/usr/include"},
        cache_mounts={"cargo": "/opt/reviewbench-cache/cargo", "target": "/opt/reviewbench-cache/target",
                      "rust-build": rust_overlay.BUILD_CACHE},
        bootstrap=["mkdir -p /workspace/.reviewbench/tmp"], dev_checks=[shlex.join(prefix)],
        assets=assets, scratch_paths=[], submodules=ctx.modules(task),
        grading={"multifile_build": True, "immutable_paths": ctx.immutable_tests(task), "golden_patch": patch,
                 "bootstrap": grade, "commands": [{"command": shlex.join([*prefix, "--private", PRIVATE]),
                                                   "parser": "rust-test", "expected_tests": [TEST]}]},
    )
    recipe.validate()
    return recipe
