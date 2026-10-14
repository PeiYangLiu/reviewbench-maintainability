from pathlib import Path
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from agentic_runtime_migration.native_fetchcontent import declaration, validated_command, attach, seed_source
from reviewbench_maintainability.agentic.contracts import ContractError

PIN = "1" * 40
URL = "https://github.com/example/dependency.git"
DECL = f"""include(FetchContent)
FetchContent_Declare(
  dependency
  GIT_REPOSITORY {URL}
  GIT_TAG {PIN} # retained source version
  GIT_SHALLOW TRUE
  SYSTEM
)
"""


def inputs(tmp_path):
    root = tmp_path / "candidate"
    source = tmp_path / "dependency"
    (root / "cmake").mkdir(parents=True)
    source.mkdir()
    (root / "cmake/dependencies.cmake").write_text(DECL)
    (source / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.14)\n")
    spec = {"schema_version": "reviewbench-native-fetchcontent/v1",
            "declaration": "cmake/dependencies.cmake",
            "dependencies": {"dependency": {"repository": URL, "commit": PIN, "target": str(source)}}}
    return root, source, spec


def test_complete_literal_declaration_keeps_original_repository_and_pin():
    assert declaration(DECL, "dependency") == {"repository": URL, "commit": PIN}


@pytest.mark.parametrize("change", [
    lambda value: value.replace(PIN, "main"),
    lambda value: value.replace(URL, "file:///private/reference"),
    lambda value: value.replace(PIN, "${candidate_pin}"),
    lambda value: value.replace("GIT_SHALLOW TRUE", "GIT_SHALLOW FALSE"),
    lambda value: value.replace("SYSTEM", "SYSTEM UPDATE_COMMAND do_something"),
    lambda value: value + value,
])
def test_unbound_or_ambiguous_source_declarations_are_rejected(change):
    with pytest.raises(ValueError):
        declaration(change(DECL), "dependency")


def test_configure_adds_only_the_matching_local_source_location(tmp_path):
    root, source, spec = inputs(tmp_path)
    original = ["cmake", "-S", ".", "-B", "build-native-public", "-DWITH_TESTS=ON"]
    assert validated_command(spec, root, original) == [
        *original, "-DFETCHCONTENT_SOURCE_DIR_DEPENDENCY=" + str(source)]
    assert (root / "cmake/dependencies.cmake").read_text() == DECL
    assert original[-1] == "-DWITH_TESTS=ON"


@pytest.mark.parametrize("change", [
    lambda spec: spec["dependencies"]["dependency"].update(commit="2" * 40),
    lambda spec: spec["dependencies"]["dependency"].update(repository="https://github.com/other/project.git"),
    lambda spec: spec.update(declaration="../outside.cmake"),
    lambda spec: spec.update(schema_version="unknown"),
])
def test_runtime_binds_the_current_candidate_declaration_not_a_variant_label(tmp_path, change):
    root, _, spec = inputs(tmp_path)
    change(spec)
    with pytest.raises((ValueError, KeyError)):
        validated_command(spec, root, ["cmake", "-S", ".", "-B", "build-oracle"])


@pytest.mark.parametrize("command", [
    ["cmake", "-S", "/private", "-B", "build-oracle"],
    ["cmake", "-S", ".", "-B", "undeclared-build"],
    ["cmake", "-S", ".", "-S", "/private", "-B", "build-oracle"],
    ["cmake", "-S", ".", "-B", "build-oracle", "-B", "/private"],
    ["cmake", "-S", ".", "-B", "build-oracle", "-DFETCHCONTENT_SOURCE_DIR_DEPENDENCY=/private"],
])
def test_command_cannot_override_dependency_locations_or_candidate_root(tmp_path, command):
    root, _, spec = inputs(tmp_path)
    with pytest.raises(ValueError):
        validated_command(spec, root, command)


