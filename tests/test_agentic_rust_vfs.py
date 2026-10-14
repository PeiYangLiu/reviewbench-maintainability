"""VFS61 selector controls; these do not execute the original VFS candidate."""
import copy
import json
import os
from pathlib import Path
import shutil
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_api, rust_overlay, rust_vfs
from agentic_runtime_migration import rust_vfs_entry as entry
from reviewbench_maintainability.agentic.contracts import ContractError

ORIGINAL = b"""use async_std::io::{ReadExt, WriteExt};
use vfs::async_vfs::{MemoryFS, VfsPath};

#[test]
fn in_memory_async_path_round_trips_file_contents() {
    async_std::task::block_on(async {
        let root = VfsPath::new(MemoryFS::new());
        let directory = root.join("nested").unwrap();
        directory.create_dir().await.unwrap();
        let file = directory.join("message.txt").unwrap();

        let mut writer = file.create_file().await.unwrap();
        writer.write_all(b"reviewbench").await.unwrap();
        drop(writer);

        assert!(file.exists().await.unwrap());
        let mut contents = String::new();
        file.open_file()
            .await
            .unwrap()
            .read_to_string(&mut contents)
            .await
            .unwrap();
        assert_eq!(contents, "reviewbench");
    });
}
"""


def fixture_source(root):
    root.mkdir()
    (root / "tests").mkdir()
    (root / "src").mkdir()
    (root / "Cargo.toml").write_text('[package]\nname="vfs"\nversion="0.1.0"\nedition="2021"\n'
                                    '[features]\nasync-vfs=[]\n')
    (root / "Cargo.lock").write_text("version = 4\n")
    (root / "src/lib.rs").write_text("pub fn current_production() -> u8 { 7 }\n")
    for name, data in zip(entry.NAMES, (ORIGINAL, ORIGINAL.replace(
            b"MemoryFS", b"AsyncMemoryFS").replace(b"VfsPath", b"AsyncVfsPath"))):
        path = f"tests/{name}.rs"
        assert entry.guard.digest(data) == entry.HARNESSES[path]
        (root / path).write_bytes(data)
    return {
        "schema_version": entry.SCHEMA, "profile": entry.PROFILE, "task_id": entry.TASK,
        "plan_sha256": entry.PLAN, "toolchain": "1.93.0", "workdir": ".",
        "files": [{"path": p, "sha256": h} for p, h in entry.HARNESSES.items()],
        "forms": [{"name": n, "target": n, "harness": f"tests/{n}.rs",
                   "command": ["cargo", "--offline", *entry.arguments(n)]} for n in entry.NAMES],
        "expected_tests": list(entry.EXPECTED), "selection": entry.guard.SELECTION,
        "cpus": 2, "timeout_seconds": 120, "max_log_bytes": 2 * 1024**2,
    }


def record(code=0, stdout=None, stderr="", **extra):
    return {"returncode": code, "stdout": stdout if stdout is not None else
            '{"reason":"build-finished","success":true}\n',
            "stderr": stderr, "limit": None, **extra}


def graph(root):
    names = [*entry.NAMES, *("__reviewbench_vfs_api_" + n for n in entry.NAMES)]
    targets = [{"name": "vfs", "kind": ["lib"], "src_path": str(root / "src/lib.rs")}]
    targets += [{"name": name, "kind": ["test"], "src_path": str(root / "tests" / (name + ".rs")),
                 "test": True} for name in names]
    return record(stdout=json.dumps({
        "workspace_root": str(root), "workspace_members": ["vfs"],
        "packages": [{"id": "vfs", "source": None, "manifest_path": str(root / "Cargo.toml"),
                      "targets": targets}],
        "resolve": {"nodes": [{"id": "vfs", "dependencies": []}]},
    }))


