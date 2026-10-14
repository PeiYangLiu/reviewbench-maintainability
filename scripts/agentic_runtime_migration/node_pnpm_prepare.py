"""One bounded, original pnpm install; external assets only, never task scoring."""
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
import tarfile
import tempfile
import time
import urllib.request
import uuid

sys.path.insert(0, os.environ.get("REVIEWBENCH_ENGINE_SRC", str(Path(__file__).resolve().parents[2] / "src")))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reviewbench_maintainability.agentic.cli import load_config
from reviewbench_maintainability.agentic.contracts import (
    ContractError, atomic_json, canonical_hash, component, file_hash, read_jsonl, selected_tasks,
)
from reviewbench_maintainability.agentic.execution import ExecutionServices
from reviewbench_maintainability.agentic.models import engine_identity
from reviewbench_maintainability.agentic.resources import CURRENT_JOB, GiB, ResourceRequest
from reviewbench_maintainability.agentic.snapshots import extract_tar, start_archive, tree_files
from agentic_runtime_migration.common import MigrationContext, bounded_cpu_set
from agentic_runtime_migration.tsjs_lane import IMAGE, node_asset
from agentic_runtime_migration.tsjs_pnpm import NODE, MANAGER, validate_case, bind_oracle
from agentic_runtime_migration.tsjs_pnpm_payload import SCHEMA, input_plan, check_inputs, export_payload

ENGINE = "5142d1fec94eb6c5a6177a66ee7ce8533a26f0a2578280075a6947a399c01ca3"


def sri(value):
    match = re.fullmatch(r"(sha512|sha384|sha256|sha1)-([A-Za-z0-9+/=]+)", value)
    if not match:
        raise ContractError("unsupported pnpm content integrity")
    return match[1], base64.b64decode(match[2], validate=True).hex()


def seed_store(packages, retained, destination):
    """Look up only exact lock-selected pnpm10 indexes and their named CAS files."""
    retained, destination = Path(retained), Path(destination)
    result = {"packages_considered": len(packages), "indexes_reused": 0,
              "files_reused": 0, "bytes_reused": 0, "side_effects_reused": False, "indexes": []}
    for package, row in sorted(packages.items()):
        integrity = row["resolution"]["integrity"]
        _, digest = sri(integrity)
        # pnpm10.11.0 getIndexFilePathInCafs uses the first64 hex characters.
        key = digest[:64]
        suffix = re.sub(r'[\\/:*?"<>|]', "+", package)
        relative = Path("index") / key[:2] / (key[2:] + "-" + suffix + ".json")
        index = retained / relative
        if not index.is_file() or index.is_symlink():
            continue
        record = json.loads(index.read_text())
        if package != str(record.get("name")) + "@" + str(record.get("version")):
            raise ContractError("retained pnpm index package identity differs")
        files = []
        for entry in record.get("files", {}).values():
            algorithm, h = sri(entry["integrity"])
            name = Path("files") / h[:2] / (h[2:] + ("-exec" if entry["mode"] & 0o111 else ""))
            source = retained / name
            if not source.is_file() or source.is_symlink():
                files = []
                break
            if hashlib.new(algorithm, source.read_bytes()).hexdigest() != h:
                raise ContractError("retained pnpm content differs from its exact integrity")
            files.append((source, destination / name))
        if not files:
            continue
        for source, target in files:
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                result["files_reused"] += 1
                result["bytes_reused"] += source.stat().st_size
        # Never reuse lifecycle-script side effects under --ignore-scripts.
        clean = {k: v for k, v in record.items() if k != "sideEffects"}
        target = destination / relative
        atomic_json(target, clean)
        result["indexes"].append({"package": package, "source": str(index),
                                  "source_sha256": file_hash(index), "seed_sha256": file_hash(target)})
        result["indexes_reused"] += 1
    return result


