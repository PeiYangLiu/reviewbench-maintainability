"""Workspace package selection preserves complete candidates and fixed test targets."""
import copy
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
import tomllib

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_image_entry as entry, rust_cargo_contract as host
from reviewbench_maintainability.agentic.contracts import ContractError
from test_agentic_rust_image import fixture, output

MEMBER = "members/image"
MANIFEST = MEMBER + "/Cargo.toml"


def nested(source, public, private):
    member = source / MEMBER
    member.mkdir(parents=True)
    for name in ("Cargo.toml", "src", "tests"):
        shutil.move(str(source / name), member / name)
    (source / "Cargo.toml").write_text('[workspace]\nmembers=["members/image"]\nresolver="2"\n')
    public.update(schema_version=entry.WORKSPACE_REFERENCE_SCHEMA,
                  reference_seed_sha256=public["locks"][0]["sha256"],
                  reference_seed_policy="replace-only-identical-committed-seed-in-private-mirror",
                  package="image", package_manifest=MANIFEST,
                  public_command=["check", "--locked", "-p", "image"], timeout_seconds=3600)
    private.update(schema_version=entry.WORKSPACE_PRIVATE_SCHEMA,
                   package_manifest=MANIFEST, original_unlocked=True)
    private["harness"]["path"] = MEMBER + "/" + private["harness"]["path"]
    private["command"].remove("--locked")
    private["command"][1:1] = ["-p", "image"]
    (source / "Cargo.lock").write_bytes(Path(public["locks"][0]["path"]).read_bytes())
    return source, public, private


def nested_fixture(tmp_path, version="0.1.0"):
    return nested(*fixture(tmp_path, version=version))


def runner(calls, *, passed=True, target_override=None, owner_override=None):
    def run(command, cwd, env, folder, name, deadline, limit):
        calls.append(list(command))
        lock = tomllib.loads((cwd / "Cargo.lock").read_text())
        if "metadata" in command:
            desired = tomllib.loads((cwd / MANIFEST).read_text())["dependencies"]["dep"].lstrip("=")
            dependency = next(p for p in lock["package"] if p["name"] == "dep")
            if dependency["version"] != desired:
                return output(command, cwd, code=101,
                              stderr="error: the lock file needs to be updated but --locked was passed\n")
            packages = []
            for package in lock["package"]:
                row = {**package, "id": package["name"] + "@" + package["version"], "targets": []}
                if package.get("source") is None:
                    row["manifest_path"] = str(cwd / (owner_override or MANIFEST))
                    row["targets"] = [{"name": "oracle_pnm", "kind": ["test"],
                                       "src_path": str(cwd / (target_override or MEMBER + "/tests/oracle_pnm.rs"))}]
                packages.append(row)
            graph = {"workspace_root": str(cwd), "workspace_members": [packages[0]["id"]],
                     "packages": packages, "resolve": {"nodes": [{"id": p["id"]} for p in packages]}}
            return output(command, cwd, stdout=json.dumps(graph))
        if "test" in command:
            status = "ok" if passed else "FAILED"
            return output(command, cwd, stdout="test original_behavior ... " + status
                          + "\ntest result: " + status + ".\n", code=0 if passed else 101)
        return output(command, cwd, stderr="Finished dev profile")
    return run


@pytest.mark.parametrize("version", ["0.1.0", "0.2.0"])
@pytest.mark.parametrize("grading", [False, True])
def test_nested_original_seed_is_selected_only_by_complete_current_graph(tmp_path, version, grading):
    source, public, private = nested_fixture(tmp_path, version)
    before, calls = entry.snapshot(source), []
    record = entry.run_candidate(public, source, tmp_path / "build", private if grading else None, run=runner(calls))
    assert record["exit_status"] == 0 and record["source_before"] == record["source_after"] == before
    assert record["candidate_has_original_reference_seed"] and not record["candidate_lock_preserved"]
    assert record["graph"]["all_manifest_files"] == 4
    assert record["graph"]["local_manifests"] == [MANIFEST]
    assert len(calls) == 3 and record["command_executions"] == 1
    if grading:
        assert calls[-1][-len(private["command"]):] == private["command"] and "--locked" not in calls[-1]


