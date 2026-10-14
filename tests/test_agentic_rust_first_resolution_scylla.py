"""Scylla96 runtime protocol controls; records are LOCAL TOOL fixtures."""
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_first_resolution_entry as entry
from test_agentic_rust_first_resolution_runtime import record

HARNESS_BYTES = b'''use scylla::transport::topology::Metadata;
use std::net::SocketAddr;

struct OneAddressArgument;
struct TwoAddressArguments;

trait BuildDummyMetadata<Marker> {
    fn build(self, peers: &[SocketAddr], control: SocketAddr) -> Metadata;
}

impl<F> BuildDummyMetadata<OneAddressArgument> for F
where
    F: Fn(&[SocketAddr]) -> Metadata,
{
    fn build(self, peers: &[SocketAddr], _control: SocketAddr) -> Metadata {
        self(peers)
    }
}

impl<F> BuildDummyMetadata<TwoAddressArguments> for F
where
    F: Fn(&[SocketAddr], SocketAddr) -> Metadata,
{
    fn build(self, peers: &[SocketAddr], control: SocketAddr) -> Metadata {
        self(peers, control)
    }
}

fn build_dummy<F, Marker>(factory: F, peers: &[SocketAddr], control: SocketAddr) -> Metadata
where
    F: BuildDummyMetadata<Marker>,
{
    factory.build(peers, control)
}

#[test]
fn dummy_metadata_assigns_each_initial_peer_a_distinct_token() {
    let peers = [
        "127.0.0.1:9042".parse().unwrap(),
        "127.0.0.2:9042".parse().unwrap(),
    ];
    let metadata = build_dummy(Metadata::new_dummy, &peers, peers[0]);

    assert_eq!(metadata.peers.len(), peers.len());
    assert_eq!(metadata.peers[0].address, peers[0]);
    assert_eq!(metadata.peers[1].address, peers[1]);
    assert_ne!(metadata.peers[0].tokens, metadata.peers[1].tokens);
}
'''
MEMBERS = ("examples", "scylla", "scylla-macros", "scylla-cql")


def lock_bytes(version="0.5.0", external=()):
    rows = []
    for name in MEMBERS:
        row = {"name": name, "version": version if name == "scylla" else "0.1.0"}
        if name == "examples":
            row["dependencies"] = ["scylla"]
        elif name == "scylla":
            row["dependencies"] = ["scylla-cql", "scylla-macros", *[p["name"] for p in external]]
        rows.append(row)
    rows.extend(external)
    return b"version = 3\n" + b"".join(
        ("\n[[package]]\n" + "".join(k + "=" + json.dumps(v) + "\n" for k, v in p.items())).encode()
        for p in rows)


def create_source(root):
    root.mkdir()
    (root / "Cargo.toml").write_text(
        "[workspace]\nmembers=" + json.dumps(list(MEMBERS)) + '\ndefault-members=["examples"]\n')
    for name in MEMBERS:
        (root / name / "src").mkdir(parents=True)
        (root / name / "Cargo.toml").write_text(
            '[package]\nname=' + json.dumps(name) + '\nversion=' +
            json.dumps("0.5.0" if name == "scylla" else "0.1.0") + '\nedition="2021"\n')
        (root / name / "src/lib.rs").write_text("pub fn current() -> bool { true }\n")
    with (root / "examples/Cargo.toml").open("a") as out:
        out.write('[dependencies]\nscylla={path="../scylla"}\n')
    with (root / "scylla/Cargo.toml").open("a") as out:
        out.write('[dependencies]\nscylla-cql={path="../scylla-cql"}\n'
                  'scylla-macros={path="../scylla-macros"}\n')
    (root / "scylla/tests").mkdir()
    assert entry.digest(HARNESS_BYTES) == entry.SCYLLA_HARNESS["sha256"]
    assert HARNESS_BYTES.count(b"assert_") == 4
    (root / entry.SCYLLA_HARNESS["path"]).write_bytes(HARNESS_BYTES)


