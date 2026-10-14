"""Prepare an original Yarn1 install once, reusing verified compressed cache entries.

This module prepares dependencies, not benchmark episodes. It never injects
private tests into installation workspaces or counts preparation as evaluation.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
from urllib.parse import unquote, urlparse
import uuid

sys.path.insert(0, os.environ.get("REVIEWBENCH_ENGINE_SRC", str(Path(__file__).resolve().parents[2] / "src")))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reviewbench_maintainability.agentic.assets import AssetPool, asset_descriptor
from reviewbench_maintainability.agentic.cli import load_config
from reviewbench_maintainability.agentic.contracts import (
    ContractError, atomic_json, canonical_hash, file_hash, read_jsonl, repo_path, selected_tasks,
)
from reviewbench_maintainability.agentic.execution import ExecutionServices
from reviewbench_maintainability.agentic.models import engine_identity
from reviewbench_maintainability.agentic.resources import CURRENT_JOB, GiB, ResourceRequest
from reviewbench_maintainability.agentic.snapshots import tree_files
from reviewbench_maintainability.agentic.storage import ObjectSpec, describe_file
from agentic_runtime_migration.common import MigrationContext, bounded_cpu_set
from agentic_runtime_migration.tsjs_lane import IMAGE
from agentic_runtime_migration.tsjs_pnpm import bind_oracle
from agentic_runtime_migration.tsjs_pnpm_payload import check_inputs, export_payload

ENGINE = "5142d1fec94eb6c5a6177a66ee7ce8533a26f0a2578280075a6947a399c01ca3"
SCHEMA = "reviewbench-yarn1-original-dependencies/v1"
NODE = "/opt/reviewbench-assets/node"
MANAGER = "/opt/reviewbench-assets/yarn-manager"
SCRATCH_BYTES = 4 * GiB


def validate_case(case):
    manager = "yarn@1.22.22"
    original = ["corepack", manager, "install", "--frozen-lockfile", "--ignore-scripts",
                "--network-timeout", "600000"]
    if (case.get("status") != "ready" or case.get("package_manager") != manager
            or case.get("install") != original or case.get("install_cwd") != "."
            or case.get("workspace") != "." or case.get("reinstall_golden") is not False
            or case.get("build") not in (None, "SKIP")
            or not re.fullmatch(r"\d+\.\d+\.\d+", str(case.get("node", "")))):
        raise ContractError("unsupported original Yarn1 install/build policy")
    command = case.get("test")
    if not isinstance(command, list) or command[:2] != ["corepack", manager]:
        raise ContractError("original Yarn1 test command changed")
    return "1.22.22"


def install_command(case):
    validate_case(case)
    return ["node", MANAGER + "/bin/yarn.js", *case["install"][2:],
            "--network-concurrency", "2", "--prefer-offline",
            "--cache-folder", "/yarn-cache", "--use-yarnrc", "/install-home/cache.yarnrc"]


def input_plan(source):
    """Discover only original workspace manifests, without interpreting target metadata."""
    source = Path(source).resolve()
    root = json.loads((source / "package.json").read_text())
    workspaces = root.get("workspaces", [])
    if isinstance(workspaces, dict):
        workspaces = workspaces.get("packages", [])
    if not isinstance(workspaces, list):
        raise ContractError("Yarn1 workspace list is invalid")
    importers = {"."}
    for pattern in workspaces:
        repo_path(pattern)
        if pattern.startswith("!"):
            raise ContractError("Yarn1 workspace exclusions need a separate original binding")
        for path in source.glob(pattern):
            if path.is_symlink() or not path.resolve().is_relative_to(source):
                raise ContractError("Yarn1 workspace escapes its exact source")
            if (path / "package.json").is_file():
                importers.add(path.relative_to(source).as_posix())
    names, hashes = {}, {"yarn.lock": file_hash(source / "yarn.lock")}
    for importer in sorted(importers):
        p = source / importer / "package.json"
        if p.is_symlink():
            raise ContractError("Yarn1 manifest must be an original regular file")
        package = json.loads(p.read_text())
        name = package.get("name")
        if not isinstance(name, str) or name in names:
            raise ContractError("Yarn1 workspace package identities are missing or duplicate")
        names[name] = importer
        hashes[p.relative_to(source).as_posix()] = file_hash(p)
        for field in ("dependencies", "devDependencies", "optionalDependencies"):
            for version in package.get(field, {}).values():
                if isinstance(version, str) and version.startswith(("file:", "link:", "portal:")):
                    raise ContractError("Yarn1 local copied dependencies require explicit current-source mapping")
    for name in (".yarnrc", ".npmrc"):
        path = source / name
        if path.is_file():
            if path.is_symlink():
                raise ContractError("Yarn1 configuration is redirected")
            hashes[name] = file_hash(path)
    return {"input_hashes": hashes, "workspace_names": names, "importers": sorted(importers),
            "module_roots": ["node_modules" if p == "." else p + "/node_modules"
                             for p in sorted(importers)]}


def locked_tarballs(text):
    """Read top-level resolved/integrity fields; Yarn itself remains the lock resolver."""
    rows = {}
    for stanza in re.split(r"\n(?=[^\s#])", text):
        match = re.search(r'^  resolved "([^"]+)"\s*$', stanza, re.M)
        if not match:
            continue
        url = match[1]
        parsed = urlparse(url)
        if parsed.scheme != "https" or not re.fullmatch(r"[0-9a-f]{40}", parsed.fragment):
            continue
        integrity = re.search(r"^  integrity (.+)$", stanza, re.M)
        rows[(unquote(parsed.path), parsed.fragment)] = {
            "resolved": url, "sha1": parsed.fragment,
            "integrity": integrity[1].strip().strip('"') if integrity else "",
        }
    return rows


def mirror_name(url):
    parts = unquote(urlparse(url).path).split("/")
    scope = next((part for part in parts if part.startswith("@")), None)
    name = (scope + "-" if scope else "") + parts[-1]
    if not name.endswith(".tgz") or "/" in name or name in {"", ".", ".."}:
        raise ContractError("unsupported Yarn mirror filename")
    return name


def seed_mirror(lock_text, retained, destination):
    """Copy exact-lock tar bytes only; never trust mutable extracted cache files."""
    selected = locked_tarballs(lock_text)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    rows, seen = [], set()
    retained = Path(retained)
    metadata = sorted(retained.glob("npm-*/node_modules/*/.yarn-metadata.json"))
    metadata += sorted(retained.glob("npm-*/node_modules/@*/*/.yarn-metadata.json"))
    for path in metadata:
        if path.is_symlink():
            continue
        remote = json.loads(path.read_text()).get("remote", {})
        parsed = urlparse(remote.get("resolved", ""))
        key = unquote(parsed.path), parsed.fragment
        original = selected.get(key)
        body = path.parent / ".yarn-tarball.tgz"
        if original is None or key in seen or body.is_symlink() or not body.is_file():
            continue
        data = body.read_bytes()
        if hashlib.sha1(data).hexdigest() != original["sha1"]:
            raise ContractError("retained Yarn tarball differs from the original locked SHA1")
        for sri in original["integrity"].split():
            algorithm, encoded = sri.split("-", 1)
            if algorithm not in {"sha512", "sha384", "sha256", "sha1"}:
                raise ContractError("unsupported original Yarn integrity")
            if hashlib.new(algorithm, data).digest() != base64.b64decode(encoded, validate=True):
                raise ContractError("retained Yarn tarball differs from the original locked SRI")
        target = destination / mirror_name(original["resolved"])
        digest = hashlib.sha256(data).hexdigest()
        if target.exists():
            if file_hash(target) != digest:
                raise ContractError("different locked packages collide in the Yarn offline mirror")
        else:
            shutil.copy2(body, target)
        if file_hash(target) != digest:
            raise ContractError("Yarn seed changed during copy")
        rows.append({"source": str(body), "destination": str(target), "sha256": digest,
                     "bytes": len(data), **original})
        seen.add(key)
    return {"selected_tarballs": len(selected), "reused_tarballs": len(rows),
            "reused_bytes": sum(row["bytes"] for row in rows), "entries": rows,
            "extracted_cache_files_reused": 0, "source_or_lock_rewritten": False}


def source_hashes(source, names=None):
    paths = tree_files(source) if names is None else {name: source / name for name in names}
    return {name: ("link:" + os.readlink(path) if path.is_symlink() else file_hash(path))
            for name, path in paths.items()}


def install(case, source, cache, mirror, home, node, manager, log, primary_scratch):
    version = validate_case(case)
    job = CURRENT_JOB.get()
    if not job:
        raise ContractError("Yarn preparation requires the shared resource gate")
    name = "rb-yarn-prepare-" + uuid.uuid4().hex[:12]
    job["mark_container_use"]()
    argv = install_command(case)
    command = [
        "docker", "run", "--rm", "--init", "--name", name, "--read-only",
        "--user", f"{os.getuid()}:{os.getgid()}", "--cap-drop=ALL", "--security-opt=no-new-privileges",
        "--pids-limit", "512", "--cpus", "2", "--cpuset-cpus", bounded_cpu_set(2, name),
        "--memory", "2g", "--memory-swap", "2g", "--network", "bridge",
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m",
        "--label", "reviewbench.resource-lease=" + job["lease"],
        "--mount", f"type=bind,source={source.resolve()},target=/workspace",
        "--mount", f"type=bind,source={cache.resolve()},target=/yarn-cache",
        "--mount", f"type=bind,source={mirror.resolve()},target=/yarn-mirror",
        "--mount", f"type=bind,source={home.resolve()},target=/install-home",
        "--mount", f"type=bind,source={node.resolve()},target={NODE},readonly",
        "--mount", f"type=bind,source={manager.resolve()},target={MANAGER},readonly",
        "-w", "/workspace", "-e", "PATH=" + NODE + "/bin:/usr/local/bin:/usr/bin:/bin",
        "-e", "HOME=/install-home", "-e", "NODE_OPTIONS=--max-old-space-size=1536",
        "-e", "CI=true", "-e", "HUSKY=0", "-e", "CHILD_CONCURRENCY=2",
        "-e", "COREPACK_ENABLE_NETWORK=0", "-e", "COREPACK_ENABLE_PROJECT_SPEC=0",
        "--entrypoint", "/bin/bash", IMAGE, "-c",
        "set -euo pipefail; test \"$(node --version)\" = v" + case["node"]
        + "; test \"$(node " + MANAGER + "/bin/yarn.js --version)\" = " + version
        + "; exec " + shlex.join(argv),
    ]
    started = time.time()
    receipt = {"command": command, "install_argv": argv, "original_install_argv": case["install"],
               "container": name, "lease": job["lease"], "started_at": started, "log": str(log),
               "resources": {"cpus": 2, "memory_bytes": 2 * GiB, "scratch_bytes": SCRATCH_BYTES,
                             "network_concurrency": 2, "child_concurrency": 2}}
    with log.open("x") as stream:
        process = subprocess.Popen(command, stdout=stream, stderr=stream)
        receipt["pid"] = process.pid
        atomic_json(log.with_suffix(".json"), receipt)
        try:
            while process.poll() is None:
                if time.time() - started > case["install_timeout"]:
                    raise ContractError("original Yarn installation timeout expired")
                if any(shutil.disk_usage(p).free < 80 * GiB for p in (source, primary_scratch)):
                    raise ContractError("Yarn preparation reached the unchanged 80GiB disk floor")
                time.sleep(1)
        finally:
            if process.poll() is None:
                subprocess.run(["docker", "stop", "--time", "10", name], capture_output=True, timeout=30)
                process.wait(timeout=30)
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
            receipt.update(ended_at=time.time(), log_sha256=file_hash(log), exit_status=process.returncode)
            atomic_json(log.with_suffix(".json"), receipt)
    if process.returncode:
        raise ContractError("original frozen Yarn install failed: " + str(log))
    return receipt


def readback(ctx, services, dependency, payload, output):
    descriptor = asset_descriptor(dependency, ctx.output)
    expected = {name: {"mode": path.lstat().st_mode & 0o777,
                      **({"link": os.readlink(path)} if path.is_symlink() else {"sha256": file_hash(path)})}
                for name, path in tree_files(payload).items()}
    body = output / "independent-dependency-readback.tar.gz"
    receipts = []
    with body.open("xb") as stream:
        for index, part in enumerate(descriptor["archive"]["parts"]):
            obj = ObjectSpec.from_dict(part)
            copy = output / ("readback-part-" + str(index))
            with services.cache.transfers.acquire():
                receipts.append(services.cache.remote.get(obj, copy))
            if describe_file(copy) != obj:
                raise ContractError("independent Yarn dependency GET differs")
            with copy.open("rb") as incoming:
                shutil.copyfileobj(incoming, stream, length=1024**2)
            copy.unlink()
    found = set()
    with tarfile.open(body, "r:gz") as archive:
        for member in archive:
            if member.isdir():
                continue
            name = repo_path(member.name)
            if name in found or name not in expected:
                raise ContractError("Yarn dependency archive has an extra/duplicate entry")
            found.add(name)
            row = expected[name]
            if member.mode != row["mode"]:
                raise ContractError("Yarn dependency archive mode differs")
            if "link" in row:
                if not member.issym() or member.linkname != row["link"]:
                    raise ContractError("Yarn dependency archive link differs")
            elif not member.isfile() or hashlib.sha256(archive.extractfile(member).read()).hexdigest() != row["sha256"]:
                raise ContractError("Yarn dependency archive bytes differ")
    if found != set(expected):
        raise ContractError("Yarn dependency archive is incomplete")
    with gzip.open(body, "rb") as stream:
        while stream.read(1024**2):
            pass
    proof = {"status": "independent_Blob_bytes_modes_links_crc_verified",
             "files": len(found), "body": str(body), "sha256": file_hash(body),
             "bytes": body.stat().st_size, "receipts": receipts}
    atomic_json(output / "dependency-readback.json", proof)
    return proof


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("config", "legacy-root", "inputs", "out-dir", "work-root", "retained-cache"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    inputs = json.loads(args.inputs.read_text())
    config = load_config(args.config)
    tasks = selected_tasks(Path(config["tasks"]), Path(config["scope_ids"]))
    oracles = read_jsonl(Path(config["oracles"]))
    ident = inputs["id"]
    if len(tasks) != 402 or ident not in tasks or engine_identity()["fingerprint"] != ENGINE:
        raise ContractError("Yarn preparation must retain the original engine and full402 scope")
    task, oracle = tasks[ident], oracles[ident]
    case = json.loads((args.legacy_root / "tsjs/cases.json").read_text())["cases"][ident]
    validate_case(case)
    bind_oracle(case, oracle)
    if inputs["case_fingerprint"] != canonical_hash(case):
        raise ContractError("Yarn original case binding changed")
    if args.out_dir.exists() or args.work_root.exists():
        raise ContractError("Yarn preparation preserves prior attempts; an existing install is not replayed")
    args.out_dir.mkdir(parents=True)
    state = {"schema_version": SCHEMA, "id": ident, "status": "waiting_for_gate",
             "full_scope": 402, "case_fingerprint": canonical_hash(case), "start_sha": task["start_sha"],
             "node_version": case["node"], "yarn_version": validate_case(case),
             "model_calls": 0, "calibration_calls": 0, "evaluation_completion": False,
             "started_at": time.time()}
    atomic_json(args.out_dir / "process.json", state)
    services = ExecutionServices(config)
    with services.gate.acquire(ResourceRequest("yarn1-original-install:" + ident, cpus=2,
            memory_bytes=2 * GiB, scratch_bytes=SCRATCH_BYTES), uses_containers=True, timeout=7200) as lease:
        ctx = MigrationContext(config, tasks, oracles, args.legacy_root, args.out_dir / "package")
        pool = AssetPool(services.cache, services.scratch, lease["lease"], ctx.output)
        try:
            state.update(status="preparing_original_install", lease=lease["lease"])
            atomic_json(args.out_dir / "process.json", state)
            args.work_root.mkdir(parents=True)
            source = args.work_root / "installed"
            state["source_tree_hash"] = services.sources.extract(task["repo"], task["start_sha"],
                source, submodules=ctx.modules(task), asset_pool=pool)
            before = source_hashes(source)
            atomic_json(args.out_dir / "original-source-hashes.json", before)
            plan = input_plan(source)
            atomic_json(args.out_dir / "original-plan.json", plan)
            mirror = args.work_root / "mirror"
            seed = seed_mirror((source / "yarn.lock").read_text(), args.retained_cache, mirror)
            atomic_json(args.out_dir / "cache-reuse.json", seed)
            cache, home = args.work_root / "cache", args.work_root / "home"
            cache.mkdir()
            home.mkdir()
            (home / "cache.yarnrc").write_text('yarn-offline-mirror "/yarn-mirror"\n'
                                              'yarn-offline-mirror-pruning false\n')
            node, manager = pool.materialize(inputs["node"]), pool.materialize(inputs["manager"])
            state["install"] = install(case, source, cache, mirror, home, node, manager,
                                       args.out_dir / "install.log", ctx.scratch)
            check_inputs(source, plan["input_hashes"])
            if source_hashes(source, before) != before:
                raise ContractError("original Yarn source files changed during installation")
            state.update(status="installed_not_exported", source=str(source), source_files_unchanged=True)
            atomic_json(args.out_dir / "process.json", state)
            payload = args.work_root / "payload"
            layout, index = export_payload(source, payload, plan)
            atomic_json(args.out_dir / "external-index.json", index)
            dependency = ctx.bundle(payload, role="dependency")
            proof = readback(ctx, services, dependency, payload, args.out_dir)
            state.update(status="prepared_not_calibrated", node=inputs["node"], manager=inputs["manager"],
                         dependencies=dependency, original_logs=inputs["original_logs"],
                         input_hashes=plan["input_hashes"], payload=str(payload),
                         module_roots=layout["module_roots"], workspace_links=layout["workspace_links"],
                         workspace_production_files_exported=0, external_bytes=layout["external_bytes"],
                         external_index_sha256=layout["external_index_sha256"],
                         cache_reused_tarballs=seed["reused_tarballs"], dependency_readback=proof,
                         source_manifest_or_lock_rewritten=False,
                         next="bind current-source fixed-install transport and original private tests; calibrate once")
        except Exception as error:
            state.update(status="preparation_incomplete", error=type(error).__name__ + ": " + str(error))
            raise
        finally:
            state["ended_at"] = time.time()
            atomic_json(args.out_dir / "process.json", state)
            pool.close()
    atomic_json(args.out_dir / "dependencies.json", state)
    print("YARN1_ORIGINAL_DEPENDENCIES_PREPARED id=" + ident + " reused_tarballs="
          + str(seed["reused_tarballs"]) + " Blob_verified=true calibration_calls=0 model_calls=0", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
