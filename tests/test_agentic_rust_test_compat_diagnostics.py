"""Mixed compiler failures must never discard an otherwise supported form."""
import copy
import json
from pathlib import Path

import pytest

from test_agentic_rust_test_compat import entry, mismatch, record, runtime_spec


def aborts_before_any_tests(tmp_path, mutate, *, first_compiles=False):
    root = tmp_path / "source"
    spec = runtime_spec(root)
    calls = []

    def run(command, *_):
        assert "--no-run" in command, "a failing/mixed probe must not run assertions"
        calls.append(command)
        if first_compiles and len(calls) == 1:
            return record()
        result = mismatch(Path.cwd())
        mutate(result)
        assert not entry.rename_mismatch(result, Path.cwd(), spec, Path.cwd().parent.name)
        return result

    with pytest.raises(entry.SelectionError) as error:
        entry.select(spec, root, tmp_path / "build", run=run)
    assert len(calls) == 1 + first_compiles
    assert error.value.evidence["tests"] == []
    assert error.value.evidence["selection_uses_test_results"] is False


@pytest.mark.parametrize("signal", list(range(1, 32)) + [34, 64])
def test_every_numbered_child_signal_mixed_with_attested_diagnostic_aborts(tmp_path, signal):
    def mutate(result):
        result["stderr"] = f"process didn't exit successfully (signal: {signal})"
        with pytest.raises(entry.SelectionError, match="infrastructure"):
            entry.infrastructure(result, {})
    aborts_before_any_tests(tmp_path, mutate)


@pytest.mark.parametrize("text", [
    "process didn't exit successfully (signal: 11, SIGSEGV: invalid memory reference)",
    "process didn't exit successfully (signal: 6, SIGABRT: process abort signal)",
    "process killed by signal", "terminated by a signal", "SIGTRAP", "SIGPIPE",
    "SIGRTMIN+1", "sigsegv", "sigabrt", "Segmentation fault",
    "Bus error", "Illegal instruction", "Floating point exception", "Aborted (core dumped)",
    "rustc exited with SIGXCPU", "signal 64",
])
def test_non_kill_termination_records_cannot_be_discarded(tmp_path, text):
    aborts_before_any_tests(tmp_path, lambda result: result.update(stderr=text))


@pytest.mark.parametrize("text", [
    "error: failed to run custom build command for `another-dependency v1.0.0`",
    "error: could not compile `another-dependency` due to previous error",
    "error: could not compile `frost-core` due to 3 previous errors",
    "error: failed to get `dependency` as a dependency of package `frost-core`",
    "error: failed to download `dependency v1.0.0`",
    "error: failed to select a version for `dependency`",
    "error: linking with `cc` failed: exit status: 1",
    "error: could not execute process `rustc` (never executed)",
    "failed to read dependency source",
    "warning: helper build script exited unsuccessfully",
    "rustc driver exited with status 1",
    "opaque non-JSON process output",
    "warning: build failed, waiting for other jobs to finish...\nconcurrent child failed",
])
@pytest.mark.parametrize("first_compiles", [False, True])
def test_mixed_dependency_build_script_or_unstructured_failure_is_fatal(tmp_path, text, first_compiles):
    aborts_before_any_tests(tmp_path, lambda result: result.update(stderr=text),
                            first_compiles=first_compiles)


@pytest.mark.parametrize("extra", [
    "error: unexpected raw compiler failure", "unstructured child output",
    '{"reason": "compiler-message"', "null", "[]", '""',
    '{"reason": "unknown-process-failure", "error": "failed"}',
    '{"reason": "build-script-executed", "success": false}',
    '{"reason": "compiler-artifact", "error": "concurrent failure"}',
    '{"reason": "build-finished", "success": true}',
    '{"reason": "build-finished", "success": false, "error": "failure"}',
])
@pytest.mark.parametrize("before", [False, True])
def test_entire_stdout_is_accounted_for_even_with_valid_json_error(tmp_path, extra, before):
    def mutate(result):
        result["stdout"] = ((extra + "\n" + result["stdout"]) if before
                            else (result["stdout"] + "\n" + extra))
    aborts_before_any_tests(tmp_path, mutate)


