"""Original build-only overlays do not replace current source or select by assertions."""
import copy
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import tomllib

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_session_overlay_entry as entry, rust_session_overlay as host
from agentic_runtime_migration.rust_build_entry import manifest_operations
from reviewbench_maintainability.agentic.contracts import ContractError
from test_agentic_rust_image import output


def ref(path):
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def fixture(tmp_path, version="0.1.0", grading=True):
    root = tmp_path / "candidate"
    files = {
        "Cargo.toml": '[workspace]\nmembers=["production","ignored"]\nresolver="2"\n',
        "production/Cargo.toml": '[package]\nname="production"\nversion="1.0.0"\nedition="2021"\n'
            '[dependencies]\ndep="=' + version + '"\n[dev-dependencies]\nignored={path="../ignored"}\n'
            '[features]\nclient=[]\nprofiling=[]\ntest=[]\n',
        "production/src/lib.rs": "pub fn value() -> u8 { dep::value() }\n",
        "ignored/Cargo.toml": '[package]\nname="ignored"\nversion="1.0.0"\nedition="2021"\n'
            '[dependencies]\nunavailable="=0.0.1"\n',
        "ignored/src/lib.rs": "pub fn retained() {}\n",
    }
    session = {
        "tests/session/Cargo.toml": '[package]\nname="private-session"\nversion="0.0.0"\nedition="2021"\n'
            '[dependencies]\nproduction={path="../../production",features=["client","profiling","test"]}\n',
        "tests/session/src/lib.rs": '#[cfg(test)]\nmod tests {\n#[test]\nfn original_behavior() '
            '{ assert_eq!(production::value(), 1); }\n}\n',
    }
    for name, data in {**files, **(session if grading else {})}.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
    before = tomllib.loads(files["Cargo.toml"])
    after = copy.deepcopy(before)
    after["workspace"]["members"] = ["production", "tests/session"]
    prod = tomllib.loads(files["production/Cargo.toml"])
    prod_after = copy.deepcopy(prod)
    prod_after.pop("dev-dependencies")
    overlay = {
        "schema_version": "reviewbench-cargo-build-overlay/v1", "task_id": "session-fixture",
        "private_members": ["tests/session"], "locks": [], "cpus": 2,
        "manifests": [
            {"path": "Cargo.toml", "operations": manifest_operations(before, after)},
            {"path": "production/Cargo.toml", "operations": manifest_operations(prod, prod_after)},
        ],
    }
    (tmp_path / "overlay.json").write_text(json.dumps(overlay))
    locks = []
    for version in ("0.1.0", "0.2.0"):
        path = tmp_path / ("original-" + version + ".lock")
        path.write_text(
            'version=3\n[[package]]\nname="production"\nversion="1.0.0"\ndependencies=["dep"]\n'
            '[[package]]\nname="private-session"\nversion="0.0.0"\ndependencies=["production"]\n'
            '[[package]]\nname="dep"\nversion="' + version + '"\nsource="' + entry.core.CRATES_IO + '"\n'
            'checksum="' + ("a" if version == "0.1.0" else "b") * 64 + '"\n')
        locks.append(ref(path))
    for name in ("cargo", "rustc", "sources.toml"):
        (tmp_path / name).write_text("fixed " + name)
    public = {
        "schema_version": entry.SCHEMA, "task_id": "session-fixture", "selection": entry.core.SELECTION,
        "cargo": ref(tmp_path / "cargo"), "rustc": ref(tmp_path / "rustc"),
        "source_config": ref(tmp_path / "sources.toml"), "overlay": ref(tmp_path / "overlay.json"),
        "locks": locks, "session": {"manifest": "tests/session/Cargo.toml", "package": "private-session",
                                    "version": "0.0.0"},
        "public_command": ["check", "--locked", "-p", "production", "--features", "client,profiling,test"],
        "cpus": 2, "timeout_seconds": 120, "metadata_timeout_seconds": 30, "max_log_bytes": 1024**2,
    }
    private = {
        "schema_version": entry.PRIVATE_SCHEMA, "task_id": public["task_id"],
        "command": ["test", "--locked", "-p", "private-session", "--", "--nocapture"],
        "session_files": [{"path": n, "sha256": entry.core.digest(data.encode())} for n, data in session.items()],
        "expected_tests": ["tests::original_behavior"],
    }
    return root, public, private, session


