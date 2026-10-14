#!/usr/bin/env python3
"""Prepare pinned npm dependencies outside agent execution and scoring."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.environ.get("REVIEWBENCH_ENGINE_SRC", str(Path(__file__).resolve().parents[1]/"src")))
from reviewbench_maintainability.agentic.cli import load_config
from reviewbench_maintainability.agentic.contracts import (
    ContractError, atomic_json, canonical_hash, component, file_hash, repo_path, selected_tasks,
)
from reviewbench_maintainability.agentic.execution import ExecutionServices
from reviewbench_maintainability.agentic.resources import ResourceRequest
from reviewbench_maintainability.agentic.scheduling import completed_jobs, output_lock
from reviewbench_maintainability.agentic.snapshots import start_archive, extract_tar


def minimal_packages(install):
    if (not isinstance(install, list) or install[:2] != ["npm", "install"]
            or "--prefix" not in install or "--ignore-scripts" not in install):
        return None
    prefix = install.index("--prefix")
    if install[prefix + 1] != "node_modules/.oracle-deps":
        raise ContractError("unrecognized frozen minimal npm prefix")
    tokens = [value for index, value in enumerate(install[2:], start=2)
              if index not in {prefix, prefix + 1} and not value.startswith("--")]
    packages = {}
    for value in tokens:
        name, separator, version = value.rpartition("@")
        if not separator or not re.fullmatch(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?", version):
            raise ContractError("minimal npm package must have an exact version")
        packages[name] = version
    if not packages:
        raise ContractError("empty minimal npm dependency selection")
    return packages


def relocate_workspace_links(modules, source, workspace):
    """Keep npm workspace links attached to the actual future candidate tree."""
    links = []
    for directory, dirs, files in os.walk(modules, followlinks=False):
        for name in [*dirs, *files]:
            path = Path(directory)/name
            if not path.is_symlink():
                continue
            target = path.resolve()
            if target.is_relative_to(modules.resolve()):
                continue
            if not target.is_relative_to(source.resolve()):
                raise ContractError("npm dependency link escapes the prepared source/dependency trees")
            relative = path.relative_to(modules).as_posix()
            destination = (Path(workspace)/target.relative_to(source.resolve())).as_posix()
            links.append({"path": repo_path(relative), "workspace_target": repo_path(destination)})
    return links


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--legacy-root", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--minimal-only", action="store_true")
    parser.add_argument("--task-ids", type=Path, help="preparation batch IDs; does not reduce the declared evaluation scope")
    args = parser.parse_args()
    config = load_config(args.config)
    tasks = selected_tasks(Path(config["tasks"]), Path(config["scope_ids"]) if config.get("scope_ids") else None)
    batch = set(args.task_ids.read_text().splitlines()) if args.task_ids else set(tasks)
    if not batch or not batch <= tasks.keys():
        raise ContractError("Node preparation batch contains foreign/empty task IDs")
    cases = json.loads((args.legacy_root/"tsjs/cases.json").read_text())["cases"]
    services = ExecutionServices(config)
    output = args.out_dir.resolve()
    runtime_root = args.legacy_root.parent/"strict_screen_20260726/oracle_recovery/tsjs/_runtimes"
    candidates = {}
    for ident, case in cases.items():
        if ident not in batch or case.get("build") not in (None, "SKIP"):
            continue
        packages = minimal_packages(case.get("install"))
        if packages is not None or (not args.minimal_only and case.get("install") == ["npm", "ci", "--ignore-scripts"]):
            candidates[ident] = packages
    with output_lock(output):
        def prepare(ident):
            task, case, packages = tasks[ident], cases[ident], candidates[ident]
            target = output/component(ident)
            target.mkdir(exist_ok=True)
            record_file = target/"preparation.json"
            if record_file.is_file():
                old = json.loads(record_file.read_text())
                if old.get("status") == "prepared" and old["case_fingerprint"] == canonical_hash(case):
                    return old
            record = {"id": ident, "case_fingerprint": canonical_hash(case), "node_version": case["node"],
                      "kind": "minimal" if packages is not None else "npm-ci", "status": "preparing",
                      "expected_tasks": len(tasks), "scoring_result": False, "model_calls": 0}
            node_root = runtime_root/("node-v"+case["node"]+"-linux-x64")
            node, npm = node_root/"bin/node", node_root/"lib/node_modules/npm/bin/npm-cli.js"
            try:
                observed = subprocess.check_output([str(node), "--version"], text=True, timeout=10).strip()
                if observed != "v"+case["node"]:
                    raise ContractError("prepared Node version differs from frozen case")
                with services.gate.acquire(ResourceRequest("npm-prepare:"+ident, cpus=2,
                                                           memory_bytes=2*1024**3, scratch_bytes=6*1024**3)):
                    with tempfile.TemporaryDirectory(prefix="npm-dependencies-", dir=services.scratch) as temporary:
                        source = Path(temporary)/"source"
                        cwd = repo_path(case.get("workspace", "."), allow_root=True)
                        if packages is not None:
                            source.mkdir()
                            (source/"package.json").write_text(json.dumps({"private": True, "dependencies": packages}))
                            install = ["install", "--ignore-scripts", "--package-lock=true"]
                        else:
                            repository = Path(config["repo_cache"])/task["repo"].replace("/", "__")
                            tree = task["start_sha"] + (":"+cwd if cwd != "." else "")
                            with start_archive(repository, tree) as stream:
                                extract_tar(stream, source)
                            if not (source/"package-lock.json").is_file():
                                raise ContractError("frozen npm-ci package lock is missing")
                            lock_before = file_hash(source/"package-lock.json")
                            install = ["ci", "--ignore-scripts"]
                        env = {key: value for key, value in os.environ.items()
                               if key in {"PATH", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE",
                                          "http_proxy", "https_proxy", "no_proxy"}}
                        env.update(PATH=str(node_root/"bin")+":"+env.get("PATH", "/usr/bin:/bin"),
                                   HOME=str(source), NODE_OPTIONS="--max-old-space-size=1536",
                                   npm_config_cache=str(output/"npm-download-cache"), npm_config_maxsockets="4",
                                   npm_config_audit="false", npm_config_fund="false")
                        command = [str(node), str(npm), *install, "--no-audit", "--no-fund"]
                        record.update(command=command, log=str(target/"install.log"))
                        atomic_json(record_file, record)
                        with (target/"install.log").open("w") as log:
                            process = subprocess.Popen(command, cwd=source, env=env, stdout=log,
                                                       stderr=subprocess.STDOUT, start_new_session=True)
                            start = time.monotonic()
                            try:
                                while process.poll() is None:
                                    if time.monotonic()-start > 600:
                                        raise ContractError("npm dependency preparation exceeded 600 seconds")
                                    if shutil.disk_usage(services.scratch).free < 80*1024**3:
                                        raise ContractError("npm preparation reached the 80-GiB disk floor")
                                    time.sleep(.5)
                            finally:
                                if process.poll() is None:
                                    os.killpg(process.pid, signal.SIGTERM)
                                    try:
                                        process.wait(timeout=10)
                                    except subprocess.TimeoutExpired:
                                        os.killpg(process.pid, signal.SIGKILL)
                                        process.wait()
                        record["exit_status"] = process.returncode
                        if process.returncode:
                            raise ContractError("frozen npm install failed")
                        if packages is None and file_hash(source/"package-lock.json") != lock_before:
                            raise ContractError("npm-ci changed its frozen dependency lock")
                        modules = source/"node_modules"
                        if not modules.is_dir():
                            raise ContractError("npm produced no dependency directory")
                        links = relocate_workspace_links(modules, source, cwd)
                        final = target/"node_modules"
                        if final.exists():
                            raise ContractError("existing dependency snapshot needs an explicit new preparation directory")
                        shutil.copytree(modules, final, symlinks=True)
                        for link in links:
                            (final/link["path"]).unlink()
                        shutil.copyfile(source/"package-lock.json", target/"package-lock.json")
                        record.update(status="prepared", workspace_links=links,
                                      package_lock_sha256=file_hash(target/"package-lock.json"))
            except Exception as exc:
                record.update(status="pending", error=f"{type(exc).__name__}: {exc}")
            atomic_json(record_file, record)
            return record
        records = {}
        for ident, record in completed_jobs(prepare, candidates, args.workers):
            records[ident] = record
            print(json.dumps({key: record.get(key) for key in ("id", "status", "kind", "error", "exit_status")}), flush=True)
            atomic_json(output/"preparation-summary.json", {"expected_tasks": len(tasks), "batch_tasks": len(candidates),
                                                           "prepared": sum(r["status"] == "prepared" for r in records.values()),
                                                           "records": records})
    return 0 if all(row["status"] == "prepared" for row in records.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