def mismatch(root, form):
    data = (root / form["harness"]).read_bytes()
    token = entry.TOKENS[form["name"]][0]
    at = data.index(token.encode())
    error = {
        "reason": "compiler-message", "manifest_path": str(root / "Cargo.toml"),
        "package_id": "vfs", "target": {"kind": ["test"], "name": form["target"],
                                       "src_path": str(root / form["harness"])},
        "message": {"level": "error", "message": f"unresolved import `{token}`",
                    "code": {"code": "E0432"}, "children": [],
                    "spans": [{"is_primary": True, "file_name": str(root / form["harness"]),
                               "byte_start": at, "byte_end": at + len(token)}]},
    }
    return record(101, json.dumps(error) + '\n{"reason":"build-finished","success":false}\n',
                  f'error: could not compile `vfs` (test "{form["target"]}") due to 1 previous error\n')


def outcome(passed=True):
    return record(0 if passed else 101,
                  "test " + entry.EXPECTED[0] + " ... " + ("ok" if passed else "FAILED")
                  + "\ntest result: " + ("ok." if passed else "FAILED.") + "\n")


@pytest.mark.parametrize("field", ["task_id", "profile", "plan_sha256", "toolchain", "workdir",
                                  "files", "expected_tests", "selection", "command", "prefix",
                                  "cpus", "timeout_seconds", "outcome", "reference_hash"])
def test_unknown_mutated_or_outcome_dependent_spec_never_runs(tmp_path, field):
    root = tmp_path / "source"
    spec = fixture_source(root)
    if field in {"files", "expected_tests"}:
        spec[field] = []
    elif field in {"command", "prefix"}:
        spec["forms"][0]["command"].insert(2, "--skip" if field == "command" else "--config=bad")
    elif field in {"cpus", "timeout_seconds"}:
        spec[field] = 9999
    else:
        spec[field] = "untrusted"
    with pytest.raises(entry.SelectionError):
        entry.select(spec, root, tmp_path / "forms", run=lambda *a: pytest.fail("command before spec guard"))


@pytest.mark.parametrize("selected,label", [("oracle_start", "golden"), ("oracle_golden", "start")])
def test_only_current_compile_result_selects_harness_before_assertions(tmp_path, monkeypatch, selected, label):
    root = tmp_path / "source"
    spec = fixture_source(root)
    monkeypatch.setenv("ORACLE_VARIANT", label)
    calls = []
    def run(command, *_):
        if "metadata" in command:
            calls.append(("graph", None))
            assert "--locked" in command and "--no-deps" not in command
            return graph(Path.cwd())
        form = next(f for f in spec["forms"] if f["name"] == Path.cwd().parent.name)
        if command[command.index("--test") + 1].startswith("__reviewbench_vfs_api_"):
            calls.append(("api-probe", form["name"]))
            return record() if form["name"] == selected else mismatch(Path.cwd(), entry.surface_form(form))
        calls.append(("probe" if "--no-run" in command else "tests", form["name"]))
        if "--no-run" in command:
            return record()
        return outcome()
    result = entry.select(spec, root, tmp_path / "forms", run=run)
    assert result["selected"] == [selected] and all(result["results"].values())
    assert calls == [("graph", None), ("api-probe", "oracle_start"), ("api-probe", "oracle_golden"),
                     ("probe", selected), ("tests", selected)]


@pytest.mark.parametrize("first,second", [(False, True), (True, False), (True, True)])
def test_all_compilable_forms_execute_conjunctively_even_after_assertion_failure(tmp_path, first, second):
    root = tmp_path / "source"
    spec = fixture_source(root)
    calls = []
    def run(command, *_):
        if "metadata" in command:
            return graph(Path.cwd())
        form = Path.cwd().parent.name
        if command[command.index("--test") + 1].startswith("__reviewbench_vfs_api_"):
            calls.append(("api-probe", form))
            return record()
        calls.append(("probe" if "--no-run" in command else "tests", form))
        return record() if "--no-run" in command else outcome(first if form == "oracle_start" else second)
    result = entry.select(spec, root, tmp_path / "forms", run=run)
    assert calls == [("api-probe", "oracle_start"), ("api-probe", "oracle_golden"),
                     ("probe", "oracle_start"), ("probe", "oracle_golden"),
                     ("tests", "oracle_start"), ("tests", "oracle_golden")]
    assert all(result["results"].values()) == (first and second)


