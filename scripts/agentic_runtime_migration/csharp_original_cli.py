"""Hash-bound original full-project Dafny13 SDK6 and Roslyn19 SDK7 workflows."""
from __future__ import annotations

import json
from pathlib import Path
import shlex
import shutil
import tempfile

from reviewbench_maintainability.agentic.assets import asset_descriptor
from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, atomic_json, canonical_hash, component, file_hash,
)
from .csharp_lane import completion_pattern
from .csharp_original_cli_entry import PINS, CACHE

TOOLS = "/opt/reviewbench-assets/original-csharp-cli"
STAGER = "/opt/reviewbench-assets/original-csharp-stager"
PACKAGES = "/opt/reviewbench-assets/original-csharp-packages"
GRADLE = "/opt/reviewbench-assets/original-dafny-gradle"
PRIVATE = "/opt/reviewbench-assets/original-csharp-tests"
PROFILES = {
    13: {
        "name": "dafny13", "repo": "dafny-lang/dafny",
        "id": "github__dafny-lang__dafny__pull_4342__comment_1307681583__api_design",
        "start": "853e95bdb48d77af644c0d53abbd251f83443c6f",
        "spec": "de4761392de3b855f0cffc10f7ee51c9e7dafd16c5d2b5d09d5ad24bcddd8002",
        "golden": "e7a1ed48673df371400ea34adef453a9efac2d44e4b18a9a4ed81ad1cda62598",
        "project": "repo/Source/Dafny/Dafny.csproj",
        "production": ["Source/Dafny/Dafny.csproj"],
        "files": {"oracle/sample.dfy": "5f987bec3eefa47fa8fd1addbe70497b5c9e809f08f0812b59d046b5cfaf60f1"},
        "image": "sha256:ca5e6737718b6c8b46f06f8c49ed3218958cfd320e5278f4f605eecb8cdf9851",
    },
    19: {
        "name": "roslyn19", "repo": "dotnet/roslyn",
        "id": "github__dotnet__roslyn__pull_62840__comment_977100725__api_design",
        "start": "de801be95e3fc621ba035bbe4f2fe44012b6b5c0",
        "spec": "746fb4ca61fab6def77b94aee2da3b37371d368cffc19c660156bef3a26aaeb9",
        "golden": "d1c5ba6b361abb63ecadd19439b546d695da0733b4220a8f00606d2d0344976f",
        "project": "oracle/Oracle.csproj",
        "production": ["src/Workspaces/Core/Portable/Microsoft.CodeAnalysis.Workspaces.csproj",
                       "src/Workspaces/CSharp/Portable/Microsoft.CodeAnalysis.CSharp.Workspaces.csproj"],
        "files": {
            "oracle/Oracle.csproj": "9db476deb960271e551e66e9dda5933ad42675cba195ad1b609d1a53ebeddbb1",
            "oracle/Program.cs": "4900d49ac13270967b537172e16ba24c51b99bf709222317109d95dc2321ddbe",
            "sdk-adapter.sh": "52f57ce5b1bbe070600278b7c28111f73dbd41e512e00e0b354341e3e30f7bee",
        },
        "image": "sha256:c409b0b017c4205aceae82591b45928ae368a4735c5d1700e109d478d36e7ef2",
    },
}


def checked(reference, label):
    if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
        raise ContractError("original CLI input reference missing: " + label)
    path = Path(reference["path"])
    if (not path.is_absolute() or not path.is_file() or path.is_symlink()
            or path.stat().st_size > 16 * 1024**2 or file_hash(path) != reference["sha256"]):
        raise ContractError("original CLI input reference changed: " + label)
    return json.loads(path.read_text())


def bind_original(task, oracle, spec, directory):
    profile = PROFILES.get(oracle.get("index"))
    if not profile or task.get("repo") != profile["repo"]:
        return None
    if (task.get("id") != profile["id"] or task.get("start_sha") != profile["start"]
            or canonical_hash(spec) != profile["spec"]
            or oracle.get("evidence", {}).get("source_only_patch_sha256") != profile["golden"]):
        raise ContractError("original CLI needs the complete task/spec/golden binding")
    for name, digest in profile["files"].items():
        path = directory / name
        if not path.is_file() or path.is_symlink() or file_hash(path) != digest:
            raise ContractError("original CLI harness or setup changed: " + name)
    if (file_hash(directory / "source-only-golden.patch") != profile["golden"]
            or spec["pass_to_pass_tests"] != oracle["pass_to_pass_tests"]):
        raise ContractError("original CLI golden or assertion identities changed")
    return profile


