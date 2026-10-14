#!/usr/bin/env python3
"""Pin declared Git selectors, validate the complete graph, test all compatible original APIs."""
from __future__ import annotations

import argparse
import copy
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import time

if __package__:
    from . import rust_image_entry as core, rust_api_entry as api
    from .rust_build_entry import manifest_operations, overlay_files, synchronize_candidate, tomllib
else:
    import image_locks as core
    import original_api as api
    from build import manifest_operations, overlay_files, synchronize_candidate, tomllib

SCHEMA = "reviewbench-original-bitcoin-git-runtime/v1"
PREFIX = "ReviewBench original Git inputs: "
SELECTION = "public-git-identity; full-locked-metadata; compile-only-original-apis; all-compatible-conjunction"
GIT_KEYS = {"git", "branch", "tag", "rev", "path"}
INFRA = re.compile(
    r"internal compiler error|the compiler unexpectedly panicked|rustc interrupted by|"
    r"\bsignal\s*:?\s*[1-9][0-9]*\b|(?-i:\bSIG[A-Z][A-Z0-9_]*(?:[+-][0-9]+)?\b)|"
    r"(?:terminated|killed|interrupted) by (?:a )?signal|segmentation fault|bus error|"
    r"illegal instruction|floating point exception|core dumped|killed signal terminated program|"
    r"no space left on device|disk quota exceeded|input/output error|"
    r"failed to (?:spawn|execute) (?:process|rustc)|could not execute process|"
    r"cannot allocate memory|out of memory|failed to run custom build command", re.I)


def require(value, message):
    core.require(value, "original Git inputs: " + message)


def checked_path(row):
    path = Path(row["path"])
    require(path.is_absolute() and path.is_file() and not path.is_symlink()
            and core.file_digest(path) == row["sha256"], "fixed asset changed")
    return path


def fixed(row):
    return core.bounded(checked_path(row))


def git_identity(value):
    if not isinstance(value, dict):
        return {}
    result = {k: v for k, v in value.items() if k in GIT_KEYS}
    if isinstance(result.get("git"), str):
        result["git"] = result["git"].removesuffix(".git")
    return result


def pin_manifest(document, public):
    """Transport pins follow current public declarations; explicit edits survive."""
    desired = copy.deepcopy(document)
    dependencies = desired.get("dependencies", {})
    patches = desired.get("patch", {}).get("crates-io", {})
    bitcoin = dependencies.get("bitcoin")
    hashes = patches.get("bitcoin_hashes")
    for pin in public["git_pins"]:
        current = git_identity(bitcoin)
        if current == {"git": pin["url"], "branch": pin["branch"]}:
            bitcoin.pop("branch")
            bitcoin["rev"] = pin["commit"]
        current = git_identity(hashes)
        if current in ({"git": pin["url"]}, {"git": pin["url"], "rev": pin["commit"]}):
            hashes["rev"] = pin["commit"]
            # This addition is part of the original recorded preparation, not
            # permission to replace a candidate-authored patch or its features.
            if "bitcoin-internals" not in patches:
                patches["bitcoin-internals"] = {"git": hashes["git"], "rev": pin["commit"]}
    return desired


def validate(public, private=None):
    require(public["schema_version"] == SCHEMA and public["selection"] == SELECTION, "unbound protocol")
    require(len(public["git_pins"]) == len(public["locks"]) == 2, "missing original Git forms")
    identities = set()
    for pin in public["git_pins"]:
        require(set(pin) == {"url", "branch", "commit"} and pin["url"].startswith("https://github.com/")
                and pin["branch"] and re.fullmatch(r"[0-9a-f]{40}", pin["commit"]), "invalid public Git pin")
        identities.add((pin["url"], pin["commit"]))
    require(len(identities) == 2
            and {(row["git"], row["commit"]) for row in public["locks"]} == identities,
            "lock/Git identities disagree")
    require(public["public_command"] == ["check", "--locked", "--features", "rand", "--message-format=json"],
            "public command changed")
    for key, maximum in (("cpus", 4), ("timeout_seconds", 1800), ("metadata_timeout_seconds", 120),
                          ("max_log_bytes", 32 * 1024**2)):
        require(type(public[key]) is int and 0 < public[key] <= maximum, "invalid resource bound")
    if private:
        require(private["task_id"] == public["task_id"] and private["api"]["task_id"] == public["task_id"],
                "private task changed")
        api.validate(private["api"])
        require(private["api"]["cpus"] == public["cpus"]
                and private["api"]["timeout_seconds"] == public["timeout_seconds"],
                "original API limits changed")


