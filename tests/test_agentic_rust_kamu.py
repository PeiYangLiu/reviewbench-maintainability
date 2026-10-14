"""Kamu reuses the same-target loop without changing its original inputs."""
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_api_entry as api, rust_kamu as host, rust_lane
from agentic_runtime_migration import rust_overlay_api_entry as entry
from reviewbench_maintainability.agentic.contracts import ContractError
from test_agentic_rust_image import ref
from test_agentic_rust_same_target_api import fixture as old_fixture
from test_agentic_rust_api_selection import record


def private_fixture(tmp_path):
    root, spec = old_fixture(tmp_path)
    destination = root / "src/domain/core"
    destination.parent.mkdir(parents=True)
    (root / "package").rename(destination)
    spec.update(profile=host.PROFILE, task_id=host.TASK, package="kamu-core",
                package_manifest=host.MANIFESTS[1], expected_tests=[host.TEST], cpus=16)
    for form in spec["forms"]:
        form["harness"] = host.HARNESS
        form["command"] = [
            spec["cargo"]["path"], "test", "-p", "kamu-core", "--test", "oracle",
            host.TEST, "--", "--nocapture",
        ]
    return root, spec


def public_fixture(tmp_path):
    root, private = private_fixture(tmp_path)
    (root / "Cargo.toml").write_text('[workspace]\nmembers=["src/domain/core","old"]\n')
    (root / host.MANIFESTS[1]).write_text('[package]\nname="kamu-core"\nversion="0.1.0"\n')
    (root / "Cargo.lock").write_text("# original fixture seed\n")
    (root / "old").mkdir()
    (root / "old/untouched.rs").write_text("retain the complete source tree\n")
    for name in ("rustc", "sources"):
        (tmp_path / name).write_text("fixed fixture input")
    doc = {
        "schema_version": "reviewbench-cargo-build-overlay/v1", "task_id": host.TASK,
        "locks": [], "private_members": [], "manifests": [
            {"path": "Cargo.toml", "operations": [
                {"kind": "list_edit", "key": ["workspace", "members"],
                 "remove": ["old"], "add": [], "order": ["src/domain/core"]},
            ]},
            {"path": host.MANIFESTS[1], "operations": [
                {"kind": "ensure_table", "key": ["dev-dependencies"]},
                {"kind": "add", "key": ["dev-dependencies", "url"], "value": "2"},
            ]},
        ],
    }
    path = tmp_path / "overlay.json"
    path.write_text(json.dumps(doc))
    public = {
        "schema_version": entry.SAME_TARGET_SCHEMA, "profile": host.PROFILE, "task_id": host.TASK,
        "selection": entry.core.SELECTION, "package": "kamu-core", "package_manifest": host.MANIFESTS[1],
        "feature_args": [], "locks": [ref(root / "Cargo.lock")],
        "reference_seed_sha256": ref(root / "Cargo.lock")["sha256"],
        "cargo": private["cargo"], "rustc": ref(tmp_path / "rustc"),
        "source_config": ref(tmp_path / "sources"), "overlay": ref(path), "overlay_paths": host.MANIFESTS,
        "private_spec_sha256": "a" * 64, "private_tool_sha256": "b" * 64,
        "public_command": ["build", "--locked", "-p", "kamu-core", "--lib", "--message-format=json"],
        "cpus": 16, "timeout_seconds": 3600, "metadata_timeout_seconds": 180, "max_log_bytes": 1024**2,
    }
    return root, public, private, doc


def test_original_sixteen_job_profile_uses_existing_validators(tmp_path):
    _, public, private, _ = public_fixture(tmp_path)
    entry.validate(public)
    api.validate(private)
    assert private["forms"][0]["command"] == private["forms"][1]["command"]
    assert private["forms"][0]["command"][1:] == host.COMMAND.split()[2:]
    assert len({r["asset"]["sha256"] for r in private["forms"]}) == 2


@pytest.mark.parametrize("change", ["profile", "task", "cpus8", "cpus17", "boolean", "timeout", "package", "manifest"])
def test_kamu_binding_never_relaxes_other_original_profiles(tmp_path, change):
    _, public, private, _ = public_fixture(tmp_path)
    for value in (public, private):
        if change == "profile":
            value.pop("profile")
        elif change == "task":
            value["task_id"] = "other"
        elif change == "cpus8":
            value["cpus"] = 8
        elif change == "cpus17":
            value["cpus"] = 17
        elif change == "boolean":
            value["cpus"] = True
        elif change == "timeout":
            value["timeout_seconds"] = 1
        elif change == "package":
            value["package"] = "other"
        else:
            value["package_manifest"] = "another/Cargo.toml"
    with pytest.raises(entry.core.VariantError):
        entry.validate(public)
    with pytest.raises(api.SelectionError):
        api.validate(private)


