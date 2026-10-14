"""Seed an isolated Conan home and run the original candidate build configuration."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else _digest(stream)


def _digest(stream):
    result = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        result.update(block)
    return result.hexdigest()


def regular(path):
    return path.is_file() and not path.is_symlink()


def relative(name):
    path = PurePosixPath(name)
    require(name and not path.is_absolute() and ".." not in path.parts and "." not in path.parts,
            "Conan input path must be relative")
    require(path.as_posix() == name, "Conan input path must be canonical")
    return name


def tree(root):
    result = {}
    for folder, dirs, files in os.walk(root, followlinks=False):
        for name in [*files, *(d for d in dirs if (Path(folder) / d).is_symlink())]:
            path = Path(folder) / name
            result[path.relative_to(root).as_posix()] = path
    return result


PLUGIN_NAMES = {
    "extensions/plugins/profile.py",
    "extensions/plugins/compatibility/compatibility.py",
    "extensions/plugins/compatibility/cppstd_compat.py",
}


def seed(spec, workspace):
    source = Path(spec["source"])
    manifest_path = Path(spec["manifest"])
    require(source.is_absolute() and source.is_dir() and not source.is_symlink(), "Conan seed missing or redirected")
    require(regular(manifest_path) and digest(manifest_path) == spec["manifest_sha256"], "Conan seed manifest changed")
    manifest = json.loads(manifest_path.read_text())
    plugin_source = Path(spec["plugin_source"])
    plugin_files = spec["plugin_files"]
    require(plugin_source.is_absolute() and plugin_source.is_dir() and not plugin_source.is_symlink(),
            "original Conan plugin layer missing or redirected")
    require(set(plugin_files) == PLUGIN_NAMES, "original Conan plugin configuration is incomplete")
    expected_marker = {
        "manifest_sha256": spec["manifest_sha256"],
        "plugin_files_sha256": hashlib.sha256(json.dumps(
            plugin_files, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
    }
    files, links = manifest["files"], manifest["links"]
    require(len(files) <= 30000 and not set(files) & set(links), "Conan seed inventory conflicts")
    names = set(files) | set(links)
    for name in names:
        relative(name)
        require(".conan.db" not in PurePosixPath(name).parts and ".git" not in PurePosixPath(name).parts,
                "Conan input contains unrelated state")
    destination = workspace / ".reviewbench/conan2"
    marker = workspace / ".reviewbench/conan-seed.json"
    require(not destination.is_symlink() and destination.resolve().is_relative_to(workspace.resolve()),
            "Conan home escaped workspace")
    require(not marker.is_symlink(), "Conan seed marker redirected")
    if marker.exists():
        require(regular(marker) and json.loads(marker.read_text()) == expected_marker,
                "Conan home belongs to different inputs")
        require(destination.is_dir(), "Conan home missing after seed")
        return destination
    require(not destination.exists(), "Conan destination exists without its seed identity")
    observed = tree(source)
    require(set(observed) == names, "Conan seed inventory changed")
    for name, row in files.items():
        path = observed[name]
        require(regular(path) and digest(path) == row["sha256"]
                and bool(path.stat().st_mode & 0o111) == bool(row["mode"] & 0o111),
                "Conan original dependency file changed: " + name)
    for name, target in links.items():
        path = observed[name]
        require(not PurePosixPath(target).is_absolute() and path.is_symlink() and os.readlink(path) == target,
                "Conan original dependency link changed: " + name)
        resolved = path.resolve()
        require(resolved.is_relative_to(source.resolve()) and resolved.is_file()
                and resolved.relative_to(source).as_posix() in files, "Conan input link escaped complete package")
    plugins = tree(plugin_source)
    require(set(plugins) == PLUGIN_NAMES and not names & PLUGIN_NAMES, "Conan plugin layer overlaps the binary seed")
    for name, row in plugin_files.items():
        path = plugins[name]
        require(regular(path) and digest(path) == row["sha256"] and path.stat().st_size == row["bytes"]
                and bool(path.stat().st_mode & 0o111) == bool(row["mode"] & 0o111),
                "original Conan plugin bytes or executable mode changed: " + name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Conan updates its database/LRU. Never hardlink those writes into shared inputs.
    shutil.copytree(source, destination, symlinks=True)
    for name, path in plugins.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    marker.write_text(json.dumps(expected_marker) + "\n")
    require(digest(destination / "p/cache.sqlite3") == files["p/cache.sqlite3"]["sha256"], "Conan database copy changed")
    return destination


def configure(spec, workspace, run=subprocess.run):
    """Restore the candidate's exact recipe bytes after the original BSMPT sed."""
    recipe = workspace / "conanfile.py"
    require(regular(recipe), "current candidate Conan recipe missing or redirected")
    original, mode = recipe.read_bytes(), recipe.stat().st_mode & 0o777
    env = {**os.environ, "CONAN_HOME": str(workspace / ".reviewbench/conan2")}
    try:
        return run(["bash", "-e", "-o", "pipefail", "-c", spec["configure"]],
                   cwd=workspace, env=env).returncode
    finally:
        if spec["profile"] == "bsmpt4":
            # The frozen configure script changes nlopt 2.9.1 -> 2.7.1. This
            # setup edit is not an LLM patch; keep the actual candidate intact.
            if recipe.is_symlink():
                recipe.unlink()
            recipe.write_bytes(original)
            recipe.chmod(mode)
            require(recipe.read_bytes() == original, "candidate recipe restoration failed")


def execute(spec, workspace, mode, run=subprocess.run):
    require(spec.get("schema_version") == "reviewbench-original-conan/v1"
            and spec.get("profile") in {"bsmpt4", "lapis16"}, "Conan configuration schema differs")
    require(workspace.is_dir() and not workspace.is_symlink(), "candidate workspace missing or redirected")
    destination = seed(spec, workspace)
    (workspace / ".reviewbench/tmp").mkdir(parents=True, exist_ok=True)
    (workspace / ".reviewbench/home").mkdir(parents=True, exist_ok=True)
    # Reconfigure on every developer check, so candidate-added source files and
    # changed CMake/Conan declarations are not frozen to the start snapshot.
    status = configure(spec, workspace, run=run)
    if status or mode == "configure":
        return status
    env = {**os.environ, "CONAN_HOME": str(destination)}
    return run(["bash", "-e", "-o", "pipefail", "-c", spec["build"]],
               cwd=workspace, env=env).returncode


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--mode", choices=("configure", "check"), required=True)
    args = parser.parse_args()
    require(regular(args.spec), "Conan runtime specification redirected")
    return execute(json.loads(args.spec.read_text()), args.workspace, args.mode)


if __name__ == "__main__":
    raise SystemExit(main())
