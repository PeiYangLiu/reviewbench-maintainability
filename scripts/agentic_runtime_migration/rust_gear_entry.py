#!/usr/bin/env python3
"""Original asymmetric Gear environments; public selection precedes private WASM."""
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
from urllib.parse import parse_qsl, urlsplit

if __package__:
    from . import rust_image_entry as core, rust_manifest_variants_entry as forms
    from .rust_build_entry import (
        OverlayError, apply_manifest_operations, manifest_operations, same_value,
        synchronize_candidate, tomllib, tomli_w,
    )
else:
    import image_locks as core
    import manifest_variants as forms
    from build import (
        OverlayError, apply_manifest_operations, manifest_operations, same_value,
        synchronize_candidate, tomllib, tomli_w,
    )

SCHEMA = "reviewbench-original-asymmetric-gear-environments/v1"
PRIVATE_SCHEMA = "reviewbench-original-asymmetric-gear-session/v1"
TASK = "github__gear-tech__gear__pull_4381__comment_2031746639__readability"
NAMES = ("start", "golden")
TOOLCHAINS = ("nightly-2025-01-09", "nightly-2025-05-09")
SESSION = "tests/oracle-gear"
HARNESS = SESSION + "/tests/oracle_session.rs"
SESSION_PATHS = (SESSION + "/Cargo.toml", HARNESS, SESSION + "/test_syscalls.wasm")
TEST = "executes_block_height_through_the_public_runtime"
PUBLIC = ["build", "--locked", "-p", "gtest", "--lib", "--message-format=json"]
COMMAND = ["test", "-p", "gear-oracle-session", "--test", "oracle_session",
           "--", "--nocapture", "--test-threads=1"]
OMITTED = ("merkleized-metadata", "0.1.0", core.CRATES_IO)
OMITTED_CHECKSUM = "f313fcff1d2a4bcaa2deeaa00bf7530d77d5f7bd0467a117dde2e29a75a7a17a"
EVIDENCE_PREFIX = "ReviewBench Gear environments: "
EMPTY = {"manifests": [], "locks": [], "private_members": []}


def require(value, message):
    core.require(value, "original Gear environments: " + message)


def fingerprint(value):
    return core.digest(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                  separators=(",", ":")).encode())


def fixed(row, *, contents=False):
    require(set(row) == {"path", "sha256"} and Path(row["path"]).is_absolute()
            and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]), "invalid input binding")
    path = Path(row["path"])
    require(path.is_file() and not path.is_symlink()
            and core.file_digest(path) == row["sha256"], "fixed input changed")
    return core.bounded(path) if contents else path


def validate(public, private=None):
    require(public.get("schema_version") == SCHEMA and public.get("task_id") == TASK
            and public.get("selection") == forms.SELECTION
            and public.get("package") == "gtest" and public.get("package_manifest") == "Cargo.toml"
            and public.get("public_command") == PUBLIC, "original public contract changed")
    require([r["name"] for r in public["forms"]] == list(NAMES)
            and [r["toolchain"] for r in public["forms"]] == list(TOOLCHAINS)
            and [r["path"] for r in public["manifests"]] == ["Cargo.toml"],
            "original compiler/manifest pair changed")
    require(re.fullmatch(r"[0-9a-f]{64}", public["reference_seed_sha256"]), "missing original lock seed")
    for form in public["forms"]:
        require([r["path"] for r in form["manifests"]] == ["Cargo.toml"], "non-root manifest replacement")
        for row in (form["cargo"], form["rustc"], form["source_config"], form["lock"],
                    form["manifests"][0]["asset"]):
            require(set(row) == {"path", "sha256"} and Path(row["path"]).is_absolute()
                    and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]), "unbound form input")
        require(Path(form["cargo"]["path"]).name == "cargo"
                and Path(form["rustc"]["path"]).name == "rustc"
                and Path(form["cargo"]["path"]).parent == Path(form["rustc"]["path"]).parent,
                "compiler tools no longer belong to one toolchain")
    for key, maximum in (("cpus", 4), ("timeout_seconds", 3600),
                         ("metadata_timeout_seconds", 180), ("max_log_bytes", 32 * 1024**2)):
        require(type(public[key]) is int and 0 < public[key] <= maximum, "invalid bound " + key)
    if private is None:
        return
    require(private.get("schema_version") == PRIVATE_SCHEMA and private.get("task_id") == TASK
            and private.get("expected_tests") == [TEST] and private.get("workdir") == SESSION
            and private.get("command") == COMMAND
            and [r["name"] for r in private["forms"]] == list(NAMES)
            and fingerprint(private) == public["private_fingerprint"], "original private contract changed")
    for row in private["forms"]:
        require([p["path"] for p in row["session_files"]] == list(SESSION_PATHS),
                "original session path inventory changed")


