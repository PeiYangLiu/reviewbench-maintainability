"""GraphQL3270-only SDK6 compiler with the retained SDK8 generic staging tool.

The old dependency receipt remains an SDK8 restore receipt. The opt-in adds the
separately hash-bound SDK6 public-use attestation; it never upgrades it to ready6.
"""
from __future__ import annotations

import copy
from dataclasses import replace
import json
from pathlib import Path
import shlex
import tempfile

from reviewbench_maintainability.agentic.contracts import (
    ContractError, atomic_json, canonical_hash, component, file_hash,
)
from .csharp_graphql3270 import (
    GOLDEN_SHA256, SDK6_PROFILE, TEST_ID, bind_original_inputs,
)

SDK6_IMAGE = "sha256:c878321ec43288c9f3cbcf5978315ba82cdde2df6f7ceca9c0a9348b8139d8ae"
SDK8_IMAGE = "sha256:458efe2c4a9d07585bbd4b0cbceed6a3f56a89896c0d30095d2de40808ce568b"
SDK6_VERSION = "6.0.400"
SDK8_VERSION = "8.0.425"
SDK6_ROOT = "/opt/reviewbench-dotnet6"
SDK8_ROOT = "/usr/share/dotnet"
SUT_HOST = SDK6_ROOT + "/dotnet"
STAGER_HOST = SDK8_ROOT + "/dotnet"
CSC = SDK6_ROOT + "/sdk/6.0.400/Roslyn/bincore/csc.dll"
CSC_VERSION = "4.3.0-3.22365.10 (dd078d4e)"
CSC_SHA256 = "6b5d7141a2f8593cc901d6df092cf77a9c669b61edd29ea6037d20b8a8a59418"
DEPENDENCIES_SHA256 = "3a3667dcf853aa7353164db6fd669c2c949a967b2ea9d7541ef61b9dcc459bc4"
PACKAGE_INDEX_SHA256 = "4b46058fe19e3466e11dd805b0a5c8a55bec20ea95b636dad1254ec537c73a90"
STAGER_ASSET_SHA256 = "c4bc8ca6459b59e4e3c3e29241d06ea29b8f7c0bcf08c5f0793986131c510249"
PACKAGES_ASSET_SHA256 = "d7ebd51e762f51c4afe493052f1fb6e71e27c64cef1b0bb42a44d0391de57a54"
SOURCE_LEDGER_SHA256 = "cabe09a2aa6613e94716f5684e8320ce88da24feaed7d5e37f4e36fb3a40ad71"
PINS = {
    "toolchain_receipt": "87a61542f6f4816178b642c79d16ed4b754711bb7314e8898ce031521f5d27cc",
    "public_control": "d364907747e061b872f6494576b24002ea5147e0f05a91b0b54690655e5e9b62",
    "input_audit": "466406f15547f78089e071910c261049faa5726f060073b5cea574babbd439e2",
}


