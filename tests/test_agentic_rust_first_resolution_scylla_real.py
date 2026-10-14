"""Pinned Cargo1.64 LOCAL TOOL controls, never the original Scylla SUT.

Only tiny local crates implement the shape consumed by the unchanged original
four-assertion harness. No database, network resolution or benchmark is run.
"""
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tarfile

import pytest

from test_agentic_rust_first_resolution_scylla import (
    scylla, entry, HARNESS_BYTES, lock_bytes, move_scylla,
)

LABEL = "SCYLLA96_LOCAL_TOOL_RUNTIME_FIXTURE_NOT_ORIGINAL_SUT"
LIBRARY = r'''
mod current;
pub mod transport {
    pub mod topology {
        use std::net::SocketAddr;
        pub struct Peer { pub address: SocketAddr, pub tokens: Vec<i64> }
        pub struct Metadata { pub peers: Vec<Peer> }
        impl Metadata {
            pub fn new_dummy(peers: &[SocketAddr]CONTROL) -> Self {
                Self { peers: crate::current::build(peers) }
            }
        }
    }
}
'''
CURRENT = r'''
use std::net::SocketAddr;
use crate::transport::topology::Peer;
pub fn build(peers: &[SocketAddr]) -> Vec<Peer> {
    let mut result: Vec<Peer> = peers.iter().enumerate().map(|(i, address)|
        Peer { address: *address, tokens: vec![scylla_cql::token(i)] }).collect();
    result.shrink_to_fit();
    CHANGE
    result
}
'''


@pytest.fixture
def real_scylla(scylla, monkeypatch):
    name = os.environ.get("SCYLLA_TEST_CARGO")
    if not name or not Path(name).is_file():
        pytest.skip("explicit retained Cargo1.64 required; never install a toolchain")
    f = scylla
    cargo = Path(name)
    rustc = cargo.with_name("rustc")
    monkeypatch.setattr(entry, "REAL_CARGO", str(cargo))
    monkeypatch.setattr(entry, "REAL_RUSTC", str(rustc))
    f.spec.update(cargo_sha256=entry.file_hash(cargo), rustc_sha256=entry.file_hash(rustc))
    monkeypatch.setattr(entry, "PREFIX", [
        "--offline", "--config", 'source.crates-io.replace-with="reviewbench-vendor"',
        "--config", 'source.reviewbench-vendor.directory="' + str(f.vendor) + '"'])
    monkeypatch.setenv("CARGO_HOME", str(f.source.parent / "empty-cargo-home"))
    monkeypatch.setenv("CARGO_NET_OFFLINE", "true")
    monkeypatch.setenv("RUSTFLAGS", "")
    monkeypatch.setenv("CARGO_INCREMENTAL", "0")
    monkeypatch.setenv("RUSTUP_AUTO_INSTALL", "0")
    monkeypatch.delenv("RUSTC_WRAPPER", raising=False)
    monkeypatch.delenv("RUSTC_WORKSPACE_WRAPPER", raising=False)
    f.tool_setup = []
    for tool, prefix in [(cargo, "cargo 1.64.0 "), (rustc, "rustc 1.64.0 ")]:
        argv = [str(tool), "--version"]
        proc = subprocess.run(argv, cwd=f.source, capture_output=True, text=True, timeout=15)
        f.tool_setup.append({"record_label": LABEL, "command": argv, "exit_status": proc.returncode,
                             "stdout": proc.stdout, "stderr": proc.stderr, "tool_sha256": entry.file_hash(tool)})
        assert proc.returncode == 0 and proc.stdout.startswith(prefix), f.tool_setup[-1]
    (f.source / "scylla/src/lib.rs").write_text(LIBRARY.replace("CONTROL", ""))
    (f.source / "scylla/src/current.rs").write_text(CURRENT.replace("CHANGE", ""))
    (f.source / "scylla-cql/src/lib.rs").write_text("pub fn token(i: usize) -> i64 { i as i64 }\n")
    return f


def run_fixture(f, private=True):
    p = entry.profile_for(f.spec)
    command = ["cargo", *p["prefix"], *(p["command"] if private else p["public"])]
    return entry.execute(f.spec, f.source, f.build, command, f.private if private else None,
                         lock_sources=f.locks, vendor=f.vendor)


def retain(f, report, label):
    folder = os.environ.get("SCYLLA_FIXTURE_EVIDENCE")
    if folder:
        path = Path(folder)
        path.mkdir(parents=True, exist_ok=True)
        (path / (label + ".json")).write_text(json.dumps({
            "record_label": LABEL, "runtime_report": report, "tool_setup": f.tool_setup,
            "original_harness_sha256": entry.digest(HARNESS_BYTES), "original_assertions": 4,
            "network_download_bytes": 0, "original_sut_runs": 0,
        }, indent=2, sort_keys=True) + "\n")


