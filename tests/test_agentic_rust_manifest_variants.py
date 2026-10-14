"""Correlated original dependency forms never select an assertion outcome."""
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_manifest_variants_entry as entry, rust_libp2p as host
from reviewbench_maintainability.agentic.contracts import ContractError
from test_agentic_rust_image import ref, output

PATHS = ["app/Cargo.toml", "codec/Cargo.toml"]


def fixture(root):
    source = root / "source"
    source.mkdir()
    (source / "Cargo.toml").write_text('[workspace]\nmembers=["app","codec"]\n')
    (source / "Cargo.lock").write_text("# original committed seed\n")
    originals, common, forms = [], [], []
    for folder in ("app", "codec"):
        (source / folder / "src").mkdir(parents=True)
        (source / folder / "src/lib.rs").write_text("pub fn value()->u8 {1}\n")
        text = ('[package]\nname="' + folder + '"\nversion="0.1.0"\n'
                '[dependencies]\nwire="0.6"\n[dev-dependencies]\noriginal-test-support="1"\n')
        p = source / folder / "Cargo.toml"
        p.write_text(text)
        original = root / (folder + "-original.toml")
        original.write_text(text)
        originals.append({"path": folder + "/Cargo.toml", "reference": ref(original)})
        value = entry.tomllib.loads(text)
        after = copy.deepcopy(value)
        after.pop("dev-dependencies")
        common.append({"path": folder + "/Cargo.toml", "operations": entry.manifest_operations(value, after)})
    for name, version in (("old", "0.6"), ("new", "0.7")):
        manifests = []
        for item in originals:
            original = entry.tomllib.loads(Path(item["reference"]["path"]).read_text())
            original.pop("dev-dependencies")
            original["dependencies"]["wire"] = version
            path = root / (Path(item["path"]).parent.name + "-" + name + ".toml")
            from agentic_runtime_migration.rust_build_entry import tomli_w
            path.write_text(tomli_w.dumps(original))
            manifests.append({"path": item["path"], "asset": ref(path)})
        lock = root / (name + ".lock")
        lock.write_text('version=3\n[[package]]\nname="app"\nversion="0.1.0"\n'
                        '[[package]]\nname="codec"\nversion="0.1.0"\n'
                        '[[package]]\nname="wire"\nversion="' + version + '"\nsource="' + entry.core.CRATES_IO + '"\n')
        forms.append({"name": name, "manifests": manifests, "lock": ref(lock)})
    semantic = root / "semantics.json"
    semantic.write_text(json.dumps({"task_id": "fixture", "common": common}))
    for name in ("cargo", "rustc", "sources.toml"):
        (root / name).write_text("fixed " + name)
    (source / "app/tests").mkdir()
    (source / "app/tests/original.rs").write_text("#[test]\nfn original_behaviour(){}\n")
    public = {
        "schema_version": entry.SCHEMA, "task_id": "fixture", "selection": entry.SELECTION,
        "package": "app", "package_manifest": PATHS[0], "manifests": originals, "forms": forms,
        "slots": [{"path": path, "key": ["dependencies", "wire"]} for path in PATHS],
        "semantic_forms": ref(semantic), "reference_seed_sha256": ref(source / "Cargo.lock")["sha256"],
        "cargo": ref(root / "cargo"), "rustc": ref(root / "rustc"), "source_config": ref(root / "sources.toml"),
        "public_command": ["check", "--locked", "-p", "app", "--message-format=json"],
        "cpus": 2, "timeout_seconds": 60, "metadata_timeout_seconds": 10, "max_log_bytes": 1024**2,
    }
    test = {"path": "app/tests/original.rs", "sha256": ref(source / "app/tests/original.rs")["sha256"]}
    private = {"task_id": "fixture", "package_manifest": PATHS[0], "harness": test,
               "session_files": [test], "expected_tests": ["original_behaviour"],
               "command": ["test", "--locked", "-p", "app", "--test", "original", "--", "--nocapture"]}
    return source, public, private


def diagnostic(command, cwd, *, external=False, linker=False):
    value = {"reason": "compiler-message",
             "manifest_path": str(cwd / "app/Cargo.toml"),
             "target": {"kind": ["lib"], "src_path": str(cwd / "app/src/lib.rs")},
             "message": {"level": "error", "code": None if linker else {"code": "E0308"},
                         "message": "types differ", "spans": [
                             {"is_primary": True, "file_name": "/vendor/wire/src/lib.rs" if external else "app/src/lib.rs"}]}}
    return output(command, cwd, json.dumps(value) + "\n" + json.dumps({"reason": "build-finished", "success": False}), code=101)


