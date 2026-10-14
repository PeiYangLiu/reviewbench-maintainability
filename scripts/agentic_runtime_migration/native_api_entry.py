"""Compile-only current-candidate API selection; never select using test results.

This dependency-free runner is also a public build tool. Private assertions,
expected outputs and harness paths live only in a separate grader-only spec.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import zipfile


SCHEMA = "reviewbench-native-api/v1"
MAX_FILES = 20000
MAX_SOURCE_BYTES = 256 * 1024**2
COMPILER_INFRASTRUCTURE_DIAGNOSTIC = re.compile(
    r"(?m)^(?:[^\s:]+(?::\d+){0,2}:\s*)?"
    r"(?:fatal error: (?:Killed signal terminated program\b|cannot execute\b)"
    r"|internal compiler error:"
    r"|error: unable to execute command: (?:Killed|Segmentation fault)\b"
    r"|error: (?:clang(?:\+\+)? frontend|linker) command failed due to signal\b)"
    r"|^LLVM ERROR:\s"
)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    os.replace(temporary, path)


def relative(path, workspace):
    name = Path(path)
    if name.is_absolute() or ".." in name.parts or not name.parts:
        raise ValueError("native API output must stay in the candidate build directories")
    value = workspace / name
    if not value.resolve().is_relative_to(workspace) or value.is_symlink():
        raise ValueError("native API output traverses a link")
    return value


def validate(spec):
    if spec.get("schema_version") != SCHEMA or spec.get("kind") not in {"public", "grader"}:
        raise ValueError("unknown native API specification")
    expected = "build-native-api-" + spec["kind"]
    if spec.get("state_directory") != expected:
        raise ValueError("native API state directory is not the declared isolated build directory")
    forms = spec.get("forms")
    if not isinstance(forms, list) or not 1 <= len(forms) <= 2:
        raise ValueError("native API selection needs one or two bounded forms")
    names = [form.get("name", "") for form in forms]
    if len(set(names)) != len(names) or any(not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", n) for n in names):
        raise ValueError("invalid or duplicate native API form")
    if not 1 <= spec.get("timeout_seconds", 0) <= 7200:
        raise ValueError("native API command timeout is not bounded")
    if not 1024 <= spec.get("max_log_bytes", 0) <= 32 * 1024**2:
        raise ValueError("native API command log bound is invalid")
    commands = list(spec.get("setup", []))
    for form in forms:
        commands += form.get("setup", []) + form.get("build", [])
        probe = form.get("probe", {})
        if set(probe) == {"command"}:
            commands.append(probe["command"])
        elif set(probe) != {"compile_database", "source"}:
            raise ValueError("native API probe must be a compiler command or one compilation-database entry")
        if spec["kind"] == "grader" and not form.get("executable"):
            raise ValueError("native API grader form lacks its original executable")
    if (any(not isinstance(cmd, str) or not cmd.strip() for cmd in commands)
            or any("ORACLE_VARIANT" in cmd for cmd in commands)):
        raise ValueError("native API commands must not inspect reference variant labels")
    if spec["kind"] == "public" and any(
            key in spec for key in ("test_command", "expected_tests", "harnesses", "golden_patch")):
        raise ValueError("public native API spec contains private grading data")
    if spec["kind"] == "grader" and not spec.get("test_command"):
        raise ValueError("native API spec lacks the complete original assertion command")
    return spec


def dependencies(spec, workspace):
    """Copy only attested source assets; no former build/install directories."""
    for item in spec.get("file_bindings", []):
        if digest(item["path"]) != item["sha256"]:
            raise ValueError("native API frozen input changed: " + item["path"])
    for guard in spec.get("source_guards", []):
        source = relative(guard["path"], workspace).read_text()
        if any(token not in source for token in guard["required_literals"]):
            raise ValueError("current candidate changed its prepared public dependency declaration")
    for item in spec.get("source_copies", []):
        source, destination = Path(item["source"]), relative(item["destination"], workspace)
        if source.is_symlink() or not source.is_dir():
            raise ValueError("native API source asset is absent or linked")
        entries = list(source.rglob("*"))
        if (len(entries) > MAX_FILES
                or sum(p.stat().st_size for p in entries if p.is_file()) > MAX_SOURCE_BYTES
                or any(p.is_symlink() and not p.resolve().is_relative_to(source.resolve()) for p in entries)):
            raise ValueError("native API source asset exceeds its bounded source tree")
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination, symlinks=True)
    for item in spec.get("source_zips", []):
        source = relative(item["path"], workspace)
        if digest(source) != item["sha256"]:
            raise ValueError("current candidate changed its pinned bundled dependency archive")
        destination = relative(item["destination"], workspace)
        if destination.exists():
            shutil.rmtree(destination)
        destination.mkdir(parents=True)
        with zipfile.ZipFile(source) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_FILES or sum(p.file_size for p in entries) > MAX_SOURCE_BYTES:
                raise ValueError("native API bundled dependency exceeds its source bound")
            roots = {Path(p.filename).parts[0] for p in entries if Path(p.filename).parts}
            if len(roots) != 1:
                raise ValueError("native API source archive has ambiguous roots")
            for entry in entries:
                path = Path(entry.filename)
                if path.is_absolute() or ".." in path.parts or (entry.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError("native API source archive has a linked or escaping entry")
                parts = path.parts[1:]
                if not parts:
                    continue
                target = destination.joinpath(*parts)
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(entry) as stream, target.open("xb") as output:
                        shutil.copyfileobj(stream, output)


def syntax_command(probe, workspace):
    """Use the real CMake target's compiler/includes, but never link or run it."""
    database = relative(probe["compile_database"], workspace)
    rows = json.loads(database.read_text())
    wanted = Path(probe["source"]).resolve()
    entries = [row for row in rows if (Path(row["directory"]) / row["file"]).resolve() == wanted]
    if len(entries) != 1:
        raise ValueError("native API probe lacks one unambiguous real compiler invocation")
    row = entries[0]
    if Path(row["directory"]).resolve() != workspace and not Path(row["directory"]).resolve().is_relative_to(workspace):
        raise ValueError("native API compilation database escaped the candidate")
    words = row.get("arguments") or shlex.split(row["command"])
    compiler_at = 1 if words and Path(words[0]).name == "ccache" else 0
    if (len(words) <= compiler_at
            or not re.fullmatch(r"(?:.*/)?(?:g\+\+|c\+\+|clang\+\+)(?:-\d+)?", words[compiler_at])):
        raise ValueError("native API CMake probe requires its literal C++ compiler")
    result, skip = [], False
    for word in words:
        if skip:
            skip = False
        elif word in {"-o", "-MF", "-MT", "-MQ"}:
            skip = True
        elif word not in {"-c", "-MD", "-MMD", "-MP"}:
            result.append(word)
    result.append("-fsyntax-only")
    return "cd " + shlex.quote(row["directory"]) + " && " + shlex.join(result)


