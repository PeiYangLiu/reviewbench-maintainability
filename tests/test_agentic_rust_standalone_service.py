"""Original standalone service uses metadata selection, not test outcome fallback."""
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import tomllib

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_standalone_service_entry as entry, rust_relay_service as host
from reviewbench_maintainability.agentic.contracts import ContractError
from test_agentic_rust_image import output, ref


def fixture(tmp_path, monkeypatch, version="0.1.0", private=True):
    root = tmp_path / "candidate"
    files = {
        "Cargo.toml": '[workspace]\nmembers=["production"]\nresolver="2"\n',
        "production/Cargo.toml": '[package]\nname="production"\nversion="' + version + '"\nedition="2021"\n'
            '[features]\nredis=[]\n',
        "production/src/lib.rs": "pub fn value() -> u8 { 1 }\n",
    }
    session = {
        "tests/oracle/Cargo.toml": '[package]\nname="oracle"\nversion="0.0.0"\nedition="2021"\n'
            '[workspace]\n[dependencies]\nproduction={path="../../production",features=["redis"]}\n',
        "tests/oracle/tests/behavior.rs": "#[test]\nfn original_behavior() { assert_eq!(production::value(), 1); }\n",
    }
    for name, data in {**files, **(session if private else {})}.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
    locks = {"public": [], "private": []}
    for role in locks:
        for number in ("0.1.0", "0.2.0"):
            path = tmp_path / (role + "-" + number + ".lock")
            text = 'version=3\n[[package]]\nname="production"\nversion="' + number + '"\n'
            if role == "private":
                text += '[[package]]\nname="oracle"\nversion="0.0.0"\ndependencies=["production"]\n'
            path.write_text(text)
            locks[role].append(ref(path))
    (root / "Cargo.lock").write_bytes(Path(locks["public"][0]["path"]).read_bytes())
    for name in ("cargo", "rustc", "sources.toml", "redis-server", "redis-cli"):
        (tmp_path / name).write_text("fixed " + name)
    public = {
        "schema_version": entry.SCHEMA, "task_id": "standalone-fixture", "selection": entry.core.SELECTION,
        "cargo": ref(tmp_path / "cargo"), "rustc": ref(tmp_path / "rustc"),
        "source_config": ref(tmp_path / "sources.toml"), "toolchain": "1.73.0", "locks": locks,
        "public_seed_sha256": locks["public"][0]["sha256"], "private_workdir": "tests/oracle",
        "public_command": ["check", "--locked", "-p", "production", "--features", "production/redis"],
        "cpus": 2, "timeout_seconds": 120, "metadata_timeout_seconds": 30, "max_log_bytes": 1024**2,
    }
    command = ["test", "--locked", "--test", "behavior", "original_behavior", "--", "--nocapture"]
    shell = ("redis-server --save '' --appendonly no --port 6379 --bind 127.0.0.1 "
             '--dir "$CARGO_TARGET_DIR" >"$CARGO_TARGET_DIR/redis.log" 2>&1 & redis_pid=$!; '
             'cargo +1.73.0 ' + " ".join(command) + '; rc=$?; kill "$redis_pid"; wait "$redis_pid"; exit "$rc"')
    private_spec = {
        "schema_version": entry.PRIVATE_SCHEMA, "task_id": public["task_id"],
        "session_files": [{"path": n, "sha256": entry.core.digest(data.encode())} for n, data in session.items()],
        "test_path": "tests/oracle/tests/behavior.rs", "test_command": command,
        "original_shell": shell, "original_shell_sha256": entry.core.digest(shell.encode()),
        "expected_tests": ["original_behavior"],
        "service_binaries": {name: ref(tmp_path / name) for name in ("redis-server", "redis-cli")},
    }
    original_which = entry.shutil.which
    monkeypatch.setattr(entry.shutil, "which", lambda n: str(tmp_path / n)
                        if n in ("redis-server", "redis-cli") else original_which(n))
    return root, public, private_spec, session


def runner(calls, *, passed=True, extra_package=False, outside=False, metadata_error=None):
    def run(command, cwd, env, folder, name, deadline, limit):
        calls.append(list(command))
        if "metadata" in command:
            if metadata_error:
                return output(command, cwd, code=101, stderr=metadata_error)
            private = cwd.name == "oracle"
            root = cwd.parents[1] if private else cwd
            wanted = tomllib.loads((root / "production/Cargo.toml").read_text())["package"]["version"]
            lock = tomllib.loads((cwd / "Cargo.lock").read_text())
            if lock["package"][0]["version"] != wanted:
                return output(command, cwd, code=101,
                              stderr="error: the lock file needs to be updated but --locked was passed\n")
            packages = []
            for p in lock["package"]:
                manifest = cwd / "Cargo.toml" if p["name"] == "oracle" else root / "production/Cargo.toml"
                row = {**p, "id": p["name"] + "@" + p["version"], "manifest_path": str(manifest),
                       "targets": []}
                if p["name"] == "oracle":
                    row["targets"] = [{"name": "behavior", "kind": ["test"],
                                       "src_path": str(root / "tests/oracle/tests/behavior.rs")}]
                packages.append(row)
            if extra_package:
                packages.pop()
            graph = {"workspace_root": "/outside" if outside else str(cwd), "packages": packages,
                     "workspace_members": [p["id"] for p in packages if (p["name"] == "oracle") == private],
                     "resolve": {"nodes": [{"id": p["id"]} for p in packages]}}
            return output(command, cwd, stdout=json.dumps(graph))
        if command[0] == "/bin/bash":
            result = "ok" if passed else "FAILED"
            return output(command, cwd, stdout="test original_behavior ... " + result
                          + "\ntest result: " + result + ".\n", code=0 if passed else 101)
        return output(command, cwd, stderr="Finished dev")
    return run


