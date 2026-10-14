"""A declared private harness is fixed input, not a missing Git-owned test."""
import copy
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash, file_hash
from reviewbench_maintainability.agentic.evaluation import correctness, materialize_grader_immutable_inputs
from reviewbench_maintainability.agentic.snapshots import (
    collect_patch, copy_source, extract_start, restore_immutable,
)
from test_agentic_exec import repository, recipe_for


def fixture(tmp_path):
    base, work = tmp_path / "base", tmp_path / "candidate"
    base.mkdir()
    work.mkdir()
    (base / "production.py").write_text("VALUE=1\n")
    (work / "production.py").write_text("VALUE=2\n")
    fixed = tmp_path / "fixed.py"
    fixed.write_text("assert VALUE == 2\n")
    asset = {"path": str(fixed), "sha256": file_hash(fixed), "kind": "file",
             "visibility": "grader", "target": "/opt/assets/fixed.py"}
    recipe = SimpleNamespace(
        assets=[asset],
        grading={"immutable_paths": ["tests/fixed.py"],
                 "bootstrap": ["mkdir -p /workspace/tests && cp /opt/assets/fixed.py /workspace/tests/fixed.py"]},
    )
    return base, work, fixed, recipe


def test_declared_fixed_file_is_materialized_without_relabeling_git_input(tmp_path):
    base, work, fixed, recipe = fixture(tmp_path)
    assert not (base / "tests/fixed.py").exists()
    before = copy.deepcopy(recipe)
    records = materialize_grader_immutable_inputs(base, recipe)
    assert records == [{"path": "tests/fixed.py", "asset_target": "/opt/assets/fixed.py",
                        "sha256": file_hash(fixed), "origin": "fixed-grader-file-not-Git-start"}]
    restore_immutable(base, work, recipe.grading["immutable_paths"])
    assert (work / "tests/fixed.py").read_bytes() == fixed.read_bytes()
    assert (work / "production.py").read_text() == "VALUE=2\n"
    assert recipe.assets == before.assets and recipe.grading == before.grading
    assert materialize_grader_immutable_inputs(base, recipe) == []


@pytest.mark.parametrize("change", ["public-asset", "archive-asset", "no-copy", "extra-shell",
                                  "different-destination", "different-source", "no-asset", "unparsed-shell"])
def test_missing_unbound_tests_retain_the_original_error(tmp_path, change):
    base, work, _, recipe = fixture(tmp_path)
    if change == "public-asset":
        recipe.assets[0]["visibility"] = "developer"
    elif change == "archive-asset":
        recipe.assets[0]["kind"] = "archive"
    elif change == "no-copy":
        recipe.grading["bootstrap"] = []
    elif change == "extra-shell":
        recipe.grading["bootstrap"][0] += " && echo OK"
    elif change == "different-destination":
        recipe.grading["bootstrap"][0] = recipe.grading["bootstrap"][0].replace(
            "/workspace/tests/fixed.py", "/workspace/tests/other.py")
    elif change == "different-source":
        recipe.assets[0]["target"] = "/opt/assets/other.py"
    elif change == "unparsed-shell":
        recipe.grading["bootstrap"] = ["unmatched 'quote"]
    else:
        recipe.assets = []
    assert materialize_grader_immutable_inputs(base, recipe) == []
    with pytest.raises(ContractError, match="immutable test missing at start: tests/fixed.py"):
        restore_immutable(base, work, recipe.grading["immutable_paths"])


@pytest.mark.parametrize("change", ["bytes", "symlink", "duplicate-asset", "duplicate-copy"])
def test_changed_or_ambiguous_assets_are_not_installed(tmp_path, change):
    base, _, fixed, recipe = fixture(tmp_path)
    if change == "bytes":
        fixed.write_text("weakened\n")
    elif change == "symlink":
        saved = tmp_path / "saved.py"
        fixed.rename(saved)
        fixed.symlink_to(saved)
    elif change == "duplicate-asset":
        recipe.assets.append(copy.deepcopy(recipe.assets[0]))
    else:
        recipe.grading["bootstrap"] *= 2
    with pytest.raises(ContractError):
        materialize_grader_immutable_inputs(base, recipe)
    assert not (base / "tests/fixed.py").exists()


def test_committed_test_still_comes_from_git_not_private_asset(tmp_path):
    base, work, _, recipe = fixture(tmp_path)
    (base / "tests").mkdir()
    (base / "tests/fixed.py").write_text("COMMITTED_TEST\n")
    assert materialize_grader_immutable_inputs(base, recipe) == []
    restore_immutable(base, work, recipe.grading["immutable_paths"])
    assert (work / "tests/fixed.py").read_text() == "COMMITTED_TEST\n"


