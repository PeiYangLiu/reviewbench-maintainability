from dataclasses import asdict
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from reviewbench_maintainability.agentic.assets import ASSET_SCHEMA, AssetPool
from reviewbench_maintainability.agentic.contracts import ContractError, atomic_json, file_hash
from reviewbench_maintainability.agentic.evaluation import parse_tests
from reviewbench_maintainability.agentic.retention import archive_files
from reviewbench_maintainability.agentic.resources import ResourceGate, ResourceLimits, ResourceRequest
from reviewbench_maintainability.agentic.snapshots import git, initialize_git, tree_hash
from reviewbench_maintainability.agentic.sources import SourceCatalog, stage_sources
from reviewbench_maintainability.agentic.storage import DirectoryObjectStore, LocalObjectCache


def test_archive_assets_hydrate_once_and_release_after_the_task(tmp_path, monkeypatch):
    from reviewbench_maintainability.agentic import assets
    source = tmp_path / "source"
    source.mkdir()
    (source / "dependency.py").write_text("VALUE = 42\n")
    cache = LocalObjectCache(tmp_path / "cache", DirectoryObjectStore(tmp_path / "remote"), min_free_bytes=0)
    archive, _ = archive_files(source, ["dependency.py"], cache, tmp_path / "compression")
    manifest = tmp_path / "dependency.json"
    atomic_json(manifest, {"schema_version": ASSET_SCHEMA, "source_kind": "dependency",
                          "tree_sha256": tree_hash(source), "archive": archive})
    spec = {"path": str(manifest), "sha256": file_hash(manifest)}
    pool = AssetPool(cache, tmp_path / "scratch", "a" * 32, tmp_path)
    first = pool.materialize(spec)
    assert pool.materialize(spec) == first
    assert (first / "dependency.py").read_text() == "VALUE = 42\n"
    monkeypatch.setattr(assets, "containers_alive", lambda _: True)
    pool.close()
    assert first.exists()
    monkeypatch.setattr(assets, "containers_alive", lambda _: False)
    pool.close()
    assert not first.exists()


def test_frozen_signature_requires_completed_assertions_not_only_exit_zero():
    pattern = r'^P2P_SIGNATURE=\{"result":42\}$'
    for text, code, passed in [("", True, False), ('P2P_SIGNATURE={"result":42}\n', False, False),
                                ('P2P_SIGNATURE={"result":41}\n', True, False),
                                ('P2P_SIGNATURE={"result":42}\n', True, True)]:
        assert parse_tests("regex", text, command_ok=code, expected=["public behavior"],
                           success_pattern=pattern) == {"public behavior": passed}


def test_optional_submodules_need_complete_explicit_policy(tmp_path):
    root = tmp_path / "repos"
    repo = root / "fixture__repo"
    repo.mkdir(parents=True)
    (repo / "main.py").write_text("VALUE = 42\n")
    initialize_git(repo)
    commit = git(repo, "rev-parse", "HEAD").decode().strip()
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{commit},optional-corpus")
    git(repo, "commit", "--quiet", "-m", "external corpus")
    sha = git(repo, "rev-parse", "HEAD").decode().strip()
    cache = LocalObjectCache(tmp_path / "cache", DirectoryObjectStore(tmp_path / "remote"), min_free_bytes=0)
    gate = ResourceGate(tmp_path / "gate", tmp_path / "scratch",
                        ResourceLimits(min_free_disk_bytes=0, min_available_memory_bytes=0))
    tasks = {"task": {"id": "task", "repo": "fixture/repo", "start_sha": sha}}
    stage_sources(tasks, root, cache, tmp_path / "scratch", tmp_path / "sources", gate)
    catalog = SourceCatalog(tmp_path / "sources/source-catalog.json", cache)
    with pytest.raises(ContractError, match="root-only"):
        catalog.extract("fixture/repo", sha, tmp_path / "missing")
    policy = [{"path": "optional-corpus", "commit": commit, "omit": True,
               "reason": "explicit optional test corpus; not a complete submodule source closure"}]
    catalog.extract("fixture/repo", sha, tmp_path / "task-checkout", submodules=policy)
    assert (tmp_path / "task-checkout/main.py").is_file()
    assert catalog.source("fixture/repo", sha)["source_closure"] == "root-only"
    with pytest.raises(ContractError, match="Git pin"):
        catalog.extract("fixture/repo", sha, tmp_path / "wrong-pin",
                        submodules=[{**policy[0], "commit": "1" * 40}])