def runner(calls, *, passed=True, metadata_extra=False, target=False, fail_metadata=False):
    def run(command, cwd, env, folder, name, deadline, limit):
        calls.append(list(command))
        if "metadata" in command:
            if fail_metadata:
                return output(command, cwd, code=101, stderr="error: cannot read dependency cache\n")
            lock = tomllib.loads((cwd / "Cargo.lock").read_text())
            wanted = tomllib.loads((cwd / "production/Cargo.toml").read_text())["dependencies"]["dep"].lstrip("=")
            if next(p for p in lock["package"] if p["name"] == "dep")["version"] != wanted:
                return output(command, cwd, code=101,
                    stderr="error: the lock file " + str(cwd / "Cargo.lock")
                    + " needs to be updated but --frozen was passed to prevent this\n"
                    "If you want to try to generate the lock file without accessing the network, "
                    "remove the --frozen flag and use --offline instead.\n")
            private_present = (cwd / "tests/session/Cargo.toml").exists()
            packages = []
            for row in lock["package"]:
                if row["name"] == "private-session" and not private_present:
                    continue
                item = {**row, "id": row["name"] + "@" + row["version"], "targets": []}
                if row.get("source") is None:
                    prefix = "tests/session" if row["name"] == "private-session" else "production"
                    item["manifest_path"] = str(cwd / prefix / "Cargo.toml")
                    item["targets"] = [{"kind": ["lib"], "test": not target,
                                       "src_path": str(cwd / prefix / "src/lib.rs")}]
                packages.append(item)
            if metadata_extra:
                packages.pop()
            graph = {"workspace_root": str(cwd), "packages": packages,
                     "workspace_members": [p["id"] for p in packages if p.get("source") is None],
                     "resolve": {"nodes": [{"id": p["id"]} for p in packages]}}
            return output(command, cwd, stdout=json.dumps(graph))
        if "test" in command:
            result = "ok" if passed else "FAILED"
            return output(command, cwd, stdout="test tests::original_behavior ... " + result
                          + "\ntest result: " + result + ".\n", code=0 if passed else 101)
        return output(command, cwd, stderr="Finished dev")
    return run


@pytest.mark.parametrize("version", ["0.1.0", "0.2.0"])
@pytest.mark.parametrize("grading", [False, True])
def test_source_complete_and_original_locked_commands(tmp_path, version, grading):
    source, public, private, _ = fixture(tmp_path, version, grading)
    before, calls = entry.core.snapshot(source), []
    record = entry.run_candidate(public, source, tmp_path / "build", private if grading else None, run=runner(calls))
    assert record["exit_status"] == 0 and record["command_executions"] == 1 and len(record["probes"]) == 2
    assert record["source_before"] == record["source_after"] == before
    assert "ignored/src/lib.rs" in record["mirror_after"]
    assert record["graph"]["packages"] == (3 if grading else 2)
    assert calls[-1] == [public["cargo"]["path"], "--offline",
                         *(private["command"] if grading else public["public_command"])]
    assert not (source / "Cargo.lock").exists()


def test_failed_assertion_never_tries_another_environment(tmp_path):
    source, public, private, _ = fixture(tmp_path)
    calls = []
    record = entry.run_candidate(public, source, tmp_path / "build", private, run=runner(calls, passed=False))
    assert record["exit_status"] == 101 and record["test_results"] == {"tests::original_behavior": False}
    assert record["command_executions"] == 1 and len(calls) == 3


