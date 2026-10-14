"""Prometheus82 runtime data/process fixtures; never the original SUT."""
import copy
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_first_resolution_entry as entry

ORIGINAL_HARNESS = b"""use prometheus_client::encoding::text::encode;
use prometheus_client::metrics::counter::Counter;
use prometheus_client::registry::Registry;

#[derive(Default)]
struct Output(String);

impl std::io::Write for Output {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        let text = std::str::from_utf8(bytes)
            .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))?;
        self.0.push_str(text);
        Ok(bytes.len())
    }

    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

impl std::fmt::Write for Output {
    fn write_str(&mut self, text: &str) -> std::fmt::Result {
        self.0.push_str(text);
        Ok(())
    }
}

#[test]
fn registered_counter_is_encoded_through_the_public_registry() {
    let mut registry = Registry::default();
    let counter: Counter = Counter::default();
    registry.register("events", "Counted events", counter.clone());
    counter.inc();

    let mut output = Output::default();
    encode(&mut output, &registry).unwrap();

    assert_eq!(
        output.0,
        "# HELP events Counted events.\\n# TYPE events counter\\nevents_total 1\\n# EOF\\n"
    );
}
"""


def lock_bytes(selector=None, extra=()):
    rows = [{"name": "prometheus-client", "version": "0.0.0",
             **({"dependencies": [selector]} if selector else {})}]
    if selector:
        rows.append({"name": selector, "version": "0.0.0"})
    rows.extend(extra)
    return b"version = 4\n" + b"".join(
        ("\n[[package]]\n" + "".join(f"{k} = {json.dumps(v)}\n" for k, v in row.items())).encode()
        for row in rows)


def source_tree(root, selector, *, workspace=True, alias=False, inherited=False):
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "helper/src").mkdir(parents=True)
    (root / "src/lib.rs").write_text("pub mod current;\n")
    (root / "src/current.rs").write_text("pub fn current() -> bool { true }\n")
    (root / "helper/src/lib.rs").write_text("pub fn helper() -> bool { true }\n")
    (root / "helper/Cargo.toml").write_text(
        '[package]\nname=' + json.dumps(selector) + '\nversion="0.0.0"\nedition="2021"\n')
    name = "renamed_helper" if alias else selector
    detail = '{path="./helper/", package=' + json.dumps(selector) + ', version="0.0.0"}'
    manifest = '[package]\nname="prometheus-client"\nversion="0.0.0"\nedition="2021"\n'
    if inherited:
        manifest += ('[workspace]\nmembers=["helper"]\n[workspace.dependencies]\n' + name + "=" + detail +
                     '\n[dependencies]\n' + name + "={workspace=true}\n")
    else:
        manifest += "[dependencies]\n" + name + "=" + detail + "\n"
        if workspace:
            manifest += '[workspace]\nmembers=["helper"]\n'
    (root / "Cargo.toml").write_text(manifest)
    assert entry.digest(ORIGINAL_HARNESS) == entry.HARNESS["sha256"]
    (root / entry.HARNESS["path"]).write_bytes(ORIGINAL_HARNESS)


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    source = tmp_path / "candidate"
    source_tree(source, entry.SELECTORS[0])
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("cargo", "rustc"):
        (tools / name).write_text("authenticated Python data-fixture tool bytes: " + name)
    monkeypatch.setattr(entry, "REAL_CARGO", str(tools / "cargo"))
    monkeypatch.setattr(entry, "REAL_RUSTC", str(tools / "rustc"))
    defaults, lock_sources = [], {}
    for index, selector in enumerate(entry.SELECTORS):
        path = tmp_path / ("default-" + str(index) + ".lock")
        path.write_bytes(lock_bytes(selector))
        target = "/opt/reviewbench-assets/rust-new-lock-" + str(index)
        defaults.append({"selector_package": selector, "asset_target": target, "sha256": entry.file_hash(path)})
        lock_sources[target] = path
    spec = {"schema_version": entry.SCHEMA, "task_id": entry.TASK, "profile": entry.PROFILE,
            "toolchain": "1.93.0", "cargo_sha256": entry.file_hash(tools / "cargo"),
            "rustc_sha256": entry.file_hash(tools / "rustc"), "cpus": 4,
            "max_log_bytes": 33554432, "metadata_timeout_seconds": 120,
            "receipt_sha256": "f" * 64, "defaults": defaults}
    private = {"schema_version": entry.PRIVATE_SCHEMA, "task_id": entry.TASK,
               "command": entry.PRIVATE_COMMAND, "harness": dict(entry.HARNESS), "expected_tests": entry.EXPECTED}
    return SimpleNamespace(source=source, build=tmp_path / "build", spec=spec, private=private,
                           locks=lock_sources, vendor=tmp_path / "vendor")


