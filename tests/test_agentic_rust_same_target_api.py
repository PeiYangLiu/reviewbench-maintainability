"""Original private variants share one unchanged target and never select by test outcomes."""
import copy
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_api_entry as api, rust_overlay_api_entry as overlay
from test_agentic_rust_image import ref
from test_agentic_rust_image import output
from test_agentic_rust_api_selection import record


def fixture(tmp_path):
    root = tmp_path / "candidate"
    (root / "package/src").mkdir(parents=True)
    (root / "package/tests").mkdir()
    (root / "package/src/lib.rs").write_text("current candidate source\n")
    cargo = tmp_path / "cargo"
    cargo.write_text("fixed compiler")
    forms = []
    command = [str(cargo), "test", "-p", "original", "--test", "oracle", "works", "--", "--nocapture"]
    for name in ("start", "golden"):
        path = tmp_path / (name + ".rs")
        path.write_text("original " + name + " harness\n")
        forms.append({"name": name, "target": "oracle", "harness": "package/tests/oracle.rs",
                      "asset": ref(path), "command": list(command)})
    spec = {"schema_version": api.SAME_TARGET_SCHEMA, "task_id": "fixture", "workdir": ".",
            "selection": "compile-only-current-candidate; execute-all-compilable-forms; conjunctive-results",
            "cargo": ref(cargo), "package": "original", "package_manifest": "package/Cargo.toml",
            "forms": forms, "expected_tests": ["works"], "cpus": 8,
            "timeout_seconds": 3600, "max_log_bytes": 1024**2}
    return root, spec


def runner(root, calls, compatible=("golden",), passed=True, fault=None):
    def run(command, *args):
        path = root / "package/tests/oracle.rs"
        name = path.read_text().split()[1]
        probe = "--no-run" in command
        calls.append((name, probe, list(command), path.read_bytes()))
        if fault:
            return record(101, stderr=fault)
        if probe:
            target = {"kind": ["test"], "name": "oracle", "src_path": str(path)}
            if name in compatible:
                return record(stdout=json.dumps({"reason": "compiler-artifact", "target": target,
                                                 "manifest_path": str(root / "package/Cargo.toml")}))
            return record(101, stdout=json.dumps({
                "reason": "compiler-message", "manifest_path": str(root / "package/Cargo.toml"),
                "target": target, "message": {"level": "error", "code": {"code": "E0308"},
                    "message": "original API type mismatch",
                    "spans": [{"is_primary": True, "file_name": str(path)}]}}))
        ok = passed or name == "start"
        return record(0 if ok else 101, stdout="test works ... " + ("ok" if ok else "FAILED") +
                      "\ntest result: " + ("ok. 1 passed; 0 failed" if ok else "FAILED. 0 passed; 1 failed") + "\n")
    return run


@pytest.mark.parametrize("selected", ["start", "golden"])
@pytest.mark.parametrize("existing", [False, True])
def test_original_same_target_commands_and_current_mirror_are_preserved(tmp_path, monkeypatch, selected, existing):
    root, spec = fixture(tmp_path)
    path = root / spec["forms"][0]["harness"]
    if existing:
        path.write_bytes(b"candidate-owned placeholder\n")
        path.chmod(0o755)
    monkeypatch.chdir(root)
    calls = []
    result = api.select_and_test(spec, root, tmp_path, run=runner(root, calls, (selected,)))
    assert result["selected"] == [selected] and result["results"] == {"works": True}
    assert [(n, p) for n, p, *_ in calls] == [("start", True), ("golden", True), (selected, False)]
    assert calls[-1][2] == spec["forms"][0]["command"]
    assert all(c[2][c[2].index("--test") + 1] == "oracle" for c in calls)
    if existing:
        assert path.read_bytes() == b"candidate-owned placeholder\n" and path.stat().st_mode & 0o777 == 0o755
    else:
        assert not path.exists()
    assert (root / "package/src/lib.rs").read_text() == "current candidate source\n"


def test_all_compilable_forms_are_tested_before_conjunctive_result(tmp_path, monkeypatch):
    root, spec = fixture(tmp_path)
    monkeypatch.chdir(root)
    calls = []
    result = api.select_and_test(spec, root, tmp_path, run=runner(root, calls, ("start", "golden"), passed=False))
    assert [(n, p) for n, p, *_ in calls] == [
        ("start", True), ("golden", True), ("start", False), ("golden", False)]
    assert result["results"] == {"works": False}
    assert not (root / spec["forms"][0]["harness"]).exists()


@pytest.mark.parametrize("fault", ["network unavailable", "No space left on device", "failed production compilation"])
def test_unknown_errors_stop_without_another_form_and_restore_the_mirror(tmp_path, monkeypatch, fault):
    root, spec = fixture(tmp_path)
    monkeypatch.chdir(root)
    calls = []
    with pytest.raises(api.SelectionError, match="outside its frozen harness"):
        api.select_and_test(spec, root, tmp_path, run=runner(root, calls, fault=fault))
    assert len(calls) == 1 and not (root / spec["forms"][0]["harness"]).exists()


def test_successful_compile_of_another_test_is_not_accepted(tmp_path, monkeypatch):
    root, spec = fixture(tmp_path)
    monkeypatch.chdir(root)
    def run(command, *args):
        return record(stdout=json.dumps({"reason": "compiler-artifact", "manifest_path": str(root / "other/Cargo.toml"),
                                        "target": {"kind": ["test"], "name": "oracle",
                                                   "src_path": str(root / "other/tests/oracle.rs")}}))
    with pytest.raises(api.SelectionError, match="original package/test"):
        api.select_and_test(spec, root, tmp_path, run=run)
    assert not (root / spec["forms"][0]["harness"]).exists()


