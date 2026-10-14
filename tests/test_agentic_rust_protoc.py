import hashlib
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_protoc
from reviewbench_maintainability.agentic.contracts import ContractError


def fixture(tmp_path, monkeypatch, *, version="3.19.2", headers=None):
    binary, archive = tmp_path / "cached-protoc", tmp_path / "headers.jar"
    binary.write_text("#!/bin/sh\nprintf 'libprotoc " + version + "\\n'\n")
    with zipfile.ZipFile(archive, "w") as stream:
        for name in rust_protoc.HEADER_NAMES if headers is None else headers:
            stream.writestr(name, 'syntax="proto3";\n')
        stream.writestr("com/google/protobuf/NotATool.class", b"not a runtime asset")
    # Small fixtures exercise the same fixed-pin checks as the actual cached
    # 3.19.2 executable. Integration evidence separately runs that executable.
    pins = {key: {"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for key, path in [("binary", binary), ("headers", archive)]}
    monkeypatch.setattr(rust_protoc, "PINS", pins)

    def bundle(root, *, role):
        assert role == "toolchain"
        shutil.copytree(root, tmp_path / "bundle")
        return {"path": str(tmp_path / "bundle"), "sha256": "a" * 64}

    ctx = SimpleNamespace(
        legacy=tmp_path, config={"rust_protoc_assets": {"binary": str(binary), "headers": str(archive)}},
        output=tmp_path / "output", scratch=tmp_path, bundle=bundle,
    )
    return ctx, binary, archive


def test_protoc_fixed_asset_contains_executable_and_only_matched_standard_headers(tmp_path, monkeypatch):
    ctx, binary, archive = fixture(tmp_path, monkeypatch)
    original_mode = binary.stat().st_mode
    asset = rust_protoc.build_asset(ctx)
    root = Path(asset["path"])
    assert (root / "bin/protoc").stat().st_mode & 0o111
    assert binary.stat().st_mode == original_mode
    assert not list(root.rglob("*.class"))
    assert {p.relative_to(root / "include").as_posix() for p in root.rglob("*.proto")} == rust_protoc.HEADER_NAMES
    probe = json.loads((ctx.output / "rust-protoc/probe-3.19.2.json").read_text())
    assert probe["stdout"] == "libprotoc 3.19.2\n"
    assert probe["stderr"] == "" and probe["exit_status"] == 0
    assert probe["scoring_result"] is False
    assert json.loads((root / "pins.json").read_text())["artifacts"] == rust_protoc.PINS


@pytest.mark.parametrize("key", ["binary", "headers"])
def test_protoc_changed_or_missing_input_is_not_replaced_from_network(tmp_path, monkeypatch, key):
    ctx, binary, archive = fixture(tmp_path, monkeypatch)
    source = binary if key == "binary" else archive
    source.write_bytes(b"unattested bytes")
    with pytest.raises(ContractError, match="absent or has changed"):
        rust_protoc.build_asset(ctx)
    source.unlink()
    with pytest.raises(ContractError, match="absent or has changed"):
        rust_protoc.build_asset(ctx)
    assert not (tmp_path / "bundle").exists()


def test_protoc_relocation_cannot_select_another_version_or_digest(tmp_path, monkeypatch):
    ctx, _, _ = fixture(tmp_path, monkeypatch)
    ctx.config["rust_protoc_assets"]["version"] = "latest"
    with pytest.raises(ContractError, match="only binary/headers paths"):
        rust_protoc.build_asset(ctx)


def test_protoc_version_probe_is_required_even_when_hashes_match(tmp_path, monkeypatch):
    ctx, _, _ = fixture(tmp_path, monkeypatch, version="99.0")
    with pytest.raises(ContractError, match="version probe"):
        rust_protoc.build_asset(ctx)
    assert not (tmp_path / "bundle").exists()
    probe = json.loads((ctx.output / "rust-protoc/probe-3.19.2.json").read_text())
    assert probe["stdout"] == "libprotoc 99.0\n" and probe["exit_status"] == 0


@pytest.mark.parametrize("headers", [
    {"google/protobuf/timestamp.proto"},
    set(rust_protoc.HEADER_NAMES) | {"google/protobuf/../../escape.proto"},
])
def test_protoc_header_set_is_closed_and_cannot_escape(tmp_path, monkeypatch, headers):
    ctx, _, _ = fixture(tmp_path, monkeypatch, headers=headers)
    with pytest.raises(ContractError, match="unexpected include set"):
        rust_protoc.build_asset(ctx)
    assert not (tmp_path / "escape.proto").exists()


def test_protoc_binding_does_not_touch_unrelated_rust_tasks(tmp_path, monkeypatch):
    monkeypatch.setattr(rust_protoc, "build_asset", lambda _: pytest.fail("unrelated task read protoc assets"))
    assert rust_protoc.binding(SimpleNamespace(), {"id": "unrelated-rust-task"}) == ([], {}, [])


def test_protoc_binding_is_public_and_shared_with_grading(tmp_path, monkeypatch):
    ctx, _, _ = fixture(tmp_path, monkeypatch)
    assets, env, bootstrap = rust_protoc.binding(ctx, {"id": rust_protoc.VECTOR_TASK})
    assert assets[0]["visibility"] == "developer" and assets[0]["kind"] == "archive"
    assert env == {"PROTOC": rust_protoc.TARGET + "/bin/protoc", "PROTOC_INCLUDE": rust_protoc.TARGET + "/include"}
    assert bootstrap == ['test "$(/opt/reviewbench-assets/protoc/bin/protoc --version)" = \'libprotoc 3.19.2\'']
    assert not any(str(tmp_path) in value for value in env.values())


def test_chroma_original_task_gets_same_fixed_readonly_compiler_and_headers(tmp_path, monkeypatch):
    ctx, _, _ = fixture(tmp_path, monkeypatch)
    task = {"id": rust_protoc.CHROMA_TASK, "repo": "chroma-core/chroma",
            "start_sha": "db611257f776fd05be2b1068d80feabeaa2b9381"}
    assets, env, bootstrap = rust_protoc.binding(ctx, task)
    assert len(assets) == 1 and assets[0]["visibility"] == "developer"
    assert env == {"PROTOC": rust_protoc.TARGET + "/bin/protoc",
                   "PROTOC_INCLUDE": rust_protoc.TARGET + "/include"}
    assert len(bootstrap) == 1 and "libprotoc 3.19.2" in bootstrap[0]


@pytest.mark.parametrize("field,value", [("repo", "another/repo"), ("start_sha", "0" * 40)])
def test_chroma_pin_cannot_be_reinterpreted_as_another_input(tmp_path, monkeypatch, field, value):
    monkeypatch.setattr(rust_protoc, "build_asset", lambda _: pytest.fail("foreign input read assets"))
    task = {"id": rust_protoc.CHROMA_TASK, "repo": "chroma-core/chroma",
            "start_sha": "db611257f776fd05be2b1068d80feabeaa2b9381"}
    task[field] = value
    with pytest.raises(ContractError, match="original task/start"):
        rust_protoc.binding(SimpleNamespace(), task)
