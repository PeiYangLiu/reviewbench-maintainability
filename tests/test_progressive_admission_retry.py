"""Controller-only fixtures: real ResourceGate + watch loop; no external model calls."""
from contextlib import contextmanager
from dataclasses import asdict, replace
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from reviewbench_maintainability.agentic import PROTOCOL
from reviewbench_maintainability.agentic.contracts import (
    ContractError, InfrastructureError, atomic_json, canonical_hash, component, file_hash, write_jsonl,
)
from reviewbench_maintainability.agentic.resources import GiB, ResourceGate, ResourceLimits, ResourceRequest
from test_agentic_exec import recipe_for


def load_controller(path):
    spec = importlib.util.spec_from_file_location("admission_retry_target", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULE = load_controller(Path(__file__).resolve().parents[1] / "scripts/run_agentic_progressive.py")
TIMEOUT = "InfrastructureError: resource admission timed out: heavy_slots"


class StopFixtureWatch(Exception):
    pass


def exercise_watch(module, root, *, failures=1, seed=None, artifacts=None,
                   admission_only=False, retry_infra=True, watch=True,
                   changed_runtime=False, episode_error=False, gate_exit_error=False,
                   verification_recovery=False):
    """Drive the unmodified main() scheduler, preserving its real 402-row report.

    A held local gate lease forces the requested number of real admission
    timeouts. The lease is released normally; the next attempt has capacity.
    Only run_episode is a deterministic counting fixture, not a benchmark answer.
    """
    root.mkdir(parents=True, exist_ok=True)
    task = {"id": "fixture-task", "repo": "fixture/repository", "start_sha": "0" * 40,
            "task_instruction": "fixture", "locality": {"path": "main.py", "line": 1}}
    tasks = {task["id"]: task, **{f"not-ready-{i}": {**task, "id": f"not-ready-{i}"} for i in range(401)}}
    recipe = replace(recipe_for(task), lane="native")
    if verification_recovery:
        recipe = replace(recipe, grading={
            "multifile_build": True, "immutable_paths": [],
            "commands": [{"command": "original JVM fixture", "parser": "json",
                          "report_path": ".oracle/result.json", "expected_tests": ["Fixed#case"]}],
        })
    original_recipe = recipe
    if changed_runtime:
        recipe = replace(recipe, build_scratch_mb=recipe.build_scratch_mb + 1)
    oracle = {"id": task["id"]}
    output, package = root / "output", root / "package"
    config = {"protocol": PROTOCOL, "tasks": str(root / "tasks.jsonl"), "oracles": str(root / "oracles.jsonl"),
              "repo_cache": str(root / "repos"), "asset_root": str(root / "assets"),
              "models": {"solver": {"model_name": "fixture-no-network"}}, "judge": {}}
    write_jsonl(Path(config["tasks"]), list(tasks.values()))
    write_jsonl(Path(config["oracles"]), [oracle])
    write_jsonl(package / "runtime-recipes.jsonl", [asdict(recipe)])
    proof_path = package / "execution/preparation/verified" / component(task["id"]) / "verification.json"
    atomic_json(proof_path, {"status": "ready", "task_fingerprint": canonical_hash(task),
                            "oracle_fingerprint": canonical_hash(oracle),
                            "runtime_fingerprint": recipe.fingerprint, "engine_fingerprint": "fixture-engine"})
    pair_root = output / "pairs" / component("solver") / component(task["id"])
    pair_file = pair_root / "pair-result.json"
    events = {"admission_attempts": 0, "first_generation_calls": 0, "started_before_admission": [],
              "idle_polls": 0, "clock": 1000.0}
    gate = ResourceGate(root / "gate", root / "scratch",
                        ResourceLimits(tasks=2, heavy_tasks=1, cpus=2, memory_bytes=GiB,
                                       scratch_bytes=GiB, min_available_memory_bytes=0, min_free_disk_bytes=0),
                        probe=lambda: {"free_disk_bytes": 8 * GiB, "available_memory_bytes": 8 * GiB,
                                       "load_1m": 0, "host_cpus": 2})

    class Services:
        cache = None

        def __init__(self, _config):
            self.gate = gate

        def source_bytes(self, *args):
            return 0

        @contextmanager
        def job(self, _task, _recipe, _repository, phase, **kwargs):
            assert phase == "progressive-run:solver", "unexpected verification/measurement repetition"
            events["admission_attempts"] += 1
            events["started_before_admission"].append(json.loads(pair_file.read_text()).get("generation_started"))
            request = ResourceRequest(phase, cpus=1, memory_bytes=0, scratch_bytes=0, heavy=True)
            if events["admission_attempts"] <= failures:
                with gate.acquire(ResourceRequest("capacity-holder", cpus=1, memory_bytes=0,
                                                   scratch_bytes=0, heavy=True), timeout=0):
                    with gate.acquire(request, timeout=0):
                        pytest.fail("a second heavy lease exceeded the real gate limit")
            else:
                with gate.acquire(request, timeout=0) as lease:
                    yield lease
                    if gate_exit_error:
                        raise InfrastructureError("resource admission timed out: heavy_slots")

    def generate(_task, _recipe, settings, repository, episode, asset_root, **kwargs):
        assert json.loads(pair_file.read_text())["generation_started"] is True
        events["first_generation_calls"] += 1
        episode.mkdir(parents=True, exist_ok=True)
        atomic_json(episode / "trajectory.json", {"pending_model_request": episode_error, "messages": []})
        if episode_error:
            raise InfrastructureError("resource admission timed out: heavy_slots")
        generation = {"id": task["id"], "model": "solver", "protocol": PROTOCOL,
                      "task_fingerprint": canonical_hash(task), "runtime_fingerprint": recipe.fingerprint,
                      "engine_fingerprint": "fixture-engine", "status": "generation_failed"}
        atomic_json(episode / "result.json", generation)
        return generation

    def sleep(seconds):
        events["clock"] += max(seconds, 30)
        events["idle_polls"] += 1
        if events["idle_polls"] >= 5:
            raise StopFixtureWatch()

    argv = ["run_agentic_progressive.py", "--config", str(root / "unused-config.yaml"),
            "--package", str(package), "--out-dir", str(output), "--model", "solver",
            "--workers", "2", "--heavy-workers", "1", "--poll-seconds", "0.001"]
    if watch:
        argv.append("--watch")
    if retry_infra:
        argv.append("--retry-infra")
    if admission_only:
        argv.append("--retry-admission-only")
    original_bytes = None
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(module, "load_config", lambda _: config)
        patch.setattr(module, "engine_identity", lambda: {"fingerprint": "fixture-engine"})
        patch.setattr(module, "ExecutionServices", Services)
        patch.setattr(module, "run_episode", generate)
        patch.setattr(module, "time", SimpleNamespace(time=lambda: events["clock"], sleep=sleep))
        patch.setattr(sys, "argv", argv)
        if seed is not None:
            manifest = module.experiment_manifest({**config, "out_dir": str(output)}, tasks, ["solver"])
            row = {"id": task["id"], "model": "solver", "status": "incomplete",
                   "runtime_fingerprint": original_recipe.fingerprint,
                   "experiment_fingerprint": canonical_hash(manifest),
                   "generation_started": True, "error": TIMEOUT, **seed}
            for name in ("generation", "correctness", "judgment"):
                if name in row:
                    row[name] = {"id": task["id"], "model": "solver", "protocol": PROTOCOL,
                                 "task_fingerprint": canonical_hash(task), **row[name]}
            atomic_json(pair_file, row)
            original_bytes = pair_file.read_bytes()
            write_jsonl(output / "fixed-runtime-recipes.jsonl", [asdict(original_recipe)])
        if verification_recovery:
            from test_agentic_calibration_continuation import make_case, compose
            case = make_case(root / "verification-evidence", task, oracle, recipe)
            atomic_json(proof_path, compose(case))
            current = output / "preparation/verified" / component(task["id"]) / "verification.json"
            current.parent.mkdir(parents=True, exist_ok=True)
            current.write_bytes((case["old_dir"] / "verification.json").read_bytes())
        for name, content in (artifacts or {}).items():
            path = pair_root / name
            if content is None:
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
        try:
            exit_status = module.main()
        except StopFixtureWatch:
            exit_status = None
    row = json.loads(pair_file.read_text())
    report = json.loads((output / "report.json").read_text())
    assert report["expected_pairs"] == report["task_count"] == 402
    assert gate.status()["active"]["tasks"] == 0
    return {"events": events, "row": row, "report": report, "output": output,
            "pair_file": pair_file, "original_bytes": original_bytes, "exit_status": exit_status}


def test_watch_retries_capacity_timeout_without_restarting(tmp_path):
    result = exercise_watch(MODULE, tmp_path)
    assert result["events"]["admission_attempts"] == 2
    assert result["events"]["first_generation_calls"] == 1
    assert result["events"]["started_before_admission"] == [False, False]
    assert result["report"]["measured_generations"] == 1
    assert result["row"]["generation_admission_status"] == "admitted"
    history = result["row"]["admission_failure_history"]
    assert len(history) == 1
    old = json.loads(Path(history[0]["path"]).read_text())
    assert old["error"] == TIMEOUT and old["generation_started"] is False
    assert file_hash(Path(history[0]["path"])) == history[0]["sha256"]


def test_multiple_timeouts_keep_history_and_call_first_generation_once(tmp_path):
    result = exercise_watch(MODULE, tmp_path, failures=2)
    assert result["events"]["admission_attempts"] == 3
    assert result["events"]["first_generation_calls"] == 1
    assert len(result["row"]["admission_failure_history"]) == 2
    assert result["events"]["started_before_admission"] == [False, False, False]


@pytest.mark.parametrize("options", [{"watch": False}, {"retry_infra": False}])
def test_no_same_process_retry_without_both_watch_and_retry_infra(tmp_path, options):
    result = exercise_watch(MODULE, tmp_path, **options)
    assert result["events"]["admission_attempts"] == 1
    assert result["events"]["first_generation_calls"] == 0
    assert result["row"]["generation_started"] is False


def test_legacy_false_positive_started_marker_is_archived_before_first_call(tmp_path):
    result = exercise_watch(MODULE, tmp_path, seed={}, failures=0, admission_only=True)
    assert result["events"]["admission_attempts"] == result["events"]["first_generation_calls"] == 1
    history = result["row"]["admission_failure_history"]
    assert len(history) == 1
    assert Path(history[0]["path"]).read_bytes() == result["original_bytes"]
    assert result["events"]["started_before_admission"] == [False]


@pytest.mark.parametrize("seed", [
    {"generation": {"status": "generation_failed"}},
    {"generation": {"status": "infra_error", "usage": {"model_calls": 1}}},
    {"generation": {"status": "infra_error", "usage": {}}},
    {"generation": {"status": "generated", "apply_ok": True, "patch_sha256": "a" * 64}},
    {"pending_model_request": True},
    {"answer_recovery_required": True},
    {"previous_infrastructure_attempt": "episode-attestation/old/result.json"},
    {"error": "ContractError: golden fixed PASS_TO_PASS replay failed", "generation_started": False},
])
def test_admission_only_restart_preserves_all_other_observed_rows(tmp_path, seed):
    result = exercise_watch(MODULE, tmp_path, seed=seed, failures=0, admission_only=True)
    assert result["events"]["admission_attempts"] == result["events"]["first_generation_calls"] == 0
    assert result["pair_file"].read_bytes() == result["original_bytes"]


def test_admission_only_restart_preserves_completed_negative_votes(tmp_path):
    result = exercise_watch(MODULE, tmp_path, failures=0, admission_only=True, seed={
        "generation": {"status": "generated", "apply_ok": True, "patch_sha256": "a" * 64},
        "correctness": {"status": "pass", "scope_ok": True, "correctness_ok": True,
                        "oracle_replayed": True, "patch_sha256": "a" * 64},
        "judgment": {"status": "completed", "maintainability_pass": False, "patch_sha256": "a" * 64,
                     "panel": [{"judge_model": name, "passed": passed}
                               for name, passed in [("j1", True), ("j2", False), ("j3", True)]]},
    })
    assert result["events"]["first_generation_calls"] == result["events"]["admission_attempts"] == 0
    assert result["pair_file"].read_bytes() == result["original_bytes"]
    assert result["report"]["leaderboard"]["solver"]["votes"] == 2


@pytest.mark.parametrize("name,content", [
    ("episode", None),
    ("episode/trajectory.json", '{"pending_model_request":true}'),
    ("episode/result.json", '{"status":"infra_error"}'),
    ("episode/retry-001/trajectory.json", '{"inflight":true}'),
    ("episode-attestation-old/trajectory.json", '{"messages":[]}'),
    ("candidate.patch", "a retained answer"),
    (".pair-result.json.interrupted.tmp", "uncertain write"),
])
def test_episode_or_other_artifacts_block_admission_only_retry(tmp_path, name, content):
    result = exercise_watch(MODULE, tmp_path, seed={}, failures=0, admission_only=True,
                            artifacts={name: content})
    assert result["events"]["first_generation_calls"] == result["events"]["admission_attempts"] == 0
    assert result["pair_file"].read_bytes() == result["original_bytes"]


def test_different_runtime_cannot_use_the_admission_only_retry(tmp_path):
    result = exercise_watch(MODULE, tmp_path, seed={}, failures=0,
                            admission_only=True, changed_runtime=True)
    assert result["events"]["admission_attempts"] == result["events"]["first_generation_calls"] == 0
    assert result["pair_file"].read_bytes() == result["original_bytes"]


@pytest.mark.parametrize("option", ["episode_error", "gate_exit_error"])
def test_identical_timeout_after_gate_entry_is_not_a_pregeneration_retry(tmp_path, option):
    result = exercise_watch(MODULE, tmp_path, failures=0, **{option: True})
    assert result["events"]["admission_attempts"] == result["events"]["first_generation_calls"] == 1
    assert result["row"]["generation_admission_status"] == "admitted"
    assert result["row"]["generation_started"] is True
    assert "admission_failure_history" not in result["row"]


@pytest.mark.parametrize("failed_at", [-1, "yesterday", float("nan"), float("inf")])
def test_invalid_retry_timestamp_fails_closed(failed_at):
    with pytest.raises(ContractError, match="timestamp"):
        MODULE.admission_retry_at({"generation_admission_failure_at": failed_at}, 30)


def test_retry_backoff_is_bounded_and_does_not_charge_agent_budget():
    assert MODULE.admission_retry_at({}, 30) == 0
    assert MODULE.admission_retry_at({"generation_admission_failure_at": 100}, 30) == 130
    assert MODULE.admission_retry_at({"generation_admission_failure_at": 100,
                                     "admission_failure_history": [{}, {}]}, 30) == 220
    assert MODULE.admission_retry_at({"generation_admission_failure_at": 100,
                                     "admission_failure_history": [{}] * 100}, 30) == 400


def admission_row(root):
    row = {"id": "fixture-task", "model": "solver", "status": "incomplete",
           "runtime_fingerprint": "runtime", "experiment_fingerprint": "experiment",
           "generation_started": True, "error": TIMEOUT}
    atomic_json(root / "pair-result.json", row)
    return row


@pytest.mark.parametrize("extra", [
    {"generation": None}, {"correctness": {}}, {"judgment": {}}, {"trace_file": "elsewhere"},
    {"generation_admission_status": "waiting"},
    {"generation_admission_status": "admitted"},
    {"generation_admission_status": "timed_out", "generation_started": True},
])
def test_retry_proof_rejects_ambiguous_fields(tmp_path, extra):
    row = {**admission_row(tmp_path), **extra}
    atomic_json(tmp_path / "pair-result.json", row)
    assert not MODULE.generation_admission_retryable(row, tmp_path, "runtime")


def test_retry_proof_rechecks_exact_row_and_symlinks(tmp_path):
    root = tmp_path / "pair"
    row = admission_row(root)
    assert MODULE.generation_admission_retryable(row, root, "runtime")
    assert not MODULE.generation_admission_retryable(row, root, "different-runtime")
    atomic_json(root / "pair-result.json", {**row, "generation_started": False})
    assert not MODULE.generation_admission_retryable(row, root, "runtime")
    original = tmp_path / "elsewhere.json"
    (root / "pair-result.json").replace(original)
    (root / "pair-result.json").symlink_to(original)
    assert not MODULE.generation_admission_retryable(row, root, "runtime")
    link = tmp_path / "alias"
    link.symlink_to(root, target_is_directory=True)
    assert not MODULE.generation_admission_retryable(row, link, "runtime")


def test_history_hash_guard_preserves_old_row_on_corruption(tmp_path):
    root = tmp_path / "pairs" / component("solver") / component("fixture-task")
    row = admission_row(root)
    history = MODULE.preserve_admission_failure(row, root, tmp_path)
    assert MODULE.preserve_admission_failure(row, root, tmp_path) == history
    assert Path(history[0]["path"]).read_bytes() == (root / "pair-result.json").read_bytes()
    row = {**row, "admission_failure_history": history}
    atomic_json(root / "pair-result.json", row)
    before = (root / "pair-result.json").read_bytes()
    Path(history[0]["path"]).write_text("changed evidence")
    with pytest.raises(ContractError, match="evidence is missing or changed"):
        MODULE.preserve_admission_failure(row, root, tmp_path)
    assert (root / "pair-result.json").read_bytes() == before


@pytest.mark.parametrize("args", [
    ["--poll-seconds", "0"], ["--poll-seconds", "-1"], ["--poll-seconds", "nan"],
    ["--poll-seconds", "inf"], ["--retry-admission-only"],
    ["--retry-admission-only", "--watch"], ["--retry-admission-only", "--retry-infra"],
])
def test_invalid_controller_arguments_fail_before_execution(monkeypatch, args):
    monkeypatch.setattr(sys, "argv", ["controller", "--config", "unused", "--out-dir", "unused",
                                    "--package", "unused", "--model", "solver", *args])
    with pytest.raises(ContractError):
        MODULE.main()
