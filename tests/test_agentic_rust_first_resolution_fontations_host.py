"""Host-side virtual-workspace and full pinned-Git receipt controls."""
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


def test_profiles_are_separate_ordered_and_preserve_the_original_prometheus_constants():
    before = deepcopy(first.DEFAULTS)
    font = first.profile_for(first.FONT_TASK)
    assert list(font["defaults"]) == ["start", "golden"]
    assert font["virtual_workspace"] and font["local_packages"] == 6
    assert font["command"] == ["test", "-p", "punchcut", "--test", "oracle_session", "--", "--nocapture"]
    assert [row["selector_version"] for row in font["defaults"].values()] == ["0.0.4", "0.0.5"]
    assert first.profile_for(first.TASK)["defaults"] == first.DEFAULTS == before
    assert first.TASK != first.FONT_TASK
    assert first.PROFILE == "prometheus82-first-new-locks-v1"


def git_lock(source, checksum=""):
    return (
        'version=4\n[[package]]\nname="local"\nversion="1.0.0"\n'
        '[[package]]\nname="git-package"\nversion="1.0.0"\nsource="' + source + '"\n' + checksum
    ).encode()


def test_full_git_reference_requires_an_explicit_git_profile():
    data = git_lock("git+https://example.invalid/repo#" + "a" * 40)
    with pytest.raises(ContractError):
        first.reference_packages(data)
    assert len(first.reference_packages(data, allow_git=True)["package"]) == 2


@pytest.mark.parametrize("source,checksum", [
    ("git+https://example.invalid/repo", ""),
    ("git+https://example.invalid/repo#abcdef0", ""),
    ("git+https://example.invalid/repo#" + "g" * 40, ""),
    ("git+https://example.invalid/repo#" + "a" * 40, 'checksum="not-a-registry-package"\n'),
])
def test_unpinned_or_confused_git_identity_is_rejected(source, checksum):
    with pytest.raises(ContractError):
        first.reference_packages(git_lock(source, checksum), allow_git=True)


def test_resolver_warning_is_preserved_and_narrow_not_a_general_stderr_bypass():
    data = first.VIRTUAL_WARNING.encode()
    assert first.metadata_stderr_allowed(data, virtual=True)
    assert not first.metadata_stderr_allowed(data, virtual=False)
    assert first.metadata_stderr_allowed(b"")
    for extra in (b"error: lock failed\n", b"SIGINT\n", b"warning: something else\n"):
        assert not first.metadata_stderr_allowed(data + extra, virtual=True)
    assert not first.metadata_stderr_allowed(data.replace(b'resolver = "1"', b'resolver = "2"'), virtual=True)


def virtual_graph(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "Cargo.toml").write_text('[workspace]\nmembers=["m0","m1","m2","m3","m4","m5"]\n')
    manifests = {"Cargo.toml": reference(root / "Cargo.toml")["sha256"]}
    packages, nodes, locked = [], [], []
    for index in range(6):
        member = root / f"m{index}"
        (member / "src").mkdir(parents=True)
        (member / "Cargo.toml").write_text(f'[package]\nname="m{index}"\nversion="0.1.0"\n')
        (member / "src/lib.rs").write_text("pub fn value() -> u8 { 1 }\n")
        manifests[f"m{index}/Cargo.toml"] = reference(member / "Cargo.toml")["sha256"]
        packages.append({
            "id": f"local#{index}", "name": f"m{index}", "version": "0.1.0", "source": None,
            "manifest_path": str(member / "Cargo.toml"),
            "targets": [{"kind": ["lib"], "src_path": str(member / "src/lib.rs")}],
        })
        nodes.append({"id": f"local#{index}", "dependencies": [], "deps": [], "features": []})
        locked.append({"name": f"m{index}", "version": "0.1.0"})
    graph = {"version": 1, "workspace_root": str(root),
             "workspace_members": [p["id"] for p in packages], "packages": packages,
             "resolve": {"root": None, "nodes": nodes}}
    return root, manifests, graph, {"version": 4, "package": locked}


def test_complete_virtual_workspace_is_not_reduced_to_one_library(tmp_path):
    root, manifests, graph, lock = virtual_graph(tmp_path)
    assert first.graph_summary(graph, lock, root, manifests, local_packages=6, virtual=True)["packages"] == 6
    with pytest.raises(ContractError):
        first.graph_summary(graph, lock, root, manifests)


