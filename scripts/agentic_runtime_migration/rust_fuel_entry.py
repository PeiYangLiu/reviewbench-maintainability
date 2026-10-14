#!/usr/bin/env python3
"""Fuel's full current-source mirror and original77-test repository command."""
from __future__ import annotations

import argparse
from collections import Counter
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import time

if __package__:
    from . import rust_image_entry as core, rust_libsbp_entry as inline
    from .rust_build_entry import synchronize_candidate, tomllib
else:
    import image_locks as core
    import rust_libsbp_entry as inline
    from build import synchronize_candidate, tomllib

SCHEMA = "reviewbench-published-legacy-fuel-original-command/v1"
PRIVATE_SCHEMA = "reviewbench-published-legacy-fuel-original-tests/v1"
TASK = "github__FuelLabs__fuel-tx__pull_197__comment_1003742605__complexity"
PUBLIC = ["build", "--locked", "-p", "fuel-tx", "--all-features", "--lib", "--message-format=json"]
COMMAND = ["test", "-p", "fuel-tx", "--all-features", "--", "--nocapture"]
TARGETS = {"test-bytes": "tests/bytes.rs", "test-display": "tests/display.rs",
           "test-offsets": "tests/offset.rs", "test-prepared-init": "tests/prepared_init.rs",
           "test-valid": "tests/valid.rs"}
PREFIX = "ReviewBench Fuel original command: "
SELECTION = "unique-full-current-locked-offline-metadata; never-test-outcomes"
EMPTY = {"manifests": [], "locks": [], "private_members": []}


def require(value, message):
    core.require(value, "original Fuel: " + message)


def fixed(row, *, content=False):
    path = Path(row["path"])
    require(set(row) == {"path", "sha256"} and path.is_absolute()
            and path.is_file() and not path.is_symlink() and core.file_digest(path) == row["sha256"],
            "fixed input changed")
    return core.bounded(path) if content else path


def validate(public, private=None):
    require(public.get("schema_version") == SCHEMA and public.get("task_id") == TASK
            and public.get("selection") == SELECTION and public.get("mirror_basename") == "fuel-tx"
            and public.get("public_command") == PUBLIC and public.get("original_lock_recovered") is False,
            "unbound original public contract")
    require(len(public["locks"]) == 2 and len({r["sha256"] for r in public["locks"]}) == 2,
            "both newly derived original-source locks are required")
    require(public["cpus"] == 16 and public["timeout_seconds"] == 3600,
            "original build jobs/timeout changed")
    require(0 < public["metadata_timeout_seconds"] <= 180
            and 0 < public["max_log_bytes"] <= 16 * 1024**2, "invalid evidence bounds")
    if private is not None:
        require(private.get("schema_version") == PRIVATE_SCHEMA and private.get("task_id") == TASK
                and private.get("command") == COMMAND and private.get("test_targets") == TARGETS
                and len(private["expected_tests"]) == len(set(private["expected_tests"])) == 77
                and len(private["inline_test_blocks"]) == 7, "original repository test contract changed")
        require(core.digest(json.dumps(private, sort_keys=True, ensure_ascii=False,
                                       separators=(",", ":")).encode()) == public["private_fingerprint"],
                "private contract changed")


def mismatch(row):
    if row["returncode"] != 101 or row.get("limit"):
        return False
    if core.catalog_lock_incompatible(row):
        return True
    expected = ("error: cannot update the lock file " + str(Path(row["cwd"]) / "Cargo.lock")
                + " because --locked was passed to prevent this\n"
                "help: to generate the lock file without accessing the network, remove the --locked flag and use --offline instead.\n")
    return row["stderr"] == expected


def verify_original_tests(mirror, private, metadata):
    bindings = inline.verify_blocks(mirror, private["inline_test_blocks"])
    for row in private["files"]:
        path = mirror / row["path"]
        require(path.is_file() and not path.is_symlink()
                and core.file_digest(path) == row["sha256"], "original test/snapshot file changed")
    roots = [p for p in metadata["packages"] if p.get("source") is None
             and Path(p["manifest_path"]).resolve() == (mirror / "Cargo.toml").resolve()]
    require(len(roots) == 1 and roots[0]["name"] == "fuel-tx", "current package redirected")
    for name, relative in TARGETS.items():
        targets = [t for t in roots[0]["targets"] if t["name"] == name and t["kind"] == ["test"]]
        require(len(targets) == 1 and Path(targets[0]["src_path"]).resolve() == (mirror / relative).resolve(),
                "original integration test missing/redirected")
    document = tomllib.loads((mirror / "Cargo.toml").read_text())
    require(not any(t.get("harness") is False for t in document.get("test", [])), "libtest harness disabled")
    libraries = [t for t in roots[0]["targets"] if "lib" in t["kind"]]
    require(len(libraries) == 1 and libraries[0].get("test") is True
            and Path(libraries[0]["src_path"]).resolve() == (mirror / "src/lib.rs").resolve(),
            "original library test target disabled or redirected")
    return bindings


