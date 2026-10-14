"""Read-only, evidence-bound inputs for published-legacy runtime migration.

This is an *input* adapter, not a runtime recipe or a correctness verifier.
``capture`` records observations, including missing evidence. ``load_inventory``
requires an independently supplied SHA-256, reopens the original inputs, and
recomputes the observations. Neither operation executes historical commands,
creates tests, chooses another golden, nor adds ``lane_results_source``.

Path aliases are explicit. In particular, ${HOME} is NOT expanded from the
process environment, and a missing SESSION/recovery is never silently replaced
with another recovery tree. A caller may bind a retained home and individual
source_artifact files explicitly.

The normalized contract retains original field values/types and six separate
start/golden observations. Counts are not test identities; successful status
arrays are not log bindings; captured hashes are not historical attestations.
An execution adapter must still preserve fixed tests, package pinned inputs,
and pass the engine's developer check and three start/three source-only-golden
replays through its normal resource gate.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping


SCHEMA = "reviewbench-agentic-legacy-inputs/v1"
MAX_FILE_BYTES = 64 * 1024 * 1024
PHASES = ("start", "golden")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
HISTORICAL_PREFIXES = (
    "strict_screen_20260726/", "oracle_recovery/", "oracle_results/",
    "recovery/", "increment5-recovery/",
)
class LegacyInputError(ValueError):
    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(f"{code}: {detail}")


def canonical_sha256(value: Any) -> str:
    """Hash typed JSON, not Python equality (True and 1 are different)."""
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        allow_nan=False,
    ).encode()).hexdigest()


def _json(data: bytes, label: str) -> Any:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise LegacyInputError("duplicate_key", f"{label}: {key}")
            result[key] = value
        return result

    def nonfinite(value):
        raise LegacyInputError("invalid_json", f"{label}: {value}")

    try:
        return json.loads(data, object_pairs_hook=unique, parse_constant=nonfinite)
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, LegacyInputError):
            raise
        raise LegacyInputError("invalid_json", label) from exc


def _relative(value: str) -> str:
    if (not isinstance(value, str) or not value or "\\" in value
            or any(ord(c) < 32 for c in value) or "${" in value
            or value.startswith(("~", "/")) or re.match(r"^[A-Za-z]:", value)
            or any(p in ("", ".", "..") for p in value.split("/"))):
        raise LegacyInputError("path_boundary", repr(value))
    if any(c in value for c in "*?{}"):
        raise LegacyInputError("unresolved_path_pattern", value)
    return value


def _open_beneath(base: Path, relative: str, *, directory=False) -> int:
    """Open relative to a trusted directory FD, rejecting every symlink."""
    _relative(relative)
    descriptor = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
    try:
        parts = relative.split("/")
        for index, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if index < len(parts) - 1 or directory:
                flags |= os.O_DIRECTORY
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        mode = os.fstat(descriptor).st_mode
        if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
            raise LegacyInputError("not_regular_file", relative)
        result, descriptor = descriptor, -1
        return result
    except FileNotFoundError as exc:
        raise LegacyInputError("missing_file", str(base / relative)) from exc
    except OSError as exc:
        raise LegacyInputError("unsafe_file", str(base / relative)) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_descriptor(descriptor: int, label: str) -> bytes:
    with os.fdopen(descriptor, "rb") as stream:
        if os.fstat(stream.fileno()).st_size > MAX_FILE_BYTES:
            raise LegacyInputError("file_too_large", label)
        data = stream.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise LegacyInputError("file_too_large", label)
        return data


class Roots:
    """Only regular files below explicit roots; no symlink components."""

    def __init__(self, roots: Mapping[str, str | Path]):
        self.paths = {}
        self._aliases = {}
        for name, value in roots.items():
            path = Path(value)
            if not path.is_absolute() or ".." in path.parts:
                raise LegacyInputError("root_not_absolute", str(path))
            declared = path
            path = path.resolve(strict=True)
            if not path.is_dir():
                raise LegacyInputError("root_not_directory", str(path))
            self.paths[name] = path
            # The caller's explicit root may itself be a deployment symlink.
            # Admit that spelling only; links *below* the root remain rejected.
            self._aliases[(name, declared)] = path
            self._aliases[(name, path)] = path
        if not {"release", "session", "oracle"} <= self.paths.keys():
            raise LegacyInputError("missing_roots", "release, session, oracle required")
        if self.paths["oracle"].parent != self.paths["session"]:
            raise LegacyInputError("oracle_session_mismatch", str(self.paths["oracle"]))

    def locate(self, path: Path) -> tuple[str, str]:
        if not path.is_absolute() or ".." in path.parts:
            raise LegacyInputError("path_boundary", str(path))
        for (name, spelling), _ in sorted(self._aliases.items(), key=lambda item: -len(item[0][1].parts)):
            if path.is_relative_to(spelling) and path != spelling:
                return name, _relative(path.relative_to(spelling).as_posix())
        raise LegacyInputError("path_boundary", str(path))

    def path(self, root: str, relative: str) -> Path:
        if root not in self.paths:
            raise LegacyInputError("unknown_root", root)
        return self.paths[root] / _relative(relative)

    def _open(self, root: str, relative: str, *, directory=False) -> int:
        self.path(root, relative)
        # openat + O_NOFOLLOW checks each component at the point of use, not
        # just a preceding resolve()/is_symlink() check. O_NONBLOCK avoids
        # hanging on a substituted FIFO before fstat rejects it.
        return _open_beneath(self.paths[root], relative, directory=directory)

    def read(self, root: str, relative: str, expected_sha256: str | None = None) -> bytes:
        if expected_sha256 is not None and (
                not isinstance(expected_sha256, str) or not SHA256.fullmatch(expected_sha256)):
            raise LegacyInputError("invalid_sha256", relative)
        data = _read_descriptor(self._open(root, relative), relative)
        if expected_sha256 is not None and hashlib.sha256(data).hexdigest() != expected_sha256:
            raise LegacyInputError("sha256_mismatch", str(self.path(root, relative)))
        return data

    def directory(self, path: Path) -> None:
        root, relative = self.locate(path)
        os.close(self._open(root, relative, directory=True))

    def legacy_path(self, value: str, *, directory: Path | None = None) -> Path:
        if not isinstance(value, str) or not value:
            raise LegacyInputError("unbound_path", repr(value))
        aliases = {
            "${REVIEWBENCH_SESSION_FILES}/": "session",
            "${SESSION_FILES}/": "session",
            "session files/": "session",
            "${HOME}/": "home",
        }
        for prefix, name in aliases.items():
            if value.startswith(prefix):
                if name not in self.paths:
                    raise LegacyInputError("unbound_path_alias", prefix)
                return self.path(name, value[len(prefix):])
        if value.startswith(("oracle_recovery/", "oracle_results/")):
            return self.path("session", "strict_screen_20260726/" + value)
        if value.startswith(("strict_screen_20260726/", "recovery/", "increment5-recovery/")):
            return self.path("session", value)
        if value.startswith("/"):
            path = Path(value)
            name, relative = self.locate(path)
            return self.path(name, relative)
        relative = _relative(value)
        if directory is None:
            raise LegacyInputError("unbound_relative_path", value)
        result = directory / relative
        self.locate(result)
        return result

    def comparable(self, value: Any) -> Any:
        """Undo only explicit release root spellings; retain all other values."""
        if isinstance(value, dict):
            return {k: self.comparable(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.comparable(v) for v in value]
        if isinstance(value, str):
            for alias, root in (
                ("${REVIEWBENCH_SESSION_FILES}/", "session"),
                ("${SESSION_FILES}/", "session"), ("session files/", "session"),
                ("${HOME}/", "home"),
            ):
                if root in self.paths:
                    value = value.replace(alias, str(self.paths[root]) + "/")
        return value


class _Capture:
    def __init__(self, roots: Roots):
        self.roots = roots
        self.files: dict[tuple[str, str], tuple[dict, bytes | None]] = {}

    def file(self, path: Path, expected: str | None = None) -> tuple[dict, bytes | None]:
        root, relative = self.roots.locate(path)
        path = self.roots.path(root, relative)
        key = root, relative
        if key not in self.files:
            ref = {"root": root, "path": relative, "absolute_path": str(path),
                   "sha256": None, "hash_basis": "captured_bytes_not_historical_attestation"}
            try:
                data = self.roots.read(root, relative)
                ref.update(state="present", sha256=hashlib.sha256(data).hexdigest(), size=len(data))
            except LegacyInputError as exc:
                ref.update(state=exc.code, error=str(exc))
                data = None
            self.files[key] = ref, data
        cached, data = self.files[key]
        ref = deepcopy(cached)
        if expected is not None:
            ref["declared_sha256"] = expected
            if not isinstance(expected, str) or not SHA256.fullmatch(expected):
                ref.update(state="invalid_sha256")
            elif data is not None and ref["sha256"] != expected:
                ref.update(state="sha256_mismatch")
            elif data is not None:
                ref["hash_basis"] = "original_declared_sha256"
        return ref, data

    def jsonl(self, path: Path, expected: str | None = None) -> tuple[dict, list[tuple[int, dict]]]:
        ref, data = self.file(path, expected)
        if ref["state"] != "present":
            raise LegacyInputError(ref["state"], str(path))
        rows, seen = [], set()
        for number, line in enumerate(data.splitlines(), 1):
            if not line.strip():
                continue
            row = _json(line, f"{path}:{number}")
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
                raise LegacyInputError("invalid_record", f"{path}:{number}")
            if row["id"] in seen:
                raise LegacyInputError("duplicate_id", f"{path}:{row['id']}")
            seen.add(row["id"])
            rows.append((number, row))
        return ref, rows


def _row_ref(ref: dict, line: int, value: dict) -> dict:
    return {**ref, "line": line, "id": value["id"], "record_sha256": canonical_sha256(value)}


def _lane(task: dict) -> str:
    language = str(task.get("inferred_language", "")).lower()
    for names, lane in (
        ({"go", "golang", "python"}, "gopy"), ({"java", "scala", "kotlin"}, "jvm"),
        ({"typescript", "javascript"}, "tsjs"), ({"c#", "csharp"}, "csharp"),
        ({"rust"}, "rust"), ({"c", "c++", "c/c++", "native"}, "native"),
    ):
        if language in names:
            return lane
    return "scripted"


def _source(capture: _Capture, task: dict, oracle: dict, source_files: dict) -> dict:
    provenance = oracle.get("release_oracle_provenance")
    result = {"release_oracle_provenance": deepcopy(provenance), "record": None, "reference": None}
    if not isinstance(provenance, dict) or not (provenance.get("file") or provenance.get("source_artifact")):
        return {**result, "status": "unattributed_release_record"}
    try:
        if provenance.get("file") and provenance.get("source_artifact"):
            raise LegacyInputError("ambiguous_provenance", task["id"])
        if provenance.get("file"):
            relative = _relative(provenance["file"])
            if not relative.startswith("oracle_results/") or not relative.endswith(".jsonl"):
                raise LegacyInputError("invalid_provenance_file", relative)
            path = capture.roots.legacy_path(relative)
            number = provenance.get("line")
            if type(number) is not int or number < 1:
                raise LegacyInputError("invalid_provenance_line", repr(number))
        else:
            name = _relative(provenance["source_artifact"])
            if "/" in name or not name.endswith(".jsonl"):
                raise LegacyInputError("invalid_source_artifact", name)
            path = Path(source_files[name]) if name in source_files else capture.roots.path("session", name)
            number = None
        ref, _ = capture.file(path, provenance.get("sha256"))
        result["reference"] = ref
        ref, records = capture.jsonl(path, provenance.get("sha256"))
        matches = [(n, r) for n, r in records if (n == number if number is not None else r["id"] == task["id"])]
        if len(matches) != 1:
            raise LegacyInputError("source_record_missing", str(path))
        number, original = matches[0]
        result.update(reference=_row_ref(ref, number, original), record=original)
        for key in ("id", "repo", "start_sha", "target_sha"):
            if key in original and key in task and original[key] != task[key]:
                raise LegacyInputError("source_identity_mismatch", key)
        if original["id"] != task["id"]:
            raise LegacyInputError("source_identity_mismatch", "id")
        # No merging of a convenient command/test list from a different run.
        # Release-added fields are allowed; every source-owned field survives.
        for key, value in original.items():
            if key not in oracle or canonical_sha256(capture.roots.comparable(value)) != canonical_sha256(
                    capture.roots.comparable(oracle[key])):
                raise LegacyInputError("source_release_mismatch", key)
        return {**result, "status": "original_record_bound"}
    except LegacyInputError as exc:
        return {**result, "status": exc.code, "error": str(exc)}


def _directory(oracle: dict, roots: Roots) -> Path | None:
    artifacts = oracle.get("artifacts")
    value = (artifacts.get("directory") or artifacts.get("root")) if isinstance(artifacts, dict) else artifacts
    value = value or oracle.get("recovery_artifact")
    return roots.legacy_path(value) if value else None


def _path_values(value: Any, pointer: str, expected=None):
    if isinstance(value, str):
        # Only an explicit three-element brace list, never shell/glob expansion.
        if "{1,2,3}" in value:
            if value.count("{1,2,3}") != 1:
                yield pointer, value, expected
            else:
                for number in (1, 2, 3):
                    yield pointer + f"[{number}]", value.replace("{1,2,3}", str(number)), expected
        else:
            yield pointer, value, expected
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _path_values(item, f"{pointer}/{index}", expected)


def _references(oracle: dict):
    """Explicit artifact-bearing fields; do not mistake shell text for a path."""
    artifacts = oracle.get("artifacts")
    if isinstance(artifacts, dict):
        for key, value in artifacts.items():
            if key not in {"directory", "root"} and "sha256" not in key:
                role = ("patch" if "patch" in key else "harness" if key in {"harness", "session_oracles"}
                        else "directory" if key == "logs" and isinstance(value, str) and value.endswith("/logs")
                        else "artifact")
                for p, v, h in _path_values(value, "/artifacts/" + key, artifacts.get(key + "_sha256")):
                    yield role, p, v, h
    for field in ("source_only_patch", "source_only_golden", "golden_scope"):
        value = oracle.get(field)
        if isinstance(value, dict):
            for key in ("artifact", "patch"):
                if isinstance(value.get(key), str):
                    yield "patch", f"/{field}/{key}", value[key], value.get("sha256") or value.get("patch_sha256")
    spec = oracle.get("session_spec")
    if isinstance(spec, dict) and isinstance(spec.get("artifact"), str):
        yield "session_spec", "/session_spec/artifact", spec["artifact"], spec.get("sha256")
    harness = oracle.get("harness")
    if isinstance(harness, dict):
        if isinstance(harness.get("path"), str):
            yield "harness", "/harness/path", harness["path"], harness.get("sha256")
        for pointer, value, digest in _path_values(harness.get("files"), "/harness/files"):
            yield "harness", pointer, value, digest
    for field in ("session_behavior_tests", "session_behavior_test", "session_test"):
        for pointer, value, digest in _path_values(oracle.get(field), "/" + field):
            yield "harness", pointer, value, digest
    for key, value in (oracle.get("coverage") or {}).items() if isinstance(oracle.get("coverage"), dict) else ():
        if key in {"artifact", "report", "start_report", "golden_report", "coverage_report", "json", "xml"}:
            for pointer, name, digest in _path_values(value, "/coverage/" + key):
                yield "coverage", pointer, name, digest
    environment = oracle.get("environment")
    if isinstance(environment, dict) and isinstance(environment.get("dockerfile"), str):
        yield "environment", "/environment/dockerfile", environment["dockerfile"], environment.get("dockerfile_sha256")
    logs = oracle.get("logs")
    if isinstance(logs, dict):
        for key, value in logs.items():
            for pointer, name, digest in _path_values(value, "/logs/" + key):
                yield "log", pointer, name, digest
    containers = [(f"/{key}", oracle.get(key)) for key in ("start_runs", "golden_runs", "source_only_golden_runs")]
    if isinstance(oracle.get("runs"), dict):
        containers += [(f"/runs/{key}", value) for key, value in oracle["runs"].items()]
    for prefix, runs in containers:
        if not isinstance(runs, list):
            continue
        for index, run in enumerate(runs):
            if not isinstance(run, dict):
                continue
            for key, value in run.items():
                if key in {"log", "driver_log", "coverage_profile"} or key.endswith(("_trace", "_commands")):
                    for pointer, name, digest in _path_values(value, f"{prefix}/{index}/{key}", run.get(key + "_sha256")):
                        yield "log", pointer, name, digest


def _evidence(capture: _Capture, oracle: dict) -> tuple[list[dict], Path | None]:
    evidence = []
    try:
        directory = _directory(oracle, capture.roots)
        if directory:
            root, relative = capture.roots.locate(directory)
            item = {"role": "directory", "root": root, "path": relative,
                    "absolute_path": str(directory), "sha256": None,
                    "hash_basis": "directory_is_not_a_content_attestation"}
            try:
                capture.roots.directory(directory)
                item["state"] = "directory_present"
            except LegacyInputError as exc:
                item.update(state=exc.code, error=str(exc))
            evidence.append(item)
    except LegacyInputError as exc:
        directory = None
        evidence.append({"role": "directory", "state": exc.code, "error": str(exc)})
    for role, pointer, value, expected in _references(oracle):
        item = {"role": role, "field": pointer, "original_path": value}
        # A repository test is bound to start_sha by the task, not to a mutable
        # historical checkout. A runtime adapter must obtain it from that tree.
        try:
            if (pointer.startswith("/harness/files/") and not value.startswith(
                    ("/", "${", "session files/", *HISTORICAL_PREFIXES))):
                relative = value.removesuffix(" (repository test)")
                _relative(relative)
                item.update(state="repository_reference", repository_path=relative,
                            hash_basis="requires_start_tree_blob_binding", sha256=None)
            else:
                path = capture.roots.legacy_path(value, directory=directory)
                if role == "directory":
                    root, relative = capture.roots.locate(path)
                    item.update(root=root, path=relative, absolute_path=str(path), sha256=None,
                                hash_basis="directory_is_not_a_content_attestation")
                    capture.roots.directory(path)
                    item["state"] = "directory_present"
                else:
                    ref, _ = capture.file(path, expected)
                    item.update(ref)
        except LegacyInputError as exc:
            item.update(state=exc.code, error=str(exc), sha256=None)
        evidence.append(item)
    return evidence, directory


def _success(value: Any) -> bool | None:
    if type(value) is int:
        return value == 0
    if not isinstance(value, dict):
        return None
    # A missing status is unknown, not a default zero exit.
    statuses = []
    for key in ("exit_code", "returncode", "failed"):
        if key in value:
            if type(value[key]) is not int:
                return False
            statuses.append(value[key] == 0)
    if "ok" in value:
        statuses.append(value["ok"] is True)
    return all(statuses) if statuses else None


def _signals(oracle: dict, evidence: list[dict], *, capture: _Capture) -> dict:
    by_field = {e["field"]: e for e in evidence if e.get("field")}
    signals, issues = [], []
    for phase in PHASES:
        variants = []
        if isinstance(oracle.get("runs"), dict) and phase in oracle["runs"]:
            variants.append((f"/runs/{phase}", oracle["runs"][phase]))
        for key in ((phase + "_runs",) if phase == "start" else ("golden_runs", "source_only_golden_runs")):
            if key in oracle:
                variants.append(("/" + key, oracle[key]))
        lists = [(p, v) for p, v in variants if isinstance(v, list)]
        if len(lists) > 1 and len({canonical_sha256(v) for _, v in lists}) != 1:
            issues.append("conflicting_" + phase + "_runs")
        pointer, runs = lists[0] if lists else (None, [])
        if len(runs) != 3:
            issues.append("three_" + phase + "_observations_missing")
        for index in range(3):
            raw = runs[index] if index < len(runs) else None
            field = f"{pointer}/{index}" if pointer else None
            refs = [by_field[p] for p in (f"{field}/log", f"{field}/driver_log") if p in by_field]
            # Separate log lists may be explicit even if start_runs is just 3.
            if not refs:
                for key in (phase, phase + "_runs"):
                    for suffix in (f"/{index}", f"[{index + 1}]"):
                        p = f"/logs/{key}" + suffix
                        if p in by_field:
                            refs.append(by_field[p])
            if isinstance(raw, dict) and "run" in raw and (type(raw["run"]) is not int or raw["run"] != index + 1):
                issues.append(f"{phase}_{index + 1}_run_number_mismatch")
            if isinstance(raw, dict) and raw.get("signature"):
                for reference in refs:
                    if reference.get("state") == "present":
                        _, data = capture.file(Path(reference["absolute_path"]))
                        marker = "P2P_SIGNATURE=" + str(raw["signature"])
                        if marker not in data.decode(errors="replace").splitlines():
                            issues.append(f"{phase}_{index + 1}_signature_not_in_log")
            signals.append({
                "phase": phase, "repetition": index + 1, "field": field,
                "raw": deepcopy(raw), "recorded_success": _success(raw),
                "logs": refs, "signature": deepcopy(oracle.get(phase + "_signature")),
                "aggregate_fields": {p: v for p, v in variants if not isinstance(v, list)},
            })
    bound = all(s["recorded_success"] is True and s["logs"]
                and all(r.get("state") == "present" and r.get("size", 0) > 0 for r in s["logs"])
                for s in signals)
    log_paths = [(r.get("root"), r.get("path")) for s in signals for r in s["logs"]]
    if len(log_paths) != len(set(log_paths)):
        issues.append("replay_log_reused")
    return {"signals": signals, "issues": issues,
            "six_original_signals_bound": bound and not issues,
            "meaning": "historical byte bindings only; execution has not been verified"}


def _lane_requirements(capture: _Capture, task: dict, oracle: dict, directory: Path | None,
                       evidence: list[dict], replay: dict) -> tuple[list[str], dict]:
    """Report the actual committed lane input contracts, not readiness guesses."""
    lane = _lane(task)
    roots = capture.roots
    expected = oracle.get("pass_to_pass_tests")
    issues = []
    lane_root = roots.paths["oracle"] / lane
    catalog = {"task_membership": False, "task_reference": None, "specification": None}
    ref, _ = capture.file(lane_root / "tasks.jsonl")
    catalog["task_reference"] = ref
    try:
        ref, records = capture.jsonl(lane_root / "tasks.jsonl")
        matched = [(n, r) for n, r in records if r["id"] == task["id"]]
        if matched:
            number, member = matched[0]
            if any(member.get(k) != task.get(k) for k in ("id", "repo", "start_sha")):
                raise LegacyInputError("lane_task_identity_mismatch", task["id"])
            catalog.update(task_membership=True, task_reference=_row_ref(ref, number, member))
        else:
            issues.append("task_absent_from_existing_lane_catalog")
    except LegacyInputError as exc:
        issues.append(exc.code + ":lane_task_catalog")
    if not (isinstance(expected, list) and expected and all(isinstance(v, str) and v for v in expected)
            and len(set(expected)) == len(expected)):
        issues.append("fixed_unique_string_test_identities")
    if directory is None or not directory.is_relative_to(lane_root):
        issues.append("lane_local_artifact_layout")
    if lane == "gopy":
        spec = {}
        if directory is not None:
            pin = oracle.get("session_spec")
            spec_path = directory / "session-spec.json"
            pin_matches = False
            if isinstance(pin, dict) and pin.get("artifact") and pin.get("sha256"):
                try:
                    pin_matches = roots.legacy_path(pin["artifact"], directory=directory) == spec_path
                except LegacyInputError:
                    pass
            if not pin_matches:
                issues.append("original_session_spec_reference_and_sha256")
            ref, data = capture.file(spec_path, pin["sha256"] if pin_matches else None)
            ref["binding"] = "source_declared" if pin_matches else "convention_discovery_only"
            catalog["specification"] = ref
            if ref["state"] == "present":
                parsed = _json(data, ref["absolute_path"])
                if isinstance(parsed, dict):
                    spec = parsed
                    catalog["specification_record"] = deepcopy(spec)
                    evidence.append({**ref, "role": "session_spec", "field": "lane/session-spec.json"})
        command = spec.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(s, str) for s in command):
            issues.append("original_session_spec_command")
        elif str(task.get("inferred_language")).lower() == "go":
            if len(command) < 2 or Path(command[0]).name != "go" or command[1] != "test" or not spec.get("inject_files"):
                issues.append("fixed_go_session_command_and_inject_files")
        elif Path(command[0]).name not in {"python", "python3", "python3.11"} or spec.get("inject_files"):
            issues.append("python_oracle_support_and_candidate_import_contract")
        if spec.get("pass_to_pass_tests") is not None and spec["pass_to_pass_tests"] != expected:
            issues.append("session_spec_selectors_differ")
        observations = [s["raw"] for s in replay["signals"]]
        signatures = [r.get("signature") for r in observations if isinstance(r, dict)]
        if (len(signatures) != 6 or any(not isinstance(s, str) or not s for s in signatures)
                or len(set(signatures)) != 1):
            issues.append("six_stable_P2P_SIGNATURE_observations")
    elif lane in {"native", "rust"}:
        ref, _ = capture.file(lane_root / "plans.jsonl")
        catalog["specification"] = ref
        # Old input_index values belong to another task table. Do not join
        # them to a modern plan by integer alone, even if that integer exists.
        issues.append(f"identity_bound_{lane}_plans_jsonl_entry")
        if lane == "rust" and oracle.get("workspace_command"):
            issues.append("workspace_command_to_rust_plan_adapter")
    elif lane == "tsjs":
        ref, data = capture.file(lane_root / "cases.json")
        catalog["specification"] = ref
        if ref["state"] == "present":
            cases = _json(data, ref["absolute_path"])
            case = cases.get("cases", {}).get(task["id"]) if isinstance(cases, dict) else None
            catalog["case_present"] = case is not None
            if case is not None:
                catalog["case_record"] = case
        issues += ["identity_bound_tsjs_cases_json_entry", "original_case_session_files_and_dependencies"]
    elif lane == "scripted":
        catalog["specification"] = capture.file(lane_root / "oracle_config.json")[0]
        issues.append("identity_bound_scripted_oracle_config_entry")
        if str(task.get("inferred_language")).lower() == "shell":
            issues.append("shell_harness_adapter_not_ruby_php")
    elif lane == "jvm":
        environment = oracle.get("environment") or {}
        if not isinstance(environment, dict) or not re.fullmatch(
                r"oracle-java-tools:(8|11|17|21)", str(environment.get("container", ""))):
            issues.append("pinned_jvm_toolchain_environment")
        if oracle.get("build_system") == "gradle" or "./gradlew" in str(oracle.get("command", "")):
            issues.append("legacy_gradle_count_selectors_and_fresh_test_build_adapter")
        elif not (isinstance(environment, dict) and environment.get("build_system") == "maven"
                  and str(oracle.get("command", "")).count("/oracle-tools/maven/bin/mvn") == 1):
            issues.append("jvm_original_command_environment_adapter")
    else:
        catalog["specification"] = capture.file(lane_root / "specs.json")[0]
        issues.append("identity_bound_csharp_specs_json_entry")
    if not any(e["role"] == "patch" and e.get("state") == "present" and e.get("declared_sha256")
               and e.get("field", "").startswith(("/source_only_patch/", "/source_only_golden/", "/golden_scope/"))
               for e in evidence):
        issues.append("explicit_pinned_source_only_golden")
    if oracle.get("source_only_golden") is False or oracle.get("golden_mode") == "target-commit":
        issues.append("target_commit_is_not_a_source_only_golden")
    return issues, catalog


def _capture_task(capture, task, oracle, task_ref, oracle_ref, source_files, secondary):
    for key in ("id", "repo", "start_sha", "target_sha"):
        if key in oracle and key in task and oracle[key] != task[key]:
            raise LegacyInputError("release_task_identity_mismatch", f"{task['id']}:{key}")
    source = _source(capture, task, oracle, source_files)
    evidence, directory = _evidence(capture, oracle)
    replay = _signals(oracle, evidence, capture=capture)
    requirements, catalog = _lane_requirements(capture, task, oracle, directory, evidence, replay)
    evidence_issues = [
        {"field": e.get("field"), "path": e.get("absolute_path", e.get("original_path")), "state": e["state"]}
        for e in evidence if e.get("state") not in {"present", "directory_present"}
    ]
    bound = (source["status"] == "original_record_bound"
             and replay["six_original_signals_bound"] and not evidence_issues)
    # These lanes still need their explicit plan/case/layout adapters. Do not
    # promote merely because a directory, release status, or six exits exist.
    row = {
        "id": task["id"], "repo": task["repo"], "lane": _lane(task),
        "task": task_ref, "released_oracle": oracle_ref,
        "task_sha256": canonical_sha256(task), "oracle_sha256": canonical_sha256(oracle),
        "source_kind": "published_legacy", "source": source, "evidence": evidence,
        "evidence_issues": evidence_issues,
        "existing_lane_catalog": catalog,
        # The normalization is in the sidecar path/replay schema, never in
        # source-owned values. Retain unknown fields too, not only a whitelist.
        "normalized_contract": deepcopy(oracle),
        "replay": replay, "required_lane_adapter": requirements,
        "migration_route": "new_adapter" if requirements else "existing_lane" if bound else "evidence_blocked",
        "status": "pending", "runtime_status": "not_verified",
        "reason": "input normalization only; runtime recipe and engine verification remain separate",
        "required_execution": ["developer_check", *[f"{p}-{n}" for p in PHASES for n in (1, 2, 3)]],
    }
    if task["id"] in secondary.get("tasks", {}):
        item = secondary["tasks"][task["id"]]
        row["secondary_contract"] = {
            "reference": secondary["reference"], "record_sha256": canonical_sha256(item),
            "provenance": deepcopy(item.get("provenance")), "declared_status": item.get("status"),
            "use": "diagnostic_only_never_a_replacement_for_original_assertions",
            "expected_tests": deepcopy(item.get("recipe", {}).get("expected_tests")),
        }
    return row


def capture_inventory(
    *, tasks: Path, oracles: Path, scope_ids: Path, roots: Mapping[str, str | Path],
    source_files: Mapping[str, str | Path] | None = None, secondary_contract: Path | None = None,
) -> dict:
    """Capture an inventory without interpreting a captured hash as old proof."""
    boundary = Roots(roots)
    # Persist canonical root-relative input locations so reopening does not
    # depend on a deployment's optional root alias.
    def canonical(path):
        return boundary.path(*boundary.locate(Path(path)))

    tasks, oracles, scope_ids = map(canonical, (tasks, oracles, scope_ids))
    if secondary_contract is not None:
        secondary_contract = canonical(secondary_contract)
    capture = _Capture(boundary)
    task_file, task_rows = capture.jsonl(tasks)
    oracle_file, oracle_rows = capture.jsonl(oracles)
    scope_file, raw_scope = capture.file(scope_ids)
    if raw_scope is None or scope_file["state"] != "present":
        raise LegacyInputError("scope_missing", str(scope_ids))
    ids = raw_scope.decode().splitlines()
    if not ids or any(not i or i.strip() != i for i in ids) or len(set(ids)) != len(ids):
        raise LegacyInputError("invalid_scope", str(scope_ids))
    task_map = {r["id"]: (n, r) for n, r in task_rows}
    oracle_map = {r["id"]: (n, r) for n, r in oracle_rows}
    if not set(ids) <= task_map.keys() or not set(ids) <= oracle_map.keys():
        raise LegacyInputError("scope_records_missing", str(scope_ids))
    sources = {name: str(canonical(path)) for name, path in (source_files or {}).items()}
    for name, path in sources.items():
        if _relative(name) != Path(name).name or not name.endswith(".jsonl"):
            raise LegacyInputError("invalid_source_artifact", name)
        boundary.locate(Path(path))
    secondary = {}
    if secondary_contract is not None:
        ref, data = capture.file(secondary_contract)
        if data is None or ref["state"] != "present":
            raise LegacyInputError(ref["state"], str(secondary_contract))
        parsed = _json(data, str(secondary_contract))
        if not isinstance(parsed, dict) or not isinstance(parsed.get("tasks"), dict):
            raise LegacyInputError("invalid_secondary_contract", str(secondary_contract))
        secondary = {**parsed, "reference": ref}
    rows = []
    for ident in sorted(ids):
        number, task = task_map[ident]
        oracle_number, oracle = oracle_map[ident]
        if oracle.get("lane_results_source"):
            continue
        rows.append(_capture_task(
            capture, task, oracle, _row_ref(task_file, number, task),
            _row_ref(oracle_file, oracle_number, oracle), sources, secondary,
        ))
    return {
        "schema_version": SCHEMA,
        "roots": {k: str(v) for k, v in sorted(boundary.paths.items())},
        "capture_parameters": {"tasks": str(tasks), "oracles": str(oracles), "scope_ids": str(scope_ids),
                               "source_files": sources,
                               "secondary_contract": str(secondary_contract) if secondary_contract else None},
        "inputs": {"tasks": task_file, "oracles": oracle_file, "scope_ids": scope_file},
        "scope_size": len(ids), "legacy_count": len(rows),
        "hash_policy": "externally pin this inventory SHA256; observed hashes do not certify historical success",
        "summary": {
            "by_lane": dict(sorted(Counter(r["lane"] for r in rows).items())),
            "source_status": dict(sorted(Counter(r["source"]["status"] for r in rows).items())),
            "migration_route": dict(sorted(Counter(r["migration_route"] for r in rows).items())),
            "six_original_signals_bound": sum(r["replay"]["six_original_signals_bound"] for r in rows),
            "runtime_verified": 0,
        },
        "rows": rows,
    }


@dataclass(frozen=True)
class LegacyInputs:
    """An inspected task input; never an execution proof."""

    _row: dict
    roots: Roots

    @property
    def row(self) -> dict:
        return deepcopy(self._row)

    def read_evidence(self, reference: dict) -> bytes:
        allowed = [r for r in self._row["evidence"] if r.get("state") == "present"]
        if reference not in allowed:
            raise LegacyInputError("unbound_evidence", str(reference.get("path")))
        return self.roots.read(reference["root"], reference["path"], reference["sha256"])

    def require_existing_lane(self) -> dict:
        if (self._row["migration_route"] != "existing_lane"
                or self._row["source"]["status"] != "original_record_bound"
                or not self._row["replay"]["six_original_signals_bound"]
                or any(e.get("state") not in {"present", "directory_present"}
                       for e in self._row["evidence"])):
            raise LegacyInputError("legacy_lane_adapter_required", "; ".join(
                self._row["required_lane_adapter"] or ["original source and six replay bindings required"]))
        return deepcopy(self._row["normalized_contract"])


@dataclass(frozen=True)
class LegacyInventory:
    _document: dict
    roots: Roots

    @property
    def document(self) -> dict:
        return deepcopy(self._document)

    def for_task(self, task: dict, oracle: dict) -> LegacyInputs:
        rows = [row for row in self._document["rows"] if row["id"] == task.get("id")]
        if len(rows) != 1:
            raise LegacyInputError("task_not_in_legacy_inventory", str(task.get("id")))
        row = rows[0]
        if (canonical_sha256(task) != row["task_sha256"]
                or canonical_sha256(oracle) != row["oracle_sha256"]):
            raise LegacyInputError("task_or_oracle_changed", row["id"])
        return LegacyInputs(deepcopy(row), self.roots)


def load_inventory(path: Path, *, expected_sha256: str) -> LegacyInventory:
    """Verify the external pin, then recompute every row from reopened evidence.

    Missing files must remain missing too: newly appearing evidence requires a
    new capture/review rather than silently preserving an obsolete blocker.
    Call once per migration batch, then ``for_task(task, oracle)``. Consumers
    reopen assets with ``read_evidence`` immediately before packaging them.
    """
    if not isinstance(expected_sha256, str) or not SHA256.fullmatch(expected_sha256):
        raise LegacyInputError("inventory_sha256_required", str(path))
    # The inventory itself is an explicitly supplied file, not a path from it.
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or path.is_symlink():
        raise LegacyInputError("unsafe_inventory_path", str(path))
    data = _read_descriptor(_open_beneath(Path("/"), path.relative_to("/").as_posix()), str(path))
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise LegacyInputError("inventory_sha256_mismatch", str(path))
    document = _json(data, str(path))
    if not isinstance(document, dict) or document.get("schema_version") != SCHEMA:
        raise LegacyInputError("invalid_inventory_schema", str(path))
    params = document["capture_parameters"]
    observed = capture_inventory(
        tasks=Path(params["tasks"]), oracles=Path(params["oracles"]),
        scope_ids=Path(params["scope_ids"]), roots=document["roots"],
        source_files=params["source_files"],
        secondary_contract=Path(params["secondary_contract"]) if params["secondary_contract"] else None,
    )
    if canonical_sha256(observed) != canonical_sha256(document):
        raise LegacyInputError("inventory_evidence_changed", str(path))
    return LegacyInventory(observed, Roots(observed["roots"]))


def load_legacy_inputs(task: dict, oracle: dict, *, inventory: LegacyInventory) -> LegacyInputs:
    """Entry-point adapter: keep the frozen task/oracle unchanged."""
    return inventory.for_task(task, oracle)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    capture = commands.add_parser("capture", help="read-only discovery; no correctness claims")
    capture.add_argument("--config", type=Path, required=True)
    capture.add_argument("--oracle-root", type=Path, required=True)
    capture.add_argument("--historical-home", type=Path)
    capture.add_argument("--source-file", action="append", default=[], metavar="NAME=ABSOLUTE_PATH")
    capture.add_argument("--secondary-contract", type=Path)
    capture.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify", help="reopen and verify a pinned input inventory")
    verify.add_argument("--inventory", type=Path, required=True)
    verify.add_argument("--sha256", required=True)
    args = parser.parse_args(argv)
    try:
        if args.operation == "capture":
            import yaml
            config = yaml.safe_load(args.config.read_text())
            release = Path(config["root"])
            oracle_root = args.oracle_root
            roots = {"release": release, "oracle": oracle_root, "session": oracle_root.parent}
            if args.historical_home:
                roots["home"] = args.historical_home
            sources = {}
            for item in args.source_file:
                name, separator, value = item.partition("=")
                if not separator or name in sources:
                    raise LegacyInputError("invalid_source_file_binding", item)
                sources[name] = value
            document = capture_inventory(
                tasks=release / config["tasks"], oracles=release / config["oracles"],
                scope_ids=release / config["scope_ids"], roots=roots, source_files=sources,
                secondary_contract=args.secondary_contract,
            )
            # CLI writes only the caller's new deliverable, never overwrites an
            # input, inventory, or registry. Revisions require a new output path.
            with args.output.open("x", encoding="utf-8") as stream:
                json.dump(document, stream, sort_keys=True, ensure_ascii=False, indent=2)
                stream.write("\n")
            print(json.dumps({"operation": "capture", "scope_size": document["scope_size"],
                              "legacy_count": document["legacy_count"], **document["summary"]}, sort_keys=True))
        else:
            inventory = load_inventory(args.inventory, expected_sha256=args.sha256)
            print(json.dumps({"operation": "verify", "inventory_unchanged": True,
                              "scope_size": inventory.document["scope_size"],
                              "legacy_count": inventory.document["legacy_count"],
                              **inventory.document["summary"]}, sort_keys=True))
        return 0
    except (LegacyInputError, OSError, KeyError, TypeError, ValueError) as exc:
        print(json.dumps({"operation": args.operation, "error": str(exc), "runtime_verified": 0}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