@pytest.mark.parametrize("failure", [
    "signal6", "signal9", "signal11", "signal15", "shell137", "negative", "driver1",
    "custom_build", "ice", "disk", "timeout", "truncated", "garbage", "bad_json",
    "missing_finish", "duplicate_finish", "unknown_stderr", "wrong_count",
    "foreign_manifest", "production_path", "foreign_target", "other_token",
    "method_error", "zero_error", "zero_background_failure",
])
def test_complete_mixed_failure_stream_aborts_before_reselection(tmp_path, failure):
    root = tmp_path / "source"
    spec = fixture_source(root)
    calls = []
    def run(command, *_):
        if "metadata" in command:
            return graph(Path.cwd())
        calls.append("probe" if "--no-run" in command else "tests")
        form = entry.surface_form(spec["forms"][0])
        r = mismatch(Path.cwd(), form)
        if failure.startswith("signal"):
            r["stderr"] += "process failed (signal: " + failure[6:] + ")"
        elif failure in {"shell137", "negative", "driver1"}:
            r["returncode"] = {"shell137": 137, "negative": -11, "driver1": 1}[failure]
        elif failure in {"custom_build", "ice", "disk", "unknown_stderr"}:
            r["stderr"] += {"custom_build": "failed to run custom build command for dependency",
                            "ice": "internal compiler error", "disk": "No space left on device",
                            "unknown_stderr": "unaccounted dependency failure"}[failure]
        elif failure == "timeout":
            r["limit"] = "deadline"
        elif failure == "truncated":
            r.update(stdout_total_bytes=1000, stdout_captured_bytes=100)
        elif failure in {"garbage", "bad_json", "duplicate_finish"}:
            r["stdout"] += {"garbage": "raw text", "bad_json": '{"unfinished":',
                            "duplicate_finish": '{"reason":"build-finished","success":false}'}[failure]
        elif failure == "missing_finish":
            r["stdout"] = r["stdout"].splitlines()[0]
        elif failure == "wrong_count":
            r["stderr"] = r["stderr"].replace("1 previous", "2 previous")
        elif failure == "zero_background_failure":
            r = record(stderr="warning: build failed, waiting for other jobs to finish...\n")
        else:
            rows = [json.loads(x) for x in r["stdout"].splitlines()]
            e = rows[0]
            if failure == "foreign_manifest":
                e["manifest_path"] = str(Path.cwd() / "other/Cargo.toml")
            elif failure == "production_path":
                e["message"]["spans"][0]["file_name"] = str(Path.cwd() / "src/lib.rs")
            elif failure == "foreign_target":
                e["target"]["kind"] = ["lib"]
            elif failure == "other_token":
                e["message"]["spans"][0].update(byte_start=0, byte_end=3)
            elif failure == "method_error":
                e["message"]["code"]["code"] = "E0599"
            elif failure == "zero_error":
                r["returncode"] = 0
                rows[-1]["success"] = True
            r["stdout"] = "\n".join(map(json.dumps, rows)) + "\n"
        return r
    with pytest.raises(entry.SelectionError):
        entry.select(spec, root, tmp_path / "forms", run=run)
    assert calls == ["probe"]


def test_supported_api_with_full_harness_type_error_aborts_instead_of_reselecting(tmp_path):
    root = tmp_path / "source"
    spec = fixture_source(root)
    calls = []
    def run(command, *_):
        if "metadata" in command:
            return graph(Path.cwd())
        target = command[command.index("--test") + 1]
        calls.append(target)
        if target.startswith("__reviewbench_vfs_api_"):
            return record()
        result = mismatch(Path.cwd(), spec["forms"][0])
        rows = [json.loads(line) for line in result["stdout"].splitlines()]
        rows[0]["message"]["code"]["code"] = "E0282"
        rows[0]["message"]["message"] = "type annotations needed"
        result["stdout"] = "\n".join(map(json.dumps, rows))
        return result
    with pytest.raises(entry.SelectionError, match="complete original"):
        entry.select(spec, root, tmp_path / "forms", run=run)
    assert calls == [entry.surface_form(f)["target"] for f in spec["forms"]] + ["oracle_start"]


