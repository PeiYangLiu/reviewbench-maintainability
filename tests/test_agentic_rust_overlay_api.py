"""Original overlays preserve current edits; metadata and API compilation are separate."""
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_overlay_api_entry as entry, rust_vcx as host
from reviewbench_maintainability.agentic.contracts import ContractError
from test_agentic_rust_image import ref, output

MANIFEST = host.MANIFEST


def fixture(root, *, selected="0.1.0"):
    source = root / "candidate"
    package = source / Path(MANIFEST).parent
    (package / "src").mkdir(parents=True)
    (package / "tests").mkdir()
    (package / "src/lib.rs").write_text("pub fn value()->u8 {1}\n")
    (source / "Cargo.toml").write_text(
        '[workspace]\nmembers=["aries/aries_vcx_core","old-member"]\n[lints.clippy]\nall="warn"\n')
    (package / "Cargo.toml").write_text(
        '[package]\nname="aries_vcx_core"\nversion="0.1.0"\n[dependencies]\ndep="' + selected + '"\n')
    (source / "old-member").mkdir()
    (source / "old-member/Cargo.toml").write_text('[package]\nname="original-extra"\nversion="0.1.0"\n')
    files = []
    for target in ("original_a", "original_b"):
        name = "aries/aries_vcx_core/tests/" + target + ".rs"
        (source / name).write_text("#[test]\nfn original() {}\n")
        files.append({"path": name, "sha256": ref(source / name)["sha256"]})
    before = entry.tomllib.loads((source / "Cargo.toml").read_text())
    after = copy.deepcopy(before)
    after["workspace"]["members"] = ["aries/aries_vcx_core"]
    after["workspace"]["lints"] = after.pop("lints")
    overlay = {"schema_version": "reviewbench-cargo-build-overlay/v1", "task_id": "unit-overlay-api",
               "manifests": [{"path": "Cargo.toml", "operations": entry.manifest_operations(before, after)},
                             {"path": MANIFEST, "operations": [
                                 {"kind": "ensure_table", "key": ["dev-dependencies"]},
                                 {"kind": "add", "key": ["dev-dependencies", "support"], "value": "1"}]}],
               "locks": [], "private_members": []}
    path = root / "overlay.json"
    path.write_text(json.dumps(overlay))
    locks = []
    for version in ("0.1.0", "0.2.0"):
        path = root / ("lock-" + version)
        path.write_text('version=3\n[[package]]\nname="aries_vcx_core"\nversion="0.1.0"\n'
                        '[[package]]\nname="dep"\nversion="' + version + '"\nsource="' + entry.core.CRATES_IO + '"\n')
        locks.append(ref(path))
    (source / "Cargo.lock").write_text("# original committed seed\n")
    for name in ("cargo", "rustc", "sources.toml"):
        (root / name).write_text("fixed input " + name)
    public = {
        "schema_version": entry.SCHEMA, "task_id": "unit-overlay-api", "selection": entry.core.SELECTION,
        "package": host.PACKAGE, "package_manifest": MANIFEST, "locks": locks,
        "reference_seed_sha256": ref(source / "Cargo.lock")["sha256"],
        "cargo": ref(root / "cargo"), "rustc": ref(root / "rustc"),
        "overlay": ref(root / "overlay.json"), "source_config": ref(root / "sources.toml"),
        "public_command": ["check", "--locked", "-p", host.PACKAGE, "--features", "askar_wallet", "--message-format=json"],
        "cpus": 2, "timeout_seconds": 60, "metadata_timeout_seconds": 10, "max_log_bytes": 1024**2,
    }
    private = {"expected_tests": ["original"], "files": files, "forms": [
        {"name": Path(r["path"]).stem, "target": Path(r["path"]).stem, "harness": r["path"]} for r in files]}
    return source, public, private


def runner(calls, *, passed=True, error=None, both=False, owner=None, target=None):
    def run(command, cwd, env, folder, label, deadline, limit):
        calls.append(list(command))
        manifest = entry.tomllib.loads((cwd / MANIFEST).read_text())
        lock = entry.tomllib.loads((cwd / "Cargo.lock").read_text())
        if "metadata" in command:
            if error:
                return output(command, cwd, stderr=error, code=101)
            if manifest["dependencies"]["dep"] != lock["package"][1]["version"] and not both:
                return output(command, cwd, stderr="error: the lock file needs to be updated but --locked was passed\n", code=101)
            packages = [{**p, "id": p["name"] + "@" + p["version"], "targets": []} for p in lock["package"]]
            packages[0]["manifest_path"] = str(cwd / (owner or MANIFEST))
            packages[0]["targets"] = [{
                "name": name, "kind": ["test"],
                "src_path": str(cwd / (target or ("aries/aries_vcx_core/tests/" + name + ".rs")))}
                for name in ("original_a", "original_b")]
            graph = {"workspace_root": str(cwd), "workspace_members": [packages[0]["id"]],
                     "packages": packages, "resolve": {"nodes": [{"id": p["id"]} for p in packages]}}
            return output(command, cwd, json.dumps(graph))
        if "check" in command:
            return output(command, cwd, stderr="public compile feedback\n", code=0 if passed else 101)
        assert command[1:] == ["/private-api.py", "--spec", "/private-api.json"]
        return output(command, cwd, "test original ... " + ("ok" if passed else "FAILED") + "\n",
                      code=0 if passed else 1)
    return run


