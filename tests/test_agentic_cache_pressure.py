"""Real cache-body/lock/restore controls; disk capacity is scaled for fixtures."""
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import cache_pressure as pressure
import run_agentic_prepare_queue as queue
from reviewbench_maintainability.agentic.contracts import ContractError, atomic_json
from reviewbench_maintainability.agentic.resources import file_lock
from reviewbench_maintainability.agentic.storage import DirectoryObjectStore, LocalObjectCache

KIB = 1024
POLICY = {"max_reclaim_bytes": 96 * KIB, "min_object_bytes": 1, "max_objects": 8,
          "min_idle_seconds": 10, "cooldown_seconds": 1, "headroom_bytes": 0}


def fixture(tmp_path, monkeypatch):
    remote = DirectoryObjectStore(tmp_path / "remote")
    cache = LocalObjectCache(tmp_path / "cache", remote, max_bytes=1024**2, min_free_bytes=0)
    specs = []
    for number in range(3):
        source = tmp_path / f"original-{number}"
        source.write_bytes(bytes([65 + number]) * (32 * KIB))
        spec, _ = cache.publish(source)
        specs.append(spec)
    with file_lock(cache.lock_path):
        index = cache._index()
        for number, spec in enumerate(specs):
            index[spec.sha256]["last_used"] = time.time() - 100 + number
        atomic_json(cache.index_path, index)

    def disk(_):
        used = sum(p.stat().st_size for p in (cache.root / "objects").iterdir())
        return SimpleNamespace(total=128 * KIB, used=used, free=128 * KIB - used)

    monkeypatch.setattr(pressure.shutil, "disk_usage", disk)
    return cache, specs


def test_cold_bodies_reclaimed_and_unchanged_backing_objects_restore(tmp_path, monkeypatch):
    cache, specs = fixture(tmp_path, monkeypatch)
    result = pressure.reclaim(cache, tmp_path, 80 * KIB, POLICY)
    assert result["target_reached"] and result["reclaimed_bytes"] == 64 * KIB
    assert result["reclaimed_objects"] == 2 and result["free_before_bytes"] == 32 * KIB
    assert set(cache._index()) == {specs[2].sha256}
    assert len(list(cache.remote.root.iterdir())) == 3
    rows = [json.loads(p.read_text()) for p in Path(result["journal"]).glob("*.json")
            if p.name != "pass.json"]
    assert len(rows) == 2 and all(row["state"] == "reclaimed" for row in rows)
    # Restore into a separate cache, never refill the changed fixture.
    restored = LocalObjectCache(tmp_path / "rollback", cache.remote,
                                max_bytes=1024**2, min_free_bytes=0)
    for number, spec in enumerate(specs):
        with restored.acquire(spec) as path:
            assert path.read_bytes() == bytes([65 + number]) * (32 * KIB)
    assert len(cache._index()) == 1


def test_active_readers_and_recent_assets_are_not_evicted(tmp_path, monkeypatch):
    cache, specs = fixture(tmp_path, monkeypatch)
    with file_lock(cache.lock_path):
        index = cache._index()
        index[specs[2].sha256]["last_used"] = time.time()
        atomic_json(cache.index_path, index)
    with file_lock(cache.root / "locks" / specs[0].sha256, shared=True):
        result = pressure.reclaim(cache, tmp_path, 120 * KIB, POLICY)
    assert result["reclaimed_bytes"] == 32 * KIB and not result["target_reached"]
    assert result["skipped"]["active_object_lock"] == 1
    assert set(cache._index()) == {specs[0].sha256, specs[2].sha256}


@pytest.mark.parametrize("fault", ["missing", "corrupt", "aliased"])
def test_no_independent_backing_proof_means_no_deletion(tmp_path, monkeypatch, fault):
    cache, specs = fixture(tmp_path, monkeypatch)
    spec = specs[0]
    if fault == "missing":
        (cache.remote.root / spec.sha256).unlink()
    elif fault == "corrupt":
        (cache.remote.root / spec.sha256).write_bytes(b"bad")
    else:
        cache.remote.root = cache.root / "objects"
    result = pressure.reclaim(cache, tmp_path, 128 * KIB, POLICY)
    assert cache._path(spec).exists() and spec.sha256 in cache._index()
    if fault != "missing":
        assert result["reclaimed_bytes"] == 0 and result["errors"]
    else:
        assert result["skipped"]["backing_object_absent"] == 1


