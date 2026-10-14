"""Identity, profile, public/private separation and reversible adapter fixtures."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import native_lane
from agentic_runtime_migration import native_rippled as adapter
from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash, file_hash
from test_agentic_native_dependency_closure import context, repository
from test_agentic_native_rippled import modern_log, legacy_log


def fixture(tmp_path, monkeypatch, key="directory"):
    specs = copy.deepcopy(adapter.SPECS)
    spec = specs[key]
    spec["id"] = "synthetic-rippled-" + key
    source = tmp_path / "candidate"
    sha = repository(source, {"CMakeLists.txt": "# current candidate\n",
                              "conanfile.py": "# current dependency recipe\n",
                              "src/one.cpp": "int one(){return 1;}\n",
                              "src/two.cpp": "int two(){return 2;}\n",
                              "tests/public.cpp": "// immutable original test\n"})
    spec["start_sha"] = sha
    task = {"id": spec["id"], "repo": "XRPLF/rippled", "start_sha": sha}
    legacy = key == "stobject"
    build = "build" if legacy else "build-oracle"
    target = "rippled_view_oracle" if key == "view" else spec["target"]
    test = "set -e; out=$(./build-oracle/rippled_view_oracle); printf '%s\\n' \"$out\"; test \"$out\" = 'view-complete'" if key == "view" else (
        "./" + build + "/" + target + " --unittest=" + {"directory": "Directory", "stobject": "STObject",
                                                       "confidential": "ConfidentialTransfer"}[key])
    plan = {"input_index": spec["index"] if not legacy else 999, "repo": task["repo"],
            "docker_image": "synthetic-rippled-toolchain", "timeout_seconds": 7200,
            "configure_command": "bash '{root}/harnesses/" + spec.get("helper", "unused.sh") + "'",
            "build_command": "cmake --build " + build + " --target " + target + " -j32", "test_command": test}
    ctx = context(tmp_path, source, plan)
    ctx.copy_asset = lambda path, category: adapter.pinned_file(path)
    if legacy:
        root = ctx.legacy.parent / "strict_screen_20260726"
        directory = root / "oracle_recovery/native-real/tasks/22-rippled"
    else:
        root = ctx.legacy / "native"
        directory = root / "artifacts/fixture"
    directory.mkdir(parents=True)
    patch = directory / "source-only.patch"
    patch.write_text("synthetic private golden patch\n")
    commands = directory / "commands.txt"
    commands.write_text("original public commands fixture\n")
    oracle = {**task, "pass_to_pass_tests": ["fixed/" + key],
              "artifacts": {"directory": str(directory.relative_to(root))}}
    if legacy:
        commands.rename(directory / "build-commands.txt")
        commands = directory / "build-commands.txt"
        spec["commands_sha256"] = file_hash(commands)
        oracle["golden_scope"] = {"patch": str(patch.relative_to(root)), "patch_sha256": file_hash(patch)}
        log = directory / "start-conan.log"
        profile = "Configuration:\n" + adapter.public_spec(key)["host_profile"] + "[build_requires]\n[env]\n\n"
        log.write_text(profile + legacy_log())
    else:
        spec["plan_sha256"] = canonical_hash(plan)
        helper = root / "harnesses" / spec["helper"]
        helper.write_text("#!/bin/bash\nset -eu\nconan install .\ncmake -S . -B build-oracle\n")
        spec["helper_sha256"] = file_hash(helper)
        oracle.update(input_index=spec["index"], lane_results_source="native/results.jsonl",
                      golden_patch_sha256=file_hash(patch))
        oracle["artifacts"].update(commands=str(commands.relative_to(root)), patch=str(patch.relative_to(root)))
        log = directory / "logs/start-configure-attempt1.log"
        log.parent.mkdir()
        public = adapter.public_spec(key)
        log.write_text("Profile host:\n" + public["host_profile"] + "\nProfile build:\n"
                       + public["build_profile"] + "\n======== Computing dependency graph ========\n" + modern_log())
        if key == "view":
            view_files = {}
            for name in adapter.VIEW_FILES:
                path = root / "harnesses" / name
                path.parent.mkdir(exist_ok=True, parents=True)
                path.write_text("PRIVATE_ASSERTION_MUST_STAY_GRADER_ONLY\n")
                view_files[name] = file_hash(path)
            monkeypatch.setattr(adapter, "VIEW_FILES", view_files)
    for variant in ("start", "golden"):
        rows = []
        for repetition in range(1, 4):
            path = directory / f"{variant}-{repetition}.log"
            output = ("".join("ripple.fixture.case" + str(i) + "\n" for i in range(spec["cases"]))
                      + f"{repetition}ms, 1 suite, {spec['cases']} cases, {spec['checks']} tests total, 0 failures\n"
                      if key != "view" else "amount=0\nmessages=1\nlog=original behavior\n")
            path.write_text(output)
            rows.append({"run": repetition, "ok": True, "passed": 1, "failed": 0,
                         "log": str(path.relative_to(root))})
        if legacy:
            oracle[variant + "_runs"] = rows
        else:
            oracle.setdefault("runs", {})[variant] = rows
    monkeypatch.setattr(adapter, "SPECS", specs)
    monkeypatch.setattr(adapter, "load_preparation", lambda *args: {
        "request_sha256": "d" * 64,
        "dependency_asset": {"path": "/fixture/external-dependency.json", "sha256": "e" * 64}})
    return ctx, task, oracle, plan, log


@pytest.mark.parametrize("key", ["directory", "stobject", "confidential", "view"])
def test_all_four_inputs_preserve_original_test_id_patch_and_full_candidate_build(tmp_path, monkeypatch, key):
    ctx, task, oracle, plan, _ = fixture(tmp_path, monkeypatch, key)
    before = copy.deepcopy(oracle)
    recipe = native_lane.migrate(ctx, task, oracle)
    assert oracle == before
    assert recipe.grading["commands"][0]["expected_tests"] == oracle["pass_to_pass_tests"]
    original_test = "./build/rippled --unittest=STObject" if key == "stobject" else plan["test_command"]
    assert recipe.grading["commands"][0]["command"] == original_test
    assert recipe.grading["immutable_paths"] == ["tests/public.cpp"]
    assert recipe.grading["multifile_build"] is True
    assert recipe.grading["golden_patch"]["sha256"] == (oracle["golden_scope"]["patch_sha256"]
        if key == "stobject" else oracle["golden_patch_sha256"])
    assert recipe.dev_checks[0].endswith("--target " + adapter.SPECS[key]["target"] + " -j4")
    assert "-j4" in recipe.grading["bootstrap"][-1]
    assert not any("golden" in command or "ORACLE_VARIANT" in command
                   for command in [*recipe.bootstrap, *recipe.dev_checks, *recipe.grading["bootstrap"]])
    assert "entry.py" in recipe.bootstrap[1]
    for bundle in ctx.bundles:
        if bundle["role"] != "grader":
            assert all(b"PRIVATE_ASSERTION_MUST_STAY_GRADER_ONLY" not in data for data in bundle["files"].values())
            assert all(b"synthetic private golden patch" not in data for data in bundle["files"].values())
            assert not any(name.endswith((".a", ".o")) for name in bundle["files"])
    if key == "view":
        assert "build-native-rippled-public" in recipe.dev_checks[0]
        assert "rippled-view-oracle" in recipe.grading["bootstrap"][0]
        public = next(bundle for bundle in ctx.bundles if bundle["role"] != "grader")
        assert b'add_subdirectory("${XRPL_SOURCE_DIR}" xrpl)' in public["files"]["view/CMakeLists.txt"]
        assert b"add_executable" not in public["files"]["view/CMakeLists.txt"]
    if key == "stobject":
        assert "input_index" not in oracle and "lane_results_source" not in oracle


@pytest.mark.parametrize("key", ["directory", "stobject", "confidential", "view"])
def test_compiler_test_and_dependency_profiles_are_checked_against_actual_original_log(tmp_path, monkeypatch, key):
    ctx, task, oracle, _, log = fixture(tmp_path, monkeypatch, key)
    assert adapter.original_inputs(ctx, task, oracle)["key"] == key
    log.write_text(log.read_text().replace("compiler.cppstd=20", "compiler.cppstd=17", 1))
    with pytest.raises(ContractError, match="host compiler/options"):
        adapter.original_inputs(ctx, task, oracle)


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(test_command="./build-oracle/rippled --unittest=Unrelated"),
    lambda p: p.update(build_command=p["build_command"].replace("rippled", "old.a")),
    lambda p: p.update(configure_command=p["configure_command"] + "; true"),
    lambda p: p.update(env={"FLAGS": "-O3"}),
])
def test_changed_original_plan_is_not_reinterpreted(tmp_path, monkeypatch, mutation):
    ctx, task, oracle, plan, _ = fixture(tmp_path, monkeypatch)
    mutation(plan)
    (ctx.legacy / "native/plans.jsonl").write_text(json.dumps(plan) + "\n")
    with pytest.raises(ContractError, match="complete original plan"):
        adapter.original_inputs(ctx, task, oracle)


def test_fewer_original_cases_or_changed_golden_fail_closed(tmp_path, monkeypatch):
    ctx, task, oracle, _, _ = fixture(tmp_path, monkeypatch)
    row = oracle["runs"]["golden"][2]
    path = ctx.legacy / "native" / row["log"]
    path.write_text(path.read_text().replace("5 cases", "4 cases"))
    with pytest.raises(ContractError, match="different cases"):
        adapter.original_inputs(ctx, task, oracle)


def test_signature_requires_cases_counts_and_zero_failures(tmp_path, monkeypatch):
    ctx, task, oracle, _, _ = fixture(tmp_path, monkeypatch)
    inputs = adapter.original_inputs(ctx, task, oracle)
    path = ctx.legacy / "native" / oracle["runs"]["start"][0]["log"]
    text = path.read_text()
    assert re.search(inputs["success_pattern"], text)
    for changed in [text.replace("0 failures", "1 failures"), text.replace("2442 tests", "1 tests"),
                    text.replace("ripple.fixture.case3\n", ""), ""]:
        assert not re.search(inputs["success_pattern"], changed)


@pytest.mark.parametrize("key", ["directory", "confidential", "view"])
def test_modern_configuration_keeps_exact_coverage_and_feature_flags(key):
    args = shlex.split(adapter.configure_command(key))
    assert "-DCMAKE_BUILD_TYPE=Release" in args
    for lang in ("C", "CXX"):
        assert "-DCMAKE_" + lang + "_FLAGS_RELEASE=-O0 -g --coverage -fprofile-abs-path -DNDEBUG" in args
    assert "-DCMAKE_EXE_LINKER_FLAGS=--coverage" in args
    assert "-DCMAKE_CXX_COMPILER_LAUNCHER=ccache" in args
    if key == "directory":
        assert "-Drocksdb=OFF" in args and "-Dtests=ON" in args and "-Dunity=OFF" in args
    if key == "confidential":
        assert all(value in args for value in ("-Drocksdb=ON", "-Dtests=ON", "-Dxrpld=ON", "-Dunity=OFF"))


def test_legacy_coverage_and_public_current_root_are_preserved():
    args = shlex.split(adapter.configure_command("stobject"))
    assert args[:5] == ["cmake", "-S", ".", "-B", "build"]
    assert "-DCMAKE_CXX_FLAGS=-O0 -g --coverage" in args
    assert "-DCMAKE_EXE_LINKER_FLAGS=--coverage" in args
    profile = adapter.public_spec("stobject")["host_profile"]
    assert "compiler.version=11" in profile and "build_type=Debug" in profile
    assert "xrpl:coverage=True" in profile and "xrpl:tests=True" in profile


def test_transaction(tmp_path, monkeypatch):
    ctx, task, oracle, plan, _ = fixture(tmp_path, monkeypatch)
    lane = native_lane
    if location := os.environ.get("RIPPLED_NATIVE_LANE_FILE"):
        spec = importlib.util.spec_from_file_location("agentic_runtime_migration.rippled_transaction_lane", location)
        lane = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lane)
    if os.environ.get("RIPPLED_EXPECT", "configured") == "blocked":
        with pytest.raises(ContractError, match="public CMake|public dependency") as result:
            lane.migrate(ctx, task, oracle)
        print("STATE=blocked; RESULT=" + str(result.value))
    else:
        recipe = lane.migrate(ctx, task, oracle)
        assert recipe.grading["commands"][0]["command"] == plan["test_command"]
        assert recipe.grading["commands"][0]["expected_tests"] == ["fixed/directory"]
        print("STATE=configured; FULL_CURRENT_TARGET=1; ORIGINAL_ASSERTIONS_IDS=1; PRIVATE_LEAK=0")
