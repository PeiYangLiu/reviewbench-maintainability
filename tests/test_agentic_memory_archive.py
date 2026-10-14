"""RAM spooling lowers disk admission without hiding bytes from the shared limits."""
from dataclasses import asdict
import io
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tarfile
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import archive_agentic_public_image as cli
from agentic_runtime_migration import archive_spool as spool
from reviewbench_maintainability.agentic.contracts import ContractError
from reviewbench_maintainability.agentic.resources import GiB, ResourceGate, ResourceRequest


def test_default_archive_reservation_is_unchanged():
    for size in (1, GiB, 3 * GiB):
        row = spool.archive_request(size)
        assert row.memory_bytes == 2 * GiB and row.scratch_bytes == max(4 * GiB, size * 2)


def test_ram_buffer_and_daemon_tar_staging_have_distinct_real_reservations(tmp_path):
    image_size = 2509959211
    old, new = spool.archive_request(image_size), spool.archive_request(image_size, 1024)
    assert new.cpus == old.cpus == 2 and not new.heavy
    assert new.memory_bytes == 3 * GiB
    assert new.scratch_bytes == image_size + max(256 * spool.MiB, (image_size + 3) // 4)
    gate = ResourceGate(tmp_path / "gate", tmp_path / "data")
    verifier = ResourceRequest("original-verifier", cpus=4, memory_bytes=8 * GiB,
                               scratch_bytes=13540712448, heavy=True)
    totals = gate._totals({"verify": {"request": asdict(verifier)}})
    host = {"free_disk_bytes": 96 * GiB, "available_memory_bytes": 480 * GiB,
            "load_1m": 4, "host_cpus": 256}
    assert gate._blocked(old, totals, host) == "host_disk_headroom"
    assert gate._blocked(new, totals, host) is None
    assert gate.storage_path == (tmp_path / "data").resolve()
    host["available_memory_bytes"] = 42 * GiB
    assert gate._blocked(new, totals, host) == "host_memory_headroom"
    host["available_memory_bytes"], host["free_disk_bytes"] = 480 * GiB, 80 * GiB
    assert gate._blocked(new, totals, host) == "host_disk_headroom"


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "1024", 8193])
def test_unbounded_or_ambiguous_spool_size_is_rejected(value):
    with pytest.raises(ContractError, match="integer"):
        spool.archive_request(GiB, value)


def test_bounded_writer_rejects_before_allocating_extra_bytes():
    stream = io.BytesIO()
    writer = spool.BoundedWriter(stream, 4)
    assert writer.write(b"1234") == 4 and writer.tell() == 4
    with pytest.raises(ContractError, match="byte limit"):
        writer.write(b"x")
    assert stream.getvalue() == b"1234"


def test_short_writes_are_not_accepted():
    writer = spool.BoundedWriter(SimpleNamespace(write=lambda _: 0), 4)
    with pytest.raises(ContractError, match="short"):
        writer.write(b"x")


def test_docker_staging_is_not_erased_by_an_image_pin():
    size = 10 * GiB
    request = spool.archive_request(size, 1024)
    assert request.scratch_bytes >= size and request.memory_bytes == 3 * GiB


def tmpfs(root, monkeypatch):
    monkeypatch.setattr(spool.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout="tmpfs\n", stderr=""))
    return spool.prepare_root(root)


def test_ext4_is_not_miscounted_as_ram(tmp_path, monkeypatch):
    monkeypatch.setattr(spool.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout="ext4\n", stderr=""))
    root = tmp_path / "spool"
    with pytest.raises(ContractError, match="tmpfs"):
        spool.prepare_root(root)
    assert not root.exists()


@pytest.mark.parametrize("fault", ["foreign-file", "world-readable", "symlink", "lock-symlink", "marker-symlink"])
def test_unknown_or_redirected_spool_directory_is_not_managed(tmp_path, monkeypatch, fault):
    root = tmp_path / "spool"
    root.mkdir(mode=0o700)
    if fault == "foreign-file":
        (root / "keep").write_text("not ours")
    elif fault == "world-readable":
        root.chmod(0o755)
    elif fault == "symlink":
        root.rmdir()
        root.symlink_to(tmp_path)
    else:
        (root / (".lock" if fault == "lock-symlink" else "OWNER.json")).symlink_to(tmp_path / "unrelated")
    with pytest.raises(ContractError):
        tmpfs(root, monkeypatch)
    if fault == "foreign-file":
        assert (root / "keep").read_text() == "not ours"
    assert not (tmp_path / "unrelated").exists()


def stale(root, token="a" * 32):
    folder = root / ("job-" + token)
    folder.mkdir(mode=0o700)
    owner = {"schema_version": spool.SCHEMA, "uid": os.getuid(), "pid": 999999,
             "process_start": "12345", "maximum_bytes": spool.MiB, "pin": None}
    (folder / "owner.json").write_text(json.dumps(owner))
    (folder / "image.tar.gz").write_bytes(b"old buffer")
    return folder, owner


@pytest.mark.parametrize("state", ["alive", "unknown", "missing", "reused"])
def test_only_definitely_dead_owned_buffers_are_reaped(tmp_path, monkeypatch, state):
    root, _ = tmpfs(tmp_path / "spool", monkeypatch)
    folder, owner = stale(root)
    identity = {"alive": "12345", "unknown": "unknown", "missing": None, "reused": "54321"}[state]
    monkeypatch.setattr(spool, "process_identity", lambda _: identity)
    _, reclaimed = spool.prepare_root(root)
    assert folder.exists() == (state in {"alive", "unknown"})
    assert bool(reclaimed) == (state in {"missing", "reused"})


