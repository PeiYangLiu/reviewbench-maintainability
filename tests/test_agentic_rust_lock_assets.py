"""Retained lock recovery must not fabricate evidence or narrow a candidate tree."""
import copy
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_lock_assets as recovery, rust_lock_entry as entry
from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash


def sha(data):
    return hashlib.sha256(data).hexdigest()


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE)


def files(root):
    return {name: path.read_bytes() for name, path in entry.candidate_files(root).items()
            if not path.is_symlink()}


def fixture(tmp_path, monkeypatch, *, session=False):
    source = tmp_path / "candidate"
    source.mkdir()
    contents = {
        "Cargo.toml": b'[workspace]\nmembers=["app","peer"]\nresolver="2"\n',
        ".gitignore": b"Cargo.lock\ntarget/\n",
        "app/Cargo.toml": b'[package]\nname="app"\nversion="0.1.0"\nedition="2021"\n',
        "peer/Cargo.toml": b'[package]\nname="peer"\nversion="0.1.0"\nedition="2021"\n',
        "app/src/lib.rs": b"pub fn value()->u8 { 1 }\n",
        "peer/src/lib.rs": b"pub fn other()->u8 { 2 }\n",
        "app/tests/fixed.rs": b"#[test]\nfn original_identity(){assert_eq!(app::value(),1);}\n",
    }
    for name, data in contents.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    git(source, "init", "-q")
    git(source, "config", "user.name", "Fixture")
    git(source, "config", "user.email", "fixture@example.invalid")
    git(source, "add", ".")
    git(source, "commit", "-qm", "frozen input")
    start = git(source, "rev-parse", "HEAD").decode().strip()
    (source / "app/src/lib.rs").write_bytes(contents["app/src/lib.rs"] + b"// refactored\n")
    patch_file = tmp_path / "accepted.patch"
    patch_file.write_bytes(git(source, "diff", "--binary"))
    (source / "app/src/lib.rs").write_bytes(contents["app/src/lib.rs"])
    patch = {"path": str(patch_file), "sha256": sha(patch_file.read_bytes())}
    task = {"id": "fixture", "repo": "fixture/repo", "start_sha": start}
    plan = {"input_index": 1, "toolchain": "fixture", "command": "cargo test -p app --test fixed",
            "workdir": "."}
    lock = (b'version = 3\n\n[[package]]\nname="app"\nversion="0.1.0"\n\n'
            b'[[package]]\nname="peer"\nversion="0.1.0"\n')
    legacy = tmp_path / "legacy"
    artifacts = "artifacts/001-fixture"
    root = legacy / "rust"
    directory = root / artifacts
    directory.mkdir(parents=True)
    oracle = {**task, "input_index": 1, "golden_patch_sha256": patch["sha256"],
              "pass_to_pass_tests": ["original_identity"], "runs": {},
              "session_files": {"start": [], "golden": []}}
    log_pins = {}
    for variant in ("start", "golden"):
        snapshot = directory / variant
        git(tmp_path, "clone", "-q", "--shared", str(source), str(snapshot))
        if variant == "golden":
            git(snapshot, "apply", str(patch_file))
        (snapshot / "Cargo.lock").write_bytes(lock)
        oracle["runs"][variant] = []
        for n in range(1, 4):
            log = f"{artifacts}/logs/{variant}-run{n}-attempt1.log"
            path = root / log
            path.parent.mkdir(exist_ok=True)
            path.write_text("test original_identity ... ok\n")
            log_pins[path.name] = sha(path.read_bytes())
            oracle["runs"][variant].append({
                "log": log, "ok": True, "failed": 0, "passed_tests": ["original_identity"],
                "attempts": [{"exit_code": 0, "timed_out": False}],
            })
    pin = {**task, "input_index": 1, "artifacts": artifacts, "lock_path": "Cargo.lock",
           "lock_sha256": sha(lock), "retained": True, "logs": log_pins}
    if session:
        path = root / "session/Cargo.lock"
        path.parent.mkdir()
        path.write_bytes(lock)
        plan["session_files"] = [{"path": "Cargo.lock", "source": "session/Cargo.lock"}]
        evidence = [{"path": "Cargo.lock", "sha256": sha(lock)}]
        oracle["session_files"] = {"start": evidence, "golden": copy.deepcopy(evidence)}
        pin["source"] = "session/Cargo.lock"
    original = directory / "result.json"
    original.write_text(json.dumps(oracle, sort_keys=True))
    pin.update(plan_sha256=canonical_hash(plan), oracle_file_sha256=sha(original.read_bytes()))
    monkeypatch.setattr(recovery, "BINDINGS", {task["id"]: pin})
    output = tmp_path / "output"
    def copy_asset(path, category):
        target = output / "files" / category / (sha(path.read_bytes()) + "-" + path.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        return {"path": str(target), "sha256": sha(target.read_bytes())}
    ctx = SimpleNamespace(legacy=legacy, output=output, scratch=tmp_path,
                          repo=lambda _: source, copy_asset=copy_asset)
    return ctx, task, plan, oracle, patch, pin, source


def bind(f):
    return recovery.recover(*f[:5])


def lock_sources(spec, assets):
    return {r["asset_target"]: assets[r["path"]]["path"] for r in spec["locks"]}


@pytest.mark.parametrize("session", [False, True])
def test_binds_only_actual_lock_bytes_and_full_reference_manifests(tmp_path, monkeypatch, session):
    f = fixture(tmp_path, monkeypatch, session=session)
    before = files(f[-1])
    spec, assets = bind(f)
    record = spec["locks"][0]
    assert record["sha256"] == f[-2]["lock_sha256"]
    assert record["allow_missing"] is True and record["replace_if_sha256"] == []
    assert spec["manifests"] == [] and spec["private_members"] == []
    assert files(f[-1]) == before and not (f[-1] / "Cargo.lock").exists()
    assert Path(assets["Cargo.lock"]["path"]).read_bytes() == (
        f[0].legacy / "rust" / f[-2]["artifacts"] / "start/Cargo.lock").read_bytes()
    audit = json.loads(next((f[0].output / "rust-lock-recovery").glob("*.json")).read_text())
    assert audit["status"] == "bound" and audit["scoring_result"] is False
    assert audit["oracle_declares_lock_sha256"] is session
    assert set(audit["reference_manifests"]["start"]) == {"Cargo.toml", "app/Cargo.toml", "peer/Cargo.toml"}
    assert audit["production_changes"] == audit["test_changes"] == []
    assert "pending" in audit["lock_compatibility"]


@pytest.mark.parametrize("change", [
    "task", "repo", "start", "index", "plan", "oracle", "oracle-file", "patch",
    "start-lock", "golden-lock", "nested-manifest", "extra-manifest", "log", "session-lock",
])
def test_rejects_changed_or_wrong_frozen_inputs_before_copying_assets(tmp_path, monkeypatch, change):
    f = fixture(tmp_path, monkeypatch, session=change == "session-lock")
    ctx, task, plan, oracle, patch, pin, source = f
    directory = ctx.legacy / "rust" / pin["artifacts"]
    if change == "task":
        oracle["id"] = "another-task"
    elif change == "repo":
        task["repo"] = "another/repo"
    elif change == "start":
        task["start_sha"] = "0" * 40
    elif change == "index":
        oracle["input_index"] = 2
    elif change == "plan":
        plan["command"] += " --lib"
    elif change == "oracle":
        oracle["pass_to_pass_tests"] = ["a_different_test"]
    else:
        path = {
            "oracle-file": directory / "result.json",
            "patch": Path(patch["path"]),
            "start-lock": directory / "start/Cargo.lock",
            "golden-lock": directory / "golden/Cargo.lock",
            "nested-manifest": directory / "golden/peer/Cargo.toml",
            "extra-manifest": directory / "golden/extra/Cargo.toml",
            "log": directory / "logs/start-run1-attempt1.log",
            "session-lock": ctx.legacy / "rust/session/Cargo.lock",
        }[change]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# changed input\n")
    with pytest.raises(ContractError):
        bind(f)
    assert not (ctx.output / "files").exists()
    audit = json.loads(next((ctx.output / "rust-lock-recovery").glob("*.json")).read_text())
    assert audit["status"] == "rejected" and audit["gap"]
    assert not (source / "Cargo.lock").exists()


@pytest.mark.parametrize("change", ["missing", "symlink", "parent-symlink"])
def test_recovery_requires_regular_retained_evidence(tmp_path, monkeypatch, change):
    f = fixture(tmp_path, monkeypatch)
    path = f[0].legacy / "rust" / f[-2]["artifacts"] / "start/Cargo.lock"
    outside = tmp_path / "outside.lock"
    shutil.copyfile(path, outside)
    if change == "missing":
        path.unlink()
    elif change == "symlink":
        path.unlink()
        path.symlink_to(outside)
    else:
        parent = path.parent
        moved = parent.with_name("moved")
        parent.rename(moved)
        parent.symlink_to(moved, target_is_directory=True)
    with pytest.raises(ContractError, match="absent/not a regular"):
        bind(f)


def test_unpinned_snapshot_records_gap_instead_of_inventing_a_digest(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    f[-2].pop("lock_sha256")
    with pytest.raises(ContractError, match="no original lock digest or pinned retained snapshot"):
        bind(f)
    audit = json.loads(next((f[0].output / "rust-lock-recovery").glob("*.json")).read_text())
    assert len(audit["retained_paths"]) == 2
    assert all(p["exists"] for p in audit["retained_paths"])
    assert not (f[0].output / "files").exists()


def test_unknown_task_cannot_inherit_another_tasks_recovery():
    assert recovery.recover(None, {"id": "not-a-bound-Rust-input"}, {}, {}, {}) is None


def test_rejects_a_changed_copied_asset(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    original = f[0].copy_asset
    f[0].copy_asset = lambda *a: {**original(*a), "sha256": "0" * 64}
    with pytest.raises(ContractError, match="copied Rust"):
        bind(f)


def test_reference_tree_includes_manifest_additions_and_deletions(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    ctx, task, _, _, patch, _, source = f
    (source / "peer/Cargo.toml").unlink()
    (source / "new").mkdir()
    (source / "new/Cargo.toml").write_text('[package]\nname="new"\nversion="0.1.0"\n')
    git(source, "add", "-N", "new/Cargo.toml")
    Path(patch["path"]).write_bytes(git(source, "diff", "--binary"))
    before_index = (source / ".git/index").read_bytes()
    trees = recovery.reference_trees(ctx, task, patch, {"Cargo.lock"})
    assert "peer/Cargo.toml" in trees["start"][0] and "peer/Cargo.toml" not in trees["golden"][0]
    assert "new/Cargo.toml" not in trees["start"][0] and "new/Cargo.toml" in trees["golden"][0]
    assert (source / ".git/index").read_bytes() == before_index


@pytest.fixture
def cargo(tmp_path, monkeypatch):
    executable = os.environ.get("REVIEWBENCH_TEST_CARGO") or shutil.which("cargo")
    if not executable:
        pytest.skip("preinstalled Cargo is required for the small offline fixture")
    result = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=30,
                            env={**os.environ, "RUSTUP_AUTO_INSTALL": "0"})
    if result.returncode:
        pytest.skip("no installed Cargo (automatic installation is disabled)")
    monkeypatch.setenv("RUSTUP_AUTO_INSTALL", "0")
    monkeypatch.setenv("CARGO_HOME", str(tmp_path / "isolated-cargo-home"))
    monkeypatch.setenv("CARGO_TARGET_DIR", str(tmp_path / "cargo-target"))
    monkeypatch.setenv("CARGO_NET_OFFLINE", "true")
    compiler = Path(executable).parent / "rustc"
    if compiler.is_file():
        monkeypatch.setenv("RUSTC", str(compiler))
    return executable


@contextmanager
def readonly(root):
    paths = [root, *root.rglob("*")]
    modes = {p: p.stat().st_mode & 0o777 for p in paths if not p.is_symlink()}
    try:
        for p in modes:
            p.chmod(0o555 if p.is_dir() else 0o444)
        yield
    finally:
        for p, mode in modes.items():
            p.chmod(mode)


def test_offline_compatibility_uses_all_members_and_keeps_readonly_candidate_intact(tmp_path, monkeypatch, cargo):
    f = fixture(tmp_path, monkeypatch)
    spec, assets = bind(f)
    source = f[-1]
    (source / "app/src/new_helper.rs").write_text("pub fn helper() {}\n")
    # Compatible manifest edits are checked by Cargo, not coerced to a historical
    # manifest or rejected merely because they have a new byte hash.
    with (source / "peer/Cargo.toml").open("a") as stream:
        stream.write('[features]\nrefactored=[]\n')
    before = files(source)
    mirror = tmp_path / "mirror"
    with readonly(source), readonly(f[0].output / "files"):
        command, report = entry.prepare_candidate(source, mirror, spec, "fixture", ".",
                                                   [cargo, "check", "-p", "app"],
                                                   lock_sources=lock_sources(spec, assets))
    assert report["metadata_exit_status"] == 0 and len(report["workspace_members"]) == 2
    assert report["reference_manifest_match"] is False
    assert "--no-deps" not in report["metadata_command"] and "-p" not in report["metadata_command"]
    assert "--locked" in command and "--offline" in command
    assert report["locks_before"] == report["locks_after"]
    assert report["injected_defaults"] == ["Cargo.lock"]
    assert files(source) == before and not (source / "Cargo.lock").exists()
    assert all((mirror / name).read_bytes() == data for name, data in before.items())


def test_unselected_members_dev_dependency_must_match_the_retained_lock(tmp_path, monkeypatch, cargo):
    f = fixture(tmp_path, monkeypatch)
    spec, assets = bind(f)
    source = f[-1]
    with (source / "peer/Cargo.toml").open("a") as stream:
        stream.write('[dev-dependencies]\nnew_helper={path="../new-helper"}\n')
    (source / "new-helper/src").mkdir(parents=True)
    (source / "new-helper/Cargo.toml").write_text('[package]\nname="new_helper"\nversion="0.1.0"\n')
    (source / "new-helper/src/lib.rs").write_text("pub fn retained() {}\n")
    before = files(source)
    with pytest.raises(entry.OverlayError, match="incompatible with the full candidate manifest tree"):
        entry.prepare_candidate(source, tmp_path / "mirror", spec, "fixture", ".",
                                [cargo, "check", "-p", "app"], lock_sources=lock_sources(spec, assets))
    assert files(source) == before
    assert files(tmp_path / "mirror")["peer/Cargo.toml"] == before["peer/Cargo.toml"]


def test_candidate_owned_lock_is_preserved_and_checked_not_overwritten(tmp_path, monkeypatch, cargo):
    f = fixture(tmp_path, monkeypatch)
    spec, assets = bind(f)
    source = f[-1]
    own = Path(assets["Cargo.lock"]["path"]).read_bytes() + b"\n# candidate-owned lock\n"
    (source / "Cargo.lock").write_bytes(own)
    _, report = entry.prepare_candidate(source, tmp_path / "mirror", spec, "fixture", ".",
                                        [cargo, "check"], lock_sources=lock_sources(spec, assets))
    assert report["injected_defaults"] == []
    assert (source / "Cargo.lock").read_bytes() == (tmp_path / "mirror/Cargo.lock").read_bytes() == own


@pytest.mark.parametrize("change", ["task", "asset", "overlay", "cwd", "duplicate", "manifest-symlink", "lock-symlink"])
def test_runtime_binding_rejects_wrong_or_changed_inputs_before_cargo(tmp_path, monkeypatch, change):
    f = fixture(tmp_path, monkeypatch)
    spec, assets = bind(f)
    source = f[-1]
    task_id, cwd = "fixture", "."
    if change == "task":
        task_id = "wrong"
    elif change == "asset":
        Path(assets["Cargo.lock"]["path"]).write_text("changed asset")
    elif change == "overlay":
        spec["manifests"] = [{"path": "Cargo.toml", "operations": []}]
    elif change == "cwd":
        cwd = "peer"
    elif change == "duplicate":
        spec["locks"].append(copy.deepcopy(spec["locks"][0]))
    elif change == "manifest-symlink":
        (source / "peer/Cargo.toml").unlink()
        (source / "peer/Cargo.toml").symlink_to("../app/Cargo.toml")
    else:
        (source / "Cargo.lock").symlink_to(assets["Cargo.lock"]["path"])
    monkeypatch.setattr(entry.subprocess, "run", lambda *a, **k: pytest.fail("Cargo ran before input validation"))
    monkeypatch.setattr(entry.subprocess, "Popen", lambda *a, **k: pytest.fail("Cargo ran before input validation"))
    with pytest.raises(entry.OverlayError):
        entry.prepare_candidate(source, tmp_path / "mirror", spec, task_id, cwd, ["cargo", "test"],
                                lock_sources=lock_sources(spec, assets))


def test_locked_command_preserves_fixed_test_identities_and_libtest_arguments():
    command = ["cargo", "--config", "sources.toml", "test", "-p", "app", "--features", "f",
               "--test", "fixed", "--", "original_identity", "--exact", "--nocapture"]
    metadata, execute = entry.locked_commands(command)
    assert execute[execute.index("--"):] == command[command.index("--"):]
    assert execute[:execute.index("--")] == command[:command.index("--")] + ["--locked", "--offline"]
    assert metadata == ["cargo", "--config", "sources.toml", "metadata", "--format-version", "1",
                        "--locked", "--offline", "--features", "f"]


def test_metadata_success_does_not_authorize_a_lock_mutation(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    spec, assets = bind(f)
    cargo = tmp_path / "tool/cargo"
    cargo.parent.mkdir()
    cargo.write_text(
        "#!" + sys.executable + "\n"
        "import pathlib\n"
        "pathlib.Path('Cargo.lock').write_text('changed despite --locked')\n"
        "print('{}')\n"
    )
    cargo.chmod(0o755)
    with pytest.raises(entry.OverlayError, match="metadata changed a frozen lock"):
        entry.prepare_candidate(f[-1], tmp_path / "mirror", spec, "fixture", ".", [str(cargo), "check"],
                                lock_sources=lock_sources(spec, assets))
    assert not (f[-1] / "Cargo.lock").exists()


def test_metadata_compatibility_has_a_finite_timeout(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    spec, assets = bind(f)
    observed = []
    class Process:
        pid = 12345
        def wait(self, timeout=None):
            if timeout is not None:
                assert timeout == 120
                raise subprocess.TimeoutExpired("cargo metadata", timeout)
            observed.append("reaped")
    def start(command, **kwargs):
        assert kwargs["start_new_session"] is True
        return Process()
    monkeypatch.setattr(entry.subprocess, "Popen", start)
    monkeypatch.setattr(entry.os, "killpg", lambda pid, sig: observed.append((pid, sig)))
    with pytest.raises(entry.OverlayError, match="exceeded 120 seconds"):
        entry.prepare_candidate(f[-1], tmp_path / "mirror", spec, "fixture", ".", ["cargo", "check"],
                                lock_sources=lock_sources(spec, assets))
    assert observed == [(12345, entry.signal.SIGKILL), "reaped"]


def test_oversized_candidate_manifest_is_a_bounded_input_error(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    spec, assets = bind(f)
    monkeypatch.setattr(entry, "MAX_INPUT_BYTES", 512)
    (f[-1] / "peer/Cargo.toml").write_text("#" * 513)
    with pytest.raises(entry.OverlayError, match="exceeds its input bound"):
        entry.prepare_candidate(f[-1], tmp_path / "mirror", spec, "fixture", ".", ["cargo", "check"],
                                lock_sources=lock_sources(spec, assets))


def test_standalone_packaged_entry_runs_small_offline_check_without_writing_candidate(tmp_path, monkeypatch, cargo):
    f = fixture(tmp_path, monkeypatch)
    spec, assets = bind(f)
    for record in spec["locks"]:
        record["asset_target"] = assets[record["path"]]["path"]
    tools = tmp_path / "tools"
    tools.mkdir()
    shutil.copyfile(Path(entry.__file__).with_name("rust_build_entry.py"), tools / "build.py")
    shutil.copyfile(entry.__file__, tools / "locks.py")
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(spec))
    before = files(f[-1])
    command = [sys.executable, str(tools / "locks.py"), "--source", str(f[-1]),
               "--build-root", str(tmp_path / "build"), "--spec", str(spec_file),
               "--task-id", "fixture", "--", cargo, "check", "-p", "app"]
    with readonly(f[-1]):
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "locked offline full-candidate resolution" in result.stdout
    assert files(f[-1]) == before
    report = json.loads((tmp_path / "build/lock-compatibility.json").read_text())
    assert report["metadata_exit_status"] == 0 and report["scoring_result"] is False
    # A later rejected candidate must not leave an old successful compatibility receipt.
    with (f[-1] / "peer/Cargo.toml").open("a") as stream:
        stream.write('[dependencies]\nmissing_dep="=99.0.0"\n')
    rejected = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert rejected.returncode == 2
    report = json.loads((tmp_path / "build/lock-compatibility.json").read_text())
    assert report["status"] == "rejected" and "incompatible" in report["gap"]


def test_separate_existing_public_root_and_frozen_session_keep_all_workspace_members(tmp_path, monkeypatch, cargo):
    f = fixture(tmp_path, monkeypatch)
    spec, _ = bind(f)
    source = f[-1]
    (source / "Cargo.toml").write_text('[workspace]\nmembers=["app","peer"]\nexclude=["cli"]\n')
    (source / "cli/src").mkdir(parents=True)
    (source / "cli/Cargo.toml").write_text(
        '[package]\nname="cli"\nversion="0.1.0"\n[dependencies]\napp={path="../app"}\n')
    (source / "cli/src/main.rs").write_text("fn main(){let _=app::value();}\n")
    cli_lock = (b'version=3\n[[package]]\nname="app"\nversion="0.1.0"\n'
                b'[[package]]\nname="cli"\nversion="0.1.0"\ndependencies=["app"]\n')
    private_lock = cli_lock.replace(b'name="cli"', b'name="oracle"')
    (source / "cli/Cargo.lock").write_bytes(cli_lock)
    spec["locks"] = []
    for n, data, missing in [("cli/Cargo.lock", cli_lock, False), ("tests/oracle/Cargo.lock", private_lock, True)]:
        asset = tmp_path / (sha(data) + ".lock")
        asset.write_bytes(data)
        spec["locks"].append({"path": n, "asset_target": str(asset), "sha256": sha(data),
                              "allow_missing": missing, "replace_if_sha256": []})
    spec["frozen_lock_recovery"]["entrypoints"] = {
        "cli": "cli/Cargo.lock", "tests/oracle": "tests/oracle/Cargo.lock"}
    before = files(source)
    mirror = tmp_path / "mirror"
    execute, report = entry.prepare_candidate(source, mirror, spec, "fixture", "cli",
                                               [cargo, "check", "-p", "app"])
    result = subprocess.run(execute, cwd=mirror / "cli", capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert files(source) == before
    assert (mirror / "Cargo.toml").read_bytes() == before["Cargo.toml"]
    assert (mirror / "peer/src/lib.rs").read_bytes() == before["peer/src/lib.rs"]
    (source / "tests/oracle/tests").mkdir(parents=True)
    (source / "tests/oracle/Cargo.toml").write_text(
        '[package]\nname="oracle"\nversion="0.1.0"\n[workspace]\n[dependencies]\napp={path="../../app"}\n')
    assertion = b"#[test]\nfn fixed_identity(){assert_eq!(app::value(),1);}\n"
    (source / "tests/oracle/tests/fixed.rs").write_bytes(assertion)
    _, report = entry.prepare_candidate(source, mirror, spec, "fixture", "tests/oracle",
                                        [cargo, "test", "--locked", "--test", "fixed"])
    assert report["metadata_exit_status"] == 0
    assert (mirror / "tests/oracle/tests/fixed.rs").read_bytes() == assertion
    assert (mirror / "Cargo.toml").read_bytes() == before["Cargo.toml"]


def test_recovered_vendor_uses_private_and_public_lock_union_without_resolution(tmp_path, monkeypatch):
    from agentic_runtime_migration import rust_lane
    assets, records = {}, []
    for n, package in enumerate(("private_dep", "public_dep")):
        archive = tmp_path / (package + ".crate")
        with tarfile.open(archive, "w:gz") as stream:
            for name, data in [("Cargo.toml", f'[package]\nname="{package}"\nversion="1.0.0"\n'.encode()),
                               ("src/lib.rs", b"pub fn unchanged() {}\n")]:
                info = tarfile.TarInfo(f"{package}-1.0.0/{name}")
                info.size = len(data)
                stream.addfile(info, io.BytesIO(data))
        lock = tmp_path / f"{package}.lock"
        lock.write_text('version=3\n[[package]]\nname="' + package + '"\nversion="1.0.0"\n'
                        'source="registry+https://github.com/rust-lang/crates.io-index"\nchecksum="' +
                        sha(archive.read_bytes()) + '"\n')
        name = "private/Cargo.lock" if n == 0 else "public/Cargo.lock"
        assets[name] = {"path": str(lock), "sha256": sha(lock.read_bytes())}
        records.append({"path": name, "sha256": sha(lock.read_bytes())})
    spec = {"locks": records, "frozen_lock_recovery": {"task_id": "fixture"}}
    monkeypatch.setattr(rust_lane, "crate_archive", lambda ctx, name, version, checksum: tmp_path / (name + ".crate"))
    monkeypatch.setattr(rust_lane, "git", lambda *a: pytest.fail("bound locks should be used"))
    observed = {}
    def bundle(root):
        observed["packages"] = sorted(p.name for p in root.iterdir())
        return {"path": "fixture-bundle", "sha256": "a" * 64}
    ctx = SimpleNamespace(scratch=tmp_path, output=tmp_path / "out", bundle=bundle)
    rust_lane.vendor(ctx, {"id": "fixture"}, {}, "private", assets, build_spec=spec, oracle={})
    assert observed["packages"] == ["private_dep-1.0.0", "public_dep-1.0.0"]


def test_migration_uses_readonly_lock_entry_and_existing_public_cargo_root(tmp_path, monkeypatch):
    from agentic_runtime_migration import rust_lane, rust_overlay, rust_protoc
    f = fixture(tmp_path, monkeypatch, session=True)
    ctx, task, plan, oracle, patch, _, _ = f
    spec, assets = bind(f)
    plan["workdir"] = "private/harness"
    spec["frozen_lock_recovery"].update(public_cwd="existing-cli", public_package="app")
    monkeypatch.setattr(recovery, "recover", lambda *a: (spec, assets))
    monkeypatch.setattr(rust_lane, "load_plan", lambda *a: plan)
    monkeypatch.setattr(rust_lane, "compiler", lambda *a: patch)
    monkeypatch.setattr(rust_lane, "vendor", lambda *a, **k: (patch, None))
    monkeypatch.setattr(rust_overlay, "cargo_cli_prefix", lambda *a: [])
    monkeypatch.setattr(rust_overlay, "build_tools", lambda *a: patch)
    monkeypatch.setattr(rust_protoc, "binding", lambda *a: ([], {}, []))
    monkeypatch.setattr(rust_lane.RuntimeRecipe, "validate", lambda *a: None)
    ctx.task_directory = lambda *a: tmp_path
    ctx.golden = lambda *a: patch
    ctx.bundle = lambda *a, **k: patch
    ctx.image = lambda *a: (rust_lane.IMAGE, {})
    ctx.modules = lambda *a: []
    ctx.immutable_tests = lambda *a: ["app/tests/fixed.rs"]
    recipe = rust_lane.migrate(ctx, task, oracle)
    assert recipe.workdir == "/workspace" and recipe.scratch_paths == []
    assert "--cwd existing-cli" in recipe.dev_checks[0]
    assert "check --locked -p app" in recipe.dev_checks[0]
    fixed = recipe.grading["commands"][0]
    assert "locks.py" in fixed["command"] and "--cwd private/harness" in fixed["command"]
    assert "test -p app --test fixed" in fixed["command"]
    assert fixed["expected_tests"] == oracle["pass_to_pass_tests"]
    assert not any("Cargo.lock" in command for command in recipe.bootstrap)
    plan["prepare_command"] = "git submodule update --init --recursive native"
    monkeypatch.setattr(rust_lane, "compiler", lambda *a: pytest.fail("non-lock preparation was silently skipped"))
    with pytest.raises(ContractError, match="original non-lock preparation still needs an adapter"):
        rust_lane.migrate(ctx, task, oracle)
