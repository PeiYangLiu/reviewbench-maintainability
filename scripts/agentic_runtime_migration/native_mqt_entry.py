"""Seed the original complete MQT96 dependency trees in one isolated workspace."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        while data := stream.read(1024**2):
            h.update(data)
    return h.hexdigest()


def tree_digest(root):
    if root.is_symlink() or not root.is_dir():
        raise ValueError("MQT source tree is absent or linked")
    records, size = [], 0
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            if not path.resolve().is_relative_to(root.resolve()):
                raise ValueError("MQT dependency link escapes its input tree")
            records.append((name, os.readlink(path), "symlink"))
        elif path.is_file():
            size += path.stat().st_size
            records.append((name, digest(path), bool(path.stat().st_mode & 0o111)))
        elif not path.is_dir():
            raise ValueError("MQT input contains a special file")
    if not records or len(records) > 20000 or size > 256 * 1024**2:
        raise ValueError("MQT original dependency source exceeds its bound")
    # Path ordering compares path components; archive identities sort the full
    # relative POSIX names (e.g. ".drone.star" precedes ".drone/after.sh").
    records.sort(key=lambda row: row[0])
    return hashlib.sha256(json.dumps(records, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest(), len(records)


def local(workspace, relative):
    name = Path(relative)
    if name.is_absolute() or ".." in name.parts or not name.parts:
        raise ValueError("MQT output path must be relative")
    path = workspace / name
    if path.is_symlink() or any(p.is_symlink() for p in path.parents if p.is_relative_to(workspace)):
        raise ValueError("MQT output path traverses a link")
    if not path.resolve().is_relative_to(workspace):
        raise ValueError("MQT output escapes the current candidate")
    return path


def seed(spec, workspace):
    if spec.get("schema_version") != "reviewbench-mqt96-source/v1":
        raise ValueError("unknown MQT original-source specification")
    workspace = Path(workspace).resolve()
    declaration = local(workspace, spec["declaration"])
    if not declaration.is_file() or digest(declaration) != spec["declaration_sha256"]:
        raise ValueError("current MQT dependency declaration differs")
    source = Path(spec["source"])
    if not source.is_absolute():
        raise ValueError("MQT dependency source is not an absolute asset")
    checksum, files = tree_digest(source)
    if checksum != spec["source_tree_sha256"]:
        raise ValueError("MQT dependency input digest differs")
    destination = local(workspace, spec["destination"])
    wrapper = local(workspace, spec["public_wrapper_path"])
    contents = spec["public_wrapper"].encode()
    if destination.exists() and tree_digest(destination)[0] != checksum:
        raise ValueError("candidate-owned MQT dependency files differ")
    if wrapper.exists() and (not wrapper.is_file() or wrapper.read_bytes() != contents):
        raise ValueError("candidate-owned MQT public wrapper differs")
    # All sources, declaration, and destinations are checked before any write.
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination, symlinks=True)
    if not wrapper.exists():
        wrapper.parent.mkdir(parents=True, exist_ok=True)
        wrapper.write_bytes(contents)
    if tree_digest(destination)[0] != checksum or wrapper.read_bytes() != contents:
        raise ValueError("MQT source seeding changed an input")
    print("MQT96_ORIGINAL_SOURCES_READY files=" + str(files) + " source_changes=0", flush=True)
    return files


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    seed(json.loads(args.spec.read_text()), args.workspace)
