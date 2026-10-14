"""Stage the whole current candidate and execute the original SDK6 build options."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

SDK6 = "/opt/reviewbench-dotnet6"
SDK8 = "/usr/share/dotnet"
CSC = SDK6 + "/sdk/6.0.400/Roslyn/bincore/csc.dll"
CSC_SHA256 = "6b5d7141a2f8593cc901d6df092cf77a9c669b61edd29ea6037d20b8a8a59418"
ADAPTER_SHA256 = "65def34e652616912de259af984ac883353fa418955fe40546a34721689771c4"
ADAPTER_FILE = "src/GraphQL/Validation/ValidationOptions.cs"
EXTERNAL_STAGE = "/opt/reviewbench-cache/original-sdk6-staging"
MARKER = "    public readonly struct ValidationOptions\n    {\n"
INSERT = (MARKER + "        public ValidationOptions()\n        {\n"
          "            Rules = null;\n            CancellationToken = default;\n        }\n\n")


def require(value, message):
    if not value:
        raise RuntimeError(message)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(data)
    return h.hexdigest()


def run(command, *, cwd, env, call=subprocess.run):
    result = call(command, cwd=cwd, env=env)
    if result.returncode:
        raise SystemExit(result.returncode)


def apply_original_adapter(repository, helper, ledger, *, call=subprocess.run):
    """Execute the original setup on the staged copy, never on the solver tree."""
    require(helper.is_file() and not helper.is_symlink() and digest(helper) == ADAPTER_SHA256,
            "original GraphQL build adapter changed")
    target = repository / ADAPTER_FILE
    require(target.is_file() and not target.is_symlink(), "original adapter candidate target is absent")
    before = target.read_bytes()
    text = before.decode()
    expected = before if "public ValidationOptions()" in text else text.replace(MARKER, INSERT, 1).encode()
    for relative, sha256 in ledger.items():
        require(digest(repository / relative) == sha256, "staged candidate changed before original setup")
    result = call(["bash", str(helper)], cwd=repository,
                  env={**os.environ, "REPO_DIR": str(repository)})
    require(result.returncode == 0, "original GraphQL setup command failed")
    require(target.read_bytes() == expected, "original GraphQL setup made an unaccounted source change")
    for relative, sha256 in ledger.items():
        if relative != ADAPTER_FILE:
            require(digest(repository / relative) == sha256, "original setup changed another production file")
    return {"path": ADAPTER_FILE, "before": hashlib.sha256(before).hexdigest(),
            "after": hashlib.sha256(expected).hexdigest(), "root_candidate_changed": False}


def seed_packages(spec, workspace):
    source = Path(spec["packages"]) / "packages"
    destination = workspace / ".reviewbench/nuget"
    marker = workspace / ".reviewbench/sdk6-packages.json"
    require(source.is_dir() and not source.is_symlink(), "original package seed is absent")
    require(not destination.is_symlink() and destination.resolve().is_relative_to(workspace.resolve()),
            "task-local NuGet home escaped workspace")
    require(not marker.is_symlink(), "package seed marker redirected")
    expected = {"asset_sha256": spec["packages_asset_sha256"]}
    if marker.exists():
        require(json.loads(marker.read_text()) == expected and destination.is_dir(),
                "task-local NuGet home belongs to different inputs")
        return destination
    require(not destination.exists(), "unowned task-local NuGet home")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, symlinks=False)
    marker.write_text(json.dumps(expected) + "\n")
    return destination


def staging_area(spec, workspace, mode):
    if spec["profile"] == "osu30":
        require(spec.get("staging_root") == EXTERNAL_STAGE, "original analyzer isolation root differs")
        root = Path(EXTERNAL_STAGE)
        require(root.is_dir() and not root.is_symlink()
                and not root.resolve().is_relative_to(workspace.resolve()),
                "original analyzer isolation needs its separate local-disk cache mount")
        area = root / ("public" if mode == "public" else "grading")
        require(not area.is_symlink() and area.resolve().is_relative_to(root.resolve()),
                "isolated candidate staging directory escaped its local-disk mount")
        return area
    area = workspace / (".reviewbench/sdk6-public" if mode == "public" else ".reviewbench/sdk6-grading")
    require(not area.is_symlink() and area.resolve().is_relative_to(workspace.resolve()),
            "candidate staging directory escaped workspace")
    return area


def execute(spec, workspace, mode, *, call=subprocess.run):
    require(spec.get("schema_version") == "reviewbench-original-sdk6-project/v1"
            and spec.get("profile") in {"graphql25", "osu30"}, "original SDK6 profile differs")
    require(workspace.is_dir() and not workspace.is_symlink(), "candidate workspace redirected")
    require(digest(CSC) == CSC_SHA256, "original SDK6 compiler bytes changed")
    packages = seed_packages(spec, workspace)
    area = staging_area(spec, workspace, mode)
    if area.exists():
        shutil.rmtree(area)
    area.mkdir(parents=True)
    if mode == "grade-build":
        private = Path(spec["private"])
        require(private.is_dir() and not private.is_symlink(), "original private harness missing")
        shutil.copytree(private, area, dirs_exist_ok=True, symlinks=False)
    (area / "global.json").write_text(json.dumps(
        {"sdk": {"version": "6.0.400", "rollForward": "disable", "allowPrerelease": False}}) + "\n")
    source = area / "repo"
    stager_env = {**os.environ, "DOTNET_ROOT": SDK8, "DOTNET_ROOT_X64": SDK8,
                  "DOTNET_HOST_PATH": SDK8 + "/dotnet", "DOTNET_MULTILEVEL_LOOKUP": "0"}
    run([SDK8 + "/dotnet", spec["tool"] + "/CandidateStage.dll", str(workspace), str(source), "6.0.400", ""],
        cwd=area, env=stager_env, call=call)
    ledger_file = area / "candidate-source-ledger.json"
    require(ledger_file.is_file() and not ledger_file.is_symlink(), "current candidate source ledger is absent")
    ledger = json.loads(ledger_file.read_text())
    adaptation = None
    if spec["profile"] == "graphql25":
        adaptation = apply_original_adapter(source, Path(spec["adapter"]), ledger, call=call)
    env = {**os.environ, "DOTNET_ROOT": SDK6, "DOTNET_ROOT_X64": SDK6,
           "DOTNET_HOST_PATH": SDK6 + "/dotnet", "DOTNET_MULTILEVEL_LOOKUP": "0",
           "NUGET_PACKAGES": str(packages)}
    checked = call([SDK6 + "/dotnet", "--version"], cwd=area, env=env, capture_output=True, text=True)
    require(checked.returncode == 0 and checked.stdout.strip() == "6.0.400", "SUT selected another SDK")
    commands = spec["public_commands"] if mode == "public" else spec["private_commands"]
    for command in commands:
        run(["bash", "-e", "-o", "pipefail", "-c", command], cwd=area, env=env, call=call)
    (area / "original-build-binding.json").write_text(json.dumps({
        "source_ledger_sha256": digest(ledger_file), "original_setup": adaptation,
        "sut_sdk": "6.0.400", "sut_csc_sha256": CSC_SHA256,
        "source_whitelist_used": False, "mode": mode,
    }) + "\n")
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--mode", choices=("public", "grade-build"), required=True)
    args = parser.parse_args()
    require(args.spec.is_file() and not args.spec.is_symlink(), "SDK6 specification changed")
    return execute(json.loads(args.spec.read_text()), args.workspace, args.mode)


if __name__ == "__main__":
    raise SystemExit(main())
