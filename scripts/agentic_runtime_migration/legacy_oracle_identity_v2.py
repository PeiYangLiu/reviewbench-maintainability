"""Explicit execution views for two original legacy per-run-count Oracles.

The release Oracle stays byte-for-byte unchanged. The frozen verifier receives
the named-test view and keeps that view's own fingerprint. Host admission
validates the separate, reproducible translation certificate.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import shutil

from reviewbench_maintainability.agentic.contracts import ContractError, atomic_json, canonical_hash
from reviewbench_maintainability.agentic.evaluation import validate_grading
from .jvm_legacy_gradle import CASES, load_profile

SCHEMA = "reviewbench-original-legacy-count-oracle-view/v1"
BRIDGE_KEY = "original_oracle_identity_bridge"
EXECUTION_ENGINE = "5142d1fec94eb6c5a6177a66ee7ce8533a26f0a2578280075a6947a399c01ca3"


def execution_view(task, oracle, recipe):
    """Decode only the two code-bound six-count records with original fixtures."""
    reference = recipe.grading.get("original_legacy_profile")
    if (task.get("id") not in CASES or not isinstance(reference, dict)
            or set(reference) != {"path", "sha256"}):
        raise ContractError("legacy Oracle count decoding requires a bound original profile")
    profile = load_profile(task, oracle, reference)
    validate_grading(recipe)
    if ((recipe.task_id, recipe.repo, recipe.start_sha, recipe.lane)
            != (task["id"], task["repo"], task["start_sha"], "jvm")):
        raise ContractError("legacy Oracle count view belongs to another runtime")
    counts = oracle.get("pass_to_pass_tests")
    if (type(counts) is not list or len(counts) != 6
            or any(type(value) is not int or value != 1 for value in counts)
            or type(oracle.get("pass_to_pass")) is not int or oracle["pass_to_pass"] != 1):
        raise ContractError("legacy Oracle count view requires six original integer counts")
    original_runs = []
    for field in ("start_runs", "golden_runs"):
        rows = oracle.get(field)
        if type(rows) is not list or len(rows) != 3:
            raise ContractError("legacy Oracle count view lacks three original runs per variant")
        for repetition, row in enumerate(rows, 1):
            if (type(row.get("run")) is not int or row["run"] != repetition
                    or type(row.get("tests")) is not int or row["tests"] != 1
                    or type(row.get("exit_code")) is not int or row["exit_code"] != 0
                    or row.get("error")):
                raise ContractError("legacy Oracle count view changed an original execution")
            original_runs.append(row["tests"])
    if original_runs != counts:
        raise ContractError("legacy Oracle field is not the retained six-run count vector")
    case = profile["case"]
    expected = [case["junit_id"]]
    actual = [name for command in recipe.grading["commands"] for name in command["expected_tests"]]
    if actual != expected or recipe.grading["golden_patch"]["sha256"] != case["golden_patch"]["sha256"]:
        raise ContractError("legacy Oracle count view changed the original test/reference")
    raw_fingerprint = canonical_hash(oracle)
    view = deepcopy(oracle)
    view["pass_to_pass_tests"] = list(expected)
    certificate = {
        "schema_version": SCHEMA,
        "task_fingerprint": canonical_hash(task),
        "runtime_fingerprint": recipe.fingerprint,
        "original_oracle_fingerprint": raw_fingerprint,
        "execution_oracle_fingerprint": canonical_hash(view),
        "execution_engine_fingerprint": EXECUTION_ENGINE,
        "source_profile": deepcopy(reference),
        "original_fixture": deepcopy(case["fixture"]),
        "changed_fields": ["pass_to_pass_tests"],
        "source_field_encoding": "six_original_run_counts",
        "original_count_vector": list(counts),
        "execution_test_ids": list(expected),
        "original_assertions_changed": False,
        "original_reference_changed": False,
    }
    if canonical_hash(oracle) != raw_fingerprint:
        raise ContractError("legacy Oracle count decoding mutated the original")
    return view, certificate


def _portable_certificate_view(proof, task, oracle, recipe):
    """Verify certificate without host-only profile files when running on remote VMs,
    and accept either the raw 6-count Oracle or its exact translated execution view."""
    cert = proof.get(BRIDGE_KEY)
    reference = recipe.grading.get("original_legacy_profile")
    fixed = CASES.get(task.get("id"))
    if (not isinstance(cert, dict) or fixed is None or not isinstance(reference, dict)
            or set(reference) != {"path", "sha256"}):
        raise ContractError("legacy Oracle count decoding requires a bound original profile")
    validate_grading(recipe)
    if ((recipe.task_id, recipe.repo, recipe.start_sha, recipe.lane)
            != (task["id"], task["repo"], task["start_sha"], "jvm")):
        raise ContractError("legacy Oracle count view belongs to another runtime")
    expected = [fixed["junit_id"]]
    actual = [name for command in recipe.grading["commands"] for name in command["expected_tests"]]
    if actual != expected or recipe.grading["golden_patch"]["sha256"] != fixed["golden_sha256"]:
        raise ContractError("legacy Oracle count view changed the original test/reference")
    if (oracle.get("command") != fixed["command"]
            or oracle.get("source_only_patch", {}).get("sha256") != fixed["golden_sha256"]
            or type(oracle.get("pass_to_pass")) is not int or oracle["pass_to_pass"] != 1):
        raise ContractError("legacy Oracle count view changed an original field")
    original_runs = []
    for field in ("start_runs", "golden_runs"):
        rows = oracle.get(field)
        if type(rows) is not list or len(rows) != 3:
            raise ContractError("legacy Oracle count view lacks three original runs per variant")
        for repetition, row in enumerate(rows, 1):
            if (type(row.get("run")) is not int or row["run"] != repetition
                    or type(row.get("tests")) is not int or row["tests"] != 1
                    or type(row.get("exit_code")) is not int or row["exit_code"] != 0
                    or row.get("error")):
                raise ContractError("legacy Oracle count view changed an original execution")
            original_runs.append(row["tests"])
    if original_runs != [1] * 6:
        raise ContractError("legacy Oracle field is not the retained six-run count vector")
    p2p = oracle.get("pass_to_pass_tests")
    if type(p2p) is list and len(p2p) == 6 and all(type(v) is int and v == 1 for v in p2p):
        raw_oracle = deepcopy(oracle)
        view = deepcopy(oracle)
        view["pass_to_pass_tests"] = list(expected)
    elif p2p == expected:
        view = deepcopy(oracle)
        raw_oracle = deepcopy(oracle)
        raw_oracle["pass_to_pass_tests"] = [1] * 6
    else:
        raise ContractError("legacy Oracle count view requires six original integer counts or exact junit view")
    fixture_ref = cert.get("original_fixture")
    if (not isinstance(fixture_ref, dict) or set(fixture_ref) != {"path", "sha256"}
            or fixture_ref["sha256"] != fixed["fixture_sha256"]):
        raise ContractError("legacy Oracle fixture certificate mismatch")
    # If the profile exists on the local host and oracle is raw, also run full load_profile
    if Path(reference["path"]).is_file() and p2p == [1] * 6:
        load_profile(task, raw_oracle, reference)
    certificate = {
        "schema_version": SCHEMA,
        "task_fingerprint": canonical_hash(task),
        "runtime_fingerprint": recipe.fingerprint,
        "original_oracle_fingerprint": canonical_hash(raw_oracle),
        "execution_oracle_fingerprint": canonical_hash(view),
        "execution_engine_fingerprint": EXECUTION_ENGINE,
        "source_profile": deepcopy(reference),
        "original_fixture": deepcopy(fixture_ref),
        "changed_fields": ["pass_to_pass_tests"],
        "source_field_encoding": "six_original_run_counts",
        "original_count_vector": [1] * 6,
        "execution_test_ids": list(expected),
        "original_assertions_changed": False,
        "original_reference_changed": False,
    }
    return view, certificate


def proof_identity_matches(proof, task, oracle, recipe):
    """Admit only an exact certificate and six actual named-test pass records."""
    try:
        view, certificate = _portable_certificate_view(proof, task, oracle, recipe)
        if (proof.get("status") != "ready"
                or proof.get("oracle_fingerprint") != canonical_hash(view)
                or proof.get("engine_fingerprint") != EXECUTION_ENGINE
                or canonical_hash(proof.get(BRIDGE_KEY)) != canonical_hash(certificate)):
            return False
        expected = {"0::" + name: True for name in certificate["execution_test_ids"]}
        slots = [(variant, repetition) for variant in ("start", "golden") for repetition in range(3)]
        runs = proof.get("runs")
        if type(runs) is not list or len(runs) != 6:
            return False
        for record, slot in zip(runs, slots):
            if (type(record.get("repetition")) is not int
                    or (record.get("variant"), record["repetition"]) != slot
                    or record.get("tests") != expected
                    or any(type(value) is not bool for value in record["tests"].values())):
                return False
        return True
    except (ContractError, KeyError, TypeError, ValueError, OSError):
        return False


def verify_original_schema(task, recipe, oracle, repository, asset_root, output, *, verify=None):
    """Run the unchanged verifier; annotate, never relabel, its execution proof."""
    if verify is None:
        from reviewbench_maintainability.agentic.preparation import verify_recipe
        verify = verify_recipe
    if "original_legacy_profile" not in recipe.grading:
        return verify(task, recipe, oracle, repository, asset_root, output)
    view, certificate = execution_view(task, oracle, recipe)
    output = Path(output)
    proof_file = output / "verification.json"
    if proof_file.exists() or (output / "execution-verification.json").exists():
        raise ContractError("legacy Oracle execution proof is append-only; use a new unfinished attempt directory")
    original_bytes = canonical_hash(oracle)
    output.mkdir(parents=True, exist_ok=True)
    atomic_json(output / "original-oracle-view.json", {
        "original_oracle": oracle, "execution_oracle": view, "translation": certificate,
        "annotation_owner": "host_schema_adapter_not_frozen_execution_engine",
    })
    proof = verify(task, recipe, view, repository, asset_root, output)
    if (canonical_hash(oracle) != original_bytes
            or proof.get("oracle_fingerprint") != certificate["execution_oracle_fingerprint"]
            or proof.get("task_fingerprint") != certificate["task_fingerprint"]
            or proof.get("runtime_fingerprint") != certificate["runtime_fingerprint"]
            or proof.get("engine_fingerprint") != EXECUTION_ENGINE):
        raise ContractError("legacy Oracle verifier returned a changed execution binding")
    if not proof_file.is_file() or json.loads(proof_file.read_text()) != proof:
        raise ContractError("legacy Oracle verifier did not retain its original proof bytes")
    shutil.copy2(proof_file, output / "execution-verification.json")
    annotated = deepcopy(proof)
    annotated[BRIDGE_KEY] = certificate
    atomic_json(proof_file, annotated)
    return annotated
