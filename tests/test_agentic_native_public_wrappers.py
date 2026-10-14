"""Synthetic fixtures matching the frozen wrapper shapes, not oracle evidence."""
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
from agentic_runtime_migration.native_public_wrappers import FLUTTER_SOURCES, PUBLIC_BUILD, STREAM_HEADER, public_wrapper
from reviewbench_maintainability.agentic.contracts import ContractError, file_hash
from reviewbench_maintainability.agentic.snapshots import git, tree_files


GOOGLE_BUILD = '''#!/usr/bin/env bash
set -euo pipefail
task=$1
definition=
if [[ "${ORACLE_VARIANT}" == start ]]; then
  rm -rf build-oracle
  if [[ "${task}" == 65 ]]; then
    definition=GOOGLE_CLOUD_ORACLE_TASK65_EXACT
  elif [[ "${task}" == 67 ]]; then
    definition=GOOGLE_CLOUD_ORACLE_TASK67_EXACT
  fi
fi
export CCACHE_BASEDIR=$PWD
cmake -S . -B build-oracle -GNinja \\
  -DBUILD_TESTING=OFF \\
  -DGOOGLE_CLOUD_CPP_ENABLE:STRING= \\
  -DGOOGLE_CLOUD_CPP_ENABLE_GRPC=OFF \\
  -DGOOGLE_CLOUD_CPP_ENABLE_REST=OFF \\
  -DGOOGLE_CLOUD_CPP_ENABLE_EXAMPLES=OFF \\
  -DCMAKE_PROJECT_INCLUDE="$(dirname "$0")/google_cloud_resumable_stream.cmake" \\
  -DGOOGLE_CLOUD_ORACLE_HARNESS="$(dirname "$0")/google_cloud_resumable_stream.cpp" \\
  -DGOOGLE_CLOUD_ORACLE_DEFINITION:STRING="${definition}" \\
  >/dev/null
cmake --build build-oracle --target google_cloud_resumable_stream_oracle -j4
'''
GOOGLE_CMAKE = '''function(add_google_cloud_resumable_stream_oracle)
  add_executable(google_cloud_resumable_stream_oracle
                 "${GOOGLE_CLOUD_ORACLE_HARNESS}")
  target_link_libraries(google_cloud_resumable_stream_oracle
                        PRIVATE google-cloud-cpp::common)
  target_compile_options(google_cloud_resumable_stream_oracle
                         PRIVATE -O0 -g --coverage)
  target_link_options(google_cloud_resumable_stream_oracle PRIVATE --coverage)
  if (GOOGLE_CLOUD_ORACLE_DEFINITION)
    target_compile_definitions(google_cloud_resumable_stream_oracle
                               PRIVATE "${GOOGLE_CLOUD_ORACLE_DEFINITION}")
  endif ()
endfunction()
cmake_language(DEFER CALL add_google_cloud_resumable_stream_oracle)
'''
FLUTTER_CONFIGURE = "#!/bin/bash\nset -euo pipefail\nrm -rf build-oracle\nmkdir -p build-oracle/obj\n"
NMOS_CONFIGURE = '''#!/bin/bash
set -euo pipefail
oracle_root=$1
cmake -S "$oracle_root/harnesses/nmos_ocsp" -B build-oracle -GNinja \\
  -DNMOS_SOURCE="$PWD" \\
  -DORACLE_ROOT="$oracle_root" \\
  -DCMAKE_BUILD_TYPE=Debug \\
  -DCMAKE_CXX_COMPILER_LAUNCHER=ccache \\
  -DCMAKE_CXX_FLAGS="--coverage -O0 -g" \\
  -DCMAKE_EXE_LINKER_FLAGS="--coverage"
'''
NMOS_CMAKE = '''cmake_minimum_required(VERSION 3.17)
project(nmos-ocsp-oracle LANGUAGES CXX)
set(NMOS_CPP_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(NMOS_CPP_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(NMOS_CPP_BUILD_LLDP OFF CACHE BOOL "" FORCE)
set(NMOS_CPP_USE_CONAN OFF CACHE BOOL "" FORCE)
add_subdirectory("${NMOS_SOURCE}/Development" nmos-cpp)
add_executable(nmos_ocsp_oracle "${ORACLE_ROOT}/harnesses/nmos_ocsp_oracle.cpp")
target_link_libraries(nmos_ocsp_oracle PRIVATE nmos-cpp::nmos-cpp)
'''
NMOS_BUILD = '''#!/bin/bash
set -euo pipefail
jobs=${CMAKE_BUILD_PARALLEL_LEVEL:-12}
cmake --build build-oracle --target nmos_ocsp_oracle -j"$jobs"
mkdir -p build-oracle/oracle-data
openssl req -x509 -newkey rsa:2048 \\
  -keyout build-oracle/oracle-data/key.pem \\
  -out build-oracle/oracle-data/cert.pem \\
  -sha256 -days 1 -nodes -subj /CN=localhost >/dev/null 2>&1
'''