@pytest.fixture
def scylla(tmp_path, monkeypatch):
    root = tmp_path / "candidate"
    create_source(root)
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("cargo", "rustc"):
        (tools / name).write_text("authenticated LOCAL TOOL record: " + name)
    monkeypatch.setattr(entry, "REAL_CARGO", str(tools / "cargo"))
    monkeypatch.setattr(entry, "REAL_RUSTC", str(tools / "rustc"))
    default = tmp_path / "original-prepare-final-tool.lock"
    default.write_bytes(lock_bytes())
    target = "/opt/reviewbench-assets/rust-new-lock-0"
    spec = {
        "schema_version": entry.SCHEMA, "task_id": entry.SCYLLA_TASK, "profile": entry.SCYLLA_PROFILE,
        "toolchain": "1.64.0", "cargo_sha256": entry.file_hash(tools / "cargo"),
        "rustc_sha256": entry.file_hash(tools / "rustc"), "cpus": 4,
        "max_log_bytes": 33554432, "metadata_timeout_seconds": 120,
        "command_timeout_seconds": 2400, "receipt_sha256": "f" * 64,
        "defaults": [{"selector_package": "scylla", "asset_target": target,
                      "sha256": entry.file_hash(default)}],
    }
    private = {"schema_version": entry.PRIVATE_SCHEMA, "task_id": entry.SCYLLA_TASK,
               "command": list(entry.SCYLLA_COMMAND), "harness": dict(entry.SCYLLA_HARNESS),
               "expected_tests": list(entry.SCYLLA_EXPECTED)}
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    return SimpleNamespace(source=root, build=tmp_path / "build", vendor=vendor,
                           spec=spec, private=private, locks={target: default})


def move_scylla(f):
    """Current manifest/source move keeps the original harness at its fixed path."""
    folder = f.source / "members/driver"
    folder.mkdir(parents=True)
    (f.source / "scylla/src").rename(folder / "src")
    (f.source / "scylla/Cargo.toml").rename(folder / "Cargo.toml")
    p = f.source / "Cargo.toml"
    p.write_text(p.read_text().replace('"scylla"', '"./members/driver/"'))
    p = f.source / "examples/Cargo.toml"
    p.write_text(p.read_text().replace("../scylla", "../members/driver"))
    p = folder / "Cargo.toml"
    p.write_text(p.read_text().replace('path="../scylla-', 'path="../../scylla-') +
                 '[[test]]\nname="oracle_session"\npath="../../scylla/tests/oracle_session.rs"\n')
    return folder


def graph_for(root, f):
    packages = entry.parse_lock((root / "Cargo.lock").read_bytes(), f.spec)
    rows, keys = [], {}
    for i, key in enumerate(packages):
        keys[key] = "package-" + str(i)
        if key[2] is None:
            manifests = [p for p in root.rglob("Cargo.toml")
                         if entry.tomllib.loads(p.read_text()).get("package", {}).get("name") == key[0]]
            assert len(manifests) == 1
            path = manifests[0]
        else:
            path = f.vendor / (key[0] + "-" + key[1]) / "Cargo.toml"
        targets = [{"kind": ["lib"], "name": key[0].replace("-", "_"),
                    "src_path": str(path.parent / "src/lib.rs"), "test": True}]
        if key[0] == "scylla":
            targets.append({"kind": ["test"], "name": "oracle_session",
                            "src_path": str(root / entry.SCYLLA_HARNESS["path"]), "test": True})
        rows.append({"id": keys[key], "name": key[0], "version": key[1], "source": key[2],
                     "manifest_path": str(path), "targets": targets, "features": {}})
    nodes = []
    for key, package in packages.items():
        deps = entry.lock_edges(packages, package)
        nodes.append({"id": keys[key], "features": [], "dependencies": [keys[d] for d in deps],
                      "deps": [{"name": d[0].replace("-", "_"), "pkg": keys[d],
                                "dep_kinds": [{"kind": None, "target": None}]} for d in deps]})
    # Cargo1.64 does not supply workspace_default_members.
    return {"version": 1, "workspace_root": str(root),
            "workspace_members": [r["id"] for r in rows if r["source"] is None],
            "packages": rows, "resolve": {"root": None, "nodes": nodes}}


