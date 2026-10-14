"""Original Git transport pins and complete API selection, not assertion-result fallback."""
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_bitcoin_entry as entry, rust_bitcoin as host
from agentic_runtime_migration.rust_build_entry import tomli_w
from reviewbench_maintainability.agentic.contracts import ContractError
from test_agentic_rust_image import output, ref


def fixture(tmp_path, variant=0):
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    (source / "tests").mkdir()
    (source / "src/lib.rs").write_text("pub fn public_value() -> u32 { 7 }\n")
    doc = {"package": {"name": "app", "version": "0.1.0"},
           "dependencies": {"bitcoin": {"git": host.PINS[variant]["url"], "branch": host.PINS[variant]["branch"]}},
           "patch": {"crates-io": {"bitcoin_hashes": {"git": host.PINS[variant]["url"] + ".git"}}}}
    (source / "Cargo.toml").write_text(tomli_w.dumps(doc))
    for n in ("cargo", "rustc", "sources.toml"):
        (tmp_path / n).write_text("fixed " + n)
    locks = []
    for i, pin in enumerate(host.PINS):
        src = "git+" + pin["url"] + "?rev=" + pin["commit"] + "#" + pin["commit"]
        lock = tmp_path / ("lock-" + str(i))
        lock.write_text(tomli_w.dumps({"version": 3, "package": [
            {"name": "app", "version": "0.1.0"},
            {"name": "bitcoin", "version": "0.30.0", "source": src}]}))
        locks.append({"git": pin["url"], "commit": pin["commit"], "asset": ref(lock)})
    forms, files = [], []
    for name in ("oracle_start", "oracle_golden"):
        p = source / "tests" / (name + ".rs")
        p.write_text("#[test] fn fixed_behaviour() {}\n")
        files.append({"path": p.relative_to(source).as_posix(), "sha256": ref(p)["sha256"]})
        forms.append({"name": name, "target": name, "harness": "tests/" + name + ".rs",
                      "command": ["cargo", "test", "--features", "rand", "--test", name, "--", "--nocapture"]})
    public = {"schema_version": entry.SCHEMA, "task_id": "fixture", "selection": entry.SELECTION,
              "git_pins": host.PINS, "locks": locks, "cargo": ref(tmp_path / "cargo"),
              "rustc": ref(tmp_path / "rustc"), "source_config": ref(tmp_path / "sources.toml"),
              "public_command": ["check", "--locked", "--features", "rand", "--message-format=json"],
              "cpus": 2, "timeout_seconds": 60, "metadata_timeout_seconds": 5, "max_log_bytes": 1024**2}
    private = {"task_id": "fixture", "api": {
        "schema_version": entry.api.SCHEMA, "task_id": "fixture",
        "selection": "compile-only-current-candidate; execute-all-compilable-forms; conjunctive-results",
        "files": files, "forms": forms, "expected_tests": ["fixed_behaviour"],
        "workdir": ".", "cpus": 2, "timeout_seconds": 60, "max_log_bytes": 1024**2}}
    return source, public, private


def diagnostic(command, cwd, target, extra=""):
    message = {"reason": "compiler-message", "manifest_path": str(cwd / "Cargo.toml"),
               "target": {"kind": ["test"], "src_path": str(cwd / "tests" / (target + ".rs"))},
               "message": {"level": "error", "code": {"code": "E0061"}, "message": "argument count differs",
                           "spans": [{"is_primary": True, "file_name": "tests/" + target + ".rs"}]}}
    return output(command, cwd, stdout=json.dumps(message) + "\n" + json.dumps({"reason": "build-finished", "success": False}) + "\n",
                  stderr='error: could not compile `app` (test "' + target + '") due to 1 previous error\n' + extra, code=101)


def runner(calls, compatible=("oracle_start",), failed=(), fault=None):
    def run(command, cwd, env, folder, name, deadline, limit):
        calls.append(list(command))
        if "metadata" in command:
            lock = entry.tomllib.loads((cwd / "Cargo.lock").read_text())
            rows = []
            for package in lock["package"]:
                row = {**package, "id": package["name"] + "@" + package["version"], "targets": []}
                if package.get("source") is None:
                    row["manifest_path"] = str(cwd / "Cargo.toml")
                    row["targets"] = [{"name": n, "kind": ["test"], "src_path": str(cwd / "tests" / (n + ".rs"))}
                                      for n in ("oracle_start", "oracle_golden")]
                rows.append(row)
            return output(command, cwd, json.dumps({"workspace_root": str(cwd), "workspace_members": [rows[0]["id"]],
                          "packages": rows, "resolve": {"nodes": [{"id": row["id"]} for row in rows]}}))
        if "check" in command:
            return output(command, cwd)
        target = command[command.index("--test") + 1]
        if "--no-run" in command:
            if target in compatible:
                return output(command, cwd, json.dumps({"reason": "build-finished", "success": True}))
            result = diagnostic(command, cwd, target, fault or "")
            return result
        assert command == ["cargo", "test", "--features", "rand", "--test", target, "--", "--nocapture"]
        ok = target not in failed
        return output(command, cwd, "test fixed_behaviour ... " + ("ok" if ok else "FAILED") +
                      "\ntest result: " + ("ok." if ok else "FAILED.") + "\n", code=0 if ok else 101)
    return run