def metadata(root, vendor):
    packages = entry.parse_lock((root / "Cargo.lock").read_bytes())
    rows = []
    by_key = {}
    for key, package in packages.items():
        if key[2] is None:
            choices = [p for p in root.rglob("Cargo.toml") if
                       (lambda d: (d["name"], d["version"]) == key[:2])(
                           entry.tomllib.loads(p.read_text()).get("package", {}))]
            assert len(choices) == 1
            manifest = choices[0]
        else:
            manifest = vendor / (key[0] + "-" + key[1]) / "Cargo.toml"
        identity = "pkg:" + key[0] + "@" + key[1]
        by_key[key] = identity
        targets = [{"kind": ["lib"], "name": key[0].replace("-", "_"),
                    "src_path": str(manifest.parent / "src/lib.rs"), "test": True}]
        if manifest == root / "Cargo.toml":
            targets.append({"kind": ["test"], "name": "oracle_session",
                            "src_path": str(root / entry.HARNESS["path"]), "test": True})
        rows.append({"id": identity, "name": key[0], "version": key[1], "source": key[2],
                     "manifest_path": str(manifest), "targets": targets, "features": {}})
    nodes = []
    for key, package in packages.items():
        dependencies = [by_key[k] for k in sorted(entry.lock_edges(packages, package))]
        nodes.append({"id": by_key[key], "dependencies": dependencies, "features": [],
                      "deps": [{"name": packages[k]["name"].replace("-", "_"), "pkg": by_key[k],
                                "dep_kinds": [{"kind": None, "target": None}]}
                               for k in entry.lock_edges(packages, package)]})
    root_id = next(p["id"] for p in rows if p["manifest_path"] == str(root / "Cargo.toml"))
    return {"version": 1, "workspace_root": str(root), "packages": rows,
            "workspace_members": [p["id"] for p in rows if p["source"] is None],
            "workspace_default_members": [root_id], "resolve": {"root": root_id, "nodes": nodes}}


def record(code=0, stdout="", stderr="", **kw):
    return {"returncode": code, "stdout": stdout, "stderr": stderr, "limit": None,
            "stdout_total_bytes": len(stdout.encode()), "stdout_captured_bytes": len(stdout.encode()),
            "stderr_total_bytes": len(stderr.encode()), "stderr_captured_bytes": len(stderr.encode()), **kw}


def execute(f, *, private=True, change=None, code=0):
    calls = []
    def run(command, root, env, logs, name, deadline, limit, **kw):
        calls.append(name)
        assert env["CARGO_NET_OFFLINE"] == "true" and env["CARGO_BUILD_JOBS"] == "4"
        assert not Path(env["CARGO_TARGET_DIR"]).is_relative_to(root)
        assert "--locked" in command and "--offline" in command
        if name == "metadata":
            assert "--all-features" in command and "--no-deps" not in command
            r = record(stdout=json.dumps(metadata(root, f.vendor)))
        else:
            text = "test " + entry.EXPECTED[0] + " ... " + ("ok" if code == 0 else "FAILED") + "\n"
            text += "test result: " + ("ok." if code == 0 else "FAILED.") + "\n"
            r = record(code, stdout=text)
        if change:
            change(name, root, r)
        return r
    command = ["cargo", *entry.PREFIX, *(entry.PRIVATE_COMMAND if private else ["check"])]
    result = entry.execute(f.spec, f.source, f.build, command, f.private if private else None,
                           run=run, lock_sources=f.locks, vendor=f.vendor)
    return *result, calls