@pytest.mark.parametrize("has_path", [True, False])
def test_python_boundary_stub_can_import_new_candidate_helpers(tmp_path, has_path):
    root = Path(__file__).resolve().parents[1]
    source = tmp_path / "source"
    (source / "pkg").mkdir(parents=True)
    (source / "pkg/main.py").write_text("from .helper import inc\n")
    (source / "pkg/helper.py").write_text("def inc(x): return x + 1\n")
    private = tmp_path / "private"
    private.mkdir()
    (private / "oracle_support.py").write_text("# fixed trusted helper\n")
    script = private / "oracle.py"
    script.write_text(
        "import importlib.util, os, sys, types\n"
        "pkg=types.ModuleType('pkg')\n"
        + ("pkg.__path__=[]\n" if has_path else "")
        + "sys.modules['pkg']=pkg\n"
        "spec=importlib.util.spec_from_file_location('pkg.main',os.environ['SOURCE_ROOT']+'/pkg/main.py')\n"
        "module=importlib.util.module_from_spec(spec)\n"
        "sys.modules[spec.name]=module\n"
        "spec.loader.exec_module(module)\n"
        "assert module.inc(1)==2\n"
        "print('ASSERTIONS_COMPLETED')\n"
    )
    command = [sys.executable, str(root / "scripts/agentic_runtime_migration/python_oracle_entry.py"), str(script)]
    env = {**os.environ, "SOURCE_ROOT": str(source), "PYTHONPATH": str(private),
           "PYTHONDONTWRITEBYTECODE": "1"}
    passed = subprocess.run(command, env=env, capture_output=True, text=True, timeout=20)
    assert passed.returncode == 0, passed.stderr
    assert passed.stdout == "ASSERTIONS_COMPLETED\n"
    (source / "pkg/helper.py").write_text("def inc(x): return x - 1\n")
    failed = subprocess.run(command, env=env, capture_output=True, text=True, timeout=20)
    assert failed.returncode != 0 and "ASSERTIONS_COMPLETED" not in failed.stdout


def test_pure_job_does_not_leak_a_reservation_when_docker_is_absent(tmp_path, monkeypatch):
    from reviewbench_maintainability.agentic import resources
    gate = ResourceGate(tmp_path / "gate", tmp_path / "scratch",
                        ResourceLimits(min_free_disk_bytes=0, min_available_memory_bytes=0))
    monkeypatch.setattr(resources, "containers_alive", lambda _: pytest.fail("no container was attempted"))
    with gate.acquire(ResourceRequest("terminal-fast-path", memory_bytes=1), uses_containers=True):
        pass
    assert gate.status()["active"]["tasks"] == 0


def test_mutable_caches_are_shared_only_within_one_task_lease(tmp_path, monkeypatch):
    from reviewbench_maintainability.agentic import assets
    monkeypatch.setattr(assets, "containers_alive", lambda _: False)
    first = AssetPool(None, tmp_path, "a"*32, tmp_path)
    second = AssetPool(None, tmp_path, "b"*32, tmp_path)
    cache = first.mutable_cache("ccache")
    (cache / "compiled-object").write_text("first task only")
    assert first.mutable_cache("ccache") == cache
    assert not (second.mutable_cache("ccache") / "compiled-object").exists()
    first.close()
    assert not cache.exists()
    second.close()


def test_declared_build_outputs_do_not_enter_candidate_patch(tmp_path):
    from reviewbench_maintainability.agentic.resources import CURRENT_JOB
    from reviewbench_maintainability.agentic.snapshots import collect_patch, copy_source
    base, work = tmp_path / "base", tmp_path / "work"
    base.mkdir()
    (base / "source.py").write_text("VALUE = 1\n")
    copy_source(base, work)
    (work / "source.py").write_text("VALUE = 2\n")
    (work / "build-oracle").mkdir()
    (work / "build-oracle/object.o").write_bytes(b"compiled, not source")
    (work / "build-oracle/toolchain-link").symlink_to("/usr/lib")
    token = CURRENT_JOB.set({"scratch_root": str(tmp_path),
                            "workspace_exclusions": {str(work.resolve()): ["build-oracle"]}})
    try:
        result = collect_patch(base, work, tmp_path / "candidate.patch")
    finally:
        CURRENT_JOB.reset(token)
    assert result["changed_files"] == ["source.py"]


def test_whitespace_tolerance_is_explicit_for_reference_patches(tmp_path):
    from reviewbench_maintainability.agentic.snapshots import apply_patch, copy_source
    base = tmp_path / "base"
    base.mkdir()
    (base / "source.py").write_bytes(b"x = 1\r\ny = 2\r\n")
    initialize_git(base)
    (base / "source.py").write_bytes(b"x = 1\r\ny = 3\r\n")
    patch = tmp_path / "frozen-reference.patch"
    patch.write_bytes(git(base, "diff", "--binary", "--full-index").replace(b"\r\n", b"\n"))
    git(base, "checkout", "--", "source.py")
    with pytest.raises(ContractError):
        apply_patch(base, patch, tmp_path / "strict")
    apply_patch(base, patch, tmp_path / "reference", ignore_space_change=True)
    assert "y = 3" in (tmp_path / "reference/source.py").read_text()
