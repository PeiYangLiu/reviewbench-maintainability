"""Real offline Cargo controls, separate from the original Image SUT calibration."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_image_entry as entry


def ref(path):
    return {"path": str(path), "sha256": entry.digest(path.read_bytes())}


def cargo_fixture(tmp_path, monkeypatch, version="0.1.0"):
    cargo = os.environ.get("IMAGE_TEST_CARGO")
    rustc = os.environ.get("IMAGE_TEST_RUSTC")
    if not cargo or not rustc:
        pytest.skip("IMAGE_TEST_CARGO/IMAGE_TEST_RUSTC bind the original installed toolchain")
    cargo, rustc = Path(cargo), Path(rustc)
    vendor = tmp_path / "vendor"
    for version_number, checksum in (("0.1.0", "1" * 64), ("0.2.0", "2" * 64)):
        folder = vendor / ("dep-" + version_number)
        (folder / "src").mkdir(parents=True)
        (folder / "Cargo.toml").write_text(
            '[package]\nname="dep"\nversion="' + version_number + '"\nedition="2021"\n')
        (folder / "src/lib.rs").write_text("pub fn value() -> u8 { 1 }\n")
        (folder / ".cargo-checksum.json").write_text(json.dumps({
            "package": checksum,
            "files": {name: entry.digest((folder / name).read_bytes()) for name in ("Cargo.toml", "src/lib.rs")},
        }))
    source_config = tmp_path / "sources.toml"
    source_config.write_text('[source.crates-io]\nreplace-with="unit-vendor"\n'
                             '[source.unit-vendor]\ndirectory=' + json.dumps(str(vendor)) + "\n")
    for name in ("home", "cargo-home", "target"):
        (tmp_path / name).mkdir()
    for name, value in {
        "HOME": str(tmp_path / "home"), "CARGO_HOME": str(tmp_path / "cargo-home"),
        "RUSTC": str(rustc), "CARGO_NET_OFFLINE": "true", "CARGO_BUILD_JOBS": "2",
        "CARGO_TARGET_DIR": str(tmp_path / "target"), "CARGO_TERM_COLOR": "never",
        "RUSTUP_AUTO_INSTALL": "0",
    }.items():
        monkeypatch.setenv(name, value)
    locks = []
    for version_number in ("0.1.0", "0.2.0"):
        source = tmp_path / ("reference-" + version_number)
        (source / "src").mkdir(parents=True)
        (source / "Cargo.toml").write_text(
            '[package]\nname="image"\nversion="0.25.9"\nedition="2021"\n'
            '[dependencies]\ndep="=' + version_number + '"\n')
        (source / "src/lib.rs").write_text("pub fn value() -> u8 { dep::value() }\n")
        command = [str(cargo), "--offline", "--config", str(source_config), "generate-lockfile"]
        result = subprocess.run(command, cwd=source, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        locks.append(ref(source / "Cargo.lock"))
    source = tmp_path / "candidate"
    shutil.copytree(tmp_path / ("reference-" + version), source)
    (source / "Cargo.lock").unlink()
    (source / "tests").mkdir()
    harness = source / "tests/oracle_pnm.rs"
    harness.write_text("#[test]\nfn original_behavior() { assert_eq!(image::value(), 1); }\n")
    for name in ("fuzz", "fuzz-afl"):
        (source / name / "src").mkdir(parents=True)
        (source / name / "src/main.rs").write_text("fn main() {}\n")
        (source / name / "Cargo.toml").write_text(
            '[package]\nname="' + name + '"\nversion="0.1.0"\nedition="2021"\n[workspace]\n')
    public = {
        "schema_version": entry.SCHEMA, "task_id": "offline-fixture-image",
        "selection": entry.SELECTION, "workdir": ".", "lock_path": "Cargo.lock",
        "locks": locks, "cargo": ref(cargo), "rustc": ref(rustc), "source_config": ref(source_config),
        "cpus": 2, "timeout_seconds": 60, "metadata_timeout_seconds": 30, "max_log_bytes": 1024 * 1024,
    }
    private = {
        "schema_version": entry.PRIVATE_SCHEMA, "task_id": public["task_id"],
        "harness": {"path": "tests/oracle_pnm.rs", "sha256": entry.digest(harness.read_bytes())},
        "command": ["test", "--locked", "--test", "oracle_pnm", "original_behavior",
                    "--", "--nocapture", "--test-threads=1"],
        "expected_tests": ["original_behavior"],
    }
    return source, public, private


@pytest.mark.parametrize("version", ["0.1.0", "0.2.0"])
def test_real_cargo_selects_unique_graph_and_runs_original_command_once(tmp_path, monkeypatch, version):
    source, public, private = cargo_fixture(tmp_path, monkeypatch, version)
    before = entry.snapshot(source)
    record = entry.run_candidate(public, source, tmp_path / "build", private)
    assert record["status"] == "completed" and record["exit_status"] == 0
    assert record["test_results"] == {"original_behavior": True}
    assert sorted(p["command_result"]["returncode"] for p in record["probes"]) == [0, 101]
    assert record["command_executions"] == 1
    assert record["graph"]["packages"] == record["graph"]["nodes"] == 2
    assert record["graph"]["all_manifest_files"] == 3
    assert record["source_before"] == record["source_after"] == before
    assert record["source_unchanged"] and record["mirror_unchanged"]
    assert record["execution"]["command"][-len(private["command"]):] == private["command"]
    assert "1 passed; 0 failed;" in record["execution"]["stdout"]


def test_real_original_assertion_failure_is_not_retried_with_other_lock(tmp_path, monkeypatch):
    source, public, private = cargo_fixture(tmp_path, monkeypatch)
    (source / "src/lib.rs").write_text("pub fn value() -> u8 { 9 }\n")
    before = entry.snapshot(source)
    record = entry.run_candidate(public, source, tmp_path / "build", private)
    assert record["status"] == "completed" and record["exit_status"] == 101
    assert record["test_results"] == {"original_behavior": False}
    assert len(record["probes"]) == 2 and record["command_executions"] == 1
    assert "0 passed; 1 failed;" in record["execution"]["stdout"]
    assert entry.snapshot(source) == before


def test_real_candidate_lock_with_new_local_helper_is_preserved(tmp_path, monkeypatch):
    source, public, private = cargo_fixture(tmp_path, monkeypatch)
    with (source / "Cargo.toml").open("a") as stream:
        stream.write('helper={path="helper"}\n')
    (source / "helper/src").mkdir(parents=True)
    (source / "helper/Cargo.toml").write_text('[package]\nname="helper"\nversion="0.1.0"\nedition="2021"\n')
    (source / "helper/src/lib.rs").write_text("pub fn value() -> u8 { 1 }\n")
    (source / "src/lib.rs").write_text("pub fn value() -> u8 { helper::value() }\n")
    command = [public["cargo"]["path"], "--offline", "--config", public["source_config"]["path"],
               "generate-lockfile"]
    result = subprocess.run(command, cwd=source, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    before = entry.snapshot(source)
    record = entry.run_candidate(public, source, tmp_path / "build", private)
    assert record["exit_status"] == 0 and record["candidate_lock_preserved"]
    assert len(record["probes"]) == 1 and record["graph"]["packages"] == 3
    assert record["source_before"] == record["mirror_before"] == record["mirror_after"] == before


def test_real_standalone_packaged_entry_emits_complete_execution_evidence(tmp_path, monkeypatch):
    source, public, private = cargo_fixture(tmp_path, monkeypatch)
    tools = tmp_path / "tools"
    tools.mkdir()
    origin = Path(entry.__file__).parent
    shutil.copyfile(origin / "rust_image_entry.py", tools / "image_locks.py")
    shutil.copyfile(origin / "rust_build_entry.py", tools / "build.py")
    for name, value in (("public.json", public), ("private.json", private)):
        (tmp_path / name).write_text(json.dumps(value))
    command = [sys.executable, "-B", str(tools / "image_locks.py"),
               "--spec", str(tmp_path / "public.json"), "--private", str(tmp_path / "private.json"),
               "--task-id", public["task_id"], "--source", str(source), "--build-root", str(tmp_path / "build")]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    rows = [json.loads(line[len(entry.EVIDENCE_PREFIX):]) for line in result.stdout.splitlines()
            if line.startswith(entry.EVIDENCE_PREFIX)]
    assert len(rows) == 1 and rows[0]["test_results"] == {"original_behavior": True}
    for probe in rows[0]["probes"]:
        entry.complete(probe["command_result"])
    entry.complete(rows[0]["execution"])
    assert rows[0]["source_unchanged"] and rows[0]["mirror_unchanged"]


@pytest.mark.parametrize("program,deadline,limit,reason", [
    ("import time; time.sleep(5)", 0.15, 1024, "deadline"),
    ("print('X'*10000)", 5, 1024, "output_limit"),
])
def test_actual_child_timeout_and_output_limits_are_terminal_not_compatibility(tmp_path, program, deadline, limit, reason):
    row = entry.invoke([sys.executable, "-c", program], tmp_path, dict(os.environ),
                       tmp_path, "bounded", time.monotonic() + deadline, limit)
    assert row["limit"] == reason
    with pytest.raises(entry.VariantError):
        entry.complete(row)
