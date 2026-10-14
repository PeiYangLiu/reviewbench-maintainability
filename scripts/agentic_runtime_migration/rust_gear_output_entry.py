#!/usr/bin/env python3
"""Share repeated grader metadata strings without dropping a byte of Cargo evidence.

Only the grading evidence transport changes. The pinned original execution entry,
public/model-facing commands, source, tests, compilers and 16-MiB host limit remain.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys

SCHEMA = "reviewbench-shared-grader-metadata-stdout/v1"
CORE_PATH = "/opt/reviewbench-assets/rust-build-tools/gear.py"
MAX_RECORDS = 8
MAX_PARTS = 100_000
MAX_TEXT = 32 * 1024**2


def require(value, message):
    if not value:
        raise ValueError("shared grader metadata: " + message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def records(evidence):
    result = [row["command_result"] for key in ("probes", "private_probes")
              for row in evidence.get(key, [])]
    require(len(result) <= MAX_RECORDS, "record bound exceeded")
    return result


def verify_stdout(row):
    data = row["stdout"].encode()
    require(row["stdout_utf8_valid"] is True and len(data) == row["stdout_total_bytes"]
            == row["stdout_captured_bytes"] and len(data) <= MAX_TEXT
            and digest(data) == row["stdout_sha256"], "literal stdout changed")


def segments(text):
    """Find exact raw package/node JSON spans; preserve numeric/Unicode spelling.

    The C JSON decoder finds value boundaries. No metadata value is rewritten or
    discarded; even whitespace and number representations survive reconstruction.
    """
    decoder = json.JSONDecoder()
    spans = []

    def skip(pos):
        while pos < len(text) and text[pos] in " \t\r\n":
            pos += 1
        return pos

    def array(pos):
        require(text[pos] == "[", "metadata sequence changed")
        pos = skip(pos + 1)
        while text[pos] != "]":
            start = pos
            _, pos = decoder.raw_decode(text, pos)
            spans.append((start, pos))
            pos = skip(pos)
            if text[pos] == "]":
                break
            require(text[pos] == ",", "invalid metadata sequence")
            pos = skip(pos + 1)
        return pos + 1

    def obj(pos, path=()):
        require(text[pos] == "{", "metadata object changed")
        pos = skip(pos + 1)
        while text[pos] != "}":
            key, pos = decoder.raw_decode(text, pos)
            pos = skip(pos)
            require(isinstance(key, str) and text[pos] == ":", "invalid metadata key")
            pos = skip(pos + 1)
            child = (*path, key)
            if child in {("packages",), ("resolve", "nodes")}:
                pos = array(pos)
            elif child == ("resolve",) and text[pos] == "{":
                pos = obj(pos, child)
            else:
                _, pos = decoder.raw_decode(text, pos)
            pos = skip(pos)
            if text[pos] == "}":
                break
            require(text[pos] == ",", "invalid metadata object")
            pos = skip(pos + 1)
        return pos + 1

    end = obj(skip(0))
    require(skip(end) == len(text), "metadata has a trailing payload")
    cursor = 0
    result = []
    for begin, end in spans:
        if cursor != begin:
            result.append((cursor, begin))
        result.append((begin, end))
        cursor = end
    if cursor != len(text):
        result.append((cursor, len(text)))
    return result


def share(base, target):
    lookup = {}
    for begin, end in segments(base):
        lookup.setdefault(base[begin:end], (begin, end))
    parts = []
    for begin, end in segments(target):
        text = target[begin:end]
        if parts and isinstance(parts[-1], list):
            at = parts[-1][1]
            if base[at:at + len(text)] == text:
                parts[-1][1] += len(text)
                continue
        match = lookup.get(text)
        if match is not None:
            parts.append(list(match))
        elif parts and isinstance(parts[-1], str):
            parts[-1] += text
        else:
            parts.append(text)
    require(len(parts) <= MAX_PARTS, "span inventory exceeded")
    require(expand(base, parts) == target, "sharing changed original stdout")
    return parts


def expand(base, parts):
    require(isinstance(parts, list) and len(parts) <= MAX_PARTS, "invalid span inventory")
    strings, length = [], 0
    for part in parts:
        if isinstance(part, str):
            text = part
        else:
            require(isinstance(part, list) and len(part) == 2
                    and all(type(n) is int for n in part)
                    and 0 <= part[0] < part[1] <= len(base), "invalid prior stdout span")
            text = base[part[0]:part[1]]
        length += len(text)
        require(length <= MAX_TEXT, "reconstructed stdout exceeds bound")
        strings.append(text)
    return "".join(strings)


def unpack(evidence):
    if "transport" not in evidence:
        return copy.deepcopy(evidence)
    result = copy.deepcopy(evidence)
    transport = result.pop("transport")
    require(transport.get("schema_version") == SCHEMA and result.get("role") == "grading"
            and re.fullmatch(r"[0-9a-f]{64}", transport.get("core_entry_sha256", "")),
            "unbound grading transport")
    previous = []
    for index, row in enumerate(records(result)):
        if "stdout_reference" in row:
            require("stdout" not in row, "ambiguous literal/reference stdout")
            reference = row.pop("stdout_reference")
            base = reference["record"]
            require(type(base) is int and 0 <= base < index, "stdout reference is cyclic or forward")
            require(previous[base]["stdout_sha256"] == reference["base_sha256"],
                    "prior Cargo stdout binding changed")
            row["stdout"] = expand(previous[base]["stdout"], reference["parts"])
        verify_stdout(row)
        previous.append(row)
    return result


def pack(evidence, core_sha256):
    require(evidence.get("role") == "grading" and "transport" not in evidence,
            "public/model-facing evidence must stay unchanged")
    require(re.fullmatch(r"[0-9a-f]{64}", core_sha256), "original execution entry is unbound")
    result = copy.deepcopy(evidence)
    raw_records = records(evidence)
    for index, row in enumerate(records(result)):
        verify_stdout(row)
        if row.get("returncode") != 0 or "metadata" not in row.get("command", []):
            continue
        choices = []
        for base, previous in enumerate(raw_records[:index]):
            if previous.get("returncode") != 0 or "metadata" not in previous.get("command", []):
                continue
            parts = share(previous["stdout"], row["stdout"])
            reference = {"record": base, "base_sha256": previous["stdout_sha256"], "parts": parts}
            choices.append((len(json.dumps(reference, ensure_ascii=False).encode()), reference))
        if choices:
            size, reference = min(choices, key=lambda item: item[0])
            if size + 1024 < len(json.dumps(row["stdout"], ensure_ascii=False).encode()):
                row.pop("stdout")
                row["stdout_reference"] = reference
    result["transport"] = {"schema_version": SCHEMA, "core_entry_sha256": core_sha256}
    require(unpack(result) == evidence, "complete execution evidence did not round-trip")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--private", required=True, type=Path)
    parser.add_argument("--core-sha256", required=True)
    args = parser.parse_args()
    original = Path(CORE_PATH)
    require(original.is_file() and not original.is_symlink()
            and digest(original.read_bytes()) == args.core_sha256, "original execution entry changed")
    sys.path.insert(0, str(original.parent))
    spec = importlib.util.spec_from_file_location("gear_original_pinned", original)
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    public, private = [json.loads(entry.core.bounded(p)) for p in (args.spec, args.private)]
    entry.require(args.task_id == public["task_id"], "task changed")
    root = Path("/opt/reviewbench-cache/rust-build/gear-environments")
    root.mkdir(parents=True, exist_ok=True)
    with (root / "execution.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cpus = sorted(os.sched_getaffinity(0))
        offset = int(entry.core.digest(entry.TASK.encode())[:16], 16) % len(cpus)
        os.sched_setaffinity(0, {cpus[(offset + i) % len(cpus)] for i in range(min(len(cpus), public["cpus"]))})
        try:
            evidence = entry.run_candidate(public, Path("/workspace"), root, private)
        except entry.core.VariantError as error:
            evidence = error.evidence or {"role": "grading", "status": "rejected", "exit_status": 2, "error": str(error)}
        shared = pack(evidence, args.core_sha256)
        if evidence["status"] == "completed":
            print("test " + entry.TEST + " ... " + ("ok" if evidence["test_results"][entry.TEST] else "FAILED"))
            print("test result: " + ("ok." if evidence["exit_status"] == 0 else "FAILED."))
        if evidence.get("error"):
            sys.stderr.write(evidence["error"] + "\n")
        print(entry.EVIDENCE_PREFIX + json.dumps(shared, sort_keys=True), flush=True)
        return evidence["exit_status"]


if __name__ == "__main__":
    raise SystemExit(main())