def test_generated_surface_probe_collision_is_not_allowed_to_replace_source(tmp_path):
    root = tmp_path / "source"
    spec = fixture_source(root)
    path = root / entry.surface_form(spec["forms"][0])["harness"]
    path.write_text("candidate-owned file")
    with pytest.raises(entry.SelectionError, match="collides"):
        entry.select(spec, root, tmp_path / "forms", run=lambda *a: pytest.fail("command before collision guard"))
    assert path.read_text() == "candidate-owned file"


@pytest.mark.parametrize("change", ["redirect-original", "redirect-probe", "disable",
                                  "missing", "duplicate", "foreign-library"])
def test_metadata_binds_cargo_targets_to_original_harness_and_current_library(tmp_path, change):
    root = tmp_path / "source"
    spec = fixture_source(root)
    calls = []
    def run(command, *_):
        calls.append(command)
        assert "metadata" in command
        r = graph(Path.cwd())
        value = json.loads(r["stdout"])
        targets = value["packages"][0]["targets"]
        if change in {"redirect-original", "redirect-probe"}:
            targets[1 if change == "redirect-original" else 3]["src_path"] = str(Path.cwd() / "tests/other.rs")
        elif change == "disable":
            targets[1]["test"] = False
        elif change == "missing":
            targets.pop(1)
        elif change == "duplicate":
            targets.append(copy.deepcopy(targets[1]))
        else:
            targets[0]["name"] = "candidate_not_tested"
        r["stdout"] = json.dumps(value)
        return r
    with pytest.raises(entry.SelectionError):
        entry.select(spec, root, tmp_path / "forms", run=run)
    assert len(calls) == 1


@pytest.mark.parametrize("text", [
    "SIGINT", "SIGTERM", "SIGSEGV", "signal: 11", "internal compiler error",
    "failed to run custom build command", "No space left on device",
])
@pytest.mark.parametrize("field", ["keywords", "description", "metadata"])
def test_metadata_diagnostic_looking_package_data_is_not_a_process_failure(tmp_path, text, field):
    root = tmp_path / "source"
    spec = fixture_source(root)
    r = graph(root)
    value = json.loads(r["stdout"])
    value["packages"][0][field] = ([text] if field == "keywords" else
                                   {"description": text} if field == "metadata" else text)
    r["stdout"] = json.dumps(value)
    size = len(r["stdout"].encode())
    r.update(stdout_total_bytes=size, stdout_captured_bytes=size,
             stdout_sha256=entry.guard.digest(r["stdout"].encode()),
             stderr_total_bytes=0, stderr_captured_bytes=0)
    before, evidence = copy.deepcopy(r), {}
    entry.verify_graph(spec, root, tmp_path, lambda *a: r, time.monotonic() + 30, evidence)
    assert evidence["graph"] is r
    assert r == before  # Never blank, truncate, or replace the retained record.
    assert json.loads(evidence["graph"]["stdout"]) == value


