"""Synthetic Git/configuration fixtures, not task execution proofs."""
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import native_kokkos as adapter, native_lane
from agentic_runtime_migration.native_kokkos_prepare import source_snapshot
from reviewbench_maintainability.agentic.contracts import ContractError, file_hash
from reviewbench_maintainability.agentic.snapshots import git, gitlinks, tree_files
from test_agentic_native_dependency_closure import context, oracle, repository


def original_cache():
    values = {
        "BUILD_SHARED_LIBS": "OFF", "BUILD_TESTING": "ON", "CMAKE_BUILD_TYPE": "Debug",
        "CMAKE_CXX_COMPILER": "/usr/bin/g++-14", "CMAKE_CXX_STANDARD": "20",
        "Kokkos_ENABLE_SERIAL": "ON", "Kokkos_ENABLE_TESTS": "OFF",
        "Kokkos_ENABLE_EXAMPLES": "OFF", "Kokkos_ENABLE_BENCHMARKS": "OFF",
        "Kokkos_ENABLE_CUDA": "OFF", "Kokkos_ENABLE_HIP": "OFF", "Kokkos_ENABLE_SYCL": "OFF",
        "Kokkos_ENABLE_OPENMP": "OFF", "Kokkos_ENABLE_OPENACC": "OFF",
        "Kokkos_ENABLE_THREADS": "OFF", "Kokkos_ENABLE_HPX": "OFF", "Kokkos_ARCH_NATIVE": "OFF",
        "CMAKE_CXX_FLAGS": "", "CMAKE_CXX_FLAGS_DEBUG": "-g",
    }
    return "".join(f"{key}:{'BOOL' if value in ('ON', 'OFF') else 'STRING'}={value}\n"
                   for key, value in values.items()) + "CMAKE_CXX_COMPILER-ADVANCED:INTERNAL=1\n"


def test_exact_cache_preserves_testing_backend_and_compiler_flags():
    options = adapter.build_options(adapter.cache_values(original_cache()))
    command = adapter.configure_argv(options)
    assert "-DBUILD_TESTING:BOOL=ON" in command
    assert "-DKokkos_ENABLE_TESTS:BOOL=OFF" in command  # Original dependency setting, not test removal.
    assert "-DKokkos_ENABLE_SERIAL:BOOL=ON" in command
    assert "-DCMAKE_CXX_COMPILER:STRING=/usr/bin/g++-14" in command
    assert "-DCMAKE_CXX_FLAGS:STRING=" in command
    assert "-DCMAKE_CXX_FLAGS_DEBUG:STRING=-g" in command
    assert not any("--coverage" in item for item in command)  # Coverage is in the original task wrapper only.
    assert "CMAKE_CXX_COMPILER-ADVANCED" not in options


@pytest.mark.parametrize("key,value", [
    ("BUILD_TESTING", "OFF"), ("Kokkos_ENABLE_CUDA", "ON"), ("Kokkos_ENABLE_SERIAL", "OFF"),
    ("Kokkos_ENABLE_TESTS", "ON"), ("Kokkos_ENABLE_OPENMP", "ON"),
    ("CMAKE_CXX_COMPILER", "/usr/bin/g++"), ("CMAKE_CXX_STANDARD", "17"),
    ("Kokkos_ARCH_NATIVE", "ON"), ("CMAKE_CXX_FLAGS", "$PRIVATE"),
])
def test_changed_original_configuration_is_rejected(key, value):
    values = adapter.cache_values(original_cache())
    values[key]["value"] = value
    with pytest.raises(ContractError):
        adapter.build_options(values)


def test_duplicate_cache_entries_rejected():
    with pytest.raises(ContractError, match="duplicate"):
        adapter.cache_values("BUILD_TESTING:BOOL=ON\nBUILD_TESTING:BOOL=OFF\n")


def test_cache_boolean_normalization_does_not_hide_real_flag_changes():
    expected = adapter.build_options(adapter.cache_values(original_cache()))
    actual = json.loads(json.dumps(expected))
    actual["BUILD_TESTING"]["value"] = "TRUE"
    assert adapter.same_options(expected, actual)
    actual["BUILD_TESTING"]["value"] = "OFF"
    assert not adapter.same_options(expected, actual)
    actual = json.loads(json.dumps(expected))
    actual["CMAKE_CXX_FLAGS"]["value"] = "-O3"
    assert not adapter.same_options(expected, actual)


