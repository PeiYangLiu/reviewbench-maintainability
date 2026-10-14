import hashlib
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_anchor as anchor, rust_lane
from reviewbench_maintainability.agentic.contracts import ContractError


def test_original_test_command_keeps_async_package_target_and_nocapture():
    argv = shlex.split(anchor.cargo_command(grading=True))
    assert argv[:3] == [anchor.RUST + "/bin/cargo", "--config", anchor.SOURCES]
    assert argv[3:] == anchor.ORIGINAL.split()[2:]
    assert argv[3:] == ["test", "-p", "anchor-client", "--features", "async",
                        "--test", "oracle_nonblocking", "--", "--nocapture"]
    assert "--locked" not in argv and "--lib" not in argv


def test_public_check_compiles_current_production_without_original_harness():
    argv = shlex.split(anchor.cargo_command(grading=False))
    assert argv[3:] == ["check", "-p", "anchor-client", "--features", "async"]
    assert not any("oracle" in value or "test" == value for value in argv)
    assert "round2_test.rs" not in " ".join(argv)


def test_original_named_rpc_tests_and_historical_file_pin_are_fixed():
    assert anchor.TESTS == [
        "async_account_fetches_and_deserializes_real_rpc_payload",
        "async_account_reports_missing_account",
    ]
    assert anchor.HARNESS == "b8744706bfd1b115231c623b0851efdc7be1a12155c304e2a14964ecebc1d767"
    assert anchor.LOCK == "697ad719700463c2046039dc0625f74b5d56e6fc89663c365d6acd13d6c4cb1c"


def test_no_implicit_legacy_anchor_admission_and_no_other_task_profile():
    assert not anchor.requested(SimpleNamespace(config={}), {"id": anchor.TASK})
    ctx = SimpleNamespace(config={"migration": {anchor.OPTION: {anchor.TASK: {"path": "/pin"}}}})
    assert anchor.requested(ctx, {"id": anchor.TASK})
    assert not anchor.requested(ctx, {"id": "different"})
    with pytest.raises(ContractError, match="foreign"):
        anchor.requested(SimpleNamespace(config={"migration": {anchor.OPTION: {"other": {}}}}),
                         {"id": anchor.TASK})


def test_explicit_rust_entry_dispatch_and_small_preparation_budget(monkeypatch):
    ctx = SimpleNamespace(config={"migration": {anchor.OPTION: {anchor.TASK: {}}}})
    task, oracle = {"id": anchor.TASK}, {"original": True}
    expected, calls = object(), []
    monkeypatch.setattr(anchor, "migrate", lambda *args: calls.append(args) or expected)
    assert rust_lane.migrate(ctx, task, oracle) is expected
    assert calls == [(ctx, task, oracle)]
    assert rust_lane.migration_scratch_bytes(ctx, task) == 2 * 1024**3


def test_input_reference_requires_unchanged_regular_absolute_file(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text('{"original": true}\n')
    ref = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    assert anchor.referenced(ref) == {"original": True}
    assert anchor.referenced(ref, document=False) == path.read_bytes()
    link = tmp_path / "link"
    link.symlink_to(path)
    for value in ({}, {"path": "relative", "sha256": ref["sha256"]},
                  {**ref, "path": str(link)}, {**ref, "sha256": "a" * 64}):
        with pytest.raises(ContractError):
            anchor.referenced(value)


def test_fixture_gitlinks_are_explicit_unchanged_and_not_claimed_hydrated():
    ctx = SimpleNamespace(modules=lambda task: [
        {"path": path, "commit": commit, "omit": True, "reason": "old"}
        for path, commit in anchor.GITLINKS.items()
    ])
    rows = anchor.submodule_policy(ctx, {"id": anchor.TASK})
    assert {row["path"]: row["commit"] for row in rows} == anchor.GITLINKS
    assert all(row["omit"] is True and "six original calibrations required" in row["reason"] for row in rows)
    with pytest.raises(ContractError, match="gitlinks"):
        anchor.submodule_policy(SimpleNamespace(modules=lambda _: []), {"id": anchor.TASK})


@pytest.mark.parametrize("field,value", [
    ("status", "uploaded"), ("all_paths_bytes_full_modes_verified", False),
    ("gzip_crc_and_eof_verified", False), ("tree_sha256", "different"),
])
def test_archive_presence_is_not_an_independent_transport_proof(tmp_path, monkeypatch, field, value):
    descriptor = {"source_kind": "dependency", "tree_sha256": "a" * 64}
    asset = {"path": "/original.json", "sha256": "b" * 64}
    monkeypatch.setattr(anchor, "asset_descriptor", lambda *args: descriptor)
    prepared = {"status": "prepared", "asset": asset}
    readback = {"status": "independent_Blob_component_readback_verified", "asset": asset,
                "tree_sha256": "a" * 64, "all_paths_bytes_full_modes_verified": True,
                "gzip_crc_and_eof_verified": True}
    readback[field] = value
    with pytest.raises(ContractError, match="restoration"):
        anchor.checked_component(SimpleNamespace(output=tmp_path), prepared, readback,
                                 status="prepared", role="dependency")


def test_native_git_database_is_usable_after_file_only_image_transport(tmp_path):
    seed = tmp_path / "original"
    repository = seed / "git/db" / anchor.GIT_DATABASE
    repository.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", "--template=", str(repository)],
                   check=True, capture_output=True)
    object_id = subprocess.check_output(
        ["git", "--git-dir", str(repository), "hash-object", "-w", "--stdin"],
        input=b"original object bytes\n").decode().strip()
    file_only = tmp_path / "file-only"
    for path in seed.rglob("*"):
        if path.is_file():
            target = file_only / path.relative_to(seed)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    transported = file_only / "git/db" / anchor.GIT_DATABASE
    before = subprocess.run(["git", "--git-dir", str(transported), "cat-file", "-e", object_id],
                            capture_output=True, text=True)
    assert before.returncode != 0
    home = tmp_path / "cargo-home"
    home.mkdir()
    for command in anchor.git_seed_commands(str(file_only), str(home)):
        result = subprocess.run(["bash", "-c", command], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    restored = home / "git/db" / anchor.GIT_DATABASE
    assert all((restored / name).is_dir() for name in anchor.GIT_EMPTY_DIRECTORIES)
    assert subprocess.check_output(["git", "--git-dir", str(restored), "cat-file", "-p", object_id]) == b"original object bytes\n"
    assert not (file_only / "git/db" / anchor.GIT_DATABASE / "refs").exists()


def test_git_layout_restore_never_rewrites_reference_or_source_bytes():
    commands = "\n".join(anchor.git_seed_commands())
    assert "update-ref" not in commands and "fetch" not in commands and "checkout" not in commands
    assert all(path in commands for path in anchor.GIT_EMPTY_DIRECTORIES)