@pytest.mark.parametrize("selector", entry.SELECTORS)
@pytest.mark.parametrize("topology", ["root", "workspace", "alias", "inherited"])
def test_defaults_follow_current_local_package_names_only(fixture, selector, topology, monkeypatch):
    f = fixture
    (f.source / "helper/Cargo.toml").write_text('[package]\nname=' + json.dumps(selector) +
                                                '\nversion="0.0.0"\ndescription="harmless current attribute"\n')
    name = "renamed_helper" if topology in {"alias", "inherited"} else selector
    detail = '{package=' + json.dumps(selector) + ',path="./helper/"}'
    original = '[package]\nname="prometheus-client"\nversion="0.0.0"\nedition="2021"\n'
    if topology == "inherited":
        original += ('[workspace]\nmembers=["helper"]\n[workspace.dependencies]\n' + name + "=" + detail +
                     '\n[dependencies]\n' + name + "={workspace=true}\n")
    else:
        original += "[dependencies]\n" + name + "=" + detail + "\n"
        if topology != "root":
            original += '[workspace]\nmembers=["helper"]\n'
    (f.source / "Cargo.toml").write_text(original + '[package.metadata.fixture]\nnote="current"\n')
    monkeypatch.setenv("ORACLE_VARIANT", "opposite-label-is-inert")
    before = entry.snapshot(f.source)
    status, report, calls = execute(f)
    assert status == 0 and calls == ["metadata", "cargo"]
    assert report["mirror"]["selection"]["selector_package"] == selector
    assert report["selection_uses_test_results"] is False
    assert entry.snapshot(f.source) == before and not (f.source / "Cargo.lock").exists()


@pytest.mark.parametrize("shape", ["both", "neither"])
def test_authored_lock_bypasses_default_selector_and_preserves_current_bytes(fixture, shape, monkeypatch):
    f = fixture
    if shape == "neither":
        (f.source / "Cargo.toml").write_text('[package]\nname="prometheus-client"\nversion="0.0.0"\n')
        own = lock_bytes()
    else:
        peer = f.source / "peer"
        (peer / "src").mkdir(parents=True)
        (peer / "src/lib.rs").write_text("// current peer\n")
        (peer / "Cargo.toml").write_text('[package]\nname=' + json.dumps(entry.SELECTORS[1]) + '\nversion="0.0.0"\n')
        text = (f.source / "Cargo.toml").read_text().replace("[workspace]", entry.SELECTORS[1] +
                    '={path="peer"}\n[workspace]')
        (f.source / "Cargo.toml").write_text(text)
        own = lock_bytes(entry.SELECTORS[0], [{"name": entry.SELECTORS[1], "version": "0.0.0"}])
        own = own.replace(json.dumps([entry.SELECTORS[0]]).encode(), json.dumps(list(entry.SELECTORS)).encode())
    own += b"\n# candidate-authored lock, not either default hash\n"
    (f.source / "Cargo.lock").write_bytes(own)
    monkeypatch.setattr(entry, "select_default", lambda *a: pytest.fail("authored lock triggered selection"))
    before = entry.snapshot(f.source)
    status, report, calls = execute(f)
    assert status == 0 and calls == ["metadata", "cargo"]
    assert report["mirror"]["selected_default"] is None
    assert (f.build / "source/Cargo.lock").read_bytes() == own
    assert entry.snapshot(f.source) == before