def test_existing_repository_directory_is_left_to_original_gitlink_handling(tmp_path):
    base, _, _, recipe = fixture(tmp_path)
    (base / "tests/fixed.py").mkdir(parents=True)
    assert materialize_grader_immutable_inputs(base, recipe) == []
    assert (base / "tests/fixed.py").is_dir()


@pytest.mark.parametrize("which", ["base", "candidate"])
def test_private_materialization_cannot_follow_test_parent_links(tmp_path, which):
    base, work, _, recipe = fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (base if which == "base" else work).joinpath("tests").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ContractError, match="parent"):
        materialize_grader_immutable_inputs(base, recipe)
        restore_immutable(base, work, recipe.grading["immutable_paths"])
    assert not (outside / "fixed.py").exists()


def test_complete_correctness_path_restores_private_bytes_before_environment_creation(repository, tmp_path):
    repo, task = repository
    base, candidate = tmp_path / "snapshot", tmp_path / "answer"
    extract_start(repo, task["start_sha"], base)
    copy_source(base, candidate)
    (candidate / "main.py").write_text("def inc(x):\n    return 1 + x\n")
    (candidate / "tests/private.py").write_text("CANDIDATE_FORGERY\n")
    patch = tmp_path / "answer.patch"
    generation = collect_patch(base, candidate, patch)
    fixed = tmp_path / "private.py"
    fixed.write_text("ORIGINAL_FIXED_ASSERTION\n")
    recipe = recipe_for(task)
    recipe.assets.append({"path": str(fixed), "sha256": file_hash(fixed), "kind": "file",
                          "visibility": "grader", "target": "/opt/assets/private.py"})
    recipe.grading["immutable_paths"].append("tests/private.py")
    recipe.grading["bootstrap"] = [
        "mkdir -p /workspace/tests && cp /opt/assets/private.py /workspace/tests/private.py"
    ]
    generation.update(
        id=task["id"], model="fixture", protocol="reviewbench-agentic-exec/v1",
        task_fingerprint=canonical_hash(task), runtime_fingerprint=recipe.fingerprint,
        start_sha=task["start_sha"], patch_file=str(patch), patch_sha256=file_hash(patch),
    )
    calls = []
    class Environment:
        def __init__(self, bound, work, output, asset_root, *, grader):
            assert grader and bound.fingerprint == recipe.fingerprint
            assert (work / "tests/private.py").read_bytes() == fixed.read_bytes()
            assert (work / "tests/test_main.py").read_bytes() == (base / "tests/test_main.py").read_bytes()
            assert (work / "main.py").read_bytes() == (candidate / "main.py").read_bytes()
            self.output = output
            output.mkdir(parents=True)
        def execute(self, action):
            calls.append(action["command"])
            index = str(len(calls))
            (self.output / (index + ".stdout")).write_text("OK\n")
            (self.output / (index + ".stderr")).write_text("")
            return {"returncode": 0, "extra": {"stdout_file": index + ".stdout", "stderr_file": index + ".stderr"}}
        def close(self):
            pass
    result = correctness(task, recipe, generation, repo, tmp_path / "evaluation", tmp_path,
                         environment_factory=Environment)
    assert result["status"] == "pass" and result["oracle_replayed"]
    assert result["grader_immutable_inputs"][0]["sha256"] == file_hash(fixed)
    assert result["runtime_fingerprint"] == generation["runtime_fingerprint"]
    assert len(calls) == len(recipe.bootstrap) + len(recipe.grading["bootstrap"]) + 1
    assert not (base / "tests/private.py").exists()


def archive_fixture(tmp_path, monkeypatch):
    from reviewbench_maintainability.agentic import assets
    from reviewbench_maintainability.agentic.assets import ASSET_SCHEMA, AssetPool
    from reviewbench_maintainability.agentic.contracts import atomic_json
    from reviewbench_maintainability.agentic.retention import archive_files
    from reviewbench_maintainability.agentic.snapshots import tree_hash
    from reviewbench_maintainability.agentic.storage import DirectoryObjectStore, LocalObjectCache
    source = tmp_path / "grader-source"
    (source / "session").mkdir(parents=True)
    (source / "session/private.py").write_text(
        "from main import inc\nassert inc(1) == 2\nprint('PRIVATE_ASSERTIONS_PASSED')\n")
    (source / "session/second.py").write_text("SECOND_FIXED_PRIVATE_INPUT\n")
    cache = LocalObjectCache(tmp_path / "object-cache", DirectoryObjectStore(tmp_path / "remote"),
                             min_free_bytes=0)
    archive, _ = archive_files(source, ["session/private.py", "session/second.py"], cache,
                               tmp_path / "compression")
    descriptor = tmp_path / "grader.json"
    atomic_json(descriptor, {"schema_version": ASSET_SCHEMA, "source_kind": "grader",
                            "tree_sha256": tree_hash(source), "archive": archive})
    asset = {"path": str(descriptor), "sha256": file_hash(descriptor), "kind": "archive",
             "visibility": "grader", "target": "/opt/assets/grader"}
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    pool = AssetPool(cache, scratch, "d" * 32, tmp_path)
    monkeypatch.setattr(assets, "containers_alive", lambda _: False)
    base, work = tmp_path / "base", tmp_path / "candidate"
    base.mkdir()
    work.mkdir()
    recipe = SimpleNamespace(
        assets=[asset],
        grading={"immutable_paths": ["tests/private.py", "tests/second.py"], "bootstrap": [
            "mkdir -p /workspace/tests && cp /opt/assets/grader/session/private.py /workspace/tests/private.py",
            "mkdir -p /workspace/tests && cp /opt/assets/grader/session/second.py /workspace/tests/second.py",
        ]},
    )
    return base, work, source, recipe, pool, scratch


