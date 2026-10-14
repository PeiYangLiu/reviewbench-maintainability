#!/usr/bin/env python3
"""Select original dependency forms by public compilation; execute every compatible original service test."""
from __future__ import annotations

import argparse
from collections import Counter
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import sys
import time

if __package__:
    from . import rust_image_entry as core
    from .rust_build_entry import (
        OverlayError, apply_manifest_operations, manifest_operations, synchronize_candidate, tomllib, tomli_w,
    )
else:
    import image_locks as core
    from build import (
        OverlayError, apply_manifest_operations, manifest_operations, synchronize_candidate, tomllib, tomli_w,
    )

SCHEMA = "reviewbench-original-manifest-service/v1"
PRIVATE_SCHEMA = "reviewbench-original-manifest-service-private/v1"
EVIDENCE_PREFIX = "ReviewBench manifest service: "
SOURCE_KEYS = ("git", "version", "rev", "tag", "branch", "path", "package")
TEST = "launches_a_real_terminal_window_and_exits_with_its_child"
TEST_COMMAND = ["test", "--locked", "-p", "alacritty", "--no-default-features",
                "--features", "x11", "--test", "oracle_launch", TEST, "--", "--nocapture"]


def require(value, message):
    core.require(value, "manifest service: " + message)


def validate(public, private=None):
    require(public["schema_version"] == SCHEMA and public["selection"] ==
            "full-candidate-metadata-and-public-compile; all-compatible-private-results",
            "schema/selection changed")
    require(public["manifest_path"] == "alacritty/Cargo.toml"
            and public["dependency_key"] == ["dependencies", "glutin"], "unbound dependency slot")
    require(public["toolchain"] == "1.65.0" and public["package"] == "alacritty", "compiler/package changed")
    for name in ("cargo", "rustc", "source_config"):
        row = public[name]
        require(set(row) == {"path", "sha256"} and Path(row["path"]).is_absolute()
                and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]), "unbound " + name)
    require(public["public_command"] == ["check", "--locked", "-p", "alacritty",
            "--no-default-features", "--features", "x11", "--message-format=json"], "public build changed")
    require(len(public["forms"]) == 2 and [f["name"] for f in public["forms"]] == ["git-beta", "registry-stable"],
            "original form inventory changed")
    for form in public["forms"]:
        for field in ("manifest", "lock"):
            row = form[field]
            require(set(row) == {"path", "sha256"} and Path(row["path"]).is_absolute()
                    and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]), "unbound original form")
        for operation in form["operations"]:
            require(operation["key"] in [public["dependency_key"] + ["git"], public["dependency_key"] + ["version"]],
                    "dependency form alters unrelated fields")
    require(public["forms"][0]["operations"] == [], "original form must preserve candidate manifest")
    for key, maximum in (("cpus", 4), ("timeout_seconds", 3600), ("metadata_timeout_seconds", 120),
                         ("max_log_bytes", 32 * 1024**2)):
        require(type(public[key]) is int and 0 < public[key] <= maximum, "invalid bound " + key)
    if private is None:
        return
    require(private["schema_version"] == PRIVATE_SCHEMA and private["task_id"] == public["task_id"],
            "private identity changed")
    require(private["harness"]["path"] == "alacritty/tests/oracle_launch.rs"
            and re.fullmatch(r"[0-9a-f]{64}", private["harness"]["sha256"]), "original harness changed")
    require(private["package_manifest"] == public["manifest_path"] and private["command"] == TEST_COMMAND,
            "original test owner/argv changed")
    require(private["expected_tests"] == [TEST],
            "original test identities changed")
    require(core.digest(private["original_shell"].encode()) == private["original_shell_sha256"],
            "original service shell changed")
    require(private["original_shell"].count("cargo +1.65.0") == 1
            and "cargo +1.65.0 " + shlex.join(TEST_COMMAND) + "; rc=$?;" in private["original_shell"]
            and "Xvfb :99 -screen 0 1024x768x24 -ac -nolock -nolisten unix -listen tcp" in private["original_shell"]
            and 'kill "$xvfb_pid"' in private["original_shell"] and 'wait "$xvfb_pid"' in private["original_shell"],
            "original Xvfb lifecycle changed")


