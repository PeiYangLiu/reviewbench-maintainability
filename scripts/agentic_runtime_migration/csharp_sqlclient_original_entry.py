"""Compile the complete SqlClient candidate with the original SDK6/setup."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

try:
    from .csharp_original_cli_entry import digest, require, run, seed
except ImportError:  # The same hash-bound core is mounted beside the runtime entry.
    from csharp_original_cli_entry import digest, require, run, seed

SDK = "/opt/reviewbench-dotnet6"
SDK8 = "/usr/share/dotnet"
VERSION = "6.0.400"
CSC = "6b5d7141a2f8593cc901d6df092cf77a9c669b61edd29ea6037d20b8a8a59418"
CACHE = "/opt/reviewbench-cache/original-sqlclient"
SETUP = "9255e2d628b0b3ad9147383287b27e2492a37b9f6d352fb7a0a73a996f96b330"
TARGET = "src/Microsoft.Data.SqlClient/netcore/src/Microsoft/Data/SqlClient/SNI/SNITcpHandle.cs"


def apply_original_setup(source, helper, *, call=subprocess.run):
    require(helper.is_file() and not helper.is_symlink() and digest(helper) == SETUP,
            "original SqlClient source setup differs")
    target = source / TARGET
    require(target.is_file() and not target.is_symlink(), "original SqlClient setup target missing")
    before = {p.relative_to(source).as_posix(): digest(p) for p in source.rglob("*")
              if p.is_file() and not p.is_symlink()}
    original_bytes = target.read_bytes()
    original = target.read_text()
    updated = original.replace("_isTDSS &&", "_isTDS8 &&")
    expected = updated.encode() if updated != original else original_bytes
    result = call([sys.executable, str(helper), str(target)], cwd=source)
    require(result.returncode == 0 and target.read_bytes() == expected,
            "original SqlClient source setup changed unexpected bytes")
    after = {p.relative_to(source).as_posix(): digest(p) for p in source.rglob("*")
             if p.is_file() and not p.is_symlink()}
    require(set(before) == set(after), "original SqlClient setup changed candidate paths")
    require(all(after[n] == value for n, value in before.items() if n != TARGET),
            "original SqlClient setup changed another candidate source")
    return {"path": TARGET, "before": before[TARGET], "after": after[TARGET],
            "original_python_setup_sha256": SETUP, "root_candidate_changed": False}


def staging_area(workspace, mode):
    root = Path(CACHE)
    require(root.is_dir() and not root.is_symlink()
            and not root.resolve().is_relative_to(workspace.resolve()),
            "SqlClient staging requires its independent local-disk cache")
    area = root / ("public" if mode == "public" else "grading")
    require(not area.is_symlink() and area.resolve().is_relative_to(root.resolve()), "SqlClient stage redirected")
    return area


def execute(spec, workspace, mode, *, call=subprocess.run):
    require(spec.get("schema_version") == "reviewbench-original-sqlclient/v1"
            and spec.get("profile") == "sqlclient16", "original SqlClient profile differs")
    require(workspace.is_dir() and not workspace.is_symlink(), "SqlClient candidate redirected")
    require(digest(Path(SDK) / "sdk" / VERSION / "Roslyn/bincore/csc.dll") == CSC,
            "original SqlClient compiler changed")
    area = staging_area(workspace, mode)
    packages = seed(Path(spec["packages"]) / "packages", Path(CACHE) / "nuget",
                    {"asset_sha256": spec["packages_asset_sha256"]})
    if area.exists():
        shutil.rmtree(area)
    area.mkdir()
    if mode == "grade-build":
        private = Path(spec["private"])
        require(private.is_dir() and not private.is_symlink(), "original SqlClient harness missing")
        shutil.copytree(private, area, dirs_exist_ok=True, symlinks=False)
    (area / "global.json").write_text(json.dumps({"sdk": {"version": VERSION, "rollForward": "disable", "allowPrerelease": False}}) + "\n")
    (area / "NuGet.Config").write_text(
        '<configuration><packageSources><clear /><add key="original-pinned" value="'
        + spec["packages"] + '/feed" /></packageSources></configuration>\n')
    env = {**os.environ, "PATH": SDK + ":" + os.environ["PATH"], "DOTNET_ROOT": SDK,
           "DOTNET_ROOT_X64": SDK, "DOTNET_HOST_PATH": SDK + "/dotnet",
           "DOTNET_MULTILEVEL_LOOKUP": "0", "NUGET_PACKAGES": str(packages)}
    source = area / "repo"
    staging_env = {**env, "DOTNET_ROOT": SDK8, "DOTNET_ROOT_X64": SDK8,
                   "DOTNET_HOST_PATH": SDK8 + "/dotnet"}
    run([SDK8 + "/dotnet", spec["tool"] + "/CandidateStage.dll",
         str(workspace), str(source), VERSION, ""], cwd=area, env=staging_env, call=call)
    ledger = area / "candidate-source-ledger.json"
    require(ledger.is_file() and not ledger.is_symlink(), "SqlClient source ledger missing")
    setup = apply_original_setup(source, Path(spec["setup"]), call=call)
    selected = call([SDK + "/dotnet", "--version"], cwd=area, env=env, capture_output=True, text=True)
    require(selected.returncode == 0 and selected.stdout.strip() == VERSION, "SqlClient selected another SDK")
    for command in spec["public_commands"] if mode == "public" else spec["private_commands"]:
        run(["bash", "-e", "-o", "pipefail", "-c", command], cwd=area, env=env, call=call)
    (area / "original-build-binding.json").write_text(json.dumps({
        "sdk": VERSION, "compiler_sha256": CSC, "original_setup": setup,
        "source_ledger_sha256": digest(ledger), "source_whitelist_used": False, "mode": mode,
    }, sort_keys=True) + "\n")
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--mode", choices=("public", "grade-build"), required=True)
    args = parser.parse_args()
    require(args.spec.is_file() and not args.spec.is_symlink(), "SqlClient specification redirected")
    return execute(json.loads(args.spec.read_text()), args.workspace, args.mode)


if __name__ == "__main__":
    raise SystemExit(main())