def private_mismatch(record, harness, source):
    """Typed errors only in the frozen harness; mixed process failures are fatal."""
    if (record["returncode"] != 101 or record.get("limit")
            or INFRA.search(record["stdout"] + "\n" + record["stderr"])
            or not api.private_api_mismatch(record, harness, source)):
        return False
    finished, errors = False, []
    for line in record["stdout"].splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            return False
        reason = value.get("reason")
        if reason not in {"compiler-message", "compiler-artifact", "build-script-executed", "build-finished"}:
            return False
        if reason == "build-finished":
            finished = value.get("success") is False
        message = value.get("message", {})
        if reason != "compiler-message" or message.get("level") != "error":
            continue
        if not message.get("spans") and message.get("code") is None and re.fullmatch(
                r"aborting due to (?:\d+ )?previous errors?(?:; \d+ warnings? emitted)?", message.get("message", "")):
            continue
        code = message.get("code")
        if not isinstance(code, dict) or not re.fullmatch(r"E\d{4}", code.get("code", "")):
            return False
        errors.append(code["code"])
    if not finished or not errors:
        return False
    for line in record["stderr"].splitlines():
        text = line.strip()
        if not text:
            continue
        if re.fullmatch(r"(?:Compiling|Checking|Fresh) [A-Za-z0-9_-]+ v[^ \t]+(?: \([^\r\n]+\))?", text):
            continue
        if text == "warning: build failed, waiting for other jobs to finish...":
            continue
        if re.fullmatch(r"error: could not compile `[A-Za-z0-9_-]+` \(test \"" + re.escape(Path(harness).stem)
                        + r"\"\) due to (?:\d+ )?previous errors?(?:; \d+ warnings? emitted)?", text):
            continue
        if re.fullmatch(r"Some errors have detailed explanations: E\d{4}(?:, E\d{4})*\.", text):
            continue
        if re.fullmatch(r"For more information about (?:this error|an error), try `rustc --explain E\d{4}`\.", text):
            continue
        return False
    return True


