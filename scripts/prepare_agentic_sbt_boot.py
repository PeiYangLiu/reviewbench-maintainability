#!/usr/bin/env python3
"""Prepare exact sbt boot distributions without project sources or test code."""
from __future__ import annotations

import argparse
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

sys.path.insert(0, os.environ.get("REVIEWBENCH_ENGINE_SRC", str(Path(__file__).resolve().parents[1] / "src")))
from reviewbench_maintainability.agentic.cli import load_config
from reviewbench_maintainability.agentic.contracts import ContractError, atomic_json, file_hash
from reviewbench_maintainability.agentic.execution import ExecutionServices
from reviewbench_maintainability.agentic.resources import CURRENT_JOB, ResourceRequest, GiB
from reviewbench_maintainability.agentic.scheduling import completed_jobs, output_lock
from agentic_runtime_migration.common import MigrationContext, bounded_cpu_set, retain_preparation_attempt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--legacy-root", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--toolchains", type=Path, required=True)
    parser.add_argument("--version", action="append", required=True)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    if any(not re.fullmatch(r"\d+\.\d+\.\d+", version) for version in args.version):
        raise ContractError("sbt boot preparation requires exact release versions")
    config = load_config(args.config)
    config["migration"] = {"toolchain_registry": str(args.toolchains.resolve())}
    services = ExecutionServices(config)
    output = args.out_dir.resolve()
    with output_lock(output):
        ctx = MigrationContext(config, {}, {}, args.legacy_root.resolve(), output / "toolchains")
        tools = output / "tools"
        tools.mkdir(exist_ok=True)
        shutil.copyfile(args.legacy_root / "jvm/cache/tools/sbt", tools / "sbt")
        (tools / "sbt").chmod(0o755)
        (tools / "repositories").write_text("[repositories]\nlocal\nmaven-central: https://repo.maven.apache.org/maven2\n")
        def prepare(version):
            target = output / version
            target.mkdir(exist_ok=True)
            retain_preparation_attempt(target)
            record_file = target / "preparation.json"
            record = {"version": version, "status": "preparing", "model_calls": 0, "scoring_result": False}
            launcher = target / "home/.sbt/launchers" / version / "sbt-launch.jar"
            if record_file.exists():
                old = json.loads(record_file.read_text())
                if old.get("status") == "prepared" and launcher.is_file() and file_hash(launcher) == old["launcher_sha256"]:
                    return old
            try:
                with services.gate.acquire(ResourceRequest("sbt-boot:" + version, cpus=2,
                        memory_bytes=4 * GiB, scratch_bytes=4 * GiB), uses_containers=True) as lease:
                    image, audit = ctx.image("oracle-java-tools:11")
                    home = target / "home"
                    home.mkdir(exist_ok=True)
                    with tempfile.TemporaryDirectory(prefix="sbt-boot-", dir=services.scratch) as temporary:
                        workspace = Path(temporary)
                        (workspace / "project").mkdir()
                        (workspace / "project/build.properties").write_text("sbt.version=" + version + "\n")
                        (workspace / "build.sbt").write_text('name := "reviewbench-boot-only"\n')
                        name = "rb-sbt-boot-" + uuid.uuid4().hex[:16]
                        command = ("/tools/sbt -sbt-version " + version +
                                   " -sbt-launch-repo https://repo.maven.apache.org/maven2 "
                                   "-sbt-launch-dir /sbt-home/.sbt/launchers -batch about")
                        argv = [
                            "docker", "run", "--rm", "--init", "--name", name, "--read-only",
                            "--network", "bridge", "--user", f"{os.getuid()}:{os.getgid()}",
                            "--cpus", "2", "--cpuset-cpus", bounded_cpu_set(2, lease["lease"]),
                            "--memory", "4096m", "--pids-limit", "256", "--cap-drop=ALL",
                            "--security-opt=no-new-privileges", "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=512m",
                            "--label", "reviewbench.resource-lease=" + lease["lease"],
                            "--mount", f"type=bind,source={workspace},target=/workspace",
                            "--mount", f"type=bind,source={tools},target=/tools,readonly",
                            "--mount", f"type=bind,source={home},target=/sbt-home",
                            "-e", "HOME=/sbt-home", "-e", "COURSIER_CACHE=/sbt-home/.cache/coursier",
                            "-e", "JVM_OPTS=-Xms256m -Xmx2g -XX:ActiveProcessorCount=2",
                            "-e", "SBT_OPTS=-Dsbt.boot.directory=/sbt-home/.sbt/boot -Dsbt.ivy.home=/sbt-home/ivy "
                                  "-Dsbt.global.base=/sbt-home/global -Dsbt.task.cpus=2 -Dsbt.supershell=false "
                                  "-Dsbt.repository.config=/tools/repositories -Dsbt.override.build.repos=true",
                            "-w", "/workspace", "--entrypoint", "bash", image, "-c", command,
                        ]
                        record.update(image=image, image_audit=audit, command=shlex.join(argv),
                                      log=str(target / "install.log"))
                        atomic_json(record_file, record)
                        started = time.monotonic()
                        with (target / "install.log").open("w") as log:
                            CURRENT_JOB.get()["mark_container_use"]()
                            process = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT)
                            try:
                                while process.poll() is None:
                                    if time.monotonic() - started > 900:
                                        raise ContractError("sbt boot preparation exceeded 900 seconds")
                                    if shutil.disk_usage(services.scratch).free < 80 * GiB:
                                        raise ContractError("sbt boot preparation reached the disk floor")
                                    time.sleep(.5)
                            finally:
                                if process.poll() is None:
                                    subprocess.run(["docker", "stop", "-t", "5", name], capture_output=True, timeout=20)
                                    process.wait(timeout=30)
                        record.update(exit_status=process.returncode, log_sha256=file_hash(target / "install.log"))
                        if process.returncode or not launcher.is_file():
                            raise ContractError("exact sbt boot initialization failed")
                        record.update(status="prepared", launcher_sha256=file_hash(launcher))
            except Exception as exc:
                record.update(status="pending", error=f"{type(exc).__name__}: {exc}")
            atomic_json(record_file, record)
            return record
        records = {}
        for version, record in completed_jobs(prepare, sorted(set(args.version)), args.workers):
            records[version] = record
            print(json.dumps({key: record.get(key) for key in ("version", "status", "exit_status", "error")}), flush=True)
            atomic_json(output / "preparation-summary.json", records)
    return 0 if all(row["status"] == "prepared" for row in records.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
