"""Fixed GraphQL3270 inputs and an assertion-free, full-project public build.

This profile adds no candidate implementation and no test-source rewrite. The
historical SDK declaration and the selected, attested toolchain remain distinct.
"""
from __future__ import annotations

from pathlib import Path
import re
import shlex

from reviewbench_maintainability.agentic.contracts import ContractError, file_hash
from reviewbench_maintainability.agentic.snapshots import tree_files

TASK_ID = "github__graphql-dotnet__graphql-dotnet__pull_3270__comment_939769493__duplication"
START_SHA = "530417e8f7cf497b758d6f22f0bb3883a8e84ab1"
TEST_ID = "public_auto_registering_graph_types"
HISTORICAL_IMAGE = "rb-csharp-sdk6.0.400-cov"
HISTORICAL_SDK = "6.0.400"
SIGNATURE = "P2P public_auto_registering_graph_types assertions=21 PASS"
PREPARE = (
    "dotnet restore oracle/Oracle.csproj --source /task/local-nuget "
    "--source https://api.nuget.org/v3/index.json --nologo -v minimal && "
    "dotnet build oracle/Oracle.csproj --no-restore --nologo -v minimal"
)
GOLDEN_SHA256 = "0e4e6141240223905c874aeb916d0dc7110b5c8a498bbdd14f9c486aa43a6ee7"
FROZEN_FILES = {
    "oracle/Oracle.csproj": "90ebe36a5b0e82649a129cd7340fcae00f878ff3452e14c23528be17abd3cfb5",
    "oracle/Program.cs": "de9f33600917a7363694da83e2a56916ee30b04d19a4a7350b3e2d57c7f9dc14",
    "source-only-golden.patch": GOLDEN_SHA256,
    **{f"{variant}-{n}.log": "8e36dbf0868ffbaf721ea8e6a1e988420cbdc54fb026155eb45eb69d48b48e39"
       for variant in ("start", "golden") for n in (1, 2, 3)},
}
PLAN = {
    "project": "oracle/Oracle.csproj", "kind": "console", "sdk": "rb-csharp-sdk8-cov",
    "production": ["src/GraphQL/GraphQL.csproj"],
    "fixed_input_profile": "graphql3270/v1", "public_project_build": True,
}

SDK6_OPTION = "csharp_graphql3270_sdk6"
SDK6_PROFILE = "graphql3270/sdk6.0.400-stager8/v1"


def requested_sdk6_profile(config, task, oracle):
    """An explicit task-only opt-in; other C# tasks keep their original route."""
    if task.get("id") != TASK_ID:
        return None
    migration = config.get("migration", {})
    if SDK6_OPTION not in migration:
        return None
    value = migration[SDK6_OPTION]
    if (not isinstance(value, dict)
            or set(value) != {"profile", "toolchain_receipt", "public_control", "input_audit"}
            or value["profile"] != SDK6_PROFILE):
        raise ContractError("GraphQL3270 SDK6 needs its explicit bounded profile and three evidence pins")
    if (task.get("repo") != "graphql-dotnet/graphql-dotnet" or task.get("start_sha") != START_SHA
            or str(oracle.get("index")) != "26"):
        raise ContractError("GraphQL3270 SDK6 task/start/index binding changed")
    return value


