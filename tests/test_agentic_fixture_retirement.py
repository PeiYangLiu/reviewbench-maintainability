"""Real filesystem controls for non-traversing terminal fixture retention."""
import copy
import gzip
import io
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tarfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import fixture_retirement as retention
from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash, file_hash


def mixed_fixture(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "nested").mkdir()
    (root / "same-a").write_bytes(b"UNCHANGED_DATA\n" * 128)
    (root / "nested/same-b").write_bytes((root / "same-a").read_bytes())
    os.link(root / "same-a", root / "same-hardlink")
    (root / "empty").write_bytes(b"")
    (root / "odd\nname\\literal").write_bytes(b"odd-filename-bytes")
    external = tmp_path / "outside"
    external.mkdir()
    (external / "sentinel").write_bytes(b"EXTERNAL_NOT_AN_ARCHIVE_PAYLOAD")
    os.symlink(str(external), root / "outside-directory")
    os.symlink(str(external / "sentinel"), root / "outside-file")
    os.symlink(str(root / "same-a"), root / "absolute-inside")
    os.symlink("../same-a", root / "nested/relative-inside")
    os.mkfifo(root / "nested/fifo", 0o640)
    program = "import socket; s=socket.socket(socket.AF_UNIX); s.bind('socket'); s.close()"
    subprocess.run([sys.executable, "-I", "-B", "-c", program], cwd=root / "nested", check=True)
    (root / "same-a").chmod(0o640)
    (root / "nested").chmod(0o500)
    os.utime(root / "empty", ns=(-1000000000, -1000000000))
    return root, external


def archive_fixture(tmp_path):
    root, external = mixed_fixture(tmp_path)
    manifest = retention.scan_tree(root)
    body = tmp_path / "fixture.tar.gz"
    compact = retention.create_archive(root, manifest, body)
    return root, external, manifest, body, compact


def test_full_types_data_modes_times_and_link_equivalence_roundtrip(tmp_path):
    root, external, manifest, body, compact = archive_fixture(tmp_path)
    original = retention.scan_tree(root)
    restored = tmp_path / "independent-restoration"
    proof = retention.restore_archive(body, restored, expected=manifest)
    assert proof["status"] == "all_fixture_nodes_independently_restored"
    assert proof["counts"]["fifo"] == proof["counts"]["socket"] == 1
    assert proof["gzip_crc_verified"] and proof["links_followed"] == 0
    assert retention.scan_tree(root) == original
    assert stat.S_ISFIFO((restored / "nested/fifo").lstat().st_mode)
    assert stat.S_ISSOCK((restored / "nested/socket").lstat().st_mode)
    assert (restored / "same-a").stat().st_ino == (restored / "same-hardlink").stat().st_ino
    assert (restored / "same-a").stat().st_ino != (restored / "nested/same-b").stat().st_ino
    assert compact["unique_payloads"] == 3
    assert compact["unique_payload_bytes"] < manifest["logical_bytes"]
    assert os.readlink(restored / "outside-directory") == str(external)
    assert (external / "sentinel").read_bytes() == b"EXTERNAL_NOT_AN_ARCHIVE_PAYLOAD"
    with tarfile.open(body) as archive:
        assert all(member.isfile() for member in archive)
        assert all(member.name == "manifest.json" or member.name.startswith("payload/") for member in archive)


@pytest.mark.parametrize("variant", ["traversal", "absolute", "duplicate", "link-parent", "missing-parent",
                                    "missing-root", "bad-mode", "bad-group", "bad-digest", "unknown-type"])
