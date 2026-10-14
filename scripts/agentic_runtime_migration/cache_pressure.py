"""Free cold, committed cache bodies before disk admission, not during extraction.

This is host orchestration: the frozen execution gate and object-cache protocol
stay unchanged. Only indexed, stamp-verified, idle and exclusively locked bodies
may be reclaimed. The independent backing store is verified before each unlink.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
import math
import os
from pathlib import Path
import shutil
import time
import uuid

from reviewbench_maintainability.agentic.contracts import ContractError, atomic_json
from reviewbench_maintainability.agentic.resources import GiB, file_lock
from reviewbench_maintainability.agentic.storage import (
    DirectoryObjectStore, ObjectSpec, describe_file,
)

DEFAULT_POLICY = {
    "max_reclaim_bytes": 8 * GiB,
    "min_object_bytes": 16 * 1024**2,
    "max_objects": 64,
    "min_idle_seconds": 1800,
    "cooldown_seconds": 60,
    "headroom_bytes": GiB,
}


def policy_options(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULT_POLICY):
        raise ContractError("cache pressure: unknown policy fields")
    options = {**DEFAULT_POLICY, **value}
    for key in ("max_reclaim_bytes", "min_object_bytes", "max_objects", "headroom_bytes"):
        if type(options[key]) is not int:
            raise ContractError("cache pressure: byte/object limits must be integers")
    if not (0 < options["min_object_bytes"] <= options["max_reclaim_bytes"] <= 16 * GiB
            and 1 <= options["max_objects"] <= 256 and 0 <= options["headroom_bytes"] <= 4 * GiB):
        raise ContractError("cache pressure: limits exceed the bounded reclaim policy")
    for key, lower in (("min_idle_seconds", 0), ("cooldown_seconds", 1)):
        if (type(options[key]) not in (int, float) or not math.isfinite(options[key])
                or not lower <= options[key] <= 86400):
            raise ContractError("cache pressure: invalid age/cooldown")
    return options


def _regular_directory(path):
    if path.is_symlink():
        raise ContractError("cache pressure: journal directory is linked")
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise ContractError("cache pressure: journal directory is not a directory")


def _committed(cache, spec):
    """No local reread is accepted as evidence of an independent Azure copy."""
    with cache.transfers.acquire():
        if isinstance(cache.remote, DirectoryObjectStore):
            path = cache.remote.root / spec.sha256
            if not path.exists():
                return None
            if (path.is_symlink() or path.samefile(cache._path(spec))
                    or describe_file(path) != spec):
                raise ContractError("cache pressure: independent backing object changed")
            return {**asdict(spec), "uri": str(path), "verification": "local-filesystem-sha256"}
        return cache.remote.head(spec)


def reclaim(cache, storage_path, required_free_bytes, policy):
    """One cross-process, bounded pass. No task, test, model or build is replayed."""
    options = policy_options(policy)
    if type(required_free_bytes) is not int or required_free_bytes < 0:
        raise ContractError("cache pressure: invalid free-space target")
    storage_path = Path(storage_path)
    if cache.root.stat().st_dev != storage_path.stat().st_dev:
        return {"status": "different_filesystem", "reclaimed_bytes": 0}
    journal = cache.root / "pressure-reclaims"
    _regular_directory(journal)
    latest = journal / "latest.json"
    if latest.is_symlink() or (cache.root / "pressure-reclaim.lock").is_symlink():
        raise ContractError("cache pressure: journal/lock is linked")
    try:
        with file_lock(cache.root / "pressure-reclaim.lock", nonblocking=True):
            now = time.time()
            if latest.exists():
                previous = json.loads(latest.read_text())
                until = previous["completed_at"] + options["cooldown_seconds"]
                if now < until:
                    return {"status": "cooldown", "retry_at": until, "reclaimed_bytes": 0,
                            "previous_journal": previous["journal"]}
            start_free = shutil.disk_usage(storage_path).free
            target = required_free_bytes + options["headroom_bytes"]
            if start_free >= target:
                return {"status": "headroom_available", "reclaimed_bytes": 0}
            directory = journal / (str(time.time_ns()) + "-" + uuid.uuid4().hex)
            directory.mkdir()
            with file_lock(cache.lock_path):
                index = cache._index()
            candidates = sorted(index.items(), key=lambda row: (row[1].get("last_used", now), row[0]))
            record = {
                "status": "running", "journal": str(directory), "started_at": now,
                "storage_path": str(storage_path.resolve()), "cache_root": str(cache.root),
                "required_free_bytes": required_free_bytes, "target_free_bytes": target,
                "free_before_bytes": start_free, "policy": options,
                "reclaimed_bytes": 0, "reclaimed_objects": 0, "examined_objects": 0,
                "skipped": {}, "errors": [], "new_SUT_model_judge_calls": 0,
            }
            atomic_json(directory / "pass.json", record)

            def skip(reason):
                record["skipped"][reason] = record["skipped"].get(reason, 0) + 1

            for key, old in candidates:
                if (record["examined_objects"] >= options["max_objects"]
                        or record["reclaimed_bytes"] >= options["max_reclaim_bytes"]
                        or shutil.disk_usage(storage_path).free >= target):
                    break
                spec = ObjectSpec.from_dict(old["spec"])
                if (old.get("state") != "verified" or spec.size_bytes < options["min_object_bytes"]
                        or spec.size_bytes > options["max_reclaim_bytes"] - record["reclaimed_bytes"]
                        or now - old.get("last_used", now) < options["min_idle_seconds"]):
                    skip("ineligible")
                    continue
                record["examined_objects"] += 1
                try:
                    # Nonblocking object lock first; the index lock is never
                    # held across a remote HEAD or a wait for an active reader.
                    with file_lock(cache.root / "locks" / key, nonblocking=True):
                        with file_lock(cache.lock_path):
                            current = cache._index().get(key)
                            path = cache._path(spec)
                            if (current != old or not cache._valid(spec, current)
                                    or path.stat().st_nlink != 1):
                                skip("changed_or_invalid")
                                continue
                        receipt = _committed(cache, spec)
                        if receipt is None:
                            skip("backing_object_absent")
                            continue
                        if any(receipt.get(k) != value for k, value in asdict(spec).items()):
                            raise ContractError("cache pressure: backing proof differs from object identity")
                        intent = {"state": "intent", "object": asdict(spec),
                                  "old_index_entry": old, "remote_receipt": receipt,
                                  "local_path": str(path), "recorded_at": time.time()}
                        intent_path = directory / (key + ".json")
                        atomic_json(intent_path, intent)
                        with file_lock(cache.lock_path):
                            index = cache._index()
                            if index.get(key) != old or not cache._valid(spec, index.get(key)):
                                skip("changed_after_head")
                                continue
                            # HEAD can be slow: another writer may already have
                            # freed space. Do not evict extra bodies needlessly.
                            if shutil.disk_usage(storage_path).free >= target:
                                break
                            path.unlink()
                            index.pop(key)
                            atomic_json(cache.index_path, index)
                        intent.update(state="reclaimed", completed_at=time.time())
                        atomic_json(intent_path, intent)
                        record["reclaimed_bytes"] += spec.size_bytes
                        record["reclaimed_objects"] += 1
                        atomic_json(directory / "pass.json", record)
                except BlockingIOError:
                    skip("active_object_lock")
                except Exception as error:
                    # A failed HEAD is a retained diagnosis, never permission
                    # to delete the local body. Backfill can continue meanwhile.
                    record["errors"].append({"sha256": key, "error": f"{type(error).__name__}: {error}"})
                    break
            record.update(status="completed", completed_at=time.time(),
                          free_after_bytes=shutil.disk_usage(storage_path).free)
            record["target_reached"] = record["free_after_bytes"] >= target
            atomic_json(directory / "pass.json", record)
            atomic_json(latest, {"completed_at": record["completed_at"], "journal": str(directory)})
            return record
    except BlockingIOError:
        return {"status": "another_reclaimer_active", "reclaimed_bytes": 0}


class CachePressure:
    """One background metadata worker, so Blob latency never blocks backfill."""

    def __init__(self):
        self.executor = None
        self.future = None
        self.record = {"status": "idle"}
        self.retry_at = 0
        self.options = None

    def status(self):
        if self.future is not None and self.future.done():
            try:
                self.record = self.future.result()
            except Exception as error:
                self.record = {"status": "error", "error": f"{type(error).__name__}: {error}",
                               "reclaimed_bytes": 0}
            self.future = None
            self.retry_at = max(self.record.get("retry_at", 0),
                                time.time() + self.options["cooldown_seconds"])
        return dict(self.record)

    def request(self, cache, storage_path, required_free_bytes, policy):
        self.status()
        if self.future is not None or time.time() < self.retry_at:
            return
        self.options = policy_options(policy)
        if cache is None:
            self.record = {"status": "no_object_cache"}
            self.retry_at = time.time() + self.options["cooldown_seconds"]
            return
        if self.executor is None:
            self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cache-pressure")
        self.record = {"status": "running", "required_free_bytes": required_free_bytes,
                       "started_at": time.time()}
        self.future = self.executor.submit(reclaim, cache, storage_path, required_free_bytes, self.options)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self.executor is not None:
            self.executor.shutdown(wait=True)
