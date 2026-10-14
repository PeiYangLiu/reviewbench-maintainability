"""Locked originals keep their command and use only full current-candidate metadata."""
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_image_entry as entry, rust_locked_workspace as host
from reviewbench_maintainability.agentic.contracts import ContractError
from test_agentic_rust_cargo_contract import nested_fixture, runner, MANIFEST, MEMBER
from test_agentic_rust_image import ref


def locked(source, public, private):
    public.update(schema_version=entry.LOCKED_WORKSPACE_SCHEMA,
                  source_config_mode="isolated-cargo-home", git_dependencies="full-locked-source-identities")
    private.update(schema_version=entry.LOCKED_WORKSPACE_PRIVATE_SCHEMA, original_locked=True)
    private.pop("original_unlocked")
    private["command"].insert(1, "--locked")
    return source, public, private


def fixture(tmp_path, version="0.1.0"):
    return locked(*nested_fixture(tmp_path, version))


@pytest.mark.parametrize("version", ["0.1.0", "0.2.0"])
@pytest.mark.parametrize("grading", [False, True])
def test_original_locked_command_and_configuration_survive_selection(tmp_path, version, grading):
    source, public, private = fixture(tmp_path, version)
    before, calls = entry.snapshot(source), []
    original = runner(calls)

    def checked(command, cwd, env, folder, name, deadline, limit):
        assert "--config" not in command and "--offline" in command
        assert (Path(env["CARGO_HOME"]) / "config.toml").read_bytes() == Path(public["source_config"]["path"]).read_bytes()
        return original(command, cwd, env, folder, name, deadline, limit)

    result = entry.run_candidate(public, source, tmp_path / "work", private if grading else None, run=checked)
    assert result["exit_status"] == 0 and result["command_executions"] == 1
    assert result["source_before"] == result["source_after"] == before
    assert len(result["probes"]) == 2
    assert calls[-1][-len(private["command"]):] == private["command"] if grading else calls[-1][-4:] == public["public_command"]
    assert "--locked" in calls[-1]


def test_negative_original_assertion_never_selects_another_environment(tmp_path):
    source, public, private = fixture(tmp_path, "0.2.0")
    calls = []
    result = entry.run_candidate(public, source, tmp_path / "work", private, run=runner(calls, passed=False))
    assert result["exit_status"] == 101 and result["test_results"] == {"original_behavior": False}
    assert len(calls) == 3 and result["command_executions"] == 1


@pytest.mark.parametrize("removed", [False, True])
def test_changed_or_removed_candidate_lock_is_not_silently_replaced(tmp_path, removed):
    source, public, private = fixture(tmp_path, "0.2.0")
    lock = source / "Cargo.lock"
    if removed:
        lock.unlink()
    else:
        lock.write_bytes(lock.read_bytes() + b"\n# candidate owns this lock\n")
    before, calls = entry.snapshot(source), []
    with pytest.raises(entry.VariantError):
        entry.run_candidate(public, source, tmp_path / "work", private, run=runner(calls))
    assert entry.snapshot(source) == before
    assert len(calls) == (0 if removed else 1)


@pytest.mark.parametrize("change", [
    "old-public", "old-private", "unlocked-command", "unlocked-flag", "no-locked-flag",
    "wrong-package", "wrong-manifest", "cli-transport", "unbound-git", "wrong-seed",
])
def test_new_profile_does_not_loosen_old_commands_or_bindings(tmp_path, change):
    _, public, private = fixture(tmp_path)
    if change == "old-public":
        public["schema_version"] = entry.WORKSPACE_REFERENCE_SCHEMA
    elif change == "old-private":
        private["schema_version"] = entry.WORKSPACE_PRIVATE_SCHEMA
    elif change == "unlocked-command":
        private["command"].remove("--locked")
    elif change == "unlocked-flag":
        private["original_unlocked"] = True
    elif change == "no-locked-flag":
        private.pop("original_locked")
    elif change == "wrong-package":
        private["command"][private["command"].index("-p") + 1] = "other"
    elif change == "wrong-manifest":
        private["package_manifest"] = "other/Cargo.toml"
    elif change == "cli-transport":
        public["source_config_mode"] = "upgrade-cargo"
    elif change == "unbound-git":
        public["git_dependencies"] = "any-revision"
    else:
        public["reference_seed_sha256"] = "0" * 64
    with pytest.raises(entry.VariantError):
        entry.validate(public, private)


@pytest.mark.parametrize("git_source", [
    "git+https://example.invalid/dep?rev=" + "a" * 40 + "#" + "a" * 40,
    "git+https://example.invalid/dep#branch",
    "registry+https://example.invalid/index",
])
def test_non_registry_dependencies_require_full_locked_identity(tmp_path, git_source):
    source, public, private = fixture(tmp_path)
    for row in public["locks"]:
        path = Path(row["path"])
        path.write_text(path.read_text().replace(entry.CRATES_IO, git_source))
        row.update(ref(path))
    public["reference_seed_sha256"] = public["locks"][0]["sha256"]
    (source / "Cargo.lock").write_bytes(Path(public["locks"][0]["path"]).read_bytes())
    calls = []
    if git_source.endswith("#" + "a" * 40):
        result = entry.run_candidate(public, source, tmp_path / "work", private, run=runner(calls))
        assert result["exit_status"] == 0
    else:
        with pytest.raises(entry.VariantError, match="non-registry"):
            entry.run_candidate(public, source, tmp_path / "work", private, run=runner(calls))


