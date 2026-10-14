from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import native_ripsim as subject


def declared(url=None, timestamp=False):
    return ("FetchContent_Declare(googletest URL " + (url or subject.URL)
            + (" DOWNLOAD_EXTRACT_TIMESTAMP true" if timestamp else "") + ")\n")


def inputs(tmp_path):
    workspace = tmp_path / "workspace"
    source = tmp_path / "original-google"
    workspace.mkdir()
    source.mkdir()
    (workspace / "CMakeLists.txt").write_text(declared() + declared(timestamp=True))
    (source / "CMakeLists.txt").write_text("project(original_googletest)\n")
    (source / "original-header.h").write_text("original dependency bytes\n")
    spec = {"schema_version": subject.SCHEMA, "asset_sha256": "a" * 64,
            "git_commit": subject.COMMIT, "source": str(source)}
    return workspace, source, spec


def test_both_original_cmake_version_branches_keep_identical_pin():
    assert len(subject.declarations(declared() + declared(timestamp=True))) == 2


@pytest.mark.parametrize("bad", [
    "", "FetchContent_Declare(googletest GIT_REPOSITORY x GIT_TAG main)",
    declared("https://example.invalid/other.zip"),
    declared() + declared("https://example.invalid/other.zip"),
    declared() * 3,
])
def test_unbound_or_ambiguous_source_declarations_rejected(bad):
    with pytest.raises(ValueError):
        subject.declarations(bad)


def test_public_and_grader_use_separate_exact_copies_and_original_flags(tmp_path):
    workspace, source, spec = inputs(tmp_path)
    before = {p.name: (p.read_bytes(), p.stat().st_mode) for p in source.iterdir()}
    first = subject.configuration(spec, workspace, "developer", cache_root=tmp_path / "cache")
    second = subject.configuration(spec, workspace, "grader", cache_root=tmp_path / "cache")
    assert first[:5] == ["cmake", "-S", ".", "-B", "build-native-public"]
    assert second[:5] == ["cmake", "-S", ".", "-B", "build-oracle"]
    assert first[5:-1] == second[5:-1] == subject.FLAGS
    assert first[-1] != second[-1]
    assert "ORACLE_VARIANT" not in str(first + second)
    for command in (first, second):
        path = Path(command[-1].split("=", 1)[1])
        assert (path / "original-header.h").read_bytes() == before["original-header.h"][0]
    assert {p.name: (p.read_bytes(), p.stat().st_mode) for p in source.iterdir()} == before
    assert subject.configuration(spec, workspace, "developer", cache_root=tmp_path / "cache") == first


@pytest.mark.parametrize("key,value", [
    ("schema_version", "unknown"),
    ("git_commit", "b" * 40),
    ("asset_sha256", "../not-a-digest"),
    ("source", "relative"),
])
def test_wrong_specs_rejected(tmp_path, key, value):
    workspace, source, spec = inputs(tmp_path)
    spec[key] = value
    with pytest.raises(ValueError):
        subject.configuration(spec, workspace, "developer", cache_root=tmp_path / "cache")


def test_changed_candidate_pin_is_not_silently_overridden(tmp_path):
    workspace, source, spec = inputs(tmp_path)
    (workspace / "CMakeLists.txt").write_text(declared(subject.URL.replace(subject.COMMIT, "b" * 40)))
    with pytest.raises(ValueError):
        subject.configuration(spec, workspace, "developer", cache_root=tmp_path / "cache")


def test_linked_candidate_declaration_rejected(tmp_path):
    workspace, source, spec = inputs(tmp_path)
    path = workspace / "CMakeLists.txt"
    outside = tmp_path / "outside.cmake"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(ValueError):
        subject.configuration(spec, workspace, "developer", cache_root=tmp_path / "cache")


def test_reference_variant_label_cannot_select_a_dependency_mode(tmp_path):
    workspace, source, spec = inputs(tmp_path)
    with pytest.raises(ValueError):
        subject.configuration(spec, workspace, "golden", cache_root=tmp_path / "cache")


def test_unrelated_native_tasks_do_not_acquire_the_profile():
    assert subject.adapt(None, {"repo": "unrelated"}, {"input_index": 23}) is None
