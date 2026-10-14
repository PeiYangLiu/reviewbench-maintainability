"""Bounded, hash-attested preparation for C# current-project recipes.

The retained package feed is read-only. Only downloaded dependency packages,
the exact declared submodule, and newly source-built generic tooling become
assets. No production build outputs are exported. This command never registers
a runtime or invokes a model; dependency preparation is not verification.
"""
from __future__ import annotations

import argparse
import base64
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
import zipfile

sys.path.insert(0, os.environ.get("REVIEWBENCH_ENGINE_SRC", str(Path(__file__).resolve().parents[2] / "src")))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reviewbench_maintainability.agentic.cli import load_config
from reviewbench_maintainability.agentic.contracts import (
    ContractError, atomic_json, canonical_hash, component, file_hash, read_jsonl, selected_tasks,
)
from reviewbench_maintainability.agentic.environment import docker
from reviewbench_maintainability.agentic.execution import ExecutionServices
from reviewbench_maintainability.agentic.models import engine_identity
from reviewbench_maintainability.agentic.resources import CURRENT_JOB, GiB, ResourceRequest
from reviewbench_maintainability.agentic.snapshots import (
    apply_patch, extract_tar, git, start_archive, tree_files,
)
from agentic_runtime_migration.common import MigrationContext, bounded_cpu_set
from agentic_runtime_migration.csharp_candidate import (
    POLICY, PLANS, LEGACY_PROJECT_INDICES, build_commands, curate_harness, plan_for, sdk_version,
)
from agentic_runtime_migration.csharp_staging import PROJECT, SOURCE

SQL_IMAGE = "sha256:90488d58c6a5c19f24ff716e14330b85b5b26ee54b44a36ea24e6206533e7edd"
POSTGRES_BASE = "pgvector/pgvector@sha256:a20a57d7aa5217a6af0a391ccf69f4a8512406d6c14be08132f801468cc3cc62"
EXPECTED_ENGINE = "5142d1fec94eb6c5a6177a66ee7ce8533a26f0a2578280075a6947a399c01ca3"


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def run_container(image, workspace, command, log, *, mounts=(), network="none", timeout=1200, cpus=4, memory_gib=8):
    """A child can only start inside an admitted, container-owning original gate."""
    job = CURRENT_JOB.get()
    if not job:
        raise ContractError("C# dependency/fixture build requires the shared resource gate")
    job["mark_container_use"]()
    name = "rb-csharp-prepare-" + uuid.uuid4().hex[:12]
    argv = ["docker", "run", "--rm", "--name", name, "--read-only", "--init",
            "--user", f"{os.getuid()}:{os.getgid()}", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--pids-limit", "1024",
            "--cpus", str(cpus), "--cpuset-cpus", bounded_cpu_set(cpus, name),
            "--memory", str(memory_gib) + "g", "--network", network, "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=2g",
            "--label", "reviewbench.resource-lease=" + job["lease"],
            "--mount", f"type=bind,source={workspace.resolve()},target=/work",
            "-w", "/work", "-e", "HOME=/work/home", "-e", "DOTNET_CLI_HOME=/work/home",
            "-e", "DOTNET_CLI_TELEMETRY_OPTOUT=1", "-e", "DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1",
            "-e", "DOTNET_NOLOGO=1", "-e", "DOTNET_ROLL_FORWARD=Major",
            "-e", f"DOTNET_PROCESSOR_COUNT={cpus}", "-e", "MSBUILDDISABLENODEREUSE=1",
            "-e", "NUGET_PACKAGES=/work/packages", "-e", "NUGET_HTTP_CACHE_PATH=/work/http-cache",
            "-e", "GIT_CONFIG_NOSYSTEM=1", "-e", "GIT_CONFIG_GLOBAL=/dev/null"]
    for source, target in mounts:
        argv += ["--mount", f"type=bind,source={Path(source).resolve()},target={target},readonly"]
    argv += ["--entrypoint", "/bin/bash", image, "-c",
             "mkdir -p /work/home /work/packages; exec timeout --signal=TERM --kill-after=5s " +
             str(timeout) + "s /bin/bash -c " + shlex.quote("set -euo pipefail; " + command)]
    log.parent.mkdir(parents=True, exist_ok=True)
    began = now()
    started = time.monotonic()
    with log.open("w") as stream:
        try:
            result = subprocess.run(argv, stdout=stream, stderr=subprocess.STDOUT, timeout=timeout + 45)
            code = result.returncode
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
            code = 124
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
    record = {"command": shlex.join(argv), "input_command": command, "image": image,
              "started_at_utc": began, "ended_at_utc": now(), "seconds": round(time.monotonic() - started, 3),
              "exit_status": code, "log": str(log), "log_sha256": file_hash(log),
              "lease": job["lease"], "pid": os.getpid(), "container_name": name}
    atomic_json(log.with_suffix(".json"), record)
    return record