def environment_forms(public, source, before):
    """Unedited slots receive paired originals; candidate-owned slots receive no repair."""
    choices = forms.root_build_forms(public, source, before)
    owned = choices[0]["candidate_owned"]
    result = []
    for index, original in enumerate(public["forms"]):
        form = copy.deepcopy(choices[0] if owned else choices[index])
        form.update(original_name=original["name"], name=(
            "current-candidate-" + original["name"] if owned else original["name"]),
            cargo=original["cargo"], rustc=original["rustc"], source_config=original["source_config"],
            toolchain=original["toolchain"])
        if owned:
            form["lock"] = {"path": str(source / "Cargo.lock"), "sha256": before["Cargo.lock"][1]}
        form["cache"] = "form-" + fingerprint({
            "manifest": tomllib.loads(form["contents"]["Cargo.toml"].decode()), "command": PUBLIC,
            "cargo": form["cargo"]["sha256"], "rustc": form["rustc"]["sha256"],
            "source_config": form["source_config"]["sha256"], "lock": form["lock"]["sha256"],
        })[:20]
        result.append(form)
    require(len({r["cache"] for r in result}) == 2, "asymmetric caches were merged")
    return result


def git_source_key(source):
    """Compare only Cargo's selector spelling; retain repository and full commit.

    This standalone image tool has no dependency on the host's Blob/Git
    transport module. Tests bind this boundary to rust_vendor.git_source_identity.
    """
    require(isinstance(source, str) and source.startswith("git+"), "invalid Git source")
    url = urlsplit(source[4:])
    query = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True)
    require(re.fullmatch(r"[0-9a-f]{40}", url.fragment) and len(query) <= 1
            and all(key in {"branch", "tag", "rev"} and value for key, value in query)
            and url.path and url.scheme in {"https", "http", "ssh", "git", "file"}
            and (url.scheme == "file" or url.netloc), "invalid complete Git selector")
    return url.scheme, url.netloc, url.path, tuple(query), url.fragment


def graph_check(output, mirror, snapshot, lock_bytes, *, private=False):
    """Retain the entire lock, including its unused default-feature registry entry."""
    graph = json.loads(output)
    packages = tomllib.loads(lock_bytes.decode())["package"]
    present = {(p["name"], p["version"], p.get("source")) for p in graph["packages"]}
    absent = []
    for row in packages:
        identity = row["name"], row["version"], row.get("source")
        if identity == OMITTED and identity not in present:
            require(row.get("checksum") == OMITTED_CHECKSUM, "unresolved registry pin changed")
            absent.append(identity)
    binding = ({"schema_version": core.LOCKED_WORKSPACE_PRIVATE_SCHEMA,
                "harness": {"path": HARNESS}, "package_manifest": SESSION + "/Cargo.toml",
                "command": COMMAND} if private else None)
    result = core.graph_check(output, mirror, snapshot, lock_bytes, binding, locked_git=True,
                             omitted_locked_registry_packages=tuple(absent), git_source_key=git_source_key)
    result["unresolved_locked_registry_packages"] = [list(p) for p in absent]
    result["lock_bytes_preserved"] = True
    return result


def private_environment(form, private_form):
    current_bytes = form["contents"]["Cargo.toml"]
    current = tomllib.loads(current_bytes.decode())
    exact = fixed(private_form["manifest"], contents=True)
    declared = tomllib.loads(exact.decode())
    base = copy.deepcopy(declared)
    require(base["workspace"]["members"].count(SESSION) == 1, "private member absent or duplicated")
    base["workspace"]["members"].remove(SESSION)
    # Only register the original grader package. Preserve every current member,
    # dependency field and unrelated edit in the public-selected environment.
    operations = [{"kind": "list_edit", "key": ["workspace", "members"],
                   "remove": [], "add": [SESSION], "order": [SESSION]}]
    desired = apply_manifest_operations(current, operations)
    emitted = exact if same_value(current, base) else tomli_w.dumps(desired).encode()
    require(same_value(tomllib.loads(emitted.decode()), desired), "private member changed other configuration")
    lock_bytes = fixed(form["lock"], contents=True)
    reference_lock = fixed(private_form["lock"], contents=True)
    template = tomllib.loads(reference_lock.decode())
    local = [p for p in template["package"] if p["name"] == "gear-oracle-session"]
    require(local == [{"name": "gear-oracle-session", "version": "0.0.0",
                       "dependencies": ["gtest", "parity-scale-codec"]}],
            "original private lock registration changed")
    current_lock = tomllib.loads(lock_bytes.decode())
    without_session = {**template, "package": [p for p in template["package"] if p not in local]}
    if not form["candidate_owned"]:
        require(current_lock == without_session, "public/private original lock pair changed external packages")
        result_lock = reference_lock
    else:
        existing = [p for p in current_lock["package"] if p["name"] == "gear-oracle-session"]
        require(not existing or existing == local, "candidate owns a conflicting private registration")
        result_lock = lock_bytes if existing else lock_bytes + b"\n" + tomli_w.dumps({"package": local}).encode()
        require(tomllib.loads(result_lock.decode()) == (
            current_lock if existing else {**current_lock, "package": [*current_lock["package"], *local]}),
            "private registration changed candidate lock fields")
    files = {"Cargo.toml": emitted, "Cargo.lock": result_lock}
    files.update({r["path"]: fixed(r["asset"], contents=True) for r in private_form["session_files"]})
    return files


