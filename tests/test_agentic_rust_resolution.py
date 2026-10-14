"""Candidate-selected dependency defaults must not select by replay labels."""
import hashlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tomllib
from types import SimpleNamespace

import pytest
import tomli_w

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration.rust_build_entry import OverlayError, overlay_files, synchronize_candidate
from reviewbench_maintainability.agentic.contracts import ContractError


def digest(data):
    return hashlib.sha256(data).hexdigest()


def lock_fixture(tmp_path):
    source = tmp_path / "candidate"
    source.mkdir()
    (source / "Cargo.toml").write_text('[package]\nname="app"\nversion="0.1.0"\n')
    (source / "Cargo.lock").write_text("version=3\n# original locked graph\n")
    (source / "lib.rs").write_text("pub fn candidate_owned() -> u8 { 7 }\n")
    asset = tmp_path / "recorded-resolution.lock"
    asset.write_text("version=3\n# recorded compatible graph\n")
    original = (source / "Cargo.lock").read_bytes()
    record = {
        "path": "Cargo.lock", "asset_target": str(asset), "sha256": digest(asset.read_bytes()),
        "replace_if_sha256": [digest(original)], "allow_missing": False,
        "when": [{"path": "Cargo.toml", "key": ["dependencies", "bytemuck"],
                  "value": {"version": "1.21.0", "optional": True}}],
    }
    return source, asset, original, {"locks": [record]}


def select_manifest(source):
    (source / "Cargo.toml").write_text(
        '[package]\nname="app"\nversion="0.1.0"\n'
        '[dependencies]\nbytemuck={version="1.21.0",optional=true}\n'
    )


def test_resolution_default_does_not_replace_the_start_candidates_lock(tmp_path):
    source, asset, original, spec = lock_fixture(tmp_path)
    result = overlay_files(source, spec)
    assert "Cargo.lock" not in result
    assert (source / "Cargo.lock").read_bytes() == original


def test_resolution_default_does_not_restore_a_candidate_deleted_lock(tmp_path):
    source, asset, original, spec = lock_fixture(tmp_path)
    select_manifest(source)
    (source / "Cargo.lock").unlink()
    assert "Cargo.lock" not in overlay_files(source, spec)
    assert not (source / "Cargo.lock").exists()


def test_resolution_selects_only_known_candidate_data_and_preserves_candidate_files(tmp_path):
    source, asset, original, spec = lock_fixture(tmp_path)
    select_manifest(source)
    (source / "new-helper.rs").write_text("pub fn new_helper() {}\n")
    before = {p.name: p.read_bytes() for p in source.iterdir() if p.is_file()}
    assert overlay_files(source, spec)["Cargo.lock"] == asset.read_bytes()
    mirror = tmp_path / "mirror"
    synchronize_candidate(source, mirror, spec)
    assert (mirror / "Cargo.lock").read_bytes() == asset.read_bytes()
    assert (mirror / "new-helper.rs").read_bytes() == before["new-helper.rs"]
    assert all((source / name).read_bytes() == data for name, data in before.items())
    # A compatible public manifest is sufficient, regardless of the directory
    # name or any caller-supplied replay label.
    spec["variant"] = "start"
    assert overlay_files(source, spec)["Cargo.lock"] == asset.read_bytes()
    (source / "Cargo.lock").write_bytes(original + b"# candidate-edited graph\n")
    assert "Cargo.lock" not in overlay_files(source, spec)


def test_resolution_does_not_coerce_unrecognized_candidate_dependency_or_read_inactive_asset(tmp_path):
    source, asset, original, spec = lock_fixture(tmp_path)
    select_manifest(source)
    (source / "Cargo.toml").write_text((source / "Cargo.toml").read_text().replace("1.21.0", "1.22.0"))
    asset.unlink()
    assert overlay_files(source, spec) == {}
    assert (source / "Cargo.lock").read_bytes() == original