def run_logged(command, state, label, spec, *, echo=True):
    directory = state / label
    directory.mkdir(parents=True, exist_ok=False)
    out, err = directory / "stdout", directory / "stderr"
    environment = {key: value for key, value in os.environ.items() if key != "ORACLE_VARIANT"}
    start = time.monotonic()
    print("NATIVE_API_BEGIN " + json.dumps({"stage": label, "command": command}), flush=True)
    with out.open("wb") as stdout, err.open("wb") as stderr:
        process = subprocess.Popen(
            ["/bin/bash", "-c", command], stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL,
            env=environment, start_new_session=True,
        )
        timed_out, log_limit = False, False
        while process.poll() is None:
            timed_out = time.monotonic() - start > spec["timeout_seconds"]
            log_limit = out.stat().st_size + err.stat().st_size > spec["max_log_bytes"]
            if timed_out or log_limit:
                os.killpg(process.pid, signal.SIGKILL)
                break
            time.sleep(0.05)
        code = process.wait()
    log_limit |= out.stat().st_size + err.stat().st_size > spec["max_log_bytes"]
    # Bash encodes child signals as 128+signal; GNU timeout/launch failures
    # occupy 124-127. GCC can additionally return 1 after cc1plus was killed.
    # None of these says that the candidate lacks one of the probed APIs.
    infrastructure_failure = code < 0 or code >= 124
    if code and not log_limit:
        infrastructure_failure |= any(
            COMPILER_INFRASTRUCTURE_DIAGNOSTIC.search(path.read_text(errors="replace"))
            is not None for path in (out, err)
        )
    record = {
        "stage": label, "command": command, "returncode": code, "timed_out": timed_out,
        "log_limit_exceeded": log_limit, "duration_seconds": round(time.monotonic() - start, 6),
        "compiler_infrastructure_failure": infrastructure_failure,
        "stdout_sha256": digest(out), "stderr_sha256": digest(err),
        "stdout_bytes": out.stat().st_size, "stderr_bytes": err.stat().st_size,
    }
    atomic(directory / "command.json", record)
    if echo and not log_limit:
        sys.stdout.flush()
        sys.stderr.flush()
        sys.stdout.buffer.write(out.read_bytes())
        sys.stderr.buffer.write(err.read_bytes())
        sys.stdout.flush()
        sys.stderr.flush()
    print("NATIVE_API_RECORD " + json.dumps(record, sort_keys=True), flush=True)
    if timed_out or log_limit or infrastructure_failure:
        raise ValueError("native API compiler infrastructure failed; no compatibility fallback")
    return record


