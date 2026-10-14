"""Task5143 public-target restoration; fixtures are not original runtime proof."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import native_rippled as adapter
from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash
from test_agentic_native_rippled_adapter import fixture


ORIGINAL_ARGS = [
    "cmake", "-S", ".", "-B", "build", "-G", "Ninja",
    "-DCMAKE_TOOLCHAIN_FILE=/workspace/build/generators/conan_toolchain.cmake",
    "-DCMAKE_CXX_FLAGS=-O0 -g --coverage", "-DCMAKE_EXE_LINKER_FLAGS=--coverage",
]
LEGACY_BASE = (
    "[settings]\narch=x86_64\nbuild_type=Debug\ncompiler=gcc\ncompiler.cppstd=20\n"
    "compiler.libcxx=libstdc++11\ncompiler.version=11\nos=Linux\n"
    "arch_build=x86_64\nos_build=Linux\n"
)


@pytest.mark.parametrize("private", [False, True])
def test_stobject_adds_only_required_target_flag(private):
    assert shlex.split(adapter.configure_command("stobject", private=private)) == [
        *ORIGINAL_ARGS, "-Dxrpld=ON"]


def test_original_conan_profiles_and_dependency_mode_are_byte_identical():
    assert adapter.public_spec("stobject") == {
        "conan_version": "1.60.2", "build_profile": LEGACY_BASE,
        "host_profile": LEGACY_BASE + "[options]\nxrpl:coverage=True\nxrpl:tests=True\n",
        "build_directory": "build", "recipe_mode": "current",
    }


@pytest.mark.parametrize("key", ["directory", "confidential", "view"])
@pytest.mark.parametrize("private", [False, True])
def test_other_three_configure_commands_are_unchanged(key, private):
    source = (adapter.PRIVATE + "/harnesses/rippled-view-oracle" if private else adapter.PUBLIC + "/view")
    args = [
        "cmake", "-S", source if key == "view" else ".", "-B",
        "build-native-rippled-public" if key == "view" and not private else "build-oracle",
        "-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release",
        "-DCMAKE_TOOLCHAIN_FILE=/workspace/build-oracle/conan/build/generators/conan_toolchain.cmake",
        "-DCMAKE_C_COMPILER_LAUNCHER=ccache", "-DCMAKE_CXX_COMPILER_LAUNCHER=ccache",
        "-DCMAKE_C_FLAGS_RELEASE=-O0 -g --coverage -fprofile-abs-path -DNDEBUG",
        "-DCMAKE_CXX_FLAGS_RELEASE=-O0 -g --coverage -fprofile-abs-path -DNDEBUG",
        "-DCMAKE_EXE_LINKER_FLAGS=--coverage",
    ]
    if key == "view":
        args += ["-DXRPL_SOURCE_DIR=/workspace"]
        if private:
            args += ["-DORACLE_ROOT=" + adapter.PRIVATE]
    else:
        args += ["-Drocksdb=" + ("ON" if key == "confidential" else "OFF"),
                 "-Dtests=ON", "-Dunity=OFF"]
        if key == "confidential":
            args += ["-Dxrpld=ON"]
    assert shlex.split(adapter.configure_command(key, private=private)) == args


def test_stobject_recipe_preserves_complete_build_oracle_and_dependency_request(tmp_path, monkeypatch):
    ctx, task, oracle, _, _ = fixture(tmp_path, monkeypatch, "stobject")
    oracle["pass_to_pass_tests"] = ["ripple.protocol.STObject repository unit suite: 6 cases, 2175 checks"]
    before = copy.deepcopy(oracle)
    inputs = adapter.original_inputs(ctx, task, oracle)
    request = adapter.dependency_request(task, inputs, "sha256:" + "a" * 64)
    expected_request = copy.deepcopy(request)
    recipe = adapter.migrate(ctx, task, oracle)
    assert oracle == before
    assert recipe.bootstrap[-1] == shlex.join([*ORIGINAL_ARGS, "-Dxrpld=ON"])
    assert recipe.dev_checks == ["cmake --build build --target rippled -j4"]
    assert recipe.grading["bootstrap"] == ["cmake --build build --target rippled -j4"]
    assert recipe.grading["commands"] == [{
        "command": "./build/rippled --unittest=STObject", "parser": "regex",
        "expected_tests": oracle["pass_to_pass_tests"], "success_pattern": inputs["success_pattern"],
    }]
    assert recipe.grading["multifile_build"] is True
    assert recipe.grading["immutable_paths"] == ["tests/public.cpp"]
    assert recipe.grading["golden_patch"]["sha256"] == oracle["golden_scope"]["patch_sha256"]
    assert adapter.dependency_request(task, adapter.original_inputs(ctx, task, oracle),
                                      "sha256:" + "a" * 64) == expected_request
    # Reverting just the configure generator must not change closure identity.
    monkeypatch.setattr(adapter, "configure_command", lambda *a, **kw: shlex.join(ORIGINAL_ARGS))
    assert adapter.dependency_request(task, inputs, "sha256:" + "a" * 64) == request
    assert canonical_hash(request) == canonical_hash(expected_request)


@pytest.mark.parametrize("old,new", [
    ("xrpl:coverage=True", "xrpl:coverage=False"),
    ("xrpl:tests=True", "xrpl:tests=False"),
    ("build_type=Debug", "build_type=Release"),
    ("compiler.version=11", "compiler.version=13"),
    ("compiler.cppstd=20", "compiler.cppstd=17"),
    ("compiler.libcxx=libstdc++11", "compiler.libcxx=libstdc++"),
])
def test_target_restoration_does_not_accept_changed_original_profiles(tmp_path, monkeypatch, old, new):
    ctx, task, oracle, _, log = fixture(tmp_path, monkeypatch, "stobject")
    log.write_text(log.read_text().replace(old, new))
    with pytest.raises(ContractError, match="host compiler/options"):
        adapter.migrate(ctx, task, oracle)


@pytest.mark.parametrize("old,new", [
    ("2175 tests total", "2174 tests total"),
    ("6 cases", "5 cases"),
    ("0 failures", "1 failures"),
    ("ripple.fixture.case3\n", ""),
])
def test_original_stobject_assertions_and_cases_remain_mandatory(tmp_path, monkeypatch, old, new):
    ctx, task, oracle, _, _ = fixture(tmp_path, monkeypatch, "stobject")
    inputs = adapter.original_inputs(ctx, task, oracle)
    log = ctx.legacy.parent / "strict_screen_20260726" / oracle["start_runs"][0]["log"]
    text = log.read_text()
    assert re.search(inputs["success_pattern"], text)
    assert not re.search(inputs["success_pattern"], text.replace(old, new))


def test_target_option_does_not_relax_original_golden_pin(tmp_path, monkeypatch):
    ctx, task, oracle, _, _ = fixture(tmp_path, monkeypatch, "stobject")
    root = ctx.legacy.parent / "strict_screen_20260726"
    (root / oracle["golden_scope"]["patch"]).write_text("different golden\n")
    with pytest.raises(ContractError, match="original input changed"):
        adapter.migrate(ctx, task, oracle)


def _target_fixture(root, arguments):
    """Tiny real CMake target-gate fixture, not a Rippled/test replacement."""
    root.mkdir()
    (root / "one.cpp").write_text("int one(){return 1;}\n")
    (root / "main.cpp").write_text(
        "#include <cassert>\nint one();int test_value();\n"
        "int main(){assert(one()==1);assert(test_value()==2175);}\n")
    (root / "checks.cpp").write_text(
        "#ifndef ENABLE_TESTS\n#error Original tests must be enabled\n#endif\n"
        "int test_value(){return 2175;}\n")
    (root / "conan_toolchain.cmake").write_text(
        'set(xrpld "False" CACHE STRING "Conan default")\n'
        'set(tests "True" CACHE STRING "Original profile")\n'
        'set(coverage "True" CACHE STRING "Original profile")\n'
        "set(CMAKE_CXX_STANDARD 20)\n")
    (root / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.16)\nproject(TargetGate LANGUAGES CXX)\n"
        'option(xrpld "Build executable" ON)\noption(tests "Build tests" ON)\n'
        'option(only_docs "Docs only" OFF)\noption(packages_only "Packages only" OFF)\n'
        'if(coverage)\nset(CMAKE_BUILD_TYPE Debug CACHE STRING "" FORCE)\nendif()\n'
        "if(only_docs OR packages_only)\nreturn()\nendif()\n"
        "if(xrpld)\nadd_executable(rippled main.cpp one.cpp)\n"
        "if(tests)\ntarget_sources(rippled PRIVATE checks.cpp)\n"
        "target_compile_definitions(rippled PRIVATE ENABLE_TESTS)\nendif()\nendif()\n")
    before = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    args = list(arguments)
    args[args.index("-S") + 1] = str(root)
    args[args.index("-B") + 1] = str(root / "build")
    args = ["-DCMAKE_TOOLCHAIN_FILE=" + str(root / "conan_toolchain.cmake")
            if arg.startswith("-DCMAKE_TOOLCHAIN_FILE=") else arg for arg in args]
    configured = subprocess.run(args, capture_output=True, text=True, timeout=60)
    assert configured.returncode == 0, configured.stdout + configured.stderr
    built = subprocess.run(["cmake", "--build", str(root / "build"), "--target", "rippled", "--parallel", "1"],
                           capture_output=True, text=True, timeout=60)
    assert all((root / name).read_bytes() == data for name, data in before.items())
    cache = (root / "build/CMakeCache.txt").read_text()
    for value in ("coverage:STRING=True", "tests:STRING=True", "CMAKE_BUILD_TYPE:STRING=Debug",
                  "only_docs:BOOL=OFF", "packages_only:BOOL=OFF",
                  "CMAKE_CXX_FLAGS:STRING=-O0 -g --coverage"):
        assert value in cache
    return built


def test_real_target_positive_negative_and_independent_rollback(tmp_path):
    if any(shutil.which(tool) is None for tool in ("cmake", "ninja", "c++")):
        pytest.skip("real target-gate fixture requires CMake/Ninja/C++")
    current = shlex.split(adapter.configure_command("stobject"))
    assert current == [*ORIGINAL_ARGS, "-Dxrpld=ON"]
    rolled_back = current[:-1]
    assert rolled_back == ORIGINAL_ARGS
    for label, arguments, enabled in [
        ("baseline", ORIGINAL_ARGS, False), ("modified", current, True),
        ("rollback", rolled_back, False),
    ]:
        result = _target_fixture(tmp_path / label, arguments)
        if enabled:
            assert result.returncode == 0, result.stdout + result.stderr
        else:
            assert result.returncode != 0 and "rippled" in result.stderr
            assert "missing and no known rule" in result.stderr or "unknown target" in result.stderr


def test_transaction(tmp_path):
    """The delivery runner also loads baseline/modified/rollback real copies."""
    if any(shutil.which(tool) is None for tool in ("cmake", "ninja", "c++")):
        pytest.skip("real target-gate fixture requires CMake/Ninja/C++")
    selected = adapter
    if path := os.environ.get("RIPPLED_TARGET_MODULE"):
        spec = importlib.util.spec_from_file_location("agentic_runtime_migration.target_transaction", path)
        selected = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(selected)
    args = shlex.split(selected.configure_command("stobject"))
    enabled = os.environ.get("RIPPLED_TARGET_EXPECT", "enabled") == "enabled"
    assert args == ORIGINAL_ARGS + (["-Dxrpld=ON"] if enabled else [])
    result = _target_fixture(tmp_path / "candidate", args)
    if enabled:
        assert result.returncode == 0, result.stdout + result.stderr
    else:
        assert result.returncode != 0 and "rippled" in result.stderr
    print(json.dumps({"state": "target_enabled" if enabled else "target_missing",
                      "build_exit": result.returncode, "profile_unchanged": True,
                      "coverage": True, "tests": True, "source_unchanged": True}, sort_keys=True))
