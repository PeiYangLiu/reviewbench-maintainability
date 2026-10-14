#!/usr/bin/env python3
"""Fill sbt dependency gaps using thin per-task caches over a read-only seed."""
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
from reviewbench_maintainability.agentic.contracts import (
    ContractError, atomic_json, canonical_hash, component, file_hash, read_jsonl, repo_path, selected_tasks,
)
from reviewbench_maintainability.agentic.execution import ExecutionServices
from reviewbench_maintainability.agentic.resources import CURRENT_JOB, GiB, ResourceRequest
from reviewbench_maintainability.agentic.scheduling import completed_jobs, output_lock
from reviewbench_maintainability.agentic.snapshots import extract_tar, git, initialize_git, start_archive
from agentic_runtime_migration.common import MigrationContext, bounded_cpu_set, retain_preparation_attempt
from agentic_runtime_migration.jvm_ivy import remote_ivy_files
from agentic_runtime_migration.jvm_managed import (
    SBT_BASE, SBT_CACHE, TOOLS, public_download_path, sbt_boot_files, sbt_cache_files,
    sbt_compile_arguments, sbt_test_dependency_arguments, sbt_prefix, shared_archive,
    sbt_jvm_options, pom_only_negative_cache,
)

PREPARATION_POLICY = "sbt-download-delta/test-classifiers-v5"
MAX_CLASSIFIER_PROJECTS = 16