def run_candidate(public, source, folder, private=None, *, run=core.invoke):
    validate(public, private)
    source, folder = Path(source).resolve(), Path(folder).resolve()
    evidence = {"schema_version": SCHEMA, "task_id": TASK, "role": "grading" if private else "public",
                "selection_uses_test_results": False, "new_lock_generation_commands": 0,
                "status": "preparing", "probes": [], "public_compiles": [], "executions": []}
    mirrors = []
    try:
        require(not source.is_relative_to(folder) and not folder.is_relative_to(source), "source/cache overlap")
        before = core.snapshot(source)
        evidence["source_before"] = before
        require(before.get("Cargo.lock", [None])[0] == "file", "candidate removed or redirected lock")
        require(not any(p == SESSION or p.startswith(SESSION + "/") for p in before),
                "candidate collides with original grader package")
        choices = environment_forms(public, source, before)
        evidence["candidate_configuration_preserved"] = choices[0]["candidate_owned"]
        folder.mkdir(parents=True, exist_ok=True)
        deadline, compatible = time.monotonic() + public["timeout_seconds"], []
        targets = Path(os.environ.get("CARGO_TARGET_DIR", str(folder / "target")))

        def install(form, mirror, extra=None):
            synchronize_candidate(source, mirror, EMPTY)
            changes = {**form["contents"], "Cargo.lock": fixed(form["lock"], contents=True), **(extra or {})}
            for name, data in changes.items():
                path = mirror / name
                require(not path.is_symlink() and path.resolve().is_relative_to(mirror), "overlay redirected")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                path.chmod(0o755 if before.get(name, [None, None, False])[2] else 0o644)
            expected = {**before, **{n: ["file", core.digest(data), before.get(n, [None, None, False])[2]]
                                    for n, data in changes.items()}}
            require(core.snapshot(source) == before and core.snapshot(mirror) == expected,
                    "environment changed unrelated candidate inputs")
            return expected

        def invoke(command, cwd, env, logdir, label, expected, mirror, *, metadata=False):
            row = run(command, cwd, env, logdir, label,
                      min(deadline, time.monotonic() + public["metadata_timeout_seconds"]) if metadata else deadline,
                      public["max_log_bytes"])
            core.complete(row)
            require(row["command"] == command and row["cwd"] == str(cwd), "command or cwd changed")
            require(core.snapshot(source) == before and core.snapshot(mirror) == expected,
                    "command changed source/manifest/lock/session bytes")
            return row

        try:
            for number, form in enumerate(choices):
                for name in ("cargo", "rustc", "source_config"):
                    fixed(form[name])
                work = folder / form["cache"]
                require(not work.is_symlink(), "form cache redirected")
                work.mkdir(exist_ok=True)
                mirror, home = work / "source", work / "cargo-home"
                mirrors.append(mirror)
                require(not home.is_symlink(), "Cargo home redirected")
                home.mkdir(exist_ok=True)
                require(not (home / "config.toml").is_symlink(), "Cargo config redirected")
                (home / "config.toml").write_bytes(fixed(form["source_config"], contents=True))
                env = {**os.environ, "RUSTC": form["rustc"]["path"], "CARGO_HOME": str(home),
                       "RUSTDOC": str(Path(form["rustc"]["path"]).with_name("rustdoc")),
                       "PATH": str(Path(form["cargo"]["path"]).parent) + ":" + os.environ.get("PATH", ""),
                       "CARGO_TARGET_DIR": str(targets / form["cache"]), "SOURCE_ROOT": str(mirror),
                       "CARGO_NET_OFFLINE": "true", "CARGO_NET_RETRY": "0",
                       "CARGO_BUILD_JOBS": str(public["cpus"]), "RUSTUP_TOOLCHAIN": form["toolchain"]}
                expected = install(form, mirror)
                command = [form["cargo"]["path"], "--offline", "metadata", "--format-version", "1", "--locked"]
                row = invoke(command, mirror, env, work, "metadata-public", expected, mirror, metadata=True)
                probe = {"form": form["name"], "lock_sha256": form["lock"]["sha256"], "command_result": row}
                evidence["probes"].append(probe)
                if row["returncode"]:
                    require(core.catalog_lock_incompatible(row), "metadata failed outside locked graph compatibility")
                    continue
                graph = graph_check(row["stdout"], mirror, expected, fixed(form["lock"], contents=True))
                probe["graph"] = graph
                command = [form["cargo"]["path"], "--offline", *PUBLIC]
                row = invoke(command, mirror, env, work, "public-build", expected, mirror)
                evidence["public_compiles"].append({"form": form["name"], "command_result": row,
                                                   "target_cache": form["cache"]})
                if row["returncode"]:
                    require(forms.public_api_mismatch(row, mirror, expected),
                            "public build failed outside a current-production API mismatch")
                    continue
                compatible.append((form, mirror, env, work, expected, graph, row))
            require(compatible, "no original environment compiles the current candidate")
            evidence["compatible_forms"] = [r[0]["name"] for r in compatible]
            results = {TEST: True} if private else {}
            # No private package or WASM is installed before this complete list
            # is fixed. A failing assertion never selects a different form.
            for form, mirror, env, work, expected, graph, compiled in compatible:
                if private:
                    original = next(p for p in private["forms"] if p["name"] == form["original_name"])
                    changes = private_environment(form, original)
                    expected = install(form, mirror, changes)
                    command = [form["cargo"]["path"], "--offline", "metadata", "--format-version", "1", "--locked"]
                    row = invoke(command, mirror, env, work, "metadata-private", expected, mirror, metadata=True)
                    evidence.setdefault("private_probes", []).append({"form": form["name"], "command_result": row})
                    require(row["returncode"] == 0, "original private package graph failed")
                    graph = graph_check(row["stdout"], mirror, expected, changes["Cargo.lock"], private=True)
                    command = [form["cargo"]["path"], *COMMAND]
                    row = invoke(command, mirror / SESSION, env, work, "original-test", expected, mirror)
                    values = core.RESULT.findall(row["stdout"])
                    counts, passed = Counter(n for n, _ in values), dict(values)
                    results[TEST] &= bool(row["returncode"] == 0 and "test result: ok." in row["stdout"]
                                          and counts[TEST] == 1 and passed.get(TEST) == "ok")
                else:
                    row = compiled
                evidence["executions"].append({"form": form["name"], "target_cache": form["cache"],
                                                "command_result": row, "graph": graph})
            evidence.update(test_results=results, exit_status=0 if not private or all(results.values()) else 1)
        finally:
            for mirror in mirrors:
                synchronize_candidate(source, mirror, EMPTY)
                require(core.snapshot(mirror) == before, "mirror restoration differed from original candidate")
            evidence.update(source_after=core.snapshot(source), mirrors_restored=True)
            require(evidence["source_after"] == before, "original candidate changed")
            evidence["source_unchanged"] = True
        evidence["status"] = "completed"
        return evidence
    except (OverlayError, ValueError, OSError, KeyError, TypeError) as error:
        evidence.update(status="rejected", exit_status=2, error=type(error).__name__ + ": " + str(error))
        raise core.VariantError(str(error), evidence) from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--private", type=Path)
    args = parser.parse_args()
    public = json.loads(core.bounded(args.spec))
    private = json.loads(core.bounded(args.private)) if args.private else None
    require(args.task_id == public["task_id"], "task changed")
    root = Path("/opt/reviewbench-cache/rust-build/gear-environments")
    root.mkdir(parents=True, exist_ok=True)
    with (root / "execution.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cpus = sorted(os.sched_getaffinity(0))
        offset = int(core.digest(TASK.encode())[:16], 16) % len(cpus)
        os.sched_setaffinity(0, {cpus[(offset + i) % len(cpus)] for i in range(min(len(cpus), public["cpus"]))})
        try:
            evidence = run_candidate(public, Path("/workspace"), root, private)
        except core.VariantError as error:
            evidence = error.evidence or {"status": "rejected", "exit_status": 2, "error": str(error)}
        if private and evidence["status"] == "completed":
            print("test " + TEST + " ... " + ("ok" if evidence["test_results"][TEST] else "FAILED"))
            print("test result: " + ("ok." if evidence["exit_status"] == 0 else "FAILED."))
        if evidence.get("error"):
            sys.stderr.write(evidence["error"] + "\n")
        print(EVIDENCE_PREFIX + json.dumps(evidence, sort_keys=True), flush=True)
        return evidence["exit_status"]


if __name__ == "__main__":
    raise SystemExit(main())