def runner(calls, *, compatible=("old",), failed=(), error=None, both_locks=False):
    def run(command, cwd, env, folder, name, deadline, limit):
        values = [entry.source_identity(entry.tomllib.loads((cwd / path).read_text())["dependencies"]["wire"])["version"]
                  for path in PATHS]
        form = "old" if values[0] == "0.6" else "new"
        calls.append((list(command), env["CARGO_TARGET_DIR"], list(values)))
        lock = entry.tomllib.loads((cwd / "Cargo.lock").read_text())
        if "metadata" in command:
            if error:
                return output(command, cwd, stderr=error, code=101)
            if (len(set(values)) != 1 or lock["package"][-1]["version"] != values[0]) and not both_locks:
                return output(command, cwd, stderr="error: the lock file needs to be updated but --locked was passed\n", code=101)
            packages = []
            for p in lock["package"]:
                row = {**p, "id": p["name"] + "@" + p["version"], "targets": []}
                if p.get("source") is None:
                    row["manifest_path"] = str(cwd / p["name"] / "Cargo.toml")
                    if p["name"] == "app":
                        row["targets"] = [{"name": "original", "kind": ["test"],
                                           "src_path": str(cwd / "app/tests/original.rs")}]
                packages.append(row)
            return output(command, cwd, json.dumps({
                "workspace_root": str(cwd), "workspace_members": [p["id"] for p in packages if p.get("source") is None],
                "packages": packages, "resolve": {"nodes": [{"id": p["id"]} for p in packages]}}))
        if "check" in command:
            return output(command, cwd) if form in compatible else diagnostic(command, cwd)
        assert command[1:] == ["test", "--locked", "-p", "app", "--test", "original", "--", "--nocapture"]
        ok = form not in failed
        return output(command, cwd, "test original_behaviour ... " + ("ok" if ok else "FAILED") +
                      "\ntest result: " + ("ok." if ok else "FAILED.") + "\n", code=0 if ok else 101)
    return run


@pytest.mark.parametrize("grading", [False, True])
@pytest.mark.parametrize("compatible", [("old",), ("new",), ("old", "new")])
def test_full_current_graphs_and_public_compiles_precede_all_original_tests(tmp_path, grading, compatible):
    source, public, private = fixture(tmp_path)
    before, calls = entry.core.snapshot(source), []
    result = entry.run_candidate(public, source, tmp_path / "cache", private if grading else None,
                                 run=runner(calls, compatible=compatible))
    assert result["exit_status"] == 0 and result["compatible_forms"] == list(compatible)
    assert result["source_before"] == result["source_after"] == before
    assert len(result["probes"]) == 4 and len(result["public_compiles"]) == 2
    assert not result["selection_uses_test_results"] and result["new_lock_generation_commands"] == 0
    tests = [i for i, (c, _, _) in enumerate(calls) if "test" in c]
    assert not tests or min(tests) > max(i for i, (c, _, _) in enumerate(calls) if "check" in c)
    caches = {form: {cache for command, cache, versions in calls if ("old" if versions[0] == "0.6" else "new") == form}
              for form in ("old", "new")}
    assert len(caches["old"]) == len(caches["new"]) == 1 and caches["old"] != caches["new"]


def test_passing_form_does_not_erase_another_failed_original_assertion(tmp_path):
    source, public, private = fixture(tmp_path)
    calls = []
    result = entry.run_candidate(public, source, tmp_path / "cache", private,
                                 run=runner(calls, compatible=("old", "new"), failed=("old",)))
    assert result["exit_status"] == 1 and result["test_results"] == {"original_behaviour": False}
    assert len(result["executions"]) == 2


def test_no_public_form_compiles_without_private_outcome_selection(tmp_path):
    source, public, private = fixture(tmp_path)
    calls = []
    with pytest.raises(entry.core.VariantError, match="no original dependency form"):
        entry.run_candidate(public, source, tmp_path / "cache", private, run=runner(calls, compatible=()))
    assert all("test" not in command for command, _, _ in calls)