@pytest.mark.parametrize("arguments", [1, 2])
@pytest.mark.parametrize("private", [False, True])
def test_real_164_full_virtual_metadata_and_original_harness_accept_both_existing_api_shapes(real_scylla, arguments, private):
    f = real_scylla
    control = ", _control: std::net::SocketAddr" if arguments == 2 else ""
    (f.source / "scylla/src/lib.rs").write_text(LIBRARY.replace("CONTROL", control))
    before = entry.snapshot(f.source)
    code, report = run_fixture(f, private)
    retain(f, report, "api-" + str(arguments) + ("-private" if private else "-public"))
    assert code == 0, report
    assert report["metadata"]["returncode"] == report["execution"]["returncode"] == 0
    graph = json.loads(report["metadata"]["stdout"])
    selected = next(p for p in graph["packages"] if p["name"] == "scylla")
    assert graph["resolve"]["root"] is None and len(graph["workspace_members"]) == 4
    assert selected["id"] not in graph.get("workspace_default_members", [])
    assert len(graph["packages"]) == len(graph["resolve"]["nodes"]) == 4
    assert report["environment"]["CARGO_BUILD_JOBS"] == "4"
    assert report["binding"]["toolchain"] == "1.64.0" and report["binding"]["command_timeout_seconds"] == 2400
    assert entry.snapshot(f.source) == before and not (f.source / "Cargo.lock").exists()
    assert (f.build / "source/Cargo.lock").read_bytes() == next(iter(f.locks.values())).read_bytes()
    if private:
        assert report["test_results"] == {entry.SCYLLA_EXPECTED[0]: True}


def test_real_moved_current_member_still_uses_fixed_original_harness_path(real_scylla):
    f = real_scylla
    move_scylla(f)
    before = entry.snapshot(f.source)
    code, report = run_fixture(f)
    retain(f, report, "moved-member")
    assert code == 0, report
    assert report["mirror"]["selection"]["manifest"] == "members/driver/Cargo.toml"
    assert report["test_results"] == {entry.SCYLLA_EXPECTED[0]: True}
    assert entry.snapshot(f.source) == before


def test_real_candidate_owned_changed_graph_and_harmless_manifest_text_preserve_bytes(real_scylla, monkeypatch):
    f = real_scylla
    p = f.source / "scylla/Cargo.toml"
    p.write_text(p.read_text().replace("0.5.0", "0.5.99") +
                 '[package.metadata.current]\nnote="SIGINT / signal: 11 is metadata"\n')
    own = lock_bytes("0.5.99") + b"\n# independent candidate-authored text, not the default hash\n"
    (f.source / "Cargo.lock").write_bytes(own)
    monkeypatch.setattr(entry, "select_workspace_default",
                        lambda *args: pytest.fail("candidate-owned locks never choose defaults"))
    before = entry.snapshot(f.source)
    code, report = run_fixture(f)
    retain(f, report, "candidate-owned-current-graph")
    assert code == 0 and report["mirror"]["selected_default"] is None, report
    assert (f.build / "source/Cargo.lock").read_bytes() == own
    assert "SIGINT / signal: 11" in report["metadata"]["stdout"]
    assert entry.snapshot(f.source) == before


@pytest.mark.parametrize("failure", ["count", "address0", "address1", "tokens", "dependency_multifile"])
def test_real_each_original_assertion_and_unrelated_dependency_compile_failure_are_preserved(real_scylla, failure):
    f = real_scylla
    changes = {"count": "result.pop();", "address0": "result[0].address = peers[1];",
               "address1": "result[1].address = peers[0];",
               "tokens": "result[1].tokens = result[0].tokens.clone();",
               "dependency_multifile": ""}
    (f.source / "scylla/src/current.rs").write_text(CURRENT.replace("CHANGE", changes[failure]))
    if failure == "dependency_multifile":
        (f.source / "scylla-cql/src/lib.rs").write_text(
            'mod other;\npub fn token(i: usize) -> i64 { i as i64 }\n')
        (f.source / "scylla-cql/src/other.rs").write_text('compile_error!("unrelated CURRENT dependency file");\n')
    before = entry.snapshot(f.source)
    code, report = run_fixture(f)
    retain(f, report, "failure-" + failure)
    assert code == 101 and report["execution"]["returncode"] == 101, report
    assert report["test_results"] == {entry.SCYLLA_EXPECTED[0]: False}
    assert report["metadata"]["returncode"] == 0
    assert ("unrelated CURRENT dependency file" if failure == "dependency_multifile"
            else "assertion failed") in report["execution"]["stderr"]
    assert entry.snapshot(f.source) == before and report["default_resolution_commands"] == 0
    assert report["selection_uses_test_results"] is False