def test_unbound_file_in_dead_spool_is_retained_for_inspection(tmp_path, monkeypatch):
    root, _ = tmpfs(tmp_path / "spool", monkeypatch)
    folder, _ = stale(root)
    (folder / "unexpected").write_text("keep")
    monkeypatch.setattr(spool, "process_identity", lambda _: None)
    with pytest.raises(ContractError, match="unbound"):
        spool.prepare_root(root)
    assert (folder / "unexpected").read_text() == "keep"


@pytest.mark.parametrize("failure", [False, True])
def test_memory_buffer_and_unstarted_pin_close_together(tmp_path, monkeypatch, failure):
    root, _ = tmpfs(tmp_path / "spool", monkeypatch)
    image, calls, token = "sha256:" + "b" * 64, [], None

    def docker(*args):
        nonlocal token
        calls.append(args)
        if args[0] == "create":
            token = args[args.index("--name") + 1].removeprefix("rb-archive-pin-")
            assert "--read-only" in args and args[args.index("--network") + 1] == "none"
            return "c" * 64
        assert args[:2] == ("container", "inspect")
        return json.dumps([{"Image": image, "State": {"Status": "created", "Running": False},
                            "HostConfig": {"ReadonlyRootfs": True}}])

    removed = []
    monkeypatch.setattr(spool, "docker", docker)
    monkeypatch.setattr(spool, "remove_pin", lambda pin: removed.append(dict(pin)) or "removed")
    monkeypatch.setattr(spool, "process_identity", lambda _: "12345")
    record = None
    try:
        with spool.memory_spool(root, spool.MiB, image, "lease") as (path, record):
            path.write_bytes(b"archive")
            assert path.is_file() and not path.is_symlink()
            if failure:
                raise RuntimeError("controlled failure")
    except RuntimeError:
        assert failure
    assert not list(root.glob("job-*")) and record["buffer_removed"]
    assert removed == [{"name": "rb-archive-pin-" + token, "image": image, "token": token}]
    assert all(row[0] not in {"start", "run"} for row in calls)


@pytest.mark.parametrize("fault", ["token", "image", "name", "running", "label"])
def test_cleanup_never_removes_an_unbound_or_started_container(monkeypatch, fault):
    pin = {"token": "a" * 32, "name": "rb-archive-pin-" + "a" * 32, "image": "sha256:" + "b" * 64}
    row = {"Id": "c" * 64, "Image": pin["image"],
           "Config": {"Labels": {"reviewbench.archive-spool": pin["token"]}},
           "State": {"Status": "created", "Running": False}}
    if fault in {"token", "image", "name"}:
        pin[fault] = "changed"
    elif fault == "running":
        row["State"] = {"Status": "running", "Running": True}
    else:
        row["Config"]["Labels"] = {}
    monkeypatch.setattr(spool.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=json.dumps([row]), stderr=""))
    monkeypatch.setattr(spool, "docker", lambda *_: pytest.fail("unbound container removed"))
    with pytest.raises(ContractError):
        spool.remove_pin(pin)


def test_a_failed_transfer_records_terminal_failure_without_overwriting_old_success(tmp_path, monkeypatch):
    output = tmp_path / "new"

    def fail(*args, **kwargs):
        args[2].mkdir()
        (args[2] / "publication.json").write_text(json.dumps({"status": "running"}))
        raise ContractError("controlled failure")

    monkeypatch.setattr(cli, "_archive", fail)
    with pytest.raises(ContractError):
        cli.archive(tmp_path / "config", tmp_path / "prepared", output)
    assert json.loads((output / "publication.json").read_text())["status"] == "failed"
    previous = tmp_path / "previous"
    previous.mkdir()
    (previous / "publication.json").write_text('{"status":"archived_and_loaded"}')
    monkeypatch.setattr(cli, "_archive", lambda *a, **k: (_ for _ in ()).throw(ContractError("exists")))
    with pytest.raises(ContractError):
        cli.archive(tmp_path / "config", tmp_path / "prepared", previous)
    assert (previous / "publication.json").read_text() == '{"status":"archived_and_loaded"}'


def tiny_saved_image(tmp_path):
    layer = b"one public synthetic layer\n"
    layers = ["sha256:" + hashlib.sha256(layer).hexdigest()]
    config = json.dumps({"rootfs": {"diff_ids": layers}}).encode()
    image = "sha256:" + hashlib.sha256(config).hexdigest()
    contents = {
        "config.json": config, "layer.tar": layer,
        "manifest.json": json.dumps([{"Config": "config.json", "Layers": ["layer.tar"]}]).encode(),
    }
    path = tmp_path / "image.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name, data in contents.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return path, image, {"RootFS": {"Layers": layers}}


def test_actual_outer_tar_bytes_are_checked_before_docker_load(tmp_path, monkeypatch):
    path, image, inspect = tiny_saved_image(tmp_path)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: pytest.fail("staging overflow was loaded"))
    with pytest.raises(ValueError, match="staging"):
        cli.verify_and_load(image, inspect, path, staging_limit=spool.MiB)


def test_matching_config_and_all_layers_are_loaded_from_verified_memory_file(tmp_path, monkeypatch):
    path, image, inspect = tiny_saved_image(tmp_path)
    calls = []
    monkeypatch.setattr(cli.subprocess, "run", lambda args, **k: (
        calls.append(args) or SimpleNamespace(returncode=0, stdout="loaded", stderr="", args=args)))
    layers, result, staging = cli.verify_and_load(image, inspect, path, staging_limit=128 * spool.MiB)
    assert layers == inspect["RootFS"]["Layers"] and staging == 3 * 4096
    assert calls == [["docker", "load", "-i", str(path)]] and result.returncode == 0