@pytest.mark.parametrize("change", ["target", "filter", "locked", "cpus9", "asset", "package"])
def test_same_target_schema_rejects_changed_original_contract(tmp_path, change):
    _, spec = fixture(tmp_path)
    if change == "target":
        spec["forms"][1]["target"] = "invented"
    elif change == "filter":
        spec["forms"][1]["command"].remove("works")
    elif change == "locked":
        for form in spec["forms"]:
            form["command"].insert(2, "--locked")
    elif change == "cpus9":
        spec["cpus"] = 9
    elif change == "asset":
        spec["forms"][1]["asset"] = spec["forms"][0]["asset"]
    else:
        spec["package"] = "another"
    with pytest.raises(api.SelectionError):
        api.validate(spec)


def test_candidate_environment_conflict_keeps_current_inputs_and_lock(tmp_path):
    source = tmp_path / "candidate"
    source.mkdir()
    path = source / "src/workspace-hack/Cargo.toml"
    path.parent.mkdir(parents=True)
    path.write_text('[package]\nname="workspace-hack"\nversion="0.1.0"\n[dependencies]\nold="candidate"\n')
    doc = {"schema_version": "reviewbench-cargo-build-overlay/v1", "task_id": "x",
           "manifests": [{"path": "src/workspace-hack/Cargo.toml",
                          "operations": [{"kind": "remove", "key": ["dependencies", "old"], "old": "original"}]}],
           "locks": [], "private_members": []}
    asset = tmp_path / "overlay.json"
    asset.write_text(json.dumps(doc))
    public = {"schema_version": overlay.SAME_TARGET_SCHEMA, "task_id": "x",
              "overlay": ref(asset), "overlay_paths": ["src/workspace-hack/Cargo.toml"]}
    spec, changed = overlay.current_overlay(source, public)
    assert spec["candidate_owned_manifest_inputs"] and spec["manifests"] == [] and changed == {}
    assert 'old="candidate"' in path.read_text()


@pytest.mark.parametrize("private_mode", [False, True])
def test_shared_overlay_loop_uses_one_original_graph_and_exact_execution(tmp_path, private_mode):
    root = tmp_path / "candidate"
    for directory in ("src/repr/src", "src/extra/src"):
        (root / directory).mkdir(parents=True)
    (root / "Cargo.toml").write_text('[workspace]\nmembers=["src/repr","src/extra"]\n')
    for package, name in (("repr", "mz-repr"), ("extra", "untouched")):
        (root / ("src/" + package + "/Cargo.toml")).write_text(
            '[package]\nname="' + name + '"\nversion="0.1.0"\n')
        (root / ("src/" + package + "/src/lib.rs")).write_text("pub fn original(){}\n")
    lock = tmp_path / "original.lock"
    lock.write_text('version=3\n[[package]]\nname="mz-repr"\nversion="0.1.0"\n'
                    '[[package]]\nname="untouched"\nversion="0.1.0"\n')
    (root / "Cargo.lock").write_bytes(lock.read_bytes())
    for name in ("cargo", "rustc", "sources.toml"):
        (tmp_path / name).write_text("fixed " + name)
    doc = tmp_path / "overlay.json"
    doc.write_text(json.dumps({"schema_version": "reviewbench-cargo-build-overlay/v1", "task_id": "fixture",
                              "manifests": [], "locks": [], "private_members": []}))
    public = {"schema_version": overlay.SAME_TARGET_SCHEMA, "task_id": "fixture",
              "selection": overlay.core.SELECTION, "package": "mz-repr",
              "package_manifest": "src/repr/Cargo.toml", "feature_args": [], "locks": [ref(lock)],
              "reference_seed_sha256": ref(lock)["sha256"], "overlay": ref(doc), "overlay_paths": [],
              "cargo": ref(tmp_path / "cargo"), "rustc": ref(tmp_path / "rustc"),
              "source_config": ref(tmp_path / "sources.toml"), "private_spec_sha256": "a" * 64,
              "private_tool_sha256": "b" * 64, "cpus": 8, "timeout_seconds": 3600,
              "metadata_timeout_seconds": 180, "max_log_bytes": 1024**2,
              "public_command": ["build", "--locked", "-p", "mz-repr", "--lib", "--message-format=json"]}
    calls = []
    def run(command, cwd, env, folder, label, deadline, limit):
        calls.append(command)
        assert (cwd / "src/extra/src/lib.rs").read_text() == "pub fn original(){}\n"
        if "metadata" in command:
            packages = [{"id": name, "name": name, "version": "0.1.0", "source": None, "targets": [],
                         "manifest_path": str(cwd / ("src/" + directory + "/Cargo.toml"))}
                        for directory, name in (("repr", "mz-repr"), ("extra", "untouched"))]
            return output(command, cwd, stdout=json.dumps({
                "workspace_root": str(cwd), "packages": packages,
                "workspace_members": [p["id"] for p in packages],
                "resolve": {"nodes": [{"id": p["id"]} for p in packages]}}))
        if private_mode:
            assert command[1:] == ["/private-api.py", "--spec", "/private-api.json"]
            return output(command, cwd, stdout="test original ... ok\n")
        assert command == [public["cargo"]["path"], "--offline", *public["public_command"]]
        return output(command, cwd)
    before = overlay.core.snapshot(root)
    result = overlay.run_candidate(public, root, tmp_path / "cache",
                                   {"expected_tests": ["original"]} if private_mode else None,
                                   private_tool="/private-api.py", private_path="/private-api.json", run=run)
    assert result["exit_status"] == 0 and result["graph"]["workspace_members"] == 2
    assert len(calls) == 2 and result["source_before"] == result["source_after"] == before
