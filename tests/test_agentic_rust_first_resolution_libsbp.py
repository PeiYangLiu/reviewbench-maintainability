"""Original libsbp input and current-candidate controls; no substitute SUT."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_first_resolution as host
from agentic_runtime_migration import rust_first_resolution_entry as entry
from agentic_runtime_migration import rust_libsbp_resolution as reader
from agentic_runtime_migration import rust_libsbp_entry as library
from agentic_runtime_migration.rust_build_entry import OverlayError
from reviewbench_maintainability.agentic.contracts import ContractError


@pytest.fixture(scope="module")
def checked():
    name = os.environ.get("LIBSBP_FIRST_RESOLUTION_INPUTS")
    if not name:
        pytest.skip("LIBSBP_FIRST_RESOLUTION_INPUTS binds the real original preparation")
    path = Path(name)
    return host.prepared_inputs({"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})


@pytest.fixture
def runtime(checked):
    profile = checked["profile"]
    public = {
        "schema_version": host.RUNTIME_SCHEMA, "task_id": reader.TASK, "profile": profile["profile"],
        "toolchain": "1.64.0", "cargo_sha256": reader.CARGO_SHA, "rustc_sha256": reader.RUSTC_SHA,
        "cpus": 4, "max_log_bytes": 33554432, "metadata_timeout_seconds": 120,
        "receipt_sha256": checked["receipt_sha256"],
        "defaults": [{"selector_package": "sbp", "asset_target": host.LOCK_PREFIX + "0", "sha256": reader.LOCK_SHA}],
    }
    private = {
        "schema_version": host.PRIVATE_SCHEMA, "task_id": reader.TASK, "command": profile["command"],
        "harness": None, "repository_test_blocks": profile["test_blocks"], "expected_tests": profile["expected"],
    }
    return public, private, {host.LOCK_PREFIX + "0": checked["root"] / "first-success/Cargo.lock"}


@pytest.fixture
def source(checked, tmp_path):
    path = tmp_path / "candidate"
    shutil.copytree(checked["root"] / "source-complete/start", path, symlinks=True)
    return path


def test_real_inputs_bind_all_five_original_updates_and_full_graph(checked):
    assert len(checked["archives"]) == 152
    assert {r["packages"] for r in checked["graph_audits"].values()} == {154}
    assert checked["locks"]["start"]["sha256"] == checked["locks"]["golden"]["sha256"] == reader.LOCK_SHA
    assert len(checked["profile"]["expected"]) == 17
    assert checked["profile"]["harness"] is None
    assert {name: row["commit"] for name, row in checked["modules"].items()} == reader.MODULES


def test_no_oracle_session_substitution_and_exact_library_transport(runtime):
    public, private, _ = runtime
    entry.validate(public, private)
    profile = entry.profile_for(public)
    command = ["cargo", *profile["prefix"], *profile["command"]]
    actual = entry.transport(command, private, public)
    assert actual[-4:] == ["--locked", "--offline", "--", "--nocapture"]
    assert actual[actual.index("test"):] == ["test", "-p", "sbp", "--lib", "--locked", "--offline", "--", "--nocapture"]
    assert profile["command_cwd"] == "rust" and profile["public"] == ["check", "-p", "sbp"]
    assert "oracle_session" not in actual


@pytest.mark.parametrize("variant", ["start", "golden"])
def test_complete_source_and_fixed_children_survive_mirror(checked, runtime, tmp_path, variant):
    public, private, locks = runtime
    original = checked["root"] / "source-complete" / variant
    before = entry.snapshot(original)
    result = entry.prepare_mirror(original, tmp_path / "mirror", public, private, lock_sources=locks)
    after = entry.snapshot(tmp_path / "mirror")
    assert len(before) == 2842 and len(after) == 2843
    assert sum(row[0] == "link" for row in before.values()) == 2
    assert all(after[name] == value for name, value in before.items())
    assert entry.snapshot(original) == before
    assert result["selection"]["selector_package"] == "sbp"
    assert (tmp_path / "mirror/rust/sbp2json/Cargo.lock").read_bytes() == (
        original / "rust/sbp2json/Cargo.lock").read_bytes()
    for name, child in checked["modules"].items():
        for relative in child["source"]["files"]:
            assert (tmp_path / "mirror" / name / relative).is_file() or (
                tmp_path / "mirror" / name / relative).is_symlink()


def test_production_edit_is_preserved_not_replaced_by_reference(source, runtime, tmp_path):
    public, private, locks = runtime
    path = source / "rust/sbp/src/de.rs"
    path.write_bytes(path.read_bytes() + b"\nfn new_production_helper() {}\n")
    entry.prepare_mirror(source, tmp_path / "mirror", public, private, lock_sources=locks)
    assert (tmp_path / "mirror/rust/sbp/src/de.rs").read_bytes() == path.read_bytes()


def test_fixed_items_may_move_without_freezing_production_file_paths(source, runtime):
    _, private, _ = runtime
    old = source / "rust/sbp/src/de.rs"
    old.rename(old.with_name("moved_decoder.rs"))
    found = library.verify_blocks(source, private["repository_test_blocks"])
    assert any(row["path"].endswith("moved_decoder.rs") for rows in found.values() for row in rows)


@pytest.mark.parametrize("kind", ["modified", "missing", "comment", "raw-string", "conditional", "duplicate"])
def test_original_test_items_cannot_be_replaced_or_hidden(source, runtime, kind):
    _, private, _ = runtime
    path = source / "rust/sbp/src/de.rs"
    data = path.read_bytes()
    marker = b"#[cfg(test)]\nmod tests {"
    at = data.index(marker)
    block = data[at:]
    if kind == "modified":
        path.write_bytes(data[:at] + block.replace(b"assert_eq!", b"changed_assert_eq!", 1))
    elif kind == "missing":
        path.write_bytes(data[:at])
    elif kind == "comment":
        path.write_bytes(data[:at] + b"/*\n" + block + b"*/\n")
    elif kind == "raw-string":
        path.write_bytes(data[:at] + b'const OLD: &str = r########"' + block + b'"########;\n')
    elif kind == "conditional":
        path.write_bytes(data[:at] + b'#[cfg(feature="hidden")]\nmod hidden {\n' + block + b"\n}\n")
    else:
        (source / "rust/sbp/src/copied_test_item.rs").write_bytes(block)
    with pytest.raises((OverlayError, entry.guard.SelectionError)):
        library.verify_blocks(source, private["repository_test_blocks"])


def test_existing_authored_lock_is_not_overwritten(source, runtime, tmp_path):
    public, private, locks = runtime
    data = Path(next(iter(locks.values()))).read_bytes() + b"\n# candidate-authored bytes\n"
    (source / "Cargo.lock").write_bytes(data)
    result = entry.prepare_mirror(source, tmp_path / "mirror", public, private, lock_sources=locks)
    assert result["selected_default"] is None
    assert (tmp_path / "mirror/Cargo.lock").read_bytes() == data


def test_missing_default_requires_current_workspace_package(source, runtime):
    public, _, _ = runtime
    path = source / "rust/sbp/Cargo.toml"
    path.write_text(path.read_text().replace('name = "sbp"', 'name = "unrelated"', 1))
    with pytest.raises(OverlayError, match="workspace"):
        entry.select_default(source, public)


@pytest.mark.parametrize("field,value", [
    ("toolchain", "1.93.0"), ("cpus", 1), ("metadata_timeout_seconds", 0), ("task_id", host.ZEROCOPY_TASK),
])
def test_private_contract_fields_are_not_reinterpreted(runtime, field, value):
    public, private, _ = runtime
    public = deepcopy(public)
    public[field] = value
    with pytest.raises(OverlayError):
        entry.validate(public, private)


@pytest.mark.parametrize("change", ["harness", "test_ids", "test_blocks", "command"])
def test_private_library_contract_is_exact(runtime, change):
    public, private, _ = runtime
    private = deepcopy(private)
    if change == "harness":
        private["harness"] = {"path": "tests/oracle_session.rs", "sha256": "a" * 64}
    elif change == "test_ids":
        private["expected_tests"].pop()
    elif change == "test_blocks":
        private["repository_test_blocks"].pop()
    else:
        private["command"] = ["test", "-p", "sbp", "--", "--nocapture"]
    with pytest.raises(OverlayError):
        entry.validate(public, private)


def test_actual_original_graph_selects_library_target(checked, runtime):
    public, private, _ = runtime
    graph = checked["graphs"]["start"]
    provided = {(p["name"], p["version"], p["source"]): p["manifest_path"]
                for p in graph["packages"] if p.get("source")}
    result = entry.verify_graph(graph, checked["root"] / "work/start",
                                entry.parse_lock((checked["root"] / "first-success/Cargo.lock").read_bytes(), public),
                                provided, private, public)
    assert result["packages"] == result["nodes"] == 154


def test_disabling_actual_library_target_is_rejected(checked, runtime):
    public, private, _ = runtime
    graph = deepcopy(checked["graphs"]["start"])
    for package in graph["packages"]:
        if package["name"] == "sbp" and package.get("source") is None:
            for target in package["targets"]:
                if target["kind"] == ["lib"]:
                    target["test"] = False
    provided = {(p["name"], p["version"], p["source"]): p["manifest_path"]
                for p in graph["packages"] if p.get("source")}
    with pytest.raises(OverlayError, match="lib target"):
        entry.verify_graph(graph, checked["root"] / "work/start",
                           entry.parse_lock((checked["root"] / "first-success/Cargo.lock").read_bytes(), public),
                           provided, private, public)


def test_original_warning_is_recognized_without_editing_manifests(checked, runtime):
    public, _, _ = runtime
    root = checked["root"] / "work/start"
    text = (checked["root"] / "commands/metadata-start/stderr").read_text()
    assert entry.metadata_stderr_allowed(text, public, root=root)
    with pytest.raises(OverlayError):
        entry.metadata_stderr_allowed(text.replace(str(root), "/outside"), public, root=root)
    assert not entry.metadata_stderr_allowed(text + "unexpected diagnostic\n", public, root=root)


def test_platform_requires_real_pinned_children_and_native_image(checked):
    seen = []
    def bundle(path, *, role, git_commit):
        seen.append((path, role, git_commit))
        return {"path": str(path / "descriptor"), "sha256": "a" * 64}
    # This is the same sealed preparation, not a fake omission-compatible source.
    reference = checked["envelope"]
    path = Path(os.environ["LIBSBP_FIRST_RESOLUTION_INPUTS"])
    image, modules = reader.platform(
        SimpleNamespace(bundle=bundle),
        {"first_new_resolution_inputs": {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}},
    )
    assert image == reader.IMAGE
    assert len(seen) == len(modules) == 3
    assert {m["path"]: m["commit"] for m in modules} == reader.MODULES
    assert all(not m.get("omit") and "asset" in m for m in modules)
    assert all(role == "submodule" for _, role, _ in seen)
