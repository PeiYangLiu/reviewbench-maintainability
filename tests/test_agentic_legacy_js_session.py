"""Original legacy-session binding controls; no benchmark executions."""
import copy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration.tsjs_legacy_session import COMMAND, DISCOURSE, read_original_inputs
from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash, file_hash


def fixture(tmp_path):
    task = {"id": DISCOURSE, "repo": "discourse/discourse", "start_sha": "a" * 40, "target_sha": "b" * 40}
    oracle = {"id": DISCOURSE, "historical_node": "20.19.5"}
    logs = []
    for phase in ("start", "golden"):
        for number in (1, 2, 3):
            path = tmp_path / (phase + str(number) + ".log")
            path.write_text("Test Files 1 passed (1)\nTests 2 passed (2)\n")
            logs.append({"path": str(path), "sha256": file_hash(path), "phase": phase, "run": number})
    private = []
    for index, name in enumerate(["session/checklist.behavior.test.js", "session/vitest.config.js"]):
        path = tmp_path / ("private" + str(index))
        path.write_text("ORIGINAL_PRIVATE_FIXTURE_" + str(index))
        private.append({"path": str(path), "destination": name, "sha256": file_hash(path)})
    record = {
        "schema_version": "reviewbench-original-legacy-js-inputs/v1",
        "status": "original_runtime_inputs_ready_without_reinstall", "id": DISCOURSE, "full_scope": 402,
        "task_fingerprint": canonical_hash(task), "oracle_fingerprint": canonical_hash(oracle),
        "node_version": "20.19.5", "signature": "vitest:files=1,tests=2", "effective_test_argv": COMMAND,
        "new_install_calibration_model_judge_calls": 0, "original_logs": logs, "private_files": private,
    }
    path = tmp_path / "inputs.json"
    path.write_text(json.dumps(record))
    return task, oracle, record, path


def pointer(path):
    return {"path": str(path), "sha256": file_hash(path)}


def test_complete_original_binding_and_coverage_flags_are_preserved(tmp_path):
    task, oracle, record, path = fixture(tmp_path)
    assert read_original_inputs(task, oracle, pointer(path)) == record
    assert COMMAND == ["node", "/workspace/node_modules/vitest/vitest.mjs", "run",
                       "--config", "session/vitest.config.js", "--coverage"]


@pytest.mark.parametrize("field,value", [
    ("full_scope", 1), ("node_version", "22.0.0"), ("signature", "vitest:files=1,tests=1"),
    ("task_fingerprint", "0" * 64), ("oracle_fingerprint", "0" * 64),
    ("new_install_calibration_model_judge_calls", 1), ("status", "prepared"),
    ("effective_test_argv", ["node", "vitest.mjs", "run"]),
])
def test_relabelled_or_reduced_original_contracts_are_rejected(tmp_path, field, value):
    task, oracle, record, path = fixture(tmp_path)
    record[field] = value
    path.write_text(json.dumps(record))
    with pytest.raises(ContractError):
        read_original_inputs(task, oracle, pointer(path))


@pytest.mark.parametrize("change", ["missing", "duplicate", "bytes", "count"])
def test_all_six_original_completed_logs_are_required(tmp_path, change):
    task, oracle, record, path = fixture(tmp_path)
    if change == "missing":
        record["original_logs"].pop()
    elif change == "duplicate":
        record["original_logs"][-1] = copy.deepcopy(record["original_logs"][0])
    elif change == "bytes":
        Path(record["original_logs"][0]["path"]).write_text("changed")
    else:
        p = Path(record["original_logs"][0]["path"])
        p.write_text("Test Files 1 passed (1)\nTests 1 passed (1)\n")
        record["original_logs"][0]["sha256"] = file_hash(p)
    path.write_text(json.dumps(record))
    with pytest.raises(ContractError):
        read_original_inputs(task, oracle, pointer(path))


@pytest.mark.parametrize("change", ["bytes", "mapping", "record-hash"])
def test_private_inputs_are_exact_and_separate_from_source_metadata(tmp_path, change):
    task, oracle, record, path = fixture(tmp_path)
    if change == "bytes":
        Path(record["private_files"][0]["path"]).write_text("weakened")
    elif change == "mapping":
        record["private_files"][0]["destination"] = "another-test.js"
        path.write_text(json.dumps(record))
    ref = pointer(path)
    if change == "record-hash":
        ref["sha256"] = "0" * 64
    with pytest.raises(ContractError):
        read_original_inputs(task, oracle, ref)
