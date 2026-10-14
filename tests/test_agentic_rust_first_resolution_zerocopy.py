"""Zerocopy current-candidate controls; original SUT runs are separately recorded."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_first_resolution as host
from agentic_runtime_migration import rust_first_resolution_entry as entry
from agentic_runtime_migration import rust_zerocopy_resolution as reader
from agentic_runtime_migration.rust_build_entry import OverlayError
from reviewbench_maintainability.agentic.contracts import ContractError


@pytest.fixture(scope="module")
def prepared():
    name = os.environ.get("ZEROCOPY_FIRST_RESOLUTION_INPUTS")
    if not name:
        pytest.skip("set ZEROCOPY_FIRST_RESOLUTION_INPUTS for original prepared input controls")
    path = Path(name)
    return host.prepared_inputs({"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})


@pytest.fixture
def runtime(prepared):
    profile = prepared["profile"]
    public = {
        "schema_version": host.RUNTIME_SCHEMA, "task_id": reader.TASK, "profile": profile["profile"],
        "toolchain": "1.81.0", "cargo_sha256": reader.CARGO_SHA, "rustc_sha256": reader.RUSTC_SHA,
        "cpus": 4, "max_log_bytes": 33554432, "metadata_timeout_seconds": 120,
        "command_timeout_seconds": 2400, "receipt_sha256": prepared["receipt_sha256"],
        "defaults": [{"selector_package": "zerocopy-derive", "asset_target": host.LOCK_PREFIX + "0",
                      "sha256": reader.LOCK_SHA}],
    }
    private = {
        "schema_version": host.PRIVATE_SCHEMA, "task_id": reader.TASK,
        "command": profile["command"],
        "harness": {"path": profile["harness"], "sha256": profile["harness_sha256"]},
        "expected_tests": profile["expected"],
    }
    return public, private, {host.LOCK_PREFIX + "0": prepared["root"] / "first-success/Cargo.lock"}


@pytest.fixture
def source(tmp_path, prepared):
    root = tmp_path / "candidate"
    shutil.copytree(prepared["root"] / "source-originals/start", root, symlinks=True)
    path = root / prepared["profile"]["harness"]
    path.write_bytes((prepared["root"] / "original-harness.rs").read_bytes())
    return root


def test_original_shared_lock_graph_and_transport_are_bound(prepared, runtime):
    public, private, _ = runtime
    entry.validate(public, private)
    assert prepared["locks"]["start"]["sha256"] == prepared["locks"]["golden"]["sha256"] == reader.LOCK_SHA
    assert len(prepared["archives"]) == 58
    assert all(row["packages"] == row["resolve_nodes"] == 61 for row in prepared["graph_audits"].values())
    profile = entry.profile_for(public)
    command = ["cargo", *profile["prefix"], *profile["command"]]
    actual = entry.transport(command, private, public)
    assert actual[-4:] == ["--locked", "--offline", "--", "--nocapture"]
    assert "-p" in actual and actual[actual.index("-p") + 1] == "zerocopy-derive"


@pytest.mark.parametrize("variant", ["start", "golden"])
def test_full_original_trees_keep_all_literal_links(prepared, runtime, tmp_path, variant):
    public, private, locks = runtime
    root = tmp_path / variant
    shutil.copytree(prepared["root"] / "source-originals" / variant, root, symlinks=True)
    (root / private["harness"]["path"]).write_bytes((prepared["root"] / "original-harness.rs").read_bytes())
    before = entry.snapshot(root)
    result = entry.prepare_mirror(root, tmp_path / "mirror", public, private, lock_sources=locks)
    after = entry.snapshot(tmp_path / "mirror")
    assert len(before) == 553 and len(after) == 554
    assert sum(row[0] == "link" for row in before.values()) == 155
    assert all(after[name] == row for name, row in before.items())
    assert entry.snapshot(root) == before
    assert result["selected_default"]["sha256"] == reader.LOCK_SHA
    assert (tmp_path / "mirror/tools/cargo-zerocopy/Cargo.toml").is_file()


def test_current_production_edit_is_not_restored_to_reference(source, runtime, tmp_path):
    public, private, locks = runtime
    path = source / "zerocopy-derive/src/lib.rs"
    path.write_bytes(path.read_bytes() + b"\n// current-candidate control, not a reference source\n")
    entry.prepare_mirror(source, tmp_path / "mirror", public, private, lock_sources=locks)
    assert (tmp_path / "mirror/zerocopy-derive/src/lib.rs").read_bytes() == path.read_bytes()


def test_existing_candidate_lock_is_preserved_and_bypasses_selector(source, runtime, tmp_path):
    public, private, locks = runtime
    data = Path(next(iter(locks.values()))).read_bytes() + b"\n# candidate-owned lock bytes\n"
    (source / "Cargo.lock").write_bytes(data)
    # Default selection would reject this local dependency rename. Existing
    # authored locks bypass default selection; full Cargo metadata still gates execution.
    local = source / "zerocopy-derive/Cargo.toml"
    local.write_text(local.read_text().replace('name = "zerocopy-derive"', 'name = "candidate-derive"', 1))
    result = entry.prepare_mirror(source, tmp_path / "mirror", public, private, lock_sources=locks)
    assert result["selected_default"] is None
    assert (tmp_path / "mirror/Cargo.lock").read_bytes() == data


def test_current_local_dependency_alias_selects_by_actual_package(source, runtime):
    public, _, _ = runtime
    root = source / "Cargo.toml"
    text = root.read_text().replace(
        'zerocopy-derive = { version = "=0.8.0-alpha.25", path = "zerocopy-derive", optional = true }',
        'candidate_derive = { package = "zerocopy-derive", version = "=0.8.0-alpha.25", path = "zerocopy-derive", optional = true }',
        1,
    )
    root.write_text(text)
    selected, reason = entry.select_default(source, public)
    assert selected["sha256"] == reader.LOCK_SHA and reason["dependency_alias"] == "candidate_derive"


def test_missing_default_rejects_wrong_local_package(source, runtime):
    public, _, _ = runtime
    local = source / "zerocopy-derive/Cargo.toml"
    local.write_text(local.read_text().replace('name = "zerocopy-derive"', 'name = "unrelated"', 1))
    with pytest.raises(OverlayError):
        entry.select_default(source, public)


def test_symlinked_or_escaping_lock_is_not_followed(source, runtime, tmp_path):
    public, private, locks = runtime
    external = tmp_path / "outside.lock"
    external.write_bytes(Path(next(iter(locks.values()))).read_bytes())
    (source / "Cargo.lock").symlink_to(external)
    with pytest.raises(OverlayError):
        entry.prepare_mirror(source, tmp_path / "mirror", public, private, lock_sources=locks)
    assert external.read_bytes() == Path(next(iter(locks.values()))).read_bytes()


def test_changed_private_harness_is_rejected(source, runtime, tmp_path):
    public, private, locks = runtime
    (source / private["harness"]["path"]).write_text("#[test] fn replacement() {}\n")
    with pytest.raises(OverlayError, match="harness changed"):
        entry.prepare_mirror(source, tmp_path / "mirror", public, private, lock_sources=locks)


@pytest.mark.parametrize("key,value", [
    ("toolchain", "1.93.0"), ("cpus", 2), ("command_timeout_seconds", 1800),
    ("metadata_timeout_seconds", 1), ("task_id", host.SCYLLA_TASK),
])
def test_profile_rejects_changed_original_contract(runtime, key, value):
    public, private, _ = runtime
    public = deepcopy(public)
    public[key] = value
    with pytest.raises(OverlayError):
        entry.validate(public, private)


@pytest.mark.parametrize("change", ["extra_default", "wrong_selector", "wrong_asset"])
def test_one_default_cannot_be_reinterpreted_by_variant(runtime, change):
    public, private, _ = runtime
    public = deepcopy(public)
    if change == "extra_default":
        public["defaults"].append(deepcopy(public["defaults"][0]))
    elif change == "wrong_selector":
        public["defaults"][0]["selector_package"] = "scylla"
    else:
        public["defaults"][0]["asset_target"] = "/tmp/another-lock"
    with pytest.raises(OverlayError):
        entry.validate(public, private)


def test_original_tree_reader_rejects_link_flattening(prepared, tmp_path):
    root = tmp_path / "source"
    shutil.copytree(prepared["root"] / "work/start", root, symlinks=True)
    projection = json.loads((prepared["root"] / "original-source-projection.json").read_text())
    row = projection["sources"]["start"]["files"]
    path = next(p for p in root.rglob("*") if p.is_symlink())
    data = path.read_bytes()
    path.unlink()
    path.write_bytes(data)
    with pytest.raises(ContractError, match="link changed"):
        reader.verify_original_tree(root, row)


def test_default_blob_tampering_is_rejected(source, runtime, tmp_path):
    public, private, locks = runtime
    bad = tmp_path / "changed-lock"
    bad.write_bytes(Path(next(iter(locks.values()))).read_bytes() + b"\n# altered default\n")
    with pytest.raises(OverlayError, match="hash changed"):
        entry.prepare_mirror(source, tmp_path / "mirror", public, private,
                             lock_sources={host.LOCK_PREFIX + "0": bad})
