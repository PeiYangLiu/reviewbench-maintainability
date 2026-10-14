"""Blazesym runtime LOCAL TOOL data controls, never original SUT evaluations."""
import copy
import gzip
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
from types import SimpleNamespace

import pytest
import tomli_w

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_blazesym_entry as entry
from test_agentic_rust_first_resolution_runtime import record


def crate_files(name, *, modern=False):
    return {"Cargo.toml": ('[package]\nname="' + name + '"\nversion="0.1.0"\nedition="' +
                           ("2024" if modern else "2021") + '"\n').encode(),
            "src/lib.rs": b"pub fn bound() -> bool { true }\n"}


def create_registry(f, duplicate=None):
    f.registry.mkdir()
    contents, rows, files = {}, [], {}
    for name in ["tool-used", "tool-modern", *["tool-pad-" + str(i) for i in range(53)]]:
        content = crate_files(name, modern=name == "tool-modern")
        contents[(name, "0.1.0", entry.REGISTRY_SOURCE)] = content
        memory = io.BytesIO()
        with tarfile.open(fileobj=memory, mode="w") as tar:
            items = list(content.items())
            if name == "tool-pad-0" and duplicate:
                items.append(("src/lib.rs", content["src/lib.rs"] if duplicate == "identical" else b"conflict\n"))
            for path, body in items:
                info = tarfile.TarInfo(name + "-0.1.0/" + path)
                info.mode, info.size = 0o644, len(body)
                tar.addfile(info, io.BytesIO(body))
        archive = f.registry / (name + "-0.1.0.crate")
        archive.write_bytes(gzip.compress(memory.getvalue(), mtime=0))
        checksum = entry.file_hash(archive)
        index = {"name": name, "vers": "0.1.0", "deps": [], "cksum": checksum,
                 "features": {}, "yanked": False}
        line = json.dumps(index, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        path = f.registry / "index" / entry.index_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(line)
        rows.append({"name": name, "version": "0.1.0", "cksum": checksum,
                     "index_path": entry.index_path(name), "original_line_sha256": entry.sha(line)})
        for p in [archive, path]:
            files[p.relative_to(f.registry).as_posix()] = {
                "sha256": entry.file_hash(p), "bytes": p.stat().st_size, "mode": p.stat().st_mode & 0o777}
    document = {"schema_version": entry.REGISTRY_SCHEMA, "source": entry.REGISTRY_SOURCE,
                "packages": rows, "files": files}
    f.registry_manifest.write_text(json.dumps(document, sort_keys=True) + "\n")
    f.contents, f.registry_document = contents, document
    return document


def lock_bytes(f, version="0.1.0"):
    rows = [{"name": "blazesym", "version": version, "dependencies": ["blazesym", "tool-modern", "tool-used"]}]
    for name in ("tool-modern", "tool-used"):
        package = next(p for p in f.registry_document["packages"] if p["name"] == name)
        rows.append({"name": name, "version": "0.1.0", "source": entry.REGISTRY_SOURCE,
                     "checksum": package["cksum"]})
    return tomli_w.dumps({"version": 3, "package": rows}).encode()


@pytest.fixture
def blaze(tmp_path, monkeypatch):
    source = tmp_path / "candidate"
    (source / "src").mkdir(parents=True)
    (source / "tests").mkdir()
    (source / "data").mkdir()
    (source / "Cargo.toml").write_text(
        '[package]\nname="blazesym"\nversion="0.1.0"\nedition="2021"\n'
        '[dependencies]\ntool-used="=0.1.0"\ntool-modern={version="=0.1.0",optional=true}\n'
        '[dev-dependencies]\nblazesym={path=".",features=["generate-test-files"]}\n'
        '[features]\ngenerate-test-files=[]\ndont-generate-test-files=[]\nnightly=[]\n')
    (source / "src/lib.rs").write_text("// LOCAL TOOL source\n")
    (source / "build.rs").write_text("fn main() {}\n")
    (source / "tests/blazesym.rs").write_bytes(HARNESS_BYTES)
    for path, body in FIXTURE_BYTES.items():
        (source / path).write_bytes(body)
        assert entry.sha(body) == entry.FIXTURES[path]
    (source / "data/dwarf-example").write_bytes(b"committed LOCAL TOOL data, never scratch\n")
    (source / "data/dwarf-example").chmod(0o755)
    assert entry.sha(HARNESS_BYTES) == entry.HARNESS["sha256"]
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("cargo", "rustc", "metadata-cargo", "metadata-rustc", "cc", "strip", "llvm-gsymutil-14"):
        p = tools / name
        p.write_text("#!/bin/sh\n# LOCAL TOOL " + name + "\nexit 0\n"); p.chmod(0o755)
    for name, tool in [("REAL_CARGO", "cargo"), ("REAL_RUSTC", "rustc"),
                       ("METADATA_CARGO", "metadata-cargo"), ("METADATA_RUSTC", "metadata-rustc")]:
        monkeypatch.setattr(entry, name, str(tools / tool))
    monkeypatch.setattr(entry, "NATIVE_PATHS", {name: str(tools / name) for name in entry.NATIVE_PATHS})
    config = tmp_path / "sources.toml"
    monkeypatch.setattr(entry, "SOURCE_CONFIG", str(config))
    f = SimpleNamespace(source=source, build=tmp_path / "build", registry=tmp_path / "registry",
                        registry_manifest=tmp_path / "registry-manifest.json", config=config,
                        default=tmp_path / "default.lock", cargo_home=tmp_path / "cargo",
                        metadata_home=tmp_path / "cargo-metadata", tools=tools)
    create_registry(f)
    config.write_text(tomli_w.dumps({
        "source": {"crates-io": {"replace-with": "reviewbench-locked-local"},
                   "reviewbench-locked-local": {"local-registry": str(f.registry)}},
        "net": {"offline": True}}))
    f.default.write_bytes(lock_bytes(f))
    f.spec = {"schema_version": entry.SCHEMA, "task_id": entry.TASK, "profile": entry.PROFILE,
              "toolchain": "1.68.2", "metadata_toolchain": "1.93.0",
              "cargo_sha256": entry.file_hash(Path(entry.REAL_CARGO)),
              "rustc_sha256": entry.file_hash(Path(entry.REAL_RUSTC)),
              "metadata_cargo_sha256": entry.file_hash(Path(entry.METADATA_CARGO)),
              "metadata_rustc_sha256": entry.file_hash(Path(entry.METADATA_RUSTC)),
              "cpus": 4, "metadata_timeout_seconds": 120, "command_timeout_seconds": 1800,
              "max_log_bytes": 33554432, "receipt_sha256": "f" * 64,
              "default_lock": {"asset_target": entry.DEFAULT_LOCK, "sha256": entry.file_hash(f.default)},
              "registry_manifest_sha256": entry.file_hash(f.registry_manifest),
              "source_config_sha256": entry.file_hash(config),
              "native_tools": {name: {"path": path, "sha256": entry.file_hash(Path(path))}
                               for name, path in entry.NATIVE_PATHS.items()}}
    f.private = {"schema_version": entry.PRIVATE_SCHEMA, "task_id": entry.TASK,
                 "command": list(entry.COMMAND), "harness": dict(entry.HARNESS),
                 "expected_tests": list(entry.EXPECTED), "fixtures": dict(entry.FIXTURES)}
    f.calls = []
    return f


def mocked_graph(f, root, modern):
    lock = entry.common.parse_lock((root / "Cargo.lock").read_bytes())
    selected = [key for key in lock if modern or key[0] != "tool-modern"]
    ids = {key: ("modern:" if modern else "old:") + key[0] for key in selected}
    rows, nodes = [], []
    home = f.metadata_home if modern else f.cargo_home
    for key in selected:
        if key[2] is None:
            paths = [p for p in root.rglob("Cargo.toml")
                     if entry.tomllib.loads(p.read_text()).get("package", {}).get("name") == key[0]]
            assert len(paths) == 1
            path = paths[0]
            features = {"generate-test-files": [], "dont-generate-test-files": [], "nightly": [],
                        "tool-modern": ["dep:tool-modern"]}
            dependencies = [
                {"name": "blazesym", "rename": None, "kind": "dev", "target": None, "optional": False,
                 "uses_default_features": True, "features": ["generate-test-files"]},
                {"name": "tool-used", "rename": None, "kind": None, "target": None, "optional": False,
                 "uses_default_features": True, "features": []},
                {"name": "tool-modern", "rename": None, "kind": None, "target": None, "optional": True,
                 "uses_default_features": True, "features": []},
            ]
            targets = [{"name": "blazesym", "kind": ["lib"], "test": True, "src_path": str(path.parent / "src/lib.rs")},
                       {"name": "build-script-build", "kind": ["custom-build"], "test": False, "src_path": str(path.parent / "build.rs")},
                       {"name": "blazesym", "kind": ["test"], "test": True, "src_path": str(root / entry.HARNESS["path"])}]
            children = [k for k in selected]
            node_features = list(features) if modern else ["generate-test-files"]
        else:
            folder = home / "registry/src/-local-tool" / (key[0] + "-" + key[1])
            for name, body in f.contents[key].items():
                p = folder / name
                p.parent.mkdir(parents=True, exist_ok=True)
                if not p.exists():
                    p.write_bytes(body)
            (folder / ".cargo-ok").write_bytes(b'{"v":1}' if modern else b"ok")
            path = folder / "Cargo.toml"
            features, dependencies, node_features, children = {}, [], [], []
            targets = [{"name": key[0].replace("-", "_"), "kind": ["lib"], "test": True,
                        "src_path": str(folder / "src/lib.rs")}]
        rows.append({"id": ids[key], "name": key[0], "version": key[1], "source": key[2],
                     "manifest_path": str(path), "targets": targets, "features": features, "dependencies": dependencies})
        nodes.append({"id": ids[key], "features": node_features, "dependencies": [ids[k] for k in children],
                      "deps": [{"name": k[0].replace("-", "_"), "pkg": ids[k],
                                "dep_kinds": [{"kind": "dev" if k == key else None, "target": None}]} for k in children]})
    locals_ = [ids[k] for k in selected if k[2] is None]
    graph = {"version": 1, "workspace_root": str(root), "workspace_members": locals_,
             "packages": rows, "resolve": {"root": locals_[0], "nodes": nodes}}
    if modern:
        graph["workspace_default_members"] = locals_
    return graph


def run_fixture(f, *, public=False, mutate=None, code=0):
    def run(command, root, environment, logs, name, deadline, limit, **kwargs):
        f.calls.append(name)
        assert "--locked" in command and "--offline" in command
        if name.startswith("metadata"):
            modern = name == "metadata_full"
            assert ("--all-features" in command) is modern and "--no-deps" not in command
            assert command[0] == (entry.METADATA_CARGO if modern else entry.REAL_CARGO)
            r = record(stdout=json.dumps(mocked_graph(f, root, modern)))
        else:
            assert "--all-features" not in command and command[0] == entry.REAL_CARGO
            assert environment["RUSTC"] == entry.REAL_RUSTC
            for path in entry.GENERATED:
                p = root / path
                if not p.exists():
                    p.write_bytes(b"LOCAL TOOL generated output")
            r = record(code, "\n".join("test " + name + " ... " + ("ok" if code == 0 else "FAILED")
                                      for name in entry.EXPECTED) + "\ntest result: " +
                       ("ok." if code == 0 else "FAILED.") + "\n")
        if mutate:
            mutate(name, root, r)
        return r
    command = ["cargo", "--offline", "--config", entry.SOURCE_CONFIG, *(["check"] if public else entry.COMMAND)]
    return entry.execute(f.spec, f.source, f.build, command, None if public else f.private,
                         run=run, lock_source=f.default, registry=f.registry,
                         registry_manifest=f.registry_manifest, source_config=f.config,
                         cargo_home=f.cargo_home, metadata_home=f.metadata_home)


@pytest.mark.parametrize("public", [False, True])
def test_dual_graphs_original_commands_and_all_current_source_are_preserved(blaze, public):
    f = blaze
    before = entry.source_snapshot(f.source, derived=True)
    code, report = run_fixture(f, public=public)
    assert code == 0, report
    assert f.calls == ["metadata_full", "metadata_original", "cargo"]
    assert report["full_graph"]["packages"] == 3 and report["original_selected_graph"]["packages"] == 2
    assert report["registry"]["archives"] == 55
    assert "not-full-lock" in report["original_selected_graph"]["kind"]
    assert report["source_unchanged"] and entry.source_snapshot(f.source, derived=True) == before
    assert not (f.source / "Cargo.lock").exists()
    assert set(report["generated_outputs"]) == set(entry.GENERATED)
    assert (f.build / "current/source/data/dwarf-example").read_bytes() == (f.source / "data/dwarf-example").read_bytes()


def test_same_input_reuses_build_cache_but_never_reuses_a_test_outcome(blaze):
    f = blaze
    assert run_fixture(f)[0] == 0
    marker = f.build / "current/target/retained"
    marker.parent.mkdir(exist_ok=True); marker.write_text("current compilation only")
    code, report = run_fixture(f, code=101)
    assert code == 101 and report["cache"]["reuse"] is True
    assert marker.is_file() and len(f.calls) == 6
    assert report["test_results"] == {name: False for name in entry.EXPECTED}


@pytest.mark.parametrize("change", ["source", "missing_output", "modified_output", "mode_output", "native", "forged_state"])
def test_only_private_current_cache_is_invalidated_by_input_or_output_changes(blaze, change):
    f = blaze
    assert run_fixture(f)[0] == 0
    marker = f.build / "current/target/stale"
    marker.parent.mkdir(exist_ok=True); marker.write_text("old compilation")
    retained = f.cargo_home / "registry-retained"
    retained.write_text("dependency-only retained")
    if change == "source":
        (f.source / "src/second.rs").write_text("// complete new input\n")
    elif change in {"missing_output", "modified_output", "mode_output"}:
        p = f.build / "current/source/data/test.gsym"
        if change == "missing_output":
            p.unlink()
        elif change == "modified_output":
            p.write_bytes(b"changed generated bytes")
        else:
            p.chmod(0o755)
    elif change == "native":
        row = f.spec["native_tools"]["llvm-gsymutil-14"]
        p = Path(row["path"]); p.write_text(p.read_text() + "# new authenticated tool binding\n")
        row["sha256"] = entry.file_hash(p)
    else:
        p = f.build / "current/source/src/lib.rs"; p.write_text("// forged cached candidate\n")
        state = json.loads((f.build / "cache-state.json").read_text())
        state["mirror_inputs"] = entry.source_snapshot(f.build / "current/source")
        (f.build / "cache-state.json").write_text(json.dumps(state))
    code, report = run_fixture(f)
    assert code == 0 and report["cache"]["reuse"] is False, report
    assert not marker.exists() and retained.read_text() == "dependency-only retained"
    assert (f.build / "current/source/src/lib.rs").read_bytes() == (f.source / "src/lib.rs").read_bytes()


def test_candidate_authored_valid_lock_and_unrelated_manifest_edits_are_not_original_hash_selected(blaze, monkeypatch):
    f = blaze
    p = f.source / "Cargo.toml"
    p.write_text(p.read_text().replace('version="0.1.0"', 'version="0.1.9"', 1) +
                 '[package.metadata.current]\nnote="harmless current manifest"\n')
    own = lock_bytes(f, version="0.1.9") + b"# own serialization\n"
    (f.source / "Cargo.lock").write_bytes(own)
    monkeypatch.setattr(entry, "select_package", lambda *a: pytest.fail("own locks bypass selection"))
    code, report = run_fixture(f)
    assert code == 0, report
    assert (f.build / "current/source/Cargo.lock").read_bytes() == own
    assert (f.source / "Cargo.lock").read_bytes() == own


@pytest.mark.parametrize("field,value", [
    ("task_id", "another"), ("profile", "another"), ("toolchain", "1.93.0"),
    ("metadata_toolchain", "1.68.2"), ("cpus", 2), ("command_timeout_seconds", 1801),
    ("metadata_timeout_seconds", 121), ("max_log_bytes", 33554433), ("unexpected", True),
    ("source_config_sha256", "0" * 64), ("registry_manifest_sha256", "0" * 64),
    ("cargo_sha256", "0" * 64), ("metadata_rustc_sha256", "0" * 64),
])
def test_spec_tool_configuration_and_receipt_bindings_fail_before_any_command(blaze, field, value):
    blaze.spec[field] = value
    code, _ = run_fixture(blaze)
    assert code == 2 and blaze.calls == []


@pytest.mark.parametrize("path", ["tests/blazesym.rs", "data/test.c", "data/test-stable-addresses.c",
                               "data/test-stable-addresses.ld"])
def test_original_harness_and_three_native_fixture_bytes_are_fixed(blaze, path):
    (blaze.source / path).write_bytes(b"changed original input")
    assert run_fixture(blaze)[0] == 2 and blaze.calls == []


@pytest.mark.parametrize("problem", ["missing", "unused_archive", "index", "extra", "mode", "symlink", "schema"])
def test_every_provided_registry_file_is_verified_even_when_not_selected(blaze, problem):
    f = blaze
    p = f.registry / "tool-pad-52-0.1.0.crate"
    if problem == "missing":
        p.unlink()
    elif problem == "unused_archive":
        p.write_bytes(b"changed archive")
    elif problem == "index":
        (f.registry / "index" / entry.index_path("tool-pad-52")).write_bytes(b"{}\n")
    elif problem == "extra":
        (f.registry / "unbound").write_text("extra")
    elif problem == "mode":
        p.chmod(0o755)
    elif problem == "symlink":
        p.unlink(); p.symlink_to(f.default)
    else:
        document = json.loads(f.registry_manifest.read_text())
        document["host_path"] = "/not-public"
        f.registry_manifest.write_text(json.dumps(document))
        f.spec["registry_manifest_sha256"] = entry.file_hash(f.registry_manifest)
    code, _ = run_fixture(f)
    assert code == 2 and f.calls == []


@pytest.mark.parametrize("duplicate,valid", [("identical", True), ("conflict", False)])
def test_bound_identical_tar_duplicates_are_not_confused_with_conflicting_payloads(blaze, duplicate, valid):
    f = blaze
    shutil.rmtree(f.registry)
    create_registry(f, duplicate)
    f.spec["registry_manifest_sha256"] = entry.file_hash(f.registry_manifest)
    code, report = run_fixture(f)
    assert code == (0 if valid else 2), report
    if not valid:
        assert f.calls == []


@pytest.mark.parametrize("phase", ["metadata_full", "metadata_original"])
@pytest.mark.parametrize("problem", ["node", "edge", "feature", "prefix", "suffix", "malformed", "truncated",
                                  "signal", "nonzero", "limit", "source_mutation", "unpacked_source"])
def test_complete_and_selected_metadata_failures_never_reach_original_test(blaze, phase, problem):
    def mutate(name, root, r):
        if name != phase:
            return
        graph = json.loads(r["stdout"])
        if problem == "node":
            graph["resolve"]["nodes"].pop()
        elif problem == "edge":
            node = graph["resolve"]["nodes"][0]
            node["dependencies"] = []; node["deps"] = []
        elif problem == "feature":
            graph["resolve"]["nodes"][0]["features"] = ["unbound-feature"]
        elif problem == "truncated":
            r["stdout_total_bytes"] += 1
        elif problem == "signal":
            r["stderr"] += "signal: 11, SIGSEGV\n"
        elif problem == "nonzero":
            r["returncode"] = 101
        elif problem == "limit":
            r["limit"] = "output_limit"
        elif problem == "source_mutation":
            (root / "src/lib.rs").write_text("// command mutated current code")
        elif problem == "unpacked_source":
            p = next(p for p in graph["packages"] if p["source"])
            (Path(p["manifest_path"]).parent / "src/lib.rs").write_text("// unbound cached source")
        r["stdout"] = json.dumps(graph)
        if problem == "prefix":
            r["stdout"] = "extra\n" + r["stdout"]
        elif problem == "suffix":
            r["stdout"] += "{}"
        elif problem == "malformed":
            r["stdout"] = "[]"
    code, _ = run_fixture(blaze, mutate=mutate)
    assert code == 2 and "cargo" not in blaze.calls


@pytest.mark.parametrize("problem", ["missing", "redirect", "disabled", "wrong_default", "nightly", "dont_generate", "no_generate"])
def test_actual_default_selection_cannot_skip_or_change_original_test(blaze, problem):
    def mutate(name, root, r):
        if name not in {"metadata_full", "metadata_original"}:
            return
        graph = json.loads(r["stdout"]); p = graph["packages"][0]
        if problem == "wrong_default" and name == "metadata_full":
            graph["workspace_default_members"] = []
        elif name == "metadata_original":
            if problem == "missing":
                p["targets"] = p["targets"][:2]
            elif problem == "redirect":
                p["targets"][2]["src_path"] = str(root / "src/lib.rs")
            elif problem == "disabled":
                p["targets"][2]["test"] = False
            elif problem in {"nightly", "dont_generate", "no_generate"}:
                graph["resolve"]["nodes"][0]["features"] = {
                    "nightly": ["generate-test-files", "nightly"],
                    "dont_generate": ["generate-test-files", "dont-generate-test-files"],
                    "no_generate": [],
                }[problem]
        r["stdout"] = json.dumps(graph)
    code, _ = run_fixture(blaze, mutate=mutate)
    assert code == 2 and "cargo" not in blaze.calls


@pytest.mark.parametrize("problem", ["unknown_package", "wrong_registry", "extra_error", "duplicate"])
def test_unpacking_lines_are_strictly_bound_and_never_hide_errors(blaze, problem):
    line = "   Unpacking tool-used v0.1.0 (registry `" + str(blaze.registry) + "`)\n"
    def mutate(name, root, r):
        if name == "metadata_full":
            r["stderr"] = {
                "unknown_package": line.replace("tool-used", "unknown"),
                "wrong_registry": line.replace(str(blaze.registry), "/other"),
                "extra_error": line + "error: failed to run custom build command\n",
                "duplicate": line + line,
            }[problem]
    code, report = run_fixture(blaze, mutate=mutate)
    assert code == 2 and blaze.calls == ["metadata_full"]
    assert report["metadata_full"]["stderr"]


def test_known_unpacking_and_json_signal_words_are_retained_not_treated_as_failures(blaze, capsys):
    line = "   Unpacking tool-used v0.1.0 (registry `" + str(blaze.registry) + "`)\n"
    def mutate(name, root, r):
        if name.startswith("metadata"):
            r["stderr"] = line
            graph = json.loads(r["stdout"]); graph["packages"][0]["description"] = "SIGINT / signal: 11"
            r["stdout"] = json.dumps(graph)
    code, report = run_fixture(blaze, mutate=mutate)
    assert code == 0, report
    entry.publish(report, blaze.private)
    data = json.loads(capsys.readouterr().out.split(entry.EVIDENCE_PREFIX, 1)[1])
    assert data["metadata_full"]["stderr"] == data["metadata_original"]["stderr"] == line
    assert "SIGINT" in data["metadata_full"]["stdout"]
    entry.publish(report)
    output = capsys.readouterr().out
    assert len(output) < 500 and "metadata_full" not in output and "test_results" not in output


@pytest.mark.parametrize("code", [101, 124, 137, -2, -6, -9, -11, -15])
def test_original_process_codes_and_signals_are_preserved_without_resolver_retry(blaze, code):
    result, report = run_fixture(blaze, code=code)
    assert result == (code if code >= 0 else 128 - code)
    assert report["execution"]["returncode"] == code and blaze.calls.count("cargo") == 1
    assert report["runtime_resolution_commands"] == 0


@pytest.mark.parametrize("problem", ["stdout_signal", "stderr_signal", "missing_test", "extra_source",
                                  "original_data", "symlink_output", "lock", "native_fixture"])
def test_success_status_cannot_hide_source_or_oracle_integrity_failures(blaze, problem):
    def mutate(name, root, r):
        if name != "cargo":
            return
        if problem == "stdout_signal":
            r["stdout"] += "SIGABRT\n"
        elif problem == "stderr_signal":
            r["stderr"] += "signal: 11, SIGSEGV\n"
        elif problem == "missing_test":
            r["stdout"] = "test symbolize_dwarf ... ok\ntest result: ok.\n"
        elif problem == "extra_source":
            (root / "data/unapproved-sixth.bin").write_bytes(b"not an allowed generated path")
        elif problem == "original_data":
            (root / "data/dwarf-example").write_bytes(b"must not be discarded")
        elif problem == "symlink_output":
            p = root / "data/test.gsym"; p.unlink(); p.symlink_to(root / "data/dwarf-example")
        elif problem == "lock":
            (root / "Cargo.lock").write_bytes(b"changed lock")
        else:
            (root / "data/test.c").write_bytes(b"changed original native input")
    code, _ = run_fixture(blaze, mutate=mutate)
    assert code == 2


@pytest.mark.parametrize("tail", [["check", "--all-features"], ["test", "--all-features"],
                                ["test", "--test", "blazesym", "symbolize_dwarf"],
                                ["generate-lockfile"], ["update"], ["fetch"]])
def test_fixed_original_command_never_gains_features_filters_or_resolution(tail):
    with pytest.raises(entry.OverlayError):
        entry.transport(["cargo", "--offline", "--config", entry.SOURCE_CONFIG, *tail])


def test_native_environment_change_is_part_of_the_complete_current_cache_identity(blaze, monkeypatch):
    f = blaze
    first, _ = run_fixture(f)
    assert first == 0
    marker = f.build / "current/target/stale-native-environment"
    marker.parent.mkdir(exist_ok=True); marker.write_text("compiled with old native environment")
    monkeypatch.setenv("CPATH", "/LOCAL-TOOL-changed-native-include-path")
    code, report = run_fixture(f)
    assert code == 0 and not report["cache"]["reuse"], report
    assert not marker.exists()


def test_metadata_is_an_observer_even_for_the_five_declared_generated_paths(blaze):
    def mutate(name, root, record):
        if name == "metadata_full":
            (root / "data/test.gsym").write_bytes(b"unexpected metadata-phase output")
    code, report = run_fixture(blaze, mutate=mutate)
    assert code == 2 and blaze.calls == ["metadata_full"], report
    assert "metadata observer" in report["error"]
    assert not (blaze.build / "cache-state.json").exists()


def retain_filesystem_control(name, *, code, calls, before, after, extra=None):
    folder = os.environ.get("BLAZESYM_FIXTURE_EVIDENCE")
    if folder:
        destination = Path(folder)
        destination.mkdir(parents=True, exist_ok=True)
        (destination / ("filesystem-" + name + ".json")).write_text(json.dumps({
            "record_label": "BLAZESYM_REAL_FILESYSTEM_BOUNDARY_TOOL_NOT_ORIGINAL_SUT",
            "exit_status": code, "child_calls_during_guarded_operation": calls,
            "victim_before": before, "victim_after": after, "victim_unchanged": before == after,
            "extra": extra or {}, "original_Cargo_SUT_API_calls": 0,
        }, sort_keys=True, indent=2) + "\n")


@pytest.mark.parametrize("name", ["cache-state.tmp", "cache-state.json", "build.lock"])
@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory", "hardlink"])
def test_peer_p1_bookkeeping_paths_reject_before_children_and_leave_real_victim_unchanged(blaze, name, kind):
    f = blaze
    f.build.mkdir()
    victim = f.source / "src/lib.rs"
    before = entry.file_hash(victim)
    path = f.build / name
    if kind == "symlink":
        path.symlink_to(victim)
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    else:
        os.link(victim, path)
    code, report = run_fixture(f)
    after = entry.file_hash(victim)
    retain_filesystem_control("bookkeeping-" + name + "-" + kind,
                              code=code, calls=f.calls, before=before, after=after)
    assert code == 2 and f.calls == [], report
    assert after == before and not f.cargo_home.exists() and not f.metadata_home.exists()


def test_peer_p1_atomic_writer_never_uses_the_legacy_temporary_symlink(tmp_path):
    root = tmp_path / "owned"
    root.mkdir()
    victim = tmp_path / "victim"
    victim.write_bytes(b"unchanged victim\n")
    (root / "cache-state.tmp").symlink_to(victim)
    before = entry.file_hash(victim)
    result = entry.atomic_bookkeeping(root, "cache-state.json", {"local_tool": True})
    after = entry.file_hash(victim)
    stamp = root / "cache-state.json"
    assert stamp.is_file() and not stamp.is_symlink() and json.loads(stamp.read_text()) == {"local_tool": True}
    assert before == after and (root / "cache-state.tmp").is_symlink()
    retain_filesystem_control("exclusive-success", code=0, calls=[], before=before, after=after, extra=result)


@pytest.mark.parametrize("late", [False, True])
def test_peer_p1_exclusive_nofollow_creation_rejects_preexisting_and_raced_symlinks(tmp_path, monkeypatch, late):
    root = tmp_path / "owned"
    root.mkdir()
    victim = tmp_path / "victim"
    victim.write_bytes(b"victim bytes must remain\n")
    nonce = "a" * 32
    name = ".cache-state.json-" + nonce + ".tmp"
    monkeypatch.setattr(entry.uuid, "uuid4", lambda: SimpleNamespace(hex=nonce))
    original_open = os.open
    observed_flags = []
    if late:
        def planting_open(path, flags, *args, **kwargs):
            if str(path) == name:
                observed_flags.append(flags)
                (root / name).symlink_to(victim)
            return original_open(path, flags, *args, **kwargs)
        monkeypatch.setattr(entry.os, "open", planting_open)
    else:
        (root / name).symlink_to(victim)
    before = entry.file_hash(victim)
    with pytest.raises(FileExistsError):
        entry.atomic_bookkeeping(root, "cache-state.json", {"never_written": True})
    after = entry.file_hash(victim)
    assert before == after and not (root / "cache-state.json").exists()
    assert (root / name).is_symlink()  # Cleanup must not delete a collision it does not own.
    if late:
        assert observed_flags and all(flags & os.O_EXCL and flags & os.O_NOFOLLOW for flags in observed_flags)
    retain_filesystem_control("exclusive-collision-" + str(late), code=2, calls=[], before=before, after=after,
                              extra={"O_EXCL_and_O_NOFOLLOW": bool(observed_flags) if late else True})


@pytest.mark.parametrize("destination", ["cache-state.json", "record.json"])
def test_peer_p1_atomic_destination_link_is_rejected_without_a_victim_write(tmp_path, destination):
    root = tmp_path / "owned"
    root.mkdir()
    victim = tmp_path / "victim"
    victim.write_bytes(b"protected source or sibling cache\n")
    (root / destination).symlink_to(victim)
    before = entry.file_hash(victim)
    with pytest.raises(entry.OverlayError):
        entry.atomic_bookkeeping(root, destination, {"no_follow": True})
    after = entry.file_hash(victim)
    assert before == after and (root / destination).is_symlink()
    retain_filesystem_control("atomic-destination-" + destination, code=2, calls=[], before=before, after=after)


@pytest.mark.parametrize("component", ["target", "metadata-target", "current", "build-parent"])
def test_peer_p1_cache_hit_target_and_parent_symlinks_never_reach_children(blaze, component):
    f = blaze
    assert run_fixture(f)[0] == 0
    f.calls.clear()
    external = f.source.parent / "external-victim"
    external.mkdir()
    (external / "sentinel").write_bytes(b"external directory must not change\n")
    if component in {"target", "metadata-target"}:
        path = f.build / "current" / component
        shutil.rmtree(path)
        path.symlink_to(external, target_is_directory=True)
    elif component == "current":
        path = f.build / "current"
        moved = external / "retained-current"
        path.rename(moved)
        path.symlink_to(moved, target_is_directory=True)
    else:
        alias = f.source.parent / "linked-build-parent"
        alias.symlink_to(f.build.parent, target_is_directory=True)
        f.build = alias / f.build.name
    before = {str(p.relative_to(external)): entry.file_hash(p) for p in external.rglob("*") if p.is_file()}
    code, report = run_fixture(f)
    after = {str(p.relative_to(external)): entry.file_hash(p) for p in external.rglob("*") if p.is_file()}
    assert code == 2 and f.calls == [] and before == after, report
    retain_filesystem_control("target-" + component, code=code, calls=f.calls, before=before, after=after)


@pytest.mark.parametrize("home_name", ["cargo_home", "metadata_home"])
@pytest.mark.parametrize("location", ["inside", "equal", "ancestor"])
def test_peer_p1_dependency_home_overlap_fails_before_any_mkdir_rmtree_or_child(blaze, monkeypatch, home_name, location):
    f = blaze
    current = f.build / "current"
    path = {"inside": current / "dependency-home", "equal": current, "ancestor": f.build}[location]
    path.mkdir(parents=True)
    victim = path / "dependency-sentinel"
    victim.write_bytes(b"original dependency-only cache\n")
    setattr(f, home_name, path)
    before = entry.file_hash(victim)
    with monkeypatch.context() as guard:
        guard.setattr(Path, "mkdir", lambda *a, **k: pytest.fail("overlap reached mkdir"))
        guard.setattr(entry.shutil, "rmtree", lambda *a, **k: pytest.fail("overlap reached rmtree"))
        code, report = run_fixture(f)
    after = entry.file_hash(victim)
    assert code == 2 and f.calls == [] and before == after, report
    assert "overlap" in report["error"]
    retain_filesystem_control("home-" + home_name + "-" + location,
                              code=code, calls=f.calls, before=before, after=after,
                              extra={"mkdir_calls": 0, "rmtree_calls": 0})


def test_peer_p1_log_directory_collision_cannot_write_through_a_preexisting_symlink(blaze, monkeypatch):
    f = blaze
    f.build.mkdir()
    external = f.source.parent / "external-log-victim"
    external.mkdir()
    victim = external / "record.json"
    victim.write_bytes(b"original sibling record\n")
    monkeypatch.setattr(entry.time, "time_ns", lambda: 123456789)
    (f.build / "blazesym-123456789").symlink_to(external, target_is_directory=True)
    before = entry.file_hash(victim)
    code, report = run_fixture(f)
    after = entry.file_hash(victim)
    assert code == 2 and f.calls == [] and before == after, report
    retain_filesystem_control("log-collision", code=code, calls=f.calls, before=before, after=after)

HARNESS_BYTES = b'use std::path::Path;\n\nuse blazesym::BlazeSymbolizer;\nuse blazesym::SymbolSrcCfg;\nuse blazesym::SymbolizerFeature;\n\n\n/// Check that we can correctly symbolize an address using GSYM.\n#[test]\nfn symbolize_gsym() {\n    let test_gsym = Path::new(&env!("CARGO_MANIFEST_DIR"))\n        .join("data")\n        .join("test.gsym");\n\n    let features = vec![SymbolizerFeature::LineNumberInfo(true)];\n    let srcs = vec![SymbolSrcCfg::Gsym {\n        file_name: test_gsym,\n        base_address: 0,\n    }];\n    let symbolizer = BlazeSymbolizer::new_opt(&features).unwrap();\n\n    let results = symbolizer\n        .symbolize(&srcs, &[0x2000100])\n        .into_iter()\n        .flatten()\n        .collect::<Vec<_>>();\n    assert_eq!(results.len(), 1);\n\n    let result = results.first().unwrap();\n    assert_eq!(result.symbol, "factorial");\n}\n\n/// Check that we can symbolize an address using DWARF.\n#[test]\nfn symbolize_dwarf() {\n    let test_dwarf = Path::new(&env!("CARGO_MANIFEST_DIR"))\n        .join("data")\n        .join("test-dwarf.bin");\n    let features = [\n        SymbolizerFeature::LineNumberInfo(true),\n        SymbolizerFeature::DebugInfoSymbols(true),\n    ];\n    let srcs = [SymbolSrcCfg::Elf {\n        file_name: test_dwarf,\n        base_address: 0,\n    }];\n    let symbolizer = BlazeSymbolizer::new_opt(&features).unwrap();\n    let results = symbolizer\n        .symbolize(&srcs, &[0x2000100])\n        .into_iter()\n        .flatten()\n        .collect::<Vec<_>>();\n    assert_eq!(results.len(), 1);\n\n    let result = results.first().unwrap();\n    assert_eq!(result.symbol, "factorial");\n}\n'
FIXTURE_BYTES = {'data/test.c': b'/*\n * The sample program is used to generate test.bin.\n */\n\n#include <stdio.h>\n\nstatic\nunsigned int fibonacci(unsigned int n) {\n  if (n <= 1)\n    return n;\n  return fibonacci(n - 1) + fibonacci(n - 2);\n}\n\nint\nmain() {\n  int i;\n  printf("calculate fibonacci(n); n = ");\n  scanf("%d", &i);\n\n  printf("fibonacci(%d) = %d\\n", i, fibonacci(i));\n  return 0;\n}\n', 'data/test-stable-addresses.c': b'/* The sample program is used to generate test.gsym.\n *\n * Chosen functions are placed in dedicated sections to allow for control placement.\n */\n\n__attribute__((section(".text.factorial"))) unsigned int\nfactorial(unsigned int n) {\n\tif (n == 0)\n\t\treturn 1;\n\treturn factorial(n - 1) * n;\n}\n\nstatic inline void\nfactorial_inline_wrapper() {\n\tfactorial(5);\n}\n\n__attribute__((section(".text.main"))) int\nmain(int argc, const char *argv[]) {\n\tfactorial_inline_wrapper();\n\treturn 0;\n}\n', 'data/test-stable-addresses.ld': b'SECTIONS {\n  .text (0x2000000) : {\n    *(.text.main)\n    *(.text)\n    . = ABSOLUTE(0x2000100);\n    *(.text.factorial)\n  }\n  .data : {\n    *(.data)\n  }\n  .bss : {\n    *(.bss)\n  }\n\n  /* DWARF debug sections.\n     Symbols in the DWARF debugging sections are relative to the beginning\n     of the section so we begin them at 0.\n   */\n  /* DWARF 1.  */\n  .debug          0 : { *(.debug) }\n  .line           0 : { *(.line) }\n  /* GNU DWARF 1 extensions.  */\n  .debug_srcinfo  0 : { *(.debug_srcinfo) }\n  .debug_sfnames  0 : { *(.debug_sfnames) }\n  /* DWARF 1.1 and DWARF 2.  */\n  .debug_aranges  0 : { *(.debug_aranges) }\n  .debug_pubnames 0 : { *(.debug_pubnames) }\n  /* DWARF 2.  */\n  .debug_info     0 : { *(.debug_info .gnu.linkonce.wi.*) }\n  .debug_abbrev   0 : { *(.debug_abbrev) }\n  .debug_line     0 : { *(.debug_line .debug_line.* .debug_line_end) }\n  .debug_frame    0 : { *(.debug_frame) }\n  .debug_str      0 : { *(.debug_str) }\n  .debug_loc      0 : { *(.debug_loc) }\n  .debug_macinfo  0 : { *(.debug_macinfo) }\n\n  /DISCARD/ : {\n    *(.*)\n  }\n}\n'}
