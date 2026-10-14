"""Dual retained Cargo on synthetic LOCAL TOOL crates, never original Blazesym.

The original harness/C/ld bytes are transported, but these synthetic crates do
not implement actual ELF/GSYM symbolization and are not benchmark evidence.
"""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys

import pytest

from test_agentic_rust_blazesym_runtime import blaze, entry, HARNESS_BYTES, lock_bytes

LABEL = "BLAZESYM_DUAL_CARGO_LOCAL_TOOL_NOT_ORIGINAL_SUT"
LIBRARY = r'''
mod current;
use std::path::PathBuf;
#[cfg(feature = "nightly")]
compile_error!("all-features must never compile this original-command TOOL");
pub enum SymbolizerFeature { LineNumberInfo(bool), DebugInfoSymbols(bool) }
pub enum SymbolSrcCfg {
    Gsym { file_name: PathBuf, base_address: u64 },
    Elf { file_name: PathBuf, base_address: u64 },
}
pub struct Symbol { pub symbol: String }
pub struct BlazeSymbolizer;
impl BlazeSymbolizer {
    pub fn new_opt(_: &[SymbolizerFeature]) -> Result<Self, &'static str> { Ok(Self) }
    pub fn symbolize(&self, sources: &[SymbolSrcCfg], addresses: &[u64]) -> Vec<Vec<Symbol>> {
        assert!(tool_used::bound());
        assert_eq!(addresses, &[0x2000100]);
        for source in sources {
            let path = match source {
                SymbolSrcCfg::Gsym { file_name, .. } => file_name,
                SymbolSrcCfg::Elf { file_name, .. } => file_name,
            };
            assert_eq!(std::fs::read(path).unwrap(), b"LOCAL TOOL generated fixture");
        }
        vec![vec![Symbol { symbol: crate::current::name().to_string() }]]
    }
}
'''
BUILD = r'''
fn main() {
    println!("cargo:rerun-if-changed=build.rs");
    if cfg!(feature="generate-test-files") && !cfg!(feature="dont-generate-test-files") {
        let root = std::path::PathBuf::from(std::env::var("SOURCE_ROOT").unwrap());
        let tool = std::env::var("LLVM_GSYMUTIL").unwrap();
        assert!(std::process::Command::new(tool).arg("--version").status().unwrap().success());
        for path in ["data/test-no-debug.bin", "data/test-dwarf-v4.bin",
                     "data/test-stable-addresses.bin", "data/test.gsym", "data/test-dwarf.bin"] {
            std::fs::write(root.join(path), b"LOCAL TOOL generated fixture").unwrap();
        }
        EXTRA
    }
}
'''


@pytest.fixture
def real_blaze(blaze, monkeypatch):
    f = blaze
    old, modern = os.environ.get("BLAZESYM_TEST_CARGO"), os.environ.get("BLAZESYM_TEST_METADATA_CARGO")
    if not old or not modern or not Path(old).is_file() or not Path(modern).is_file():
        pytest.skip("explicit retained Cargo1.68.2/1.93.0 required; never install tools")
    f.tool_setup = []
    for attribute, path, field, version in [
        ("REAL_CARGO", Path(old), "cargo_sha256", "cargo 1.68.2 "),
        ("REAL_RUSTC", Path(old).with_name("rustc"), "rustc_sha256", "rustc 1.68.2 "),
        ("METADATA_CARGO", Path(modern), "metadata_cargo_sha256", "cargo 1.93.0 "),
        ("METADATA_RUSTC", Path(modern).with_name("rustc"), "metadata_rustc_sha256", "rustc 1.93.0 "),
    ]:
        monkeypatch.setattr(entry, attribute, str(path))
        f.spec[field] = entry.file_hash(path)
        proc = subprocess.run([str(path), "--version"], cwd=f.source, capture_output=True, text=True, timeout=15)
        f.tool_setup.append({"command": [str(path), "--version"], "exit_status": proc.returncode,
                             "stdout": proc.stdout, "stderr": proc.stderr, "sha256": f.spec[field]})
        assert proc.returncode == 0 and proc.stdout.startswith(version), f.tool_setup[-1]
    native = {"cc": shutil.which("cc", path="/usr/bin:/bin"),
              "strip": shutil.which("strip", path="/usr/bin:/bin"),
              "llvm-gsymutil-14": str(f.tools / "llvm-gsymutil-14")}
    assert all(native.values()), "retained local linker/strip required; no installation"
    monkeypatch.setattr(entry, "NATIVE_PATHS", native)
    f.spec["native_tools"] = {name: {"path": path, "sha256": entry.file_hash(Path(path))}
                              for name, path in native.items()}
    f.tool_setup.append({"native_binding": f.spec["native_tools"],
                         "note": "host linker/strip plus explicitly synthetic unused-format TOOL shim, not original LLVM validation"})
    monkeypatch.setenv("CARGO_NET_OFFLINE", "true")
    monkeypatch.setenv("RUSTUP_AUTO_INSTALL", "0")
    monkeypatch.setenv("RUSTFLAGS", "")
    monkeypatch.setenv("CARGO_INCREMENTAL", "0")
    for name in ("RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "CARGO_BUILD_RUSTC_WRAPPER",
                 "CARGO_BUILD_RUSTC_WORKSPACE_WRAPPER"):
        monkeypatch.delenv(name, raising=False)
    (f.source / "src/lib.rs").write_text(LIBRARY)
    (f.source / "src/current.rs").write_text('pub fn name() -> &\'static str { "factorial" }\n')
    (f.source / "build.rs").write_text(BUILD.replace("EXTRA", ""))
    return f