def retained_manager(root, version, archive_path, *, package_name="pnpm"):
    """Bind the retained executable tree to the independently retained tar SRI."""
    root, archive_path = Path(root), Path(archive_path)
    if package_name not in {"pnpm", "yarn"} or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ContractError("retained package manager needs a supported exact identity")
    package = json.loads((root / "package.json").read_text())
    witness = json.loads((root / ".corepack").read_text())
    if package.get("name") != package_name or package.get("version") != version:
        raise ContractError("retained pnpm manager version differs")
    algorithm, digest = witness["hash"].split(".", 1)
    if algorithm != "sha512" or not re.fullmatch(r"[0-9a-f]{128}", digest):
        raise ContractError("retained pnpm distribution SRI is absent")
    url = f"https://registry.npmjs.org/{package_name}/-/{package_name}-{version}.tgz"
    downloaded = False
    if not archive_path.exists():
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        partial = archive_path.with_suffix(".partial")
        with urllib.request.urlopen(url, timeout=60) as response, partial.open("xb") as stream:
            shutil.copyfileobj(response, stream, length=1024 * 1024)
        if hashlib.sha512(partial.read_bytes()).hexdigest() != digest:
            raise ContractError("pnpm tarball differs from the historical corepack integrity")
        partial.rename(archive_path)
        downloaded = True
    if hashlib.sha512(archive_path.read_bytes()).hexdigest() != digest:
        raise ContractError("retained pnpm tarball hash differs")
    expected = {}
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive:
            if member.isdir():
                continue
            if not member.name.startswith("package/"):
                raise ContractError("pnpm distribution path is not package-relative")
            name = member.name[len("package/"):]
            if Path(name).is_absolute() or ".." in Path(name).parts:
                raise ContractError("pnpm distribution path escapes")
            path = root / name
            if member.isfile():
                digest256 = hashlib.sha256(archive.extractfile(member).read()).hexdigest()
                if path.is_symlink() or not path.is_file() or file_hash(path) != digest256:
                    raise ContractError("retained pnpm file differs from the pinned distribution: " + name)
                expected[name] = digest256
            elif member.issym():
                if not path.is_symlink() or os.readlink(path) != member.linkname:
                    raise ContractError("retained pnpm distribution link differs")
                expected[name] = "link:" + member.linkname
            else:
                raise ContractError("unsupported pnpm tar entry")
    actual = set(tree_files(root)) - {".corepack"}
    if set(expected) != actual:
        raise ContractError("retained pnpm executable tree has missing or additional files")
    return {"root": str(root), "version": version, "url": url, "archive": str(archive_path),
            "archive_sha256": file_hash(archive_path), "archive_sha512": digest,
            "corepack_receipt_sha256": file_hash(root / ".corepack"),
            "distribution_files": len(expected), "file_hashes": expected,
            "downloaded_tar_for_independent_byte_verification": downloaded}


def install_command(version):
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ContractError("pnpm install needs an exact version")
    return ["node", MANAGER + "/bin/pnpm.cjs", "install", "--frozen-lockfile", "--ignore-scripts",
            "--network-concurrency=2", "--child-concurrency=2", "--store-dir=/pnpm-store",
            "--prefer-offline", "--reporter=append-only"]


