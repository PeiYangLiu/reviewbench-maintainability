"""Original Conan cache transport, source-preserving bootstrap, and graph controls."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_runtime_migration import native_conan as adapter
from agentic_runtime_migration import native_conan_entry as entry
from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n")
    return {"path": str(path), "sha256": entry.digest(path)}


@pytest.fixture
def seed_case(tmp_path):
    root = tmp_path / "seed"
    files = {}
    for name, data, mode in [
        ("p/cache.sqlite3", b"original dependency identity database", 0o644),
        ("version.txt", b"2.0.14", 0o644),
        ("p/package/p/bin/tool", b"original package executable", 0o755),
        ("p/package/p/lib/library.a", b"original complete dependency library", 0o644),
    ]:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)
        files[name] = {"sha256": entry.digest(path), "bytes": len(data), "mode": mode}
    links = {"p/package/p/lib/alias.a": "library.a"}
    (root / next(iter(links))).symlink_to("library.a")
    manifest = write_json(tmp_path / "manifest.json", {"files": files, "links": links, "records": {}})
    spec = {"schema_version": "reviewbench-original-conan/v1", "profile": "bsmpt4",
            "source": str(root), "manifest": manifest["path"], "manifest_sha256": manifest["sha256"],
            "configure": "configure-current-candidate", "build": "build-complete-candidate"}
    plugin_source = tmp_path / "plugins"
    plugin_files = {}
    for name in sorted(entry.PLUGIN_NAMES):
        path = plugin_source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# original synthetic plugin: " + name + "\n")
        plugin_files[name] = {"sha256": entry.digest(path), "bytes": path.stat().st_size, "mode": 0o644}
    spec.update(plugin_source=str(plugin_source), plugin_files=plugin_files)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "conanfile.py").write_text("candidate-recipe nlopt/2.9.1\n")
    return spec, workspace, root


def test_seed_copies_original_database_and_preserves_links(seed_case):
    spec, work, source = seed_case
    home = entry.seed(spec, work)
    assert (home / "p/cache.sqlite3").read_bytes() == (source / "p/cache.sqlite3").read_bytes()
    assert (home / "p/cache.sqlite3").stat().st_ino != (source / "p/cache.sqlite3").stat().st_ino
    assert (home / "p/package/p/lib/alias.a").readlink() == Path("library.a")
    assert (home / "p/package/p/bin/tool").stat().st_mode & 0o111
    (home / "p/cache.sqlite3").write_bytes(b"task-local Conan LRU update")
    assert entry.seed(spec, work) == home
    assert (source / "p/cache.sqlite3").read_bytes() == b"original dependency identity database"
    for name, row in spec["plugin_files"].items():
        assert entry.digest(home / name) == row["sha256"]
        assert (home / name).stat().st_ino != (Path(spec["plugin_source"]) / name).stat().st_ino


@pytest.mark.parametrize("mutation", ["missing", "extra", "bytes", "executable", "symlink", "different-marker"])
def test_original_plugin_configuration_is_a_complete_separate_layer(seed_case, mutation):
    spec, work, source = seed_case
    root = Path(spec["plugin_source"])
    name = sorted(spec["plugin_files"])[0]
    path = root / name
    if mutation == "missing":
        path.unlink()
    elif mutation == "extra":
        (root / "unexpected.py").write_text("# extra")
    elif mutation == "bytes":
        path.write_text("# different original plugin")
    elif mutation == "executable":
        path.chmod(0o755)
    elif mutation == "symlink":
        path.unlink()
        path.symlink_to(source / "version.txt")
    else:
        entry.seed(spec, work)
        spec["plugin_files"][name]["sha256"] = "0" * 64
    with pytest.raises(RuntimeError):
        entry.seed(spec, work)


@pytest.mark.parametrize("mutation", ["bytes", "executable", "extra", "missing", "link", "manifest"])
def test_seed_rejects_mutated_public_inputs(seed_case, mutation):
    spec, work, source = seed_case
    if mutation == "bytes":
        (source / "p/cache.sqlite3").write_bytes(b"changed")
    elif mutation == "executable":
        (source / "p/package/p/bin/tool").chmod(0o644)
    elif mutation == "extra":
        (source / "unregistered").write_text("extra")
    elif mutation == "missing":
        (source / "version.txt").unlink()
    elif mutation == "link":
        path = source / "p/package/p/lib/alias.a"
        path.unlink()
        path.symlink_to("../../../../../external")
    else:
        Path(spec["manifest"]).write_text("{}")
    with pytest.raises(RuntimeError):
        entry.seed(spec, work)


@pytest.mark.parametrize("name", ["../other", "/absolute", "a/../../other", "a//b", "./x", ""])
def test_cache_paths_are_relative_canonical_and_confined(name):
    with pytest.raises(RuntimeError):
        entry.relative(name)


def test_seed_rejects_an_unowned_existing_home(seed_case):
    spec, work, _ = seed_case
    (work / ".reviewbench/conan2").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="without its seed"):
        entry.seed(spec, work)


def test_seed_rejects_redirected_home(seed_case, tmp_path):
    spec, work, _ = seed_case
    (work / ".reviewbench").mkdir()
    (work / ".reviewbench/conan2").symlink_to(tmp_path)
    with pytest.raises(RuntimeError, match="escaped"):
        entry.seed(spec, work)


def test_seed_rejects_another_manifest_marker(seed_case):
    spec, work, _ = seed_case
    entry.seed(spec, work)
    (work / ".reviewbench/conan-seed.json").write_text("{}")
    with pytest.raises(RuntimeError, match="different inputs"):
        entry.seed(spec, work)


@pytest.mark.parametrize("status", [0, 1, 42])
def test_original_bsmpt_setup_edit_never_becomes_an_agent_patch(seed_case, status):
    spec, work, _ = seed_case
    recipe = work / "conanfile.py"
    # Preserve any actual candidate edits, not just the exact start recipe.
    original = b"candidate-added-requirement\nnlopt/2.9.1\n"
    recipe.write_bytes(original)
    recipe.chmod(0o744)
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        assert kwargs["cwd"] == work
        assert kwargs["env"]["CONAN_HOME"] == str(work / ".reviewbench/conan2")
        recipe.write_bytes(recipe.read_bytes().replace(b"nlopt/2.9.1", b"nlopt/2.7.1"))
        return SimpleNamespace(returncode=status)
    assert entry.configure(spec, work, run) == status
    assert len(calls) == 1 and recipe.read_bytes() == original
    assert recipe.stat().st_mode & 0o777 == 0o744


def test_original_bsmpt_recipe_restores_even_if_command_raises(seed_case):
    spec, work, _ = seed_case
    path = work / "conanfile.py"
    before = path.read_bytes()
    def broken(command, **kwargs):
        path.write_text("nlopt/2.7.1")
        raise OSError("external build interruption")
    with pytest.raises(OSError):
        entry.configure(spec, work, broken)
    assert path.read_bytes() == before


def test_check_reconfigures_the_whole_current_candidate_each_time(seed_case):
    spec, work, _ = seed_case
    calls = []
    def run(command, **kwargs):
        calls.append((command[-1], sorted(p.name for p in work.glob("*.cpp"))))
        return SimpleNamespace(returncode=0)
    (work / "old.cpp").write_text("int old_source;")
    assert entry.execute(spec, work, "check", run) == 0
    (work / "added.cpp").write_text("int candidate_added_source;")
    (work / "old.cpp").unlink()
    assert entry.execute(spec, work, "check", run) == 0
    assert calls == [
        (spec["configure"], ["old.cpp"]), (spec["build"], ["old.cpp"]),
        (spec["configure"], ["added.cpp"]), (spec["build"], ["added.cpp"]),
    ]


@pytest.mark.parametrize("mode,status,expected_calls", [("configure", 0, 1), ("check", 5, 1), ("check", 0, 2)])
def test_configure_and_build_status_are_not_overridden(seed_case, mode, status, expected_calls):
    spec, work, _ = seed_case
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=status)
    assert entry.execute(spec, work, mode, run) == status
    assert len(calls) == expected_calls


def test_public_bsmpt_preserves_original_options_but_excludes_private_harness():
    configure = ("#!/bin/bash\n# frozen configure comment\nset -e\n"
                 "sed -i 's#nlopt/2.9.1#nlopt/2.7.1#' conanfile.py\n"
                 "conan install . -s build_type=Debug -s compiler.cppstd=17 --build=missing\n"
                 "cmake -S . -B build-oracle -DCMAKE_CXX_FLAGS_DEBUG='-g -O0 --coverage -fprofile-abs-path'\n")
    build = ("source build-oracle/build/Debug/generators/conanbuild.sh\n"
             'cmake --build build-oracle --target Models -j"${CMAKE_BUILD_PARALLEL_LEVEL:-12}"\n'
             'g++ "$ROOT/harnesses/private.cpp"\n')
    public_configure, public_build = adapter.public_commands("bsmpt4", configure, build)
    assert "--coverage -fprofile-abs-path" in public_configure
    assert "-s build_type=Debug -s compiler.cppstd=17" in public_configure
    assert "nlopt/2.7.1" in public_configure
    assert public_configure.count("--no-remote") == 1
    assert "private.cpp" not in public_build and "--target Models" in public_build


def test_public_lapis_retains_full_original_target_and_compiler_wrapper():
    configure = ('set -euo pipefail\noracle_root=$1\nconan install . --build=missing -s build_type=Release\n'
                 'cmake -S . -B build-oracle -DCMAKE_CXX_COMPILER="$oracle_root/harnesses/lapis_coverage_g++" '
                 '-DBUILD_WITH_CLANG_TIDY=OFF\n')
    build = "cmake --build build-oracle --target silo_test -j12"
    current, public = adapter.public_commands("lapis16", configure, build)
    assert public == build and "conan install . --no-remote --build=missing -s build_type=Release" in current
    assert adapter.COMPILER in current and "/harnesses/" not in current
    assert "-DBUILD_WITH_CLANG_TIDY=OFF" in current
    assert "--gtest_filter" not in current + public
    assert current.count("--no-remote") == 1


@pytest.mark.parametrize("installs", ["", "conan install .\nconan install .\n"])
def test_offline_transport_does_not_guess_a_missing_or_ambiguous_install(installs):
    configure = ('set -euo pipefail\noracle_root=$1\n' + installs
                 + 'cmake -S . -DCMAKE_CXX_COMPILER="$oracle_root/harnesses/lapis_coverage_g++"\n')
    with pytest.raises(ContractError, match="command boundary"):
        adapter.public_commands("lapis16", configure, "cmake --build build-oracle --target silo_test")


@pytest.mark.parametrize("executable", [True, False])
def test_lapis_compiler_transport_preserves_the_original_executable_mode(tmp_path, executable):
    path = tmp_path / "native/harnesses/lapis_coverage_g++"
    path.parent.mkdir(parents=True)
    path.write_text('#!/usr/bin/env bash\nexec ccache /usr/bin/g++-11 "$@" --coverage -O0 -g\n')
    path.chmod(0o755 if executable else 0o644)
    calls = []
    def bundle(root, names, role):
        calls.append((root, names, role))
        return {"path": "compiler-archive.json", "sha256": "a" * 64}
    ctx = SimpleNamespace(legacy=tmp_path, bundle=bundle)
    if not executable:
        with pytest.raises(ContractError, match="executable mode"):
            adapter.compiler_asset(ctx)
    else:
        asset = adapter.compiler_asset(ctx)
        assert asset["kind"] == "archive" and asset["visibility"] == "developer"
        assert asset["target"] + "/" + path.name == adapter.COMPILER
        assert calls == [(path.parent, [path.name], "toolchain")]


@pytest.mark.parametrize("name", ["bsmpt4", "lapis16", "different"])
def test_unrecognized_original_shell_boundaries_fail_closed(name):
    with pytest.raises(ContractError):
        adapter.public_commands(name, "cmake --unknown", "different build")


@pytest.fixture
def binding_case(tmp_path, monkeypatch):
    declaration = b"original declared dependency recipe"
    helper = tmp_path / "native/harnesses/configure.sh"
    helper.parent.mkdir(parents=True)
    helper.write_text("original configuration")
    task = {"id": "original-id", "repo": "BSMPT/BSMPT", "start_sha": "original-start"}
    oracle = {"input_index": 4}
    plan = {"input_index": 4, "repo": task["repo"], "build_command": "original-build", "test_command": "original-test"}
    profile = {"id": task["id"], "repo": task["repo"], "index": 4, "start": task["start_sha"],
               "plan": canonical_hash(adapter.complete_plan(plan, tmp_path)),
               "harnesses": {"configure.sh": entry.digest(helper)}, "declaration": hashlib.sha256(declaration).hexdigest()}
    monkeypatch.setattr(adapter, "PROFILES", {"bsmpt4": profile})
    monkeypatch.setattr(adapter, "git", lambda *args, **kwargs: declaration)
    ctx = SimpleNamespace(legacy=tmp_path, repo=lambda task: tmp_path / "repo")
    return ctx, task, oracle, plan, helper


def test_complete_original_plan_binding(binding_case):
    ctx, task, oracle, plan, _ = binding_case
    assert adapter.bound_profile(ctx, task, oracle, plan)[0] == "bsmpt4"


@pytest.mark.parametrize("field", ["build_command", "test_command", "timeout_seconds", "source_patch_file", "expected_min_passed"])
def test_any_original_plan_field_change_is_rejected(binding_case, field):
    ctx, task, oracle, plan, _ = binding_case
    with pytest.raises(ContractError, match="complete original"):
        adapter.bound_profile(ctx, task, oracle, {**plan, field: "changed"})


@pytest.mark.parametrize("field", ["id", "start_sha"])
def test_task_identity_change_is_rejected(binding_case, field):
    ctx, task, oracle, plan, _ = binding_case
    with pytest.raises(ContractError, match="complete original"):
        adapter.bound_profile(ctx, {**task, field: "changed"}, oracle, plan)


def test_original_private_helper_change_is_rejected(binding_case):
    ctx, task, oracle, plan, helper = binding_case
    helper.write_text("different original assertion helper")
    with pytest.raises(ContractError, match="helper changed"):
        adapter.bound_profile(ctx, task, oracle, plan)


def test_original_dependency_declaration_change_is_rejected(binding_case, monkeypatch):
    ctx, task, oracle, plan, _ = binding_case
    monkeypatch.setattr(adapter, "git", lambda *args, **kwargs: b"different dependency declaration")
    with pytest.raises(ContractError, match="declaration changed"):
        adapter.bound_profile(ctx, task, oracle, plan)


def test_other_repositories_do_not_enter_the_adapter():
    assert adapter.migrate(None, {"repo": "elsewhere/project"}, {"input_index": 4}) is None


@pytest.mark.parametrize("mutation", ["changed", "missing", "symlink", "extra-key"])
def test_input_receipts_are_bound_to_real_immutable_files(tmp_path, mutation):
    path = tmp_path / "receipt.json"
    reference = write_json(path, {"value": "original"})
    if mutation == "changed":
        path.write_text("{}")
    elif mutation == "missing":
        path.unlink()
    elif mutation == "symlink":
        copy_path = tmp_path / "copy.json"
        copy_path.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(copy_path)
    else:
        reference["other"] = "unrecognized"
    with pytest.raises(ContractError):
        adapter.checked_receipt(reference, "test input")