@pytest.mark.parametrize("selected", ["0.1.0", "0.2.0"])
@pytest.mark.parametrize("grading", [False, True])
def test_unique_full_graph_precedes_original_public_or_private_execution(tmp_path, selected, grading):
    source, public, private = fixture(tmp_path, selected=selected)
    before, calls = entry.core.snapshot(source), []
    value = entry.run_candidate(public, source, tmp_path / "work", private if grading else None,
                               private_path="/private-api.json", private_tool="/private-api.py", run=runner(calls))
    assert value["status"] == "completed" and value["exit_status"] == 0 and value["command_executions"] == 1
    assert value["source_before"] == value["source_after"] == before == entry.core.snapshot(source)
    assert len(calls) == 3 and all("metadata" in c for c in calls[:2])
    assert value["graph"]["all_manifest_files"] == 3
    assert not value["candidate_lock_preserved"] and not value["selection_uses_test_results"]
    root = entry.tomllib.loads((tmp_path / "work/source/Cargo.toml").read_text())
    assert "lints" not in root and root["workspace"]["lints"]["clippy"]["all"] == "warn"


@pytest.mark.parametrize("grading", [False, True])
def test_negative_execution_is_not_retried_with_another_original_lock(tmp_path, grading):
    source, public, private = fixture(tmp_path)
    calls = []
    value = entry.run_candidate(public, source, tmp_path / "work", private if grading else None,
                               private_path="/private-api.json", private_tool="/private-api.py",
                               run=runner(calls, passed=False))
    assert value["exit_status"] == (1 if grading else 101) and len(calls) == 3
    if grading:
        assert value["test_results"] == {"original": False}
    else:
        assert value["execution"]["stderr"] == "public compile feedback\n"


def test_model_edited_lints_and_new_members_survive_original_header_move(tmp_path):
    source, public, _ = fixture(tmp_path)
    path = source / "Cargo.toml"
    value = entry.tomllib.loads(path.read_text())
    value["lints"]["clippy"]["all"] = "deny"
    value["lints"]["clippy"]["candidate_rule"] = "allow"
    value["workspace"]["members"].append("new-member")
    value["package"] = {"metadata": {"candidate": True}}
    path.write_text(entry.tomli_w.dumps(value))
    _, data = entry.current_overlay(source, public)
    transformed = entry.tomllib.loads(data["Cargo.toml"].decode())
    assert transformed["workspace"]["members"] == ["aries/aries_vcx_core", "new-member"]
    assert transformed["workspace"]["lints"] == value["lints"]
    assert transformed["package"] == value["package"]
    assert entry.tomllib.loads(path.read_text()) == value


def test_conflicting_lint_namespaces_fail_instead_of_overwriting_candidate(tmp_path):
    source, public, _ = fixture(tmp_path)
    path = source / "Cargo.toml"
    value = entry.tomllib.loads(path.read_text())
    value["workspace"]["lints"] = {"clippy": {"all": "deny"}}
    path.write_text(entry.tomli_w.dumps(value))
    with pytest.raises(entry.core.VariantError, match="conflicting lint"):
        entry.current_overlay(source, public)


@pytest.mark.parametrize("compatible", [True, False])
def test_candidate_owned_lock_is_never_overwritten(tmp_path, compatible):
    source, public, private = fixture(tmp_path)
    lock = Path(public["locks"][0 if compatible else 1]["path"]).read_bytes() + b"\n# owned\n"
    (source / "Cargo.lock").write_bytes(lock)
    calls = []
    if compatible:
        value = entry.run_candidate(public, source, tmp_path / "work", run=runner(calls))
        assert value["candidate_lock_preserved"] and len(calls) == 2
    else:
        with pytest.raises(entry.core.VariantError, match="no unique"):
            entry.run_candidate(public, source, tmp_path / "work", run=runner(calls))
        assert len(calls) == 1
    assert (source / "Cargo.lock").read_bytes() == lock


def test_two_compatible_locks_are_rejected_before_any_private_test(tmp_path):
    source, public, private = fixture(tmp_path)
    calls = []
    with pytest.raises(entry.core.VariantError, match="no unique"):
        entry.run_candidate(public, source, tmp_path / "work", private,
                            private_tool="/private-api.py", private_path="/private-api.json", run=runner(calls, both=True))
    assert len(calls) == 2


