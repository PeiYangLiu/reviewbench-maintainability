"""Bind public FetchContent source archives without changing candidate declarations."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile

TOOLS = "/opt/reviewbench-assets/native-fetchcontent-tools"
SPEC = "/opt/reviewbench-assets/native-fetchcontent.json"
CACHE = "/opt/reviewbench-cache/native-fetchcontent"
MAX_INPUT_BYTES = 1024 * 1024
MAX_SOURCE_BYTES = 512 * 1024 * 1024
MAX_SOURCE_FILES = 100000


def seed_source(source, destination, asset_sha256, cache_root):
    """Create one private build copy, leaving its immutable source asset intact."""
    source, destination, cache_root = Path(source), Path(destination), Path(cache_root)
    if (not re.fullmatch(r"[0-9a-f]{64}", asset_sha256) or cache_root.is_symlink()
            or destination.is_symlink() or not destination.resolve().is_relative_to(cache_root.resolve())
            or any(parent.is_symlink() for parent in destination.parents if parent.is_relative_to(cache_root))):
        raise ValueError("native dependency build copy escapes its private cache")
    destination.parent.mkdir(parents=True, exist_ok=True)
    marker = destination.with_name(destination.name + ".source.json")
    lock_path = destination.with_name(destination.name + ".lock")
    if marker.is_symlink() or lock_path.is_symlink():
        raise ValueError("native dependency copy metadata is linked")
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if destination.exists():
            if (not destination.is_dir() or not marker.is_file()
                    or json.loads(marker.read_text()).get("asset_sha256") != asset_sha256):
                raise ValueError("native dependency build copy has an incomplete or different source binding")
            return destination
        count, size = 0, 0
        for path in source.rglob("*"):
            if path.is_symlink():
                if not path.resolve().is_relative_to(source.resolve()):
                    raise ValueError("native dependency source link escapes its archive")
            elif path.is_file():
                size += path.stat().st_size
            count += 1
            if count > MAX_SOURCE_FILES or size > MAX_SOURCE_BYTES:
                raise ValueError("native dependency source copy exceeds its bounded footprint")
        with tempfile.TemporaryDirectory(prefix=".native-source-copy-", dir=destination.parent) as temporary:
            staged = Path(temporary) / "source"
            shutil.copytree(source, staged, symlinks=True)
            os.replace(staged, destination)
        with tempfile.NamedTemporaryFile(mode="w", dir=marker.parent, prefix=".native-source-marker-",
                                         delete=False) as stream:
            stream.write(json.dumps({"asset_sha256": asset_sha256, "source_bytes": size,
                                     "source_entries": count}, sort_keys=True) + "\n")
            temporary_marker = Path(stream.name)
        os.replace(temporary_marker, marker)
    return destination


def declaration(text, name):
    """Recognize the complete literal git declaration used by the native build."""
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise ValueError("invalid FetchContent dependency name")
    text = re.sub(r"#[^\n]*", "", text)
    calls = re.findall(r"\bFetchContent_Declare\s*\(([^()]*)\)", text, re.S)
    matches = []
    for body in calls:
        words = shlex.split(body)
        if not words or words[0] != name:
            continue
        if (len(words) != 8 or words[1] != "GIT_REPOSITORY" or words[3] != "GIT_TAG"
                or words[5:] != ["GIT_SHALLOW", "TRUE", "SYSTEM"]
                or not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git", words[2])
                or not re.fullmatch(r"[0-9a-f]{40}", words[4])):
            raise ValueError("FetchContent declaration is not a literal pinned source")
        matches.append({"repository": words[2], "commit": words[4]})
    if len(matches) != 1:
        raise ValueError("FetchContent declaration is absent or ambiguous: " + name)
    return matches[0]


def validated_command(spec, workspace, command, *, cache_root=None):
    """Check current source pins before invoking CMake against prepared sources."""
    if spec.get("schema_version") != "reviewbench-native-fetchcontent/v1":
        raise ValueError("unknown FetchContent input schema")
    relative = Path(spec["declaration"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("FetchContent declaration escapes the candidate")
    workspace = workspace.resolve()
    path = workspace / relative
    if (not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(workspace)
            or path.stat().st_size > MAX_INPUT_BYTES):
        raise ValueError("FetchContent declaration is not a bounded current source file")
    text = path.read_text()
    if (not command or command[0] != "cmake" or command.count("-S") != 1
            or command[command.index("-S") + 1:command.index("-S") + 2] != ["."]
            or command.count("-B") != 1
            or command[command.index("-B") + 1:command.index("-B") + 2]
            not in (["build-oracle"], ["build-native-public"])
            or any("FETCHCONTENT_" in arg.upper() for arg in command)):
        raise ValueError("FetchContent wrapper requires an unambiguous original CMake configure command")
    dependencies = spec["dependencies"]
    if not isinstance(dependencies, dict) or not dependencies or len(dependencies) > 8:
        raise ValueError("FetchContent dependencies exceed the bounded declaration set")
    result = list(command)
    mode = "developer" if command[command.index("-B") + 1] == "build-native-public" else "grader"
    for name, dependency in sorted(dependencies.items()):
        if declaration(text, name) != {key: dependency[key] for key in ("repository", "commit")}:
            raise ValueError("current candidate changed the prepared FetchContent pin: " + name)
        source = Path(dependency["target"])
        if (not source.is_absolute() or source.is_symlink() or not source.is_dir()
                or not (source / "CMakeLists.txt").is_file()):
            raise ValueError("FetchContent source asset is absent: " + name)
        if spec.get("private_build_copies"):
            cache = Path(cache_root or CACHE)
            digest = dependency["asset_sha256"]
            source = seed_source(source, cache / mode / (name + "-" + digest), digest, cache)
        result.append("-DFETCHCONTENT_SOURCE_DIR_" + name.upper() + "=" + str(source))
    return result


def attach(ctx, task, wrapper):
    """Add explicitly configured public dependency assets to a parsed wrapper."""
    configured = ctx.config.get("migration", {}).get("native_fetchcontent_assets", {}).get(task["repo"], {})
    if not configured:
        return wrapper
    from reviewbench_maintainability.agentic.assets import asset_descriptor
    from reviewbench_maintainability.agentic.contracts import ContractError, atomic_json, component, file_hash
    from reviewbench_maintainability.agentic.snapshots import git

    source_file = "cmake/dependencies.cmake"
    source = git(ctx.repo(task), "show", task["start_sha"] + ":" + source_file).decode()
    assets, dependencies, copy_bytes = [], {}, 0
    for name, binding in sorted(configured.items()):
        expected = declaration(source, name)
        if expected != {key: binding.get(key) for key in ("repository", "commit")}:
            raise ContractError("prepared FetchContent source differs from the original dependency pin")
        reference = binding["asset"]
        descriptor = asset_descriptor(reference, ctx.output)
        if (descriptor.get("source_kind") != "dependency"
                or descriptor.get("git_commit") != expected["commit"]):
            raise ContractError("FetchContent archive lacks its exact public Git source binding")
        size = descriptor["archive"]["input_bytes"]
        if type(size) is not int or not 0 <= size <= MAX_SOURCE_BYTES:
            raise ContractError("FetchContent source exceeds its private-copy bound")
        copy_bytes += size
        target = "/opt/reviewbench-assets/native-fetchcontent/" + name
        assets.append({**reference, "kind": "archive", "visibility": "developer", "target": target})
        dependencies[name] = {**expected, "target": target, "asset_sha256": reference["sha256"]}
    spec = {"schema_version": "reviewbench-native-fetchcontent/v1",
            "declaration": source_file, "dependencies": dependencies, "private_build_copies": True}
    path = ctx.output / "native-fetchcontent" / (component(task["id"]) + ".json")
    atomic_json(path, spec)
    tools = ctx.bundle(Path(__file__).parent, [Path(__file__).name], role="toolchain")
    assets += [
        {**tools, "kind": "archive", "visibility": "developer", "target": TOOLS},
        {"path": str(path), "sha256": file_hash(path), "visibility": "developer", "target": SPEC},
    ]
    original = shlex.split(wrapper["configure"])
    if original.count("-B") != 1 or original[original.index("-B") + 1] != "build-native-public":
        raise ContractError("FetchContent input requires its accounted public configure")
    grader = list(original)
    grader[grader.index("-B") + 1] = "build-oracle"
    def wrap(command):
        return shlex.join(["python3", TOOLS + "/native_fetchcontent.py", "--spec", SPEC,
                           "--workspace", "/workspace", "--", *command])
    return {**wrapper, "assets": [*wrapper.get("assets", []), *assets],
            "cache_mounts": {**wrapper.get("cache_mounts", {}), "native-fetchcontent": CACHE},
            "build_scratch_mb_extra": (2 * copy_bytes + 1024**2 - 1) // 1024**2,
            "configure": wrap(original),
            "grading_bootstrap": [wrap(grader), *wrapper["grading_bootstrap"][1:]],
            "fetchcontent_bindings": spec}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        checked = validated_command(json.loads(args.spec.read_text()), args.workspace, command)
        return subprocess.run(checked, cwd=args.workspace).returncode
    except (ValueError, OSError, KeyError) as exc:
        print("Native FetchContent input rejected: " + str(exc), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
