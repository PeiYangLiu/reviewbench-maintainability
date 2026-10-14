from copy import deepcopy
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from agentic_runtime_migration import legacy_oracle_identity as bridge
from reviewbench_maintainability.agentic.contracts import ContractError, RuntimeRecipe, atomic_json, canonical_hash
from reviewbench_maintainability.agentic.preparation import verify_recipe
from run_agentic_progressive import proof_matches


@pytest.fixture(params=["skript17", "reposense41"])
def original(request, external_artifact_root):
    directory = external_artifact_root("LEGACY_GRADLE_PROFILE_ROOT")
    profile = json.loads((directory / "profiles" / (request.param + ".json")).read_text())
    task = json.loads(Path(profile["case"]["task"]["path"]).read_text())
    oracle = json.loads(Path(profile["case"]["oracle"]["path"]).read_text())
    package = directory / ("runtime-" + request.param)
    recipe = RuntimeRecipe.from_dict(json.loads((package / "runtime-recipes.jsonl").read_text()))
    return task, oracle, recipe, package


def proof_fixture(task, oracle, recipe, status="ready"):
    expected = {"0::" + name: True for command in recipe.grading["commands"] for name in command["expected_tests"]}
    return {
        "id": task["id"], "status": status, "task_fingerprint": canonical_hash(task),
        "oracle_fingerprint": canonical_hash(oracle), "runtime_fingerprint": recipe.fingerprint,
        "engine_fingerprint": bridge.EXECUTION_ENGINE,
        "runs": [{"variant": variant, "repetition": repetition, "tests": dict(expected)}
                 for variant in ("start", "golden") for repetition in range(3)],
    }


def annotated_fixture(original):
    task, oracle, recipe, _ = original
    view, certificate = bridge.execution_view(task, oracle, recipe)
    proof = proof_fixture(task, view, recipe)
    proof[bridge.BRIDGE_KEY] = certificate
    return proof


def test_raw_field_and_every_other_oracle_field_remain_unchanged(original):
    task, oracle, recipe, _ = original
    before = deepcopy(oracle)
    view, certificate = bridge.execution_view(task, oracle, recipe)
    assert oracle == before and oracle["pass_to_pass_tests"] == [1] * 6
    assert {key for key in view if view[key] != oracle[key]} == {"pass_to_pass_tests"}
    assert view["pass_to_pass_tests"] == recipe.grading["commands"][0]["expected_tests"]
    assert certificate["original_oracle_fingerprint"] == canonical_hash(oracle)
    assert certificate["execution_oracle_fingerprint"] == canonical_hash(view)
    assert canonical_hash(view) != canonical_hash(oracle)


def test_frozen_verifier_reproduces_original_pre_execution_guard(original, tmp_path):
    task, oracle, recipe, package = original
    proof = verify_recipe(task, recipe, oracle, tmp_path / "unused-repository", package, tmp_path / "baseline",
                          environment_factory=lambda *a, **k: pytest.fail("original guard should run before any SUT"))
    assert proof["status"] == "blocked" and proof["runs"] == []
    assert proof["error"] == "ContractError: runtime PASS_TO_PASS identities differ from frozen Oracle"
    assert proof["oracle_fingerprint"] == canonical_hash(oracle)


def test_only_explicit_complete_translation_is_admitted(original):
    task, oracle, recipe, _ = original
    proof = annotated_fixture(original)
    assert proof_matches(proof, task, oracle, recipe, bridge.EXECUTION_ENGINE)
    assert bridge.proof_identity_matches(proof, task, oracle, recipe)


