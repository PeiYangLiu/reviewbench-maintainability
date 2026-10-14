#!/usr/bin/env python3
"""Durable bounded preparation DAG; slow verification/archive jobs occupy separate pools."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import nullcontext
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.environ.get("REVIEWBENCH_ENGINE_SRC", str(Path(__file__).resolve().parents[1] / "src")))
from reviewbench_maintainability.agentic.cli import load_config
from reviewbench_maintainability.agentic.contracts import ContractError, atomic_json, canonical_hash, file_hash
from reviewbench_maintainability.agentic.execution import ExecutionServices
from reviewbench_maintainability.agentic.resources import ResourceRequest, file_lock, process_identity
from agentic_runtime_migration.cache_pressure import CachePressure, policy_options

SCHEMA = "reviewbench-preparation-dag/v1"
DEFAULT_WORKERS = {"prepare": 2, "verify": 2, "archive": 1}
INFLIGHT_GRACE_SECONDS = 10


def require(value, message):
    if not value:
        raise ContractError("preparation queue: " + message)


def read(path):
    return json.loads(Path(path).read_text())


def dispatch_request(job):
    """A non-reserving hint; the command's existing gate remains authoritative."""
    if job["resource_owner"] == "queue":
        require("admission_resources" not in job, "queue-owned jobs already declare resources")
        values = job.get("resources")
    else:
        values = job.get("admission_resources")
        if values is None:
            return None  # Preserve old child-owned jobs without inventing an estimate.
    require(isinstance(values, dict) and set(values) <= {
        "cpus", "memory_bytes", "scratch_bytes", "heavy"}, "invalid dispatch resource fields")
    request = ResourceRequest(name="prepare-queue:" + job["id"], **values)
    require(type(request.cpus) in (int, float) and math.isfinite(request.cpus)
            and request.cpus > 0, "invalid dispatch CPU request")
    require(all(type(getattr(request, key)) is int and getattr(request, key) >= 0
                for key in ("memory_bytes", "scratch_bytes")), "invalid dispatch byte request")
    require(type(request.heavy) is bool, "invalid dispatch heavy flag")
    return request


def admission_wait(gate, request):
    """Backfill around an unfit job without acquiring a lease or starting its clock.

    The locked observation uses exactly the frozen gate's admission rules.
    A concurrent owner may change capacity after this observation; the real
    supervisor/child still acquires its own lease and handles that race.
    """
    require(all(getattr(request, key) <= getattr(gate.limits, key)
                for key in ("cpus", "memory_bytes", "scratch_bytes")),
            "single dispatch request exceeds configured capacity")
    with file_lock(gate.lock_path):
        state = gate._state()
        totals, host = gate._totals(state["leases"]), gate.probe()
        reason = gate._blocked(request, totals, host)
    if reason is None:
        return None
    return {"reason": reason, "active": totals,
            "free_disk_bytes": host["free_disk_bytes"],
            "available_memory_bytes": host["available_memory_bytes"]}


