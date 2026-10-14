"""Build one pinned public Relic dependency, then publish its immutable closure.

No Go checkout/build cache is copied. The original crypto ZIP pins the upstream
build scripts; Git exports the exact Relic commit independently of its worktree.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import subprocess
import tarfile
import time
import zipfile

from reviewbench_maintainability.agentic.cli import load_config
from reviewbench_maintainability.agentic.contracts import (
    ContractError, atomic_json, file_hash, read_jsonl, selected_tasks,
)
from reviewbench_maintainability.agentic.execution import ExecutionServices
from reviewbench_maintainability.agentic.resources import ResourceRequest, file_lock
from reviewbench_maintainability.agentic.snapshots import git
from .common import MigrationContext, bounded_cpu_set
from .go_dependencies import content_sum
from . import go_relic


def checked_crypto_zip(path: Path) -> dict:
    if (path.is_symlink() or not path.is_file()
            or file_hash(path) != go_relic.ZIP_SHA256
            or content_sum(path, ".zip") != go_relic.ZIP_H1):
        raise ContractError("go_relic_crypto_zip_differs")
    prefix = go_relic.MODULE + "@" + go_relic.VERSION + "/"
    with zipfile.ZipFile(path) as archive:
        for name, expected in go_relic.BUILD_SCRIPTS.items():
            raw = archive.read(prefix + name)
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ContractError("go_relic_original_build_script_differs: " + name)
    return {"path": str(path), "sha256": file_hash(path), "h1": go_relic.ZIP_H1,
            "script_sha256": dict(go_relic.BUILD_SCRIPTS)}


def extract_git_export(archive: Path, target: Path):
    """Extract only bounded, regular files from our exact-commit git archive."""
    target.mkdir()
    size = 0
    with tarfile.open(archive) as stream:
        members = stream.getmembers()
        if len(members) > 2000:
            raise ContractError("go_relic_source_export_member_bound")
        for member in members:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or not name.parts:
                raise ContractError("go_relic_source_export_path_invalid")
            destination = target.joinpath(*name.parts)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                size += member.size
                if size > 32 * 1024**2:
                    raise ContractError("go_relic_source_export_size_bound")
                destination.parent.mkdir(parents=True, exist_ok=True)
                with stream.extractfile(member) as source, destination.open("xb") as output:
                    shutil.copyfileobj(source, output)
                destination.chmod(0o755 if member.mode & 0o111 else 0o644)
            else:
                raise ContractError("go_relic_source_export_special_file")


def prepare(config: dict, task_id: str, crypto_zip: Path, relic_git: Path,
            legacy: Path, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    with file_lock(output / "preparation.lock"):
        descriptor_path = output / "dependency.json"
        if descriptor_path.exists():
            value = json.loads(descriptor_path.read_text())
            for ref in (value["runtime_asset"], value["source_asset"]):
                if file_hash(Path(ref["path"])) != ref["sha256"]:
                    raise ContractError("go_relic_retained_asset_changed")
            return {"path": str(descriptor_path), "sha256": file_hash(descriptor_path),
                    "reused_completed_preparation": True}
        if (output / "process.json").exists():
            raise ContractError("go_relic_attempt_already_exists_inspect_its_evidence")
        if platform.system() != "Linux" or platform.machine() != "x86_64":
            raise ContractError("go_relic_original_architecture_requires_linux_x86_64")
        tasks = selected_tasks(Path(config["tasks"]), Path(config["scope_ids"]))
        oracles = read_jsonl(Path(config["oracles"]))
        task = tasks[task_id]
        ctx = MigrationContext(config, tasks, oracles, legacy, output / "assets")
        binding = go_relic.frozen_crypto_binding(ctx, task, "")
        crypto = checked_crypto_zip(crypto_zip)
        observed = git(relic_git, "rev-parse", go_relic.RELIC_COMMIT + "^{commit}").decode().strip()
        tree = git(relic_git, "rev-parse", go_relic.RELIC_COMMIT + "^{tree}").decode().strip()
        if observed != go_relic.RELIC_COMMIT or tree != go_relic.RELIC_TREE:
            raise ContractError("go_relic_git_pin_differs")
        tools = {}
        for name in ("cmake", "cc", "ar", "ranlib", "ld", "make"):
            path = Path(shutil.which(name) or "/missing-tool").resolve()
            if not path.is_file():
                raise ContractError("go_relic_build_tool_missing: " + name)
            tools[name] = {
                "path": str(path), "sha256": file_hash(path),
                "version": subprocess.check_output([str(path), "--version"], text=True).splitlines()[0],
            }
        if "3.28.3" not in tools["cmake"]["version"] or "13.3.0" not in tools["cc"]["version"]:
            raise ContractError("go_relic_original_cmake_or_gcc_version_differs")
        cc1 = Path(subprocess.check_output([tools["cc"]["path"], "-print-prog-name=cc1"], text=True).strip())
        tools["cc1"] = {"path": str(cc1), "sha256": file_hash(cc1)}
        record = {
            "status": "running", "pid": os.getpid(), "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "task_id": task_id, "full_scope_size": len(tasks),
            "crypto": crypto, "relic_git": str(relic_git), "relic_commit": observed, "relic_tree": tree,
            "platform": "linux/amd64", "tools": tools, "commands": [],
            "old_static_library_reused": False, "candidate_inputs_used": False,
            "models_called": 0, "automatic_retry": False,
        }
        atomic_json(output / "process.json", record)
        services = ExecutionServices(config)
        try:
            with services.gate.acquire(
                ResourceRequest("prepare-external-relic:" + task_id, cpus=2,
                                memory_bytes=2 * 1024**3, scratch_bytes=3 * 1024**3,
                                heavy=True),
                uses_containers=False, timeout=3600,
            ) as lease:
                record["lease"] = lease["lease"]
                cpus = bounded_cpu_set(2, lease["lease"])
                record["cpu_set"] = cpus
                atomic_json(output / "process.json", record)
                print(json.dumps({"status": "admitted", "pid": os.getpid(),
                                  "lease": lease["lease"], "cpu_set": cpus,
                                  "output": str(output)}), flush=True)
                source_tar = output / "relic-original-source.tar"
                with source_tar.open("xb") as stream:
                    subprocess.run(["git", "-C", str(relic_git), "archive", "--format=tar", observed],
                                   stdout=stream, stderr=subprocess.PIPE, check=True,
                                   env={**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"}, timeout=120)
                record["source_tar"] = {"path": str(source_tar), "sha256": file_hash(source_tar)}
                source, build = output / "source", output / "build"
                extract_git_export(source_tar, source)
                build.mkdir()
                home = output / "home"
                home.mkdir()
                environment = {
                    "PATH": "/usr/bin:/bin", "HOME": str(home), "LANG": "C", "LC_ALL": "C",
                    "CC": tools["cc"]["path"], "CMAKE_BUILD_PARALLEL_LEVEL": "2", "OMP_NUM_THREADS": "2",
                }
                commands = [
                    ("configure", [tools["cmake"]["path"], "-S", str(source), "-B", str(build),
                                   *go_relic.cmake_definitions()], 180),
                    ("build", [tools["cmake"]["path"], "--build", str(build),
                               "--target", "relic_s", "--parallel", "2"], 1200),
                ]
                for phase, command, timeout in commands:
                    actual = ["taskset", "-c", cpus, *command]
                    log = output / (phase + ".log")
                    started = time.monotonic()
                    with log.open("x") as stream:
                        result = subprocess.run(actual, cwd=output, env=environment,
                                                stdout=stream, stderr=subprocess.STDOUT, timeout=timeout)
                    row = {"phase": phase, "command": actual, "cwd": str(output),
                           "environment": environment, "exit_code": result.returncode,
                           "elapsed_seconds": time.monotonic() - started,
                           "log": str(log), "log_sha256": file_hash(log)}
                    record["commands"].append(row)
                    atomic_json(output / "process.json", record)
                    if result.returncode:
                        raise ContractError("go_relic_external_" + phase + "_failed")
                flags = (build / "src/CMakeFiles/relic_s.dir/flags.make").read_text()
                if "-march=westmere -mtune=native" not in flags:
                    raise ContractError("go_relic_architecture_flags_differ")
                payload = output / "runtime"
                for source_file in sorted((source / "include").rglob("*.h")):
                    destination = payload / source_file.relative_to(source)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source_file, destination)
                for source_file, name in [
                    (build / "include/relic_conf.h", "generated/relic_conf.h"),
                    (build / "lib/librelic_s.a", "lib/librelic_s.a"),
                    *[(source / name, name) for name in ("LICENSE", "LICENSE.Apache-2.0", "LICENSE.LGPL-2.1")],
                ]:
                    destination = payload / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source_file, destination)
                files = []
                for path in sorted(payload.rglob("*")):
                    if path.is_file():
                        path.chmod(0o644)
                        files.append({"path": path.relative_to(payload).as_posix(),
                                      "bytes": path.stat().st_size, "sha256": file_hash(path)})
                go_relic.runtime_file_records(files)
                runtime_asset = ctx.bundle(payload, role="dependency", git_commit=observed)
                source_asset = ctx.bundle(source, role="dependency", git_commit=observed)
                record.update(status="built", runtime_asset=runtime_asset, source_asset=source_asset)
                value = {
                    "schema_version": go_relic.SCHEMA, "platform": "linux/amd64",
                    "crypto": binding, "relic_commit": observed, "relic_tree": tree,
                    "cmake_definitions": go_relic.cmake_definitions(),
                    "build_status": "built", "candidate_inputs_used": False,
                    "runtime_files": files, "runtime_asset": runtime_asset,
                    "source_asset": source_asset, "source_tar_sha256": file_hash(source_tar),
                    "tools": tools, "build_commands": record["commands"],
                }
                atomic_json(descriptor_path, value)
            record.update(status="terminal", exit_code=0,
                          ended_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                          descriptor_sha256=file_hash(descriptor_path))
            atomic_json(output / "process.json", record)
            return {"path": str(descriptor_path), "sha256": file_hash(descriptor_path),
                    "reused_completed_preparation": False}
        except BaseException as exc:
            record.update(status="terminal", exit_code=1,
                          ended_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                          error=type(exc).__name__ + ": " + str(exc))
            atomic_json(output / "process.json", record)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--crypto-zip", type=Path, required=True)
    parser.add_argument("--relic-git", type=Path, required=True)
    parser.add_argument("--legacy-root", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    result = prepare(load_config(args.config), args.task_id, args.crypto_zip.resolve(),
                     args.relic_git.resolve(), args.legacy_root.resolve(), args.out_dir.resolve())
    print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    main()
