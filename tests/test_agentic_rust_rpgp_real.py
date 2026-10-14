"""Real offline Cargo controls for original committed seeds and unlocked tests."""
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_image_entry as entry
from test_agentic_rust_image_real import cargo_fixture


def real_fixture(tmp_path, monkeypatch, version):
    variables = ("RPGP_TEST_CARGO", "RPGP_TEST_RUSTC")
    if all(os.environ.get(name) is None for name in variables):
        pytest.skip("set RPGP_TEST_CARGO/RPGP_TEST_RUSTC to the original installed toolchain")
    # An explicit but incomplete or invalid opt-in is a setup failure, not a skip.
    for name in variables:
        value = os.environ.get(name)
        if not value or not Path(value).is_file() or not os.access(value, os.X_OK):
            pytest.fail(f"configured RPGP integration requires an executable {name}: {value!r}")
    monkeypatch.setenv("IMAGE_TEST_CARGO", os.environ["RPGP_TEST_CARGO"])
    monkeypatch.setenv("IMAGE_TEST_RUSTC", os.environ["RPGP_TEST_RUSTC"])
    source, public, private = cargo_fixture(tmp_path, monkeypatch, version)
    public.update(schema_version=entry.REFERENCE_SCHEMA, package="image",
                  public_command=["check", "--locked", "-p", "image"],
                  reference_seed_sha256=public["locks"][0]["sha256"],
                  reference_seed_policy="replace-only-identical-committed-seed-in-private-mirror")
    private["command"].remove("--locked")
    private["command"][1:1] = ["-p", "image"]
    private["original_unlocked"] = True
    (source / "Cargo.lock").write_bytes(Path(public["locks"][0]["path"]).read_bytes())
    return source, public, private


@pytest.mark.parametrize("version", ["0.1.0", "0.2.0"])
def test_real_reference_lock_preserves_source_and_original_unlocked_test(tmp_path, monkeypatch, version):
    source, public, private = real_fixture(tmp_path, monkeypatch, version)
    before = entry.snapshot(source)
    result = entry.run_candidate(public, source, tmp_path / "build", private)
    assert result["exit_status"] == 0 and result["test_results"] == {"original_behavior": True}
    assert result["source_before"] == result["source_after"] == before
    assert result["candidate_has_original_reference_seed"] and len(result["probes"]) == 2
    assert "--locked" not in result["execution"]["command"]
    assert result["execution"]["command"][-len(private["command"]):] == private["command"]


def test_real_candidate_authored_environment_lock_is_preserved(tmp_path, monkeypatch):
    source, public, private = real_fixture(tmp_path, monkeypatch, "0.2.0")
    (source / "Cargo.lock").write_bytes(Path(public["locks"][1]["path"]).read_bytes())
    before = entry.snapshot(source)
    result = entry.run_candidate(public, source, tmp_path / "build", private)
    assert result["exit_status"] == 0 and result["candidate_lock_preserved"]
    assert not result["candidate_has_original_reference_seed"] and len(result["probes"]) == 1
    assert result["mirror_before"] == result["mirror_after"] == before


def test_real_assertion_failure_is_not_a_lock_selection_signal(tmp_path, monkeypatch):
    source, public, private = real_fixture(tmp_path, monkeypatch, "0.2.0")
    (source / "src/lib.rs").write_text("pub fn value() -> u8 { 9 }\n")
    result = entry.run_candidate(public, source, tmp_path / "build", private)
    assert result["exit_status"] == 101 and result["test_results"] == {"original_behavior": False}
    assert len(result["probes"]) == 2 and result["command_executions"] == 1
