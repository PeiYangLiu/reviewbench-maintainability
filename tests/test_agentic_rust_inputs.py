from pathlib import Path
from types import SimpleNamespace
import hashlib
import io
import json
import sys
import tarfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration.rust_lane import (
    load_plan, unpack_crate, missing_lock_assets, cargo_vendor, restore_git_build_scripts,
)
from reviewbench_maintainability.agentic.contracts import ContractError


def test_rust_plan_uses_last_record_and_validates_frozen_toolchain(tmp_path):
    root = tmp_path / "rust"
    root.mkdir()
    rows = [{"input_index": 21, "toolchain": "1.82.0", "command": "cargo +1.82.0 test"},
            {"input_index": 21, "toolchain": "1.85.0", "command": "cargo +1.85.0 test --locked"}]
    (root / "plans.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
    oracle = {"input_index": 21, "environment": {"toolchain": "1.85.0",
              "variant_commands": {"start": "cargo +1.85.0 test --locked"}}}
    ctx = SimpleNamespace(legacy=tmp_path)
    assert load_plan(ctx, oracle) == rows[-1]
    oracle["environment"]["toolchain"] = "1.82.0"
    with pytest.raises(ContractError, match="toolchain differs"):
        load_plan(ctx, oracle)


def test_rust_plan_reference_preserves_inline_override_and_checks_hash(tmp_path):
    root = tmp_path / "rust"
    root.mkdir()
    payload = root / "plan.json"
    payload.write_text(json.dumps({"input_index": 1, "toolchain": "1.85.0", "command": "cargo test", "timeout_seconds": 10}))
    row = {"input_index": 1, "plan_file": "plan.json", "plan_file_sha256": hashlib.sha256(payload.read_bytes()).hexdigest(),
           "timeout_seconds": 90}
    (root / "plans.jsonl").write_text(json.dumps(row))
    ctx = SimpleNamespace(legacy=tmp_path)
    assert load_plan(ctx, {"input_index": 1})["timeout_seconds"] == 90
    payload.write_text("{}")
    with pytest.raises(ContractError, match="changed"):
        load_plan(ctx, {"input_index": 1})


def make_archive(path, entries):
    with tarfile.open(path, "w:gz") as stream:
        for name, content in entries:
            member = tarfile.TarInfo(name)
            member.size = len(content)
            stream.addfile(member, io.BytesIO(content))


def test_registry_crate_omits_git_metadata_without_changing_production_bytes(tmp_path):
    source = tmp_path / "dependency.crate"
    contents = b"pub fn preserved() -> u8 { 1 }\n"
    make_archive(source, [("dep-1.0/src/lib.rs", contents), ("dep-1.0/papers/.git/HEAD", b"old vcs data"),
                          ("dep-1.0/.gitignore", b"target/\n")])
    before = source.read_bytes()
    omitted = unpack_crate(source, tmp_path / "out", "dep-1.0")
    assert omitted == ["dep-1.0/papers/.git/HEAD"]
    assert (tmp_path / "out/dep-1.0/src/lib.rs").read_bytes() == contents
    assert (tmp_path / "out/dep-1.0/.gitignore").read_bytes() == b"target/\n"
    assert not (tmp_path / "out/dep-1.0/papers/.git").exists()
    assert source.read_bytes() == before


@pytest.mark.parametrize("name", ["../escape", "/absolute", "dep-1.0/.git/../../../../escape", "other-1.0/file"])
def test_crate_metadata_omission_does_not_bypass_archive_boundaries(tmp_path, name):
    source = tmp_path / "bad.crate"
    make_archive(source, [(name, b"bytes")])
    with pytest.raises(ContractError):
        unpack_crate(source, tmp_path / "out", "dep-1.0")
    assert not (tmp_path / "out").exists()


def test_lock_injection_requires_same_frozen_start_and_golden_hash(tmp_path, monkeypatch):
    from agentic_runtime_migration import rust_lane
    source = tmp_path / "rust/session/lock"
    source.parent.mkdir(parents=True)
    source.write_text("version = 3\n")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setattr(rust_lane, "git", lambda *a: (_ for _ in ()).throw(ContractError("missing path")))
    ctx = SimpleNamespace(legacy=tmp_path, repo=lambda _: tmp_path,
                          copy_asset=lambda p, _: {"path": str(p), "sha256": digest})
    plan = {"environment_file_replacements": [{"path": "Cargo.lock", "source": "session/lock",
             "source_sha256": digest, "allow_missing": True}]}
    evidence = [{"path": "Cargo.lock", "sha256": digest}]
    oracle = {"environment_replacements": {"start": evidence, "golden": evidence}}
    assert "Cargo.lock" in missing_lock_assets(ctx, {"start_sha": "start"}, plan, oracle)
    oracle["environment_replacements"]["golden"] = [{"path": "Cargo.lock", "sha256": "other"}]
    with pytest.raises(ContractError, match="frozen"):
        missing_lock_assets(ctx, {"start_sha": "start"}, plan, oracle)


@pytest.mark.parametrize("change_lock", [False, True])
def test_pinned_cargo_vendor_keeps_lock_and_emits_portable_source_configuration(tmp_path, monkeypatch, change_lock):
    from contextlib import contextmanager
    from agentic_runtime_migration import rust_lane
    tool = tmp_path/"tool"
    (tool/"bin").mkdir(parents=True)
    cargo = tool/"bin/cargo"
    cargo.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys
assert '--locked' in sys.argv
assert os.environ['GIT_CONFIG_VALUE_0'] == '2'
root = pathlib.Path(sys.argv[-1])
(root/'dep').mkdir(parents=True)
(root/'dep/lib.rs').write_text('pub fn preserved() {}')
(root/'dep/.cargo-checksum.json').write_text(json.dumps({'package':None,'files':{'lib.rs':'hash','docs/.git/HEAD':'vcs'}}))
print('[source.vendored-sources]\\ndirectory = ' + json.dumps(str(root)))
""" + ("pathlib.Path('Cargo.lock').write_text('changed')\n" if change_lock else ""))
    cargo.chmod(0o755)
    registry = tmp_path/"toolchains.json"
    registry.write_text(json.dumps({"1.85.0": {"root": str(tool)}}))
    archive_data = io.BytesIO()
    with tarfile.open(fileobj=archive_data, mode="w") as stream:
        for name, content in [("Cargo.toml", b"[workspace]\n"), ("Cargo.lock", b"version = 3\n")]:
            member = tarfile.TarInfo(name)
            member.size = len(content)
            stream.addfile(member, io.BytesIO(content))
    @contextmanager
    def source(*args):
        yield io.BytesIO(archive_data.getvalue())
    monkeypatch.setattr(rust_lane, "start_archive", source)
    monkeypatch.setattr(rust_lane.shutil, "disk_usage", lambda _: SimpleNamespace(free=100*1024**3))
    output, scratch = tmp_path/"output", tmp_path/"scratch"
    output.mkdir()
    scratch.mkdir()
    observed = {}
    def bundle(path):
        observed.update(json.loads((path/"dep/.cargo-checksum.json").read_text()))
        assert (path/"dep/lib.rs").read_text() == "pub fn preserved() {}"
        return {"path": "bundle", "sha256": "digest"}
    ctx = SimpleNamespace(config={"rust_toolchains": str(registry)}, output=output, scratch=scratch,
                          repo=lambda _: tmp_path, bundle=bundle,
                          copy_asset=lambda path, _: {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    task, plan = {"id": "task", "start_sha": "start"}, {"input_index": 1, "toolchain": "1.85.0"}
    if change_lock:
        with pytest.raises(ContractError, match="changed the frozen"):
            cargo_vendor(ctx, task, plan, "", {}, "Cargo.lock")
    else:
        archive, config = cargo_vendor(ctx, task, plan, "", {}, "Cargo.lock")
        assert archive["path"] == "bundle"
        assert observed["files"] == {"lib.rs": "hash"}
        assert "/opt/reviewbench-assets/vendor" in Path(config["path"]).read_text()


def test_git_build_script_repair_uses_locked_blob_not_mutable_checkout(tmp_path):
    import subprocess
    cache = tmp_path/"cargo"
    checkout = cache/"git/checkouts/dependency/commit"
    package = checkout/"algebra"
    package.mkdir(parents=True)
    (package/"Cargo.toml").write_text('[package]\nname="algebra"\nversion="0.3.1"\nbuild="build.rs"\n')
    original = b"fn main() { println!(\"cargo:rerun-if-changed=src/lib.rs\"); }\n"
    (package/"build.rs").write_bytes(original)
    def git(*args):
        return subprocess.check_output(["git", "-C", str(checkout), *args]).decode().strip()
    git("init", "-q")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    git("add", ".")
    git("commit", "-qm", "locked dependency")
    commit = git("rev-parse", "HEAD")
    (package/"build.rs").write_text("untrusted mutable checkout bytes")
    vendor = tmp_path/"vendor"
    target = vendor/"algebra-0.3.1"
    target.mkdir(parents=True)
    (target/"Cargo.toml").write_bytes((package/"Cargo.toml").read_bytes())
    (target/".cargo-checksum.json").write_text(json.dumps({"package": None, "files": {}}))
    source = "git+https://example.test/dep#" + commit
    metadata = {"packages": [{"name": "algebra", "version": "0.3.1", "source": source,
                              "manifest_path": str(package/"Cargo.toml")}]}
    lock = {"package": [{"name": "algebra", "version": "0.3.1", "source": source}]}
    audit = restore_git_build_scripts(vendor, metadata, lock, cache)
    assert (target/"build.rs").read_bytes() == original
    assert (package/"build.rs").read_text() == "untrusted mutable checkout bytes"
    assert audit[0]["commit"] == commit
    checksums = json.loads((target/".cargo-checksum.json").read_text())
    assert checksums["package"] is None
    assert checksums["files"]["build.rs"] == hashlib.sha256(original).hexdigest()


def test_git_build_script_repair_rejects_unbound_package(tmp_path):
    target = tmp_path/"vendor/dep"
    target.mkdir(parents=True)
    (target/"Cargo.toml").write_text('[package]\nname="dep"\nversion="1.0"\nbuild="build.rs"\n')
    (target/".cargo-checksum.json").write_text('{"package":null,"files":{}}')
    with pytest.raises(ContractError, match="ambiguous/unbound"):
        restore_git_build_scripts(tmp_path/"vendor", {"packages": []}, {"package": []}, tmp_path/"cargo")
