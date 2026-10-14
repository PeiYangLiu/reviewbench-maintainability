"""Fixed native-library inputs, not version substitution or a skipped V8 build."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_v8 as v8
from reviewbench_maintainability.agentic.contracts import ContractError


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    source = tmp_path / "original.a"
    source.write_bytes(b"!<arch>\n" + b"explicit local unit-fixture payload")
    monkeypatch.setattr(v8, "SHA256", digest(source))
    monkeypatch.setattr(v8, "SIZE", source.stat().st_size)
    output, scratch = tmp_path / "output", tmp_path / "scratch"
    output.mkdir()
    scratch.mkdir()
    captured = []
    def bundle(root, *, role):
        captured.append({
            "role": role,
            "files": {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()},
            "mode": (root / v8.NAME).stat().st_mode & 0o777,
        })
        descriptor = output / "descriptor.json"
        descriptor.write_text('{"fixture":true}\n')
        return {"path": str(descriptor), "sha256": digest(descriptor)}
    ctx = SimpleNamespace(config={"rust_v8_archive": str(source)}, output=output,
                          scratch=scratch, bundle=bundle)
    task = {"id": v8.TASK, "repo": v8.REPO, "start_sha": v8.START}
    return SimpleNamespace(ctx=ctx, task=task, source=source, captured=captured)


def test_other_tasks_do_not_read_an_archive():
    assert v8.binding(SimpleNamespace(), {"id": "unrelated-task"}) == ([], {}, [])


@pytest.mark.parametrize("field", ["repo", "start_sha"])
def test_original_source_identity_is_fixed(fixture, field):
    task = dict(fixture.task, **{field: "different"})
    with pytest.raises(ContractError, match="binding changed"):
        v8.binding(fixture.ctx, task)
    assert fixture.captured == []


@pytest.mark.parametrize("value", [None, "", "relative.a", {"url": "https://example.invalid"}])
def test_only_local_absolute_path_relocation(fixture, value):
    fixture.ctx.config["rust_v8_archive"] = value
    with pytest.raises(ContractError, match="absolute local path"):
        v8.binding(fixture.ctx, fixture.task)
    assert fixture.captured == []


def test_missing_input_does_not_download(fixture):
    fixture.source.unlink()
    with pytest.raises(ContractError, match="absent or has changed"):
        v8.binding(fixture.ctx, fixture.task)


def test_wrong_bytes_are_rejected_even_at_same_size(fixture):
    data = fixture.source.read_bytes()
    fixture.source.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    with pytest.raises(ContractError, match="has changed"):
        v8.binding(fixture.ctx, fixture.task)


def test_wrong_size_is_rejected(fixture):
    fixture.source.write_bytes(fixture.source.read_bytes() + b"x")
    with pytest.raises(ContractError, match="has changed"):
        v8.binding(fixture.ctx, fixture.task)


def test_linked_input_is_rejected(fixture, tmp_path):
    linked = tmp_path / "linked.a"
    linked.symlink_to(fixture.source)
    fixture.ctx.config["rust_v8_archive"] = str(linked)
    with pytest.raises(ContractError, match="has changed"):
        v8.binding(fixture.ctx, fixture.task)


def test_thin_or_non_archive_input_is_rejected(fixture, monkeypatch):
    fixture.source.write_bytes(b"!<thin>\n" + fixture.source.read_bytes()[8:])
    monkeypatch.setattr(v8, "SHA256", digest(fixture.source))
    with pytest.raises(ContractError, match="self-contained static archive"):
        v8.binding(fixture.ctx, fixture.task)


def test_exact_offline_asset_and_only_existing_build_script_override(fixture):
    before = fixture.source.read_bytes()
    assets, environment, bootstrap = v8.binding(fixture.ctx, fixture.task)
    location = v8.TARGET + "/" + v8.NAME
    assert len(assets) == len(bootstrap) == 1
    assert assets[0]["kind"] == "archive" and assets[0]["visibility"] == "developer"
    assert assets[0]["target"] == v8.TARGET
    assert environment == {"RUSTY_V8_ARCHIVE": location}
    assert not location.startswith(("https:", "http:", "file:"))
    assert v8.SHA256 in bootstrap[0] and "sha256sum -c -" in bootstrap[0]
    assert not {"DOCS_RS", "DENO_TRYBUILD", "V8_FROM_SOURCE", "RUSTC_BOOTSTRAP"} & environment.keys()
    assert len(fixture.captured) == 1 and fixture.captured[0]["role"] == "toolchain"
    assert set(fixture.captured[0]["files"]) == {v8.NAME, "pins.json"}
    assert fixture.captured[0]["files"][v8.NAME] == before
    assert fixture.captured[0]["mode"] == 0o444
    pins = json.loads(fixture.captured[0]["files"]["pins.json"])
    assert pins["original_url"] == v8.URL and pins["version"] == v8.VERSION
    assert pins["target"] == "x86_64-unknown-linux-gnu" and pins["profile"] == "release"
    assert fixture.source.read_bytes() == before


def test_corrupt_staging_copy_is_rejected(fixture, monkeypatch):
    def corrupt(source, target):
        target.write_bytes(b"corrupt")
    monkeypatch.setattr(v8.shutil, "copyfile", corrupt)
    with pytest.raises(ContractError, match="staged original V8"):
        v8.binding(fixture.ctx, fixture.task)
    assert fixture.captured == []


def test_input_is_rechecked_after_bundling(fixture):
    original = fixture.ctx.bundle
    def mutate(root, *, role):
        value = original(root, role=role)
        fixture.source.write_bytes(b"changed by a concurrent writer")
        return value
    fixture.ctx.bundle = mutate
    with pytest.raises(ContractError, match="has changed"):
        v8.binding(fixture.ctx, fixture.task)


def test_relocation_keeps_identity(fixture, tmp_path):
    relocated = tmp_path / "relocated" / v8.NAME
    relocated.parent.mkdir()
    relocated.write_bytes(fixture.source.read_bytes())
    fixture.ctx.config["rust_v8_archive"] = str(relocated)
    assert v8.binding(fixture.ctx, fixture.task)[1] == {
        "RUSTY_V8_ARCHIVE": v8.TARGET + "/" + v8.NAME}


def test_real_original_release_library_is_hash_bound():
    configured = os.environ.get("V8_ORIGINAL_ARCHIVE")
    assert configured, "supply the downloaded, verified original v0.42.1 library"
    source = Path(configured)
    ctx = SimpleNamespace(config={"rust_v8_archive": str(source)})
    assert v8.checked_input(ctx) == source
    assert source.stat().st_size == 78632406
    assert digest(source) == "ac0a88909f39979792a234e8c15665e7068afdf934d092387c8d40635b9efe2e"
    process = subprocess.run(["ar", "t", str(source)], capture_output=True, text=True, timeout=30)
    assert process.returncode == 0 and len(process.stdout.splitlines()) == 1483
