"""Pinned nested-module admission controls; no original model or replay is run."""
from copy import deepcopy
import json

import pytest

from test_agentic_bootstrap_recovery import failed as root_failed
import recover_agentic_bootstrap as recovery
from reviewbench_maintainability.agentic.contracts import ContractError, atomic_json, canonical_hash, file_hash


@pytest.fixture
def nested(root_failed, monkeypatch):
    f = root_failed
    f.task.update(id="nested-fixture", repo="fixture/nested", start_sha="1"*40)
    generation = f.row["generation"]
    generation.update(id=f.task["id"], start_sha=f.task["start_sha"],
                      task_fingerprint=canonical_hash(f.task),
                      error="InfrastructureError: bootstrap changed original source: bigtable/go.mod")
    f.row["id"] = f.task["id"]
    atomic_json(f.root/"episode/result.json", generation)
    atomic_json(f.root/"pair-result.json", f.row)
    atomic_json(f.root/"episode/terminal-archive.json", {"binding":{
        "result_sha256":file_hash(f.root/"episode/result.json"), "trace_sha256":None, "patch_sha256":None}})
    f.selection.update(id=f.task["id"], profile="bigtable6314/nested-shadow-first/v1",
                       source_path="bigtable/go.mod", source_generation_fingerprint=canonical_hash(generation),
                       source_result_sha256=file_hash(f.root/"episode/result.json"),
                       repaired_runtime_fingerprint="new-runtime", repaired_proof_sha256="a"*64)
    pins = {**f.selection, "repo":f.task["repo"], "start_sha":f.task["start_sha"],
            "engine_fingerprint":"fixture-engine", "task_fingerprint":canonical_hash(f.task),
            "source_pair_sha256":file_hash(f.root/"pair-result.json"),
            "source_archive_sha256":file_hash(f.root/"episode/terminal-archive.json")}
    monkeypatch.setattr(recovery, "BIGTABLE_ZERO_CALL", pins)
    return f


def validate(f):
    return recovery.validate_zero_call_failure(f.row, f.root, f.task, f.config, f.selection, "fixture-engine")


def test_exact_nested_profile_preserves_original_inputs(nested):
    files = [nested.root/"pair-result.json", nested.root/"episode/result.json",
             nested.root/"episode/terminal-archive.json"]
    before = [path.read_bytes() for path in files]
    assert validate(nested) == nested.row["generation"]
    assert [path.read_bytes() for path in files] == before


@pytest.mark.parametrize("key,value", [
    ("profile", "automatic-nested"),
    ("source_path", "other/go.mod"),
    ("source_path", "bigtable/../go.mod"),
    ("repaired_runtime_fingerprint", "unreviewed-runtime"),
    ("repaired_proof_sha256", "b"*64),
])
def test_nested_route_rejects_unreviewed_selection(nested, key, value):
    nested.selection[key] = value
    with pytest.raises(ContractError):
        validate(nested)


@pytest.mark.parametrize("key,value", [
    ("id", "other-task"), ("repo", "other/repo"), ("start_sha", "f"*40),
    ("task_instruction", "changed original task"),
])
def test_nested_route_does_not_license_other_tasks(nested, key, value):
    nested.task[key] = value
    with pytest.raises(ContractError):
        validate(nested)


@pytest.mark.parametrize("name", ["pair-result.json", "episode/terminal-archive.json"])
@pytest.mark.parametrize("change", ["bytes", "missing", "symlink"])
def test_nested_route_requires_original_pair_and_archive_bytes(nested, name, change):
    path = nested.root/name
    data = path.read_bytes()
    if change == "bytes":
        path.write_bytes(data+b" ")
    elif change == "missing":
        path.unlink()
    else:
        path.unlink()
        other = path.with_suffix(".original")
        other.write_bytes(data)
        path.symlink_to(other)
    with pytest.raises(ContractError):
        validate(nested)


@pytest.mark.parametrize("relative", [
    "episode/trajectory.json", "episode/candidate.patch",
    "episode/retry-001/trajectory.json", "episode-attestation-old/trajectory.json",
])
def test_nested_failure_never_overrides_any_original_answer_or_uncertain_request(nested, relative):
    atomic_json(nested.root/relative, {"pending_model_request":True})
    with pytest.raises(ContractError):
        validate(nested)


@pytest.mark.parametrize("field,value", [
    ("usage", {"model_calls":0}), ("usage", {"model_calls":1}),
    ("status", "generated"), ("status", "generation_failed"),
    ("trace_sha256", "c"*64), ("patch_sha256", "d"*64), ("apply_ok", True),
])
def test_nested_admission_keeps_existing_zero_call_guards(nested, field, value):
    nested.row["generation"][field] = value
    with pytest.raises(ContractError):
        validate(nested)


def test_root_module_route_remains_unchanged(root_failed):
    f = root_failed
    assert recovery.validate_zero_call_failure(
        f.row, f.root, f.task, f.config, f.selection, "fixture-engine") == f.row["generation"]


def test_nested_profile_pins_cannot_be_changed_through_selection(nested):
    nested.selection.update(source_pair_sha256="0"*64, source_archive_sha256="0"*64)
    before = deepcopy(recovery.BIGTABLE_ZERO_CALL)
    assert validate(nested) == nested.row["generation"]
    assert recovery.BIGTABLE_ZERO_CALL == before
