import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from types import SimpleNamespace
import tomllib

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration.rust_build_entry import (
    OverlayError, apply_manifest_operations, manifest_operations, overlay_files, synchronize_candidate,
)
from agentic_runtime_migration.rust_overlay import (
    build_specification, lock_prepare, session_bytes, portable_environment, cargo_cli_prefix,
)
from agentic_runtime_migration.rust_lane import validate_lock_normalization
from reviewbench_maintainability.agentic.contracts import ContractError


def digest(data):
    return hashlib.sha256(data).hexdigest()


def test_cargo_overlay_keeps_candidate_helpers_and_unrelated_dependency_edits():
    before = {"workspace": {"members": ["app", "unrelated"]},
              "dependencies": {"same": {"version": "1.0"}, "removed": "2.0"}}
    after = {"workspace": {"members": ["app"]}, "dependencies": {"same": {"version": "1.0"}}}
    operations = manifest_operations(before, after)
    assert apply_manifest_operations(before, operations) == after
    candidate = {"workspace": {"members": ["app", "unrelated", "new-helper"]},
                 "dependencies": {"same": {"version": "1.1"}, "removed": "2.0", "new-helper": {"path": "helper"}}}
    result = apply_manifest_operations(candidate, operations)
    assert result["workspace"]["members"] == ["app", "new-helper"]
    assert result["dependencies"] == {"same": {"version": "1.1"}, "new-helper": {"path": "helper"}}
    assert candidate["workspace"]["members"] == ["app", "unrelated", "new-helper"]


def test_cargo_manifest_add_remove_replace_and_empty_tables_round_trip():
    before = {"package": {"name": "old"}, "dependencies": {"unchanged": "1", "dep": ">=1"}}
    after = {"workspace": {}, "dependencies": {"unchanged": "1", "dep": "=1", "new": {"version": "2"}}}
    operations = manifest_operations(before, after)
    assert apply_manifest_operations(before, operations) == after
    assert apply_manifest_operations(after, operations) == after
    changed = {"package": {"name": "different"}, "dependencies": dict(before["dependencies"])}
    with pytest.raises(OverlayError, match="changed a removed"):
        apply_manifest_operations(changed, operations)
    with pytest.raises(OverlayError, match="fixed build"):
        apply_manifest_operations({"dependencies": {"dep": "9"}}, operations)


def test_cargo_member_order_preserves_the_frozen_order_and_candidate_additions():
    operations = manifest_operations({"workspace": {"members": ["b", "a"]}},
                                     {"workspace": {"members": ["a", "b", "c"]}})
    assert apply_manifest_operations({"workspace": {"members": ["b", "a", "helper"]}}, operations) == {
        "workspace": {"members": ["a", "b", "c", "helper"]}}


def test_cargo_overlay_distinguishes_invalid_integer_from_boolean_candidate_setting():
    operations = manifest_operations({"dependencies": {"dep": {"default-features": False}}},
                                     {"dependencies": {"dep": {"default-features": True}}})
    with pytest.raises(OverlayError, match="fixed build"):
        apply_manifest_operations({"dependencies": {"dep": {"default-features": 0}}}, operations)


def test_checked_rust_prepare_only_accepts_attested_lock_copy():
    sha = "a" * 64
    command = "printf '%s  %s\\n' '" + sha + "' 'tests/oracle.Cargo.lock' | sha256sum -c - >/dev/null && cp tests/oracle.Cargo.lock Cargo.lock"
    assert lock_prepare(command) == {"source": "tests/oracle.Cargo.lock", "path": "Cargo.lock", "sha256": sha}
    assert lock_prepare(None) is None
    for invalid in [command + "; echo extra", command.replace("Cargo.lock", "Cargo.toml"), "cargo update"]:
        with pytest.raises(ContractError):
            lock_prepare(invalid)


def _spec(source, default, *, replace=()):
    before = tomllib.loads((source / "Cargo.toml").read_text())
    return {
        "task_id": "fixture", "cpus": 2,
        "manifests": [{"path": "Cargo.toml", "operations": manifest_operations(
            before, {**before, "workspace": {"members": ["app"]}})}],
        "locks": [{"path": "Cargo.lock", "asset_target": str(default), "sha256": digest(default.read_bytes()),
                   "replace_if_sha256": list(replace)}],
    }


