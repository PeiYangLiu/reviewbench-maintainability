"""Seed the original Maven distribution in the current Wrapper's private HOME."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil


def wrapper_hash(url):
    value = 0
    for byte in url.encode("ascii"):
        value = (value * 31 + byte) % 4294967296
    return format(value, "x")


def seed(spec, workspace):
    if spec.get("schema_version") != "reviewbench-timefold-wrapper/v1":
        raise ValueError("unknown Maven Wrapper specification")
    workspace = workspace.resolve()
    for name, expected in spec["wrapper_files"].items():
        path = workspace / name
        if (path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(workspace)
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected):
            raise ValueError("candidate Wrapper declaration changed: " + name)
    source, home = Path(spec["source"]), Path(spec["home"])
    if source.is_symlink() or not source.is_dir() or not home.resolve().is_relative_to(workspace / ".reviewbench"):
        raise ValueError("Maven Wrapper inputs escaped their declared locations")
    destination = home / ".m2/wrapper/dists/apache-maven-3.9.1" / wrapper_hash(spec["original_distribution_url"])
    if destination.is_symlink() or not destination.resolve().is_relative_to(home.resolve()):
        raise ValueError("Maven Wrapper destination traverses a link")
    files = sorted(path for path in source.rglob("*") if path.is_file() or path.is_symlink())
    if not files or len(files) > 256 or sum(path.stat().st_size for path in files) > 32 * 1024**2:
        raise ValueError("Maven Wrapper source exceeds its input bound")
    if any(path.is_symlink() or not path.is_file() for path in files):
        raise ValueError("Maven Wrapper source contains a non-regular input")
    if destination.exists():
        names = {path.relative_to(source).as_posix() for path in files}
        present = {path.relative_to(destination).as_posix() for path in destination.rglob("*")
                   if path.is_file() or path.is_symlink()}
        if present != names:
            raise ValueError("existing Maven Wrapper tree differs")
        for path in files:
            other = destination / path.relative_to(source)
            if (other.is_symlink() or not other.is_file()
                    or path.read_bytes() != other.read_bytes()
                    or bool(path.stat().st_mode & 0o111) != bool(other.stat().st_mode & 0o111)):
                raise ValueError("existing Maven Wrapper input differs")
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination, symlinks=False)
    print("ORIGINAL_MAVEN_WRAPPER_READY version=3.9.1 files=" + str(len(files)), flush=True)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    args = parser.parse_args()
    seed(json.loads(args.spec.read_text()), Path.cwd())