@pytest.mark.parametrize("words", [["--locked"], ["--features", "replacement"], ["--release"], ["--all-features"]])
def test_original_private_argv_has_no_additional_flags(tmp_path, words):
    _, private = private_fixture(tmp_path)
    for form in private["forms"]:
        form["command"][2:2] = words
    with pytest.raises(api.SelectionError, match="Kamu Cargo argv"):
        api.validate(private)


def test_only_original_workspace_membership_changes_and_all_source_files_survive(tmp_path):
    root, public, _, _ = public_fixture(tmp_path)
    entry.validate(public)
    before = entry.core.snapshot(root)
    spec, changed = entry.current_overlay(root, public)
    assert entry.tomllib.loads(changed["Cargo.toml"].decode())["workspace"]["members"] == ["src/domain/core"]
    assert (root / "old/untouched.rs").read_text() == "retain the complete source tree\n"
    assert entry.core.snapshot(root) == before
    assert spec["locks"] == [] and spec["private_members"] == []


@pytest.mark.parametrize("key", [["workspace", "resolver"], ["workspace", "exclude"], ["package", "version"]])
def test_unrelated_root_manifest_mutations_are_not_admitted(tmp_path, key):
    root, public, _, doc = public_fixture(tmp_path)
    doc["manifests"][0]["operations"][0]["key"] = key
    path = Path(public["overlay"]["path"])
    path.write_text(json.dumps(doc))
    public["overlay"] = ref(path)
    with pytest.raises(entry.core.VariantError, match="unrelated"):
        entry.current_overlay(root, public)


def test_same_target_selection_remains_conjunctive_and_restores_current_mirror(tmp_path, monkeypatch):
    root, private = private_fixture(tmp_path)
    monkeypatch.chdir(root)
    original = root / host.HARNESS
    original.write_text("current placeholder\n")
    original.chmod(0o755)
    calls = []

    def run(command, *args):
        name = original.read_text().split()[1]
        probing = "--no-run" in command
        calls.append((name, probing, command))
        if probing:
            return record(stdout=json.dumps({
                "reason": "compiler-artifact", "manifest_path": str(root / host.MANIFESTS[1]),
                "target": {"kind": ["test"], "name": "oracle", "src_path": str(original)},
            }))
        passed = name == "start"
        return record(0 if passed else 101, stdout=(
            "test " + host.TEST + " ... " + ("ok" if passed else "FAILED") + "\n"
            "test result: " + ("ok. 1 passed; 0 failed" if passed else "FAILED. 0 passed; 1 failed") + "\n"
        ))

    result = api.select_and_test(private, root, tmp_path, run=run)
    assert [(name, probing) for name, probing, _ in calls] == [
        ("start", True), ("golden", True), ("start", False), ("golden", False),
    ]
    assert result["results"] == {host.TEST: False}
    assert original.read_text() == "current placeholder\n" and original.stat().st_mode & 0o777 == 0o755
    assert all(command == private["forms"][0]["command"] for _, probe, command in calls if not probe)


def test_dispatch_is_explicit_and_does_not_change_unrelated_preparation_resources(monkeypatch):
    ctx = SimpleNamespace(config={})
    assert not host.requested(ctx, {"id": host.TASK})
    assert rust_lane.migration_scratch_bytes(ctx, {"id": host.TASK}) == 12 * 1024**3
    ctx.config = {"migration": {host.OPTION: {host.TASK: {}}}}
    assert rust_lane.migration_scratch_bytes(ctx, {"id": host.TASK}) == 2 * 1024**3
    calls = []
    monkeypatch.setattr(host, "migrate", lambda *args: calls.append(args) or "prepared")
    assert rust_lane.migrate(ctx, {"id": host.TASK}, {}) == "prepared"
    assert len(calls) == 1


@pytest.mark.parametrize("options", [None, [], {"foreign-task": {}}])
def test_unknown_input_profiles_stop_before_preparation(options):
    with pytest.raises(ContractError):
        host.requested(SimpleNamespace(config={"migration": {host.OPTION: options}}), {"id": host.TASK})


def test_wrong_original_identity_stops_before_file_access():
    value = {"id": host.TASK, "repo": "kamu-data/kamu-cli", "start_sha": "wrong"}
    with pytest.raises(ContractError, match="task/source"):
        host.original_inputs(SimpleNamespace(), value, value, {}, {})
