"""Real tiny, gated baseline/modified/rollback C# adapter fixture.

This synthetic repository is separate from the 402 tasks. Only the modified
phase builds it; old adapters genuinely return their original pending error.
The modified phase executes the generated recipe using the frozen environment,
including positive multi-file and negative candidate-change/deleted-helper tests.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.environ.get("REVIEWBENCH_ENGINE_SRC", str(Path(__file__).resolve().parents[2] / "src")))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reviewbench_maintainability.agentic.assets import AssetPool
from reviewbench_maintainability.agentic.cli import load_config
from reviewbench_maintainability.agentic.contracts import ContractError, atomic_json, component, file_hash
from reviewbench_maintainability.agentic.environment import ReviewBenchDockerEnvironment
from reviewbench_maintainability.agentic.execution import ExecutionServices
from reviewbench_maintainability.agentic.resources import CURRENT_JOB, GiB, ResourceRequest
from agentic_runtime_migration.common import MigrationContext
from agentic_runtime_migration.csharp_candidate import POLICY
from agentic_runtime_migration.csharp_dependencies import EXPECTED_ENGINE
from reviewbench_maintainability.agentic.models import engine_identity

PRODUCTION = "src/Compilers/CSharp/Portable"


class FixtureContext(MigrationContext):
    def task_directory(self, task, oracle, lane):
        return self.legacy / "fixture"

    def modules(self, task):
        return []

    def immutable_tests(self, task):
        return []

    def golden(self, task, oracle, lane, directory):
        return self.copy_asset(directory / "fixture-only.patch", "fixture-golden")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--tool-record", type=Path, required=True)
    parser.add_argument("--toolchains", type=Path, required=True)
    parser.add_argument("--adapter-file", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--phase", choices=("baseline", "modified", "rollback"), required=True)
    args = parser.parse_args()
    if engine_identity()["fingerprint"] != EXPECTED_ENGINE:
        raise ContractError("fixture execution engine changed")
    output = args.out_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    legacy = output / "legacy"
    (legacy / "csharp").mkdir(parents=True, exist_ok=True)
    directory = legacy / "fixture"
    (directory / "oracle").mkdir(parents=True, exist_ok=True)
    spec = {"classification": "pass", "host_adapter": "sdk-adapter.sh", "workdir": "/task",
            "image": "rb-csharp-sdk9-cov", "timeout": 120, "pass_to_pass_tests": ["multifile_fixture"],
            "signature_regex": r"P2P fixture assertions=(\d+) PASS",
            "test_command": "dotnet run --project oracle/Oracle.csproj --no-restore --no-build --nologo",
            "prepare_command": "dotnet restore oracle/Oracle.csproj --nologo -v minimal && "
                               "dotnet build oracle/Oracle.csproj --no-restore --nologo -v minimal"}
    (legacy / "csharp/specs.json").write_text(json.dumps({"20": spec}))
    task = {"id": "csharp_candidate_fixture", "repo": "reviewbench/csharp-fixture", "start_sha": "1" * 40,
            "locality": {"path": PRODUCTION + "/Entry.cs"}}
    oracle = {"index": 20, "start_signature": "3", "golden_signature": "3",
              "pass_to_pass_tests": ["multifile_fixture"]}
    module_spec = importlib.util.spec_from_file_location(
        "agentic_runtime_migration.csharp_lane_fixture", args.adapter_file)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    if args.phase != "modified":
        try:
            module.migrate(SimpleNamespace(legacy=legacy), task, oracle)
        except ContractError as exc:
            result = "C# FIXTURE route=rejected error=" + str(exc)
            print(result, flush=True)
            atomic_json(output / "result.json", {"phase": args.phase, "adapter_sha256": file_hash(args.adapter_file),
                                                "exit_status": 2, "literal_output": result,
                                                "production_build_started": False})
            return 2
        raise ContractError("old adapter unexpectedly admitted the production-project fixture")
    (directory / "oracle/Oracle.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType>'
        '<TargetFramework>net9.0</TargetFramework><ImplicitUsings>enable</ImplicitUsings></PropertyGroup>'
        '<ItemGroup><ProjectReference Include="../repo/' + PRODUCTION +
        '/Microsoft.CodeAnalysis.CSharp.csproj" /></ItemGroup></Project>\n')
    (directory / "oracle/Program.cs").write_text(
        'using Fixture;\n'
        'if (Entry.Value() != 11) throw new Exception("current non-anchor input was not 11");\n'
        'if (Extra.Value != 7) throw new Exception("existing non-anchor input was not used");\n'
        'if (NewHelper.Value != 4) throw new Exception("new helper was not used");\n'
        'Console.WriteLine("P2P fixture assertions=3 PASS");\n')
    (directory / "fixture-only.patch").write_text(
        "diff --git a/fixture b/fixture\n--- a/fixture\n+++ b/fixture\n@@ -1 +1 @@\n-old\n+new\n")
    for variant in ("start", "golden"):
        for repetition in (1, 2, 3):
            (directory / f"{variant}-{repetition}.log").write_text("P2P fixture assertions=3 PASS\n")
    config = load_config(args.config)
    deps = output / "dependencies"
    config = {**config, "out_dir": str(output), "asset_root": str(output),
              "migration": {**config.get("migration", {}), "toolchain_registry": str(args.toolchains),
                            "csharp_dependency_root": str(deps)}}
    services = ExecutionServices(config)
    ctx = FixtureContext(config, {task["id"]: task}, {task["id"]: oracle}, legacy, output)
    tool = json.loads(args.tool_record.read_text())
    registry = json.loads(args.toolchains.read_text())
    from agentic_runtime_migration.csharp_candidate import sdk_version
    checks = []
    with services.gate.acquire(ResourceRequest("csharp:real-multifile-fixture", cpus=4, memory_bytes=8 * GiB,
                                               scratch_bytes=3 * GiB, heavy=True),
                               uses_containers=True, timeout=3600) as lease:
        packages = output / "empty-packages"
        (packages / "feed").mkdir(parents=True, exist_ok=True)
        (packages / "packages").mkdir(exist_ok=True)
        (packages / "feed/.root").write_text("fixture uses only the installed SDK\n")
        (packages / "packages/.root").write_text("no legacy DLL\n")
        atomic_json(deps / component(task["id"]) / "dependencies.json", {
            "task_id": task["id"], "start_sha": task["start_sha"], "policy": POLICY,
            "image": registry["rb-csharp-sdk9-cov"]["image"],
            "sdk_version": sdk_version(registry, "rb-csharp-sdk9-cov"),
            "tool": tool["asset"], "packages": ctx.bundle(packages), "status": "prepared",
        })
        recipe = module.migrate(ctx, task, oracle)
        atomic_json(output / "fixture-recipe.json", __import__("dataclasses").asdict(recipe))
        pool = AssetPool(services.cache, services.scratch, lease["lease"], output)
        CURRENT_JOB.get().update(asset_pool=pool, workspace_exclusions={})
        try:
            for name in ("current_multifile", "changed_non_anchor", "missing_new_helper", "escaping_link"):
                with tempfile.TemporaryDirectory(prefix="csharp-small-fixture-", dir=ctx.scratch) as temporary:
                    workspace = Path(temporary)
                    source = workspace / PRODUCTION
                    source.mkdir(parents=True)
                    (source / "Microsoft.CodeAnalysis.CSharp.csproj").write_text(
                        '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net9.0</TargetFramework>'
                        '<AssemblyName>Fixture.Production</AssemblyName>'
                        '<CustomAfterMicrosoftCommonTargets>$(MSBuildProjectDirectory)/Keep.targets</CustomAfterMicrosoftCommonTargets>'
                        '</PropertyGroup></Project>\n')
                    (source / "Keep.targets").write_text(
                        '<Project><Target Name="KeepCandidateBuildConfiguration" BeforeTargets="BeforeCompile">'
                        '<PropertyGroup><DefineConstants>$(DefineConstants);CANDIDATE_CONFIG_KEPT</DefineConstants>'
                        '</PropertyGroup></Target></Project>\n')
                    (source / "Entry.cs").write_text(
                        "#if !CANDIDATE_CONFIG_KEPT\n#error current candidate build configuration was overridden\n#endif\n"
                        "namespace Fixture { public class Entry { public static int Value() => Extra.Value + NewHelper.Value; } }\n")
                    (source / "Extra.cs").write_text(
                        "namespace Fixture { public class Extra { public const int Value = " +
                        ("8" if name == "changed_non_anchor" else "7") + "; } }\n")
                    if name != "missing_new_helper":
                        (source / "NewHelper.cs").write_text(
                            "namespace Fixture { public class NewHelper { public const int Value = 4; } }\n")
                    (source / "bin").mkdir()
                    (source / "bin/Fixture.Production.dll").write_bytes(b"stale target DLL must not be used")
                    if name == "escaping_link":
                        (source / "escape.cs").symlink_to("/etc/passwd")
                    (workspace / "global.json").write_text(
                        '{"sdk":{"version":"0.0.0"},"tools":{"kept":"yes"},"msbuild-sdks":{}}\n')
                    env = ReviewBenchDockerEnvironment(recipe, workspace, output / name, output, grader=True)
                    observations = []
                    try:
                        for command in [*recipe.bootstrap, *recipe.grading["bootstrap"],
                                        recipe.grading["commands"][0]["command"]]:
                            observed = env.execute({"command": command})
                            observations.append({"command": command, "returncode": observed["returncode"],
                                                 "stdout_file": observed["extra"]["stdout_file"],
                                                 "stderr_file": observed["extra"]["stderr_file"]})
                            if observed["returncode"]:
                                break
                    finally:
                        env.close()
                    code = observations[-1]["returncode"]
                    expected = code == 0 if name == "current_multifile" else code != 0
                    checks.append({"name": name, "expected_result_observed": expected,
                                   "exit_status": code, "commands": observations})
                    print(f"C# FIXTURE {name} exit={code} expected_observed={str(expected).lower()}", flush=True)
                    if not expected:
                        raise ContractError("tiny C# fixture did not observe the required candidate behavior: " + name)
        finally:
            pool.close()
    result = "C# FIXTURE multi-file=PASS negative-controls=3/3 PASS"
    atomic_json(output / "result.json", {"phase": args.phase, "adapter_sha256": file_hash(args.adapter_file),
                                        "exit_status": 0, "literal_output": result, "checks": checks,
                                        "engine_fingerprint": EXPECTED_ENGINE, "fixture_not_task_proof": True})
    print(result, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
