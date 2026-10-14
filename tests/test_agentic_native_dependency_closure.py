"""Small synthetic source fixtures, not frozen calibration evidence."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import native_lane
from reviewbench_maintainability.agentic.contracts import ContractError, file_hash
from reviewbench_maintainability.agentic.snapshots import git, gitlinks, tree_files


def repository(path, files, links=()):
    path.mkdir(parents=True)
    git(path, "init", "--quiet")
    for name, text in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    git(path, "add", ".")
    for name, commit in links:
        git(path, "update-index", "--add", "--cacheinfo", "160000", commit, name)
    git(path, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "--quiet", "-m", "synthetic public source")
    return git(path, "rev-parse", "HEAD").decode().strip()


def context(tmp_path, repo, plan, modules=()):
    legacy = tmp_path / "legacy"
    lane = legacy / "native"
    (lane / "harnesses").mkdir(parents=True)
    (lane / "plans.jsonl").write_text(json.dumps(plan) + "\n")
    (lane / "fixture.log").write_text("fixture assertions completed\n")
    output = tmp_path / "output"
    output.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    bundles = []

    def bundle(root, *, role="dependency", git_commit=None):
        files = {name: path.read_bytes() for name, path in tree_files(root).items()}
        record = {"role": role, "git_commit": git_commit, "files": files}
        bundles.append(record)
        descriptor = output / f"bundle-{len(bundles)}.json"
        descriptor.write_text(json.dumps({"role": role, "git_commit": git_commit, "files": sorted(files)}))
        return {"path": str(descriptor), "sha256": file_hash(descriptor)}

    return SimpleNamespace(
        legacy=legacy, output=output, scratch=scratch, config={}, bundles=bundles,
        repo=lambda task: repo, modules=lambda task: modules,
        task_directory=lambda *args: lane,
        image=lambda reference: ("sha256:" + "a" * 64, {"path": "/fixture/image.json", "sha256": "b" * 64}),
        bundle=bundle, immutable_tests=lambda task: ["tests/public.cpp"],
        golden=lambda *args: {"path": "/fixture/patch", "sha256": "c" * 64},
    )


def oracle(plan):
    return {"input_index": plan["input_index"], "runs": {"start": [{"log": "fixture.log"}]},
            "pass_to_pass_tests": ["fixture assertion"]}


@pytest.fixture
def autopas(tmp_path):
    repo = tmp_path / "repo"
    files = {
        "CMakeLists.txt": "cmake_minimum_required(VERSION 3.14)\nproject(AutoPas)\n"
                         "add_subdirectory(src)\nadd_subdirectory(applicationLibrary)\n",
        "src/CMakeLists.txt": "add_subdirectory(autopas)\n",
        "src/autopas/CMakeLists.txt": "add_library(autopas STATIC tuning/AutoTuner.cpp utils/ExceptionHandler.cpp)\n",
        "applicationLibrary/CMakeLists.txt": "add_library(molecularDynamicsLibrary STATIC "
                                             "molecularDynamics/molecularDynamicsLibrary/MoleculeLJ.cpp)\n"
                                             "target_link_libraries(molecularDynamicsLibrary PUBLIC autopas)\n",
        "src/autopas/LogicHandler.h": "// synthetic public header\n",
        "src/autopas/tuning/AutoTuner.cpp": "int fixture_autopas() { return 1; }\n",
        "src/autopas/utils/ExceptionHandler.cpp": "int fixture_exception() { return 2; }\n",
        "applicationLibrary/molecularDynamics/molecularDynamicsLibrary/MoleculeLJ.cpp":
            "int fixture_molecule() { return 3; }\n",
        "tests/public.cpp": "public test bytes\n",
    }
    sha = repository(repo, files)
    command = (
        "set -e; rm -rf oracle-fixture oracle-fixture-cmake; mkdir oracle-fixture-cmake; "
        "cp '{root}/harnesses/CMakeLists.fixture.txt' oracle-fixture-cmake/CMakeLists.txt; "
        "cmake -S oracle-fixture-cmake -B oracle-fixture -GNinja -DCMAKE_BUILD_TYPE=Debug "
        "-DCMAKE_CXX_COMPILER=g++-11 -DAUTOPAS_REPOSITORY=$PWD "
        "-DAUTOPAS_ORACLE_HARNESS='{root}/harnesses/fixture.cpp'; "
        "cmake --build oracle-fixture --target fixture_oracle -j2"
    )
    plan = {"repo": "AutoPas/AutoPas", "input_index": 3, "build_command": command,
            "docker_image": "fixture-native", "test_command": "./oracle-fixture/fixture_oracle"}
    ctx = context(tmp_path, repo, plan)
    helper = ctx.legacy / "native/harnesses/CMakeLists.fixture.txt"
    helper.write_text("""cmake_minimum_required(VERSION 3.14)