def execute(f, *, public=False, mutate=None, code=0):
    calls = []
    def run(command, root, environment, logs, name, deadline, limit, **kwargs):
        calls.append({"name": name, "command": command, "deadline": deadline})
        assert environment["CARGO_BUILD_JOBS"] == "4"
        assert "--locked" in command and "--offline" in command and "--all-targets" not in command
        if name == "metadata":
            assert "--all-features" in command and "--no-deps" not in command and "-p" not in command
            r = record(stdout=json.dumps(graph_for(root, f)))
        else:
            assert command[command.index("-p") + 1] == "scylla"
            r = record(code, "test " + entry.SCYLLA_EXPECTED[0] + " ... " + ("ok" if code == 0 else "FAILED")
                       + "\ntest result: " + ("ok." if code == 0 else "FAILED.") + "\n")
        if mutate:
            mutate(name, root, r)
        return r
    p = entry.profile_for({"task_id": entry.SCYLLA_TASK, "profile": entry.SCYLLA_PROFILE})
    command = ["cargo", *p["prefix"], *(p["public"] if public else p["command"])]
    result = entry.execute(f.spec, f.source, f.build, command, None if public else f.private,
                           run=run, lock_sources=f.locks, vendor=f.vendor)
    return *result, calls


@pytest.mark.parametrize("public", [False, True])
@pytest.mark.parametrize("layout", ["original", "moved", "attributes", "normalized", "glob"])
def test_single_default_uses_current_workspace_package_not_phase_or_manifest_hash(scylla, public, layout, monkeypatch):
    f = scylla
    if layout in {"moved", "glob"}:
        move_scylla(f)
        if layout == "glob":
            p = f.source / "Cargo.toml"
            p.write_text(p.read_text().replace('"./members/driver/"', '"members/*"'))
    elif layout == "attributes":
        p = f.source / "scylla/Cargo.toml"
        p.write_text(p.read_text() + '[package.metadata.current]\nnote="harmless complete candidate"\n')
    elif layout == "normalized":
        p = f.source / "Cargo.toml"
        p.write_text(p.read_text().replace('"scylla"', '"./scylla/."'))
    monkeypatch.setenv("ORACLE_VARIANT", "ignored-reference-label")
    before = entry.snapshot(f.source)
    code, report, calls = execute(f, public=public)
    assert code == 0, report
    assert [c["name"] for c in calls] == ["metadata", "cargo"]
    assert report["mirror"]["selection"]["selector_package"] == "scylla"
    assert "selector_version" not in report["mirror"]["selection"]
    assert report["graph"]["packages"] == 4
    assert report["binding"]["toolchain"] == "1.64.0"
    assert entry.snapshot(f.source) == before and not (f.source / "Cargo.lock").exists()


def test_authored_changed_graph_and_different_lock_bytes_bypass_default_without_overwrite(scylla, monkeypatch):
    f = scylla
    p = f.source / "scylla/Cargo.toml"
    p.write_text(p.read_text().replace("0.5.0", "0.5.99") + '[package.metadata.current]\ntext="SIGINT"\n')
    own = lock_bytes("0.5.99") + b"\n# candidate-authored text and valid changed graph\n"
    (f.source / "Cargo.lock").write_bytes(own)
    monkeypatch.setattr(entry, "select_workspace_default", lambda *a: pytest.fail("must not select an authored lock"))
    before = entry.snapshot(f.source)
    code, report, _ = execute(f)
    assert code == 0 and report["mirror"]["selected_default"] is None, report
    assert (f.build / "source/Cargo.lock").read_bytes() == own and entry.snapshot(f.source) == before


@pytest.mark.parametrize("problem", ["empty", "both", "missing_manifest", "symlink", "escape", "asset",
                                  "harness", "lock", "missing_source"])
def test_invalid_source_default_or_original_harness_aborts_before_actual_command(scylla, problem):
    f = scylla
    if problem == "empty":
        p = f.source / "scylla/Cargo.toml"
        p.write_text(p.read_text().replace('name="scylla"', 'name="not-scylla"'))
    elif problem == "both":
        p = f.source / "scylla-cql/Cargo.toml"
        p.write_text(p.read_text().replace('name="scylla-cql"', 'name="scylla"'))
    elif problem == "missing_manifest":
        (f.source / "scylla/Cargo.toml").unlink()
    elif problem == "symlink":
        p = f.source / "scylla/Cargo.toml"
        p.unlink(); p.symlink_to("../scylla-cql/Cargo.toml")
    elif problem == "escape":
        p = f.source / "Cargo.toml"
        p.write_text(p.read_text().replace('"scylla"', '"../outside"'))
    elif problem == "asset":
        next(iter(f.locks.values())).write_bytes(b"wrong final default")
    elif problem == "harness":
        (f.source / entry.SCYLLA_HARNESS["path"]).write_bytes(b"changed original assertions")
    elif problem == "lock":
        (f.source / "Cargo.lock").write_bytes(b"not a valid current lock")
    else:
        (f.source / "scylla/src/lib.rs").unlink()
    code, report, calls = execute(f)
    assert code == 2 and report["status"] == "rejected"
    assert [c["name"] for c in calls] == (["metadata"] if problem == "missing_source" else [])


