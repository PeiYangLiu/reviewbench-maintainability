"""Original-command routing, full-source preservation and inline assertion controls."""
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_fuel as host, rust_fuel_entry as entry
from agentic_runtime_migration.rust_build_entry import tomli_w
from reviewbench_maintainability.agentic.contracts import canonical_hash


def put(root, name, data):
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return {"path": str(p), "sha256": entry.core.digest(data)}


def fixture(tmp_path, variant="start"):
    source = tmp_path / "candidate"
    manifest = '[package]\nname="fuel-tx"\nversion="0.21.0"\n'
    if variant == "golden":
        manifest += '[dependencies]\nbitflags="1"\n'
    put(source, "Cargo.toml", manifest.encode())
    put(source, "src/lib.rs", b"pub fn original() {}\n")
    put(source, "test-helpers/Cargo.toml", b'[dependencies]\nfuel-tx={path="../../fuel-tx"}\n')
    blocks = []
    for i in range(7):
        marker = '#[cfg(all(test, feature = "random"))]\nmod tests {' if i == 0 else '#[cfg(test)]\nmod tests {'
        block = (marker + f'\n#[test] fn original_{i}() {{ assert_eq!(1, 1); }}\n}}\n').encode()
        path = f"src/item{i}.rs"
        put(source, path, b"pub fn production() {}\n" + block)
        blocks.append({"source_path": path, "marker": marker, "bytes": len(block), "sha256": entry.core.digest(block)})
    files = []
    for name in entry.TARGETS.values():
        data = b"#[test] fn unchanged() {}\n"
        put(source, name, data)
        files.append({"path": name, "sha256": entry.core.digest(data)})
    private = {"schema_version": entry.PRIVATE_SCHEMA, "task_id": entry.TASK, "command": entry.COMMAND,
               "expected_tests": ["original_" + str(i) for i in range(77)], "test_targets": entry.TARGETS,
               "inline_test_blocks": blocks, "files": files}
    locks = []
    for name in ("start", "golden"):
        data = {"version": 3, "package": [{"name": "fuel-tx", "version": "0.21.0"},
                                         {"name": "fuel-tx-test-helpers", "version": "0.1.0"}]}
        if name == "golden":
            data["package"].append({"name": "bitflags", "version": "1.3.2", "source": entry.core.CRATES_IO})
        locks.append(put(tmp_path, "assets/" + name + ".lock", tomli_w.dumps(data).encode()))
    public = {"schema_version": entry.SCHEMA, "task_id": entry.TASK, "selection": entry.SELECTION,
              "mirror_basename": "fuel-tx", "public_command": entry.PUBLIC, "original_lock_recovered": False,
              "locks": locks, "cargo": put(tmp_path, "bin/cargo", b"pinned Cargo"),
              "rustc": put(tmp_path, "bin/rustc", b"pinned rustc"),
              "source_config": put(tmp_path, "assets/sources.toml", b"native fixed local registry"),
              "cpus": 16, "timeout_seconds": 3600, "metadata_timeout_seconds": 30,
              "max_log_bytes": 1024**2, "private_fingerprint": canonical_hash(private)}
    return source, public, private


def result(command, cwd, stdout="", code=0, stderr=""):
    row = {"command": command, "cwd": str(cwd), "returncode": code, "limit": None}
    for key, text in (("stdout", stdout), ("stderr", stderr)):
        row.update({key: text, key + "_utf8_valid": True, key + "_total_bytes": len(text.encode()),
                    key + "_captured_bytes": len(text.encode()), key + "_sha256": entry.core.digest(text.encode())})
    return row


def runner(calls, *, fail_test=False, fault=None):
    def run(command, cwd, env, folder, label, deadline, limit):
        calls.append((command, cwd, env))
        assert cwd.name == "fuel-tx"
        assert (cwd / "test-helpers" / "../../fuel-tx").resolve() == cwd.resolve()
        if "metadata" in command:
            lock = entry.tomllib.loads((cwd / "Cargo.lock").read_text())
            golden = "bitflags" in entry.tomllib.loads((cwd / "Cargo.toml").read_text()).get("dependencies", {})
            selected = any(p["name"] == "bitflags" for p in lock["package"])
            if fault == "infrastructure":
                return result(command, cwd, "", 101, "no space left on device\n")
            if golden != selected:
                return result(command, cwd, "", 101, (
                    f"error: cannot update the lock file {cwd}/Cargo.lock because --locked was passed to prevent this\n"
                    "help: to generate the lock file without accessing the network, remove the --locked flag and use --offline instead.\n"))
            packages = []
            for p in lock["package"]:
                path = cwd / ("Cargo.toml" if p["name"] == "fuel-tx" else "test-helpers/Cargo.toml")
                targets = []
                if p["name"] == "fuel-tx":
                    targets = [{"name": "fuel_tx", "kind": ["lib"], "src_path": str(cwd / "src/lib.rs"), "test": True},
                               *[{"name": name, "kind": ["test"], "src_path": str(cwd / relative)}
                                 for name, relative in entry.TARGETS.items()]]
                    if fault == "target":
                        targets[-1]["src_path"] = "/outside/test.rs"
                packages.append({**p, "id": p["name"], "source": p.get("source"),
                                 "manifest_path": str(path), "targets": targets})
            graph = {"packages": packages, "workspace_root": str(cwd), "workspace_members": ["fuel-tx"],
                     "resolve": {"nodes": [{"id": p["id"]} for p in packages]}}
            return result(command, cwd, json.dumps(graph))
        if "test" in command:
            text = "".join("test original_" + str(i) + " ... " + ("FAILED" if fail_test and i == 1 else "ok") + "\n"
                           for i in range(77))
            return result(command, cwd, text, int(fail_test))
        return result(command, cwd, '{"reason":"build-finished","success":true}\n')
    return run