@pytest.mark.parametrize("version", ["0.1.0", "0.2.0"])
@pytest.mark.parametrize("private", [False, True])
def test_complete_candidate_role_roots_and_original_commands(tmp_path, monkeypatch, version, private):
    source, public, spec, _ = fixture(tmp_path, monkeypatch, version, private)
    before, calls = entry.core.snapshot(source), []
    result = entry.run_candidate(public, source, tmp_path / "build", spec if private else None, run=runner(calls))
    assert result["exit_status"] == 0 and result["source_before"] == result["source_after"] == before
    assert len(result["probes"]) == 2 and result["command_executions"] == 1
    assert result["graph"]["packages"] == (2 if private else 1)
    if private:
        assert calls[-1] == ["/bin/bash", "-c", spec["original_shell"].replace("cargo +1.73.0", public["cargo"]["path"])]
        assert result["test_results"] == {"original_behavior": True}
    else:
        assert calls[-1] == [public["cargo"]["path"], "--offline", *public["public_command"]]


def test_failed_original_test_does_not_try_other_service_or_lock(tmp_path, monkeypatch):
    source, public, private, _ = fixture(tmp_path, monkeypatch)
    calls = []
    result = entry.run_candidate(public, source, tmp_path / "build", private, run=runner(calls, passed=False))
    assert result["exit_status"] == 101 and result["test_results"] == {"original_behavior": False}
    assert len(calls) == 3 and result["command_executions"] == 1


@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("valid", [False, True])
def test_changed_candidate_lock_is_not_overwritten(tmp_path, monkeypatch, private, valid):
    source, public, spec, _ = fixture(tmp_path, monkeypatch, "0.2.0", private)
    role = "private" if private else "public"
    lock = source / ("tests/oracle/Cargo.lock" if private else "Cargo.lock")
    data = Path(public["locks"][role][1 if valid else 0]["path"]).read_bytes() + b"\n# owned\n"
    lock.write_bytes(data)
    calls = []
    if valid:
        result = entry.run_candidate(public, source, tmp_path / "build", spec if private else None, run=runner(calls))
        assert result["candidate_lock_preserved"] and len(result["probes"]) == 1
    else:
        with pytest.raises(entry.core.VariantError, match="no unique"):
            entry.run_candidate(public, source, tmp_path / "build", spec if private else None, run=runner(calls))
        assert len(calls) == 1
    assert lock.read_bytes() == data


@pytest.mark.parametrize("change", ["outside-workspace", "lost-package", "infra", "harness", "redis-path", "redis-hash"])
def test_invalid_metadata_harness_and_service_are_not_environment_fallbacks(tmp_path, monkeypatch, change):
    source, public, private, _ = fixture(tmp_path, monkeypatch)
    options = {}
    if change == "outside-workspace":
        options["outside"] = True
    elif change == "lost-package":
        options["extra_package"] = True
    elif change == "infra":
        options["metadata_error"] = "error: dependency cache is missing\n"
    elif change == "harness":
        (source / private["test_path"]).write_text("changed")
    elif change == "redis-path":
        monkeypatch.setattr(entry.shutil, "which", lambda name: "/unbound/" + name)
    else:
        (tmp_path / "redis-server").write_text("changed")
    calls = []
    with pytest.raises(entry.core.VariantError):
        entry.run_candidate(public, source, tmp_path / "build", private, run=runner(calls, **options))
    assert all("metadata" in call for call in calls)


@pytest.mark.parametrize("change", ["schema", "version", "private-task", "unlocked", "wrong-test", "shell-hash",
                                  "missing-shutdown", "wrong-service", "budget"])
def test_original_contract_is_explicit(tmp_path, monkeypatch, change):
    _, public, private, _ = fixture(tmp_path, monkeypatch)
    if change == "schema":
        public["schema_version"] = entry.core.SCHEMA
    elif change == "version":
        public["toolchain"] = "latest"
    elif change == "private-task":
        private["task_id"] = "other"
    elif change == "unlocked":
        private["test_command"].remove("--locked")
    elif change == "wrong-test":
        private["test_command"][private["test_command"].index("--test") + 1] = "other"
    elif change == "shell-hash":
        private["original_shell_sha256"] = "0" * 64
    elif change == "missing-shutdown":
        private["original_shell"] = private["original_shell"].replace('wait "$redis_pid"', "true")
        private["original_shell_sha256"] = entry.core.digest(private["original_shell"].encode())
    elif change == "wrong-service":
        private["service_binaries"].pop("redis-cli")
    else:
        public["timeout_seconds"] = 3601
    with pytest.raises(entry.core.VariantError):
        entry.validate(public, private)


def test_host_identity_and_optin(tmp_path):
    assert not host.requested(SimpleNamespace(config={}), {"id": host.TASK})
    ctx = SimpleNamespace(config={"migration": {host.OPTION: {host.TASK: {}}}})
    assert host.requested(ctx, {"id": host.TASK}) and not host.requested(ctx, {"id": "other"})
    with pytest.raises(ContractError, match="identity"):
        host.original_inputs(ctx, {"id": host.TASK, "repo": "wrong", "start_sha": host.START}, {}, {}, {})
