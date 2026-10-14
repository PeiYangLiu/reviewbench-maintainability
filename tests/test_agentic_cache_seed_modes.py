"""Real frozen archive transport, original artifact permissions and isolated restores."""
import io
import json
from pathlib import Path
import stat
import sys
import tarfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import cache_seed_entry as entry, public_build_cache as host
from reviewbench_maintainability.agentic.snapshots import extract_tar, tree_files
from test_agentic_public_build_cache import fixture, recipe


def transported(tmp_path, mode):
    target, seed, value, metadata = fixture(tmp_path)
    name = value["files"][0]["path"]
    (target / name).chmod(mode)
    raw = tmp_path / "raw-seed"
    value = host.capture(target, metadata, raw, "a" * 64, producer_receipt={
        "producer": "clean-start-public-check", "grader_assets_visible": False, "public_check_exit_status": 0})
    body = io.BytesIO()
    with tarfile.open(fileobj=body, mode="w") as stream:
        for path in sorted(tree_files(raw)):
            stream.add(raw / path, arcname=path, recursive=False)
    body.seek(0)
    restored = tmp_path / "archive-restored"
    extract_tar(body, restored)
    assert stat.S_IMODE((restored / "payload" / name).stat().st_mode) == (0o755 if mode & 0o111 else 0o644)
    return target, restored, value, name


@pytest.mark.parametrize("mode", [0o600, 0o640, 0o644, 0o664, 0o700, 0o711, 0o750, 0o755])
def test_canonical_transport_restores_original_mode_bytes_and_timestamp(tmp_path, mode):
    original, source, manifest, name = transported(tmp_path, mode)
    target = tmp_path / "private-cache"
    record = entry.restore(source, target, "a" * 64, canonical_archive_modes=True)
    assert record["status"] == "restored"
    for row in manifest["files"]:
        actual = target / row["path"]
        assert stat.S_IMODE(actual.stat().st_mode) == row["mode"]
        assert actual.stat().st_mtime_ns == row["mtime_ns"]
        assert actual.read_bytes() == (original / row["path"]).read_bytes()
        assert actual.stat().st_ino != (source / "payload" / row["path"]).stat().st_ino
    assert stat.S_IMODE((target / name).stat().st_mode) == mode
    assert entry.restore(source, target, "a" * 64, canonical_archive_modes=True)["status"] == "already_initialized"
    assert json.loads((target / entry.MARKER).read_text())["canonical_archive_modes"] is True


def test_default_exact_mode_contract_is_not_silently_relaxed(tmp_path):
    _, source, _, _ = transported(tmp_path, 0o600)
    with pytest.raises(ValueError, match="mode changed"):
        entry.restore(source, tmp_path / "private", "a" * 64)
    assert not (tmp_path / "private").exists()


@pytest.mark.parametrize("mutation", ["execute", "other-mode", "bytes", "extra", "role", "binding"])
def test_normalization_does_not_allow_unbound_changes(tmp_path, mutation):
    _, source, value, name = transported(tmp_path, 0o600)
    path = source / "payload" / name
    if mutation == "execute":
        path.chmod(0o755)
    elif mutation == "other-mode":
        path.chmod(0o640)
    elif mutation == "bytes":
        path.write_text("changed")
    elif mutation == "extra":
        (source / "payload/extra").write_text("unlisted")
    elif mutation == "role":
        value["role"] = "grader"
    else:
        value["binding_sha256"] = "b" * 64
    (source / entry.MANIFEST).write_text(json.dumps(value))
    with pytest.raises(ValueError):
        entry.restore(source, tmp_path / "private", "a" * 64, canonical_archive_modes=True)
    assert not (tmp_path / "private").exists()


@pytest.mark.parametrize("mode", [True, -1, 0o4755, "0644"])
def test_original_restore_mode_must_be_a_regular_permission_integer(tmp_path, mode):
    _, source, value, name = transported(tmp_path, 0o600)
    next(row for row in value["files"] if row["path"] == name)["mode"] = mode
    (source / entry.MANIFEST).write_text(json.dumps(value))
    with pytest.raises(ValueError, match="invalid original"):
        entry.restore(source, tmp_path / "private", "a" * 64, canonical_archive_modes=True)


def test_private_caches_remain_independent_and_keep_candidate_mutations(tmp_path):
    _, source, _, name = transported(tmp_path, 0o600)
    first, second = tmp_path / "one", tmp_path / "two"
    entry.restore(source, first, "a" * 64, canonical_archive_modes=True)
    entry.restore(source, second, "a" * 64, canonical_archive_modes=True)
    old = (source / "payload" / name).read_bytes()
    (first / name).write_bytes(b"candidate mutation")
    assert entry.restore(source, first, "a" * 64, canonical_archive_modes=True)["status"] == "already_initialized"
    assert (first / name).read_bytes() == b"candidate mutation"
    assert (second / name).read_bytes() == (source / "payload" / name).read_bytes() == old


def test_new_recipe_declares_transport_without_changing_original_commands():
    base = recipe()
    descriptor = {"public_runtime_binding": host.public_binding(base),
                  "asset": {"path": "/original-seed.json", "sha256": "f" * 64}}
    result = host.bind(base, descriptor, {"path": "/new-importer.py", "sha256": "1" * 64})
    assert result.bootstrap[0].endswith("--canonical-archive-modes")
    assert result.grading == base.grading and result.dev_checks == base.dev_checks
    assert result.image == base.image and result.env == base.env
    assert next(a for a in result.assets if a["target"] == host.TARGET)["sha256"] == "f" * 64