@pytest.mark.parametrize("problem", ["package", "node", "edge", "feature", "member", "local_path",
                                  "redirect", "disabled", "target_missing", "selected_package",
                                  "prefix", "suffix", "truncated", "signal", "error", "malformed",
                                  "stdout_utf8", "stderr_utf8", "output_limit", "deadline", "source_mutation"])
def test_full_metadata_selected_target_and_process_guards(scylla, problem):
    def mutate(phase, root, r):
        assert phase == "metadata", "bad metadata reached the actual command"
        graph = json.loads(r["stdout"])
        selected = next(p for p in graph["packages"] if p["name"] == "scylla")
        node = next(n for n in graph["resolve"]["nodes"] if n["id"] == selected["id"])
        if problem == "package":
            graph["packages"].pop()
        elif problem == "node":
            graph["resolve"]["nodes"].pop()
        elif problem == "edge":
            node["dependencies"] = []; node["deps"] = []
        elif problem == "feature":
            node["features"] = ["undeclared"]
        elif problem == "member":
            graph["workspace_members"].remove(selected["id"])
        elif problem == "local_path":
            selected["manifest_path"] = "/outside/Cargo.toml"
        elif problem == "redirect":
            selected["targets"][1]["src_path"] = str(root / "examples/src/lib.rs")
        elif problem == "disabled":
            selected["targets"][1]["test"] = False
        elif problem == "target_missing":
            selected["targets"].pop()
        elif problem == "selected_package":
            selected["name"] = "not-scylla"
        elif problem == "truncated":
            r["stdout_total_bytes"] += 1
        elif problem == "signal":
            r["stderr"] = "process exited with signal: 11, SIGSEGV\n"
        elif problem == "error":
            r["returncode"] = 101
        elif problem in {"stdout_utf8", "stderr_utf8"}:
            r[problem + "_valid"] = False
        elif problem in {"output_limit", "deadline"}:
            r["limit"] = problem
        elif problem == "source_mutation":
            (root / "scylla/src/lib.rs").write_text("// mutated current source")
        r["stdout"] = json.dumps(graph)
        if problem == "prefix":
            r["stdout"] = "extra stdout\n" + r["stdout"]
        elif problem == "suffix":
            r["stdout"] += "\n{}"
        elif problem == "malformed":
            r["stdout"] = "{"
    code, report, calls = execute(scylla, mutate=mutate)
    assert code == 2 and [c["name"] for c in calls] == ["metadata"]
    assert report["default_resolution_commands"] == 0


@pytest.mark.parametrize("text", ["SIGINT", "signal: 11", "failed to run custom build command"])
def test_metadata_signal_words_are_data_and_no_default_members_field_is_needed(scylla, text, capsys):
    def mutate(phase, root, r):
        if phase == "metadata":
            graph = json.loads(r["stdout"])
            assert graph["resolve"]["root"] is None and "workspace_default_members" not in graph
            graph["packages"][0]["description"] = text
            r["stdout"] = json.dumps(graph)
    code, report, _ = execute(scylla, mutate=mutate)
    assert code == 0, report
    entry.publish(report, scylla.private)
    output = capsys.readouterr().out
    assert output.count(entry.EVIDENCE_PREFIX) == 1
    assert text in json.loads(output.split(entry.EVIDENCE_PREFIX, 1)[1])["metadata"]["stdout"]


@pytest.mark.parametrize("code", [1, 101, 124, 126, 127, 130, 137, -2, -6, -9, -11, -15])
def test_actual_failed_assertions_or_signals_preserve_codes_without_reselection(scylla, code):
    result, report, calls = execute(scylla, code=code)
    assert result == (code if code >= 0 else 128 - code)
    assert report["execution"]["returncode"] == code
    assert [c["name"] for c in calls] == ["metadata", "cargo"]
    assert not report["selection_uses_test_results"] and report["default_resolution_commands"] == 0