@pytest.mark.parametrize("mutation", [
    "missing_bridge", "raw_retag", "wrong_engine", "wrong_task", "wrong_runtime", "blocked",
    "missing_run", "duplicate_run", "wrong_order", "integer_result", "wrong_name", "failed_test",
    "certificate_scope", "certificate_raw", "certificate_view", "certificate_count",
    "certificate_boolean_count", "certificate_extra", "certificate_profile",
])
def test_forged_incomplete_or_ambiguous_proof_is_rejected(original, mutation):
    task, oracle, recipe, _ = original
    proof = annotated_fixture(original)
    certificate = proof[bridge.BRIDGE_KEY]
    if mutation == "missing_bridge":
        del proof[bridge.BRIDGE_KEY]
    elif mutation == "raw_retag":
        proof["oracle_fingerprint"] = canonical_hash(oracle)
    elif mutation == "wrong_engine":
        proof["engine_fingerprint"] = "0" * 64
    elif mutation == "wrong_task":
        proof["task_fingerprint"] = "0" * 64
    elif mutation == "wrong_runtime":
        proof["runtime_fingerprint"] = "0" * 64
    elif mutation == "blocked":
        proof["status"] = "blocked"
    elif mutation == "missing_run":
        proof["runs"].pop()
    elif mutation == "duplicate_run":
        proof["runs"][-1] = deepcopy(proof["runs"][0])
    elif mutation == "wrong_order":
        proof["runs"].reverse()
    elif mutation in {"integer_result", "failed_test"}:
        key = next(iter(proof["runs"][0]["tests"]))
        proof["runs"][0]["tests"][key] = 1 if mutation == "integer_result" else False
    elif mutation == "wrong_name":
        proof["runs"][0]["tests"] = {"0::invented#test": True}
    elif mutation == "certificate_scope":
        certificate["schema_version"] = "unbound"
    elif mutation == "certificate_raw":
        certificate["original_oracle_fingerprint"] = "0" * 64
    elif mutation == "certificate_view":
        certificate["execution_oracle_fingerprint"] = "0" * 64
    elif mutation == "certificate_count":
        certificate["original_count_vector"] = [2] * 6
    elif mutation == "certificate_boolean_count":
        certificate["original_count_vector"] = [True] * 6
    elif mutation == "certificate_extra":
        certificate["ignored_override"] = True
    elif mutation == "certificate_profile":
        certificate["source_profile"]["sha256"] = "0" * 64
    assert not proof_matches(proof, task, oracle, recipe, bridge.EXECUTION_ENGINE)


@pytest.mark.parametrize("value", [[True] * 6, [1] * 5, ["1"] * 6, [2] * 6, None])
def test_original_record_changes_are_not_normalized_away(original, value):
    task, oracle, recipe, _ = original
    changed = deepcopy(oracle)
    changed["pass_to_pass_tests"] = value
    with pytest.raises(ContractError):
        bridge.execution_view(task, changed, recipe)


def test_normal_oracle_admission_is_unchanged_and_rejects_foreign_bridge():
    task, oracle = {"id": "ordinary"}, {"pass_to_pass_tests": ["class#test"]}
    recipe = SimpleNamespace(fingerprint="runtime", grading={})
    proof = {"status": "ready", "task_fingerprint": canonical_hash(task),
             "oracle_fingerprint": canonical_hash(oracle), "runtime_fingerprint": "runtime",
             "engine_fingerprint": "engine"}
    assert proof_matches(proof, task, oracle, recipe, "engine")
    proof[bridge.BRIDGE_KEY] = {}
    assert not proof_matches(proof, task, oracle, recipe, "engine")


@pytest.mark.parametrize("status", ["ready", "blocked"])
def test_wrapper_retains_distinct_execution_proof_and_raw_oracle(original, tmp_path, status):
    task, oracle, recipe, package = original
    before = deepcopy(oracle)
    calls = []

    def verifier(actual_task, actual_recipe, view, repo, asset_root, output):
        calls.append(deepcopy(view))
        assert actual_task is task and actual_recipe is recipe and asset_root == package
        assert view["pass_to_pass_tests"] == recipe.grading["commands"][0]["expected_tests"]
        proof = proof_fixture(task, view, recipe, status)
        if status == "blocked":
            proof.update(error="preserved ordinary failure", runs=[])
        atomic_json(output / "verification.json", proof)
        return proof

    proof = bridge.verify_original_schema(
        task, recipe, oracle, tmp_path / "repo", package, tmp_path / "out", verify=verifier)
    assert oracle == before and len(calls) == 1
    raw_execution = json.loads((tmp_path / "out/execution-verification.json").read_text())
    assert bridge.BRIDGE_KEY not in raw_execution
    assert proof["oracle_fingerprint"] == raw_execution["oracle_fingerprint"] == canonical_hash(calls[0])
    assert proof["oracle_fingerprint"] != canonical_hash(oracle)
    assert proof["status"] == status
    if status == "blocked":
        assert proof["runs"] == [] and proof["error"] == "preserved ordinary failure"
    with pytest.raises(ContractError, match="append-only"):
        bridge.verify_original_schema(
            task, recipe, oracle, tmp_path / "repo", package, tmp_path / "out", verify=verifier)
    assert len(calls) == 1
