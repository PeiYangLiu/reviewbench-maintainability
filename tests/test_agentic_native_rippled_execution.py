"""Small real process failures and offline setup fixtures, not calibration."""
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import native_rippled_entry as entry
from agentic_runtime_migration import native_rippled_prepare as prepare
from reviewbench_maintainability.agentic.contracts import ContractError
from agentic_runtime_migration.native_rippled import public_spec
from test_agentic_native_rippled import modern_log, cache, RREV, PREV, PKGID


@pytest.mark.parametrize("code", [1, 2, 124, 126, 127, 137, 143])
def test_every_nonzero_is_an_infrastructure_preparation_error_with_original_output(tmp_path, code):
    cmd = [sys.executable, "-c", "import sys; print('original-output'); sys.exit(" + str(code) + ")"]
    with pytest.raises(ContractError, match="failed"):
        prepare.run_logged(cmd, tmp_path, "failure")
    record = json.loads((tmp_path / "failure.json").read_text())
    assert record["exit_status"] == code
    assert (tmp_path / "failure.stdout").read_text() == "original-output\n"
    assert record["status"] == "terminal"


@pytest.mark.parametrize("sig", [signal.SIGKILL, signal.SIGTERM])
def test_real_signal_preserves_terminal_receipt_and_never_becomes_optional(tmp_path, sig):
    cmd = [sys.executable, "-c", f"import os,signal;os.kill(os.getpid(),{int(sig)})"]
    with pytest.raises(ContractError, match="failed"):
        prepare.run_logged(cmd, tmp_path, "signal")
    assert json.loads((tmp_path / "signal.json").read_text())["exit_status"] == -int(sig)


def test_existing_attempt_is_not_overwritten_or_restarted(tmp_path):
    record = tmp_path / "command.json"
    record.write_text('{"status":"retained_original"}\n')
    with pytest.raises(ContractError, match="evidence exists"):
        prepare.run_logged([sys.executable, "-c", "raise SystemExit(0)"], tmp_path, "command")
    assert record.read_text() == '{"status":"retained_original"}\n'


def test_public_entry_never_swallows_process_errors():
    with pytest.raises(subprocess.CalledProcessError) as error:
        entry.run([sys.executable, "-c", "raise SystemExit(137)"], env=os.environ.copy())
    assert error.value.returncode == 137


def test_production_immutability_check_ignores_only_new_generated_files(tmp_path):
    (tmp_path / "original.cpp").write_text("original source")
    before = prepare.source_files(tmp_path)
    (tmp_path / "build-rippled-dependencies").mkdir()
    (tmp_path / "build-rippled-dependencies/cache").symlink_to("/nonexistent/immutable-asset")
    assert prepare.source_files(tmp_path, before) == before
    (tmp_path / "original.cpp").write_text("changed production")
    assert prepare.source_files(tmp_path, before) != before


def test_container_arguments_bind_shared_gate_and_never_have_network_or_host_cache_write():
    _, command = prepare.container_command("sha256:" + "a" * 64, {"lease": "synthetic"})
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--memory") + 1] == "4g"
    assert command[command.index("--cpus") + 1] == "2"
    assert "reviewbench.resource-lease=synthetic" in command
    assert "--read-only" in command and "--cpuset-cpus" in command


