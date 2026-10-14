import json
from pathlib import Path

import pytest

from reviewbench_maintainability.agentic.contracts import ContractError
from reviewbench_maintainability.agentic.resources import ResourceGate, ResourceLimits
from reviewbench_maintainability.agentic.snapshots import extract_start, git, initialize_git, tree_hash
from reviewbench_maintainability.agentic.sources import SourceCatalog, stage_sources
from reviewbench_maintainability.agentic.storage import DirectoryObjectStore, LocalObjectCache, MiB


def test_all_tasks_share_exact_start_archives_and_resume_without_new_transfers(tmp_path):
    cache_root = tmp_path / "repos"
    repo = cache_root / "fixture__repo"
    repo.mkdir(parents=True)
    (repo / "source.py").write_text("VALUE = 42\n")
    initialize_git(repo)
    sha = git(repo, "rev-parse", "HEAD").decode().strip()
    tasks = {f"t-{n}": {"id": f"t-{n}", "repo": "fixture/repo", "start_sha": sha} for n in range(3)}
    remote = DirectoryObjectStore(tmp_path / "remote")
    cache = LocalObjectCache(tmp_path / "cache", remote, max_bytes=MiB, min_free_bytes=0)
    limits = ResourceLimits(min_free_disk_bytes=0, min_available_memory_bytes=0)
    gate = ResourceGate(tmp_path / "gate", tmp_path / "scratch", limits)
    out = tmp_path / "out"
    first = stage_sources(tasks, cache_root, cache, tmp_path / "scratch", out, gate, workers=3)
    assert first["expected_tasks"] == 3 and first["archived_snapshots"] == 1 and first["complete"]
    assert first["runtime_ready"] is False
    uploads = remote.stats["uploads"]
    again = stage_sources(tasks, cache_root, cache, tmp_path / "scratch", out, gate, workers=3)
    assert again["catalog_sha256"] == first["catalog_sha256"]
    assert remote.stats["uploads"] == uploads
    catalog = SourceCatalog(out / "source-catalog.json", cache)
    expected = tree_hash(repo)
    repo.rename(tmp_path / "preserved-original")
    assert catalog.extract("fixture/repo", sha, tmp_path / "restored") == expected
    assert not (tmp_path / "restored/.git").exists()
    with pytest.raises(ContractError, match="different"):
        stage_sources({"other": {"id": "other", "repo": "fixture/repo", "start_sha": sha}},
                      cache_root, cache, tmp_path / "scratch", out, gate)


def test_source_catalog_keeps_submodule_gaps_explicit(tmp_path):
    cache_root = tmp_path / "repos"
    repo = cache_root / "fixture__repo"
    repo.mkdir(parents=True)
    (repo / "source.py").write_text("VALUE = 42\n")
    initialize_git(repo)
    sha = git(repo, "rev-parse", "HEAD").decode().strip()
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{sha},dependency")
    git(repo, "commit", "--quiet", "-m", "pin")
    sha = git(repo, "rev-parse", "HEAD").decode().strip()
    tasks = {"t": {"id": "t", "repo": "fixture/repo", "start_sha": sha}}
    cache = LocalObjectCache(tmp_path / "cache", DirectoryObjectStore(tmp_path / "remote"), min_free_bytes=0)
    gate = ResourceGate(tmp_path / "gate", tmp_path / "scratch",
                        ResourceLimits(min_free_disk_bytes=0, min_available_memory_bytes=0))
    out = tmp_path / "out"
    summary = stage_sources(tasks, cache_root, cache, tmp_path / "scratch", out, gate)
    assert summary["root_only_snapshots"] == 1 and summary["closed_snapshots"] == 0
    with pytest.raises(ContractError, match="root-only"):
        SourceCatalog(out / "source-catalog.json", cache).extract("fixture/repo", sha, tmp_path / "restored")
