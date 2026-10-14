from dataclasses import asdict
import io
from pathlib import Path
import tarfile

import pytest

from reviewbench_maintainability.agentic.archives import archive_stream, extract_archive
from reviewbench_maintainability.agentic.contracts import ContractError
from reviewbench_maintainability.agentic.snapshots import extract_start, git, initialize_git, start_archive, tree_hash
from reviewbench_maintainability.agentic.storage import DirectoryObjectStore, LocalObjectCache


def source_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".gitattributes").write_text("secret.txt export-ignore\n*.txt export-subst\n")
    (repo / "secret.txt").write_text("must be present at exact start\n")
    (repo / "value.txt").write_text("$Format:%H$\n" * 100)
    (repo / "executable").write_text("#!/bin/sh\nprintf hello\n")
    (repo / "executable").chmod(0o755)
    (repo / "link").symlink_to("value.txt")
    import random
    (repo / "data").write_bytes(random.Random(42).randbytes(40000))
    initialize_git(repo)
    return repo, git(repo, "rev-parse", "HEAD").decode().strip()


def test_exact_archive_disables_distribution_attribute_rewrites(tmp_path):
    repo, sha = source_repo(tmp_path)
    original_attributes = (repo / ".gitattributes").read_bytes()
    result = tmp_path / "result"
    assert extract_start(repo, sha, result) == tree_hash(repo)
    assert (result / "secret.txt").exists()
    assert (result / "value.txt").read_text().startswith("$Format:%H$")
    assert (result / "executable").stat().st_mode & 0o111
    assert (result / "link").is_symlink()
    assert (repo / ".gitattributes").read_bytes() == original_attributes


def test_chunked_roundtrip_and_lru_smaller_than_archive(tmp_path):
    repo, sha = source_repo(tmp_path)
    store = DirectoryObjectStore(tmp_path / "remote")
    cache = LocalObjectCache(tmp_path / "cache", store, max_bytes=16384, min_free_bytes=0)
    with start_archive(repo, sha) as source:
        archive, receipts = archive_stream(source, cache, tmp_path / "scratch", part_bytes=8192)
    assert len(archive["parts"]) > 2
    assert len(receipts) == len(archive["parts"])
    assert all(item["size_bytes"] <= 8192 for item in archive["parts"])
    assert not list((tmp_path / "scratch").iterdir())
    extract_archive(archive, cache, tmp_path / "restored")
    assert tree_hash(tmp_path / "restored") == tree_hash(repo)
    assert cache.status()["bytes"] <= 16384
    # A second encoding is byte-for-byte deterministic and reuses CAS objects.
    with start_archive(repo, sha) as source:
        again, _ = archive_stream(source, cache, tmp_path / "scratch", part_bytes=8192)
    assert again == archive


def test_expansion_limit_removes_only_the_new_destination(tmp_path):
    repo, sha = source_repo(tmp_path)
    store = DirectoryObjectStore(tmp_path / "remote")
    cache = LocalObjectCache(tmp_path / "cache", store, min_free_bytes=0)
    with start_archive(repo, sha) as source:
        archive, _ = archive_stream(source, cache, tmp_path / "scratch")
    archive["input_bytes"] = 1024
    with pytest.raises((ContractError, tarfile.ReadError)):
        extract_archive(archive, cache, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()
    assert (repo / "secret.txt").is_file()


@pytest.mark.parametrize("name,link", [("../escape", ""), ("dir/file", "../../escape"),
                                    (".git/config", "")])
def test_streaming_tar_still_rejects_escape(tmp_path, name, link):
    from reviewbench_maintainability.agentic.snapshots import extract_tar
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as tar:
        item = tarfile.TarInfo(name)
        if link:
            item.type, item.linkname = tarfile.SYMTYPE, link
        tar.addfile(item)
    stream.seek(0)
    with pytest.raises(ContractError):
        extract_tar(stream, tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_gitlinks_are_not_silently_treated_as_complete_sources(tmp_path):
    repo, sha = source_repo(tmp_path)
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{sha},dependency")
    git(repo, "commit", "--quiet", "-m", "add pinned module")
    with pytest.raises(ContractError, match="pinned submodule"):
        extract_start(repo, git(repo, "rev-parse", "HEAD").decode().strip(), tmp_path / "output")