def dependency_slot(manifest, key):
    value = manifest
    for part in key:
        require(isinstance(value, dict) and part in value, "candidate removed its production dependency")
        value = value[part]
    return value


def dependency_forms(public, manifest_bytes, lock_sha):
    current = tomllib.loads(manifest_bytes.decode())
    original = tomllib.loads(core.bounded(Path(public["forms"][0]["manifest"]["path"])).decode())
    left = dependency_slot(current, public["dependency_key"])
    right = dependency_slot(original, public["dependency_key"])
    source_unchanged = (isinstance(left, dict) and
                        {k: left[k] for k in SOURCE_KEYS if k in left} ==
                        {k: right[k] for k in SOURCE_KEYS if k in right})
    reference_lock = lock_sha == public["forms"][0]["lock"]["sha256"]
    if not (source_unchanged and reference_lock):
        return [{"name": "current-candidate", "manifest_bytes": manifest_bytes, "operations": []}]
    result = []
    for form in public["forms"]:
        changed = apply_manifest_operations(current, form["operations"])
        original_form = core.bounded(Path(form["manifest"]["path"]))
        if manifest_bytes == core.bounded(Path(public["forms"][0]["manifest"]["path"])):
            require(changed == tomllib.loads(original_form.decode()), "original manifest operation mismatch")
            content = original_form
        elif not form["operations"]:
            content = manifest_bytes
        else:
            content = tomli_w.dumps(changed).encode()
        require(tomllib.loads(content.decode()) == changed, "manifest serialization changed semantics")
        result.append({"name": form["name"], "manifest_bytes": content, "operations": form["operations"]})
    return result


def public_api_mismatch(record, workspace, source_snapshot):
    """Only typed rustc diagnostics in current production targets select a form."""
    if record["returncode"] != 101 or record.get("limit"):
        return False
    errors = []
    finished = False
    for line in record["stdout"].splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("reason") == "build-finished":
            finished = row.get("success") is False
        message = row.get("message", {})
        if row.get("reason") == "compiler-message" and message.get("level") == "error":
            target = row.get("target", {})
            target_path = Path(target.get("src_path", ""))
            if (not target_path.is_absolute() or not target_path.resolve().is_relative_to(workspace)
                    or target_path.relative_to(workspace).as_posix() not in source_snapshot
                    or not set(target.get("kind", [])) & {"bin", "lib", "proc-macro", "rlib"}
                    or set(target.get("kind", [])) & {"test", "bench", "custom-build"}):
                return False
            spans = [s for s in message.get("spans", []) if s.get("is_primary")]
            if not spans:
                if message.get("code") is None and re.fullmatch(
                        r"aborting due to (?:\d+ )?previous errors?(?:; \d+ warnings? emitted)?",
                        message.get("message", "")):
                    continue
                return False
            if not isinstance(message.get("code"), dict) or not re.fullmatch(
                    r"E\d{4}", message["code"].get("code", "")):
                return False
            for span in spans:
                raw = Path(span["file_name"])
                manifest = Path(row.get("manifest_path", target_path.parent / "Cargo.toml"))
                candidates = [raw] if raw.is_absolute() else [workspace / raw, manifest.parent / raw]
                if not any(p.resolve().is_relative_to(workspace) and p.is_file()
                           and p.resolve().relative_to(workspace).as_posix() in source_snapshot
                           and p.suffix == ".rs"
                           and not {"tests", "benches"} & set(p.relative_to(workspace).parts)
                           and p.name != "build.rs" for p in candidates):
                    return False
            errors.append(message)
    return finished and bool(errors)


