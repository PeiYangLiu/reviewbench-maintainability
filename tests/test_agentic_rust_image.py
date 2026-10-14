"""Candidate lock selection is dependency-only; original test failures stay failures."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tomllib

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_image_entry as entry, rust_image_locks as host
from reviewbench_maintainability.agentic.contracts import ContractError


def ref(path):
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def output(command, cwd, stdout="", stderr="", code=0, limit=None):
    row = {"command": command, "cwd": str(cwd), "returncode": code, "limit": limit, "seconds": 0}
    for key, value in (("stdout", stdout), ("stderr", stderr)):
        raw = value.encode()
        row.update({key: value, key + "_sha256": entry.digest(raw),
                    key + "_captured_bytes": len(raw), key + "_total_bytes": len(raw),
                    key + "_utf8_valid": True})
    return row


def fixture(tmp_path, *, version="0.1.0"):
    root = tmp_path / "candidate"
    root.mkdir()
    files = {
        "Cargo.toml": '[package]\nname="image"\nversion="0.25.9"\nedition="2021"\n'
                      '[dependencies]\ndep="' + version + '"\n',
        "src/lib.rs": "pub fn value() -> u8 { 1 }\n",
        "tests/oracle_pnm.rs": "#[test]\nfn original_behavior() { assert_eq!(image::value(), 1); }\n",
        "fuzz/Cargo.toml": '[package]\nname="fuzzer"\nversion="0.0.1"\n[workspace]\n',
        "fuzz/src/main.rs": "fn main() {}\n",
        "fuzz-afl/Cargo.toml": '[package]\nname="afl-fuzzer"\nversion="0.0.1"\n[workspace]\n',
        "fuzz-afl/src/main.rs": "fn main() {}\n",
    }
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    locks = []
    for version in ("0.1.0", "0.2.0"):
        path = tmp_path / ("original-" + version + ".lock")
        path.write_text(
            'version = 3\n[[package]]\nname="image"\nversion="0.25.9"\ndependencies=["dep"]\n'
            '[[package]]\nname="dep"\nversion="' + version + '"\nsource="' + entry.CRATES_IO
            + '"\nchecksum="' + "a" * 64 + '"\n'
        )
        locks.append(ref(path))
    for name in ("cargo", "rustc", "sources.toml"):
        (tmp_path / name).write_text("fixed " + name)
    public = {
        "schema_version": entry.SCHEMA, "task_id": "unit-original-image",
        "selection": entry.SELECTION, "workdir": ".", "lock_path": "Cargo.lock", "locks": locks,
        "cargo": ref(tmp_path / "cargo"), "rustc": ref(tmp_path / "rustc"),
        "source_config": ref(tmp_path / "sources.toml"),
        "cpus": 2, "timeout_seconds": 60, "metadata_timeout_seconds": 10, "max_log_bytes": 1024 * 1024,
    }
    private = {
        "schema_version": entry.PRIVATE_SCHEMA, "task_id": public["task_id"],
        "harness": {"path": "tests/oracle_pnm.rs", "sha256": ref(root / "tests/oracle_pnm.rs")["sha256"]},
        "command": ["test", "--locked", "--test", "oracle_pnm", "original_behavior",
                    "--", "--nocapture", "--test-threads=1"],
        "expected_tests": ["original_behavior"],
    }
    return root, public, private


def fake_runner(calls, *, test_pass=True, both_compatible=False, metadata_error=None):
    def run(command, cwd, env, folder, name, deadline, limit):
        calls.append((list(command), (cwd / "Cargo.lock").read_bytes()))
        lock = tomllib.loads((cwd / "Cargo.lock").read_text())
        if "metadata" in command:
            if metadata_error is not None:
                return output(command, cwd, stderr=metadata_error, code=101)
            wanted = tomllib.loads((cwd / "Cargo.toml").read_text())["dependencies"]["dep"]
            dep = next(p for p in lock["package"] if p["name"] == "dep")
            if dep["version"] != wanted and not both_compatible:
                return output(command, cwd, code=101,
                              stderr="error: the lock file needs to be updated but --locked was passed\n")
            packages = []
            for package in lock["package"]:
                row = {**package, "id": package["name"] + "@" + package["version"], "targets": []}
                if package.get("source") is None:
                    row["manifest_path"] = str(cwd / "Cargo.toml")
                    row["targets"] = [{"name": "oracle_pnm", "kind": ["test"],
                                       "src_path": str(cwd / "tests/oracle_pnm.rs")}]
                packages.append(row)
            graph = {"workspace_root": str(cwd), "workspace_members": [packages[0]["id"]],
                     "packages": packages,
                     "resolve": {"nodes": [{"id": p["id"], "dependencies": []} for p in packages]}}
            return output(command, cwd, json.dumps(graph))
        if "test" in command:
            status = "ok" if test_pass else "FAILED"
            return output(command, cwd, "test original_behavior ... " + status
                          + "\ntest result: " + status + ".\n", code=0 if test_pass else 101)
        assert "check" in command
        return output(command, cwd, stderr="Finished `dev` profile\n")
    return run


@pytest.mark.parametrize("version", ["0.1.0", "0.2.0"])
@pytest.mark.parametrize("grading", [False, True])
def test_current_dependency_graph_selects_original_lock_without_source_labels(tmp_path, version, grading):
    root, public, private = fixture(tmp_path, version=version)
    before, calls = entry.snapshot(root), []
    result = entry.run_candidate(public, root, tmp_path / "mirror", private if grading else None,
                                 run=fake_runner(calls))
    assert result["status"] == "completed" and result["exit_status"] == 0
    assert len(result["probes"]) == 2 and len(calls) == 3
    assert all("metadata" in row[0] for row in calls[:2])
    assert calls[-1][0][-len(private["command"]):] == private["command"] if grading else "check" in calls[-1][0]
    assert result["graph"]["all_manifest_files"] == 3
    assert result["source_before"] == result["source_after"] == before
    assert result["mirror_before"] == result["mirror_after"]
    assert result["source_unchanged"] and result["mirror_unchanged"]
    assert result["new_lock_generation_commands"] == 0 and not result["selection_uses_test_results"]
    selected = next(r for r in public["locks"] if version in r["path"])
    assert result["selected_lock_sha256"] == selected["sha256"]
    assert not (root / "Cargo.lock").exists()
    if grading:
        assert result["test_results"] == {"original_behavior": True}


def test_failed_original_assertion_never_selects_or_tests_the_other_lock(tmp_path):
    root, public, private = fixture(tmp_path)
    calls = []
    result = entry.run_candidate(public, root, tmp_path / "mirror", private,
                                 run=fake_runner(calls, test_pass=False))
    assert result["exit_status"] == 101 and result["test_results"] == {"original_behavior": False}
    assert len(calls) == 3 and sum("test" in c for c, _ in calls) == 1
    assert result["command_executions"] == 1 and not result["selection_uses_test_results"]


def test_candidate_authored_lock_and_production_edits_are_preserved(tmp_path):
    root, public, private = fixture(tmp_path)
    text = Path(public["locks"][0]["path"]).read_text().replace('version="0.25.9"', 'version="0.26.0"')
    (root / "Cargo.lock").write_text(text)
    (root / "Cargo.toml").write_text((root / "Cargo.toml").read_text().replace("0.25.9", "0.26.0"))
    (root / "src/lib.rs").write_text("mod helper;\npub fn value() -> u8 { helper::read() }\n")
    (root / "src/helper.rs").write_text("pub fn read() -> u8 { 1 }\n")
    before, calls = entry.snapshot(root), []
    result = entry.run_candidate(public, root, tmp_path / "mirror", private, run=fake_runner(calls))
    assert result["candidate_lock_supplied"] and result["candidate_lock_preserved"]
    assert result["selected_lock_sha256"] == entry.digest(text.encode())
    assert len(result["probes"]) == 1 and len(calls) == 2
    assert entry.snapshot(root) == result["mirror_after"] == before


def test_incompatible_candidate_authored_lock_is_not_replaced(tmp_path):
    root, public, private = fixture(tmp_path, version="0.2.0")
    supplied = Path(public["locks"][0]["path"]).read_bytes()
    (root / "Cargo.lock").write_bytes(supplied)
    before, calls = entry.snapshot(root), []
    with pytest.raises(entry.VariantError, match="no unique compatible") as caught:
        entry.run_candidate(public, root, tmp_path / "mirror", private, run=fake_runner(calls))
    assert len(calls) == 1 and "metadata" in calls[0][0]
    assert entry.snapshot(root) == before
    assert caught.value.evidence["command_executions"] == 0


def test_two_compatible_locks_require_candidate_to_disambiguate_without_test_probing(tmp_path):
    root, public, private = fixture(tmp_path)
    calls = []
    with pytest.raises(entry.VariantError, match="no unique compatible retained lock: 2"):
        entry.run_candidate(public, root, tmp_path / "mirror", private,
                            run=fake_runner(calls, both_compatible=True))
    assert len(calls) == 2 and all("metadata" in command for command, _ in calls)
    assert not (root / "Cargo.lock").exists()


@pytest.mark.parametrize("error", ["error: could not execute process rustc: No such file",
                                  "error: failed to parse manifest", "error: Permission denied"])
def test_infrastructure_or_parse_errors_do_not_authorize_another_lock(tmp_path, error):
    root, public, private = fixture(tmp_path)
    calls = []
    with pytest.raises(entry.VariantError, match="outside dependency compatibility"):
        entry.run_candidate(public, root, tmp_path / "mirror", private,
                            run=fake_runner(calls, metadata_error=error))
    assert len(calls) == 1


@pytest.mark.parametrize("field", ["cargo", "rustc", "source_config", "lock", "harness"])
def test_changed_fixed_inputs_reject_before_any_cargo_call(tmp_path, field):
    root, public, private = fixture(tmp_path)
    path = (root / private["harness"]["path"] if field == "harness"
            else Path(public["locks"][0]["path"] if field == "lock" else public[field]["path"]))
    path.write_bytes(path.read_bytes() + b"\nchanged")
    calls = []
    with pytest.raises(entry.VariantError, match="bytes changed"):
        entry.run_candidate(public, root, tmp_path / "mirror", private, run=fake_runner(calls))
    assert calls == []


@pytest.mark.parametrize("mutation", ["source", "manifest", "lock", "metadata_hash", "limit", "graph"])
def test_metadata_must_preserve_inputs_and_supply_complete_full_graph(tmp_path, mutation):
    root, public, private = fixture(tmp_path)
    ordinary, calls = fake_runner([]), []
    def bad(command, cwd, env, folder, name, deadline, limit):
        row = ordinary(command, cwd, env, folder, name, deadline, limit)
        calls.append(command)
        if mutation == "source":
            (root / "src/lib.rs").write_text("changed source")
        elif mutation == "manifest":
            (cwd / "fuzz/Cargo.toml").write_text("changed manifest")
        elif mutation == "lock":
            (cwd / "Cargo.lock").write_text("changed lock")
        elif mutation == "metadata_hash":
            row["stdout_sha256"] = "0" * 64
        elif mutation == "limit":
            row["limit"] = "deadline"
        elif row["returncode"] == 0:
            graph = json.loads(row["stdout"])
            graph["resolve"]["nodes"] = []
            row = output(command, cwd, json.dumps(graph))
        return row
    with pytest.raises(entry.VariantError):
        entry.run_candidate(public, root, tmp_path / "mirror", private, run=bad)
    assert all("metadata" in command for command in calls)


@pytest.mark.parametrize("field,value", [
    ("cpus", 0), ("cpus", True), ("timeout_seconds", 1801),
    ("metadata_timeout_seconds", 121), ("max_log_bytes", 16 * 1024**2 + 1),
    ("workdir", "smaller-workspace"), ("lock_path", "src/Cargo.lock"),
    ("selection", "choose-whichever-test-passes"),
])
def test_selector_rejects_changed_root_selection_or_resource_rules(tmp_path, field, value):
    _, public, private = fixture(tmp_path)
    public[field] = value
    with pytest.raises(entry.VariantError):
        entry.validate(public, private)


def test_task_optin_is_exact_and_does_not_affect_other_rust_tasks():
    from types import SimpleNamespace
    ctx = SimpleNamespace(config={host.OPTION: {host.TASK: "/prepared.json"}})
    assert host.requested(ctx, {"id": host.TASK})
    assert not host.requested(ctx, {"id": "another-task"})
    assert not host.requested(SimpleNamespace(), {"id": host.TASK})
    with pytest.raises(ContractError):
        host.requested(SimpleNamespace(config={host.OPTION: "bad"}), {"id": host.TASK})


def test_original_task_identity_rejects_before_prepared_files_or_compilers(tmp_path):
    from types import SimpleNamespace
    for key, value in (("id", "other"), ("repo", "other/repo"), ("start_sha", "0" * 40)):
        task = {"id": host.TASK, "repo": host.REPO, "start_sha": host.START}
        oracle = copy.deepcopy(task)
        task[key] = value
        with pytest.raises(ContractError, match="wrong original task"):
            host.original_inputs(SimpleNamespace(), task, oracle, {}, {})


def test_task_specific_tool_bundle_does_not_contaminate_ordinary_callers(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import zipfile
    from agentic_runtime_migration import rust_overlay
    directory = tmp_path / "python-tool-wheels"
    directory.mkdir()
    wheel = directory / "test-tomli.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("tomli/__init__.py", "# fixture TOML package\n")
    monkeypatch.setattr(rust_overlay, "WHEELS", {
        "tomli": {"filename": wheel.name, "sha256": ref(wheel)["sha256"], "url": "unused"}
    })
    ctx = SimpleNamespace(output=tmp_path, bundle=lambda path, **kwargs: {
        "directory": str(path), "names": sorted(p.name for p in path.glob("*.py")),
    })
    ordinary = rust_overlay.build_tools(ctx)
    special = rust_overlay.build_tools(ctx, extra_entries={"image_locks.py": Path(entry.__file__)})
    again = rust_overlay.build_tools(ctx)
    assert ordinary == again and "image_locks.py" not in ordinary["names"]
    assert "image_locks.py" in special["names"] and special["directory"] != ordinary["directory"]
