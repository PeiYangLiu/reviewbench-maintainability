"""Real concurrency, HTTP transport, cache integrity and admission regressions."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import multiprocessing
import os
from pathlib import Path
import threading
import time

import pytest

from reviewbench_maintainability.agentic.contracts import ContractError, InfrastructureError, atomic_json
from reviewbench_maintainability.agentic.resources import ResourceGate, ResourceLimits, ResourceRequest
from reviewbench_maintainability.agentic.storage import (
    AzureBlobStore, DirectoryObjectStore, LocalObjectCache, ObjectSpec, TransferSlots, describe_file,
)


def object_file(root, name, size=128):
    path = root / name
    path.write_bytes(hashlib.sha256(name.encode()).digest() * (size // 32))
    return path, describe_file(path)


def test_cache_download_once_and_protect_active_lease(tmp_path):
    remote = DirectoryObjectStore(tmp_path / "remote")
    a, sa = object_file(tmp_path, "a")
    b, sb = object_file(tmp_path, "b")
    remote.put(a, sa)
    remote.put(b, sb)
    cache = LocalObjectCache(tmp_path / "cache", remote, max_bytes=128, min_free_bytes=0, wait_seconds=.1)
    barrier = threading.Barrier(8)

    def use(_):
        barrier.wait()
        with cache.acquire(sa) as path:
            time.sleep(.01)
            assert describe_file(path) == sa

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(use, range(8)))
    assert remote.stats["downloads"] == 1
    with cache.acquire(sa):
        with pytest.raises(InfrastructureError, match="full/pinned"):
            with cache.acquire(sb):
                pytest.fail("evicted a leased object")
    with cache.acquire(sb) as path:
        assert describe_file(path) == sb
    assert not cache._path(sa).exists()
    assert cache.status()["bytes"] == 128


def test_publish_requires_remote_verification_and_never_deletes_source(tmp_path):
    remote = DirectoryObjectStore(tmp_path / "remote")
    source, spec = object_file(tmp_path, "source")
    cache = LocalObjectCache(tmp_path / "cache", remote, max_bytes=1024, min_free_bytes=0)
    actual, proof = cache.publish(source)
    assert actual == spec and proof["sha256"] == spec.sha256
    assert source.exists()
    with cache.acquire(spec):
        pass
    assert remote.stats["downloads"] == 0
    # A modified cache file is refetched even when the length stays unchanged.
    cache._path(spec).chmod(0o644)
    cache._path(spec).write_bytes(b"x" * spec.size_bytes)
    with cache.acquire(spec) as path:
        assert describe_file(path) == spec
    assert remote.stats["downloads"] == 1


def test_failed_download_releases_reservation_and_removes_partial(tmp_path):
    class FailingStore:
        def get(self, spec, path):
            path.write_bytes(b"partial")
            raise InfrastructureError("network error")
    _, spec = object_file(tmp_path, "source")
    cache = LocalObjectCache(tmp_path / "cache", FailingStore(), min_free_bytes=0)
    with pytest.raises(InfrastructureError):
        with cache.acquire(spec):
            pass
    assert cache.status()["objects"] == 0
    assert not list((cache.root / "staging").iterdir())


def test_cache_rejects_unmanaged_root_foreign_keys_and_changed_settings(tmp_path):
    remote = DirectoryObjectStore(tmp_path / "remote")
    unmanaged = tmp_path / "unmanaged"
    unmanaged.mkdir()
    (unmanaged / "keep").write_text("not our cache")
    with pytest.raises(ContractError, match="unmarked"):
        LocalObjectCache(unmanaged, remote, min_free_bytes=0)
    assert (unmanaged / "keep").read_text() == "not our cache"
    cache = LocalObjectCache(tmp_path / "cache", remote, max_bytes=1024, min_free_bytes=0)
    with pytest.raises(ContractError, match="configuration"):
        LocalObjectCache(cache.root, remote, max_bytes=2048, min_free_bytes=0)
    atomic_json(cache.index_path, {"../../keep": {}})
    with pytest.raises(ContractError, match="key"):
        cache.status()


def test_dead_pending_cache_owner_is_reclaimed(tmp_path):
    remote = DirectoryObjectStore(tmp_path / "remote")
    source, spec = object_file(tmp_path, "new")
    _, old_spec = object_file(tmp_path, "old")
    remote.put(source, spec)
    cache = LocalObjectCache(tmp_path / "cache", remote, max_bytes=128, min_free_bytes=0)
    pending = "a" * 32 + ".part"
    (cache.root / "staging" / pending).write_bytes(b"partial")
    atomic_json(cache.index_path, {old_spec.sha256: {
        "state": "pending", "spec": asdict(old_spec), "pid": 2**30, "process_start": "0",
        "temporary": pending,
    }})
    with cache.acquire(spec):
        pass
    assert not (cache.root / "staging" / pending).exists()
    assert cache.status()["pending"] == 0


class BlobFixture:
    def __enter__(self):
        self.objects, self.calls = {}, []
        self.corrupt = self.truncate = self.fail_put = False
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def do_HEAD(self):
                fixture.calls.append("HEAD")
                value = fixture.objects.get(self.path)
                self.send_response(200 if value else 404)
                self.send_header("Content-Length", str(len(value[0])) if value else "0")
                if value:
                    self.send_header("Content-MD5", value[1])
                    self.send_header("x-ms-meta-sha256", value[2])
                    self.send_header("ETag", '"test-etag"')
                self.end_headers()

            def do_PUT(self):
                fixture.calls.append("PUT")
                data = self.rfile.read(int(self.headers["Content-Length"]))
                assert self.headers["Authorization"] == "Bearer fixture-token"
                md5 = base64.b64encode(hashlib.md5(data).digest()).decode()
                assert self.headers["Content-MD5"] == md5
                assert self.headers["If-None-Match"] == "*"
                code = 503 if fixture.fail_put else (412 if self.path in fixture.objects else 201)
                if code == 201:
                    fixture.objects[self.path] = (data, md5, self.headers["x-ms-meta-sha256"])
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                fixture.calls.append("GET")
                data = fixture.objects[self.path][0]
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("ETag", '"test-etag"')
                self.end_headers()
                if fixture.corrupt:
                    data = b"x" * len(data)
                if fixture.truncate:
                    data = data[:len(data) // 2]
                    fixture.truncate = False
                    self.close_connection = True
                self.wfile.write(data)
                self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.store = AzureBlobStore(
            f"http://127.0.0.1:{self.server.server_port}", "fixture", "archives",
            credential=lambda **kw: "fixture-token", allow_loopback_http=True, retries=1,
        )
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def test_real_http_put_head_get_checksums_reuse_and_truncated_stream(tmp_path):
    source, spec = object_file(tmp_path, "source", 2**20)
    with BlobFixture() as fixture:
        assert fixture.store.put(source, spec)["etag"] == '"test-etag"'
        fixture.store.put(source, spec)
        assert fixture.calls.count("PUT") == 1
        fixture.truncate = True
        destination = tmp_path / "fresh"
        fixture.store.get(spec, destination)
        assert describe_file(destination) == spec
        assert fixture.calls.count("GET") == 2
        fixture.corrupt = True
        with pytest.raises(ContractError, match="verification"):
            fixture.store.get(spec, tmp_path / "corrupt")
        assert not (tmp_path / "corrupt").exists()


def test_failed_upload_never_promotes_local_cache(tmp_path):
    source, _ = object_file(tmp_path, "source")
    with BlobFixture() as fixture:
        fixture.fail_put = True
        cache = LocalObjectCache(tmp_path / "cache", fixture.store, min_free_bytes=0)
        with pytest.raises(InfrastructureError, match="503"):
            cache.publish(source)
        assert cache.status()["objects"] == 0 and source.exists()


@pytest.mark.parametrize("url", ["http://example.com", "https://user:pass@example.com",
                                "https://example.com?sig=secret", "https://example.com/path"])
def test_blob_endpoint_rejects_credentials_or_non_https(url):
    with pytest.raises(ContractError):
        AzureBlobStore(url, "fixture", "archives")


def resources(**changes):
    return {"available_memory_bytes": 10000, "free_disk_bytes": 10000,
            "load_1m": 0, "host_cpus": 8, **changes}


def limits(**changes):
    return ResourceLimits(**{"tasks": 3, "heavy_tasks": 1, "cpus": 4, "memory_bytes": 1000,
                             "scratch_bytes": 1000, "min_free_disk_bytes": 100,
                             "min_available_memory_bytes": 100, **changes})


def test_gate_heavy_slots_and_release(tmp_path):
    gate = ResourceGate(tmp_path / "gate", tmp_path, limits(), probe=resources)
    request = ResourceRequest("build", memory_bytes=128, scratch_bytes=256, heavy=True)
    with gate.acquire(request):
        with pytest.raises(InfrastructureError, match="heavy_slots"):
            with gate.acquire(request, timeout=.01, poll_seconds=.01):
                pass
        with gate.acquire(ResourceRequest("light", memory_bytes=128)):
            assert gate.status()["active"]["tasks"] == 2
    state = gate.status()
    assert state["active"]["tasks"] == 0
    assert state["completed"] == 2 and state["peaks"]["heavy_tasks"] == 1


@pytest.mark.parametrize("host,reason", [
    (resources(available_memory_bytes=150), "host_memory_headroom"),
    (resources(free_disk_bytes=150), "host_disk_headroom"),
    (resources(load_1m=7), "host_cpu_pressure"),
])
def test_host_pressure_pauses_admission(tmp_path, host, reason):
    gate = ResourceGate(tmp_path / "gate", tmp_path, limits(), probe=lambda: host)
    with pytest.raises(InfrastructureError, match=reason):
        with gate.acquire(ResourceRequest("task", memory_bytes=100, scratch_bytes=100), timeout=0):
            pass
    assert gate.status()["admitted"] == 0


def test_gate_checks_all_memory_reservations(tmp_path):
    gate = ResourceGate(tmp_path / "gate", tmp_path, limits(),
                        probe=lambda: resources(available_memory_bytes=350))
    with gate.acquire(ResourceRequest("first", memory_bytes=200)):
        with pytest.raises(InfrastructureError, match="host_memory_headroom"):
            with gate.acquire(ResourceRequest("second", memory_bytes=100), timeout=0):
                pass


def _gate_worker(root, active, peak, lock):
    gate = ResourceGate(Path(root) / "gate", Path(root), limits(), probe=resources)
    with gate.acquire(ResourceRequest("process", memory_bytes=1), timeout=10, poll_seconds=.01):
        with lock:
            active.value += 1
            peak.value = max(peak.value, active.value)
        time.sleep(.05)
        with lock:
            active.value -= 1


def test_gate_limit_is_shared_by_processes(tmp_path):
    ctx = multiprocessing.get_context("fork")
    active, peak, lock = ctx.Value("i", 0), ctx.Value("i", 0), ctx.Lock()
    processes = [ctx.Process(target=_gate_worker, args=(str(tmp_path), active, peak, lock)) for _ in range(8)]
    for p in processes:
        p.start()
    for p in processes:
        p.join(15)
        assert p.exitcode == 0
    assert 1 < peak.value <= 3
    assert ResourceGate(tmp_path / "gate", tmp_path, limits(), probe=resources).status()["completed"] == 8


def test_gate_recovers_only_dead_process_reservations(tmp_path):
    gate = ResourceGate(tmp_path / "gate", tmp_path, limits(), probe=resources)
    state = gate.status()
    state["leases"]["dead"] = {"pid": 2**30, "process_start": "0",
                               "request": asdict(ResourceRequest("dead", memory_bytes=1))}
    atomic_json(gate.state_path, state)
    assert gate.status()["reclaimed_dead_owners"] == 1


def test_orphan_container_reservation_is_retained_then_adopted(tmp_path, monkeypatch):
    from reviewbench_maintainability.agentic import resources as module
    gate = ResourceGate(tmp_path / "gate", tmp_path, limits(tasks=1), probe=resources)
    request = ResourceRequest("resume-this-task", memory_bytes=1)
    key = "a" * 32
    state = gate.status()
    state["leases"][key] = {"pid": 2**30, "process_start": "0", "uses_containers": True,
                            "request": asdict(request)}
    atomic_json(gate.state_path, state)
    monkeypatch.setattr(module, "containers_alive", lambda _: True)
    assert gate.status()["active"]["tasks"] == 1
    with pytest.raises(InfrastructureError, match="task_slots"):
        with gate.acquire(request, timeout=0):
            pass
    with gate.acquire(request, adopt_lease=key, uses_containers=True) as lease:
        assert lease["lease"] == key
        assert gate.status()["active"]["tasks"] == 1
        assert gate.status()["adopted"] == 1
        monkeypatch.setattr(module, "containers_alive", lambda _: False)
    assert gate.status()["active"]["tasks"] == 0


def test_a_live_task_reservation_cannot_be_adopted(tmp_path):
    gate = ResourceGate(tmp_path / "gate", tmp_path, limits(), probe=resources)
    request = ResourceRequest("live", memory_bytes=1)
    with gate.acquire(request) as lease:
        with pytest.raises(ContractError, match="live/different"):
            with gate.acquire(request, adopt_lease=lease["lease"]):
                pass


def test_transfer_pool_is_bounded_across_independent_instances(tmp_path):
    active = peak = 0
    lock = threading.Lock()

    def transfer(_):
        nonlocal active, peak
        pool = TransferSlots(tmp_path, 2)
        with pool.acquire():
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(.03)
            with lock:
                active -= 1
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(transfer, range(8)))
    assert peak == 2