@pytest.mark.parametrize("change", ["both", "neither", "missing_path", "wrong_package_alias",
                                  "escape", "symlink", "asset_hash", "missing_asset", "lock_symlink"])
def test_invalid_missing_lock_initialization_fails_before_any_cargo(fixture, change):
    f = fixture
    if change == "both":
        peer = f.source / "peer"
        peer.mkdir()
        (peer / "Cargo.toml").write_text('[package]\nname=' + json.dumps(entry.SELECTORS[1]) + '\nversion="0.0.0"\n')
        path = f.source / "Cargo.toml"
        path.write_text(path.read_text().replace("[workspace]", entry.SELECTORS[1] + '={path="peer"}\n[workspace]'))
    elif change == "neither":
        path = f.source / "Cargo.toml"
        path.write_text('[package]\nname="prometheus-client"\nversion="0.0.0"\n')
    elif change == "missing_path":
        (f.source / "helper/Cargo.toml").unlink()
    elif change == "wrong_package_alias":
        path = f.source / "Cargo.toml"
        path.write_text(path.read_text().replace("package=" + json.dumps(entry.SELECTORS[0]),
                                                'package="not-the-current-name"'))
    elif change == "escape":
        path = f.source / "Cargo.toml"
        path.write_text(path.read_text().replace("./helper/", "../foreign"))
    elif change == "symlink":
        path = f.source / "helper/Cargo.toml"
        path.unlink()
        path.symlink_to("../Cargo.toml")
    elif change == "asset_hash":
        next(iter(f.locks.values())).write_text("altered default")
    elif change == "missing_asset":
        next(iter(f.locks.values())).unlink()
    else:
        (f.source / "Cargo.lock").symlink_to("Cargo.toml")
    status, report, calls = execute(f)
    assert status == 2 and calls == [] and report["status"] == "rejected"


@pytest.mark.parametrize("change", ["package", "node", "edge", "deps", "feature_type", "feature_unknown",
                                  "duplicate_id", "local_manifest", "local_target", "target_redirect",
                                  "target_disabled", "default_members", "workspace_members", "bad_json",
                                  "prefix", "suffix", "stderr", "rc101", "rc137", "signal11",
                                  "truncated", "deadline", "limit", "utf8", "source_mutation", "harness"])
def test_bad_metadata_never_reaches_original_check_or_test(fixture, change):
    f = fixture
    def mutate(phase, root, r):
        if phase != "metadata":
            pytest.fail("bad metadata reached SUT")
        graph = json.loads(r["stdout"])
        if change == "package":
            graph["packages"].pop()
        elif change == "node":
            graph["resolve"]["nodes"].pop()
        elif change == "edge":
            graph["resolve"]["nodes"][0]["dependencies"] = []
            graph["resolve"]["nodes"][0]["deps"] = []
        elif change == "deps":
            graph["resolve"]["nodes"][0]["deps"] = []
        elif change == "feature_type":
            graph["resolve"]["nodes"][0]["features"] = "not-a-list"
        elif change == "feature_unknown":
            graph["resolve"]["nodes"][0]["features"] = ["not-declared"]
        elif change == "duplicate_id":
            graph["packages"][1]["id"] = graph["packages"][0]["id"]
        elif change == "local_manifest":
            graph["packages"][1]["manifest_path"] = "/outside/Cargo.toml"
        elif change == "local_target":
            graph["packages"][1]["targets"][0]["src_path"] = "/outside/lib.rs"
        elif change == "target_redirect":
            graph["packages"][0]["targets"][1]["src_path"] = str(root / "src/lib.rs")
        elif change == "target_disabled":
            graph["packages"][0]["targets"][1]["test"] = False
        elif change == "default_members":
            graph["workspace_default_members"] = [graph["packages"][1]["id"]]
        elif change == "workspace_members":
            graph["workspace_members"] = ["foreign-id"]
        elif change == "stderr":
            r["stderr"] = "process terminated by SIGINT"
        elif change.startswith("rc"):
            r["returncode"] = int(change[2:])
        elif change == "signal11":
            r["returncode"] = -11
        elif change == "truncated":
            r["stdout_total_bytes"] += 1
        elif change in {"deadline", "limit"}:
            r["limit"] = "deadline" if change == "deadline" else "output_limit"
        elif change == "utf8":
            r["stdout_utf8_valid"] = False
        elif change == "source_mutation":
            (root / "src/current.rs").write_text("// mutated\n")
        elif change == "harness":
            (root / entry.HARNESS["path"]).write_text("// not the original\n")
        r["stdout"] = json.dumps(graph)
        if change == "bad_json":
            r["stdout"] = r["stdout"][:-1]
        elif change == "prefix":
            r["stdout"] = "SIGINT\n" + r["stdout"]
        elif change == "suffix":
            r["stdout"] += "\n{}"
    status, report, calls = execute(f, change=mutate)
    assert status == 2 and calls == ["metadata"] and report["metadata"]