@pytest.mark.parametrize("predicate", [
    [], [{"path": "lib.rs", "key": ["dependencies"], "value": {}}],
    [{"path": "../Cargo.toml", "key": ["dependencies"], "value": {}}],
    [{"path": "Cargo.toml", "key": [], "value": {}}],
    [{"path": "Cargo.toml", "key": "dependencies", "value": {}}],
    [{"path": "Cargo.toml", "key": ["dependencies"], "value": {}, "variant": "golden"}],
])
def test_resolution_rejects_unbound_or_non_manifest_selectors(tmp_path, predicate):
    source, asset, original, spec = lock_fixture(tmp_path)
    spec["locks"][0]["when"] = predicate
    with pytest.raises(OverlayError):
        overlay_files(source, spec)


def test_resolution_checks_active_asset_hash_and_rejects_ambiguous_defaults(tmp_path):
    source, asset, original, spec = lock_fixture(tmp_path)
    select_manifest(source)
    spec["locks"].append(copy.deepcopy(spec["locks"][0]))
    with pytest.raises(OverlayError, match="ambiguous"):
        overlay_files(source, spec)
    spec["locks"].pop()
    asset.write_text("different lock bytes")
    with pytest.raises(OverlayError, match="dependency lock changed"):
        overlay_files(source, spec)


def test_resolution_selector_rejects_manifest_symlinks_and_distinguishes_boolean_from_integer(tmp_path):
    source, asset, original, spec = lock_fixture(tmp_path)
    select_manifest(source)
    (source / "Cargo.toml").write_text((source / "Cargo.toml").read_text().replace("optional=true", "optional=1"))
    assert overlay_files(source, spec) == {}
    (source / "Cargo.toml").unlink()
    (tmp_path / "outside.toml").write_text('[dependencies]\nbytemuck={version="1.21.0",optional=true}\n')
    (source / "Cargo.toml").symlink_to(tmp_path / "outside.toml")
    with pytest.raises(OverlayError, match="escapes"):
        overlay_files(source, spec)


def recorded_fixture(tmp_path, monkeypatch):
    from agentic_runtime_migration import rust_resolution as resolution, rust_overlay
    start_manifest = b"[dependencies]\n"
    resolved_manifest = b'[dependencies]\nbytemuck={version="1.21.0",optional=true}\n'
    old_packages = [
        {"name": name, "version": version, "source": resolution.CRATES_IO, "checksum": "a" * 64}
        for name, version in sorted(resolution.REMOVED)
    ]
    new_packages = [
        {"name": name, "version": version, "source": resolution.CRATES_IO, "checksum": checksum}
        for (name, version), checksum in sorted(resolution.ADDED.items())
    ]
    old_lock = tomli_w.dumps({"version": 3, "package": old_packages}).encode()
    new_lock = tomli_w.dumps({"version": 3, "package": new_packages}).encode()
    assertion = "#[test]\nfn frozen_assertion() { assert_eq!(2, 2); }\n"
    plan = {"input_index": 18, "session_files": [{"path": resolution.SESSION, "content": assertion}]}
    legacy = tmp_path / "legacy"
    files = {
        "rust/plans.d/18.json": json.dumps(plan).encode(),
        "rust/" + resolution.ARTIFACTS + "/golden/Cargo.lock": new_lock,
        "rust/" + resolution.ARTIFACTS + "/logs/golden-run1-attempt1.log":
            ("\n".join(resolution.RESOLUTION_LINES) + "\n").encode(),
    }
    for name, contents in files.items():
        path = legacy / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
    patch = tmp_path / "reference.patch"
    patch.write_text("fixture patch whose bytes are bound independently")
    pins = dict(resolution.PINS, plan=digest(files["rust/plans.d/18.json"]), patch=digest(patch.read_bytes()),
                session=digest(assertion.encode()), start_manifest=digest(start_manifest),
                resolved_manifest=digest(resolved_manifest), start_lock=digest(old_lock),
                resolved_lock=digest(new_lock),
                resolution_log=digest(files["rust/" + resolution.ARTIFACTS + "/logs/golden-run1-attempt1.log"]))
    monkeypatch.setattr(resolution, "PINS", pins)
    inputs = {
        "start": {resolution.MANIFEST: start_manifest, "Cargo.lock": old_lock},
        "golden": {resolution.MANIFEST: resolved_manifest, "Cargo.lock": old_lock},
    }
    monkeypatch.setattr(rust_overlay, "reference_inputs", lambda *args: copy.deepcopy(inputs))
    evidence = [{"path": resolution.SESSION, "sha256": pins["session"]}]
    log = resolution.ARTIFACTS + "/logs/golden-run1-attempt1.log"
    oracle = {"golden_patch_sha256": pins["patch"], "pass_to_pass_tests": [resolution.TEST_NAME],
              "session_files": {"start": evidence, "golden": evidence},
              "runs": {"golden": [{"log": log, "ok": True, "passed": 1, "failed": 0,
                                    "passed_tests": [resolution.TEST_NAME], "failed_tests": [],
                                    "attempts": [{"exit_code": 0, "timed_out": False, "log": log}]}]}}
    task = {"id": resolution.TASK_ID, "repo": "alpenlabs/alpen", "start_sha": pins["start_sha"]}
    def copy_asset(path, category):
        target = tmp_path / "copied.lock"
        shutil.copyfile(path, target)
        return {"path": str(target), "sha256": digest(target.read_bytes())}
    ctx = SimpleNamespace(legacy=legacy, output=tmp_path / "output", copy_asset=copy_asset)
    return ctx, task, plan, oracle, {"path": str(patch), "sha256": pins["patch"]}, inputs