@contextmanager
def archive_job(pool, scratch):
    from reviewbench_maintainability.agentic.resources import CURRENT_JOB
    token = CURRENT_JOB.set({"asset_pool": pool, "scratch_root": str(scratch), "lease": pool.lease})
    try:
        yield
    finally:
        CURRENT_JOB.reset(token)
        pool.close()


def test_fixed_archive_members_hydrate_once_and_preserve_complete_recipe(tmp_path, monkeypatch):
    from reviewbench_maintainability.agentic import assets
    base, work, source, recipe, pool, scratch = archive_fixture(tmp_path, monkeypatch)
    before = copy.deepcopy(recipe)
    extractions = []
    original = assets.extract_archive
    def counted(*args, **kwargs):
        extractions.append(args[0])
        return original(*args, **kwargs)
    monkeypatch.setattr(assets, "extract_archive", counted)
    with archive_job(pool, scratch):
        records = materialize_grader_immutable_inputs(base, recipe)
        assert len(records) == 2
        assert all(row["origin"] == "fixed-grader-archive-member-not-Git-start" for row in records)
        assert records[0]["archive_member"] == "session/private.py"
        assert records[0]["descriptor_sha256"] == recipe.assets[0]["sha256"]
        restore_immutable(base, work, recipe.grading["immutable_paths"])
        assert (work / "tests/private.py").read_bytes() == (source / "session/private.py").read_bytes()
        assert materialize_grader_immutable_inputs(base, recipe) == []
        pool.materialize(recipe.assets[0])  # the grading container uses this same hydrated tree
        assert len(extractions) == 1
        assert recipe.assets == before.assets and recipe.grading == before.grading


@pytest.mark.parametrize("change", ["public", "role", "descriptor", "missing-member", "traversal",
                                  "duplicate", "extra-shell", "different-mount"])
def test_unbound_changed_or_ambiguous_archives_do_not_stage_private_tests(tmp_path, monkeypatch, change):
    base, work, _, recipe, pool, scratch = archive_fixture(tmp_path, monkeypatch)
    if change == "public":
        recipe.assets[0]["visibility"] = "developer"
    elif change == "role":
        p = Path(recipe.assets[0]["path"])
        data = json.loads(p.read_text())
        data["source_kind"] = "dependency"
        p.write_text(json.dumps(data))
        recipe.assets[0]["sha256"] = file_hash(p)
    elif change == "descriptor":
        with Path(recipe.assets[0]["path"]).open("a") as stream:
            stream.write(" ")
    elif change in {"missing-member", "traversal"}:
        recipe.grading["bootstrap"][0] = recipe.grading["bootstrap"][0].replace(
            "/session/private.py", "/missing.py" if change == "missing-member" else "/../outside.py")
    elif change == "duplicate":
        recipe.assets.append(copy.deepcopy(recipe.assets[0]))
    elif change == "extra-shell":
        recipe.grading["bootstrap"] = [command + " && true" for command in recipe.grading["bootstrap"]]
    else:
        recipe.assets[0]["target"] = "/opt/assets/grader-prefix-only"
    with archive_job(pool, scratch), pytest.raises(ContractError):
        materialize_grader_immutable_inputs(base, recipe)
        restore_immutable(base, work, recipe.grading["immutable_paths"])
    assert not (base / "tests/private.py").exists()


def test_fixed_archive_requires_an_admitted_asset_pool(tmp_path, monkeypatch):
    from reviewbench_maintainability.agentic.resources import CURRENT_JOB
    base, _, _, recipe, pool, _ = archive_fixture(tmp_path, monkeypatch)
    token = CURRENT_JOB.set(None)
    try:
        with pytest.raises(ContractError, match="admitted asset pool"):
            materialize_grader_immutable_inputs(base, recipe)
    finally:
        CURRENT_JOB.reset(token)
        pool.close()


