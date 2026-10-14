import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import tomllib

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_retained_resolution as retained
from agentic_runtime_migration.rust_build_entry import manifest_operations, overlay_files, OverlayError
from reviewbench_maintainability.agentic.contracts import canonical_hash, ContractError


def sha(data):
    return hashlib.sha256(data).hexdigest()


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE)


@pytest.fixture(params=[False, True])
def fixture(tmp_path, monkeypatch, request):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "--quiet")
    git(repo, "config", "user.email", "fixture@example.invalid")
    git(repo, "config", "user.name", "fixture")
    original_toml = '[workspace]\nmembers=["member", "unused"]\n'
    prepared_toml = '[workspace]\nmembers=["member"]\n'
    (repo / "Cargo.toml").write_text(original_toml)
    for name in ("member", "unused"):
        (repo / name / "src").mkdir(parents=True)
        (repo / name / "Cargo.toml").write_text(
            '[package]\nname="' + name + '"\nversion="0.0.0"\nedition="2021"\n')
        (repo / name / "src/lib.rs").write_text("pub fn value() -> u32 { 42 }\n")
    lock = b'version = 3\n\n[[package]]\nname = "member"\nversion = "0.0.0"\n'
    before_lock = lock + b"# original complete-workspace lock\n" if request.param else None
    if before_lock is not None:
        (repo / "Cargo.lock").write_bytes(before_lock)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "public fixture")
    start = git(repo, "rev-parse", "HEAD").decode().strip()
    legacy = tmp_path / "legacy"
    artifact = legacy / "rust/artifacts/fixture"
    artifact.mkdir(parents=True)
    patch = artifact / "source-only-golden.patch"
    source = repo / "member/src/lib.rs"
    original_source = source.read_bytes()
    source.write_text("pub fn value() -> u32 { 43 }\n")
    patch.write_bytes(git(repo, "diff", "--binary"))
    source.write_bytes(original_source)
    task = {"id": "fixture", "repo": "fixture/project", "start_sha": start}
    replacements = [{"path": "Cargo.toml", "old": original_toml, "new": prepared_toml, "count": 1}]
    plan = {"input_index": 7, "environment_replacements": replacements, "session_files": []}
    oracle = {**task, "input_index": 7, "golden_patch_sha256": sha(patch.read_bytes()), "pass_to_pass_tests": ["works"],
              "environment_replacements": {}, "runs": {}}
    logs = {}
    for variant in ("start", "golden"):
        snapshot = artifact / variant
        subprocess.run(["git", "clone", "--quiet", "--shared", str(repo), str(snapshot)], check=True)
        if variant == "golden":
            git(snapshot, "apply", str(patch))
        (snapshot / "Cargo.toml").write_text(prepared_toml)
        (snapshot / "Cargo.lock").write_bytes(lock)
        oracle["environment_replacements"][variant] = [
            {"path": "Cargo.toml", "sha256": sha(prepared_toml.encode())}]
        oracle["runs"][variant] = []
        for index in range(3):
            name = f"artifacts/fixture/logs/{variant}-{index}.log"
            path = legacy / "rust" / name
            path.parent.mkdir(exist_ok=True)
            data = f"test works ... ok\nrecorded {variant} {index}\n".encode()
            path.write_bytes(data)
            logs[name] = sha(data)
            oracle["runs"][variant].append({
                "log": name, "ok": True, "failed": 0, "passed_tests": ["works"], "failed_tests": [],
                "attempts": [{"exit_code": 0, "timed_out": False, "log": name}],
            })
    original = artifact / "result.json"
    original.write_text(json.dumps(oracle))
    pin = {**{key: task[key] for key in ("repo", "start_sha")}, "input_index": 7, "artifacts": "artifacts/fixture",
           "plan_sha256": canonical_hash(plan), "oracle_file_sha256": sha(original.read_bytes()),
           "patch_sha256": sha(patch.read_bytes()), "lock_sha256": sha(lock),
           "reference_lock_sha256": sha(before_lock) if before_lock is not None else None, "logs": logs}
    monkeypatch.setattr(retained, "BINDINGS", {"fixture": pin})
    output = tmp_path / "output"
    output.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    def copy_asset(path, role):
        destination = output / "copied-Cargo.lock"
        shutil.copyfile(path, destination)
        return {"path": str(destination), "sha256": sha(destination.read_bytes())}
    ctx = SimpleNamespace(legacy=legacy, output=output, scratch=scratch, repo=lambda _: repo, copy_asset=copy_asset)
    spec = {"manifests": [{"path": "Cargo.toml", "operations":
                           manifest_operations(tomllib.loads(original_toml), tomllib.loads(prepared_toml))}],
            "locks": [], "private_members": []}
    defaults = {}
    patch_record = {"path": str(patch), "sha256": sha(patch.read_bytes())}
    return SimpleNamespace(**locals())