def test_invalid_stamp_pending_and_small_objects_are_left_alone(tmp_path, monkeypatch):
    cache, specs = fixture(tmp_path, monkeypatch)
    cache._path(specs[0]).chmod(0o644)
    cache._path(specs[0]).write_bytes(b"different size and stamp")
    with file_lock(cache.lock_path):
        index = cache._index()
        index[specs[1].sha256]["state"] = "pending"
        atomic_json(cache.index_path, index)
    result = pressure.reclaim(cache, tmp_path, 128 * KIB, POLICY)
    assert result["reclaimed_objects"] == 1
    assert set(cache._index()) == {specs[0].sha256, specs[1].sha256}


def test_reclaim_budget_and_persisted_cooldown_bound_repeated_polls(tmp_path, monkeypatch):
    cache, specs = fixture(tmp_path, monkeypatch)
    options = {**POLICY, "max_reclaim_bytes": 32 * KIB}
    first = pressure.reclaim(cache, tmp_path, 128 * KIB, options)
    assert first["reclaimed_bytes"] == 32 * KIB
    second = pressure.reclaim(cache, tmp_path, 128 * KIB, options)
    assert second["status"] == "cooldown" and second["reclaimed_bytes"] == 0
    assert len(cache._index()) == 2


def test_foreign_filesystem_does_not_remove_any_object(tmp_path, monkeypatch):
    cache, _ = fixture(tmp_path, monkeypatch)
    other = Path("/dev/shm")
    if not other.is_dir() or other.stat().st_dev == cache.root.stat().st_dev:
        pytest.skip("two filesystems required")
    result = pressure.reclaim(cache, other, 128 * KIB, POLICY)
    assert result["status"] == "different_filesystem" and len(cache._index()) == 3


def test_cross_process_lock_refuses_duplicate_reclaimer(tmp_path, monkeypatch):
    cache, _ = fixture(tmp_path, monkeypatch)
    with file_lock(cache.root / "pressure-reclaim.lock"):
        result = pressure.reclaim(cache, tmp_path, 128 * KIB, POLICY)
    assert result["status"] == "another_reclaimer_active" and len(cache._index()) == 3


