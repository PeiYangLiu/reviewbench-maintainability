"""Synthetic Conan caches; none of these fixtures is a runtime-ready proof."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import native_rippled_conan as cache
from agentic_runtime_migration import native_rippled_entry as entry


RREV, PREV, PKGID = "a" * 32, "b" * 32, "c" * 40


def modern_log():
    return (
        "Requirements\n    zlib/1.2.13#" + RREV + " - Cache\n"
        "======== Computing necessary packages ========\n"
        "zlib/1.2.13: Main binary package '" + "d" * 40 + "' missing\n"
        "Requirements\n    zlib/1.2.13#" + RREV + ":" + PKGID + " - Build\n"
        "zlib/1.2.13: Full package reference: zlib/1.2.13#" + RREV + ":" + PKGID + "#" + PREV + "\n")


def legacy_log():
    return (
        "zlib/1.2.13: Downloaded recipe revision " + RREV + "\n"
        "Packages\n    zlib/1.2.13:" + PKGID + " - Build\n"
        "zlib/1.2.13: Created package revision " + PREV + "\n")


@pytest.mark.parametrize("major,log", [(1, legacy_log()), (2, modern_log())])
def test_graph_uses_completed_exact_package_not_missing_binary_guess(major, log):
    graph = cache.graph_from_log(log, major)
    assert graph["packages"] == [{"ref": "zlib/1.2.13", "rrev": RREV, "package_id": PKGID,
                                  "prev": PREV, "contexts": ["host"]}]


@pytest.mark.parametrize("change", [
    lambda text: text.replace("zlib/1.2.13", "xrpl/2.0.1"),
    lambda text: text.replace("zlib/1.2.13", "rippled/2.0.1"),
    lambda text: text.replace("zlib/1.2.13", "unreviewed/1.0"),
    lambda text: text.split("zlib/1.2.13: Full")[0],
    lambda text: text + "Requirements\n    zlib/1.2.13#" + "e" * 32 + " - Cache\n",
    lambda text: text + "zlib/1.2.13: Full package reference: zlib/1.2.13#" + RREV + ":" + PKGID + "#" + "e" * 32 + "\n",
])
def test_incomplete_ambiguous_or_candidate_package_graph_is_rejected(change):
    with pytest.raises(ValueError):
        cache.graph_from_log(change(modern_log()), 2)


def test_host_build_and_test_contexts_are_not_lost():
    text = modern_log()
    for heading in ("Test requirements", "Build requirements"):
        text += heading + "\n    zlib/1.2.13#" + RREV + ":" + PKGID + "#" + PREV + " - Cache\n"
    graph = cache.graph_from_log(text, 2)
    assert graph["packages"][0]["contexts"] == ["build", "host", "test"]
    lock = entry.dependency_lock(graph)
    assert lock["requires"] == lock["build_requires"] == ["zlib/1.2.13#" + RREV]


def write_manifest(directory, files, *, missing_exports=()):
    directory.mkdir(parents=True, exist_ok=True)
    sums = {}
    for name, text in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        sums[name] = hashlib.md5(text.encode()).hexdigest()
    for name in missing_exports:
        sums[name] = "a" * 32
    body = "".join(name + ": " + digest + "\n" for name, digest in sorted(sums.items()))
    (directory / "conanmanifest.txt").write_text("123\n" + body)
    return hashlib.md5(body.encode()).hexdigest()


def test_manifest_rejects_timestamp_missing_duplicate_and_escape():
    for text in ("x\nfile: " + RREV, "123\n../file: " + RREV,
                 "123\nfile: " + RREV + "\nfile: " + RREV, "123\nfile: invalid"):
        with pytest.raises(ValueError):
            cache.manifest(text)


def test_payload_hashes_are_bound_and_original_bytes_remain_unchanged(tmp_path):
    original, copied = tmp_path / "original", tmp_path / "copied"
    rev = write_manifest(original, {"include/zlib.h": "public header\n", "lib/libz.a": "dependency fixture\n"})
    before = {str(p.relative_to(original)): cache.sha256(p) for p in original.rglob("*") if p.is_file()}
    records = cache.checked_payload(original, rev)
    cache.copy_payload(records, copied)
    assert before == {str(p.relative_to(copied)): cache.sha256(p) for p in copied.rglob("*") if p.is_file()}
    assert before == {str(p.relative_to(original)): cache.sha256(p) for p in original.rglob("*") if p.is_file()}
    (original / "include/zlib.h").write_text("changed dependency\n")
    with pytest.raises(ValueError, match="differs"):
        cache.checked_payload(original, rev)


def test_unmaterialized_source_export_is_explicit_and_never_applies_to_packages(tmp_path):
    rev = write_manifest(tmp_path, {"conanfile.py": "# external recipe\n"},
                         missing_exports=["export_source/patches/source-only.patch"])
    with pytest.raises(ValueError, match="absent"):
        cache.checked_payload(tmp_path, rev)
    missing = []
    records = cache.checked_payload(tmp_path, rev, missing_exports=missing)
    assert [row["path"] for row in missing] == ["export_source/patches/source-only.patch"]
    assert {row["path"] for row in records} == {"conanfile.py", "conanmanifest.txt"}
    (tmp_path / "conanfile.py").unlink()
    with pytest.raises(ValueError, match="absent"):
        cache.checked_payload(tmp_path, rev, missing_exports=[])


@pytest.mark.parametrize("name", ["old-task.a", "libxrpl.a", "rippled", "golden.patch"])
def test_unmanifested_files_never_leak_into_dependency_asset(tmp_path, name):
    rev = write_manifest(tmp_path, {"include/zlib.h": "header"})
    (tmp_path / name).write_text("foreign old candidate or private data")
    with pytest.raises(ValueError, match="unmanifested"):
        cache.checked_payload(tmp_path, rev)


def test_symlink_cannot_escape_dependency_payload(tmp_path):
    rev = write_manifest(tmp_path, {"header.h": "h"})
    (tmp_path / "outside").symlink_to("/etc")
    with pytest.raises(ValueError, match="escapes"):
        cache.checked_payload(tmp_path, rev)


def synthetic_cache(tmp_path):
    source = tmp_path / "retained"
    rrev = write_manifest(source / "p/zlib-recipe/e", {"conanfile.py": "# public zlib recipe\n"})
    prev = write_manifest(source / "p/b/zlib-binary/p", {
        "conaninfo.txt": "[settings]\nos=Linux\n", "lib/libz.a": "synthetic external archive\n"})
    # A source/build directory is not a dependency installation.
    (source / "p/b/zlib-binary/b").mkdir()
    (source / "p/b/zlib-binary/b/old-task.o").write_text("never transfer")
    db = sqlite3.connect(source / "p/cache.sqlite3")
    db.executescript(
        "CREATE TABLE recipes(reference text,rrev text,path text,timestamp real,lru integer);"
        "CREATE TABLE packages(reference text,rrev text,pkgid text,prev text,path text,timestamp real,build_id text,lru integer);")
    db.execute("INSERT INTO recipes VALUES(?,?,?,?,?)", ("zlib/1.2.13", rrev, "zlib-recipe", 1.0, 1))
    db.execute("INSERT INTO packages VALUES(?,?,?,?,?,?,?,?)",
               ("zlib/1.2.13", rrev, PKGID, prev, "b/zlib-binary", 1.0, None, 1))
    db.commit()
    db.close()
    graph = {"major": 2, "recipes": [{"ref": "zlib/1.2.13", "rrev": rrev}],
             "packages": [{"ref": "zlib/1.2.13", "rrev": rrev, "package_id": PKGID,
                           "prev": prev, "contexts": ["host"]}]}
    return source, graph


@pytest.mark.parametrize("major", [1, 2])
def test_symlink_manifest_uses_pinned_conan_major_semantics(tmp_path, major):
    content, link = "public compiler payload", "protoc-versioned"
    (tmp_path / link).write_text(content)
    (tmp_path / "protoc").symlink_to(link)
    sums = {link: hashlib.md5(content.encode()).hexdigest(),
            "protoc": hashlib.md5((content if major == 1 else link).encode()).hexdigest()}
    body = "".join(name + ": " + digest + "\n" for name, digest in sorted(sums.items()))
    (tmp_path / "conanmanifest.txt").write_text("123\n" + body)
    rev = hashlib.md5(body.encode()).hexdigest()
    records = cache.checked_payload(tmp_path, rev, conan_major=major)
    row = next(row for row in records if row["path"] == "protoc")
    assert row["kind"] == "symlink" and row["sha256"] == hashlib.sha256(link.encode()).hexdigest()
    with pytest.raises(ValueError, match="differs"):
        cache.checked_payload(tmp_path, rev, conan_major=3 - major)


def test_projection_recheck_never_copies_payload(tmp_path, monkeypatch):
    source, graph = synthetic_cache(tmp_path)
    target = tmp_path / "projected"
    cache.modern_cache(source, graph, target)
    before = cache.sha256(target / "p/cache.sqlite3")
    monkeypatch.setattr(cache.shutil, "copyfile", lambda *a, **kw: pytest.fail("unexpected payload copy"))
    audit = cache.modern_cache(target, graph)
    assert audit["database_sha256"] == before == cache.sha256(target / "p/cache.sqlite3")
    assert len(audit["entries"]) == 2


def test_private_cache_does_not_inherit_false_initialized_version_marker(tmp_path):
    source, graph = synthetic_cache(tmp_path)
    target = tmp_path / "projected"
    cache.modern_cache(source, graph, target)
    (target / "version.txt").write_text("2.17.1")
    destination = tmp_path / "private"
    entry.private_cache(target, destination, 2)
    assert not (destination / "version.txt").exists()


def test_sqlite_projection_uses_exact_pins_does_not_modify_lru_or_copy_build(tmp_path):
    source, graph = synthetic_cache(tmp_path)
    before = cache.sha256(source / "p/cache.sqlite3")
    target = tmp_path / "projected"
    result = cache.modern_cache(source, graph, target)
    assert result["database_sha256"] == before == cache.sha256(source / "p/cache.sqlite3")
    assert (target / "p/b/zlib-binary/p/lib/libz.a").read_text() == "synthetic external archive\n"
    assert not (target / "p/b/zlib-binary/b").exists()
    assert all("old-task.o" not in row["path"] for row in result["files"])
    other = tmp_path / "runtime"
    entry.private_cache(target, other, 2)
    assert (other / "p/b/zlib-binary/p").is_symlink()
    assert (other / "p/cache.sqlite3").read_bytes() == (target / "p/cache.sqlite3").read_bytes()


@pytest.mark.parametrize("field,value", [("rrev", "d" * 32), ("package_id", "d" * 40), ("prev", "e" * 32)])
def test_cache_never_uses_latest_or_nearest_revision(tmp_path, field, value):
    source, graph = synthetic_cache(tmp_path)
    graph["packages"][0][field] = value
    with pytest.raises(ValueError, match="exact retained"):
        cache.modern_cache(source, graph, tmp_path / "projected")


def test_legacy_projection_keeps_only_pinned_binary_and_export(tmp_path):
    source, target = tmp_path / "retained", tmp_path / "projected"
    directory = source / "data/zlib/1.2.13/_/_"
    rrev = write_manifest(directory / "export", {"conanfile.py": "# external dependency"})
    prev = write_manifest(directory / "package" / PKGID, {"conaninfo.txt": "[settings]\n", "zlib.h": "header"})
    (directory / "source").mkdir()
    (directory / "source/old.c").write_text("old source and builds not transferred")
    (directory / "metadata.json").write_text(json.dumps({
        "recipe": {"revision": rrev}, "packages": {PKGID: {"revision": prev, "recipe_revision": rrev},
                                                "d" * 40: {"revision": "f" * 32}}}))
    graph = {"major": 1, "recipes": [{"ref": "zlib/1.2.13", "rrev": rrev}],
             "packages": [{"ref": "zlib/1.2.13", "rrev": rrev, "package_id": PKGID,
                           "prev": prev, "contexts": ["host"]}]}
    cache.legacy_cache(source, graph, target)
    dest = target / directory.relative_to(source)
    assert set(json.loads((dest / "metadata.json").read_text())["packages"]) == {PKGID}
    assert not (dest / "source").exists()
    runtime = tmp_path / "runtime"
    entry.private_cache(target, runtime, 1)
    assert (runtime / directory.relative_to(source) / "package" / PKGID).is_symlink()
    before = cache.sha256(dest / "metadata.json")
    audit = cache.legacy_cache(target, graph)
    assert len(audit["entries"]) == 2
    assert before == cache.sha256(dest / "metadata.json")


def test_graph_validation_preserves_every_host_test_pin():
    graph = cache.graph_from_log(modern_log(), 2)
    node = {"ref": "zlib/1.2.13#" + RREV, "recipe": "Cache", "binary": "Cache",
            "package_id": PKGID, "prev": PREV, "context": "host"}
    entry.compare_graph(graph, {"graph": {"nodes": {"1": node}}})
    for field, value in [("ref", "xrpl/1.0#" + RREV), ("prev", "e" * 32),
                         ("binary", "Build"), ("package_id", "d" * 40), ("binary", "Skip")]:
        with pytest.raises(ValueError):
            entry.compare_graph(graph, {"graph": {"nodes": {"1": {**node, field: value}}}})
    with pytest.raises(ValueError, match="omitted"):
        entry.compare_graph(graph, {"graph": {"nodes": {}}})


def test_historical_transform_is_the_original_copy_transform_only():
    text = "source_marker = 'untouched'\n  'boost:shared':False,\n" + "\n".join(
        "'" + value + "'" for value in ("protobuf/3.21.9", "lz4/1.9.3", "boost/1.82.0", "sqlite3/3.42.0"))
    transformed = entry.historical_recipe(text)
    assert "'boost/*:shared'" in transformed
    assert "source_marker = 'untouched'" in transformed
    for value in ("protobuf/3.21.12", "lz4/1.9.4", "boost/1.83.0", "sqlite3/3.44.2"):
        assert "'" + value + "'" in transformed
    assert text != transformed and "boost/1.82.0" in text