def install(ctx, source, store, node_root, manager_root, log, version):
    job = CURRENT_JOB.get()
    if not job:
        raise ContractError("pnpm install requires the original shared gate")
    name = "rb-pnpm-prepare-" + uuid.uuid4().hex[:12]
    job["mark_container_use"]()
    command = install_command(version)
    argv = [
        "docker", "run", "--rm", "--init", "--name", name, "--read-only",
        "--user", f"{os.getuid()}:{os.getgid()}", "--cap-drop=ALL", "--security-opt=no-new-privileges",
        "--pids-limit", "512", "--cpus", "2", "--cpuset-cpus", bounded_cpu_set(2, name),
        "--memory", "2g", "--memory-swap", "2g", "--network", "bridge",
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m",
        "--label", "reviewbench.resource-lease=" + job["lease"],
        "--mount", f"type=bind,source={source.resolve()},target=/workspace",
        "--mount", f"type=bind,source={store.resolve()},target=/pnpm-store",
        "--mount", f"type=bind,source={node_root.resolve()},target={NODE},readonly",
        "--mount", f"type=bind,source={manager_root.resolve()},target={MANAGER},readonly",
        "-w", "/workspace", "-e", "PATH=" + NODE + "/bin:/usr/local/bin:/usr/bin:/bin",
        "-e", "HOME=/tmp", "-e", "NODE_OPTIONS=--max-old-space-size=1536",
        "-e", "CI=true", "-e", "HUSKY=0", "-e", "COREPACK_ENABLE_NETWORK=0",
        "-e", "COREPACK_ENABLE_PROJECT_SPEC=0", "--entrypoint", "/bin/bash", IMAGE, "-c",
        "set -euo pipefail; test \"$(node --version)\" = v" +
        ctx._pnpm_node_version + "; test \"$(node " + MANAGER +
        "/bin/pnpm.cjs --version)\" = " + version + "; exec " + shlex.join(command),
    ]
    started = time.time()
    receipt = {"command": argv, "install_argv": command, "container": name, "lease": job["lease"],
               "started_at": started, "log": str(log), "cwd": "/workspace",
               "resources": {"cpus": 2, "memory_bytes": 2 * GiB, "scratch_bytes": 12 * GiB,
                             "node_heap_mb": 1536, "network_concurrency": 2, "child_concurrency": 2}}
    with log.open("x") as stream:
        process = subprocess.Popen(argv, stdout=stream, stderr=stream)
        receipt["pid"] = process.pid
        atomic_json(log.with_suffix(".json"), receipt)
        try:
            while process.poll() is None:
                if time.time() - started > 1200:
                    raise ContractError("bounded pnpm installation exceeded1200 seconds")
                if shutil.disk_usage(ctx.scratch).free < 80 * GiB:
                    raise ContractError("pnpm preparation reached the unchanged80GiB disk floor")
                time.sleep(1)
            receipt["exit_code"] = process.returncode
        finally:
            if process.poll() is None:
                subprocess.run(["docker", "stop", "--time", "10", name], capture_output=True, timeout=30)
                process.wait(timeout=30)
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
            receipt.update(ended_at=time.time(), log_sha256=file_hash(log),
                           exit_code=process.returncode)
            atomic_json(log.with_suffix(".json"), receipt)
    if process.returncode:
        raise ContractError("original frozen pnpm install failed: " + str(log))
    return receipt


