"""Cross-process admission limits for local work, not unbounded thread counts."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import Callable
import uuid

from .contracts import ContractError, InfrastructureError, atomic_json

GiB = 1024**3
CURRENT_JOB: ContextVar[dict | None] = ContextVar("reviewbench_resource_job", default=None)


def job_directory(*, prefix: str):
    job = CURRENT_JOB.get()
    return tempfile.TemporaryDirectory(prefix=prefix, dir=job["scratch_root"] if job else None)


def containers_alive(lease: str) -> bool:
    """An uncertain Docker observation retains the reservation, never frees it."""
    try:
        result = subprocess.run(
            ["docker", "ps", "-q", "--filter", "label=reviewbench.resource-lease=" + lease],
            capture_output=True, text=True, timeout=15,
        )
        return result.returncode != 0 or bool(result.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return True


@dataclass(frozen=True)
class ResourceLimits:
    tasks: int = 8
    heavy_tasks: int = 3
    cpus: float = 32
    memory_bytes: int = 64 * GiB
    scratch_bytes: int = 96 * GiB
    min_available_memory_bytes: int = 32 * GiB
    min_free_disk_bytes: int = 80 * GiB
    max_load_fraction: float = .8

    def __post_init__(self):
        if (self.tasks < 1 or not 1 <= self.heavy_tasks <= self.tasks or self.cpus <= 0
                or self.memory_bytes <= 0 or self.scratch_bytes <= 0
                or min(self.min_available_memory_bytes, self.min_free_disk_bytes) < 0
                or not 0 < self.max_load_fraction <= 1):
            raise ContractError("invalid resource admission limits")


@dataclass(frozen=True)
class ResourceRequest:
    name: str
    cpus: float = 1
    memory_bytes: int = 512 * 1024**2
    scratch_bytes: int = 0
    heavy: bool = False


def process_identity(pid: int) -> str | None:
    """Linux start ticks distinguish a still-live owner from a reused PID."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        return text[text.rfind(")") + 2:].split()[19]
    except FileNotFoundError:
        return None
    except (PermissionError, IndexError):
        # Ambiguous observation never licenses removal of a live reservation.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            pass
        return "unknown"


def host_resources(storage_path: Path) -> dict:
    memory = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        memory[key] = int(value.split()[0]) * 1024
    return {
        "available_memory_bytes": memory["MemAvailable"],
        "free_disk_bytes": shutil.disk_usage(storage_path).free,
        "load_1m": os.getloadavg()[0], "host_cpus": os.cpu_count() or 1,
        "time": time.time(),
    }


