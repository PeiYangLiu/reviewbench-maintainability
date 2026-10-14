"""Regress the real fresh-workspace/stale-Cargo-build-output failure."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_clarity_submodule as clarity
from reviewbench_maintainability.agentic.resources import CURRENT_JOB
from reviewbench_maintainability.agentic.snapshots import tree_files


def test_fresh_policy_touches_only_declared_current_input_and_excludes_only_generated_output():
    original = {"workdir": "/workspace", "bootstrap": ["original bootstrap"], "scratch_paths": [],
                "env": {"RUSTC": "original"}, "grading": {"commands": ["original command"]}}
    value = clarity.fresh_workspace_policy(original)
    assert value["bootstrap"] == ["original bootstrap", clarity.FRESH_BUILD]
    assert value["scratch_paths"] == [clarity.WASM]
    assert original["bootstrap"] == ["original bootstrap"] and original["scratch_paths"] == []
    assert value["env"] == original["env"] and value["grading"] == original["grading"]


def test_missing_or_relocated_candidate_wat_is_not_recreated(tmp_path):
    command = clarity.FRESH_BUILD.replace("/workspace", str(tmp_path))
    result = subprocess.run(["bash", "-c", command], capture_output=True, text=True)
    assert result.returncode == 0 and not (tmp_path / clarity.WAT).exists()


def test_only_generated_standard_wasm_is_excluded_from_candidate_files(tmp_path):
    for name in (clarity.WAT, clarity.WASM, "clar2wasm/src/helper.wasm", "clar2wasm/src/helper.rs"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)
    token = CURRENT_JOB.set({"workspace_exclusions": {str(tmp_path.resolve()): [clarity.WASM]}})
    try:
        assert set(tree_files(tmp_path)) == {
            clarity.WAT, "clar2wasm/src/helper.wasm", "clar2wasm/src/helper.rs"}
    finally:
        CURRENT_JOB.reset(token)


def test_real_cargo_reexecutes_original_build_script_in_fresh_workspaces(tmp_path, monkeypatch):
    cargo, rustc = os.environ.get("CLARITY_TEST_CARGO"), os.environ.get("CLARITY_TEST_RUSTC")
    if not cargo or not rustc:
        pytest.skip("CLARITY_TEST_CARGO and CLARITY_TEST_RUSTC select the original1.73 compiler")
    template = tmp_path / "template"
    crate = template / "clar2wasm"
    (crate / "src/standard").mkdir(parents=True)
    (template / "Cargo.toml").write_text('[workspace]\nmembers=["clar2wasm"]\nresolver="2"\n')
    (crate / "Cargo.toml").write_text(
        '[package]\nname="clar2wasm"\nversion="0.1.0"\nedition="2021"\nbuild="build.rs"\n')
    (crate / "build.rs").write_text(
        'fn main(){let b=std::fs::read("src/standard/standard.wat").unwrap();'
        'std::fs::write("src/standard/standard.wasm",b).unwrap();'
        'println!("cargo:rerun-if-changed=src/standard/standard.wat");}\n')
    (crate / "src/lib.rs").write_text(
        'pub const GENERATED:&[u8]=include_bytes!("standard/standard.wasm");\n'
        '#[test] fn original_bytes(){assert_eq!(GENERATED,include_bytes!("standard/standard.wat"));}\n')
    (crate / "src/standard/standard.wat").write_bytes(b"original-fixture-input")
    for name in ("home", "cargo-home", "target"):
        (tmp_path / name).mkdir()
    env = dict(os.environ, RUSTC=rustc, CARGO_HOME=str(tmp_path / "cargo-home"),
               HOME=str(tmp_path / "home"), CARGO_TARGET_DIR=str(tmp_path / "target"),
               CARGO_NET_OFFLINE="true", CARGO_INCREMENTAL="0", CARGO_BUILD_JOBS="2")
    generated = subprocess.run([cargo, "generate-lockfile", "--offline"], cwd=template,
                               env=env, capture_output=True, text=True, timeout=30)
    assert generated.returncode == 0, generated.stderr
    workspace = tmp_path / "workspace"
    shutil.copytree(template, workspace)
    check = [cargo, "check", "--locked", "--offline", "-p", "clar2wasm"]
    test = [cargo, "test", "--locked", "--offline", "-p", "clar2wasm", "--lib", "--", "--nocapture"]
    first = subprocess.run(check, cwd=workspace, env=env, capture_output=True, text=True, timeout=60)
    assert first.returncode == 0, first.stderr
    assert (workspace / clarity.WASM).is_file()
    shutil.rmtree(workspace)
    shutil.copytree(template, workspace)
    baseline = subprocess.run(test, cwd=workspace, env=env, capture_output=True, text=True, timeout=60)
    assert baseline.returncode == 101 and "standard/standard.wasm" in baseline.stderr
    assert not (workspace / clarity.WASM).exists()
    original = (workspace / clarity.WAT).read_bytes()
    command = clarity.FRESH_BUILD.replace("/workspace", str(workspace))
    touched = subprocess.run(["bash", "-c", command], cwd=workspace, env=env, capture_output=True, text=True)
    assert touched.returncode == 0 and (workspace / clarity.WAT).read_bytes() == original
    modified = subprocess.run(test, cwd=workspace, env=env, capture_output=True, text=True, timeout=60)
    assert modified.returncode == 0 and "1 passed; 0 failed;" in modified.stdout, modified.stderr
    assert (workspace / clarity.WASM).read_bytes() == original
    # A later candidate can change the WAT input; the build still uses its bytes.
    shutil.rmtree(workspace)
    shutil.copytree(template, workspace)
    (workspace / clarity.WAT).write_bytes(b"candidate-input-with-changed-content")
    changed = (workspace / clarity.WAT).read_bytes()
    subprocess.run(["bash", "-c", command], cwd=workspace, env=env, check=True)
    result = subprocess.run(test, cwd=workspace, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0 and (workspace / clarity.WASM).read_bytes() == changed