def flutter_make():
    sources = ["$(HARNESS_DIR)/flutter_tizen_keyboard_oracle.cc", *FLUTTER_SOURCES]
    objects = ["$(OBJ)/" + name + ".o" for name in (
        "oracle", "core_implementations", "standard_codec", "json_message_codec", "key_mapping", "keyboard_channel", "logger")]
    header = """CXX := ccache g++-11
BUILD := build-oracle
OBJ := $(BUILD)/obj
HARNESS_DIR := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
CPPFLAGS := -I. -Iflutter/shell/platform/common/public -I$(HARNESS_DIR)/flutter_tizen_stubs
CXXFLAGS := -std=gnu++17 -O0 -g --coverage -ffunction-sections -fdata-sections -MMD -MP
LDLIBS := --coverage -Wl,--gc-sections -pthread
"""
    header += "SOURCES := \\\n\t" + " \\\n\t".join(sources) + "\n"
    header += "OBJECTS := \\\n\t" + " \\\n\t".join(objects) + "\n"
    header += """all: $(BUILD)/flutter_tizen_keyboard_oracle
$(BUILD)/flutter_tizen_keyboard_oracle: $(OBJECTS)
\t$(CXX) $(OBJECTS) $(LDLIBS) -o $@
"""
    for source, obj in zip(sources, objects):
        header += f"{obj}: {source}\n\t@mkdir -p $(OBJ)\n\t$(CXX) $(CPPFLAGS) $(CXXFLAGS) -c $< -o $@\n"
    return header + "-include $(OBJECTS:.o=.d)\n.PHONY: all\n"


def stream_header(index, modern=False):
    arg = "std::unique_ptr<AsyncStreamingReadWriteRpc<RequestType, ResponseType>>"
    if index == 67 and not modern:
        arg = "std::shared_ptr<" + arg + ">"
    old = index == 65 and not modern
    clock = "std::chrono::duration<double>" if old else "std::chrono::milliseconds"
    return (f"using StreamInitializer = std::function<future<StatusOr<{arg}>>({arg})>;\n"
            f"using AsyncSleeper = std::function<future<void>({clock})>;\n"
            f"virtual future<void> {'Finish' if old else 'Shutdown'}() = 0;\n")