def test_retained_lock_is_bound_but_never_relabels_it_as_an_original_oracle_digest(fixture):
    f = fixture
    retained.add_retained_resolution(f.ctx, f.task, f.plan, f.oracle, f.patch_record, f.spec, f.defaults)
    assert f.defaults["Cargo.lock"]["sha256"] == sha(f.lock)
    assert f.spec["retained_lock_resolution"]["original_oracle_declares_lock_sha256"] is False
    sources = {f.spec["locks"][0]["asset_target"]: f.defaults["Cargo.lock"]["path"]}
    assert overlay_files(f.repo, f.spec, sources)["Cargo.lock"] == f.lock
    candidate_lock = f.lock + b"# candidate-owned independent graph\n"
    (f.repo / "Cargo.lock").write_bytes(candidate_lock)
    assert "Cargo.lock" not in overlay_files(f.repo, f.spec, sources)
    assert (f.repo / "Cargo.lock").read_bytes() == candidate_lock


@pytest.mark.parametrize("mutation", ["manifest", "extra-manifest", "lock", "log", "plan", "patch",
                                    "reference-lock", "overlay", "snapshot-link"])
def test_changed_incomplete_or_redirected_retained_inputs_are_rejected(fixture, mutation):
    f = fixture
    if mutation == "manifest":
        (f.artifact / "golden/member/Cargo.toml").write_text('[package]\nname="other"\n')
    elif mutation == "extra-manifest":
        path = f.artifact / "start/extra/Cargo.toml"
        path.parent.mkdir()
        path.write_text('[package]\nname="extra"\n')
    elif mutation == "lock":
        (f.artifact / "golden/Cargo.lock").write_bytes(f.lock + b"# changed")
    elif mutation == "log":
        (f.legacy / "rust" / next(iter(f.logs))).write_text("different run")
    elif mutation == "plan":
        f.plan["prepare_command"] = "cargo update"
    elif mutation == "patch":
        f.patch.write_text("changed patch")
    elif mutation == "reference-lock":
        f.pin["reference_lock_sha256"] = "f" * 64
    elif mutation == "overlay":
        f.spec["manifests"][0]["operations"] = []
    else:
        source = f.artifact / "start"
        target = f.tmp_path / "redirected"
        source.rename(target)
        source.symlink_to(target, target_is_directory=True)
    with pytest.raises((ContractError, KeyError)):
        retained.add_retained_resolution(f.ctx, f.task, f.plan, f.oracle, f.patch_record, f.spec, f.defaults)
    assert not f.defaults and not f.spec["locks"]


def test_nightly_cargo_config_removal_preserves_unrelated_candidate_build_settings(tmp_path):
    root = tmp_path / "candidate"
    (root / ".cargo").mkdir(parents=True)
    original = {"include": "../embedded.toml", "unstable": {"features": ["all"], "config-include": True}}
    candidate = {**original, "build": {"rustflags": ["--cfg", "candidate_added"]}}
    (root / ".cargo/config.toml").write_text(
        'include="../embedded.toml"\n[unstable]\nfeatures=["all"]\nconfig-include=true\n'
        '[build]\nrustflags=["--cfg","candidate_added"]\n')
    before = (root / ".cargo/config.toml").read_bytes()
    spec = {"manifests": [{"path": ".cargo/config.toml", "operations": manifest_operations(original, {})}]}
    result = overlay_files(root, spec)
    assert tomllib.loads(result[".cargo/config.toml"].decode()) == {"build": candidate["build"]}
    assert (root / ".cargo/config.toml").read_bytes() == before
    spec["manifests"][0]["operations"] = [{"kind": "remove", "key": ["build"], "old": candidate["build"]}]
    with pytest.raises(OverlayError, match="nightly"):
        overlay_files(root, spec)