@pytest.mark.parametrize("failure", [
    "stderr_SIGINT", "stderr_signal11", "stderr_unknown", "rc1", "rc101", "rc124",
    "rc126", "rc127", "rc137", "rc_negative2", "rc_negative9", "rc_negative15",
    "rc_not_int", "stdout_truncated", "stderr_truncated", "deadline", "output_limit",
])
def test_metadata_process_status_stderr_limits_and_capture_counters_remain_guarded(tmp_path, failure):
    root = tmp_path / "source"
    spec = fixture_source(root)
    r = graph(root)
    data = json.loads(r["stdout"])
    data["packages"][0]["keywords"] = ["SIGINT"]
    r["stdout"] = json.dumps(data)
    size = len(r["stdout"].encode())
    r.update(stdout_total_bytes=size, stdout_captured_bytes=size,
             stderr_total_bytes=0, stderr_captured_bytes=0)
    if failure.startswith("stderr_") and failure != "stderr_truncated":
        r["stderr"] = {"stderr_SIGINT": "process terminated by SIGINT",
                       "stderr_signal11": "process failed (signal: 11)",
                       "stderr_unknown": "unaccounted Cargo stderr"}[failure]
        r["stderr_total_bytes"] = r["stderr_captured_bytes"] = len(r["stderr"].encode())
    elif failure.startswith("rc_negative"):
        r["returncode"] = -int(failure[len("rc_negative"):])
    elif failure == "rc_not_int":
        r["returncode"] = False
    elif failure.startswith("rc"):
        r["returncode"] = int(failure[2:])
    elif failure.endswith("_truncated"):
        r[failure.split("_")[0] + "_total_bytes"] += 1
    else:
        r["limit"] = failure
    before, evidence = copy.deepcopy(r), {}
    with pytest.raises(entry.SelectionError):
        entry.verify_graph(spec, root, tmp_path, lambda *a: r, time.monotonic() + 30, evidence)
    assert evidence["graph"] is r and r == before


@pytest.mark.parametrize("prefix,suffix", [
    ("SIGINT\n", ""), ("not JSON\n", ""), ("", "\nSIGINT"), ("", "\nnot JSON"),
    ("", "\n{}"), ("{}", ""), ("\ufeff", ""),
])
def test_metadata_entire_stdout_requires_one_valid_json_document(tmp_path, prefix, suffix):
    root = tmp_path / "source"
    spec = fixture_source(root)
    r = graph(root)
    r["stdout"] = prefix + r["stdout"] + suffix
    before, evidence = copy.deepcopy(r), {}
    with pytest.raises(entry.SelectionError, match="invalid framing"):
        entry.verify_graph(spec, root, tmp_path, lambda *a: r, time.monotonic() + 30, evidence)
    assert evidence["graph"] is r and r == before


@pytest.mark.parametrize("phase", ["api-probe", "harness-probe", "tests"])
def test_metadata_exception_does_not_relax_any_compile_or_test_stdout_guard(tmp_path, phase):
    root = tmp_path / "source"
    spec = fixture_source(root)
    reached = []
    def run(command, *_):
        if "metadata" in command:
            r = graph(Path.cwd())
            value = json.loads(r["stdout"])
            value["packages"][0]["keywords"] = ["SIGINT"]
            r["stdout"] = json.dumps(value)
            return r
        kind = ("api-probe" if command[command.index("--test") + 1].startswith("__reviewbench_vfs_api_")
                else "harness-probe" if "--no-run" in command else "tests")
        reached.append(kind)
        r = record() if "--no-run" in command else outcome()
        if kind == phase:
            r["stdout"] += "\nSIGINT\n"
        return r
    with pytest.raises(entry.SelectionError, match="infrastructure failure"):
        entry.select(spec, root, tmp_path / "forms", run=run)
    assert reached[-1] == phase


@pytest.mark.parametrize("change", ["harness", "symlink", "old_target", "missing_lock"])
def test_source_guards_precede_compiler(tmp_path, change):
    root = tmp_path / "source"
    spec = fixture_source(root)
    path = root / "tests/oracle_start.rs"
    if change == "harness":
        path.write_bytes(ORIGINAL.replace(b"reviewbench", b"different"))
    elif change == "symlink":
        path.unlink()
        path.symlink_to("oracle_golden.rs")
    elif change == "old_target":
        (root / "target").mkdir()
        (root / "target/old.rlib").write_bytes(b"old candidate output")
    else:
        (root / "Cargo.lock").unlink()
    with pytest.raises(entry.SelectionError):
        entry.select(spec, root, tmp_path / "forms", run=lambda *a: pytest.fail("compiler before input guard"))