@pytest.mark.parametrize("problem", ["stdout_signal", "stderr_signal", "limit", "missing_id", "mutation"])
def test_zero_cargo_code_cannot_hide_failure_or_changed_source(scylla, problem):
    def mutate(phase, root, r):
        if phase != "cargo":
            return
        if problem == "stdout_signal":
            r["stdout"] += "terminated by SIGABRT\n"
        elif problem == "stderr_signal":
            r["stderr"] += "signal: 11, SIGSEGV\n"
        elif problem == "limit":
            r["limit"] = "output_limit"
        elif problem == "missing_id":
            r["stdout"] = "test result: ok. 0 passed;\n"
        else:
            (root / "scylla/src/lib.rs").write_text("// current source mutated by command")
    code, report, calls = execute(scylla, mutate=mutate)
    assert code == 2 and report["execution"]["returncode"] == 0
    assert [c["name"] for c in calls] == ["metadata", "cargo"]


def add_registry(f):
    package = {"name": "fixture-external", "version": "0.1.0", "source": entry.REGISTRY, "checksum": "a" * 64}
    folder = f.vendor / "fixture-external-0.1.0"
    (folder / "src").mkdir(parents=True)
    (folder / "Cargo.toml").write_text('[package]\nname="fixture-external"\nversion="0.1.0"\n')
    (folder / "src/lib.rs").write_text("// immutable local fixture dependency\n")
    (folder / ".cargo-checksum.json").write_text(json.dumps({
        "package": package["checksum"], "files": {"Cargo.toml": entry.file_hash(folder / "Cargo.toml")}}))
    (f.source / "Cargo.lock").write_bytes(lock_bytes(external=[package]))
    return folder


@pytest.mark.parametrize("problem", [None, "missing", "checksum", "manifest", "escape", "git"])
def test_flat_registry_vendor_and_current_lock_identity_are_required(scylla, problem):
    f = scylla
    folder = add_registry(f)
    if problem == "missing":
        (folder / "Cargo.toml").unlink()
    elif problem == "checksum":
        p = folder / ".cargo-checksum.json"
        d = json.loads(p.read_text()); d["package"] = "b" * 64; p.write_text(json.dumps(d))
    elif problem == "manifest":
        (folder / "Cargo.toml").write_text('[package]\nname="wrong"\nversion="0.1.0"\n')
    elif problem == "escape":
        p = folder / "Cargo.toml"
        p.unlink(); p.symlink_to(f.source / "scylla/Cargo.toml")
    elif problem == "git":
        p = f.source / "Cargo.lock"
        p.write_text(p.read_text().replace(entry.REGISTRY, "git+https://example.invalid/local-tool#" + "a" * 40))
    code, report, calls = execute(f)
    assert code == (0 if problem is None else 2), report
    assert [c["name"] for c in calls] == (["metadata", "cargo"] if problem is None else [])


@pytest.mark.parametrize("field,value", [
    ("task_id", "another"), ("profile", "another"), ("toolchain", "1.93.0"),
    ("cpus", 2), ("command_timeout_seconds", 1800), ("command_timeout_seconds", 2401),
    ("command_timeout_seconds", True), ("metadata_timeout_seconds", 121),
    ("max_log_bytes", 33554433), ("source_config_sha256", "a" * 64), ("vendor_sources", {}),
])
def test_scylla_only_toolchain_timeout_resource_and_extra_field_contract(scylla, field, value):
    scylla.spec[field] = value
    code, _, calls = execute(scylla)
    assert code == 2 and calls == []


@pytest.mark.parametrize("problem", ["none", "two", "selector", "version", "target", "hash"])
def test_exact_one_shared_default_shape(scylla, problem):
    defaults = scylla.spec["defaults"]
    if problem == "none":
        defaults.clear()
    elif problem == "two":
        defaults.append(copy.deepcopy(defaults[0]))
    elif problem == "selector":
        defaults[0]["selector_package"] = "examples"
    elif problem == "version":
        defaults[0]["selector_version"] = "0.5.0"
    elif problem == "target":
        defaults[0]["asset_target"] = "/opt/reviewbench-assets/rust-new-lock-1"
    else:
        defaults[0]["sha256"] = "unbound"
    code, _, calls = execute(scylla)
    assert code == 2 and calls == []