def run_candidate(public, source, folder, private=None, *, run=core.invoke):
    validate(public, private)
    source, folder = Path(source).resolve(), Path(folder).resolve()
    evidence = {"schema_version": SCHEMA, "task_id": TASK, "role": "grading" if private else "public",
                "selection_uses_test_results": False, "new_lock_generation_commands": 0,
                "status": "preparing", "probes": [], "original_lock_recovered": False}
    mirror = folder / "fuel-tx"
    before = None
    try:
        require(not source.is_relative_to(folder) and not folder.is_relative_to(source), "source/cache overlap")
        for name in ("cargo", "rustc", "source_config"):
            fixed(public[name])
        before = core.snapshot(source)
        supplied = "Cargo.lock" in before
        require(not supplied or before["Cargo.lock"][0] == "file", "candidate lock redirected")
        locks = ([{"path": str(source / "Cargo.lock"), "sha256": before["Cargo.lock"][1]}]
                 if supplied else public["locks"])
        for row in locks:
            fixed(row)
        folder.mkdir(parents=True, exist_ok=True)
        home = folder / "cargo-home"
        require(not home.is_symlink(), "Cargo home redirected")
        home.mkdir(exist_ok=True)
        require(not (home / "config.toml").is_symlink(), "source config redirected")
        (home / "config.toml").write_bytes(fixed(public["source_config"], content=True))
        env = {**os.environ, "CARGO_HOME": str(home), "RUSTC": public["rustc"]["path"],
               "RUSTDOC": str(Path(public["rustc"]["path"]).with_name("rustdoc")),
               "PATH": str(Path(public["cargo"]["path"]).parent) + ":" + os.environ.get("PATH", ""),
               "CARGO_NET_OFFLINE": "true", "CARGO_NET_RETRY": "0", "SOURCE_ROOT": str(mirror),
               "CARGO_BUILD_JOBS": "16"}
        env.setdefault("CARGO_TARGET_DIR", str(folder / "target"))
        deadline, matches = time.monotonic() + public["timeout_seconds"], []
        evidence.update(source_before=before, candidate_lock_preserved=supplied)

        def install(lock):
            synchronize_candidate(source, mirror, EMPTY)
            if not supplied:
                (mirror / "Cargo.lock").write_bytes(fixed(lock, content=True))
            expected = {**before, "Cargo.lock": ["file", lock["sha256"],
                                                before.get("Cargo.lock", [None, None, False])[2]]}
            require(core.snapshot(mirror) == expected and core.snapshot(source) == before, "mirror changed candidate inputs")
            return expected

        for index, lock in enumerate(locks):
            expected = install(lock)
            command = [public["cargo"]["path"], "--offline", "metadata", "--locked", "--format-version", "1", "--all-features"]
            row = run(command, mirror, env, folder, f"metadata-{index}",
                      min(deadline, time.monotonic() + public["metadata_timeout_seconds"]), public["max_log_bytes"])
            item = {"lock_sha256": lock["sha256"], "command_result": row}
            evidence["probes"].append(item)
            core.complete(row)
            require(row["command"] == command and row["cwd"] == str(mirror), "metadata argv/cwd changed")
            require(core.snapshot(mirror) == expected and core.snapshot(source) == before, "metadata changed source/lock")
            if row["returncode"] == 0:
                graph = core.graph_check(row["stdout"], mirror, expected, fixed(lock, content=True))
                item["graph"] = graph
                matches.append((lock, graph, json.loads(row["stdout"])))
            else:
                require(mismatch(row), "metadata failed outside locked compatibility")
        require(len(matches) == 1, "no unique complete current-candidate graph")
        lock, graph, metadata = matches[0]
        expected = install(lock)
        evidence.update(selected_lock_sha256=lock["sha256"], graph=graph)
        if private:
            evidence["original_inline_tests"] = verify_original_tests(mirror, private, metadata)
        command = [public["cargo"]["path"], *(COMMAND if private else ["--offline", *PUBLIC])]
        row = run(command, mirror, env, folder, "original-tests" if private else "public-build",
                  deadline, public["max_log_bytes"])
        evidence["execution"] = row
        core.complete(row)
        require(row["command"] == command and row["cwd"] == str(mirror), "original argv/cwd changed")
        require(core.snapshot(source) == before and core.snapshot(mirror) == expected, "execution changed input files")
        if private:
            found = core.RESULT.findall(row["stdout"])
            counts, values = Counter(n for n, _ in found), dict(found)
            results = {n: bool(row["returncode"] == 0 and counts[n] == 1 and values.get(n) == "ok")
                       for n in private["expected_tests"]}
            evidence["test_results"] = results
            code = 0 if all(results.values()) else (row["returncode"] or 1)
        else:
            code = row["returncode"]
        evidence.update(status="completed", exit_status=code, command_executions=1)
    except (ValueError, OSError, KeyError, TypeError) as error:
        evidence.update(status="rejected", exit_status=2, error=type(error).__name__ + ": " + str(error))
    finally:
        if before is not None and mirror.exists():
            synchronize_candidate(source, mirror, EMPTY)
            evidence.update(source_after=core.snapshot(source),
                            source_unchanged=core.snapshot(source) == before,
                            mirror_restored=core.snapshot(mirror) == before)
            require(evidence["source_unchanged"] and evidence["mirror_restored"], "source/mirror restoration failed")
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--private", type=Path)
    args = parser.parse_args()
    public = json.loads(core.bounded(args.spec))
    private = json.loads(core.bounded(args.private)) if args.private else None
    require(args.task_id == TASK, "task changed")
    root = Path("/opt/reviewbench-cache/rust-build/fuel-original")
    root.mkdir(parents=True, exist_ok=True)
    with (root / "execution.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cpus = sorted(os.sched_getaffinity(0))
        offset = int(core.digest(TASK.encode())[:16], 16) % len(cpus)
        os.sched_setaffinity(0, {cpus[(offset + n) % len(cpus)] for n in range(min(len(cpus), public["cpus"]))})
        result = run_candidate(public, Path("/workspace"), root, private)
        if private and result["status"] == "completed":
            for name, passed in result["test_results"].items():
                print("test " + name + " ... " + ("ok" if passed else "FAILED"))
            print("test result: " + ("ok." if result["exit_status"] == 0 else "FAILED."))
        if result.get("error"):
            sys.stderr.write(result["error"] + "\n")
        print(PREFIX + json.dumps(result, sort_keys=True), flush=True)
        return result["exit_status"]


if __name__ == "__main__":
    raise SystemExit(main())
