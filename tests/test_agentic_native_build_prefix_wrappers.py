"""Synthetic DiskANN/Halide wrapper fixtures; no private corpus or assertions."""
import hashlib
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
from agentic_runtime_migration import native_lane, native_public_wrappers as wrappers
from reviewbench_maintainability.agentic.contracts import ContractError, file_hash
from reviewbench_maintainability.agentic.snapshots import git, tree_files


# The real private corpus program is fingerprinted in the adapter, not copied
# into these tests. This synthetic stand-in must never run in a public check.
CORPUS = 'raise RuntimeError("private corpus entered a public check")'
DISKANN_CONFIGURE = '''#!/usr/bin/env bash
set -euo pipefail
cmake -S . -B build-oracle -GNinja \\
  -DCMAKE_BUILD_TYPE=Debug \\
  -DCMAKE_CXX_COMPILER_LAUNCHER=ccache \\
  -DCMAKE_CXX_FLAGS="--coverage -O0 -g" \\
  -DCMAKE_EXE_LINKER_FLAGS="-L/opt/diskann-mkl-compat --coverage" \\
  -DCMAKE_SHARED_LINKER_FLAGS="-L/opt/diskann-mkl-compat --coverage"
'''
DISKANN_BUILD = '''#!/usr/bin/env bash
set -euo pipefail
oracle_root=$1
jobs=${CMAKE_BUILD_PARALLEL_LEVEL:-12}
export LD_LIBRARY_PATH=/opt/diskann-mkl-compat:/usr/lib/x86_64-linux-gnu
cmake --build build-oracle --target build_disk_index -j"$jobs"
ccache g++ -std=gnu++14 -O0 -g --coverage -fopenmp -march=native \\
  -DUSE_AVX2 -DMKL_ILP64 \\
  -Iinclude -I/usr/include/mkl \\
  "$oracle_root/harnesses/diskann_public_oracle.cpp" \\
  build-oracle/src/libdiskann.a \\
  -L/opt/diskann-mkl-compat -L/usr/lib/x86_64-linux-gnu/mkl \\
  -lmkl_intel_ilp64 -lmkl_intel_thread -lmkl_core -liomp5 \\
  -ltcmalloc -laio -lpthread -lm -ldl \\
  -o build-oracle/diskann_public_oracle
find build-oracle -name '*.gcda' -delete
rm -rf build-oracle/oracle-data
mkdir -p build-oracle/oracle-data
python3 - <<'PY'
''' + CORPUS + '''
PY
build-oracle/tests/build_disk_index \\
  --data_type float \\
  --dist_fn l2 \\
  --data_path build-oracle/oracle-data/vectors.bin \\
  --index_path_prefix build-oracle/oracle-data/index \\
  -R 16 -L 32 -B 0.1 -M 0.5 -T 2
'''
HALIDE_CONFIGURE = '''#!/usr/bin/env bash
set -euo pipefail
cmake -S . -B build-oracle -GNinja \\
  -DCMAKE_BUILD_TYPE=RelWithDebInfo \\
  -DHalide_LLVM_ROOT=/usr/lib/llvm-20 \\
  -DHalide_CCACHE_BUILD=ON \\
  -DBUILD_SHARED_LIBS=OFF \\
  -DWITH_TESTS=OFF \\
  -DWITH_PYTHON_BINDINGS=OFF \\
  -DWITH_TUTORIALS=OFF \\
  -DWITH_UTILS=OFF \\
  -DCMAKE_CXX_FLAGS="--coverage -O0 -g" \\
  -DCMAKE_EXE_LINKER_FLAGS="--coverage"
'''
HALIDE_BUILD = '''#!/usr/bin/env bash
set -euo pipefail
oracle_root=$1
jobs=${CMAKE_BUILD_PARALLEL_LEVEL:-24}
cmake --build build-oracle --target Halide -j"$jobs"
g++-11 --coverage -O0 -g -std=c++17 \\
  -Ibuild-oracle/include \\
  "$oracle_root/harnesses/halide_shuffle_oracle.cpp" \\
  build-oracle/src/libHalide.a \\
  /usr/lib/llvm-20/lib/liblldWasm.a \\
  /usr/lib/llvm-20/lib/liblldCommon.a \\
  /usr/lib/llvm-20/lib/libLLVM.so.1 \\
  -ldl -pthread \\
  -o build-oracle/halide_shuffle_oracle
'''


