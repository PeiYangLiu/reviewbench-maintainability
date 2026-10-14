"""Source/receipt fixtures only: never execute GraphQL or a real SDK build."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import csharp_candidate as candidate
from agentic_runtime_migration import csharp_graphql3270 as original
from agentic_runtime_migration import csharp_graphql3270_sdk6 as sdk6
from agentic_runtime_migration import csharp_lane
from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash, component, file_hash
from test_agentic_csharp_graphql3270 import fixed, inputs


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True))
    return {"path": str(path), "sha256": file_hash(path)}


@pytest.fixture
def configured(fixed, tmp_path, monkeypatch):
    task, oracle, spec = inputs()
    lane = fixed.parent / "csharp"
    lane.mkdir()
    write_json(lane / "specs.json", {"26": spec})
    registry = tmp_path / "registry.json"
    write_json(registry, {"rb-csharp-sdk8-cov": {
        "status": "ready", "image": sdk6.SDK8_IMAGE,
        "versions": ".NET SDK:\n Version: 8.0.425\n Microsoft.NETCore.App 8.0.31 [/usr/share/dotnet/shared]\n",
    }})
    tool = write_json(tmp_path / "tool.json", {"source_kind": "dependency", "fixture": "generic-stager"})
    packages = write_json(tmp_path / "packages.json", {"source_kind": "dependency", "fixture": "twenty-packages"})
    monkeypatch.setattr(sdk6, "STAGER_ASSET_SHA256", tool["sha256"])
    monkeypatch.setattr(sdk6, "PACKAGES_ASSET_SHA256", packages["sha256"])
    monkeypatch.setattr(sdk6, "GOLDEN_SHA256", file_hash(fixed / "source-only-golden.patch"))
    index = [{"package": f"fixture-{n}/1.0.0", "sha256": str(n) * 64} for n in range(20)]
    monkeypatch.setattr(sdk6, "PACKAGE_INDEX_SHA256", canonical_hash(index))
    dependency_path = tmp_path / "dependencies" / component(task["id"]) / "dependencies.json"
    dependencies = {
        "task_id": task["id"], "start_sha": task["start_sha"], "policy": candidate.POLICY,
        "image": sdk6.SDK8_IMAGE, "sdk_version": sdk6.SDK8_VERSION, "status": "prepared",
        "production_source_rewrites": [], "package_index": index, "tool": tool, "packages": packages,
    }
    write_json(dependency_path, dependencies)
    monkeypatch.setattr(sdk6, "DEPENDENCIES_SHA256", file_hash(dependency_path))
    toolchain = {
        "status": "official_sdk_image_prepared_not_task_ready", "image": sdk6.SDK6_IMAGE,
        "base_image": sdk6.SDK8_IMAGE, "base_layers_match_prefix": True, "base_tag_id_guard_before_after": True,
        "sdk_version": sdk6.SDK6_VERSION, "bundled_runtime_version": "6.0.8",
        "sut_dotnet": sdk6.SUT_HOST, "stager_dotnet": sdk6.STAGER_HOST, "stager_runtime": "8.0.31",
        "sut_compiler_sha256": sdk6.CSC_SHA256, "stager_rebuilt": False,
        "production_changes": [], "warning_policy_changes": [], "original_net6_harness_changed": False,
    }
    tool_ref = write_json(tmp_path / "evidence/toolchain.json", toolchain)
    public = {
        "status": "public_control_passed_not_task_evaluation_complete", "public": {"returncode": 0},
        "public_attempts": 1, "source_unchanged": True, "stager_rebuilt": False,
        "original_assertion_count": 21, "behavioral_assertions_executed": 0, "six_replay_calibrations": 0,
        "toolchain": tool_ref, "staged_non_build_changes": ["global.json"], "staged_global_json": sdk6.sdk_metadata(),
    }
    public_ref = write_json(tmp_path / "evidence/public.json", public)
    audit = {
        "sdk6_public_exit": 0, "extra_source_changes": [], "resolved_packages_outside_original_twenty": [],
        "stager_source_ledger_sha256": sdk6.SOURCE_LEDGER_SHA256,
        "three_fresh_public_outputs": {name: {} for name in ("netstandard2.1", "netstandard2.0", "net6")},
    }
    audit_ref = write_json(tmp_path / "evidence/audit.json", audit)
    options = {"profile": original.SDK6_PROFILE, "toolchain_receipt": tool_ref,
               "public_control": public_ref, "input_audit": audit_ref}
    monkeypatch.setattr(sdk6, "PINS", {key: options[key]["sha256"] for key in sdk6.PINS})
    captured = {"images": [], "bundles": []}

    def bundle(root, **kwargs):
        assert kwargs == {"role": "grader"}
        contents = {str(p.relative_to(root)): p.read_text() for p in root.rglob("*") if p.is_file()}
        assert "repo" not in {p.name for p in root.iterdir()}
        captured["bundles"].append(contents)
        # This is a descriptor fixture, not a runnable/prepared asset.
        return write_json(tmp_path / "fixture-assets" / (canonical_hash(contents) + ".json"),
                          {"fixture_only": True, "contents": contents})

    def image(reference):
        captured["images"].append(reference)
        ident = sdk6.SDK8_IMAGE if reference == "rb-csharp-sdk8-cov" else reference
        return ident, write_json(tmp_path / "image-audits" / (ident.split(":")[1] + ".json"),
                                {"fixture_only": True, "image": ident})

    ctx = SimpleNamespace(
        legacy=fixed.parent, output=tmp_path / "output", scratch=tmp_path,
        config={"migration": {"toolchain_registry": str(registry),
                              "csharp_dependency_root": str(dependency_path.parent.parent),
                              original.SDK6_OPTION: options}},
        task_directory=lambda *args: fixed, repo=lambda *args: tmp_path / "unused-repo",
        image=image, bundle=bundle, modules=lambda *args: [], immutable_tests=lambda *args: ["fixed/test.cs"],
        golden=lambda *args: {"path": str(fixed / "source-only-golden.patch"),
                             "sha256": file_hash(fixed / "source-only-golden.patch")},
    )
    return SimpleNamespace(ctx=ctx, task=task, oracle=oracle, spec=spec, directory=fixed,
                           dependency_path=dependency_path, dependencies=dependencies,
                           options=options, captured=captured, tmp=tmp_path)


def migrate(f):
    return csharp_lane.migrate(f.ctx, f.task, f.oracle)


def test_opt_in_emits_sdk6_and_separate_sdk8_stager_with_original_harness(configured):
    f = configured
    before = f.dependency_path.read_bytes()
    recipe = migrate(f)
    assert recipe.image == sdk6.SDK6_IMAGE
    assert recipe.cpus == 4 and recipe.memory_mb == 8192 and recipe.build_scratch_mb == 12288
    assert recipe.command_timeout_seconds == 2400
    assert recipe.env["DOTNET_ROOT"] == sdk6.SDK6_ROOT
    assert recipe.env["DOTNET_HOST_PATH"] == sdk6.SUT_HOST
    assert recipe.env["DOTNET_MULTILEVEL_LOOKUP"] == "0"
    assert recipe.env["DOTNET_ROLL_FORWARD"] == "Major"
    assert f.dependency_path.read_bytes() == before
    first, second = f.captured["bundles"]
    for name in ("oracle/Oracle.csproj", "oracle/Program.cs"):
        assert second[name] == first[name] == (f.directory / name).read_text()
    assert "net6.0" in second["oracle/Oracle.csproj"]
    assert json.loads(first["global.json"])["sdk"]["version"] == sdk6.SDK8_VERSION
    assert json.loads(second["global.json"]) == sdk6.sdk_metadata()
    assert f.captured["images"] == ["rb-csharp-sdk8-cov", sdk6.SDK6_IMAGE]
    developer_assets = [a for a in recipe.assets if a["visibility"] == "developer"]
    assert {a["sha256"] for a in developer_assets} == {
        f.dependencies["tool"]["sha256"], f.dependencies["packages"]["sha256"]}
    assert len([a for a in recipe.assets if a["visibility"] == "grader"]) == 1


def test_sdk6_keeps_original_test_arguments_ids_signature_and_all_public_frameworks(configured):
    f = configured
    recipe = migrate(f)
    public = recipe.dev_checks[0]
    private = recipe.grading["bootstrap"]
    assert sdk6.STAGER_HOST + " " + candidate.TOOLS + "/CandidateStage.dll" in public
    assert "DOTNET_ROOT=/usr/share/dotnet" in public
    assert sdk6.sdk_checks("/workspace/.reviewbench/csharp-public/repo") in public
    assert sdk6.SUT_HOST + " build /workspace/.reviewbench/csharp-public/repo/src/GraphQL/GraphQL.csproj" in public
    assert sdk6.sdk_checks(candidate.RUNTIME) in private[-1]
    assert sdk6.STAGER_HOST in private[0] and "6.0.400 ''" in private[0]
    original_tail = f.spec["test_command"][len("dotnet"):]
    command = recipe.grading["commands"][0]
    assert command["command"] == "cd " + candidate.RUNTIME + " && " + sdk6.SUT_HOST + original_tail
    assert command["expected_tests"] == [original.TEST_ID]
    assert recipe.grading["multifile_build"] is True
    assert recipe.grading["immutable_paths"] == ["fixed/test.cs"]
    assert command["success_pattern"] == candidate.project_completion_pattern(f.directory, f.spec, f.oracle, original.PLAN)
    for forbidden in ("oracle", "Program.cs", "golden", "--framework", "TargetFramework=", "TargetFrameworks=",
                      "NoWarn", "TreatWarningsAsErrors=false", "Nullable=disable", "RunAnalyzers=false",
                      "BuildProjectReferences=false", "CSharpSyntax.dll"):
        assert forbidden not in public
    assert "--no-restore --nologo -v minimal -p:NuGetAudit=false -m:1 -p:UseSharedCompilation=false -nr:false" in public
    assert not recipe.cache_mounts and not recipe.sidecars


def test_usage_record_does_not_relabel_sdk8_restore_or_claim_ready6(configured):
    f = configured
    recipe = migrate(f)
    record = json.loads((f.ctx.output / "csharp-plans" / (component(f.task["id"]) + ".json")).read_text())
    use = record["sdk6_usage_binding"]
    assert use["original_preparation"]["sdk_version"] == "8.0.425"
    assert use["original_preparation"]["image"] == sdk6.SDK8_IMAGE
    assert use["original_preparation"]["sha256"] == file_hash(f.dependency_path)
    assert use["effective_compiler"]["sdk"] == "6.0.400"
    assert use["dependency_receipt_relabelled"] is False and use["six_replay_proof"] is False
    assert record["toolchain_attestation"]["historical_entire_image_equivalent"] is False
    assert record["runtime_fingerprint"] == recipe.fingerprint
    assert record["original_test_command"] == f.spec["test_command"]
    assert "requires new actual" in record["readiness"]


def test_absent_option_is_exact_sdk8_recipe(configured):
    f = configured
    f.ctx.config["migration"].pop(original.SDK6_OPTION)
    dispatched = migrate(f)
    direct = candidate._migrate_project_sdk8(f.ctx, f.task, f.oracle, f.spec, f.directory)
    assert dispatched == direct and dispatched.image == sdk6.SDK8_IMAGE
    assert "DOTNET_ROOT" not in dispatched.env
    assert all(image == "rb-csharp-sdk8-cov" for image in f.captured["images"])


def test_other_csharp_task_ignores_this_task_only_option(configured):
    f = configured
    task = {**f.task, "id": "other-csharp-task"}
    oracle = {**f.oracle, "index": 25}
    other = f.dependency_path.parent.parent / component(task["id"]) / "dependencies.json"
    write_json(other, {**f.dependencies, "task_id": task["id"]})
    with_option = candidate.migrate_project(f.ctx, task, oracle, f.spec, f.directory)
    f.ctx.config["migration"].pop(original.SDK6_OPTION)
    without = candidate.migrate_project(f.ctx, task, oracle, f.spec, f.directory)
    assert with_option == without and without.image == sdk6.SDK8_IMAGE
    assert sdk6.SDK6_IMAGE not in f.captured["images"]


@pytest.mark.parametrize("value", [None, False, True, "", "sdk6", {}, {"profile": "unknown"}])
def test_no_implicit_or_unknown_profile(configured, value):
    f = configured
    f.ctx.config["migration"][original.SDK6_OPTION] = value
    with pytest.raises(ContractError, match="explicit bounded"):
        migrate(f)
    assert not f.captured["images"] and not f.captured["bundles"]


@pytest.mark.parametrize("field,value", [("host", "/different/dotnet"), ("image", "latest"),
                                       ("flags", "-p:NoWarn=CS8600"), ("selector", "golden")])
def test_no_arbitrary_profile_hosts_flags_or_selectors(configured, field, value):
    f = configured
    f.options[field] = value
    with pytest.raises(ContractError, match="explicit bounded"):
        migrate(f)
    assert not f.captured["images"]


@pytest.mark.parametrize("field,value", [("repo", "other/repo"), ("start_sha", "f" * 40)])
def test_target_start_binding_is_not_an_alias(configured, field, value):
    f = configured
    f.task[field] = value
    with pytest.raises(ContractError, match="task/start"):
        migrate(f)
    assert not f.captured["images"]


@pytest.mark.parametrize("name", ["toolchain_receipt", "public_control", "input_audit"])
@pytest.mark.parametrize("mutation", ["hash", "bytes", "relative", "link", "extra_field"])
def test_evidence_pins_fail_before_recipe_generation(configured, name, mutation):
    f = configured
    reference = f.options[name]
    p = Path(reference["path"])
    if mutation == "hash":
        reference["sha256"] = "f" * 64
    elif mutation == "bytes":
        p.write_text("{}")
    elif mutation == "relative":
        reference["path"] = p.name
    elif mutation == "link":
        data = p.read_bytes()
        p.unlink()
        elsewhere = p.with_suffix(".target")
        elsewhere.write_bytes(data)
        p.symlink_to(elsewhere)
    else:
        reference["arbitrary"] = True
    with pytest.raises(ContractError):
        migrate(f)
    assert not f.captured["images"] and not f.captured["bundles"]


@pytest.mark.parametrize("name,field,value", [
    ("toolchain_receipt", "image", "sha256:" + "f" * 64),
    ("toolchain_receipt", "sdk_version", "6.0.428"),
    ("toolchain_receipt", "sut_dotnet", "/usr/share/dotnet/dotnet"),
    ("toolchain_receipt", "stager_dotnet", "/opt/reviewbench-dotnet6/dotnet"),
    ("toolchain_receipt", "stager_rebuilt", True),
    ("toolchain_receipt", "warning_policy_changes", ["disabled"]),
    ("toolchain_receipt", "sut_compiler_sha256", "0" * 64),
    ("public_control", "public", {"returncode": 1}),
    ("public_control", "source_unchanged", False),
    ("public_control", "original_assertion_count", 20),
    ("public_control", "six_replay_calibrations", 6),
    ("public_control", "staged_non_build_changes", ["source.cs"]),
    ("input_audit", "extra_source_changes", ["source.cs"]),
    ("input_audit", "resolved_packages_outside_original_twenty", ["new/package"]),
    ("input_audit", "three_fresh_public_outputs", {"net6": {}}),
])
def test_resigned_fixture_still_must_obey_profile_semantics(configured, monkeypatch, name, field, value):
    f = configured
    reference = f.options[name]
    data = json.loads(Path(reference["path"]).read_text())
    data[field] = value
    replacement = write_json(Path(reference["path"]), data)
    f.options[name] = replacement
    monkeypatch.setitem(sdk6.PINS, name, replacement["sha256"])
    with pytest.raises(ContractError):
        migrate(f)
    assert not f.captured["images"]


@pytest.mark.parametrize("field,value", [("sdk_version", "6.0.400"), ("image", sdk6.SDK6_IMAGE),
                                       ("status", "failed"), ("production_source_rewrites", ["source.cs"])])
def test_original_preparation_is_not_promoted_or_rewritten(configured, monkeypatch, field, value):
    f = configured
    f.dependencies[field] = value
    write_json(f.dependency_path, f.dependencies)
    monkeypatch.setattr(sdk6, "DEPENDENCIES_SHA256", file_hash(f.dependency_path))
    with pytest.raises(ContractError, match="original SDK8"):
        migrate(f)
    assert not f.captured["images"]


@pytest.mark.parametrize("mutation", ["private_test", "golden", "prepare", "test_command", "test_id"])
def test_original_commands_and_assertions_fail_closed(configured, mutation):
    f = configured
    if mutation == "private_test":
        (f.directory / "oracle/Program.cs").write_text("easier")
    elif mutation == "golden":
        (f.directory / "source-only-golden.patch").write_text("different")
    elif mutation == "prepare":
        f.spec["prepare_command"] += " -p:TreatWarningsAsErrors=false"
    elif mutation == "test_command":
        f.spec["test_command"] += " --framework net6"
    else:
        f.oracle["pass_to_pass_tests"] = ["easier"]
    write_json(f.ctx.legacy / "csharp/specs.json", {"26": f.spec})
    with pytest.raises(ContractError):
        migrate(f)
    assert not f.captured["images"]


@pytest.mark.parametrize("sdk,csc,content,expected", [
    ("6.0.400", sdk6.CSC_VERSION, b"compiler", 0),
    ("8.0.425", sdk6.CSC_VERSION, b"compiler", 1),
    ("6.0.400", "different compiler", b"compiler", 1),
    ("6.0.400", sdk6.CSC_VERSION, b"changed compiler", 1),
])
def test_actual_shell_sdk_and_compiler_guards_without_a_real_sdk(tmp_path, monkeypatch, sdk, csc, content, expected):
    host, compiler, cwd = tmp_path / "dotnet-fixture", tmp_path / "csc.fixture", tmp_path / "cwd"
    cwd.mkdir()
    compiler.write_bytes(content)
    host.write_text("#!/bin/bash\nset -eu\n"
                    f'if [ "$1" = --version ]; then echo "{sdk}"; '
                    f'elif [ "$1" = --info ]; then echo fixture-only; else echo "{csc}"; fi\n')
    host.chmod(0o755)
    monkeypatch.setattr(sdk6, "SUT_HOST", str(host))
    monkeypatch.setattr(sdk6, "CSC", str(compiler))
    monkeypatch.setattr(sdk6, "CSC_SHA256", hashlib.sha256(b"compiler").hexdigest())
    run = subprocess.run(["bash", "-c", "set -euo pipefail; " + sdk6.sdk_checks(str(cwd))],
                         capture_output=True, text=True)
    assert (run.returncode == 0) is (expected == 0)
    assert "CSHARP_SDK6_SELECTED=" in run.stdout


@pytest.mark.parametrize("code", [1, 2, 124, 137])
def test_build_failure_is_not_a_signal_to_try_sdk8(tmp_path, monkeypatch, code):
    host = tmp_path / "compiler-fixture"
    counter = tmp_path / "calls"
    host.write_text(f'#!/bin/bash\nprintf "%s\\n" "$*" >> {counter}\nexit {code}\n')
    host.chmod(0o755)
    monkeypatch.setattr(sdk6, "SUT_HOST", str(host))
    command = "set -euo pipefail; " + sdk6._sut("dotnet build original.csproj --no-restore")
    result = subprocess.run(["bash", "-c", command], capture_output=True, text=True)
    assert result.returncode == code
    assert counter.read_text().splitlines() == ["build original.csproj --no-restore"]
