#!/usr/bin/env python3
"""Check recovered locks against a complete, unmodified current-candidate mirror.

No old manifest, production source, workspace member, or assertion is installed.
Cargo (not a root-manifest heuristic or --no-deps) validates the candidate graph.
Even a successful historical run does not waive the locked offline check.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

if __package__:
    from .rust_build_entry import OverlayError, candidate_files, relative_path, synchronize_candidate
else:  # Shipped beside the existing standalone build.py and its pinned TOML wheels.
    from build import OverlayError, candidate_files, relative_path, synchronize_candidate

MAX_METADATA_BYTES = 32 * 1024 * 1024
MAX_INPUT_BYTES = 16 * 1024 * 1024
METADATA_TIMEOUT = 120


def digest(data):
    return hashlib.sha256(data).hexdigest()


def input_bytes(path):
    with path.open("rb") as stream:
        data = stream.read(MAX_INPUT_BYTES + 1)
    if len(data) > MAX_INPUT_BYTES:
        raise OverlayError("Cargo lock/manifest exceeds its input bound: " + str(path))
    return data


def manifest_tree(source):
    result = {}
    for name, path in candidate_files(source).items():
        if Path(name).name == "Cargo.toml":
            if path.is_symlink() or not path.resolve().is_relative_to(source.resolve()):
                raise OverlayError("recovered Cargo lock has a non-regular candidate manifest: " + name)
            result[name] = digest(input_bytes(path))
    if not result or len(result) > 2048:
        raise OverlayError("Cargo recovery needs a bounded complete candidate manifest tree")
    return result


def lock_tree(source):
    result = {}
    for name, path in candidate_files(source).items():
        if Path(name).name == "Cargo.lock":
            if path.is_symlink() or not path.resolve().is_relative_to(source.resolve()):
                raise OverlayError("recovered Cargo lock has a non-regular candidate lock: " + name)
            result[name] = digest(input_bytes(path))
    return result


def locked_commands(command):
    """Keep the exact test selectors and libtest arguments; only pin Cargo resolution."""
    if not command or Path(command[0]).name != "cargo":
        raise OverlayError("recovered Rust lock entry requires the pinned Cargo")
    try:
        index = next(i for i, arg in enumerate(command) if arg in {"check", "test"})
    except StopIteration as exc:
        raise OverlayError("recovered Rust locks support only check/test commands") from exc
    end = command.index("--") if "--" in command else len(command)
    cargo_args = command[index + 1:end]
    metadata = [*command[:index], "metadata", "--format-version", "1", "--locked", "--offline"]
    i = 0
    while i < len(cargo_args):
        arg = cargo_args[i]
        if arg in {"--features", "-F", "--manifest-path", "--config"}:
            if i + 1 == len(cargo_args):
                raise OverlayError("missing Cargo option value")
            metadata.extend(cargo_args[i:i + 2])
            i += 1
        elif arg in {"--no-default-features", "--all-features"} or arg.startswith(
                ("--features=", "--manifest-path=", "--config=")):
            metadata.append(arg)
        i += 1
    execute = [*command[:end], *[flag for flag in ("--locked", "--offline")
                               if flag not in command[:end]], *command[end:]]
    return metadata, execute


def prepare_candidate(source, mirror, spec, task_id, cwd, command, *, lock_sources=None):
    recovery = spec.get("frozen_lock_recovery", {})
    if task_id != spec.get("task_id") or task_id != recovery.get("task_id"):
        raise OverlayError("recovered Rust lock has a wrong task binding")
    if spec.get("manifests") or spec.get("private_members"):
        raise OverlayError("frozen Rust lock recovery must preserve the full manifest tree")
    key = relative_path(cwd) if cwd != "." else "."
    if key not in recovery.get("entrypoints", {}):
        raise OverlayError("recovered Rust lock has an unbound Cargo working directory")
    lock_name = relative_path(recovery["entrypoints"][key])
    records = {record["path"]: record for record in spec.get("locks", [])}
    if len(records) != len(spec.get("locks", [])) or lock_name not in records:
        raise OverlayError("recovered Rust lock has an ambiguous/missing lock binding")
    # Check shipped bytes even when a candidate supplies its own lock. Candidate
    # locks themselves are copied unchanged, never replaced by this adapter.
    for record in records.values():
        if record.get("replace_if_sha256"):
            raise OverlayError("recovered Rust defaults must not overwrite candidate locks")
        asset = Path((lock_sources or {}).get(record["asset_target"], record["asset_target"]))
        if asset.is_symlink() or not asset.is_file() or digest(input_bytes(asset)) != record["sha256"]:
            raise OverlayError("recovered Rust lock asset hash changed")
    source = source.resolve()
    manifests = manifest_tree(source)
    original_locks = lock_tree(source)
    synchronized = synchronize_candidate(source, mirror, spec, lock_sources)
    mirror = mirror.resolve()
    if manifest_tree(mirror) != manifests:
        raise OverlayError("Rust lock recovery changed the candidate manifest tree")
    before = lock_tree(mirror)
    if lock_name not in before:
        raise OverlayError("candidate removed a required committed Cargo lock: " + lock_name)
    metadata, execute = locked_commands(command)
    environment = {**os.environ, "CARGO_NET_OFFLINE": "true", "SOURCE_ROOT": str(mirror)}
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(metadata, cwd=mirror / key, env=environment, stdout=stdout, stderr=stderr,
                                   start_new_session=True)
        try:
            process.wait(timeout=METADATA_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            # Cargo may have spawned a compiler/version probe. Bound the whole
            # preparation process group, not just its immediate parent.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise OverlayError("full-candidate Cargo lock compatibility exceeded 120 seconds") from exc
        size = stdout.tell()
        stderr.seek(max(0, stderr.tell() - 4000))
        error = stderr.read().decode(errors="replace")
        if process.returncode:
            raise OverlayError("retained Cargo lock is incompatible with the full candidate manifest tree "
                               "(--locked --offline), exit " + str(process.returncode) + ": " + error)
        if size > MAX_METADATA_BYTES:
            raise OverlayError("Cargo lock compatibility metadata exceeds its output bound")
        stdout.seek(0)
        output = stdout.read()
    if lock_tree(mirror) != before or manifest_tree(mirror) != manifests:
        raise OverlayError("Cargo metadata changed a frozen lock or candidate manifest")
    if manifest_tree(source) != manifests or lock_tree(source) != original_locks:
        raise OverlayError("Cargo lock compatibility changed the read-only candidate inputs")
    data = json.loads(output)
    workspace = Path(data["workspace_root"]).resolve()
    if (workspace / "Cargo.lock" != mirror / lock_name or data.get("resolve") is None
            or not data.get("workspace_members")):
        raise OverlayError("Cargo metadata did not validate the complete bound workspace resolution")
    for package in data.get("packages", []):
        if package.get("source") is None:
            path = Path(package["manifest_path"]).resolve()
            if not path.is_relative_to(mirror) or path.relative_to(mirror).as_posix() not in manifests:
                raise OverlayError("Cargo resolved a local package outside the full candidate manifest tree")
    report = {
        "task_id": task_id, "candidate_manifests": manifests, "locks_before": before, "locks_after": before,
        "injected_defaults": sorted(set(before) - set(original_locks)),
        "status": "compatible", "metadata_command": metadata, "metadata_exit_status": process.returncode,
        "metadata_stdout_sha256": digest(output), "workspace_members": data["workspace_members"],
        "synchronized": synchronized, "scoring_result": False,
        "reference_manifest_match": digest(json.dumps(
            manifests, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        ) in recovery.get("reference_manifest_trees", []),
    }
    return execute, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("/workspace"))
    parser.add_argument("--build-root", type=Path, default=Path("/opt/reviewbench-cache/rust-build"))
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--cwd", default=".")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    args.build_root.mkdir(parents=True, exist_ok=True)
    with (args.build_root / "build.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        report_path = args.build_root / "lock-compatibility.json"
        report_path.unlink(missing_ok=True)
        try:
            spec = json.loads(args.spec.read_text())
            mirror = args.build_root / "source"
            if hasattr(os, "sched_getaffinity"):
                cpus = spec.get("cpus", 4)
                if type(cpus) is not int or cpus < 1 or cpus > 4:
                    raise OverlayError("invalid frozen Cargo CPU bound")
                available = sorted(os.sched_getaffinity(0))
                offset = int.from_bytes(hashlib.sha256(args.task_id.encode()).digest()[:8], "big") % len(available)
                os.sched_setaffinity(0, {available[(offset + i) % len(available)]
                                        for i in range(min(cpus, len(available)))})
            execute, report = prepare_candidate(args.source, mirror, spec, args.task_id, args.cwd, command)
            report_path.write_text(json.dumps(report, sort_keys=True) + "\n")
            print("ReviewBench Cargo lock compatibility: locked offline full-candidate resolution", flush=True)
            result = subprocess.run(execute, cwd=mirror / args.cwd,
                                    env={**os.environ, "CARGO_NET_OFFLINE": "true", "SOURCE_ROOT": str(mirror)})
            if lock_tree(mirror) != report["locks_after"] or manifest_tree(mirror) != report["candidate_manifests"]:
                raise OverlayError("Cargo command changed a frozen lock or candidate manifest")
            return result.returncode
        except (OverlayError, OSError, ValueError, KeyError) as exc:
            report_path.write_text(json.dumps({
                "task_id": args.task_id, "status": "rejected", "gap": str(exc), "scoring_result": False,
            }, sort_keys=True) + "\n")
            print("Cargo frozen lock recovery: " + str(exc), file=sys.stderr)
            return 2


if __name__ == "__main__":
    raise SystemExit(main())