def test_recorded_resolution_requires_the_original_run_patch_session_and_exact_delta(tmp_path, monkeypatch):
    from agentic_runtime_migration import rust_resolution as resolution
    ctx, task, plan, oracle, patch, inputs = recorded_fixture(tmp_path, monkeypatch)
    spec, defaults = {"locks": [], "manifests": []}, {}
    resolution.add_recorded_resolution(ctx, task, plan, oracle, patch, spec, defaults)
    assert list(defaults) == ["Cargo.lock"]
    record = spec["locks"][0]
    assert record["when"] == [{"path": resolution.MANIFEST, "key": ["dependencies", "bytemuck"],
                               "value": {"version": "1.21.0", "optional": True}}]
    assert record["allow_missing"] is False and record["sha256"] == resolution.PINS["resolved_lock"]
    assert digest(plan["session_files"][0]["content"].encode()) == resolution.PINS["session"]
    audit = json.loads(next((ctx.output / "rust-resolution-audits").glob("*.json")).read_text())
    assert audit["oracle_declares_lock_sha256"] is False
    assert not audit["production_changes"] and not audit["test_changes"]
    assert len(audit["additional_registry_packages"]) == 3


@pytest.mark.parametrize("failure", ["patch", "log", "lock", "plan", "session", "run", "reference_lock", "manifest"])
def test_recorded_resolution_rejects_changed_or_inconsistent_historical_inputs(tmp_path, monkeypatch, failure):
    from agentic_runtime_migration import rust_resolution as resolution
    ctx, task, plan, oracle, patch, inputs = recorded_fixture(tmp_path, monkeypatch)
    if failure == "patch":
        Path(patch["path"]).write_text("changed reference")
    elif failure in {"log", "lock", "plan"}:
        path = {"log": "rust/" + resolution.ARTIFACTS + "/logs/golden-run1-attempt1.log",
                "lock": "rust/" + resolution.ARTIFACTS + "/golden/Cargo.lock",
                "plan": "rust/plans.d/18.json"}[failure]
        (ctx.legacy / path).write_text("changed retained evidence")
    elif failure == "session":
        plan["session_files"][0]["content"] += "// different frozen input"
    elif failure == "run":
        oracle["runs"]["golden"][0]["attempts"][0]["exit_code"] = 101
    elif failure == "reference_lock":
        inputs["golden"]["Cargo.lock"] += b"# candidate reference lock changed\n"
    else:
        inputs["golden"][resolution.MANIFEST] = b"[dependencies]\n"
    spec, defaults = {"locks": [], "manifests": []}, {}
    with pytest.raises(ContractError):
        resolution.add_recorded_resolution(ctx, task, plan, oracle, patch, spec, defaults)
    assert spec["locks"] == [] and defaults == {}
    assert not (tmp_path / "copied.lock").exists()