def _read_json(reference, *, expected=None, exact_reference=False):
    try:
        if not isinstance(reference, dict) or not {"path", "sha256"} <= reference.keys():
            raise ContractError("SDK6 evidence needs a file/hash reference")
        if exact_reference and set(reference) != {"path", "sha256"}:
            raise ContractError("SDK6 evidence reference contains unsupported fields")
        path = Path(reference["path"])
        if (not path.is_absolute() or path.is_symlink() or not path.is_file()
                or path.stat().st_size > 8 * 1024**2
                or (expected is not None and reference["sha256"] != expected)
                or file_hash(path) != reference["sha256"]):
            raise ContractError("SDK6 evidence bytes/path differ from the reviewed pin")
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ContractError("SDK6 evidence must be a JSON object")
        return value
    except (OSError, TypeError, ValueError, KeyError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError("SDK6 evidence is unreadable or malformed") from exc


def validate_usage(options, dependency_path, dependencies):
    """Keep original preparation provenance separate from the new toolchain use."""
    if (not isinstance(options, dict)
            or set(options) != {"profile", *PINS}
            or options["profile"] != SDK6_PROFILE):
        raise ContractError("SDK6 usage requires the explicitly selected fixed profile")
    evidence = {name: _read_json(options[name], expected=pin, exact_reference=True)
                for name, pin in PINS.items()}
    tool, public, audit = (evidence[name] for name in ("toolchain_receipt", "public_control", "input_audit"))
    if (file_hash(dependency_path) != DEPENDENCIES_SHA256
            or dependencies.get("status") != "prepared" or dependencies.get("image") != SDK8_IMAGE
            or dependencies.get("sdk_version") != SDK8_VERSION
            or dependencies.get("production_source_rewrites") != []
            or len(dependencies.get("package_index", [])) != 20
            or canonical_hash(dependencies["package_index"]) != PACKAGE_INDEX_SHA256
            or dependencies["tool"]["sha256"] != STAGER_ASSET_SHA256
            or dependencies["packages"]["sha256"] != PACKAGES_ASSET_SHA256):
        raise ContractError("SDK6 use must retain the exact original SDK8 dependency/stager receipt")
    required_tool = {
        "status": "official_sdk_image_prepared_not_task_ready", "image": SDK6_IMAGE,
        "base_image": SDK8_IMAGE, "base_layers_match_prefix": True, "base_tag_id_guard_before_after": True,
        "sdk_version": SDK6_VERSION, "bundled_runtime_version": "6.0.8",
        "sut_dotnet": SUT_HOST, "stager_dotnet": STAGER_HOST, "stager_runtime": "8.0.31",
        "sut_compiler_sha256": CSC_SHA256, "stager_rebuilt": False,
        "production_changes": [], "warning_policy_changes": [], "original_net6_harness_changed": False,
    }
    if any(tool.get(key) != value for key, value in required_tool.items()):
        raise ContractError("SDK6 toolchain attestation differs from the reviewed dual-host image")
    if (public.get("status") != "public_control_passed_not_task_evaluation_complete"
            or public.get("public", {}).get("returncode") != 0 or public.get("public_attempts") != 1
            or public.get("source_unchanged") is not True or public.get("stager_rebuilt") is not False
            or public.get("original_assertion_count") != 21 or public.get("behavioral_assertions_executed") != 0
            or public.get("six_replay_calibrations") != 0
            or public.get("toolchain", {}).get("sha256") != options["toolchain_receipt"]["sha256"]
            or public.get("staged_non_build_changes") != ["global.json"]
            or public.get("staged_global_json") != sdk_metadata()):
        raise ContractError("SDK6 use lacks its exact source-preserving public-only control")
    if (audit.get("sdk6_public_exit") != 0 or audit.get("extra_source_changes") != []
            or audit.get("resolved_packages_outside_original_twenty") != []
            or audit.get("stager_source_ledger_sha256") != SOURCE_LEDGER_SHA256
            or set(audit.get("three_fresh_public_outputs", {})) != {"netstandard2.1", "netstandard2.0", "net6"}):
        raise ContractError("SDK6 input audit changes the full-project/dependency boundary")
    # Receipts are immutable inputs, not runtime mounts and not an admission proof.
    return {
        "profile": SDK6_PROFILE, "evidence": copy.deepcopy(options),
        "original_preparation": {"path": str(dependency_path), "sha256": file_hash(dependency_path),
                                 "image": SDK8_IMAGE, "sdk_version": SDK8_VERSION, "status": "prepared"},
        "effective_compiler": {"image": SDK6_IMAGE, "sdk": SDK6_VERSION, "host": SUT_HOST,
                               "csc": CSC, "csc_version": CSC_VERSION, "csc_sha256": CSC_SHA256,
                               "runtime": "6.0.8"},
        "retained_stager": {"host": STAGER_HOST, "sdk": SDK8_VERSION, "runtime": "8.0.31",
                            "asset": copy.deepcopy(dependencies["tool"]), "rebuilt": False},
        "dependency_receipt_relabelled": False, "six_replay_proof": False,
    }


def sdk_metadata():
    return {"sdk": {"version": SDK6_VERSION, "rollForward": "disable", "allowPrerelease": False}}


def stage_command(destination, tools):
    # A managed net8 application must always be launched by its retained net8 host.
    return ("env DOTNET_ROOT=" + SDK8_ROOT + " DOTNET_ROOT_X64=" + SDK8_ROOT
            + " DOTNET_HOST_PATH=" + STAGER_HOST + " DOTNET_MULTILEVEL_LOOKUP=0 "
            + STAGER_HOST + " " + tools + "/CandidateStage.dll /workspace "
            + destination + " " + SDK6_VERSION + " ''")


def sdk_checks(cwd):
    """Pin selection at the process cwd and assert the actual compiler bytes/version."""
    return (
        "cd " + cwd + " && "
        "selected_sdk=$(" + SUT_HOST + " --version) && "
        "printf 'CSHARP_SDK6_CWD=%s\\nCSHARP_SDK6_SELECTED=%s\\n' \"$PWD\" \"$selected_sdk\" && "
        "test \"$selected_sdk\" = " + SDK6_VERSION + " && " + SUT_HOST + " --info && "
        "selected_csc=$(" + SUT_HOST + " " + CSC + " -version) && "
        "printf 'CSHARP_SDK6_CSC=%s\\n' \"$selected_csc\" && "
        "test \"$selected_csc\" = " + shlex.quote(CSC_VERSION) + " && "
        "printf '%s\\n' " + shlex.quote(CSC_SHA256 + "  " + CSC) + " | sha256sum -c -"
    )


def _sut(command):
    if not command.startswith("dotnet "):
        raise ContractError("SDK6 command lacks the original literal dotnet entry")
    return SUT_HOST + command[len("dotnet"):]


def public_command(tools, packages):
    root = "/workspace/.reviewbench/csharp-public"
    project = root + "/repo/src/GraphQL/GraphQL.csproj"
    return "set -euo pipefail; " + " && ".join([
        "rm -rf " + root + " && mkdir -p " + root,
        stage_command(root + "/repo", tools),
        "mkdir -p /workspace/.reviewbench/nuget && cp -R " + packages + "/packages/. /workspace/.reviewbench/nuget/",
        sdk_checks(root + "/repo"),
        SUT_HOST + " restore " + project + " --source " + packages
        + "/feed --nologo -v minimal --disable-parallel -p:NuGetAudit=false",
        SUT_HOST + " build " + project + " --no-restore --nologo -v minimal "
        "-p:NuGetAudit=false -m:1 -p:UseSharedCompilation=false -nr:false",
    ])


def migrate(ctx, task, oracle, spec, directory, options, *, build_sdk8):
    from .csharp_candidate import (
        PACKAGES, PRIVATE, RUNTIME, TOOLS, build_commands, curate_harness, dependency_manifest, plan_for,
    )
    fixed = bind_original_inputs(directory, task, oracle, spec)
    plan = plan_for(oracle["index"], spec)
    dependency_path, dependencies = dependency_manifest(ctx, task)
    usage = validate_usage(options, dependency_path, dependencies)
    before_receipt = file_hash(dependency_path)
    base = build_sdk8(ctx, task, oracle, spec, directory)
    if (base.image != SDK8_IMAGE or base.grading["golden_patch"]["sha256"] != GOLDEN_SHA256
            or base.grading["commands"][0]["expected_tests"] != [TEST_ID]
            or len(base.grading["commands"]) != 1
            or not base.grading["commands"][0]["command"].endswith(spec["test_command"])):
        raise ContractError("SDK6 base recipe is not the original GraphQL3270 contract")
    image, image_audit = ctx.image(SDK6_IMAGE)
    if image != SDK6_IMAGE:
        raise ContractError("SDK6 image resolution differs from the exact reviewed image")
    with tempfile.TemporaryDirectory(prefix="graphql3270-sdk6-harness-", dir=ctx.scratch) as temporary:
        curated = Path(temporary)
        hashes = curate_harness(directory, curated, task, spec, plan, repository=ctx.repo(task))
        atomic_json(curated / "global.json", sdk_metadata())
        harness = ctx.bundle(curated, role="grader")
    assets = [({**harness, "kind": "archive", "visibility": "grader", "target": PRIVATE}
               if asset["target"] == PRIVATE else copy.deepcopy(asset)) for asset in base.assets]
    restore, build = build_commands(spec, plan)
    grading = copy.deepcopy(base.grading)
    grading["bootstrap"] = [
        "mkdir -p " + RUNTIME + " && cp -R " + PRIVATE + "/. " + RUNTIME + " && "
        + stage_command(RUNTIME + "/repo", TOOLS),
        "mkdir -p /workspace/.reviewbench/nuget && cp -R " + PACKAGES + "/packages/. /workspace/.reviewbench/nuget/",
        sdk_checks(RUNTIME) + " && " + _sut(restore) + " --source " + PACKAGES + "/feed && " + _sut(build),
    ]
    grading["commands"][0]["command"] = "cd " + RUNTIME + " && " + _sut(spec["test_command"])
    result = replace(base, image=image, image_audit=image_audit, assets=assets,
        env={**base.env, "DOTNET_ROOT": SDK6_ROOT, "DOTNET_ROOT_X64": SDK6_ROOT,
             "DOTNET_HOST_PATH": SUT_HOST, "DOTNET_MULTILEVEL_LOOKUP": "0"},
        dev_checks=[public_command(TOOLS, PACKAGES)], grading=grading)
    result.validate()
    if file_hash(dependency_path) != before_receipt:
        raise ContractError("SDK6 use changed the original SDK8 preparation receipt")
    audit_path = ctx.output / "csharp-plans" / (component(task["id"]) + ".json")
    audit = json.loads(audit_path.read_text())
    audit.update({
        "sdk_version": SDK6_VERSION, "runtime_fingerprint": result.fingerprint,
        "sdk8_baseline_runtime_fingerprint": base.fingerprint, "sdk6_usage_binding": usage,
        "harness_files": hashes, "original_input_bindings": fixed,
        "toolchain_attestation": {
            "historical_sdk_declaration": "6.0.400", "actual_sdk_version": SDK6_VERSION,
            "historical_sdk_microversion_equal": True, "historical_entire_image_equivalent": False,
            "image": SDK6_IMAGE, "sut_host": SUT_HOST, "sut_runtime": "6.0.8",
            "stager_host": STAGER_HOST, "stager_runtime": "8.0.31", "csc_version": CSC_VERSION,
            "harness_target_framework": "net6.0", "harness_target_framework_changed": False,
            "public_cwd": "/workspace/.reviewbench/csharp-public/repo", "private_cwd": RUNTIME,
        },
        "original_test_command": spec["test_command"], "production_source_rewrites": [],
        "readiness": "requires new actual public/start3/golden3 verification; public-only evidence is not ready6",
    })
    atomic_json(audit_path, audit)
    return result