def fixture(tmp_path, family, index):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "--quiet")
    plan = {"input_index": index, "docker_image": "fixture-image", "test_command": "echo fixture-assertions"}
    if family == "google":
        plan.update(repo="googleapis/google-cloud-cpp",
                    build_command=f"bash '{{root}}/harnesses/build_google_cloud_resumable_stream.sh' {index}")
        wrappers = {"build_google_cloud_resumable_stream.sh": GOOGLE_BUILD,
                    "google_cloud_resumable_stream.cmake": GOOGLE_CMAKE,
                    "google_cloud_resumable_stream.cpp": "// synthetic private assertion fixture\n"}
        files = {"CMakeLists.txt": "cmake_minimum_required(VERSION 3.17)\nproject(fixture)\nadd_subdirectory(google/cloud)\n",
                 "google/cloud/CMakeLists.txt": "add_library(google_cloud_cpp_common STATIC "
                                                "internal/future_impl.cc status.cc internal/log_impl.cc)\n",
                 STREAM_HEADER: stream_header(index)}
        for number, name in enumerate(("google/cloud/internal/future_impl.cc", "google/cloud/status.cc", "google/cloud/internal/log_impl.cc")):
            files[name] = f"int fixture_{number}() {{ return {number}; }}\n"
    elif family == "flutter":
        plan.update(repo="flutter-tizen/embedder",
                    configure_command="bash '{root}/harnesses/configure_flutter_tizen_keyboard.sh'",
                    build_command="make -f '{root}/harnesses/flutter_tizen_keyboard.mk' -j2")
        wrappers = {"configure_flutter_tizen_keyboard.sh": FLUTTER_CONFIGURE, "flutter_tizen_keyboard.mk": flutter_make(),
                    "flutter_tizen_keyboard_oracle.cc": "// synthetic private assertion fixture\n",
                    "flutter_tizen_stubs/dlog.h": "#error private header must not enter the public build\n"}
        files = {name: f"int fixture_{number}() {{ return {number}; }}\n" for number, name in enumerate(FLUTTER_SOURCES)}
    else:
        plan.update(repo="sony/nmos-cpp",
                    configure_command="bash '{root}/harnesses/configure_nmos_ocsp.sh' '{root}'",
                    build_command="bash '{root}/harnesses/build_nmos_ocsp.sh'")
        wrappers = {"configure_nmos_ocsp.sh": NMOS_CONFIGURE, "nmos_ocsp/CMakeLists.txt": NMOS_CMAKE,
                    "build_nmos_ocsp.sh": NMOS_BUILD, "nmos_ocsp_oracle.cpp": "// synthetic private assertion fixture\n"}
        files = {
            "Development/CMakeLists.txt": "cmake_minimum_required(VERSION 3.17)\nproject(fixture)\n"
                                          "include(cmake/NmosCppLibraries.cmake)\n",
            "Development/cmake/NmosCppLibraries.cmake":
                "add_library(nmos-cpp STATIC nmos/server_utils.cpp nmos/ocsp_utils.cpp ssl/ssl_utils.cpp)\n",
        }
        for number, name in enumerate(("Development/nmos/server_utils.cpp", "Development/nmos/ocsp_utils.cpp",
                                      "Development/ssl/ssl_utils.cpp")):
            files[name] = f"int fixture_{number}() {{ return {number}; }}\n"
    files["tests/public.cpp"] = "// unchanged public test fixture\n"
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(repo, "add", ".")
    git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--quiet", "-m", "fixture")
    task = {"id": f"{family}-{index}", "repo": plan["repo"],
            "start_sha": git(repo, "rev-parse", "HEAD").decode().strip()}
    legacy = tmp_path / "legacy"
    lane = legacy / "native"
    for name, text in wrappers.items():
        path = lane / "harnesses" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (lane / "plans.jsonl").write_text(json.dumps(plan) + "\n")
    (lane / "fixture.log").write_text("fixture assertions completed\n")
    output, scratch = tmp_path / "output", tmp_path / "scratch"
    output.mkdir()
    scratch.mkdir()
    bundles = []

    def bundle(root, *, role, **kwargs):
        bundles.append((role, {name: path.read_bytes() for name, path in tree_files(root).items()}))
        return {"path": str(output / "fixture-tree.json"), "sha256": "a" * 64}

    ctx = SimpleNamespace(legacy=legacy, output=output, scratch=scratch, repo=lambda task: repo, config={},
                          modules=lambda task: [], bundle=bundle, bundles=bundles,
                          image=lambda name: ("sha256:" + "a" * 64, {"path": "/fixture/image.json", "sha256": "b" * 64}),
                          task_directory=lambda *args: lane, immutable_tests=lambda task: ["tests/public.cpp"],
                          golden=lambda *args: {"path": "/fixture/patch", "sha256": "c" * 64})
    oracle = {"input_index": index, "runs": {"start": [{"log": "fixture.log"}]},
              "pass_to_pass_tests": ["fixed assertion " + str(i) for i in range(3 if family == "flutter" else 1)]}
    return ctx, task, plan, oracle


