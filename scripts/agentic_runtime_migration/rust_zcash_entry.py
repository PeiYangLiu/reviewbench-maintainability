#!/usr/bin/env python3
"""Complete current wallet source, original compatibility operations and locked tests."""
from __future__ import annotations

import argparse
from collections import Counter
import fcntl
import json
import os
from pathlib import Path
import re
import time

if __package__:
    from . import rust_image_entry as core
    from .rust_build_entry import synchronize_candidate, tomllib, tomli_w, same_value
else:
    import image_locks as core
    from build import synchronize_candidate, tomllib, tomli_w, same_value

SCHEMA = "reviewbench-original-zcash-wallet-runtime/v1"
PREFIX = "ReviewBench original Zcash wallet: "
PACKAGE = "zcash_client_sqlite"
FEATURES = "transparent-inputs,zcash_keys/transparent-key-encoding"
MANIFEST = "zcash_client_sqlite/Cargo.toml"
LIB = "zcash_client_sqlite/src/lib.rs"
HARNESS = "zcash_client_sqlite/tests/oracle_gap.rs"
PUBLIC = ["check", "--locked", "-p", PACKAGE, "--features", FEATURES, "--message-format=json"]
COMMAND = ["test", "--locked", "-p", PACKAGE, "--features", FEATURES,
           "--test", "oracle_gap", "--", "--nocapture", "--test-threads=1"]
OLD_DEP = {"workspace": True, "features": [
    "test-dependencies", "unstable-serialization", "unstable-spanning-tree"]}
NEW_DEP = {"workspace": True, "features": ["unstable-serialization", "unstable-spanning-tree"]}
OLD_IMPORT = '''#[cfg(any(test, feature = "test-dependencies"))]
use {
    rusqlite::named_params,
    zcash_client_backend::data_api::{OutputOfSentTx, WalletTest, testing::TransactionSummary},
    zcash_keys::address::Address,
};'''
NEW_IMPORT = '''#[cfg(any(test, feature = "test-dependencies"))]
use {
    rusqlite::named_params,
    zcash_client_backend::data_api::{OutputOfSentTx, WalletTest, testing::TransactionSummary},
};

#[cfg(any(test, feature = "test-dependencies", feature = "transparent-inputs"))]
use zcash_keys::address::Address;'''


def require(value, message):
    core.require(value, "original wallet: " + message)


def checked(row, *, binary=False):
    path = Path(row["path"])
    require(path.is_file() and not path.is_symlink() and core.file_digest(path) == row["sha256"],
            "fixed input changed")
    return path if binary else core.bounded(path)


def validate(public, private=None):
    require(public["schema_version"] == SCHEMA and public["public_command"] == PUBLIC
            and public["selection"] == "unique-full-current-locked-metadata-no-test-outcomes",
            "unbound command or selection")
    require(isinstance(public["task_id"], str) and public["task_id"], "missing task")
    for key in ("cargo", "rustc", "source_config"):
        row = public[key]
        require(set(row) == {"path", "sha256"} and Path(row["path"]).is_absolute()
                and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]), "unbound " + key)
    require(Path(public["cargo"]["path"]).name == "cargo" and Path(public["rustc"]["path"]).name == "rustc",
            "wrong original compiler")
    require(len(public["locks"]) == 2 and len({r["sha256"] for r in public["locks"]}) == 2
            and len({r["path"] for r in public["locks"]}) == 2
            and public["reference_seed_sha256"] in {r["sha256"] for r in public["locks"]},
            "original lock pair changed")
    for row in public["locks"]:
        require(set(row) == {"path", "sha256"} and Path(row["path"]).is_absolute()
                and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]), "unbound lock")
    require(public["compatibility_policy"] == "recorded-dev-feature-and-import-only-current-mirror",
            "compatibility scope changed")
    for key, maximum in (("cpus", 4), ("timeout_seconds", 3600), ("metadata_timeout_seconds", 180),
                         ("max_log_bytes", 32 * 1024**2)):
        require(type(public[key]) is int and 0 < public[key] <= maximum, "invalid limit")
    if private:
        require(private["task_id"] == public["task_id"] and private["command"] == COMMAND
                and private["harness"]["path"] == HARNESS, "original private command changed")
        require(private["expected_tests"] and len(set(private["expected_tests"])) == len(private["expected_tests"])
                and all(re.fullmatch(r"[A-Za-z0-9_:]+", n) for n in private["expected_tests"])
                and re.fullmatch(r"[0-9a-f]{64}", private["harness"]["sha256"]),
                "original tests missing or repeated")


