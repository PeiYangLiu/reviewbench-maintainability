"""Retain five successful original slots and one measured PID-headroom recovery.

This is an explicit host composition, not a relabeled frozen six-run proof.
Only the PID/thread capacity bound changes; all executable inputs stay pinned.
"""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from reviewbench_maintainability.agentic.contracts import ContractError, RuntimeRecipe, canonical_hash
from . import solr_hibernate_identity as original

CASE = "github__apache__solr__pull_3433__comment_2231064740__modularity"
INPUT_KEY = "original_solr_composition_inputs"
KEY = "original_solr_resource_composition"
SCHEMA = "reviewbench-original-solr-five-plus-one-resource-composition/v1"
INPUT_SCHEMA = "reviewbench-original-solr-resource-composition-inputs/v1"
ENGINE = original.ENGINE
SLOTS = original.SLOTS
require, checked, read = original.require, original.checked, original.read


def pointer(path):
    path = Path(path)
    return {"path": str(path.resolve()), "sha256": original._digest(str(path), original.stamp(path))}


def command_record(directory, command):
    found = []
    for path in directory.glob("*.json"):
        value = json.loads(path.read_text())
        if isinstance(value, dict) and value.get("command") == command:
            found.append((path, value))
    require(len(found) == 1, "one exact fixed command per slot is required")
    return found[0]


def audit_slot(directory, artifact_directory, recipe, view, profile):
    original.audit_command_directory(
        directory, [*recipe.bootstrap, *recipe.grading.get("bootstrap", []), original.COMMAND])
    command_path, command = command_record(directory, original.COMMAND)
    report_path = artifact_directory / "results.json"
    report = read(pointer(report_path))
    expected = view["pass_to_pass_tests"]
    require(report.get("schema_version") == "reviewbench-exec-test-results/v1"
            and type(report.get("original_exit_code")) is int and report["original_exit_code"] == 0
            and type(report.get("observed_count")) is int and report["observed_count"] == 215
            and type(report.get("expected_count")) is int and report["expected_count"] == 215
            and report.get("original_command") == view["command"]
            and report.get("command") == original.original_argv(view["command"])
            and report.get("test_source_manifest_sha256") == profile["test_source_manifest"]["sha256"],
            "original execution command/count/source differs")
    require(report.get("tests") == [{"id": name, "status": "passed"} for name in expected]
            and report.get("observed_statuses") == {name: "PASSED" for name in expected}
            and report.get("failures") == [], "original fixed tests did not all pass")
    declared = report.get("raw_xml")
    require(type(declared) is list and declared, "complete original XML is required")
    files = []
    for row in declared:
        relative = Path(row["path"])
        require(not relative.is_absolute() and ".." not in relative.parts
                and relative.parts[0] == "original-xml", "XML inventory path escaped its slot")
        files.append(checked({"path": str(artifact_directory / relative), "sha256": row["sha256"]}))
    require(len(set(files)) == len(files)
            and set(files) == {p for p in (artifact_directory / "original-xml").rglob("*")
                              if p.is_file() or p.is_symlink()}, "original XML inventory was narrowed")
    raw = [entry for path in files for entry in original._xml_statuses(str(path), original.stamp(path))]
    require(len(raw) == 215 and len({entry[1] for entry in raw}) == 215
            and {entry[1] for entry in raw} == set(expected)
            and all(entry[2] is True for entry in raw), "raw tests were omitted, aliased, skipped or failed")
    require(report.get("raw_test_id_mapping") == [
        {"raw_id": first, "id": second, "status": "PASSED"} for first, second, passed in raw],
        "reported identities differ from original XML")
    return {
        "command": pointer(command_path), "raw_report": pointer(report_path),
        "raw_xml": [pointer(path) for path in files], "command_id": command["id"],
        "tests": {"0::" + name: True for name in expected},
    }