@pytest.mark.parametrize("variant", ["start", "golden"])
def test_missing_lock_uses_full_current_metadata_before_original_tests(tmp_path, variant):
    source, public, private = fixture(tmp_path, variant)
    before, calls = entry.core.snapshot(source), []
    value = entry.run_candidate(public, source, tmp_path / "cache", private, run=runner(calls))
    assert value["status"] == "completed" and value["exit_status"] == 0
    assert value["selected_lock_sha256"] == public["locks"][int(variant == "golden")]["sha256"]
    assert len(calls) == 3 and calls[-1][0][1:] == entry.COMMAND
    assert "--locked" not in calls[-1][0] and "--no-run" not in calls[-1][0]
    assert all(value["test_results"].values()) and len(value["test_results"]) == 77
    assert entry.core.snapshot(source) == before and value["mirror_restored"]
    assert not (source / "Cargo.lock").exists()


def test_public_prewarmer_never_executes_repository_tests(tmp_path):
    source, public, _ = fixture(tmp_path)
    calls = []
    evidence = entry.run_candidate(public, source, tmp_path / "cache", run=runner(calls))
    assert evidence["exit_status"] == 0 and all("test" not in r[0] for r in calls)
    assert calls[-1][0][1:] == ["--offline", *entry.PUBLIC]


def test_candidate_owned_lock_and_production_changes_are_preserved(tmp_path):
    source, public, private = fixture(tmp_path)
    (source / "Cargo.lock").write_bytes(Path(public["locks"][0]["path"]).read_bytes() + b"\n# candidate lock\n")
    (source / "src/item1.rs").write_bytes((source / "src/item1.rs").read_bytes().replace(
        b"pub fn production() {}", b"pub fn production() { let keep = 3; }"))
    before, calls = entry.core.snapshot(source), []
    evidence = entry.run_candidate(public, source, tmp_path / "cache", private, run=runner(calls))
    assert evidence["exit_status"] == 0 and evidence["candidate_lock_preserved"]
    assert len(calls) == 2 and entry.core.snapshot(source) == before


@pytest.mark.parametrize("fault", ["removed", "changed", "hidden", "duplicated", "conditional", "fixture", "target", "harness"])
def test_original_assertions_cannot_be_removed_changed_hidden_or_redirected(tmp_path, fault):
    source, public, private = fixture(tmp_path)
    path = source / "src/item0.rs"
    data = path.read_bytes()
    if fault == "removed":
        path.unlink()
    elif fault == "changed":
        path.write_bytes(data.replace(b"assert_eq!(1, 1)", b"assert_eq!(2, 2)"))
    elif fault == "hidden":
        path.write_bytes(b"/*\n" + data + b"*/\n")
    elif fault == "duplicated":
        (source / "src/duplicate.rs").write_bytes(data)
    elif fault == "conditional":
        path.write_bytes(data.replace(b'#[cfg(all', b'#[cfg(any())]\n#[cfg(all'))
    elif fault == "fixture":
        (source / "tests/bytes.rs").write_text("changed assertion")
    elif fault == "harness":
        with (source / "Cargo.toml").open("a") as stream:
            stream.write('\n[[test]]\nname="test-bytes"\nharness=false\n')
    calls = []
    value = entry.run_candidate(public, source, tmp_path / "cache", private,
                                run=runner(calls, fault="target" if fault == "target" else None))
    assert value["exit_status"] == 2 and not any("test" in r[0] for r in calls)


def test_failed_original_test_is_not_a_lock_selection_signal(tmp_path):
    source, public, private = fixture(tmp_path)
    calls = []
    value = entry.run_candidate(public, source, tmp_path / "cache", private, run=runner(calls, fail_test=True))
    assert value["exit_status"] == 1 and len([r for r in calls if "test" in r[0]]) == 1
    assert not value["test_results"]["original_1"]


def test_infrastructure_metadata_failure_is_not_compatibility(tmp_path):
    source, public, private = fixture(tmp_path)
    calls = []
    value = entry.run_candidate(public, source, tmp_path / "cache", private, run=runner(calls, fault="infrastructure"))
    assert value["exit_status"] == 2 and len(calls) == 1


def test_explicit_legacy_route_does_not_admit_other_tasks():
    from agentic_runtime_migration import rust_lane
    context = SimpleNamespace(config={"migration": {host.OPTION: {entry.TASK: {}}}})
    assert host.requested(context, {"id": entry.TASK})
    assert not host.requested(context, {"id": "different"})
    assert rust_lane.migration_scratch_bytes(context, {"id": entry.TASK}) == 2 * 1024**3
    context.config["migration"][host.OPTION]["other"] = {}
    with pytest.raises(host.ContractError):
        host.requested(context, {"id": entry.TASK})