def test_invalid_manifest_cannot_choose_extraction_paths(tmp_path, variant):
    root, _ = mixed_fixture(tmp_path)
    manifest = retention.scan_tree(root)
    bad = copy.deepcopy(manifest)
    rows = bad["entries"]
    file = next(row for row in rows if row["path"] == "same-a")
    directory = next(row for row in rows if row["path"] == "nested")
    if variant == "traversal":
        file["path"] = "../escaped"
    elif variant == "absolute":
        file["path"] = "/tmp/escaped"
    elif variant == "duplicate":
        rows.append(copy.deepcopy(file))
    elif variant == "link-parent":
        directory.update(type="symlink", link="/tmp", mode=0o777)
    elif variant == "missing-parent":
        rows.remove(directory)
    elif variant == "missing-root":
        rows.remove(next(row for row in rows if row["path"] == "."))
    elif variant == "bad-mode":
        file["mode"] = 0o10000
    elif variant == "bad-group":
        file["group"] = "absent"
    elif variant == "bad-digest":
        file["sha256"] = "not-a-hash"
    else:
        file["type"] = "block_device"
    body = tmp_path / "malformed.tar.gz"
    data = json.dumps(bad).encode()
    with tarfile.open(body, "w:gz") as archive:
        info = tarfile.TarInfo("manifest.json")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    destination = tmp_path / "restored"
    with pytest.raises(ContractError):
        retention.restore_archive(body, destination)
    assert not destination.exists()
    assert root.is_dir()


@pytest.mark.parametrize("limit,value", [("max_nodes", 2), ("max_file_bytes", 1)])
def test_scan_bounds_preserve_original_tree(tmp_path, limit, value):
    root, _ = mixed_fixture(tmp_path)
    with pytest.raises(ContractError):
        retention.scan_tree(root, {limit: value})
    assert (root / "same-a").read_bytes() == b"UNCHANGED_DATA\n" * 128


@pytest.mark.parametrize("limit,value", [("max_archive_bytes", 32), ("max_manifest_bytes", 32)])
def test_archive_bounds_preserve_original_tree(tmp_path, limit, value):
    root, _ = mixed_fixture(tmp_path)
    manifest = retention.scan_tree(root)
    with pytest.raises(ContractError):
        retention.create_archive(root, manifest, tmp_path / "bounded.tar.gz", {limit: value})
    assert retention.scan_tree(root) == manifest


def test_archive_does_not_follow_redirected_payload_parent(tmp_path):
    root, external = mixed_fixture(tmp_path)
    manifest = retention.scan_tree(root)
    original = root / "nested"
    original.rename(root / "retained-nested")
    (root / "nested").symlink_to(external, target_is_directory=True)
    with pytest.raises((OSError, ContractError)):
        retention.create_archive(root, manifest, tmp_path / "redirected.tar.gz")
    assert (external / "sentinel").read_bytes() == b"EXTERNAL_NOT_AN_ARCHIVE_PAYLOAD"


@pytest.mark.parametrize("variant", ["manifest", "digest", "crc", "existing"])
def test_restore_requires_complete_pinned_bytes_and_new_destination(tmp_path, variant):
    root, _, manifest, body, _ = archive_fixture(tmp_path)
    destination = tmp_path / "restored"
    kwargs = {}
    if variant == "manifest":
        wrong = copy.deepcopy(manifest)
        wrong["source_root"] += "-different"
        kwargs["expected"] = wrong
    elif variant == "digest":
        kwargs["expected_digest"] = "0" * 64
    elif variant == "crc":
        data = bytearray(body.read_bytes())
        data[-8] ^= 1
        body.write_bytes(data)
    else:
        destination.mkdir()
        (destination / "keep").write_text("original")
    with pytest.raises((OSError, ContractError, tarfile.ReadError)):
        retention.restore_archive(body, destination, **kwargs)
    assert root.is_dir()
    if variant == "existing":
        assert (destination / "keep").read_text() == "original"
    elif variant in {"manifest", "digest"}:
        assert not destination.exists()


def test_live_open_descriptor_is_observed_without_opening_fifo(tmp_path):
    root, _, manifest, _, _ = archive_fixture(tmp_path)
    with (root / "same-a").open("rb"):
        observation = retention.live_references(root, manifest, inspect_docker=False)
    assert any(row.get("pid") == os.getpid() for row in observation["references"])
    assert not observation["observation_errors"]


