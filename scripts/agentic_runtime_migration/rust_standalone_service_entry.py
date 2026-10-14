#!/usr/bin/env python3
"""Full current-candidate public/private locks with the original Redis test lifecycle."""
from __future__ import annotations

import argparse
from collections import Counter
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
import time

if __package__:
    from . import rust_image_entry as core
    from .rust_build_entry import OverlayError, relative_path, synchronize_candidate, tomllib
else:
    import image_locks as core
    from build import OverlayError, relative_path, synchronize_candidate, tomllib

SCHEMA = "reviewbench-original-standalone-rust-service/v1"
PRIVATE_SCHEMA = "reviewbench-original-standalone-rust-service-private/v1"
EVIDENCE_PREFIX = "ReviewBench original standalone service: "


def require(value, message):
    core.require(value, "original standalone service: " + message)


def validate(public, private=None):
    require(public.get("schema_version") == SCHEMA and public.get("selection") == core.SELECTION,
            "schema/selection changed")
    require(isinstance(public.get("task_id"), str) and public["task_id"], "missing task")
    for name in ("cargo", "rustc", "source_config"):
        row = public[name]
        require(set(row) == {"path", "sha256"} and Path(row["path"]).is_absolute()
                and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]), "unbound " + name)
    require(Path(public["cargo"]["path"]).name == "cargo" and Path(public["rustc"]["path"]).name == "rustc",
            "wrong compiler paths")
    require(re.fullmatch(r"1\.[0-9]+\.[0-9]+", public["toolchain"]) is not None, "unbound compiler version")
    for role in ("public", "private"):
        locks = public["locks"][role]
        require(len(locks) == 2 and len({p["sha256"] for p in locks}) == 2
                and len({p["path"] for p in locks}) == 2, "need distinct " + role + " locks")
        for row in locks:
            require(set(row) == {"path", "sha256"} and Path(row["path"]).is_absolute()
                    and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]), "unbound lock")
    require(public["public_seed_sha256"] in {p["sha256"] for p in public["locks"]["public"]},
            "original public seed is unbound")
    cwd = relative_path(public["private_workdir"])
    require(cwd != "." and not cwd.startswith(".") and Path(cwd).name, "private workspace root changed")
    command = public["public_command"]
    require(command[:2] == ["check", "--locked"] and command.count("-p") >= 1
            and command.count("--features") == 1 and not any(x in command for x in ("--no-run", "--test", "--all")),
            "public command is not an explicit locked production build")
    for key, low, high in (("cpus", 1, 4), ("timeout_seconds", 1, 3600),
                           ("metadata_timeout_seconds", 1, 120), ("max_log_bytes", 1024, 16 * 1024**2)):
        require(type(public.get(key)) is int and low <= public[key] <= high, "invalid bound: " + key)
    if private is None:
        return
    require(private.get("schema_version") == PRIVATE_SCHEMA and private.get("task_id") == public["task_id"],
            "private identity changed")
    files = private["session_files"]
    require(len(files) == 2 and len({p["path"] for p in files}) == 2, "private file inventory changed")
    for row in files:
        require(set(row) == {"path", "sha256"} and Path(relative_path(row["path"])).is_relative_to(Path(cwd))
                and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]), "private file is not bound to its workspace")
    require(cwd + "/Cargo.toml" in {p["path"] for p in files}, "private manifest missing")
    require(private["test_path"].startswith(cwd + "/tests/") and private["test_path"].endswith(".rs")
            and private["test_path"] in {p["path"] for p in files}, "private test target changed")
    require(private["test_command"][:2] == ["test", "--locked"]
            and private["test_command"].count("--test") == 1
            and private["test_command"][private["test_command"].index("--test") + 1]
                == Path(private["test_path"]).stem
            and "--no-run" not in private["test_command"] and "--list" not in private["test_command"],
            "private original Cargo test command changed")
    shell = private["original_shell"]
    require(core.digest(shell.encode()) == private["original_shell_sha256"]
            and shell.count("cargo +" + public["toolchain"]) == 1,
            "original shell binding changed")
    original_cargo = "cargo +" + public["toolchain"] + " " + shlex.join(private["test_command"])
    require(original_cargo in shell, "original shell lost its exact Cargo command")
    require(shell.startswith("redis-server --save '' --appendonly no --port 6379 --bind 127.0.0.1 ")
            and 'kill "$redis_pid"' in shell and 'wait "$redis_pid"' in shell
            and 'exit "$rc"' in shell, "original Redis lifecycle changed")
    require(private["expected_tests"] and all(re.fullmatch(r"[A-Za-z0-9_:]+", n)
                                             for n in private["expected_tests"]), "invalid test identities")
    require(set(private["service_binaries"]) == {"redis-server", "redis-cli"}, "service binaries changed")
    for name, row in private["service_binaries"].items():
        require(set(row) == {"path", "sha256"} and Path(row["path"]).is_absolute()
                and Path(row["path"]).name == name and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]),
                "service executable is not pinned")


