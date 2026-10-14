"""Real retained Cargo1.93 on small LOCAL TOOL crates, not Prometheus source."""
import json
import os
from pathlib import Path
import time

import pytest

from test_agentic_rust_first_resolution_runtime import fixture, entry, lock_bytes

TOOL_LIBRARY = r'''
mod current;
pub mod metrics {
    pub mod counter {
        use std::sync::{Arc, atomic::{AtomicU64, Ordering}};
        #[derive(Default, Clone)]
        pub struct Counter(Arc<AtomicU64>);
        impl Counter {
            pub fn inc(&self) { self.0.fetch_add(1, Ordering::SeqCst); }
            pub fn get(&self) -> u64 { self.0.load(Ordering::SeqCst) }
        }
    }
}
pub mod registry {
    use crate::metrics::counter::Counter;
    #[derive(Default)]
    pub struct Registry { pub counter: Option<Counter> }
    impl Registry {
        pub fn register(&mut self, _: &str, _: &str, counter: Counter) { self.counter = Some(counter); }
    }
}
pub mod encoding {
    pub mod text {
        pub fn encode<W: std::fmt::Write>(out: &mut W, registry: &crate::registry::Registry)
            -> std::fmt::Result {
            if !HELPER::helper() { panic!("current path dependency failed"); }
            write!(out, "# HELP events Counted events.\n# TYPE events counter\nevents_total {}\n# EOF\n",
                   crate::current::value(registry.counter.as_ref().unwrap().get()))
        }
    }
}
'''


@pytest.fixture
def real_cargo(fixture, monkeypatch):
    name = os.environ.get("FIRST_RESOLUTION_TEST_CARGO")
    if not name or not Path(name).is_file():
        pytest.skip("explicit retained Cargo1.93 required; never install a toolchain")
    f = fixture
    cargo = Path(name)
    rustc = cargo.with_name("rustc")
    monkeypatch.setattr(entry, "REAL_CARGO", str(cargo))
    monkeypatch.setattr(entry, "REAL_RUSTC", str(rustc))
    f.spec.update(cargo_sha256=entry.file_hash(cargo), rustc_sha256=entry.file_hash(rustc))
    f.vendor.mkdir()
    # The tool fixture uses a local directory instead of a container mount.
    # Released source/PREFIX is never modified by this test-only monkeypatch.
    monkeypatch.setattr(entry, "PREFIX", [
        "--offline", "--config", 'source.crates-io.replace-with="reviewbench-vendor"',
        "--config", 'source.reviewbench-vendor.directory="' + str(f.vendor) + '"'])
    monkeypatch.setenv("CARGO_HOME", str(f.source.parent / "cargo-home"))
    monkeypatch.setenv("CARGO_NET_OFFLINE", "true")
    monkeypatch.setenv("RUSTFLAGS", "")
    monkeypatch.setenv("CARGO_INCREMENTAL", "0")
    monkeypatch.setenv("RUSTUP_AUTO_INSTALL", "0")
    return f


def run_fixture(f, *, private=True):
    command = ["cargo", *entry.PREFIX, *(entry.PRIVATE_COMMAND if private else ["check"])]
    code, report = entry.execute(f.spec, f.source, f.build, command, f.private if private else None,
                                  lock_sources=f.locks, vendor=f.vendor)
    return code, report


def retain(report, label):
    folder = os.environ.get("FIRST_RESOLUTION_FIXTURE_EVIDENCE")
    if folder:
        path = Path(folder)
        path.mkdir(parents=True, exist_ok=True)
        (path / (label + ".json")).write_text(json.dumps({
            "kind": "synthetic local TOOL fixture; not original SUT/readiness",
            "runtime_report": report, "new_network": 0, "original_sut_runs": 0,
        }, indent=2, sort_keys=True) + "\n")