@pytest.mark.parametrize("text", ["SIGINT", "signal: 11", "failed to run custom build command"])
def test_metadata_package_text_is_data_not_a_signal(fixture, text):
    def mutate(phase, root, r):
        if phase == "metadata":
            graph = json.loads(r["stdout"])
            graph["packages"][0]["description"] = text
            graph["packages"][0]["keywords"] = [text]
            r["stdout"] = json.dumps(graph)
    code, report, calls = execute(fixture, change=mutate)
    assert code == 0 and calls == ["metadata", "cargo"]
    assert text in report["metadata"]["stdout"]


@pytest.mark.parametrize("code", [1, 101, 124, 126, 127, 137, -2, -6, -9, -11, -15])
def test_real_assertion_and_process_failure_codes_are_never_success_or_reselection(fixture, code):
    status, report, calls = execute(fixture, code=code)
    assert status == (code if code >= 0 else 128 - code)
    assert report["execution"]["returncode"] == code and calls == ["metadata", "cargo"]
    assert not all(report["test_results"].values())


@pytest.mark.parametrize("change", ["stderr_signal", "stdout_signal", "limit", "missing_id", "mutation"])
def test_zero_exit_cannot_hide_infrastructure_incomplete_tests_or_source_changes(fixture, change):
    def mutate(phase, root, r):
        if phase == "cargo":
            if change == "stderr_signal":
                r["stderr"] = "SIGSEGV: signal11"
            elif change == "stdout_signal":
                r["stdout"] += "\nSIGABRT\n"
            elif change == "limit":
                r["limit"] = "deadline"
            elif change == "missing_id":
                r["stdout"] = "test result: ok.\n"
            else:
                (root / "src/current.rs").write_text("// late mutation\n")
    code, report, calls = execute(fixture, change=mutate)
    assert code == 2 and calls == ["metadata", "cargo"]
    assert report["execution"]["returncode"] == 0


@pytest.mark.parametrize("field", ["task_id", "profile", "toolchain", "cpus", "max_log_bytes",
                                  "metadata_timeout_seconds", "receipt_sha256", "extra"])
def test_unknown_spec_never_runs_a_command(fixture, field):
    fixture.spec[field] = "changed"
    code, _, calls = execute(fixture)
    assert code == 2 and calls == []


@pytest.mark.parametrize("field", ["command", "harness", "expected_tests", "task_id"])
def test_private_identity_and_harness_contract_cannot_be_changed(fixture, field):
    fixture.private = copy.deepcopy(fixture.private)
    fixture.private[field] = [] if field in {"command", "expected_tests"} else {}
    code, _, calls = execute(fixture)
    assert code == 2 and calls == []


