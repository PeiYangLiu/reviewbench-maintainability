"""MQT input-closure controls on synthetic fixtures; no benchmark calibration."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import native_mqt as adapter, native_mqt_entry as entry, native_lane
from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash, file_hash
from reviewbench_maintainability.agentic.snapshots import tree_hash
from test_agentic_native_dependency_closure import context, repository, oracle

WRAPPER = """cmake_minimum_required(VERSION 3.20)
project(mqt_dcx_oracle LANGUAGES CXX)

set(BUILD_MQT_CORE_BINDINGS OFF CACHE BOOL "" FORCE)
set(BUILD_MQT_CORE_TESTS OFF CACHE BOOL "" FORCE)
set(BUILD_MQT_CORE_BENCHMARKS OFF CACHE BOOL "" FORCE)
set(MQT_CORE_INSTALL OFF CACHE BOOL "" FORCE)

add_subdirectory("${PRODUCTION_ROOT}" production)

add_executable(mqt-dcx-oracle "${ORACLE_SOURCE}")
target_link_libraries(mqt-dcx-oracle PRIVATE MQT::CoreDD)
target_compile_features(mqt-dcx-oracle PRIVATE cxx_std_17)
target_compile_options(mqt-dcx-oracle PRIVATE -O0 -g --coverage)
target_link_options(mqt-dcx-oracle PRIVATE --coverage)
"""
PLANS = {
    95: {
        "repo": adapter.REPO, "input_index": 95, "configure_command": None,
        "build_command": "set -e; git submodule update --init --recursive; cmake -S . -B build-oracle "
            "-GNinja -DBUILD_MQT_CORE_BINDINGS=ON -DBUILD_MQT_CORE_TESTS=OFF -DPython_EXECUTABLE=/usr/bin/python3 "
            "-DCMAKE_BUILD_TYPE=Debug -DCMAKE_CXX_FLAGS='-O0 -g --coverage' "
            "-DCMAKE_SHARED_LINKER_FLAGS=--coverage -DCMAKE_EXE_LINKER_FLAGS=--coverage; "
            "cmake --build build-oracle --target _core -j2",
        "test_command": 'PYTHONPATH=build-oracle/src/python python3 -c "import _core; q=_core.QuantumComputation(2); '
            'q.x(0); q.h(1); print(q.n_qubits, q.n_ops, str(_core.OpType.x))"',
        "real_translation_units": ["src/python/module.cpp", "src/python/register_core.cpp (golden)",
                                   "src/QuantumComputation.cpp", "src/operations/StandardOperation.cpp"],
        "docker_image": "reviewbench-hard-native-mqt:20260804", "timeout_seconds": 1800, "build_jobs": 2,
    },
    96: {
        "repo": adapter.REPO, "input_index": 96, "configure_command": None,
        "build_command": "set -e; mkdir -p oracle-src; cp '{root}/harnesses/CMakeLists.mqt96.txt' "
            "oracle-src/CMakeLists.txt; cmake -S oracle-src -B build-oracle -GNinja -DPRODUCTION_ROOT=$PWD "
            "-DORACLE_SOURCE='{root}/harnesses/mqt_dcx_inverse.cpp' -DCMAKE_BUILD_TYPE=Debug "
            "-DCMAKE_CXX_FLAGS='-O0 -g --coverage' -DCMAKE_EXE_LINKER_FLAGS=--coverage "
            "-DCMAKE_SHARED_LINKER_FLAGS=--coverage; cmake --build build-oracle --target mqt-dcx-oracle -j2",
        "test_command": "set -e; out=$(./build-oracle/mqt-dcx-oracle); printf '%s\\n' \"$out\"; test \"$out\" = 'true 4'",
        "real_translation_units": ["src/ir/operations/StandardOperation.cpp", "src/ir/operations/Operation.cpp",
                                   "src/dd/Operations.cpp",
                                   "include/mqt-core/dd/Operations.hpp instantiated by harnesses/mqt_dcx_inverse.cpp"],
        "docker_image": "reviewbench-native-real-mqt-core:latest", "timeout_seconds": 1800, "build_jobs": 2,
    },
}


@pytest.mark.parametrize("index", [95, 96])
def test_original_operational_plans_have_exact_bound_hashes(index):
    assert canonical_hash(PLANS[index]) == adapter.PROFILES[index]["plan"]
    assert hashlib.sha256(WRAPPER.encode()).hexdigest() == adapter.HARNESS96["CMakeLists.mqt96.txt"]


@pytest.mark.parametrize("index", [95, 96])
@pytest.mark.parametrize("field", ["build_command", "test_command", "docker_image", "real_translation_units",
                                  "configure_command", "timeout_seconds", "build_jobs"])
def test_plan_drift_rejected_before_any_execution(tmp_path, index, field):
    plan = copy.deepcopy(PLANS[index])
    value = plan[field]
    plan[field] = value + 1 if isinstance(value, int) else [] if isinstance(value, list) else str(value) + " changed"
    task = {"repo": adapter.REPO, "id": adapter.PROFILES[index]["id"], "start_sha": adapter.PROFILES[index]["start"]}
    with pytest.raises(ContractError, match="complete original"):
        adapter.attested(SimpleNamespace(legacy=tmp_path), task, plan, index)


def test_other_native_tasks_do_not_enter_new_profiles():
    assert adapter.mqt95_submodules(None, {"repo": adapter.REPO}, {"input_index": 94}) is None
    assert adapter.mqt96(None, {"repo": "other/repo"}, {"input_index": 96}) is None


def ref(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return {"path": str(path), "sha256": file_hash(path)}


def fixture95(tmp_path, monkeypatch):
    plan = {**PLANS[95], "expected_min_passed": 1}
    repo = tmp_path / "candidate"
    repository(repo, {"source.cpp": "// unchanged\n", "tests/public.cpp": "// test\n"})
    modules = [{"path": p, "commit": c, "omit": True} for p, c in adapter.PINS95.items()]
    ctx = context(tmp_path, repo, plan, modules)
    task = {"repo": adapter.REPO, "id": adapter.PROFILES[95]["id"], "start_sha": adapter.PROFILES[95]["start"]}
    assets, receipts, descriptors = {}, {}, {}
    for index, (path, pin) in enumerate(adapter.PINS95.items()):
        asset = ref(tmp_path / f"asset-{index}.json", {"fixture": path})
        assets[path] = asset
        descriptors[asset["sha256"]] = {"source_kind": "submodule", "git_commit": pin,
                                       "files": 1, "archive": {"input_bytes": 1024}}
        receipts[path] = ref(tmp_path / f"readback-{index}.json", {
            "status": "independent_Blob_component_readback_verified", "asset": asset,
            "all_paths_bytes_full_modes_verified": True, "gzip_crc_and_eof_verified": True,
        })
    original = {"status": "original_MQT95_git_inputs_archived", "id": task["id"],
                "pins": adapter.PINS95, "assets": assets}
    rb = {"status": "next_original_inputs_independently_verified", "id": task["id"], "receipts": receipts}
    ctx.config = {"migration": {
        "mqt95_git_inputs": ref(tmp_path / "original.json", original),
        "mqt95_git_readback": ref(tmp_path / "readback.json", rb),
    }}
    monkeypatch.setattr(adapter, "asset_descriptor", lambda a, *_: descriptors[a["sha256"]])
    monkeypatch.setattr(adapter, "readonly_links", lambda *_: modules)
    monkeypatch.setattr(native_lane, "completed_signature", lambda *_: "^fixture assertions completed$")
    return ctx, task, plan


def test_95_reuses_six_archives_without_rebundling_or_changing_test(tmp_path, monkeypatch):
    ctx, task, plan = fixture95(tmp_path, monkeypatch)
    before = copy.deepcopy(plan)
    updated, modules = adapter.mqt95_submodules(ctx, task, plan)
    assert plan == before and not ctx.bundles
    assert updated["test_command"] == plan["test_command"]
    original_tail = plan["build_command"].split("git submodule update --init --recursive;", 1)[1]
    assert updated["build_command"].endswith(original_tail)
    assert len(modules) == 6 and not any(m.get("omit") for m in modules)
    recipe = native_lane.migrate(ctx, task, oracle(plan))
    assert recipe.grading["commands"][0]["command"] == plan["test_command"]
    assert recipe.grading["golden_patch"] == {"path": "/fixture/patch", "sha256": "c" * 64}
    assert "--target _core -j2" in recipe.dev_checks[0]
    assert "git submodule" not in recipe.dev_checks[0]
    assert recipe.submodules == modules


def test_95_changed_pin_rejected(tmp_path, monkeypatch):
    ctx, task, plan = fixture95(tmp_path, monkeypatch)
    changed = [{"path": p, "commit": c} for p, c in adapter.PINS95.items()]
    changed[0]["commit"] = "0" * 40
    monkeypatch.setattr(adapter, "readonly_links", lambda *_: changed)
    with pytest.raises(ContractError, match="superproject pins"):
        adapter.mqt95_submodules(ctx, task, plan)


def test_95_incomplete_or_changed_readback_rejected(tmp_path, monkeypatch):
    ctx, task, plan = fixture95(tmp_path, monkeypatch)
    report = tmp_path / "readback-0.json"
    report.write_text("{}")
    with pytest.raises(ContractError, match="readback reference changed"):
        adapter.mqt95_submodules(ctx, task, plan)


def seed_fixture(tmp_path):
    workspace = tmp_path / "candidate"
    (workspace / "cmake").mkdir(parents=True)
    declaration = workspace / "cmake/ExternalDependencies.cmake"
    declaration.write_text("// public original dependency declarations\n")
    source = tmp_path / "dependencies"
    for name in ("json", "boost_mp"):
        (source / name).mkdir(parents=True)
        (source / name / "public.hpp").write_text("// original complete input\n")
    spec = adapter.mqt96_specification(WRAPPER, tree_hash(source))
    spec.update(source=str(source), declaration_sha256=file_hash(declaration))
    return workspace, source, spec


def test_96_public_wrapper_keeps_original_library_graph_without_assertions(tmp_path):
    workspace, source, spec = seed_fixture(tmp_path)
    text = spec["public_wrapper"]
    assert "add_subdirectory(\"${PRODUCTION_ROOT}\" production)" in text
    assert "add_dependencies(reviewbench-public-mqt MQT::CoreDD)" in text
    assert "mqt_dcx_inverse" not in text and "ORACLE_SOURCE" not in text and "add_executable" not in text
    for option in ("BUILD_MQT_CORE_BINDINGS", "BUILD_MQT_CORE_TESTS", "BUILD_MQT_CORE_BENCHMARKS", "MQT_CORE_INSTALL"):
        assert "set(" + option + " OFF CACHE BOOL \"\" FORCE)" in text
    assert entry.tree_digest(source)[0] == tree_hash(source)
    assert entry.seed(spec, workspace) == 2
    assert entry.seed(spec, workspace) == 2


@pytest.mark.parametrize("siblings", [
    (".drone.star", ".drone/after.sh"),
    ("indexes.html", "indexes/s01.html"),
])
def test_source_identity_sorts_complete_posix_paths_not_path_components(tmp_path, siblings):
    source = tmp_path / "source"
    for name in siblings:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("same original input\n")
    assert entry.tree_digest(source)[0] == tree_hash(source)


@pytest.mark.parametrize("what", ["declaration", "source", "target", "wrapper", "link"])
def test_96_validates_all_inputs_and_destinations_before_writes(tmp_path, what):
    workspace, source, spec = seed_fixture(tmp_path)
    if what == "declaration":
        (workspace / spec["declaration"]).write_text("different")
    elif what == "source":
        (source / "json/public.hpp").write_text("different")
    elif what == "target":
        target = workspace / spec["destination"]
        target.mkdir()
        (target / "candidate-owned").write_text("keep")
    elif what == "wrapper":
        target = workspace / spec["public_wrapper_path"]
        target.parent.mkdir()
        target.write_text("candidate-owned wrapper")
    else:
        (workspace / spec["destination"]).symlink_to(source, target_is_directory=True)
    with pytest.raises(ValueError):
        entry.seed(spec, workspace)
    if what not in {"target", "link"}:
        assert not (workspace / spec["destination"]).exists()


@pytest.mark.parametrize("target_name", ["original_dd", "renamed_dd"])
def test_real_cmake_builds_full_candidate_alias_graph(tmp_path, target_name):
    if not shutil.which("cmake") or not shutil.which("c++"):
        pytest.skip("synthetic integration fixture needs CMake and a C++ compiler")
    workspace, source, spec = seed_fixture(tmp_path)
    (workspace / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.20)\nproject(tiny_candidate LANGUAGES CXX)\n"
        "add_library(ir STATIC ir.cpp)\nadd_library(MQT::CoreIR ALIAS ir)\n"
        f"add_library({target_name} STATIC dd.cpp added-production.cpp)\n"
        f"target_link_libraries({target_name} PUBLIC MQT::CoreIR)\n"
        f"add_library(MQT::CoreDD ALIAS {target_name})\n")
    (workspace / "ir.cpp").write_text("int input() {return 1;}\n")
    (workspace / "dd.cpp").write_text("int input(); int output() {return input();}\n")
    (workspace / "added-production.cpp").write_text("int added() {return 2;}\n")
    original = {p.name: file_hash(p) for p in workspace.glob("*.cpp")}
    entry.seed(spec, workspace)
    build = workspace / "build-native-public"
    commands = [
        ["cmake", "-S", str(workspace / adapter.PUBLIC_SOURCE96), "-B", str(build),
         "-DPRODUCTION_ROOT=" + str(workspace), "-DCMAKE_BUILD_TYPE=Debug",
         "-DCMAKE_CXX_FLAGS=-O0 -g --coverage"],
        ["cmake", "--build", str(build), "--target", "reviewbench-public-mqt", "-j2"],
    ]
    for command in commands:
        p = subprocess.run(command, capture_output=True, text=True, timeout=90)
        assert p.returncode == 0, p.stdout + p.stderr
    assert len(list(build.rglob("*.cpp.o"))) == 3
    assert {p.name: file_hash(p) for p in workspace.glob("*.cpp")} == original
