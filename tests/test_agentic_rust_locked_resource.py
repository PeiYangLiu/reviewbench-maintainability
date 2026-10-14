"""Original resource modules must match their complete immutable Git source."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_locked_workspace as host
from reviewbench_maintainability.agentic.contracts import ContractError
from reviewbench_maintainability.agentic.snapshots import start_archive, extract_tar


def ref(path):
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], stderr=subprocess.PIPE).decode().strip()


def commit(path):
    git(path, "add", ".")
    git(path, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
    return git(path, "rev-parse", "HEAD")


def fixture(tmp_path):
    module = tmp_path / "module-git"
    module.mkdir()
    git(module, "init", "-q")
    (module / "regexes.yaml").write_text("original: resource\n")
    (module / "LICENSE").write_text("original license\n")
    (module / "other.txt").write_text("keep all original module files\n")
    revision = commit(module)
    parent = tmp_path / "parent"
    parent.mkdir()
    git(parent, "init", "-q")
    (parent / "Cargo.toml").write_text('[workspace]\n')
    git(parent, "add", "Cargo.toml")
    git(parent, "update-index", "--add", "--cacheinfo", "160000", revision, "external/ua")
    git(parent, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "parent")
    start = git(parent, "rev-parse", "HEAD")
    source = tmp_path / "module-source"
    with start_archive(module, revision) as archive:
        extract_tar(archive, source)
    legacy = tmp_path / "legacy"
    (legacy / "rust/session").mkdir(parents=True)
    original = legacy / "rust/session/regexes.yaml"
    original.write_bytes((module / "regexes.yaml").read_bytes())
    checksum = hashlib.sha256(original.read_bytes()).hexdigest()
    files = tmp_path / "files.json"
    files.write_text(json.dumps(host.entry.snapshot(source)))
    descriptor = {
        "path": "external/ua", "commit": revision, "git_repository": str(module), "source": str(source),
        "resource": {"path": "regexes.yaml", "sha256": checksum}, "files": ref(files),
    }
    path = tmp_path / "module.json"
    path.write_text(json.dumps(descriptor))
    profile = {"environment": {"RUST_BACKTRACE": "1", "RELAY_RELEASE": "original"},
               "resource": {"path": "external/ua/regexes.yaml", "source": "session/regexes.yaml",
                            "sha256": checksum, "module": "external/ua", "commit": revision,
                            "relative": "regexes.yaml"}}
    plan = {"env": profile["environment"].copy(), "environment_file_replacements": [
        {"path": "external/ua/regexes.yaml", "source": "session/regexes.yaml", "source_sha256": checksum,
         "allow_missing": True}, {"path": "Cargo.lock"}]}
    evidence = [{"path": "external/ua/regexes.yaml", "source": "session/regexes.yaml", "sha256": checksum},
                {"path": "Cargo.lock", "sha256": "0" * 64}]
    oracle = {"environment_replacements": {"start": copy.deepcopy(evidence), "golden": copy.deepcopy(evidence)}}
    ctx = SimpleNamespace(legacy=legacy, scratch=tmp_path, repo=lambda task: parent)
    task = {"id": "fixture", "start_sha": start}
    prepared = {"resource_module": ref(path)}
    return ctx, task, plan, oracle, prepared, profile, descriptor


def test_complete_locked_resource_module_is_reconstructed_from_git(tmp_path):
    ctx, task, plan, oracle, prepared, profile, descriptor = fixture(tmp_path)
    actual = host.resource_module(ctx, task, plan, oracle, prepared, profile)
    assert actual == descriptor
    assert set(host.entry.snapshot(Path(actual["source"]))) == {"regexes.yaml", "LICENSE", "other.txt"}


@pytest.mark.parametrize("change", [
    "release-env", "plan-path", "plan-hash", "missing-allowed", "extra-preparation",
    "start-evidence", "golden-evidence", "legacy-resource",
])
def test_original_resource_and_environment_bindings_are_not_optional(tmp_path, change):
    ctx, task, plan, oracle, prepared, profile, _ = fixture(tmp_path)
    if change == "release-env":
        plan["env"]["RELAY_RELEASE"] = "other"
    elif change == "plan-path":
        plan["environment_file_replacements"][0]["path"] = "production.rs"
    elif change == "plan-hash":
        plan["environment_file_replacements"][0]["source_sha256"] = "0" * 64
    elif change == "missing-allowed":
        plan["environment_file_replacements"][0]["allow_missing"] = False
    elif change == "extra-preparation":
        plan["environment_file_replacements"].append({"path": "extra"})
    elif change == "legacy-resource":
        (ctx.legacy / "rust/session/regexes.yaml").write_text("changed")
    else:
        oracle["environment_replacements"][change.split("-")[0]][0]["sha256"] = "0" * 64
    with pytest.raises(ContractError):
        host.resource_module(ctx, task, plan, oracle, prepared, profile)


@pytest.mark.parametrize("change", ["descriptor-commit", "descriptor-path", "descriptor-resource",
                                  "resource-bytes", "missing-license", "changed-other-file", "extra-file"])
def test_resource_is_not_a_single_file_stub_or_mutable_checkout(tmp_path, change):
    ctx, task, plan, oracle, prepared, profile, descriptor = fixture(tmp_path)
    path = Path(prepared["resource_module"]["path"])
    source = Path(descriptor["source"])
    if change == "descriptor-commit":
        descriptor["commit"] = "a" * 40
    elif change == "descriptor-path":
        descriptor["path"] = "different"
    elif change == "descriptor-resource":
        descriptor["resource"]["sha256"] = "0" * 64
    elif change == "resource-bytes":
        (source / "regexes.yaml").write_text("changed")
    elif change == "missing-license":
        (source / "LICENSE").unlink()
    elif change == "changed-other-file":
        (source / "other.txt").write_text("changed")
    else:
        (source / "extra").write_text("unbound")
    # Even a self-consistent new file inventory cannot replace immutable Git evidence.
    if change in {"missing-license", "changed-other-file", "extra-file"}:
        files = Path(descriptor["files"]["path"])
        files.write_text(json.dumps(host.entry.snapshot(source)))
        descriptor["files"] = ref(files)
    path.write_text(json.dumps(descriptor))
    prepared["resource_module"] = ref(path)
    with pytest.raises(ContractError):
        host.resource_module(ctx, task, plan, oracle, prepared, profile)


def test_old_profiles_do_not_acquire_resource_overrides():
    assert host.resource_module(SimpleNamespace(), {}, {}, {}, {}, {}) is None
    with pytest.raises(ContractError, match="old profile"):
        host.resource_module(SimpleNamespace(), {}, {}, {}, {"resource_module": {}}, {})


def test_original_named_production_actor_is_not_frozen_as_a_test():
    name = "relay-server/src/actors/test_store.rs"
    ctx = SimpleNamespace(immutable_tests=lambda task: [name, "tests/original.rs"])
    profile = {"production_test_named_paths": [name], "resource": {"path": "external/ua/regexes.yaml"}}
    plan = {"production_test_named_paths": [name], "source_paths": [name]}
    oracle = {"source_changed": [name], "inline_test_preservation": {name: {"start_items": 0, "target_items": 0}},
              "source_only_audit": {name: {"production_matches_target_after_test_stripping": True,
                                         "final_patch_changes_start_test_content": False}}}
    assert host.immutable_paths(ctx, {}, plan, oracle, profile) == ["external/ua/regexes.yaml", "tests/original.rs"]
    oracle["inline_test_preservation"][name]["start_items"] = 1
    with pytest.raises(ContractError):
        host.immutable_paths(ctx, {}, plan, oracle, profile)


def test_old_immutable_inventory_order_is_unchanged():
    paths = ["z/test.rs", "a/test.rs"]
    ctx = SimpleNamespace(immutable_tests=lambda task: paths)
    assert host.immutable_paths(ctx, {}, {}, {}, {}) is paths


def test_relay_profile_is_explicit_opt_in_and_preserves_original_command_family():
    task_id = "github__getsentry__relay__pull_2862__comment_1440311244__modularity"
    assert not host.requested(SimpleNamespace(config={}), {"id": task_id})
    ctx = SimpleNamespace(config={"migration": {host.OPTION: {task_id: {}}}})
    assert host.requested(ctx, {"id": task_id})
    profile = host.PROFILES[task_id]
    assert profile["toolchain"] == "1.75.0" and profile["manifest"] == "relay/Cargo.toml"
    assert profile["resource"]["path"] == "relay-ua/uap-core/regexes.yaml"
