#!/usr/bin/env python3
"""Probe both original VFS harnesses on complete current-candidate copies."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import tempfile
import time

try:
    from . import rust_test_compat_entry as guard
    from .rust_build_entry import candidate_files, synchronize_candidate, tomllib
except ImportError:
    import rust_test_compat_entry as guard
    from build import candidate_files, synchronize_candidate, tomllib

api = guard.api
SelectionError = guard.SelectionError
TASK = "github__manuel-woelker__rust-vfs__pull_54__comment_1341909533__modularity"
PROFILE = "vfs61-original-api-harnesses-v1"
SCHEMA = "reviewbench-rust-vfs-api/v1"
PLAN = "0170b037305e53006e34f372a0216eb41ac760236402268d9b74d2c7f2c33aff"
EXPECTED = ("in_memory_async_path_round_trips_file_contents",)
NAMES = ("oracle_start", "oracle_golden")  # Original target names, not a selector.
HARNESSES = {
    "tests/oracle_start.rs": "8a0d9ab586bdae67d8697ecaf29220b52dc6d193ee6dd7e09316bf63218db1e5",
    "tests/oracle_golden.rs": "390fb7638c9969d6387394d19fd4038b34c8f7b7fc02e0fa4841a64b52b4016e",
}
TOKENS = {"oracle_start": ("MemoryFS", "VfsPath"),
          "oracle_golden": ("AsyncMemoryFS", "AsyncVfsPath")}
FINISHED = re.compile(r"Finished `(?:test|dev)` profile \[[^\r\n]+\] target\(s\) in "
                      r"(?:[0-9]+m )?[0-9]+(?:\.[0-9]+)?s")


def arguments(name):
    return ["test", "--features", "async-vfs", "--test", name,
            "--", "--nocapture", "--test-threads=1"]


def surface_form(form):
    """A compile-only name probe, never a replacement assertion target."""
    target = "__reviewbench_vfs_api_" + form["name"]
    command = list(form["command"])
    command[command.index("--test") + 1] = target
    return {**form, "target": target, "harness": f"tests/{target}.rs", "command": command}


def surface_bytes(form):
    return ("use vfs::async_vfs::{" + ", ".join(TOKENS[form["name"]]) + "};\n").encode()


def validate(spec):
    keys = {"schema_version", "profile", "task_id", "plan_sha256", "toolchain", "workdir",
            "files", "expected_tests", "forms", "max_log_bytes", "cpus",
            "timeout_seconds", "selection"}
    if (set(spec) != keys or spec["schema_version"] != SCHEMA or spec["profile"] != PROFILE
            or spec["task_id"] != TASK or spec["plan_sha256"] != PLAN
            or spec["toolchain"] != "1.93.0" or spec["workdir"] != "."
            or spec["selection"] != guard.SELECTION or spec["expected_tests"] != list(EXPECTED)
            or spec["files"] != [{"path": k, "sha256": v} for k, v in HARNESSES.items()]
            or not isinstance(spec["forms"], list) or len(spec["forms"]) != 2):
        raise SelectionError("unknown/outcome-dependent VFS61 specification or original inputs")
    prefixes = []
    for form, name in zip(spec["forms"], NAMES):
        if (set(form) != {"name", "target", "harness", "command"}
                or (form["name"], form["target"], form["harness"]) != (name, name, f"tests/{name}.rs")):
            raise SelectionError("original VFS61 harness target changed")
        command, tail = form["command"], arguments(name)
        if not isinstance(command, list) or command[:1] != ["cargo"] or command[-len(tail):] != tail:
            raise SelectionError("original VFS61 command/features/assertion selection changed")
        prefix = command[1:-len(tail)]
        if prefix not in [
            ["--offline"],
            ["--offline", "--config", "/opt/reviewbench-assets/cargo-sources.toml"],
            ["--offline", "--config", 'source.crates-io.replace-with="reviewbench-vendor"',
             "--config", 'source.reviewbench-vendor.directory="/opt/reviewbench-assets/vendor"'],
        ]:
            raise SelectionError("unbound VFS61 Cargo source prefix")
        prefixes.append(prefix)
    if prefixes[0] != prefixes[1]:
        raise SelectionError("VFS61 forms have different dependency configurations")
    if (type(spec["cpus"]) is not int or not 1 <= spec["cpus"] <= 4
            or type(spec["timeout_seconds"]) is not int or not 1 <= spec["timeout_seconds"] <= 1800
            or type(spec["max_log_bytes"]) is not int or not 1024 <= spec["max_log_bytes"] <= 32 * 1024**2):
        raise SelectionError("invalid bounded VFS61 resources")


def snapshot(source):
    files = candidate_files(source)
    if (len(files) > 10000 or any("target" in Path(name).parts for name in files)
            or sum(p.stat().st_size for p in files.values() if not p.is_symlink()) > 128 * 1024**2):
        raise SelectionError("VFS candidate has old target outputs or exceeds the source bound")
    return guard.fingerprint(source)


def compiler_target(row, root, form):
    target, manifest = row.get("target"), row.get("manifest_path")
    if (not isinstance(target, dict) or not isinstance(manifest, str)
            or target.get("kind") != ["test"] or target.get("name") != form["target"]
            or not isinstance(target.get("src_path"), str)):
        return False
    manifest, source = Path(manifest), Path(target["src_path"])
    manifest = manifest if manifest.is_absolute() else root / manifest
    source = source if source.is_absolute() else root / source
    return (manifest.resolve() == root / "Cargo.toml"
            and source.resolve() == root / form["harness"])


def compiler_stream(record, root, form):
    """Account for every JSON/stderr record on success as well as failure."""
    messages, counts, notes = [], [], []
    finished = False
    success = record["returncode"] == 0
    package = tomllib.loads((root / "Cargo.toml").read_text())["package"]["name"]
    summary = re.compile(r"error: could not compile `" + re.escape(package)
                         + r"` \(test \"" + re.escape(form["target"])
                         + r"\"\) due to (?P<count>[1-9][0-9]*) previous errors?"
                         + r"(?:; [0-9]+ warnings? emitted)?")
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
            if not isinstance(message, dict) or not guard.diagnostic_children(message):
                return None
            level = message.get("level")
            if level == "error":
                if success:
                    return None
                messages.append(row)
            elif level == "failure-note":
                if (success or not compiler_target(row, root, form)
                        or not guard.FAILURE_NOTE.fullmatch(message["message"])):
                    return None
                notes.extend(re.findall(r"\bE[0-9]{4}\b", message["message"]))
            elif level not in {"warning", "note", "help"}:
                return None
        elif reason == "compiler-artifact":
            if (set(row) - {"reason", "package_id", "manifest_path", "target", "profile",
                            "features", "filenames", "executable", "fresh"}
                    or not isinstance(row.get("target"), dict)
                    or not isinstance(row.get("filenames"), list) or type(row.get("fresh")) is not bool):
                return None
        elif reason == "build-script-executed":
            if (set(row) - {"reason", "package_id", "linked_libs", "linked_paths", "cfgs", "env", "out_dir"}
                    or not all(isinstance(row.get(k), list) for k in ("linked_libs", "linked_paths", "cfgs", "env"))
                    or not isinstance(row.get("package_id"), str) or not isinstance(row.get("out_dir"), str)):
                return None
        elif reason == "build-finished":
            if set(row) != {"reason", "success"} or row["success"] is not success:
                return None
            finished = True
        else:
            return None
    if not finished:
        return None
    for line in record.get("stderr", "").splitlines():
        line = line.strip()
        if not line:
            continue
        match = summary.fullmatch(line)
        if match and not success:
            counts.append(int(match["count"]))
        elif (success and "build failed" in line) or not (
                guard.CARGO_PROGRESS.fullmatch(line) or success and FINISHED.fullmatch(line)):
            return None
    if not success and len(counts) != 1:
        return None
    return messages, counts, notes


def import_mismatch(record, root, form):
    if record.get("returncode") != 101 or guard.process_failure(record):
        return False
    parsed = compiler_stream(record, root, form)
    if parsed is None:
        return False
    messages, counts, notes = parsed
    data = (root / form["harness"]).read_bytes()
    # Cascading errors in an assertion body are never classified as a rename.
    # Only this exact, assertion-free surface probe can establish absent names.
    expected_target = "__reviewbench_vfs_api_" + form["name"]
    if (form["target"] != expected_target or form["harness"] != f"tests/{expected_target}.rs"
            or data != surface_bytes(form)):
        return False
    tokens = TOKENS[form["name"]]
    spans_allowed = [(m.start(), m.end()) for token in tokens
                     for m in re.finditer(rb"\b" + token.encode() + rb"\b", data)]
    errors, codes, abort_counts = [], set(), []
    for row in messages:
        message = row["message"]
        if not compiler_target(row, root, form):
            return False
        spans = message.get("spans")
        if not isinstance(spans, list) or not all(isinstance(s, dict) for s in spans):
            return False
        primary = [s for s in spans if s.get("is_primary")]
        abort = guard.ABORT.fullmatch(message["message"])
        if not primary and message.get("code") is None and abort:
            if abort["count"]:
                abort_counts.append(int(abort["count"]))
            continue
        code = message.get("code")
        if (not primary or not isinstance(code, dict)
                or code.get("code") not in {"E0412", "E0425", "E0432", "E0433"}
                or not any(re.search(r"\b" + token + r"\b", message["message"]) for token in tokens)):
            return False
        for span in primary:
            start, end = span.get("byte_start"), span.get("byte_end")
            name = span.get("file_name")
            if not isinstance(name, str):
                return False
            path = Path(name) if Path(name).is_absolute() else root / name
            if (path.resolve() != root / form["harness"] or type(start) is not int
                    or type(end) is not int or not any(a <= start < end <= b for a, b in spans_allowed)):
                return False
        errors.append(message)
        codes.add(code["code"])
    return (bool(errors) and all(n == len(errors) for n in abort_counts)
            and all(n == len(messages) for n in counts) and set(notes) <= codes)


def verify_graph(spec, root, logs, run, deadline, evidence):
    command = spec["forms"][0]["command"]
    command = command[:command.index("test")] + [
        "metadata", "--format-version", "1", "--offline", "--locked"]
    if not (root / "Cargo.lock").is_file():
        raise SelectionError("VFS61 dependency lock/preparation closure is not present", evidence)
    record = run(command, logs, "graph", min(deadline, time.monotonic() + 120), spec["max_log_bytes"])
    evidence["graph"] = record
    # Metadata stdout is package data, not a compiler diagnostic stream.
    # Keep the full record and byte/limit/status/stderr checks; validate its
    # complete stdout as JSON and the bound target graph below. Compile/test
    # stdout continues through the unchanged shared diagnostic guard.
    guard.infrastructure({**record, "stdout": ""}, evidence)
    if record["returncode"] or any(
            line.strip() and not guard.CARGO_PROGRESS.fullmatch(line.strip())
            for line in record.get("stderr", "").splitlines()):
        raise SelectionError("full current VFS dependency graph failed", evidence)
    try:
        graph = json.loads(record["stdout"])
    except ValueError as exc:
        raise SelectionError("VFS Cargo metadata has invalid framing", evidence) from exc
    if (not isinstance(graph, dict) or Path(graph.get("workspace_root", "")).resolve() != root
            or not isinstance(graph.get("resolve"), dict)
            or not isinstance(graph.get("packages"), list)):
        raise SelectionError("VFS metadata lacks its complete current workspace", evidence)
    packages, nodes = graph["packages"], graph["resolve"].get("nodes")
    if (not isinstance(nodes, list) or not nodes or not packages
            or not all(isinstance(p, dict) and isinstance(p.get("id"), str) for p in packages + nodes)):
        raise SelectionError("VFS metadata has invalid package/node identities", evidence)
    ids, resolved = {p["id"] for p in packages}, {p["id"] for p in nodes}
    members = graph.get("workspace_members")
    if (len(ids) != len(packages) or len(resolved) != len(nodes) or not isinstance(members, list)
            or not members or not set(members) <= resolved <= ids
            or any(not isinstance(n.get("dependencies"), list)
                   or not set(n["dependencies"]) <= ids for n in nodes)):
        raise SelectionError("VFS metadata omits current graph members", evidence)
    for package in packages:
        if package.get("source") is None:
            name = package.get("manifest_path")
            if not isinstance(name, str) or not Path(name).resolve().is_relative_to(root):
                raise SelectionError("VFS local production dependency escapes complete candidate", evidence)
    primary = [p for p in packages if p.get("manifest_path") == str(root / "Cargo.toml")]
    if len(primary) != 1 or not isinstance(primary[0].get("targets"), list):
        raise SelectionError("VFS metadata lacks the current root package targets", evidence)
    targets = primary[0]["targets"]
    if not all(isinstance(t, dict) for t in targets):
        raise SelectionError("VFS metadata target shape changed", evidence)
    libraries = [t for t in targets if t.get("name") == "vfs" and t.get("kind") == ["lib"]]
    if (len(libraries) != 1 or not isinstance(libraries[0].get("src_path"), str)
            or not Path(libraries[0]["src_path"]).resolve().is_relative_to(root)):
        raise SelectionError("original vfs import no longer binds to the current candidate library", evidence)
    # Existing files/IDs alone are insufficient: Cargo.toml could redirect a
    # named target to a different source file while leaving both originals.
    required = [*spec["forms"], *(surface_form(f) for f in spec["forms"])]
    for form in required:
        matches = [t for t in targets if t.get("name") == form["target"] and t.get("kind") == ["test"]]
        if (len(matches) != 1 or matches[0].get("src_path") != str(root / form["harness"])
                or matches[0].get("test") is not True):
            raise SelectionError("VFS Cargo target redirects or disables an original/probe harness", evidence)


def select(spec, source, folder, *, run=api.run_command):
    validate(spec)
    source, folder = source.resolve(), folder.resolve()
    if source == folder or source.is_relative_to(folder) or folder.is_relative_to(source):
        raise SelectionError("VFS candidate and fresh build copies must be separate")
    before = snapshot(source)
    api.verify_files(source, spec)
    evidence = {"task_id": TASK, "selection_uses_test_results": False,
                "api_probes": [], "probes": [], "tests": [], "selected": []}
    copies = []
    for form in spec["forms"]:
        path = source / surface_form(form)["harness"]
        if path.exists() or path.is_symlink():
            raise SelectionError("VFS generated probe collides with current source", evidence)
    for form in spec["forms"]:
        root = folder / form["name"] / "source"
        synchronize_candidate(source, root, {})
        for original in spec["forms"]:
            (root / surface_form(original)["harness"]).write_bytes(surface_bytes(original))
        logs = root.parent / "logs"
        logs.mkdir()
        copies.append((form, root, logs, snapshot(root)))
    def unchanged():
        if snapshot(source) != before or any(snapshot(root) != expected for _, root, _, expected in copies):
            raise SelectionError("VFS candidate/harness/lock changed during selection or tests", evidence)
    deadline = time.monotonic() + spec["timeout_seconds"]
    _, root, logs, _ = copies[0]
    with guard.invocation(root, root.parent / "target"):
        verify_graph(spec, root, logs, run, deadline, evidence)
    unchanged()
    supported = []
    for form, root, logs, expected in copies:
        probe = surface_form(form)
        unchanged()
        with guard.invocation(root, root.parent / "target"):
            record = run(api.probe_command(probe["command"]), logs, "api-probe", deadline, spec["max_log_bytes"])
        evidence["api_probes"].append({"form": form["name"], **record})
        unchanged()
        guard.infrastructure(record, evidence)
        if record["returncode"] == 0:
            if compiler_stream(record, root, probe) is None:
                raise SelectionError("successful VFS probe has an incomplete/unaccounted stream", evidence)
            supported.append((form, root, logs))
        elif not import_mismatch(record, root, probe):
            raise SelectionError("VFS surface compilation failed outside an original API-name token", evidence)
    if not supported:
        raise SelectionError("no compile-compatible original VFS harness", evidence)
    # Missing types can produce E0282 cascades throughout an async body. Do not
    # whitelist those errors: after the name probe, the complete unchanged
    # original harness must compile. Any failure aborts, never reselects.
    for form, root, logs in supported:
        unchanged()
        with guard.invocation(root, root.parent / "target"):
            record = run(api.probe_command(form["command"]), logs, "probe", deadline, spec["max_log_bytes"])
        evidence["probes"].append({"form": form["name"], **record})
        unchanged()
        guard.infrastructure(record, evidence)
        if record["returncode"] or compiler_stream(record, root, form) is None:
            raise SelectionError("complete original VFS harness failed after its API-name probe", evidence)
    evidence["selected"] = [form["name"] for form, _, _ in supported]
    results = dict.fromkeys(EXPECTED, True)
    for form, root, logs in supported:
        unchanged()
        with guard.invocation(root, root.parent / "target"):
            record = run(form["command"], logs, "tests", deadline, spec["max_log_bytes"])
        evidence["tests"].append({"form": form["name"], **record})
        unchanged()
        guard.infrastructure(record, evidence)
        observed = api.test_results(record, EXPECTED)
        results = {name: passed and observed[name] for name, passed in results.items()}
    evidence["results"] = results
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    args = parser.parse_args()
    try:
        spec = json.loads(args.spec.read_text())
        validate(spec)
        if hasattr(os, "sched_getaffinity"):
            os.sched_setaffinity(0, set(sorted(os.sched_getaffinity(0))[:spec["cpus"]]))
        with tempfile.TemporaryDirectory(prefix="vfs61-api-") as temporary:
            evidence = select(spec, Path(os.environ.get("SOURCE_ROOT", "/workspace")), Path(temporary))
        print("ReviewBench VFS61 evidence: " + json.dumps(evidence, sort_keys=True), flush=True)
        for name, passed in evidence["results"].items():
            print(f"test {name} ... " + ("ok" if passed else "FAILED"), flush=True)
        return 0 if all(evidence["results"].values()) else 1
    except (SelectionError, OSError, ValueError, KeyError, TypeError) as exc:
        if isinstance(exc, SelectionError) and exc.evidence is not None:
            print("ReviewBench VFS61 evidence: " + json.dumps(exc.evidence, sort_keys=True), flush=True)
        print("VFS61 selection infrastructure error: " + str(exc), file=os.sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
