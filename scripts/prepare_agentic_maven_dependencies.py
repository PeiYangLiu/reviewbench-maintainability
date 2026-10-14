#!/usr/bin/env python3
"""Prefetch exact missing Maven artifacts from bound offline-verification logs.

Preparation has registry access, but no private tests or candidate outputs.
Each task gets an independent copy of its dependency cache. A successful
download is not runtime readiness: migration must still replay all six tests.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid

sys.path.insert(0, os.environ.get("REVIEWBENCH_ENGINE_SRC", str(Path(__file__).resolve().parents[1] / "src")))
from reviewbench_maintainability.agentic.assets import AssetPool, asset_descriptor
from reviewbench_maintainability.agentic.cli import load_config
from reviewbench_maintainability.agentic.contracts import (
    ContractError, atomic_json, canonical_hash, component, file_hash, repo_path, selected_tasks,
)
from reviewbench_maintainability.agentic.execution import ExecutionServices
from reviewbench_maintainability.agentic.preparation import load_recipes
from reviewbench_maintainability.agentic.resources import GiB, ResourceRequest
from reviewbench_maintainability.agentic.scheduling import completed_jobs, output_lock
from agentic_runtime_migration.jvm_lane import TOOLS, project_coordinates
from agentic_runtime_migration.common import bounded_cpu_set, retain_preparation_attempt


def missing_artifacts(text, excluded_coordinates=()):
    """Translate Maven's G:A:type[:classifier]:version into dependency:get IDs."""
    excluded = set(excluded_coordinates)
    artifacts = set()
    dependency_path = False
    coordinate = re.compile(r"(?<![\w.-])([\w.-]+(?::[\w.-]+){3,4})(?![\w.:-])", re.ASCII)
    for line in text.splitlines():
        if re.match(r"^\[ERROR\]\s+Path to dependency:", line):
            dependency_path = True
            continue
        if dependency_path:
            if re.fullmatch(r"\[ERROR\]\s*", line):
                dependency_path = False
            continue
        # A goal name and a plugin version are not dependency coordinates.
        if not line.startswith("[ERROR]") or not (
                any(marker in line for marker in ("dependency:", "artifact ", "artifacts could not be resolved:"))
                or re.match(r"^\[ERROR\]\s+\d+\)\s+", line)):
            continue
        for value in coordinate.findall(line):
            group, name, packaging, *rest = value.split(":")
            if packaging not in {"jar", "pom", "zip", "war", "tar.gz", "exe", "maven-plugin", "test-jar"}:
                continue
            if (group, name) in excluded:
                continue
            classifier, version = ("", rest[0]) if len(rest) == 1 else rest
            if version in {"LATEST", "RELEASE"} or version.endswith("-SNAPSHOT"):
                raise ContractError("missing Maven dependency is not an immutable release: " + value)
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", version):
                raise ContractError("invalid Maven artifact version")
            # dependency:get takes version before packaging and classifier.
            artifacts.add(":".join([group, name, version, packaging] + ([classifier] if classifier else [])))
    return sorted(artifacts)


def latest_failure(directory):
    records = []
    for path in directory.rglob("*.json"):
        value = json.loads(path.read_text())
        if (isinstance(value, dict) and type(value.get("returncode")) is int and value["returncode"] != 0
                and value.get("status") == "completed"):
            records.append((path, value))
    if not records:
        raise ContractError("no completed failing command evidence for this task")
    path, value = max(records, key=lambda item: (item[0].stat().st_mtime_ns, item[0].name))
    logs = []
    for stream in ("stdout", "stderr"):
        name = repo_path(value[stream + "_file"])
        log = path.parent / name
        if log.is_symlink() or file_hash(log) != value[stream + "_sha256"]:
            raise ContractError("Maven failure log differs from its command record")
        logs.append(log.read_text(errors="replace"))
    return "\n".join(logs), {"path": str(path), "sha256": file_hash(path), "command": value["command"]}


