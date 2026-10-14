"""Actual file/XML controls, with no project builds or external model calls."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import calibration_continuation as continuation
from agentic_runtime_migration.jvm_lane import frozen_non_p2p_exit_codes, frozen_non_p2p_statuses
from reviewbench_maintainability.agentic.contracts import ContractError, atomic_json, canonical_hash, file_hash
from test_agentic_exec import recipe_for


def ref(path):
    return {"path": str(path.resolve()), "sha256": file_hash(path)}


def commands(root, argv):
    root.mkdir(parents=True, exist_ok=True)
    for index, command in enumerate(argv):
        name = str(index)
        stdout, stderr = root / (name + ".stdout"), root / (name + ".stderr")
        stdout.write_text("original output\n")
        stderr.write_text("")
        atomic_json(root / (name + ".json"), {
            "id": name, "command": command, "status": "completed", "returncode": 0,
            "stdout_file": stdout.name, "stderr_file": stderr.name,
            "stdout_sha256": file_hash(stdout), "stderr_sha256": file_hash(stderr),
            "timed_out": False, "log_limit_exceeded": False,
        })


def make_case(root, task=None, oracle=None, recipe=None, engine="fixture-engine"):
    root.mkdir(parents=True, exist_ok=True)
    task = task or {"id": "original-task", "repo": "fixture/repo", "start_sha": "a" * 40}
    if recipe is None:
        recipe = replace(recipe_for(task), bootstrap=["original setup"], grading={
            "multifile_build": True, "immutable_paths": [],
            "commands": [{"command": "original test", "parser": "json",
                          "report_path": ".oracle/result.json",
                          "expected_tests": ["Fixed#first", "Fixed#second"]}],
        })
    names = recipe.grading["commands"][0]["expected_tests"]
    if oracle is None:
        statuses = {name: "PASSED" for name in names} | {"Allowed#setup": "FAILED"}
        oracle = {"id": task["id"], "pass_to_pass_tests": names,
                  "start_run_records": [{"exit_code": 1, "statuses": statuses} for _ in range(3)],
                  "golden_run_records": [{"exit_code": 1, "statuses": statuses} for _ in range(3)]}
    identity = {
        "id": task["id"], "task_fingerprint": canonical_hash(task), "oracle_fingerprint": canonical_hash(oracle),
        "runtime_fingerprint": recipe.fingerprint, "engine_fingerprint": engine,
    }
    old_dir, fresh = root / "original", root / "new-golden2"
    old_dir.mkdir()
    fresh.mkdir()
    tests = continuation.expected_tests(recipe)
    original = {
        **identity, "status": "blocked", "error": "InfrastructureError: golden fixed PASS_TO_PASS replay failed",
        "runs": [{"variant": variant, "repetition": number,
                  "tests": {name: (variant, number) != ("golden", 2) for name in tests}}
                 for variant, number in continuation.SLOTS],
    }
    atomic_json(old_dir / "verification.json", original)
    expected_commands = [*recipe.bootstrap, *recipe.grading.get("bootstrap", []),
                         *[row["command"] for row in recipe.grading["commands"]]]
    commands(old_dir / "developer", [*recipe.bootstrap, *recipe.dev_checks])
    for variant, number in continuation.SLOTS[:-1]:
        commands(old_dir / f"{variant}-{number}", expected_commands)
    commands(fresh / "commands", expected_commands)
    allowed, codes = frozen_non_p2p_statuses(oracle), frozen_non_p2p_exit_codes(oracle)
    observed = {name: "PASSED" for name in names} | {name: "FAILED" for name in allowed}
    xml_root = fresh / "original-xml"
    xml_root.mkdir()
    document = ET.Element("testsuite")
    for name, state in observed.items():
        cls, case = name.split("#", 1)
        element = ET.SubElement(document, "testcase", classname=cls, name=case)
        if state == "FAILED":
            ET.SubElement(element, "error", message="original allowed setup failure")
    xml = xml_root / "TEST-original.xml"
    ET.ElementTree(document).write(xml)
    code = 1 if allowed else 0
    report = {
        "schema_version": "reviewbench-exec-test-results/v1",
        "tests": [{"id": name, "status": "passed"} for name in names],
        "original_exit_code": code, "observed_nonpassing": {name: "FAILED" for name in allowed},
        "frozen_non_p2p_statuses": allowed, "frozen_non_p2p_exit_codes": codes,
    }
    atomic_json(fresh / "original-report.json", report)
    atomic_json(fresh / "observed-statuses.json", observed)
    record = {
        "schema_version": "reviewbench-original-failed-slot-diagnostic/v1",
        "status": "diagnostic_completed", "id": task["id"], "full_scope": 402,
        "engine_fingerprint": engine, "runtime_fingerprint": recipe.fingerprint,
        "original_failed_proof": ref(old_dir / "verification.json"), "original_failed_slot": ["golden", 2],
        "wrapper_exit_status": 0, "replayed_successful_calibrations": 0,
        "new_failed_slot_executions": 1, "new_model_or_judge_calls": 0,
        "raw_report": ref(fresh / "original-report.json"), "raw_xml": [ref(xml)],
        "observed_statuses": ref(fresh / "observed-statuses.json"),
        "observed_nonpassing": report["observed_nonpassing"], "original_subprocess_exit_status": code,
        "tests": {name: True for name in names}, "not_a_readiness_proof": True,
    }
    atomic_json(fresh / "result.json", record)
    return {"task": task, "oracle": oracle, "recipe": recipe, "engine": engine,
            "original": original, "old_dir": old_dir, "fresh": fresh,
            "record": record, "xml": xml}


def compose(case):
    return continuation.compose_readiness(
        case["task"], case["oracle"], case["recipe"], case["old_dir"],
        [case["fresh"] / "result.json"], case["engine"])


def test_compose_preserves_five_successes_and_the_prior_failed_attempt(tmp_path):
    case = make_case(tmp_path)
    before = (case["old_dir"] / "verification.json").read_bytes()
    proof = compose(case)
    assert proof["status"] == "ready"
    assert proof["runs"][:5] == case["original"]["runs"][:5]
    assert (case["old_dir"] / "verification.json").read_bytes() == before
    assert proof["calibration_continuation"]["new_execution_slots"] == [["golden", 2]]
    assert proof["calibration_continuation"]["successful_calibration_replays"] == 0


@pytest.mark.parametrize("change", [
    "model", "runtime", "engine", "slot", "wrapper", "successful-replay",
    "missing-xml", "omitted-xml", "changed-report", "duplicate-test",
    "changed-old-output", "missing-old-slot", "changed-policy",
])
def test_incomplete_changed_or_resampled_evidence_is_rejected(tmp_path, change):
    case = make_case(tmp_path)
    record = case["record"]
    if change == "model":
        record["new_model_or_judge_calls"] = 1
    elif change in {"runtime", "engine"}:
        record[change + "_fingerprint"] = "changed"
    elif change == "slot":
        record["original_failed_slot"] = ["start", 0]
    elif change == "wrapper":
        record["wrapper_exit_status"] = 1
    elif change == "successful-replay":
        record["replayed_successful_calibrations"] = 1
    elif change == "missing-xml":
        record["raw_xml"] = []
    elif change == "omitted-xml":
        (case["xml"].parent / "TEST-unlisted.xml").write_text("<testsuite/>")
    elif change == "changed-report":
        (case["fresh"] / "original-report.json").write_text("{}")
    elif change == "duplicate-test":
        path = case["fresh"] / "original-report.json"
        report = json.loads(path.read_text())
        report["tests"].append(report["tests"][0])
        atomic_json(path, report)
        record["raw_report"] = ref(path)
    elif change == "changed-old-output":
        (case["old_dir"] / "start-0/0.stdout").write_text("different actual command output")
    elif change == "missing-old-slot":
        old = deepcopy(case["original"])
        old["runs"].pop(1)
        atomic_json(case["old_dir"] / "verification.json", old)
        record["original_failed_proof"] = ref(case["old_dir"] / "verification.json")
    elif change == "changed-policy":
        path = case["fresh"] / "original-report.json"
        report = json.loads(path.read_text())
        report["frozen_non_p2p_exit_codes"] = [1, 137]
        atomic_json(path, report)
        record["raw_report"] = ref(path)
    atomic_json(case["fresh"] / "result.json", record)
    with pytest.raises(ContractError):
        compose(case)


def test_extra_skipped_non_p2p_xml_is_not_hidden_by_all_fixed_tests_passing(tmp_path):
    case = make_case(tmp_path)
    tree = ET.parse(case["xml"])
    entry = ET.SubElement(tree.getroot(), "testcase", classname="Unexpected", name="skipped")
    ET.SubElement(entry, "skipped")
    tree.write(case["xml"])
    statuses_path = case["fresh"] / "observed-statuses.json"
    statuses = json.loads(statuses_path.read_text())
    statuses["Unexpected#skipped"] = "SKIPPED"
    atomic_json(statuses_path, statuses)
    report_path = case["fresh"] / "original-report.json"
    report = json.loads(report_path.read_text())
    report["observed_nonpassing"]["Unexpected#skipped"] = "SKIPPED"
    atomic_json(report_path, report)
    record = case["record"]
    record.update(raw_xml=[ref(case["xml"])], raw_report=ref(report_path),
                  observed_statuses=ref(statuses_path), observed_nonpassing=report["observed_nonpassing"])
    atomic_json(case["fresh"] / "result.json", record)
    with pytest.raises(ContractError, match="original policy"):
        compose(case)