def test_recorded_resolution_never_adds_packages_not_named_in_the_frozen_run(tmp_path):
    from agentic_runtime_migration import rust_resolution as resolution
    before = {"package": [{"name": n, "version": v, "source": resolution.CRATES_IO, "checksum": "a" * 64}
                          for n, v in resolution.REMOVED]}
    after = {"package": [{"name": n, "version": v, "source": resolution.CRATES_IO, "checksum": sha}
                         for (n, v), sha in resolution.ADDED.items()]}
    assert len(resolution.validate_resolution_delta(before, after)) == 3
    after["package"][0]["checksum"] = "b" * 64
    with pytest.raises(ContractError, match="recorded external package delta"):
        resolution.validate_resolution_delta(before, after)


def test_recorded_resolution_does_not_affect_other_rust_tasks():
    from agentic_runtime_migration.rust_resolution import add_recorded_resolution
    spec, defaults = {"locks": []}, {}
    add_recorded_resolution(None, {"id": "other-task"}, {}, {}, {}, spec, defaults)
    assert spec == {"locks": []} and defaults == {}


def test_rust_lane_passes_conditional_lock_to_vendor_union_without_installing_it_on_start(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from agentic_runtime_migration import rust_lane, rust_vendor
    source, asset, original, spec = lock_fixture(tmp_path)
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as stream:
        for name in ("Cargo.toml", "Cargo.lock"):
            data = (source / name).read_bytes()
            info = tarfile.TarInfo(name); info.size = len(data)
            stream.addfile(info, io.BytesIO(data))
    @contextmanager
    def start_archive(*args):
        yield io.BytesIO(archive.getvalue())
    monkeypatch.setattr(rust_lane, "start_archive", start_archive)
    observed = {}
    def vendor(*args, **kwargs):
        observed.update(kwargs)
        assert (args[3] / "Cargo.lock").read_bytes() == original
        return {"path": "vendor", "sha256": "v"}, {"path": "config", "sha256": "c"}
    monkeypatch.setattr(rust_vendor, "vendor_by_source", vendor)
    output = tmp_path / "output"; output.mkdir()
    scratch = tmp_path / "scratch"; scratch.mkdir()
    registry = tmp_path / "toolchains.json"
    registry.write_text(json.dumps({"fixture": {"root": str(tmp_path / "tool")}}))
    ctx = SimpleNamespace(config={"rust_toolchains": str(registry)}, output=output, scratch=scratch,
                          repo=lambda _: tmp_path)
    task, plan = {"id": "fixture", "start_sha": "start"}, {
        "input_index": 1, "toolchain": "fixture", "command": "cargo test --locked"}
    locks = {"Cargo.lock": {"path": str(asset), "sha256": digest(asset.read_bytes())}}
    rust_lane.cargo_vendor(ctx, task, plan, "", locks, "Cargo.lock", build_spec=spec, oracle={})
    assert observed["extra_locks"] == [tomllib.loads(asset.read_text())]


@pytest.fixture
def cargo_tool():
    path = os.environ.get("REVIEWBENCH_TEST_CARGO") or shutil.which("cargo")
    if not path:
        pytest.skip("preinstalled Cargo required for offline metadata")
    result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=30,
                            env=dict(os.environ, RUSTUP_AUTO_INSTALL="0"))
    if result.returncode:
        pytest.skip("no installed Cargo toolchain: " + result.stderr)
    return Path(path)