@pytest.mark.parametrize("error", ["permission denied", "error: failed to parse manifest", "rustc not found"])
def test_noncompatibility_metadata_failure_does_not_select_another_lock(tmp_path, error):
    source, public, _ = fixture(tmp_path)
    calls = []
    with pytest.raises(entry.core.VariantError, match="outside original lock"):
        entry.run_candidate(public, source, tmp_path / "work", run=runner(calls, error=error))
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["owner", "target", "disabled"])
def test_original_private_targets_remain_bound_to_current_package(tmp_path, change):
    source, public, private = fixture(tmp_path)
    if change == "disabled":
        path = source / MANIFEST
        path.write_text(path.read_text() + '\n[[test]]\nname="original_a"\nharness=false\n')
    with pytest.raises(entry.core.VariantError):
        entry.run_candidate(public, source, tmp_path / "work", private,
                            private_tool="/private-api.py", private_path="/private-api.json",
                            run=runner([], owner="old-member/Cargo.toml" if change == "owner" else None,
                                       target="aries/aries_vcx_core/src/lib.rs" if change == "target" else None))


@pytest.mark.parametrize("field", ["cargo", "rustc", "source_config", "overlay", "lock"])
def test_fixed_inputs_changed_before_execution_are_rejected(tmp_path, field):
    source, public, _ = fixture(tmp_path)
    path = Path(public["locks"][0]["path"] if field == "lock" else public[field]["path"])
    path.write_bytes(path.read_bytes() + b"\nchanged\n")
    calls = []
    with pytest.raises(entry.core.VariantError):
        entry.run_candidate(public, source, tmp_path / "work", run=runner(calls))
    assert calls == []


@pytest.mark.parametrize("mutation", ["source", "mirror", "lock", "hash", "limit"])
def test_complete_raw_metadata_and_candidate_bytes_are_required(tmp_path, mutation):
    source, public, _ = fixture(tmp_path)
    ordinary = runner([])
    def altered(command, cwd, env, folder, name, deadline, limit):
        row = ordinary(command, cwd, env, folder, name, deadline, limit)
        if mutation == "source":
            (source / MANIFEST).write_text("changed")
        elif mutation == "mirror":
            (cwd / "aries/aries_vcx_core/src/lib.rs").write_text("changed")
        elif mutation == "lock":
            (cwd / "Cargo.lock").write_text("changed")
        elif mutation == "hash":
            row["stdout_sha256"] = "0" * 64
        else:
            row["limit"] = "deadline"
        return row
    with pytest.raises(entry.core.VariantError):
        entry.run_candidate(public, source, tmp_path / "work", run=altered)


def test_only_explicit_original_task_is_enabled():
    assert not host.requested(SimpleNamespace(), {"id": host.ID})
    ctx = SimpleNamespace(config={"migration": {host.OPTION: {host.ID: {}}}})
    assert host.requested(ctx, {"id": host.ID}) and not host.requested(ctx, {"id": "unrelated"})
    ctx.config["migration"][host.OPTION]["unrelated"] = {}
    with pytest.raises(ContractError):
        host.requested(ctx, {"id": host.ID})


@pytest.mark.parametrize("change", ["unlocked", "feature", "test", "compiler", "prefix"])
def test_original_apt_prefix_and_cargo_arguments_are_not_general_shell_rewrites(change):
    plan = {}
    for v, target in (("start", "oracle_start"), ("golden", "oracle_golden")):
        plan[v + "_command"] = host.PREFIX + (
            "cargo +1.79.0 test -p aries_vcx_core --features askar_wallet --test " + target
            + " anoncrypt_pack_then_unpack_recovers_plaintext -- --nocapture")
    oracle = {"environment": {"variant_commands": {v: plan[v + "_command"] for v in ("start", "golden")}}}
    good = host.original_commands(plan, oracle)
    assert all("--locked" not in c for c in good)
    replacements = {"unlocked": ("test -p", "test --locked -p"), "feature": ("askar_wallet", "other"),
                    "test": ("oracle_start", "other"), "compiler": ("1.79.0", "1.80.0"),
                    "prefix": ("libsodium-dev", "other-dev")}
    a, b = replacements[change]
    plan["start_command"] = plan["start_command"].replace(a, b)
    oracle["environment"]["variant_commands"]["start"] = plan["start_command"]
    with pytest.raises(ContractError):
        host.original_commands(plan, oracle)


def test_existing_route_is_unchanged_without_opt_in(monkeypatch):
    from agentic_runtime_migration import rust_lane, rust_manifest_service
    marker = object()
    monkeypatch.setattr(rust_manifest_service, "requested", lambda *_: True)
    monkeypatch.setattr(rust_manifest_service, "migrate", lambda *_: marker)
    assert rust_lane.migrate(SimpleNamespace(), {"id": "existing"}, {}) is marker


def test_moved_table_order_uses_the_exact_shared_mirror_emitter(tmp_path):
    source, public, _ = fixture(tmp_path)
    path = source / "Cargo.toml"
    value = entry.tomllib.loads(path.read_text())
    # Distinct insertion/alphabetic orders reproduce the real original lints.
    value["lints"]["clippy"].update({"z_last": "deny", "a_first": "allow"})
    path.write_text(entry.tomli_w.dumps(value))
    before = entry.core.snapshot(source)
    result = entry.run_candidate(public, source, tmp_path / "work", run=runner([]))
    assert result["exit_status"] == 0 and entry.core.snapshot(source) == before
    observed = entry.tomllib.loads((tmp_path / "work/source/Cargo.toml").read_text())
    assert observed["workspace"]["lints"] == value["lints"]