def prefix_fixture(tmp_path, monkeypatch, family):
    monkeypatch.setattr(wrappers, "_DISKANN_CORPUS_SHA256", hashlib.sha256(CORPUS.encode()).hexdigest())
    if family == "diskann":
        repo_name, index, stem, target = "microsoft/DiskANN", 86, "diskann_public_oracle", "build_disk_index"
        configure, build = DISKANN_CONFIGURE, DISKANN_BUILD
        files = {
            "CMakeLists.txt": "cmake_minimum_required(VERSION 3.17)\nproject(diskann)\n"
                             "add_subdirectory(src)\nadd_subdirectory(tests)\n",
            "src/CMakeLists.txt": "add_library(diskann STATIC pq_flash_index.cpp linux_aligned_file_reader.cpp disk_utils.cpp)\n",
            "tests/CMakeLists.txt": "add_executable(build_disk_index build_disk_index.cpp)\n"
                                    "target_link_libraries(build_disk_index PRIVATE diskann)\n",
            "tests/build_disk_index.cpp": "extern int fixture_0();\nint main() { return fixture_0(); }\n",
        }
        sources = ["src/pq_flash_index.cpp", "src/linux_aligned_file_reader.cpp", "src/disk_utils.cpp"]
    else:
        repo_name, index, stem, target = "halide/Halide", 68, "halide_shuffle", "Halide"
        configure, build = HALIDE_CONFIGURE, HALIDE_BUILD
        files = {
            "CMakeLists.txt": "cmake_minimum_required(VERSION 3.17)\nproject(halide)\nadd_subdirectory(src)\n",
            "src/CMakeLists.txt": "add_library(Halide STATIC DecomposeVectorShuffle.cpp CodeGen_ARM.cpp)\n",
            "src/DecomposeVectorShuffle.h": "// public header fixture\n",
        }
        sources = ["src/DecomposeVectorShuffle.cpp", "src/CodeGen_ARM.cpp"]
    for number, name in enumerate(sources):
        files[name] = f"int fixture_{number}() {{ return {number}; }}\n"
    files["tests/unchanged.cpp"] = "// fixed public test fixture\n"
    repository = tmp_path / "repo"
    repository.mkdir()
    git(repository, "init", "--quiet")
    for name, text in files.items():
        path = repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(repository, "add", ".")
    git(repository, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "--quiet", "-m", "synthetic public inputs")
    task = {"id": family + "-fixture", "repo": repo_name,
            "start_sha": git(repository, "rev-parse", "HEAD").decode().strip()}
    plan = {"repo": repo_name, "input_index": index, "docker_image": "fixture-image",
            "configure_command": f"bash '{{root}}/harnesses/configure_{stem}.sh'",
            "build_command": f"bash '{{root}}/harnesses/build_{stem}.sh' '{{root}}'",
            "test_command": f"bash '{{root}}/harnesses/run_{stem}.sh'"}
    legacy = tmp_path / "legacy"
    lane = legacy / "native"
    harnesses = lane / "harnesses"
    harnesses.mkdir(parents=True)
    (harnesses / f"configure_{stem}.sh").write_text(configure)
    (harnesses / f"build_{stem}.sh").write_text(build)
    assertion_name = "diskann_public_oracle.cpp" if family == "diskann" else "halide_shuffle_oracle.cpp"
    (harnesses / assertion_name).write_text("// synthetic private assertion fixture\n")
    (harnesses / f"run_{stem}.sh").write_text("printf 'fixed fixture completed\\n'\n")
    (lane / "plans.jsonl").write_text(json.dumps(plan) + "\n")
    (lane / "fixture.log").write_text("fixed fixture completed\n")
    scratch, output = tmp_path / "scratch", tmp_path / "output"
    scratch.mkdir()
    output.mkdir()
    bundles = []

    def bundle(root, *, role, **kwargs):
        bundles.append((role, {name: path.read_bytes() for name, path in tree_files(root).items()}))
        return {"path": str(output / "fixture-tree.json"), "sha256": "a" * 64}

    ctx = SimpleNamespace(legacy=legacy, output=output, scratch=scratch, config={}, bundles=bundles, bundle=bundle,
                          repo=lambda task: repository, modules=lambda task: [],
                          image=lambda name: ("sha256:" + "a" * 64, {"path": "/fixture/image.json", "sha256": "b" * 64}),
                          task_directory=lambda *args: lane, immutable_tests=lambda task: ["tests/unchanged.cpp"],
                          golden=lambda *args: {"path": "/fixture/patch", "sha256": "c" * 64})
    oracle = {"input_index": index, "runs": {"start": [{"log": "fixture.log"}]},
              "pass_to_pass_tests": ["fixed public behavior"]}
    return SimpleNamespace(ctx=ctx, task=task, plan=plan, oracle=oracle, family=family, stem=stem,
                           target=target, configure=configure, build=build, sources=sources,
                           assertion_name=assertion_name,
                           build_file=harnesses / f"build_{stem}.sh",
                           configure_file=harnesses / f"configure_{stem}.sh")


