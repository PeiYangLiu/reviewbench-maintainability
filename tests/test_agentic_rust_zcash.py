import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_zcash as host, rust_zcash_entry as entry, rust_lane
from test_agentic_rust_image import output, ref
from reviewbench_maintainability.agentic.contracts import ContractError


def fixture(tmp_path, version="0.19.1"):
    source = tmp_path / "source"
    files = {
        "Cargo.toml": '[workspace]\nmembers=["zcash_client_sqlite","backend"]\n'
                      '[workspace.dependencies]\nzcash_client_backend={path="backend"}\n',
        entry.MANIFEST: '[package]\nname="zcash_client_sqlite"\nversion="' + version + '"\nedition="2024"\n'
                        '[dependencies]\nzcash_client_backend={workspace=true,features=["unstable-serialization","unstable-spanning-tree"]}\n'
                        '[dev-dependencies]\nzcash_client_backend={workspace=true,features=["test-dependencies","unstable-serialization","unstable-spanning-tree"]}\n',
        entry.LIB: entry.OLD_IMPORT + "\n\npub fn current_value() -> u32 { 7 }\n",
        entry.HARNESS: "#[test] fn original_wallet() { assert_eq!(2+2,4); }\n",
        "backend/Cargo.toml": '[package]\nname="zcash_client_backend"\nversion="0.1.0"\n',
        "backend/src/lib.rs": "pub fn untouched() {}\n",
        "unrelated.txt": "the entire candidate survives\n",
    }
    for name, text in files.items():
        p = source / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    locks = []
    for value in ("0.19.1", "0.19.2"):
        p = tmp_path / (value + ".lock")
        p.write_text('version = 4\n[[package]]\nname="zcash_client_sqlite"\nversion="' + value + '"\n'
                     '[[package]]\nname="zcash_client_backend"\nversion="0.1.0"\n'
                     '[[package]]\nname="external"\nversion="1.0.0"\nsource="' + entry.core.CRATES_IO +
                     '"\nchecksum="' + "a" * 64 + '"\n')
        locks.append(ref(p))
    (source / "Cargo.lock").write_bytes(Path(locks[0]["path"]).read_bytes())
    for name in ("cargo", "rustc", "sources.toml"):
        (tmp_path / name).write_text("fixed " + name)
    public = {
        "schema_version": entry.SCHEMA, "task_id": "fixture", "public_command": entry.PUBLIC,
        "selection": "unique-full-current-locked-metadata-no-test-outcomes",
        "compatibility_policy": "recorded-dev-feature-and-import-only-current-mirror",
        "reference_seed_sha256": locks[0]["sha256"], "locks": locks,
        "cargo": ref(tmp_path / "cargo"), "rustc": ref(tmp_path / "rustc"),
        "source_config": ref(tmp_path / "sources.toml"), "cpus": 4, "timeout_seconds": 3600,
        "metadata_timeout_seconds": 180, "max_log_bytes": 1024**2,
    }
    private = {"task_id": "fixture", "command": entry.COMMAND,
               "harness": {"path": entry.HARNESS, "sha256": ref(source / entry.HARNESS)["sha256"]},
               "expected_tests": ["original_wallet"]}
    return source, public, private


def runner(calls, *, passed=True, ambiguous=False, failure=None, test_text=None):
    def run(command, cwd, env, folder, label, deadline, limit):
        calls.append(list(command))
        assert env["CARGO_BUILD_JOBS"] == "4" and env["CARGO_NET_OFFLINE"] == "true"
        assert (cwd / "unrelated.txt").read_text() == "the entire candidate survives\n"
        if "metadata" in command:
            assert "--all-features" in command and "--locked" in command and "--no-deps" not in command
            if failure:
                return output(command, cwd, stderr=failure, code=101)
            lock = entry.tomllib.loads((cwd / "Cargo.lock").read_text())
            wanted = entry.tomllib.loads((cwd / entry.MANIFEST).read_text())["package"]["version"]
            package = next(p for p in lock["package"] if p["name"] == entry.PACKAGE)
            if not ambiguous and package["version"] != wanted:
                return output(command, cwd, stderr="error: the lock file needs to be updated but --locked was passed\n", code=101)
            rows = []
            for item in lock["package"]:
                row = {**item, "id": item["name"] + "@" + item["version"], "targets": []}
                if not item.get("source"):
                    directory = "zcash_client_sqlite" if item["name"] == entry.PACKAGE else "backend"
                    row["manifest_path"] = str(cwd / directory / "Cargo.toml")
                    if item["name"] == entry.PACKAGE:
                        row["targets"] = [{"name": "oracle_gap", "kind": ["test"], "src_path": str(cwd / entry.HARNESS)}]
                rows.append(row)
            return output(command, cwd, json.dumps({
                "workspace_root": str(cwd), "packages": rows,
                "workspace_members": [p["id"] for p in rows if not p.get("source")],
                "resolve": {"nodes": [{"id": p["id"]} for p in rows]},
            }))
        assert command[1:] == (entry.COMMAND if "test" in command else ["--offline", *entry.PUBLIC])
        text = test_text if test_text is not None else (
            "test original_wallet ... " + ("ok" if passed else "FAILED") +
            "\ntest result: " + ("ok." if passed else "FAILED.") + "\n")
        return output(command, cwd, text, code=0 if passed else 101)
    return run


