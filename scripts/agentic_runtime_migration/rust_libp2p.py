"""Bind the original libp2p codec environment forms and their declared workspace locks."""
import hashlib
import json
from pathlib import Path
import shlex
import tomllib

from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, atomic_json, canonical_hash, file_hash,
)
from . import rust_manifest_variants_entry as entry, rust_vendor
from .rust_build_entry import manifest_operations
from .rust_lock_assets import reference_trees
from .rust_locked_workspace import referenced
from .rust_manifest_service import attest_vendor
from .rust_overlay import session_bytes, reference_inputs, lock_prepare

ID = "github__libp2p__rust-libp2p__pull_4811__comment_1388724471__complexity"
OPTION = "rust_original_manifest_variants"
PROFILE = {
    "repo": "libp2p/rust-libp2p", "start": "838b14f96357d130f31641a32ce0d6016ea3745c",
    "plan": "8691b8875f5ff21bc8b33e1bfcbf75af2763b87c33c8766ff78f982e9466077a",
    "golden": "555ce810aff6e34cb84e72f6f86ebad02d82e586558b0b669f1233180d4d82ed",
    "seed": "d7dea8e9910809341f23d8963df02f2d32acfd17caa4a99fcaae0d3339d7e940",
    "locks": ["d9fa7b1aab9272595644763107dc4a9120283d1b63bba78d30e508fa483b8129",
              "cd9d6d5159819daa0ae5cc16e8672ecafcbf564cf744e2c64fcc7dff54cb3829"],
    "harness": "1d1621fa8d51c7f633085d46d66cac891dba6a07af21e1c5c92fb93b44367470",
    "cargo": "b34acb7e9d9da98cac5882b98d47ddb7654c8650ae82905ffb1c33c1a00b6a0f",
    "rustc": "464d1dc87e7edf5a740af344dcdffb1f8e4cbf958367514f47cebdc0c4e72e17",
}
MANIFEST = "protocols/gossipsub/Cargo.toml"
MANIFESTS = ["misc/quick-protobuf-codec/Cargo.toml", MANIFEST]
PACKAGE = "libp2p-gossipsub"
HARNESS = "protocols/gossipsub/tests/oracle_session.rs"
TEST = "public_behaviour_subscribes_and_unsubscribes_a_topic"
SPEC = "/opt/reviewbench-assets/manifest-variants.json"
PRIVATE = "/opt/reviewbench-assets/manifest-variants-private.json"
SOURCES = "/opt/reviewbench-assets/manifest-variants-sources.toml"
TOOL = "/opt/reviewbench-assets/rust-build-tools/manifest_variants.py"
SESSION_LOCK_PATHS = ["protocols/gossipsub/tests/oracle." + variant + ".Cargo.lock"
                      for variant in ("start", "golden")]


def require(value, message):
    if not value:
        raise ContractError("original libp2p: " + message)