@contextmanager
def file_lock(path: Path, *, shared: bool = False, nonblocking: bool = False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        flags = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        fcntl.flock(stream, flags | (fcntl.LOCK_NB if nonblocking else 0))
        try:
            yield stream
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


class ResourceGate:
    """A shared reservation ledger plus current host headroom checks.

    A waiting task has not started an agent's interaction budget. Existing jobs
    are never killed just to admit another job. Reservations of a crashed
    process are reclaimed only after checking the actual process identity.
    """

    def __init__(self, root: Path, storage_path: Path, limits: ResourceLimits | None = None,
                 *, probe: Callable | None = None):
        self.root, self.storage_path = root.resolve(), storage_path.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.storage_path.mkdir(parents=True, exist_ok=True)
        self.limits = limits or ResourceLimits()
        self.probe = probe or (lambda: host_resources(self.storage_path))
        self.state_path = self.root / "reservations.json"
        self.lock_path = self.root / "reservations.lock"

    def _state(self) -> dict:
        if self.state_path.exists():
            state = json.loads(self.state_path.read_text())
            if state["limits"] != asdict(self.limits) or state["storage_path"] != str(self.storage_path):
                raise ContractError("resource gate already has a different configuration")
        else:
            state = {"limits": asdict(self.limits), "storage_path": str(self.storage_path),
                     "leases": {}, "peaks": {"tasks": 0, "heavy_tasks": 0, "cpus": 0,
                                            "memory_bytes": 0, "scratch_bytes": 0},
                     "admitted": 0, "completed": 0, "reclaimed_dead_owners": 0}
        for key, lease in list(state["leases"].items()):
            if lease.get("orphaned_containers") and not containers_alive(key):
                del state["leases"][key]
                state["reclaimed_dead_owners"] += 1
                continue
            actual = process_identity(lease["pid"])
            if actual is None or (actual != "unknown" and lease["process_start"] not in {None, "unknown"}
                                  and actual != lease["process_start"]):
                if lease.get("uses_containers") and containers_alive(key):
                    lease["orphaned_containers"] = True
                    continue
                del state["leases"][key]
                state["reclaimed_dead_owners"] += 1
        return state

    def _totals(self, leases: dict) -> dict:
        return {"tasks": len(leases),
                "heavy_tasks": sum(bool(v["request"]["heavy"]) for v in leases.values()),
                **{name: sum(v["request"][name] for v in leases.values())
                   for name in ("cpus", "memory_bytes", "scratch_bytes")}}

    def _blocked(self, request: ResourceRequest, totals: dict, host: dict) -> str | None:
        limits = self.limits
        if totals["tasks"] >= limits.tasks:
            return "task_slots"
        if request.heavy and totals["heavy_tasks"] >= limits.heavy_tasks:
            return "heavy_slots"
        for key in ("cpus", "memory_bytes", "scratch_bytes"):
            if totals[key] + getattr(request, key) > getattr(limits, key):
                return key + "_reservation"
        # Conservative accounting: reserved bytes need not have been allocated
        # yet. Reserving them again against physical free space avoids a burst
        # of admitted jobs each assuming it owns the same remaining capacity.
        if (host["free_disk_bytes"] - totals["scratch_bytes"] - request.scratch_bytes
                < limits.min_free_disk_bytes):
            return "host_disk_headroom"
        if (host["available_memory_bytes"] - totals["memory_bytes"] - request.memory_bytes
                < limits.min_available_memory_bytes):
            return "host_memory_headroom"
        if host["load_1m"] > host["host_cpus"] * limits.max_load_fraction:
            return "host_cpu_pressure"
        return None

    @contextmanager
    def acquire(self, request: ResourceRequest, *, timeout: float | None = None,
                poll_seconds: float = .5, uses_containers: bool = False,
                source_catalog=None, task: dict | None = None, adopt_lease: str | None = None):
        if (not request.name or request.cpus <= 0 or request.memory_bytes < 0
                or request.scratch_bytes < 0):
            raise ContractError("invalid resource request")
        if any(getattr(request, key) > getattr(self.limits, key)
               for key in ("cpus", "memory_bytes", "scratch_bytes")):
            raise ContractError("single task exceeds configured reservation capacity; provision a separate lane")
        if adopt_lease and not re.fullmatch(r"[0-9a-f]{32}", adopt_lease):
            raise ContractError("invalid saved resource lease")
        key = adopt_lease or uuid.uuid4().hex
        started, previous_reason = time.monotonic(), None
        while True:
            with file_lock(self.lock_path):
                state = self._state()
                host = self.probe()
                old = state["leases"].get(key) if adopt_lease else None
                if old:
                    owner = process_identity(old["pid"])
                    alive_owner = owner is not None and (owner == "unknown" or owner == old["process_start"])
                    if old["request"] != asdict(request) or (alive_owner and not old.get("orphaned_containers")):
                        raise ContractError("saved resource reservation is owned by a live/different task")
                    reason = None
                else:
                    reason = self._blocked(request, self._totals(state["leases"]), host)
                if reason is None:
                    state["leases"][key] = {
                        "pid": os.getpid(), "process_start": process_identity(os.getpid()),
                        "request": asdict(request), "started_at": time.time(),
                        "uses_containers": bool(old and old.get("uses_containers")),
                    }
                    totals = self._totals(state["leases"])
                    state["peaks"] = {name: max(value, totals[name]) for name, value in state["peaks"].items()}
                    state["admitted"] += int(old is None)
                    state["adopted"] = state.get("adopted", 0) + int(old is not None)
                state["last_host_sample"] = host
                atomic_json(self.state_path, state)
            if reason is None:
                break
            if reason != previous_reason:
                with (self.root / "admission-waits.jsonl").open("a") as stream:
                    stream.write(json.dumps({"name": request.name, "reason": reason, "time": time.time()}) + "\n")
                previous_reason = reason
            if timeout is not None and time.monotonic() - started >= timeout:
                raise InfrastructureError(f"resource admission timed out: {reason}")
            time.sleep(poll_seconds)
        token = CURRENT_JOB.set({"lease": key, "scratch_root": str(self.storage_path),
                                 "disk_floor_bytes": self.limits.min_free_disk_bytes,
                                 "source_catalog": source_catalog, "task": task,
                                 "mark_container_use": lambda: self._mark_container_use(key, uses_containers)})
        try:
            yield {"lease": key, "wait_seconds": time.monotonic() - started}
        finally:
            CURRENT_JOB.reset(token)
            with file_lock(self.lock_path):
                state = self._state()
                if state["leases"].get(key, {}).get("uses_containers") and containers_alive(key):
                    # A failed docker rm must not let the next task assume the
                    # same CPU/RAM/disk reservation has become available.
                    state["leases"][key]["orphaned_containers"] = True
                else:
                    state["leases"].pop(key, None)
                    state["completed"] += 1
                atomic_json(self.state_path, state)

    def _mark_container_use(self, key: str, allowed: bool) -> None:
        # Persist before the first Docker creation/reconnection attempt. Pure
        # source jobs and mocked/terminal fast paths need no Docker observation.
        if not allowed:
            raise ContractError("source-only resource lease may not start a container")
        with file_lock(self.lock_path):
            state = self._state()
            state["leases"][key]["uses_containers"] = True
            atomic_json(self.state_path, state)

    def status(self) -> dict:
        with file_lock(self.lock_path):
            state = self._state()
            atomic_json(self.state_path, state)
            return {**state, "active": self._totals(state["leases"])}
