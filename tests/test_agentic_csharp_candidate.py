from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import csharp_candidate as adapter
from agentic_runtime_migration.csharp_staging import SOURCE
from reviewbench_maintainability.agentic.contracts import ContractError


def console_spec():
    return {"classification": "pass", "workdir": "/task", "pass_to_pass_tests": ["fixed"],
            "test_command": "dotnet run --project oracle/Oracle.csproj --no-restore --no-build --nologo",
            "prepare_command": "dotnet restore oracle/Oracle.csproj --nologo -v minimal && "
                               "dotnet build oracle/Oracle.csproj --no-restore --nologo -v minimal"}


def test_all_eight_get_candidate_project_plans():
    legacy = {16, 18, 19, 20, 21, 24, 25, 28}
    assert adapter.LEGACY_PROJECT_INDICES == legacy
    assert set(adapter.PLANS) == legacy | {26}
    for index in (16, 19, 20, 25, 26, 28):
        plan = adapter.plan_for(index, console_spec())
        assert plan["kind"] == "console"
        assert plan["production"]
        assert all(".." not in path for path in plan["production"])


def test_no_alias_or_variant_specific_implementation_selector():
    assert "golden" not in SOURCE.lower()
    assert "reference checkout" not in SOURCE.lower()
    assert "legacy" not in SOURCE.lower()
    assert "task_id" not in SOURCE.lower()
    assert "File.Copy(item.FullName, target, false)" in SOURCE
    assert "production bytes changed after staging" in SOURCE
    assert "candidate link escapes source" in SOURCE


@pytest.mark.parametrize("replacement", ["dotnet run --project other.csproj",
                                        "dotnet run --project oracle/Oracle.csproj --no-build --nologo"])
def test_fixed_console_command_cannot_be_weakened(replacement):
    spec = console_spec()
    spec["test_command"] = replacement
    with pytest.raises(ContractError, match="command changed"):
        adapter.plan_for(25, spec)


def test_original_restore_build_options_survive():
    spec = console_spec()
    spec["prepare_command"] = (
        "dotnet restore oracle/Oracle.csproj --source /task/local-nuget --source https://example.invalid "
        "-p:NO_ANDROID=true --nologo && dotnet build oracle/Oracle.csproj --no-restore "
        "-p:NO_ANDROID=true -p:NoWarn=CS8600 --nologo"
    )
    before = deepcopy(spec)
    restore, build = adapter.build_commands(spec, adapter.PLANS[25])
    assert "--source" not in restore
    assert "-p:NO_ANDROID=true" in restore and "-p:NO_ANDROID=true" in build
    assert "-p:NoWarn=CS8600" in build
    assert "-m:1" in build and "UseSharedCompilation=false" in build
    assert "--no-restore" in build
    assert spec == before
    assert "BuildProjectReferences=false" not in build
    assert "CustomAfterMicrosoftCommonTargets=" not in build


def test_postgres_changes_only_fixed_transport_literals():
    value = ('const string admin = "Host=localhost;Database=postgres;User Id=postgres";\n'
             'var child = "Host=localhost;Database={0};User Id=postgres";\n'
             'Check(await store.DoesCollectionExistAsync(collection), "same assertion");\n')
    changed = adapter.remap_service_program(value, "postgres", "task")
    assert changed.replace("Host=reviewbench-postgres;", "Host=localhost;") == value
    with pytest.raises(ContractError, match="literals changed"):
        adapter.remap_service_program(value + 'Host=localhost;', "postgres", "task")


def test_sql_transport_does_not_copy_historical_credential_or_change_assertions():
    original = ('"Server=172.17.0.1,14339;User ID=sa;Password=old-fixture-only;"\n'
                'Check(result == 42); Check(connection.State == ConnectionState.Closed);\n')
    changed = adapter.remap_service_program(original, "sql", "fixture")
    assert "reviewbench-sql,1433" in changed
    assert "old-fixture-only" not in changed
    assert changed.splitlines()[1:] == original.splitlines()[1:]
    assert adapter.service_password("fixture") == adapter.service_password("fixture")
    assert adapter.service_password("other") != adapter.service_password("fixture")


