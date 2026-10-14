"""Real Cargo1.93 TOOL crates with frozen harnesses, never original VFS runs."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from test_agentic_rust_vfs import fixture_source, entry

ASYNC_STD = r"""
pub mod io {
    pub trait ReadExt: std::io::Read {
        fn read_to_string(&mut self, value: &mut String) -> std::future::Ready<std::io::Result<usize>> {
            std::future::ready(std::io::Read::read_to_string(self, value))
        }
    }
    impl<T: std::io::Read + ?Sized> ReadExt for T {}
    pub trait WriteExt: std::io::Write {
        fn write_all(&mut self, value: &[u8]) -> std::future::Ready<std::io::Result<()>> {
            std::future::ready(std::io::Write::write_all(self, value))
        }
    }
    impl<T: std::io::Write + ?Sized> WriteExt for T {}
}
pub mod task {
    pub fn block_on<F: std::future::Future>(future: F) -> F::Output {
        let mut future = Box::pin(future);
        let mut cx = std::task::Context::from_waker(std::task::Waker::noop());
        loop {
            if let std::task::Poll::Ready(value) = future.as_mut().poll(&mut cx) { return value; }
        }
    }
}
"""
TOOL_LIBRARY = r"""
mod behavior;
pub mod async_vfs {
    use std::{collections::BTreeMap, io, sync::{Arc, Mutex}};
    #[derive(Default)]
    struct State { files: BTreeMap<String, Vec<u8>>, directories: Vec<String> }
    pub struct Storage(Arc<Mutex<State>>);
    impl Storage { pub fn new() -> Self { Self(Arc::new(Mutex::new(State::default()))) } }
    pub struct PathHandle { state: Arc<Mutex<State>>, path: String }
    pub struct Writer { state: Arc<Mutex<State>>, path: String, data: Vec<u8> }
    impl io::Write for Writer {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            self.data.extend_from_slice(bytes); Ok(bytes.len())
        }
        fn flush(&mut self) -> io::Result<()> { Ok(()) }
    }
    impl Drop for Writer {
        fn drop(&mut self) { self.state.lock().unwrap().files.insert(self.path.clone(), self.data.clone()); }
    }
    impl PathHandle {
        pub fn new(storage: Storage) -> Self { Self { state: storage.0, path: String::new() } }
        pub fn join(&self, name: &str) -> io::Result<Self> {
            Ok(Self { state: self.state.clone(), path: if self.path.is_empty() {
                name.to_owned() } else { format!("{}/{}", self.path, name) } })
        }
        pub async fn create_dir(&self) -> io::Result<()> {
            self.state.lock().unwrap().directories.push(self.path.clone()); Ok(())
        }
        pub async fn create_file(&self) -> io::Result<Writer> {
            Ok(Writer { state: self.state.clone(), path: self.path.clone(), data: Vec::new() })
        }
        pub async fn exists(&self) -> io::Result<bool> {
            Ok(crate::behavior::exists(self.state.lock().unwrap().files.contains_key(&self.path)))
        }
        pub async fn open_file(&self) -> io::Result<io::Cursor<Vec<u8>>> {
            let bytes = self.state.lock().unwrap().files.get(&self.path).unwrap().clone();
            Ok(io::Cursor::new(crate::behavior::contents(bytes)))
        }
    }
    // Exports are supplied below for this synthetic TOOL API shape only.
    EXPORTS
}
"""


@pytest.fixture
def cargo193(tmp_path, monkeypatch):
    name = os.environ.get("VFS_TEST_CARGO")
    if not name or not Path(name).is_file():
        pytest.skip("explicit retained Cargo1.93.0; never install/download a toolchain")
    cargo = Path(name)
    assert subprocess.check_output([str(cargo), "--version"], text=True).startswith("cargo 1.93.0 ")
    for key, value in {
        "PATH": str(cargo.parent) + os.pathsep + os.environ["PATH"],
        "RUSTC": str(cargo.parent / "rustc"), "CARGO_HOME": str(tmp_path / "cargo-home"),
        "CARGO_TARGET_DIR": str(tmp_path / "outside-source-target"),
        "CARGO_NET_OFFLINE": "true", "CARGO_BUILD_JOBS": "2", "CARGO_TERM_COLOR": "never",
        "RUSTFLAGS": "", "CARGO_PROFILE_DEV_DEBUG": "0", "CARGO_PROFILE_TEST_DEBUG": "0",
    }.items():
        monkeypatch.setenv(key, value)
    return cargo


def source(root, cargo, style="new", broken=None):
    spec = fixture_source(root)
    (root / "Cargo.toml").write_text(
        '[package]\nname="vfs"\nversion="0.1.0"\nedition="2021"\n'
        '[features]\nasync-vfs=[]\n[dependencies]\nasync-std={path="tool-async-std"}\n')
    exports = []
    if style in {"old", "both"}:
        exports.append("pub use {PathHandle as VfsPath, Storage as MemoryFS};")
    if style in {"new", "both"}:
        exports.append("pub use {PathHandle as AsyncVfsPath, Storage as AsyncMemoryFS};")
    (root / "src/lib.rs").write_text(TOOL_LIBRARY.replace("EXPORTS", "\n".join(exports)))
    behavior = ("pub fn contents(value: Vec<u8>) -> Vec<u8> { value }\n"
                "pub fn exists(value: bool) -> bool { value }\n")
    if broken == "contents":
        behavior = behavior.replace("{ value }", '{ b"changed".to_vec() }', 1)
    elif broken == "exists":
        behavior = behavior.replace("-> bool { value }", "-> bool { false }")
    elif broken == "compile":
        behavior += 'compile_error!("unrelated current production failure");\n'
    elif broken == "signature":
        path = root / "src/lib.rs"
        path.write_text(path.read_text().replace("pub fn join(", "pub fn join<T>("))
    (root / "src/behavior.rs").write_text(behavior)
    support = root / "tool-async-std"
    (support / "src").mkdir(parents=True)
    (support / "Cargo.toml").write_text('[package]\nname="async-std"\nversion="0.1.0"\nedition="2021"\n')
    (support / "src/lib.rs").write_text(ASYNC_STD)
    result = subprocess.run([str(cargo), "generate-lockfile", "--offline"], cwd=root,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return spec


def save(request, value):
    directory = os.environ.get("VFS_FIXTURE_EVIDENCE")
    if directory:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / (request.node.name.replace("/", "_") + ".json")).write_text(json.dumps({
            "kind": "SYNTHETIC dependency-free TOOL crates; NOT original VFS source/build/calibration",
            "original_harness_bytes_preserved": True, "original_sut_runs": 0,
            "model_calls": 0, "downloads": 0, "evidence": value,
        }, indent=2, sort_keys=True) + "\n")


@pytest.mark.parametrize("style,expected", [
    ("old", ["oracle_start"]), ("new", ["oracle_golden"]), ("both", list(entry.NAMES)),
])
def test_real_whole_tool_candidate_selects_only_compilable_original_harnesses(
        tmp_path, cargo193, request, style, expected):
    root = tmp_path / "source"
    spec = source(root, cargo193, style)
    before = entry.snapshot(root)
    try:
        result = entry.select(spec, root, tmp_path / "forms")
    except entry.SelectionError as exc:
        save(request, {"error": str(exc), "records": exc.evidence})
        raise
    save(request, result)
    assert result["selected"] == expected
    assert all(result["results"].values()) and len(result["tests"]) == len(expected)
    assert entry.snapshot(root) == before


@pytest.mark.parametrize("broken", ["contents", "exists"])
def test_real_changed_multifile_behavior_fails_each_original_assertion_without_reselection(
        tmp_path, cargo193, request, broken):
    root = tmp_path / "source"
    spec = source(root, cargo193, "both", broken)
    result = entry.select(spec, root, tmp_path / "forms")
    save(request, result)
    assert result["selected"] == list(entry.NAMES)
    assert len(result["tests"]) == 2 and not any(result["results"].values())
    assert all(r["returncode"] == 101 for r in result["tests"])


def test_real_unrelated_production_compile_error_aborts_before_second_form(tmp_path, cargo193, request):
    root = tmp_path / "source"
    spec = source(root, cargo193, "new", "compile")
    with pytest.raises(entry.SelectionError) as raised:
        entry.select(spec, root, tmp_path / "forms")
    save(request, {"error": str(raised.value), "records": raised.value.evidence})
    assert len(raised.value.evidence["api_probes"]) == 1
    assert raised.value.evidence["probes"] == []
    assert raised.value.evidence["tests"] == []


def test_real_inference_error_with_available_names_is_not_discarded(tmp_path, cargo193, request):
    root = tmp_path / "source"
    spec = source(root, cargo193, "both", "signature")
    with pytest.raises(entry.SelectionError, match="complete original") as raised:
        entry.select(spec, root, tmp_path / "forms")
    save(request, {"error": str(raised.value), "records": raised.value.evidence})
    assert len(raised.value.evidence["api_probes"]) == 2
    assert all(p["returncode"] == 0 for p in raised.value.evidence["api_probes"])
    assert len(raised.value.evidence["probes"]) == 1 and raised.value.evidence["tests"] == []
    assert '"E0282"' in raised.value.evidence["probes"][0]["stdout"]


def test_real_cargo_metadata_redirect_is_rejected_before_any_compile(tmp_path, cargo193, request):
    root = tmp_path / "source"
    spec = source(root, cargo193, "old")
    manifest = root / "Cargo.toml"
    manifest.write_text(manifest.read_text()
                        + '\n[[test]]\nname="oracle_start"\npath="tests/redirect.rs"\n')
    (root / "tests/redirect.rs").write_text(
        "#[test]\nfn in_memory_async_path_round_trips_file_contents() {}\n")
    with pytest.raises(entry.SelectionError, match="redirects") as raised:
        entry.select(spec, root, tmp_path / "forms")
    save(request, {"error": str(raised.value), "records": raised.value.evidence})
    assert raised.value.evidence["graph"]["returncode"] == 0
    assert raised.value.evidence["api_probes"] == raised.value.evidence["probes"] == []
    assert raised.value.evidence["tests"] == []


@pytest.mark.parametrize("signal_number", [6, 9, 11, 15])
def test_real_cargo_child_signal_after_missing_name_diagnostic_aborts(
        tmp_path, monkeypatch, cargo193, request, signal_number):
    root = tmp_path / "source"
    spec = source(root, cargo193, "new")
    rustc, events = os.environ["RUSTC"], tmp_path / "rustc-events.jsonl"
    wrapper = tmp_path / "rustc-wrapper"
    wrapper.write_text(
        "#!/usr/bin/env python3\nimport json,os,resource,subprocess,sys\n"
        "resource.setrlimit(resource.RLIMIT_CORE,(0,0))\nargs=sys.argv[1:]\n"
        f"compiler={rustc!r}\n"
        "target=args[args.index('--crate-name')+1] if '--crate-name' in args else None\n"
        "if target != '__reviewbench_vfs_api_oracle_start': os.execv(compiler,[compiler,*args])\n"
        "p=subprocess.run([compiler,*args],stdout=subprocess.PIPE,stderr=subprocess.PIPE)\n"
        "sys.stdout.buffer.write(p.stdout);sys.stdout.buffer.flush()\n"
        "sys.stderr.buffer.write(p.stderr);sys.stderr.buffer.flush()\n"
        f"with open({str(events)!r},'a') as f: f.write(json.dumps({{'signal':{signal_number},"
        "'original_rustc_exit':p.returncode,'forwarded_stderr':p.stderr.decode(errors='replace')})+'\\n')\n"
        f"os.kill(os.getpid(),{signal_number})\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("RUSTC", str(wrapper))
    with pytest.raises(entry.SelectionError) as raised:
        entry.select(spec, root, tmp_path / "forms")
    rows = [json.loads(x) for x in events.read_text().splitlines()]
    save(request, {"signal": signal_number, "child_events": rows,
                   "error": str(raised.value), "records": raised.value.evidence})
    assert len(rows) == 1 and rows[0]["original_rustc_exit"] != 0
    assert '"E0432"' in rows[0]["forwarded_stderr"]
    assert len(raised.value.evidence["api_probes"]) == 1
    assert raised.value.evidence["probes"] == [] and raised.value.evidence["tests"] == []
    assert "infrastructure failure" in str(raised.value)
