"""Project attested external Conan payloads and resolve four Rippled candidates."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
import uuid

import yaml

from reviewbench_maintainability.agentic.cli import load_config
from reviewbench_maintainability.agentic.contracts import (
    ContractError, atomic_json, canonical_hash, component, file_hash, read_jsonl, selected_tasks, write_jsonl,
)
from reviewbench_maintainability.agentic.execution import ExecutionServices
from reviewbench_maintainability.agentic.models import engine_identity
from reviewbench_maintainability.agentic.resources import ResourceRequest, GiB
from reviewbench_maintainability.agentic.snapshots import extract_tar, start_archive, tree_files
from .common import MigrationContext, bounded_cpu_set
from .native_rippled import (
    DEPENDENCIES, POLICY, PUBLIC, SPECS, dependency_request, load_preparation, migrate,
    original_inputs, pinned_file, write_public,
)
from .native_submodules import object_view
from .native_rippled_conan import legacy_cache, modern_cache


LEGACY_BASE = "sha256:8268d755008211c1e35be7b2387dca7498d460008febae21e6af1ce0394094b5"


def run_logged(command, output, label, *, timeout=900, env=None, container=None):
    paths = {stream: output / (label + "." + stream) for stream in ("stdout", "stderr", "json")}
    if any(path.exists() for path in paths.values()):
        raise ContractError("Rippled command evidence exists; preserve the original attempt: " + label)
    started = time.monotonic()
    with paths["stdout"].open("x") as stdout, paths["stderr"].open("x") as stderr:
        process = subprocess.Popen(command, stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL,
                                   start_new_session=True, env=env)
        value = {"command": command, "shell": shlex.join(command), "pid": process.pid,
                 "status": "running", "started_at": datetime.now(timezone.utc).isoformat()}
        atomic_json(paths["json"], value)
        print(json.dumps(value), flush=True)
        timed_out = False
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            if container:
                subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=30)
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            code = process.wait()
    value.update(status="terminal", exit_status=code, timed_out=timed_out,
                 seconds=time.monotonic() - started,
                 **{stream: pinned_file(paths[stream]) for stream in ("stdout", "stderr")})
    atomic_json(paths["json"], value)
    print(json.dumps({"label": label, "exit_status": code, "record": str(paths["json"])}), flush=True)
    if code != 0 or timed_out:
        raise ContractError("Rippled " + label + " failed; see " + str(paths["json"]))
    return pinned_file(paths["json"])


def container_command(image, lease, *, cpus=2, memory="4g"):
    name = "rb-rippled-prepare-" + uuid.uuid4().hex[:16]
    return name, [
        "docker", "run", "--rm", "--init", "--name", name, "--read-only", "--network", "none",
        "--user", f"{os.getuid()}:{os.getgid()}", "--cpus", str(cpus),
        "--cpuset-cpus", bounded_cpu_set(cpus, lease["lease"]), "--memory", memory,
        "--pids-limit", "256", "--cap-drop=ALL", "--security-opt=no-new-privileges",
        "--label", "reviewbench.resource-lease=" + lease["lease"],
        "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=512m",
        "-e", "HOME=/tmp", "-e", "PYTHONDONTWRITEBYTECODE=1",
    ]


def legacy_overlay(registry, output, services):
    """Pinned compiler base plus the original Conan version; no apt rebuild."""
    if registry.get("oracle-rippled:real", {}).get("status") == "ready":
        return registry
    directory = output / "legacy-toolchain"
    directory.mkdir()
    dockerfile = directory / "Dockerfile"
    dockerfile.write_text(
        "FROM " + LEGACY_BASE + "\n"
        "RUN python3 -m pip install --no-cache-dir --prefer-binary conan==1.60.2\n"
        "RUN test \"$(g++ -dumpfullversion)\" = 11.4.0 && test \"$(conan --version)\" = 'Conan version 1.60.2'\n")
    tag = "reviewbench-rippled-conan1:" + file_hash(dockerfile)[:16]
    with services.gate.acquire(ResourceRequest("rippled-conan1-toolchain-overlay", cpus=1,
                                               memory_bytes=GiB, scratch_bytes=4 * GiB),
                               uses_containers=True) as lease:
        command = ["docker", "build", "--cpuset-cpus", bounded_cpu_set(1, lease["lease"]),
                   "--cpu-period", "100000", "--cpu-quota", "100000", "--memory", "1g",
                   "--memory-swap", "1g", "--label", "reviewbench.resource-lease=" + lease["lease"],
                   "--tag", tag, "--file", str(dockerfile), str(directory)]
        receipt = run_logged(command, directory, "build", env={**os.environ, "DOCKER_BUILDKIT": "0"})
        details = json.loads(subprocess.check_output(["docker", "image", "inspect", tag]))[0]
        image = details["Id"]
        name, command = container_command(image, lease, cpus=1, memory="1g")
        command += ["--entrypoint", "bash", image, "-ec",
                    "g++ -dumpfullversion; gcc -dumpfullversion; cmake --version; ninja --version; "
                    "conan --version; python3 -m pip freeze"]
        version = run_logged(command, directory, "versions", timeout=60, container=name)
    registry = {**registry, "oracle-rippled:real": {
        "status": "ready", "image": image, "base_image": LEGACY_BASE,
        "dockerfile": pinned_file(dockerfile), "build_receipt": receipt, "versions_receipt": version,
        "candidate_or_reference_source_in_image": False,
    }}
    return registry


def project_cache(inputs, source, bundle, directory, image, lease, *, volume=None):
    atomic_json(bundle / "graph.json", inputs["graph"])
    script = Path(__file__).with_name("native_rippled_conan.py")
    name, command = container_command(image, lease)
    command += ["--mount", f"type=bind,source={bundle},target=/output",
                "--mount", f"type=bind,source={directory},target=/evidence",
                "--mount", f"type=bind,source={script},target=/project.py,readonly"]
    if volume:
        if volume != "rippled-conan-cache":
            raise ContractError("Rippled legacy cache volume is not the explicit retained dependency volume")
        command += ["--mount", "type=volume,source=" + volume + ",target=/retained,readonly"]
    else:
        if not source.is_absolute() or source.is_symlink() or not source.is_dir():
            raise ContractError("Rippled retained Conan 2 cache must be an existing absolute directory")
        command += ["--mount", f"type=bind,source={source},target=/retained,readonly"]
    command += ["--entrypoint", "python3", image, "/project.py", "--source", "/retained",
                "--graph", "/output/graph.json", "--target", "/output/cache",
                "--audit", "/evidence/cache-payload-audit.json"]
    return run_logged(command, directory, "cache-projection", timeout=1800, container=name)


def source_files(source, names=None):
    files = tree_files(source) if names is None else {name: source / name for name in names}
    return {name: ("link", os.readlink(path)) if path.is_symlink()
            else ("file", file_hash(path), bool(path.stat().st_mode & 0o111))
            for name, path in files.items()}


def reusable_projection(previous, task, inputs, image):
    """Recheck an existing exact closure with no external payload copying."""
    if previous is None:
        return None
    directory = previous / component(task["id"])
    marker = directory / "cache-projection.json"
    if not marker.is_file():
        return None
    command = json.loads(marker.read_text())
    if command.get("exit_status") != 0 or command.get("timed_out"):
        return None
    request = json.loads((directory / "attempt-start.json").read_text())["request"]
    for key, expected in {
        "task_id": task["id"], "start_sha": task["start_sha"], "image": image,
        "graph": inputs["graph"], "public": inputs["public"], "original_conan_log": inputs["log"],
        "original_commands": inputs["commands"], "original_helpers": inputs["helpers"],
    }.items():
        if request.get(key) != expected:
            raise ContractError("Rippled projected-cache reuse changed an original binding: " + key)
    bundle = directory / "bundle"
    if json.loads((bundle / "graph.json").read_text()) != inputs["graph"]:
        raise ContractError("Rippled projected-cache graph changed")
    major = inputs["graph"]["major"]
    audit = (modern_cache if major == 2 else legacy_cache)(bundle / "cache", inputs["graph"])
    recipes = {row["path"] for row in audit["entries"] if "package_id" not in row}
    allowed = {"graph.json", "cache/settings.yml", "cache/version.txt"}
    if major == 2:
        allowed.add("cache/p/cache.sqlite3")
    else:
        allowed.update(str(Path("cache") / row["path"]).removesuffix("/export") + "/metadata.json"
                       for row in audit["entries"] if "package_id" not in row)
    for row in audit["files"]:
        root = Path("cache/p" if major == 2 else "cache") / row["cache_entry"]
        name = row["path"]
        if major == 1:
            if name.startswith("export_source/"):
                root = root.parent / "export_source"
                name = name.removeprefix("export_source/")
        elif row["cache_entry"] in recipes:
            root /= "es" if name.startswith("export_source/") else "e"
            name = name.removeprefix("export_source/")
        else:
            root /= "p"
        allowed.add(str(root / name))
    if set(tree_files(bundle)) != allowed:
        raise ContractError("Rippled projected-cache reuse contains extra or missing payloads")
    return bundle, {"command": pinned_file(marker),
                    "audit": pinned_file(directory / "cache-payload-audit.json"),
                    "rechecked_files": len(audit["files"]), "payload_bytes_copied": 0}


def prepare_one(ctx, task, inputs, directory, services, image, conan2, legacy_volume, exporter_image, reuse=None):
    request = dependency_request(task, inputs, image)
    manifest = directory / "preparation.json"
    if manifest.is_file():
        ctx.config.setdefault("migration", {}).setdefault("native_rippled_preparations", {})[task["id"]] = pinned_file(manifest)
        value = load_preparation(ctx, task, inputs, image)
        print("REUSED_EXACT_RIPPLED_PREPARATION=" + str(manifest), flush=True)
        return value
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / "attempt-start.json").exists():
        raise ContractError("Rippled preparation already attempted; use new output without rewriting it")
    atomic_json(directory / "attempt-start.json", {"pid": os.getpid(), "request": request,
                                                   "request_sha256": canonical_hash(request)})
    print(json.dumps({"phase": "waiting_shared_gate", "id": task["id"], "pid": os.getpid()}), flush=True)
    # There is deliberately no CMake or source dependency build in this normal
    # reservation. Candidate compilation belongs to the heavy verification job.
    reservation = ResourceRequest("prepare-rippled-exact-cache:" + task["id"], cpus=2,
                                  memory_bytes=4 * GiB, scratch_bytes=20 * GiB)
    with services.gate.acquire(reservation, uses_containers=True) as lease:
        bundle, public = directory / "bundle", directory / "public"
        write_public(public, inputs["public"], view=inputs["key"] == "view")
        roots = [reuse] if isinstance(reuse, Path) else list(reuse or [])
        if len(roots) > 4 or len(roots) != len(set(roots)):
            raise ContractError("Rippled projection reuse needs at most four distinct explicit roots")
        prior = None
        for previous in roots:
            prior = reusable_projection(previous, task, inputs, image)
            if prior is not None:
                break
        if prior:
            bundle, reuse_receipt = prior
            projection = reuse_receipt["command"]
            payload_audit = reuse_receipt["audit"]
            atomic_json(directory / "projection-reuse.json", reuse_receipt)
        else:
            bundle.mkdir()
            projection = project_cache(inputs, conan2, bundle, directory, exporter_image, lease,
                                       volume=legacy_volume if inputs["key"] == "stobject" else None)
            payload_audit = pinned_file(directory / "cache-payload-audit.json")
        with tempfile.TemporaryDirectory(prefix="rippled-conan-candidate-", dir=ctx.scratch) as temporary:
            source = Path(temporary) / "source"
            with object_view(ctx.repo(task)) as view, start_archive(view, task["start_sha"], timeout=180) as archive:
                extract_tar(archive, source)
            before = source_files(source)
            name, command = container_command(image, lease)
            command += ["--mount", f"type=bind,source={source},target=/workspace",
                        "--mount", f"type=bind,source={bundle},target={DEPENDENCIES},readonly",
                        "--mount", f"type=bind,source={public},target={PUBLIC},readonly",
                        "--workdir", "/workspace", "--entrypoint", "python3", image,
                        PUBLIC + "/entry.py", "--public", PUBLIC, "--dependencies", DEPENDENCIES]
            try:
                resolved = run_logged(command, directory, "resolve", timeout=600, container=name)
            finally:
                # Keep the actual generated graph on failures too, before the
                # private current-source tree is cleaned up.
                for filename in ("install-graph.json", "dependency-check.json", "conan.lock"):
                    path = source / "build-rippled-dependencies" / filename
                    if path.is_file():
                        shutil.copyfile(path, directory / filename)
            after = source_files(source, before)
            if any(after.get(name) != row for name, row in before.items()):
                raise ContractError("Rippled source inputs changed during external dependency resolution")
        asset = ctx.bundle(bundle, role="dependency")
        value = {"policy": POLICY, "status": "dependencies_resolved", "request": request,
                 "request_sha256": canonical_hash(request), "dependency_asset": asset,
                 "cache_projection_command": projection,
                 "cache_projection": payload_audit,
                 "resolve_command": resolved, "source_unchanged": True,
                 "old_candidate_artifacts_copied": False, "runtime_verified": False,
                 "engine_fingerprint": engine_identity()["fingerprint"], "model_calls": 0,
                 "registration_performed": False, "finished_at": datetime.now(timezone.utc).isoformat()}
        atomic_json(manifest, value)
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--legacy-root", type=Path, required=True)
    parser.add_argument("--toolchains", type=Path, required=True)
    parser.add_argument("--ids", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--conan2-cache", type=Path, required=True)
    parser.add_argument("--legacy-cache-volume", default="rippled-conan-cache")
    parser.add_argument("--build-legacy-overlay", action="store_true")
    parser.add_argument("--reuse-projections", type=Path, action="append",
                        help="recheck prior exact projections without payload copying; at most four explicit roots")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    engine = engine_identity()["fingerprint"]
    if engine != config.get("migration", {}).get("verification_engine_fingerprint"):
        raise ContractError("Rippled preparation requires the configured frozen generation engine")
    tasks = selected_tasks(Path(config["tasks"]), Path(config["scope_ids"]))
    oracles = read_jsonl(Path(config["oracles"]))
    ids = args.ids.read_text().splitlines()
    supported = {row["id"] for row in SPECS.values()}
    if len(tasks) != 402 or len(ids) != len(set(ids)) or not set(ids) <= tasks.keys() & supported or not ids:
        raise ContractError("Rippled preparation must retain Full402 and a bounded supported cohort")
    output = args.out_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    config = {**config, "out_dir": str(output / "execution")}
    services = ExecutionServices(config)
    registry = json.loads(args.toolchains.read_text())
    if args.build_legacy_overlay:
        registry = legacy_overlay(registry, output, services)
    registry_file = output / "toolchains.json"
    atomic_json(registry_file, registry)
    config.setdefault("migration", {})["toolchain_registry"] = str(registry_file)
    ctx = MigrationContext(config, tasks, oracles, args.legacy_root.resolve(), output)
    rows = []
    source_inputs = {}
    for ident in ids:
        task = tasks[ident]
        directory = output / component(ident)
        row = {"id": ident, "runtime_verified": False}
        try:
            inputs = original_inputs(ctx, task, oracles[ident])
            source_inputs[ident] = inputs
            with services.gate.acquire(ResourceRequest("audit-rippled-toolchain", cpus=1,
                                                       memory_bytes=GiB, scratch_bytes=GiB),
                                       uses_containers=True):
                image, _ = ctx.image(inputs["plan"]["docker_image"])
            exporter = registry["oracle-rippled-modern:real"]["image"]
            value = prepare_one(ctx, task, inputs, directory, services, image,
                                args.conan2_cache, args.legacy_cache_volume, exporter, args.reuse_projections)
            reference = pinned_file(directory / "preparation.json")
            config["migration"].setdefault("native_rippled_preparations", {})[ident] = reference
            row.update(status=value["status"], preparation=reference)
        except Exception as exc:
            row.update(status="blocked", reason=type(exc).__name__ + ": " + str(exc))
            directory.mkdir(parents=True, exist_ok=True)
            # A repeated invocation must not replace a prior attempt's failure.
            failure = directory / "failure.json"
            if not failure.exists():
                atomic_json(failure, row)
        rows.append(row)
        atomic_json(output / "progress.json", {"pid": os.getpid(), "engine_fingerprint": engine,
                                               "scope_size": len(tasks), "rows": rows})
        print(json.dumps(row), flush=True)
    atomic_json(output / "original-inputs.json", source_inputs)
    # Published-legacy input has no native task-table index. Preimport an
    # unverified, independently attested recipe; the unchanged migration CLI
    # still verifies it against the original task and original released Oracle.
    from migrate_agentic_runtimes import import_existing
    recipes = import_existing(config, tasks)
    for row in rows:
        if row["status"] == "dependencies_resolved" and row["id"] == SPECS["stobject"]["id"]:
            recipes[row["id"]] = migrate(ctx, tasks[row["id"]], oracles[row["id"]])
    preimport = output / "unverified-preimport-recipes.jsonl"
    write_jsonl(preimport, [asdict(recipe) for recipe in recipes.values()])
    config["runtime_recipes"] = str(preimport)
    (output / "prepared-config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    atomic_json(output / "preparation-outcome.json", {
        "scope_size": len(tasks), "selected_ids": ids, "rows": rows,
        "dependencies_resolved": sum(row["status"] == "dependencies_resolved" for row in rows),
        "runtime_ready": 0, "model_calls": 0, "registration_performed": False,
        "engine_fingerprint": engine,
    })
    return 0 if all(row["status"] == "dependencies_resolved" for row in rows) else 2


if __name__ == "__main__":
    raise SystemExit(main())