@pytest.mark.parametrize("version", ["0.19.1", "0.19.2"])
@pytest.mark.parametrize("private_run", [False, True])
def test_original_full_metadata_selects_lock_without_changing_current_source(tmp_path, version, private_run):
    source, public, private = fixture(tmp_path, version)
    before = entry.core.snapshot(source)
    calls = []
    result = entry.run_candidate(public, source, tmp_path / "cache", private if private_run else None, run=runner(calls))
    assert result["exit_status"] == 0 and result["source_unchanged"]
    assert result["selected_lock_sha256"] == public["locks"][version == "0.19.2"]["sha256"]
    assert entry.core.snapshot(source) == before
    assert len(calls) == 3 and sum("test" in c for c in calls) == int(private_run)
    assert result["graph"]["packages"] == 3 and result["graph"]["workspace_members"] == 2
    assert (tmp_path / "cache/source" / entry.LIB).read_text().endswith("pub fn current_value() -> u32 { 7 }\n")


def test_compatibility_changes_only_recorded_dev_feature_and_import(tmp_path):
    source, _, _ = fixture(tmp_path)
    before = entry.tomllib.loads((source / entry.MANIFEST).read_text())
    changes, operations = entry.compatibility(source)
    after = entry.tomllib.loads(changes[entry.MANIFEST].decode())
    assert after["dependencies"] == before["dependencies"]
    assert after["dev-dependencies"]["zcash_client_backend"] == entry.NEW_DEP
    assert before["dev-dependencies"]["zcash_client_backend"] == entry.OLD_DEP
    assert changes[entry.LIB] == (source / entry.LIB).read_bytes().replace(entry.OLD_IMPORT.encode(), entry.NEW_IMPORT.encode(), 1)
    assert len(operations) == 2
    for name, content in changes.items():
        (source / name).write_bytes(content)
    assert entry.compatibility(source) == ({}, [])


def test_candidate_owned_declarations_and_functions_are_not_replaced(tmp_path):
    source, _, _ = fixture(tmp_path)
    d = entry.tomllib.loads((source / entry.MANIFEST).read_text())
    owned = {"path": "../my-backend", "features": ["mine", "test-dependencies"]}
    d["dev-dependencies"]["zcash_client_backend"] = owned
    (source / entry.MANIFEST).write_text(entry.tomli_w.dumps(d))
    (source / entry.LIB).write_text("use candidate::Address;\npub fn different() -> u32 { 99 }\n")
    assert entry.compatibility(source) == ({}, [])


def test_invalid_candidate_integer_is_not_coerced_into_workspace_boolean(tmp_path):
    source, _, _ = fixture(tmp_path)
    d = entry.tomllib.loads((source / entry.MANIFEST).read_text())
    d["dev-dependencies"]["zcash_client_backend"]["workspace"] = 1
    (source / entry.MANIFEST).write_text(entry.tomli_w.dumps(d))
    changes, _ = entry.compatibility(source)
    assert entry.MANIFEST not in changes


@pytest.mark.parametrize("compatible", [True, False])
def test_candidate_lock_is_never_replaced_by_an_original_alternative(tmp_path, compatible):
    source, public, private = fixture(tmp_path, "0.19.1" if compatible else "0.19.2")
    data = (source / "Cargo.lock").read_bytes() + b"\n# candidate owned\n"
    (source / "Cargo.lock").write_bytes(data)
    calls = []
    if compatible:
        result = entry.run_candidate(public, source, tmp_path / "cache", private, run=runner(calls))
        assert result["exit_status"] == 0 and result["candidate_lock_preserved"]
        assert len(calls) == 2
    else:
        with pytest.raises(entry.core.VariantError, match="unique"):
            entry.run_candidate(public, source, tmp_path / "cache", private, run=runner(calls))
        assert len(calls) == 1
    assert (source / "Cargo.lock").read_bytes() == data


