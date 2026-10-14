#!/usr/bin/env python3
"""Full current candidate plus fixed recursive native source, original gRPC tests."""
from __future__ import annotations

import argparse
from collections import Counter
import copy
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import time

if __package__:
    from . import rust_image_entry as core
    from .rust_build_entry import synchronize_candidate, tomllib
else:
    import image_locks as core
    from build import synchronize_candidate, tomllib

SCHEMA = "reviewbench-original-grpc-recursive-runtime/v1"
PREFIX = "ReviewBench original gRPC inputs: "


def require(value, message):
    core.require(value, "original gRPC: " + message)


def digest_object(value):
    return core.digest(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode())


def checked_path(row):
    path = Path(row["path"])
    require(path.is_absolute() and path.is_file() and not path.is_symlink()
            and core.file_digest(path) == row["sha256"], "fixed compiler/config/lock changed")
    return path


def fixed_text(row):
    return core.bounded(checked_path(row))


def snapshot(root, native):
    """Hash every file; compact only the emitted immutable native inventory."""
    full = core.snapshot(root)
    prefix = native["path"] + "/"
    dependencies = {name.removeprefix(prefix): value for name, value in full.items() if name.startswith(prefix)}
    require(len(dependencies) == native["files"] and digest_object(dependencies) == native["snapshot_sha256"],
            "original recursive native source changed or is incomplete")
    project = {name: value for name, value in full.items() if not name.startswith(prefix)}
    return {"project": project, "native": {"path": native["path"], "commit": native["commit"],
            "files": len(dependencies), "snapshot_sha256": native["snapshot_sha256"]}}, full


def validate(public, private=None):
    require(public["schema_version"] == SCHEMA and public["package"] == "interop"
            and public["package_manifest"] == "interop/Cargo.toml", "unbound package")
    require(public["public_command"] == ["check", "--locked", "-p", "interop", "--message-format=json"],
            "public command changed")
    require(public["metadata_reader_role"] == "complete-locked-graph-only",
            "metadata reader acquired a build/test role")
    for key in ("cargo", "rustc", "metadata_cargo", "source_config", "lock"):
        row = public[key]
        require(set(row) == {"path", "sha256"} and Path(row["path"]).is_absolute()
                and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]), "unbound " + key)
    require(Path(public["cargo"]["path"]).name == Path(public["metadata_cargo"]["path"]).name == "cargo"
            and Path(public["rustc"]["path"]).name == "rustc", "wrong fixed compiler basename")
    require(public["native"]["path"] == "grpc-sys/grpc" and public["native"]["files"] > 0
            and re.fullmatch(r"[0-9a-f]{64}", public["native"]["snapshot_sha256"])
            and re.fullmatch(r"[0-9a-f]{40}", public["native"]["commit"]), "unbound native source")
    for name, maximum in (("cpus", 16), ("timeout_seconds", 3600), ("metadata_timeout_seconds", 180),
                          ("max_log_bytes", 32 * 1024**2)):
        require(type(public[name]) is int and 0 < public[name] <= maximum, "invalid resource limit")
    if private:
        require(private["task_id"] == public["task_id"] and private["command"] == [
            "test", "-p", "interop", "--test", "tests", "--", "--test-threads=1"], "original test argv changed")
        require(private["harness"] == "interop/tests/tests.rs" and private["harness"] in private["files"],
                "original existing harness missing")
        require(private["expected_tests"] and len(set(private["expected_tests"])) == len(private["expected_tests"])
                and all(re.fullmatch(r"[A-Za-z0-9_:]+", n) for n in private["expected_tests"]),
                "original test identities changed")
        require(all(name.startswith("interop/tests/") and ".." not in Path(name).parts
                    for name in private["files"]), "unbound existing test closure")


def verify_tests(source, private):
    for name, expected in private["files"].items():
        path = source / name
        require(path.is_file() and not path.is_symlink() and core.file_digest(path) == expected,
                "original existing test bytes changed")


def check_selected_target(metadata, mirror, private):
    graph = json.loads(metadata)
    packages = [p for p in graph["packages"] if p.get("source") is None and p["name"] == "interop"
                and Path(p["manifest_path"]).resolve() == (mirror / "interop/Cargo.toml").resolve()]
    require(len(packages) == 1, "selected current package changed")
    targets = [t for t in packages[0]["targets"] if t["name"] == "tests" and t["kind"] == ["test"]]
    require(len(targets) == 1 and Path(targets[0]["src_path"]).resolve() ==
            (mirror / private["harness"]).resolve(), "original selected integration target changed")
    doc = tomllib.loads(core.bounded(mirror / "interop/Cargo.toml").decode())
    require(not any(t.get("name") == "tests" and t.get("harness") is False for t in doc.get("test", [])),
            "original libtest harness disabled")


