"""Real original profiles; positive execution controls require actual six-run evidence."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from agentic_runtime_migration import solr_hibernate_identity as bridge
from reviewbench_maintainability.agentic.contracts import ContractError, RuntimeRecipe, canonical_hash
from run_agentic_progressive import proof_matches


def ref(path):
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


@pytest.fixture(params=["solr31", "hibernate35"])
def original(request):
    root = Path(os.environ["SOLR_HIBERNATE_PROFILE_ROOT"]) / request.param
    task = json.loads((root / "inputs/task.json").read_text())
    oracle = json.loads((root / "inputs/oracle.json").read_text())
    recipe = RuntimeRecipe.from_dict(json.loads((root / "runtime-final-v2/runtime-recipes.jsonl").read_text()))
    return root, task, oracle, recipe


def test_original_view_changes_only_historical_count_encoding(original):
    root, task, oracle, recipe = original
    before = deepcopy(oracle)
    view, certificate = bridge.execution_view(task, oracle, recipe)
    count = bridge.CASES[task["id"]]["count"]
    assert oracle == before and view["pass_to_pass"] == count
    assert {key for key in oracle if view[key] != oracle[key]} == {"pass_to_pass_tests"}
    assert certificate["original_count_vector"] == [count] * 6
    assert len(view["pass_to_pass_tests"]) == len(set(view["pass_to_pass_tests"])) == count
    assert len(certificate["original_logs"]) == 6
    assert certificate["original_assertions_changed"] is False
    assert certificate["original_reference_changed"] is False


@pytest.mark.parametrize("mutation", [
    "schema", "scope", "count", "count-type", "count-vector-type", "test-list", "test-type",
    "manifest", "declarations", "selector", "gradle", "jdk", "reference", "init", "agent",
    "protected-paths", "report-roots", "binding-task", "binding-oracle", "log-order",
    "log-count", "log-type", "log-copy", "missing-log",
])
def test_profile_or_historical_execution_mutations_fail(original, tmp_path, mutation):
    root, task, oracle, recipe = original
    profile = deepcopy(json.loads(Path(recipe.grading[bridge.PROFILE_KEY]["path"]).read_text()))
    binding = deepcopy(json.loads(Path(profile["original_input_bindings"]["path"]).read_text()))
    if mutation == "schema":
        profile["schema_version"] = "other"
    elif mutation == "scope":
        profile["full_scope"] = 1
    elif mutation == "count":
        profile["original_count"] -= 1
    elif mutation == "count-type":
        profile["original_count"] = float(profile["original_count"])
    elif mutation == "count-vector-type":
        profile["original_count_vector"][0] = float(profile["original_count_vector"][0])
    elif mutation == "test-list":
        profile["expected_tests"][0] = "foreign#test"
    elif mutation == "test-type":
        profile["expected_tests"][0] = 1
    elif mutation in {"manifest", "declarations", "reference", "init", "agent"}:
        key = {"manifest": "test_source_manifest", "declarations": "declarations",
               "reference": "original_golden_patch", "init": "retained_legacy_init",
               "agent": "jacoco_agent"}[mutation]
        profile[key]["sha256"] = "0" * 64
    elif mutation == "selector":
        profile["original_command"] += " || true"
    elif mutation == "gradle":
        profile["original_gradle_version"] = "9.0"
    elif mutation == "jdk":
        profile["original_jdk_major"] = 8
    elif mutation == "protected-paths":
        profile["immutable_paths"] = []
    elif mutation == "report-roots":
        profile["report_roots"] = []
    elif mutation in {"binding-task", "binding-oracle"}:
        binding[mutation.split("-")[1] + "_fingerprint"] = "0" * 64
    elif mutation == "log-order":
        binding["original_logs"].reverse()
    elif mutation == "log-count":
        binding["original_logs"][0]["count"] -= 1
    elif mutation == "log-type":
        binding["original_logs"][0]["repetition"] = True
    elif mutation == "log-copy":
        binding["original_logs"][0]["copy"]["sha256"] = "0" * 64
    elif mutation == "missing-log":
        binding["original_logs"].pop()
    file = tmp_path / "binding.json"
    file.write_text(json.dumps(binding))
    profile["original_input_bindings"] = ref(file)
    file = tmp_path / "profile.json"
    file.write_text(json.dumps(profile))
    recipe.grading[bridge.PROFILE_KEY] = ref(file)
    with pytest.raises(ContractError):
        bridge.execution_view(task, oracle, recipe)


@pytest.mark.parametrize("mutation", [
    "raw-oracle", "foreign-task", "grading-command", "grading-parser", "protected",
    "public-command", "environment", "public-asset", "private-target", "private-role",
    "private-tree", "old-public-profile", "old-private-profile",
])
def test_changed_source_or_runtime_is_rejected(original, tmp_path, mutation):
    root, task, oracle, recipe = original
    if mutation == "raw-oracle":
        oracle["pass_to_pass_tests"][0] -= 1
    elif mutation == "foreign-task":
        task["id"] = "foreign"
    elif mutation == "grading-command":
        recipe.grading["commands"][0]["command"] = "true"
    elif mutation == "grading-parser":
        recipe.grading["commands"][0]["parser"] = "junit"
    elif mutation == "protected":
        recipe.grading["immutable_paths"] = []
    elif mutation == "public-command":
        recipe.dev_checks.append("true")
    elif mutation == "environment":
        recipe.env["NEW_INSTRUMENTATION"] = "true"
    elif mutation == "public-asset":
        recipe.assets[0]["sha256"] = "0" * 64
    elif mutation in {"private-target", "private-role", "private-tree"}:
        asset = next(a for a in recipe.assets if a["visibility"] == "grader")
        if mutation == "private-target":
            asset["target"] += "-other"
        elif mutation == "private-role":
            asset["visibility"] = "developer"
        else:
            descriptor = json.loads(Path(asset["path"]).read_text())
            descriptor["tree_sha256"] = "0" * 64
            file = tmp_path / "descriptor.json"
            file.write_text(json.dumps(descriptor))
            asset.update(ref(file))
    else:
        recipe.grading["original_public_gradle_profile" if mutation == "old-public-profile"
                       else "original_legacy_profile"] = {}
    with pytest.raises(ContractError):
        bridge.execution_view(task, oracle, recipe)


def actual(original):
    root, task, oracle, recipe = original
    case = root.name
    path = Path(os.environ["SOLR_HIBERNATE_ADMISSION_ROOT"]) / "actual-admission-proofs" / (case + ".json")
    if not path.exists():
        pytest.skip("actual six-run proof is still executing; no fabricated positive fixture")
    return json.loads(path.read_text()), task, oracle, recipe


def test_actual_six_run_proof_preserves_both_original_identities(original):
    proof, task, oracle, recipe = actual(original)
    before = deepcopy(oracle)
    assert bridge.proof_identity_matches(proof, task, oracle, recipe)
    assert proof_matches(proof, task, oracle, recipe, bridge.ENGINE)
    raw = {key: value for key, value in proof.items() if key != bridge.BRIDGE_KEY}
    assert not proof_matches(raw, task, oracle, recipe, bridge.ENGINE)
    assert oracle == before and proof["oracle_fingerprint"] != canonical_hash(oracle)


@pytest.mark.parametrize("mutation", [
    "status", "engine", "raw-retag", "runtime", "task", "missing-certificate", "missing-slot",
    "slot-order", "slot-type", "missing-test", "extra-test", "failed-test", "integer-pass",
    "certificate-count", "certificate-tests", "missing-audit", "foreign-public-bridge", "foreign-private-bridge",
])
def test_corrupted_actual_execution_proof_is_not_admitted(original, mutation):
    proof, task, oracle, recipe = actual(original)
    if mutation == "status":
        proof["status"] = "blocked"
    elif mutation == "engine":
        proof["engine_fingerprint"] = "0" * 64
    elif mutation == "raw-retag":
        proof["oracle_fingerprint"] = canonical_hash(oracle)
    elif mutation in {"runtime", "task"}:
        proof[mutation + "_fingerprint"] = "0" * 64
    elif mutation == "missing-certificate":
        proof.pop(bridge.BRIDGE_KEY)
    elif mutation == "missing-slot":
        proof["runs"].pop()
    elif mutation == "slot-order":
        proof["runs"].reverse()
    elif mutation == "slot-type":
        proof["runs"][0]["repetition"] = False
    elif mutation == "missing-test":
        proof["runs"][0]["tests"].pop(next(iter(proof["runs"][0]["tests"])))
    elif mutation == "extra-test":
        proof["runs"][0]["tests"]["0::foreign"] = True
    elif mutation in {"failed-test", "integer-pass"}:
        proof["runs"][0]["tests"][next(iter(proof["runs"][0]["tests"]))] = False if mutation == "failed-test" else 1
    elif mutation in {"certificate-count", "certificate-tests"}:
        proof[bridge.BRIDGE_KEY]["original_count_vector" if mutation == "certificate-count"
                                else "execution_test_ids"] = []
    elif mutation == "missing-audit":
        proof[bridge.BRIDGE_KEY]["execution_audit"]["sha256"] = "0" * 64
    else:
        proof["original_public_gradle_identity_bridge" if mutation == "foreign-public-bridge"
              else "original_oracle_identity_bridge"] = {}
    assert not bridge.proof_identity_matches(proof, task, oracle, recipe)
    assert not proof_matches(proof, task, oracle, recipe, bridge.ENGINE)


def test_known_case_cannot_strip_profile_and_retag_raw_oracle(original):
    root, task, oracle, recipe = original
    recipe.grading.pop(bridge.PROFILE_KEY)
    forged = {"status": "ready", "task_fingerprint": canonical_hash(task),
              "runtime_fingerprint": recipe.fingerprint, "oracle_fingerprint": canonical_hash(oracle),
              "engine_fingerprint": bridge.ENGINE, "runs": []}
    assert not proof_matches(forged, task, oracle, recipe, bridge.ENGINE)