def test_missing_or_symlinked_asset_does_not_trigger_network_fallback(tmp_path):
    root, source, spec = inputs(tmp_path)
    original = tmp_path / "real"
    source.rename(original)
    source.symlink_to(original, target_is_directory=True)
    with pytest.raises(ValueError, match="asset is absent"):
        validated_command(spec, root, ["cmake", "-S", ".", "-B", "build-oracle"])


def test_small_real_project_builds_from_bound_source_and_rejects_broken_candidate(tmp_path):
    if not shutil.which("cmake") or not shutil.which("cc"):
        pytest.skip("local CMake and C compiler required")
    root, source, spec = inputs(tmp_path)
    (source / "CMakeLists.txt").write_text("add_library(dependency STATIC library.c)\n")
    (source / "library.c").write_text("int answer(void) { return 7; }\n")
    (root / "main.c").write_text("int answer(void); int main(void) { return answer() != 7; }\n")
    (root / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.14)\nproject(public_fixture C)\n"
        "include(cmake/dependencies.cmake)\nFetchContent_MakeAvailable(dependency)\n"
        "add_executable(public_program main.c)\ntarget_link_libraries(public_program PRIVATE dependency)\n")
    command = validated_command(spec, root, ["cmake", "-S", ".", "-B", "build-native-public"])
    subprocess.run(command, cwd=root, check=True, capture_output=True, timeout=30)
    subprocess.run(["cmake", "--build", "build-native-public", "-j2"], cwd=root,
                   check=True, capture_output=True, timeout=30)
    assert subprocess.run([str(root / "build-native-public/public_program")]).returncode == 0
    (root / "main.c").write_text("not valid C\n")
    assert subprocess.run(["cmake", "--build", "build-native-public", "-j2"], cwd=root,
                          capture_output=True, timeout=30).returncode != 0


def test_attach_preserves_configure_flags_and_checks_archive_commit_binding(tmp_path, monkeypatch):
    from reviewbench_maintainability.agentic import assets, snapshots
    root, source, spec = inputs(tmp_path)
    archive = {"path": str(tmp_path / "archive.json"), "sha256": "a" * 64}
    class Context:
        output = tmp_path / "output"
        config = {"migration": {"native_fetchcontent_assets": {
            "example/repository": {"dependency": {"repository": URL, "commit": PIN, "asset": archive}}}}}
        def repo(self, task):
            return root
        def bundle(self, *args, **kwargs):
            return {"path": str(tmp_path / "tools.json"), "sha256": "b" * 64}
    monkeypatch.setattr(snapshots, "git", lambda *a, **kw: DECL.encode())
    monkeypatch.setattr(assets, "asset_descriptor",
                        lambda *a, **kw: {"source_kind": "dependency", "git_commit": PIN,
                                          "archive": {"input_bytes": 1024**2 + 1}})
    original = {"configure": "cmake -S . -B build-native-public -DWITH_TESTS=ON",
                "build": "cmake --build build-native-public --target library -j4",
                "grading_bootstrap": ["original-configure", "original-private-build"]}
    task = {"id": "task", "repo": "example/repository", "start_sha": "c" * 40}
    result = attach(Context(), task, original)
    assert result["build"] == original["build"]
    assert result["grading_bootstrap"][1:] == ["original-private-build"]
    assert "-B build-native-public" in result["configure"]
    assert "-B build-oracle" in result["grading_bootstrap"][0]
    assert "-DWITH_TESTS=ON" in result["configure"] and "-DWITH_TESTS=ON" in result["grading_bootstrap"][0]
    assert len(result["assets"]) == 3 and all(row["visibility"] == "developer" for row in result["assets"])
    assert result["cache_mounts"] == {"native-fetchcontent": "/opt/reviewbench-cache/native-fetchcontent"}
    assert result["build_scratch_mb_extra"] == 3
    monkeypatch.setattr(assets, "asset_descriptor",
                        lambda *a, **kw: {"source_kind": "dependency", "git_commit": "2" * 40})
    with pytest.raises(ContractError, match="Git source binding"):
        attach(Context(), task, original)