@pytest.mark.parametrize("change", ["target", "owner", "disable"])
def test_original_nested_test_identity_is_still_enforced(tmp_path, change):
    source, public, private = fixture(tmp_path)
    calls, extra = [], {}
    if change == "target":
        extra["target_override"] = MEMBER + "/tests/other.rs"
    elif change == "owner":
        extra["owner_override"] = "other/Cargo.toml"
    else:
        with (source / MANIFEST).open("a") as stream:
            stream.write('\n[[test]]\nname="oracle_pnm"\nharness=false\n')
    with pytest.raises(entry.VariantError):
        entry.run_candidate(public, source, tmp_path / "work", private, run=runner(calls, **extra))
    assert all("metadata" in command for command in calls)


def test_profile_is_opt_in_and_foreign_tasks_do_not_enter_it():
    task = next(iter(host.PROFILES))
    assert not host.requested(SimpleNamespace(config={}), {"id": task})
    ctx = SimpleNamespace(config={"migration": {host.OPTION: {task: {"path": "/p", "sha256": "a" * 64}}}})
    assert host.requested(ctx, {"id": task})
    assert not host.requested(ctx, {"id": "another"})
    with pytest.raises(ContractError):
        host.requested(SimpleNamespace(config={"migration": {host.OPTION: {"foreign": {}}}}), {"id": task})


@pytest.mark.parametrize("field", ["repo", "start_sha"])
def test_original_identity_rejected_before_prepared_input_access(field):
    task_id, profile = next(iter(host.PROFILES.items()))
    task = {"id": task_id, "repo": profile["repo"], "start_sha": profile["start"]}
    original = copy.deepcopy(task)
    task[field] = "changed"
    with pytest.raises(ContractError, match="identity"):
        host.original_inputs(SimpleNamespace(), task, original, {}, {})


def test_prepared_inputs_require_independent_hash(tmp_path):
    path = tmp_path / "input.json"
    path.write_text("{}")
    with pytest.raises(ContractError, match="hash"):
        host.referenced({"path": str(path), "sha256": "0" * 64})


def test_cargo_160_exact_frozen_diagnostic_is_not_a_global_exit_101_override(tmp_path):
    command = ["/pinned/cargo", "--offline", "metadata", "--locked", "--all-features"]
    message = (
        "error: the lock file " + str(tmp_path / "Cargo.lock")
        + " needs to be updated but --frozen was passed to prevent this\n"
        "If you want to try to generate the lock file without accessing the network, "
        "remove the --frozen flag and use --offline instead.\n"
    )
    row = {"command": command, "returncode": 101, "cwd": str(tmp_path), "stderr": message}
    assert not entry.lock_incompatible(row)
    assert entry.lock_incompatible(row, original_locked=True)
    for wrong in (
        {**row, "returncode": 1}, {**row, "stderr": message + "another failure\n"},
        {**row, "command": ["/pinned/cargo", "test", "--locked", "--offline"]},
        {**row, "command": ["/pinned/cargo", "metadata", "--locked"]},
    ):
        assert not entry.lock_incompatible(wrong, original_locked=True)


def git_diagnostic(root):
    source = "git+https://example.invalid/modelator#" + "a" * 40
    key = "reviewbench-original-" + entry.digest(source.encode())
    message = (
        "error: failed to get `modelator` as a dependency of package `integration v0.15.0 ("
        + str(root / "tools/integration-test") + ")`\n\n"
        "Caused by:\n  failed to load source for dependency `modelator`\n\n"
        "Caused by:\n  Unable to update https://example.invalid/modelator\n\n"
        "Caused by:\n  the source " + key + " requires a lock file to be present first before it can be\n"
        "  used against vendored source code\n\n"
        "  remove the source replacement configuration, generate a lock file, and then\n"
        "  restore the source replacement configuration to continue the build\n"
    )
    return {"command": ["/pinned/cargo", "--offline", "metadata", "--locked", "--all-features"],
            "returncode": 101, "cwd": str(root), "stderr": message}, [("modelator", "0.2.0", source)]


def test_exact_git_seed_diagnostic_uses_original_lock_union_not_test_outcomes(tmp_path):
    row, packages = git_diagnostic(tmp_path)
    assert entry.lock_incompatible(row, original_locked=True, locked_git_packages=packages)
    assert not entry.lock_incompatible(row, locked_git_packages=packages)
    assert not entry.lock_incompatible(row, original_locked=True)


@pytest.mark.parametrize("change", [
    "exit", "not-metadata", "not-offline", "not-locked", "other-owner", "other-url",
    "other-source-key", "other-dependency", "extra-error", "other-original-commit",
])
def test_git_seed_mismatch_is_not_a_generic_dependency_error_override(tmp_path, change):
    row, packages = git_diagnostic(tmp_path)
    if change == "exit":
        row["returncode"] = 1
    elif change == "not-metadata":
        row["command"] = [a if a != "metadata" else "test" for a in row["command"]]
    elif change == "not-offline":
        row["command"].remove("--offline")
    elif change == "not-locked":
        row["command"].remove("--locked")
    elif change == "other-owner":
        row["stderr"] = row["stderr"].replace(str(tmp_path / "tools/integration-test"), "/elsewhere/package")
    elif change == "other-url":
        row["stderr"] = row["stderr"].replace("Unable to update https://example.invalid/modelator",
                                             "Unable to update https://example.invalid/different")
    elif change == "other-source-key":
        row["stderr"] = row["stderr"].replace("reviewbench-original-" + entry.digest(packages[0][2].encode()),
                                             "reviewbench-original-" + "0" * 64)
    elif change == "other-dependency":
        row["stderr"] = row["stderr"].replace("`modelator`", "`unbound`")
    elif change == "extra-error":
        row["stderr"] += "network request failed\n"
    else:
        packages = [("modelator", "0.2.0", "git+https://example.invalid/modelator#" + "b" * 40)]
    assert not entry.lock_incompatible(row, original_locked=True, locked_git_packages=packages)