def prepare_tool(ctx, services, registry):
    image = registry["rb-csharp-sdk8-cov"]["image"]
    version = sdk_version(registry, "rb-csharp-sdk8-cov")
    ident = canonical_hash({"source": SOURCE, "project": PROJECT, "image": image, "sdk": version,
                            "build_limits": {"cpus": 2, "memory_gib": 2, "scratch_gib": 1}})
    root = ctx.output / "tools" / ident[:20]
    result_path = root / "tool.json"
    if result_path.is_file():
        result = json.loads(result_path.read_text())
        for name, digest in result["output_hashes"].items():
            if file_hash(root / "output" / name) != digest:
                raise ContractError("source-built C# tool changed")
        return result
    root.mkdir(parents=True, exist_ok=True)
    (root / "CandidateStage.csproj").write_text(PROJECT)
    (root / "Program.cs").write_text(SOURCE)
    (root / "global.json").write_text(json.dumps({"sdk": {"version": version, "rollForward": "disable"}}))
    (root / "NuGet.Config").write_text("<configuration><packageSources><clear /></packageSources></configuration>\n")
    with services.gate.acquire(ResourceRequest(
        "csharp:source-built-staging-tool", cpus=2, memory_bytes=2 * GiB, scratch_bytes=GiB, heavy=True,
    ), uses_containers=True, timeout=3600):
        record = run_container(image, root,
                               "dotnet build CandidateStage.csproj --nologo -v minimal -m:1 "
                               "-p:UseSharedCompilation=false -nr:false -o /work/output",
                               root / "build.log", timeout=180, cpus=2, memory_gib=2)
        if record["exit_status"]:
            raise ContractError("C# source-built staging tool failed: " + str(root / "build.log"))
        descriptor = ctx.bundle(root / "output", role="dependency")
    result = {"input_sha256": ident, "sdk_version": version, "image": image,
              "asset": descriptor, "output": str(root / "output"), "build": record,
              "output_hashes": {n: file_hash(p) for n, p in tree_files(root / "output").items()}}
    atomic_json(result_path, result)
    return result


def reuse_tool(path, registry):
    """Reuse only the exact source-built generic stager and its original receipt."""
    path = Path(path)
    value = json.loads(path.read_text())
    output = Path(value["output"])
    actual = tree_files(output)
    expected = value["output_hashes"]
    if (value["image"] != registry["rb-csharp-sdk8-cov"]["image"]
            or value["sdk_version"] != sdk_version(registry, "rb-csharp-sdk8-cov")
            or value["build"]["exit_status"] != 0
            or set(actual) != set(expected)):
        raise ContractError("reused C# staging tool build/output binding differs")
    for name, digest in expected.items():
        if actual[name].is_symlink() or file_hash(actual[name]) != digest:
            raise ContractError("reused C# staging tool bytes differ: " + name)
    for name, text in (("Program.cs", SOURCE), ("CandidateStage.csproj", PROJECT)):
        source = output.parent / name
        if source.is_symlink() or source.read_bytes() != text.encode():
            raise ContractError("reused C# staging tool source differs: " + name)
    asset = value["asset"]
    descriptor = Path(asset["path"])
    if descriptor.is_symlink() or file_hash(descriptor) != asset["sha256"]:
        raise ContractError("reused C# staging tool descriptor differs")
    if json.loads(descriptor.read_text()).get("source_kind") != "dependency":
        raise ContractError("reused C# staging tool is not a dependency asset")
    return value


def validate_preparation_cohort(ids, tasks, oracles):
    """Keep the original eight intact; allow index26 only as its own cohort."""
    if not ids or len(set(ids)) != len(ids) or not set(ids) <= set(tasks):
        raise ContractError("C# preparation cohort contains missing/duplicate/foreign tasks")
    indices = [int(oracles[ident]["index"]) for ident in ids]
    if not ((len(ids) == 8 and set(indices) == LEGACY_PROJECT_INDICES)
            or (len(ids) == 1 and indices == [26])):
        raise ContractError("C# preparation needs the original eight or the separate index26 cohort")


