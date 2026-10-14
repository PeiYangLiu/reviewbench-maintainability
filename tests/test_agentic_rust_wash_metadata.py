"""Cargo metadata is data, not a compiler diagnostic stream."""
import copy
import json
from pathlib import Path
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_wash_compat_entry as entry


def graph_fixture(tmp_path):
    (tmp_path / "Cargo.toml").write_text('[package]\nname="wash"\nversion="0.1.0"\n')
    (tmp_path / "Cargo.lock").write_text("version = 3\n")
    graph = {
        "workspace_root": str(tmp_path), "workspace_members": ["wash"],
        "packages": [
            {"id": "wash", "source": None, "manifest_path": str(tmp_path / "Cargo.toml")},
            {"id": "ctrlc", "source": "registry+https://github.com/rust-lang/crates.io-index",
             "keywords": ["ctrlc", "signal", "SIGINT"], "description": "Handles SIGINT and SIGTERM."},
        ],
        "resolve": {"nodes": [
            {"id": "wash", "dependencies": ["ctrlc"]}, {"id": "ctrlc", "dependencies": []},
        ]},
    }
    stdout = json.dumps(graph)
    record = {
        "returncode": 0, "limit": None, "stdout": stdout, "stderr": "",
        "stdout_total_bytes": len(stdout.encode()), "stdout_captured_bytes": len(stdout.encode()),
        "stderr_total_bytes": 0, "stderr_captured_bytes": 0,
    }
    spec = {"mode": "public", "command": ["cargo", "--offline", "check", "--locked"],
            "max_log_bytes": 32 * 1024**2}
    return spec, record


def verify(tmp_path, spec, record, callback=None):
    evidence, calls = {}, []
    def run(command, logs, name, deadline, max_log_bytes):
        calls.append((command, name, max_log_bytes))
        if callback:
            callback()
        return record
    entry.verify_graph(spec, tmp_path, tmp_path, time.monotonic() + 30, run, evidence)
    assert len(calls) == 1 and calls[0][1] == "graph"
    assert "--locked" in calls[0][0] and "--offline" in calls[0][0]
    assert evidence["graph"] is record
    return evidence


@pytest.mark.parametrize("mode", ["public", "grading"])
def test_legitimate_package_signal_keywords_preserve_full_raw_evidence(tmp_path, mode):
    spec, record = graph_fixture(tmp_path)
    if mode == "grading":
        spec.update(mode=mode, command=["cargo", "--offline", "test", "--locked", "--bin", "wash"])
    before = copy.deepcopy(record)
    evidence = verify(tmp_path, spec, record)
    assert evidence["graph"] == before
    assert "SIGINT" in evidence["graph"]["stdout"] and record == before


@pytest.mark.parametrize("code", [-9, -11, 124, 137, 139, 101])
def test_metadata_process_failure_is_not_ignored(tmp_path, code):
    spec, record = graph_fixture(tmp_path)
    record["returncode"] = code
    with pytest.raises(entry.SelectionError):
        verify(tmp_path, spec, record)


@pytest.mark.parametrize("mutation", ["deadline", "output_limit", "stdout_truncated", "stderr_truncated"])
def test_metadata_limits_and_complete_byte_capture_remain_mandatory(tmp_path, mutation):
    spec, record = graph_fixture(tmp_path)
    if mutation in {"deadline", "output_limit"}:
        record["limit"] = mutation
    else:
        record[mutation.removesuffix("_truncated") + "_total_bytes"] += 1
    with pytest.raises(entry.SelectionError, match="infrastructure"):
        verify(tmp_path, spec, record)


@pytest.mark.parametrize("text", [
    "process terminated by signal 9\n", "rustc interrupted by SIGSEGV\n",
    "internal compiler error: compiler panicked\n", "unaccounted stderr text\n",
])
def test_metadata_stderr_diagnostics_stay_strict(tmp_path, text):
    spec, record = graph_fixture(tmp_path)
    record.update(stderr=text, stderr_total_bytes=len(text), stderr_captured_bytes=len(text))
    with pytest.raises(entry.SelectionError):
        verify(tmp_path, spec, record)


@pytest.mark.parametrize("prefix,suffix", [("SIGINT\n", ""), ("", "\nSIGKILL"), ("", "\n{}")])
def test_unframed_stdout_diagnostics_are_not_accepted_as_metadata(tmp_path, prefix, suffix):
    spec, record = graph_fixture(tmp_path)
    record["stdout"] = prefix + record["stdout"] + suffix
    size = len(record["stdout"].encode())
    record.update(stdout_total_bytes=size, stdout_captured_bytes=size)
    with pytest.raises(entry.SelectionError, match="framing"):
        verify(tmp_path, spec, record)


def test_valid_json_without_complete_graph_still_rejected(tmp_path):
    spec, record = graph_fixture(tmp_path)
    record["stdout"] = '{"reason":"compiler-message","message":"SIGINT"}'
    record.update(stdout_total_bytes=len(record["stdout"]), stdout_captured_bytes=len(record["stdout"]))
    with pytest.raises(entry.SelectionError, match="complete workspace graph"):
        verify(tmp_path, spec, record)


def test_metadata_source_mutation_still_rejected(tmp_path):
    spec, record = graph_fixture(tmp_path)
    with pytest.raises(entry.SelectionError, match="mutated"):
        verify(tmp_path, spec, record, lambda: (tmp_path / "Cargo.lock").write_text("changed\n"))


def test_compiler_and_test_stdout_keep_unchanged_shared_signal_guard(tmp_path):
    _, record = graph_fixture(tmp_path)
    # The fix is deliberately confined to metadata framing. It does not
    # weaken FROST, build-script, compiler, or assertion-process diagnostics.
    with pytest.raises(entry.SelectionError, match="infrastructure"):
        entry.guard.infrastructure(record, {})