def image_classifier(image):
    architectures = {"amd64": "x86_64", "arm64": "aarch_64"}
    if image.get("Os") != "linux" or image.get("Architecture") not in architectures:
        raise ContractError("Maven resolver image has no supported native classifier")
    return "linux-" + architectures[image["Architecture"]]


def resolve_command(artifacts, classifier="linux-x86_64"):
    if not artifacts:
        raise ContractError("no exact missing artifacts to prefetch")
    name, arch = classifier.split("-", 1)
    common = [
        TOOLS + "/maven/bin/mvn", "-B", "-ntp",
        "-Dmaven.repo.local=/opt/reviewbench-cache/maven",
        "-Dmaven.artifact.threads=2", "-Dos.detected.classifier=" + classifier,
        "-Dos.detected.name=" + name, "-Dos.detected.arch=" + arch,
        "org.apache.maven.plugins:maven-dependency-plugin:3.7.1:get",
        "-Dtransitive=true",
    ]
    return "set -e\n" + "\n".join(shlex.join([*common, "-Dartifact=" + value]) for value in artifacts)


@contextmanager
def resolver_job(services, tool, asset_root, seed, ident):
    seed_bytes = sum(path.stat().st_size for path in seed.rglob("*")
                     if path.is_file() and not path.is_symlink())
    tool_bytes = asset_descriptor(tool, asset_root)["archive"]["input_bytes"]
    request = ResourceRequest("maven-missing:" + ident, cpus=2, memory_bytes=2 * GiB,
                              scratch_bytes=seed_bytes + tool_bytes + 4 * GiB)
    with services.gate.acquire(request, uses_containers=True) as lease:
        pool = AssetPool(services.cache, services.scratch, lease["lease"], asset_root)
        try:
            yield pool.materialize(tool), lease["lease"]
        finally:
            pool.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--seed-root", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--task-ids", type=Path)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--max-artifacts", type=int, default=64)
    args = parser.parse_args()
    if args.timeout_seconds < 1 or args.max_artifacts < 1:
        raise ContractError("preparation bounds must be positive")
    config = load_config(args.config)
    services = ExecutionServices(config)
    tasks = selected_tasks(Path(config["tasks"]), Path(config["scope_ids"]) if config.get("scope_ids") else None)
    recipes = load_recipes(args.package / "runtime-recipes.jsonl")
    batch = set(args.task_ids.read_text().splitlines()) if args.task_ids else set(tasks)
    if not batch or not batch <= tasks.keys():
        raise ContractError("Maven preparation batch contains empty/foreign task IDs")
    output = args.out_dir.resolve()
    seed_root = args.seed_root.resolve()
    package = args.package.resolve()
    if output == seed_root:
        raise ContractError("dependency preparation must not modify the seed cache")
    candidates = []
    for ident, recipe in recipes.items():
        proof = package / "execution/preparation/verified" / component(ident) / "verification.json"
        if (ident in batch and recipe.lane == "jvm" and recipe.cache_mounts.get("maven")
                and proof.is_file() and json.loads(proof.read_text()).get("status") != "ready"):
            candidates.append(ident)

    with output_lock(output):
        def prepare(ident):
            recipe, task = recipes[ident], tasks[ident]
            target = output / component(ident)
            target.mkdir(exist_ok=True)
            retain_preparation_attempt(target)
            manifest = target / "preparation.json"
            record = {"id": ident, "status": "preparing", "expected_tasks": len(tasks),
                      "scoring_result": False, "model_calls": 0, "runtime_fingerprint": recipe.fingerprint}
            try:
                text, evidence = latest_failure(package / "execution/preparation/verified" / component(ident))
                repo = Path(config["repo_cache"]) / task["repo"].replace("/", "__")
                artifacts = missing_artifacts(text, project_coordinates(repo, task["start_sha"]))
                if not artifacts or len(artifacts) > args.max_artifacts:
                    raise ContractError("failure needs a build adapter or exceeds the artifact batch bound")
                binding = canonical_hash({"evidence": evidence, "artifacts": artifacts,
                                          "runtime": recipe.fingerprint})
                if manifest.exists():
                    old = json.loads(manifest.read_text())
                    if old.get("status") == "prefetched" and old.get("binding") == binding:
                        return old
                record.update(binding=binding, artifacts=artifacts, failure_evidence=evidence)
                tools = [asset for asset in recipe.assets
                         if asset["visibility"] == "developer" and asset["target"] == TOOLS]
                if len(tools) != 1:
                    raise ContractError("exact Maven developer toolchain is absent or ambiguous")
                seed = seed_root / component(ident) / "repository"
                if not seed.is_dir():
                    raise ContractError("explicit task-specific Maven seed is missing")
                with resolver_job(services, tools[0], Path(config["asset_root"]), seed, ident) as (tool, lease):
                    cache = target / "repository"
                    if not cache.exists():
                        shutil.copytree(seed, cache, symlinks=True)
                    name = "rb-maven-missing-" + uuid.uuid4().hex[:16]
                    image = json.loads(subprocess.check_output(
                        ["docker", "image", "inspect", recipe.image], text=True, timeout=30))[0]
                    classifier = image_classifier(image)
                    argv = [
                        "docker", "run", "--rm", "--name", name, "--init", "--read-only",
                        "--network", "bridge", "--user", f"{os.getuid()}:{os.getgid()}",
                        "--cpus", "2", "--cpuset-cpus", bounded_cpu_set(2, lease),
                        "--memory", "2048m", "--pids-limit", "256",
                        "--cap-drop=ALL", "--security-opt=no-new-privileges",
                        "--label", "reviewbench.resource-lease=" + lease,
                        "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=512m",
                        "--mount", f"type=bind,source={tool},target={TOOLS},readonly",
                        "--mount", f"type=bind,source={cache},target=/opt/reviewbench-cache/maven",
                        "-e", "HOME=/tmp", "-e", "MAVEN_OPTS=-Xmx1536m -XX:ActiveProcessorCount=2",
                        "-w", "/tmp", "--entrypoint", "/bin/bash", recipe.image, "-c",
                        resolve_command(artifacts, classifier),
                    ]
                    record.update(command=shlex.join(argv), image=recipe.image, log=str(target / "install.log"),
                                  toolchain_sha256=tools[0]["sha256"], cache=str(cache), classifier=classifier)
                    atomic_json(manifest, record)
                    started = time.monotonic()
                    with (target / "install.log").open("w") as log:
                        process = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT)
                        try:
                            while process.poll() is None:
                                if time.monotonic() - started > args.timeout_seconds:
                                    raise ContractError("Maven dependency preparation exceeded its time bound")
                                if shutil.disk_usage(services.scratch).free < 80 * GiB:
                                    raise ContractError("Maven preparation reached the 80-GiB disk floor")
                                time.sleep(.5)
                        finally:
                            if process.poll() is None:
                                subprocess.run(["docker", "stop", "-t", "5", name], capture_output=True, timeout=20)
                                process.wait(timeout=30)
                    record.update(exit_status=process.returncode, seconds=round(time.monotonic() - started, 2),
                                  log_sha256=file_hash(target / "install.log"))
                    if process.returncode:
                        raise ContractError("exact Maven artifact prefetch failed; downloaded cache retained")
                    record["status"] = "prefetched"
            except Exception as exc:
                record.update(status="pending", error=f"{type(exc).__name__}: {exc}")
            atomic_json(manifest, record)
            return record

        records = {}
        for ident, record in completed_jobs(prepare, candidates, args.workers):
            records[ident] = record
            print(json.dumps({key: record.get(key) for key in ("id", "status", "artifacts", "exit_status", "error")}),
                  flush=True)
            atomic_json(output / "preparation-summary.json", {
                "expected_tasks": len(tasks), "batch_tasks": len(candidates),
                "prefetched": sum(row["status"] == "prefetched" for row in records.values()), "records": records,
            })
        atomic_json(output / "resource-usage.json", services.gate.status())
    return 0 if records and all(row["status"] == "prefetched" for row in records.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
