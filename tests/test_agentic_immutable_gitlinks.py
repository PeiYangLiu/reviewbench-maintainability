from contextlib import contextmanager
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from reviewbench_maintainability.agentic.contracts import ContractError
from reviewbench_maintainability.agentic.resources import CURRENT_JOB
from reviewbench_maintainability.agentic.snapshots import restore_immutable, tree_hash

NAME = "tests/external-suite"
PIN = "a" * 40


@contextmanager
def admitted(*, omit=True, pin=PIN, source_pin=PIN, asset=None, reason="optional original component"):
    task = {"repo": "fixture/project", "start_sha": "b" * 40}
    def source(repo, sha):
        assert (repo, sha) == (task["repo"], task["start_sha"])
        return {"gitlinks": [{"path": NAME, "commit": source_pin}]}
    entry = {"path": NAME, "commit": pin, "omit": omit, "reason": reason}
    if asset is not None:
        entry["asset"] = asset
    token = CURRENT_JOB.set({"source_catalog": SimpleNamespace(source=source), "task": task,
                             "submodules": [entry]})
    try:
        yield
    finally:
        CURRENT_JOB.reset(token)


@pytest.mark.parametrize("base_present", [False, True])
@pytest.mark.parametrize("destination_kind", ["absent", "file", "directory", "link"])
def test_omitted_gitlink_restores_exact_absent_or_empty_state_without_candidate_injection(
        tmp_path, base_present, destination_kind):
    base, work, outside = (tmp_path / name for name in ("base", "work", "outside"))
    (base / "tests").mkdir(parents=True)
    (work / "tests").mkdir(parents=True)
    outside.mkdir()
    (outside / "keep").write_text("retained")
    (base / "production.py").write_text("before")
    (work / "production.py").write_text("current candidate")
    if base_present:
        (base / NAME).mkdir()
    destination = work / NAME
    if destination_kind == "file":
        destination.write_text("injected test")
    elif destination_kind == "directory":
        destination.mkdir()
        (destination / "injected.py").write_text("injected test")
    elif destination_kind == "link":
        destination.symlink_to(outside, target_is_directory=True)
    before = tree_hash(base)
    with admitted():
        restore_immutable(base, work, [NAME])
    assert (base / NAME).exists() == destination.exists() == base_present
    if base_present:
        assert destination.is_dir() and not list(destination.iterdir())
    assert not destination.is_symlink()
    assert (work / "production.py").read_text() == "current candidate"
    assert (outside / "keep").read_text() == "retained"
    assert tree_hash(base) == before


def test_materialized_required_gitlink_restores_frozen_files_links_and_empty_directories(tmp_path):
    base, work = tmp_path / "base", tmp_path / "work"
    source, destination = base / NAME, work / NAME
    (source / "empty").mkdir(parents=True)
    (source / "fixture.py").write_text("frozen assertion")
    (source / "reference").symlink_to("fixture.py")
    (destination / "extra").mkdir(parents=True)
    (destination / "fixture.py").write_text("changed assertion")
    with admitted(omit=False, asset={"path": "/pinned/descriptor", "sha256": "c" * 64}):
        restore_immutable(base, work, [NAME])
    assert (destination / "fixture.py").read_text() == "frozen assertion"
    assert (destination / "empty").is_dir() and not (destination / "extra").exists()
    assert os.readlink(destination / "reference") == "fixture.py"


@pytest.mark.parametrize("change", ["no-catalog", "wrong-pin", "missing-reason", "nonempty-omission",
                                    "required-missing", "source-link"])
def test_unbound_or_unexpected_gitlink_state_is_not_silently_ignored(tmp_path, change):
    base, work = tmp_path / "base", tmp_path / "work"
    (base / "tests").mkdir(parents=True)
    (work / "tests").mkdir(parents=True)
    (work / NAME).write_text("candidate unchanged on rejection")
    options = {}
    if change == "wrong-pin":
        options["pin"] = "c" * 40
    elif change == "missing-reason":
        options["reason"] = ""
    elif change == "nonempty-omission":
        (base / NAME).mkdir()
        (base / NAME / "unexpected").write_text("real input")
    elif change == "required-missing":
        options.update(omit=False, asset={"path": "/pinned", "sha256": "c" * 64})
    elif change == "source-link":
        (base / NAME).symlink_to("../outside")
    with admitted(**options):
        if change == "no-catalog":
            CURRENT_JOB.get().pop("source_catalog")
        with pytest.raises(ContractError):
            restore_immutable(base, work, [NAME])
    assert (work / NAME).read_text() == "candidate unchanged on rejection"


def test_unrelated_missing_regular_test_still_fails(tmp_path):
    base, work = tmp_path / "base", tmp_path / "work"
    base.mkdir()
    work.mkdir()
    with admitted(), pytest.raises(ContractError, match="missing at start"):
        restore_immutable(base, work, ["tests/not-a-gitlink.py"])


def test_gitlink_restore_does_not_follow_candidate_parent_links(tmp_path):
    base, work, outside = (tmp_path / name for name in ("base", "work", "outside"))
    base.mkdir()
    work.mkdir()
    outside.mkdir()
    (outside / "external-suite").write_text("untouched")
    (work / "tests").symlink_to(outside, target_is_directory=True)
    with admitted(), pytest.raises(ContractError, match="parent became a symlink"):
        restore_immutable(base, work, [NAME])
    assert (outside / "external-suite").read_text() == "untouched"