def test_real_previous_target_output_cannot_mask_a_changed_complete_current_candidate(real_scylla):
    f = real_scylla
    code, first = run_fixture(f)
    retain(f, first, "sync-before")
    assert code == 0, first
    (f.source / "scylla-cql/src/lib.rs").write_text("pub fn token(_: usize) -> i64 { 0 }\n")
    (f.source / "scylla/src/unselected.rs").write_text("// current file must still be synchronized\n")
    before = entry.snapshot(f.source)
    code, second = run_fixture(f)
    retain(f, second, "sync-after")
    assert code == 101 and second["test_results"] == {entry.SCYLLA_EXPECTED[0]: False}, second
    assert (f.build / "source/scylla-cql/src/lib.rs").read_bytes() == (f.source / "scylla-cql/src/lib.rs").read_bytes()
    assert (f.build / "source/scylla/src/unselected.rs").is_file()
    assert entry.snapshot(f.source) == before


def test_real_164_flat_registry_source_replacement_uses_only_provided_fixed_bytes(real_scylla):
    f = real_scylla
    contents = {
        "Cargo.toml": b'[package]\nname="fixture-external"\nversion="0.1.0"\nedition="2021"\n',
        "src/lib.rs": b"pub fn token(i: usize) -> i64 { i as i64 }\n",
    }
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        for name, body in contents.items():
            info = tarfile.TarInfo("fixture-external-0.1.0/" + name)
            info.size, info.mode = len(body), 0o644
            tar.addfile(info, io.BytesIO(body))
    fixed = f.source.parent / "fixed-local-tool.crate"
    fixed.write_bytes(archive.getvalue())
    package = {"name": "fixture-external", "version": "0.1.0", "source": entry.REGISTRY,
               "checksum": entry.file_hash(fixed)}
    folder = f.vendor / "fixture-external-0.1.0"
    (folder / "src").mkdir(parents=True)
    for name, body in contents.items():
        (folder / name).write_bytes(body)
    (folder / ".cargo-checksum.json").write_text(json.dumps({
        "package": package["checksum"], "files": {name: entry.file_hash(folder / name) for name in contents}}))
    with (f.source / "scylla/Cargo.toml").open("a") as stream:
        stream.write('fixture-external="=0.1.0"\n')
    (f.source / "Cargo.lock").write_bytes(lock_bytes(external=[package]))
    p = f.source / "scylla/src/current.rs"
    p.write_text(p.read_text().replace("scylla_cql::token", "fixture_external::token"))
    before = entry.snapshot(f.source)
    code, report = run_fixture(f)
    retain(f, report, "flat-vendor-164")
    assert code == 0 and report["graph"]["packages"] == 5, report
    assert report["mirror"]["selected_default"] is None and entry.snapshot(f.source) == before
    graph = json.loads(report["metadata"]["stdout"])
    external = next(p for p in graph["packages"] if p["source"] is not None)
    assert external["manifest_path"] == str(folder / "Cargo.toml")
    assert external["source"] == entry.REGISTRY


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGSEGV])
def test_real_cargo164_reports_a_signaled_compiler_child_without_reselection(real_scylla, monkeypatch, sig):
    f = real_scylla
    real = entry.REAL_RUSTC
    wrapper = f.source.parent / "rustc-local-tool-signal.py"
    wrapper.write_text("#!" + sys.executable + "\nimport os,signal,sys\n"
                       "a=sys.argv[1:]\n"
                       "if '--crate-name' in a and a[a.index('--crate-name')+1]=='scylla':\n"
                       " sys.stderr.write('LOCAL TOOL deliberate compiler child signal\\n');sys.stderr.flush()\n"
                       " os.kill(os.getpid()," + str(int(sig)) + ")\n"
                       "os.execv(" + repr(real) + ",[" + repr(real) + ",*a])\n")
    wrapper.chmod(0o755)
    monkeypatch.setattr(entry, "REAL_RUSTC", str(wrapper))
    f.spec["rustc_sha256"] = entry.file_hash(wrapper)
    f.tool_setup.append({"record_label": LABEL, "purpose": "synthetic process-signal injection, not original rustc replacement",
                         "wrapper": str(wrapper), "wrapper_sha256": entry.file_hash(wrapper),
                         "delegated_rustc": real, "delegated_rustc_sha256": entry.file_hash(Path(real)),
                         "signal": int(sig)})
    before = entry.snapshot(f.source)
    code, report = run_fixture(f)
    retain(f, report, "real-child-signal-" + str(int(sig)))
    assert code == 101 and report["metadata"]["returncode"] == 0, report
    assert report["execution"]["returncode"] == 101 and report.get("infrastructure_error"), report
    assert report["test_results"] == {entry.SCYLLA_EXPECTED[0]: False}
    assert entry.snapshot(f.source) == before and report["default_resolution_commands"] == 0