def test_console_harness_never_imports_legacy_production_or_outputs(tmp_path):
    legacy = tmp_path / "legacy"
    (legacy / "oracle/bin").mkdir(parents=True)
    (legacy / "repo/src").mkdir(parents=True)
    (legacy / "oracle/Oracle.csproj").write_text(
        '<Project><ProjectReference Include="../repo/src/GraphQL/GraphQL.csproj" /></Project>')
    (legacy / "oracle/Program.cs").write_bytes(b'Check(candidate.Value == 42);\n')
    (legacy / "oracle/bin/GraphQL.dll").write_bytes(b"stale implementation")
    (legacy / "repo/src/OLD.cs").write_text("reference implementation")
    output = tmp_path / "private"
    output.mkdir()
    records = adapter.curate_harness(legacy, output, {"id": "fixture"}, console_spec(), adapter.PLANS[25])
    assert (output / "oracle/Program.cs").read_bytes() == b'Check(candidate.Value == 42);\n'
    assert not (output / "repo").exists()
    assert not (output / "oracle/bin").exists()
    assert records["oracle/Program.cs"]["sha256"] == records["oracle/Program.cs"]["original_sha256"]


def test_full_native_suite_and_certificates_are_retained(tmp_path):
    legacy, output = tmp_path / "legacy", tmp_path / "private"
    root = legacy / "repo/test"
    for name, data in {
        "ReverseProxy.Tests/Yarp.ReverseProxy.Tests.csproj": "<Project />",
        "ReverseProxy.Tests/ConfigValidatorTests.cs": "all 119 original cases",
        "Tests.Common/Helper.cs": "frozen infrastructure",
        "TestCertificates/certificate.cer": "fixed fixture",
    }.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
    output.mkdir()
    spec = console_spec()
    records = adapter.curate_harness(legacy, output, {"id": "fixture"}, spec, adapter.PLANS[21])
    assert len(records) == 4
    assert all(r["sha256"] == r["original_sha256"] for r in records.values())
    assert (output / "tests/test/TestCertificates/certificate.cer").read_text() == "fixed fixture"
    assert not (output / "tests/src").exists()


def test_test_overlay_rejects_links(tmp_path):
    legacy, output = tmp_path / "legacy", tmp_path / "private"
    (legacy / "repo/test/ReverseProxy.Tests").mkdir(parents=True)
    (legacy / "repo/test/ReverseProxy.Tests/Yarp.ReverseProxy.Tests.csproj").write_text("<Project />")
    (legacy / "repo/test/link.cs").symlink_to("ReverseProxy.Tests/Yarp.ReverseProxy.Tests.csproj")
    output.mkdir()
    with pytest.raises(ContractError, match="regular file"):
        adapter.curate_harness(legacy, output, {"id": "fixture"}, console_spec(), adapter.PLANS[21])


def test_sdk_requires_immutable_image_and_retained_version():
    record = {"sdk": {"status": "ready", "image": "sha256:" + "a" * 64,
                      "versions": ".NET SDK:\n Version: 8.0.425\n"}}
    assert adapter.sdk_version(record, "sdk") == "8.0.425"
    record["sdk"]["image"] = "latest"
    with pytest.raises(ContractError):
        adapter.sdk_version(record, "sdk")


def test_msbuild_sdks_are_not_discarded_by_staging():
    assert 'config["sdk"]' in SOURCE
    assert 'config["msbuild-sdks"] =' not in SOURCE
    assert 'config["tools"] =' not in SOURCE
    assert 'version["version"] = sdk' in SOURCE
    assert 'version["rollForward"] = "disable"' in SOURCE


def test_dependency_export_does_not_include_project_build_outputs(tmp_path):
    import hashlib
    import zipfile
    from agentic_runtime_migration.csharp_dependencies import package_snapshot
    source = tmp_path / "packages"
    package = source / "example/1.2.3"
    package.mkdir(parents=True)
    with zipfile.ZipFile(package / "example.1.2.3.nupkg", "w") as archive:
        archive.writestr("example.nuspec", '<package><metadata><id>Example</id><version>1.2.3</version></metadata></package>')
    (tmp_path / "old-target.dll").write_text("must not be exported")
    export = tmp_path / "export"
    records = package_snapshot(source, export, tmp_path / "shared")
    assert len(records) == 1
    assert records[0]["sha256"] == hashlib.sha256((package / "example.1.2.3.nupkg").read_bytes()).hexdigest()
    assert (export / "feed/example.1.2.3.nupkg").is_file()
    assert not (export / "old-target.dll").exists()