def test_private_source_copies_keep_generated_files_out_of_shared_assets_and_other_modes(tmp_path):
    root, source, spec = inputs(tmp_path)
    spec["private_build_copies"] = True
    spec["dependencies"]["dependency"]["asset_sha256"] = "a" * 64
    cache = tmp_path / "lease-cache"
    public = validated_command(spec, root, ["cmake", "-S", ".", "-B", "build-native-public"], cache_root=cache)
    public_source = Path(public[-1].split("=", 1)[1])
    assert public_source.is_relative_to(cache / "developer")
    (public_source / "generated.cc").write_text("generated during public build")
    assert not (source / "generated.cc").exists()
    assert validated_command(spec, root, ["cmake", "-S", ".", "-B", "build-native-public"],
                             cache_root=cache) == public
    grader = validated_command(spec, root, ["cmake", "-S", ".", "-B", "build-oracle"], cache_root=cache)
    grader_source = Path(grader[-1].split("=", 1)[1])
    assert grader_source.is_relative_to(cache / "grader")
    assert not (grader_source / "generated.cc").exists()
    independent = validated_command(spec, root, ["cmake", "-S", ".", "-B", "build-native-public"],
                                    cache_root=tmp_path / "other-lease")
    assert not (Path(independent[-1].split("=", 1)[1]) / "generated.cc").exists()


def test_real_source_generator_writes_only_the_private_build_copy(tmp_path):
    if not shutil.which("cmake") or not shutil.which("cc"):
        pytest.skip("local CMake and C compiler required")
    root, source, spec = inputs(tmp_path)
    spec["private_build_copies"] = True
    spec["dependencies"]["dependency"]["asset_sha256"] = "a" * 64
    (source / "CMakeLists.txt").write_text(
        'file(WRITE "${CMAKE_CURRENT_SOURCE_DIR}/generated.c" "int answer(void) { return 7; }\\n")\n'
        'add_library(dependency STATIC generated.c)\n')
    (root / "main.c").write_text("int answer(void); int main(void) { return answer() != 7; }\n")
    (root / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.14)\nproject(public_fixture C)\n"
        "include(cmake/dependencies.cmake)\nFetchContent_MakeAvailable(dependency)\n"
        "add_executable(public_program main.c)\ntarget_link_libraries(public_program PRIVATE dependency)\n")
    command = validated_command(spec, root, ["cmake", "-S", ".", "-B", "build-native-public"],
                                cache_root=tmp_path / "private-cache")
    subprocess.run(command, cwd=root, check=True, capture_output=True, timeout=30)
    subprocess.run(["cmake", "--build", "build-native-public", "-j2"], cwd=root,
                   check=True, capture_output=True, timeout=30)
    assert subprocess.run([str(root / "build-native-public/public_program")]).returncode == 0
    assert not (source / "generated.c").exists()
    assert not (root / "generated.c").exists()


def test_private_copy_rejects_incomplete_or_wrong_source_markers(tmp_path):
    _, source, _ = inputs(tmp_path)
    cache = tmp_path / "cache"
    destination = cache / "source"
    destination.mkdir(parents=True)
    with pytest.raises(ValueError, match="incomplete or different"):
        seed_source(source, destination, "a" * 64, cache)
    destination.rmdir()
    seed_source(source, destination, "a" * 64, cache)
    with pytest.raises(ValueError, match="incomplete or different"):
        seed_source(source, destination, "b" * 64, cache)


def test_private_copy_rejects_cache_escape_and_excessive_source_bytes(tmp_path, monkeypatch):
    from agentic_runtime_migration import native_fetchcontent as module
    _, source, _ = inputs(tmp_path)
    cache = tmp_path / "cache"
    with pytest.raises(ValueError, match="escapes"):
        seed_source(source, tmp_path / "outside", "a" * 64, cache)
    monkeypatch.setattr(module, "MAX_SOURCE_BYTES", 1)
    with pytest.raises(ValueError, match="bounded footprint"):
        seed_source(source, cache / "source", "a" * 64, cache)
    assert not (cache / "source").exists()
