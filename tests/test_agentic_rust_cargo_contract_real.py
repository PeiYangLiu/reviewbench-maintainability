"""Real Cargo1.70 verifies the complete nested-package selector without original SUTs."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from test_agentic_rust_image_real import cargo_fixture
from test_agentic_rust_cargo_contract import nested, MEMBER, MANIFEST
from agentic_runtime_migration import rust_image_entry as entry


def real_fixture(tmp_path, monkeypatch, version="0.1.0"):
    cargo, rustc = os.environ.get("WORKSPACE_TEST_CARGO"), os.environ.get("WORKSPACE_TEST_RUSTC")
    if not cargo or not rustc:
        pytest.skip("WORKSPACE_TEST_CARGO/RUSTC select the original1.70 compiler")
    monkeypatch.setenv("IMAGE_TEST_CARGO", cargo)
    monkeypatch.setenv("IMAGE_TEST_RUSTC", rustc)
    return nested(*cargo_fixture(tmp_path, monkeypatch, version))


@pytest.mark.parametrize("version", ["0.1.0", "0.2.0"])
def test_real_original_unlocked_nested_test_preserves_all_candidate_source(tmp_path, monkeypatch, version):
    source, public, private = real_fixture(tmp_path, monkeypatch, version)
    before = entry.snapshot(source)
    row = entry.run_candidate(public, source, tmp_path / "build", private)
    assert row["exit_status"] == 0 and row["test_results"] == {"original_behavior": True}
    assert row["candidate_has_original_reference_seed"] and len(row["probes"]) == 2
    assert row["source_before"] == row["source_after"] == before
    assert row["graph"]["local_manifests"] == [MANIFEST]
    assert row["execution"]["command"][-len(private["command"]):] == private["command"]


def test_real_changed_candidate_lock_is_used_without_default_probe(tmp_path, monkeypatch):
    source, public, private = real_fixture(tmp_path, monkeypatch, "0.2.0")
    (source / "Cargo.lock").write_bytes(Path(public["locks"][1]["path"]).read_bytes())
    row = entry.run_candidate(public, source, tmp_path / "build", private)
    assert row["exit_status"] == 0 and row["candidate_lock_preserved"] and len(row["probes"]) == 1


def test_real_assertion_failure_stays_negative(tmp_path, monkeypatch):
    source, public, private = real_fixture(tmp_path, monkeypatch)
    (source / MEMBER / "src/lib.rs").write_text("pub fn value() -> u8 { 9 }\n")
    row = entry.run_candidate(public, source, tmp_path / "build", private)
    assert row["exit_status"] == 101 and row["test_results"] == {"original_behavior": False}
    assert row["command_executions"] == 1 and len(row["probes"]) == 2


def test_real_member_cannot_disable_the_original_libtest_harness(tmp_path, monkeypatch):
    source, public, private = real_fixture(tmp_path, monkeypatch)
    with (source / MANIFEST).open("a") as stream:
        stream.write('\n[[test]]\nname="oracle_pnm"\npath="tests/oracle_pnm.rs"\nharness=false\n')
    with pytest.raises(entry.VariantError, match="harness disabled") as caught:
        entry.run_candidate(public, source, tmp_path / "build", private)
    assert caught.value.evidence["command_executions"] == 0


def test_real_standalone_workspace_entry_keeps_complete_raw_evidence(tmp_path, monkeypatch):
    source, public, private = real_fixture(tmp_path, monkeypatch)
    tools = tmp_path / "tools"
    tools.mkdir()
    origin = Path(entry.__file__).parent
    shutil.copyfile(origin / "rust_image_entry.py", tools / "workspace_locks.py")
    shutil.copyfile(origin / "rust_build_entry.py", tools / "build.py")
    for name, value in (("public.json", public), ("private.json", private)):
        (tmp_path / name).write_text(json.dumps(value))
    command = [sys.executable, "-B", str(tools / "workspace_locks.py"),
               "--spec", str(tmp_path / "public.json"), "--private", str(tmp_path / "private.json"),
               "--task-id", public["task_id"], "--source", str(source), "--build-root", str(tmp_path / "build")]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    rows = [json.loads(line[len(entry.EVIDENCE_PREFIX):]) for line in result.stdout.splitlines()
            if line.startswith(entry.EVIDENCE_PREFIX)]
    assert len(rows) == 1 and rows[0]["test_results"] == {"original_behavior": True}
    assert rows[0]["source_unchanged"] and rows[0]["mirror_unchanged"]