def bind_original_inputs(directory, task, oracle, spec):
    """Reject mismatched original bytes before creating any runtime asset."""
    if (task.get("id") != TASK_ID or task.get("repo") != "graphql-dotnet/graphql-dotnet"
            or task.get("start_sha") != START_SHA or int(oracle["index"]) != 26):
        raise ContractError("GraphQL3270 fixed task/start binding changed")
    if (spec.get("image") != HISTORICAL_IMAGE or spec.get("prepare_command") != PREPARE
            or spec.get("workdir") != "/task" or spec.get("classification") != "pass"
            or spec.get("host_adapter") or spec.get("adapter_patch") or spec.get("environment")):
        raise ContractError("GraphQL3270 fixed preparation/image/adapter declaration changed")
    if (spec.get("pass_to_pass_tests") != [TEST_ID]
            or oracle.get("pass_to_pass_tests") != [TEST_ID]
            or str(oracle.get("start_signature")) != "21"
            or str(oracle.get("golden_signature")) != "21"
            or spec.get("signature_regex") != r"P2P public_auto_registering_graph_types assertions=(\d+) PASS"):
        raise ContractError("GraphQL3270 original assertion signature/test ID changed")
    records = {}
    for name, expected in FROZEN_FILES.items():
        path = directory / name
        if path.is_symlink() or not path.is_file() or file_hash(path) != expected:
            raise ContractError("GraphQL3270 original input bytes changed: " + name)
        records[name] = {"path": str(path), "sha256": expected, "bytes": path.stat().st_size}
    names = {name for name in tree_files(directory / "oracle")
             if not any(part in {"bin", "obj"} for part in Path(name).parts)}
    if names != {"Oracle.csproj", "Program.cs"}:
        raise ContractError("GraphQL3270 original harness contains undeclared input files")
    return records


def toolchain_attestation(registry, reference, version):
    """Report the actual SDK/runtime without relabelling historical SDK6.0.400."""
    row = registry[reference]
    versions = row.get("versions", "")
    if not re.search(r"\.NET SDK:\s*\n\s*Version:\s*" + re.escape(version) + r"(?:\s|$)", versions):
        raise ContractError("GraphQL3270 actual SDK version evidence differs")
    runtimes = sorted(set(re.findall(r"^\s*Microsoft\.NETCore\.App\s+(\S+)\s+\[", versions, re.MULTILINE)))
    if not runtimes:
        raise ContractError("GraphQL3270 actual runtime version evidence is absent")
    return {
        "historical_image_declaration": HISTORICAL_IMAGE,
        "historical_sdk_declaration": HISTORICAL_SDK,
        "selected_registry_key": reference, "image": row["image"],
        "actual_sdk_version": version, "installed_netcore_runtimes": runtimes,
        "historical_sdk_microversion_equal": version == HISTORICAL_SDK,
        "harness_target_framework": "net6.0", "harness_target_framework_changed": False,
        "runtime_roll_forward": "Major",
        "compiler_version_check": "dotnet /usr/share/dotnet/sdk/" + version + "/Roslyn/bincore/csc.dll -version",
        "production_source_rewrites": [],
    }


def public_build_command(plan, version, *, tools, packages):
    """Build every framework of the current project, without private assertions."""
    if (plan.get("production") != ["src/GraphQL/GraphQL.csproj"]
            or plan.get("fixed_input_profile") != "graphql3270/v1"):
        raise ContractError("GraphQL3270 full public project binding changed")
    root = "/workspace/.reviewbench/csharp-public"
    project = root + "/repo/src/GraphQL/GraphQL.csproj"
    # This deletes only this command's generated staging tree, never /workspace
    # production. Repeated public checks rebuild the then-current candidate.
    commands = [
        "rm -rf " + root + " && mkdir -p " + root,
        "dotnet " + tools + "/CandidateStage.dll /workspace " + root + "/repo " +
        shlex.quote(version) + " " + shlex.quote(""),
        "mkdir -p /workspace/.reviewbench/nuget && cp -R " + packages +
        "/packages/. /workspace/.reviewbench/nuget/",
        "dotnet --info",
        "dotnet /usr/share/dotnet/sdk/" + shlex.quote(version) + "/Roslyn/bincore/csc.dll -version",
        "dotnet restore " + project + " --source " + packages +
        "/feed --nologo -v minimal --disable-parallel -p:NuGetAudit=false",
        "dotnet build " + project + " --no-restore --nologo -v minimal "
        "-p:NuGetAudit=false -m:1 -p:UseSharedCompilation=false -nr:false",
    ]
    return "set -euo pipefail; " + " && ".join(commands)