def test_snapshot_uses_real_pinned_objects_and_no_dirty_or_compiled_files(tmp_path):
    repo = tmp_path / "original"
    pin = repository(repo, {"header.hpp": "// exact public bytes\n", "tests/public.cpp": "// tests\n",
                            ".gitattributes": "tests export-ignore\n"})
    (repo / "header.hpp").write_text("// dirty original checkout\n")
    (repo / "old.a").write_bytes(b"old compilation")
    before = {str(p.relative_to(repo / ".git")): file_hash(p)
              for p in (repo / ".git").rglob("*") if p.is_file()}
    target = tmp_path / "fresh"
    result = source_snapshot(repo, pin, target)
    assert git(target, "rev-parse", "HEAD").decode().strip() == pin
    assert git(target, "rev-parse", "HEAD^{tree}").decode().strip() == result["git_tree"]
    assert (target / "header.hpp").read_text() == "// exact public bytes\n"
    assert (target / "tests/public.cpp").is_file()
    assert not (target / "old.a").exists()
    assert not git(target, "remote")
    assert (target / ".git/shallow").read_text().strip() == pin
    assert all(".git" not in Path(name).parts for name in tree_files(target))
    assert before == {str(p.relative_to(repo / ".git")): file_hash(p)
                      for p in (repo / ".git").rglob("*") if p.is_file()}


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    dependency = tmp_path / "stdexec"
    pin = repository(dependency, {"include/stdexec/execution.hpp": "// public dependency fixture\n"})
    root = tmp_path / "candidate"
    files = {header: "// public candidate header\n" for header in adapter.HEADERS}
    files[".gitmodules"] = ('[submodule "external/stdexec"]\n path = external/stdexec\n'
                           ' url = https://github.com/NVIDIA/stdexec\n')
    sha = repository(root, files, [("external/stdexec", pin)])
    plan = {
        "repo": "uliegecsm/kokkos-execution", "input_index": 126, "docker_image": "fixture-native",
        "build_command": "set -e; git submodule update --init external/stdexec; "
                         "rm -rf oracle-kokkos oracle-kokkos-cmake; mkdir oracle-kokkos-cmake; "
                         "cp '{root}/harnesses/CMakeLists.kokkos126.txt' oracle-kokkos-cmake/CMakeLists.txt; "
                         "cmake -S oracle-kokkos-cmake -B oracle-kokkos -GNinja -DCMAKE_BUILD_TYPE=Debug "
                         "-DCMAKE_CXX_COMPILER=g++-14 -DCMAKE_PREFIX_PATH='" + adapter.OLD_PREFIX + "' "
                         "-DORACLE_SOURCE='{root}/harnesses/kokkos_execution_nested.cpp' -DPRODUCTION_ROOT=$PWD; "
                         "cmake --build oracle-kokkos -j2",
        "test_command": "set -e; out=$(./oracle-kokkos/kokkos_execution_oracle); printf '%s\\n' \"$out\"; "
                        "test \"$out\" = '16 48'",
    }
    modules = [{**link, "omit": True, "reason": "fixture"} for link in gitlinks(root, sha)]
    ctx = context(tmp_path, root, plan, modules)
    ctx.config = {"migration": {"native_submodule_repositories": {
        plan["repo"]: {"external/stdexec": str(dependency)}}}}
    cmake = ctx.legacy / "native/harnesses/CMakeLists.kokkos126.txt"
    cmake.write_text("# private synthetic CMake program\n")
    (ctx.legacy / "native/harnesses/kokkos_execution_nested.cpp").write_text("PRIVATE_ASSERTION_BODY\n")
    monkeypatch.setattr(adapter, "HARNESS_CMAKE_SHA256", file_hash(cmake))
    fixture_receipt = {"install_asset": {"path": "/fixture/fresh-install.json", "sha256": "d" * 64},
                       "install_tree_sha256": "e" * 64}
    monkeypatch.setattr(adapter, "load_dependency",
                        lambda *args: (fixture_receipt, {"path": "/fixture/synthetic-receipt.json",
                                                       "sha256": "f" * 64}))
    task = {"id": "synthetic-kokkos-fixture", "repo": plan["repo"], "start_sha": sha}
    return ctx, task, plan


