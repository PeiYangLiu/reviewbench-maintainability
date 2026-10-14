"""Standalone recipe adapter for the two explicitly resolved Ruby candidates.

The only shared-file hook is an early task-ID dispatch from scripted_lane.migrate.
Imports of that module's frozen wrapper utilities are deliberately function-local.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import shlex
import tempfile

from reviewbench_maintainability.agentic import contracts, snapshots
from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, canonical_hash, component, file_hash, repo_path,
)
from .common import relocate
from . import ruby_candidate as ruby

IMAGE_ALIAS = "ruby:3.2-bookworm"
TOOLS = "/opt/reviewbench-assets/scripted-tools"
PRIVATE = "/opt/reviewbench-assets/scripted-tests"
DEPENDENCIES = "/opt/reviewbench-assets/ruby-candidates"
CACHE = "/opt/reviewbench-cache/ruby-candidates"
RUNTIME = "/workspace/.reviewbench/ruby-candidate"
STATE = RUNTIME + "/build"


def engine_binding(engine_file):
    engine_file = Path(engine_file)
    if not engine_file.is_file() or engine_file.is_symlink():
        raise ContractError("Ruby integration requires the current engine-src.txt")
    expected = Path(engine_file.read_text().strip())
    if not expected.is_absolute() or not expected.is_dir():
        raise ContractError("Ruby engine source must be an existing absolute directory")
    expected = expected.resolve()
    modules = {"contracts": Path(contracts.__file__).resolve(), "snapshots": Path(snapshots.__file__).resolve()}
    if any(not path.is_relative_to(expected) for path in modules.values()):
        raise ContractError("Ruby integration imported a different engine than engine-src.txt")
    return {"src": str(expected), "engine_src_file_sha256": file_hash(engine_file),
            "modules_sha256": {name: file_hash(path) for name, path in modules.items()}}


def case_plan(task, case):
    from .scripted_lane import container_payload
    if task["id"] not in ruby.TARGETS or case.get("environment", {}).get("docker") != IMAGE_ALIAS:
        raise ContractError("standalone Ruby adapter requires one of its two original Ruby 3.2 cases")
    command, _, cwd = container_payload(case["setup_host_command"], IMAGE_ALIAS)
    if cwd != "/work":
        raise ContractError("Ruby setup must use the complete candidate root")
    return ruby.setup_plan(task["id"], command, bundler_version=ruby.BUNDLER_VERSION, jobs=2)


def pinned_toolchain(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ContractError("Ruby integration requires the existing pinned toolchain registry")
    entry = json.loads(path.read_text()).get(IMAGE_ALIAS, {})
    if entry.get("status") != "ready":
        raise ContractError("Ruby 3.2 toolchain is not ready; no image build is performed here")
    ruby._image(entry.get("image"))
    return entry


def validate_record(record, *, payload, task, case, plan, start, image, engine):
    if (record.get("status") != "prepared" or record.get("id") != task["id"]
            or record.get("start_sha") != task["start_sha"] or record.get("plan") != plan
            or record.get("config_fingerprint") != canonical_hash(case)
            or record.get("image") != image or record.get("engine") != engine):
        raise ContractError("prepared Ruby task/config/image/engine differs; never re-resolve")
    ruby.validate_prepared(payload, record["ruby_candidate"], plan=plan, start=start, image=image)
    return record


def _dependency_root(ctx):
    migration = ctx.config.get("migration", {})
    configured = migration.get("ruby_candidate_dependency_root") or migration.get("scripted_dependency_root")
    if not configured:
        raise ContractError("Ruby candidate lane requires an explicit prepared dependency root")
    root = Path(configured)
    if not root.is_absolute():
        raise ContractError("prepared Ruby dependency root must be absolute")
    return root


def seed_dependency_commands(dependencies, cache, bootstrap_path):
    commands = ["cp -a " + shlex.quote(dependencies) + "/. " + shlex.quote(cache)]
    if bootstrap_path is not None:
        # File-only assets omit empty directories. An empty bootstrap is still
        # hash-bound in prepared records when Bundler comes from the image.
        commands.append("mkdir -p -- " + shlex.quote(bootstrap_path))
    return commands


def framework_environment(task_id):
    # Minitest 5.15 otherwise starts Etc.nprocessors workers, even for a single
    # selected test. A CPU quota does not hide all host CPUs from that API.
    return {"MT_CPU": "2"} if task_id == ruby.PROMETHEUS else {}


def migrate(ctx, task, oracle):
    from .scripted_lane import container_payload, completion_pattern, relocate_harness_paths

    if task["id"] not in ruby.TARGETS:
        raise ContractError("Ruby candidate lane does not accept another task")
    root = _dependency_root(ctx)
    engine_file = ctx.config.get("migration", {}).get("ruby_candidate_engine_file", root.parent / "engine-src.txt")
    engine = engine_binding(engine_file)
    registry = ctx.config.get("migration", {}).get("toolchain_registry", root.parent / "scripted-toolchains.json")
    pinned = pinned_toolchain(registry)
    case = json.loads((ctx.legacy / "scripted/oracle_config.json").read_text())["tasks"][str(oracle["index"])]
    plan = case_plan(task, case)
    start = ruby.inspect_start(ctx.repo(task), task, plan)
    directory = root / component(task["id"])
    manifest = directory / "preparation.json"
    if manifest.is_symlink() or not manifest.is_file():
        raise ContractError("standalone Ruby preparation record is absent or redirected")
    payload = directory / "payload"
    record = validate_record(json.loads(manifest.read_text()), payload=payload, task=task, case=case,
                             plan=plan, start=start, image=pinned["image"], engine=engine)
    image, audit = ctx.image(pinned["image"])
    if image != record["image"]:
        raise ContractError("Ruby tool image differs from the prepared immutable image")
    raw, private_env, cwd = container_payload(case["test_host_command"], IMAGE_ALIAS)
    if cwd != "/work":
        raise ContractError("original Ruby test wrapper must run at the candidate root")
    task_directory = ctx.task_directory(task, oracle, "scripted")
    tools = ctx.bundle(ctx.legacy / "scripted/tools", ["ruby_coverage.rb"], role="grader")
    dependency = ctx.bundle(payload)
    assets = [
        {**tools, "kind": "archive", "visibility": "grader", "target": TOOLS},
        {**dependency, "kind": "archive", "visibility": "developer", "target": DEPENDENCIES},
    ]
    bootstrap_path = CACHE + "/bootstrap" if record["ruby_candidate"]["bootstrap_sha256"] is not None else None
    environment = {
        "CI": "true", "TMPDIR": "/workspace/.reviewbench/tmp",
        **framework_environment(task["id"]),
        **ruby.runtime_environment(plan, source="/workspace", state=STATE,
                                   bundle_path=CACHE + "/gems", bootstrap_path=bootstrap_path),
    }
    replay = ruby.replay_command(record["ruby_candidate"], source="/workspace", state=STATE,
                                 bundle_path=CACHE + "/gems", bootstrap_path=bootstrap_path, assets=DEPENDENCIES)
    bootstrap = ["mkdir -p /workspace/.reviewbench/tmp",
                 *seed_dependency_commands(DEPENDENCIES, CACHE, bootstrap_path), replay]
    development = ["ruby -c " + shlex.quote("/workspace/" + repo_path(task["locality"]["path"])), replay]
    grading = ["mkdir -p " + RUNTIME + "/artifacts"]
    names = [repo_path(name) for name in case.get("session_files", [])]
    if names:
        with tempfile.TemporaryDirectory(prefix="ruby-harness-", dir=ctx.scratch) as temporary:
            curated = Path(temporary)
            for name in names:
                destination = curated / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(relocate_harness_paths((task_directory / name).read_text()))
            harness = ctx.bundle(curated, names, role="grader")
        assets.append({**harness, "kind": "archive", "visibility": "grader", "target": PRIVATE})
        for name in names:
            destination = "/workspace/" + name
            grading.append("mkdir -p " + shlex.quote(str(Path(destination).parent)) +
                           " && cp " + shlex.quote(PRIVATE + "/" + name) + " " + shlex.quote(destination))
    # The engine restores fixed tests before this refresh. Its view therefore
    # contains the final candidate and original assertions, not start snapshots.
    grading.append(replay)

    def normalize(value):
        value = relocate(value, {"mode": "candidate", "run": "1", "task_dir": RUNTIME, "work": "/workspace"})
        value = value.replace("/oracle-tools", TOOLS).replace("/oracle-task", RUNTIME)
        value = value.replace("/work/.bundle", CACHE + "/gems")
        value = re.sub(r"/work(?=/|$)", "/workspace", value)
        if any(part in value for part in ("{root}", "{repo}", "/data/", "/home/")):
            raise ContractError("Ruby wrapper still contains host-only paths")
        return value

    wrapper_env = {name: normalize(value) for name, value in private_env.items()}
    # Legacy -e BUNDLE_PATH must not supersede the prepared lease-local path.
    wrapper_env.update({key: value for key, value in environment.items()
                        if key.startswith(("BUNDLE", "GEM_")) or key in {"PATH", "MT_CPU"}})
    command = shlex.join(["env", *[key + "=" + value for key, value in sorted(wrapper_env.items())],
                         "bash", "-c", normalize(raw)])
    expected = oracle.get("pass_to_pass_tests") or [task["id"] + "::" + item for item in case["test_selectors"]]
    if not expected:
        raise ContractError("original Ruby fixed selectors are absent")
    immutable = set(ctx.immutable_tests(task))
    # The generic helper recognizes test/, but RSpec's spec/ is equally frozen.
    names = snapshots.git(ctx.repo(task), "ls-tree", "-r", "--name-only", "-z", task["start_sha"]).split(b"\0")
    immutable.update(name.decode() for name in names if name and
                     re.search(r"(^|/)spec(/|$)|(^|/)[^/]+_spec\.rb$", name.decode()))
    recipe = RuntimeRecipe(
        task_id=task["id"], repo=task["repo"], start_sha=task["start_sha"], lane="scripted",
        image=image, image_audit=audit, workdir="/workspace", cpus=2, memory_mb=4096,
        build_scratch_mb=4096, pids_limit=256, command_timeout_seconds=case.get("timeout", 1200),
        assets=assets, submodules=ctx.modules(task), cache_mounts={"ruby-candidates": CACHE},
        scratch_paths=[], env=environment, bootstrap=bootstrap, dev_checks=development,
        grading={"multifile_build": True, "immutable_paths": sorted(immutable),
                 "golden_patch": ctx.golden(task, oracle, "scripted", task_directory),
                 "bootstrap": grading, "commands": [{
                     "command": command, "parser": "regex", "expected_tests": expected,
                     "success_pattern": completion_pattern(ctx, oracle, case),
                 }]},
    )
    recipe.validate()
    return recipe
