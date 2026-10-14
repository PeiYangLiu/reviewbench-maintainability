#!/usr/bin/env python3
"""Gated, frozen-controller preparation of Kirby's explicitly derived test tools.

No model calls or calibration. The only source execution is an isolated
current-source build/real PHPUnit --version probe, not the frozen test selector.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

sys.dont_write_bytecode = True
from agentic_runtime_migration import composer_test_tools as tools
from agentic_runtime_migration import composer_test_tools_runtime as runtime

GIB = 1024**3
IMAGE_ALIAS = "rb-scripted-php84-cov"
CONTROLLER_SCHEMA = "reviewbench-kirby-preparation-controller/v1"
REQUIRED_FILES = (
    "scripts/prepare_agentic_composer_test_tools.py",
    "scripts/agentic_runtime_migration/__init__.py",
    "scripts/agentic_runtime_migration/common.py",
    "scripts/agentic_runtime_migration/composer_candidate.py",
    "scripts/agentic_runtime_migration/composer_test_tools.py",
    "scripts/agentic_runtime_migration/composer_test_tools_runtime.py",
    "scripts/agentic_runtime_migration/composer_test_tools_lane.py",
)


def atomic_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    pending.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    pending.replace(path)


def sha(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def configure_engine(pointer: Path) -> Path:
    text = pointer.read_text().strip()
    engine = Path(text)
    if not engine.is_absolute() or not (engine / "reviewbench_maintainability/agentic/execution.py").is_file():
        raise tools.TestToolsError("engine-src pointer must select a real absolute engine src")
    engine = engine.resolve()
    sys.path.insert(0, str(engine))
    os.environ["REVIEWBENCH_ENGINE_SRC"] = str(engine)
    from reviewbench_maintainability.agentic import contracts
    if not Path(contracts.__file__).resolve().is_relative_to(engine):
        raise tools.TestToolsError("a different engine is already imported")
    return engine


def verify_controller(path: Path, engine: Path, registry_entry: dict, *,
                      running_script: Path | None = None) -> dict:
    """The real CLI runs only the exact source snapshot that passed focused tests."""
    path = path.resolve()
    root = path.parent
    data = json.loads(path.read_text())
    if (data.get("schema") != CONTROLLER_SCHEMA or data.get("engine_src") != str(engine.resolve())
            or data.get("toolchain_entry_sha256") != tools._fingerprint(registry_entry)
            or not set(REQUIRED_FILES) <= data.get("files", {}).keys()):
        raise tools.TestToolsError("frozen controller/engine/toolchain binding differs")
    for name, expected in data["files"].items():
        tools.cc._relative(name)
        source = root / name
        if source.is_symlink() or not source.is_file() or sha(source) != expected:
            raise tools.TestToolsError("frozen controller source changed: " + name)
    tests = data.get("tests", {})
    if (type(tests.get("exit_status")) is not int or tests["exit_status"] != 0
            or not tests.get("command") or sha(root / tests["log"]) != tests.get("log_sha256")):
        raise tools.TestToolsError("frozen controller lacks retained passing focused tests")
    actual = Path(__file__) if running_script is None else running_script
    if actual.resolve() != root / REQUIRED_FILES[0]:
        raise tools.TestToolsError("execute the frozen controller copy, not its mutable workspace")
    return {"path": str(path), "sha256": sha(path), "engine_src": str(engine), "tests": tests}


class ContainerRunner:
    """One sequential container at a time, within an already acquired shared lease."""

    def __init__(self, *, services, lease, image, stage, evidence, cache=None,
                 timeout_seconds=1200, extra_mounts=(), network="bridge"):
        self.services, self.lease, self.image = services, lease, image
        self.stage, self.evidence = Path(stage).resolve(), Path(evidence).resolve()
        self.cache = Path(cache).resolve() if cache else None
        self.deadline = time.monotonic() + timeout_seconds
        self.extra_mounts, self.network = list(extra_mounts), network
        self.floor = max(80 * GIB, services.gate.limits.min_free_disk_bytes)
        self.evidence.mkdir(parents=True, exist_ok=True)

    def environment(self):
        return {
            "HOME": "/tmp", "CI": "true", "COMPOSER_HOME": "/tmp/composer-home",
            "COMPOSER_CACHE_DIR": "/composer-cache" if self.cache else "/tmp/composer-cache",
            "COMPOSER_CACHE_READ_ONLY": "1", "COMPOSER_MAX_PARALLEL_HTTP": "2",
            "COMPOSER_MAX_PARALLEL_PROCESSES": "2", "MAKEFLAGS": "-j2",
            **tools.ENVIRONMENT,
        }

    def argv(self, name, command, environment, *, stage=None, mounts=(), network=None,
             workdir="/workspace", user=None, rootfs_audit=False):
        from agentic_runtime_migration.common import bounded_cpu_set
        argv = [
            "docker", "run", "--rm", "--pull=never", "--name", name, "--init", "--read-only",
            "--network", "none" if rootfs_audit else (network or self.network),
            "--user", "0:0" if rootfs_audit else (user or f"{os.getuid()}:{os.getgid()}"),
            "--cpus", "2", "--cpuset-cpus", bounded_cpu_set(2, self.lease["lease"]),
            "--memory", "4096m", "--pids-limit", "256", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--label", "reviewbench.resource-lease=" + self.lease["lease"],
        ]
        if rootfs_audit:
            # Inspect the IMAGE, not inaccessible host mounts. apt's _apt-owned
            # 0700 directories require DAC read access even for uid 0 after
            # cap-drop=ALL. The image remains read-only/network-disabled.
            argv += ["--cap-add=DAC_OVERRIDE", "-w", "/"]
        else:
            argv += ["--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=512m",
                     "--mount", f"type=bind,source={Path(stage or self.stage).resolve()},target=/workspace", "-w", workdir]
            if self.cache:
                argv += ["--mount", f"type=bind,source={self.cache},target=/composer-cache,readonly"]
            for source, target, readonly in [*self.extra_mounts, *mounts]:
                argv += ["--mount", f"type=bind,source={Path(source).resolve()},target={target}" + (",readonly" if readonly else "")]
        for key, value in sorted(environment.items()):
            if not re.fullmatch(r"[A-Za-z_]\w*", key) or not isinstance(value, str):
                raise tools.TestToolsError("invalid container environment")
            argv += ["-e", key + "=" + value]
        argv += ["--entrypoint", command[0], self.image, *command[1:]]
        return argv

    def run(self, phase, command, *, environment=None, stage=None, mounts=(), network=None,
            workdir="/workspace", user=None, rootfs_audit=False):
        from reviewbench_maintainability.agentic.resources import CURRENT_JOB
        if phase != tools.cc._relative(phase) or "/" in phase:
            raise tools.TestToolsError("invalid preparation phase name")
        directory = self.evidence / phase
        directory.mkdir(exist_ok=False)
        env = {**self.environment(), **(environment or {})}
        name = "rb-kirby-tools-" + uuid.uuid4().hex[:18]
        if rootfs_audit:
            workdir = "/"
        argv = self.argv(name, list(command), env, stage=stage, mounts=mounts, network=network,
                         workdir=workdir, user=user, rootfs_audit=rootfs_audit)
        record = {"argv": list(command), "environment": env, "workdir": workdir,
                  "image": self.image, "container_argv": argv, "container_command": shlex.join(argv),
                  "lease": self.lease["lease"], "phase": phase}
        atomic_json(directory / "execution.json", record)
        started, error, process, cleanup = time.monotonic(), None, None, []
        try:
            if time.monotonic() >= self.deadline:
                raise tools.TestToolsError("Kirby preparation exceeded its time bound")
            if shutil.disk_usage(self.services.scratch).free < self.floor:
                raise tools.TestToolsError("Kirby preparation reached the 80-GiB/shared disk floor")
            job = CURRENT_JOB.get()
            if not job or job.get("lease") != self.lease["lease"]:
                raise tools.TestToolsError("container requires its live shared resource lease")
            job["mark_container_use"]()
            with (directory / "stdout.log").open("wb") as out, (directory / "stderr.log").open("wb") as err:
                process = subprocess.Popen(argv, stdout=out, stderr=err)
                while process.poll() is None:
                    if time.monotonic() >= self.deadline:
                        raise tools.TestToolsError("Kirby preparation exceeded its time bound")
                    if shutil.disk_usage(self.services.scratch).free < self.floor:
                        raise tools.TestToolsError("Kirby preparation reached the 80-GiB/shared disk floor")
                    time.sleep(.25)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            if process is not None and process.poll() is None:
                for cleanup_argv in (["docker", "stop", "-t", "5", name], ["docker", "rm", "-f", name]):
                    try:
                        result = subprocess.run(cleanup_argv, capture_output=True, text=True, timeout=20)
                        cleanup.append({"argv": cleanup_argv, "stdout": result.stdout,
                                        "stderr": result.stderr, "exit_status": result.returncode})
                    except Exception as exc:
                        cleanup.append({"argv": cleanup_argv, "error": str(exc)})
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
            for key, filename in (("stdout", "stdout.log"), ("stderr", "stderr.log")):
                path = directory / filename
                record[key] = path.read_text(errors="replace") if path.exists() else ""
            record.update(exit_status=process.returncode if process else None, error=error,
                          seconds=round(time.monotonic() - started, 3), cleanup=cleanup)
            atomic_json(directory / "execution.json", record)
        if error:
            raise tools.TestToolsError(error)
        return record


def resolver_identity(runner: ContainerRunner) -> dict:
    found = runner.run("resolver-path", ["bash", "-c", 'p=$(command -v composer) && printf "%s\\n" "$p" && sha256sum "$p"'],
                       network="none")
    if found["exit_status"] != 0:
        raise tools.TestToolsError("actual Composer executable lookup failed")
    lines = found["stdout"].strip().splitlines()
    if len(lines) != 2:
        raise tools.TestToolsError("unexpected Composer executable lookup output")
    path = lines[0]
    digest, filename = lines[1].split(maxsplit=1)
    if filename.strip() != path:
        raise tools.TestToolsError("Composer executable hash/path differ")
    version = runner.run("resolver-version", [path, "--version"], network="none")
    result = {
        "image": runner.image, "composer_path": path, "composer_sha256": digest,
        "version_argv": [path, "--version"], "version_exit_status": version["exit_status"],
        "version_stdout": version["stdout"], "version_stderr": version["stderr"],
    }
    tools._resolver(result)
    return result


def image_audit(runner: ContainerRunner, output: Path) -> dict:
    inspect = subprocess.run(["docker", "image", "inspect", runner.image], capture_output=True, text=True, timeout=30)
    if inspect.returncode or json.loads(inspect.stdout)[0]["Id"] != runner.image:
        raise tools.TestToolsError("pinned PHP84 image is not locally available with the expected digest")
    scan = ("find / -xdev \\( -name .git -o -name '*source-only-golden.patch' "
            "-o -name '*golden*.patch' -o -name oracles.jsonl "
            "-o -name generation_results.jsonl \\) -print")
    result = runner.run("image-rootfs-audit", ["bash", "-c", scan], rootfs_audit=True)
    if result["exit_status"] != 0 or result["stdout"].strip():
        raise tools.TestToolsError("PHP84 image rootfs audit failed")
    history = subprocess.run(["docker", "history", "--no-trunc", "--format", "{{json .}}", runner.image],
                             capture_output=True, text=True, timeout=30)
    if history.returncode:
        raise tools.TestToolsError("PHP84 image history audit failed")
    record = {
        "schema_version": "reviewbench-developer-image-audit/v1", "image": runner.image,
        "expected_image_id": runner.image, "no_reference_assets": True, "no_repository_history": True,
        "basis": "pinned developer image, gated rootfs scan and retained history",
        "rootfs_scan_command": result["container_command"], "rootfs_scan_output": result["stdout"],
        "history": [json.loads(line) for line in history.stdout.splitlines()],
    }
    atomic_json(output, record)
    return {"path": str(output.resolve()), "sha256": sha(output)}


def snapshot_inputs(stage: Path, destination: Path):
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("composer.json", "composer.lock", "vendor/composer/installed.json"):
        if (stage / name).is_file():
            (destination / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(stage / name, destination / name)


def retain_attempt(target: Path):
    names = [path for path in target.iterdir() if path.name != "attempts"]
    if not names:
        return
    destination = target / "attempts" / uuid.uuid4().hex
    destination.mkdir(parents=True)
    for path in names:
        path.rename(destination / path.name)


def reuse_prepared(target, repository, task, case, image):
    from agentic_runtime_migration.composer_test_tools_lane import validate_prepared
    path = target / "preparation.json"
    record = json.loads(path.read_text()) if path.is_file() else None
    if record and record.get("status") == "prepared":
        validate_prepared(repository, task, case, record, target / "payload", image=image)
        if record.get("runtime_php_sha256") != hashlib.sha256(runtime.php_runtime_source().encode()).hexdigest():
            raise tools.TestToolsError("prepared Kirby runtime differs from this frozen controller")
        return record
    if (target / "payload").exists():
        raise tools.TestToolsError("published Kirby payload requires validation, not another resolution")
    return None


def prepare_one(*, task, oracle, case, config, services, legacy, output, image, controller, timeout_seconds):
    from reviewbench_maintainability.agentic.contracts import canonical_hash, component
    from reviewbench_maintainability.agentic.resources import ResourceRequest
    from reviewbench_maintainability.agentic.snapshots import extract_tar, git, start_archive
    from agentic_runtime_migration.composer_test_tools_lane import validate_case, validate_prepared

    validate_case(task, case)
    repository = Path(config["repo_cache"]) / task["repo"].replace("/", "__")
    target = output / component(task["id"])
    target.mkdir(parents=True, exist_ok=True)
    # A prepared reuse failure is not converted into a new pending resolution.
    old = reuse_prepared(target, repository, task, case, image)
    if old is not None:
        return old
    retain_attempt(target)
    record = {
        "id": task["id"], "repo": task["repo"], "start_sha": task["start_sha"], "status": "preparing",
        "config_fingerprint": canonical_hash(case), "image": image, "controller": controller,
        "historical_lock": False, "scratch_paths": [], "model_calls": 0, "scoring_result": False,
        "runtime_php_sha256": hashlib.sha256(runtime.php_runtime_source().encode()).hexdigest(),
    }
    atomic_json(target / "preparation.json", record)
    try:
        manifest = git(repository, "show", task["start_sha"] + ":composer.json")
        lock = git(repository, "show", task["start_sha"] + ":composer.lock")
        tracked = [name.decode() for name in git(repository, "ls-tree", "-r", "--name-only", "-z",
                                                task["start_sha"]).split(b"\0") if name]
        plan = tools.plan_test_tools(task, manifest, lock)
        record.update(plan_sha256=tools._fingerprint(plan), manifest_sha256=hashlib.sha256(manifest).hexdigest(),
                      lock_sha256=hashlib.sha256(lock).hexdigest())
        atomic_json(target / "plan.json", plan)
        size = services.source_bytes(task, repository)
        request = ResourceRequest("kirby-test-tools-prepare:" + task["id"], cpus=2,
                                  memory_bytes=4 * GIB, scratch_bytes=size * 2 + 8 * GIB, heavy=False)
        with services.gate.acquire(
            request, uses_containers=True, source_catalog=services.sources, task=task,
            timeout=config.get("execution", {}).get("admission_timeout_seconds", 3600),
        ) as lease:
            record["resource_lease"] = lease
            atomic_json(target / "preparation.json", record)
            with tempfile.TemporaryDirectory(prefix="kirby-test-tools-", dir=services.scratch) as temporary:
                root = Path(temporary)
                stage = tools.stage_test_tools(root / "install", plan, manifest, lock)
                runner = ContainerRunner(
                    services=services, lease=lease, image=image, stage=stage, evidence=target / "phases",
                    cache=legacy / "scripted/cache/composer", timeout_seconds=timeout_seconds)
                record["image_audit"] = image_audit(runner, target / "image-audit.json")
                resolver = resolver_identity(runner)
                record["resolver"] = resolver
                atomic_json(target / "preparation.json", record)
                original, resolution = None, None
                for phase, command in plan["commands"].items():
                    execution = runner.run(phase, command)
                    snapshot_inputs(stage, target / "phases" / phase / "inputs")
                    record["last_phase"], record["exit_status"] = phase, execution["exit_status"]
                    atomic_json(target / "preparation.json", record)
                    if execution["exit_status"] != 0:
                        raise tools.TestToolsError(phase + " failed with exit " + str(execution["exit_status"]))
                    if phase == "original_install":
                        original = tools.record_original_install(plan, stage, resolver=resolver, execution=execution)
                        atomic_json(target / "original-install-checkpoint.json", original)
                    elif phase == "resolve_tools":
                        resolution = tools.record_resolution(plan, stage, original_install=original,
                                                             resolver=resolver, execution=execution)
                        atomic_json(target / "resolution-checkpoint.json", resolution)
                    else:
                        reference = tools.seal_prepared_tools(
                            root / "sealed", plan, stage, original_manifest=manifest, original_lock=lock,
                            original_install=original, resolution=resolution, resolver=resolver, execution=execution)
                payload = root / "payload"
                runtime.export_payload(stage, payload, tracked, sealed=root / "sealed",
                                       expected_sha256=reference["record_sha256"], image=image)
                entry = runtime.write_php_runtime(root / "entry.php")
                candidate = root / "candidate"
                with start_archive(repository, task["start_sha"]) as stream:
                    extract_tar(stream, candidate)
                source_hash = runtime.tree_fingerprint(candidate)
                probe = runner.run(
                    "current-source-build-probe",
                    ["php", "/kirby-entry.php", "/workspace", "/kirby-dependencies",
                     "/workspace/.reviewbench/kirby-preparation-mirror", image, reference["record_sha256"]],
                    stage=candidate, mounts=[(entry, "/kirby-entry.php", True), (payload, "/kirby-dependencies", True)],
                    network="none")
                if probe["exit_status"] != 0:
                    raise tools.TestToolsError("real Kirby source mirror/PHPUnit version probe failed")
                built = json.loads(probe["stdout"].strip().splitlines()[-1])
                # Ignore only internal probe scratch when checking its full input copy.
                shutil.rmtree(candidate / ".reviewbench")
                if runtime.tree_fingerprint(candidate) != source_hash or built.get("source_unchanged") is not True:
                    raise tools.TestToolsError("preparation probe changed original candidate source")
                record.update(test_tools=reference, build_probe={**built, "exit_status": probe["exit_status"],
                              "execution": str(target / "phases/current-source-build-probe/execution.json")},
                              source_tree_sha256=source_hash)
                if (target / "payload").exists():
                    raise tools.TestToolsError("refusing to replace an existing published Kirby payload")
                shutil.copytree(payload, target / "payload", symlinks=True)
                record.update(payload_tree_sha256=runtime.tree_fingerprint(target / "payload"), status="prepared",
                              exit_status=0)
                atomic_json(target / "preparation.json", record)
                validate_prepared(repository, task, case, record, target / "payload", image=image)
    except Exception as exc:
        record.update(status="pending", error=f"{type(exc).__name__}: {exc}")
    atomic_json(target / "preparation.json", record)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("config", "legacy-root", "out-dir", "task-ids", "toolchains", "engine-src-file", "controller-manifest"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=int, default=1200)
    args = parser.parse_args(argv)
    if args.workers != 1 or args.timeout_seconds < 1:
        parser.error("this standalone Kirby preparer requires workers=1 and a positive timeout")
    engine = configure_engine(args.engine_src_file)
    from reviewbench_maintainability.agentic.cli import load_config
    from reviewbench_maintainability.agentic.contracts import read_jsonl, selected_tasks
    from reviewbench_maintainability.agentic.execution import ExecutionServices
    from reviewbench_maintainability.agentic.scheduling import output_lock
    registry = json.loads(args.toolchains.read_text())
    pinned = registry.get(IMAGE_ALIAS, {})
    if pinned.get("status") != "ready" or not re.fullmatch(r"sha256:[0-9a-f]{64}", pinned.get("image", "")):
        parser.error("the explicit toolchain registry needs a ready pinned PHP84 image")
    controller = verify_controller(args.controller_manifest, engine, pinned)
    config = load_config(args.config)
    tasks = selected_tasks(Path(config["tasks"]), Path(config["scope_ids"]) if config.get("scope_ids") else None)
    oracles = read_jsonl(Path(config["oracles"]))
    batch = args.task_ids.read_text().splitlines()
    if batch != [tools.TASK_ID] or tools.TASK_ID not in tasks:
        parser.error("task-ids must contain the single frozen Kirby task")
    legacy, output = args.legacy_root.resolve(), args.out_dir.resolve()
    cases = json.loads((legacy / "scripted/oracle_config.json").read_text())["tasks"]
    services = ExecutionServices(config)
    with output_lock(output):
        task, oracle = tasks[tools.TASK_ID], oracles[tools.TASK_ID]
        record = prepare_one(task=task, oracle=oracle, case=cases[str(oracle["index"])], config=config,
                             services=services, legacy=legacy, output=output, image=pinned["image"],
                             controller=controller, timeout_seconds=args.timeout_seconds)
        summary = {"expected_tasks": len(tasks), "batch_tasks": 1, "prepared": int(record["status"] == "prepared"),
                   "model_calls": 0, "scoring_result": False, "records": {tools.TASK_ID: record}}
        atomic_json(output / "preparation-summary.json", summary)
        atomic_json(output / "resource-usage.json", services.gate.status())
        print(json.dumps({key: record.get(key) for key in ("id", "status", "last_phase", "exit_status", "error")},
                         sort_keys=True), flush=True)
    return 0 if record["status"] == "prepared" else 2


if __name__ == "__main__":
    raise SystemExit(main())
