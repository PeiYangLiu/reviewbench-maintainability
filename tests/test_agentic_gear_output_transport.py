"""Grader-only sharing keeps exact complete Cargo stdout and all execution data."""
import copy
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_gear_output_entry as output


def record(text, code=0):
    return {"command": ["/pinned/cargo", "--offline", "metadata"], "returncode": code,
            "stdout": text, "stdout_total_bytes": len(text.encode()), "stdout_captured_bytes": len(text.encode()),
            "stdout_sha256": hashlib.sha256(text.encode()).hexdigest(), "stdout_utf8_valid": True,
            "stderr": "unchanged stderr", "other_original_field": {"keep": True}}


def metadata(private=False):
    packages = [{"id": str(i), "name": "crate-" + str(i), "version": "1.0",
                 "dependencies": [{"name": "dependency", "req": "1"}],
                 "arbitrary": "♥ unicode metadata with long descriptive text " * 150} for i in range(12)]
    nodes = [{"id": str(i), "deps": [str((i + 1) % 12)], "features": ["std"]} for i in range(12)]
    if private:
        packages.insert(4, {"id": "private", "all_original_private_fields": ["retain"]})
        nodes.insert(4, {"id": "private", "deps": ["0"], "features": []})
    return json.dumps({"packages": packages, "resolve": {"nodes": nodes, "root": None},
                       "workspace_members": ["0", "private"] if private else ["0"],
                       "other": {"negative": -0.0}}, ensure_ascii=False) + "\n"


def fixture():
    return {"role": "grading", "schema_version": "unchanged-execution-schema",
            "probes": [{"command_result": record(metadata())},
                       {"command_result": record("", 101)}],
            "private_probes": [{"command_result": record(metadata(True))}],
            "executions": [{"command_result": {"stdout": "test original ... ok\n", "returncode": 0}}],
            "source_before": {"all": "original"}, "source_after": {"all": "original"},
            "test_results": {"original": True}, "exit_status": 0}


def test_exact_full_execution_round_trip_without_metadata_projection():
    original = fixture()
    preserved = copy.deepcopy(original)
    packed = output.pack(original, "a" * 64)
    assert original == preserved and output.unpack(packed) == original
    assert "stdout_reference" in packed["private_probes"][0]["command_result"]
    assert len(json.dumps(packed)) < len(json.dumps(original)) * .7
    assert packed["executions"] == original["executions"]
    assert packed["source_before"] == original["source_before"]


def test_whitespace_unicode_and_original_numeric_spelling_are_byte_exact():
    base = metadata().replace("-0.0", "1.234e+09")
    target = metadata(True).replace("-0.0", "1.234e+09").replace('"workspace_members":', '"workspace_members" : ')
    assert json.loads(base)["other"]["negative"] == 1234000000.0
    parts = output.share(base, target)
    assert output.expand(base, parts).encode() == target.encode()
    assert "1.234e+09" in output.expand(base, parts)


def test_multiple_complete_graphs_can_share_earlier_literal_stdout():
    original = fixture()
    original["probes"].append({"command_result": record(metadata().replace('"name": "crate-2"', '"name": "new-name"'))})
    original["private_probes"].append({"command_result": record(metadata(True))})
    assert output.unpack(output.pack(original, "b" * 64)) == original


@pytest.mark.parametrize("fault", ["base-digest", "negative", "outside", "cycle", "forward",
                                  "drop-part", "both-literal", "hash", "bytes", "role", "schema"])
def test_changed_or_ambiguous_transport_is_not_accepted(fault):
    packed = output.pack(fixture(), "a" * 64)
    row = packed["private_probes"][0]["command_result"]
    ref = row["stdout_reference"]
    if fault == "base-digest":
        ref["base_sha256"] = "b" * 64
    elif fault == "negative":
        ref["parts"][0] = [-1, 1]
    elif fault == "outside":
        ref["parts"][0] = [0, 100_000_000]
    elif fault == "cycle":
        ref["record"] = 2
    elif fault == "forward":
        ref["record"] = 5
    elif fault == "drop-part":
        ref["parts"].pop()
    elif fault == "both-literal":
        row["stdout"] = "alternate data"
    elif fault == "hash":
        row["stdout_sha256"] = "0" * 64
    elif fault == "bytes":
        row["stdout_total_bytes"] += 1
    elif fault == "role":
        packed["role"] = "public"
    else:
        packed["transport"]["schema_version"] = "different"
    with pytest.raises(ValueError):
        output.unpack(packed)


def test_public_model_facing_output_never_uses_grading_transport():
    original = fixture()
    original["role"] = "public"
    with pytest.raises(ValueError, match="public"):
        output.pack(original, "a" * 64)
    assert output.unpack(original) == original


def test_literal_stdout_binding_is_checked_before_sharing():
    original = fixture()
    original["probes"][0]["command_result"]["stdout"] += "changed"
    with pytest.raises(ValueError, match="stdout"):
        output.pack(original, "a" * 64)


@pytest.mark.parametrize("reference", [None, [], [True, 1], [1, 1], [0, 9, 10]])
def test_invalid_spans_reject(reference):
    with pytest.raises(ValueError):
        output.expand("text", [reference])