def original_commands(spec, profile):
    parts = spec["prepare_command"].split(" && ")
    if len(parts) != 2:
        raise ContractError("original CLI preparation boundary differs")
    restore, build = parts
    if not restore.startswith("dotnet restore " + profile["project"] + " ") or not build.startswith("dotnet build " + profile["project"] + " "):
        raise ContractError("original CLI project boundary differs")
    sdk_root = PINS[profile["name"]][0]
    restore += " --source " + PACKAGES + "/feed --disable-parallel -p:NuGetAudit=false"
    build += " -m:1 -p:UseSharedCompilation=false -nr:false -p:NuGetAudit=false"
    private = [x.replace("dotnet ", sdk_root + "/dotnet ", 1) for x in (restore, build)]
    public = [command.replace(profile["project"], "repo/" + production, 1)
              for production in profile["production"] for command in private]
    return public, private


def inputs(ctx, task, profile):
    references = ctx.config.get("migration", {}).get("original_cli_projects", {}).get(profile["name"], {})
    deps = checked(references.get("dependencies"), "full-project NuGet graph")
    readback = checked(references.get("readback"), "NuGet independent readback")
    host = checked(references.get("host"), "neutral compiler host")
    image = checked(references.get("image"), "independent image recovery")
    sdk_root, sdk, compiler = PINS[profile["name"]]
    if (deps.get("status") != "original_CSharp_dependency_graphs_prepared"
            or deps.get("task_id") != task["id"] or deps.get("start_sha") != task["start_sha"]
            or deps.get("image") != profile["image"] or deps.get("sdk_version") != sdk
            or deps.get("sdk_root") != sdk_root or not deps.get("original_source_unchanged")
            or deps.get("project_build_test_model_judge_calls") != 0
            or len(deps.get("restore_attempts", [])) != 2
            or any(row["exit_status"] != 0 for row in deps["restore_attempts"])):
        raise ContractError("original CLI dependencies are incomplete or relabelled")
    if (readback.get("asset") != deps["packages"]
            or readback.get("status") != "independent_Blob_component_readback_verified"
            or not readback.get("all_paths_bytes_full_modes_verified") or not readback.get("gzip_crc_and_eof_verified")):
        raise ContractError("original CLI NuGet closure has no complete independent readback")
    compiler_field = "sdk6_compiler_sha256" if profile["name"] == "dafny13" else "sdk7_compiler_sha256"
    if (host.get("status") != "original_CSharp_neutral_host_prepared" or host.get("image") != profile["image"]
            or host.get("sdk_version") != sdk or host.get("sdk_root") != sdk_root
            or host.get(compiler_field) != compiler or host.get("version_probe", {}).get("exit_status") != 0
            or image.get("image") != profile["image"] or not image.get("all_layers_match")
            or not image.get("gzip_crc_and_eof_verified") or image.get("load_exit_status") != 0):
        raise ContractError("original CLI compiler/image recovery binding differs")
    if canonical_hash(deps["package_index"]) != deps["package_index_sha256"]:
        raise ContractError("original CLI package index changed")
    for key in ("packages", "tool"):
        if asset_descriptor(deps[key], ctx.output)["source_kind"] != "dependency":
            raise ContractError("original CLI public input contains reference material")
    gradle = None
    if profile["name"] == "dafny13":
        gradle = checked(references.get("gradle"), "original Gradle dependency graph")
        restored = checked(references.get("gradle_readback"), "Gradle independent readback")
        if (gradle.get("status") != "original_Dafny_Gradle_inputs_prepared"
                or gradle.get("task_id") != task["id"] or gradle.get("start_sha") != task["start_sha"]
                or gradle.get("image") != profile["image"] or gradle.get("gradle_version") != "8.1.1"
                or not gradle.get("start_and_golden_Java_sources_identical")
                or gradle.get("original_gradle_build_target_disabled") is not False
                or gradle.get("offline", {}).get("exit_status") != 0
                or gradle.get("project_build_test_model_judge_calls") != 0
                or restored.get("asset") != gradle["asset"]
                or restored.get("status") != "independent_Blob_component_readback_verified"
                or not restored.get("all_paths_bytes_full_modes_verified")
                or not restored.get("gzip_crc_and_eof_verified")
                or asset_descriptor(gradle["asset"], ctx.output)["source_kind"] != "dependency"):
            raise ContractError("original Dafny Gradle graph has no verified offline closure")
    return deps, gradle, references