def test_new_candidate_lock_is_never_replaced_by_retained_variant(tmp_path):
    source, public, private = nested_fixture(tmp_path, "0.2.0")
    (source / "Cargo.lock").write_bytes(Path(public["locks"][1]["path"]).read_bytes())
    before, calls = entry.snapshot(source), []
    record = entry.run_candidate(public, source, tmp_path / "build", private, run=runner(calls))
    assert record["candidate_lock_preserved"] and len(calls) == 2
    assert record["mirror_before"] == record["source_after"] == before


def test_original_assertion_failure_is_not_retried_with_a_different_graph(tmp_path):
    source, public, private = nested_fixture(tmp_path)
    calls = []
    record = entry.run_candidate(public, source, tmp_path / "build", private, run=runner(calls, passed=False))
    assert record["exit_status"] == 101 and record["test_results"] == {"original_behavior": False}
    assert len(calls) == 3 and record["command_executions"] == 1


@pytest.mark.parametrize("path", ["/outside/Cargo.toml", "../Cargo.toml", "Cargo.toml",
                                 "members/./image/Cargo.toml", "members/image/Other.toml"])
def test_workspace_manifest_path_is_explicit_and_relative(tmp_path, path):
    _, public, private = nested_fixture(tmp_path)
    public["package_manifest"] = path
    with pytest.raises(entry.VariantError):
        entry.validate(public, private)


@pytest.mark.parametrize("change", ["old-public", "old-private", "wrong-private-package", "over-timeout"])
def test_workspace_mode_does_not_relax_old_schemas_or_resource_limits(tmp_path, change):
    _, public, private = nested_fixture(tmp_path)
    if change == "old-public":
        public["schema_version"] = entry.REFERENCE_SCHEMA
    elif change == "old-private":
        private["schema_version"] = entry.PRIVATE_SCHEMA
    elif change == "wrong-private-package":
        private["package_manifest"] = "other/Cargo.toml"
    else:
        public["timeout_seconds"] = 3601
    with pytest.raises(entry.VariantError):
        entry.validate(public, private)


@pytest.mark.parametrize("change", ["disabled-harness", "redirected-target", "wrong-owner"])
def test_current_member_manifest_cannot_disable_or_redirect_original_harness(tmp_path, change):
    source, public, private = nested_fixture(tmp_path)
    calls, options = [], {}
    if change == "disabled-harness":
        with (source / MANIFEST).open("a") as stream:
            stream.write('\n[[test]]\nname="oracle_pnm"\npath="tests/oracle_pnm.rs"\nharness=false\n')
    elif change == "redirected-target":
        options["target_override"] = MEMBER + "/tests/not_original.rs"
    else:
        options["owner_override"] = "fuzz/Cargo.toml"
    with pytest.raises(entry.VariantError):
        entry.run_candidate(public, source, tmp_path / "build", private, run=runner(calls, **options))
    assert all("metadata" in command for command in calls)


def test_only_the_two_original_tasks_can_opt_in():
    first, second = host.TASKS
    context = SimpleNamespace(config={host.OPTION: {first: "/prepared.json", second: "/prepared.json"}})
    assert host.requested(context, {"id": first}) and host.requested(context, {"id": second})
    assert not host.requested(context, {"id": "another-task"})
    assert not host.requested(SimpleNamespace(), {"id": first})
    with pytest.raises(ContractError):
        host.requested(SimpleNamespace(config={host.OPTION: {"another-task": "/prepared.json"}}), {"id": first})


def test_task109_cannot_acquire_task108_target_lock():
    values = list(host.TASKS.values())
    assert values[0]["retained_golden"] == host.TARGET_LOCK
    assert values[1]["retained_golden"] != host.TARGET_LOCK
    assert values[0]["seed"] != values[1]["seed"]