def original_inputs(ctx, task, oracle, plan, patch):
    require(task["id"] == oracle["id"] == ID and task["repo"] == oracle["repo"] == PROFILE["repo"]
            and task["start_sha"] == oracle["start_sha"] == PROFILE["start"], "task identity changed")
    require(canonical_hash(plan) == PROFILE["plan"] and plan["input_index"] == oracle["input_index"] == 58,
            "original plan changed")
    require(patch["sha256"] == oracle["golden_patch_sha256"] == PROFILE["golden"]
            and file_hash(Path(patch["path"])) == PROFILE["golden"], "source-only golden changed")
    prepared = referenced(ctx.config["migration"][OPTION][ID], document=True)
    require(prepared["schema_version"] == "reviewbench-original-multi-manifest-inputs/v1"
            and prepared["task_id"] == ID and prepared["complete"] is True
            and prepared["new_resolutions"] == prepared["new_original_SUT_model_judge_calls"] == 0,
            "unbound original graph preparation")
    for name, value in (("task", task), ("oracle", oracle), ("plan", plan)):
        require(referenced(prepared[name], document=True) == value, "prepared " + name + " changed")
    require(hashlib.sha256(referenced(prepared["golden"])).hexdigest() == PROFILE["golden"], "prepared golden changed")
    require(oracle["pass_to_pass_tests"] == [TEST] and len(plan["session_files"]) == 3,
            "original test/session inventory changed")
    session = {r["path"]: session_bytes(ctx, plan, oracle, r) for r in plan["session_files"]}
    for name, data in session.items():
        require(data == referenced(prepared["original_session_files"][name]), "original session bytes changed")
    require(hashlib.sha256(session[HARNESS]).hexdigest() == PROFILE["harness"], "original assertion changed")
    references = reference_trees(ctx, task, patch, {"Cargo.lock"})
    snapshots = referenced(prepared["source_preparation"], document=True)
    initial = reference_inputs(ctx, task, patch, MANIFESTS)
    require(initial["start"] == initial["golden"], "source-only golden unexpectedly changes the manifest seed")
    effective, locks = {}, []
    for number, variant in enumerate(("start", "golden")):
        require(oracle["environment"]["variant_toolchains"][variant] == plan["toolchain"] == "1.73.0"
                and shlex.split(oracle["environment"]["variant_commands"][variant]) ==
                shlex.split(plan["command"]) ==
                ["cargo", "+1.73.0", "test", "--locked", "-p", PACKAGE, "--test", "oracle_session", "--", "--nocapture"],
                "original compiler/command changed")
        manifests, seed = references[variant]
        require(hashlib.sha256(seed["Cargo.lock"]).hexdigest() == PROFILE["seed"],
                "original source lock seed changed")
        require(manifests == {n: r[1] for n, r in snapshots[variant]["snapshot"].items()
                              if Path(n).name == "Cargo.toml"}, "full original manifest tree changed")
        require(entry.core.snapshot(Path(snapshots[variant]["source"])) == snapshots[variant]["snapshot"],
                "complete prepared source changed")
        data = dict(initial[variant])
        for index, row in enumerate(plan["environment_replacements"]):
            old, new = row.get(variant + "_old", row.get("old")), row.get(variant + "_new", row.get("new"))
            content = data[row["path"]].decode()
            require(content.count(old) == row.get("count", 1), "original overlay occurrence count changed")
            data[row["path"]] = content.replace(old, new, row.get("count", 1)).encode()
            evidence = oracle["environment_replacements"][variant][index]
            require(evidence["path"] == row["path"]
                    and hashlib.sha256(data[row["path"]]).hexdigest() == evidence["sha256"],
                    "original intermediate environment hash changed")
        for name in MANIFESTS:
            require(data[name] == referenced(prepared["effective_manifests"][variant][name]),
                    "effective original manifest changed")
        effective[variant] = {n: tomllib.loads(b.decode()) for n, b in data.items()}
        operation = lock_prepare(plan[variant + "_prepare_command"])
        content = referenced(prepared["locks"][variant])
        require(operation["path"] == "Cargo.lock" and operation["sha256"] == PROFILE["locks"][number]
                and session[operation["source"]] == content and hashlib.sha256(content).hexdigest() ==
                PROFILE["locks"][number], "original checksum-verified lock copy changed")
        lock = tomllib.loads(content.decode())
        locks.append(lock)
        require(len(lock["package"]) == (588 if variant == "start" else 589), "original graph size changed")
        require(len(oracle["runs"][variant]) == len(prepared["original_logs"][variant]) == 3,
                "six original observations missing")
        for original, row in zip(oracle["runs"][variant], prepared["original_logs"][variant]):
            text = referenced(row).decode()
            require(Path(row["path"]).resolve() == (ctx.legacy / "rust" / original["log"]).resolve()
                    and original["ok"] and not original["failed_tests"] and original["passed_tests"] == [TEST]
                    and "test " + TEST + " ... ok" in text, "original behavior evidence changed")
        for role in ("public", "private"):
            row = prepared["offline_observations"][role + "-" + variant]
            require(row["exit_status"] == 0 and row["source_unchanged"], "original offline graph check failed")
            graph = referenced(row["stdout"], document=True)
            referenced(row["stderr"])
            require(len(graph["packages"]) == len(graph["resolve"]["nodes"]) == len(lock["package"])
                    and {(p["name"], p["version"], p.get("source")) for p in graph["packages"]} ==
                    {(p["name"], p["version"], p.get("source")) for p in lock["package"]},
                    "original complete graph was narrowed")
    originals = {n: tomllib.loads(initial["start"][n].decode()) for n in MANIFESTS}
    for name in MANIFESTS:
        require(referenced(prepared["original_manifests"][name]) == initial["start"][name], "original manifest seed changed")
    common = [{"path": n, "operations": manifest_operations(originals[n], effective["start"][n])} for n in MANIFESTS]
    forms = {v: [{"path": n, "operations": manifest_operations(effective["start"][n], effective[v][n])}
                 for n in MANIFESTS] for v in ("start", "golden")}
    require(referenced(prepared["semantic_forms"], document=True) == {"task_id": ID, "common": common, "forms": forms},
            "semantic original forms changed")
    require(all(op["key"][:1] == ["dev-dependencies"] for row in common for op in row["operations"]),
            "common environment operation changes production inputs")
    for row in forms["golden"]:
        require(all(op["key"] in [["dependencies", "asynchronous-codec"], ["dependencies", "asynchronous-codec", "version"],
                                  ["dependencies", "unsigned-varint", "version"]] for op in row["operations"]),
                "original dependency form alters unrelated production fields")
    attest_vendor(prepared, locks)
    require(all(prepared[n]["sha256"] == PROFILE[n] == file_hash(Path(prepared[n]["path"]))
                for n in ("cargo", "rustc")), "original compiler changed")
    return prepared, session


