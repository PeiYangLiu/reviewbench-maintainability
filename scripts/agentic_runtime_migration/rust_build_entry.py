#!/usr/bin/env python3
"""Apply fixed build-only Cargo overlays to a complete current-candidate mirror.

No production file or test assertion is replaced by this tool. The original
workspace stays byte-for-byte unchanged. The mirror is synchronized by content,
including additions and deletions, before every real Cargo invocation.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys

try:
    import tomllib
except ImportError:  # The pinned runtime images also include Python 3.10.
    import tomli as tomllib
import tomli_w


class OverlayError(ValueError):
    pass


def same_value(left, right):
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(same_value(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(same_value(a, b) for a, b in zip(left, right))
    return left == right


def relative_path(value):
    if not isinstance(value, str):
        raise OverlayError("invalid Cargo build-overlay path")
    path = PurePosixPath(value)
    if (not value or path.is_absolute()
            or any(part in {"..", ".git", ".reviewbench"} for part in path.parts)
            or "\0" in value or "\\" in value):
        raise OverlayError("invalid Cargo build-overlay path")
    return path.as_posix()


def manifest_operations(before, after, path=()):
    """Derive semantic edits; preserve unrelated candidate fields and members."""
    result = []
    for name in sorted(set(before) | set(after)):
        key = [*path, name]
        if name not in before:
            if isinstance(after[name], dict):
                result.append({"kind": "ensure_table", "key": key})
                result.extend(manifest_operations({}, after[name], key))
            else:
                result.append({"kind": "add", "key": key, "value": after[name]})
        elif name not in after:
            if isinstance(before[name], dict) and key != ["package"]:
                result.extend(manifest_operations(before[name], {}, key))
                result.append({"kind": "prune_empty", "key": key})
            else:
                result.append({"kind": "remove", "key": key, "old": before[name]})
        elif isinstance(before[name], dict) and isinstance(after[name], dict):
            result.extend(manifest_operations(before[name], after[name], key))
        elif not same_value(before[name], after[name]):
            old, new = before[name], after[name]
            if (isinstance(old, list) and isinstance(new, list)
                    and all(isinstance(value, (str, int, bool)) for value in [*old, *new])):
                result.append({"kind": "list_edit", "key": key,
                               "remove": [value for value in old if value not in new],
                               "add": [value for value in new if value not in old], "order": new})
            else:
                result.append({"kind": "replace", "key": key, "old": old, "value": new})
    return result


def apply_manifest_operations(candidate, operations):
    candidate = copy.deepcopy(candidate)
    missing = object()
    for operation in operations:
        key = operation["key"]
        if not key or any(not isinstance(value, str) for value in key):
            raise OverlayError("invalid Cargo manifest key")
        parent = candidate
        for part in key[:-1]:
            value = parent.setdefault(part, {})
            if not isinstance(value, dict):
                raise OverlayError("candidate manifest conflicts with build table: " + ".".join(key))
            parent = value
        name, kind = key[-1], operation["kind"]
        current = parent.get(name, missing)
        if kind == "ensure_table":
            if current is missing:
                parent[name] = {}
            elif not isinstance(current, dict):
                raise OverlayError("candidate conflicts with a required Cargo table: " + ".".join(key))
        elif kind == "prune_empty":
            if current == {}:
                parent.pop(name)
        elif kind == "remove":
            if current is missing:
                continue
            if not same_value(current, operation["old"]):
                raise OverlayError("candidate changed a removed build input: " + ".".join(key))
            del parent[name]
        elif kind == "add":
            if current is not missing and not same_value(current, operation["value"]):
                raise OverlayError("candidate conflicts with a required build dependency: " + ".".join(key))
            parent[name] = copy.deepcopy(operation["value"])
        elif kind == "replace":
            if current is missing or not any(same_value(current, value) for value in (operation["old"], operation["value"])):
                raise OverlayError("candidate conflicts with a fixed build replacement: " + ".".join(key))
            parent[name] = copy.deepcopy(operation["value"])
        elif kind == "list_edit":
            if not isinstance(current, list):
                raise OverlayError("candidate build list is missing: " + ".".join(key))
            parent[name] = [value for value in current if value not in operation["remove"]]
            parent[name].extend(value for value in operation["add"] if value not in parent[name])
            kept = parent[name]
            parent[name] = [value for value in operation["order"] if value in kept]
            parent[name].extend(value for value in kept if value not in parent[name])
        else:
            raise OverlayError("unrecognized Cargo manifest operation")
    return candidate


def lock_matches_candidate(source, record):
    """Select only from public Cargo data, never a start/golden replay label."""
    conditions = record.get("when")
    if conditions is None:
        return True
    if not isinstance(conditions, list) or not conditions:
        raise OverlayError("conditional Cargo lock needs nonempty manifest conditions")
    matches = True
    for condition in conditions:
        if not isinstance(condition, dict) or set(condition) != {"path", "key", "value"}:
            raise OverlayError("invalid conditional Cargo lock predicate")
        name = relative_path(condition["path"])
        if PurePosixPath(name).name != "Cargo.toml":
            raise OverlayError("Cargo lock selection may inspect only Cargo manifests")
        key = condition["key"]
        if (not isinstance(key, list) or not key
                or any(not isinstance(part, str) or not part for part in key)):
            raise OverlayError("invalid conditional Cargo manifest key")
        original = source / name
        if original.is_symlink() or not original.resolve().is_relative_to(source):
            raise OverlayError("conditional Cargo manifest escapes the candidate")
        if not original.exists():
            matches = False
            continue
        value = tomllib.loads(original.read_text())
        for part in key:
            if not isinstance(value, dict) or part not in value:
                matches = False
                break
            value = value[part]
        else:
            matches = matches and same_value(value, condition["value"])
    return matches


def overlay_files(source, spec, lock_sources=None):
    """Return only build-configuration bytes; do not write into the candidate."""
    source = source.resolve()
    output = {}
    for record in spec.get("manifests", []):
        name = relative_path(record["path"])
        cargo_manifest = PurePosixPath(name).name == "Cargo.toml"
        config_removal = name == ".cargo/config.toml"
        if not cargo_manifest and not config_removal:
            raise OverlayError("only Cargo manifests can receive build overlays")
        if config_removal and any(
                operation.get("kind") not in {"remove", "prune_empty"}
                or not operation.get("key") or operation["key"][0] not in {"include", "unstable"}
                for operation in record["operations"]):
            raise OverlayError("Cargo config overlay may only remove the attested nightly include/unstable inputs")
        original = source / name
        if original.is_symlink() or not original.resolve().is_relative_to(source):
            raise OverlayError("manifest is not a regular candidate file")
        value = tomllib.loads(original.read_text())
        value = apply_manifest_operations(value, record["operations"])
        # A private session crate is present only in a grader workspace.
        workspace = value.get("workspace")
        if cargo_manifest and isinstance(workspace, dict) and "members" in workspace:
            parent = PurePosixPath(name).parent
            private = set(spec.get("private_members", []))
            workspace["members"] = [
                member for member in workspace["members"]
                if (parent / member).as_posix() not in private
                or (source / parent / member / "Cargo.toml").is_file()
            ]
        serialized = tomli_w.dumps(value).encode()
        if tomllib.loads(serialized.decode()) != value:
            raise OverlayError("Cargo manifest round-trip changed its semantic data")
        output[name] = serialized
    for record in spec.get("locks", []):
        name = relative_path(record["path"])
        if PurePosixPath(name).name != "Cargo.lock":
            raise OverlayError("only dependency lockfiles can be build defaults")
        if not lock_matches_candidate(source, record):
            continue
        current = source / name
        if current.is_symlink() or not current.resolve().is_relative_to(source):
            raise OverlayError("dependency lock escapes the candidate")
        allow_missing = record.get("allow_missing", True)
        if type(allow_missing) is not bool:
            raise OverlayError("invalid missing-lock default policy")
        if not current.exists() and not allow_missing:
            continue
        if current.exists():
            digest = hashlib.sha256(current.read_bytes()).hexdigest()
            if digest not in record.get("replace_if_sha256", []):
                # A genuinely edited/new candidate lock is a candidate input,
                # not something to overwrite with an environment snapshot.
                continue
        if name in output:
            raise OverlayError("ambiguous candidate dependency lock defaults")
        asset = Path((lock_sources or {}).get(record["asset_target"], record["asset_target"]))
        contents = asset.read_bytes()
        if hashlib.sha256(contents).hexdigest() != record["sha256"]:
            raise OverlayError("fixed Cargo dependency lock changed")
        output[name] = contents
    return output


def apply_in_place(source, spec, lock_sources=None):
    """Used only on private source extractions during dependency preparation."""
    for name, content in overlay_files(source, spec, lock_sources).items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def candidate_files(source):
    result = {}
    for directory, dirs, files in os.walk(source, followlinks=False):
        dirs[:] = sorted(name for name in dirs if name not in {".git", ".reviewbench"})
        for name in sorted([f for f in files if f not in {".git", ".reviewbench"}] +
                           [d for d in dirs if (Path(directory) / d).is_symlink()]):
            path = Path(directory) / name
            relative = relative_path(path.relative_to(source).as_posix())
            if path.is_symlink():
                target = os.readlink(path)
                resolved = os.path.normpath(os.path.join(os.path.dirname(relative), target))
                if os.path.isabs(target) or resolved == ".." or resolved.startswith("../"):
                    raise OverlayError("candidate link escapes the build mirror")
            elif not path.is_file():
                raise OverlayError("candidate contains a non-regular build input")
            result[relative] = path
    return result


def synchronize_candidate(source, mirror, spec, lock_sources=None):
    source = source.resolve()
    if mirror.is_symlink():
        raise OverlayError("build mirror must not be a symbolic link")
    mirror.mkdir(parents=True, exist_ok=True)
    mirror = mirror.resolve()
    if source == mirror or source.is_relative_to(mirror) or mirror.is_relative_to(source):
        raise OverlayError("build mirror and candidate must be separate trees")
    overrides = overlay_files(source, spec, lock_sources)
    inputs = candidate_files(source)
    desired = set(inputs) | set(overrides)
    existing = candidate_files(mirror)
    for name in sorted(set(existing) - desired, reverse=True):
        (mirror / name).unlink()
    updated = 0
    for name in sorted(desired):
        original, destination = inputs.get(name), mirror / name
        # Never traverse an old/candidate-created link while synchronizing.
        for parent in reversed(destination.parents):
            if parent == mirror or not parent.is_relative_to(mirror):
                continue
            if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                parent.unlink()
            parent.mkdir(exist_ok=True)
        if name not in overrides and original.is_symlink():
            link = os.readlink(original)
            if destination.is_symlink() and os.readlink(destination) == link:
                continue
            if destination.is_dir() and not destination.is_symlink():
                shutil.rmtree(destination)
            else:
                destination.unlink(missing_ok=True)
            destination.symlink_to(link)
            updated += 1
            continue
        contents = overrides[name] if name in overrides else original.read_bytes()
        executable = bool(original and original.stat().st_mode & 0o111)
        if (destination.is_file() and not destination.is_symlink()
                and destination.read_bytes() == contents
                and bool(destination.stat().st_mode & 0o111) == executable):
            continue
        if destination.is_symlink():
            destination.unlink()
        elif destination.is_dir():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(contents)
        destination.chmod(0o755 if executable else 0o644)
        updated += 1
    # Empty generated directories have no authority over later candidates.
    for directory, dirs, files in os.walk(mirror, topdown=False, followlinks=False):
        path = Path(directory)
        if path != mirror and not path.is_symlink() and not any(path.iterdir()):
            path.rmdir()
    return {"files": len(desired), "updated": updated, "deleted": len(set(existing) - desired)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("/workspace"))
    parser.add_argument("--build-root", type=Path, default=Path("/opt/reviewbench-cache/rust-build"))
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--cwd", default=".")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a Cargo command is required")
    args.build_root.mkdir(parents=True, exist_ok=True)
    with (args.build_root / "build.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            spec = json.loads(args.spec.read_text())
            mirror = args.build_root / "source"
            report = synchronize_candidate(args.source, mirror, spec)
            cwd = mirror / (relative_path(args.cwd) if args.cwd != "." else ".")
            if hasattr(os, "sched_getaffinity"):
                available = sorted(os.sched_getaffinity(0))
                count = spec.get("cpus", 4)
                if type(count) is not int or count < 1:
                    raise OverlayError("invalid Cargo worker CPU bound")
                offset = int.from_bytes(hashlib.sha256(spec["task_id"].encode()).digest()[:8], "big") % len(available)
                os.sched_setaffinity(0, {available[(offset + n) % len(available)]
                                        for n in range(min(count, len(available)))})
            print("ReviewBench Cargo mirror: " + json.dumps(report, sort_keys=True), flush=True)
            result = subprocess.run(command, cwd=cwd, env={**os.environ, "SOURCE_ROOT": str(mirror)})
            return result.returncode
        except (OverlayError, OSError, tomllib.TOMLDecodeError) as exc:
            print("Cargo build overlay: " + str(exc), file=sys.stderr)
            return 2


if __name__ == "__main__":
    raise SystemExit(main())
