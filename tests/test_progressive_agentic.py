from dataclasses import asdict
import importlib.util
import json
from pathlib import Path
import sys
import pytest

from reviewbench_maintainability.agentic.contracts import (
    ContractError, atomic_json, canonical_hash, component, file_hash, write_jsonl,
)
from reviewbench_maintainability.agentic.reporting import make_report
from test_agentic_exec import repository, recipe_for

PATH = Path(__file__).resolve().parents[1]/"scripts/run_agentic_progressive.py"
SPEC = importlib.util.spec_from_file_location("progressive_test_target", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_progressive_report_does_not_shrink_denominator(repository):
    _, task = repository
    tasks = {str(i): {**task, "id": str(i)} for i in range(402)}
    rows, report = make_report(tasks, ["solver"], [], [], [])
    assert len(rows) == 402
    assert report["task_count"] == report["leaderboard"]["solver"]["tasks"] == 402
    assert report["complete"] is False
    assert report["leaderboard"]["solver"]["resolved_rate"] is None


def test_successful_old_engine_proof_requires_revalidation(repository):
    _, task = repository
    recipe = recipe_for(task)
    oracle = {"id": task["id"]}
    proof = {"status": "ready", "task_fingerprint": canonical_hash(task),
             "oracle_fingerprint": canonical_hash(oracle), "runtime_fingerprint": recipe.fingerprint,
             "engine_fingerprint": "old"}
    assert MODULE.proof_matches(proof, task, oracle, recipe)
    assert not MODULE.proof_matches(proof, task, oracle, recipe, "current")
    assert MODULE.proof_matches({**proof, "engine_fingerprint": "current"}, task, oracle, recipe, "current")


def test_pending_or_stale_runtime_never_enters_model_queue(repository, tmp_path):
    _, task = repository
    recipe = recipe_for(task)
    oracle = {"id": task["id"]}
    tasks, oracles = {task["id"]: task}, {task["id"]: oracle}
    root = tmp_path/"package"
    write_jsonl(root/"runtime-recipes.jsonl", [asdict(recipe)])
    assert MODULE.package_candidates([root], tasks, oracles) == {}
    proof_file = root/"execution/preparation/verified"/component(task["id"])/"verification.json"
    proof = {"status": "ready", "task_fingerprint": canonical_hash(task),
             "oracle_fingerprint": canonical_hash(oracle), "runtime_fingerprint": recipe.fingerprint,
             "engine_fingerprint": "current"}
    atomic_json(proof_file, {**proof, "status": "blocked"})
    assert MODULE.package_candidates([root], tasks, oracles) == {}
    atomic_json(proof_file, {**proof, "oracle_fingerprint": "stale"})
    assert MODULE.package_candidates([root], tasks, oracles) == {}
    atomic_json(proof_file, proof)
    assert set(MODULE.package_candidates([root], tasks, oracles)) == {task["id"]}


def test_experiment_manifest_preserves_scope_and_model_configuration(repository):
    _, task = repository
    tasks = {"a": {**task, "id": "a"}, "b": {**task, "id": "b"}}
    config = {"protocol": "reviewbench-agentic-exec/v1",
              "models": {"solver": {"model_name": "fixed-model"}}, "judge": {"models": ["j1","j2","j3"]}}
    manifest = MODULE.experiment_manifest(config, tasks, ["solver"])
    assert manifest["task_ids"] == ["a", "b"] and manifest["expected_pairs"] == 2
    changed = MODULE.experiment_manifest({**config, "models": {"solver": {"model_name": "another-model"}}},
                                         tasks, ["solver"])
    assert changed["behavior_fingerprint"] != manifest["behavior_fingerprint"]


def test_only_spelling_equivalent_attestation_alias_updates_are_compatible():
    import copy
    old = {"models": {"solver": {"model_name": "openai/gpt-5.4",
            "expected_response_models": ["gpt-5.4", "gpt-5-4-2026-03-05"]}}}
    new = copy.deepcopy(old)
    new["models"]["solver"]["expected_response_models"].append("gpt-5.4-2026-03-05")
    assert MODULE.format_equivalent_attestation_update(old, new, ["solver"])
    new["models"]["solver"]["expected_response_models"].append("gpt-5.4-2026-07-01")
    assert not MODULE.format_equivalent_attestation_update(old, new, ["solver"])


def test_grading_retry_preserves_model_results_and_completed_negative_votes():
    row = {"generation": {"status": "generated"}, "correctness": {"status": "fail"},
           "judgment": {"status": "completed", "maintainability_pass": False}}
    assert MODULE.pair_complete(row)
    assert not MODULE.pair_complete({**row, "judgment": {"status": "infra_error"}})
    assert MODULE.pair_complete({"generation": {"status": "generation_failed"}})


def test_completed_answer_with_host_serialization_failure_is_not_resampled(tmp_path):
    bindings = {"id": "task", "model": "solver", "task_fingerprint": "task-hash",
                "runtime_fingerprint": "runtime-hash", "engine_fingerprint": "engine-hash"}
    trace_path = tmp_path/"trajectory.json"
    trace = {"bindings": bindings, "messages": [{"role": "exit", "extra": {"exit_status": "Submitted"}}],
             "inflight": False, "pending_model_request": False}
    atomic_json(trace_path, trace)
    generation = {**bindings, "status": "infra_error", "trace_file": str(trace_path),
                  "trace_sha256": file_hash(trace_path)}
    assert MODULE.completed_answer_needs_host_recovery(generation)
    atomic_json(trace_path, {**trace, "messages": [{"role": "user", "content": "in progress"}]})
    with pytest.raises(ContractError, match="changed"):
        MODULE.completed_answer_needs_host_recovery(generation)
    generation["trace_sha256"] = file_hash(trace_path)
    assert not MODULE.completed_answer_needs_host_recovery(generation)


def test_bootstrap_failure_with_no_trajectory_is_not_a_completed_model_answer(tmp_path):
    row = {"status": "infra_error", "trace_file": str(tmp_path/"trajectory.json"),
           "error": "developer bootstrap failed", "usage": {}}
    assert not MODULE.completed_answer_needs_host_recovery(row)
    row["trace_sha256"] = "a"*64
    with pytest.raises(ContractError, match="missing or changed"):
        MODULE.completed_answer_needs_host_recovery(row)


def test_heavy_admission_cap_leaves_slots_for_ready_light_tasks():
    assert not MODULE.admission_slot_available(2, 2, 4, 2, True)
    assert MODULE.admission_slot_available(2, 2, 4, 2, False)
    assert MODULE.admission_slot_available(3, 1, 4, 2, True)
    assert not MODULE.admission_slot_available(4, 1, 4, 2, False)
