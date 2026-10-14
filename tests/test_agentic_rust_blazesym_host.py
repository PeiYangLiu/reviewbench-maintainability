"""Original first-lock/native/dual-metadata input admission without new Cargo."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_blazesym as blaze
from reviewbench_maintainability.agentic.contracts import ContractError


def reference(path):
    data = path.read_bytes()
    return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def test_optin_is_separate_and_preserves_unselected_tasks():
    assert not blaze.requested(SimpleNamespace(), {"id": blaze.TASK})
    ctx = SimpleNamespace(config={"migration": {blaze.OPTION: {blaze.TASK: {}}}})
    assert blaze.requested(ctx, {"id": blaze.TASK})
    assert not blaze.requested(ctx, {"id": "other"})
    with pytest.raises(ContractError):
        blaze.requested(SimpleNamespace(config={"migration": {blaze.OPTION: {"other": {}}}}), {"id": "other"})


def test_original_command_and_all_derived_paths_stay_exact():
    assert blaze.COMMAND == ["test", "--test", "blazesym", "--", "--nocapture"]
    assert blaze.TESTS == ["symbolize_dwarf", "symbolize_gsym"]
    assert len(blaze.GENERATED) == 5
    assert "data/dwarf-example" not in blaze.GENERATED
    assert not set(blaze.GENERATED).intersection(blaze.FIXTURES)
    assert blaze.MANIFESTS["start"] != blaze.MANIFESTS["golden"]


@pytest.mark.parametrize("stderr,accepted", [
    (b"", True),
    (b"   Unpacking dep v1.2.3 (registry `/registry`)\n", True),
    (b"   Unpacking dep v1.2.3 (registry `/different`)\n", False),
    (b"   Unpacking dep v1.2.4 (registry `/registry`)\n", False),
    (b"   Unpacking different v1.2.3 (registry `/registry`)\n", False),
    (b"warning: selected a different graph\n", False),
    (b"error: failure\n", False),
    (b"   Unpacking dep v1.2.3 (registry `/registry`)\n" * 2, False),
])
def test_offline_unpacking_is_not_a_blanket_diagnostic_allowance(stderr, accepted):
    graph = {"packages": [{"name": "dep", "version": "1.2.3", "source": blaze.REGISTRY_SOURCE}]}
    assert blaze.metadata_stderr(stderr, graph, "/registry") is accepted


@pytest.fixture
def actual_inputs():
    name = os.environ.get("BLAZESYM_LOCAL_REGISTRY_INPUTS")
    if not name:
        pytest.skip("requires explicitly sealed original Blazesym inputs")
    return Path(name)


def test_actual_complete_inputs_and_dual_metadata_are_admitted(actual_inputs):
    checked = blaze.prepared_inputs(reference(actual_inputs))
    assert checked["locks"]["start"]["sha256"] == checked["locks"]["golden"]["sha256"] == blaze.LOCK_SHA
    assert len(checked["lock"]["package"]) == 56
    assert len(checked["runtime_manifest"]["packages"]) == 55
    for graph in checked["graphs"].values():
        assert graph["all"]["packages"] == 56
        assert graph["default"]["packages"] == 19
    assert checked["graphs"]["start"]["default"] == checked["graphs"]["golden"]["default"]
    assert checked["graphs"]["start"]["all"] != checked["graphs"]["golden"]["all"]


@pytest.mark.parametrize("change", ["task", "schema", "metadata_exit", "metadata_flags",
                                   "metadata_compiler", "manifest_members", "extra_index_package"])
def test_actual_inputs_reject_changed_admission_on_independent_json_copies(actual_inputs, tmp_path, change):
    value = json.loads(actual_inputs.read_text())
    if change == "task":
        value["task_id"] = "other"
    elif change == "schema":
        value["schema_version"] = "another/v1"
    else:
        key = "runtime_registry_manifest" if change in ("manifest_members", "extra_index_package") else None
        ref = value[key] if key else value["metadata"]["start"]["all"]
        data = json.loads(Path(ref["path"]).read_text())
        if change == "metadata_exit":
            data["exit_status"] = 1
        elif change == "metadata_flags":
            data["argv"].remove("--all-features")
        elif change == "metadata_compiler":
            data["argv"][0] = value["tools"]["1.68.2"]["cargo"]["path"]
        elif change == "manifest_members":
            data["packages"].pop()
        else:
            data["packages"].append(deepcopy(data["packages"][0]))
        copied = tmp_path / "changed-record.json"
        copied.write_text(json.dumps(data))
        if key:
            value[key] = reference(copied)
        else:
            value["metadata"]["start"]["all"] = reference(copied)
    copied = tmp_path / "envelope.json"
    copied.write_text(json.dumps(value))
    with pytest.raises(ContractError):
        blaze.prepared_inputs(reference(copied))