def test_dependency_export_checks_original_package_sha512(tmp_path):
    import zipfile
    from agentic_runtime_migration.csharp_dependencies import package_snapshot
    source = tmp_path / "packages"
    package = source / "example/1"
    package.mkdir(parents=True)
    with zipfile.ZipFile(package / "example.1.nupkg", "w") as archive:
        archive.writestr("example.nuspec", "<package />")
    (package / "example.1.nupkg.sha512").write_text("incorrect")
    with pytest.raises(ContractError, match="SHA512"):
        package_snapshot(source, tmp_path / "export", tmp_path / "shared")


def test_sentry_auxiliary_tool_is_source_declared_not_unpinned_download():
    tools = adapter.PLANS[24]["auxiliary"]
    assert tools == {"tools/sentry-cli/2.21.2/sentry-cli-Linux-x86_64":
                     "0016d21b20d6b83fe10d514d10ca1dbed854e8694d71fada9749e702730ed728"}


def test_native_summary_matches_complete_frozen_test_set_not_legacy_prefix(tmp_path):
    import re
    line = "Passed! - Failed: 0, Passed: 119, Skipped: 0, Total: 119, Duration: 123 ms - Yarp.ReverseProxy.Tests.dll (net8.0)"
    spec = {"signature_regex": r"Passed!\s+- Failed:\s+0, Passed:\s+(\d+),"}
    oracle = {"start_signature": "119", "golden_signature": "119"}
    for variant in ("start", "golden"):
        for repetition in (1, 2, 3):
            (tmp_path / f"{variant}-{repetition}.log").write_text(line + "\n")
    expression = adapter.project_completion_pattern(tmp_path, spec, oracle, {"kind": "tests"})
    assert re.search(expression, line + "\n", re.MULTILINE)
    assert re.search(expression, line.replace("123 ms", "3 s"), re.MULTILINE)
    for wrong in ("Passed: 118", "Skipped: 1", "Total: 120", "Failed: 1"):
        field = wrong.split(":")[0]
        changed = re.sub(field + r": \d+", wrong, line)
        assert not re.search(expression, changed, re.MULTILINE)
    assert not re.search(expression, line.replace("(net8.0)", "(net6.0)"), re.MULTILINE)
    assert not re.search(expression, line.replace("Yarp.ReverseProxy.Tests.dll", "Other.Tests.dll"), re.MULTILINE)


def test_native_frozen_summary_cannot_hide_skipped_cases(tmp_path):
    line = "Passed! - Failed: 0, Passed: 17, Skipped: 1, Total: 18, Duration: 1 ms - Sentry.Tests.dll (net6.0)"
    spec = {"signature_regex": r"Passed!\s+- Failed:\s+0, Passed:\s+(\d+),"}
    oracle = {"start_signature": "17", "golden_signature": "17"}
    for variant in ("start", "golden"):
        for repetition in (1, 2, 3):
            (tmp_path / f"{variant}-{repetition}.log").write_text(line + "\n")
    with pytest.raises(ContractError, match="failed/skipped/missing"):
        adapter.project_completion_pattern(tmp_path, spec, oracle, {"kind": "tests"})


def test_exact_submodule_reuses_only_correct_commit_and_submodule_role(tmp_path):
    from types import SimpleNamespace
    from reviewbench_maintainability.agentic.contracts import file_hash
    source = tmp_path / "descriptor.json"
    source.write_text(json.dumps({"source_kind": "submodule", "git_commit": "a" * 40}))
    asset = {"path": str(source), "sha256": file_hash(source)}
    module = {"path": "modules/Ben.Demystifier", "commit": "a" * 40, "asset": asset}
    ctx = SimpleNamespace(output=tmp_path)
    assert adapter.exact_submodule_asset(ctx, tmp_path, module) == asset
    module["commit"] = "b" * 40
    with pytest.raises(ContractError, match="Git commit"):
        adapter.exact_submodule_asset(ctx, tmp_path, module)


def test_submodule_rejects_grader_archive_instead_of_relabelling(tmp_path):
    from types import SimpleNamespace
    from reviewbench_maintainability.agentic.contracts import file_hash
    source = tmp_path / "descriptor.json"
    source.write_text(json.dumps({"source_kind": "grader", "git_commit": "a" * 40}))
    module = {"path": "modules/Ben.Demystifier", "commit": "a" * 40,
              "asset": {"path": str(source), "sha256": file_hash(source)}}
    with pytest.raises(ContractError, match="unexpected role"):
        adapter.exact_submodule_asset(SimpleNamespace(output=tmp_path), tmp_path, module)