def migrate(ctx, task, oracle):
    if oracle.get("index") not in PROFILES or task.get("repo") != PROFILES[oracle["index"]]["repo"]:
        return None
    spec = json.loads((ctx.legacy / "csharp/specs.json").read_text())[str(oracle["index"])]
    directory = ctx.task_directory(task, oracle, "csharp")
    profile = bind_original(task, oracle, spec, directory)
    deps, gradle, references = inputs(ctx, task, profile)
    image, audit = ctx.image(profile["image"])
    if image != profile["image"]:
        raise ContractError("original CLI image identity differs")
    public, private = original_commands(spec, profile)
    with tempfile.TemporaryDirectory(prefix="original-cli-harness-", dir=ctx.scratch) as temporary:
        root = Path(temporary)
        for name in profile["files"]:
            if not name.startswith("oracle/"):
                continue
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(directory / name, target)
        if profile["name"] == "roslyn19":
            for name in ("Directory.Build.props", "Directory.Build.targets"):
                (root / "oracle" / name).write_text("<Project />\n")
        harness = ctx.bundle(root, role="grader")
    value = {"schema_version": "reviewbench-original-csharp-cli/v1", "profile": profile["name"],
             "packages": PACKAGES, "packages_asset_sha256": deps["packages"]["sha256"],
             "tool": STAGER, "private": PRIVATE, "public_commands": public, "private_commands": private,
             "adapter": TOOLS + "/sdk-adapter.sh" if profile["name"] == "roslyn19" else None}
    if gradle:
        value.update(gradle=GRADLE, gradle_asset_sha256=gradle["asset"]["sha256"])
    config_file = ctx.output / "original-cli" / (component(task["id"]) + "-spec.json")
    atomic_json(config_file, value)
    assets = [
        {**deps["packages"], "kind": "archive", "visibility": "developer", "target": PACKAGES},
        {**deps["tool"], "kind": "archive", "visibility": "developer", "target": STAGER},
        {**ctx.copy_asset(Path(__file__).with_name("csharp_original_cli_entry.py"), "original-cli"),
         "kind": "file", "visibility": "developer", "target": TOOLS + "/entry.py"},
        {**ctx.copy_asset(config_file, "original-cli"), "kind": "file", "visibility": "developer", "target": TOOLS + "/spec.json"},
        {**harness, "kind": "archive", "visibility": "grader", "target": PRIVATE},
    ]
    if gradle:
        assets.append({**gradle["asset"], "kind": "archive", "visibility": "developer", "target": GRADLE})
    if profile["name"] == "roslyn19":
        assets.append({**ctx.copy_asset(directory / "sdk-adapter.sh", "original-cli"),
                       "kind": "file", "visibility": "developer", "target": TOOLS + "/sdk-adapter.sh"})
    sdk_root, sdk, csc = PINS[profile["name"]]
    command = shlex.join(["python3", TOOLS + "/entry.py", "--spec", TOOLS + "/spec.json", "--workspace", "/workspace"])
    recipe = RuntimeRecipe(
        task_id=task["id"], repo=task["repo"], start_sha=task["start_sha"], lane="csharp",
        image=image, image_audit=audit, cpus=2, memory_mb=4096, build_scratch_mb=4096,
        command_timeout_seconds=spec["timeout"], submodules=ctx.modules(task), assets=assets,
        cache_mounts={"original-csharp-cli": CACHE},
        env={"TMPDIR": "/workspace/.reviewbench/tmp", "DOTNET_CLI_HOME": "/workspace/.reviewbench/dotnet-home",
             "DOTNET_ROOT": sdk_root, "DOTNET_ROOT_X64": sdk_root, "DOTNET_HOST_PATH": sdk_root + "/dotnet",
             "DOTNET_MULTILEVEL_LOOKUP": "0", "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
             "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1", "DOTNET_NOLOGO": "1", "DOTNET_PROCESSOR_COUNT": "2",
             "NUGET_PACKAGES": CACHE + "/nuget", "MSBUILDDISABLENODEREUSE": "1", "CI": "true"},
        bootstrap=["mkdir -p /workspace/.reviewbench/tmp /workspace/.reviewbench/dotnet-home"],
        dev_checks=[command + " --mode public"],
        grading={"multifile_build": True, "immutable_paths": ctx.immutable_tests(task),
                 "golden_patch": ctx.golden(task, oracle, "csharp", directory),
                 "bootstrap": [command + " --mode grade-build"],
                 "commands": [{"command": "cd " + CACHE + "/grading && "
                               + spec["test_command"].replace("dotnet ", sdk_root + "/dotnet ", 1),
                               "parser": "regex", "expected_tests": oracle["pass_to_pass_tests"],
                               "success_pattern": completion_pattern(directory, spec, oracle)}]},
    )
    recipe.validate()
    atomic_json(ctx.output / "original-cli" / (component(task["id"]) + ".json"), {
        "id": task["id"], "original_spec_sha256": profile["spec"], "original_harness_hashes": profile["files"],
        "original_golden_sha256": profile["golden"], "input_references": references,
        "production_projects": profile["production"], "original_test_command": spec["test_command"],
        "effective_public_commands": public, "effective_private_commands": private,
        "sut_compiler_sdk": sdk, "sut_compiler_sha256": csc, "staging_runtime_sdk": "8.0.425",
        "original_root_source_changes": [], "assertion_changes": [], "golden_changes": [],
        "original_dafny_Java_build_disabled": False,
    })
    return recipe
