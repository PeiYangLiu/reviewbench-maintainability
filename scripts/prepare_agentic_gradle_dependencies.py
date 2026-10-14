#!/usr/bin/env python3
"""Prepare Gradle dependency caches from frozen source, never from scored answers."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, os.environ.get("REVIEWBENCH_ENGINE_SRC", str(Path(__file__).resolve().parents[1] / "src")))
from reviewbench_maintainability.agentic.cli import load_config
from reviewbench_maintainability.agentic.contracts import (
    ContractError, atomic_json, canonical_hash, component, file_hash, read_jsonl, selected_tasks,
)
from reviewbench_maintainability.agentic.execution import ExecutionServices
from reviewbench_maintainability.agentic.resources import CURRENT_JOB, GiB, ResourceRequest
from reviewbench_maintainability.agentic.scheduling import completed_jobs, output_lock
from reviewbench_maintainability.agentic.snapshots import extract_tar, git, initialize_git, start_archive
from agentic_runtime_migration.common import MigrationContext, bounded_cpu_set, retain_preparation_attempt
from agentic_runtime_migration.jvm_managed import (
    GRADLE_CACHE, TOOLS, gradle_compile_arguments, gradle_cache_files, gradle_init_script, repository_mirrors,
    gradle_test_dependency_resolver, gradle_test_targets,
)

PREPARATION_POLICY = "gradle-production-and-test-resolution/v3"


def dependency_input_names(names):
    return sorted(name for name in names if
                  Path(name).name in {"build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts",
                                     "gradle-wrapper.properties", "gradle-wrapper.jar", "verification-metadata.xml",
                                     "gradle.properties"}
                  or name.endswith(".lockfile"))


def successful_public_build(target, expected, mirrors):
    manifests = [target / "preparation.json", *sorted((target / "attempts").glob("*/preparation.json"))]
    for manifest in manifests:
        if not manifest.is_file():
            continue
        record = json.loads(manifest.read_text())
        log = manifest.parent / "install.log"
        if (record.get("status") == "prepared" and record.get("exit_status") == 0
                and record.get("oracle_fingerprint") == expected
                and record.get("repository_mirrors", {}) == mirrors and not record.get("resolve_only")
                and log.is_file() and file_hash(log) == record.get("log_sha256")):
            return record
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--legacy-root", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--task-ids", type=Path, required=True)
    parser.add_argument("--toolchains", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=int, default=1200)
    parser.add_argument("--repository-mirror", action="append", default=[])
    parser.add_argument("--resolve-only", action="store_true",
                        help="resolve test classpaths after an already successful public compilation")
    args = parser.parse_args()
    if args.timeout_seconds < 1:
        raise ContractError("Gradle preparation time bound must be positive")
    config = load_config(args.config)
    mirrors = repository_mirrors(args.repository_mirror)
    config["migration"] = {**config.get("migration", {}),
                           "toolchain_registry": str(args.toolchains.resolve())}
    tasks = selected_tasks(Path(config["tasks"]), Path(config["scope_ids"]) if config.get("scope_ids") else None)
    oracles = read_jsonl(Path(config["oracles"]))
    selected = set(args.task_ids.read_text().splitlines())
    if not selected or not selected <= tasks.keys():
        raise ContractError("Gradle preparation batch contains missing/foreign task IDs")
    services = ExecutionServices(config)
    output = args.out_dir.resolve()
    with output_lock(output):
        ctx = MigrationContext(config, tasks, oracles, args.legacy_root.resolve(), output / "toolchains")
        tools = output / "public-tools"
        tools.mkdir(exist_ok=True)
        (tools / "oracle-gradle.init.gradle").write_text(gradle_init_script(
            (args.legacy_root / "jvm/cache/tools/oracle-gradle.init.gradle").read_text(), mirrors) +
            gradle_test_dependency_resolver())

        def prepare(ident):
            task, oracle = tasks[ident], oracles[ident]
            target = output / component(ident)
            target.mkdir(exist_ok=True)
            retain_preparation_attempt(target)
            receipt = target / "preparation.json"
            record = {"id": ident, "start_sha": task["start_sha"], "expected_tasks": len(tasks),
                      "oracle_fingerprint": canonical_hash(oracle), "status": "preparing",
                      "model_calls": 0, "scoring_result": False, "repository_mirrors": mirrors,
                      "preparation_policy": PREPARATION_POLICY}
            try:
                if receipt.exists():
                    old = json.loads(receipt.read_text())
                    if (old.get("status") == "prepared" and old.get("oracle_fingerprint") == record["oracle_fingerprint"]
                            and old.get("repository_mirrors", {}) == mirrors):
                        if old.get("preparation_policy") == PREPARATION_POLICY:
                            return old
                else:
                    old = {}
                public_build = successful_public_build(target, record["oracle_fingerprint"], mirrors) if args.resolve_only else None
                if args.resolve_only and not public_build:
                    raise ContractError("resolve-only preparation requires a matching successful public build")
                command = oracle["command"]
                if "/task/session-setup/" in command or "emulator" in command:
                    raise ContractError("Gradle specialized source/service setup needs its own build adapter")
                wrappers = [value for value in shlex.split(command) if value in {"gradlew", "./gradlew"}]
                if len(wrappers) != 1:
                    raise ContractError("frozen Gradle command has no unique wrapper")
                wrapper = wrappers[0]
                arguments = gradle_compile_arguments(command, wrapper)
                if args.resolve_only:
                    arguments = [token for token in arguments if token.rsplit(":", 1)[-1] != "classes"]
                build = shlex.join(["bash", wrapper, "--no-daemon", "--max-workers=2",
                                    "--no-configure-on-demand", "--no-configuration-cache",
                                    *arguments,
                                    "-Dreviewbench.test.targets=" + ",".join(gradle_test_targets(command, wrapper)),
                                    ":reviewbenchResolveTestDependencies"])
                build = build.replace("/oracle-tools/oracle-gradle.init.gradle", TOOLS + "/oracle-gradle.init.gradle")
                if "--init-script" not in build and " -I " not in build:
                    build += " --init-script " + TOOLS + "/oracle-gradle.init.gradle"
                repository = ctx.repo(task)
                source_bytes = services.source_bytes(task, repository)
                with services.gate.acquire(ResourceRequest("gradle-prepare:" + ident, cpus=2,
                        memory_bytes=(4 if args.resolve_only else 8) * GiB,
                        scratch_bytes=source_bytes * 2 + (6 if args.resolve_only else 16) * GiB,
                        heavy=not args.resolve_only),
                        uses_containers=True) as lease:
                    image, audit = ctx.image(oracle["environment"]["container"])
                    with tempfile.TemporaryDirectory(prefix="gradle-dependencies-", dir=services.scratch) as temporary:
                        workspace = Path(temporary) / "workspace"
                        with start_archive(repository, task["start_sha"]) as stream:
                            extract_tar(stream, workspace)
                        initialize_git(workspace)
                        names = [name.decode() for name in git(repository, "ls-tree", "-r", "--name-only", "-z",
                                                               task["start_sha"]).split(b"\0") if name]
                        inputs = {name: file_hash(workspace / name) for name in dependency_input_names(names)}
                        cache = target / "gradle-home"
                        cache.mkdir(exist_ok=True)
                        container = "rb-gradle-prepare-" + uuid.uuid4().hex[:16]
                        argv = [
                            "docker", "run", "--rm", "--name", container, "--init", "--read-only",
                            "--network", "bridge", "--user", f"{os.getuid()}:{os.getgid()}",
                            "--cpus", "2", "--cpuset-cpus", bounded_cpu_set(2, lease["lease"]),
                            "--memory", "4096m" if args.resolve_only else "8192m",
                            "--pids-limit", "512", "--cap-drop=ALL",
                            "--security-opt=no-new-privileges", "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=1g",
                            "--label", "reviewbench.resource-lease=" + lease["lease"],
                            "--mount", f"type=bind,source={workspace},target=/workspace",
                            "--mount", f"type=bind,source={cache},target={GRADLE_CACHE}",
                            "--mount", f"type=bind,source={tools},target={TOOLS},readonly",
                            "-e", "HOME=/tmp", "-e", "USER=reviewbench", "-e", "CI=true",
                            "-e", "GRADLE_USER_HOME=" + GRADLE_CACHE,
                            "-e", "JAVA_TOOL_OPTIONS=-Duser.home=/tmp -XX:ActiveProcessorCount=2 -Xmx3g",
                            "-e", "GRADLE_OPTS=-Dorg.gradle.workers.max=2 -Dorg.gradle.parallel=false -Dorg.gradle.vfs.watch=false",
                            "-w", "/workspace", "--entrypoint", "bash", image, "-c", build,
                        ]
                        record.update(image=image, image_audit=audit, command=shlex.join(argv), input_hashes=inputs,
                                      log=str(target / "install.log"), resolve_only=args.resolve_only,
                                      prior_public_build=public_build["log_sha256"] if public_build else None)
                        atomic_json(receipt, record)
                        started = time.monotonic()
                        with (target / "install.log").open("w") as log:
                            CURRENT_JOB.get()["mark_container_use"]()
                            process = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT)
                            try:
                                while process.poll() is None:
                                    if time.monotonic() - started > args.timeout_seconds:
                                        raise ContractError("Gradle preparation exceeded its time bound")
                                    if shutil.disk_usage(services.scratch).free < 80 * GiB:
                                        raise ContractError("Gradle preparation reached the 80-GiB disk floor")
                                    time.sleep(.5)
                            finally:
                                if process.poll() is None:
                                    subprocess.run(["docker", "stop", "-t", "5", container], capture_output=True, timeout=20)
                                    process.wait(timeout=30)
                        record.update(exit_status=process.returncode, seconds=round(time.monotonic() - started, 2),
                                      log_sha256=file_hash(target / "install.log"))
                        if any(file_hash(workspace / name) != digest for name, digest in inputs.items()):
                            raise ContractError("Gradle preparation changed an original build/dependency descriptor")
                        if process.returncode:
                            raise ContractError("Gradle public build failed; downloaded dependencies retained")
                        record.update(status="prepared", cache_files=len(gradle_cache_files(cache)))
            except Exception as exc:
                record.update(status="pending", error=f"{type(exc).__name__}: {exc}")
            atomic_json(receipt, record)
            return record

        records = {}
        for ident, record in completed_jobs(prepare, sorted(selected), args.workers):
            records[ident] = record
            print(json.dumps({key: record.get(key) for key in ("id", "status", "exit_status", "error")}), flush=True)
            atomic_json(output / "preparation-summary.json", {
                "expected_tasks": len(tasks), "batch_tasks": len(selected),
                "prepared": sum(row["status"] == "prepared" for row in records.values()), "records": records,
            })
        atomic_json(output / "resource-usage.json", services.gate.status())
    return 0 if records and all(row["status"] == "prepared" for row in records.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
