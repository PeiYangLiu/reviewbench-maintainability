#!/usr/bin/env python3
"""Use the current project's public compiler declaration and complete Cargo graph."""
from __future__ import annotations

import argparse
import copy
import fcntl
import json
import os
from pathlib import Path
import sys

if __package__:
    from . import rust_image_entry as core
    from .rust_build_entry import tomllib
else:
    import image_locks as core
    from build import tomllib

SCHEMA = "reviewbench-original-project-rust-toolchains/v1"
VERSIONS = ("1.68.2", "nightly-2023-05-04")


def require(value, message):
    core.require(value, "project compiler: " + message)


def project_toolchain(root, default, *, cwd=None):
    """Read only public declarations, with the legacy file's Rustup precedence."""
    root = Path(root).resolve()
    current = Path(cwd).resolve() if cwd is not None else root
    require(current.is_relative_to(root), "working directory escaped current source")
    for directory in (current, *current.parents):
        if not directory.is_relative_to(root):
            break
        for name in ("rust-toolchain", "rust-toolchain.toml"):
            path = directory / name
            if not path.exists() and not path.is_symlink():
                continue
            real = path.resolve(strict=True)
            require(real.is_relative_to(root), "compiler declaration escaped current source")
            data = core.bounded(real)
            raw = data.decode().strip()
            if name.endswith(".toml") or raw.startswith("["):
                value = tomllib.loads(raw)
                channel = value["toolchain"]["channel"]
                require("path" not in value["toolchain"], "external compiler path is not provisioned")
            else:
                channel = raw
            require(channel in VERSIONS, "compiler is not one of the two original versions")
            return channel, {"path": path.relative_to(root).as_posix(), "sha256": core.digest(data),
                             "channel": channel}
    require(default == VERSIONS[0], "original no-declaration compiler changed")
    return default, {"path": None, "channel": default}


def selected_spec(spec, root, *, cwd=None, override=None):
    require(spec["schema_version"] == SCHEMA and spec["default_toolchain"] == VERSIONS[0]
            and set(spec["toolchains"]) == set(VERSIONS), "unbound original compiler inventory")
    if override is not None:
        require(override in VERSIONS, "explicit compiler is not provisioned")
        version, declaration = override, {"path": None, "channel": override, "explicit_override": True}
    else:
        version, declaration = project_toolchain(root, spec["default_toolchain"], cwd=cwd)
    tool = spec["toolchains"][version]
    for field in ("cargo", "rustc"):
        require(core.file_digest(Path(tool[field]["path"])) == tool[field]["sha256"],
                "original compiler binary changed")
    public = {**copy.deepcopy(spec["cargo_contract"]), "cargo": tool["cargo"], "rustc": tool["rustc"]}
    core.validate(public)
    require(public["task_id"] == spec["task_id"], "task binding changed")
    return version, declaration, public


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--private", type=Path)
    parser.add_argument("--forward", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    spec = json.loads(core.bounded(args.spec))
    require(args.task_id == spec["task_id"], "wrong task")
    root = Path("/workspace")
    try:
        values = list(args.forward or [])
        values = values[1:] if values[:1] == ["--"] else values
        override = values.pop(0)[1:] if values and values[0].startswith("+") else None
        if args.forward is not None and override is None:
            override = os.environ.get("RUSTUP_TOOLCHAIN")
            if override and override.endswith("-x86_64-unknown-linux-gnu"):
                override = override.removesuffix("-x86_64-unknown-linux-gnu")
        version, declaration, public = selected_spec(
            spec, root, cwd=Path.cwd() if args.forward is not None else root, override=override)
        if args.forward is not None:
            target = Path(os.environ.get("CARGO_TARGET_DIR", "/opt/reviewbench-cache/target")) / version
            environment = {**os.environ, "RUSTC": public["rustc"]["path"], "CARGO_TARGET_DIR": str(target),
                           "CARGO_NET_OFFLINE": "true"}
            os.execve(public["cargo"]["path"], [public["cargo"]["path"], *values], environment)
        private = json.loads(core.bounded(args.private)) if args.private else None
        core.validate(public, private)
        cache = Path("/opt/reviewbench-cache/rust-build/project-toolchains")
        cache.mkdir(parents=True, exist_ok=True)
        with (cache / "execution.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            os.environ["CARGO_TARGET_DIR"] = str(Path(os.environ["CARGO_TARGET_DIR"]) / version)
            cpus = sorted(os.sched_getaffinity(0))
            offset = int(core.digest(args.task_id.encode())[:16], 16) % len(cpus)
            os.sched_setaffinity(0, {cpus[(offset + n) % len(cpus)]
                                    for n in range(min(len(cpus), public["cpus"]))})
            evidence = core.run_candidate(public, root, cache / version, private)
            evidence.update(project_compiler=version, public_compiler_declaration=declaration,
                            compiler_selected_from_private_results=False)
    except (core.VariantError, OSError, KeyError, ValueError, TypeError) as error:
        evidence = getattr(error, "evidence", None) or {
            "status": "rejected", "exit_status": 2, "error": type(error).__name__ + ": " + str(error)}
    if "execution" in evidence:
        sys.stdout.write(evidence["execution"]["stdout"])
        sys.stderr.write(evidence["execution"]["stderr"])
    elif evidence.get("error"):
        sys.stderr.write(evidence["error"] + "\n")
    print(core.EVIDENCE_PREFIX + json.dumps(evidence, sort_keys=True), flush=True)
    return evidence["exit_status"]


if __name__ == "__main__":
    raise SystemExit(main())
