"""Actual Cargo1.60 proves full current source, original overlays and private libtests."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from test_agentic_rust_session_overlay import fixture as source_fixture, ref
from agentic_runtime_migration import rust_session_overlay_entry as entry
from agentic_runtime_migration.rust_build_entry import apply_in_place


def fixture(tmp_path, monkeypatch, version="0.1.0", grading=True):
    source, public, private, session = source_fixture(tmp_path, version, grading)
    registry = os.environ.get("LOCKED_WORKSPACE_TOOLCHAINS")
    if not registry:
        pytest.skip("LOCKED_WORKSPACE_TOOLCHAINS binds original Cargo1.60")
    tool = Path(json.loads(Path(registry).read_text())["1.60.0"]["root"])
    public["cargo"], public["rustc"] = ref(tool / "bin/cargo"), ref(tool / "bin/rustc")
    vendor = tmp_path / "vendor"
    for number, checksum in (("0.1.0", "a" * 64), ("0.2.0", "b" * 64)):
        path = vendor / ("dep-" + number)
        (path / "src").mkdir(parents=True)
        (path / "Cargo.toml").write_text('[package]\nname="dep"\nversion="' + number + '"\nedition="2021"\n')
        (path / "src/lib.rs").write_text("pub fn value() -> u8 { 1 }\n")
        (path / ".cargo-checksum.json").write_text(json.dumps({
            "package": checksum, "files": {name: entry.core.file_digest(path / name)
                                          for name in ("Cargo.toml", "src/lib.rs")}}))
    config = tmp_path / "sources.toml"
    config.write_text('[source.crates-io]\nreplace-with="fixture"\n[source.fixture]\ndirectory='
                      + json.dumps(str(vendor)) + "\n")
    public["source_config"] = ref(config)
    home = tmp_path / "generation-home"
    home.mkdir()
    shutil.copy2(config, home / "config.toml")
    for name, value in {"CARGO_HOME": str(home), "RUSTC": str(tool / "bin/rustc"),
                        "CARGO_TARGET_DIR": str(tmp_path / "target"), "CARGO_NET_OFFLINE": "true",
                        "CARGO_BUILD_JOBS": "2", "RUSTUP_AUTO_INSTALL": "0"}.items():
        monkeypatch.setenv(name, value)
    overlay = json.loads(Path(public["overlay"]["path"]).read_text())
    for number in ("0.1.0", "0.2.0"):
        root = tmp_path / ("original-" + number)
        shutil.copytree(source, root)
        for name, data in session.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(data)
        manifest = root / "production/Cargo.toml"
        manifest.write_text(manifest.read_text().replace('dep="=' + version + '"', 'dep="=' + number + '"'))
        apply_in_place(root, overlay)
        command = [str(tool / "bin/cargo"), "--offline", "generate-lockfile"]
        result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        row = next(row for row in public["locks"] if number in row["path"])
        Path(row["path"]).write_bytes((root / "Cargo.lock").read_bytes())
        row.update(ref(Path(row["path"])))
    return source, public, private


@pytest.mark.parametrize("version", ["0.1.0", "0.2.0"])
@pytest.mark.parametrize("grading", [False, True])
def test_real_cargo_locked_public_private_overlay_graphs(tmp_path, monkeypatch, version, grading):
    source, public, private = fixture(tmp_path, monkeypatch, version, grading)
    before = entry.core.snapshot(source)
    row = entry.run_candidate(public, source, tmp_path / "build", private if grading else None)
    assert row["exit_status"] == 0 and row["source_before"] == row["source_after"] == before
    assert row["graph"]["packages"] == (3 if grading else 2)
    assert sorted(p["command_result"]["returncode"] for p in row["probes"]) == [0, 101]
    assert row["command_executions"] == 1 and "ignored/src/lib.rs" in row["mirror_after"]
    if grading:
        assert row["test_results"] == {"tests::original_behavior": True}
        assert row["execution"]["command"][-len(private["command"]):] == private["command"]


def test_real_assertion_failure_does_not_reselect_lock(tmp_path, monkeypatch):
    source, public, private = fixture(tmp_path, monkeypatch)
    (source / "production/src/lib.rs").write_text("pub fn value() -> u8 { 9 }\n")
    row = entry.run_candidate(public, source, tmp_path / "build", private)
    assert row["exit_status"] == 101 and row["test_results"] == {"tests::original_behavior": False}
    assert row["command_executions"] == 1 and len(row["probes"]) == 2


def test_real_added_helper_is_compiled_and_a_later_deletion_is_not_hidden(tmp_path, monkeypatch):
    source, public, private = fixture(tmp_path, monkeypatch)
    (source / "production/src/lib.rs").write_text("mod helper;\npub fn value() -> u8 { helper::value() }\n")
    helper = source / "production/src/helper.rs"
    helper.write_text("pub fn value() -> u8 { 1 }\n")
    first = entry.run_candidate(public, source, tmp_path / "build", private)
    assert first["exit_status"] == 0 and "production/src/helper.rs" in first["mirror_after"]
    helper.unlink()
    second = entry.run_candidate(public, source, tmp_path / "build", private)
    assert second["exit_status"] == 101 and second["command_executions"] == 1
    assert "production/src/helper.rs" not in second["mirror_after"]


def test_real_candidate_lock_remains_authoritative(tmp_path, monkeypatch):
    source, public, private = fixture(tmp_path, monkeypatch, "0.2.0")
    content = Path(public["locks"][1]["path"]).read_bytes() + b"\n# candidate-owned\n"
    (source / "Cargo.lock").write_bytes(content)
    row = entry.run_candidate(public, source, tmp_path / "build", private)
    assert row["exit_status"] == 0 and row["candidate_lock_preserved"] and len(row["probes"]) == 1
    assert (source / "Cargo.lock").read_bytes() == content