def composition(task, oracle, recipe):
    require(task.get("id") == CASE, "resource composition is bound to the original Solr task")
    reference = recipe.grading.get(INPUT_KEY)
    inputs = read(reference)
    require(inputs.get("schema_version") == INPUT_SCHEMA
            and type(inputs.get("full_scope")) is int and inputs["full_scope"] == 402,
            "unknown resource composition inputs")
    before = RuntimeRecipe.from_dict(read(inputs["original_recipe"]))
    executed = RuntimeRecipe.from_dict(read(inputs["executed_recipe"]))
    view, identity = original.execution_view(task, oracle, before)
    require(canonical_hash(read(inputs["source_task"])) == canonical_hash(task)
            and canonical_hash(read(inputs["source_oracle"])) == canonical_hash(oracle),
            "task/Oracle inputs differ")
    profile = read(before.grading[original.PROFILE_KEY])
    old, measured, final = asdict(before), asdict(executed), asdict(recipe)
    require({key for key in old if old[key] != measured[key]} == {"pids_limit"}
            and type(before.pids_limit) is int and before.pids_limit == 512
            and type(executed.pids_limit) is int and executed.pids_limit == 2048,
            "the measured change is not the bounded PID-only revision")
    desired = deepcopy(measured)
    desired["grading"][INPUT_KEY] = deepcopy(reference)
    require(final == desired, "admitted runtime changed an unmeasured executable input")
    require(inputs.get("retained_successful_slot_count") == 5
            and inputs.get("new_fixed_slot_executions") == 1
            and inputs.get("new_model_or_judge_calls") == 0
            and all(type(inputs.get(key)) is int for key in
                    ["retained_successful_slot_count", "new_fixed_slot_executions", "new_model_or_judge_calls"]),
            "execution allowance differs")
    proof_path = checked(inputs["original_proof"])
    old_proof = read(inputs["original_proof"])
    require(old_proof.get("status") == "blocked"
            and old_proof.get("engine_fingerprint") == ENGINE
            and old_proof.get("runtime_fingerprint") == before.fingerprint
            and old_proof.get("task_fingerprint") == canonical_hash(task)
            and old_proof.get("oracle_fingerprint") == canonical_hash(view),
            "original interrupted calibration identity differs")
    old_runs = old_proof.get("runs")
    require(type(old_runs) is list and len(old_runs) == 5, "exactly five retained successes are required")
    expected = {"0::" + name: True for name in view["pass_to_pass_tests"]}
    original.audit_command_directory(proof_path.parent / "developer", [*before.bootstrap, *before.dev_checks])
    slot_evidence, retained_paths = [], {}
    for run, (variant, repetition) in zip(old_runs, SLOTS[:5]):
        require(type(run.get("repetition")) is int and (run.get("variant"), run["repetition"]) == (variant, repetition)
                and run.get("tests") == expected and all(type(v) is bool for v in run["tests"].values()),
                "a retained original slot or result changed")
        directory = proof_path.parent / (variant + "-" + str(repetition))
        entry = audit_slot(directory, directory / "original-artifacts", before, view, profile)
        require(entry["tests"] == run["tests"], "retained raw outcomes differ from the frozen slot")
        slot_evidence.append({"variant": variant, "repetition": repetition, "origin": "retained-success", **entry})
        retained_paths.update({str(p): pointer(p)["sha256"] for p in directory.rglob("*")
                               if p.is_file() and not p.is_symlink()})
    recovery_path = checked(inputs["recovery_result"])
    recovery = read(inputs["recovery_result"])
    admission = read(inputs["recovery_input"])
    require(admission.get("original_proof") == inputs["original_proof"]
            and admission.get("original_runtime_fingerprint") == before.fingerprint
            and admission.get("new_runtime_fingerprint") == executed.fingerprint
            and admission.get("changed_fields") == ["pids_limit"]
            and admission.get("only_new_slot") == ["golden", 2]
            and admission.get("old_pids_limit") == 512 and admission.get("new_pids_limit") == 2048
            and admission.get("cpus_unchanged") == before.cpus
            and admission.get("memory_mb_unchanged") == before.memory_mb
            and admission.get("retained_successful_files") == retained_paths,
            "recovery does not preserve every original successful artifact")
    failed_path = checked(admission["original_failed_command"])
    failed = read(admission["original_failed_command"])
    require(failed_path.parent == proof_path.parent / "golden-2"
            and failed.get("command") == original.COMMAND
            and type(failed.get("returncode")) is int and failed["returncode"] != 0,
            "prior failed slot was removed or changed")
    for stream in ("stdout", "stderr"):
        checked({"path": str(failed_path.parent / failed[stream + "_file"]),
                 "sha256": failed[stream + "_sha256"]})
    require("pthread_create failed (EAGAIN)" in (failed_path.parent / failed["stdout_file"]).read_text(),
            "original thread-creation failure evidence is missing")
    require(recovery.get("status") == "original_failed_slot_passed" and recovery.get("id") == CASE
            and recovery.get("full_scope") == 402 and recovery.get("slot") == ["golden", 2]
            and recovery.get("original_runtime_fingerprint") == before.fingerprint
            and recovery.get("new_runtime_fingerprint") == executed.fingerprint
            and recovery.get("changed_fields") == ["pids_limit"] and recovery.get("error") is None
            and recovery.get("exit_statuses") == [0] * (len(executed.bootstrap) + 1)
            and recovery.get("tests") == recovery.get("passed") == 215
            and recovery.get("new_failed_slot_executions") == 1
            and recovery.get("replayed_successful_calibrations") == 0
            and recovery.get("new_model_or_judge_calls") == 0
            and recovery.get("retained_successful_file_hashes_unchanged") == len(retained_paths),
            "new execution is not the one preserved original failed slot")
    container = read(inputs["recovery_container"])
    require(container.get("pids_limit") == 2048 and container.get("memory_limit_bytes") == 8 * 1024**3,
            "actual recovery resource limits differ")
    samples_path = checked(inputs["resource_samples"])
    samples = [json.loads(line) for line in samples_path.read_text().splitlines()]
    require(samples and all(s.get("pids.max") == 2048 and s.get("memory.max") == 8 * 1024**3
                           and 0 <= s.get("pids.current", -1) <= 2048
                           and 0 <= s.get("memory.current", -1) <= 8 * 1024**3 for s in samples),
            "observed recovery bounds differ")
    require(max(s["pids.current"] for s in samples) == recovery["peak_observed_pids"]
            and max(s["memory.current"] for s in samples) == recovery["peak_observed_memory_bytes"],
            "resource observation summary differs")
    fresh_root = recovery_path.parent
    fresh = audit_slot(fresh_root / "commands", fresh_root / "original-artifacts", executed, view, profile)
    slot_evidence.append({"variant": "golden", "repetition": 2, "origin": "new-original-failed-slot", **fresh})
    require(len({row["command_id"] for row in slot_evidence}) == 6, "a successful execution was reused as a new slot")
    # The complete secondary XML capture must also match; it was copied even
    # when a grader reports malformed/duplicate outcomes.
    complete = {p.relative_to(fresh_root / "complete-raw-xml").as_posix(): pointer(p)["sha256"]
                for p in (fresh_root / "complete-raw-xml").rglob("*") if p.is_file() or p.is_symlink()}
    primary = {Path(p["path"]).relative_to(fresh_root / "original-artifacts/original-xml").as_posix(): p["sha256"]
               for p in fresh["raw_xml"]}
    require(complete == primary, "secondary original XML capture differs")
    host = read(inputs["host_directory_controller"])
    require(host.get("engine_fingerprint") == "32ccad74e14232da7218d560f2028533c66b61bab8270de334ec2dc468970143"
            and host.get("generation_engine_fingerprint") == ENGINE,
            "directory-aware fixed-input recovery plan changed")
    certificate = {
        "schema_version": SCHEMA, "inputs": deepcopy(reference), "original_count_identity": identity,
        "original_runtime_fingerprint": before.fingerprint, "measured_runtime_fingerprint": executed.fingerprint,
        "admitted_runtime_fingerprint": recipe.fingerprint,
        "resource_revision": {"field": "pids_limit", "before": 512, "after": 2048,
                              "cpu_and_memory_unchanged": True},
        "source_assertions_reference_commands_seed_policy_unchanged": True,
        "retained_successful_slots": 5, "new_original_failed_slot_executions": 1,
        "successful_calibration_replays": 0, "slots": slot_evidence,
        "prior_failed_execution": deepcopy(admission["original_failed_command"]),
        "original_frozen_proof": deepcopy(inputs["original_proof"]),
    }
    return {
        "id": CASE, "task_fingerprint": canonical_hash(task), "oracle_fingerprint": canonical_hash(view),
        "runtime_fingerprint": recipe.fingerprint, "engine_fingerprint": ENGINE, "status": "ready",
        "runs": [*deepcopy(old_runs), {"variant": "golden", "repetition": 2, "tests": deepcopy(fresh["tests"])}],
        KEY: certificate,
    }


def proof_identity_matches(proof, task, oracle, recipe):
    try:
        return canonical_hash(proof) == canonical_hash(composition(task, oracle, recipe))
    except (ContractError, KeyError, TypeError, ValueError, OSError, ET.ParseError):
        return False
