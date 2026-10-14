"""Explicit named execution view for the original Web3Signer six-count Oracle.

The release record and frozen verifier retain their different fingerprints.
This host certificate explains the count-to-identity projection; it neither
changes a test nor turns unsuccessful execution into an admission proof.
"""
from copy import deepcopy
import json
from pathlib import Path
import re

from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash, file_hash
from reviewbench_maintainability.agentic.evaluation import validate_grading

SCHEMA = "reviewbench-original-public-gradle-count-view/v1"
BRIDGE_KEY = "original_public_gradle_identity_bridge"
ENGINE = "5142d1fec94eb6c5a6177a66ee7ce8533a26f0a2578280075a6947a399c01ca3"
CASE = "github__Consensys__web3signer__pull_489__comment_789312375__modularity"
START = "4b896acf3fb5dc1c19993e41528a415e7c449052"
ORACLE = "dc3e6a630fa57b70f8c42be0392130814e0bc7690caf6d2d1712d56c63dac755"
GOLDEN = "c139ecef8d365069c9021ae74f9d4281f75733f72bcf396135c20f55c3fa3919"
TEST_SOURCE = "45e07ac64a5c220887a02b59948c10c26dfcea893cc440afaa3c6d2b929cd64e"
TEST_PATH = "acceptance-tests/src/test/java/tech/pegasys/web3signer/tests/keymanager/DeleteKeystoresAcceptanceTest.java"
CLASS = "tech.pegasys.web3signer.tests.keymanager.DeleteKeystoresAcceptanceTest"
METHODS = [
    "invalidRequestBodyReturnsError", "deletingNonExistingKeyReturnNotFound",
    "deletingExistingKeyReturnDeleted", "deletingExistingTwiceReturnsNotActive",
    "deletingRemovesSignerFromActiveSigners", "testRequestBodyParsing",
]
TESTS = [CLASS + "#" + method + "()" for method in METHODS]


def require(condition, message):
    if not condition:
        raise ContractError("original public Gradle view: " + message)


def checked(reference):
    require(isinstance(reference, dict) and set(reference) == {"path", "sha256"},
            "input requires a path and digest")
    require(isinstance(reference["path"], str) and isinstance(reference["sha256"], str)
            and re.fullmatch(r"[0-9a-f]{64}", reference["sha256"]), "invalid input binding")
    path = Path(reference["path"])
    require(path.is_absolute() and path.is_file() and not path.is_symlink()
            and file_hash(path) == reference["sha256"], "bound input is missing or changed")
    return path


def read(reference):
    return json.loads(checked(reference).read_text())


