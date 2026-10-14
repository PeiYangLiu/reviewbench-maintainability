#!/usr/bin/env python3
"""Compile-select recorded public helper names; preserve every original test."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time

try:
    from . import rust_api_entry as api
    from .rust_build_entry import candidate_files, synchronize_candidate
except ImportError:
    import rust_api_entry as api
    sys.path.insert(0, "/opt/reviewbench-assets/rust-build-tools")
    from build import candidate_files, synchronize_candidate

SCHEMA = "reviewbench-rust-test-compat/v1"
SELECTION = "compile-only-current-candidate; execute-all-compilable-forms; conjunctive-results"
OPERATIONS = (
    ("frost-core/src/tests.rs", "frost::keys::SecretShareValue::<C>::random", "frost::keys::SharedSecret::<C>::random"),
    ("frost-core/src/tests/vectors.rs", "SecretShareValue::<C>::from_hex", "SigningShare::<C>::from_hex"),
    ("frost-core/src/tests/vectors.rs", "frost::keys::PublicVerificationShare::from", "frost::keys::VerifyingShare::from"),
)
ERROR_CODES = {"E0412", "E0425", "E0432", "E0433", "E0599"}
INFRA = re.compile(
    r"internal compiler error|the compiler unexpectedly panicked|rustc interrupted by|"
    r"\bsignal\s*:?\s*[1-9][0-9]*\b|(?-i:\bSIG[A-Z][A-Z0-9_]*(?:[+-][0-9]+)?\b)|"
    r"\b(?:sigsegv|sigabrt|sigkill|sigterm|sigbus|sigill|sigfpe)\b|"
    r"(?:terminated|killed|interrupted) by (?:a )?signal|"
    r"segmentation fault|bus error|illegal instruction|floating point exception|core dumped|"
    r"killed signal terminated program|no space left on device|disk quota exceeded|"
    r"input/output error|failed to (?:spawn|execute) (?:process|rustc)|"
    r"could not execute process|cannot allocate memory|out of memory|"
    r"failed to run custom build command", re.I)
ABORT = re.compile(r"aborting due to (?:(?P<count>[1-9][0-9]*) )?previous errors?"
                   r"(?:; [0-9]+ warnings? emitted)?")
CARGO_ERROR = re.compile(r"error: could not compile `frost-core` due to "
                         r"(?:(?P<count>[1-9][0-9]*) )?previous errors?"
                         r"(?:; [0-9]+ warnings? emitted)?")
# Cargo1.64's non-JSON progress/summary lines, not a general error-text filter.
# Unknown output makes a failed probe indeterminate; it never selects a form.
CARGO_PROGRESS = re.compile(
    r"(?:Compiling|Checking|Fresh) [A-Za-z0-9_-]+ v[^ \t]+(?: \([^\r\n]+\))?|"
    r"Blocking waiting for file lock on (?:package cache|build directory)|"
    r"warning: build failed, waiting for other jobs to finish\.\.\.|"
    r"warning: `[A-Za-z0-9_-]+`(?: \((?:lib|lib test|test \"[^\"]+\")\))? "
    r"generated [0-9]+ warnings?(?: \([0-9]+ duplicates?\))?|"
    r"warning: unused manifest key: [A-Za-z0-9_.-]+")
FAILURE_NOTE = re.compile(
    r"Some errors have detailed explanations: E[0-9]{4}(?:, E[0-9]{4})*\.|"
    r"For more information about (?:this error|an error), try `rustc --explain E[0-9]{4}`\.")
SelectionError = api.SelectionError


def digest(value):
    return hashlib.sha256(value).hexdigest()


def validate(spec):
    allowed = {"schema_version", "profile", "task_id", "toolchain", "selection", "files", "chain",
               "cpus", "timeout_seconds", "max_log_bytes", "mode", "command"}
    mode = spec.get("mode")
    if mode == "grading":
        allowed.add("expected_tests")
    if (set(spec) != allowed or spec.get("schema_version") != SCHEMA
            or spec.get("profile") != "frost106-public-helper-renames-v1"
            or spec.get("task_id") != "github__ZcashFoundation__frost__pull_106__comment_964209322__naming"
            or spec.get("selection") != SELECTION or mode not in {"public", "grading"}
            or spec.get("toolchain") != "1.64.0"):
        raise SelectionError("unbound or outcome-dependent Rust test compatibility specification")
    if ([r.get("path") for r in spec["files"]] != list(dict.fromkeys(row[0] for row in OPERATIONS))
            or len(spec["chain"]) != 3):
        raise SelectionError("unexpected public helper scope")
    for row in spec["files"]:
        if set(row) != {"path", "sha256"} or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"]):
            raise SelectionError("invalid original helper hash")
    hashes = {r["path"]: r["sha256"] for r in spec["files"]}
    for row, (path, old, new) in zip(spec["chain"], OPERATIONS):
        if (set(row) != {"path", "old", "new", "before", "after"}
                or (row["path"], row["old"], row["new"]) != (path, old, new)
                or row["before"] != hashes[path] or not re.fullmatch(r"[0-9a-f]{64}", row["after"])):
            raise SelectionError("changed rename-only hash chain")
        hashes[path] = row["after"]
    command = spec["command"]
    if not isinstance(command, list) or not command or command[0] != "cargo":
        raise SelectionError("compatibility command must use the pinned Cargo")
    verb = "check" if mode == "public" else "test"
    tail = [verb, "--locked", "-p", "frost-core", "--features", "test-impl"]
    if mode == "grading":
        tail += ["--test", "tests", "--", "--nocapture"]
        expected = spec["expected_tests"]
        if (not expected or len(set(expected)) != len(expected)
                or any(not re.fullmatch(r"[A-Za-z0-9_:]+", name) for name in expected)):
            raise SelectionError("invalid fixed original test identities")
    if command[-len(tail):] != tail:
        raise SelectionError("original package/features/test command changed")
    prefix = command[1:-len(tail)]
    if prefix not in [
        ["--offline"],
        ["--offline", "--config", "/opt/reviewbench-assets/cargo-sources.toml"],
        ["--offline", "--config", 'source.crates-io.replace-with="reviewbench-vendor"',
         "--config", 'source.reviewbench-vendor.directory="/opt/reviewbench-assets/vendor"'],
    ]:
        raise SelectionError("unexpected Cargo prefix")
    if (type(spec["cpus"]) is not int or not 1 <= spec["cpus"] <= 4
            or type(spec["timeout_seconds"]) is not int or not 1 <= spec["timeout_seconds"] <= 2400
            or type(spec["max_log_bytes"]) is not int or not 1024 <= spec["max_log_bytes"] <= 32 * 1024**2):
        raise SelectionError("invalid resource bounds")


def transformed(original, spec, form):
    result = dict(original)
    for row in spec["files"]:
        if digest(result[row["path"]]) != row["sha256"]:
            raise SelectionError("original public helper bytes changed")
    if form == "unrenamed":
        return result
    if form != "renamed":
        raise SelectionError("unknown preserved compatibility form")
    for row in spec["chain"]:
        data = result[row["path"]]
        old = row["old"].encode()
        if digest(data) != row["before"] or data.count(old) != 1:
            raise SelectionError("public helper intermediate input changed")
        data = data.replace(old, row["new"].encode(), 1)
        if digest(data) != row["after"]:
            raise SelectionError("public helper replacement changed an assertion/body")
        result[row["path"]] = data
    return result


def fingerprint(root):
    return {name: ("link", os.readlink(path)) if path.is_symlink() else
            ("file", digest(path.read_bytes()), bool(path.stat().st_mode & 0o111))
            for name, path in candidate_files(root).items()}


def process_failure(record):
    text = record.get("stdout", "") + "\n" + record.get("stderr", "")
    code = record.get("returncode")
    incomplete = any(record.get(kind + "_total_bytes", 0) > record.get(kind + "_captured_bytes", 0)
                     for kind in ("stdout", "stderr"))
    return (record.get("limit") or incomplete or type(code) is not int
            or code < 0 or code >= 124 or INFRA.search(text))


def infrastructure(record, evidence):
    if process_failure(record):
        raise SelectionError("compiler/process infrastructure failure; no API reselection", evidence)


def diagnostic_children(message):
    """A permitted primary error must not conceal another error in its tree."""
    children = message.get("children", [])
    text, rendered = message.get("message"), message.get("rendered") or ""
    return (isinstance(text, str) and isinstance(rendered, str)
            and not INFRA.search(text + "\n" + rendered) and isinstance(children, list)
            and all(isinstance(child, dict) and child.get("level") in {"note", "help", "warning"}
                    and diagnostic_children(child) for child in children))


def compiler_manifest(row, root):
    manifest, target = row.get("manifest_path"), row.get("target")
    if (not isinstance(manifest, str) or not isinstance(target, dict)
            or not isinstance(target.get("src_path"), str) or "lib" not in target.get("kind", [])):
        return None
    manifest, target_path = Path(manifest), Path(target["src_path"])
    manifest = manifest if manifest.is_absolute() else root / manifest
    target_path = target_path if target_path.is_absolute() else root / target_path
    if (manifest.resolve() != (root / "frost-core/Cargo.toml").resolve()
            or target_path.resolve() != (root / "frost-core/src/lib.rs").resolve()):
        return None
    return manifest


def failed_probe_messages(record, root):
    """Account for the entire failed Cargo stream, not just recognized errors.

    Successful artifact/build-script notifications may accompany a failed
    compilation. Unknown JSON reasons, raw stdout, additional Cargo failures,
    malformed messages and unrecognized failure notes are not incompatibility.
    """
    messages, counts, notes = [], [], []
    finished = False
    for line in record.get("stdout", "").splitlines():
        if not line.strip():
            continue
        if finished:
            return None
        try:
            row = json.loads(line)
        except ValueError:
            return None
        if not isinstance(row, dict):
            return None
        reason = row.get("reason")
        if reason == "compiler-message":
            if set(row) - {"reason", "package_id", "manifest_path", "target", "message"}:
                return None
            message = row.get("message")
            if not isinstance(message, dict) or not diagnostic_children(message):
                return None
            level = message.get("level")
            if level == "error":
                messages.append(row)
            elif level == "failure-note":
                if (compiler_manifest(row, root) is None
                        or not FAILURE_NOTE.fullmatch(message["message"])):
                    return None
                notes.extend(re.findall(r"\bE[0-9]{4}\b", message["message"]))
            elif level not in {"warning", "note", "help"}:
                return None
        elif reason == "compiler-artifact":
            if (set(row) - {"reason", "package_id", "manifest_path", "target", "profile", "features",
                            "filenames", "executable", "fresh"}
                    or not isinstance(row.get("target"), dict)
                    or not isinstance(row.get("filenames"), list)
                    or type(row.get("fresh")) is not bool):
                return None
        elif reason == "build-script-executed":
            if (set(row) - {"reason", "package_id", "linked_libs", "linked_paths", "cfgs", "env", "out_dir"}
                    or not isinstance(row.get("package_id"), str)
                    or not all(isinstance(row.get(k), list) for k in ("linked_libs", "linked_paths", "cfgs", "env"))
                    or not isinstance(row.get("out_dir"), str)):
                return None
        elif reason == "build-finished":
            if set(row) != {"reason", "success"} or row["success"] is not False or finished:
                return None
            finished = True
        else:
            return None
    for line in record.get("stderr", "").splitlines():
        line = line.strip()
        if not line:
            continue
        summary = CARGO_ERROR.fullmatch(line)
        if summary:
            if summary["count"]:
                counts.append(int(summary["count"]))
        elif not CARGO_PROGRESS.fullmatch(line):
            return None
    return messages, counts, notes


def rename_mismatch(record, root, spec, form):
    if record["returncode"] != 101 or process_failure(record):
        return False
    parsed = failed_probe_messages(record, root)
    if parsed is None:
        return False
    messages, cargo_counts, notes = parsed
    ranges = {}
    for path, old, new in OPERATIONS:
        token = new if form == "renamed" else old
        content = (root / path).read_bytes()
        at = content.find(token.encode())
        if at < 0 or content.count(token.encode()) != 1:
            return False
        ranges.setdefault((root / path).resolve(), []).append((at, at + len(token.encode())))
    errors, codes, rustc_counts = [], set(), []
    for row in messages:
        message = row["message"]
        manifest = compiler_manifest(row, root)
        if manifest is None:
            return False
        if (not isinstance(message.get("spans"), list)
                or not all(isinstance(span, dict) for span in message["spans"])):
            return False
        spans = [span for span in message["spans"] if span.get("is_primary")]
        abort = ABORT.fullmatch(message.get("message", ""))
        if not spans and message.get("code") is None and abort:
            if abort["count"]:
                rustc_counts.append(int(abort["count"]))
            continue
        value = message.get("code")
        if not isinstance(value, dict):
            return False
        code = value.get("code")
        if not spans or code not in ERROR_CODES:
            return False
        if not re.search(r"\b(?:SecretShareValue|SharedSecret|SigningShare|PublicVerificationShare|VerifyingShare)\b",
                         message.get("message", "")):
            return False
        for span in spans:
            path = Path(span.get("file_name", ""))
            candidates = [path] if path.is_absolute() else [root / path, manifest.parent / path]
            start, end = span.get("byte_start"), span.get("byte_end")
            if (type(start) is not int or type(end) is not int or end <= start
                    or not any(any(a <= start < end <= b for a, b in ranges.get(p.resolve(), []))
                               for p in candidates)):
                return False
        errors.append(message)
        codes.add(code)
    # Cargo1.64 counts every error envelope, including rustc's abort footer.
    # That footer itself counts only primary errors (real 3+1 -> Cargo4).
    return (bool(errors) and all(count == len(errors) for count in rustc_counts)
            and all(count == len(messages) for count in cargo_counts) and set(notes) <= codes)


@contextmanager
def invocation(cwd, target):
    before, old = Path.cwd(), os.environ.get("CARGO_TARGET_DIR")
    os.chdir(cwd)
    os.environ["CARGO_TARGET_DIR"] = str(target)
    try:
        yield
    finally:
        os.chdir(before)
        if old is None:
            os.environ.pop("CARGO_TARGET_DIR", None)
        else:
            os.environ["CARGO_TARGET_DIR"] = old


def select(spec, source, folder, *, run=api.run_command):
    validate(spec)
    source, folder = source.resolve(), folder.resolve()
    if source == folder or source.is_relative_to(folder) or folder.is_relative_to(source):
        raise SelectionError("compatibility copies must be separate from current candidate")
    before = fingerprint(source)
    for row in spec["files"]:
        path = source / row["path"]
        if (path.is_symlink() or not path.is_file()
                or any(p.is_symlink() for p in path.parents if p.is_relative_to(source))):
            raise SelectionError("public test helper is not an original regular file")
    originals = {r["path"]: (source / r["path"]).read_bytes() for r in spec["files"]}
    # The original helper hashes are checked before any compiler invocation.
    transformed(originals, spec, "unrenamed")
    evidence = {"selection_uses_test_results": False, "probes": [], "tests": [], "selected": []}
    deadline = time.monotonic() + spec["timeout_seconds"]
    selected = []
    for form in ("unrenamed", "renamed"):
        root = folder / form / "source"
        synchronize_candidate(source, root, {})
        for path, data in transformed(originals, spec, form).items():
            (root / path).write_bytes(data)
        expected = fingerprint(root)
        command = list(spec["command"])
        at = command.index("check" if spec["mode"] == "public" else "test") + 1
        command[at:at] = (["--no-run", "--message-format=json"] if spec["mode"] == "grading"
                         else ["--message-format=json"])
        logs = folder / form / "logs"
        logs.mkdir()
        with invocation(root, folder / form / "target"):
            record = run(command, logs, "probe", deadline, spec["max_log_bytes"])
        evidence["probes"].append({"form": form, **record})
        if fingerprint(source) != before or fingerprint(root) != expected:
            raise SelectionError("current candidate or helper mutated during compilation", evidence)
        infrastructure(record, evidence)
        if record["returncode"] == 0:
            selected.append((form, root, logs, expected))
        elif not rename_mismatch(record, root, spec, form):
            raise SelectionError("compilation failed outside an attested rename token", evidence)
    evidence["selected"] = [form for form, _, _, _ in selected]
    if not selected:
        raise SelectionError("no compile-compatible preserved helper form", evidence)
    results = {name: True for name in spec.get("expected_tests", [])}
    # Freeze the entire selected set before executing any original assertion.
    for form, root, logs, expected in selected if spec["mode"] == "grading" else []:
        if fingerprint(source) != before or fingerprint(root) != expected:
            raise SelectionError("candidate/helper mutation after selection", evidence)
        with invocation(root, folder / form / "target"):
            record = run(spec["command"], logs, "tests", deadline, spec["max_log_bytes"])
        evidence["tests"].append({"form": form, **record})
        if fingerprint(source) != before or fingerprint(root) != expected:
            raise SelectionError("candidate/helper mutation during original tests", evidence)
        infrastructure(record, evidence)
        observed = api.test_results(record, spec["expected_tests"])
        results = {name: value and observed[name] for name, value in results.items()}
    evidence["results"] = results
    evidence["public_build_ok"] = bool(selected)
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    args = parser.parse_args()
    try:
        spec = json.loads(args.spec.read_text())
        source = Path(os.environ.get("SOURCE_ROOT", "/workspace"))
        with tempfile.TemporaryDirectory(prefix="rust-test-compat-") as folder:
            evidence = select(spec, source, Path(folder))
        print("ReviewBench Rust test compatibility evidence: " + json.dumps(evidence, sort_keys=True), flush=True)
        for name, passed in evidence["results"].items():
            print("test " + name + " ... " + ("ok" if passed else "FAILED"), flush=True)
        return 0 if all(evidence["results"].values()) else 1
    except (SelectionError, OSError, ValueError, KeyError, TypeError) as exc:
        if isinstance(exc, SelectionError) and exc.evidence is not None:
            print("ReviewBench Rust test compatibility evidence: " + json.dumps(exc.evidence, sort_keys=True), flush=True)
        print("Rust test compatibility infrastructure error: " + str(exc), file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