def test_adapter_preserves_full_grader_and_keeps_private_assertions_private(fixture):
    ctx, task, plan = fixture
    original = dict(plan)
    recipe = native_lane.migrate(ctx, task, oracle(plan))
    assert plan == original
    assert recipe.grading["commands"][0]["command"] == plan["test_command"]
    assert recipe.grading["commands"][0]["expected_tests"] == ["fixture assertion"]
    assert recipe.dev_checks == ["cmake --build " + adapter.BUILD + " --target kokkos_execution_public -j2"]
    assert adapter.INSTALL in recipe.grading["bootstrap"][-1]
    assert "/cache/kokkos-" not in recipe.grading["bootstrap"][-1]
    assert "kokkos_execution_nested.cpp" in recipe.grading["bootstrap"][-1]
    assert "-DCMAKE_CXX_COMPILER=g++-14" in recipe.bootstrap[-1]
    assert recipe.submodules[0].get("asset") and not recipe.submodules[0].get("omit")
    public = next(item for item in ctx.bundles if item["role"] == "dependency")
    assert b"KOKKOS_IMPL_PUBLIC_INCLUDE" in public["files"]["CMakeLists.txt"]
    assert b"cxx_std_20" in public["files"]["CMakeLists.txt"]
    assert all(b"PRIVATE_ASSERTION_BODY" not in data for data in public["files"].values())
    assert "main(" not in public["files"]["public.cpp"].decode()
    assert all(header in public["files"]["public.cpp"].decode() for header in adapter.HEADERS)
    assert any(asset["visibility"] == "grader" for asset in recipe.assets)


@pytest.mark.parametrize("change", [
    lambda value: value.replace("-j2", "-j1"),
    lambda value: value.replace("g++-14", "g++-13"),
    lambda value: value + "; touch source.hpp",
    lambda value: value.replace("CMakeLists.kokkos126.txt", "other.txt"),
    lambda value: value.replace("-DCMAKE_BUILD_TYPE=Debug", "-DCMAKE_BUILD_TYPE=Release"),
])
def test_changed_build_prefix_fails_closed(fixture, change):
    ctx, task, plan = fixture
    plan["build_command"] = change(plan["build_command"])
    (ctx.legacy / "native/plans.jsonl").write_text(json.dumps(plan) + "\n")
    with pytest.raises(ContractError, match="complete original build"):
        native_lane.migrate(ctx, task, oracle(plan))


def test_pinned_preparation_rejects_changed_file(tmp_path):
    path = tmp_path / "receipt.json"
    path.write_text("{}")
    reference = {"path": str(path), "sha256": file_hash(path)}
    assert adapter._pinned_json(reference) == {}
    path.write_text('{"changed":true}')
    with pytest.raises(ContractError, match="missing or changed"):
        adapter._pinned_json(reference)


def test_transaction(fixture):
    ctx, task, plan = fixture
    lane = native_lane
    if location := os.environ.get("KOKKOS_NATIVE_LANE_FILE"):
        spec = importlib.util.spec_from_file_location("agentic_runtime_migration.kokkos_transaction_lane", location)
        lane = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lane)
    if os.environ.get("KOKKOS_EXPECT", "configured") == "blocked":
        with pytest.raises(ContractError, match="curated dependency-cache bundle") as result:
            lane.migrate(ctx, task, oracle(plan))
        print("STATE=blocked; RESULT=" + str(result.value))
    else:
        recipe = lane.migrate(ctx, task, oracle(plan))
        assert recipe.grading["commands"][0]["command"] == plan["test_command"]
        assert adapter.INSTALL in recipe.grading["bootstrap"][-1]
        print("STATE=configured; FULL_GRADER_PRESERVED=1; PRIVATE_ASSERTIONS_EXPOSED=0")