def test_full_current_multifile_sync_deletes_stale_build_source_and_targets(fixture):
    f = fixture
    mirror = f.build / "source"
    (mirror / "target").mkdir(parents=True)
    (mirror / "target/old.rlib").write_bytes(b"not a reusable dependency")
    (mirror / "stale.rs").write_text("old candidate")
    (f.source / "src/extra.rs").write_text("current addition")
    before = entry.snapshot(f.source)
    code, report, _ = execute(f)
    assert code == 0 and entry.snapshot(f.source) == before
    assert not (mirror / "target").exists() and not (mirror / "stale.rs").exists()
    assert (mirror / "src/extra.rs").read_text() == "current addition"
    assert report["mirror"]["synchronization"]["deleted"] == 2


def test_public_compact_private_complete_evidence_survives_scratch_disposal(fixture, capsys):
    code, public, _ = execute(fixture, private=False)
    assert code == 0
    entry.publish(public, None)
    text = capsys.readouterr().out
    assert len(text) < 1000 and "metadata" not in text and entry.EXPECTED[0] not in text
    # A second invocation uses a fresh record directory and resynchronizes the
    # same complete source; this is a tool data fixture, not a task replay.
    code, private, _ = execute(fixture)
    assert code == 0
    entry.publish(private, fixture.private)
    text = capsys.readouterr().out
    lines = text.splitlines()
    assert len(lines) == 1 and lines[0].startswith(entry.EVIDENCE_PREFIX)
    value = json.loads(lines[0][len(entry.EVIDENCE_PREFIX):])
    assert value["metadata"]["stdout"] == private["metadata"]["stdout"]
    assert value["mirror"]["source_before"] and value["transport"]["original_argv"]
    assert len(list(fixture.build.glob("first-new-*/record.json"))) == 2


@pytest.mark.parametrize("tail", [["update"], ["generate-lockfile"], ["fetch"], ["test"],
                                  ["check", "--lib"], ["test", "--test", "different", "--", "--nocapture"]])
def test_no_runtime_resolver_or_narrowed_original_command(tail):
    with pytest.raises(entry.OverlayError):
        entry.transport(["cargo", *entry.PREFIX, *tail], None)


def test_transport_only_adds_locked_offline_before_libtest_separator():
    original = ["cargo", *entry.PREFIX, *entry.PRIVATE_COMMAND]
    effective = entry.transport(original, {})
    assert effective == [entry.REAL_CARGO, *entry.PREFIX, "test", "--test", "oracle_session",
                         "--locked", "--offline", "--", "--nocapture"]
    assert original == ["cargo", *entry.PREFIX, *entry.PRIVATE_COMMAND]


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGABRT, signal.SIGKILL, signal.SIGSEGV])
def test_real_local_child_signal_and_literal_stream_capture(tmp_path, sig, capfdbinary):
    logs = tmp_path / "logs"
    logs.mkdir()
    code = ("import os,signal,resource,faulthandler;faulthandler.disable();"
            "resource.setrlimit(resource.RLIMIT_CORE,(0,0));"
            "os.write(1,b'literal-out\\n');os.write(2,b'literal-err\\n');os.kill(os.getpid(),"
            + str(int(sig)) + ")")
    command = [sys.executable, "-c", code]
    r = entry.run_logged(command, tmp_path, dict(os.environ), logs, "child",
                         time.monotonic() + 10, 1024**2, forward=True)
    out = capfdbinary.readouterr()
    assert r["returncode"] == -sig and r["limit"] is None
    assert out.out == b"literal-out\n" and out.err == b"literal-err\n"
    assert Path(r["stdout_path"]).read_bytes() == out.out
    assert Path(r["stderr_path"]).read_bytes() == out.err


