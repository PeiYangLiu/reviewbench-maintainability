import copy
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_agentic_prepare_queue as queue
from reviewbench_maintainability.agentic.cli import load_config
from reviewbench_maintainability.agentic.contracts import ContractError
from reviewbench_maintainability.agentic.execution import ExecutionServices
from reviewbench_maintainability.agentic.resources import ResourceRequest
from test_agentic_prepare_queue import job, plan, run, setup


def launch(tmp_path, config, document):
    path = tmp_path / "queue.json"
    path.write_text(json.dumps(document))
    return subprocess.Popen(
        [sys.executable, "-B", queue.__file__, "--plan", str(path), "--config", str(config),
         "--out-dir", str(tmp_path / "queue"), "--poll-seconds", ".03"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def wait_for(path, process, seconds=8):
    deadline = time.monotonic() + seconds
    while not path.exists():
        assert process.poll() is None, process.communicate()
        assert time.monotonic() < deadline, f"did not create {path}"
        time.sleep(.02)


def test_resource_waiters_do_not_fill_prepare_pool(tmp_path):
    config, worker = setup(tmp_path)
    heavy = [job(tmp_path, worker, f"large{i}") for i in range(2)]
    for item in heavy:
        item["resources"]["cpus"] = 2
    small = job(tmp_path, worker, "small")
    gate = ExecutionServices(load_config(config)).gate
    process = None
    try:
        with gate.acquire(ResourceRequest("external-owner", cpus=7, memory_bytes=0)):
            process = launch(tmp_path, config, plan([*heavy, small]))
            wait_for(tmp_path / "small.json", process)
            status = json.loads((tmp_path / "queue/status.json").read_text())
            assert status["dispatch_policy"] == "resource-aware-backfill"
            assert set(status["waiting_for_resources"]) == {"large0", "large1"}
            assert all(row["reason"] == "cpus_reservation"
                       for row in status["waiting_for_resources"].values())
            assert status["active_by_pool"].get("prepare", 0) <= 2
            for item in heavy:
                directory = queue.folder(tmp_path / "queue", item)
                assert (directory / "dispatch-wait.json").is_file()
                assert not (directory / "job.json").exists()
                assert not (directory / "owner.json").exists()
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, (stdout, stderr)
        small_end = json.loads((tmp_path / "small.json").read_text())["finished"]
        assert all(json.loads((tmp_path / f"large{i}.json").read_text())["started"] > small_end
                   for i in range(2))
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.communicate(timeout=5)


def test_admission_probe_does_not_reserve_or_mutate_gate(tmp_path):
    config, _ = setup(tmp_path)
    gate = ExecutionServices(load_config(config)).gate
    with gate.acquire(ResourceRequest("external-owner", cpus=7, memory_bytes=0)):
        before = gate.state_path.read_bytes()
        assert queue.admission_wait(gate, ResourceRequest("fits", cpus=1, memory_bytes=0)) is None
        assert queue.admission_wait(gate, ResourceRequest("waits", cpus=2, memory_bytes=0))["reason"] == "cpus_reservation"
        assert gate.state_path.read_bytes() == before


def test_disk_floor_uses_existing_gate_rules(tmp_path):
    config, _ = setup(tmp_path)
    gate = ExecutionServices(load_config(config)).gate
    gate.probe = lambda: {"free_disk_bytes": 50, "available_memory_bytes": 10**12,
                         "load_1m": 0, "host_cpus": 8}
    assert queue.admission_wait(gate, ResourceRequest("big", memory_bytes=0, scratch_bytes=51))["reason"] == "host_disk_headroom"
    assert queue.admission_wait(gate, ResourceRequest("small", memory_bytes=0, scratch_bytes=49)) is None


def test_pre_dispatch_timeout_is_terminal_and_does_not_run_command(tmp_path):
    config, worker = setup(tmp_path)
    large = job(tmp_path, worker, "large")
    large["resources"]["cpus"] = 2
    large["admission_timeout_seconds"] = .2
    document = plan([large, job(tmp_path, worker, "dependent", needs=["large"]),
                     job(tmp_path, worker, "independent")])
    gate = ExecutionServices(load_config(config)).gate
    with gate.acquire(ResourceRequest("external-owner", cpus=7, memory_bytes=0)):
        result = run(tmp_path, config, document)
    assert result.returncode == 2, result.stderr
    directory = queue.folder(tmp_path / "queue", large)
    failure = json.loads((directory / "result.json").read_text())
    assert failure["admission_only"] and failure["timed_out"]
    assert failure["command_executed"] is False and failure["exit_status"] == 1
    assert not (directory / "owner.json").exists()
    assert not (tmp_path / "large.json").exists()
    assert not (tmp_path / "dependent.json").exists()
    assert (tmp_path / "independent.json").is_file()
    old_result = (directory / "result.json").read_bytes()
    assert run(tmp_path, config, document).returncode == 2
    assert (directory / "result.json").read_bytes() == old_result


def test_restart_preserves_first_dispatch_wait_and_original_deadline(tmp_path, monkeypatch):
    _, worker = setup(tmp_path)
    item = job(tmp_path, worker, "large")
    item["admission_timeout_seconds"] = 10
    root = tmp_path / "queue"
    monkeypatch.setattr(queue.time, "time", lambda: 100)
    assert queue.record_dispatch_wait(root, item, {"reason": "host_disk_headroom"}) is False
    monkeypatch.setattr(queue.time, "time", lambda: 109)
    assert queue.record_dispatch_wait(root, copy.deepcopy(item), {"reason": "host_disk_headroom"}) is False
    monkeypatch.setattr(queue.time, "time", lambda: 110)
    assert queue.record_dispatch_wait(root, copy.deepcopy(item), {"reason": "host_disk_headroom"}) is True
    record = json.loads((queue.folder(root, item) / "result.json").read_text())
    assert record["started_at"] == 100 and record["completed_at"] == 110
    assert record["command_executed"] is False


@pytest.mark.parametrize("resources", [
    {"cpus": True}, {"cpus": float("nan")}, {"cpus": 0}, {"memory_bytes": -1},
    {"scratch_bytes": .5}, {"heavy": 1}, {"name": "another-owner"},
])
def test_invalid_dispatch_resources_rejected(tmp_path, resources):
    _, worker = setup(tmp_path)
    item = job(tmp_path, worker, "invalid")
    item["resources"] = resources
    with pytest.raises(ContractError, match="dispatch"):
        queue.validate(plan([item]))


def test_child_hint_is_optional_and_cannot_double_reserve(tmp_path):
    _, worker = setup(tmp_path)
    item = job(tmp_path, worker, "child")
    item["resource_owner"] = "child"
    item.pop("resources")
    assert queue.dispatch_request(item) is None
    item["admission_resources"] = {"cpus": 2, "memory_bytes": 1024, "scratch_bytes": 2048}
    assert queue.dispatch_request(item).cpus == 2
    queue.validate(plan([item]))
    item["resource_owner"] = "queue"
    item["resources"] = {"cpus": 2}
    with pytest.raises(ContractError, match="already declare"):
        queue.validate(plan([item]))


def test_single_impossible_request_fails_instead_of_waiting_forever(tmp_path):
    config, _ = setup(tmp_path)
    gate = ExecutionServices(load_config(config)).gate
    with pytest.raises(ContractError, match="exceeds configured capacity"):
        queue.admission_wait(gate, ResourceRequest("oversized", cpus=9))


def test_completion_between_terminal_and_owner_reads_is_not_an_interrupted_job(tmp_path, monkeypatch):
    config, worker = setup(tmp_path)
    item = job(tmp_path, worker, "just_finished")
    document = plan([item])
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(document))
    root = tmp_path / "queue"
    directory = queue.folder(root, item)
    directory.mkdir(parents=True)
    (directory / "owner.json").write_text(json.dumps({"supervisor_pid": 123, "process_start": "456"}))
    observations = iter([None, {"status": "succeeded"}])
    monkeypatch.setattr(queue, "terminal", lambda *args: next(observations))
    monkeypatch.setattr(queue, "process_identity", lambda pid: None)
    assert queue.run_queue(path, config, root) == 0
    assert not (directory / "job.json").exists()
    assert not (tmp_path / "just_finished.json").exists()
