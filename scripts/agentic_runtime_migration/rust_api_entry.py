#!/usr/bin/env python3
"""Choose compile-compatible frozen harnesses, never a passing test result."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import subprocess
import sys
import tempfile
import time

SCHEMA = "reviewbench-rust-api-selection/v1"
SAME_TARGET_SCHEMA = "reviewbench-original-same-target-api-selection/v1"
RESULT = re.compile(r"^test ([A-Za-z0-9_:]+) \.\.\. (ok|FAILED|ignored)\s*$", re.M)


class SelectionError(ValueError):
    def __init__(self, message, evidence=None):
        super().__init__(message)
        self.evidence = evidence


def relative(value):
    if (not isinstance(value, str) or not value or "\\" in value
            or PurePosixPath(value).is_absolute() or ".." in PurePosixPath(value).parts):
        raise SelectionError("invalid relative harness path")
    return value


def validate(spec):
    if spec.get("schema_version") == SAME_TARGET_SCHEMA:
        validate_same_target(spec)
        return
    if (spec.get("schema_version") != SCHEMA or not isinstance(spec.get("task_id"), str)
            or spec.get("selection") !=
            "compile-only-current-candidate; execute-all-compilable-forms; conjunctive-results"):
        raise SelectionError("unbound Rust API selection specification")
    forms, files, expected = spec.get("forms", []), spec.get("files", []), spec.get("expected_tests", [])
    if (len(forms) != 2 or len({form["name"] for form in forms}) != 2
            or not files or len({row["path"] for row in files}) != len(files)
            or not expected or len(set(expected)) != len(expected)
            or any(not re.fullmatch(r"[A-Za-z0-9_:]+", name) for name in expected)):
        raise SelectionError("missing or duplicate Rust API/test bindings")
    for row in files:
        relative(row["path"])
        if not re.fullmatch(r"[0-9a-f]{64}", row["sha256"]):
            raise SelectionError("invalid frozen harness checksum")
    normalized = []
    for form in forms:
        command = form.get("command", [])
        if (not command or command[0] != "cargo" or command.count("test") != 1
                or not all(isinstance(word, str) and word and "\0" not in word for word in command)):
            raise SelectionError("invalid frozen Cargo test argv")
        head = command[:command.index("--")] if "--" in command else command
        if (head.count("--test") != 1 or "--no-run" in head or "--message-format" in head
                or any(word.startswith("--message-format=") for word in head)):
            raise SelectionError("invalid Rust API target/probe argv")
        index = head.index("--test")
        if head[index + 1] != form.get("target") or not re.fullmatch(r"[A-Za-z0-9_-]+", form["target"]):
            raise SelectionError("Rust API target changed")
        if relative(form["harness"]) not in {row["path"] for row in files}:
            raise SelectionError("Rust API harness has no content binding")
        values = list(command)
        values[index + 1] = "<integration-target>"
        normalized.append(values)
    if normalized[0] != normalized[1]:
        raise SelectionError("Rust API alternatives changed non-target Cargo arguments")
    if (type(spec.get("max_log_bytes")) is not int or not 1024 <= spec["max_log_bytes"] <= 32 * 1024**2
            or type(spec.get("timeout_seconds")) is not int or not 1 <= spec["timeout_seconds"] <= 7200
            or type(spec.get("cpus")) is not int or not 1 <= spec["cpus"] <= 4):
        raise SelectionError("invalid Rust API resource bounds")
    if spec.get("workdir", ".") != ".":
        relative(spec["workdir"])


def verify_files(root, spec):
    if spec.get("schema_version") == SAME_TARGET_SCHEMA:
        for row in [spec["cargo"], *[form["asset"] for form in spec["forms"]]]:
            path = Path(row["path"])
            if (not path.is_absolute() or not path.is_file() or path.is_symlink()
                    or hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]):
                raise SelectionError("original same-target input changed")
        return
    root = root.resolve()
    for row in spec["files"]:
        path = root / relative(row["path"])
        if (not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root)
                or any(parent.is_symlink() for parent in path.parents if parent.is_relative_to(root))):
            raise SelectionError("frozen Rust harness path changed")
        if hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
            raise SelectionError("frozen Rust harness bytes changed")


def probe_command(command):
    values = list(command)
    at = values.index("test") + 1
    values[at:at] = ["--no-run", "--message-format=json"]
    return values


def run_command(command, folder, name, deadline, max_log_bytes):
    paths = {kind: folder / (name + "." + kind) for kind in ("stdout", "stderr")}
    limited = None
    started = time.monotonic()
    with paths["stdout"].open("wb") as stdout, paths["stderr"].open("wb") as stderr:
        process = subprocess.Popen(command, stdout=stdout, stderr=stderr, start_new_session=True)
        try:
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    limited = "deadline"
                    break
                if sum(path.stat().st_size for path in paths.values()) > max_log_bytes:
                    limited = "output_limit"
                    break
                time.sleep(0.05)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
    if sum(path.stat().st_size for path in paths.values()) > max_log_bytes:
        limited = limited or "output_limit"
    record = {
        "command": command, "returncode": process.returncode, "limit": limited,
        "seconds": round(time.monotonic() - started, 6),
    }
    for kind, path in paths.items():
        with path.open("rb") as stream:
            data = stream.read(max_log_bytes + 1)
        record[kind + "_total_bytes"] = path.stat().st_size
        record[kind + "_captured_bytes"] = len(data)
        record[kind + "_sha256"] = hashlib.sha256(data).hexdigest()
        record[kind] = data.decode("utf-8", errors="replace")
    return record


def private_api_mismatch(record, harness, root):
    """Only rustc errors located in this frozen harness may select another API."""
    if record["returncode"] != 101 or record.get("limit"):
        return False
    errors = []
    for line in record["stdout"].splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if value.get("reason") != "compiler-message":
            continue
        message = value.get("message", {})
        if message.get("level") != "error":
            continue
        target = value.get("target", {})
        target_path = Path(target.get("src_path", ""))
        if not target_path.is_absolute():
            target_path = Path.cwd() / target_path
        if "test" not in target.get("kind", []) or target_path.resolve() != (root / harness).resolve():
            return False
        primary = [span for span in message.get("spans", []) if span.get("is_primary")]
        if not primary:
            if message.get("code") is None and re.fullmatch(
                    r"aborting due to (?:\d+ )?previous errors?(?:; \d+ warnings? emitted)?", message.get("message", "")):
                continue
            return False
        for span in primary:
            path = Path(span.get("file_name", ""))
            candidates = [path] if path.is_absolute() else [
                Path.cwd() / path, Path(value.get("manifest_path", "")).parent / path,
            ]
            if not any(path.resolve() == (root / harness).resolve() for path in candidates):
                return False
        errors.append(message)
    return bool(errors)


def test_results(record, expected):
    matches = RESULT.findall(record["stdout"])
    counts = Counter(name for name, _ in matches)
    statuses = dict(matches)
    complete = "test result: ok." in record["stdout"] or "test result: FAILED." in record["stdout"]
    return {name: (record["returncode"] == 0 and not record.get("limit") and complete
                   and counts[name] == 1 and statuses.get(name) == "ok")
            for name in expected}


def validate_same_target(spec):
    kamu_task = "github__kamu-data__kamu-cli__pull_941__comment_1843087682__api_design"
    kamu = spec.get("profile") == "kamu53-original-v1"
    if kamu or spec.get("task_id") == kamu_task:
        if (not kamu or spec.get("task_id") != kamu_task or spec.get("cpus") != 16
                or spec.get("timeout_seconds") != 3600
                or spec.get("package") != "kamu-core"
                or spec.get("package_manifest") != "src/domain/core/Cargo.toml"
                or spec.get("expected_tests") != ["sync_ref_is_local_classifies_local_and_remote"]):
            raise SelectionError("original Kamu same-target binding changed")
    if (not isinstance(spec.get("task_id"), str) or not spec["task_id"]
            or spec.get("selection") !=
            "compile-only-current-candidate; execute-all-compilable-forms; conjunctive-results"
            or spec.get("workdir") != "."):
        raise SelectionError("unbound original same-target API selection")
    forms = spec.get("forms", [])
    expected = spec.get("expected_tests", [])
    if (len(forms) != 2 or len({f["name"] for f in forms}) != 2
            or len(expected) != 1 or not re.fullmatch(r"[A-Za-z0-9_:]+", expected[0])
            or len({f["harness"] for f in forms}) != 1 or len({f["target"] for f in forms}) != 1
            or forms[0]["command"] != forms[1]["command"]):
        raise SelectionError("original same-target command/harness pair changed")
    cargo = spec["cargo"]
    for row in [cargo, *[form["asset"] for form in forms]]:
        if (set(row) != {"path", "sha256"} or not Path(row["path"]).is_absolute()
                or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"])):
            raise SelectionError("unbound original same-target input")
    if Path(cargo["path"]).name != "cargo" or len({f["asset"]["sha256"] for f in forms}) != 2:
        raise SelectionError("original compiler or distinct harness bytes changed")
    manifest = Path(relative(spec["package_manifest"]))
    if manifest.name != "Cargo.toml" or not re.fullmatch(r"[A-Za-z0-9_-]+", spec["package"]):
        raise SelectionError("original same-target package binding changed")
    for form in forms:
        harness = Path(relative(form["harness"]))
        command = form["command"]
        if kamu and command != [
            cargo["path"], "test", "-p", "kamu-core", "--test", "oracle",
            "sync_ref_is_local_classifies_local_and_remote", "--", "--nocapture",
        ]:
            raise SelectionError("original Kamu Cargo argv changed")
        if (harness.suffix != ".rs" or harness.parent.name != "tests" or harness.stem != form["target"]
                or command[:2] != [cargo["path"], "test"] or command.count("test") != 1
                or command.count("--test") != 1 or command[-2:] != ["--", "--nocapture"]
                or command[command.index("--test") + 1] != form["target"]
                or command.count(expected[0]) != 1
                or command.count("-p") != 1 or command[command.index("-p") + 1] != spec["package"]
                or harness.parent != manifest.parent / "tests"
                or any(not isinstance(word, str) or not word or "\0" in word for word in command)
                or any(word in command for word in ("--no-run", "--locked", "--message-format"))
                or any(word.startswith("--message-format=") for word in command)):
            raise SelectionError("original same-target Cargo argv changed")
    for key, maximum in (("cpus", 16 if kamu else 8), ("timeout_seconds", 3600), ("max_log_bytes", 32 * 1024**2)):
        if type(spec.get(key)) is not int or not 0 < spec[key] <= maximum:
            raise SelectionError("invalid same-target resource bound")


@contextmanager
def harness_session(root, spec):
    """Swap exact private forms at their original path, then restore the mirror."""
    if spec.get("schema_version") != SAME_TARGET_SCHEMA:
        yield lambda form: None
        return
    path = root / relative(spec["forms"][0]["harness"])
    if any(parent.is_symlink() for parent in path.parents if parent.is_relative_to(root)):
        raise SelectionError("original harness parent redirected")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise SelectionError("original harness destination redirected")
    old = (path.read_bytes(), path.stat().st_mode & 0o777) if path.exists() else None
    created = []
    parent = path.parent
    while not parent.exists():
        created.append(parent)
        parent = parent.parent
    path.parent.mkdir(parents=True, exist_ok=True)

    def install(form):
        verify_files(root, spec)
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise SelectionError("staged original harness redirected")
        path.write_bytes(Path(form["asset"]["path"]).read_bytes())
        path.chmod(0o644)
        verify_form(root, spec, form)

    try:
        yield install
    finally:
        if any(parent.is_symlink() for parent in path.parents if parent.is_relative_to(root)):
            raise SelectionError("original harness parent changed during compilation")
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            raise SelectionError("original harness became a directory")
        if old is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(old[0])
            path.chmod(old[1])
        for directory in created:
            directory.rmdir()


def verify_form(root, spec, form):
    verify_files(root, spec)
    if spec.get("schema_version") == SAME_TARGET_SCHEMA:
        path = root / form["harness"]
        if (path.is_symlink() or not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != form["asset"]["sha256"]):
            raise SelectionError("staged original harness bytes changed")


def select_and_test(spec, root, folder, *, run=run_command):
    validate(spec)
    root = root.resolve()
    expected_cwd = root / spec.get("workdir", ".")
    if expected_cwd.resolve() != Path.cwd().resolve():
        raise SelectionError("Rust API selector is not in the current candidate build directory")
    verify_files(root, spec)
    with harness_session(root, spec) as install:
        return select_forms(spec, root, folder, run, install)


def select_forms(spec, root, folder, run, install):
    deadline = time.monotonic() + spec["timeout_seconds"]
    evidence = {"task_id": spec["task_id"], "probes": [], "tests": [],
                "selection_uses_test_results": False, "selected": []}
    selected = []
    for index, form in enumerate(spec["forms"]):
        install(form)
        verify_form(root, spec, form)
        record = run(probe_command(form["command"]), folder, "probe-" + str(index),
                     deadline, spec["max_log_bytes"])
        evidence["probes"].append({"form": form["name"], **record})
        verify_form(root, spec, form)
        if record.get("limit"):
            raise SelectionError("Rust API compilation probe exceeded " + record["limit"], evidence)
        if record["returncode"] == 0:
            if spec.get("schema_version") == SAME_TARGET_SCHEMA:
                artifacts = []
                for line in record["stdout"].splitlines():
                    try:
                        value = json.loads(line)
                    except ValueError:
                        continue
                    target = value.get("target", {})
                    if value.get("reason") == "compiler-artifact" and "test" in target.get("kind", []):
                        artifacts.append(value)
                if (len(artifacts) != 1 or artifacts[0]["target"].get("name") != form["target"]
                        or Path(artifacts[0]["target"].get("src_path", "")).resolve() != (root / form["harness"]).resolve()
                        or Path(artifacts[0].get("manifest_path", "")).resolve() != (root / spec["package_manifest"]).resolve()):
                    raise SelectionError("successful probe did not compile the original package/test target", evidence)
            selected.append(form)
        elif not private_api_mismatch(record, form["harness"], root):
            raise SelectionError("Rust API probe failed outside its frozen harness", evidence)
    evidence["selected"] = [form["name"] for form in selected]
    results = {name: bool(selected) for name in spec["expected_tests"]}
    # The complete set of compile-compatible forms is frozen before executing
    # any assertion. A failed assertion never causes a fallback/reselection.
    for index, form in enumerate(selected):
        install(form)
        verify_form(root, spec, form)
        record = run(form["command"], folder, "test-" + str(index), deadline, spec["max_log_bytes"])
        verify_form(root, spec, form)
        values = test_results(record, spec["expected_tests"])
        evidence["tests"].append({"form": form["name"], "results": values, **record})
        results = {name: value and values[name] for name, value in results.items()}
    evidence["results"] = results
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--source", type=Path, default=Path(os.environ.get("SOURCE_ROOT", "/workspace")))
    args = parser.parse_args()
    try:
        spec = json.loads(args.spec.read_text())
        validate(spec)
        if hasattr(os, "sched_getaffinity"):
            available = sorted(os.sched_getaffinity(0))
            count = min(spec["cpus"], len(available))
            offset = int.from_bytes(hashlib.sha256(spec["task_id"].encode()).digest()[:8], "big") % len(available)
            os.sched_setaffinity(0, {available[(offset + index) % len(available)] for index in range(count)})
        # Temp space belongs to the container/task lease, never a Blob mount.
        with tempfile.TemporaryDirectory(prefix="rust-api-selection-") as temporary:
            result = select_and_test(spec, args.source, Path(temporary))
        print("ReviewBench Rust API evidence: " + json.dumps(result, sort_keys=True), flush=True)
        for name, passed in result["results"].items():
            print("test " + name + " ... " + ("ok" if passed else "FAILED"), flush=True)
        return 0 if all(result["results"].values()) else 1
    except (SelectionError, OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        if isinstance(exc, SelectionError) and exc.evidence is not None:
            print("ReviewBench Rust API evidence: " + json.dumps(exc.evidence, sort_keys=True), flush=True)
        print("Rust API selection error: " + str(exc), file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