def run_real(f, *, public=False):
    command = ["cargo", "--offline", "--config", entry.SOURCE_CONFIG, *(["check"] if public else entry.COMMAND)]
    return entry.execute(f.spec, f.source, f.build, command, None if public else f.private,
                         lock_source=f.default, registry=f.registry, registry_manifest=f.registry_manifest,
                         source_config=f.config, cargo_home=f.cargo_home, metadata_home=f.metadata_home)


def retain(f, report, name):
    folder = os.environ.get("BLAZESYM_FIXTURE_EVIDENCE")
    if folder:
        output = Path(folder)
        output.mkdir(parents=True, exist_ok=True)
        (output / (name + ".json")).write_text(json.dumps({
            "record_label": LABEL, "runtime_report": report, "tool_setup": f.tool_setup,
            "original_harness_sha256": entry.sha(HARNESS_BYTES),
            "original_Blazesym_SUT_metadata_prepare": 0, "downloads": 0, "API": 0,
        }, sort_keys=True, indent=2) + "\n")


@pytest.mark.parametrize("public", [False, True])
def test_real_old_default_and_modern_complete_graphs_keep_original_compiler_and_commands(real_blaze, public):
    f = real_blaze
    before = entry.source_snapshot(f.source, derived=True)
    code, report = run_real(f, public=public)
    retain(f, report, "public" if public else "private")
    assert code == 0, report
    assert report["metadata_full"]["returncode"] == report["metadata_original"]["returncode"] == 0
    assert report["full_graph"]["packages"] == 3 and report["original_selected_graph"]["packages"] == 2
    assert report["metadata_full"]["command"][0] == entry.METADATA_CARGO
    assert report["execution"]["command"][0] == entry.REAL_CARGO
    assert "--all-features" in report["metadata_full"]["command"]
    assert "--all-features" not in report["metadata_original"]["command"] + report["execution"]["command"]
    assert "Unpacking " in report["metadata_full"]["stderr"]
    assert "Unpacking " in report["metadata_original"]["stderr"]
    assert entry.source_snapshot(f.source, derived=True) == before
    assert not (f.source / "Cargo.lock").exists()
    if not public:
        assert report["test_results"] == {name: True for name in entry.EXPECTED}
        assert set(report["generated_outputs"]) == set(entry.GENERATED)


def test_real_same_input_reuses_target_but_executes_both_original_tests_again(real_blaze):
    f = real_blaze
    code, first = run_real(f); retain(f, first, "reuse-before")
    assert code == 0, first
    marker = f.build / "current/target/reuse-marker"
    marker.write_text("private current compilation")
    code, second = run_real(f); retain(f, second, "reuse-after")
    assert code == 0 and second["cache"]["reuse"], second
    assert marker.is_file()
    assert second["execution"]["pid"] != first["execution"]["pid"]
    assert second["metadata_full"]["stderr"] == second["metadata_original"]["stderr"] == ""
    assert "running 2 tests" in second["execution"]["stdout"]