def run_candidate(public, source, folder, private=None, *, run=core.invoke):
    validate(public, private)
    source, folder = Path(source).resolve(), Path(folder).resolve()
    require(not folder.is_relative_to(source) and not source.is_relative_to(folder), "source/build overlap")
    evidence = {"schema_version": SCHEMA, "task_id": public["task_id"],
                "role": "grading" if private else "public", "status": "preparing",
                "selection_uses_test_results": False, "probes": [], "public_compiles": [], "executions": []}
    try:
        for row in [public[k] for k in ("cargo", "rustc", "source_config")] + [
                form[k] for form in public["forms"] for k in ("manifest", "lock")]:
            require(core.file_digest(Path(row["path"])) == row["sha256"], "fixed compiler/config/form changed")
        before = core.snapshot(source)
        if private:
            require(core.digest(core.bounded(source / private["harness"]["path"])) == private["harness"]["sha256"],
                    "original assertion source changed")
        manifest_name = public["manifest_path"]
        require(all(name in before and before[name][0] == "file" for name in ("Cargo.lock", manifest_name)),
                "candidate removed or redirected original build inputs")
        original_manifests = [tomllib.loads(core.bounded(Path(f["manifest"]["path"])).decode())
                              for f in public["forms"]]
        require(public["forms"][1]["operations"] == manifest_operations(*original_manifests),
                "original dependency operations changed")
        locked_git_packages = sorted({
            (p["name"], p["version"], p["source"])
            for f in public["forms"] for p in tomllib.loads(core.bounded(Path(f["lock"]["path"])).decode())["package"]
            if p.get("source", "").startswith("git+")
        })
        forms = dependency_forms(public, core.bounded(source / manifest_name), before["Cargo.lock"][1])
        owned_lock = before["Cargo.lock"][1] != public["forms"][0]["lock"]["sha256"]
        locks = ([{"path": str(source / "Cargo.lock"), "sha256": before["Cargo.lock"][1]}]
                 if owned_lock else [form["lock"] for form in public["forms"]])
        folder.mkdir(parents=True, exist_ok=True)
        mirror = folder / "source"
        home = folder / "cargo-home"
        home.mkdir(exist_ok=True)
        require(not home.is_symlink() and not (home / "config.toml").is_symlink(), "Cargo home escaped")
        (home / "config.toml").write_bytes(core.bounded(Path(public["source_config"]["path"])))
        env = {**os.environ, "CARGO_HOME": str(home), "CARGO_NET_OFFLINE": "true", "CARGO_NET_RETRY": "0",
               "RUSTC": public["rustc"]["path"], "SOURCE_ROOT": str(mirror)}
        deadline = time.monotonic() + public["timeout_seconds"]
        compatible = []
        evidence.update(source_before=before, candidate_lock_preserved=owned_lock)

        def install(form, lock):
            synchronize_candidate(source, mirror, {"manifests": [], "locks": []})
            (mirror / manifest_name).write_bytes(form["manifest_bytes"])
            (mirror / "Cargo.lock").write_bytes(core.bounded(Path(lock["path"])))
            expected = {**before, manifest_name: ["file", core.digest(form["manifest_bytes"]), before[manifest_name][2]],
                        "Cargo.lock": ["file", lock["sha256"], before["Cargo.lock"][2]]}
            require(core.snapshot(mirror) == expected and core.snapshot(source) == before,
                    "dependency preparation changed other candidate files")
            return expected

        for number, form in enumerate(forms):
            matches = []
            for index, lock in enumerate(locks):
                expected = install(form, lock)
                command = [public["cargo"]["path"], "--offline", "metadata", "--format-version", "1", "--locked", "--all-features"]
                row = run(command, mirror, env, folder, f"metadata-{number}-{index}",
                          min(deadline, time.monotonic() + public["metadata_timeout_seconds"]), public["max_log_bytes"])
                core.complete(row)
                require(row["command"] == command and row["cwd"] == str(mirror), "metadata command/cwd changed")
                item = {"form": form["name"], "lock_sha256": lock["sha256"], "command_result": row}
                evidence["probes"].append(item)
                require(core.snapshot(mirror) == expected and core.snapshot(source) == before, "metadata changed source")
                if row["returncode"] == 0:
                    # Reuse the strict nested-workspace target-owner check without
                    # changing any older selector's schema or behavior.
                    graph_private = ({**private, "schema_version": core.LOCKED_WORKSPACE_PRIVATE_SCHEMA}
                                     if private else None)
                    graph = core.graph_check(row["stdout"], mirror, expected, core.bounded(Path(lock["path"])),
                                             graph_private, locked_git=True)
                    item["graph"] = graph
                    matches.append((lock, graph))
                else:
                    require(core.lock_incompatible(row, original_locked=True, locked_git_packages=locked_git_packages),
                            "metadata failed outside lock compatibility")
            require(len(matches) <= 1, "dependency form has ambiguous locked graphs")
            if not matches:
                continue
            lock, graph = matches[0]
            expected = install(form, lock)
            command = [public["cargo"]["path"], "--offline", *public["public_command"]]
            row = run(command, mirror, env, folder, "public-compile-" + str(number),
                      deadline, public["max_log_bytes"])
            core.complete(row)
            require(row["command"] == command and row["cwd"] == str(mirror), "public command/cwd changed")
            require(core.snapshot(mirror) == expected and core.snapshot(source) == before, "public compiler changed source")
            evidence["public_compiles"].append({"form": form["name"], "command_result": row,
                                                "manifest_sha256": expected[manifest_name][1]})
            if row["returncode"] == 0:
                compatible.append((form, lock, graph, expected, row))
            else:
                require(public_api_mismatch(row, mirror, expected),
                        "public compilation failed outside candidate API compatibility")
        require(compatible, "no original dependency form compiles the current candidate")
        evidence["compatible_forms"] = [form["name"] for form, *_ in compatible]
        evidence["selected_lock_sha256"] = compatible[0][1]["sha256"]
        evidence["graph"] = compatible[0][2]
        result = {test: True for test in private["expected_tests"]} if private else {}
        for number, (form, lock, graph, expected, public_compile) in enumerate(compatible):
            if private:
                expected = install(form, lock)
                command = ["/bin/bash", "-c", private["original_shell"].replace(
                    "cargo +1.65.0", shlex.quote(public["cargo"]["path"]), 1)]
                row = run(command, mirror, env, folder, "original-test-" + str(number),
                          deadline, public["max_log_bytes"])
                core.complete(row)
                require(row["command"] == command and row["cwd"] == str(mirror), "original command/cwd changed")
                found = core.RESULT.findall(row["stdout"])
                counts, values = Counter(n for n, _ in found), dict(found)
                for test in result:
                    result[test] &= bool(row["returncode"] == 0 and "test result: ok." in row["stdout"]
                                         and counts[test] == 1 and values.get(test) == "ok")
                require(core.snapshot(mirror) == expected, "original command changed candidate inputs")
            else:
                row = public_compile
            evidence["executions"].append({"form": form["name"], "lock_sha256": lock["sha256"],
                                           "graph": graph, "manifest_sha256": expected[manifest_name][1],
                                           "command_result": row})
        evidence.update(source_after=core.snapshot(source), source_unchanged=core.snapshot(source) == before,
                        status="completed", exit_status=0 if not private or all(result.values()) else 1,
                        test_results=result)
        require(evidence["source_unchanged"], "current source changed")
        return evidence
    except (OverlayError, OSError, ValueError, KeyError, TypeError) as error:
        evidence.update(status="rejected", exit_status=2, error=type(error).__name__ + ": " + str(error))
        raise core.VariantError(str(error), evidence) from error


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--private", type=Path)
    args = parser.parse_args()
    public = json.loads(core.bounded(args.spec))
    private = json.loads(core.bounded(args.private)) if args.private else None
    require(public["task_id"] == args.task_id, "wrong task")
    root = Path("/opt/reviewbench-cache/rust-build/manifest-service")
    root.mkdir(parents=True, exist_ok=True)
    with (root / "execution.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cpus = sorted(os.sched_getaffinity(0))
        offset = int(core.digest(args.task_id.encode())[:16], 16) % len(cpus)
        os.sched_setaffinity(0, {cpus[(offset + n) % len(cpus)] for n in range(min(len(cpus), public["cpus"]))})
        try:
            evidence = run_candidate(public, Path("/workspace"), root, private)
        except core.VariantError as error:
            evidence = error.evidence or {"status": "rejected", "exit_status": 2, "error": str(error)}
        if private and evidence.get("status") == "completed":
            for test, ok in evidence["test_results"].items():
                print("test " + test + " ... " + ("ok" if ok else "FAILED"))
            print("test result: " + ("ok." if evidence["exit_status"] == 0 else "FAILED."))
        for row in evidence.get("executions", []):
            sys.stderr.write(row["command_result"]["stderr"])
        print(EVIDENCE_PREFIX + json.dumps(evidence, sort_keys=True), flush=True)
        return evidence["exit_status"]


if __name__ == "__main__":
    raise SystemExit(main())