def test_negative_assertion_is_not_a_reason_to_try_another_graph(tmp_path):
    source, public, private = fixture(tmp_path)
    calls = []
    result = entry.run_candidate(public, source, tmp_path / "cache", private, run=runner(calls, passed=False))
    assert result["exit_status"] == 1 and result["test_results"] == {"original_wallet": False}
    assert len(calls) == 3 and sum("test" in c for c in calls) == 1


def test_ambiguous_full_graph_does_not_choose_using_test_outcomes(tmp_path):
    source, public, private = fixture(tmp_path)
    calls = []
    with pytest.raises(entry.core.VariantError, match="unique"):
        entry.run_candidate(public, source, tmp_path / "cache", private, run=runner(calls, ambiguous=True))
    assert len(calls) == 2 and not any("test" in c for c in calls)


@pytest.mark.parametrize("error", ["network failure\n", "internal compiler error\n", "No space left on device\n"])
def test_unrelated_metadata_failures_do_not_license_lock_fallback(tmp_path, error):
    source, public, private = fixture(tmp_path)
    calls = []
    with pytest.raises(entry.core.VariantError, match="outside lock"):
        entry.run_candidate(public, source, tmp_path / "cache", private, run=runner(calls, failure=error))
    assert len(calls) == 1


@pytest.mark.parametrize("key", ["cargo", "rustc", "source_config", "lock", "harness", "missing-lock"])
def test_changed_fixed_inputs_and_harness_stop_before_commands(tmp_path, key):
    source, public, private = fixture(tmp_path)
    if key == "lock":
        path = Path(public["locks"][0]["path"])
    elif key == "harness":
        path = source / entry.HARNESS
    elif key == "missing-lock":
        (source / "Cargo.lock").unlink()
        path = None
    else:
        path = Path(public[key]["path"])
    if path:
        path.write_text("changed")
    calls = []
    with pytest.raises(entry.core.VariantError):
        entry.run_candidate(public, source, tmp_path / "cache", private, run=runner(calls))
    assert calls == []


@pytest.mark.parametrize("text", ["test result: ok.\n", "test original_wallet ... ignored\ntest result: ok.\n",
                                  "test original_wallet ... ok\ntest original_wallet ... ok\ntest result: ok.\n"])
def test_missing_or_duplicate_original_tests_are_not_success(tmp_path, text):
    source, public, private = fixture(tmp_path)
    result = entry.run_candidate(public, source, tmp_path / "cache", private, run=runner([], test_text=text))
    assert result["exit_status"] == 1 and result["test_results"] == {"original_wallet": False}


def test_large_compiler_binaries_do_not_use_bounded_metadata_reader(tmp_path, monkeypatch):
    source, public, private = fixture(tmp_path)
    binaries = {Path(public[k]["path"]) for k in ("cargo", "rustc")}
    old = entry.core.bounded
    def checked(path):
        assert path not in binaries
        return old(path)
    monkeypatch.setattr(entry.core, "bounded", checked)
    assert entry.run_candidate(public, source, tmp_path / "cache", private, run=runner([]))["exit_status"] == 0


def test_opt_in_and_input_scratch_leave_other_rust_routes_unchanged():
    ctx = SimpleNamespace(config={})
    assert not host.requested(ctx, {"id": host.ID})
    assert rust_lane.migration_scratch_bytes(ctx, {"id": host.ID}) == 12 * 1024**3
    ctx.config = {"migration": {host.OPTION: {host.ID: {}}}}
    assert host.requested(ctx, {"id": host.ID})
    assert rust_lane.migration_scratch_bytes(ctx, {"id": host.ID}) == 2 * 1024**3
    assert rust_lane.migration_scratch_bytes(ctx, {"id": "other"}) == 12 * 1024**3
    ctx.config["migration"][host.OPTION]["other"] = {}
    with pytest.raises(ContractError, match="unknown"):
        host.requested(ctx, {"id": host.ID})


@pytest.mark.parametrize("change", ["public-features", "private-features", "test-selector", "policy"])
def test_original_features_and_test_selection_cannot_be_narrowed(tmp_path, change):
    _, public, private = fixture(tmp_path)
    public, private = copy.deepcopy(public), copy.deepcopy(private)
    if change == "public-features":
        public["public_command"][5] = "transparent-inputs"
    elif change == "private-features":
        private["command"][5] = "transparent-inputs"
    elif change == "test-selector":
        private["command"].append("easier")
    else:
        public["compatibility_policy"] = "replace-production-source"
    with pytest.raises(entry.core.VariantError):
        entry.validate(public, private)
