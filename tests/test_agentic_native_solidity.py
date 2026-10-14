import copy
import hashlib
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import native_solidity as subject


def fixture(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    incoming = tmp_path / "inputs"
    incoming.mkdir()
    workspace.mkdir()
    files = copy.deepcopy(subject.FILES)
    for name, row in files.items():
        body = ("exact dependency " + name).encode()
        declaration = ("original CMake declaration " + name).encode()
        (incoming / name).write_bytes(body)
        path = workspace / row["declaration"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(declaration)
        row["sha256"] = hashlib.sha256(body).hexdigest()
        row["declaration_sha256"] = hashlib.sha256(declaration).hexdigest()
    monkeypatch.setattr(subject, "FILES", files)
    return workspace, incoming, {"schema_version": subject.SCHEMA, "source": str(incoming)}


def test_exact_downloads_seeded_without_changing_declarations_or_inputs(tmp_path, monkeypatch):
    workspace, incoming, spec = fixture(tmp_path, monkeypatch)
    before = {p.name: p.read_bytes() for p in incoming.iterdir()}
    declarations = {name: (workspace / r["declaration"]).read_bytes() for name, r in subject.FILES.items()}
    assert len(subject.seed(spec, workspace)) == 3
    for name, row in subject.FILES.items():
        assert (workspace / row["destination"]).read_bytes() == before[name]
        assert (workspace / row["declaration"]).read_bytes() == declarations[name]
        assert (incoming / name).read_bytes() == before[name]
    assert len(subject.seed(spec, workspace)) == 3


def test_different_existing_candidate_download_is_not_overwritten(tmp_path, monkeypatch):
    workspace, incoming, spec = fixture(tmp_path, monkeypatch)
    path = workspace / subject.FILES["json.hpp"]["destination"]
    path.parent.mkdir(parents=True)
    path.write_text("candidate-owned different content")
    with pytest.raises(ValueError, match="candidate-owned"):
        subject.seed(spec, workspace)
    assert path.read_text() == "candidate-owned different content"
    assert not (workspace / subject.FILES["fmt-9.1.0.tar.gz"]["destination"]).exists()


def test_changed_dependency_declaration_rejected_before_any_copy(tmp_path, monkeypatch):
    workspace, incoming, spec = fixture(tmp_path, monkeypatch)
    (workspace / subject.FILES["json.hpp"]["declaration"]).write_text("changed pin")
    with pytest.raises(ValueError, match="declaration changed"):
        subject.seed(spec, workspace)
    assert not (workspace / "deps").exists()


def test_corrupt_download_rejected_before_any_copy(tmp_path, monkeypatch):
    workspace, incoming, spec = fixture(tmp_path, monkeypatch)
    (incoming / "json.hpp").write_text("corrupt")
    with pytest.raises(ValueError, match="checksum differs"):
        subject.seed(spec, workspace)
    assert not (workspace / "deps").exists()


def test_linked_destination_rejected(tmp_path, monkeypatch):
    workspace, incoming, spec = fixture(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "deps").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="destination is linked"):
        subject.seed(spec, workspace)
    assert not list(outside.iterdir())


def test_linked_declaration_rejected(tmp_path, monkeypatch):
    workspace, incoming, spec = fixture(tmp_path, monkeypatch)
    path = workspace / subject.FILES["json.hpp"]["declaration"]
    outside = tmp_path / "declaration"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(ValueError, match="declaration changed"):
        subject.seed(spec, workspace)


@pytest.mark.parametrize("spec", [{}, {"schema_version": "unknown", "source": "/source"}])
def test_invalid_spec_rejected(tmp_path, spec):
    with pytest.raises(ValueError):
        subject.seed(spec, tmp_path)


def test_other_native_task_does_not_use_solidity_profile():
    assert subject.adapt(None, {"repo": "other"}, {"input_index": 33}) is None