def test_real_cargo_vendor_union_contains_both_resolutions_and_keeps_start_lock(tmp_path, cargo_tool):
    from agentic_runtime_migration import rust_vendor
    cache = tmp_path / "cargo"
    archives = cache / "registry/cache/fixture"; archives.mkdir(parents=True)
    packages = []
    for version in ("1.0.0", "2.0.0"):
        archive = archives / ("sample-lib-" + version + ".crate")
        with tarfile.open(archive, "w:gz") as stream:
            for name, data in [("Cargo.toml", ('[package]\nname="sample-lib"\nversion="' + version + '"\n').encode()),
                               ("src/lib.rs", b"pub fn value() -> u8 { 1 }\n")]:
                info = tarfile.TarInfo("sample-lib-" + version + "/" + name); info.size = len(data)
                stream.addfile(info, io.BytesIO(data))
        packages.append({"name": "sample-lib", "version": version, "source": rust_vendor.CRATES_IO,
                         "checksum": digest(archive.read_bytes())})
    provider = lambda name, version, sha: archives / (name + "-" + version + ".crate")
    seed = tmp_path / "seed"
    config, _ = rust_vendor.materialize([{"package": packages[:1]}], {"packages": []}, cache, seed, provider)
    workspace = tmp_path / "workspace"; (workspace / "src").mkdir(parents=True)
    (workspace / ".cargo").mkdir()
    (workspace / ".cargo/config.toml").write_text(config.replace(rust_vendor.TARGET, str(seed)))
    (workspace / "Cargo.toml").write_text(
        '[package]\nname="consumer"\nversion="0.1.0"\n[dependencies]\nsample-lib="=1.0.0"\n')
    (workspace / "src/lib.rs").write_text("pub fn value()->u8{sample_lib::value()}\n")
    lock = {"version": 3, "package": [
        {"name": "consumer", "version": "0.1.0", "dependencies": ["sample-lib"]}, packages[0]]}
    (workspace / "Cargo.lock").write_text(tomli_w.dumps(lock))
    before = (workspace / "Cargo.lock").read_bytes()
    output = tmp_path / "output"; (output / "cargo-vendor-logs").mkdir(parents=True)
    registry = tmp_path / "toolchains.json"
    registry.write_text(json.dumps({"fixture": {"root": str(cargo_tool.parent.parent)}}))
    observed = {}
    def bundle(root):
        observed["versions"] = sorted(p["package"]["version"] for p in
                                      (tomllib.loads(f.read_text()) for f in root.glob("*/sample-lib-*/Cargo.toml")))
        return {"path": str(root), "sha256": "v"}
    ctx = SimpleNamespace(config={"rust_toolchains": str(registry)}, output=output, scratch=tmp_path,
                          bundle=bundle, copy_asset=lambda p, _: {"path": str(p), "sha256": digest(p.read_bytes())})
    env = dict(os.environ, CARGO_HOME=str(cache), CARGO_NET_OFFLINE="true", RUSTUP_AUTO_INSTALL="0",
               RUSTC=str(cargo_tool.parent / "rustc"))
    rust_vendor.vendor_by_source(ctx, {"id": "fixture", "start_sha": "start"},
                                 {"input_index": 1, "toolchain": "fixture"}, workspace, "Cargo.lock",
                                 [workspace / "Cargo.toml"], cargo_tool, env, normalize=False,
                                 extra_locks=[{"package": packages[1:]}])
    assert observed["versions"] == ["1.0.0", "2.0.0"]
    assert (workspace / "Cargo.lock").read_bytes() == before
    audit = json.loads((output / "cargo-vendor-logs/start-1-sources.json").read_text())
    assert audit["additional_locked_packages"] == [
        {"name": "sample-lib", "version": "2.0.0", "source": rust_vendor.CRATES_IO}]
    assert all(command["exit_status"] == 0 for command in audit["commands"])