def inflight_disk_wait(gate, job, owner):
    """Observe a dispatched admission race without counting an owned lease twice.

    Queue-owned supervisors explicitly journal their admission wait. A
    child-owned command has no such journal, so it is observed only when the
    entire shared gate is idle, its exact child remains live and startup grace
    has elapsed. No process, deadline, command or result is changed here.
    """
    request = dispatch_request(job)
    if request is None:
        return None
    supervisor = owner.get("supervisor_pid")
    if (not isinstance(supervisor, int) or owner.get("process_start") in {None, "unknown"}
            or process_identity(supervisor) != owner["process_start"]):
        return None
    if job["resource_owner"] == "queue":
        if owner.get("status") != "waiting_for_resources":
            return None
    else:
        started = owner.get("command_started_at")
        child = owner.get("child_pid")
        if (owner.get("status") != "running" or not isinstance(started, (int, float))
                or time.time() - started < INFLIGHT_GRACE_SECONDS or not isinstance(child, int)
                or owner.get("child_start") in {None, "unknown"}
                or process_identity(child) != owner["child_start"]):
            return None
    with file_lock(gate.lock_path):
        state = gate._state()
        leases = state["leases"]
        if job["resource_owner"] == "child" and leases:
            return None  # Its own or another active lease makes ownership ambiguous.
        if any(lease["pid"] == owner["supervisor_pid"] and
               lease["process_start"] == owner["process_start"] for lease in leases.values()):
            return None  # Acquisition may race the supervisor's owner-file update.
        totals, host = gate._totals(leases), gate.probe()
        reason = gate._blocked(request, totals, host)
    if reason != "host_disk_headroom":
        return None
    return {"reason": reason, "active": totals, "dispatched": True,
            "observation": "supervisor-admission" if job["resource_owner"] == "queue" else "idle-gate-live-child",
            "free_disk_bytes": host["free_disk_bytes"],
            "available_memory_bytes": host["available_memory_bytes"]}


def record_dispatch_wait(root, job, wait):
    """Persist the admission deadline across controller restarts, without a worker."""
    directory = folder(root, job)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "dispatch-wait.json"
    now = time.time()
    first = read(path) if path.exists() else {
        "job_fingerprint": canonical_hash(job), "first_blocked_at": now}
    require(first["job_fingerprint"] == canonical_hash(job), "dispatch wait belongs to another job")
    elapsed = max(0, now - first["first_blocked_at"])
    record = {**first, **wait, "observed_at": now, "wait_seconds": elapsed}
    atomic_json(path, record)
    if elapsed < job.get("admission_timeout_seconds", 3600):
        return False
    # A terminal admission error is not a failed SUT/model command or a retry.
    atomic_json(directory / "result.json", {
        "job_id": job["id"], "job_fingerprint": canonical_hash(job), "pool": job["pool"],
        "status": "failed", "exit_status": 1, "timed_out": True, "admission_only": True,
        "command": job["command"], "cwd": job["cwd"], "command_executed": False,
        "error": "resource admission timed out before dispatch: " + wait["reason"],
        "outputs": [], "started_at": first["first_blocked_at"], "completed_at": now,
    })
    return True


def validate(plan):
    require(plan.get("schema_version") == SCHEMA, "unsupported schema")
    require(isinstance(plan.get("scope"), dict)
            and type(plan["scope"].get("expected_tasks")) is int
            and plan["scope"]["expected_tasks"] > 0, "the original task denominator is required")
    workers = plan.get("workers", DEFAULT_WORKERS)
    require(set(workers) == set(DEFAULT_WORKERS), "all three pools must be explicit")
    require(all(type(n) is int and 1 <= n <= DEFAULT_WORKERS[k] for k, n in workers.items()),
            "pool limits exceed prepare2/verify2/archive1")
    if "cache_pressure" in plan:
        policy_options(plan["cache_pressure"])
    jobs = plan["jobs"]
    require(isinstance(jobs, list), "jobs must be a list")
    by_id = {}
    for job in jobs:
        ident = job["id"]
        require(isinstance(ident, str) and ident and ident not in by_id, "duplicate/empty job identity")
        require(job["pool"] in workers, "unknown worker pool")
        require(isinstance(job["command"], list) and job["command"]
                and all(isinstance(s, str) and s and "\0" not in s for s in job["command"]),
                "command must be a literal argument vector")
        require(Path(job["cwd"]).is_absolute(), "cwd must be absolute")
        require(type(job.get("timeout_seconds")) in (int, float)
                and 0 < job["timeout_seconds"] <= 86400, "invalid command timeout")
        admission_timeout = job.get("admission_timeout_seconds", 3600)
        require(type(admission_timeout) in (int, float) and 0 < admission_timeout <= 86400,
                "invalid admission timeout")
        require(job.get("resource_owner") in {"queue", "child"}, "declare exactly one resource owner")
        require(job.get("resource_owner") != "queue" or isinstance(job.get("resources"), dict),
                "queue-owned jobs need a resource request")
        dispatch_request(job)
        accepted = job.get("accepted_exit_statuses", [0])
        require(isinstance(accepted, list) and accepted and
                all(type(code) is int and 0 <= code <= 255 for code in accepted), "invalid exit status policy")
        require(accepted == [0] or job.get("success_when"),
                "nonzero success requires an independently checked output condition")
        for row in job.get("inputs", []):
            require(Path(row["path"]).is_absolute() and isinstance(row.get("sha256"), str)
                    and len(row["sha256"]) == 64, "input requires an absolute path and digest")
        for output in job.get("outputs", []):
            require(Path(output).is_absolute(), "output path must be absolute")
        require(isinstance(job.get("needs", []), list), "dependencies must be a list")
        by_id[ident] = job
    require(all(set(job.get("needs", [])) <= set(by_id) for job in jobs), "unknown dependency")
    visited, active = set(), set()

    def visit(ident):
        require(ident not in active, "dependency cycle")
        if ident in visited:
            return
        active.add(ident)
        for dependency in by_id[ident].get("needs", []):
            visit(dependency)
        active.remove(ident)
        visited.add(ident)

    for ident in by_id:
        visit(ident)
    return by_id, workers