@pytest.fixture(params=["diskann", "halide"])
def prefix_case(tmp_path, monkeypatch, request):
    return prefix_fixture(tmp_path, monkeypatch, request.param)


def project(case):
    return wrappers.public_wrapper(case.ctx, case.task, case.plan, native_lane.public_configure_command)


def test_argument_bearing_build_wrappers_reproduce_the_old_prefix_blocker(prefix_case):
    with pytest.raises(ContractError, match="explicit adapter"):
        native_lane.production_prefix(prefix_case.plan["build_command"])


def test_complete_wrappers_keep_flags_sources_and_all_private_steps_in_grader(prefix_case):
    case = prefix_case
    before_sources = {name: path.read_bytes() for name, path in tree_files(case.ctx.repo(case.task)).items()}
    before_private = {name: path.read_bytes() for name, path in tree_files(case.build_file.parent).items()}
    recipe = native_lane.migrate(case.ctx, case.task, case.oracle)
    expected = shlex.split(native_lane.public_configure_command(case.configure))
    expected[4] = wrappers.PUBLIC_BUILD
    assert shlex.split(recipe.bootstrap[-1]) == expected
    assert recipe.dev_checks[0].endswith(
        f"cmake --build {wrappers.PUBLIC_BUILD} --target {case.target} -j4")
    if case.family == "diskann":
        assert recipe.dev_checks[0].startswith(
            "export LD_LIBRARY_PATH=/opt/diskann-mkl-compat:/usr/lib/x86_64-linux-gnu && ")
    public = "\n".join(recipe.bootstrap + recipe.dev_checks)
    assert not any(word in public for word in ("harnesses", "oracle-data", "vectors.bin", "python3", case.stem))
    assert recipe.grading["commands"][0]["expected_tests"] == case.oracle["pass_to_pass_tests"]
    assert recipe.grading["immutable_paths"] == ["tests/unchanged.cpp"]
    normalize = lambda command: native_lane.relocate(command, {"root": "/workspace/.reviewbench/native"})
    assert recipe.grading["bootstrap"][1:] == [
        normalize(case.plan["configure_command"]), normalize(case.plan["build_command"])]
    assert recipe.grading["commands"][0]["command"] == normalize(case.plan["test_command"])
    assert set(recipe.scratch_paths) == {"build-oracle", wrappers.PUBLIC_BUILD}
    assert all(asset["visibility"] == "grader" for asset in recipe.assets)
    for role, files in case.ctx.bundles:
        assert role == "grader"
        assert files[f"harnesses/build_{case.stem}.sh"] == case.build.encode()
        assert files["harnesses/" + case.assertion_name] == before_private[case.assertion_name]
    assert {name: path.read_bytes() for name, path in tree_files(case.ctx.repo(case.task)).items()} == before_sources
    assert {name: path.read_bytes() for name, path in tree_files(case.build_file.parent).items()} == before_private
    record = json.loads(next((case.ctx.output / "native-public-wrappers").glob("*.json")).read_text())
    assert record["source_changes"] == record["test_changes"] == []
    assert all(name in record["public_inputs"] for name in case.sources)
    assert record["wrapper_sha256"][case.build_file.name] == file_hash(case.build_file)


@pytest.mark.parametrize("where", ["before_public", "after_public", "after_private"])
def test_extra_instructions_reject_the_complete_build_wrapper(prefix_case, where):
    case = prefix_case
    command = "cp /private/reference.cpp " + case.sources[0] + "\n"
    text = case.build
    if where == "before_public":
        text = text.replace("cmake --build", command + "cmake --build", 1)
    elif where == "after_public":
        text = text.replace('-j"$jobs"\n', '-j"$jobs"\n' + command, 1)
    else:
        text += command
    case.build_file.write_text(text)
    with pytest.raises(ContractError):
        project(case)


@pytest.mark.parametrize("extra", [
    " && cp /private/reference.cpp src/injected.cpp",
    " -DCMAKE_TOOLCHAIN_FILE=/private/extra.cmake",
    " -DWITH_TESTS=OFF",
])
def test_extra_configure_instructions_or_flags_reject_extraction(prefix_case, extra):
    case = prefix_case
    case.configure_file.write_text(case.configure.rstrip() + extra + "\n")
    with pytest.raises(ContractError):
        project(case)