@pytest.mark.parametrize("variant", [0, 1])
def test_original_git_pin_changes_only_original_transport_fields(tmp_path, variant):
    source, public, _ = fixture(tmp_path, variant)
    before = entry.tomllib.loads((source / "Cargo.toml").read_text())
    before["dependencies"]["bitcoin"].update(version="0.30", features=["serde"], **{"default-features": False})
    after = entry.pin_manifest(before, public)
    assert after["dependencies"]["bitcoin"] == {
        "git": host.PINS[variant]["url"], "rev": host.PINS[variant]["commit"],
        "version": "0.30", "features": ["serde"], "default-features": False}
    assert after["patch"]["crates-io"]["bitcoin-internals"] == {
        "git": host.PINS[variant]["url"] + ".git", "rev": host.PINS[variant]["commit"]}
    assert entry.pin_manifest(after, public) == after
    assert before["dependencies"]["bitcoin"]["branch"] == host.PINS[variant]["branch"]


@pytest.mark.parametrize("change", ["foreign-rev", "foreign-git", "path", "registry", "extra-selector", "owned-internals"])
def test_candidate_authored_dependency_changes_are_not_replaced(tmp_path, change):
    source, public, _ = fixture(tmp_path)
    before = entry.tomllib.loads((source / "Cargo.toml").read_text())
    if change == "foreign-rev":
        before["dependencies"]["bitcoin"] = {"git": host.PINS[0]["url"], "rev": "e" * 40}
    elif change == "foreign-git":
        before["dependencies"]["bitcoin"]["git"] = "https://example.invalid/candidate"
    elif change == "path":
        before["dependencies"]["bitcoin"] = {"path": "candidate"}
    elif change == "registry":
        before["dependencies"]["bitcoin"] = "0.30"
    elif change == "extra-selector":
        before["dependencies"]["bitcoin"]["tag"] = "candidate"
    else:
        before["patch"]["crates-io"]["bitcoin-internals"] = {"path": "owned", "features": ["custom"]}
    after = entry.pin_manifest(before, public)
    if change == "owned-internals":
        assert after["patch"]["crates-io"]["bitcoin-internals"] == before["patch"]["crates-io"]["bitcoin-internals"]
    else:
        assert after["dependencies"]["bitcoin"] == before["dependencies"]["bitcoin"]


@pytest.mark.parametrize("grading", [False, True])
@pytest.mark.parametrize("variant", [0, 1])
def test_default_lock_uses_current_public_git_identity_and_never_mutates_source(tmp_path, grading, variant):
    source, public, private = fixture(tmp_path, variant)
    before, calls = entry.core.snapshot(source), []
    result = entry.run_candidate(public, source, tmp_path / "cache", private if grading else None,
                                 run=runner(calls, compatible=(("oracle_start",) if variant == 0 else ("oracle_golden",))))
    assert result["exit_status"] == 0
    assert result["selected_lock_sha256"] == public["locks"][variant]["asset"]["sha256"]
    assert not result["candidate_lock_preserved"] and result["new_lock_generation_commands"] == 0
    assert result["source_before"] == result["source_after"] == before
    assert "Cargo.lock" not in entry.core.snapshot(source)
    assert result["graph"]["packages"] == 2
    if grading:
        tests = [i for i, c in enumerate(calls) if "test" in c and "--no-run" not in c]
        probes = [i for i, c in enumerate(calls) if "--no-run" in c]
        assert min(tests) > max(probes)
        assert result["test_results"] == {"fixed_behaviour": True}
    else:
        assert not any("test" in c for c in calls)


def test_passing_first_api_cannot_erase_another_original_failure(tmp_path):
    source, public, private = fixture(tmp_path)
    calls = []
    result = entry.run_candidate(public, source, tmp_path / "cache", private,
                                 run=runner(calls, compatible=("oracle_start", "oracle_golden"), failed=("oracle_golden",)))
    assert result["exit_status"] == 1 and result["test_results"] == {"fixed_behaviour": False}
    assert result["api"]["selected"] == ["oracle_start", "oracle_golden"]
    assert len(result["api"]["tests"]) == 2


def test_no_compatible_api_is_a_negative_result_not_another_attempt(tmp_path):
    source, public, private = fixture(tmp_path)
    calls = []
    result = entry.run_candidate(public, source, tmp_path / "cache", private, run=runner(calls, compatible=()))
    assert result["exit_status"] == 1 and result["test_results"] == {"fixed_behaviour": False}
    assert not any("test" in c and "--no-run" not in c for c in calls)


