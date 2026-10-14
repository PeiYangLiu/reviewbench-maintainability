#!/usr/bin/env python3
"""Run a hash-bound original Maven CLI script with only its tool path relocated."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess

ORIGINAL_MAVEN = "/oracle-tools/maven/bin/mvn"


def absolute_path(value):
    if (not isinstance(value, str) or not value.startswith("/")
            or any(char in value for char in "\0\r\n")
            or ".." in PurePosixPath(value).parts):
        raise ValueError("CLI transport needs an absolute literal tool/cache path")
    return value


def relocated_script(source, expected_sha256, maven, repository):
    if (not isinstance(source, bytes) or not isinstance(expected_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
            or hashlib.sha256(source).hexdigest() != expected_sha256):
        raise ValueError("original Maven CLI script differs from its bound bytes")
    text = source.decode("utf-8")
    if text.count(ORIGINAL_MAVEN) != 1:
        raise ValueError("original CLI needs exactly one declared Maven invocation")
    replacement = shlex.join([
        absolute_path(maven), "-o", "-Dmaven.repo.local=" + absolute_path(repository),
    ])
    return text.replace(ORIGINAL_MAVEN, replacement, 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    args = parser.parse_args()
    spec = json.loads(args.spec.read_text())
    if set(spec) != {"original_script", "original_sha256", "maven", "repository"}:
        raise ValueError("original CLI transport spec has unexpected fields")
    path = Path(spec["original_script"])
    if not path.is_file() or path.is_symlink():
        raise ValueError("original CLI script must be a regular readonly asset")
    source = path.read_bytes()
    command = relocated_script(source, spec["original_sha256"], spec["maven"], spec["repository"])
    # The original shell performs the complete build, actual java -jar invocation,
    # original output assertions and report creation. No assertion is replaced.
    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", command])
    if path.read_bytes() != source:
        raise ValueError("original CLI asset changed during execution")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