@pytest.mark.parametrize("change", [
    lambda g: g["resolve"].update(root="local#0"),
    lambda g: g["workspace_members"].pop(),
    lambda g: g["resolve"]["nodes"].pop(),
    lambda g: g["packages"][0]["targets"][0].update(src_path="/outside/lib.rs"),
    lambda g: g["resolve"]["nodes"][0].update(dependencies=["unknown"]),
])
def test_virtual_workspace_does_not_waive_complete_graph_or_source_checks(tmp_path, change):
    root, manifests, graph, lock = virtual_graph(tmp_path)
    change(graph)
    with pytest.raises(ContractError):
        first.graph_summary(graph, lock, root, manifests, local_packages=6, virtual=True)


def test_observed_fontations_first_new_bundle_when_explicitly_supplied():
    configured = os.environ.get("FONTATIONS_FIRST_RESOLUTION_INPUTS")
    if not configured or not Path(configured).is_file():
        pytest.skip("explicit retained first-new Fontations input envelope is required")
    checked = first.prepared_inputs(reference(Path(configured)))
    assert checked["profile"]["task_id"] == first.FONT_TASK
    assert [checked["graph_audits"][v]["packages"] for v in ("start", "golden")] == [98, 89]
    assert len(checked["archives"]) == 91 and len(checked["git_packages"]) == 1


@pytest.mark.parametrize("change", [
    lambda r: r.update(complete=False),
    lambda r: r.update(added_files=["extra.rs"]),
    lambda r: r.update(removed_files=["original.rs"]),
    lambda r: r.update(new_cargo_resolves=1),
    lambda r: r.update(new_git_fetches=1),
    lambda r: r.update(original_export_snapshot_rewritten=True),
    lambda r: r.update(no_original_cache_edits=False),
    lambda r: r.update(full_index_paths_modes_and_blobs_equal_frozen_commit_tree=False),
])
def test_private_git_index_revalidation_rejects_unaccounted_changes(tmp_path, change):
    record = {
        "schema_version": "reviewbench-explicit-private-git-index-revalidation/v1",
        "complete": True, "added_files": [], "removed_files": [],
        "new_cargo_resolves": 0, "new_git_fetches": 0,
        "original_export_snapshot_rewritten": False, "no_original_cache_edits": True,
        "full_index_paths_modes_and_blobs_equal_frozen_commit_tree": True,
    }
    change(record)
    path = tmp_path / "review.json"
    path.write_text(json.dumps(record))
    with pytest.raises(ContractError):
        first.private_index_revalidation({"exact_private_index_revalidation": reference(path)})


def test_source_curator_text_configuration_is_bound_without_reresolving(tmp_path, monkeypatch):
    from agentic_runtime_migration import rust_vendor
    root = tmp_path / "output"
    root.mkdir()
    paths = {}
    lock_rows = {}
    for index, variant in enumerate(("start", "golden")):
        path = tmp_path / (variant + ".lock")
        path.write_text('version=4\n[[package]]\nname="local"\nversion="0.1.0"\n')
        paths[f"{index}/Cargo.lock"] = reference(path)
        lock_rows[variant] = {**reference(path), "data": {"package": []}}
    source = "git+https://example.invalid/source#" + "a" * 40
    checked = {
        "locks": lock_rows, "archives": {}, "graphs": {"start": {"packages": []}, "golden": {"packages": []}},
        "cargo_cache": tmp_path / "cargo-home",
    }
    monkeypatch.setattr(first, "prepared_inputs", lambda _: checked)
    calls = []

    def materialize(locks, metadata, cargo_cache, destination, provider):
        calls.append((locks, cargo_cache))
        configuration, mapping = rust_vendor.source_configuration([first.CRATES_IO, source])
        for directory in mapping.values():
            (destination / directory).mkdir(parents=True)
        return configuration, [
            {"source": first.CRATES_IO, "files": 1, "omitted_vcs_metadata": []},
            {"source": source, "files": 2},
        ]

    monkeypatch.setattr(rust_vendor, "materialize", materialize)
    bundles = []

    def bundle(path):
        bundles.append(path.name)
        return {"path": str(root / "vendor.json"), "sha256": "b" * 64}

    def copy_asset(path, role):
        return reference(path)

    context = SimpleNamespace(output=root, scratch=tmp_path, bundle=bundle, copy_asset=copy_asset)
    spec = {"task_id": first.FONT_TASK, first.MARKER: {}, "first_new_resolution_inputs": {}}
    _, config_asset = first.vendor(context, spec, paths)
    assert len(calls) == 1 and calls[0][1] == checked["cargo_cache"]
    assert bundles == ["source-aware"]
    data = Path(config_asset["path"]).read_bytes()
    import tomllib
    document = tomllib.loads(data.decode())
    assert document["net"] == {"offline": True}
    assert spec[first.MARKER]["source_config_sha256"] == hashlib.sha256(data).hexdigest()
    assert set(spec[first.MARKER]["vendor_sources"]) == {first.CRATES_IO, source}