def test_mirror_preserves_all_candidate_source_and_stable_mtimes_but_deletes_stale_files(tmp_path):
    source, mirror = tmp_path / "candidate", tmp_path / "build/source"
    (source / "src").mkdir(parents=True)
    (source / "Cargo.toml").write_text('[workspace]\nmembers = ["app", "other"]\n')
    (source / "src/main.rs").write_text("fn main() { helper::run(); }\n")
    (source / "src/helper.rs").write_text("pub fn run() {}\n")
    (source / ".git").mkdir()
    (source / ".git/private").write_text("not a build input")
    default = tmp_path / "Cargo.lock.default"
    default.write_text("version = 3\n")
    spec = _spec(source, default)
    original = {name: path.read_bytes() for name, path in
                [(p.relative_to(source).as_posix(), p) for p in source.rglob("*") if p.is_file()]}
    first = synchronize_candidate(source, mirror, spec)
    assert first["files"] == 4
    assert (mirror / "src/helper.rs").read_text() == "pub fn run() {}\n"
    assert not (mirror / ".git").exists()
    assert tomllib.loads((mirror / "Cargo.toml").read_text())["workspace"]["members"] == ["app"]
    stamp = (mirror / "src/main.rs").stat().st_mtime_ns
    second = synchronize_candidate(source, mirror, spec)
    assert second["updated"] == 0 and (mirror / "src/main.rs").stat().st_mtime_ns == stamp
    assert all((source / name).read_bytes() == data for name, data in original.items())
    (source / "src/helper.rs").unlink()
    (source / "src/new_helper.rs").write_text("new helper")
    third = synchronize_candidate(source, mirror, spec)
    assert third["deleted"] == 1 and not (mirror / "src/helper.rs").exists()
    assert (mirror / "src/new_helper.rs").read_text() == "new helper"


def test_model_owned_lock_is_not_overwritten_or_hidden_as_runtime_scratch(tmp_path):
    source = tmp_path / "candidate"
    source.mkdir()
    (source / "Cargo.toml").write_text('[workspace]\nmembers = ["app", "other"]\n')
    default = tmp_path / "default"
    default.write_text("version = 3\n# frozen\n")
    original = b"version = 3\n# start\n"
    (source / "Cargo.lock").write_bytes(original)
    spec = _spec(source, default, replace=[digest(original)])
    assert overlay_files(source, spec)["Cargo.lock"] == default.read_bytes()
    candidate = b"version = 3\n# candidate graph\n"
    (source / "Cargo.lock").write_bytes(candidate)
    assert "Cargo.lock" not in overlay_files(source, spec)
    synchronize_candidate(source, tmp_path / "mirror", spec)
    assert (tmp_path / "mirror/Cargo.lock").read_bytes() == candidate
    assert (source / "Cargo.lock").read_bytes() == candidate


def test_build_mirror_preserves_dangling_fixtures_but_rejects_escaping_links(tmp_path):
    source, mirror = tmp_path / "candidate", tmp_path / "mirror"
    source.mkdir()
    (source / "dangling").symlink_to("missing")
    synchronize_candidate(source, mirror, {})
    assert os.readlink(mirror / "dangling") == "missing"
    (source / "escape").symlink_to("../outside")
    with pytest.raises(OverlayError, match="escapes"):
        synchronize_candidate(source, mirror, {})


def test_build_mirror_omits_worktree_git_pointer_files(tmp_path):
    source = tmp_path / "candidate"
    source.mkdir()
    (source / ".git").write_text("gitdir: /outside/private/repository")
    (source / "lib.rs").write_text("production")
    synchronize_candidate(source, tmp_path / "mirror", {})
    assert not (tmp_path / "mirror/.git").exists()
    assert (tmp_path / "mirror/lib.rs").read_text() == "production"


def test_cargo_overlay_never_replaces_production_code(tmp_path):
    source = tmp_path / "candidate"
    source.mkdir()
    (source / "lib.rs").write_text("production")
    spec = {"manifests": [{"path": "lib.rs", "operations": []}]}
    with pytest.raises(OverlayError, match="only Cargo manifests"):
        overlay_files(source, spec)
    assert (source / "lib.rs").read_text() == "production"


def test_session_data_requires_exact_matching_hashes_for_both_variants(tmp_path):
    entry = {"path": "test.rs", "content": "unchanged assertion\n"}
    evidence = [{"path": "test.rs", "sha256": digest(entry["content"].encode())}]
    oracle = {"session_files": {"start": evidence, "golden": evidence}}
    assert session_bytes(SimpleNamespace(legacy=tmp_path), {}, oracle, entry) == entry["content"].encode()
    oracle["session_files"]["golden"] = []
    with pytest.raises(ContractError, match="frozen start/golden"):
        session_bytes(SimpleNamespace(legacy=tmp_path), {}, oracle, entry)