@pytest.mark.parametrize("selector", entry.SELECTORS)
@pytest.mark.parametrize("mode", ["public", "private"])
def test_real_pinned_cargo_selects_either_current_topology_and_preserves_all_bytes(real_cargo, selector, mode):
    f = real_cargo
    root = f.source
    (root / "Cargo.toml").write_text((root / "Cargo.toml").read_text().replace(entry.SELECTORS[0], selector))
    (root / "helper/Cargo.toml").write_text((root / "helper/Cargo.toml").read_text().replace(entry.SELECTORS[0], selector))
    (root / "src/lib.rs").write_text(TOOL_LIBRARY.replace("HELPER", selector.replace("-", "_")))
    (root / "src/current.rs").write_text("pub fn value(value: u64) -> u64 { value }\n")
    before = entry.snapshot(root)
    code, report = run_fixture(f, private=mode == "private")
    retain(report, selector + "-" + mode)
    assert code == 0, report
    assert report["metadata"]["returncode"] == report["execution"]["returncode"] == 0
    assert entry.snapshot(root) == before and not (root / "Cargo.lock").exists()
    assert report["mirror"]["selection"]["selector_package"] == selector
    if mode == "private":
        assert report["test_results"] == {entry.EXPECTED[0]: True}


@pytest.mark.parametrize("failure", ["assertion", "multifile_compile"])
def test_real_failed_original_assertion_or_unrelated_current_compile_is_preserved(real_cargo, failure):
    f = real_cargo
    (f.source / "src/lib.rs").write_text(TOOL_LIBRARY.replace("HELPER", entry.SELECTORS[0].replace("-", "_")))
    body = ("pub fn value(_: u64) -> u64 { 2 }\n" if failure == "assertion" else
            'pub fn value(value: u64) -> u64 { value }\ncompile_error!("unrelated current source failure");\n')
    (f.source / "src/current.rs").write_text(body)
    before = entry.snapshot(f.source)
    code, report = run_fixture(f)
    retain(report, failure)
    assert code == 101 and report["metadata"]["returncode"] == 0
    assert report["execution"]["returncode"] == 101
    assert report["test_results"] == {entry.EXPECTED[0]: False}
    assert entry.snapshot(f.source) == before
    assert ("assertion" in report["execution"]["stderr"] if failure == "assertion"
            else "unrelated current source failure" in report["execution"]["stderr"])


def test_real_candidate_owned_lock_and_unrelated_manifest_edits_are_not_default_hash_selected(real_cargo):
    f = real_cargo
    (f.source / "src/lib.rs").write_text(TOOL_LIBRARY.replace("HELPER", entry.SELECTORS[0].replace("-", "_")))
    (f.source / "src/current.rs").write_text("pub fn value(value: u64) -> u64 { value }\n")
    manifest = f.source / "Cargo.toml"
    manifest.write_text(manifest.read_text() + '[package.metadata.fixture]\nnote="SIGINT is metadata"\n')
    own = lock_bytes(entry.SELECTORS[0]) + b"\n# candidate-authored lock serialization\n"
    (f.source / "Cargo.lock").write_bytes(own)
    before = entry.snapshot(f.source)
    code, report = run_fixture(f)
    retain(report, "candidate-owned")
    assert code == 0, report
    assert report["mirror"]["selected_default"] is None
    assert (f.build / "source/Cargo.lock").read_bytes() == own
    assert entry.snapshot(f.source) == before


@pytest.mark.parametrize("private", [False, True])
def test_real_metadata_retains_missing_unselected_proto_bench_without_building_it(real_cargo, private):
    f = real_cargo
    (f.source / "src/lib.rs").write_text(TOOL_LIBRARY.replace("HELPER", entry.SELECTORS[0].replace("-", "_")))
    (f.source / "src/current.rs").write_text("pub fn value(value: u64) -> u64 { value }\n")
    manifest = f.source / "Cargo.toml"
    manifest.write_text(manifest.read_text() +
                        '[features]\nprotobuf=[]\n[[bench]]\nname="proto"\nharness=false\ntest=false\n'
                        'path="benches/encoding/proto.rs"\nrequired-features=["protobuf"]\n')
    before = entry.snapshot(f.source)
    code, report = run_fixture(f, private=private)
    retain(report, "unselected-proto-bench-" + ("private" if private else "public"))
    assert code == 0, report
    graph = json.loads(report["metadata"]["stdout"])
    targets = [t for p in graph["packages"] for t in p["targets"] if t["name"] == "proto"]
    assert len(targets) == 1 and targets[0]["kind"] == ["bench"] and targets[0]["test"] is False
    assert not Path(targets[0]["src_path"]).exists()
    assert "--all-targets" not in report["transport"]["effective_argv"]
    assert entry.snapshot(f.source) == before
