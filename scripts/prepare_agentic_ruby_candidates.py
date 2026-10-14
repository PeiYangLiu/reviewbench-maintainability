#!/usr/bin/env python3
"""Prepare only the two Ruby candidates, using the engine beside --config.

engine-src.txt and the default scripted-toolchains.json live beside --config.
No engine import occurs until that mandatory source has been selected. Existing
prepared packages are validated before gate admission and never re-resolved.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from types import SimpleNamespace
import uuid

OWNED_CODE = (
    "scripts/prepare_agentic_ruby_candidates.py",
    "scripts/agentic_runtime_migration/ruby_candidate.py",
    "scripts/agentic_runtime_migration/ruby_candidate_lane.py",
    "tests/test_agentic_ruby_candidate.py",
    "tests/test_agentic_ruby_candidate_integration.py",
)
DISK_FLOOR = 80 * 1024**3


def select_engine(engine_file):
    source = Path(Path(engine_file).read_text().strip())
    if not source.is_absolute() or not (source / "reviewbench_maintainability/agentic/contracts.py").is_file():
        raise RuntimeError("engine-src.txt must name the current absolute engine source")
    source = source.resolve()
    configured = os.environ.get("REVIEWBENCH_ENGINE_SRC")
    if configured and Path(configured).resolve() != source:
        raise RuntimeError("REVIEWBENCH_ENGINE_SRC differs from the mandatory engine-src.txt")
    for name, module in list(sys.modules.items()):
        if name.startswith("reviewbench_maintainability") and getattr(module, "__file__", None):
            if not Path(module.__file__).resolve().is_relative_to(source):
                raise RuntimeError("another engine was imported before Ruby preparation")
    sys.path.insert(0, str(source))
    os.environ["REVIEWBENCH_ENGINE_SRC"] = str(source)
    return source


def _api():
    from reviewbench_maintainability.agentic.cli import load_config
    from reviewbench_maintainability.agentic.contracts import (
        ContractError, atomic_json, canonical_hash, component, file_hash, read_jsonl, selected_tasks,
    )
    from reviewbench_maintainability.agentic.execution import ExecutionServices
    from reviewbench_maintainability.agentic.resources import GiB, ResourceRequest
    from reviewbench_maintainability.agentic.scheduling import completed_jobs, output_lock
    from reviewbench_maintainability.agentic.snapshots import extract_tar, initialize_git, start_archive
    from agentic_runtime_migration.common import MigrationContext, bounded_cpu_set, retain_preparation_attempt
    from agentic_runtime_migration import ruby_candidate as ruby, ruby_candidate_lane as lane
    return SimpleNamespace(**locals())


def snapshot_code(output):
    """Retain the executable implementation and tests before any real install."""
    source = Path(__file__).resolve().parents[1]
    files = {name: (source / name).read_bytes() for name in OWNED_CODE if (source / name).is_file()}
    hashes = {name: hashlib.sha256(value).hexdigest() for name, value in files.items()}
    digest = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    directory = Path(output) / "code-snapshots" / digest
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / "code.tar"
    if not archive.exists():
        with tarfile.open(archive, "w") as stream:
            for name, value in sorted(files.items()):
                entry = tarfile.TarInfo(name)
                entry.size, entry.mode = len(value), 0o644
                stream.addfile(entry, io.BytesIO(value))
    with tarfile.open(archive) as stream:
        if {entry.name: stream.extractfile(entry).read() for entry in stream.getmembers()} != files:
            raise RuntimeError("retained Ruby code snapshot differs from executing code")
    record = {"files_sha256": hashes, "archive": str(archive),
              "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}
    (directory / "snapshot.json").write_text(json.dumps(record, sort_keys=True, indent=2) + "\n")
    return record


def bootstrap_command(api, request):
    bootstrap = api.lane.CACHE + "/bootstrap"
    probe = 'gem "bundler", "= 2.4.19"; require "bundler"; abort unless Bundler::VERSION == "2.4.19"'
    command = api.ruby.resolution_command(
        request, source="/workspace", state=api.lane.STATE,
        bundle_path=api.lane.CACHE + "/gems", bootstrap_path=bootstrap)
    return (
        "mkdir -p " + shlex.quote(bootstrap) + " " + shlex.quote(api.lane.CACHE + "/gems") +
        " && if " + shlex.join(["ruby", "-e", probe]) +
        "; then printf '%s\\n' 'Bundler 2.4.19 already available in pinned environment'; "
        "else " + shlex.join(["gem", "install", "bundler", "--version", "2.4.19",
                              "--no-document", "--install-dir", bootstrap]) + " || exit $?; fi && " + command
    )


def container_argv(api, source, payload, image, lease, command, environment):
    container = "rb-ruby-prepare-" + uuid.uuid4().hex[:16]
    argv = [
        "docker", "run", "--rm", "--name", container, "--init", "--read-only", "--network", "bridge",
        "--user", f"{os.getuid()}:{os.getgid()}", "--cpus", "2",
        "--cpuset-cpus", api.bounded_cpu_set(2, lease["lease"]), "--memory", "4096m", "--pids-limit", "256",
        "--cap-drop=ALL", "--security-opt=no-new-privileges", "--label", "reviewbench.resource-lease=" + lease["lease"],
        "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=512m",
        "--mount", f"type=bind,source={source},target=/workspace",
        "--mount", f"type=bind,source={payload},target={api.lane.CACHE}", "-w", "/workspace",
    ]
    for key, value in sorted(environment.items()):
        argv += ["-e", key + "=" + value]
    argv += ["--entrypoint", "/bin/bash", image, "-c", command]
    return container, argv


def run_install(argv, container, log, *, scratch, timeout):
    if shutil.disk_usage(scratch).free < DISK_FLOOR:
        raise RuntimeError("Ruby preparation reached the 80-GiB disk floor before launch")
    started = time.monotonic()
    with Path(log).open("w") as stream:
        process = subprocess.Popen(argv, stdout=stream, stderr=subprocess.STDOUT)
        failure = None
        try:
            while process.poll() is None:
                if time.monotonic() - started > timeout:
                    raise TimeoutError("Ruby preparation exceeded its time bound")
                if shutil.disk_usage(scratch).free < DISK_FLOOR:
                    raise RuntimeError("Ruby preparation reached the 80-GiB disk floor")
                time.sleep(.5)
        except Exception as exc:
            failure = exc
        finally:
            if process.poll() is None:
                subprocess.run(["docker", "stop", "-t", "5", container], capture_output=True, timeout=20)
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    subprocess.run(["docker", "kill", container], capture_output=True, timeout=20)
                    process.kill()
                    process.wait(timeout=30)
        if failure is not None:
            failure.exit_status = process.returncode
            failure.seconds = round(time.monotonic() - started, 2)
            raise failure
    return process.returncode, round(time.monotonic() - started, 2)


def audit_image(api, ctx, image, lease):
    """Same audit contract as MigrationContext, with bounded audit containers."""
    from reviewbench_maintainability.agentic.environment import docker
    observed = json.loads(docker("image", "inspect", image))[0]["Id"]
    if observed != image:
        raise api.ContractError("actual Ruby image differs from its pinned registry ID")
    path = ctx.output / "image-audits" / (image.split(":")[1] + ".json")
    if not path.exists():
        scan = ("find / -xdev \\( -name .git -o -name '*source-only-golden.patch' "
                "-o -name '*golden*.patch' -o -name oracles.jsonl "
                "-o -name generation_results.jsonl \\) -print 2>/dev/null")
        result = docker(
            "run", "--rm", "--read-only", "--network", "none", "--cpus", "2", "--memory", "4096m",
            "--pids-limit", "256", "--cpuset-cpus", api.bounded_cpu_set(2, lease["lease"]),
            "--label", "reviewbench.resource-lease=" + lease["lease"],
            "--entrypoint", "/bin/bash", image, "-c", scan, timeout=180)
        if result.strip():
            raise api.ContractError("Ruby tool image audit found repository/reference material")
        history = [json.loads(line) for line in docker("history", "--no-trunc", "--format", "{{json .}}", image).splitlines()]
        api.atomic_json(path, {
            "schema_version": "reviewbench-developer-image-audit/v1", "image": image,
            "expected_image_id": image, "no_reference_assets": True, "no_repository_history": True,
            "basis": "immutable image, retained history, resource-bounded executed rootfs scan",
            "rootfs_scan_command": scan, "rootfs_scan_output": result, "history": history,
        })
    saved = json.loads(path.read_text())
    if (saved.get("image") != image or saved.get("expected_image_id") != image
            or saved.get("no_reference_assets") is not True or saved.get("no_repository_history") is not True
            or saved.get("rootfs_scan_output") != ""):
        raise api.ContractError("retained Ruby image audit differs from the pinned image contract")
    return {"path": str(path), "sha256": api.file_hash(path)}


def prepare_one(api, args, *, ctx, services, task, oracle, case, total_tasks, image, engine, code):
    target = args.out_dir / api.component(task["id"])
    target.mkdir(parents=True, exist_ok=True)
    record_file = target / "preparation.json"
    payload = target / "payload"
    record = {"id": task["id"], "repo": task["repo"], "start_sha": task["start_sha"], "status": "preparing",
              "expected_tasks": total_tasks, "model_calls": 0, "scoring_result": False,
              "engine": engine, "code_snapshot": code, "image": image}
    # Never rewrite a published manifest, even when current validation fails.
    published = payload.exists() or payload.is_symlink() or record_file.is_symlink()
    try:
        if record_file.is_symlink():
            raise api.ContractError("Ruby preparation manifest is redirected")
        try:
            old = json.loads(record_file.read_text()) if record_file.is_file() else None
        except Exception:
            published = True
            raise
        published = published or bool(old and old.get("status") == "prepared")
        plan = api.lane.case_plan(task, case)
        repository = ctx.repo(task)
        start = api.ruby.inspect_start(repository, task, plan)
        if published:
            if not old or old.get("status") != "prepared":
                raise api.ContractError("published Ruby payload requires validation, not a new resolution")
            return api.lane.validate_record(old, payload=payload, task=task, case=case, plan=plan,
                                            start=start, image=image, engine=engine)
        request = api.ruby.prepare_or_reuse(plan, start, image, resolve_unlocked=args.resolve_unlocked)
        api.retain_preparation_attempt(target)
        record.update(plan=plan, start=start, config_fingerprint=api.canonical_hash(case))
        api.atomic_json(record_file, record)
        size = services.source_bytes(task, repository)
        resources = api.ResourceRequest("ruby-candidate-prepare:" + task["id"], cpus=2,
                                        memory_bytes=4 * api.GiB, scratch_bytes=size * 2 + 8 * api.GiB,
                                        heavy=size > api.GiB)
        with services.gate.acquire(resources, uses_containers=True, timeout=args.timeout_seconds) as lease:
            if api.lane.engine_binding(args.engine_file) != engine:
                raise api.ContractError("engine-src.txt changed during Ruby preparation")
            if shutil.disk_usage(services.scratch).free < DISK_FLOOR:
                raise api.ContractError("Ruby preparation reached the 80-GiB disk floor")
            record["image_audit"] = audit_image(api, ctx, image, lease)
            record["resource_lease"] = lease["lease"]
            with tempfile.TemporaryDirectory(prefix="ruby-candidate-prepare-", dir=services.scratch) as temporary:
                root = Path(temporary)
                source, staged = root / "source", root / "payload"
                with api.start_archive(repository, task["start_sha"], timeout=args.timeout_seconds) as stream:
                    api.extract_tar(stream, source)
                if api.ruby.tree_fingerprint(source, source=True) != start["source_sha256"]:
                    raise api.ContractError("Ruby preparation checkout differs from the complete exact start")
                api.initialize_git(source)
                (staged / "gems").mkdir(parents=True)
                (staged / "bootstrap").mkdir()
                environment = {
                    "HOME": "/tmp", "CI": "true", "MAKEFLAGS": "-j2", "CMAKE_BUILD_PARALLEL_LEVEL": "2",
                    **api.ruby.runtime_environment(plan, source="/workspace", state=api.lane.STATE,
                                                   bundle_path=api.lane.CACHE + "/gems",
                                                   bootstrap_path=api.lane.CACHE + "/bootstrap"),
                }
                environment["BUNDLE_FROZEN"] = "false"
                command = bootstrap_command(api, request)
                container, argv = container_argv(api, source, staged, image, lease, command, environment)
                record.update(command=shlex.join(argv), environment_sha256=api.canonical_hash(environment),
                              log=str(target / "install.log"))
                api.atomic_json(record_file, record)
                status, seconds = run_install(argv, container, record["log"], scratch=services.scratch,
                                              timeout=args.timeout_seconds)
                record.update(exit_status=status, seconds=seconds, log_sha256=api.file_hash(Path(record["log"])))
                if status:
                    raise api.ContractError("original bounded Ruby dependency setup failed")
                state = source / api.lane.STATE.removeprefix("/workspace/")
                inner = api.ruby.capture_resolution(staged, request, source=source, state=state)
                if payload.exists() or payload.is_symlink():
                    raise api.ContractError("Ruby payload appeared before publication; no overwrite")
                shutil.copytree(staged, payload, symlinks=True)
                api.ruby.validate_prepared(payload, inner, plan=plan, start=start, image=image)
                record.update(status="prepared", ruby_candidate=inner, lock_origin=api.ruby.ORIGIN,
                              lock_sha256=inner["lock_sha256"])
    except Exception as exc:
        record.update(status="pending", error=f"{type(exc).__name__}: {exc}")
        if getattr(exc, "exit_status", None) is not None:
            record.update(exit_status=exc.exit_status, seconds=exc.seconds)
        log = target / "install.log"
        if log.is_file():
            record.update(log=str(log), log_sha256=api.file_hash(log))
    if not published:
        api.atomic_json(record_file, record)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--legacy-root", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--task-ids", type=Path, required=True)
    parser.add_argument("--toolchains", type=Path)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--resolve-unlocked", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=1200)
    args = parser.parse_args(argv)
    args.config = args.config.resolve()
    args.engine_file = args.config.parent / "engine-src.txt"
    select_engine(args.engine_file)
    api = _api()
    if args.workers not in (1, 2) or args.timeout_seconds < 1:
        raise api.ContractError("Ruby preparation requires one/two workers and a positive timeout")
    args.out_dir = args.out_dir.resolve()
    args.legacy_root = args.legacy_root.resolve()
    args.toolchains = (args.toolchains or args.config.parent / "scripted-toolchains.json").resolve()
    pinned = api.lane.pinned_toolchain(args.toolchains)
    engine = api.lane.engine_binding(args.engine_file)
    config = api.load_config(args.config)
    config["migration"] = {**config.get("migration", {}), "toolchain_registry": str(args.toolchains)}
    tasks = api.selected_tasks(Path(config["tasks"]), Path(config["scope_ids"]) if config.get("scope_ids") else None)
    oracles = api.read_jsonl(Path(config["oracles"]))
    batch = set(args.task_ids.read_text().splitlines())
    if not batch or not batch <= api.ruby.TARGETS.keys() or not batch <= tasks.keys():
        raise api.ContractError("Ruby preparation accepts only its two explicit task IDs")
    cases = json.loads((args.legacy_root / "scripted/oracle_config.json").read_text())["tasks"]
    services = api.ExecutionServices(config)
    with api.output_lock(args.out_dir):
        code = snapshot_code(args.out_dir)
        ctx = api.MigrationContext(config, tasks, oracles, args.legacy_root, args.out_dir / "toolchains")
        records = {}

        def prepare(ident):
            return prepare_one(api, args, ctx=ctx, services=services, task=tasks[ident],
                               oracle=oracles[ident], case=cases[str(oracles[ident]["index"])],
                               total_tasks=len(tasks), image=pinned["image"], engine=engine, code=code)

        for ident, record in api.completed_jobs(prepare, sorted(batch), args.workers):
            records[ident] = record
            print(json.dumps({key: record.get(key) for key in ("id", "status", "exit_status", "error")}), flush=True)
            api.atomic_json(args.out_dir / "preparation-summary.json", {
                "expected_tasks": len(tasks), "batch_tasks": len(batch), "model_calls": 0,
                "prepared": sum(item["status"] == "prepared" for item in records.values()), "records": records,
                "engine": engine, "code_snapshot": code,
            })
        api.atomic_json(args.out_dir / "resource-usage.json", services.gate.status())
    return 0 if records and all(item["status"] == "prepared" for item in records.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
