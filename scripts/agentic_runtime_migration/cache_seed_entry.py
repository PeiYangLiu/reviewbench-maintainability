#!/usr/bin/env python3
"""Restore an attested public-dependency seed into one private writable cache."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys

SCHEMA = "reviewbench-public-dependency-cache-seed/v1"
MANIFEST = "cache-seed.json"
MARKER = ".reviewbench-public-seed.json"
LOCK = ".reviewbench-public-seed.lock"


def require(value, message):
    if not value:
        raise ValueError("public cache seed: " + message)


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024**2):
            digest.update(chunk)
    return digest.hexdigest()


def relative(value):
    path = PurePosixPath(value)
    require(isinstance(value, str) and value and not path.is_absolute()
            and ".." not in path.parts and path.as_posix() == value
            and "\0" not in value and path.parts[0] not in {MARKER, LOCK}, "invalid cache path")
    return path


def validate(source, expected_binding, *, canonical_archive_modes=False):
    require(type(canonical_archive_modes) is bool, "archive mode policy must be explicit")
    source = Path(source)
    require(source.is_dir() and not source.is_symlink(), "seed root must be regular")
    manifest = source / MANIFEST
    require(manifest.is_file() and not manifest.is_symlink() and manifest.stat().st_size <= 32 * 1024**2,
            "manifest absent or oversized")
    value = json.loads(manifest.read_text())
    require(value.get("schema_version") == SCHEMA and value.get("role") == "public-dependencies",
            "only public dependency artifacts may seed a developer cache")
    require(value["binding_sha256"] == expected_binding and len(expected_binding) == 64,
            "compiler/source/config binding differs")
    require(value.get("producer") == "clean-start-public-check" and value.get("grader_assets_visible") is False,
            "seed did not originate in a clean public-only environment")
    require(isinstance(value["files"], list) and 0 < len(value["files"]) <= 200000,
            "missing or excessive file inventory")
    seen, total = set(), 0
    for row in value["files"]:
        name = str(relative(row["path"]))
        require(name not in seen, "duplicate cache entry")
        seen.add(name)
        path = source / "payload" / name
        require(not any(parent.is_symlink() for parent in path.parents if parent != source.parent
                        and parent.is_relative_to(source)), "symlink cache parent")
        require(type(row["mtime_ns"]) is int and row["mtime_ns"] >= 0, "invalid timestamp")
        if row["kind"] == "file":
            require(path.is_file() and not path.is_symlink()
                    and path.stat().st_size == row["bytes"] and sha(path) == row["sha256"],
                    "seed artifact bytes changed")
            require(type(row["mode"]) is int and 0 <= row["mode"] <= 0o777,
                    "invalid original artifact mode")
            transported = ((0o755 if row["mode"] & 0o111 else 0o644)
                           if canonical_archive_modes else row["mode"])
            require(stat.S_IMODE(path.stat().st_mode) == transported, "seed artifact mode changed")
            total += row["bytes"]
        else:
            require(row["kind"] == "symlink" and path.is_symlink()
                    and os.readlink(path) == row["target"], "seed link changed")
            target = PurePosixPath(name).parent / row["target"]
            require(not PurePosixPath(row["target"]).is_absolute(), "absolute cache link")
            depth = 0
            for component in target.parts:
                depth += -1 if component == ".." else int(component != ".")
                require(depth >= 0, "cache link escapes its root")
    require(total == value["bytes"] and total <= 16 * 1024**3, "cache byte bound differs")
    actual = {p.relative_to(source / "payload").as_posix()
              for p in (source / "payload").rglob("*") if p.is_file() or p.is_symlink()}
    require(actual == seen, "unlisted/missing cache files")
    return value, sha(manifest)


def restore(source, target, expected_binding, *, canonical_archive_modes=False):
    source, target = Path(source), Path(target)
    value, digest = validate(source, expected_binding, canonical_archive_modes=canonical_archive_modes)
    expected_marker = {"manifest_sha256": digest, "binding_sha256": expected_binding}
    if canonical_archive_modes:
        expected_marker["canonical_archive_modes"] = True
    require(target.is_absolute() and not target.is_symlink(), "destination must be a private regular cache")
    target.mkdir(parents=True, exist_ok=True)
    lock = target / LOCK
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        marker = target / MARKER
        require(not marker.is_symlink(), "cache marker is a symlink")
        if marker.exists():
            previous = json.loads(marker.read_text())
            require(previous == expected_marker,
                    "writable cache belongs to another seed")
            return {"status": "already_initialized", "files": 0, "bytes": 0, "manifest_sha256": digest}
        require({p.name for p in target.iterdir()} <= {LOCK}, "unmarked writable cache is not empty")
        # Data is copied, never hardlinked to immutable shared seed files.
        for row in value["files"]:
            name = str(relative(row["path"]))
            before, after = source / "payload" / name, target / name
            after.parent.mkdir(parents=True, exist_ok=True)
            if row["kind"] == "file":
                shutil.copyfile(before, after)
                after.chmod(row["mode"])
            else:
                after.symlink_to(row["target"])
            os.utime(after, ns=(row["mtime_ns"], row["mtime_ns"]), follow_symlinks=False)
            if row["kind"] == "file":
                require(sha(after) == row["sha256"], "cache copy changed")
                require(after.stat().st_ino != before.stat().st_ino or
                        after.stat().st_dev != before.stat().st_dev, "shared writable inode")
        pending = marker.with_suffix(".tmp")
        pending.write_text(json.dumps(expected_marker, sort_keys=True) + "\n")
        os.replace(pending, marker)
    return {"status": "restored", "files": len(value["files"]), "bytes": value["bytes"],
            "manifest_sha256": digest}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--binding-sha256", required=True)
    parser.add_argument("--canonical-archive-modes", action="store_true",
                        help="validate the frozen archive transport's 0644/0755 modes; restore original modes")
    args = parser.parse_args()
    # Installed runtime scripts write only the declared private cache mount.
    require(str(args.target).startswith("/opt/reviewbench-cache/")
            and len(args.target.parts) == 4, "destination is not a cache mount")
    try:
        result = restore(args.source, args.target, args.binding_sha256,
                         canonical_archive_modes=args.canonical_archive_modes)
        print("PUBLIC_CACHE_SEED " + json.dumps(result, sort_keys=True))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as error:
        print("PUBLIC_CACHE_SEED_REJECTED " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