def test_cache_home_rejects_source_prefix_and_symlink_alias(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    external = tmp_path / "home"
    external.mkdir()
    assert entry.cache_home(source, external) == external / "rippled-conan"
    for path in (source, source / "cache"):
        with pytest.raises(ValueError, match="outside"):
            entry.cache_home(source, path)
    alias = tmp_path / "alias"
    alias.symlink_to(source, target_is_directory=True)
    with pytest.raises(ValueError, match="outside"):
        entry.cache_home(source, alias)
    with pytest.raises(ValueError, match="absolute"):
        entry.cache_home(source, Path("relative"))


def test_current_public_lock_preserves_skipped_build_requirements_and_bytes(tmp_path):
    source, state = tmp_path / "source", tmp_path / "state"
    source.mkdir()
    state.mkdir()
    graph = cache.graph_from_log(modern_log(), 2)
    lock = {"version": "0.5", "requires": ["zlib/1.2.13#" + RREV],
            "build_requires": ["zlib/1.2.13#" + RREV], "python_requires": []}
    original = json.dumps(lock, indent=4).encode() + b"\n"
    (source / "conan.lock").write_bytes(original)
    selected, before = entry.select_lock(source, state, "current", graph)
    assert selected == source / "conan.lock"
    assert before == selected.read_bytes() == original
    assert not (state / "conan.lock").exists()
    assert entry.dependency_lock(graph)["build_requires"] == []
    assert json.loads(selected.read_bytes())["build_requires"] == lock["build_requires"]


@pytest.mark.parametrize("bad", [b"not JSON", b'{"version":"0.1"}',
                               b'{"version":"0.5","requires":"not a list"}'])
def test_invalid_current_lock_never_falls_back_to_derived_lock(tmp_path, bad):
    state = tmp_path / "state"
    state.mkdir()
    (tmp_path / "conan.lock").write_bytes(bad)
    with pytest.raises(ValueError):
        entry.select_lock(tmp_path, state, "current", cache.graph_from_log(modern_log(), 2))
    assert not (state / "conan.lock").exists()


@pytest.mark.parametrize("key", ["directory", "stobject", "confidential", "view"])
def test_complete_entry_keeps_original_generator_paths_and_current_lock(tmp_path, monkeypatch, key):
    source, home, public, dependencies = [tmp_path / name for name in ("source", "home", "public", "deps")]
    for path in (source, home, public, dependencies):
        path.mkdir()
    (source / "src").mkdir()
    recipe = b"# current recipe\n"
    (source / "conanfile.py").write_bytes(recipe)
    spec = public_spec(key)
    graph = cache.graph_from_log(modern_log(), 2)
    major = 1 if key == "stobject" else 2
    graph["major"] = major
    (dependencies / "graph.json").write_text(json.dumps(graph))
    (public / "public.json").write_text(json.dumps(spec))
    if key in {"confidential", "view"}:
        (source / "conan.lock").write_text(json.dumps({
            "version": "0.5", "requires": ["zlib/1.2.13#" + RREV],
            "build_requires": ["zlib/1.2.13#" + RREV]}))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(entry.subprocess, "check_output",
                        lambda *args, **kw: "Conan version " + spec["conan_version"] + "\n")
    calls = []

    def private(template, destination, major):
        (destination / "profiles").mkdir(parents=True)

    def run(arguments, *, env):
        calls.append((arguments, env))
        if arguments[:2] != ["conan", "install"]:
            return
        assert "--build=never" in arguments
        output = next(value.split("=", 1)[1] for value in arguments if value.startswith("--output-folder="))
        expected = source if major == 1 else source / "build-oracle/conan"
        assert Path(output) == expected
        toolchain = Path(output) / "build/generators/conan_toolchain.cmake"
        toolchain.parent.mkdir(parents=True, exist_ok=True)
        toolchain.write_text("# generated by the pinned Conan fixture\n")
        if major == 2:
            assert "--no-remote" in arguments
            report = Path(next(value.split("=", 1)[1] for value in arguments if value.startswith("--out-file=")))
            report.write_text(json.dumps({"graph": {"nodes": {"1": {
                "ref": "zlib/1.2.13#" + RREV, "recipe": "Cache", "binary": "Cache",
                "package_id": PKGID, "prev": PREV, "context": "host"}}}}))

    monkeypatch.setattr(entry, "private_cache", private)
    monkeypatch.setattr(entry, "run", run)
    entry.prepare(public, dependencies, cwd=source)
    check = json.loads((source / "build-rippled-dependencies/dependency-check.json").read_text())
    assert check["status"] == "resolved"
    assert not Path(check["cache_outside_candidate"]).is_relative_to(source)
    assert (source / "conanfile.py").read_bytes() == recipe
    if key in {"confidential", "view"}:
        assert check["lock_origin"] == "current_candidate"
        assert any("--lockfile=" + str(source / "conan.lock") in args for args, _ in calls)
    if major == 1:
        assert Path(check["toolchain_path"]) == source / "build/generators/conan_toolchain.cmake"
        assert not (source / "build/build").exists()


def test_real_cmake_export_rejects_in_source_dependency_and_accepts_external_cache(tmp_path):
    if any(shutil.which(tool) is None for tool in ("cmake", "c++")):
        pytest.skip("small real export fixture requires CMake and C++")
    source = tmp_path / "source"
    source.mkdir()
    (source / "one.cpp").write_text("int one() { return 1; }\n")
    (source / "two.cpp").write_text("int two() { return 2; }\n")
    (source / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.14)\nproject(Fixture LANGUAGES CXX)\n"
        "add_library(current STATIC one.cpp two.cpp)\n"
        'target_include_directories(current INTERFACE "${DEPENDENCY_INCLUDE}")\n'
        "install(TARGETS current EXPORT Current)\ninstall(EXPORT Current DESTINATION lib/cmake)\n")
    bad = source / "old-cache/include"
    good = entry.cache_home(source, tmp_path / "home") / "include"
    for path in (bad, good):
        path.mkdir(parents=True)

    def configure(build, include):
        return subprocess.run(["cmake", "-S", str(source), "-B", str(build),
                               "-DDEPENDENCY_INCLUDE=" + str(include)],
                              capture_output=True, text=True, timeout=60)

    rejected = configure(source / "bad-build", bad)
    assert rejected.returncode != 0 and "prefixed in the source directory" in rejected.stderr
    accepted = configure(source / "good-build", good)
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    compiled = subprocess.run(["cmake", "--build", str(source / "good-build"), "--parallel", "1"],
                              capture_output=True, text=True, timeout=60)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
