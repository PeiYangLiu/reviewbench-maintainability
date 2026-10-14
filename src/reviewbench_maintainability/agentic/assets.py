"""Pinned archive-tree assets, hydrated locally once per admitted task.

Shared caches retain only immutable compressed parts. A task holds at most one
part lease while extracting, not a lease on every chunk of every dependency.
Developer and grader mounts remain distinct even when a task reuses a tree.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat

from .archives import extract_archive, validate_archive
from .contracts import ContractError, InfrastructureError, atomic_json, file_hash
from .resources import containers_alive
from .snapshots import tree_hash
from .storage import require_local_filesystem

ASSET_SCHEMA = "reviewbench-runtime-asset-tree/v1"


def asset_descriptor(asset: dict, asset_root: Path) -> dict:
    # The descriptor itself follows the existing file-hash contract.
    path = Path(asset["path"])
    if not path.is_absolute():
        path = asset_root / path
    if not path.is_file() or path.is_symlink() or file_hash(path) != asset["sha256"]:
        raise ContractError("runtime archive descriptor is missing or changed")
    value = json.loads(path.read_text())
    if value.get("schema_version") != ASSET_SCHEMA:
        raise ContractError("unsupported runtime asset archive")
    validate_archive(value["archive"])
    if value.get("source_kind") not in {"dependency", "toolchain", "grader", "submodule"}:
        raise ContractError("archive asset requires an explicit content role")
    return value


class AssetPool:
    """Private to a single resource lease; mounts are readonly to containers."""

    def __init__(self, cache, scratch: Path, lease: str, asset_root: Path):
        self.cache, self.lease, self.asset_root = cache, lease, asset_root
        self.root = scratch / "runtime-assets" / lease
        self.ready = {}
        require_local_filesystem(self.root)

    def materialize(self, asset: dict) -> Path:
        descriptor = asset_descriptor(asset, self.asset_root)
        if self.cache is None:
            raise ContractError("archive assets require the configured object cache")
        key = asset["sha256"]
        if key in self.ready:
            return self.ready[key]
        target, marker = self.root / key, self.root / (key + ".json")
        self.root.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if not marker.is_file() or marker.is_symlink() or target.is_symlink():
                raise InfrastructureError("incomplete/changed asset hydration requires an infrastructure retry")
            saved = json.loads(marker.read_text())
            if saved.get("descriptor_sha256") != key or saved.get("tree_sha256") != tree_hash(target):
                raise InfrastructureError("saved runtime asset tree changed")
        else:
            extract_archive(descriptor["archive"], self.cache, target)
            digest = tree_hash(target)
            if descriptor.get("tree_sha256") and descriptor["tree_sha256"] != digest:
                shutil.rmtree(target)
                raise ContractError("hydrated runtime asset tree differs from its pinned digest")
            atomic_json(marker, {"descriptor_sha256": key, "tree_sha256": digest})
        self.ready[key] = target
        return target

    def mutable_cache(self, name: str) -> Path:
        import re
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ContractError("invalid task cache name")
        path = self.root / "mutable-caches" / name
        if path.is_symlink():
            raise ContractError("task cache became a symlink")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def close(self):
        # Failed container cleanup must not remove assets from a still-live,
        # potentially resumable process. Its resource reservation also remains.
        if self.root.exists() and not containers_alive(self.lease):
            # Go deliberately makes extracted module directories read-only.
            # They are this lease's disposable copies, not shared source or
            # dependency inputs. Restore owner directory access before unlink.
            # Never chmod through symlinks into an unrelated filesystem tree.
            for directory, _, _ in os.walk(self.root, followlinks=False):
                path = Path(directory)
                if not path.is_symlink():
                    path.chmod(path.stat().st_mode | stat.S_IWUSR | stat.S_IXUSR)
            shutil.rmtree(self.root)
