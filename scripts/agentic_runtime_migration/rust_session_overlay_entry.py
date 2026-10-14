#!/usr/bin/env python3
"""Original symmetric build overlays plus exact locked private-session graphs."""
from __future__ import annotations

import argparse
from collections import Counter
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

if __package__:
    from . import rust_image_entry as core
    from .rust_build_entry import (
        OverlayError, overlay_files, relative_path, synchronize_candidate, tomllib,
    )
else:
    import image_locks as core
    from build import OverlayError, overlay_files, relative_path, synchronize_candidate, tomllib

SCHEMA = "reviewbench-original-cargo-session-overlay/v1"
PRIVATE_SCHEMA = "reviewbench-original-cargo-session-overlay-private/v1"
EVIDENCE_PREFIX = "ReviewBench original Cargo session overlay: "


def require(value, message):
    core.require(value, "original session overlay: " + message)


def validate(public, private=None):
    require(public.get("schema_version") == SCHEMA and public.get("selection") == core.SELECTION,
            "unbound schema or selection policy")
    require(isinstance(public.get("task_id"), str) and public["task_id"], "missing task")
    for field in ("cargo", "rustc", "source_config", "overlay"):
        row = public[field]
        require(set(row) == {"path", "sha256"} and Path(row["path"]).is_absolute()
                and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]), "unbound " + field)
    require(Path(public["cargo"]["path"]).name == "cargo" and Path(public["rustc"]["path"]).name == "rustc",
            "wrong compiler paths")
    locks = public["locks"]
    require(len(locks) == 2 and len({r["sha256"] for r in locks}) == 2
            and len({r["path"] for r in locks}) == 2, "need two distinct original locks")
    for row in locks:
        require(set(row) == {"path", "sha256"} and Path(row["path"]).is_absolute()
                and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]), "unbound original lock")
    session = public["session"]
    require(set(session) == {"manifest", "package", "version"}, "private package declaration differs")
    manifest = relative_path(session["manifest"])
    require(manifest.endswith("/Cargo.toml") and manifest != "Cargo.toml"
            and re.fullmatch(r"[A-Za-z0-9_-]+", session["package"])
            and re.fullmatch(r"[0-9A-Za-z.+_-]+", session["version"]), "invalid private workspace package")
    command = public["public_command"]
    require(command[:2] == ["check", "--locked"] and command.count("-p") == 1
            and command.count("--features") == 1 and len(command) == 6
            and re.fullmatch(r"[A-Za-z0-9_-]+", command[3])
            and re.fullmatch(r"[A-Za-z0-9_,-]+", command[5]), "public package/features changed")
    for name, low, high in (("cpus", 1, 4), ("timeout_seconds", 1, 2400),
                           ("metadata_timeout_seconds", 1, 120), ("max_log_bytes", 1024, 16 * 1024**2)):
        require(type(public.get(name)) is int and low <= public[name] <= high, "invalid bound: " + name)
    if private is None:
        return
    require(private.get("schema_version") == PRIVATE_SCHEMA
            and private.get("task_id") == public["task_id"], "private task/schema changed")
    prefix = Path(manifest).parent
    files = private["session_files"]
    require(isinstance(files, list) and len(files) >= 2
            and len({f["path"] for f in files}) == len(files), "invalid private file inventory")
    for row in files:
        require(set(row) == {"path", "sha256"} and Path(relative_path(row["path"])).is_relative_to(prefix)
                and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]), "private file escapes its session package")
    required = {manifest, (prefix / "src/lib.rs").as_posix()}
    require(required <= {f["path"] for f in files}, "original session manifest/libtest missing")
    require(private["command"] == ["test", "--locked", "-p", session["package"], "--", "--nocapture"],
            "original locked private test command changed")
    require(private["expected_tests"] and len(set(private["expected_tests"])) == len(private["expected_tests"])
            and all(re.fullmatch(r"[A-Za-z0-9_:]+", n) for n in private["expected_tests"]),
            "fixed private test identities changed")


def load_overlay(public):
    data = core.bounded(Path(public["overlay"]["path"]))
    require(core.digest(data) == public["overlay"]["sha256"], "fixed overlay changed")
    overlay = json.loads(data)
    require(overlay["schema_version"] == "reviewbench-cargo-build-overlay/v1"
            and overlay["task_id"] == public["task_id"] and overlay["locks"] == []
            and overlay["private_members"] == [str(Path(public["session"]["manifest"]).parent)],
            "overlay acquired lock/default/private-member changes")
    require(len({r["path"] for r in overlay["manifests"]}) == len(overlay["manifests"]),
            "ambiguous manifest overlays")
    for row in overlay["manifests"]:
        require(Path(relative_path(row["path"])).name == "Cargo.toml", "overlay touches non-manifest source")
        for operation in row["operations"]:
            key = operation.get("key", [])
            require(key and (key == ["workspace", "members"] or key[0] == "dev-dependencies"),
                    "session overlay touches production dependency/API declarations")
    return overlay