def prepare(ctx, services, task, oracle, case, root, manager_root, retained_store):
    version = validate_case(case)
    bind_oracle(case, oracle)
    if root.exists():
        raise ContractError("pnpm preparation needs an explicit new directory; completed attempts are not retried")
    root.mkdir(parents=True)
    record = {"schema_version": SCHEMA, "id": task["id"], "start_sha": task["start_sha"],
              "case_fingerprint": canonical_hash(case), "node_version": case["node"],
              "pnpm_version": version, "status": "waiting_for_gate", "model_calls": 0,
              "evaluation_completion": False, "started_at": time.time()}
    atomic_json(root / "process.json", record)
    with services.gate.acquire(
        ResourceRequest("pnpm-prepare:" + task["id"], cpus=2, memory_bytes=2 * GiB,
                        scratch_bytes=12 * GiB),
        uses_containers=True, timeout=ctx.config["execution"].get("admission_timeout_seconds", 14400),
    ) as lease:
        record.update(status="preparing", lease=lease["lease"])
        atomic_json(root / "process.json", record)
        try:
            manager_proof = retained_manager(manager_root, version, root / ("pnpm-" + version + ".tgz"))
            atomic_json(root / "manager-provenance.json", manager_proof)
            node_root = ctx.legacy.parent / "strict_screen_20260726/oracle_recovery/tsjs/_runtimes" / (
                "node-v" + case["node"] + "-linux-x64")
            record["node"] = node_asset(ctx, case["node"])
            record["manager"] = ctx.bundle(Path(manager_root), role="toolchain")
            record["manager_provenance"] = {"path": str(root / "manager-provenance.json"),
                                             "sha256": file_hash(root / "manager-provenance.json")}
            # Preserve the one real install and receipt if a later export fails.
            # This tree is never itself archived or used by a candidate runtime.
            source = root / "installed"
            with start_archive(ctx.repo(task), task["start_sha"]) as stream:
                extract_tar(stream, source)
            plan = input_plan(source)
            record["input_hashes"] = plan["input_hashes"]
            store = root / "store"
            store.mkdir()
            seed = seed_store(plan["packages"], Path(retained_store) / "v10", store / "v10")
            atomic_json(root / "store-reuse.json", seed)
            ctx._pnpm_node_version = case["node"]
            record["install"] = install(ctx, source, store, node_root, Path(manager_root),
                                          root / "install.log", version)
            check_inputs(source, plan["input_hashes"])
            record.update(status="installed_not_exported", retained_install=str(source))
            atomic_json(root / "process.json", record)
            layout, index = export_payload(source, root / "payload", plan)
            atomic_json(root / "external-index.json", index)
            record.update({k: layout[k] for k in ("module_roots", "workspace_links",
                                                   "external_bytes", "external_index_sha256",
                                                   "workspace_production_files_exported")})
            record["dependencies"] = ctx.bundle(root / "payload", role="dependency")
            directory = ctx.legacy / "tsjs" / oracle["recovery_artifact"]
            record["original_logs"] = [
                {"name": row["log"].split("/")[-1], "sha256": file_hash(directory / row["log"].split("/")[-1])}
                for rows in (oracle["start_runs"], oracle["source_only_golden_runs"]) for row in rows
            ]
            record.update(status="prepared", ended_at=time.time())
            atomic_json(root / "dependencies.json", record)
        except Exception as exc:
            record.update(status="blocked", error=f"{type(exc).__name__}: {exc}", ended_at=time.time())
            raise
        finally:
            atomic_json(root / "process.json", record)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("config", "legacy-root", "out-dir", "cohort-ids", "manager-root", "retained-store"):
        parser.add_argument("--" + flag, type=Path, required=True)
    args = parser.parse_args()
    config = load_config(args.config)
    if engine_identity()["fingerprint"] != ENGINE:
        raise ContractError("pnpm preparation must use the original frozen engine")
    tasks = selected_tasks(Path(config["tasks"]), Path(config["scope_ids"]))
    ids = args.cohort_ids.read_text().splitlines()
    if len(ids) != 1 or ids[0] not in tasks or len(tasks) != 402:
        raise ContractError("pnpm bounded cohort must retain the complete402 scope")
    ident = ids[0]
    oracles = read_jsonl(Path(config["oracles"]))
    cases = json.loads((args.legacy_root / "tsjs/cases.json").read_text())["cases"]
    ctx = MigrationContext(config, tasks, oracles, args.legacy_root, args.out_dir)
    services = ExecutionServices(config)
    try:
        value = prepare(ctx, services, tasks[ident], oracles[ident], cases[ident],
                        args.out_dir / component(ident), args.manager_root, args.retained_store)
        summary = {"id": ident, "status": value["status"], "expected_tasks": 402,
                   "dependencies": str(args.out_dir / component(ident) / "dependencies.json"),
                   "external_bytes": value["external_bytes"], "workspace_links": len(value["workspace_links"]),
                   "evaluation_completion": False}
        code = 0
    except Exception as exc:
        summary = {"id": ident, "status": "blocked", "expected_tasks": 402,
                   "error": f"{type(exc).__name__}: {exc}", "evaluation_completion": False}
        code = 2
    atomic_json(args.out_dir / "summary.json", summary)
    print(json.dumps(summary), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