def compatibility(source):
    """Never replace candidate functions, tests, manifests or unrelated dependencies."""
    contents, operations = {}, []
    path = source / MANIFEST
    document = tomllib.loads(core.bounded(path).decode())
    value = document.get("dev-dependencies", {}).get("zcash_client_backend")
    if same_value(value, OLD_DEP):
        document["dev-dependencies"]["zcash_client_backend"] = dict(NEW_DEP)
        contents[MANIFEST] = tomli_w.dumps(document).encode()
        operations.append("remove-recorded-dev-test-dependencies-feature")
    # Already transformed or candidate-owned declarations remain exactly theirs.
    path = source / LIB
    data = core.bounded(path)
    count = data.count(OLD_IMPORT.encode())
    require(count <= 1, "recorded import anchor became ambiguous")
    if count:
        contents[LIB] = data.replace(OLD_IMPORT.encode(), NEW_IMPORT.encode(), 1)
        operations.append("split-recorded-Address-import-cfg")
    return contents, operations


def check_target(metadata, mirror, private):
    graph = json.loads(metadata)
    owners = [p for p in graph["packages"] if p.get("source") is None and p["name"] == PACKAGE
              and Path(p["manifest_path"]).resolve() == (mirror / MANIFEST).resolve()]
    require(len(owners) == 1, "original test package redirected")
    targets = [t for t in owners[0]["targets"] if t["name"] == "oracle_gap" and t["kind"] == ["test"]]
    require(len(targets) == 1 and Path(targets[0]["src_path"]).resolve() == (mirror / HARNESS).resolve(),
            "original test target redirected")
    doc = tomllib.loads(core.bounded(mirror / MANIFEST).decode())
    require(not any(t.get("name") == "oracle_gap" and t.get("harness") is False for t in doc.get("test", [])),
            "original libtest harness disabled")
    require(core.file_digest(mirror / HARNESS) == private["harness"]["sha256"],
            "original harness changed")