@pytest.mark.parametrize("family,index", [("google", 65), ("google", 66), ("google", 67), ("flutter", 58)])
def test_actual_wrapper_shapes_reproduce_the_old_prefix_blocker(tmp_path, family, index):
    _, _, plan, _ = fixture(tmp_path, family, index)
    with pytest.raises(ContractError, match="explicit adapter"):
        native_lane.production_prefix(plan["build_command"])


@pytest.mark.parametrize("family,index", [("google", 65), ("google", 66), ("google", 67), ("flutter", 58), ("nmos", 121)])
def test_wrapper_migration_keeps_candidate_sources_and_fixed_grader_assertions(tmp_path, family, index):
    ctx, task, plan, oracle = fixture(tmp_path, family, index)
    originals = {name: path.read_bytes() for name, path in tree_files(ctx.repo(task)).items()}
    wrappers = {name: path.read_bytes() for name, path in tree_files(ctx.legacy / "native/harnesses").items()}
    recipe = native_lane.migrate(ctx, task, oracle)
    assert {name: path.read_bytes() for name, path in tree_files(ctx.repo(task)).items()} == originals
    assert {name: path.read_bytes() for name, path in tree_files(ctx.legacy / "native/harnesses").items()} == wrappers
    assert recipe.grading["commands"][0]["expected_tests"] == oracle["pass_to_pass_tests"]
    assert recipe.grading["immutable_paths"] == ["tests/public.cpp"]
    assert recipe.grading["commands"][0]["command"] == plan["test_command"]
    assert all(asset["visibility"] == "grader" for asset in recipe.assets)
    assert all(role == "grader" for role, _ in ctx.bundles)
    assert PUBLIC_BUILD in recipe.scratch_paths and "build-oracle" in recipe.scratch_paths
    public = "\n".join(recipe.bootstrap + recipe.dev_checks)
    assert all(word not in public for word in ("harnesses", "stubs", "ORACLE_", "CMAKE_PROJECT_INCLUDE"))
    if family == "google":
        assert recipe.dev_checks == [f"cmake --build {PUBLIC_BUILD} --target google_cloud_cpp_common -j4"]
        assert shlex.split(recipe.bootstrap[-1])[6:] == [
            "-DBUILD_TESTING=OFF", "-DGOOGLE_CLOUD_CPP_ENABLE:STRING=", "-DGOOGLE_CLOUD_CPP_ENABLE_GRPC=OFF",
            "-DGOOGLE_CLOUD_CPP_ENABLE_REST=OFF", "-DGOOGLE_CLOUD_CPP_ENABLE_EXAMPLES=OFF"]
        assert "native_variant=$(python3" in recipe.grading["bootstrap"][-1]
        assert recipe.grading["bootstrap"][-1].endswith(
            native_lane.relocate(plan["build_command"], {"root": "/workspace/.reviewbench/native"}))
    elif family == "flutter":
        assert public.count(" -c ") == 6
        assert all(source in public for source in FLUTTER_SOURCES)
        assert "ar rcs " + PUBLIC_BUILD + "/libflutter_keyboard.a" in public
        assert "--coverage -ffunction-sections -fdata-sections -MMD -MP" in public
        assert len(recipe.grading["bootstrap"]) == 3
    else:
        assert recipe.dev_checks == [f"cmake --build {PUBLIC_BUILD} --target nmos-cpp -j4"]
        assert shlex.split(recipe.bootstrap[-1])[:6] == ["cmake", "-S", "Development", "-B", PUBLIC_BUILD, "-GNinja"]
        assert "-DCMAKE_CXX_FLAGS=--coverage -O0 -g" in shlex.split(recipe.bootstrap[-1])
        assert len(recipe.grading["bootstrap"]) == 3
        assert "configure_nmos_ocsp.sh" in recipe.grading["bootstrap"][1]
    record = json.loads(next((ctx.output / "native-public-wrappers").glob("*.json")).read_text())
    assert record["source_changes"] == record["test_changes"] == []
    for name, sha in record["wrapper_sha256"].items():
        assert file_hash(ctx.legacy / "native/harnesses" / name) == sha


