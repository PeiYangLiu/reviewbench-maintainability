"""Public offline Conan setup for current Rippled source, without test oracles."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess


def run(arguments, *, env):
    print("+ " + shlex.join(arguments), flush=True)
    # No compiler/API probing and no assertion-dependent fallback. Every
    # nonzero command (including shell-encoded signals) aborts preparation.
    subprocess.run(arguments, env=env, check=True)


def historical_recipe(source):
    """The exact five transforms in the original public dependency helper."""
    result = re.sub(r"(?m)^(\s*)'([a-z0-9+_.-]+):([a-z0-9_]+)'", r"\1'\2/*:\3'", source)
    for old, new in (("protobuf/3.21.9", "protobuf/3.21.12"), ("lz4/1.9.3", "lz4/1.9.4"),
                     ("boost/1.82.0", "boost/1.83.0"), ("sqlite3/3.42.0", "sqlite3/3.44.2")):
        result = result.replace("'" + old + "'", "'" + new + "'")
    return result


def private_cache(template, destination, major):
    """Writable task-local metadata, readonly exact external package payloads."""
    if destination.exists():
        raise ValueError("Rippled dependency work directory already exists")
    destination.mkdir(parents=True)
    if major == 2:
        (destination / "p").mkdir()
        shutil.copyfile(template / "p/cache.sqlite3", destination / "p/cache.sqlite3")
        for directory, dirs, files in os.walk(template / "p"):
            for name in list(dirs):
                path = Path(directory) / name
                if name in {"e", "es", "p"}:
                    target = destination / path.relative_to(template)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.symlink_to(path, target_is_directory=True)
                    dirs.remove(name)
    elif major == 1:
        for metadata in sorted((template / "data").glob("*/*/_/_/metadata.json")):
            root = metadata.parent
            target = destination / root.relative_to(template)
            target.mkdir(parents=True)
            shutil.copyfile(metadata, target / metadata.name)
            for name in ("export", "export_source"):
                if (root / name).is_dir():
                    (target / name).symlink_to(root / name, target_is_directory=True)
            for package in sorted((root / "package").iterdir()):
                (target / "package").mkdir(exist_ok=True)
                (target / "package" / package.name).symlink_to(package, target_is_directory=True)
    else:
        raise ValueError("unsupported pinned Conan major version")
    # A copied version marker without generated plugins skips initialization.
    # The pinned executable must initialize its own ordinary cache metadata.
    for name in ("settings.yml",):
        if (template / name).is_file():
            shutil.copyfile(template / name, destination / name)
    (destination / "profiles").mkdir()


def dependency_lock(graph):
    contexts = {row["ref"]: set() for row in graph["recipes"]}
    for row in graph["packages"]:
        contexts[row["ref"]].update(row["contexts"])
    # Include recipes for originally skipped build binaries too. No new revision
    # can enter merely because Conan's cached/built package decision changed.
    requires, build = [], []
    for row in graph["recipes"]:
        pinned = row["ref"] + "#" + row["rrev"]
        if contexts[row["ref"]] & {"host", "test"}:
            requires.append(pinned)
        if "build" in contexts[row["ref"]] or not contexts[row["ref"]]:
            build.append(pinned)
    return {"version": "0.5", "requires": sorted(requires, reverse=True),
            "build_requires": sorted(build, reverse=True), "python_requires": [], "config_requires": []}


def cache_home(cwd, home):
    """CMake exports must not treat external dependency includes as source."""
    home = Path(home)
    if not home.is_absolute():
        raise ValueError("Rippled Conan HOME must be absolute")
    base = home / "rippled-conan"
    if base.resolve().is_relative_to(Path(cwd).resolve()):
        raise ValueError("Rippled external dependency cache must stay outside current candidate source")
    return base


def select_lock(cwd, state, mode, graph):
    """Follow a current public lock once, never retry with a looser lock."""
    candidate = cwd / "conan.lock"
    if mode == "current" and (candidate.exists() or candidate.is_symlink()):
        if candidate.is_symlink() or not candidate.is_file() or candidate.stat().st_size > 1024**2:
            raise ValueError("Rippled current public lock must be a bounded regular file")
        original = candidate.read_bytes()
        lock = json.loads(original)
        if (not isinstance(lock, dict) or lock.get("version") != "0.5"
                or any(not isinstance(lock.get(name, []), list) or len(lock.get(name, [])) > 256
                       or any(not isinstance(row, str) for row in lock.get(name, []))
                       for name in ("requires", "build_requires", "python_requires", "config_requires"))):
            raise ValueError("Rippled current public Conan lock has an unsupported schema")
        return candidate, original
    derived = state / "conan.lock"
    with derived.open("x") as stream:
        stream.write(json.dumps(dependency_lock(graph), indent=2) + "\n")
    return derived, None


def compare_graph(graph, actual):
    nodes = actual.get("graph", actual).get("nodes", {})
    nodes = list(nodes.values()) if isinstance(nodes, dict) else nodes
    recipes = {row["ref"]: row["rrev"] for row in graph["recipes"]}
    packages = {(row["ref"], row["rrev"], row["package_id"], row["prev"]) for row in graph["packages"]}
    required = {(row["ref"], row["rrev"], row["package_id"], row["prev"])
                for row in graph["packages"] if set(row["contexts"]) & {"host", "test"}}
    seen = set()
    for node in nodes:
        ref = node.get("ref")
        if not ref or node.get("recipe") == "Consumer":
            continue
        ref, _, revision = ref.partition("#")
        revision = revision.split("%", 1)[0] or node.get("rrev")
        if ref not in recipes or revision != recipes[ref]:
            raise ValueError("offline Conan graph changed an original recipe: " + ref)
        status = node.get("binary")
        if status == "Skip":
            continue
        key = (ref, revision, node.get("package_id"), node.get("prev"))
        if key not in packages or status not in {"Cache", "Download"}:
            raise ValueError("offline Conan graph changed an original binary: " + repr(key))
        if node.get("context") == "host":
            seen.add(key)
    if required - seen:
        raise ValueError("offline Conan graph omitted an original host/test package: " + repr(sorted(required - seen)))


def prepare(public, dependencies, *, cwd=None):
    cwd = Path.cwd() if cwd is None else Path(cwd)
    spec = json.loads((public / "public.json").read_text())
    graph = json.loads((dependencies / "graph.json").read_text())
    major = graph["major"]
    if major not in (1, 2) or spec["conan_version"] not in {"1.60.2", "2.17.1"}:
        raise ValueError("unsupported public dependency profile")
    actual_version = subprocess.check_output(["conan", "--version"], text=True).strip()
    if actual_version != "Conan version " + spec["conan_version"]:
        raise ValueError("Conan executable differs from the pinned toolchain")
    # Only small metadata is writable here. Package bytes remain readonly asset
    # links, and each container has its own HOME; no cross-replay cache state.
    base = cache_home(cwd, os.environ.get("HOME", "/tmp"))
    state = cwd / "build-rippled-dependencies"
    state.mkdir()
    if major == 1:
        base.mkdir()
        cache = base / ".conan"
    else:
        cache = base
    private_cache(dependencies / "cache", cache, major)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", CONAN_NON_INTERACTIVE="1",
               CONAN_REVISIONS_ENABLED="1")
    env["CONAN_HOME" if major == 2 else "CONAN_USER_HOME"] = str(base)
    run(["conan", "profile", "list"], env=env)
    profile = cache / "profiles/default"
    profile.write_text(spec["build_profile"])
    host = base / "host-profile"
    host.write_text(spec["host_profile"])
    build = cwd / spec["build_directory"]
    # Conan 1's current recipe already sets folders.generators=build/generators.
    # Another build/ output prefix would incorrectly produce build/build/.
    output = build / "conan" if major == 2 else cwd
    output.mkdir(parents=True, exist_ok=True)
    original = (cwd / "conanfile.py").read_bytes()
    recipe = "."
    if spec["recipe_mode"] == "conan2-historical":
        adapted = build / "oracle-conan-recipe"
        adapted.mkdir()
        (adapted / "src").symlink_to(cwd / "src", target_is_directory=True)
        (adapted / "conanfile.py").write_text(historical_recipe(original.decode()))
        recipe = str(adapted / "conanfile.py")
    if major == 2:
        lock, original_lock = select_lock(cwd, state, spec["recipe_mode"], graph)
        report = state / "install-graph.json"
        run(["conan", "install", recipe, "--output-folder=" + str(output), "--build=never",
             "--no-remote", "--profile:host=" + str(host), "--profile:build=" + str(profile),
             "--lockfile=" + str(lock), "--format=json", "--out-file=" + str(report)], env=env)
        compare_graph(graph, json.loads(report.read_text()))
        if original_lock is not None and lock.read_bytes() != original_lock:
            raise ValueError("offline dependency resolution changed current public conan.lock")
    else:
        # There are no credentials or copied remote settings. A disabled remote
        # plus --build=never fails closed if any exact historical binary is absent.
        run(["conan", "remote", "disable", "*"], env=env)
        run(["conan", "install", recipe, "--output-folder=" + str(output),
             "--profile=" + str(host), "--build=never"], env=env)
        # The isolated cache contains only original RREV/PREV/package IDs;
        # Conan 1 cannot substitute a newer version or another cached binary.
    if (cwd / "conanfile.py").read_bytes() != original:
        raise ValueError("public dependency preparation modified current conanfile.py")
    toolchain = (build / "conan/build/generators/conan_toolchain.cmake" if major == 2
                 else build / "generators/conan_toolchain.cmake")
    if toolchain.is_symlink() or not toolchain.is_file():
        raise ValueError("Rippled generated toolchain is not at the original CMake path: " + str(toolchain))
    (state / "dependency-check.json").write_text(json.dumps({
        "status": "resolved", "conan_version": spec["conan_version"],
        "graph_sha256": hashlib.sha256((dependencies / "graph.json").read_bytes()).hexdigest(),
        "current_recipe_unchanged": True, "dependency_builds": 0,
        "cache_outside_candidate": str(base), "toolchain_path": str(toolchain),
        "toolchain_sha256": hashlib.sha256(toolchain.read_bytes()).hexdigest(),
        "lock_origin": ("current_candidate" if original_lock is not None else "derived")
                       if major == 2 else "original_conan1_profile",
        "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest() if major == 2 else None,
    }, sort_keys=True) + "\n")
    print("RIPPLED_PINNED_EXTERNAL_DEPENDENCIES_RESOLVED", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public", type=Path, required=True)
    parser.add_argument("--dependencies", type=Path, required=True)
    args = parser.parse_args(argv)
    prepare(args.public, args.dependencies)


if __name__ == "__main__":
    main()