def test_setup_directory_does_not_replace_the_public_production_target(prefix_case):
    case = prefix_case
    case.build_file.write_text(case.build.replace(
        f'cmake --build build-oracle --target {case.target} -j"$jobs"',
        "mkdir -p build-oracle-cmake-" + case.target))
    with pytest.raises(ContractError):
        project(case)


def test_private_compiler_arguments_must_remain_accounted_for(prefix_case):
    case = prefix_case
    case.build_file.write_text(case.build.replace("--coverage", "--coverage -include /private/extra.h", 1))
    with pytest.raises(ContractError):
        project(case)


def test_root_argument_is_required_and_not_silently_ignored(prefix_case):
    case = prefix_case
    case.plan["build_command"] = case.plan["build_command"].replace(" '{root}'", " '/reference'")
    with pytest.raises(ContractError):
        project(case)


@pytest.mark.parametrize("change", ["missing", "symlink", "alias_only"])
def test_public_sources_and_real_target_declarations_are_required(prefix_case, change):
    case = prefix_case
    repository = case.ctx.repo(case.task)
    if change == "alias_only":
        path = repository / ("tests/CMakeLists.txt" if case.family == "diskann" else "src/CMakeLists.txt")
        kind = "add_executable" if case.family == "diskann" else "add_library"
        path.write_text(f"{kind}({case.target}::alias ALIAS other)\n")
    else:
        path = repository / case.sources[0]
        path.unlink()
        if change == "symlink":
            path.symlink_to("other.cpp")
    git(repository, "add", "-A")
    git(repository, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "--quiet", "-m", "alter public input")
    case.task["start_sha"] = git(repository, "rev-parse", "HEAD").decode().strip()
    with pytest.raises(ContractError, match="public"):
        project(case)


def test_other_task_indices_do_not_gain_a_new_adapter(prefix_case):
    case = prefix_case
    case.plan["input_index"] += 1000
    assert project(case) is None


@pytest.mark.parametrize("change", ["body", "delimiter", "corpus_command", "duplicate_heredoc"])
def test_diskann_corpus_is_opaque_pinned_and_wholly_grader_only(tmp_path, monkeypatch, change):
    case = prefix_fixture(tmp_path, monkeypatch, "diskann")
    if change == "body":
        text = case.build.replace(CORPUS, CORPUS + "\nimport os")
    elif change == "delimiter":
        text = case.build.replace("<<'PY'", "<<PY")
    elif change == "corpus_command":
        text = case.build.replace("--data_path build-oracle/oracle-data/vectors.bin", "--data_path /private/reference.bin")
    else:
        text = case.build + "python3 - <<'PY'\nprint('extra')\nPY\n"
    case.build_file.write_text(text)
    with pytest.raises(ContractError):
        project(case)


def test_halide_preserves_an_original_required_test_switch(tmp_path, monkeypatch):
    case = prefix_fixture(tmp_path, monkeypatch, "halide")
    case.configure_file.write_text(case.configure.replace("-DWITH_TESTS=OFF", "-DWITH_TESTS=ON"))
    arguments = shlex.split(project(case)["configure"])
    assert "-DWITH_TESTS=ON" in arguments and "-DWITH_TESTS=OFF" not in arguments


def test_real_small_public_build_uses_current_candidate_and_never_runs_corpus(prefix_case):
    case = prefix_case
    # Only tiny synthetic translation units, not either real project build.
    assert all(shutil.which(tool) is not None for tool in ("cmake", "ninja", "g++", "ccache"))
    result = project(case)
    shutil.rmtree(case.build_file.parent)
    repository = case.ctx.repo(case.task)
    env = {**os.environ, "CMAKE_BUILD_PARALLEL_LEVEL": "1", "OMP_NUM_THREADS": "1"}
    for command in (result["configure"], result["build"]):
        run = subprocess.run(["bash", "-c", command], cwd=repository, env=env, timeout=30,
                             capture_output=True, text=True)
        assert run.returncode == 0, run.stdout + run.stderr
    output = repository / wrappers.PUBLIC_BUILD
    assert (output / ("tests/build_disk_index" if case.family == "diskann" else "src/libHalide.a")).is_file()
    assert not (repository / "build-oracle").exists()
    assert not list(repository.rglob("vectors.bin"))
    (repository / case.sources[0]).write_text("current_candidate_compile_error\n")
    run = subprocess.run(["bash", "-c", result["build"]], cwd=repository, env=env, timeout=30,
                         capture_output=True, text=True)
    assert run.returncode != 0 and "current_candidate_compile_error" in run.stdout + run.stderr
