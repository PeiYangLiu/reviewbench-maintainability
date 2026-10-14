"""Idempotent repeated crate files must not admit conflicting archive members."""
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tarfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_lane
from reviewbench_maintainability.agentic.contracts import ContractError, file_hash
from reviewbench_maintainability.agentic.snapshots import tree_files


def make_archive(path, entries):
    with tarfile.open(path, "w:gz") as stream:
        for name, content, kind, mode, link in entries:
            member = tarfile.TarInfo(name)
            member.type, member.mode, member.linkname = kind, mode, link
            member.size = len(content) if member.isfile() else 0
            stream.addfile(member, io.BytesIO(content) if member.isfile() else None)
    return path


def regular(content=b"original\n", *, mode=0o644, name="dep-1.0/src/lib.rs"):
    return name, content, tarfile.REGTYPE, mode, ""


@pytest.mark.parametrize("content", [b"", b"original\n", b"x" * (1024**2 + 17)])
@pytest.mark.parametrize("repetitions", [2, 3])
@pytest.mark.parametrize("mode", [0o644, 0o755])
def test_byte_and_mode_identical_regular_members_are_idempotent(tmp_path, content, repetitions, mode):
    source = make_archive(tmp_path / "same.crate", [regular(content, mode=mode)] * repetitions)
    before = file_hash(source)
    destination = tmp_path / "out"
    assert rust_lane.unpack_crate(source, destination, "dep-1.0") == []
    target = destination / "dep-1.0/src/lib.rs"
    assert target.read_bytes() == content and stat.S_IMODE(target.stat().st_mode) == mode
    assert set(tree_files(destination)) == {"dep-1.0/src/lib.rs"}
    assert file_hash(source) == before


@pytest.mark.parametrize("first,second", [
    (regular(), regular(b"changed!\n")),
    (regular(), regular(b"original!\n")),
    (regular(b"x" * (1024**2 + 17)), regular(b"x" * (1024**2 + 16) + b"y")),
    (regular(mode=0o644), regular(mode=0o755)),
    (regular(mode=0o755), regular(mode=0o644)),
    (regular(), regular(name="dep-1.0/src/./lib.rs")),
    (regular(), ("dep-1.0/src/lib.rs", b"", tarfile.DIRTYPE, 0o755, "")),
    (("dep-1.0/src/lib.rs", b"", tarfile.DIRTYPE, 0o755, ""), regular()),
    (regular(), ("dep-1.0/src/lib.rs", b"", tarfile.SYMTYPE, 0o777, "other.rs")),
    (("dep-1.0/src/lib.rs", b"", tarfile.SYMTYPE, 0o777, "other.rs"),
     ("dep-1.0/src/lib.rs", b"", tarfile.SYMTYPE, 0o777, "other.rs")),
    (regular(), ("dep-1.0/src/lib.rs", b"", tarfile.LNKTYPE, 0o644, "dep-1.0/src/lib.rs")),
])
def test_conflicting_bytes_modes_paths_types_or_links_still_fail_atomically(tmp_path, first, second):
    source = make_archive(tmp_path / "conflict.crate", [first, second])
    before = file_hash(source)
    destination = tmp_path / "out"
    with pytest.raises(ContractError):
        rust_lane.unpack_crate(source, destination, "dep-1.0")
    assert not destination.exists() and file_hash(source) == before


def test_repeated_directories_keep_the_original_behavior(tmp_path):
    directory = ("dep-1.0/src", b"", tarfile.DIRTYPE, 0o755, "")
    source = make_archive(tmp_path / "dirs.crate", [directory, regular(), directory])
    destination = tmp_path / "out"
    assert rust_lane.unpack_crate(source, destination, "dep-1.0") == []
    assert (destination / "dep-1.0/src/lib.rs").read_bytes() == b"original\n"


def test_identical_duplicate_does_not_allow_a_later_symlink_descent(tmp_path):
    link = ("dep-1.0/link", b"", tarfile.SYMTYPE, 0o777, "src")
    source = make_archive(tmp_path / "links.crate", [
        regular(), regular(), link, regular(name="dep-1.0/link/lib.rs"),
    ])
    with pytest.raises(ContractError, match="symlink"):
        rust_lane.unpack_crate(source, tmp_path / "out", "dep-1.0")
    assert not (tmp_path / "out").exists()


def test_existing_destination_still_rejected_even_for_identical_archive(tmp_path):
    destination = tmp_path / "out"
    destination.mkdir()
    (destination / "keep").write_text("original caller-owned data")
    source = make_archive(tmp_path / "same.crate", [regular(), regular()])
    with pytest.raises(ContractError, match="already exists"):
        rust_lane.unpack_crate(source, destination, "dep-1.0")
    assert (destination / "keep").read_text() == "original caller-owned data"


def test_published_winapi_archive_matches_every_original_cargo_extracted_file(tmp_path):
    configured = os.environ.get("SCYLLA_FIRST_RESOLUTION_INPUTS")
    if not configured or not Path(configured).is_file():
        pytest.skip("requires the explicit original Scylla preparation envelope")
    from agentic_runtime_migration import rust_first_resolution as first
    envelope = Path(configured)
    prepared = first.prepared_inputs({"path": str(envelope), "sha256": file_hash(envelope)})
    reference = prepared["archives"][("winapi", "0.2.4", first.CRATES_IO)]
    archive = Path(reference["path"])
    assert file_hash(archive) == "5350e40d908c7e8b9e5c9edb541ca47cc617c6229d3575a46da6f550f36c96fd"
    package = next(row for row in prepared["graphs"]["start"]["packages"]
                   if (row["name"], row["version"], row["source"]) == ("winapi", "0.2.4", first.CRATES_IO))
    cargo_source = Path(package["manifest_path"]).parent
    destination = tmp_path / "out"
    assert rust_lane.unpack_crate(archive, destination, "winapi-0.2.4") == []
    expected, repeats = {}, 0
    with tarfile.open(archive, "r:gz") as stream:
        for member in stream:
            if not member.isfile():
                continue
            relative = Path(member.name).relative_to("winapi-0.2.4").as_posix()
            data = stream.extractfile(member).read()
            digest = hashlib.sha256(data).hexdigest()
            if relative in expected:
                assert expected[relative] == (digest, member.mode)
                repeats += 1
            expected[relative] = digest, member.mode
    assert repeats == 1
    actual = tree_files(destination / "winapi-0.2.4")
    assert set(actual) == set(expected)
    for name, (digest, mode) in expected.items():
        assert file_hash(actual[name]) == file_hash(cargo_source / name) == digest
        assert stat.S_IMODE(actual[name].stat().st_mode) == mode
    assert file_hash(archive) == reference["sha256"]