def run_candidate(public, source, folder, private=None, *, run=core.invoke):
    validate(public, private)
    source, folder = Path(source).resolve(), Path(folder).resolve()
    require(not source.is_relative_to(folder) and not folder.is_relative_to(source), "source/cache overlap")
    result = {"schema_version": SCHEMA, "task_id": public["task_id"], "status": "preparing",
              "role": "grading" if private else "public", "new_lock_generation_commands": 0,
              "selection_uses_test_results": False, "probes": [], "public_compiles": []}
    try:
        for key in ("cargo", "rustc", "metadata_cargo"):
            checked_path(public[key])
        fixed_text(public["source_config"])
        fixed_text(public["lock"])
        before, full_before = snapshot(source, public["native"])
        result["source_before"] = before
        if private:
            verify_tests(source, private)
        owned = "Cargo.lock" in before["project"]
        if owned:
            require(before["project"]["Cargo.lock"][0] == "file", "candidate lock redirected")
        lock = ({"path": str(source / "Cargo.lock"), "sha256": before["project"]["Cargo.lock"][1]}
                if owned else public["lock"])
        data = fixed_text(lock)
        folder.mkdir(parents=True, exist_ok=True)
        home, mirror = folder / "cargo-home", folder / "source"
        require(not home.is_symlink(), "Cargo home redirected")
        home.mkdir(exist_ok=True)
        require(not (home / "config.toml").is_symlink(), "source config redirected")
        (home / "config.toml").write_bytes(fixed_text(public["source_config"]))
        synchronize_candidate(source, mirror, {"manifests": [], "locks": [], "private_members": []})
        if not owned:
            (mirror / "Cargo.lock").write_bytes(data)
        expected = copy.deepcopy(before)
        expected["project"]["Cargo.lock"] = ["file", lock["sha256"],
                                            before["project"].get("Cargo.lock", ["", "", False])[2]]
        mirror_before, mirror_full = snapshot(mirror, public["native"])
        require(mirror_before == expected and snapshot(source, public["native"])[0] == before,
                "mirror changed current source or manifests")
        env = {**os.environ, "CARGO_HOME": str(home), "RUSTC": public["rustc"]["path"],
               "PATH": str(Path(public["cargo"]["path"]).parent) + ":" + os.environ.get("PATH", ""),
               "CARGO_NET_OFFLINE": "true", "CARGO_NET_RETRY": "0",
               "CARGO_BUILD_JOBS": str(public["cpus"])}
        deadline = time.monotonic() + public["timeout_seconds"]
        result.update(candidate_lock_preserved=owned, selected_lock_sha256=lock["sha256"],
                      native_source_verified=True, metadata_reader=public["metadata_cargo"],
                      original_test_cargo=public["cargo"], original_rustc=public["rustc"])

        def invoke(command, label, end):
            row = run(command, mirror, env, folder, label, min(deadline, end), public["max_log_bytes"])
            result["last_command"] = row
            core.complete(row)
            require(row["command"] == command and row["cwd"] == str(mirror), "command/cwd changed")
            require(snapshot(source, public["native"])[0] == before
                    and snapshot(mirror, public["native"])[0] == expected, "command changed source or lock")
            return row

        command = [public["metadata_cargo"]["path"], "--offline", "metadata", "--locked",
                   "--format-version", "1", "--all-features"]
        row = invoke(command, "full-metadata", time.monotonic() + public["metadata_timeout_seconds"])
        result["probes"].append({"lock_sha256": lock["sha256"], "command_result": row})
        require(row["returncode"] == 0, "complete locked candidate graph failed")
        result["graph"] = core.graph_check(row["stdout"], mirror, mirror_full, data)
        if private:
            check_selected_target(row["stdout"], mirror, private)
        command = [public["cargo"]["path"], *private["command"]] if private else [
            public["cargo"]["path"], "--offline", *public["public_command"]]
        execution = invoke(command, "original-tests" if private else "public-check", deadline)
        if private:
            verify_tests(source, private)
            verify_tests(mirror, private)
            found = core.RESULT.findall(execution["stdout"])
            counts, statuses = Counter(n for n, _ in found), dict(found)
            tests = {name: bool(execution["returncode"] == 0 and counts[name] == 1
                               and statuses.get(name) == "ok" and "test result: ok." in execution["stdout"])
                     for name in private["expected_tests"]}
        else:
            result["public_compiles"].append({"command_result": execution})
            tests = {}
        result.pop("last_command", None)
        result.update(status="completed", execution=execution, test_results=tests,
                      source_after=snapshot(source, public["native"])[0], source_unchanged=True,
                      mirror_after=snapshot(mirror, public["native"])[0],
                      exit_status=0 if execution["returncode"] == 0 and (not private or all(tests.values())) else 1)
        return result
    except (ValueError, OSError, KeyError, TypeError) as error:
        result.update(status="rejected", exit_status=2, error=type(error).__name__ + ": " + str(error))
        raise core.VariantError(str(error), result) from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--private", type=Path)
    parser.add_argument("--source", type=Path, default=Path("/workspace"))
    parser.add_argument("--task-id", required=True)
    args = parser.parse_args()
    public = json.loads(args.spec.read_text())
    private = json.loads(args.private.read_text()) if args.private else None
    require(public["task_id"] == args.task_id, "task changed")
    folder = Path("/opt/reviewbench-cache/rust-build") / ("grpc-" + core.digest(args.task_id.encode())[:20])
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "command.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            result = run_candidate(public, args.source, folder, private)
        except core.VariantError as error:
            result = error.evidence
        print(PREFIX + json.dumps(result, sort_keys=True), flush=True)
        for name, passed in result.get("test_results", {}).items():
            print("test " + name + " ... " + ("ok" if passed else "FAILED"), flush=True)
        return result["exit_status"]


if __name__ == "__main__":
    raise SystemExit(main())
