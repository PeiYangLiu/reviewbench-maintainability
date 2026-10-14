"""Actual original Cargo1.60/1.64 controls; no original benchmark episodes."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from test_agentic_rust_cargo_contract_real import real_fixture
from test_agentic_rust_cargo_contract import MEMBER, MANIFEST
from test_agentic_rust_locked_workspace import locked
from test_agentic_rust_image import ref
from agentic_runtime_migration import rust_image_entry as entry


def fixture(tmp_path, monkeypatch, version="0.1.0", compiler="1.60.0"):
    source, public, private = locked(*real_fixture(tmp_path, monkeypatch, version))
    registry = os.environ.get("LOCKED_WORKSPACE_TOOLCHAINS")
    if not registry:
        pytest.skip("LOCKED_WORKSPACE_TOOLCHAINS pins original installed compilers")
    tool = Path(json.loads(Path(registry).read_text())[compiler]["root"])
    public["cargo"], public["rustc"] = ref(tool / "bin/cargo"), ref(tool / "bin/rustc")
    monkeypatch.setenv("RUSTC", str(tool / "bin/rustc"))
    return source, public, private


@pytest.mark.parametrize("version", ["0.1.0", "0.2.0"])
@pytest.mark.parametrize("compiler", ["1.60.0", "1.64.0"])
def test_real_original_compilers_keep_locked_command_and_all_source(tmp_path, monkeypatch, version, compiler):
    source, public, private = fixture(tmp_path, monkeypatch, version, compiler)
    before = entry.snapshot(source)
    result = entry.run_candidate(public, source, tmp_path / "build", private)
    assert result["exit_status"] == 0 and result["test_results"] == {"original_behavior": True}
    assert sorted(p["command_result"]["returncode"] for p in result["probes"]) == [0, 101]
    assert result["source_before"] == result["source_after"] == before
    assert result["command_executions"] == 1
    assert result["execution"]["command"][-len(private["command"]):] == private["command"]
    assert "--locked" in result["execution"]["command"] and "--config" not in result["execution"]["command"]


def test_real_failed_behavior_stays_negative_after_single_execution(tmp_path, monkeypatch):
    source, public, private = fixture(tmp_path, monkeypatch, "0.2.0")
    (source / MEMBER / "src/lib.rs").write_text("pub fn value() -> u8 { 9 }\n")
    result = entry.run_candidate(public, source, tmp_path / "build", private)
    assert result["exit_status"] == 101 and result["test_results"] == {"original_behavior": False}
    assert result["command_executions"] == 1 and len(result["probes"]) == 2


def test_real_candidate_lock_is_preserved_without_default_trials(tmp_path, monkeypatch):
    source, public, private = fixture(tmp_path, monkeypatch, "0.2.0")
    (source / "Cargo.lock").write_bytes(Path(public["locks"][1]["path"]).read_bytes())
    before = entry.snapshot(source)
    result = entry.run_candidate(public, source, tmp_path / "build", private)
    assert result["exit_status"] == 0 and result["candidate_lock_preserved"]
    assert len(result["probes"]) == 1 and result["source_after"] == result["mirror_after"] == before


def test_real_candidate_library_compile_error_is_not_an_environment_retry(tmp_path, monkeypatch):
    source, public, private = fixture(tmp_path, monkeypatch)
    (source / MEMBER / "src/lib.rs").write_text("pub fn value() -> u8 { unknown_function() }\n")
    result = entry.run_candidate(public, source, tmp_path / "build", private)
    assert result["exit_status"] == 101 and result["command_executions"] == 1
    assert "unknown_function" in result["execution"]["stderr"]


def test_real_member_may_not_disable_original_libtest(tmp_path, monkeypatch):
    source, public, private = fixture(tmp_path, monkeypatch)
    with (source / MANIFEST).open("a") as stream:
        stream.write('\n[[test]]\nname="oracle_pnm"\npath="tests/oracle_pnm.rs"\nharness=false\n')
    with pytest.raises(entry.VariantError, match="harness disabled"):
        entry.run_candidate(public, source, tmp_path / "build", private)