@pytest.mark.parametrize("fault", ["signal: 11, SIGSEGV\n", "failed to run custom build command\n",
                                 "unknown stderr\n", "internal compiler error\n"])
def test_mixed_infrastructure_failure_does_not_license_private_api_fallback(tmp_path, fault):
    source, public, private = fixture(tmp_path)
    calls = []
    with pytest.raises(entry.core.VariantError, match="not solely"):
        entry.run_candidate(public, source, tmp_path / "cache", private,
                            run=runner(calls, compatible=("oracle_start",), fault=fault))
    assert not any("test" in c and "--no-run" not in c for c in calls)


def test_existing_candidate_lock_survives_verbatim(tmp_path):
    source, public, private = fixture(tmp_path)
    original = Path(public["locks"][0]["asset"]["path"]).read_bytes() + b"\n# candidate-owned\n"
    (source / "Cargo.lock").write_bytes(original)
    result = entry.run_candidate(public, source, tmp_path / "cache", private, run=runner([]))
    assert result["candidate_lock_preserved"] and result["exit_status"] == 0
    assert (source / "Cargo.lock").read_bytes() == original


def test_compiler_validation_does_not_load_binaries_as_bounded_metadata(tmp_path, monkeypatch):
    source, public, private = fixture(tmp_path)
    bounded = entry.core.bounded
    binaries = {Path(public[key]["path"]) for key in ("cargo", "rustc")}
    read_metadata = []

    def metadata_only(path, *args, **kwargs):
        assert Path(path) not in binaries, "compiler binary used the metadata reader"
        read_metadata.append(Path(path))
        return bounded(path, *args, **kwargs)

    monkeypatch.setattr(entry.core, "bounded", metadata_only)
    result = entry.run_candidate(public, source, tmp_path / "cache", private, run=runner([]))
    assert result["exit_status"] == 0 and Path(public["source_config"]["path"]) in read_metadata


def test_configuration_still_has_the_original_metadata_size_bound(tmp_path, monkeypatch):
    source, public, _ = fixture(tmp_path)
    bounded = entry.core.bounded
    config = Path(public["source_config"]["path"])

    def reject_configuration(path, *args, **kwargs):
        if Path(path) == config:
            raise entry.core.VariantError("retained input exceeds its bound")
        return bounded(path, *args, **kwargs)

    monkeypatch.setattr(entry.core, "bounded", reject_configuration)
    with pytest.raises(entry.core.VariantError, match="exceeds"):
        entry.run_candidate(public, source, tmp_path / "cache", run=runner([]))


@pytest.mark.parametrize("fault", ["source", "dependency", "non-json", "missing-finished"])
def test_only_original_harness_diagnostics_can_exclude_a_form(tmp_path, monkeypatch, fault):
    source, _, _ = fixture(tmp_path)
    monkeypatch.chdir(source)
    command = ["cargo", "test", "--no-run", "--test", "oracle_start"]
    row = diagnostic(command, source, "oracle_start")
    lines = row["stdout"].splitlines()
    msg = json.loads(lines[0])
    if fault == "source":
        msg["target"] = {"kind": ["lib"], "src_path": str(source / "src/lib.rs")}
    elif fault == "dependency":
        msg["target"] = {"kind": ["lib"], "src_path": "/vendor/dep/src/lib.rs"}
    if fault in {"source", "dependency"}:
        row["stdout"] = json.dumps(msg) + "\n" + lines[-1]
    elif fault == "non-json":
        row["stdout"] += "unexpected output\n"
    else:
        row["stdout"] = lines[0]
    assert not entry.private_mismatch(row, "tests/oracle_start.rs", source)


@pytest.mark.parametrize("name", ["cargo", "rustc", "source_config"])
def test_fixed_compiler_or_config_tampering_is_not_a_new_environment(tmp_path, name):
    source, public, private = fixture(tmp_path)
    Path(public[name]["path"]).write_text("changed")
    calls = []
    with pytest.raises(entry.core.VariantError):
        entry.run_candidate(public, source, tmp_path / "cache", private, run=runner(calls))
    assert calls == []


def test_unknown_profile_and_other_task_routes_remain_explicit(monkeypatch):
    assert not host.requested(SimpleNamespace(), {"id": host.ID})
    ctx = SimpleNamespace(config={"migration": {host.OPTION: {host.ID: {}}}})
    assert host.requested(ctx, {"id": host.ID})
    assert not host.requested(ctx, {"id": "other"})
    ctx.config["migration"][host.OPTION]["other"] = {}
    with pytest.raises(ContractError):
        host.requested(ctx, {"id": host.ID})