@pytest.mark.parametrize("private", [False, True])
def test_unselected_test_false_bench_may_be_missing_but_full_json_survives(fixture, private):
    def mutate(phase, root, r):
        if phase == "metadata":
            graph = json.loads(r["stdout"])
            graph["packages"][0]["targets"].append({
                "name": "proto", "kind": ["bench"], "test": False,
                "required-features": ["protobuf"], "src_path": str(root / "benches/encoding/proto.rs")})
            r["stdout"] = json.dumps(graph)
    code, report, calls = execute(fixture, private=private, change=mutate)
    assert code == 0 and calls == ["metadata", "cargo"]
    target = json.loads(report["metadata"]["stdout"])["packages"][0]["targets"][-1]
    assert target["kind"] == ["bench"] and target["test"] is False
    assert target["required-features"] == ["protobuf"]
    assert not Path(target["src_path"]).exists()
    assert "--all-targets" not in report["transport"]["effective_argv"]


@pytest.mark.parametrize("kind,test,path", [
    (["bench"], True, "missing.rs"),
    (["lib"], False, "missing.rs"),
    (["bin"], False, "missing.rs"),
    (["proc-macro"], False, "missing.rs"),
    (["custom-build"], False, "missing.rs"),
    (["example"], False, "missing.rs"),
    (["bench"], False, "/outside/proto.rs"),
    (["bench"], False, "../outside/proto.rs"),
])
def test_bench_exception_never_waives_production_or_path_guards(fixture, kind, test, path):
    def mutate(phase, root, r):
        if phase == "metadata":
            graph = json.loads(r["stdout"])
            graph["packages"][0]["targets"].append({
                "name": "unbound-target", "kind": kind, "test": test, "src_path": str(root / path)})
            r["stdout"] = json.dumps(graph)
    code, report, calls = execute(fixture, change=mutate)
    assert code == 2 and calls == ["metadata"]


def test_oracle_redirect_to_missing_bench_kind_is_still_rejected(fixture):
    def mutate(phase, root, r):
        if phase == "metadata":
            graph = json.loads(r["stdout"])
            graph["packages"][0]["targets"][1].update(kind=["bench"], test=False,
                                                     src_path=str(root / "missing.rs"))
            r["stdout"] = json.dumps(graph)
    code, _, calls = execute(fixture, change=mutate)
    assert code == 2 and calls == ["metadata"]


@pytest.mark.parametrize("change", ["missing", "checksum", "manifest", "escape"])
def test_current_external_graph_requires_provided_vendor_before_sut(fixture, change):
    f = fixture
    external = {"name": "external-tool-fixture", "version": "1.0.0", "source": entry.REGISTRY,
                "checksum": "a" * 64}
    own = lock_bytes(entry.SELECTORS[0], [external])
    own = own.replace(json.dumps([entry.SELECTORS[0]]).encode(),
                      json.dumps([entry.SELECTORS[0], external["name"]]).encode())
    (f.source / "Cargo.lock").write_bytes(own)
    package = f.vendor / "external-tool-fixture-1.0.0"
    (package / "src").mkdir(parents=True)
    (package / "src/lib.rs").write_text("// public source fixture\n")
    manifest = package / "Cargo.toml"
    manifest.write_text('[package]\nname="external-tool-fixture"\nversion="1.0.0"\n')
    checksum = package / ".cargo-checksum.json"
    checksum.write_text(json.dumps({"package": "a" * 64,
                                   "files": {"Cargo.toml": entry.file_hash(manifest)}}))
    if change == "missing":
        checksum.unlink()
    elif change == "checksum":
        checksum.write_text(json.dumps({"package": "b" * 64, "files": {}}))
    elif change == "manifest":
        manifest.write_text(manifest.read_text() + "# changed after source packaging\n")
    def mutate(phase, root, r):
        if change == "escape" and phase == "metadata":
            graph = json.loads(r["stdout"])
            graph["packages"][-1]["manifest_path"] = "/outside/Cargo.toml"
            r["stdout"] = json.dumps(graph)
    code, _, calls = execute(f, change=mutate)
    assert code == 2
    assert calls == (["metadata"] if change == "escape" else [])