@pytest.mark.parametrize("phase", ["graph", "probe", "tests"])
@pytest.mark.parametrize("path", ["src/lib.rs", "Cargo.lock", "tests/oracle_start.rs"])
def test_source_lock_or_harness_mutation_always_aborts(tmp_path, phase, path):
    root = tmp_path / "source"
    spec = fixture_source(root)
    def run(command, *_):
        kind = "graph" if "metadata" in command else "probe" if "--no-run" in command else "tests"
        if kind == phase:
            (Path.cwd() / path).write_text("changed by a child")
        return graph(Path.cwd()) if kind == "graph" else record() if kind == "probe" else outcome()
    with pytest.raises(entry.SelectionError, match="changed"):
        entry.select(spec, root, tmp_path / "forms", run=run)


def original_inputs():
    name = os.environ.get("VFS_ORIGINAL_INPUTS")
    if not name:
        pytest.skip("explicit retained original VFS61 receipt is required")
    return json.loads(Path(name).read_text())


def context(tmp_path, data):
    output = tmp_path / "output"
    output.mkdir()
    def bundle(root, role):
        path = output / (root.name + "-fixture-bundle.json")
        path.write_text(json.dumps({str(p.relative_to(root)): entry.guard.digest(p.read_bytes())
                                   for p in root.rglob("*") if p.is_file()}, sort_keys=True))
        return {"path": str(path), "sha256": entry.guard.digest(path.read_bytes())}
    return SimpleNamespace(
        legacy=Path(data["golden"]["path"]).parents[3], output=output, bundle=bundle,
        copy_asset=lambda p, role: {"path": str(p), "sha256": entry.guard.digest(Path(p).read_bytes())})


def test_original_plan_harnesses_six_logs_and_grader_only_binding(tmp_path, monkeypatch):
    data = original_inputs()
    ctx = context(tmp_path, data)
    original = copy.deepcopy(data)
    spec = rust_api.specification(ctx, data["task"], data["resolved_plan"], data["oracle"])
    assert spec["profile"] == entry.PROFILE
    assert spec["files"] == [{"path": p, "sha256": h} for p, h in entry.HARNESSES.items()]
    assert data == original
    def toolkit(ctx):
        root = ctx.output / "rust-build-tools"
        root.mkdir()
        shutil.copyfile(Path(entry.__file__).with_name("rust_build_entry.py"), root / "build.py")
        return ctx.bundle(root, "toolchain")
    monkeypatch.setattr(rust_overlay, "build_tools", toolkit)
    execute, assets = rust_api.binding(ctx, spec, ["--offline"])
    assert execute == ["python3", rust_vfs.TOOLS + "/rust_vfs_entry.py", "--spec", rust_vfs.SPEC]
    assert all(a["visibility"] == "grader" for a in assets)
    assert not rust_vfs.SPEC.startswith(rust_vfs.TOOLS + "/")
    assert (ctx.output / "rust-vfs-tools/rust_test_compat_entry.py").read_bytes() == (
        Path(entry.__file__).with_name("rust_test_compat_entry.py")).read_bytes()


@pytest.mark.parametrize("mutation", ["task", "plan", "golden", "oracle", "toolchain", "harness"])
def test_altered_original_profile_is_not_generic_fallback(tmp_path, mutation):
    data = original_inputs()
    if mutation == "task":
        data["task"]["id"] = "foreign"
    elif mutation == "plan":
        data["resolved_plan"]["prepare_command"] = ""
    elif mutation == "golden":
        data["oracle"]["golden_patch_sha256"] = "0" * 64
    elif mutation == "oracle":
        data["oracle"]["runs"]["start"][0]["ok"] = False
    elif mutation == "toolchain":
        data["resolved_plan"]["toolchain"] = "1.85.1"
    else:
        data["resolved_plan"]["session_files"][0]["content"] += "// changed\n"
    with pytest.raises(ContractError):
        rust_api.specification(context(tmp_path, data), data["task"], data["resolved_plan"], data["oracle"])