def folder(root, job):
    return root / "jobs" / canonical_hash(job)


def terminal(root, job):
    path = folder(root, job) / "result.json"
    if not path.exists():
        return None
    value = read(path)
    require(value["job_fingerprint"] == canonical_hash(job), "terminal result belongs to another job")
    if value["status"] == "succeeded":
        for row in value["outputs"]:
            require(Path(row["path"]).is_file() and file_hash(Path(row["path"])) == row["sha256"],
                    "successful output changed; do not silently rerun")
    return value


def conditions_match(conditions):
    for condition in conditions:
        path = Path(condition["path"])
        if not path.is_file():
            return False
        value = read(path)
        for key in condition["keys"]:
            if not isinstance(value, dict) or key not in value:
                return False
            value = value[key]
        if value != condition["equals"]:
            return False
    return True


def ready_when(job):
    return conditions_match(job.get("ready_when", []))


def execute_job(spec, output, config_path):
    job = read(spec)
    expected = canonical_hash(job)
    output.mkdir(parents=True, exist_ok=True)
    with file_lock(output / "execution.lock"):
        require(not (output / "result.json").exists(), "job already has a terminal result")
        record = {"job_id": job["id"], "job_fingerprint": expected, "pool": job["pool"],
                  "supervisor_pid": os.getpid(), "process_start": process_identity(os.getpid()),
                  "status": "waiting_for_resources", "started_at": time.time(),
                  "command": job["command"], "cwd": job["cwd"], "inputs": job.get("inputs", [])}
        atomic_json(output / "owner.json", record)
        process = None
        try:
            for item in job.get("inputs", []):
                require(Path(item["path"]).is_file() and file_hash(Path(item["path"])) == item["sha256"],
                        "pinned command/config/input changed")
            config = load_config(config_path)
            request = ResourceRequest(name="prepare-queue:" + job["id"], **job.get("resources", {}))
            gate = (ExecutionServices(config).gate.acquire(
                request, uses_containers=job.get("uses_containers", False),
                timeout=job.get("admission_timeout_seconds", 3600))
                if job["resource_owner"] == "queue" else nullcontext(None))
            with gate as lease:
                if job["resource_owner"] == "queue":
                    cpus = sorted(os.sched_getaffinity(0))
                    offset = int(expected[:8], 16) % len(cpus)
                    os.sched_setaffinity(0, {cpus[(offset + i) % len(cpus)]
                                            for i in range(math.ceil(request.cpus))})
                environment = {**os.environ, **job.get("env", {}), "PYTHONDONTWRITEBYTECODE": "1"}
                with (output / "stdout").open("xb") as stdout, (output / "stderr").open("xb") as stderr:
                    process = subprocess.Popen(job["command"], cwd=job["cwd"], env=environment,
                                               stdout=stdout, stderr=stderr, start_new_session=True)
                    record.update(status="running", child_pid=process.pid,
                                  child_start=process_identity(process.pid), lease=lease,
                                  command_started_at=time.time())
                    atomic_json(output / "owner.json", record)
                    timed_out = False
                    try:
                        code = process.wait(timeout=job["timeout_seconds"])
                    except subprocess.TimeoutExpired:
                        timed_out = True
                        os.killpg(process.pid, signal.SIGTERM)
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait()
                        code = 124
                outputs = []
                succeeded = code in job.get("accepted_exit_statuses", [0]) and not timed_out
                if succeeded:
                    require(conditions_match(job.get("success_when", [])), "declared success output condition is false")
                    names = set(job.get("outputs", [])) | {row["path"] for row in job.get("success_when", [])}
                    for name in sorted(names):
                        path = Path(name)
                        require(path.is_file() and not path.is_symlink(), "declared output is absent/not regular")
                        outputs.append({"path": str(path), "sha256": file_hash(path),
                                        "bytes": path.stat().st_size})
                record.update(status="succeeded" if succeeded else "failed", exit_status=code,
                              timed_out=timed_out, outputs=outputs)
        except Exception as error:
            # Never convert a killed observer into a fresh model/SUT attempt.
            record.update(status="failed", exit_status=1, error=f"{type(error).__name__}: {error}", outputs=[])
        finally:
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=10)
        record.update(completed_at=time.time())
        for stream in ("stdout", "stderr"):
            path = output / stream
            if path.exists():
                record[stream + "_sha256"] = file_hash(path)
        atomic_json(output / "result.json", record)
        return record["exit_status"]


