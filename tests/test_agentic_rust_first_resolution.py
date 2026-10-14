"""Host admission of explicit first-new locks, not historical lock recovery."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_first_resolution as first
from reviewbench_maintainability.agentic.contracts import ContractError


def reference(path):
    data = path.read_bytes()
    return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


@pytest.mark.parametrize("config", [{}, {"migration": {}}, {"migration": {first.OPTION: {}}}])
def test_not_requested_by_default(config):
    assert not first.requested(SimpleNamespace(config=config), {"id": first.TASK})


def test_other_tasks_are_unchanged():
    ctx = SimpleNamespace(config={"migration": {first.OPTION: {first.TASK: {}}}})
    assert not first.requested(ctx, {"id": "another-original-task"})
    assert not first.requested(SimpleNamespace(), {"id": first.TASK})


@pytest.mark.parametrize("value", [[], "enabled", True, 1])
def test_malformed_optin_is_not_a_default(value):
    with pytest.raises(ContractError):
        first.requested(SimpleNamespace(config={"migration": {first.OPTION: value}}),
                        {"id": first.TASK})


def test_unknown_profile_is_not_implicitly_admitted():
    with pytest.raises(ContractError):
        first.requested(SimpleNamespace(config={"migration": {first.OPTION: {"other": {}}}}),
                        {"id": "other"})


def test_reference_hash_size_and_content(tmp_path):
    path = tmp_path / "value.json"
    path.write_text('{"answer": 7}\n')
    row = reference(path)
    assert first.referenced(row, json_data=True) == {"answer": 7}
    assert first.referenced(row) == path.read_bytes()
    for mutated in ({**row, "sha256": "0" * 64}, {**row, "bytes": 1}, {**row, "extra": True}):
        with pytest.raises(ContractError):
            first.referenced(mutated)
    with pytest.raises(ContractError):
        first.referenced(row, limit=1)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(ContractError):
        first.referenced({**row, "path": str(link)})


def lock_bytes(extra=""):
    return (
        'version = 4\n[[package]]\nname = "sample"\nversion = "0.1.0"\n'
        '[[package]]\nname = "sample-derive"\nversion = "0.1.0"\n' + extra
    ).encode()


def test_default_lock_parser_is_not_a_current_lock_hash_gate():
    # This helper validates general lock structure; only host authenticity checks
    # impose the two first-success hashes. Runtime authors may supply their lock.
    assert len(first.reference_packages(lock_bytes())["package"]) == 2


@pytest.mark.parametrize("extra", [
    '[[package]]\nname="sample"\nversion="0.1.0"\n',
    '[[package]]\nname="external"\nversion="1.0"\nsource="git+https://host/repo#123"\n',
    '[[package]]\nname="external"\nversion="1.0"\nsource="' + first.CRATES_IO + '"\n',
    '[[package]]\nname="external"\nversion="1.0"\nsource="' + first.CRATES_IO + '"\nchecksum="bad"\n',
])
def test_default_lock_rejects_unbound_packages(extra):
    with pytest.raises(ContractError):
        first.reference_packages(lock_bytes(extra))


def graph_fixture(tmp_path):
    source = tmp_path / "source"
    manifests = {}
    packages, nodes = [], []
    for index, (name, relative) in enumerate((("sample", "."), ("sample-derive", "derive"))):
        folder = source / relative
        (folder / "src").mkdir(parents=True)
        manifest = folder / "Cargo.toml"
        manifest.write_text(f'[package]\nname="{name}"\nversion="0.1.0"\n')
        manifests[manifest.relative_to(source).as_posix()] = reference(manifest)["sha256"]
        (folder / "src/lib.rs").write_text("pub fn value() -> u32 { 7 }\n")
        identity = f"local#{index}"
        packages.append({
            "id": identity, "name": name, "version": "0.1.0", "source": None,
            "manifest_path": str(manifest),
            "targets": [{"name": name, "kind": ["lib"], "src_path": str(folder / "src/lib.rs")}],
        })
        dependency = f"local#{1 - index}"
        nodes.append({
            "id": identity, "features": [], "dependencies": [dependency],
            "deps": [{"name": "dep", "pkg": dependency, "dep_kinds": [{"kind": None, "target": None}]}],
        })
    graph = {
        "version": 1, "workspace_root": str(source), "workspace_members": ["local#0", "local#1"],
        "packages": packages, "resolve": {"root": "local#0", "nodes": nodes},
    }
    lock = first.reference_packages(lock_bytes())
    return source, manifests, graph, lock


def test_complete_graph_and_path_independent_digest(tmp_path):
    source, manifests, graph, lock = graph_fixture(tmp_path / "left")
    summary = first.graph_summary(graph, lock, source, manifests)
    other, other_manifests, other_graph, other_lock = graph_fixture(tmp_path / "right")
    assert first.graph_summary(other_graph, other_lock, other, other_manifests) == summary
    assert summary["packages"] == summary["resolve_nodes"] == 2


@pytest.mark.parametrize("mutate", [
    lambda g: g.update(version=True),
    lambda g: g.update(workspace_root="/outside"),
    lambda g: g.update(workspace_members=["local#0"]),
    lambda g: g["resolve"].update(root="local#1"),
    lambda g: g["resolve"]["nodes"].pop(),
    lambda g: g["packages"].append(deepcopy(g["packages"][0])),
    lambda g: g["resolve"]["nodes"][0].update(id="local#1"),
    lambda g: g["resolve"]["nodes"][0].update(dependencies=["foreign"]),
    lambda g: g["resolve"]["nodes"][0].update(dependencies=["local#1", "local#1"]),
    lambda g: g["resolve"]["nodes"][0].update(deps=[]),
    lambda g: g["resolve"]["nodes"][0].update(features=["x", "x"]),
    lambda g: g["resolve"]["nodes"][0].update(features=[1]),
    lambda g: g["resolve"]["nodes"][0]["deps"][0].update(dep_kinds=[{"kind": "other", "target": None}]),
    lambda g: g["packages"][0].update(manifest_path="/outside/Cargo.toml"),
    lambda g: g["packages"][0].update(targets=[]),
    lambda g: g["packages"][0]["targets"][0].update(src_path="/outside/lib.rs"),
    lambda g: g["packages"][0]["targets"][0].update(src_path=g["workspace_root"] + "/missing.rs"),
])
def test_incomplete_default_metadata_is_rejected(tmp_path, mutate):
    source, manifests, graph, lock = graph_fixture(tmp_path)
    mutate(graph)
    with pytest.raises(ContractError):
        first.graph_summary(graph, lock, source, manifests)


def test_unselected_original_bench_is_not_an_added_all_targets_requirement(tmp_path):
    source, manifests, graph, lock = graph_fixture(tmp_path)
    bench = {"kind": ["bench"], "name": "proto", "test": False,
             "required-features": ["protobuf"],
             "src_path": str(source / "benches/encoding/proto.rs")}
    graph["packages"][0]["targets"].append(bench)
    assert first.graph_summary(graph, lock, source, manifests)["packages"] == 2
    bench["src_path"] = str(source / "../escaped-bench.rs")
    with pytest.raises(ContractError):
        first.graph_summary(graph, lock, source, manifests)
    bench.update(src_path=str(source / "missing.rs"), kind=["test"], test=True)
    with pytest.raises(ContractError):
        first.graph_summary(graph, lock, source, manifests)


def test_metadata_package_strings_are_data_not_signal_diagnostics(tmp_path):
    source, manifests, graph, lock = graph_fixture(tmp_path)
    graph["packages"][0]["description"] = "SIGINT Ctrl-C signal package metadata"
    assert first.graph_summary(graph, lock, source, manifests)["packages"] == 2


@pytest.mark.parametrize("mutation", [
    lambda x: x.update(schema_version="another-schema"),
    lambda x: x.update(task_id="another-task"),
    lambda x: x.update(extra=True),
])
def test_envelope_identity_fails_before_any_source_or_dependency_lookup(tmp_path, mutation):
    path = tmp_path / "envelope.json"
    data = {"schema_version": first.SCHEMA, "task_id": first.TASK, "root": str(tmp_path), "files": {}}
    mutation(data)
    path.write_text(json.dumps(data))
    with pytest.raises(ContractError):
        first.prepared_inputs(reference(path))


def test_original_profile_rejects_another_task_without_touching_files():
    with pytest.raises(ContractError):
        first.original_inputs(SimpleNamespace(), {"id": "other"}, {}, {}, {})


def test_actual_first_locks_are_separate_and_full_when_explicitly_supplied():
    configured = os.environ.get("PROMETHEUS_FIRST_RESOLUTION_INPUTS")
    if not configured:
        pytest.skip("requires the explicitly supplied retained first-new environment bundle")
    result = first.prepared_inputs(reference(Path(configured)))
    assert result["locks"]["start"]["sha256"] != result["locks"]["golden"]["sha256"]
    assert result["graph_audits"]["start"]["packages"] == 369
    assert result["graph_audits"]["golden"]["packages"] == 406
    assert len(result["archives"]) == 413
