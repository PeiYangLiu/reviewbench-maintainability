"""Compose an unchanged runtime's retained successes and new failed-slot runs.

Never infer readiness from console counts or recover a missing XML inventory.
New executions must retain their actual original JSON, complete XML inventory
and command outputs. Old failed attempts remain referenced; successful old
slots are retained byte-for-byte and may not appear among the new executions.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
from pathlib import Path

from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash, file_hash
from reviewbench_maintainability.agentic.evaluation import parse_tests
from .jvm_lane import frozen_non_p2p_exit_codes, frozen_non_p2p_statuses
from .jvm_oracle_entry import accepted_process_result, read_statuses

SCHEMA = "reviewbench-append-only-calibration-continuation/v1"
SLOTS = [(phase, repetition) for phase in ("start", "golden") for repetition in range(3)]


def require(value, message):
    if not value:
        raise ContractError(message)


def pointer(path):
    return {"path": str(Path(path).resolve()), "sha256": file_hash(Path(path))}


def checked(pointer_value, root=None):
    require(isinstance(pointer_value, dict) and set(pointer_value) == {"path", "sha256"},
            "calibration continuation requires a complete file pointer")
    path = Path(pointer_value["path"])
    require(path.is_absolute() and not path.is_symlink() and path.is_file()
            and path.stat().st_size <= 64 * 1024**2
            and file_hash(path) == pointer_value["sha256"], "calibration evidence changed")
    if root:
        require(path.resolve().is_relative_to(Path(root).resolve()), "calibration evidence escaped its run")
    return path


def slot(row):
    require(isinstance(row, dict) and row.get("variant") in {"start", "golden"}
            and type(row.get("repetition")) is int and row["repetition"] in range(3),
            "invalid calibration slot")
    return row["variant"], row["repetition"]


def expected_tests(recipe):
    result = {}
    for index, spec in enumerate(recipe.grading["commands"]):
        require(spec["expected_tests"] and len(spec["expected_tests"]) == len(set(spec["expected_tests"])),
                "fixed test identities are absent or duplicated")
        result.update({f"{index}::{name}": True for name in spec["expected_tests"]})
    require(result, "empty calibration scope")
    return result


def audit_commands(directory, commands):
    directory = Path(directory)
    rows = [json.loads(path.read_text()) for path in directory.glob("*.json")]
    rows = [row for row in rows if "stdout_file" in row]
    require(Counter(row["command"] for row in rows) == Counter(commands), "original command set changed")
    for row in rows:
        require(row["status"] == "completed" and type(row["returncode"]) is int and row["returncode"] == 0
                and not row["timed_out"] and not row["log_limit_exceeded"],
                "calibration command did not complete successfully")
        for stream in ("stdout", "stderr"):
            name = row[stream + "_file"]
            require(Path(name).name == name, "command output escaped its directory")
            path = directory / name
            require(not path.is_symlink() and path.is_file()
                    and file_hash(path) == row[stream + "_sha256"], "original command output changed")
    return rows


def compose_readiness(task, oracle, recipe, original_directory, diagnostic_files, engine):
    original_directory = Path(original_directory)
    original_path = original_directory / "verification.json"
    original = json.loads(original_path.read_text())
    identities = {
        "id": task["id"], "task_fingerprint": canonical_hash(task),
        "oracle_fingerprint": canonical_hash(oracle),
        "runtime_fingerprint": recipe.fingerprint, "engine_fingerprint": engine,
    }
    require(original.get("status") == "blocked"
            and all(original.get(key) == value for key, value in identities.items()),
            "original failed verification identity differs")
    all_tests = expected_tests(recipe)
    declared = oracle.get("pass_to_pass_tests")
    require(declared is None or sorted(declared) == sorted(
        name for spec in recipe.grading["commands"] for name in spec["expected_tests"]),
        "original Oracle fixed identities differ from the runtime")
    original_slots = [slot(row) for row in original["runs"]]
    require(len(original_slots) == len(set(original_slots)), "duplicate original calibration slot")
    require(original_slots == SLOTS[:len(original_slots)], "original verifier did not stop in its declared order")
    commands = [*recipe.bootstrap, *recipe.grading.get("bootstrap", []),
                *[row["command"] for row in recipe.grading["commands"]]]
    audit_commands(original_directory / "developer", [*recipe.bootstrap, *recipe.dev_checks])
    retained = {}
    for row in original["runs"]:
        require(isinstance(row["tests"], dict) and all(type(value) is bool for value in row["tests"].values()),
                "original calibration outcomes have invalid types")
        if row["tests"] and all(row["tests"].values()):
            require(row["tests"] == all_tests, "successful original calibration omitted fixed identities")
            key = slot(row)
            audit_commands(original_directory / f"{key[0]}-{key[1]}", commands)
            retained[key] = deepcopy(row)
    needed = set(SLOTS) - set(retained)
    require(needed and diagnostic_files, "no failed or missing calibration slot to continue")
    new, provenance = {}, []
    for supplied in diagnostic_files:
        supplied = Path(supplied).resolve()
        diagnostic = json.loads(supplied.read_text())
        directory = supplied.parent
        require(diagnostic.get("schema_version") == "reviewbench-original-failed-slot-diagnostic/v1"
                and diagnostic.get("status") == "diagnostic_completed"
                and diagnostic.get("full_scope") == 402
                and diagnostic.get("id") == task["id"]
                and diagnostic.get("engine_fingerprint") == engine
                and diagnostic.get("runtime_fingerprint") == recipe.fingerprint
                and diagnostic.get("wrapper_exit_status") == 0
                and diagnostic.get("replayed_successful_calibrations") == 0
                and diagnostic.get("new_failed_slot_executions") == 1
                and diagnostic.get("new_model_or_judge_calls") == 0,
                "new calibration is not an unchanged original failed-slot execution")
        require(all(type(diagnostic[key]) is int for key in (
            "wrapper_exit_status", "replayed_successful_calibrations",
            "new_failed_slot_executions", "new_model_or_judge_calls")),
            "new execution counters and exits must retain their original integer types")
        key = tuple(diagnostic["original_failed_slot"])
        require(len(key) == 2 and key in needed and key not in new,
                "a successful original slot was repeated or a fresh slot duplicated")
        original_reference = checked(diagnostic["original_failed_proof"])
        require(file_hash(original_reference) == file_hash(original_path),
                "new execution belongs to another failed verification")
        audit_commands(directory / "commands", commands)
        report_path = checked(diagnostic["raw_report"], directory)
        report = json.loads(report_path.read_text())
        require(len(recipe.grading["commands"]) == 1
                and recipe.grading["commands"][0]["parser"] == "json",
                "raw JVM continuation needs its single original JSON report")
        spec = recipe.grading["commands"][0]
        names = spec["expected_tests"]
        require(Counter(row["id"] for row in report["tests"]) == Counter(names),
                "fresh report omitted or duplicated fixed identities")
        xml_paths = [checked(row, directory / "original-xml") for row in diagnostic["raw_xml"]]
        require(xml_paths and len(set(xml_paths)) == len(xml_paths), "complete raw XML inventory is required")
        actual = {path.resolve() for path in (directory / "original-xml").rglob("*")
                  if path.is_file() or path.is_symlink()}
        require(set(xml_paths) == actual, "raw XML inventory omitted an additional report")
        observed = read_statuses(xml_paths)
        observed_file = checked(diagnostic["observed_statuses"], directory)
        require(observed == json.loads(observed_file.read_text()), "raw XML and captured status map differ")
        nonpassing = {name: value for name, value in observed.items() if value != "PASSED"}
        require(nonpassing == report["observed_nonpassing"] == diagnostic["observed_nonpassing"],
                "non-P2P outcomes were omitted or replaced")
        allowed, codes = frozen_non_p2p_statuses(oracle), frozen_non_p2p_exit_codes(oracle)
        require(report.get("frozen_non_p2p_statuses") == allowed
                and report.get("frozen_non_p2p_exit_codes") == codes,
                "the original non-P2P policy changed")
        code = report["original_exit_code"]
        require(type(code) is int and code == diagnostic["original_subprocess_exit_status"]
                and accepted_process_result(code, observed, names, allowed, codes)
                and all(observed.get(name) == "PASSED" for name in names),
                "fresh raw outcomes are not accepted by the original policy")
        parsed = parse_tests("json", report_path.read_text(), command_ok=True, expected=names)
        require(parsed == diagnostic["tests"] and parsed and all(parsed.values()),
                "actual new measurement differs from the captured original report")
        new[key] = {
            "variant": key[0], "repetition": key[1],
            "tests": {"0::" + name: value for name, value in parsed.items()},
            "execution_provenance": {
                "kind": "new-original-failed-slot-execution-with-raw-reports",
                "record": pointer(supplied), "actual_engine_fingerprint": engine,
                "actual_runtime_fingerprint": recipe.fingerprint,
                "prior_failed_attempt_retained": pointer(original_path),
            },
        }
        provenance.append(pointer(supplied))
    require(set(new) == needed, "remaining original calibration slots are not all measured")
    result = deepcopy(original)
    result.update(status="ready", runs=[deepcopy(retained[key] if key in retained else new[key]) for key in SLOTS])
    result.pop("error", None)
    result["calibration_continuation"] = {
        "schema_version": SCHEMA, "original_failed_proof": pointer(original_path),
        "original_error": original.get("error"), "retained_successful_slots": [list(key) for key in SLOTS if key in retained],
        "new_execution_slots": [list(key) for key in SLOTS if key in new],
        "new_execution_records": provenance, "new_failed_slot_executions": len(new),
        "successful_calibration_replays": 0, "old_failed_attempts_preserved": True,
        "runtime_and_execution_engine_unchanged": True, "new_calls_by_composition": 0,
        "reader": pointer(Path(__file__)),
    }
    return result