def run_candidate(public, source, folder, private=None, *, run=core.invoke):
    validate(public, private)
    source, folder = Path(source).resolve(), Path(folder).resolve()
    require(not source.is_relative_to(folder) and not folder.is_relative_to(source), "source/cache overlap")
    evidence = {"schema_version": SCHEMA, "task_id": public["task_id"], "status": "preparing",
                "role": "grading" if private else "public", "new_lock_generation_commands": 0,
                "selection_uses_test_results": False, "probes": [], "public_compiles": []}
    try:
        for key in ("cargo", "rustc"):
            checked(public[key], binary=True)
        checked(public["source_config"])
        for row in public["locks"]:
            checked(row)
        before = core.snapshot(source)
        require(before.get("Cargo.lock", [None])[0] == "file", "original committed lock missing or redirected")
        if private:
            require(core.file_digest(source / HARNESS) == private["harness"]["sha256"], "original harness changed")
        expected = dict(before)
        changes, operations = compatibility(source)
        folder.mkdir(parents=True, exist_ok=True)
        mirror, home = folder / "source", folder / "cargo-home"
        home.mkdir(exist_ok=True)
        require(not home.is_symlink() and not (home / "config.toml").is_symlink(), "Cargo config redirected")
        (home / "config.toml").write_bytes(checked(public["source_config"]))
        synchronize_candidate(source, mirror, {"manifests": [], "locks": [], "private_members": []})
        for name, content in changes.items():
            target = mirror / name
            require(target.is_file() and not target.is_symlink(), "compatibility path redirected")
            if target.read_bytes() != content:
                target.write_bytes(content)
            expected[name] = ["file", core.digest(content), before[name][2]]
        require(core.snapshot(source) == before and core.snapshot(mirror) == expected,
                "compatibility changed more than recorded current-source fields")
        evidence.update(source_before=before, compatibility_operations=operations,
                        compatibility_paths=sorted(changes), reference_seed=before["Cargo.lock"][1] ==
                        public["reference_seed_sha256"])
        owned = not evidence["reference_seed"]
        choices = ([{"path": str(source / "Cargo.lock"), "sha256": before["Cargo.lock"][1]}]
                   if owned else sorted(public["locks"], key=lambda r: r["sha256"]))
        env = {**os.environ, "CARGO_HOME": str(home), "RUSTC": public["rustc"]["path"],
               "CARGO_NET_OFFLINE": "true", "CARGO_NET_RETRY": "0", "CARGO_BUILD_JOBS": str(public["cpus"])}
        deadline = time.monotonic() + public["timeout_seconds"]
        metadata = [public["cargo"]["path"], "--offline", "metadata", "--locked", "--format-version", "1", "--all-features"]
        compatible = []
        for index, lock in enumerate(choices):
            data = checked(lock)
            if not owned:
                (mirror / "Cargo.lock").write_bytes(data)
            current = {**expected, "Cargo.lock": ["file", lock["sha256"], before["Cargo.lock"][2]]}
            require(core.snapshot(mirror) == current, "probe modified current source")
            row = run(metadata, mirror, env, folder, "metadata-" + str(index),
                      min(deadline, time.monotonic() + public["metadata_timeout_seconds"]), public["max_log_bytes"])
            evidence["last_command"] = row
            core.complete(row)
            require(row["command"] == metadata and row["cwd"] == str(mirror), "metadata command changed")
            require(core.snapshot(source) == before and core.snapshot(mirror) == current, "metadata changed source/lock")
            probe = {"lock_sha256": lock["sha256"], "command_result": row}
            evidence["probes"].append(probe)
            if row["returncode"] == 0:
                graph = core.graph_check(row["stdout"], mirror, current, data)
                if private:
                    check_target(row["stdout"], mirror, private)
                compatible.append((lock, data, graph))
                probe["graph"] = graph
            else:
                require(core.lock_incompatible(row, original_locked=True), "metadata failed outside lock compatibility")
        require(len(compatible) == 1, "no unique compatible original full graph")
        lock, data, graph = compatible[0]
        if not owned:
            (mirror / "Cargo.lock").write_bytes(data)
        expected["Cargo.lock"] = ["file", lock["sha256"], before["Cargo.lock"][2]]
        evidence.update(candidate_lock_preserved=owned, selected_lock_sha256=lock["sha256"], graph=graph)
        command = [public["cargo"]["path"], *(private["command"] if private else ["--offline", *PUBLIC])]
        row = run(command, mirror, env, folder, "original-tests" if private else "public-check",
                  deadline, public["max_log_bytes"])
        evidence["last_command"] = row
        core.complete(row)
        require(row["command"] == command and row["cwd"] == str(mirror), "original command changed")
        require(core.snapshot(source) == before and core.snapshot(mirror) == expected, "execution changed source/lock")
        tests = {}
        if private:
            require(core.file_digest(source / HARNESS) == core.file_digest(mirror / HARNESS) ==
                    private["harness"]["sha256"], "original harness changed")
            found = core.RESULT.findall(row["stdout"])
            counts, statuses = Counter(n for n, _ in found), dict(found)
            tests = {n: bool(row["returncode"] == 0 and counts[n] == 1 and statuses.get(n) == "ok"
                             and "test result: ok." in row["stdout"]) for n in private["expected_tests"]}
        else:
            evidence["public_compiles"].append({"command_result": row})
        evidence.pop("last_command", None)
        evidence.update(status="completed", execution=row, test_results=tests, source_after=before,
                        source_unchanged=True, mirror_after=expected,
                        exit_status=0 if row["returncode"] == 0 and (not private or all(tests.values())) else 1)
        return evidence
    except (ValueError, OSError, KeyError, TypeError) as error:
        evidence.update(status="rejected", exit_status=2, error=type(error).__name__ + ": " + str(error))
        raise core.VariantError(str(error), evidence) from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--private", type=Path)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--source", type=Path, default=Path("/workspace"))
    args = parser.parse_args()
    public = json.loads(args.spec.read_text())
    require(public["task_id"] == args.task_id, "task changed")
    private = json.loads(args.private.read_text()) if args.private else None
    folder = Path("/opt/reviewbench-cache/rust-build") / ("wallet-" + core.digest(args.task_id.encode())[:20])
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "command.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            evidence = run_candidate(public, args.source, folder, private)
        except core.VariantError as error:
            evidence = error.evidence
        print(PREFIX + json.dumps(evidence, sort_keys=True), flush=True)
        for name, passed in evidence.get("test_results", {}).items():
            print("test " + name + " ... " + ("ok" if passed else "FAILED"), flush=True)
        return evidence["exit_status"]


if __name__ == "__main__":
    raise SystemExit(main())