def classifier_arguments(projects):
    """Only literal, bounded project/updateClassifiers targets; no sbt shell."""
    if len(projects) > MAX_CLASSIFIER_PROJECTS:
        raise ContractError(f"sbt classifier projects allow at most {MAX_CLASSIFIER_PROJECTS} arguments")
    result = []
    for project in projects:
        if not isinstance(project, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", project):
            raise ContractError("sbt classifier project must be a literal public project ID (1-128 characters)")
        target = project + "/updateClassifiers"
        if target not in result:
            result.append(target)
    return result


def dependency_arguments(command, projects=(), classifier_projects=()):
    result = sbt_test_dependency_arguments(command)
    classifiers = classifier_arguments(classifier_projects)
    if (projects or classifiers) and not result:
        raise ContractError("sbt additional dependency projects require an original test command")
    for project in projects:
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", project):
            raise ContractError("sbt dependency project must be a literal public project ID")
        target = project + "/update"
        if target not in result:
            result.append(target)
    return [*result, *classifiers]


def dependency_request(command, projects=(), classifier_projects=(), *, resolve_only=False):
    """Bind raw opt-ins as well as the complete effective resolution arguments."""
    return {
        "dependency_projects": list(projects), "classifier_projects": list(classifier_projects),
        "resolve_only": resolve_only,
        "arguments": dependency_arguments(command, projects, classifier_projects),
    }


def cached_request_matches(previous, requested):
    """Reuse unchanged v4 binary closures; new classifier requests need v5 proof."""
    policy = previous.get("preparation_policy")
    if policy == PREPARATION_POLICY:
        return (previous.get("dependency_request") == requested
                and previous.get("dependency_request_sha256") == canonical_hash(requested))
    if policy == "sbt-download-delta/test-configurations-v4" and not requested["classifier_projects"]:
        return previous.get("test_dependency_resolution", {}).get("arguments") == requested["arguments"]
    return False


def preparation_arguments(command, resolution_arguments, *, resolve_only):
    if resolve_only:
        return list(resolution_arguments)
    return [*sbt_compile_arguments(command),
            *[arg for arg in resolution_arguments if arg.endswith(("/update", "/updateClassifiers"))]]


def preparation_profile(resolve_only):
    """Dependency-only updates are bounded separately from whole-project builds."""
    return {"cpus": 2, "memory_gib": 4 if resolve_only else 8,
            "scratch_gib": 6 if resolve_only else 8, "heavy": not resolve_only}


def public_build_receipt(target, previous, task, oracle_fingerprint, base_asset):
    """Require a successful hash-bound original public build for resolve-only."""
    log = target / "install.log"
    if (previous.get("id") != task["id"] or previous.get("start_sha") != task["start_sha"]
            or previous.get("status") != "prepared" or previous.get("exit_status") != 0
            or previous.get("oracle_fingerprint") != oracle_fingerprint
            or previous.get("base_asset") != base_asset or not log.is_file()
            or file_hash(log) != previous.get("log_sha256") or not previous.get("input_hashes")):
        raise ContractError("sbt resolve-only requires a bound successful public build")
    return {key: previous[key] for key in
            ("id", "start_sha", "command", "exit_status", "seconds", "log_sha256", "input_hashes")}


def validate_cached_dependencies(target, previous):
    """A matching classifier request still needs intact resolution evidence."""
    resolution = previous.get("test_dependency_resolution")
    if resolution:
        log = target / Path(resolution.get("log", "")).name
        if (not log.is_file() or log.is_symlink()
                or file_hash(log) != resolution.get("log_sha256")):
            raise ContractError("sbt cached dependency resolution log changed")
    for name, digest in previous.get("overlay_files", {}).items():
        path = target / "overlay" / repo_path(name)
        if not path.is_file() or path.is_symlink() or file_hash(path) != digest:
            raise ContractError("sbt cached dependency overlay bytes changed")


def seed_thin_cache(source, target, names, mount=SBT_BASE):
    """Large immutable JARs remain in the read-only mount; metadata is copied."""
    target.mkdir(parents=True, exist_ok=True)
    for name in names:
        original, destination = source / repo_path(name), target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() or destination.is_symlink():
            continue
        if original.suffix == ".jar":
            destination.symlink_to(mount + "/" + name)
        else:
            shutil.copy2(original, destination)


def dependency_overlay(source, target, seed_names, output, mount=SBT_BASE):
    """Flatten additions only; never archive container-only seed symlinks."""
    seed = set(seed_names)
    current = set(sbt_boot_files(target))
    current.update(remote_ivy_files(target))
    # rglob includes dangling container-only symlinks; do not dereference
    # them on the host, or mistake them for deleted dependencies.
    for prefix in (".sbt/boot", ".sbt/launchers", ".cache/coursier"):
        for path in (target / prefix).rglob("*"):
            if path.is_symlink():
                name = path.relative_to(target).as_posix()
                if name not in seed or os.readlink(path) != mount + "/" + name:
                    raise ContractError("sbt thin-cache link does not point to its declared seed")
                current.add(name)
            elif path.is_file() and prefix == ".cache/coursier":
                relative = path.relative_to(target / prefix)
                if public_download_path(relative) or pom_only_negative_cache(path, relative):
                    current.add(path.relative_to(target).as_posix())
    changed = {}
    for name in sorted(current):
        path = target / repo_path(name)
        if path.is_symlink():
            continue
        digest = file_hash(path)
        if name in seed and file_hash(source / name) == digest:
            continue
        if output is not None:
            destination = output / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
        changed[name] = digest
    return changed, sorted(seed - current)


def refresh_prepared_overlay(target, source, seed_names, previous, expected_oracle, base_asset,
                             *, include_remote_ivy=False):
    """Repackage proven downloads without replacing the original public build."""
    log = target / "install.log"
    if (previous.get("status") != "prepared" or previous.get("exit_status") != 0
            or previous.get("oracle_fingerprint") != expected_oracle
            or previous.get("base_asset") != base_asset or not log.is_file()
            or file_hash(log) != previous.get("log_sha256")):
        raise ContractError("overlay-only refresh requires a hash-bound successful public build")
    cache, output = target / "thin-home", target / "overlay"
    proposed, removed = dependency_overlay(source, cache, seed_names, None)
    old = previous["overlay_files"]
    if any(proposed.get(name) != digest or file_hash(output / name) != digest for name, digest in old.items()):
        raise ContractError("overlay-only refresh changed a previously bound positive dependency")
    if removed != previous.get("removed_files", []):
        raise ContractError("overlay-only refresh changed the original seed deletion set")
    additions = set(proposed) - set(old)
    remote_ivy = set(remote_ivy_files(cache)) if include_remote_ivy else set()
    for name in additions:
        path = cache / name
        if name in remote_ivy:
            continue
        if not name.startswith(".cache/coursier/"):
            raise ContractError("overlay-only refresh needs explicit remote Ivy admission")
        relative = Path(name).relative_to(".cache/coursier")
        if not pom_only_negative_cache(path, relative):
            raise ContractError("overlay-only refresh may add only POM-only negative cache metadata")
    for name in additions:
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cache / name, path)
    return {
        **previous, "preparation_policy": PREPARATION_POLICY, "refresh_overlay_only": True,
        "prior_preparation_sha256": canonical_hash(previous), "overlay_files": proposed, "removed_files": removed,
        "negative_cache_additions": sorted(additions - remote_ivy),
        "remote_ivy_additions": sorted(additions & remote_ivy),
        "refresh_remote_ivy": include_remote_ivy,
        "overlay_bytes": sum((output / name).stat().st_size for name in proposed),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--legacy-root", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--task-ids", type=Path, required=True)
    parser.add_argument("--toolchains", type=Path, required=True)
    parser.add_argument("--sbt-boot-root", type=Path)
    parser.add_argument("--jvm-shared-assets", type=Path,
                        help="shared immutable JVM descriptor index reused across output batches")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=int, default=1200)
    parser.add_argument("--refresh-overlay-only", action="store_true",
                        help="retain a proven positive dependency cache and add only validated POM-only negative metadata")
    parser.add_argument("--refresh-remote-ivy", action="store_true",
                        help="repackage a proven public build, also retaining hash-bound remote Ivy downloads")
    parser.add_argument("--resolve-only", action="store_true",
                        help="reuse a proven public build and resolve original Test/update scopes plus explicit opt-ins")
    parser.add_argument("--dependency-project", action="append", default=[],
                        help="also resolve a literal public project named by dependency-failure evidence")
    parser.add_argument("--classifier-project", action="append", default=[],
                        help="also run literal PROJECT/updateClassifiers, including sources and javadoc "
                             "(at most 16 project IDs, each 1-128 characters)")
    args = parser.parse_args()
    if args.resolve_only and (args.refresh_overlay_only or args.refresh_remote_ivy):
        parser.error("--resolve-only is separate from metadata-only overlay refresh")
    if args.classifier_project and (args.refresh_overlay_only or args.refresh_remote_ivy):
        parser.error("--classifier-project requires resolution, not metadata-only overlay refresh")
    try:
        classifier_arguments(args.classifier_project)
    except ContractError as exc:
        parser.error(str(exc))
    config = load_config(args.config)
    config["migration"] = {**config.get("migration", {}),
                           "toolchain_registry": str(args.toolchains.resolve())}
    if args.jvm_shared_assets:
        config["migration"]["jvm_shared_assets"] = str(args.jvm_shared_assets.resolve())
    tasks = selected_tasks(Path(config["tasks"]), Path(config["scope_ids"]) if config.get("scope_ids") else None)
    oracles = read_jsonl(Path(config["oracles"]))
    ids = set(args.task_ids.read_text().splitlines())
    if not ids or not ids <= tasks.keys() or args.timeout_seconds < 1:
        raise ContractError("sbt preparation requires valid task IDs and time bounds")
    services = ExecutionServices(config)
    output = args.out_dir.resolve()
    config["migration"].setdefault("jvm_shared_assets", str(output.parent / "jvm-shared-assets"))
    home = args.legacy_root.resolve() / "jvm/cache/home"
    names = sbt_cache_files(home)
    with output_lock(output):
        ctx = MigrationContext(config, tasks, oracles, args.legacy_root.resolve(), output / "assets")
        with services.gate.acquire(ResourceRequest("sbt-base-dependencies", cpus=1, memory_bytes=GiB,
                                                   scratch_bytes=2 * GiB)):
            base_asset = shared_archive(ctx, "sbt", home, names)
        tools = output / "tools"
        tools.mkdir(exist_ok=True)
        shutil.copy2(args.legacy_root / "jvm/cache/tools/sbt", tools / "sbt")

        def prepare(ident):
            task, oracle = tasks[ident], oracles[ident]
            target = output / component(ident)
            target.mkdir(exist_ok=True)
            retain_preparation_attempt(target)
            manifest = target / "preparation.json"
            record = {"id": ident, "start_sha": task["start_sha"], "oracle_fingerprint": canonical_hash(oracle),
                      "expected_tasks": len(tasks), "base_asset": base_asset, "status": "preparing",
                      "scoring_result": False, "model_calls": 0, "preparation_policy": PREPARATION_POLICY}
            try:
                old = {}
                requested = dependency_request(
                    oracle["command"], args.dependency_project, args.classifier_project,
                    resolve_only=args.resolve_only,
                )
                record.update(dependency_request=requested, dependency_request_sha256=canonical_hash(requested))
                resolution_arguments = requested["arguments"]
                if manifest.exists():
                    old = json.loads(manifest.read_text())
                    if (old.get("status") == "prepared" and old.get("exit_status") == 0
                            and old.get("id") == ident and old.get("start_sha") == task["start_sha"]
                            and old.get("oracle_fingerprint") == record["oracle_fingerprint"]
                            and old.get("base_asset") == base_asset
                            and cached_request_matches(old, requested)
                            and (not resolution_arguments
                                 or (old.get("test_dependency_resolution", {}).get("status") == "resolved"
                                     and old["test_dependency_resolution"].get("exit_status") == 0
                                     and old["test_dependency_resolution"].get("arguments") == resolution_arguments))):
                        public_build_receipt(target, old, task, record["oracle_fingerprint"], base_asset)
                        validate_cached_dependencies(target, old)
                        return old
                if args.refresh_overlay_only or args.refresh_remote_ivy:
                    if old.get("id") != ident or old.get("start_sha") != task["start_sha"]:
                        raise ContractError("sbt overlay refresh belongs to a different frozen task")
                    with services.gate.acquire(ResourceRequest("sbt-overlay-refresh:" + ident, cpus=1,
                                                              memory_bytes=GiB, scratch_bytes=GiB)):
                        refreshed = refresh_prepared_overlay(
                            target, home, names, old, record["oracle_fingerprint"], base_asset,
                            include_remote_ivy=args.refresh_remote_ivy,
                        )
                    atomic_json(manifest, refreshed)
                    return refreshed
                public_receipt = None
                if args.resolve_only:
                    if not resolution_arguments:
                        raise ContractError("sbt resolve-only has no original test configuration")
                    public_receipt = public_build_receipt(
                        target, old, task, record["oracle_fingerprint"], base_asset)
                    record["prior_public_build"] = public_receipt
                    record["prior_preparation_sha256"] = canonical_hash(old)
                arguments = preparation_arguments(
                    oracle["command"], resolution_arguments, resolve_only=args.resolve_only,
                )
                command = sbt_prefix() + " " + shlex.join(arguments)
                resolution_log = "resolve-classifiers.log" if args.classifier_project else "resolve-test-dependencies.log"
                log_path = target / (resolution_log if args.resolve_only else "install.log")
                repository = ctx.repo(task)
                profile = preparation_profile(args.resolve_only)
                record["resource_profile"] = profile
                request = ResourceRequest("sbt-prepare:" + ident, cpus=profile["cpus"],
                                           memory_bytes=profile["memory_gib"] * GiB,
                                           scratch_bytes=services.source_bytes(task, repository) * 2
                                           + profile["scratch_gib"] * GiB, heavy=profile["heavy"])
                with services.gate.acquire(request, uses_containers=True) as lease:
                    image, audit = ctx.image(oracle["environment"]["container"])
                    cache = target / "thin-home"
                    seed_thin_cache(home, cache, names)
                    if args.sbt_boot_root:
                        for prepared in args.sbt_boot_root.glob("*/home"):
                            for name in sbt_boot_files(prepared):
                                destination = cache / name
                                if destination.is_symlink():
                                    destination.unlink()
                                destination.parent.mkdir(parents=True, exist_ok=True)
                                shutil.copy2(prepared / name, destination)
                    with tempfile.TemporaryDirectory(prefix="sbt-dependency-source-", dir=services.scratch) as temporary:
                        workspace = Path(temporary) / "workspace"
                        with start_archive(repository, task["start_sha"]) as stream:
                            extract_tar(stream, workspace)
                        initialize_git(workspace)
                        protected = [path for path in workspace.rglob("*") if path.is_file() and (
                            path.suffix == ".sbt" or path.name in {"build.properties", ".sbtopts", ".jvmopts"})]
                        before = {str(path.relative_to(workspace)): file_hash(path) for path in protected}
                        if public_receipt and before != public_receipt["input_hashes"]:
                            raise ContractError("sbt resolve-only public build inputs changed")
                        container = "rb-sbt-prepare-" + uuid.uuid4().hex[:16]
                        argv = [
                            "docker", "run", "--rm", "--init", "--name", container, "--read-only",
                            "--network", "bridge", "--user", f"{os.getuid()}:{os.getgid()}",
                            "--cpus", "2", "--cpuset-cpus", bounded_cpu_set(2, lease["lease"]),
                            "--memory", str(profile["memory_gib"] * 1024) + "m",
                            "--pids-limit", "512", "--cap-drop=ALL",
                            "--security-opt=no-new-privileges", "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=1g",
                            "--label", "reviewbench.resource-lease=" + lease["lease"],
                            "--mount", f"type=bind,source={workspace},target=/workspace",
                            "--mount", f"type=bind,source={tools},target={TOOLS},readonly",
                            "--mount", f"type=bind,source={home},target={SBT_BASE},readonly",
                            "--mount", f"type=bind,source={cache},target={SBT_CACHE}",
                            "-e", "HOME=/tmp", "-e", "CI=true", "-e", "USER=reviewbench",
                            "-e", "COURSIER_CACHE=" + SBT_CACHE + "/.cache/coursier",
                            "-e", "COURSIER_PARALLEL_DOWNLOAD_COUNT=2",
                            "-e", "JVM_OPTS=" + sbt_jvm_options(),
                            "-e", "_JAVA_OPTIONS=" + sbt_jvm_options(),
                            "-w", "/workspace", "--entrypoint", "bash", image, "-c", command,
                        ]
                        record.update(command=shlex.join(argv), image=image, image_audit=audit,
                                      input_hashes=before, log=str(log_path))
                        atomic_json(manifest, record)
                        started = time.monotonic()
                        with log_path.open("w") as log:
                            CURRENT_JOB.get()["mark_container_use"]()
                            process = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT)
                            try:
                                while process.poll() is None:
                                    if time.monotonic() - started > args.timeout_seconds:
                                        raise ContractError("sbt dependency preparation exceeded its time bound")
                                    if shutil.disk_usage(services.scratch).free < 80 * GiB:
                                        raise ContractError("sbt preparation reached the disk floor")
                                    time.sleep(.5)
                            finally:
                                if process.poll() is None:
                                    subprocess.run(["docker", "stop", "-t", "5", container], capture_output=True, timeout=20)
                                    process.wait(timeout=30)
                        record.update(exit_status=process.returncode, seconds=round(time.monotonic() - started, 2),
                                      log_sha256=file_hash(log_path))
                        if any(file_hash(workspace / name) != digest for name, digest in before.items()):
                            raise ContractError("sbt changed an original build/dependency descriptor")
                        if process.returncode:
                            raise ContractError("sbt public build/dependency resolution failed; cache retained")
                        if resolution_arguments:
                            record["test_dependency_resolution"] = {
                                "status": "resolved", "arguments": resolution_arguments,
                                "command": shlex.join(argv), "log": str(log_path),
                                "log_sha256": file_hash(log_path), "exit_status": process.returncode,
                                "seconds": record["seconds"], "resolve_only": args.resolve_only,
                            }
                        if public_receipt:
                            record.update({key: public_receipt[key] for key in
                                           ("command", "exit_status", "seconds", "log_sha256", "input_hashes")})
                            record["log"] = str(target / "install.log")
                    overlay, removed = dependency_overlay(home, cache, names, target / "overlay")
                    if public_receipt and any(overlay.get(name) != digest
                                              for name, digest in old.get("overlay_files", {}).items()):
                        raise ContractError("sbt resolve-only changed a previously bound public dependency")
                    if public_receipt and removed != old.get("removed_files", []):
                        raise ContractError("sbt resolve-only changed the original seed deletion set")
                    record.update(status="prepared", overlay_files=overlay, removed_files=removed,
                                  overlay_bytes=sum((target / "overlay" / name).stat().st_size for name in overlay))
            except Exception as exc:
                record.update(status="pending", error=f"{type(exc).__name__}: {exc}")
            atomic_json(manifest, record)
            return record
        records = {}
        for ident, record in completed_jobs(prepare, sorted(ids), args.workers):
            records[ident] = record
            print(json.dumps({key: record.get(key) for key in ("id", "status", "exit_status", "overlay_bytes", "error")}),
                  flush=True)
            atomic_json(output / "preparation-summary.json", {"expected_tasks": len(tasks), "records": records})
        atomic_json(output / "resource-usage.json", services.gate.status())
    return 0 if records and all(record["status"] == "prepared" for record in records.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