@pytest.mark.parametrize("family,index,name,old,new", [
    ("google", 66, "build_google_cloud_resumable_stream.sh", "BUILD_TESTING=OFF", "BUILD_TESTING=ON"),
    ("nmos", 121, "nmos_ocsp/CMakeLists.txt", "NMOS_CPP_BUILD_TESTS OFF", "NMOS_CPP_BUILD_TESTS ON"),
])
def test_public_test_switches_are_preserved_not_forced_off(tmp_path, family, index, name, old, new):
    ctx, task, plan, _ = fixture(tmp_path, family, index)
    path = ctx.legacy / "native/harnesses" / name
    path.write_text(path.read_text().replace(old, new))
    result = public_wrapper(ctx, task, plan, native_lane.public_configure_command)
    expected = "-DBUILD_TESTING=ON" if family == "google" else "-DNMOS_CPP_BUILD_TESTS:BOOL=ON"
    assert expected in shlex.split(result["configure"])


@pytest.mark.parametrize("family,index,name,extra", [
    ("google", 65, "build_google_cloud_resumable_stream.sh", "cp /private/reference.cc google/cloud/status.cc\n"),
    ("google", 66, "google_cloud_resumable_stream.cmake", 'include("/private/assertions.cmake")\n'),
    ("google", 67, "google_cloud_resumable_stream.cmake", "add_compile_options(-include /private/source.h)\n"),
    ("flutter", 58, "flutter_tizen_keyboard.mk", "\tpython3 /private/rewrite.py\n"),
    ("flutter", 58, "flutter_tizen_keyboard.mk", "UNEXPECTED := $(shell touch injected)\n"),
    ("flutter", 58, "configure_flutter_tizen_keyboard.sh", "cp /private/keyboard.cc flutter/shell/platform/tizen/channels/keyboard_channel.cc\n"),
    ("nmos", 121, "configure_nmos_ocsp.sh", "python3 /private/prepare.py\n"),
    ("nmos", 121, "nmos_ocsp/CMakeLists.txt", 'set(NMOS_CPP_BUILD_TESTS OFF CACHE BOOL "" FORCE)\n'),
    ("nmos", 121, "build_nmos_ocsp.sh", "cp /private/source.cpp Development/nmos/server_utils.cpp\n"),
])
def test_additional_private_instructions_reject_the_entire_wrapper(tmp_path, family, index, name, extra):
    ctx, task, plan, _ = fixture(tmp_path, family, index)
    path = ctx.legacy / "native/harnesses" / name
    path.write_text(path.read_text() + extra)
    with pytest.raises(ContractError):
        public_wrapper(ctx, task, plan, native_lane.public_configure_command)


@pytest.mark.parametrize("family,index,name,old,new", [
    ("google", 65, "build_google_cloud_resumable_stream.sh", "-GNinja", "-GNinja -DCMAKE_PROJECT_INCLUDE=/private/extra.cmake"),
    ("flutter", 58, "flutter_tizen_keyboard.mk", "-I.", "-I/private"),
    ("flutter", 58, "flutter_tizen_keyboard.mk", "-std=gnu++17", "-include /private/test.h -std=gnu++17"),
    ("nmos", 121, "nmos_ocsp/CMakeLists.txt", "${NMOS_SOURCE}/Development", "/reference"),
])
def test_replaced_compile_inputs_reject_extraction(tmp_path, family, index, name, old, new):
    ctx, task, plan, _ = fixture(tmp_path, family, index)
    path = ctx.legacy / "native/harnesses" / name
    path.write_text(path.read_text().replace(old, new))
    with pytest.raises(ContractError):
        public_wrapper(ctx, task, plan, native_lane.public_configure_command)