def session_target(graph, mirror, public, private):
    manifest = (mirror / public["session"]["manifest"]).resolve()
    owners = [p for p in graph["packages"] if p.get("source") is None
              and p["name"] == public["session"]["package"]]
    require(len(owners) == 1 and Path(owners[0]["manifest_path"]).resolve() == manifest
            and owners[0]["id"] in graph["workspace_members"], "original session package is missing/redirected")
    targets = [t for t in owners[0]["targets"] if t["kind"] == ["lib"]]
    expected = (manifest.parent / "src/lib.rs").resolve()
    require(len(targets) == 1 and Path(targets[0]["src_path"]).resolve() == expected
            and targets[0].get("test") is True, "original private libtest target disabled/redirected")
    value = tomllib.loads(core.bounded(manifest).decode())
    require(value.get("lib", {}).get("test", True) is True
            and value.get("lib", {}).get("harness", True) is True, "original libtest disabled")


def run_candidate(public, source, folder, private=None, *, run=core.invoke):
    validate(public, private)
    source, folder = Path(source).resolve(), Path(folder).resolve()
    require(not source.is_relative_to(folder) and not folder.is_relative_to(source),
            "source and build cache must be separate")
    evidence = {"schema_version": SCHEMA, "task_id": public["task_id"], "status": "preparing",
                "role": "grading" if private else "public", "probes": [], "command_executions": 0,
                "selection_uses_test_results": False, "new_lock_generation_commands": 0}
    try:
        for row in [public[k] for k in ("cargo", "rustc", "source_config", "overlay")] + public["locks"]:
            path = Path(row["path"])
            require(path.is_file() and not path.is_symlink()
                    and core.file_digest(path) == row["sha256"], "fixed input changed")
        before = core.snapshot(source)
        evidence["source_before"] = before
        manifest_name = public["session"]["manifest"]
        if private:
            for row in private["session_files"]:
                require(core.digest(core.bounded(source / row["path"])) == row["sha256"],
                        "original session assertion/manifest/lock changed")
        else:
            require(not (source / manifest_name).exists(), "private session package appeared in developer source")
        overlay = load_overlay(public)
        folder.mkdir(parents=True, exist_ok=True)
        mirror = folder / "source"
        evidence["synchronized"] = synchronize_candidate(source, mirror, overlay)
        transformed = overlay_files(source, overlay)
        expected_overlay = dict(before)
        for name, content in transformed.items():
            expected_overlay[name] = ["file", core.digest(content), before[name][2]]
        require(core.snapshot(mirror) == expected_overlay, "mirror changed more than the original build overlay")
        evidence["applied_manifest_paths"] = sorted(transformed)
        owned = "Cargo.lock" in before
        if owned:
            require(before["Cargo.lock"][0] == "file", "candidate lock is not regular")
        candidates = ([{"path": str(source / "Cargo.lock"), "sha256": before["Cargo.lock"][1]}]
                      if owned else sorted(public["locks"], key=lambda row: row["sha256"]))
        evidence["candidate_lock_supplied"] = owned
        home = folder / "cargo-home"
        require(not home.is_symlink(), "Cargo home escaped build cache")
        home.mkdir(exist_ok=True)
        target = home / "config.toml"
        require(not target.is_symlink(), "Cargo config escaped build cache")
        target.write_bytes(core.bounded(Path(public["source_config"]["path"])))
        prefix = [public["cargo"]["path"], "--offline"]
        command = [*prefix, "metadata", "--format-version", "1", "--locked", "--all-features"]
        env = {**os.environ, "RUSTC": public["rustc"]["path"], "CARGO_HOME": str(home),
               "CARGO_NET_OFFLINE": "true", "CARGO_NET_RETRY": "0", "SOURCE_ROOT": str(mirror)}
        deadline = time.monotonic() + public["timeout_seconds"]
        compatible = []
        for index, lock in enumerate(candidates):
            data = core.bounded(Path(lock["path"]))
            require(core.digest(data) == lock["sha256"], "candidate/original lock changed")
            if not owned:
                (mirror / "Cargo.lock").write_bytes(data)
            expected = {**expected_overlay, "Cargo.lock": ["file", lock["sha256"],
                        before["Cargo.lock"][2] if owned else False]}
            require(core.snapshot(mirror) == expected, "lock probe changed current candidate")
            result = run(command, mirror, env, folder, "metadata-" + str(index),
                         min(deadline, time.monotonic() + public["metadata_timeout_seconds"]),
                         public["max_log_bytes"])
            observed = {"lock_sha256": lock["sha256"], "command_result": result}
            evidence["probes"].append(observed)
            core.complete(result)
            require(result["command"] == command and result["cwd"] == str(mirror), "metadata argv/cwd changed")
            require(core.snapshot(source) == before and core.snapshot(mirror) == expected,
                    "metadata changed original source/lock/overlay")
            if result["returncode"] == 0:
                omitted = None if private else (public["session"]["package"], public["session"]["version"], None)
                observed["graph"] = core.graph_check(result["stdout"], mirror, expected_overlay, data,
                                                      omitted_workspace_package=omitted)
                if private:
                    session_target(json.loads(result["stdout"]), mirror, public, private)
                compatible.append((lock, data, observed["graph"]))
            else:
                require(core.lock_incompatible(result, original_locked=True),
                        "metadata failed outside original lock compatibility")
                observed["compatible"] = False
        require(len(compatible) == 1, "no unique compatible original lock: " + str(len(compatible)))
        chosen, data, graph = compatible[0]
        if not owned:
            (mirror / "Cargo.lock").write_bytes(data)
        expected = {**expected_overlay, "Cargo.lock": ["file", chosen["sha256"],
                    before["Cargo.lock"][2] if owned else False]}
        require(core.snapshot(mirror) == expected, "selected lock changed current candidate")
        execute = [*prefix, *(private["command"] if private else public["public_command"])]
        result = run(execute, mirror, env, folder, "execution", deadline, public["max_log_bytes"])
        evidence.update(command_executions=1, execution=result, selected_lock_sha256=chosen["sha256"],
                        graph=graph, candidate_lock_preserved=owned, mirror_before=expected)
        core.complete(result)
        require(result["command"] == execute and result["cwd"] == str(mirror), "original argv/cwd changed")
        after, mirror_after = core.snapshot(source), core.snapshot(mirror)
        require(after == before and mirror_after == expected, "original command changed current source/configuration")
        evidence.update(source_after=after, mirror_after=mirror_after, source_unchanged=True, mirror_unchanged=True)
        if private:
            found = core.RESULT.findall(result["stdout"])
            counts, values = Counter(n for n, _ in found), dict(found)
            finished = "test result: ok." in result["stdout"] or "test result: FAILED." in result["stdout"]
            evidence["test_results"] = {
                n: bool(result["returncode"] == 0 and finished and counts[n] == 1 and values.get(n) == "ok")
                for n in private["expected_tests"]}
        evidence.update(status="completed", exit_status=result["returncode"])
        return evidence
    except (OverlayError, OSError, ValueError, KeyError, TypeError) as error:
        evidence.update(status="rejected", exit_status=2, error=type(error).__name__ + ": " + str(error))
        raise core.VariantError(str(error), evidence) from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--private", type=Path)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--source", type=Path, default=Path("/workspace"))
    parser.add_argument("--build-root", type=Path, default=Path("/opt/reviewbench-cache/rust-build"))
    args = parser.parse_args()
    public = json.loads(core.bounded(args.spec))
    private = json.loads(core.bounded(args.private)) if args.private else None
    validate(public, private)
    require(public["task_id"] == args.task_id, "wrong task")
    args.build_root.mkdir(parents=True, exist_ok=True)
    with (args.build_root / "session-overlay.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if hasattr(os, "sched_getaffinity"):
            cpus = sorted(os.sched_getaffinity(0))
            offset = int.from_bytes(hashlib.sha256(args.task_id.encode()).digest()[:8], "big") % len(cpus)
            os.sched_setaffinity(0, {cpus[(offset + n) % len(cpus)] for n in range(min(len(cpus), public["cpus"]))})
        try:
            evidence = run_candidate(public, args.source, args.build_root / "session-overlay", private)
        except core.VariantError as error:
            evidence = error.evidence or {"status": "rejected", "exit_status": 2, "error": str(error)}
        if "execution" in evidence:
            sys.stdout.write(evidence["execution"]["stdout"])
            sys.stderr.write(evidence["execution"]["stderr"])
        (args.build_root / "session-overlay-evidence.json").write_text(json.dumps(evidence, sort_keys=True) + "\n")
        print(EVIDENCE_PREFIX + json.dumps(evidence, sort_keys=True), flush=True)
        return evidence["exit_status"]


if __name__ == "__main__":
    raise SystemExit(main())