@pytest.mark.parametrize("change", ["missing", "modified", "native", "source"])
def test_real_changed_inputs_or_recorded_outputs_force_current_only_rebuild(real_blaze, change):
    f = real_blaze
    code, first = run_real(f); retain(f, first, "invalidate-" + change + "-before")
    assert code == 0, first
    marker = f.build / "current/target/stale-marker"
    marker.write_text("private stale compilation")
    retained = f.metadata_home / "preserve-dependency-home"
    retained.write_text("not a candidate artifact")
    if change == "missing":
        (f.build / "current/source/data/test.gsym").unlink()
    elif change == "modified":
        (f.build / "current/source/data/test.gsym").write_bytes(b"tampered generated format")
    elif change == "native":
        tool = Path(f.spec["native_tools"]["llvm-gsymutil-14"]["path"])
        tool.write_text(tool.read_text() + "# new authenticated LOCAL TOOL identity\n")
        f.spec["native_tools"]["llvm-gsymutil-14"]["sha256"] = entry.file_hash(tool)
    else:
        (f.source / "src/unused.rs").write_text("// complete current input changes identity\n")
    code, second = run_real(f); retain(f, second, "invalidate-" + change + "-after")
    assert code == 0 and not second["cache"]["reuse"], second
    assert not marker.exists() and retained.is_file()
    assert set(second["generated_outputs"]) == set(entry.GENERATED)
    assert (f.build / "current/source/data/test.gsym").read_bytes() == b"LOCAL TOOL generated fixture"


@pytest.mark.parametrize("failure", ["assertion", "multifile_compile", "unapproved_source_write"])
def test_real_assertions_compile_errors_and_non_generated_source_writes_stay_failures(real_blaze, failure):
    f = real_blaze
    if failure == "assertion":
        (f.source / "src/current.rs").write_text('pub fn name() -> &\'static str { "wrong symbol" }\n')
    elif failure == "multifile_compile":
        (f.source / "src/current.rs").write_text(
            'compile_error!("unrelated CURRENT compilation failure");\npub fn name() -> &\'static str { "factorial" }\n')
    else:
        (f.source / "build.rs").write_text(BUILD.replace(
            "EXTRA", 'std::fs::write(root.join("data/extra-output.bin"), b"not allowed").unwrap();'))
    before = entry.source_snapshot(f.source, derived=True)
    code, report = run_real(f); retain(f, report, failure)
    assert code == (2 if failure == "unapproved_source_write" else 101), report
    assert report["execution"]["returncode"] == (0 if failure == "unapproved_source_write" else 101)
    assert entry.source_snapshot(f.source, derived=True) == before
    if failure != "unapproved_source_write":
        assert report["test_results"] == {name: False for name in entry.EXPECTED}
    assert not report["selection_uses_test_results"] and report["runtime_resolution_commands"] == 0


def test_real_candidate_lock_and_excluded_committed_data_remain_current_inputs(real_blaze):
    f = real_blaze
    p = f.source / "Cargo.toml"
    p.write_text(p.read_text().replace('version="0.1.0"', 'version="0.1.9"', 1).replace(
        'edition="2021"', 'edition="2021"\nexclude=["data/dwarf-example"]', 1) +
        '[package.metadata.current]\ntext="SIGINT is data"\n')
    own = lock_bytes(f, "0.1.9") + b"\n# candidate-authored serialization\n"
    (f.source / "Cargo.lock").write_bytes(own)
    before = entry.source_snapshot(f.source, derived=True)
    code, report = run_real(f); retain(f, report, "current-lock-and-committed-data")
    assert code == 0, report
    assert (f.build / "current/source/Cargo.lock").read_bytes() == own
    assert "data/dwarf-example" in report["source_before"]
    assert entry.source_snapshot(f.source, derived=True) == before


def test_real_original_compiler_child_signal_is_not_a_selection_or_success(real_blaze, monkeypatch):
    f = real_blaze
    real = entry.REAL_RUSTC
    wrapper = f.source.parent / "rustc-tool-signal.py"
    wrapper.write_text("#!" + sys.executable + "\nimport os,signal,sys\n"
                       "a=sys.argv[1:]\n"
                       "if '--crate-name' in a and a[a.index('--crate-name')+1]=='blazesym':\n"
                       " sys.stderr.write('LOCAL TOOL deliberate compiler signal\\n');sys.stderr.flush()\n"
                       " os.kill(os.getpid(),signal.SIGSEGV)\n"
                       "os.execv(" + repr(real) + ",[" + repr(real) + ",*a])\n")
    wrapper.chmod(0o755)
    monkeypatch.setattr(entry, "REAL_RUSTC", str(wrapper))
    f.spec["rustc_sha256"] = entry.file_hash(wrapper)
    f.tool_setup.append({"purpose": "LOCAL TOOL signal injection, not original compiler replacement",
                         "wrapper_sha256": entry.file_hash(wrapper), "delegated_rustc": real,
                         "delegated_rustc_sha256": entry.file_hash(Path(real)), "signal": int(signal.SIGSEGV)})
    code, report = run_real(f); retain(f, report, "old-compiler-child-signal")
    assert code == 101 and report.get("infrastructure_error"), report
    assert report["execution"]["returncode"] == 101
    assert not (f.build / "cache-state.json").exists()
