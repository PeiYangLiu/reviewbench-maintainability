"""Offline transport/data fixtures. These never run the VFS SUT or Cargo."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
from types import SimpleNamespace

import pytest

from test_agentic_rust_vfs_resolution import (
    original_config, ref, resolution, entry, ContractError,
)


@pytest.fixture
def synthetic(tmp_path, monkeypatch):
    # Synthetic data exercises the same parser/overlay. Separately, retained
    # receipt tests validate the real 102-package graph without executing Cargo.
    source = tmp_path / "candidate"
    (source / "src").mkdir(parents=True)
    manifest = b'[package]\nname="vfs"\nversion="0.1.0"\nedition="2021"\n'
    (source / "Cargo.toml").write_bytes(manifest)
    (source / "src/lib.rs").write_text("pub fn current() -> bool { true }\n")
    packages = [{"name": "vfs", "version": "0.1.0"}]
    for n in range(101):
        packages.append({"name": "tokio-stream" if n == 0 else "dep" + str(n),
                         "version": "0.1.14" if n == 0 else "1.0.0",
                         "source": "registry+https://github.com/rust-lang/crates.io-index",
                         "checksum": "f" * 64})
    lock = b"version = 4\n" + b"".join(
        ("\n[[package]]\n" + "".join(f"{k} = {json.dumps(v)}\n" for k, v in p.items())).encode()
        for p in packages)
    lockfile = tmp_path / "first-Cargo.lock"
    lockfile.write_bytes(lock)
    monkeypatch.setattr(entry, "LOCK", hashlib.sha256(lock).hexdigest())
    monkeypatch.setattr(entry, "MANIFEST", hashlib.sha256(manifest).hexdigest())
    spec = {"schema_version": "reviewbench-cargo-build-overlay/v1", "task_id": entry.TASK,
            "manifests": [], "private_members": [], "cpus": 4,
            "locks": [{"path": "Cargo.lock", "asset_target": "/opt/reviewbench-assets/rust-lock-0",
                       "sha256": entry.LOCK, "allow_missing": True, "replace_if_sha256": []}],
            entry.MARKER: entry.marker("1" * 64, "2" * 64)}
    monkeypatch.setenv("CARGO_TARGET_DIR", str(tmp_path / "targets"))
    vendor = tmp_path / "vendor"
    for p in packages:
        if p.get("source") is None:
            continue
        folder = vendor / (p["name"] + "-" + p["version"])
        folder.mkdir(parents=True)
        manifest = folder / "Cargo.toml"
        manifest.write_text('[package]\nname=' + json.dumps(p["name"]) +
                            '\nversion=' + json.dumps(p["version"]) + '\n')
        (folder / ".cargo-checksum.json").write_text(json.dumps({
            "package": p["checksum"], "files": {"Cargo.toml": ref(manifest)["sha256"]}}))
    return source, lockfile, spec, {spec["locks"][0]["asset_target"]: str(lockfile)}, packages


def simple_graph(packages, root):
    rows = []
    for p in packages:
        local = p.get("source") is None
        folder = root if p["name"] == "vfs" else root / p["name"] if local else (
            root.parent / "vendor" / (p["name"] + "-" + p["version"]))
        rows.append({**p, "id": p["name"], "source": p.get("source"),
                     "manifest_path": str(folder / "Cargo.toml"),
                     "targets": [{"kind": ["lib"], "name": p["name"], "src_path": str(folder / "src/lib.rs")}]})
    return {"version": 1, "workspace_root": str(root), "workspace_members": ["vfs"],
            "packages": rows, "resolve": {"root": "vfs", "nodes": [
                {"id": p["name"], "dependencies": [], "deps": [], "features": []} for p in packages]}}


def bind_simple_graph(monkeypatch, packages):
    normalized = [{"package": (p["name"], p["version"], p.get("source")),
                   "features": [], "dependencies": [], "deps": []} for p in packages]
    normalized.sort(key=lambda n: n["package"])
    monkeypatch.setattr(entry, "GRAPH", hashlib.sha256(json.dumps(
        normalized, sort_keys=True, separators=(",", ":")).encode()).hexdigest())


@pytest.mark.parametrize("present", [False, True])
def test_only_if_missing_default_preserves_complete_candidate_and_existing_exact_lock(synthetic, present):
    source, lock, spec, assets, _ = synthetic
    if present:
        (source / "Cargo.lock").write_bytes(lock.read_bytes())
    before = entry.guard.fingerprint(source)
    mirror = source.parent / "mirror"
    report = entry.prepare_mirror(source, mirror, spec, lock_sources=assets)
    assert entry.guard.fingerprint(source) == before
    assert (mirror / "Cargo.lock").read_bytes() == lock.read_bytes()
    assert (mirror / "src/lib.rs").read_bytes() == (source / "src/lib.rs").read_bytes()
    assert report["source_before"]["locks"] == ({"Cargo.lock": entry.LOCK} if present else {})
    assert report["mirror_after"]["locks"] == {"Cargo.lock": entry.LOCK}


@pytest.mark.parametrize("change", ["lock", "manifest", "nested_manifest", "symlink", "target"])
def test_changed_candidate_lock_or_manifest_is_rejected_without_overwrite(synthetic, change):
    source, lock, spec, assets, _ = synthetic
    if change == "lock":
        (source / "Cargo.lock").write_bytes(lock.read_bytes().replace(b'0.1.14', b'0.1.17'))
    elif change == "manifest":
        (source / "Cargo.toml").write_text('[invalid TOML\n')
    elif change == "nested_manifest":
        (source / "nested").mkdir()
        (source / "nested/Cargo.toml").write_text('[invalid TOML\n')
    elif change == "symlink":
        (source / "Cargo.lock").symlink_to("src/lib.rs")
    else:
        (source / "target").mkdir()
        (source / "target/old.rlib").write_bytes(b"old build")
    before = entry.guard.fingerprint(source)
    with pytest.raises((entry.OverlayError, ValueError)):
        entry.prepare_mirror(source, source.parent / "mirror", spec, lock_sources=assets)
    assert entry.guard.fingerprint(source) == before
    assert not (source.parent / "mirror").exists()


@pytest.mark.parametrize("change", ["task", "plan", "historical", "pin", "allow_missing",
                                  "replace", "manifests", "extra", "cpus"])
def test_runtime_spec_cannot_widen_only_this_task_missing_lock_policy(synthetic, change):
    _, _, spec, _, _ = synthetic
    if change == "task":
        spec["task_id"] = "other"
    elif change == "plan":
        spec[entry.MARKER]["plan_sha256"] = "0" * 64
    elif change == "historical":
        spec[entry.MARKER]["historical_restoration"] = True
    elif change == "pin":
        spec[entry.MARKER]["original_prepare_command"] = entry.PREPARE.replace("0.1.14", "0.1.17")
    elif change == "allow_missing":
        spec["locks"][0]["allow_missing"] = False
    elif change == "replace":
        spec["locks"][0]["replace_if_sha256"] = ["0" * 64]
    elif change == "manifests":
        spec["manifests"] = [{"path": "Cargo.toml", "operations": []}]
    elif change == "extra":
        spec[entry.MARKER]["if_golden_passes"] = True
    else:
        spec["cpus"] = True
    with pytest.raises(entry.OverlayError):
        entry.validate(spec)


@pytest.mark.parametrize("tail", [
    ["check", "--features", "async-vfs"],
    ["test", "--features", "async-vfs", "--test", "oracle_start", "--", "--nocapture", "--test-threads=1"],
    ["test", "--features", "async-vfs", "--test", "oracle_golden", "--", "--nocapture", "--test-threads=1"],
    ["test", "--no-run", "--message-format=json", "--features", "async-vfs",
     "--test", "__reviewbench_vfs_api_oracle_start", "--", "--nocapture", "--test-threads=1"],
    ["metadata", "--format-version", "1", "--offline", "--locked"],
])
def test_transport_preserves_original_argument_order_and_libtest_tail(tail):
    original = [*entry.PREFIX, *tail]
    command = entry.locked_argv(original)
    end = original.index("--") if "--" in original else len(original)
    effective_head = command[1:command.index("--")] if "--" in command else command[1:]
    assert command[0] == entry.REAL_CARGO
    assert effective_head[:end] == original[:end]
    assert "--locked" in effective_head and "--offline" in effective_head
    if "--" in original:
        assert command[command.index("--"):] == original[end:]
    assert original == [*entry.PREFIX, *tail]


@pytest.mark.parametrize("tail", [
    ["update", "-p", "tokio-stream", "--precise", "0.1.14"],
    ["generate-lockfile"], ["fetch"], ["build"], ["test", "--manifest-path", "elsewhere/Cargo.toml"],
    ["test", "--config=net.offline=false"], ["+stable", "test"], ["test", "-Zunstable-options"],
])
def test_runtime_never_updates_resolves_or_changes_toolchain_source_selection(tail):
    with pytest.raises(entry.OverlayError):
        entry.locked_argv([*entry.PREFIX, *tail])


@pytest.mark.parametrize("text", ["SIGINT", "SIGSEGV", "signal: 11", "failed to run custom build command"])
def test_full_metadata_data_is_not_scanned_as_compiler_diagnostic(synthetic, monkeypatch, text):
    source, lock, _, _, packages = synthetic
    (source / "Cargo.lock").write_bytes(lock.read_bytes())
    bind_simple_graph(monkeypatch, packages)
    graph = simple_graph(packages, source)
    graph["packages"][0]["keywords"] = [text]
    data = json.dumps(graph)
    record = {"stdout": data, "stderr": "", "returncode": 0, "limit": None,
              "stdout_total_bytes": len(data.encode()), "stdout_captured_bytes": len(data.encode())}
    before = copy.deepcopy(record)
    result = entry.verify_graph(source, source.parent, run=lambda *a: record, vendor=source.parent / "vendor")
    assert result["record"] is record and record == before
    assert result["graph_sha256"] == entry.GRAPH
    assert "--all-features" in result["command"] and "--locked" in result["command"]
    assert "update" not in result["command"]


@pytest.mark.parametrize("change", [
    "rc1", "rc101", "rc137", "signal6", "signal11", "stderr", "unknown_stderr", "limit",
    "truncate", "prefix", "suffix", "duplicate_json", "node", "package", "edge",
    "lock_mutation", "source_mutation",
])
def test_offline_metadata_fails_closed_on_missing_graph_or_process_failure(synthetic, monkeypatch, change):
    source, lock, _, _, packages = synthetic
    (source / "Cargo.lock").write_bytes(lock.read_bytes())
    bind_simple_graph(monkeypatch, packages)
    graph = simple_graph(packages, source)
    if change == "node":
        graph["resolve"]["nodes"].pop()
    elif change == "package":
        graph["packages"].pop()
    elif change == "edge":
        graph["resolve"]["nodes"][0]["dependencies"] = ["missing"]
    data = json.dumps(graph)
    record = {"stdout": data, "stderr": "", "returncode": 0, "limit": None,
              "stdout_total_bytes": len(data.encode()), "stdout_captured_bytes": len(data.encode())}
    if change.startswith("rc"):
        record["returncode"] = int(change[2:])
    elif change.startswith("signal"):
        record["returncode"] = -int(change[6:])
    elif change in {"stderr", "unknown_stderr"}:
        record["stderr"] = "process failed: SIGSEGV" if change == "stderr" else "unaccounted progress"
    elif change == "limit":
        record["limit"] = "deadline"
    elif change == "truncate":
        record["stdout_total_bytes"] += 1
    elif change == "prefix":
        record["stdout"] = "SIGINT\n" + data
    elif change == "suffix":
        record["stdout"] += "\n{}"
    elif change == "duplicate_json":
        record["stdout"] = '{"version":0,' + data[1:]
    def run(*args):
        if change == "lock_mutation":
            (source / "Cargo.lock").write_text("mutated by process")
        elif change == "source_mutation":
            (source / "src/lib.rs").write_text("// changed by metadata child\n")
        return record
    with pytest.raises((entry.OverlayError, entry.guard.SelectionError, ValueError)):
        entry.verify_graph(source, source.parent, run=run, vendor=source.parent / "vendor")


@pytest.mark.parametrize("returncode", [0, 101, -6, -9, -11, -15, 124, 137])
def test_shim_retains_exit_and_stream_semantics_without_selecting_a_form(synthetic, monkeypatch, returncode):
    source, lock, spec, _, _ = synthetic
    (source / "Cargo.lock").write_bytes(lock.read_bytes())
    specfile = source.parent / "spec.json"
    specfile.write_text(json.dumps(spec))
    monkeypatch.setattr(entry, "SPEC", str(specfile))
    actual_sha = entry.sha
    monkeypatch.setattr(entry, "sha", lambda p: {
        entry.REAL_CARGO: entry.CARGO_SHA, entry.REAL_RUSTC: entry.RUSTC_SHA,
    }.get(str(p)) or actual_sha(p))
    monkeypatch.chdir(source)
    monkeypatch.setenv("VFS_FIRST_RESOLUTION_LOGS", str(source.parent))
    calls = []
    def run(command, **kwargs):
        assert not any(k in kwargs for k in ("stdout", "stderr", "capture_output"))
        assert kwargs["env"]["CARGO_NET_OFFLINE"] == "true"
        calls.append(command)
        return SimpleNamespace(returncode=returncode)
    monkeypatch.setattr(entry.subprocess, "run", run)
    code = entry.cargo_shim([*entry.PREFIX, "check", "--features", "async-vfs"])
    assert code == (returncode if returncode >= 0 else 128 - returncode)
    assert len(calls) == 1
    transport = json.loads(next(source.parent.glob("cargo-transport-*.json")).read_text())
    assert transport["returncode"] == returncode
    assert transport["original_argv"][1:] == [*entry.PREFIX, "check", "--features", "async-vfs"]


def test_runtime_assets_separate_public_transport_from_original_private_selector(tmp_path, synthetic):
    _, _, spec, _, _ = synthetic
    tools = tmp_path / "rust-build-tools"
    tools.mkdir()
    (tools / "build.py").write_text("# synthetic existing public build helper\n")
    bundles = []
    def bundle(root, role):
        bundles.append({str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()})
        return {"path": str(tmp_path / "descriptor.json"), "sha256": "a" * 64}
    ctx = SimpleNamespace(output=tmp_path, bundle=bundle)
    private = ["python3", "/opt/reviewbench-assets/rust-vfs-tools/rust_vfs_entry.py",
               "--spec", "/opt/reviewbench-assets/rust-vfs-spec.json"]
    public = ["cargo", *entry.PREFIX, "check", "--features", "async-vfs"]
    execute, public_command, assets = resolution.binding(ctx, spec, private, public)
    assert shlex.split(execute)[-len(private):] == private
    assert shlex.split(public_command)[-len(public):] == public
    assert all(a["visibility"] == "developer" for a in assets)
    assert not any("rust_vfs_entry.py" in name or "spec.json" in name for name in bundles[0])
    assert not any(b"in_memory_async_path_round_trips_file_contents" in data for data in bundles[0].values())
    assert (tmp_path / "rust-vfs-resolution-tools/bin/cargo").stat().st_mode & 0o111


@pytest.mark.parametrize("present_lock", [False, True])
@pytest.mark.parametrize("change", ["comment", "metadata", "formatting"])
def test_candidate_manifest_text_and_own_same_graph_lock_are_not_reference_hash_selected(
        synthetic, present_lock, change):
    source, lock, spec, assets, packages = synthetic
    manifest = source / "Cargo.toml"
    if change == "comment":
        manifest.write_text(manifest.read_text() + "# unrelated candidate comment\n")
    elif change == "metadata":
        manifest.write_text(manifest.read_text() + '[package.metadata.current]\nnote="unrelated"\n')
    else:
        manifest.write_text(manifest.read_text().replace('name="vfs"', 'name = "vfs"'))
    if present_lock:
        (source / "Cargo.lock").write_bytes(lock.read_bytes() + b"\n# candidate-owned serialization\n")
    before = entry.guard.fingerprint(source)
    mirror = source.parent / "mirror"
    entry.prepare_mirror(source, mirror, spec, lock_sources=assets)
    assert entry.guard.fingerprint(source) == before
    assert (mirror / "Cargo.toml").read_bytes() == manifest.read_bytes()
    assert (mirror / "Cargo.lock").read_bytes() == (
        (source / "Cargo.lock").read_bytes() if present_lock else lock.read_bytes())
    graph = simple_graph(packages, mirror)
    # The supplied fixture vendor is sibling to the complete source mirror.
    record = {"stdout": json.dumps(graph), "stderr": "", "returncode": 0, "limit": None}
    result = entry.verify_graph(mirror, source.parent, run=lambda *a: record,
                                vendor=source.parent / "vendor")
    assert result["current_lock_sha256"] == entry.sha(mirror / "Cargo.lock")
    assert result["selection_uses_reference_hashes"] is False
    assert (result["current_lock_sha256"] != entry.LOCK) is present_lock
    assert entry.guard.fingerprint(source) == before


def test_current_candidate_can_supply_complete_new_local_manifest_and_own_graph(synthetic):
    source, lock, spec, assets, packages = synthetic
    (source / "peer/src").mkdir(parents=True)
    (source / "peer/Cargo.toml").write_text('[package]\nname="peer"\nversion="1.0.0"\n')
    (source / "peer/src/lib.rs").write_text("pub fn current_peer() -> bool { true }\n")
    (source / "Cargo.toml").write_text((source / "Cargo.toml").read_text() +
                                      '[dependencies]\npeer={path="peer"}\n')
    (source / "Cargo.lock").write_bytes(lock.read_bytes() + b'\n[[package]]\nname="peer"\nversion="1.0.0"\n')
    graph_packages = [*packages, {"name": "peer", "version": "1.0.0"}]
    before = entry.guard.fingerprint(source)
    mirror = source.parent / "mirror"
    entry.prepare_mirror(source, mirror, spec, lock_sources=assets)
    graph = simple_graph(graph_packages, mirror)
    root = next(n for n in graph["resolve"]["nodes"] if n["id"] == "vfs")
    root["dependencies"] = ["peer"]
    root["deps"] = [{"name": "peer", "pkg": "peer", "dep_kinds": [{"kind": None, "target": None}]}]
    record = {"stdout": json.dumps(graph), "stderr": "", "returncode": 0, "limit": None}
    result = entry.verify_graph(mirror, source.parent, run=lambda *a: record,
                                vendor=source.parent / "vendor")
    assert result["current_packages"] == 103 and result["graph_sha256"] != entry.GRAPH
    assert result["current_lock_sha256"] != entry.LOCK
    assert entry.guard.fingerprint(source) == before
    assert (mirror / "peer/src/lib.rs").read_bytes() == (source / "peer/src/lib.rs").read_bytes()


@pytest.mark.parametrize("change", ["missing", "checksum", "manifest_bytes", "new_package"])
def test_missing_or_unbound_vendor_is_not_resolved_or_replaced(synthetic, change):
    source, lock, spec, assets, packages = synthetic
    own = lock.read_bytes() + b"\n# own valid lock formatting\n"
    if change == "new_package":
        own += (b'\n[[package]]\nname="not-provided"\nversion="1.0.0"\n'
                b'source="registry+https://github.com/rust-lang/crates.io-index"\nchecksum="' + b"a" * 64 + b'"\n')
    (source / "Cargo.lock").write_bytes(own)
    if change != "new_package":
        folder = source.parent / "vendor/tokio-stream-0.1.14"
        if change == "missing":
            (folder / ".cargo-checksum.json").unlink()
        elif change == "checksum":
            (folder / ".cargo-checksum.json").write_text(json.dumps({"package": "a" * 64, "files": {}}))
        else:
            (folder / "Cargo.toml").write_text('[package]\nname="wrong"\nversion="0.1.14"\n')
    before = entry.guard.fingerprint(source)
    with pytest.raises(entry.OverlayError):
        entry.verify_graph(source, source.parent, vendor=source.parent / "vendor",
                           run=lambda *a: pytest.fail("must not resolve/download a missing package"))
    assert entry.guard.fingerprint(source) == before and (source / "Cargo.lock").read_bytes() == own


@pytest.mark.parametrize("change", ["local_manifest", "local_target", "external_manifest",
                                  "workspace_member", "root", "missing_edge", "missing_lock_package"])
def test_current_metadata_must_bind_all_graph_members_to_current_source_and_provided_vendor(synthetic, change):
    source, lock, _, _, packages = synthetic
    (source / "Cargo.lock").write_bytes(lock.read_bytes() + b"\n# not a default hash\n")
    graph = simple_graph(packages, source)
    if change == "local_manifest":
        graph["packages"][0]["manifest_path"] = "/foreign/Cargo.toml"
    elif change == "local_target":
        graph["packages"][0]["targets"][0]["src_path"] = "/foreign/lib.rs"
    elif change == "external_manifest":
        graph["packages"][1]["manifest_path"] = "/other-vendor/tokio-stream-0.1.14/Cargo.toml"
    elif change == "workspace_member":
        graph["workspace_members"].append("tokio-stream")
    elif change == "root":
        graph["resolve"]["root"] = "tokio-stream"
    elif change == "missing_edge":
        graph["resolve"]["nodes"][0]["dependencies"] = ["tokio-stream"]
    else:
        graph["packages"].pop()
        graph["resolve"]["nodes"].pop()
    record = {"stdout": json.dumps(graph), "stderr": "", "returncode": 0, "limit": None}
    before = entry.guard.fingerprint(source)
    with pytest.raises(entry.OverlayError):
        entry.verify_graph(source, source.parent, run=lambda *a: record, vendor=source.parent / "vendor")
    assert entry.guard.fingerprint(source) == before


def test_legitimate_candidate_change_needing_update_retains_real_locked_failure_without_repair(synthetic):
    source, lock, spec, assets, _ = synthetic
    (source / "Cargo.lock").write_bytes(lock.read_bytes() + b"\n# own lock\n")
    (source / "Cargo.toml").write_text((source / "Cargo.toml").read_text() + '[features]\nextra=[]\n')
    before = entry.guard.fingerprint(source)
    mirror = source.parent / "mirror"
    entry.prepare_mirror(source, mirror, spec, lock_sources=assets)
    calls = []
    def run(command, *_):
        calls.append(command)
        assert "update" not in command and "--locked" in command and "--offline" in command
        return {"stdout": "", "stderr": "error: the lock file needs to be updated but --locked was passed",
                "returncode": 101, "limit": None}
    with pytest.raises(entry.OverlayError):
        entry.verify_graph(mirror, source.parent, run=run, vendor=source.parent / "vendor")
    assert len(calls) == 1 and entry.guard.fingerprint(source) == before
    assert (mirror / "Cargo.lock").read_bytes() == (source / "Cargo.lock").read_bytes()


@pytest.mark.parametrize("cpus", [1, 2, 3, 5, 8])
def test_runtime_resource_declaration_must_retain_original_four_jobs(synthetic, cpus):
    spec = synthetic[2]
    spec["cpus"] = cpus
    with pytest.raises(entry.OverlayError):
        entry.validate(spec)
