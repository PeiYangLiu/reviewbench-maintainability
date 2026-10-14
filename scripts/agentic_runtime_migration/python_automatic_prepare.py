"""Plan, or explicitly prepare, the three automatic Python dependency snapshots.

Run as a module with REVIEWBENCH_ENGINE_SRC/PYTHONPATH pointing at the frozen
engine. Default operation writes recovery/dependency plans, not ready markers.
--prepare performs sequential, shared-gate-admitted work (one 2 CPU / 4 GiB job).
It never installs a project, checks out a cache, or edits a retained virtualenv.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import shlex
import subprocess
import time

from reviewbench_maintainability.agentic.cli import load_config
from reviewbench_maintainability.agentic.contracts import (
    ContractError, atomic_json, canonical_hash, file_hash, read_jsonl, selected_tasks,
)
from reviewbench_maintainability.agentic.models import engine_identity
from reviewbench_maintainability.agentic.resources import (
    CURRENT_JOB, GiB, ResourceGate, ResourceLimits, ResourceRequest,
)
from .common import MigrationContext, bounded_cpu_set
from .python_automatic import (
    CASES, DEPENDENCY_SCHEMA, dependency_files, dependency_inventory, entry_contract,
    grading_command, recover_contract, runtime_limits,
)

FROZEN_ENGINE = "5142d1fec94eb6c5a6177a66ee7ce8533a26f0a2578280075a6947a399c01ca3"


class ReadOnlyContext:
    def __init__(self, config, legacy):
        self.config, self.legacy = config, legacy
        self.repo_cache = Path(config["repo_cache"])

    repo = MigrationContext.repo
    task_directory = MigrationContext.task_directory


def recorded(command: list[str], output: Path, name: str, *, timeout: int = 300) -> str:
    started = time.monotonic()
    try:
        process = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
        code, stdout, stderr = process.returncode, process.stdout, process.stderr
    except subprocess.TimeoutExpired as exc:
        code = 124
        stdout = (exc.stdout or b"").decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = "dependency preparation command timed out\n"
    (output / (name + ".stdout")).write_text(stdout)
    (output / (name + ".stderr")).write_text(stderr)
    atomic_json(output / (name + ".json"), {
        "command": command, "shell_command": shlex.join(command), "exit_code": code,
        "stdout": stdout, "stderr": stderr, "duration_seconds": round(time.monotonic() - started, 3),
    })
    if code:
        raise ContractError(f"Python dependency preparation {name} exited {code}: {stderr[-1000:]}")
    return stdout


def bounded_container(image: str, command: list[str], *, mounts=(), audit_rootfs=False) -> list[str]:
    job = CURRENT_JOB.get()
    if job is None:
        raise ContractError("Python dependency container requires the shared resource gate")
    job["mark_container_use"]()
    name = "rb-python-auto-" + job["lease"][:20]
    args = ["docker", "run", "--rm", "--name", name,
            "--label", "reviewbench.resource-lease=" + job["lease"],
            "--cpus", "2", "--cpuset-cpus", bounded_cpu_set(2, job["lease"]),
            "--memory", "4g", "--memory-swap", "4g", "--pids-limit", "256",
            "--read-only", "--network", "none", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--tmpfs", "/tmp:rw,nosuid,size=268435456",
            "-e", "OMP_NUM_THREADS=1", "-e", "OPENBLAS_NUM_THREADS=1", "-e", "MKL_NUM_THREADS=1",
            "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "MPLCONFIGDIR=/tmp/matplotlib",
            "-e", "LOKY_MAX_CPU_COUNT=2"]
    if audit_rootfs:
        if mounts:
            raise ContractError("read-search image audit must not mount host inputs")
        # The image-only scanner must see rootfs directories owned by system
        # accounts (e.g. _apt's 0700 partial cache). Keep read-only/no-network
        # isolation and no host mounts; ordinary runtime/import jobs get no cap.
        args += ["--cap-add=DAC_READ_SEARCH"]
    for source, target in mounts:
        args += ["--mount", f"type=bind,src={source},dst={target},readonly"]
    return [*args, "--entrypoint", command[0], image, *command[1:]]


def container_record(image, command, output, name, *, mounts=(), timeout=300, audit_rootfs=False):
    args = bounded_container(image, command, mounts=mounts, audit_rootfs=audit_rootfs)
    try:
        return recorded(args, output, name, timeout=timeout)
    finally:
        # Only our newly named container is addressed, including timeout cleanup.
        container = args[args.index("--name") + 1]
        cleanup = subprocess.run(["docker", "rm", "-f", container], text=True, capture_output=True, timeout=30)
        atomic_json(output / (name + "-cleanup.json"), {
            "command": ["docker", "rm", "-f", container], "exit_code": cleanup.returncode,
            "stdout": cleanup.stdout, "stderr": cleanup.stderr,
        })


def prepare_one(ctx, task, case, recovery, inventory, directory, *, pull=False):
    site = Path(inventory["site"])
    names = dependency_files(site, case, inventory)
    input_bytes = sum((site / name).stat().st_size for name in names)
    if input_bytes * 2 + 2 * GiB > 16 * GiB:
        raise ContractError("retained Python dependency site exceeds the 16 GiB preparation reservation: "
                            + str(input_bytes) + " input bytes; a separately pinned smaller snapshot is needed")
    reference = inventory["image_reference"]
    if pull:
        recorded(["docker", "pull", reference], directory, "image-pull", timeout=900)
    image = json.loads(recorded(["docker", "image", "inspect", reference], directory, "image-inspect"))[0]["Id"]
    version = container_record(image, ["python3", "-I", "-B", "-c",
                               "import platform; print(platform.python_version())"],
                               directory, "interpreter").strip()
    if version != inventory["python_version"]:
        raise ContractError("retained automatic Python venv and container interpreter differ")
    scan = ("find / -xdev \\( -name .git -o -name '*source-only-golden.patch' "
            "-o -name '*golden*.patch' -o -name oracles.jsonl "
            "-o -name generation_results.jsonl \\) -print")
    scanned = container_record(image, ["/bin/bash", "-c", scan], directory, "image-scan", audit_rootfs=True)
    if scanned.strip():
        raise ContractError("automatic Python developer image contains source/reference inputs")
    history = recorded(["docker", "history", "--no-trunc", "--format", "{{json .}}", image],
                       directory, "image-history")
    audit_path = directory / "image-audit.json"
    atomic_json(audit_path, {
        "schema_version": "reviewbench-developer-image-audit/v1", "image": image,
        "expected_image_id": image, "python_version": version,
        "no_reference_assets": True, "no_repository_history": True,
        "basis": "executed bounded toolchain-only rootfs scan; dependency site is a separate asset",
        "rootfs_scan_command": scan, "rootfs_scan_output": scanned,
        "history": [json.loads(line) for line in history.splitlines()],
    })
    # This read-only import probe never runs pytest or an evaluated project.
    # -I plus a plain sys.path insertion does not execute editable .pth hooks.
    probe = ("import importlib,sys; sys.path.insert(0,'/dependency-site'); "
             f"[importlib.import_module(n) for n in {case.imports!r}]; "
             f"assert not set({case.source_packages!r}).intersection(sys.modules); "
             "print('External dependency imports: PASS')")
    container_record(image, ["python3", "-I", "-B", "-c", probe], directory, "dependency-imports",
                     mounts=[(site, "/dependency-site")], timeout=300)
    site_asset = ctx.bundle(site, names, role="dependency")
    descriptor = json.loads(Path(site_asset["path"]).read_text())
    value = {
        "schema_version": DEPENDENCY_SCHEMA, "status": "prepared",
        "task_id": task["id"], "repo": task["repo"], "start_sha": task["start_sha"],
        "recovery_sha256": canonical_hash(recovery), "python_version": version,
        "image": image, "image_audit": ctx.copy_asset(audit_path, "image-audit"),
        "site_asset": site_asset, "site_tree_sha256": descriptor["tree_sha256"],
        "excluded_source_packages": list(case.source_packages), "dependency_inventory": inventory,
        "files": len(names), "input_bytes": input_bytes, "runtime_verified": False,
    }
    atomic_json(directory / "preparation.json", value)
    return value


def preparation_config(config, project_root, output):
    """Keep the shared content-addressed Blob cache instead of allocating another cache budget."""
    return {
        **config, "root": str(project_root.resolve()), "out_dir": str(output / "execution"),
        "storage": dict(config["storage"]),
        "migration": {**config["migration"], "python_automatic_dependency_root": str(output)},
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--legacy-root", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--pull", action="store_true")
    parser.add_argument("--task-ids", nargs="+", choices=list(CASES))
    args = parser.parse_args(argv)
    if args.pull and not args.prepare:
        parser.error("--pull requires --prepare")
    import yaml
    raw = yaml.safe_load(args.config.read_text())
    project_root = Path(raw.get("root", args.config.resolve().parent))
    if not project_root.is_absolute():
        project_root = args.config.resolve().parent / project_root
    config = load_config(args.config)
    identity = engine_identity()
    if (identity["fingerprint"] != FROZEN_ENGINE
            or config.get("migration", {}).get("verification_engine_fingerprint") != FROZEN_ENGINE):
        raise ContractError("Python preparation must use the attested frozen engine")
    tasks = selected_tasks(Path(config["tasks"]), Path(config["scope_ids"]))
    if len(tasks) != 402 or not CASES.keys() <= tasks.keys():
        raise ContractError("Python preparation must preserve the complete audited-402 scope")
    oracles = read_jsonl(Path(config["oracles"]))
    output, legacy = args.out_dir.resolve(), args.legacy_root.resolve()
    protected = [project_root.resolve(), legacy, Path(config["repo_cache"]).resolve(),
                 Path(config["storage"]["cache_dir"]).resolve()]
    if any(output == p or output.is_relative_to(p) or p.is_relative_to(output) for p in protected):
        raise ContractError("Python preparation output must be a private directory")
    output.mkdir(parents=True, exist_ok=True)
    # Keep the original asset_root/runtime_recipes pair and the same shared
    # cache/transfer limits. Only per-task preparation outputs are private.
    private_config = preparation_config(config, project_root, output)
    (output / "experiment-python-automatic.yaml").write_text(yaml.safe_dump(private_config, sort_keys=False))
    selected = args.task_ids or list(CASES)
    (output / "migration.ids").write_text("".join(i + "\n" for i in selected))
    gate = ResourceGate(Path(config["execution"]["gate_dir"]), Path(config["storage"]["scratch_dir"]),
                        ResourceLimits(**config["execution"]["resource_limits"]))
    readonly = ReadOnlyContext(config, legacy)
    summary = {"scope_size": len(tasks), "batch_ids": selected, "engine_fingerprint": identity["fingerprint"],
               "runtime_verified": False, "prepared": [], "plans": [], "failures": {}}
    for ident in selected:
        task, case = tasks[ident], CASES[ident]
        directory = output / Path(case.directory).name
        directory.mkdir(parents=True, exist_ok=True)
        # No nested leases. A large numerical dependency package also consumes
        # a heavy slot; plan-only inspection never starts a container/archive.
        request = ResourceRequest("python-auto-prepare:" + ident, cpus=2, memory_bytes=4 * GiB,
                                  scratch_bytes=(16 if args.prepare else 1) * GiB,
                                  heavy=args.prepare and case.distribution == "mindscape")
        try:
            with gate.acquire(request, uses_containers=args.prepare, timeout=180) as lease:
                recovery = recover_contract(readonly, task, oracles[ident])
                inventory = dependency_inventory(readonly.repo(task), case)
                atomic_json(directory / "recovery.json", recovery)
                plan = {
                    "task_id": ident, "start_sha": task["start_sha"], "scope_size": len(tasks),
                    "recovery_sha256": canonical_hash(recovery), "dependency_inventory": inventory,
                    "resource_request": asdict(request), "gate": str(gate.root), "lease": lease,
                    "mode": "prepare" if args.prepare else "plan-only",
                    "runtime_configuration": {
                        "workdir": "/workspace", **runtime_limits(recovery),
                        "entry_contract": entry_contract(recovery, inventory["python_version"]),
                        "grading_command": grading_command(recovery),
                    },
                    "runtime_verified": False,
                    "required_next": ["prepared dependency archive and image audit",
                                      "frozen-engine developer check and 3x start/source-only-golden replay"],
                }
                atomic_json(directory / "plan.json", plan)
                summary["plans"].append(ident)
                if args.prepare:
                    ctx = MigrationContext(private_config, tasks, oracles, legacy, output / "runtime")
                    prepare_one(ctx, task, case, recovery, inventory, directory, pull=args.pull)
                    summary["prepared"].append(ident)
        except Exception as exc:
            summary["failures"][ident] = f"{type(exc).__name__}: {exc}"
        atomic_json(output / "summary.json", summary)
        print(json.dumps({"task_id": ident, "mode": "prepare" if args.prepare else "plan-only",
                          "error": summary["failures"].get(ident), "runtime_verified": False}), flush=True)
    return 2 if summary["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