def execution_view(task, oracle, recipe):
    require(task.get("id") == CASE and task.get("repo") == "Consensys/web3signer"
            and task.get("start_sha") == START, "unsupported original task")
    require(canonical_hash(oracle) == ORACLE, "original release Oracle changed")
    require("original_legacy_profile" not in recipe.grading, "conflicting private/public profiles")
    reference = recipe.grading.get("original_public_gradle_profile")
    profile = read(reference)
    require(isinstance(profile, dict)
            and profile.get("schema_version") == "reviewbench-original-web3signer-public-gradle-profile/v1"
            and type(profile.get("full_scope")) is int and profile["full_scope"] == 402
            and profile.get("id") == CASE, "unknown original source profile")
    require(profile.get("original_test_source_path") == TEST_PATH
            and profile.get("original_gradle_version") == "7.3"
            and type(profile.get("original_count")) is int and profile["original_count"] == 6
            and profile.get("expected_tests") == TESTS
            and profile.get("original_command") == oracle["command"], "original test selection changed")
    source = checked(profile["original_test_source"])
    require(file_hash(source) == TEST_SOURCE
            and re.findall(r"@Test\s+public void (\w+)\(", source.read_text()) == METHODS,
            "original public assertions changed")
    golden = checked(profile["original_golden_patch"])
    require(file_hash(golden) == GOLDEN
            and oracle["source_only_patch"]["sha256"] == GOLDEN
            and file_hash(checked(recipe.grading["golden_patch"])) == GOLDEN,
            "original reference changed")
    binding = read(profile["original_input_bindings"])
    require(binding.get("id") == CASE and binding.get("full_scope") == 402
            and binding.get("task_fingerprint") == canonical_hash(task)
            and binding.get("oracle_fingerprint") == ORACLE
            and canonical_hash(read(binding["task"])) == canonical_hash(task)
            and canonical_hash(read(binding["oracle"])) == ORACLE,
            "source input binding differs from the task/Oracle")
    require(binding.get("original_methods") == METHODS
            and binding.get("original_test_source") == profile["original_test_source"]
            and binding.get("original_test_source_path") == TEST_PATH
            and binding.get("original_command") == oracle["command"]
            and binding.get("golden_patch") == profile["original_golden_patch"],
            "source fixture/reference binding changed")
    counts = oracle.get("pass_to_pass_tests")
    require(type(oracle.get("pass_to_pass")) is int and oracle["pass_to_pass"] == 6
            and type(counts) is list and len(counts) == 6
            and all(type(value) is int and value == 6 for value in counts),
            "source field is not the original six-run integer count vector")
    records = binding.get("original_logs")
    require(type(records) is list and len(records) == 6, "six original logs are required")
    logs = []
    for index, (variant, repetition) in enumerate(
            (v, n) for v in ("start", "golden") for n in range(1, 4)):
        originals = oracle[variant + "_runs"]
        require(type(originals) is list and len(originals) == 3, "original run cohort changed")
        row, original = records[index], originals[repetition - 1]
        require(row.get("variant") == variant
                and type(row.get("repetition")) is int and row["repetition"] == repetition
                and type(row.get("original_test_count")) is int and row["original_test_count"] == 6,
                "original log slot/count changed")
        require(type(original.get("run")) is int and original["run"] == repetition
                and type(original.get("tests")) is int and original["tests"] == 6
                and type(original.get("exit_code")) is int and original["exit_code"] == 0
                and not original.get("error"), "original successful replay changed")
        old, copied = checked(row["original"]), checked(row["copy"])
        require(str(old).endswith("/" + original["log"])
                and file_hash(old) == file_hash(copied)
                and "BUILD SUCCESSFUL" in copied.read_text()
                and "BUILD FAILED" not in copied.read_text(), "original log bytes differ")
        logs.append({"variant": variant, "repetition": repetition,
                     "sha256": file_hash(copied), "count": 6})
    support = read(profile["source_support"])
    require(support.get("image") == recipe.image
            and support.get("vault", {}).get("sha256")
            == "f486da4f6d08e42eb2c17e95cf36cc7f9d9c0e7cc8ced06ce3fca7c3abd7db3d",
            "runtime support no longer matches the admitted original dependency")
    validate_grading(recipe)
    require((recipe.task_id, recipe.repo, recipe.start_sha, recipe.lane)
            == (CASE, task["repo"], START, "jvm"), "runtime belongs to another source")
    require(len(recipe.grading["commands"]) == 1
            and recipe.grading["commands"][0]["expected_tests"] == TESTS
            and recipe.grading["commands"][0]["parser"] == "json"
            and recipe.grading["commands"][0]["report_path"] == ".oracle/web3signer-results.json"
            and recipe.grading["commands"][0]["command"] == (
                "python3 /opt/reviewbench-assets/web3signer-grader/runner.py "
                "--spec /opt/reviewbench-assets/web3signer-grader/spec.json "
                "--output /workspace/.oracle/web3signer-results.json")
            and TEST_PATH in recipe.grading.get("immutable_paths", []),
            "fixed named-test execution contract changed")
    view = deepcopy(oracle)
    view["pass_to_pass_tests"] = list(TESTS)
    certificate = {
        "schema_version": SCHEMA, "task_fingerprint": canonical_hash(task),
        "runtime_fingerprint": recipe.fingerprint, "original_oracle_fingerprint": ORACLE,
        "execution_oracle_fingerprint": canonical_hash(view), "execution_engine_fingerprint": ENGINE,
        "source_profile": deepcopy(reference), "original_public_test_source": deepcopy(profile["original_test_source"]),
        "original_reference_sha256": GOLDEN, "original_logs": logs,
        "changed_fields": ["pass_to_pass_tests"], "source_field_encoding": "six_original_run_counts",
        "original_count_vector": list(counts), "execution_test_ids": list(TESTS),
        "original_assertions_changed": False, "original_reference_changed": False,
    }
    return view, certificate


def annotate(proof, task, oracle, recipe):
    """Add an explicit host certificate without modifying the frozen proof fields."""
    view, certificate = execution_view(task, oracle, recipe)
    require(proof.get("oracle_fingerprint") == canonical_hash(view)
            and proof.get("task_fingerprint") == canonical_hash(task)
            and proof.get("runtime_fingerprint") == recipe.fingerprint
            and proof.get("engine_fingerprint") == ENGINE, "execution proof identity changed")
    require(BRIDGE_KEY not in proof and "original_oracle_identity_bridge" not in proof,
            "execution proof was already annotated")
    result = deepcopy(proof)
    result[BRIDGE_KEY] = certificate
    return result


def proof_identity_matches(proof, task, oracle, recipe):
    try:
        view, certificate = execution_view(task, oracle, recipe)
        if (proof.get("status") != "ready" or proof.get("engine_fingerprint") != ENGINE
                or proof.get("oracle_fingerprint") != canonical_hash(view)
                or proof.get("task_fingerprint") != canonical_hash(task)
                or proof.get("runtime_fingerprint") != recipe.fingerprint
                or "original_oracle_identity_bridge" in proof
                or canonical_hash(proof.get(BRIDGE_KEY)) != canonical_hash(certificate)):
            return False
        expected = {"0::" + name: True for name in TESTS}
        runs = proof.get("runs")
        if type(runs) is not list or len(runs) != 6:
            return False
        for row, slot in zip(runs, ((v, n) for v in ("start", "golden") for n in range(3))):
            if (type(row.get("repetition")) is not int
                    or (row.get("variant"), row["repetition"]) != slot
                    or row.get("tests") != expected
                    or any(type(value) is not bool for value in row["tests"].values())):
                return False
        return True
    except (ContractError, KeyError, TypeError, ValueError, OSError):
        return False