def test_real_native_tests_are_restored_from_exact_git_not_dirty_retained_checkout(tmp_path):
    import subprocess
    repository = tmp_path / "git-input"
    repository.mkdir()
    names = {
        "test/ReverseProxy.Tests/Yarp.ReverseProxy.Tests.csproj": "<Project />\n",
        "test/ReverseProxy.Tests/ConfigValidatorTests.cs": "ALL_ORIGINAL_ASSERTIONS\n",
        "test/TestCertificates/fixture.cer": "unchanged fixture\n",
    }
    for name, text in names.items():
        path = repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    for args in [("init", "--quiet"), ("add", "."), ("-c", "user.name=Fixture", "-c",
                 "user.email=fixture@example.invalid", "commit", "--quiet", "-m", "fixed input")]:
        subprocess.run(["git", "-C", str(repository), *args], check=True, capture_output=True)
    start = subprocess.check_output(["git", "-C", str(repository), "rev-parse", "HEAD"], text=True).strip()
    legacy, private = tmp_path / "legacy", tmp_path / "private"
    changed = legacy / "repo/test/ReverseProxy.Tests/ConfigValidatorTests.cs"
    changed.parent.mkdir(parents=True)
    changed.write_text("UNAUTHORIZED_RETAINED_EDIT\n")
    (changed.parent / "InjectedTests.cs").write_text("not a fixed input")
    private.mkdir()
    records = adapter.curate_harness(legacy, private, {"id": "fixture", "start_sha": start},
                                    console_spec(), adapter.PLANS[21], repository=repository)
    assert set(records) == set(names)
    for name, expected in names.items():
        assert (private / "tests" / name).read_text() == expected
        assert records[name]["authority"] == "exact-start-git"
        assert records[name]["git_commit"] == start
        assert records[name]["git_blob"]
    assert records["test/ReverseProxy.Tests/ConfigValidatorTests.cs"]["retained_differed"]
    assert changed.read_text() == "UNAUTHORIZED_RETAINED_EDIT\n"  # original retained data was not edited
    assert not (private / "tests/test/ReverseProxy.Tests/InjectedTests.cs").exists()


def test_real_native_task_cannot_fall_back_to_unattested_checkout(tmp_path):
    with pytest.raises(ContractError, match="exact-start Git"):
        adapter.curate_harness(tmp_path, tmp_path, {"id": "task", "start_sha": "a" * 40},
                               console_spec(), adapter.PLANS[21])


def test_declared_private_setup_uses_candidate_predicate_not_phase_and_preserves_production(tmp_path):
    import hashlib
    import subprocess
    runtime, private = tmp_path / "runtime", tmp_path / "private"
    private.mkdir()
    source = runtime / "repo/src/Service.cs"
    helper = runtime / "repo/test/Helper.cs"
    source.parent.mkdir(parents=True)
    helper.parent.mkdir(parents=True)
    original = b"KEEP_ASSERTION();\nservices.ConfigureCustomShortcuts(config);\nKEEP_OTHER_ASSERTION();\n"
    effective = b"KEEP_ASSERTION();\nKEEP_OTHER_ASSERTION();\n"
    script = private / "private-setup.sh"
    script.write_text('#!/usr/bin/env bash\nset -euo pipefail\n'
                      'if ! grep -q ConfigureCustomShortcuts "$REPO_DIR/src/Service.cs"; then\n'
                      '  sed -i /ConfigureCustomShortcuts/d "$REPO_DIR/test/Helper.cs"\nfi\n')
    digest = lambda b: hashlib.sha256(b).hexdigest()
    plan = {"private_setup": {"sha256": digest(script.read_bytes()),
                             "helper": "test/Helper.cs", "helper_sha256": digest(original),
                             "effective_helper_sha256": digest(effective),
                             "predicate_source": "src/Service.cs"}}
    command = adapter.private_setup_command(plan, runtime=str(runtime), private=str(private))
    assert all(word not in command for word in ("golden", "variant", "reference"))
    for current_source, expected in [(b"ConfigureCustomShortcuts\n", original),
                                      (b"ConfigureTemplates\n", effective)]:
        source.write_bytes(current_source)
        helper.write_bytes(original)
        result = subprocess.run(["bash", "-c", command], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert helper.read_bytes() == expected
        assert source.read_bytes() == current_source
        assert "CSHARP_PRIVATE_SETUP" in result.stdout
    helper.write_text("different unbound input")
    result = subprocess.run(["bash", "-c", command], capture_output=True, text=True)
    assert result.returncode != 0