@pytest.mark.parametrize("case", ["foreign_abort", "foreign_note", "nested_error", "nested_signal",
                                "unknown_failure_note", "unexplained_error_code",
                                "missing_manifest", "malformed_code", "malformed_span",
                                "output_after_finished", "truncated_stdout"])
def test_no_unaccounted_failure_can_hide_inside_a_diagnostic_envelope(tmp_path, case):
    def mutate(result):
        rows = [json.loads(line) for line in result["stdout"].splitlines()]
        if case == "foreign_abort":
            extra = copy.deepcopy(rows[-1])
            extra["manifest_path"] = "other/Cargo.toml"
            rows.append(extra)
        elif case in {"foreign_note", "unknown_failure_note", "unexplained_error_code"}:
            extra = copy.deepcopy(rows[-1])
            extra["message"]["level"] = "failure-note"
            extra["message"]["message"] = "Some errors have detailed explanations: E0433."
            if case == "foreign_note":
                extra["manifest_path"] = "other/Cargo.toml"
            elif case == "unknown_failure_note":
                extra["message"]["message"] = "compiler driver failed after reporting E0433"
            else:
                extra["message"]["message"] = "Some errors have detailed explanations: E0308."
            rows.append(extra)
        elif case in {"nested_error", "nested_signal"}:
            rows[0]["message"]["children"] = [{
                "level": "error" if case == "nested_error" else "note",
                "message": "another compilation failed" if case == "nested_error" else "signal:\n11",
                "spans": [], "children": [],
            }]
        elif case == "missing_manifest":
            del rows[0]["manifest_path"]
        elif case == "malformed_code":
            rows[0]["message"]["code"] = ["E0433"]
        elif case == "malformed_span":
            rows[0]["message"]["spans"] = [None]
        elif case == "output_after_finished":
            rows.insert(0, {"reason": "build-finished", "success": False})
        else:
            result["stdout_total_bytes"] = len(result["stdout"]) + 10
            result["stdout_captured_bytes"] = len(result["stdout"])
        result["stdout"] = "\n".join(json.dumps(row) for row in rows)
    aborts_before_any_tests(tmp_path, mutate)


@pytest.mark.parametrize("stream", ["minimal", "cargo-progress", "cargo-numeric", "json-progress"])
def test_ordinary_attested_missing_name_still_discards_only_that_form(tmp_path, stream):
    root = tmp_path / "source"
    spec = runtime_spec(root)
    result = mismatch(root)
    if stream in {"cargo-progress", "cargo-numeric"}:
        result["stderr"] = (
            "    Checking helper v0.0.0 (/fixture/helper)\n"
            "   Compiling frost-core v0.0.0 (/fixture/frost-core)\n"
            "warning: `frost-core` (lib) generated 1 warning\n"
            "error: could not compile `frost-core` due to previous error\n"
            "warning: build failed, waiting for other jobs to finish...\n")
        if stream == "cargo-numeric":
            result["stderr"] = result["stderr"].replace("due to previous error", "due to 2 previous errors")
    elif stream == "json-progress":
        rows = [json.loads(line) for line in result["stdout"].splitlines()]
        note = copy.deepcopy(rows[-1])
        note["message"]["level"] = "failure-note"
        note["message"]["message"] = "For more information about this error, try `rustc --explain E0433`."
        rows += [note, {"reason": "build-script-executed", "package_id": "helper 0.0.0",
                        "linked_libs": [], "linked_paths": [], "cfgs": [], "env": [], "out_dir": "/fixture/out"},
                 {"reason": "compiler-artifact", "target": {}, "filenames": [], "fresh": False},
                 {"reason": "build-finished", "success": False}]
        result["stdout"] = "\n".join(json.dumps(row) for row in rows)
    entry.infrastructure(result, {})
    assert entry.rename_mismatch(result, root, spec, "unrenamed") is True


def test_signal_guard_does_not_confuse_public_signing_share_name_with_a_signal():
    entry.infrastructure(record(101, "SigningShare SharedSecret PublicVerificationShare"), {})