@pytest.mark.parametrize("family,index", [("google", 65), ("flutter", 58), ("nmos", 121)])
def test_missing_public_translation_unit_rejects_extraction(tmp_path, family, index):
    ctx, task, plan, _ = fixture(tmp_path, family, index)
    path = next(ctx.repo(task).rglob("*.cc" if family != "nmos" else "server_utils.cpp"))
    path.unlink()
    git(ctx.repo(task), "add", "-u")
    git(ctx.repo(task), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--quiet", "-m", "missing input")
    task["start_sha"] = git(ctx.repo(task), "rev-parse", "HEAD").decode().strip()
    with pytest.raises(ContractError, match="public compile inputs"):
        public_wrapper(ctx, task, plan, native_lane.public_configure_command)


@pytest.mark.parametrize("index", [65, 66, 67])
def test_google_grader_uses_current_candidate_types_not_variant_labels(tmp_path, index):
    ctx, task, plan, _ = fixture(tmp_path, "google", index)
    result = public_wrapper(ctx, task, plan, native_lane.public_configure_command)
    # A stand-in observes only the selected environment; no original test runs.
    helper = ctx.legacy / "native/harnesses/build_google_cloud_resumable_stream.sh"
    helper.write_text('printf "%s %s\\n" "$ORACLE_VARIANT" "$1"\n')
    command = native_lane.relocate(result["grading_bootstrap"][0], {"root": str(ctx.legacy / "native")})
    for modern in (False, True):
        (ctx.repo(task) / STREAM_HEADER).write_text(stream_header(index, modern))
        run = subprocess.run(["bash", "-c", command], cwd=ctx.repo(task), timeout=10, text=True,
                             capture_output=True, env={**os.environ, "ORACLE_VARIANT": "golden" if not modern else "start"})
        assert run.returncode == 0, run.stderr
        expected = "start" if index in (65, 67) and not modern else "candidate"
        assert run.stdout == f"{expected} {index}\n"
    (ctx.repo(task) / STREAM_HEADER).write_text("using StreamInitializer = Unsupported;\n")
    run = subprocess.run(["bash", "-c", command], cwd=ctx.repo(task), timeout=10, capture_output=True)
    assert run.returncode != 0 and run.stdout == b""


@pytest.mark.parametrize("family,index", [("google", 66), ("flutter", 58), ("nmos", 121)])
def test_public_wrappers_build_real_tiny_current_sources_without_private_files(tmp_path, family, index):
    required = ("g++", "ar") if family == "flutter" else ("cmake", "ninja", "g++")
    if any(shutil.which(tool) is None for tool in required):
        pytest.skip("small public compile fixture needs local build tools")
    ctx, task, plan, _ = fixture(tmp_path, family, index)
    if family == "flutter":
        path = ctx.legacy / "native/harnesses/flutter_tizen_keyboard.mk"
        path.write_text(path.read_text().replace("CXX := ccache g++-11", "CXX := g++"))
    result = public_wrapper(ctx, task, plan, native_lane.public_configure_command)
    shutil.rmtree(ctx.legacy / "native/harnesses")
    env = {**os.environ, "CMAKE_BUILD_PARALLEL_LEVEL": "1", "OMP_NUM_THREADS": "1"}
    for command in (result["configure"], result["build"]):
        run = subprocess.run(["bash", "-c", command], cwd=ctx.repo(task), timeout=30,
                             capture_output=True, text=True, env=env)
        assert run.returncode == 0, run.stdout + run.stderr
    # A successful directory-creation command would miss this current-source error.
    name = {"google": "google/cloud/status.cc", "flutter": FLUTTER_SOURCES[-1],
            "nmos": "Development/nmos/server_utils.cpp"}[family]
    (ctx.repo(task) / name).write_text("fixture_compile_error\n")
    run = subprocess.run(["bash", "-c", result["build"]], cwd=ctx.repo(task), timeout=30, capture_output=True, text=True, env=env)
    assert run.returncode != 0 and "fixture_compile_error" in run.stdout + run.stderr