def requested(ctx, task):
    settings = getattr(ctx, "config", {}).get("migration", {}).get(OPTION, {})
    require(isinstance(settings, dict) and set(settings) <= {ID}, "unsupported original form profile")
    return task.get("id") in settings


def session_sources(session, forms, private_source):
    """Reuse declared public lock bytes; only grading installs session copies.

    A dependency lock already exposed to the developer is not a private test
    asset. Giving the same bytes both roles makes the public-image isolation
    check correctly reject the recipe. The harness remains exclusively private.
    """
    require(set(session) == {HARNESS, *SESSION_LOCK_PATHS}, "original session inventory changed")
    require(len(forms) == 2 and [f["lock"]["sha256"] for f in forms] == PROFILE["locks"],
            "public original session lock identities changed")
    require(hashlib.sha256(session[HARNESS]).hexdigest() == PROFILE["harness"],
            "original assertion changed")
    sources = {}
    for name, form, digest in zip(SESSION_LOCK_PATHS, forms, PROFILE["locks"]):
        require(hashlib.sha256(session[name]).hexdigest() == digest, "original session lock changed")
        sources[name] = dict(form["lock"])
    sources[HARNESS] = private_source(HARNESS)
    require(sources[HARNESS]["sha256"] == PROFILE["harness"], "private assertion asset changed")
    return sources


