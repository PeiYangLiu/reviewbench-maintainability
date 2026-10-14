"""Actual retained five-plus-one execution evidence, never a synthetic ready flag."""
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from agentic_runtime_migration import solr_resource_composition as bridge
from reviewbench_maintainability.agentic.contracts import ContractError, RuntimeRecipe, canonical_hash
from run_agentic_progressive import proof_matches


@pytest.fixture
def actual():
    root = Path(os.environ["SOLR_RESOURCE_COMPOSITION_ROOT"])
    inputs = json.loads((root / "composition-inputs.json").read_text())
    task = json.loads(Path(inputs["source_task"]["path"]).read_text())
    oracle = json.loads(Path(inputs["source_oracle"]["path"]).read_text())
    recipe = RuntimeRecipe.from_dict(json.loads((root / "runtime-final-v1/runtime-recipes.jsonl").read_text()))
    proof = json.loads((root / "execution-composition.json").read_text())
    return task, oracle, recipe, proof, inputs


def test_actual_composition_retains_each_original_success_and_one_new_slot(actual):
    task, oracle, recipe, proof, inputs = actual
    old = json.loads(Path(inputs["original_proof"]["path"]).read_text())
    before = deepcopy(oracle)
    result = bridge.composition(task, oracle, recipe)
    assert result == proof and oracle == before
    assert result["runs"][:5] == old["runs"]
    assert len(result["runs"]) == 6 and all(len(row["tests"]) == 215 for row in result["runs"])
    assert result["oracle_fingerprint"] != canonical_hash(oracle)
    assert proof_matches(proof, task, oracle, recipe, bridge.ENGINE)
    assert result[bridge.KEY]["successful_calibration_replays"] == 0
    assert result[bridge.KEY]["new_original_failed_slot_executions"] == 1


@pytest.mark.parametrize("field,value", [
    ("pids_limit", 4096), ("cpus", 8), ("memory_mb", 16384), ("command_timeout_seconds", 7200),
])
def test_unmeasured_resource_change_is_rejected(actual, field, value):
    task, oracle, recipe, proof, _ = actual
    changed = replace(recipe, **{field: value})
    with pytest.raises(ContractError):
        bridge.composition(task, oracle, changed)
    assert not bridge.proof_identity_matches(proof, task, oracle, changed)


@pytest.mark.parametrize("mutation", [
    "source", "selector", "tests", "reference", "public-command", "environment", "immutable-paths", "private-asset",
])
def test_non_resource_execution_changes_are_rejected(actual, mutation):
    task, oracle, recipe, _, _ = actual
    if mutation == "source":
        task["start_sha"] = "0" * 40
    elif mutation == "selector":
        recipe.grading["commands"][0]["command"] = "true"
    elif mutation == "tests":
        recipe.grading["commands"][0]["expected_tests"].pop()
    elif mutation == "reference":
        recipe.grading["golden_patch"]["sha256"] = "0" * 64
    elif mutation == "public-command":
        recipe.dev_checks.append("true")
    elif mutation == "environment":
        recipe.env["JAVA_TOOL_OPTIONS"] += " -Dtests.seed=00000000"
    elif mutation == "immutable-paths":
        recipe.grading["immutable_paths"] = []
    elif mutation == "private-asset":
        recipe.assets[-1]["sha256"] = "0" * 64
    with pytest.raises(ContractError):
        bridge.composition(task, oracle, recipe)


@pytest.mark.parametrize("mutation", [
    "ready", "raw-retag", "engine", "runtime", "missing-slot", "slot-order", "slot-type",
    "test-value", "test-omitted", "test-extra", "source-role", "original-runtime",
    "new-count", "old-count", "quota", "missing-certificate",
])
def test_any_tampered_composition_is_not_a_license(actual, mutation):
    task, oracle, recipe, proof, _ = actual
    if mutation == "ready":
        proof["status"] = "blocked"
    elif mutation == "raw-retag":
        proof["oracle_fingerprint"] = canonical_hash(oracle)
    elif mutation in {"engine", "runtime"}:
        proof[mutation + "_fingerprint"] = "0" * 64
    elif mutation == "missing-slot":
        proof["runs"].pop()
    elif mutation == "slot-order":
        proof["runs"].reverse()
    elif mutation == "slot-type":
        proof["runs"][0]["repetition"] = False
    elif mutation == "test-value":
        proof["runs"][0]["tests"][next(iter(proof["runs"][0]["tests"]))] = 1
    elif mutation == "test-omitted":
        proof["runs"][0]["tests"].pop(next(iter(proof["runs"][0]["tests"])))
    elif mutation == "test-extra":
        proof["runs"][0]["tests"]["0::foreign"] = True
    elif mutation == "source-role":
        proof[bridge.KEY]["slots"][0]["origin"] = "new-original-failed-slot"
    elif mutation == "original-runtime":
        proof[bridge.KEY]["original_runtime_fingerprint"] = "0" * 64
    elif mutation in {"new-count", "old-count"}:
        proof[bridge.KEY]["new_original_failed_slot_executions" if mutation == "new-count"
                          else "retained_successful_slots"] += 1
    elif mutation == "quota":
        proof[bridge.KEY]["resource_revision"]["after"] = 4096
    else:
        proof.pop(bridge.KEY)
    assert not bridge.proof_identity_matches(proof, task, oracle, recipe)
    assert not proof_matches(proof, task, oracle, recipe, bridge.ENGINE)


@pytest.mark.parametrize("kind", ["recovery-result", "recovery-input", "original-proof", "raw-xml"])
def test_incomplete_underlying_evidence_is_rejected(actual, monkeypatch, kind):
    task, oracle, recipe, proof, inputs = actual
    original_read = bridge.read
    if kind == "raw-xml":
        monkeypatch.setattr(bridge.original, "_xml_statuses", lambda *args: ())
    else:
        key = {"recovery-result": "recovery_result", "recovery-input": "recovery_input",
               "original-proof": "original_proof"}[kind]
        def corrupted(pointer):
            data = original_read(pointer)
            if pointer == inputs[key]:
                data = deepcopy(data)
                if kind == "recovery-result":
                    data["replayed_successful_calibrations"] = 1
                elif kind == "recovery-input":
                    data["retained_successful_files"].pop(next(iter(data["retained_successful_files"])))
                else:
                    data["runs"].pop()
            return data
        monkeypatch.setattr(bridge, "read", corrupted)
    assert not bridge.proof_identity_matches(proof, task, oracle, recipe)
