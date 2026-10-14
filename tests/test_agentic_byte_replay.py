import os
from pathlib import Path

import pytest

from reviewbench_maintainability.agentic.contracts import ContractError
from reviewbench_maintainability.agentic.snapshots import (
    apply_patch, collect_patch, copy_source, restore_immutable, tree_hash,
)


@pytest.mark.parametrize("attributes,original,modified", [
    ("*.txt text eol=lf\n", b"first\r\nbefore\r\nlast\r\n", b"first\r\nafter\r\nlast\r\n"),
    ("*.txt text eol=lf\n", b"first\r\nbefore\r\nlast\r\n", b"first\nafter\nlast\n"),
    ("*.txt text working-tree-encoding=UTF-16LE\n",
     "before\n".encode("utf-16-le"), "after\n".encode("utf-16-le")),
])
def test_host_patch_round_trip_preserves_actual_bytes_despite_git_attributes(tmp_path, attributes, original, modified):
    base, work, replay = (tmp_path / name for name in ("base", "work", "replay"))
    base.mkdir()
    (base / ".gitattributes").write_text(attributes)
    (base / "source.txt").write_bytes(original)
    original_hash = tree_hash(base)
    copy_source(base, work)
    (work / "source.txt").write_bytes(modified)
    result = collect_patch(base, work, tmp_path / "candidate.patch")
    assert result["apply_ok"]
    assert result["changed_files"] == ["source.txt"]
    apply_patch(base, Path(result["patch_file"]), replay)
    assert (replay / "source.txt").read_bytes() == modified
    assert tree_hash(replay) == tree_hash(work)
    assert tree_hash(base) == original_hash


@pytest.mark.parametrize("destination_kind", ["file", "directory", "symlink"])
def test_frozen_dangling_symlink_is_restored_as_a_link(tmp_path, destination_kind):
    base, work = tmp_path / "base", tmp_path / "work"
    (base / "tests").mkdir(parents=True)
    (work / "tests").mkdir(parents=True)
    (base / "tests/fixture").symlink_to("../intentionally-missing")
    destination = work / "tests/fixture"
    if destination_kind == "file":
        destination.write_text("candidate replacement")
    elif destination_kind == "directory":
        destination.mkdir()
        (destination / "untrusted").write_text("replacement")
    else:
        destination.symlink_to("../another-missing")
    restore_immutable(base, work, ["tests/fixture"])
    assert destination.is_symlink()
    assert os.readlink(destination) == "../intentionally-missing"
    assert not destination.exists()


def test_restoring_test_link_does_not_restore_its_modified_production_target(tmp_path):
    base, work = tmp_path / "base", tmp_path / "work"
    (base / "tests").mkdir(parents=True)
    (base / "production.py").write_text("original production")
    (base / "tests/reference").symlink_to("../production.py")
    copy_source(base, work)
    (work / "production.py").write_text("candidate production")
    restore_immutable(base, work, ["tests/reference"])
    assert (work / "tests/reference").is_symlink()
    assert (work / "production.py").read_text() == "candidate production"
    assert (base / "production.py").read_text() == "original production"


def test_immutable_restore_does_not_follow_replaced_parent_directory(tmp_path):
    base, work, outside = (tmp_path / name for name in ("base", "work", "outside"))
    (base / "tests").mkdir(parents=True)
    (base / "tests/fixture").write_text("frozen")
    work.mkdir()
    outside.mkdir()
    (outside / "fixture").write_text("untouched")
    (work / "tests").symlink_to(outside)
    with pytest.raises(ContractError, match="parent became a symlink"):
        restore_immutable(base, work, ["tests/fixture"])
    assert (outside / "fixture").read_text() == "untouched"
