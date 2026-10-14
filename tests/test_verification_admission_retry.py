"""Drive the real 402-row scheduler through a zero-call verification recovery."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from reviewbench_maintainability.agentic.contracts import ContractError, file_hash
from test_progressive_admission_retry import MODULE, exercise_watch

ERROR = "ContractError: current-engine runtime verification failed: InfrastructureError: golden fixed PASS_TO_PASS replay failed"
SEED = {"error": ERROR, "generation_started": False}


def test_audited_original_verification_continuation_calls_first_generation_once(tmp_path):
    result = exercise_watch(MODULE, tmp_path, seed=SEED, failures=0,
                            admission_only=True, verification_recovery=True)
    assert result["events"]["first_generation_calls"] == 1
    assert result["events"]["admission_attempts"] == 1
    history = result["row"]["verification_failure_history"]
    assert len(history) == 1
    assert Path(history[0]["pair"]["path"]).read_bytes() == result["original_bytes"]
    original = json.loads(Path(history[0]["proof"]["path"]).read_text())
    assert original["status"] == "blocked" and not all(original["runs"][-1]["tests"].values())
    assert file_hash(Path(history[0]["proof"]["path"])) == history[0]["proof"]["sha256"]
    assert result["report"]["expected_pairs"] == 402


@pytest.mark.parametrize("options", [{"watch": False}, {"retry_infra": False}])
def test_verification_retry_requires_explicit_watch_and_retry_mode(tmp_path, options):
    with pytest.raises(ContractError, match="requires --watch --retry-infra"):
        exercise_watch(MODULE, tmp_path, seed=SEED, failures=0,
                       admission_only=True, verification_recovery=True, **options)


@pytest.mark.parametrize("artifact", ["episode", "episode/trajectory.json", "episode/result.json", "unexpected"])
def test_any_episode_or_unexpected_artifact_closes_zero_call_route(tmp_path, artifact):
    result = exercise_watch(
        MODULE, tmp_path, seed=SEED, failures=0, admission_only=True,
        verification_recovery=True, artifacts={artifact: None if artifact == "episode" else "{}"})
    assert result["events"]["first_generation_calls"] == 0


@pytest.mark.parametrize("change", [
    {"generation_started": True}, {"generation_started": 0},
    {"generation": {"status": "infra_error", "usage": {"model_calls": 0}}},
    {"correctness": {}}, {"judgment": {}},
    {"unknown": True}, {"error": "different failure"},
])
def test_previous_calls_measurements_unknown_fields_or_errors_are_never_retried(tmp_path, change):
    result = exercise_watch(MODULE, tmp_path, seed={**SEED, **change}, failures=0,
                            admission_only=True, verification_recovery=True)
    assert result["events"]["first_generation_calls"] == 0


def test_changed_runtime_is_not_a_verification_only_continuation(tmp_path):
    result = exercise_watch(MODULE, tmp_path, seed=SEED, failures=0, admission_only=True,
                            verification_recovery=True, changed_runtime=True)
    assert result["events"]["first_generation_calls"] == 0


def test_capacity_wait_after_verification_continuation_preserves_both_histories(tmp_path):
    result = exercise_watch(MODULE, tmp_path, seed=SEED, failures=1, admission_only=True,
                            verification_recovery=True)
    assert result["events"]["first_generation_calls"] == 1
    assert result["events"]["admission_attempts"] == 2
    assert len(result["row"]["verification_failure_history"]) == 1
    assert len(result["row"]["admission_failure_history"]) == 1
