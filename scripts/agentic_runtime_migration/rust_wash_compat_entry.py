#!/usr/bin/env python3
"""Compile-select the recorded wash569 test import on complete current source."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time

try:
    from . import rust_test_compat_entry as guard
    from .rust_build_entry import candidate_files, synchronize_candidate, tomllib
except ImportError:
    import rust_test_compat_entry as guard
    sys.path.insert(0, "/opt/reviewbench-assets/rust-build-tools")
    from build import candidate_files, synchronize_candidate, tomllib

api = guard.api
SelectionError = guard.SelectionError
SCHEMA = "reviewbench-rust-wash-test-compat/v1"
PROFILE = "wash569-public-test-import-v1"
TASK = "github__wasmCloud__wash-archive__pull_569__comment_1200946053__modularity"
SELECTION = guard.SELECTION
PATH = "src/ctl/mod.rs"
MARKER = b"#[cfg(test)]\nmod test {"
IMPORT = b"#[cfg(test)]\nuse wash_lib::cli::link::LinkPutCommand;\n\n"
ANCHOR = b"use crate::{\n    appearance::spinner::Spinner,"
FORMS = ("as-is", "with-import")
RAW_STRING = re.compile(rb'(?:br|r)(#{0,255})"')
CHAR_LITERAL = re.compile(
    rb"'(?:\\(?:u\{[0-9A-Fa-f_]+\}|x[0-9A-Fa-f]{2}|[^\r\n])|"
    rb"[\x00-\x09\x0b\x0c\x0e-\x26\x28-\x5b\x5d-\x7f]|"
    rb"[\xc2-\xdf][\x80-\xbf]|[\xe0-\xef][\x80-\xbf]{2}|[\xf0-\xf4][\x80-\xbf]{3})'"
)
CARGO_ERROR = re.compile(
    r"error: could not compile `wash-cli` due to "
    r"(?:(?P<count>[1-9][0-9]*) )?previous errors?"
    r"(?:; [0-9]+ warnings? emitted)?"
)
CARGO_PROGRESS = re.compile(
    guard.CARGO_PROGRESS.pattern.replace(
        r'(?:lib|lib test|test \"[^\"]+\")',
        r'(?:lib|lib test|test \"[^\"]+\"|bin \"wash\"(?: test)?)',
    )
)
CARGO_FINISHED = re.compile(r"Finished (?:dev|test|release) \[[^\r\n]+\] target\(s\) in "
                            r"(?:[0-9]+m )?[0-9]+(?:\.[0-9]+)?s")


def masked_rust(data):
    """Mask comments/literals without changing byte offsets used by rustc."""
    if len(data) > 4 * 1024**2:
        raise SelectionError("mixed Rust test module exceeds its source bound")
    result = bytearray(data)
    i, n = 0, len(data)
    while i < n:
        end = None
        if data[i:i + 2] == b"//":
            end = data.find(b"\n", i)
            end = n if end < 0 else end
        elif data[i:i + 2] == b"/*":
            depth, j = 1, i + 2
            while depth and j < n:
                if data[j:j + 2] == b"/*":
                    depth += 1
                    j += 2
                elif data[j:j + 2] == b"*/":
                    depth -= 1
                    j += 2
                else:
                    j += 1
            if depth:
                raise SelectionError("unterminated Rust block comment")
            end = j
        else:
            raw = RAW_STRING.match(data, i)
            if raw and (i == 0 or not chr(data[i - 1]).isalnum() and data[i - 1] != 95):
                closing = b'"' + raw[1]
                j = data.find(closing, raw.end())
                if j < 0:
                    raise SelectionError("unterminated Rust raw string")
                end = j + len(closing)
            elif data[i:i + 1] == b'"':
                j = i + 1
                while j < n and data[j:j + 1] != b'"':
                    j += 2 if data[j:j + 1] == b"\\" else 1
                if j >= n:
                    raise SelectionError("unterminated Rust string")
                end = j + 1
            elif data[i:i + 1] == b"'":
                # A lifetime is not a character literal. Accept only the exact
                # one-character/escape grammar before masking a closing quote.
                char = CHAR_LITERAL.match(data, i)
                if char:
                    end = char.end()
        if end is None:
            i += 1
        else:
            for j in range(i, end):
                if data[j] not in (10, 13):
                    result[j] = 32
            i = end
    return bytes(result)


def top_level_item(mask, at):
    """Require an item boundary outside every matched Rust token-tree delimiter."""
    stack = []
    closing = {ord(")"): ord("("), ord("]"): ord("["), ord("}"): ord("{")}
    top_level = False
    for index, token in enumerate(mask):
        if index == at:
            top_level = not stack
        if token in (ord("("), ord("["), ord("{")):
            stack.append(token)
        elif token in closing:
            if not stack or stack.pop() != closing[token]:
                raise SelectionError("mismatched Rust token-tree delimiter")
    if stack:
        raise SelectionError("unclosed Rust token-tree delimiter")
    if at == len(mask):
        top_level = True
    prefix = mask[:at].rstrip()
    return top_level and (not prefix or prefix[-1:] in (b";", b"}"))


def test_block(data, spec):
    record = spec["test_block"]
    mask = masked_rust(data)
    positions = [m.start() for m in re.finditer(re.escape(MARKER), mask)]
    if len(positions) != 1:
        raise SelectionError("original test module is absent, hidden or ambiguous")
    start = positions[0]
    if not top_level_item(mask, start):
        raise SelectionError("original test module gained an enclosing/conditional item")
    end = start + record["bytes"]
    block = data[start:end]
    if guard.digest(block) != record["sha256"]:
        raise SelectionError("original test assertions/body/identity changed")
    if not top_level_item(mask, end):
        raise SelectionError("original test module boundary changed")
    return start, end


def transformed(data, spec, form):
    start, end = test_block(data, spec)
    if form == "as-is":
        return data
    if form != "with-import":
        raise SelectionError("unknown wash compatibility form")
    mask = masked_rust(data)
    if IMPORT in mask:
        # Idempotent transport, not form selection: both forms still compile.
        at = mask.index(IMPORT)
        if (mask.count(IMPORT) != 1 or start <= at < end
                or not top_level_item(mask, at)):
            raise SelectionError("ambiguous pre-existing test import")
        return data
    anchors = [m.start() for m in re.finditer(re.escape(ANCHOR), mask)
               if m.start() < start and top_level_item(mask, m.start())]
    if len(anchors) == 1:
        at = anchors[0]
        result = data[:at] + IMPORT + data[at:]
    elif not anchors:
        # Item order does not change Rust import scope. Appending the same
        # cfg(test) item also supports candidate production-import reordering.
        if not top_level_item(mask, len(mask)):
            raise SelectionError("current candidate has no top-level import boundary")
        result = data + (b"" if data.endswith(b"\n") else b"\n") + IMPORT
    else:
        raise SelectionError("ambiguous current-candidate import insertion")
    test_block(result, spec)
    return result


def validate(spec):
    keys = {"schema_version", "profile", "task_id", "toolchain", "selection", "files",
            "test_block", "cpus", "timeout_seconds", "max_log_bytes", "mode", "command"}
    if spec.get("mode") == "grading":
        keys.add("expected_tests")
    if (set(spec) != keys or spec.get("schema_version") != SCHEMA
            or spec.get("profile") != PROFILE or spec.get("task_id") != TASK
            or spec.get("selection") != SELECTION or spec.get("toolchain") != "1.65.0"
            or spec.get("mode") not in {"public", "grading"} or spec.get("files") != []):
        raise SelectionError("unknown/outcome-dependent wash test compatibility profile")
    block = spec["test_block"]
    if (set(block) != {"path", "sha256", "bytes"} or block["path"] != PATH
            or not re.fullmatch(r"[0-9a-f]{64}", block["sha256"])
            or type(block["bytes"]) is not int or not 32 <= block["bytes"] <= 128 * 1024):
        raise SelectionError("invalid original inline-test binding")
    public = spec["mode"] == "public"
    tail = (["check", "--locked"] if public else
            ["test", "--locked", "--bin", "wash", "test_ctl_comprehensive", "--", "--nocapture"])
    command = spec["command"]
    if not isinstance(command, list) or command[:1] != ["cargo"] or command[-len(tail):] != tail:
        raise SelectionError("original full public/grading command changed")
    if command[1:-len(tail)] not in [
        ["--offline"],
        ["--offline", "--config", "/opt/reviewbench-assets/cargo-sources.toml"],
        ["--offline", "--config", 'source.crates-io.replace-with="reviewbench-vendor"',
         "--config", 'source.reviewbench-vendor.directory="/opt/reviewbench-assets/vendor"'],
    ]:
        raise SelectionError("unbound Cargo command prefix")
    if not public and spec["expected_tests"] != ["ctl::test::test_ctl_comprehensive"]:
        raise SelectionError("original fixed test identity changed")
    if (type(spec["cpus"]) is not int or not 1 <= spec["cpus"] <= 2
            or type(spec["timeout_seconds"]) is not int or not 1 <= spec["timeout_seconds"] <= 3000
            or type(spec["max_log_bytes"]) is not int or not 1024 <= spec["max_log_bytes"] <= 32 * 1024**2):
        raise SelectionError("invalid bounded wash resources")


def compiler_manifest(row, root):
    manifest, target = row.get("manifest_path"), row.get("target")
    if (not isinstance(manifest, str) or not isinstance(target, dict)
            or target.get("name") != "wash" or target.get("kind") != ["bin"]
            or not isinstance(target.get("src_path"), str)):
        return None
    manifest, path = Path(manifest), Path(target["src_path"])
    manifest = manifest if manifest.is_absolute() else root / manifest
    path = path if path.is_absolute() else root / path
    if manifest.resolve() != root / "Cargo.toml" or path.resolve() != root / "src/main.rs":
        return None
    return manifest


def failed_probe_messages(record, root, *, successful=False):
    """Same complete-stream accounting as FROST, bound to this binary only."""
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
            if not isinstance(message, dict) or not guard.diagnostic_children(message):
                return None
            level = message.get("level")
            if level == "error":
                if successful:
                    return None
                messages.append(row)
            elif level == "failure-note":
                if (successful or compiler_manifest(row, root) is None
                        or not guard.FAILURE_NOTE.fullmatch(message["message"])):
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
            if set(row) != {"reason", "success"} or row["success"] is not successful:
                return None
            finished = True
        else:
            return None
    for line in record.get("stderr", "").splitlines():
        line = line.strip()
        if not line:
            continue
        if successful and line == "warning: build failed, waiting for other jobs to finish...":
            return None
        summary = CARGO_ERROR.fullmatch(line)
        if summary:
            if successful:
                return None
            if summary["count"]:
                counts.append(int(summary["count"]))
        elif not CARGO_PROGRESS.fullmatch(line) and not (successful and CARGO_FINISHED.fullmatch(line)):
            return None
    return (messages, counts, notes) if finished else None


def import_mismatch(record, root, spec, form):
    if record.get("returncode") != 101 or guard.process_failure(record):
        return False
    parsed = failed_probe_messages(record, root)
    if parsed is None:
        return False
    data = (root / PATH).read_bytes()
    start, end = test_block(data, spec)
    ranges = [(start + m.start(), start + m.end())
              for m in re.finditer(rb"\bLinkPutCommand\b", data[start:end])]
    added = []
    if form == "with-import":
        for match in re.finditer(re.escape(IMPORT), masked_rust(data)):
            at = match.start() + IMPORT.index(b"wash_lib")
            added.append((at, match.start() + IMPORT.index(b";")))
    messages, counts, notes = parsed
    errors, codes, footers = [], set(), []
    for row in messages:
        message = row["message"]
        if compiler_manifest(row, root) is None:
            return False
        spans = message.get("spans")
        if not isinstance(spans, list) or not all(isinstance(span, dict) for span in spans):
            return False
        primary = [span for span in spans if span.get("is_primary")]
        abort = guard.ABORT.fullmatch(message.get("message", ""))
        if not primary and message.get("code") is None and abort:
            if abort["count"]:
                footers.append(int(abort["count"]))
            continue
        code = message.get("code")
        code = code.get("code") if isinstance(code, dict) else None
        allowed = (ranges if code in {"E0412", "E0422"} and form == "as-is" else
                   added if code in {"E0432", "E0433", "E0252"} and form == "with-import" else [])
        name_pattern = r"\bLinkPutCommand\b" if code in {"E0412", "E0422", "E0252"} else r"\b(?:LinkPutCommand|link|cli)\b"
        if not allowed or not primary or not re.search(name_pattern, message.get("message", "")):
            return False
        for span in primary:
            path = Path(span.get("file_name", ""))
            path = path if path.is_absolute() else root / path
            a, b = span.get("byte_start"), span.get("byte_end")
            if (path.resolve() != root / PATH or type(a) is not int or type(b) is not int
                    or not any(x <= a < b <= y for x, y in allowed)):
                return False
        errors.append(message)
        codes.add(code)
    return (bool(errors) and all(n == len(errors) for n in footers)
            and all(n == len(messages) for n in counts) and set(notes) <= codes)


def candidate_snapshot(source):
    files = candidate_files(source)
    if (len(files) > 10000 or any("target" in Path(name).parts for name in files)
            or sum(path.stat().st_size for path in files.values() if not path.is_symlink()) > 128 * 1024**2):
        raise SelectionError("candidate contains target outputs or exceeds the source-copy bound")
    return guard.fingerprint(source)


def verify_graph(spec, root, logs, deadline, run, evidence):
    """Resolve all current workspace dependencies, locked/offline; never retry."""
    command = spec["command"]
    at = command.index("check" if spec["mode"] == "public" else "test")
    metadata = command[:at] + ["metadata", "--format-version", "1", "--locked", "--offline"]
    before = candidate_snapshot(root)
    if "Cargo.lock" not in before:
        raise SelectionError("current candidate removed the original required Cargo.lock", evidence)
    record = run(metadata, logs, "graph", min(deadline, time.monotonic() + 120), spec["max_log_bytes"])
    evidence["graph"] = record
    # Metadata stdout is structured package data: keywords/descriptions may
    # legitimately contain SIGINT or other diagnostic-looking text. Retain the
    # complete original record; check process status, limits, byte completeness
    # and stderr here, then validate the entire JSON graph below. Compiler and
    # test stdout still pass through the unchanged shared diagnostic guard.
    guard.infrastructure({**record, "stdout": ""}, evidence)
    if record["returncode"] or candidate_snapshot(root) != before:
        raise SelectionError("full-current-candidate locked dependency graph failed or mutated", evidence)
    if any(line.strip() and not CARGO_PROGRESS.fullmatch(line.strip())
           for line in record.get("stderr", "").splitlines()):
        raise SelectionError("Cargo graph has an unaccounted stderr record", evidence)
    try:
        data = json.loads(record["stdout"])
    except ValueError as exc:
        raise SelectionError("full Cargo metadata has invalid framing", evidence) from exc
    manifests = {name for name in before if Path(name).name == "Cargo.toml"}
    if (not isinstance(data, dict) or Path(data.get("workspace_root", "")).resolve() != root
            or not isinstance(data.get("resolve"), dict) or not data["resolve"].get("nodes")
            or not data.get("workspace_members") or not data.get("packages")):
        raise SelectionError("Cargo metadata did not return its complete workspace graph", evidence)
    packages, nodes = data["packages"], data["resolve"]["nodes"]
    if (not isinstance(packages, list) or not isinstance(nodes, list)
            or not all(isinstance(item, dict) and isinstance(item.get("id"), str) for item in packages + nodes)):
        raise SelectionError("Cargo metadata package/node shape is invalid", evidence)
    package_ids, node_ids = {item["id"] for item in packages}, {item["id"] for item in nodes}
    if (len(package_ids) != len(packages) or len(node_ids) != len(nodes)
            or not set(data["workspace_members"]) <= node_ids <= package_ids
            or any(not isinstance(node.get("dependencies"), list)
                   or not set(node["dependencies"]) <= package_ids for node in nodes)):
        raise SelectionError("Cargo workspace graph lacks member packages", evidence)
    for package in data["packages"]:
        if package.get("source") is None:
            path = Path(package["manifest_path"]).resolve()
            if not path.is_relative_to(root) or path.relative_to(root).as_posix() not in manifests:
                raise SelectionError("Cargo resolved local production outside the full candidate", evidence)


def select(spec, source, folder, *, run=api.run_command):
    validate(spec)
    source, folder = source.resolve(), folder.resolve()
    if source == folder or source.is_relative_to(folder) or folder.is_relative_to(source):
        raise SelectionError("wash compatibility copies must be separate from candidate source")
    before = candidate_snapshot(source)
    path = source / PATH
    if path.is_symlink() or not path.is_file():
        raise SelectionError("original mixed source/test file is not a regular file")
    original = path.read_bytes()
    test_block(original, spec)
    evidence = {"selection_uses_test_results": False, "probes": [], "tests": [], "selected": []}
    deadline = time.monotonic() + spec["timeout_seconds"]
    copies = []
    for form in FORMS:
        root = folder / form / "source"
        synchronize_candidate(source, root, {})
        (root / PATH).write_bytes(transformed(original, spec, form))
        expected = candidate_snapshot(root)
        logs = folder / form / "logs"
        logs.mkdir()
        copies.append((form, root, logs, expected))
    with guard.invocation(copies[0][1], folder / FORMS[0] / "target"):
        verify_graph(spec, copies[0][1], copies[0][2], deadline, run, evidence)
    if candidate_snapshot(source) != before:
        raise SelectionError("candidate changed during dependency graph validation", evidence)
    selected = []
    for form, root, logs, expected in copies:
        if candidate_snapshot(source) != before or candidate_snapshot(root) != expected:
            raise SelectionError("candidate/test helper changed before compilation", evidence)
        command = list(spec["command"])
        at = command.index("check" if spec["mode"] == "public" else "test") + 1
        command[at:at] = (["--message-format=json"] if spec["mode"] == "public"
                         else ["--no-run", "--message-format=json"])
        with guard.invocation(root, folder / form / "target"):
            record = run(command, logs, "probe", deadline, spec["max_log_bytes"])
        evidence["probes"].append({"form": form, **record})
        if candidate_snapshot(source) != before or candidate_snapshot(root) != expected:
            raise SelectionError("candidate/test helper mutated during compilation", evidence)
        guard.infrastructure(record, evidence)
        if record["returncode"] == 0:
            if failed_probe_messages(record, root, successful=True) is None:
                raise SelectionError("successful probe has an incomplete/unaccounted compiler stream", evidence)
            selected.append((form, root, logs, expected))
        elif not import_mismatch(record, root, spec, form):
            raise SelectionError("compiler failure is outside the attested test import", evidence)
    evidence["selected"] = [form for form, _, _, _ in selected]
    if not selected:
        raise SelectionError("no compile-compatible original test import form", evidence)
    results = {name: True for name in spec.get("expected_tests", [])}
    for form, root, logs, expected in selected if spec["mode"] == "grading" else []:
        if candidate_snapshot(source) != before or candidate_snapshot(root) != expected:
            raise SelectionError("candidate/test helper mutated after selection", evidence)
        with guard.invocation(root, folder / form / "target"):
            record = run(spec["command"], logs, "tests", deadline, spec["max_log_bytes"])
        evidence["tests"].append({"form": form, **record})
        if candidate_snapshot(source) != before or candidate_snapshot(root) != expected:
            raise SelectionError("candidate/test helper mutated during assertions", evidence)
        guard.infrastructure(record, evidence)
        observed = api.test_results(record, spec["expected_tests"])
        results = {name: passed and observed[name] for name, passed in results.items()}
    evidence.update(results=results, public_build_ok=True)
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    args = parser.parse_args()
    try:
        spec = json.loads(args.spec.read_text())
        source = Path(os.environ.get("SOURCE_ROOT", "/workspace"))
        with tempfile.TemporaryDirectory(prefix="wash-test-compat-") as folder:
            evidence = select(spec, source, Path(folder))
        print("ReviewBench wash compatibility evidence: " + json.dumps(evidence, sort_keys=True), flush=True)
        for name, passed in evidence["results"].items():
            print("test " + name + " ... " + ("ok" if passed else "FAILED"), flush=True)
        return 0 if all(evidence["results"].values()) else 1
    except (SelectionError, OSError, ValueError, KeyError, TypeError) as exc:
        if isinstance(exc, SelectionError) and exc.evidence is not None:
            print("ReviewBench wash compatibility evidence: " + json.dumps(exc.evidence, sort_keys=True), flush=True)
        print("wash test compatibility infrastructure error: " + str(exc), file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