def run_candidate(public, source, folder, private=None, *, run=core.invoke):
    validate(public, private)
    source, folder = Path(source).resolve(), Path(folder).resolve()
    require(not source.is_relative_to(folder) and not folder.is_relative_to(source), "source/cache overlap")
    evidence = {"schema_version": SCHEMA, "task_id": public["task_id"], "role": "grading" if private else "public",
                "selection_uses_test_results": False, "new_lock_generation_commands": 0,
                "status": "preparing", "probes": [], "public_compiles": [], "api_commands": []}
    try:
        # Compiler binaries are hash-checked files, not bounded metadata text.
        # Keep the strict size bound for locks/configuration/source inputs.
        for key in ("cargo", "rustc"):
            checked_path(public[key])
        fixed(public["source_config"])
        before = core.snapshot(source)
        if private:
            api.verify_files(source, private["api"])
        current = tomllib.loads(core.bounded(source / "Cargo.toml").decode())
        desired = pin_manifest(current, public)
        specification = {"manifests": [{"path": "Cargo.toml", "operations": manifest_operations(current, desired)}],
                         "locks": [], "private_members": []}
        emitted = overlay_files(source, specification)
        require(tomllib.loads(emitted["Cargo.toml"].decode()) == desired, "manifest semantics changed")
        owned = "Cargo.lock" in before
        if owned:
            require(before["Cargo.lock"][0] == "file", "candidate lock redirected")
            lock = {"path": str(source / "Cargo.lock"), "sha256": before["Cargo.lock"][1]}
        else:
            identity = git_identity(desired.get("dependencies", {}).get("bitcoin"))
            matches = [row for row in public["locks"]
                       if identity == {"git": row["git"], "rev": row["commit"]}]
            require(len(matches) == 1, "no unique default for current public Git declaration")
            lock = matches[0]["asset"]
        fixed(lock)
        folder.mkdir(parents=True, exist_ok=True)
        mirror, home = folder / "source", folder / "cargo-home"
        require(not home.is_symlink(), "Cargo home redirected")
        home.mkdir(exist_ok=True)
        require(not (home / "config.toml").is_symlink(), "source config redirected")
        (home / "config.toml").write_bytes(fixed(public["source_config"]))
        synchronize_candidate(source, mirror, specification)
        (mirror / "Cargo.lock").write_bytes(fixed(lock))
        expected = {**before, "Cargo.toml": ["file", core.digest(emitted["Cargo.toml"]), before["Cargo.toml"][2]],
                    "Cargo.lock": ["file", lock["sha256"], before.get("Cargo.lock", ["", "", False])[2]]}
        require(core.snapshot(source) == before and core.snapshot(mirror) == expected, "mirror altered unrelated source")
        controlled = [git_identity(desired.get("dependencies", {}).get("bitcoin")),
                      git_identity(desired.get("patch", {}).get("crates-io", {}).get("bitcoin_hashes")),
                      git_identity(desired.get("patch", {}).get("crates-io", {}).get("bitcoin-internals"))]
        namespace = "form-" + core.digest(json.dumps(controlled, sort_keys=True).encode())[:20]
        target = Path(os.environ.get("CARGO_TARGET_DIR", str(folder / "target"))) / namespace
        env = {**os.environ, "CARGO_HOME": str(home), "RUSTC": public["rustc"]["path"],
               "PATH": str(Path(public["cargo"]["path"]).parent) + ":" + os.environ.get("PATH", ""),
               "CARGO_NET_OFFLINE": "true", "CARGO_NET_RETRY": "0", "CARGO_TARGET_DIR": str(target)}
        deadline = time.monotonic() + public["timeout_seconds"]
        evidence.update(source_before=before, candidate_lock_preserved=owned,
                        selected_lock_sha256=lock["sha256"], public_git_inputs=controlled, target_cache=namespace)

        def invoke(command, name, end):
            row = run(command, mirror, env, folder, name, min(deadline, end), public["max_log_bytes"])
            core.complete(row)
            require(row["command"] == command and row["cwd"] == str(mirror), "command/cwd changed")
            require(core.snapshot(source) == before and core.snapshot(mirror) == expected, "command mutated source or lock")
            return row

        command = [public["cargo"]["path"], "--offline", "metadata", "--locked", "--format-version", "1", "--all-features"]
        row = invoke(command, "complete-metadata", time.monotonic() + public["metadata_timeout_seconds"])
        evidence["probes"].append({"lock_sha256": lock["sha256"], "command_result": row})
        require(row["returncode"] == 0, "complete locked candidate metadata failed")
        graph = core.graph_check(row["stdout"], mirror, expected, fixed(lock), locked_git=True)
        if private:
            for harness in private["api"]["files"]:
                core.graph_check(row["stdout"], mirror, expected, fixed(lock), {"harness": harness}, locked_git=True)
        evidence["graph"] = graph
        command = [public["cargo"]["path"], "--offline", *public["public_command"]]
        compiled = invoke(command, "public-compile", deadline)
        evidence["public_compiles"].append({"target_cache": namespace, "command_result": compiled})
        results = {}
        if compiled["returncode"] == 0 and private:
            original_cwd = Path.cwd()
            try:
                os.chdir(mirror)
                api_spec = copy.deepcopy(private["api"])
                api_spec["timeout_seconds"] = max(1, int(deadline - time.monotonic()))

                def api_run(command, _folder, label, end, _limit):
                    row = invoke(command, "api-" + label, end)
                    evidence["api_commands"].append({"label": label, "command_result": row})
                    if "--no-run" in command and row["returncode"]:
                        target_name = command[command.index("--test") + 1]
                        harness = next(f["harness"] for f in api_spec["forms"] if f["target"] == target_name)
                        require(private_mismatch(row, harness, mirror),
                                "API compile failure is not solely a typed original-harness mismatch")
                    return row

                selected = api.select_and_test(api_spec, mirror, folder, run=api_run)
                evidence["api"] = selected
                results = selected["results"]
            finally:
                os.chdir(original_cwd)
        elif private:
            results = {name: False for name in private["api"]["expected_tests"]}
        evidence.update(status="completed", source_after=core.snapshot(source), source_unchanged=True,
                        mirror_after=core.snapshot(mirror), test_results=results,
                        exit_status=0 if compiled["returncode"] == 0 and (not private or all(results.values())) else 1)
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
    private = json.loads(args.private.read_text()) if args.private else None
    require(public["task_id"] == args.task_id, "task changed")
    folder = Path("/opt/reviewbench-cache/rust-build") / ("bitcoin-" + core.digest(args.task_id.encode())[:20])
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