def prepare_postgres(ctx, services):
    root = ctx.output / "services/postgres"
    path = root / "image.json"
    if path.is_file():
        value = json.loads(path.read_text())
        if json.loads(docker("image", "inspect", value["image"]))[0]["Id"] != value["image"]:
            raise ContractError("private Postgres image changed")
        return value["image"]
    root.mkdir(parents=True, exist_ok=True)
    text = ("FROM " + POSTGRES_BASE + "\n"
            "ENV PGDATA=/tmp/reviewbench-pgdata\nUSER postgres\n")
    (root / "Dockerfile").write_text(text)
    with services.gate.acquire(ResourceRequest(
        "csharp:private-postgres-image", cpus=2, memory_bytes=2 * GiB, scratch_bytes=GiB, heavy=True,
    ), uses_containers=True, timeout=3600):
        # This is a metadata-only derivative; no source/tool compilation or new download.
        CURRENT_JOB.get()["mark_container_use"]()
        argv = ["docker", "build", "--network=none", "--resource", "cpu-quota=200000",
                "--resource", "memory=2g", "--iidfile", str(root / "image.id"), str(root)]
        with (root / "build.log").open("w") as stream:
            result = subprocess.run(argv, stdout=stream, stderr=subprocess.STDOUT, timeout=180)
        if result.returncode:
            raise ContractError("private Postgres image build failed: " + str(root / "build.log"))
    image = (root / "image.id").read_text().strip()
    atomic_json(path, {"image": image, "parent": POSTGRES_BASE, "dockerfile_sha256": file_hash(root / "Dockerfile"),
                       "command": shlex.join(argv), "exit_status": result.returncode,
                       "log": str(root / "build.log"), "log_sha256": file_hash(root / "build.log")})
    return image


def pinned_module(ctx, task, directory, plan, destination):
    if not plan.get("module"):
        return None
    modules = ctx.modules(task)
    entry = next((item for item in modules if item["path"] == plan["module"]), None)
    if entry is None:
        raise ContractError("C# required submodule is not declared in exact start")
    retained = directory / "repo" / entry["path"]
    commit = git(retained, "rev-parse", "HEAD").decode().strip()
    if commit != entry["commit"]:
        raise ContractError("retained C# submodule commit differs from the current candidate gitlink")
    checkout = destination / entry["path"]
    if checkout.is_dir():
        checkout.rmdir()  # Git archive's empty gitlink placeholder only.
    with start_archive(retained, entry["commit"]) as stream:
        extract_tar(stream, checkout)
    return {"path": entry["path"], "commit": entry["commit"],
            "asset": ctx.bundle(checkout, role="submodule", git_commit=entry["commit"])}


def package_snapshot(packages, snapshot, shared_feed):
    """Export exact NuGet packages only. No /repo bin, obj, or target DLL enters."""
    feed = snapshot / "feed"
    final = snapshot / "packages"
    feed.mkdir(parents=True, exist_ok=True)
    final.mkdir(parents=True, exist_ok=True)
    (feed / ".feed-root").write_text("Pinned NuGet feed; absence of a package is an explicit restore failure.\n")
    (final / ".packages-root").write_text("No production project build outputs are exported.\n")
    shared_feed.mkdir(parents=True, exist_ok=True)
    records = []
    for path in sorted(packages.glob("*/*/*.nupkg")):
        if path.is_symlink():
            raise ContractError("NuGet package must be a retained regular file")
        relative = path.relative_to(packages)
        with zipfile.ZipFile(path) as archive:
            if not any(n.lower().endswith(".nuspec") for n in archive.namelist()):
                raise ContractError("download is not a NuGet package: " + str(relative))
        sha = file_hash(path)
        sha512 = base64.b64encode(hashlib.sha512(path.read_bytes()).digest()).decode()
        witness = path.with_suffix(path.suffix + ".sha512")
        if witness.exists() and witness.read_text().strip() != sha512:
            raise ContractError("NuGet package differs from its SHA512 witness: " + str(relative))
        target = final / relative.parent
        shutil.copytree(path.parent, target, ignore=shutil.ignore_patterns("*.nupkg.metadata"),
                        symlinks=False, dirs_exist_ok=True)
        for target_root in (feed, shared_feed):
            dest = target_root / path.name
            if dest.exists():
                if file_hash(dest) != sha:
                    raise ContractError("conflicting bytes for pinned NuGet ID/version: " + path.name)
            else:
                shutil.copy2(path, dest)
        records.append({"package": relative.parent.as_posix(), "sha256": sha, "sha512": sha512,
                        "bytes": path.stat().st_size})
    return records