@pytest.mark.parametrize("compatible", [False, True])
def test_candidate_owned_lock_is_preserved(tmp_path, compatible):
    source, public, private, _ = fixture(tmp_path, "0.2.0")
    data = Path(public["locks"][1 if compatible else 0]["path"]).read_bytes() + b"\n# authored\n"
    (source / "Cargo.lock").write_bytes(data)
    calls = []
    if compatible:
        record = entry.run_candidate(public, source, tmp_path / "build", private, run=runner(calls))
        assert record["candidate_lock_preserved"] and len(record["probes"]) == 1
    else:
        with pytest.raises(entry.core.VariantError, match="no unique"):
            entry.run_candidate(public, source, tmp_path / "build", private, run=runner(calls))
        assert len(calls) == 1
    assert (source / "Cargo.lock").read_bytes() == data


@pytest.mark.parametrize("change", ["source", "task", "locks", "members", "production-dependency"])
def test_overlay_is_not_a_source_or_dependency_substitution(tmp_path, change):
    _, public, _, _ = fixture(tmp_path)
    path = Path(public["overlay"]["path"])
    data = json.loads(path.read_text())
    if change == "source":
        data["manifests"][0]["path"] = "production/src/lib.rs"
    elif change == "task":
        data["task_id"] = "another"
    elif change == "locks":
        data["locks"] = [{"path": "Cargo.lock"}]
    elif change == "members":
        data["private_members"] = ["other"]
    else:
        data["manifests"][1]["operations"] = [{"kind": "replace", "key": ["dependencies", "dep"],
                                             "old": "=0.1.0", "value": "=0.2.0"}]
    path.write_text(json.dumps(data))
    public["overlay"] = ref(path)
    with pytest.raises(entry.core.VariantError):
        entry.load_overlay(public)


@pytest.mark.parametrize("change", ["harness", "manifest", "missing-target", "lost-package", "infra"])
def test_private_integrity_and_complete_graphs_are_checked(tmp_path, change):
    source, public, private, _ = fixture(tmp_path)
    options = {}
    if change == "harness":
        (source / "tests/session/src/lib.rs").write_text("changed")
    elif change == "manifest":
        (source / "tests/session/Cargo.toml").write_text("changed")
    elif change == "missing-target":
        options["target"] = True
    elif change == "lost-package":
        options["metadata_extra"] = True
    else:
        options["fail_metadata"] = True
    calls = []
    with pytest.raises(entry.core.VariantError):
        entry.run_candidate(public, source, tmp_path / "build", private, run=runner(calls, **options))
    assert all("metadata" in call for call in calls)


@pytest.mark.parametrize("change", ["schema", "private-schema", "private-task", "unlocked", "different-target",
                                  "public-features", "public-package", "same-locks", "resource-limit"])
def test_schema_and_original_commands_are_explicit(tmp_path, change):
    _, public, private, _ = fixture(tmp_path)
    if change == "schema":
        public["schema_version"] = entry.core.LOCKED_WORKSPACE_SCHEMA
    elif change == "private-schema":
        private["schema_version"] = entry.core.PRIVATE_SCHEMA
    elif change == "private-task":
        private["task_id"] = "other"
    elif change == "unlocked":
        private["command"].remove("--locked")
    elif change == "different-target":
        private["command"][3] = "production"
    elif change == "public-features":
        public["public_command"] = ["check", "--locked", "-p", "production"]
    elif change == "public-package":
        public["public_command"][3] = "../bad"
    elif change == "same-locks":
        public["locks"][1] = public["locks"][0]
    else:
        public["cpus"] = 100
    with pytest.raises(entry.core.VariantError):
        entry.validate(public, private)


def test_host_requires_original_identity_and_independent_input_pin(tmp_path):
    assert not host.requested(SimpleNamespace(config={}), {"id": host.TASK})
    ctx = SimpleNamespace(config={"migration": {host.OPTION: {host.TASK: {"path": "/input", "sha256": "a" * 64}}}})
    assert host.requested(ctx, {"id": host.TASK})
    assert not host.requested(ctx, {"id": "other"})
    with pytest.raises(ContractError, match="identity"):
        host.original_inputs(ctx, {"id": host.TASK, "repo": "wrong", "start_sha": host.START}, {}, {}, {})
    path = tmp_path / "input.json"
    path.write_text("{}")
    with pytest.raises(ContractError):
        host.referenced({"path": str(path), "sha256": "0" * 64})