def test_network_head_holds_neither_index_lock_nor_scheduler(tmp_path, monkeypatch):
    cache, specs = fixture(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    calls = []
    original = pressure._committed

    def slow(cache, spec):
        with file_lock(cache.lock_path, nonblocking=True):
            calls.append(spec.sha256)
        entered.set()
        assert release.wait(5)
        return original(cache, spec)

    monkeypatch.setattr(pressure, "_committed", slow)
    with pressure.CachePressure() as controller:
        start = time.monotonic()
        controller.request(cache, tmp_path, 60 * KIB, POLICY)
        assert time.monotonic() - start < .5
        assert entered.wait(5)
        controller.request(cache, tmp_path, 100 * KIB, POLICY)
        assert controller.status()["status"] == "running"
        assert calls == [specs[0].sha256]
        release.set()
    result = controller.status()
    assert result["reclaimed_bytes"] == 32 * KIB and result["reclaimed_objects"] == 1


def test_changed_index_after_head_keeps_the_body(tmp_path, monkeypatch):
    cache, specs = fixture(tmp_path, monkeypatch)
    original = pressure._committed

    def touched(cache, spec):
        result = original(cache, spec)
        with file_lock(cache.lock_path):
            index = cache._index()
            index[spec.sha256]["last_used"] = time.time()
            atomic_json(cache.index_path, index)
        return result

    monkeypatch.setattr(pressure, "_committed", touched)
    result = pressure.reclaim(cache, tmp_path, 128 * KIB, POLICY)
    assert result["reclaimed_bytes"] == 0 and len(cache._index()) == 3
    assert result["skipped"]["changed_after_head"] == 3


def test_headroom_recovers_during_head_so_no_extra_body_is_deleted(tmp_path, monkeypatch):
    cache, _ = fixture(tmp_path, monkeypatch)
    original = pressure._committed

    def recovered(cache, spec):
        result = original(cache, spec)
        monkeypatch.setattr(pressure.shutil, "disk_usage", lambda _: SimpleNamespace(free=1024**2))
        return result

    monkeypatch.setattr(pressure, "_committed", recovered)
    result = pressure.reclaim(cache, tmp_path, 128 * KIB, POLICY)
    assert result["reclaimed_bytes"] == 0 and result["target_reached"] and len(cache._index()) == 3


@pytest.mark.parametrize("change", [
    {"max_reclaim_bytes": 17 * 1024**3}, {"max_reclaim_bytes": True},
    {"max_objects": 257}, {"max_objects": 0}, {"min_object_bytes": 0},
    {"min_idle_seconds": -1}, {"cooldown_seconds": 0},
    {"cooldown_seconds": float("nan")}, {"headroom_bytes": 5 * 1024**3},
    {"unknown": 1},
])
def test_invalid_policy_is_rejected(change):
    with pytest.raises(ContractError, match="cache pressure"):
        pressure.policy_options(change)


def test_queue_policy_is_optional_and_explicit(tmp_path):
    from test_agentic_prepare_queue import plan
    document = plan([])
    queue.validate(document)
    document["cache_pressure"] = {}
    queue.validate(document)
    document["cache_pressure"] = {"max_objects": 1000}
    with pytest.raises(ContractError):
        queue.validate(document)


def test_linked_journal_does_not_trigger_deletion(tmp_path, monkeypatch):
    cache, _ = fixture(tmp_path, monkeypatch)
    (cache.root / "pressure-reclaims").symlink_to(tmp_path / "foreign")
    with pytest.raises(ContractError, match="linked"):
        pressure.reclaim(cache, tmp_path, 128 * KIB, POLICY)
    assert len(cache._index()) == 3


def test_queue_backfills_during_slow_head_then_admits_original_pending_jobs(tmp_path, monkeypatch):
    import yaml
    from test_agentic_prepare_queue import setup, job, plan
    config_path, worker = setup(tmp_path)
    cache, _ = fixture(tmp_path, monkeypatch)
    config = yaml.safe_load(config_path.read_text())
    config["storage"] = {
        "remote": {"kind": "directory", "path": str(cache.remote.root)},
        "cache_dir": str(cache.root), "scratch_dir": str(tmp_path / "scratch"),
        "max_cache_bytes": cache.max_bytes, "min_free_bytes": 0, "transfers": 2,
    }
    config["execution"]["resource_limits"]["min_free_disk_bytes"] = 16 * KIB
    config_path.write_text(yaml.safe_dump(config))
    large = [job(tmp_path, worker, f"large{i}") for i in range(2)]
    small = job(tmp_path, worker, "small")
    for item in large:
        item["resources"]["scratch_bytes"] = 64 * KIB
        item["admission_timeout_seconds"] = 5
    small["resources"]["scratch_bytes"] = KIB
    document = {**plan([*large, small]), "cache_pressure": POLICY}
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(document))
    original = pressure._committed
    calls = []

    def waits_for_backfill(cache, spec):
        calls.append(spec.sha256)
        deadline = time.monotonic() + 4
        while not (tmp_path / "small.json").exists():
            assert time.monotonic() < deadline, "remote HEAD blocked independent backfill"
            time.sleep(.01)
        return original(cache, spec)

    monkeypatch.setattr(pressure, "_committed", waits_for_backfill)
    assert queue.run_queue(plan_path, config_path, tmp_path / "queue", poll_seconds=.02) == 0
    small_end = json.loads((tmp_path / "small.json").read_text())["finished"]
    assert all(json.loads((tmp_path / f"large{i}.json").read_text())["started"] >= small_end
               for i in range(2))
    assert len(calls) == 2
    for item in large:
        directory = queue.folder(tmp_path / "queue", item)
        wait = json.loads((directory / "dispatch-wait.json").read_text())
        result = json.loads((directory / "result.json").read_text())
        assert wait["reason"] == "host_disk_headroom" and result["exit_status"] == 0
        assert result["command"] == item["command"]
