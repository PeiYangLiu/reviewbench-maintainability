"""Original GraphQL25/osu30 ProjectReference workflows with the exact SDK6 compiler."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shlex
import shutil
import tempfile

from reviewbench_maintainability.agentic.assets import asset_descriptor
from reviewbench_maintainability.agentic.contracts import ContractError, RuntimeRecipe, atomic_json, canonical_hash, component, file_hash
from .csharp_lane import completion_pattern

IMAGE = "sha256:c878321ec43288c9f3cbcf5978315ba82cdde2df6f7ceca9c0a9348b8139d8ae"
SDK6 = "/opt/reviewbench-dotnet6"
TOOLS = "/opt/reviewbench-assets/csharp-original-sdk6"
STAGER = "/opt/reviewbench-assets/csharp-original-stage"
PACKAGES = "/opt/reviewbench-assets/csharp-original-nuget"
PRIVATE = "/opt/reviewbench-assets/csharp-original-tests"
RUNTIME = "/workspace/.reviewbench/sdk6-grading"
ISOLATED_STAGE = "/opt/reviewbench-cache/original-sdk6-staging"
PROFILES = {
    25: {
        "name": "graphql25", "repo": "graphql-dotnet/graphql-dotnet",
        "id": "github__graphql-dotnet__graphql-dotnet__pull_2999__comment_820505856__api_design",
        "start": "95812da2c7b6637ef2e41868b14f6acefb2495cf",
        "spec": "c686a3046b762a6f4de26e12a72f51d87a036cf38101da9f8bbaf3331c11f2c9",
        "project": "src/GraphQL/GraphQL.csproj",
        "golden": "d0a8be42ead421444ad2888b66581aa8e280ab15e098d8319f94dd1d2c105a6e",
        "files": {
            "oracle/Oracle.csproj": "90ebe36a5b0e82649a129cd7340fcae00f878ff3452e14c23528be17abd3cfb5",
            "oracle/Program.cs": "d75c836598dff339470e75cf5e745f2738f45f1b1bef36344e4d5a2bc747d1c9",
            "build-adapter.sh": "65def34e652616912de259af984ac883353fa418955fe40546a34721689771c4",
        },
    },
    30: {
        "name": "osu30", "repo": "ppy/osu",
        "id": "github__ppy__osu__pull_25371__comment_1387996678__api_design",
        "start": "81caa854e6a7ba3251658f1cd174bf6bad62b1ea",
        "spec": "da80830c193bc3363279a3cfd4d48729ee0b80e4e96a36e4ec69bd7d838a169d",
        "project": "osu.Game/osu.Game.csproj",
        "golden": "5286ce69d1740303b7a2f2525a1a9858efc057352b61a49358a0b585067080d5",
        "files": {
            "oracle/Oracle.csproj": "fdbe2582915dabf41685517688062773696d199c28861c179664e38ddc26150e",
            "oracle/Program.cs": "1ed0a9bff1127204e6c4d8a0292e14f1cffeb04f9a081656382eaa4f137d2798",
        },
    },
}


def checked(reference, label, limit=16 * 1024**2):
    if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
        raise ContractError("original SDK6 input reference missing: " + label)
    path = Path(reference["path"])
    if (not path.is_absolute() or not path.is_file() or path.is_symlink()
            or path.stat().st_size > limit or file_hash(path) != reference["sha256"]):
        raise ContractError("original SDK6 input reference changed: " + label)
    return json.loads(path.read_text())


def bind_original(task, oracle, spec, directory):
    profile = PROFILES.get(oracle.get("index"))
    if not profile or task.get("repo") != profile["repo"]:
        return None
    if (task.get("id") != profile["id"] or task.get("start_sha") != profile["start"]
            or canonical_hash(spec) != profile["spec"]
            or oracle.get("evidence", {}).get("source_only_patch_sha256") != profile["golden"]):
        raise ContractError("SDK6 adapter requires the complete original task/spec/golden binding")
    for name, digest in profile["files"].items():
        path = directory / name
        if not path.is_file() or path.is_symlink() or file_hash(path) != digest:
            raise ContractError("original SDK6 harness or setup changed: " + name)
    if file_hash(directory / "source-only-golden.patch") != profile["golden"]:
        raise ContractError("original SDK6 source-only golden changed")
    if spec["pass_to_pass_tests"] != oracle["pass_to_pass_tests"]:
        raise ContractError("original SDK6 assertion identities changed")
    return profile


def original_commands(spec, profile):
    parts = spec["prepare_command"].split(" && ")
    if len(parts) != 2:
        raise ContractError("original SDK6 preparation command boundary differs")
    restore, build = parts
    original_project = "oracle/Oracle.csproj"
    if (not restore.startswith("dotnet restore " + original_project)
            or not build.startswith("dotnet build " + original_project)):
        raise ContractError("original SDK6 project boundary differs")
    # Only dependency transport and bounded worker/node reuse are adjusted.
    restore = re.sub(r" --source \S+", "", restore)
    restore += " --source " + shlex.quote(PACKAGES + "/feed") + " --disable-parallel -p:NuGetAudit=false"
    build += " -m:1 -p:UseSharedCompilation=false -nr:false -p:NuGetAudit=false"
    private = [command.replace("dotnet ", SDK6 + "/dotnet ", 1) for command in (restore, build)]
    public_project = "repo/" + profile["project"]
    public = [command.replace(original_project, public_project, 1) for command in private]
    return public, private


def inputs(ctx, task, profile):
    references = ctx.config.get("migration", {}).get("original_sdk6_projects", {}).get(profile["name"], {})
    dependencies = checked(references.get("dependencies"), "dependency closure")
    readback = checked(references.get("readback"), "independent dependency recovery")
    shared = checked(references.get("shared"), "original compiler/stager")
    host = checked(references.get("host"), "original setup interpreter")
    image = checked(references.get("image"), "independently recovered original SDK6 image")
    if (dependencies.get("status") != "original_SDK6_dependency_graphs_prepared"
            or dependencies.get("task_id") != task["id"] or dependencies.get("start_sha") != task["start_sha"]
            or dependencies.get("image") != IMAGE or dependencies.get("sdk_version") != "6.0.400"
            or not dependencies.get("original_source_unchanged")
            or dependencies.get("project_build_test_model_judge_calls") != 0
            or len(dependencies.get("restore_attempts", [])) != 2
            or any(row["exit_status"] != 0 for row in dependencies["restore_attempts"])):
        raise ContractError("original SDK6 dependency graphs are incomplete or relabelled")
    if (readback.get("status") != "independent_Blob_component_readback_verified"
            or readback.get("asset") != dependencies["packages"]
            or not readback.get("all_paths_bytes_full_modes_verified") or not readback.get("gzip_crc_and_eof_verified")):
        raise ContractError("original SDK6 dependency closure has no complete independent readback")
    if (shared.get("status") != "original_SDK6_and_generic_stager_reused"
            or shared.get("image") != IMAGE or shared.get("sdk_version") != "6.0.400"
            or shared.get("sut_host") != SDK6 + "/dotnet"
            or shared.get("stager_host") != "/usr/share/dotnet/dotnet"
            or shared.get("version_check", {}).get("exit_status") != 0
            or shared.get("tool", {}).get("asset") != dependencies["tool"]):
        raise ContractError("original SDK6 compiler and SDK8 staging roles differ")
    if (host.get("status") != "original_SDK6_python_interpreter_layer_verified"
            or host.get("base_image") != IMAGE or not host.get("base_layers_match_prefix")
            or not host.get("original_sdk6_compiler_unchanged")
            or host.get("original_csc_sha256") != "6b5d7141a2f8593cc901d6df092cf77a9c669b61edd29ea6037d20b8a8a59418"
            or host.get("version_check", {}).get("exit_status") != 0):
        raise ContractError("SDK6 setup interpreter layer changed the original compiler")
    if image.get("image") != host["image"] or not image.get("all_layers_match") or not image.get("gzip_crc_and_eof_verified"):
        raise ContractError("original SDK6 image lacks independently verified layer recovery")
    for key in ("tool", "packages"):
        descriptor = asset_descriptor(dependencies[key], ctx.output)
        if descriptor["source_kind"] != "dependency":
            raise ContractError("original SDK6 public inputs include a reference artifact")
    if canonical_hash(dependencies["package_index"]) != dependencies["package_index_sha256"]:
        raise ContractError("original NuGet package identity index changed")
    if {row["package"].split("/")[0].lower() for row in dependencies["package_index"]} & {"graphql", "ppy.osu.game"}:
        raise ContractError("SUT package binary appeared in the dependency closure")
    return dependencies, shared, references, host["image"]


def migrate(ctx, task, oracle):
    if oracle.get("index") not in PROFILES or task.get("repo") != PROFILES[oracle["index"]]["repo"]:
        return None
    spec = json.loads((ctx.legacy / "csharp/specs.json").read_text())[str(oracle["index"])]
    directory = ctx.task_directory(task, oracle, "csharp")
    profile = bind_original(task, oracle, spec, directory)
    deps, shared, references, runtime_image = inputs(ctx, task, profile)
    image, audit = ctx.image(runtime_image)
    if image != runtime_image:
        raise ContractError("original SDK6 image identity differs")
    public, private = original_commands(spec, profile)
    with tempfile.TemporaryDirectory(prefix="original-sdk6-harness-", dir=ctx.scratch) as temporary:
        root = Path(temporary)
        for name in ("oracle/Oracle.csproj", "oracle/Program.cs"):
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(directory / name, target)
        for name in ("Directory.Build.props", "Directory.Build.targets"):
            (root / "oracle" / name).write_text("<Project />\n")
        harness = ctx.bundle(root, role="grader")
    value = {
        "schema_version": "reviewbench-original-sdk6-project/v1", "profile": profile["name"],
        "packages": PACKAGES, "packages_asset_sha256": deps["packages"]["sha256"],
        "tool": STAGER, "private": PRIVATE, "public_commands": public, "private_commands": private,
        "adapter": TOOLS + "/build-adapter.sh" if profile["name"] == "graphql25" else None,
    }
    if profile["name"] == "osu30":
        value["staging_root"] = ISOLATED_STAGE
    config_file = ctx.output / "original-sdk6" / (component(task["id"]) + "-spec.json")
    atomic_json(config_file, value)
    assets = [
        {**deps["packages"], "kind": "archive", "visibility": "developer", "target": PACKAGES},
        {**deps["tool"], "kind": "archive", "visibility": "developer", "target": STAGER},
        {**ctx.copy_asset(Path(__file__).with_name("csharp_original_sdk6_entry.py"), "original-sdk6"),
         "kind": "file", "visibility": "developer", "target": TOOLS + "/entry.py"},
        {**ctx.copy_asset(config_file, "original-sdk6"), "kind": "file", "visibility": "developer", "target": TOOLS + "/spec.json"},
        {**harness, "kind": "archive", "visibility": "grader", "target": PRIVATE},
    ]
    if profile["name"] == "graphql25":
        assets.append({**ctx.copy_asset(directory / "build-adapter.sh", "original-sdk6"),
                       "kind": "file", "visibility": "developer", "target": TOOLS + "/build-adapter.sh"})
    command = shlex.join(["python3", TOOLS + "/entry.py", "--spec", TOOLS + "/spec.json", "--workspace", "/workspace"])
    recipe = RuntimeRecipe(
        task_id=task["id"], repo=task["repo"], start_sha=task["start_sha"], lane="csharp",
        image=image, image_audit=audit, cpus=2, memory_mb=4096, build_scratch_mb=4096,
        command_timeout_seconds=spec["timeout"], submodules=ctx.modules(task), assets=assets,
        cache_mounts={"original-sdk6-staging": ISOLATED_STAGE} if profile["name"] == "osu30" else {},
        env={"TMPDIR": "/workspace/.reviewbench/tmp", "DOTNET_CLI_HOME": "/workspace/.reviewbench/dotnet-home",
             "DOTNET_ROOT": SDK6, "DOTNET_ROOT_X64": SDK6, "DOTNET_HOST_PATH": SDK6 + "/dotnet",
             "DOTNET_MULTILEVEL_LOOKUP": "0", "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
             "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1", "DOTNET_NOLOGO": "1", "DOTNET_PROCESSOR_COUNT": "2",
             "NUGET_PACKAGES": "/workspace/.reviewbench/nuget", "MSBUILDDISABLENODEREUSE": "1", "CI": "true"},
        bootstrap=["mkdir -p /workspace/.reviewbench/tmp /workspace/.reviewbench/dotnet-home"],
        dev_checks=[command + " --mode public"],
        grading={"multifile_build": True, "immutable_paths": ctx.immutable_tests(task),
                 "golden_patch": ctx.golden(task, oracle, "csharp", directory),
                 "bootstrap": [command + " --mode grade-build"],
                 "commands": [{"command": "cd " + (ISOLATED_STAGE + "/grading" if profile["name"] == "osu30" else RUNTIME)
                               + " && " + spec["test_command"].replace("dotnet ", SDK6 + "/dotnet ", 1),
                               "parser": "regex", "expected_tests": oracle["pass_to_pass_tests"],
                               "success_pattern": completion_pattern(directory, spec, oracle)}]},
    )
    recipe.validate()
    atomic_json(ctx.output / "original-sdk6" / (component(task["id"]) + ".json"), {
        "id": task["id"], "original_spec_sha256": profile["spec"], "original_harness_hashes": profile["files"],
        "original_golden_sha256": profile["golden"], "input_references": references,
        "original_setup_on_staged_candidate_only": profile["name"] == "graphql25",
        "production_project": profile["project"], "original_test_command": spec["test_command"],
        "effective_public_commands": public, "effective_private_commands": private,
        "sut_compiler_sdk": "6.0.400", "staging_runtime_sdk": "8.0.425",
        "original_root_source_changes": [], "assertion_changes": [], "golden_changes": [],
    })
    return recipe