def test_git_owned_input_is_not_replaced_by_an_archive_member(tmp_path, monkeypatch):
    base, work, _, recipe, pool, scratch = archive_fixture(tmp_path, monkeypatch)
    (base / "tests").mkdir()
    (base / "tests/private.py").write_text("ORIGINAL_GIT_TEST\n")
    with archive_job(pool, scratch):
        records = materialize_grader_immutable_inputs(base, recipe)
        assert [row["path"] for row in records] == ["tests/second.py"]
        restore_immutable(base, work, recipe.grading["immutable_paths"])
        assert (work / "tests/private.py").read_text() == "ORIGINAL_GIT_TEST\n"


def test_hydrated_member_symlinks_are_not_accepted_as_fixed_assertion_files(tmp_path, monkeypatch):
    base, _, _, recipe, pool, scratch = archive_fixture(tmp_path, monkeypatch)
    with archive_job(pool, scratch):
        root = pool.materialize(recipe.assets[0])
        member = root / "session/private.py"
        member.unlink()
        member.symlink_to("second.py")
        with pytest.raises(ContractError, match="member is missing or redirected"):
            materialize_grader_immutable_inputs(base, recipe)
    assert not (base / "tests/private.py").exists()


@pytest.mark.parametrize("good_candidate", [True, False])
def test_real_final_correctness_keeps_archive_assertions_and_rejects_candidate_forgery(
        repository, tmp_path, monkeypatch, good_candidate):
    repo, task = repository
    _, _, source, private_recipe, pool, scratch = archive_fixture(tmp_path, monkeypatch)
    base, candidate = tmp_path / "snapshot", tmp_path / "answer"
    extract_start(repo, task["start_sha"], base)
    copy_source(base, candidate)
    (candidate / "main.py").write_text(
        "def inc(x):\n    return 1 + x\n" if good_candidate else "def inc(x):\n    return 999\n")
    (candidate / "tests/private.py").write_text("print('PRIVATE_ASSERTIONS_PASSED')\n")
    patch = tmp_path / "answer.patch"
    generation = collect_patch(base, candidate, patch)
    recipe = recipe_for(task)
    recipe.assets.extend(private_recipe.assets)
    recipe.grading["immutable_paths"].append("tests/private.py")
    recipe.grading["bootstrap"] = private_recipe.grading["bootstrap"][:1]
    recipe.grading["commands"] = [{
        "command": f"{sys.executable} tests/private.py", "parser": "command",
        "expected_tests": ["private_behavior"], "success_marker": "PRIVATE_ASSERTIONS_PASSED",
    }]
    recipe_before = recipe.fingerprint
    generation.update(
        id=task["id"], model="fixture", protocol="reviewbench-agentic-exec/v1",
        task_fingerprint=canonical_hash(task), runtime_fingerprint=recipe.fingerprint,
        start_sha=task["start_sha"], patch_file=str(patch), patch_sha256=file_hash(patch),
    )
    generation_before = copy.deepcopy(generation)
    calls = []
    class Environment:
        def __init__(self, bound, work, output, asset_root, *, grader):
            assert grader and bound.fingerprint == recipe_before
            assert (work / "tests/private.py").read_bytes() == (source / "session/private.py").read_bytes()
            assert (work / "tests/test_main.py").read_bytes() == (base / "tests/test_main.py").read_bytes()
            self.work, self.output = work, output
            self.archive = pool.materialize(recipe.assets[0])
            output.mkdir(parents=True)
        def execute(self, action):
            calls.append(action["command"])
            command = action["command"].replace("/opt/assets/grader", str(self.archive)).replace(
                "/workspace", str(self.work))
            process = subprocess.run(command, shell=True, executable="/bin/bash", cwd=self.work,
                                     capture_output=True, text=True, timeout=15,
                                     env={**os.environ, "PYTHONPATH": str(self.work), "PYTHONDONTWRITEBYTECODE": "1"})
            index = str(len(calls))
            (self.output / (index + ".stdout")).write_text(process.stdout)
            (self.output / (index + ".stderr")).write_text(process.stderr)
            return {"returncode": process.returncode,
                    "extra": {"stdout_file": index + ".stdout", "stderr_file": index + ".stderr"}}
        def close(self):
            pass
    with archive_job(pool, scratch):
        result = correctness(task, recipe, generation, repo, tmp_path / "evaluation", tmp_path,
                             environment_factory=Environment)
    assert result["status"] == ("pass" if good_candidate else "fail"), result
    assert result["correctness_ok"] is good_candidate and result["oracle_replayed"]
    assert result["grader_immutable_inputs"][0]["sha256"] == file_hash(source / "session/private.py")
    assert recipe.fingerprint == recipe_before and generation == generation_before
    assert calls == [*recipe.grading["bootstrap"], recipe.grading["commands"][0]["command"]]
    assert not (base / "tests/private.py").exists()