def test_original_2400_deadline_and_four_jobs_are_profile_scoped(scylla, monkeypatch):
    monkeypatch.setattr(entry.time, "monotonic", lambda: 100.0)
    code, report, calls = execute(scylla)
    assert code == 0, report
    assert [(c["name"], c["deadline"]) for c in calls] == [("metadata", 220.0), ("cargo", 2500.0)]
    assert report["binding"]["command_timeout_seconds"] == 2400
    assert entry.COMMAND_SECONDS == 1800


@pytest.mark.parametrize("text,valid", [
    ("", True), (" \n", True), ("warning: unused manifest key: package.tool\n", True),
    ("Blocking waiting for file lock on package cache\n", True),
    (entry.FONT_RESOLVER_WARNING, True),
    (entry.FONT_RESOLVER_WARNING.splitlines()[0] + "\n", False),
    (entry.FONT_RESOLVER_WARNING + "error: dependency failed\n", False),
    ("warning: unaccounted process output\n", False),
])
def test_only_bounded_original_metadata_warning_forms_are_recognized_and_retained(scylla, text, valid):
    def mutate(phase, root, r):
        if phase == "metadata":
            r["stderr"] = text
    code, report, calls = execute(scylla, mutate=mutate)
    assert code == (0 if valid else 2)
    assert report["metadata"]["stderr"] == text
    assert len(calls) == (2 if valid else 1)


def test_prom_font_defaults_constants_and_python310_empty_git_query_remain_preserved(scylla, monkeypatch):
    assert entry.TASK.startswith("github__prometheus__")
    assert entry.profile_for()["public"] == ["check"] and entry.COMMAND_SECONDS == 1800
    font = entry.profile_for({"task_id": entry.FONT_TASK, "profile": entry.FONT_PROFILE})
    assert font["public"] == ["check", "-p", "punchcut"] and font["font"] is True
    old_parser = entry.parse_qsl
    def python310_parser(query, **kwargs):
        assert query, "Python3.10 strict parser must not receive an empty Git query"
        return old_parser(query, **kwargs)
    monkeypatch.setattr(entry, "parse_qsl", python310_parser)
    source = "git+https://example.invalid/fixed#" + "a" * 40
    assert entry.git_source(source) == ("https://example.invalid/fixed", {}, "a" * 40)


@pytest.mark.parametrize("field", ["command", "harness", "expected_tests", "task_id"])
def test_original_private_identity_cannot_be_changed(scylla, field):
    scylla.private[field] = "changed"
    code, _, calls = execute(scylla)
    assert code == 2 and calls == []


@pytest.mark.parametrize("tail", [
    ["check"], ["check", "--workspace"], ["test", "--test", "oracle_session", "--", "--nocapture"],
    ["update"], ["generate-lockfile"], ["fetch"],
])
def test_no_original_command_narrowing_or_runtime_resolution(scylla, tail):
    with pytest.raises(entry.OverlayError):
        entry.transport(["cargo", *entry.PREFIX, *tail], spec=scylla.spec)


@pytest.mark.parametrize("path_kind", ["cargo164_relative", "same_bytes_other_path", "escape", "missing"])
def test_cargo164_target_path_normalization_never_allows_a_different_harness(scylla, path_kind):
    f = scylla
    move_scylla(f)
    if path_kind == "same_bytes_other_path":
        (f.source / "scylla/tests/copied.rs").write_bytes(HARNESS_BYTES)
    def mutate(phase, root, r):
        if phase != "metadata":
            return
        graph = json.loads(r["stdout"])
        selected = next(p for p in graph["packages"] if p["name"] == "scylla")
        relative = {
            "cargo164_relative": "members/driver/../../scylla/tests/oracle_session.rs",
            "same_bytes_other_path": "members/driver/../../scylla/tests/copied.rs",
            "escape": "members/driver/../../../outside.rs",
            "missing": "members/driver/../../scylla/tests/missing.rs",
        }[path_kind]
        selected["targets"][1]["src_path"] = str(root / relative)
        r["stdout"] = json.dumps(graph)
    code, report, calls = execute(f, mutate=mutate)
    assert code == (0 if path_kind == "cargo164_relative" else 2), report
    assert [c["name"] for c in calls] == (["metadata", "cargo"] if code == 0 else ["metadata"])
    assert "/members/driver/../.." in report["metadata"]["stdout"]
