"""Pure source controls; fabricated test proofs are never runtime licenses."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from agentic_runtime_migration import public_gradle_identity as bridge
from reviewbench_maintainability.agentic.contracts import ContractError, RuntimeRecipe, canonical_hash
from run_agentic_progressive import proof_matches


def ref(path):
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


@pytest.fixture
def original():
    base = Path(os.environ["PUBLIC_GRADLE_PROFILE_ROOT"])
    profile = json.loads((base / "original-public-profile.json").read_text())
    binding = json.loads(Path(profile["original_input_bindings"]["path"]).read_text())
    task = json.loads(Path(binding["task"]["path"]).read_text())
    oracle = json.loads(Path(binding["oracle"]["path"]).read_text())
    recipe = RuntimeRecipe.from_dict(json.loads((base / "runtime-final-v2/runtime-recipes.jsonl").read_text()))
    return task, oracle, recipe, profile, binding


def fixture_proof(task, oracle, recipe):
    view, _ = bridge.execution_view(task, oracle, recipe)
    return {
        "status": "ready", "engine_fingerprint": bridge.ENGINE,
        "task_fingerprint": canonical_hash(task), "runtime_fingerprint": recipe.fingerprint,
        "oracle_fingerprint": canonical_hash(view), "not_a_runtime_license": True,
        "runs": [{"variant": variant, "repetition": n,
                  "tests": {"0::" + name: True for name in bridge.TESTS}}
                 for variant in ("start", "golden") for n in range(3)],
    }


def test_only_count_field_changes_and_execution_proof_keeps_its_identity(original):
    task, oracle, recipe, _, _ = original
    before, fingerprint = deepcopy(oracle), canonical_hash(oracle)
    view, certificate = bridge.execution_view(task, oracle, recipe)
    assert oracle == before and canonical_hash(oracle) == fingerprint
    assert {k for k in oracle if oracle[k] != view[k]} == {"pass_to_pass_tests"}
    assert view["pass_to_pass_tests"] == bridge.TESTS
    assert certificate["original_count_vector"] == [6] * 6
    assert len(certificate["original_logs"]) == 6
    raw = fixture_proof(task, oracle, recipe)
    saved = deepcopy(raw)
    annotated = bridge.annotate(raw, task, oracle, recipe)
    assert raw == saved and {k: v for k, v in annotated.items() if k != bridge.BRIDGE_KEY} == saved
    assert annotated["oracle_fingerprint"] != fingerprint
    assert proof_matches(annotated, task, oracle, recipe, bridge.ENGINE)
    assert not proof_matches(raw, task, oracle, recipe, bridge.ENGINE)


@pytest.mark.parametrize("mutation", [
    "status", "engine", "raw-retag", "task", "runtime", "certificate-original",
    "certificate-execution", "certificate-count", "certificate-test", "missing-slot",
    "reordered-slot", "boolean-repetition", "missing-test", "extra-test", "integer-pass",
    "failed-test", "foreign-private-bridge", "missing-certificate",
])
def test_incomplete_forged_or_retagged_proof_is_never_admitted(original, mutation):
    task, oracle, recipe, _, _ = original
    proof = bridge.annotate(fixture_proof(task, oracle, recipe), task, oracle, recipe)
    if mutation == "status":
        proof["status"] = "blocked"
    elif mutation == "engine":
        proof["engine_fingerprint"] = "0" * 64
    elif mutation == "raw-retag":
        proof["oracle_fingerprint"] = canonical_hash(oracle)
    elif mutation in {"task", "runtime"}:
        proof[mutation + "_fingerprint"] = "0" * 64
    elif mutation.startswith("certificate-"):
        field = {"certificate-original": "original_oracle_fingerprint",
                 "certificate-execution": "execution_oracle_fingerprint",
                 "certificate-count": "original_count_vector",
                 "certificate-test": "execution_test_ids"}[mutation]
        proof[bridge.BRIDGE_KEY][field] = "changed"
    elif mutation == "missing-slot":
        proof["runs"].pop()
    elif mutation == "reordered-slot":
        proof["runs"].reverse()
    elif mutation == "boolean-repetition":
        proof["runs"][0]["repetition"] = False
    elif mutation == "missing-test":
        proof["runs"][0]["tests"].pop(next(iter(proof["runs"][0]["tests"])))
    elif mutation == "extra-test":
        proof["runs"][0]["tests"]["0::extra"] = True
    elif mutation in {"integer-pass", "failed-test"}:
        proof["runs"][0]["tests"][next(iter(proof["runs"][0]["tests"]))] = 1 if mutation == "integer-pass" else False
    elif mutation == "foreign-private-bridge":
        proof["original_oracle_identity_bridge"] = {}
    else:
        proof.pop(bridge.BRIDGE_KEY)
    assert not bridge.proof_identity_matches(proof, task, oracle, recipe)
    assert not proof_matches(proof, task, oracle, recipe, bridge.ENGINE)


@pytest.mark.parametrize("mutation", [
    "schema", "scope", "id", "count-type", "gradle", "source-path", "source-hash",
    "test-list", "command", "reference", "binding-task", "binding-oracle",
    "missing-log", "log-count", "log-order", "log-copy-hash", "support-image",
])
def test_original_profile_and_all_six_log_bindings_are_required(original, tmp_path, mutation):
    task, oracle, recipe, source, original_binding = original
    profile, binding = deepcopy(source), deepcopy(original_binding)
    if mutation == "schema":
        profile["schema_version"] = "other"
    elif mutation == "scope":
        profile["full_scope"] = 1
    elif mutation == "id":
        profile["id"] = "other"
    elif mutation == "count-type":
        profile["original_count"] = 6.0
    elif mutation == "gradle":
        profile["original_gradle_version"] = "8.7"
    elif mutation == "source-path":
        profile["original_test_source_path"] = "other.java"
    elif mutation == "source-hash":
        profile["original_test_source"]["sha256"] = "0" * 64
    elif mutation == "test-list":
        profile["expected_tests"] = profile["expected_tests"][:-1]
    elif mutation == "command":
        profile["original_command"] += " && true"
    elif mutation == "reference":
        profile["original_golden_patch"]["sha256"] = "0" * 64
    elif mutation in {"binding-task", "binding-oracle"}:
        binding[mutation.split("-")[1] + "_fingerprint"] = "0" * 64
    elif mutation == "missing-log":
        binding["original_logs"].pop()
    elif mutation == "log-count":
        binding["original_logs"][0]["original_test_count"] = 5
    elif mutation == "log-order":
        binding["original_logs"].reverse()
    elif mutation == "log-copy-hash":
        binding["original_logs"][0]["copy"]["sha256"] = "0" * 64
    else:
        support = json.loads(Path(profile["source_support"]["path"]).read_text())
        support["image"] = "sha256:" + "0" * 64
        path = tmp_path / "support.json"
        path.write_text(json.dumps(support))
        profile["source_support"] = ref(path)
    path = tmp_path / "binding.json"
    path.write_text(json.dumps(binding))
    profile["original_input_bindings"] = ref(path)
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile))
    recipe.grading["original_public_gradle_profile"] = ref(path)
    with pytest.raises(ContractError):
        bridge.execution_view(task, oracle, recipe)


@pytest.mark.parametrize("mutation", ["raw-count", "raw-other-field", "foreign-task",
                                    "grading-command", "grading-parser", "unprotected-test"])
def test_source_or_execution_contract_changes_are_rejected(original, mutation):
    task, oracle, recipe, _, _ = original
    if mutation == "raw-count":
        oracle["pass_to_pass_tests"][0] = 5
    elif mutation == "raw-other-field":
        oracle["new_field"] = True
    elif mutation == "foreign-task":
        task["id"] = "another-task"
    elif mutation == "grading-command":
        recipe.grading["commands"][0]["command"] = "true"
    elif mutation == "grading-parser":
        recipe.grading["commands"][0]["parser"] = "junit"
    else:
        recipe.grading["immutable_paths"].remove(bridge.TEST_PATH)
    with pytest.raises(ContractError):
        bridge.execution_view(task, oracle, recipe)


def test_annotation_does_not_promote_failed_execution(original):
    task, oracle, recipe, _, _ = original
    proof = fixture_proof(task, oracle, recipe)
    proof["status"] = "blocked"
    proof["runs"][0]["tests"]["0::" + bridge.TESTS[0]] = False
    proof["error"] = "actual test failure"
    annotated = bridge.annotate(proof, task, oracle, recipe)
    assert annotated["status"] == "blocked" and annotated["error"] == proof["error"]
    assert not proof_matches(annotated, task, oracle, recipe, bridge.ENGINE)