def run_queue(plan_path, config_path, root, *, watch=False, poll_seconds=1):
    require(poll_seconds > 0, "poll interval must be positive")
    root.mkdir(parents=True, exist_ok=True)
    with file_lock(root / "queue.lock"), CachePressure() as pressure:
        known = read(root / "manifest.json") if (root / "manifest.json").exists() else {}
        children = {}
        gate, services = None, None
        while True:
            plan = read(plan_path)
            jobs, workers = validate(plan)
            require(not known or known["scope"] == plan["scope"], "full task scope changed")
            old_jobs = {job["id"]: job for job in known.get("jobs", [])}
            require(set(old_jobs) <= set(jobs) and all(jobs[k] == v for k, v in old_jobs.items()),
                    "existing jobs changed/removed; append a new explicit attempt instead")
            for row in plan["scope"].get("inputs", []):
                require(file_hash(Path(row["path"])) == row["sha256"], "scope input changed")
            atomic_json(root / "manifest.json", plan)
            known = plan
            states, counts, waiting, observed_owners = {}, Counter(), {}, {}
            for ident, job in jobs.items():
                result = terminal(root, job)
                if result:
                    states[ident] = result["status"]
                    continue
                path = folder(root, job) / "owner.json"
                if path.exists():
                    owner = read(path)
                    identity = process_identity(owner["supervisor_pid"])
                    if identity is None or identity != owner["process_start"]:
                        # Completion can race the first terminal read. Consume
                        # the verified result, never relaunch a vanished owner.
                        completed = terminal(root, job)
                        if completed:
                            states[ident] = completed["status"]
                            continue
                    require(identity is not None and identity == owner["process_start"],
                            "interrupted job needs an explicit audit; it will not be automatically rerun")
                    states[ident] = "running"
                    counts[job["pool"]] += 1
                    observed_owners[ident] = owner
            for ident, job in jobs.items():
                if ident in states or counts[job["pool"]] >= workers[job["pool"]]:
                    continue
                prerequisites = [states.get(name, "pending") for name in job.get("needs", [])]
                if any(value in {"failed", "blocked"} for value in prerequisites):
                    states[ident] = "blocked"
                    continue
                if not all(value == "succeeded" for value in prerequisites) or not ready_when(job):
                    continue
                request = dispatch_request(job)
                if request is not None:
                    if gate is None:
                        services = ExecutionServices(load_config(config_path))
                        gate = services.gate
                    wait = admission_wait(gate, request)
                    if wait:
                        if wait["reason"] == "host_disk_headroom" and "cache_pressure" in plan:
                            required = (gate.limits.min_free_disk_bytes + wait["active"]["scratch_bytes"]
                                        + request.scratch_bytes)
                            pressure.request(services.cache, gate.storage_path, required, plan["cache_pressure"])
                        if record_dispatch_wait(root, job, wait):
                            states[ident] = "failed"
                        else:
                            waiting[ident] = wait
                        continue  # Do not spend a pool slot on a resource waiter.
                directory = folder(root, job)
                directory.mkdir(parents=True, exist_ok=True)
                require(not (directory / "job.json").exists(),
                        "interrupted dispatch requires an explicit audit, not a duplicate launch")
                atomic_json(directory / "job.json", job)
                command = [sys.executable, "-B", str(Path(__file__).resolve()), "--execute-job",
                           str(directory / "job.json"), "--config", str(config_path),
                           "--out-dir", str(directory)]
                log = (directory / "supervisor.log").open("ab")
                child = subprocess.Popen(command, stdout=log, stderr=log, start_new_session=True)
                log.close()
                children[ident] = child
                # Parent and supervisor share a durable handoff before another tick.
                deadline = time.monotonic() + 10
                while not (directory / "owner.json").exists() and child.poll() is None:
                    require(time.monotonic() < deadline, "supervisor startup unconfirmed")
                    time.sleep(.01)
                require((directory / "owner.json").exists(), "supervisor stopped before claiming the job")
                states[ident] = "running"
                counts[job["pool"]] += 1
            if "cache_pressure" in plan:
                for ident, owner in observed_owners.items():
                    job = jobs[ident]
                    if dispatch_request(job) is None:
                        continue
                    if gate is None:
                        services = ExecutionServices(load_config(config_path))
                        gate = services.gate
                    wait = inflight_disk_wait(gate, job, owner)
                    if wait is not None:
                        request = dispatch_request(job)
                        required = (gate.limits.min_free_disk_bytes + wait["active"]["scratch_bytes"]
                                    + request.scratch_bytes)
                        pressure.request(services.cache, gate.storage_path, required, plan["cache_pressure"])
                        waiting[ident] = wait
                        # The original child/supervisor owns its admission
                        # timeout. Do not create another deadline or launch.
            for child in children.values():
                child.poll()
            summary = {"schema_version": SCHEMA, "scope": plan["scope"], "workers": workers,
                       "states": {ident: states.get(ident, "pending") for ident in jobs},
                       "active_by_pool": dict(counts), "waiting_for_resources": waiting,
                       "dispatch_policy": "resource-aware-backfill",
                       "controller_pid": os.getpid(), "time": time.time()}
            if "cache_pressure" in plan:
                summary["cache_pressure"] = pressure.status()
            atomic_json(root / "status.json", summary)
            finished = all(state in {"succeeded", "failed", "blocked"} for state in summary["states"].values())
            if finished and not watch:
                return 0 if all(s == "succeeded" for s in summary["states"].values()) else 2
            time.sleep(poll_seconds)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--execute-job", type=Path)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=1)
    args = parser.parse_args()
    if args.execute_job:
        return execute_job(args.execute_job, args.out_dir, args.config)
    require(args.plan is not None, "--plan is required for the controller")
    return run_queue(args.plan, args.config, args.out_dir, watch=args.watch, poll_seconds=args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