@pytest.mark.parametrize("variant", ["omitted", "modified", "read-file", "failed"])
def test_privileged_metadata_probe_is_complete_readonly_and_fail_closed(tmp_path, monkeypatch, variant):
    value = {"checked_pids": [123], "references": [], "observation_errors": [],
             "workspace_file_contents_read": 0, "modifications": 0}
    if variant == "omitted":
        value["checked_pids"] = []
    elif variant == "modified":
        value["modifications"] = 1
    elif variant == "read-file":
        value["workspace_file_contents_read"] = 1
    def run(command, **kwargs):
        assert command[:5] == ["sudo", "-n", "/usr/bin/python3", "-I", "-B"]
        request = json.loads(kwargs["input"])
        assert request["pids"] == {"123": "pinned-start"}
        return subprocess.CompletedProcess(command, 1 if variant == "failed" else 0,
                                           json.dumps(value), "")
    monkeypatch.setattr(retention.subprocess, "run", run)
    with pytest.raises(ContractError):
        retention.privileged_proc_metadata(tmp_path, {(1, 2)}, {"123": "pinned-start"}, os.geteuid())


def test_retirement_and_original_path_rollback_preserve_external_objects(tmp_path):
    root, external, manifest, body, _ = archive_fixture(tmp_path)
    restored = tmp_path / "independent-restoration"
    proof = retention.restore_archive(body, restored, expected=manifest)
    result = retention.retire_tree(root, manifest, proof, inspect_docker=False)
    assert result["status"] == "terminal_fixture_retired"
    assert not root.exists() and result["quarantine_absent"]
    assert (external / "sentinel").read_bytes() == b"EXTERNAL_NOT_AN_ARCHIVE_PAYLOAD"
    rollback = retention.restore_archive(body, root, expected=manifest)
    assert rollback["entries"] == len(manifest["entries"])
    assert retention.shape(retention.scan_tree(root)["entries"]) == retention.shape(manifest["entries"])
    assert (root / "absolute-inside").read_bytes() == (root / "same-a").read_bytes()


@pytest.mark.parametrize("variant", ["changed", "incomplete-proof", "live", "quarantine-live"])
def test_retirement_stops_before_deletion_and_restores_quarantine_on_liveness_change(
        tmp_path, monkeypatch, variant):
    root, _, manifest, body, _ = archive_fixture(tmp_path)
    proof = retention.restore_archive(body, tmp_path / "restored", expected=manifest)
    calls = []
    if variant == "changed":
        (root / "same-a").write_bytes(b"new original data")
    elif variant == "incomplete-proof":
        proof["entries"] -= 1
    else:
        def observe(path, original, inspect_docker=True):
            calls.append(str(path))
            bad = variant == "live" or len(calls) == 2
            return {"references": [{"pid": 123, "handle": "fixture"}] if bad else [],
                    "observation_errors": []}
        monkeypatch.setattr(retention, "live_references", observe)
    with pytest.raises(ContractError):
        retention.retire_tree(root, manifest, proof, inspect_docker=False)
    assert root.is_dir()
    assert not list(tmp_path.glob(".retiring-*"))


def selection_fixture(tmp_path):
    root = tmp_path / "test-tmp-synthetic-999999999"
    root.mkdir()
    info = root.stat()
    command = shlex.join([sys.executable, "-m", "pytest", "-q", "--basetemp=" + str(root)])
    receipt = tmp_path / "original-receipt.txt"
    receipt.write_text("COMMAND: " + command + "\nLITERAL OUTPUT: 3 passed\nEXIT STATUS: 0\n")
    value = {
        "schema_version": "reviewbench-terminal-pytest-fixture-selection/v1",
        "root": str(root), "parent": str(tmp_path), "root_dev": info.st_dev,
        "root_ino": info.st_ino, "producer_pid": 999999999,
        "producer_receipt": {"path": str(receipt), "sha256": file_hash(receipt)},
        "producer_command": command, "producer_literal_output": "3 passed", "producer_exit_status": 0,
    }
    return root, value