def migrate(ctx, task, oracle):
    from . import rust_lane, rust_overlay
    plan = rust_lane.load_plan(ctx, oracle)
    patch = ctx.golden(task, oracle, "rust", ctx.task_directory(task, oracle, "rust"))
    prepared, session = original_inputs(ctx, task, oracle, plan, patch)
    image, image_audit = ctx.image(rust_lane.IMAGE)
    registry = json.loads(Path(ctx.config["rust_toolchains"]).read_text())
    compiler = Path(registry["1.73.0"]["root"])
    require(all(file_hash(compiler / ("bin/" + n)) == PROFILE[n] for n in ("cargo", "rustc")), "compiler registry changed")
    assets = []

    def asset(value, target, role="developer", kind="file"):
        assets.append({**value, "target": target, "visibility": role, "kind": kind})
        return {"path": target, "sha256": value["sha256"]}

    asset(rust_lane.compiler(ctx, "1.73.0"), "/opt/reviewbench-assets/rust", kind="archive")
    asset(ctx.bundle(Path(prepared["vendor_root"])), rust_vendor.TARGET, kind="archive")
    sources = asset(ctx.copy_asset(Path(prepared["source_config"]["path"]), "manifest-variant-sources"), SOURCES)
    semantic = asset(ctx.copy_asset(Path(prepared["semantic_forms"]["path"]), "manifest-variant-operations"),
                     "/opt/reviewbench-assets/manifest-variant-operations.json")
    originals = [{"path": n, "reference": asset(ctx.copy_asset(Path(prepared["original_manifests"][n]["path"]), "manifest-seeds"),
                    "/opt/reviewbench-assets/manifest-seed-" + str(i))} for i, n in enumerate(MANIFESTS)]
    forms = []
    for i, variant in enumerate(("start", "golden")):
        manifests = [{"path": n, "asset": asset(ctx.copy_asset(
            Path(prepared["effective_manifests"][variant][n]["path"]), "original-manifest-forms"),
            f"/opt/reviewbench-assets/manifest-form-{i}-{j}")} for j, n in enumerate(MANIFESTS)]
        lock = asset(ctx.copy_asset(Path(prepared["locks"][variant]["path"]), "original-manifest-locks"),
                     "/opt/reviewbench-assets/manifest-lock-" + str(i))
        forms.append({"name": "codec-06-varint-07" if i == 0 else "codec-07-varint-08", "manifests": manifests, "lock": lock})
    public = {
        "schema_version": entry.SCHEMA, "task_id": ID, "selection": entry.SELECTION,
        "package": PACKAGE, "package_manifest": MANIFEST, "manifests": originals, "forms": forms,
        "slots": [{"path": p, "key": ["dependencies", n]} for p in MANIFESTS for n in ("asynchronous-codec", "unsigned-varint")],
        "semantic_forms": semantic, "reference_seed_sha256": PROFILE["seed"], "source_config": sources,
        "cargo": {"path": "/opt/reviewbench-assets/rust/bin/cargo", "sha256": PROFILE["cargo"]},
        "rustc": {"path": "/opt/reviewbench-assets/rust/bin/rustc", "sha256": PROFILE["rustc"]},
        "public_command": ["check", "--locked", "-p", PACKAGE, "--message-format=json"],
        "cpus": 4, "timeout_seconds": 2400, "metadata_timeout_seconds": 120, "max_log_bytes": 32 * 1024**2,
    }
    private = {
        "task_id": ID, "package_manifest": MANIFEST, "harness": {"path": HARNESS, "sha256": PROFILE["harness"]},
        "command": shlex.split(plan["command"])[2:], "expected_tests": [TEST],
        "session_files": [{"path": n, "sha256": hashlib.sha256(b).hexdigest()} for n, b in session.items()],
    }
    entry.validate(public, private)
    for name, value, target, role in (("public", public, SPEC, "developer"), ("private", private, PRIVATE, "grader")):
        path = ctx.output / ("manifest-variants-" + name + ".json")
        atomic_json(path, value)
        asset(ctx.copy_asset(path, "manifest-variant-specs"), target, role)
    tools = rust_overlay.build_tools(ctx, extra_entries={
        "manifest_variants.py": Path(entry.__file__), "image_locks.py": Path(__file__).with_name("rust_image_entry.py")})
    asset(tools, rust_overlay.TOOLS, kind="archive")
    bootstrap = []
    session_assets = session_sources(session, forms, lambda name: asset(
        ctx.copy_asset(Path(prepared["original_session_files"][name]["path"]), "original-libp2p-session"),
        "/opt/reviewbench-assets/original-session-" + Path(name).name, "grader"))
    for name in session:
        value = session_assets[name]
        destination = "/workspace/" + name
        bootstrap.append("mkdir -p " + shlex.quote(str(Path(destination).parent)) + " && cp "
                         + shlex.quote(value["path"]) + " " + shlex.quote(destination))
    prefix = ["python3", TOOL, "--spec", SPEC, "--task-id", ID]
    recipe = RuntimeRecipe(
        task_id=ID, repo=task["repo"], start_sha=task["start_sha"], lane="rust",
        image=image, image_audit=image_audit, workdir="/workspace", cpus=4, memory_mb=8192,
        build_scratch_mb=12288, command_timeout_seconds=2400,
        env={"PATH": "/opt/reviewbench-assets/rust/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
             "RUSTC": "/opt/reviewbench-assets/rust/bin/rustc", "CARGO_HOME": "/opt/reviewbench-cache/cargo",
             "CARGO_TARGET_DIR": "/opt/reviewbench-cache/target", "CARGO_NET_OFFLINE": "true", "CARGO_BUILD_JOBS": "4",
             "CARGO_INCREMENTAL": "0", "CARGO_TERM_COLOR": "never", "CARGO_PROFILE_DEV_DEBUG": "0",
             "CARGO_PROFILE_TEST_DEBUG": "0", "RUSTFLAGS": "--cap-lints=allow", "RUST_BACKTRACE": "1",
             "HOME": "/tmp", "TMPDIR": "/workspace/.reviewbench/tmp"},
        cache_mounts={"cargo": "/opt/reviewbench-cache/cargo", "target": "/opt/reviewbench-cache/target",
                      "rust-build": rust_overlay.BUILD_CACHE},
        bootstrap=["mkdir -p /workspace/.reviewbench/tmp"], dev_checks=[shlex.join(prefix)],
        assets=assets, scratch_paths=[], submodules=ctx.modules(task),
        grading={"multifile_build": True, "immutable_paths": ctx.immutable_tests(task), "golden_patch": patch,
                 "bootstrap": bootstrap, "commands": [{"command": shlex.join([*prefix, "--private", PRIVATE]),
                 "parser": "rust-test", "expected_tests": [TEST]}]},
    )
    recipe.validate()
    return recipe