project(Fixture LANGUAGES CXX)
set(AUTOPAS_BUILD_TESTS ON CACHE BOOL "" FORCE)
set(AUTOPAS_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(AUTOPAS_FORMATTING_TARGETS OFF CACHE BOOL "" FORCE)
set(AUTOPAS_OPENMP OFF CACHE BOOL "" FORCE)
set(AUTOPAS_MIN_LOG_LVL OFF CACHE STRING "" FORCE)
set(Eigen3_ForceBundled OFF CACHE BOOL "" FORCE)
set(spdlog_ForceBundled ON CACHE BOOL "" FORCE)
add_compile_options(-O0 -g --coverage)
add_link_options(--coverage)
add_subdirectory("${AUTOPAS_REPOSITORY}" autopas)
add_executable(fixture_oracle "${AUTOPAS_ORACLE_HARNESS}")
target_link_libraries(fixture_oracle PRIVATE molecularDynamicsLibrary)
""")
    (helper.parent / "fixture.cpp").write_text("// synthetic private fixture, never a public compile input\n")
    task = {"id": "autopas-fixture", "repo": plan["repo"], "start_sha": sha}
    return ctx, task, plan, helper


def test_autopas_public_build_uses_repository_sources_and_preserves_test_switch(autopas):
    ctx, task, plan, helper = autopas
    original_hash = file_hash(helper)
    recipe = native_lane.migrate(ctx, task, oracle(plan))
    arguments = shlex.split(recipe.bootstrap[-1])
    assert arguments[:5] == ["cmake", "-S", ".", "-B", "build-oracle"]
    assert "-DCMAKE_BUILD_TYPE=Debug" in arguments and "-DCMAKE_CXX_COMPILER=g++-11" in arguments
    assert "-DAUTOPAS_BUILD_TESTS:BOOL=ON" in arguments
    assert "-DAUTOPAS_STANDALONE_BUILD=OFF" in arguments
    assert "-DCMAKE_CXX_FLAGS=-O0 -g --coverage" in arguments
    assert "-DCMAKE_C_FLAGS=-O0 -g --coverage" in arguments
    assert "-DCMAKE_EXE_LINKER_FLAGS=--coverage" in arguments
    assert recipe.dev_checks == ["cmake --build build-oracle --target molecularDynamicsLibrary -j2"]
    public = "\n".join(recipe.bootstrap + recipe.dev_checks)
    assert not any(word in public for word in ("harnesses", "fixture_oracle", "ORACLE_HARNESS", "fixture.cpp"))
    assert recipe.grading["commands"][0]["expected_tests"] == ["fixture assertion"]
    assert recipe.grading["immutable_paths"] == ["tests/public.cpp"]
    assert recipe.grading["bootstrap"][-1] == native_lane.relocate(
        plan["build_command"], {"root": "/workspace/.reviewbench/native"})
    assert all(asset["visibility"] == "grader" for asset in recipe.assets)
    assert file_hash(helper) == original_hash
    record = json.loads(next((ctx.output / "native-public-build").glob("*.json")).read_text())
    assert record["source_sha256"] == original_hash
    assert record["source_changes"] == record["test_changes"] == []
    assert "src/autopas/tuning/AutoTuner.cpp" in record["public_inputs"]


def test_autopas_preserves_the_original_disabled_test_switch(autopas):
    ctx, task, plan, helper = autopas
    helper.write_text(helper.read_text().replace("AUTOPAS_BUILD_TESTS ON", "AUTOPAS_BUILD_TESTS OFF"))
    configure, _ = native_lane.autopas_public_build(ctx, task, plan)
    arguments = shlex.split(configure)
    assert "-DAUTOPAS_BUILD_TESTS:BOOL=OFF" in arguments
    assert "-Dspdlog_ForceBundled:BOOL=ON" in arguments
    assert "-DEigen3_ForceBundled:BOOL=OFF" in arguments
    assert "-DAUTOPAS_MIN_LOG_LVL:STRING=OFF" in arguments


def test_extracted_public_check_really_compiles_and_rejects_a_broken_public_source(autopas):
    # Three tiny synthetic translation units, never a real task calibration.
    if any(shutil.which(tool) is None for tool in ("cmake", "ninja", "g++")):
        pytest.skip("the local synthetic compile fixture needs CMake/Ninja/G++")
    ctx, task, plan, helper = autopas
    plan["build_command"] = plan["build_command"].replace("g++-11", "g++")
    configure, build = native_lane.autopas_public_build(ctx, task, plan)
    shutil.rmtree(helper.parent)
    env = {**os.environ, "CMAKE_BUILD_PARALLEL_LEVEL": "2", "OMP_NUM_THREADS": "1"}

    def run(command):
        return subprocess.run(shlex.split(command), cwd=ctx.repo(task), env=env, timeout=30,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    for command in (configure, build):
        result = run(command)
        assert result.returncode == 0, result.stdout
    root = ctx.repo(task) / "build-oracle"
    assert (root / "src/autopas/libautopas.a").is_file()
    assert (root / "applicationLibrary/libmolecularDynamicsLibrary.a").is_file()
    (ctx.repo(task) / "applicationLibrary/molecularDynamics/molecularDynamicsLibrary/MoleculeLJ.cpp").write_text(
        "fixture_compile_error\n")
    result = run(build)
    assert result.returncode != 0 and "fixture_compile_error" in result.stdout


@pytest.mark.parametrize("change", [
    lambda text: text + 'include("/private/assertions.cmake")\n',
    lambda text: text.replace('ON CACHE BOOL "" FORCE', '${PRIVATE_SWITCH} CACHE BOOL "" FORCE', 1),
    lambda text: text.replace("set(AUTOPAS_BUILD_TESTS ON CACHE BOOL \"\" FORCE)\n", ""),
    lambda text: text.replace("-O0 -g --coverage", "-O0 -include /private/assertions.h"),
    lambda text: text.replace("molecularDynamicsLibrary)", "private_library)"),
    lambda text: text.replace('"${AUTOPAS_REPOSITORY}"', '"/reference"'),
    lambda text: text + 'set(AUTOPAS_BUILD_TESTS OFF CACHE BOOL "" FORCE)\n',
])
def test_autopas_rejects_unaccounted_private_or_executable_cmake(autopas, change):
    ctx, task, plan, helper = autopas
    helper.write_text(change(helper.read_text()))
    with pytest.raises(ContractError):
        native_lane.migrate(ctx, task, oracle(plan))


@pytest.mark.parametrize("change", [
    lambda text: text.replace("-DCMAKE_BUILD_TYPE=Debug", "-DCMAKE_PROJECT_INCLUDE=/private/helper.cmake"),
    lambda text: text.replace("$PWD", "/reference"),
    lambda text: text + "; cp /reference/source.cpp src/autopas/AutoPas.cpp",
    lambda text: text.replace("-GNinja", "-GNinja -DAUTOPAS_BUILD_TESTS=OFF"),
])
def test_autopas_rejects_unaccounted_shell_or_configure_inputs(autopas, change):
    ctx, task, plan, _ = autopas
    plan["build_command"] = change(plan["build_command"])
    (ctx.legacy / "native/plans.jsonl").write_text(json.dumps(plan) + "\n")
    with pytest.raises(ContractError):
        native_lane.migrate(ctx, task, oracle(plan))


def test_autopas_requires_tracked_production_targets_not_a_directory_name(autopas):
    ctx, task, plan, _ = autopas
    (ctx.repo(task) / "src/autopas/tuning/AutoTuner.cpp").unlink()
    git(ctx.repo(task), "add", "-u")
    git(ctx.repo(task), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "--quiet", "-m", "remove public input")
    task["start_sha"] = git(ctx.repo(task), "rev-parse", "HEAD").decode().strip()
    with pytest.raises(ContractError, match="public.*input"):
        native_lane.migrate(ctx, task, oracle(plan))


@pytest.mark.parametrize("index", [1, 2])
def test_autopas_does_not_admit_other_frozen_api_variants(autopas, index):
    ctx, task, plan, _ = autopas
    plan["input_index"] = index
    plan["build_command"] = 'if test "$ORACLE_VARIANT" = golden; then echo private; fi; ' + plan["build_command"]
    (ctx.legacy / "native/plans.jsonl").write_text(json.dumps(plan) + "\n")
    with pytest.raises(ContractError, match="compatibility adapter"):
        native_lane.migrate(ctx, task, oracle(plan))


@pytest.fixture
def mariadb(tmp_path):
    connector = tmp_path / "connector"
    pin = repository(connector, {
        "CMakeLists.txt": "add_library(mariadbclient STATIC connector.c)\n",
        "connector.c": "int public_connector(void) { return 1; }\n",
        "tests/public.cpp": "unchanged public connector tests\n",
        ".gitattributes": "tests export-ignore\n",
    })
    # Neither uncommitted dependency edits nor export-ignore may affect the asset.
    (connector / "connector.c").write_text("uncommitted bytes must not be bundled\n")
    repo = tmp_path / "repo"
    sha = repository(repo, {"CMakeLists.txt": "# public server build\n"},
                     [("libmariadb", pin), ("extra/wolfssl/wolfssl", "d" * 40)])
    modules = [{**link, "omit": True, "reason": "optional fixture"} for link in gitlinks(repo, sha)]
    plan = {"repo": "MariaDB/server", "input_index": 18, "docker_image": "fixture-native",
            "configure_command": "bash '{root}/harnesses/configure_fixture.sh'",
            "build_command": "cmake --build build-oracle --target mariadb-binlog -j24",
            "test_command": "bash '{root}/harnesses/run_fixture.sh'"}
    ctx = context(tmp_path, repo, plan, modules)
    ctx.config = {"migration": {"native_submodule_repositories": {"MariaDB/server": {"libmariadb": str(connector)}}}}
    (ctx.legacy / "native/harnesses/configure_fixture.sh").write_text(
        "#!/bin/bash\nset -euo pipefail\ncmake -S . -B build-oracle -GNinja "
        "-DWITHOUT_SERVER=ON -DWITH_UNIT_TESTS=ON -DWITH_SSL=system -DWITH_ZLIB=system -DWITH_PCRE=system\n")
    task = {"id": "mariadb-fixture", "repo": plan["repo"], "start_sha": sha}
    return ctx, task, plan, connector, modules


def test_mariadb_materializes_only_pinned_connector_sources(mariadb):
    ctx, task, plan, connector, original_modules = mariadb
    recipe = native_lane.migrate(ctx, task, oracle(plan))
    modules = {item["path"]: item for item in recipe.submodules}
    assert modules["libmariadb"].get("omit") is not True
    assert modules["libmariadb"]["asset"]["sha256"]
    assert modules["extra/wolfssl/wolfssl"]["omit"] is True
    assert all(item["omit"] is True for item in original_modules)
    source = next(bundle for bundle in ctx.bundles if bundle["role"] == "submodule")
    assert source["git_commit"] == modules["libmariadb"]["commit"]
    assert source["files"]["connector.c"] == b"int public_connector(void) { return 1; }\n"
    assert source["files"]["tests/public.cpp"] == b"unchanged public connector tests\n"
    assert not any(".git" in Path(name).parts for name in source["files"])
    assert (connector / "connector.c").read_text() == "uncommitted bytes must not be bundled\n"
    configure = native_lane.public_configure_command(
        (ctx.legacy / "native/harnesses/configure_fixture.sh").read_text())
    assert recipe.bootstrap[-1] == configure
    assert recipe.bootstrap[-2] == "git config --local cmake.update-submodules no"
    assert "-DWITH_UNIT_TESTS=ON" in shlex.split(configure)
    assert recipe.dev_checks == ["cmake --build build-oracle --target mariadb-binlog -j4"]
    assert recipe.grading["commands"][0]["expected_tests"] == ["fixture assertion"]
    assert all(asset["visibility"] == "grader" for asset in recipe.assets)


def test_mariadb_missing_connector_is_an_explicit_dependency_blocker(mariadb):
    ctx, task, plan, _, _ = mariadb
    ctx.config = {}
    with pytest.raises(ContractError, match="libmariadb.*native_submodule_repositories"):
        native_lane.migrate(ctx, task, oracle(plan))


def test_mariadb_rejects_a_catalog_pin_different_from_the_source_gitlink(mariadb):
    ctx, task, plan, _, modules = mariadb
    next(item for item in modules if item["path"] == "libmariadb")["commit"] = "e" * 40
    with pytest.raises(ContractError, match="Git pin"):
        native_lane.migrate(ctx, task, oracle(plan))


def test_mariadb_requires_the_exact_dependency_commit_in_the_local_repository(mariadb, tmp_path):
    ctx, task, plan, _, _ = mariadb
    other = tmp_path / "other"
    repository(other, {"CMakeLists.txt": "unrelated public dependency\n"})
    ctx.config["migration"]["native_submodule_repositories"]["MariaDB/server"]["libmariadb"] = str(other)
    with pytest.raises(ContractError, match="git"):
        native_lane.migrate(ctx, task, oracle(plan))


def test_mariadb_rejects_unclosed_nested_dependency_gitlinks(mariadb):
    ctx, task, plan, connector, modules = mariadb
    git(connector, "update-index", "--add", "--cacheinfo", "160000", "f" * 40, "nested")
    git(connector, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "--quiet", "-m", "nested dependency")
    pin = git(connector, "rev-parse", "HEAD").decode().strip()
    git(ctx.repo(task), "update-index", "--cacheinfo", "160000", pin, "libmariadb")
    git(ctx.repo(task), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "--quiet", "-m", "new dependency pin")
    task["start_sha"] = git(ctx.repo(task), "rev-parse", "HEAD").decode().strip()
    next(item for item in modules if item["path"] == "libmariadb")["commit"] = pin
    with pytest.raises(ContractError, match="nested"):
        native_lane.migrate(ctx, task, oracle(plan))
