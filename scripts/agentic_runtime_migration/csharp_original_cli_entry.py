"""Whole-candidate original Dafny/Roslyn builds with separate, pinned SDK hosts."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

SDK8 = "/usr/share/dotnet"
CACHE = "/opt/reviewbench-cache/original-csharp-cli"
PINS = {
    "dafny13": ("/opt/reviewbench-dotnet6", "6.0.400",
                "6b5d7141a2f8593cc901d6df092cf77a9c669b61edd29ea6037d20b8a8a59418"),
    "roslyn19": ("/opt/reviewbench-dotnet7", "7.0.410",
                 "0637279674e12a761bf5c1f625a078972ccb7d049ee779bdcd32d58c83f2a5dd"),
}
ROSLYN_SETUP = "52f57ce5b1bbe070600278b7c28111f73dbd41e512e00e0b354341e3e30f7bee"
ROSLYN_ROOTS = ("src/Workspaces/Core/Portable/Workspace/",
                "src/Workspaces/CSharp/Portable/Workspace/LanguageServices/")


def require(value, message):
    if not value:
        raise RuntimeError(message)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024**2), b""):
            h.update(chunk)
    return h.hexdigest()


def run(command, *, cwd, env, call=subprocess.run):
    result = call(command, cwd=cwd, env=env)
    if result.returncode:
        raise SystemExit(result.returncode)


def apply_original_roslyn_setup(source, helper, *, call=subprocess.run):
    require(helper.is_file() and not helper.is_symlink() and digest(helper) == ROSLYN_SETUP,
            "original Roslyn setup bytes differ")
    paths = {p.relative_to(source).as_posix(): p for p in source.rglob("*") if p.is_file() and not p.is_symlink()}
    before = {n: digest(p) for n, p in paths.items()}
    expected = dict(before)
    for name, path in paths.items():
        if name.endswith(".cs") and name.startswith(ROSLYN_ROOTS):
            old = path.read_text()
            text = old.replace("ExceptionUtilities.Unreachable()", "ExceptionUtilities.Unreachable")
            if text != old:
                expected[name] = hashlib.sha256(text.encode()).hexdigest()
    metadata = json.loads((source / "global.json").read_text())
    metadata["sdk"].update(version="7.0.100", allowPrerelease=False, rollForward="latestFeature")
    metadata["msbuild-sdks"]["Microsoft.DotNet.Arcade.Sdk"] = "8.0.0-beta.22466.3"
    expected["global.json"] = hashlib.sha256((json.dumps(metadata, indent=2) + "\n").encode()).hexdigest()
    result = call(["bash", str(helper)], cwd=source, env={**os.environ, "REPO_DIR": str(source)})
    require(result.returncode == 0, "original Roslyn setup failed")
    after = {p.relative_to(source).as_posix(): digest(p) for p in source.rglob("*")
             if p.is_file() and not p.is_symlink()}
    require(after == expected, "original Roslyn setup changed unaccounted candidate bytes")
    return {"original_adapter_sha256": ROSLYN_SETUP,
            "changed_paths": sorted(n for n in before if before[n] != after[n]),
            "source_root_is_staged_copy": True}


def seed(source, target, identity):
    require(source.is_dir() and not source.is_symlink(), "original dependency seed missing")
    require(not target.is_symlink(), "task-local dependency seed redirected")
    marker = target.parent / (target.name + "-seed.json")
    require(not marker.is_symlink(), "dependency seed marker redirected")
    if marker.is_file():
        require(target.is_dir() and json.loads(marker.read_text()) == identity, "dependency seed identity differs")
        return target
    require(not target.exists(), "unowned task-local dependency directory")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, symlinks=False)
    marker.write_text(json.dumps(identity, sort_keys=True) + "\n")
    return target


def staging_area(workspace, mode):
    root = Path(CACHE)
    require(root.is_dir() and not root.is_symlink()
            and not root.resolve().is_relative_to(workspace.resolve()),
            "whole-candidate staging needs its independent local-disk cache mount")
    area = root / ("public" if mode == "public" else "grading")
    require(not area.is_symlink() and area.resolve().is_relative_to(root.resolve()), "staging area redirected")
    return area


def execute(spec, workspace, mode, *, call=subprocess.run):
    require(spec.get("schema_version") == "reviewbench-original-csharp-cli/v1"
            and spec.get("profile") in PINS, "original CLI profile differs")
    require(workspace.is_dir() and not workspace.is_symlink(), "candidate root redirected")
    sdk_root, sdk, csc = PINS[spec["profile"]]
    require(digest(Path(sdk_root) / "sdk" / sdk / "Roslyn/bincore/csc.dll") == csc,
            "original CLI compiler bytes differ")
    area = staging_area(workspace, mode)
    packages = seed(Path(spec["packages"]) / "packages", Path(CACHE) / "nuget",
                    {"asset_sha256": spec["packages_asset_sha256"]})
    env = {**os.environ, "PATH": sdk_root + ":" + os.environ["PATH"],
           "DOTNET_ROOT": sdk_root, "DOTNET_ROOT_X64": sdk_root,
           "DOTNET_HOST_PATH": sdk_root + "/dotnet", "DOTNET_MULTILEVEL_LOOKUP": "0",
           "NUGET_PACKAGES": str(packages)}
    if spec["profile"] == "dafny13":
        gradle = seed(Path(spec["gradle"]), Path(CACHE) / "gradle",
                      {"asset_sha256": spec["gradle_asset_sha256"]})
        (gradle / "init.d").mkdir(exist_ok=True)
        (gradle / "init.d/reviewbench-offline.gradle").write_text(
            "gradle.startParameter.offline = true\n"
            "gradle.startParameter.maxWorkerCount = 2\n")
        env.update(GRADLE_USER_HOME=str(gradle),
                   GRADLE_OPTS="-Dorg.gradle.daemon=false -Dorg.gradle.parallel=false -Dorg.gradle.vfs.watch=false")
    if area.exists():
        shutil.rmtree(area)
    area.mkdir(parents=True)
    if mode == "grade-build":
        private = Path(spec["private"])
        require(private.is_dir() and not private.is_symlink(), "original private harness absent")
        shutil.copytree(private, area, dirs_exist_ok=True, symlinks=False)
    (area / "global.json").write_text(json.dumps({"sdk": {"version": sdk, "rollForward": "disable", "allowPrerelease": False}}) + "\n")
    (area / "NuGet.Config").write_text(
        '<configuration><packageSources><clear /><add key="original-pinned" value="'
        + spec["packages"] + '/feed" /></packageSources></configuration>\n')
    source = area / "repo"
    staging_env = {**env, "DOTNET_ROOT": SDK8, "DOTNET_ROOT_X64": SDK8,
                   "DOTNET_HOST_PATH": SDK8 + "/dotnet"}
    run([SDK8 + "/dotnet", spec["tool"] + "/CandidateStage.dll",
         str(workspace), str(source), sdk, ""], cwd=area, env=staging_env, call=call)
    ledger = area / "candidate-source-ledger.json"
    require(ledger.is_file() and not ledger.is_symlink(), "candidate source ledger absent")
    adaptation = None
    if spec["profile"] == "roslyn19":
        adaptation = apply_original_roslyn_setup(source, Path(spec["adapter"]), call=call)
    checked = call([sdk_root + "/dotnet", "--version"], cwd=area, env=env, text=True, capture_output=True)
    require(checked.returncode == 0 and checked.stdout.strip() == sdk, "SUT selected a different SDK")
    commands = spec["public_commands"] if mode == "public" else spec["private_commands"]
    for command in commands:
        run(["bash", "-e", "-o", "pipefail", "-c", command], cwd=area, env=env, call=call)
    (area / "original-build-binding.json").write_text(json.dumps({
        "candidate_ledger_sha256": digest(ledger), "sdk": sdk, "csc_sha256": csc,
        "original_setup": adaptation, "mode": mode, "source_whitelist_used": False,
        "original_gradle_build_disabled": False,
    }, sort_keys=True) + "\n")
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--mode", choices=("public", "grade-build"), required=True)
    args = parser.parse_args()
    require(args.spec.is_file() and not args.spec.is_symlink(), "original CLI specification redirected")
    return execute(json.loads(args.spec.read_text()), args.workspace, args.mode)


if __name__ == "__main__":
    raise SystemExit(main())
