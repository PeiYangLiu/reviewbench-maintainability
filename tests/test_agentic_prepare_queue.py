import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_agentic_prepare_queue as queue
from reviewbench_maintainability.agentic.contracts import ContractError, file_hash


def setup(tmp_path):
    config = {
        "protocol": "reviewbench-agentic-exec/v1",
        **{name: str(tmp_path / name) for name in ("tasks", "oracles", "repo_cache", "asset_root", "out_dir")},
        "execution": {"gate_dir": str(tmp_path / "gate"), "resource_limits": {
            "tasks": 8, "heavy_tasks": 2, "cpus": 8, "memory_bytes": 8 * 1024**3,
            "scratch_bytes": 8 * 1024**3, "min_free_disk_bytes": 0,
            "min_available_memory_bytes": 0, "max_load_fraction": 1}},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    (tmp_path / "repo_cache").mkdir()
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import json,sys,time\nfrom pathlib import Path\n"
        "p=Path(sys.argv[1]);started=time.time();time.sleep(float(sys.argv[2]))\n"
        "p.write_text(json.dumps({'started':started,'finished':time.time()}))\n"
    )
    return path, worker


def job(tmp_path, worker, name, seconds=.1, pool="prepare", needs=()):
    output = tmp_path / (name + ".json")
    return {"id": name, "pool": pool, "command": [sys.executable, str(worker), str(output), str(seconds)],
            "cwd": str(tmp_path), "timeout_seconds": 15, "needs": list(needs),
            "resource_owner": "queue", "resources": {"cpus": 1, "memory_bytes": 1024**2, "scratch_bytes": 1024**2},
            "inputs": [{"path": str(worker), "sha256": file_hash(worker)}], "outputs": [str(output)]}


def plan(jobs):
    return {"schema_version": queue.SCHEMA, "scope": {"expected_tasks": 402, "inputs": []},
            "workers": dict(queue.DEFAULT_WORKERS), "jobs": jobs}


def run(tmp_path, config, document, output=None):
    path = tmp_path / "queue.json"
    path.write_text(json.dumps(document))
    command = [sys.executable, "-B", queue.__file__, "--plan", str(path), "--config", str(config),
               "--out-dir", str(output or tmp_path / "queue"), "--poll-seconds", ".03"]
    return subprocess.run(command, capture_output=True, text=True, timeout=30)


def test_two_preparers_overlap_and_archive_does_not_hold_them(tmp_path):
    config, worker = setup(tmp_path)
    document = plan([job(tmp_path, worker, "archive", 2, "archive"),
                     *[job(tmp_path, worker, f"prepare{i}", .3) for i in range(4)]])
    result = run(tmp_path, config, document)
    assert result.returncode == 0, result.stderr
    rows = [json.loads((tmp_path / f"prepare{i}.json").read_text()) for i in range(4)]
    archive = json.loads((tmp_path / "archive.json").read_text())
    assert max(r["started"] for r in rows[:2]) < min(r["finished"] for r in rows[:2])
    assert max(r["finished"] for r in rows) < archive["finished"]
    for instant in [r[k] for r in rows for k in ("started", "finished")]:
        assert sum(r["started"] <= instant < r["finished"] for r in rows) <= 2


def test_restart_reuses_completed_jobs_and_appended_work(tmp_path):
    config, worker = setup(tmp_path)
    first = job(tmp_path, worker, "first")
    document = plan([first])
    assert run(tmp_path, config, document).returncode == 0
    data = (tmp_path / "first.json").read_bytes()
    document["jobs"].append(job(tmp_path, worker, "second", needs=["first"]))
    result = run(tmp_path, config, document)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "first.json").read_bytes() == data
    first_time = json.loads(data)["finished"]
    assert json.loads((tmp_path / "second.json").read_text())["started"] >= first_time


@pytest.mark.parametrize("mutation", ["cycle", "unknown_dependency", "too_many_workers", "changed_role", "zero_timeout"])
def test_invalid_queue_shapes_are_rejected(tmp_path, mutation):
    _, worker = setup(tmp_path)
    document = plan([job(tmp_path, worker, "a")])
    if mutation == "cycle":
        document["jobs"][0]["needs"] = ["a"]
    elif mutation == "unknown_dependency":
        document["jobs"][0]["needs"] = ["missing"]
    elif mutation == "too_many_workers":
        document["workers"]["prepare"] = 100
    elif mutation == "changed_role":
        document["jobs"][0]["resource_owner"] = "both"
    else:
        document["jobs"][0]["timeout_seconds"] = 0
    with pytest.raises(ContractError):
        queue.validate(document)