def prepare(spec, workspace, *, runner=run_logged):
    state = relative(spec["state_directory"], workspace)
    if state.exists():
        raise ValueError("native API attempt already exists; preserve it and use a fresh sandbox")
    state.mkdir(parents=True)
    dependencies(spec, workspace)
    for index, command in enumerate(spec.get("setup", [])):
        if runner(command, state, f"setup-{index}", spec)["returncode"]:
            raise ValueError("native API required common build setup failed")
    compiled, records = [], []
    # Finish and record the ENTIRE compatibility set before building/asserting.
    for form in spec["forms"]:
        for index, command in enumerate(form.get("setup", [])):
            if runner(command, state, f'{form["name"]}/setup-{index}', spec)["returncode"]:
                raise ValueError("native API required probe configuration failed")
        probe = form["probe"]
        command = probe["command"] if "command" in probe else syntax_command(probe, workspace)
        record = runner(command, state, form["name"] + "/probe", spec)
        records.append(record)
        if record["returncode"] == 0:
            compiled.append(form["name"])
    selection = {
        "schema_version": SCHEMA, "spec_sha256": hashlib.sha256(
            json.dumps(spec, sort_keys=True).encode()).hexdigest(),
        "selected": compiled, "probes": records, "builds_completed": False,
        "method": "compile-only current candidate; all compatible forms; conjunctive assertions",
    }
    atomic(state / "selection.json", selection)
    print("NATIVE_API_SELECTION " + json.dumps(selection, sort_keys=True), flush=True)
    if not compiled:
        raise ValueError("no frozen harness API form compiles against the current candidate")
    binaries, failed = {}, False
    for form in spec["forms"]:
        if form["name"] not in compiled:
            continue
        for index, command in enumerate(form.get("build", [])):
            record = runner(command, state, f'{form["name"]}/build-{index}', spec)
            if record["returncode"]:
                failed = True
                break
        else:
            if spec["kind"] == "grader":
                source = relative(form["executable"], workspace)
                target = state / form["name"] / "compiled-executable"
                if not source.is_file():
                    raise ValueError("native API selected build did not produce its original executable")
                shutil.copy2(source, target)
                binaries[form["name"]] = {"sha256": digest(target), "destination": form["executable"]}
    selection.update(builds_completed=not failed, binaries=binaries)
    atomic(state / "selection.json", selection)
    if failed:
        raise ValueError("a compile-compatible form failed its mandatory build; selection is not retried")
    return selection


def run_assertions(spec, workspace, *, runner=run_logged):
    if spec["kind"] != "grader":
        raise ValueError("public native API build has no assertion phase")
    state = relative(spec["state_directory"], workspace)
    selection = json.loads((state / "selection.json").read_text())
    wanted = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    if (selection.get("spec_sha256") != wanted or not selection.get("builds_completed")
            or not selection.get("selected")
            or set(selection["selected"]) != set(selection.get("binaries", {}))):
        raise ValueError("native API grading lacks its complete hash-bound compile selection")
    runs = []
    for name in selection["selected"]:
        binary = state / name / "compiled-executable"
        binding = selection["binaries"][name]
        if digest(binary) != binding["sha256"]:
            raise ValueError("native API freshly compiled executable changed")
        destination = relative(binding["destination"], workspace)
        shutil.copy2(binary, destination)
        # Always use the COMPLETE original command. Failure never changes the
        # compile-selected set, retries a probe, or skips another selected form.
        runs.append(runner(spec["test_command"], state, name + "/assertions", spec))
    result = {"selected": selection["selected"], "runs": runs,
              "success": all(row["returncode"] == 0 for row in runs)}
    atomic(state / "assertions.json", result)
    print("NATIVE_API_ASSERTIONS " + json.dumps(result, sort_keys=True), flush=True)
    return 0 if result["success"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--phase", choices=("prepare", "run"), required=True)
    args = parser.parse_args(argv)
    try:
        spec = validate(json.loads(args.spec.read_text()))
        workspace = Path.cwd().resolve()
        if args.phase == "prepare":
            prepare(spec, workspace)
            return 0
        return run_assertions(spec, workspace)
    except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print("Native API compatibility error: " + str(exc), file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