@pytest.mark.parametrize("kind", ["extra-field", "features", "explicit-version", "explicit-git", "owned-lock"])
def test_candidate_configuration_changes_are_preserved(tmp_path, kind):
    source, public, private = fixture(tmp_path)
    from agentic_runtime_migration.rust_build_entry import tomli_w
    for name in PATHS:
        path = source / name
        doc = entry.tomllib.loads(path.read_text())
        if kind == "extra-field":
            doc["package"]["metadata"] = {"candidate": True}
        elif kind == "features":
            doc["dependencies"]["wire"] = {"version": "0.6", "features": ["candidate"]}
        elif kind == "explicit-version":
            doc["dependencies"]["wire"] = "0.7"
        elif kind == "explicit-git":
            doc["dependencies"]["wire"] = {"version": "0.6", "git": "https://example.invalid/owned"}
        path.write_text(tomli_w.dumps(doc))
    if kind == "owned-lock":
        (source / "Cargo.lock").write_bytes(Path(public["forms"][0]["lock"]["path"]).read_bytes() + b"\n# owned\n")
    before = entry.core.snapshot(source)
    forms = entry.build_forms(public, source, before)
    assert len(forms) == (2 if kind in ("extra-field", "features") else 1)
    for form in forms:
        for name, data in form["contents"].items():
            doc = entry.tomllib.loads(data.decode())
            if kind == "extra-field":
                assert doc["package"]["metadata"] == {"candidate": True}
            if kind == "features":
                assert doc["dependencies"]["wire"]["features"] == ["candidate"]
            if kind == "explicit-git":
                assert doc["dependencies"]["wire"]["git"].endswith("/owned")
    assert entry.core.snapshot(source) == before


def test_explicit_candidate_form_reuses_the_same_effective_dependency_cache_key(tmp_path):
    source, public, _ = fixture(tmp_path)
    before = entry.build_forms(public, source, entry.core.snapshot(source))
    for row in public["forms"][1]["manifests"]:
        (source / row["path"]).write_bytes(Path(row["asset"]["path"]).read_bytes())
    after = entry.build_forms(public, source, entry.core.snapshot(source))
    assert len(after) == 1 and after[0]["cache"] == before[1]["cache"]


@pytest.mark.parametrize("fault", ["compiler", "parse", "permission"])
def test_infrastructure_metadata_failure_does_not_license_another_form(tmp_path, fault):
    source, public, private = fixture(tmp_path)
    calls = []
    with pytest.raises(entry.core.VariantError, match="outside graph compatibility"):
        entry.run_candidate(public, source, tmp_path / "cache", private, run=runner(calls, error=fault))
    assert len(calls) == 1


def test_ambiguous_original_locked_graph_is_not_resolved_using_tests(tmp_path):
    source, public, private = fixture(tmp_path)
    calls = []
    with pytest.raises(entry.core.VariantError, match="ambiguous"):
        entry.run_candidate(public, source, tmp_path / "cache", private, run=runner(calls, both_locks=True))
    assert all("metadata" in command for command, _, _ in calls)


@pytest.mark.parametrize("change", ["external", "linker", "limit"])
def test_only_typed_current_production_rustc_errors_are_api_mismatches(tmp_path, change):
    source, _, _ = fixture(tmp_path)
    row = diagnostic(["cargo", "check"], source, external=change == "external", linker=change == "linker")
    if change == "limit":
        row["limit"] = "deadline"
    assert not entry.public_api_mismatch(row, source, entry.core.snapshot(source))


@pytest.mark.parametrize("changed", ["compiler", "config", "original", "form", "session"])
def test_fixed_inputs_cannot_change_before_execution(tmp_path, changed):
    source, public, private = fixture(tmp_path)
    path = {
        "compiler": Path(public["cargo"]["path"]), "config": Path(public["source_config"]["path"]),
        "original": Path(public["manifests"][0]["reference"]["path"]),
        "form": Path(public["forms"][0]["manifests"][0]["asset"]["path"]),
        "session": source / private["harness"]["path"],
    }[changed]
    path.write_bytes(path.read_bytes() + b"\nchanged\n")
    calls = []
    with pytest.raises(entry.core.VariantError):
        entry.run_candidate(public, source, tmp_path / "cache", private, run=runner(calls))
    assert calls == []


def test_opt_in_and_other_existing_routes_are_preserved(monkeypatch):
    from agentic_runtime_migration import rust_lane, rust_project_toolchains
    assert not host.requested(SimpleNamespace(), {"id": host.ID})
    ctx = SimpleNamespace(config={"migration": {host.OPTION: {host.ID: {}}}})
    assert host.requested(ctx, {"id": host.ID}) and not host.requested(ctx, {"id": "other"})
    ctx.config["migration"][host.OPTION]["other"] = {}
    with pytest.raises(ContractError):
        host.requested(ctx, {"id": host.ID})
    marker = object()
    monkeypatch.setattr(rust_project_toolchains, "requested", lambda *_: True)
    monkeypatch.setattr(rust_project_toolchains, "migrate", lambda *_: marker)
    assert rust_lane.migrate(SimpleNamespace(), {"id": "existing"}, {}) is marker