def prepare_one(ctx, services, task, oracle, tool, registry, root, *, timeout=600, reuse_feeds=()):
    path = root / "dependencies.json"
    if path.exists():
        value = json.loads(path.read_text())
        if (value.get("task_id") == task["id"] and value.get("start_sha") == task["start_sha"]
                and value.get("tool") == tool["asset"] and value.get("policy") == POLICY):
            return value  # explicit new output directory is needed for a new attempt
        raise ContractError("existing C# dependency attempt has different inputs")
    root.mkdir(parents=True, exist_ok=True)
    spec = json.loads((ctx.legacy / "csharp/specs.json").read_text())[str(oracle["index"])]
    plan = plan_for(oracle["index"], spec)
    if plan.get("fixed_input_profile") == "graphql3270/v1":
        from agentic_runtime_migration.csharp_graphql3270 import bind_original_inputs
        bind_original_inputs(ctx.task_directory(task, oracle, "csharp"), task, oracle, spec)
    image = registry[plan["sdk"]]["image"]
    version = sdk_version(registry, plan["sdk"])
    directory = ctx.task_directory(task, oracle, "csharp")
    service = None
    if plan.get("service") == "postgres":
        service = prepare_postgres(ctx, services)
    elif plan.get("service") == "sql":
        service = json.loads(docker("image", "inspect", SQL_IMAGE))[0]["Id"]
    record = {"task_id": task["id"], "start_sha": task["start_sha"], "policy": POLICY,
              "image": image, "sdk_version": version, "plan": plan, "started_at_utc": now(),
              "tool": tool["asset"], "status": "partial", "restore_attempts": [],
              "service_image": service, "production_source_rewrites": []}
    shared_feed = ctx.output / "shared-feed"
    shared_feed.mkdir(exist_ok=True)
    retained = ctx.legacy / "csharp/nuget-cache"
    extra_mounts = []
    for index, source in enumerate(reuse_feeds):
        source = Path(source)
        if not source.is_absolute() or not source.is_dir():
            raise ContractError("reused C# dependency feed must be an existing absolute directory")
        extra_mounts.append((source, "/reuse-feed-" + str(index)))
    record["reused_feed_roots"] = [str(source) for source, _ in extra_mounts]
    with services.gate.acquire(ResourceRequest(
        "csharp:fixed-dependencies:" + task["id"], cpus=4, memory_bytes=8 * GiB,
        scratch_bytes=16 * GiB, heavy=True,
    ), uses_containers=True, timeout=ctx.config.get("execution", {}).get("admission_timeout_seconds", 3600)):
        with tempfile.TemporaryDirectory(prefix="csharp-restore-", dir=ctx.scratch) as temporary:
            work = Path(temporary)
            base = work / "base"
            with start_archive(ctx.repo(task), task["start_sha"]) as stream:
                extract_tar(stream, base)
            record["module"] = pinned_module(ctx, task, directory, plan, base)
            golden = ctx.golden(task, oracle, "csharp", directory)
            record["golden_patch"] = golden
            private = work / "private"
            private.mkdir()
            curate_harness(directory, private, task, spec, plan, repository=ctx.repo(task))
            (private / "global.json").write_text(json.dumps({"sdk": {"version": version, "rollForward": "disable"}}))
            local_feed = directory / "local-nuget"
            if local_feed.is_dir():
                for package in local_feed.glob("*.nupkg"):
                    destination = shared_feed / package.name
                    if destination.exists() and file_hash(destination) != file_hash(package):
                        raise ContractError("fixed legacy local NuGet package changed")
                    if not destination.exists():
                        shutil.copy2(package, destination)
            # The union of exact start and golden dependency resolutions is exported;
            # neither implementation nor build output is used by grading.
            snapshot = work / "export"
            auxiliary = {}
            for variant in ("start", "golden"):
                variant_source = base
                if variant == "golden":
                    variant_source = work / "golden"
                    apply_patch(base, Path(golden["path"]), variant_source)
                destination = work / "build"
                if destination.exists():
                    shutil.rmtree(destination)
                shutil.copytree(private, destination)
                src = "/work/" + variant_source.name
                stage = ("dotnet /stage/CandidateStage.dll " + src + " /work/build/repo " +
                         shlex.quote(version) + " " + shlex.quote(",".join(plan["sdk_roots"])))
                if plan["kind"] == "tests":
                    stage += " && cp -R /work/private/tests/. /work/build/repo/"
                current = "/work/build" + ("/repo" if plan["kind"] == "tests" else "")
                restore, _ = build_commands(spec, plan, runtime="/work/build")
                # Keep original project-selected external feeds; these two local
                # sources reuse only dependencies, never a legacy target binary.
                restore += " -p:RestoreAdditionalProjectSources=" + "%3B".join(
                    ["/seed", "/shared-feed", *[target for _, target in extra_mounts]])
                command = stage + " && cd " + current + " && " + restore
                attempt = run_container(image, work, command, root / (variant + "-restore.log"),
                                        mounts=[(tool["output"], "/stage"), (retained, "/seed"),
                                                (shared_feed, "/shared-feed"), *extra_mounts],
                                        network="bridge", timeout=timeout)
                attempt["variant"] = variant
                record["restore_attempts"].append(attempt)
                for name, expected in plan.get("auxiliary", {}).items():
                    source = destination / "repo" / name
                    if source.is_file():
                        if source.is_symlink() or file_hash(source) != expected:
                            raise ContractError("C# source-declared auxiliary dependency hash differs: " + name)
                        target = snapshot / "workspace" / name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, target)
                        auxiliary[name] = expected
                atomic_json(root / "preparation.json", record)
                # Package SDK resolution failures recur unchanged across source
                # variants. Keep the second attempt explicit, never mark it done.
                if attempt["exit_status"] == 124:
                    record["restore_attempts"].append({"variant": "golden" if variant == "start" else "none",
                                                       "status": "not_run_after_bounded_restore_timeout"})
                    break
            records = package_snapshot(work / "packages", snapshot, shared_feed)
            record["auxiliary"] = auxiliary
            record["package_index"] = records
            record["package_index_sha256"] = canonical_hash(records)
            record["packages"] = ctx.bundle(snapshot, role="dependency")
            # A partial dependency package still gets an honest engine proof;
            # offline restore/build failures remain terminal, not readiness.
            record["status"] = ("prepared" if len(record["restore_attempts"]) == 2 and all(
                a.get("exit_status") == 0 for a in record["restore_attempts"]
            ) else "partial")
            record["ended_at_utc"] = now()
            atomic_json(path, record)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--legacy-root", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--cohort-ids", type=Path, required=True)
    parser.add_argument("--toolchains", type=Path, required=True)
    parser.add_argument("--restore-timeout", type=int, default=600)
    parser.add_argument("--prepared-tool-record", type=Path,
                        help="reuse an exact source-built staging tool after checking its original receipt")
    parser.add_argument("--reuse-feed", action="append", type=Path, default=[],
                        help="additional retained NuGet feed mounted read-only; export only resolved packages")
    parser.add_argument("--tool-only", action="store_true",
                        help="source-build the generic tool before the small gated fixture; no task builds")
    args = parser.parse_args()
    config = load_config(args.config)
    if engine_identity()["fingerprint"] != EXPECTED_ENGINE:
        raise ContractError("C# preparation must bind the original frozen execution engine")
    config = {**config, "migration": {**config.get("migration", {}), "toolchain_registry": str(args.toolchains)}}
    tasks = selected_tasks(Path(config["tasks"]), Path(config["scope_ids"]))
    oracles = read_jsonl(Path(config["oracles"]))
    ids = args.cohort_ids.read_text().splitlines()
    validate_preparation_cohort(ids, tasks, oracles)
    ctx = MigrationContext(config, tasks, oracles, args.legacy_root, args.out_dir)
    services = ExecutionServices(config)
    registry = json.loads(args.toolchains.read_text())
    tool = (reuse_tool(args.prepared_tool_record, registry) if args.prepared_tool_record
            else prepare_tool(ctx, services, registry))
    atomic_json(args.out_dir / "tool-preparation.json", tool)
    if args.tool_only:
        print(json.dumps({"source_built_tool": tool["output"], "build_exit": tool["build"]["exit_status"],
                          "expected_tasks": len(tasks), "task_builds_started": False}), flush=True)
        return 0
    results = []
    for ident in ids:
        try:
            result = prepare_one(ctx, services, tasks[ident], oracles[ident], tool, registry,
                                 args.out_dir / component(ident), timeout=args.restore_timeout,
                                 reuse_feeds=args.reuse_feed)
            results.append({"id": ident, "status": result["status"],
                            "packages": len(result["package_index"])})
        except Exception as exc:
            result = {"id": ident, "status": "blocked", "error": f"{type(exc).__name__}: {exc}"}
            atomic_json(args.out_dir / component(ident) / "failure.json", result)
            results.append(result)
        print(json.dumps({"dependency_preparation": results[-1], "expected_tasks": len(tasks)}), flush=True)
    atomic_json(args.out_dir / "summary.json", {"expected_tasks": len(tasks), "cohort_size": len(ids),
                                              "results": results, "ended_at_utc": now(),
                                              "engine_fingerprint": EXPECTED_ENGINE,
                                              "evaluation_completion": False})
    return 0 if all(r["status"] == "prepared" for r in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