def test_original_successful_pytest_receipt_and_root_identity_bind(tmp_path):
    root, value = selection_fixture(tmp_path)
    assert retention.validate_selection(value, tmp_path / "scratch") == root


@pytest.mark.parametrize("variant", ["root-inode", "receipt-bytes", "other-output", "failed-producer",
                                    "outside-parent", "wrong-basetemp"])
def test_foreign_or_unfinished_fixture_selections_are_rejected(tmp_path, variant):
    root, value = selection_fixture(tmp_path)
    if variant == "root-inode":
        value["root_ino"] += 1
    elif variant == "receipt-bytes":
        Path(value["producer_receipt"]["path"]).write_text("rewritten")
    elif variant == "other-output":
        value["producer_literal_output"] = "4 passed"
    elif variant == "failed-producer":
        value["producer_exit_status"] = 1
    elif variant == "outside-parent":
        value["parent"] = str(tmp_path / "elsewhere")
    else:
        value["producer_command"] += " --basetemp=/different"
    with pytest.raises(ContractError):
        retention.validate_selection(value, tmp_path / "scratch")
    assert root.is_dir()


def test_scan_root_symlink_and_traversal_errors_are_not_silently_skipped(tmp_path):
    root, _ = mixed_fixture(tmp_path)
    link = tmp_path / "redirect"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(OSError):
        retention.scan_tree(link)
    with pytest.raises(PermissionError):
        retention.traversal_error(PermissionError("unreadable original directory"))


def make_foreign_directory(path):
    path.mkdir()
    (path / "retained-data").write_bytes(b"root-owned-directory-fixture-data")
    program = "import os,sys; os.chown(sys.argv[1],0,0); os.chmod(sys.argv[1],0o555)"
    result = subprocess.run(["sudo", "-n", "/usr/bin/python3", "-I", "-B", "-c", program, str(path)],
                            capture_output=True, text=True, timeout=15)
    if result.returncode:
        pytest.skip("this host has no authorized fixture-ownership control")


def test_real_foreign_readonly_directory_is_changed_only_after_verified_quarantine(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    make_foreign_directory(root / "foreign")
    manifest = retention.scan_tree(root)
    body = tmp_path / "archive.tar.gz"
    retention.create_archive(root, manifest, body)
    proof = retention.restore_archive(body, tmp_path / "restored", expected=manifest)
    assert (root / "foreign").stat().st_uid == 0
    result = retention.retire_tree(root, manifest, proof, inspect_docker=False)
    assert not root.exists()
    assert result["quarantine_directory_access"]["directories_changed"] == ["foreign"]
    assert result["quarantine_directory_access"]["file_content_changes"] == 0
    assert result["quarantine_directory_access"]["outside_root_changes"] == 0


@pytest.mark.parametrize("variant", ["ordinary-root", "different-inode", "symlink-parent"])
def test_quarantine_access_helper_rejects_foreign_or_redirected_targets(tmp_path, variant):
    root = tmp_path / (".retiring-control-" + "a" * 32)
    root.mkdir()
    make_foreign_directory(root / "foreign")
    manifest = retention.scan_tree(root)
    directories = [row for row in manifest["entries"] if row["path"] == "foreign"]
    chosen = root
    if variant == "ordinary-root":
        chosen = tmp_path / "source"
        root.rename(chosen)
    elif variant == "different-inode":
        directories = copy.deepcopy(directories)
        directories[0]["ino"] += 1
    else:
        (root / "foreign").rename(root / "untouched")
        (root / "foreign").symlink_to(root / "untouched", target_is_directory=True)
    with pytest.raises(ContractError):
        retention.prepare_quarantine_directory_access(chosen, manifest, directories)
    target = chosen / ("untouched" if variant == "symlink-parent" else "foreign")
    assert target.stat().st_uid == 0
    assert stat.S_IMODE(target.stat().st_mode) == 0o555