def private_target(graph, mirror, public, private):
    cwd = mirror / public["private_workdir"]
    manifest = cwd / "Cargo.toml"
    matches = [p for p in graph["packages"] if p.get("source") is None
               and Path(p["manifest_path"]).resolve() == manifest.resolve()]
    require(len(matches) == 1 and matches[0]["id"] in graph["workspace_members"],
            "private standalone package is absent or redirected")
    targets = [t for t in matches[0]["targets"] if t["name"] == Path(private["test_path"]).stem
               and "test" in t["kind"]]
    require(len(targets) == 1 and Path(targets[0]["src_path"]).resolve() ==
            (mirror / private["test_path"]).resolve(), "private integration test is redirected")
    document = tomllib.loads(core.bounded(manifest).decode())
    require(not any(t.get("name") == targets[0]["name"] and t.get("harness") is False
                    for t in document.get("test", [])), "private libtest harness disabled")


def run_candidate(public, source, folder, private=None, *, run=core.invoke):
    validate(public, private)
    source, folder = Path(source).resolve(), Path(folder).resolve()
    require(not source.is_relative_to(folder) and not folder.is_relative_to(source), "source/build overlap")
    role = "private" if private else "public"
    evidence = {"schema_version": SCHEMA, "task_id": public["task_id"], "role": role, "status": "preparing",
                "selection_uses_test_results": False, "new_resolution_commands": 0,
                "probes": [], "command_executions": 0}
    try:
        for row in [public[k] for k in ("cargo", "rustc", "source_config")] + [
                p for values in public["locks"].values() for p in values]:
            path = Path(row["path"])
            require(path.is_file() and not path.is_symlink() and core.file_digest(path) == row["sha256"],
                    "fixed executable/config/lock changed")
        before = core.snapshot(source)
        if private:
            for row in private["session_files"]:
                require(core.digest(core.bounded(source / row["path"])) == row["sha256"],
                        "original private file changed")
            for name, row in private["service_binaries"].items():
                executable = shutil.which(name)
                require(executable == row["path"] and core.file_digest(Path(executable)) == row["sha256"],
                        "service executable/path changed")
        else:
            require(not (source / public["private_workdir"]).exists(), "private workspace appeared in developer source")
        folder.mkdir(parents=True, exist_ok=True)
        mirror = folder / "source"
        synchronized = synchronize_candidate(source, mirror, {"manifests": [], "locks": []})
        require(core.snapshot(mirror) == before, "complete candidate mirror changed")
        cwd_name = public["private_workdir"] if private else "."
        cwd = mirror / cwd_name
        lock_name = (Path(cwd_name) / "Cargo.lock").as_posix()
        supplied = lock_name in before
        if not private:
            require(supplied, "original committed public lock removed")
        if supplied:
            require(before[lock_name][0] == "file", "candidate lock is not regular")
        seed = not private and supplied and before[lock_name][1] == public["public_seed_sha256"]
        owned = supplied and not seed
        candidates = ([{"path": str(source / lock_name), "sha256": before[lock_name][1]}] if owned
                      else sorted(public["locks"][role], key=lambda x: x["sha256"]))
        evidence.update(source_before=before, synchronized=synchronized, candidate_lock_supplied=supplied,
                        candidate_has_original_public_seed=seed, selected_role_lock_path=lock_name)
        home = folder / "cargo-home"
        require(not home.is_symlink(), "Cargo home escaped build root")
        home.mkdir(exist_ok=True)
        require(not (home / "config.toml").is_symlink(), "Cargo config escaped build root")
        (home / "config.toml").write_bytes(core.bounded(Path(public["source_config"]["path"])))
        env = {**os.environ, "CARGO_HOME": str(home), "RUSTC": public["rustc"]["path"],
               "CARGO_NET_OFFLINE": "true", "CARGO_NET_RETRY": "0", "SOURCE_ROOT": str(mirror)}
        metadata = [public["cargo"]["path"], "--offline", "metadata", "--format-version", "1",
                    "--locked", "--all-features"]
        deadline = time.monotonic() + public["timeout_seconds"]
        matches = []
        for index, lock in enumerate(candidates):
            data = core.bounded(Path(lock["path"]))
            require(core.digest(data) == lock["sha256"], "candidate/default lock changed")
            if not owned:
                (mirror / lock_name).write_bytes(data)
            expected = {**before, lock_name: ["file", lock["sha256"], before[lock_name][2] if supplied else False]}
            require(core.snapshot(mirror) == expected, "lock installation changed other candidate files")
            row = run(metadata, cwd, env, folder, "metadata-" + str(index),
                      min(deadline, time.monotonic() + public["metadata_timeout_seconds"]),
                      public["max_log_bytes"])
            item = {"lock_sha256": lock["sha256"], "command_result": row}
            evidence["probes"].append(item)
            core.complete(row)
            require(row["command"] == metadata and row["cwd"] == str(cwd), "metadata command/cwd changed")
            require(core.snapshot(source) == before and core.snapshot(mirror) == expected,
                    "metadata changed current candidate/lock")
            if row["returncode"] == 0:
                item["graph"] = core.graph_check(row["stdout"], mirror, before, data, workspace_root=cwd)
                if private:
                    private_target(json.loads(row["stdout"]), mirror, public, private)
                matches.append((lock, data, item["graph"]))
            else:
                require(core.lock_incompatible(row, original_locked=True), "metadata failed outside lock compatibility")
                item["compatible"] = False
        require(len(matches) == 1, "no unique compatible locked graph: " + str(len(matches)))
        chosen, data, graph = matches[0]
        if not owned:
            (mirror / lock_name).write_bytes(data)
        expected = {**before, lock_name: ["file", chosen["sha256"], before[lock_name][2] if supplied else False]}
        require(core.snapshot(mirror) == expected, "selected lock changed current source")
        if private:
            shell = private["original_shell"].replace(
                "cargo +" + public["toolchain"], shlex.quote(public["cargo"]["path"]), 1)
            command = ["/bin/bash", "-c", shell]
        else:
            command = [public["cargo"]["path"], "--offline", *public["public_command"]]
        row = run(command, cwd, env, folder, "execution", deadline, public["max_log_bytes"])
        evidence.update(command_executions=1, execution=row, selected_lock_sha256=chosen["sha256"],
                        graph=graph, candidate_lock_preserved=owned, mirror_before=expected)
        if private:
            evidence["original_shell_sha256"] = private["original_shell_sha256"]
            evidence["only_shell_change"] = "version-manager cargo token -> same pinned Cargo executable"
        core.complete(row)
        require(row["command"] == command and row["cwd"] == str(cwd), "original command/cwd changed")
        after, mirrored = core.snapshot(source), core.snapshot(mirror)
        require(after == before and mirrored == expected, "original command modified candidate/lock")
        evidence.update(source_after=after, mirror_after=mirrored, source_unchanged=True, mirror_unchanged=True)
        if private:
            found = core.RESULT.findall(row["stdout"])
            counts, values = Counter(n for n, _ in found), dict(found)
            finished = "test result: ok." in row["stdout"] or "test result: FAILED." in row["stdout"]
            evidence["test_results"] = {
                n: bool(row["returncode"] == 0 and finished and counts[n] == 1 and values.get(n) == "ok")
                for n in private["expected_tests"]}
        evidence.update(status="completed", exit_status=row["returncode"])
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
    require(args.task_id == public["task_id"], "wrong original task")
    args.build_root.mkdir(parents=True, exist_ok=True)
    with (args.build_root / "standalone-service.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if hasattr(os, "sched_getaffinity"):
            cpus = sorted(os.sched_getaffinity(0))
            offset = int.from_bytes(hashlib.sha256(args.task_id.encode()).digest()[:8], "big") % len(cpus)
            os.sched_setaffinity(0, {cpus[(offset + n) % len(cpus)] for n in range(min(len(cpus), public["cpus"]))})
        try:
            row = run_candidate(public, args.source, args.build_root / "standalone-service", private)
        except core.VariantError as error:
            row = error.evidence or {"status": "rejected", "exit_status": 2, "error": str(error)}
        if "execution" in row:
            sys.stdout.write(row["execution"]["stdout"])
            sys.stderr.write(row["execution"]["stderr"])
        (args.build_root / "standalone-service-evidence.json").write_text(json.dumps(row, sort_keys=True) + "\n")
        print(EVIDENCE_PREFIX + json.dumps(row, sort_keys=True), flush=True)
        return row["exit_status"]


if __name__ == "__main__":
    raise SystemExit(main())
