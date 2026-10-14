"""Small source/contract fixtures; these are not the benchmark's 21 assertions."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import csharp_candidate as candidate
from agentic_runtime_migration import csharp_graphql3270 as profile
from agentic_runtime_migration import csharp_lane
from agentic_runtime_migration.csharp_dependencies import (
    reuse_tool, validate_preparation_cohort,
)
from agentic_runtime_migration.csharp_staging import PROJECT, SOURCE
from reviewbench_maintainability.agentic.contracts import ContractError, component, file_hash


def inputs():
    task = {"id": profile.TASK_ID, "repo": "graphql-dotnet/graphql-dotnet",
            "start_sha": profile.START_SHA,
            "locality": {"path": "src/GraphQL/Types/Composite/AutoRegisteringInterfaceGraphType.cs"}}
    oracle = {"index": 26, "pass_to_pass_tests": [profile.TEST_ID],
              "start_signature": "21", "golden_signature": "21"}
    spec = {"classification": "pass", "image": profile.HISTORICAL_IMAGE, "workdir": "/task",
            "prepare_command": profile.PREPARE, "pass_to_pass_tests": [profile.TEST_ID],
            "signature_regex": r"P2P public_auto_registering_graph_types assertions=(\d+) PASS",
            "test_command": "dotnet run --project oracle/Oracle.csproj --no-restore --no-build --nologo"}
    return task, oracle, spec


def registry():
    return {"rb-csharp-sdk8-cov": {
        "status": "ready", "image": "sha256:" + "a" * 64,
        "versions": ".NET SDK:\n Version: 8.0.425\n"
                    ".NET runtimes installed:\n Microsoft.NETCore.App 8.0.31 [/usr/share/dotnet/shared]\n",
    }}


@pytest.fixture
def fixed(tmp_path, monkeypatch):
    """Hash-bound synthetic inputs exercise validation, not reference behavior."""
    directory = tmp_path / "legacy/task"
    contents = {
        "oracle/Oracle.csproj": (
            '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType>'
            '<TargetFramework>net6.0</TargetFramework></PropertyGroup><ItemGroup>'
            '<ProjectReference Include="../repo/src/GraphQL/GraphQL.csproj" /></ItemGroup></Project>\n'
        ),
        "oracle/Program.cs": "/* synthetic validator fixture; not the original benchmark */\n",
        "source-only-golden.patch": "synthetic source-only patch\n",
        **{f"{v}-{n}.log": profile.SIGNATURE + "\n" for v in ("start", "golden") for n in (1, 2, 3)},
    }
    for name, text in contents.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    monkeypatch.setattr(profile, "FROZEN_FILES", {
        name: hashlib.sha256(text.encode()).hexdigest() for name, text in contents.items()
    })
    return directory


def test_index26_is_separate_from_the_original_eight():
    assert candidate.LEGACY_PROJECT_INDICES == {16, 18, 19, 20, 21, 24, 25, 28}
    assert set(candidate.PLANS) == candidate.LEGACY_PROJECT_INDICES | {26}
    assert candidate.PLANS[25] == {
        "project": "oracle/Oracle.csproj", "kind": "console", "sdk": "rb-csharp-sdk8-cov",
        "production": ["src/GraphQL/GraphQL.csproj"],
    }
    assert "service" not in candidate.PLANS[26]
    assert "private_setup" not in candidate.PLANS[26]
    assert candidate.plan_for(26, inputs()[2])["production"] == ["src/GraphQL/GraphQL.csproj"]


def test_original_profile_has_nine_explicit_independent_hashes():
    assert len(profile.FROZEN_FILES) == 9
    assert profile.FROZEN_FILES["oracle/Program.cs"] == (
        "de9f33600917a7363694da83e2a56916ee30b04d19a4a7350b3e2d57c7f9dc14"
    )
    assert profile.FROZEN_FILES["source-only-golden.patch"] == profile.GOLDEN_SHA256


def test_accepts_only_the_bound_inputs_without_editing_them(fixed):
    before = {p: file_hash(p) for p in fixed.rglob("*") if p.is_file()}
    records = profile.bind_original_inputs(fixed, *inputs())
    assert len(records) == 9
    assert before == {p: file_hash(p) for p in before}


@pytest.mark.parametrize("name", [
    "oracle/Program.cs", "oracle/Oracle.csproj", "source-only-golden.patch",
    "start-1.log", "golden-3.log",
])
def test_changed_original_input_is_rejected(fixed, name):
    (fixed / name).write_text("different")
    with pytest.raises(ContractError, match="original input bytes changed"):
        profile.bind_original_inputs(fixed, *inputs())


@pytest.mark.parametrize("field,value", [
    ("id", "another-task"), ("repo", "other/repository"), ("start_sha", "1" * 40),
])
def test_no_task_or_start_alias(fixed, field, value):
    task, oracle, spec = inputs()
    task[field] = value
    with pytest.raises(ContractError, match="task/start"):
        profile.bind_original_inputs(fixed, task, oracle, spec)


@pytest.mark.parametrize("field,value", [
    ("image", "rb-csharp-sdk8-cov"), ("prepare_command", "dotnet build easier.csproj"),
    ("host_adapter", "historical-source-rewrite.sh"), ("adapter_patch", "rewrite.patch"),
    ("environment", {"CUSTOM": "changed"}), ("workdir", "/task/repo"),
])
def test_no_historical_rewrite_or_relabelled_spec(fixed, field, value):
    task, oracle, spec = inputs()
    spec[field] = value
    with pytest.raises(ContractError, match="preparation/image/adapter"):
        profile.bind_original_inputs(fixed, task, oracle, spec)


@pytest.mark.parametrize("change", ["selector", "start_count", "golden_count", "regex"])
def test_assertion_count_and_test_id_cannot_shrink(fixed, change):
    task, oracle, spec = inputs()
    if change == "selector":
        spec["pass_to_pass_tests"] = ["easier"]
    elif change == "regex":
        spec["signature_regex"] = r"PASS"
    else:
        oracle["start_signature" if change == "start_count" else "golden_signature"] = "20"
    with pytest.raises(ContractError, match="assertion signature/test ID"):
        profile.bind_original_inputs(fixed, task, oracle, spec)


def test_extra_harness_metadata_is_not_silently_admitted(fixed):
    (fixed / "oracle/Directory.Build.targets").write_text("<Project />")
    with pytest.raises(ContractError, match="undeclared"):
        profile.bind_original_inputs(fixed, *inputs())


def test_original_harness_file_must_not_be_a_link(fixed):
    file = fixed / "oracle/Program.cs"
    contents = file.read_bytes()
    file.unlink()
    (fixed / "elsewhere").write_bytes(contents)
    file.symlink_to("../elsewhere")
    with pytest.raises(ContractError, match="original input bytes"):
        profile.bind_original_inputs(fixed, *inputs())


def test_attests_actual_sdk_without_claiming_legacy_equality():
    proof = profile.toolchain_attestation(registry(), "rb-csharp-sdk8-cov", "8.0.425")
    assert proof["historical_sdk_declaration"] == "6.0.400"
    assert proof["actual_sdk_version"] == "8.0.425"
    assert proof["installed_netcore_runtimes"] == ["8.0.31"]
    assert proof["historical_sdk_microversion_equal"] is False
    assert proof["harness_target_framework"] == "net6.0"
    assert proof["harness_target_framework_changed"] is False


def test_missing_or_mismatched_version_receipt_fails():
    with pytest.raises(ContractError, match="SDK version"):
        profile.toolchain_attestation(registry(), "rb-csharp-sdk8-cov", "6.0.400")
    rows = registry()
    rows["rb-csharp-sdk8-cov"]["versions"] = ".NET SDK:\n Version: 8.0.425\n"
    with pytest.raises(ContractError, match="runtime version"):
        profile.toolchain_attestation(rows, "rb-csharp-sdk8-cov", "8.0.425")


def test_public_check_builds_all_current_production_frameworks_without_assertions():
    text = profile.public_build_command(profile.PLAN, "8.0.425",
                                        tools=candidate.TOOLS, packages=candidate.PACKAGES)
    assert "CandidateStage.dll /workspace " in text
    assert "dotnet build /workspace/.reviewbench/csharp-public/repo/src/GraphQL/GraphQL.csproj" in text
    assert "csc.dll -version" in text and "dotnet --info" in text
    for forbidden in ("oracle", "Program.cs", "CSharpSyntax.dll", "golden", "--framework",
                      "TargetFramework=", "TargetFrameworks=", "NoWarn", "RunAnalyzers=false",
                      "BuildProjectReferences=false", "CustomAfterMicrosoftCommonTargets="):
        assert forbidden not in text
    assert "rm -rf /workspace/.reviewbench/csharp-public &&" in text
    assert text.count("--no-restore") == 1


def test_public_project_cannot_be_replaced_with_a_slice():
    plan = deepcopy(profile.PLAN)
    plan["production"] = ["one-file.csproj"]
    with pytest.raises(ContractError, match="full public project"):
        profile.public_build_command(plan, "8.0.425", tools="/tools", packages="/packages")


def test_whole_original_signature_remains_mandatory(fixed):
    task, oracle, spec = inputs()
    expression = candidate.project_completion_pattern(fixed, spec, oracle, profile.PLAN)
    assert re.search(expression, profile.SIGNATURE + "\n", re.MULTILINE)
    assert not re.search(expression, profile.SIGNATURE.replace("21", "20"), re.MULTILINE)
    assert not re.search(expression, "P2P other assertions=21 PASS", re.MULTILINE)


def test_preparation_cohort_keeps_8_and_1_separate():
    indices = sorted(candidate.LEGACY_PROJECT_INDICES)
    tasks = {str(i): {} for i in [*indices, 26]}
    oracles = {str(i): {"index": i} for i in [*indices, 26]}
    validate_preparation_cohort(list(map(str, indices)), tasks, oracles)
    validate_preparation_cohort(["26"], tasks, oracles)
    for ids in ([], ["25"], ["26", "26"], ["missing"], [*map(str, indices), "26"]):
        with pytest.raises(ContractError):
            validate_preparation_cohort(ids, tasks, oracles)


def test_dependency_script_entry_reaches_gate_without_package_context(fixed, tmp_path):
    import runpy
    task, oracle, spec = inputs()
    (fixed.parent / "csharp").mkdir()
    (fixed.parent / "csharp/specs.json").write_text(json.dumps({"26": spec}))
    script = Path(candidate.__file__).with_name("csharp_dependencies.py")
    module = runpy.run_path(str(script))
    assert not module["__package__"]

    class ReachedOriginalGate(Exception):
        pass

    class Gate:
        def acquire(self, request, *, uses_containers, timeout):
            assert request.heavy is True
            assert uses_containers is True and timeout == 14400
            raise ReachedOriginalGate

    output = tmp_path / "preparation"
    output.mkdir()
    ctx = SimpleNamespace(
        legacy=fixed.parent, output=output,
        config={"execution": {"admission_timeout_seconds": 14400}},
        task_directory=lambda *args: fixed,
    )
    with pytest.raises(ReachedOriginalGate):
        module["prepare_one"](
            ctx, SimpleNamespace(gate=Gate()), task, oracle, {"asset": {}},
            registry(), output / "task",
        )


@pytest.fixture
def tool(tmp_path):
    root = tmp_path / "tool"
    output = root / "output"
    output.mkdir(parents=True)
    (root / "Program.cs").write_text(SOURCE)
    (root / "CandidateStage.csproj").write_text(PROJECT)
    (output / "CandidateStage.dll").write_bytes(b"synthetic generic tool for receipt validation")
    descriptor = root / "asset.json"
    descriptor.write_text(json.dumps({"source_kind": "dependency"}))
    record = {
        "output": str(output), "image": registry()["rb-csharp-sdk8-cov"]["image"],
        "sdk_version": "8.0.425", "build": {"exit_status": 0},
        "output_hashes": {"CandidateStage.dll": file_hash(output / "CandidateStage.dll")},
        "asset": {"path": str(descriptor), "sha256": file_hash(descriptor)},
    }
    path = root / "tool.json"
    path.write_text(json.dumps(record))
    return path, record


def test_reuses_exact_source_built_tool_receipt_without_a_build(tool):
    path, record = tool
    assert reuse_tool(path, registry()) == record


@pytest.mark.parametrize("change", ["bytes", "extra_output", "source", "descriptor", "image", "exit"])
def test_reuse_rejects_changed_or_unbound_tool_outputs(tool, change):
    path, record = tool
    root = path.parent
    if change == "bytes":
        (root / "output/CandidateStage.dll").write_bytes(b"changed")
    elif change == "extra_output":
        (root / "output/OldGraphQL.dll").write_bytes(b"old candidate")
    elif change == "source":
        (root / "Program.cs").write_text("changed")
    elif change == "descriptor":
        (root / "asset.json").write_text("{}")
    else:
        if change == "image":
            record["image"] = "sha256:" + "b" * 64
        else:
            record["build"]["exit_status"] = 1
        path.write_text(json.dumps(record))
    with pytest.raises(ContractError):
        reuse_tool(path, registry())


def test_migrate_routes_index26_to_complete_current_project(fixed, tmp_path):
    task, oracle, spec = inputs()
    (fixed.parent / "csharp").mkdir()
    (fixed.parent / "csharp/specs.json").write_text(json.dumps({"26": spec}))
    registry_path = tmp_path / "toolchains.json"
    registry_path.write_text(json.dumps(registry()))
    deps = tmp_path / "dependencies" / component(task["id"])
    deps.mkdir(parents=True)
    descriptor = tmp_path / "asset.json"
    descriptor.write_text("{}")
    asset = {"path": str(descriptor), "sha256": file_hash(descriptor)}
    (deps / "dependencies.json").write_text(json.dumps({
        "task_id": task["id"], "start_sha": task["start_sha"], "policy": candidate.POLICY,
        "image": registry()["rb-csharp-sdk8-cov"]["image"], "sdk_version": "8.0.425",
        "tool": asset, "packages": asset, "status": "prepared",
    }))
    captured = {}

    def bundle(root, **kwargs):
        captured["program"] = (root / "oracle/Program.cs").read_bytes()
        captured["project"] = (root / "oracle/Oracle.csproj").read_bytes()
        assert not (root / "repo").exists()
        return asset

    ctx = SimpleNamespace(
        legacy=fixed.parent, output=tmp_path / "output", scratch=tmp_path,
        config={"migration": {"toolchain_registry": str(registry_path),
                              "csharp_dependency_root": str(deps.parent)}},
        task_directory=lambda *args: fixed, repo=lambda *args: tmp_path / "unused-repository",
        image=lambda *args: (registry()["rb-csharp-sdk8-cov"]["image"], asset),
        bundle=bundle, modules=lambda *args: [], immutable_tests=lambda *args: ["fixed/test.cs"],
        golden=lambda *args: {"path": str(fixed / "source-only-golden.patch"),
                              "sha256": file_hash(fixed / "source-only-golden.patch")},
    )
    recipe = csharp_lane.migrate(ctx, task, oracle)
    assert captured["program"] == (fixed / "oracle/Program.cs").read_bytes()
    assert captured["project"] == (fixed / "oracle/Oracle.csproj").read_bytes()
    assert len(recipe.dev_checks) == 1 and "dotnet build " in recipe.dev_checks[0]
    assert recipe.grading["commands"][0]["expected_tests"] == [profile.TEST_ID]
    assert recipe.grading["commands"][0]["command"].endswith(spec["test_command"])
    assert recipe.grading["multifile_build"] is True
    assert not recipe.sidecars
    assert recipe.grading["immutable_paths"] == ["fixed/test.cs"]
    audit = json.loads((ctx.output / "csharp-plans" / (component(task["id"]) + ".json")).read_text())
    assert len(audit["original_input_bindings"]) == 9
    assert audit["toolchain_attestation"]["historical_sdk_microversion_equal"] is False
