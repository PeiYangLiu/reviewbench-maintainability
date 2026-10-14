"""Observe admission races only; never restart commands or duplicate owned leases."""
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_agentic_prepare_queue as queue
from reviewbench_maintainability.agentic.cli import load_config
from reviewbench_maintainability.agentic.execution import ExecutionServices
from reviewbench_maintainability.agentic.resources import ResourceRequest, process_identity
from test_agentic_prepare_queue import setup, job


def fixture(tmp_path, child=True):
    config, worker = setup(tmp_path)
    item = job(tmp_path, worker, "same-original-job")
    item["resources"]["scratch_bytes"] = 100
    if child:
        item["resource_owner"] = "child"
        item["admission_resources"] = item.pop("resources")
    gate = ExecutionServices(load_config(config)).gate
    gate.probe = lambda: {"free_disk_bytes": 50, "available_memory_bytes": 10**12,
                         "load_1m": 0, "host_cpus": 8}
    owner = {"supervisor_pid": os.getpid(), "process_start": process_identity(os.getpid()),
             "status": "running" if child else "waiting_for_resources",
             "command_started_at": time.time() - 60, "child_pid": os.getpid(),
             "child_start": process_identity(os.getpid())}
    return gate, item, owner


def test_live_child_in_idle_gate_can_request_reclamation_without_reserving(tmp_path):
    gate, item, owner = fixture(tmp_path)
    before = gate.state_path.read_bytes() if gate.state_path.exists() else None
    result = queue.inflight_disk_wait(gate, item, owner)
    assert result["reason"] == "host_disk_headroom" and result["dispatched"] is True
    assert result["observation"] == "idle-gate-live-child" and result["active"]["tasks"] == 0
    assert (gate.state_path.read_bytes() if gate.state_path.exists() else None) == before


@pytest.mark.parametrize("owning", [True, False])
def test_child_monitor_never_double_counts_any_active_lease(tmp_path, owning):
    gate, item, owner = fixture(tmp_path)
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        with gate.acquire(ResourceRequest("already-running", cpus=1, memory_bytes=0)):
            if not owning:
                owner.update(supervisor_pid=other.pid, process_start=process_identity(other.pid),
                             child_pid=other.pid, child_start=process_identity(other.pid))
            assert queue.inflight_disk_wait(gate, item, owner) is None
    finally:
        other.terminate()
        other.wait(timeout=5)


def test_queue_owned_supervisor_can_be_observed_before_its_real_acquisition(tmp_path):
    gate, item, owner = fixture(tmp_path, child=False)
    result = queue.inflight_disk_wait(gate, item, owner)
    assert result["observation"] == "supervisor-admission"
    # Simulate acquisition racing the owner journal's state update.
    with gate.acquire(ResourceRequest("already-acquired", cpus=1, memory_bytes=0)):
        assert queue.inflight_disk_wait(gate, item, owner) is None


@pytest.mark.parametrize("change", ["young", "dead-child", "changed-child", "dead-supervisor",
                                   "changed-supervisor", "unknown-supervisor", "unknown-child",
                                   "no-hint", "not-running"])
def test_inflight_observation_requires_live_bound_owners_and_declared_hint(tmp_path, change):
    gate, item, owner = fixture(tmp_path)
    if change == "young":
        owner["command_started_at"] = time.time()
    elif change == "dead-child":
        owner["child_pid"] = 99999999
    elif change == "changed-child":
        owner["child_start"] = "different"
    elif change == "dead-supervisor":
        owner["supervisor_pid"] = 99999999
    elif change == "changed-supervisor":
        owner["process_start"] = "different"
    elif change == "unknown-supervisor":
        owner["process_start"] = "unknown"
    elif change == "unknown-child":
        owner["child_start"] = "unknown"
    elif change == "no-hint":
        item.pop("admission_resources")
    else:
        owner["status"] = "finishing"
    assert queue.inflight_disk_wait(gate, item, owner) is None


def test_non_disk_pressure_does_not_trigger_reclamation(tmp_path):
    gate, item, owner = fixture(tmp_path)
    gate.probe = lambda: {"free_disk_bytes": 10**12, "available_memory_bytes": 0,
                         "load_1m": 0, "host_cpus": 8}
    assert queue.inflight_disk_wait(gate, item, owner) is None


@pytest.mark.parametrize("status", ["running", "finishing", "succeeded", "failed"])
def test_queue_owned_monitor_only_observes_its_admission_wait(tmp_path, status):
    gate, item, owner = fixture(tmp_path, child=False)
    owner["status"] = status
    assert queue.inflight_disk_wait(gate, item, owner) is None


def test_production_startup_grace_remains_ten_seconds():
    assert queue.INFLIGHT_GRACE_SECONDS == 10
