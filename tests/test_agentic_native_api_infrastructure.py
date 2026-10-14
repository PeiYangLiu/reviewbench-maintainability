"""Process failures never become evidence for selecting another native API."""
import json
from pathlib import Path
import shlex
import shutil
import signal
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import native_api_entry as entry


def settings():
    return {"timeout_seconds": 10, "max_log_bytes": 1024 * 1024}


def python_command(program):
    return shlex.join([sys.executable, "-c", program])


@pytest.mark.parametrize("code", [124, 125, 126, 127, 128, 130, 134, 137, 139, 143])
def test_encoded_launch_timeout_and_signal_statuses_fail_closed(tmp_path, code):
    with pytest.raises(ValueError, match="infrastructure failed; no compatibility fallback"):
        entry.run_logged("exit " + str(code), tmp_path, "probe", settings(), echo=False)
    record = json.loads((tmp_path / "probe/command.json").read_text())
    assert record["returncode"] == code
    assert record["compiler_infrastructure_failure"] is True


@pytest.mark.parametrize("signum", [signal.SIGKILL, signal.SIGTERM])
def test_real_child_signal_preserved_by_shell_is_infrastructure(tmp_path, signum):
    command = python_command(
        "import os,signal; os.kill(os.getpid(), " + str(int(signum)) + ")"
    ) + '; compiler_exit=$?; exit "$compiler_exit"'
    with pytest.raises(ValueError, match="infrastructure failed"):
        entry.run_logged(command, tmp_path, "probe", settings(), echo=False)
    record = json.loads((tmp_path / "probe/command.json").read_text())
    assert record["returncode"] == 128 + signum
    assert record["compiler_infrastructure_failure"] is True


@pytest.mark.parametrize("diagnostic", [
    "g++: fatal error: Killed signal terminated program cc1plus\n",
    "/usr/bin/g++-11: fatal error: cannot execute 'cc1plus': execvp: No such file or directory\n",
    "source.cpp:17:3: internal compiler error: Segmentation fault\n",
    "clang++: error: unable to execute command: Killed\n",
    "clang-18: error: clang frontend command failed due to signal\n",
    "clang++: error: linker command failed due to signal\n",
    "LLVM ERROR: out of memory\n",
])
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_driver_wrapped_crashes_are_not_normal_compile_errors(tmp_path, diagnostic, stream):
    command = python_command(
        "import sys; sys." + stream + ".write(" + repr(diagnostic) + "); sys.exit(1)"
    )
    with pytest.raises(ValueError, match="infrastructure failed"):
        entry.run_logged(command, tmp_path, "probe", settings(), echo=False)
    record = json.loads((tmp_path / "probe/command.json").read_text())
    assert record["returncode"] == 1
    assert record["compiler_infrastructure_failure"] is True


@pytest.mark.parametrize("diagnostic", [
    "api.cpp:5:2: error: no member named 'new_api' in 'Candidate'\n",
    "api.cpp:1:1: error: #error Killed signal terminated program cc1plus\n",
    "api.cpp:1:1: error: #error internal compiler error: this is user source\n",
])
def test_regular_api_diagnostics_remain_compile_incompatibility(tmp_path, diagnostic):
    command = python_command("import sys; sys.stderr.write(" + repr(diagnostic) + "); sys.exit(1)")
    record = entry.run_logged(command, tmp_path, "probe", settings(), echo=False)
    assert record["returncode"] == 1
    assert record["compiler_infrastructure_failure"] is False


def test_actual_gcc_driver_cannot_hide_killed_frontend_as_exit_one(tmp_path):
    compiler = shutil.which("g++")
    if not compiler:
        pytest.skip("this compiler-driver fixture requires local GCC")
    hook = tmp_path / "cc1plus"
    hook.write_text("#!/bin/sh\nkill -KILL $$\n")
    hook.chmod(0o755)
    command = shlex.join([compiler, "-B", str(tmp_path) + "/", "-fsyntax-only", "-x", "c++", "/dev/null"])
    with pytest.raises(ValueError, match="infrastructure failed"):
        entry.run_logged(command, tmp_path, "probe", settings(), echo=False)
    record = json.loads((tmp_path / "probe/command.json").read_text())
    assert record["returncode"] == 1
    assert record["compiler_infrastructure_failure"] is True
    assert "Killed signal terminated program cc1plus" in (tmp_path / "probe/stderr").read_text()


def public_spec(first_probe, second_probe):
    return entry.validate({
        **settings(), "schema_version": entry.SCHEMA, "kind": "public",
        "state_directory": "build-native-api-public", "setup": [],
        "forms": [
            {"name": "first-api", "probe": {"command": first_probe}},
            {"name": "second-api", "probe": {"command": second_probe}},
        ],
    })


@pytest.mark.parametrize("first_probe", [
    "exit 137",
    python_command("import sys; sys.stderr.write('g++: fatal error: Killed signal terminated program cc1plus\\n'); sys.exit(1)"),
])
def test_failed_infrastructure_does_not_select_or_attempt_the_next_form(tmp_path, monkeypatch, first_probe):
    monkeypatch.chdir(tmp_path)
    spec = public_spec(first_probe, "touch second-probe-ran")
    with pytest.raises(ValueError, match="infrastructure failed"):
        entry.prepare(spec, tmp_path)
    assert not (tmp_path / "second-probe-ran").exists()
    assert not (tmp_path / "build-native-api-public/selection.json").exists()


def test_regular_compile_failure_still_allows_the_other_api(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    spec = public_spec("exit 1", "true")
    result = entry.prepare(spec, tmp_path)
    assert result["selected"] == ["second-api"]
    assert result["builds_completed"] is True