def test_changed_successful_output_is_not_silently_reexecuted(tmp_path):
    config, worker = setup(tmp_path)
    document = plan([job(tmp_path, worker, "only")])
    assert run(tmp_path, config, document).returncode == 0
    (tmp_path / "only.json").write_text("changed")
    result = run(tmp_path, config, document)
    assert result.returncode != 0
    assert (tmp_path / "only.json").read_text() == "changed"


def test_changed_pinned_input_fails_without_launching_command(tmp_path):
    config, worker = setup(tmp_path)
    document = plan([job(tmp_path, worker, "only")])
    worker.write_text("raise Exception('never execute changed input')")
    result = run(tmp_path, config, document)
    assert result.returncode == 2, result.stderr
    assert not (tmp_path / "only.json").exists()
    record = json.loads(next((tmp_path / "queue/jobs").glob("*/result.json")).read_text())
    assert record["status"] == "failed" and "pinned" in record["error"]


def test_failed_dependency_blocks_only_its_dependents(tmp_path):
    config, worker = setup(tmp_path)
    failure = job(tmp_path, worker, "failed")
    failure["command"] = [sys.executable, "-c", "raise SystemExit(7)"]
    failure["outputs"] = []
    document = plan([failure, job(tmp_path, worker, "blocked", needs=["failed"]),
                     job(tmp_path, worker, "independent")])
    result = run(tmp_path, config, document)
    assert result.returncode == 2, result.stderr
    states = json.loads((tmp_path / "queue/status.json").read_text())["states"]
    assert states == {"failed": "failed", "blocked": "blocked", "independent": "succeeded"}


def test_live_supervisors_survive_controller_restart_without_duplicate_work(tmp_path):
    config, worker = setup(tmp_path)
    document = plan([job(tmp_path, worker, "first", 1.2), job(tmp_path, worker, "second", 1.2)])
    path = tmp_path / "queue.json"
    path.write_text(json.dumps(document))
    args = [sys.executable, "-B", queue.__file__, "--plan", str(path), "--config", str(config),
            "--out-dir", str(tmp_path / "queue"), "--poll-seconds", ".03"]
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + 10
    while len(list((tmp_path / "queue/jobs").glob("*/owner.json"))) != 2:
        assert process.poll() is None
        assert time.monotonic() < deadline
        time.sleep(.02)
    owners = [json.loads(p.read_text()) for p in (tmp_path / "queue/jobs").glob("*/owner.json")]
    process.terminate()
    process.communicate(timeout=5)
    resumed = subprocess.run(args, capture_output=True, text=True, timeout=20)
    assert resumed.returncode == 0, resumed.stderr
    results = [json.loads(p.read_text()) for p in (tmp_path / "queue/jobs").glob("*/result.json")]
    assert {r["supervisor_pid"] for r in results} == {r["supervisor_pid"] for r in owners}
    assert len(results) == 2


def test_nonzero_calibration_exit_requires_and_checks_a_ready_proof(tmp_path):
    config, worker = setup(tmp_path)
    item = job(tmp_path, worker, "calibration")
    proof = tmp_path / "proof.json"
    item["command"] = [sys.executable, "-c",
                       f"from pathlib import Path;Path({str(proof)!r}).write_text('{{\"status\":\"ready\"}}');raise SystemExit(2)"]
    item["outputs"] = [str(proof)]
    item["accepted_exit_statuses"] = [0, 2]
    item["success_when"] = [{"path": str(proof), "keys": ["status"], "equals": "ready"}]
    result = run(tmp_path, config, plan([item]))
    assert result.returncode == 0, result.stderr
    row = json.loads(next((tmp_path / "queue/jobs").glob("*/result.json")).read_text())
    assert row["status"] == "succeeded" and row["exit_status"] == 2


def test_accepting_failure_codes_without_output_proof_is_rejected(tmp_path):
    _, worker = setup(tmp_path)
    item = job(tmp_path, worker, "bad")
    item["accepted_exit_statuses"] = [0, 2]
    with pytest.raises(ContractError, match="nonzero"):
        queue.validate(plan([item]))


def test_false_success_proof_is_a_failure(tmp_path):
    config, worker = setup(tmp_path)
    item = job(tmp_path, worker, "bad")
    proof = tmp_path / "proof.json"
    proof.write_text('{"status":"failed"}')
    item["command"] = [sys.executable, "-c", "raise SystemExit(2)"]
    item["outputs"] = []
    item["accepted_exit_statuses"] = [0, 2]
    item["success_when"] = [{"path": str(proof), "keys": ["status"], "equals": "ready"}]
    assert run(tmp_path, config, plan([item])).returncode == 2