def test_rustup_home_relocates_to_the_exact_readonly_compiler_not_a_shared_host_home(tmp_path):
    legacy = tmp_path / "legacy"
    ctx = SimpleNamespace(legacy=legacy)
    plan = {"toolchain": "1.78.0", "env": {"RUSTUP_HOME": str(legacy / "rust/cache/rustup-task"),
                                         "RUSTC_BOOTSTRAP": "1"}}
    env, commands = portable_environment(ctx, plan, {"RUSTC": "/opt/reviewbench-assets/rust/bin/rustc"})
    assert env["RUSTUP_HOME"] == "/workspace/.reviewbench/rustup"
    assert env["RUSTC_BOOTSTRAP"] == "1"
    assert any("1.78.0-x86_64-unknown-linux-gnu" in value for value in commands)
    assert any("ln -s /opt/reviewbench-assets/rust " in value for value in commands)
    plan["env"]["RUSTUP_HOME"] = "/some/unbound/home"
    with pytest.raises(ContractError, match="toolchain binding"):
        portable_environment(ctx, plan, {})


def test_unlocked_cargo_overlay_normalization_only_changes_local_edges_or_prunes_unused_packages():
    first = {"name": "dep", "version": "1.0.0", "source": "registry+index", "checksum": "a" * 64}
    second = {"name": "git-dep", "version": "2.0.0", "source": "git+https://example.test/repo#" + "b" * 40}
    before = {"package": [first, second, {"name": "local", "version": "1.0", "dependencies": ["dep"]}]}
    after = {"package": [first, {"name": "local", "version": "1.0", "dependencies": ["dep", "new-local"]},
                         {"name": "new-local", "version": "1.0"}]}
    result = validate_lock_normalization(before, after)
    assert result["external_pruned"] == 1 and result["external_added_or_changed"] == 0
    for changed in [dict(first, version="1.1.0"), dict(first, checksum="c" * 64),
                    dict(second, source="git+https://example.test/repo#" + "c" * 40)]:
        with pytest.raises(ContractError, match="external dependency"):
            validate_lock_normalization(before, {"package": [changed]})


def test_old_nightly_cargo_config_capability_is_probed_without_compiler_substitution(tmp_path):
    tool = tmp_path / "tool/bin"
    tool.mkdir(parents=True)
    cargo = tool / "cargo"
    cargo.write_text("#!/usr/bin/env python3\nimport sys\n"
                     "if '-Z' not in sys.argv:\n"
                     " print('error: the `--config` flag is unstable',file=sys.stderr);sys.exit(101)\n"
                     "assert sys.argv[sys.argv.index('-Z')+1]=='unstable-options'\n"
                     "print('{\"root\":\"Cargo.toml\"}')\n")
    cargo.chmod(0o755)
    registry = tmp_path / "toolchains.json"
    registry.write_text(json.dumps({"nightly-2021-11-24": {"root": str(tool.parent)}}))
    ctx = SimpleNamespace(config={"rust_toolchains": str(registry)}, output=tmp_path / "out", scratch=tmp_path)
    assert cargo_cli_prefix(ctx, "nightly-2021-11-24") == ["-Z", "unstable-options"]
    evidence = json.loads((ctx.output / "rust-cli-probes/nightly-2021-11-24.json").read_text())
    assert [probe["exit_status"] for probe in evidence["probes"]] == [101, 0]
    assert all(probe["command"][0] == str(cargo) for probe in evidence["probes"])


def test_build_spec_binds_intermediate_manifest_hashes_and_accepted_reference_patch(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repository), *args]).decode().strip()
    git("init", "-q")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    before = '[package]\nname = "app"\nversion = "1.0.0"\n[workspace]\nmembers = ["app", "other"]\n'
    (repository / "Cargo.toml").write_text(before)
    (repository / "lib.rs").write_text("pub fn value() -> u8 { 1 }\n")
    git("add", "."); git("commit", "-qm", "start")
    start = git("rev-parse", "HEAD")
    (repository / "lib.rs").write_text("pub fn value() -> u8 { 2 }\n")
    patch = tmp_path / "reference.patch"
    patch.write_text(git("diff") + "\n")
    old, new = 'members = ["app", "other"]', 'members = ["app"]'
    after = before.replace(old, new)
    evidence = [{"path": "Cargo.toml", "sha256": digest(after.encode())}]
    oracle = {"environment_replacements": {"start": evidence, "golden": evidence}}
    output = tmp_path / "output"; output.mkdir()
    ctx = SimpleNamespace(repo=lambda task: repository, output=output, legacy=tmp_path, scratch=tmp_path)
    plan = {"environment_replacements": [{"path": "Cargo.toml", "old": old, "new": new}]}
    spec, defaults = build_specification(ctx, {"id": "task", "start_sha": start},
                                        plan, oracle, {"path": str(patch)})
    assert not defaults and len(spec["manifests"]) == 1
    assert apply_manifest_operations(tomllib.loads(before), spec["manifests"][0]["operations"]) == tomllib.loads(after)
    oracle["environment_replacements"]["golden"] = [{"path": "Cargo.toml", "sha256": "0" * 64}]
    with pytest.raises(ContractError, match="intermediate evidence"):
        build_specification(ctx, {"id": "task", "start_sha": start}, plan, oracle, {"path": str(patch)})
