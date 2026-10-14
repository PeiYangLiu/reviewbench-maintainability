"""Rebuild frozen source-linked C# behavior suites against the full candidate.

The developer receives the SDK and an assertion-free Roslyn syntax command.
The original assertion/stub files stay private. Newly introduced production
sources are compiled as well, instead of restricting edits to the old anchor.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import shlex
import shutil
import tempfile
from xml.sax.saxutils import escape

from reviewbench_maintainability.agentic.contracts import ContractError, RuntimeRecipe, repo_path
from reviewbench_maintainability.agentic.snapshots import git

PRIVATE = "/opt/reviewbench-assets/csharp-tests"
RUNTIME = "/workspace/.reviewbench/csharp"


def additional_sources_targets(baseline):
    """Retain the original closure, but also compile newly extracted helpers."""
    # Excluding every start-tree source (not just the anchor) leaves the
    # original explicit Compile items unchanged. The extra item group picks
    # up new production files; it does not import repository test suites or
    # generated build outputs into the frozen behavior harness.
    originals = "\n".join('    <_StartSource Include="../repo/' + escape(name, {'"': "&quot;"}) + '" />'
                          for name in sorted(baseline) if name.endswith(".cs"))
    return """<Project>
  <ItemGroup>
""" + originals + """
  </ItemGroup>
  <Target Name="ReviewBenchCandidateSources" BeforeTargets="BeforeCompile">
    <ItemGroup>
      <Compile Include="../repo/**/*.cs"
               Exclude="@(_StartSource);@(Compile);../repo/.reviewbench/**;../repo/.git/**;../repo/**/bin/**;../repo/**/obj/**;../repo/**/test/**;../repo/**/tests/**;../repo/**/Tests/**;../repo/**/*Tests.cs;../repo/**/*Test.cs"
               Link="Candidate/%(RecursiveDir)%(Filename)%(Extension)" />
    </ItemGroup>
  </Target>
</Project>
"""


def completion_pattern(directory, spec, oracle):
    matches = []
    for variant in ("start", "golden"):
        for number in (1, 2, 3):
            text = (directory / f"{variant}-{number}.log").read_text(errors="replace")
            match = re.search(spec["signature_regex"], text, re.MULTILINE)
            if not match:
                raise ContractError("frozen C# assertion completion is absent")
            signature = match.group(1) if match.groups() else match.group()
            if str(oracle[variant + "_signature"]) != signature:
                raise ContractError("C# assertion count differs from its frozen Oracle")
            matches.append(match.group())
    if len(set(matches)) != 1:
        raise ContractError("C# triple-run assertion completion differs")
    return r"^" + re.escape(matches[0]) + r"\s*$"


def migrate(ctx, task, oracle):
    from .csharp_sqlclient_original import migrate as original_sqlclient
    sqlclient = original_sqlclient(ctx, task, oracle)
    if sqlclient is not None:
        return sqlclient
    from .csharp_original_cli import migrate as original_cli
    cli = original_cli(ctx, task, oracle)
    if cli is not None:
        return cli
    from .csharp_original_sdk6 import migrate as original_sdk6
    original = original_sdk6(ctx, task, oracle)
    if original is not None:
        return original
    spec = json.loads((ctx.legacy / "csharp/specs.json").read_text())[str(oracle["index"])]
    if spec.get("classification") != "pass":
        raise ContractError("C# historical recipe is not executable")
    from .csharp_candidate import PLANS, migrate_project
    if int(oracle["index"]) in PLANS:
        return migrate_project(ctx, task, oracle, spec, ctx.task_directory(task, oracle, "csharp"))
    if spec.get("host_adapter") or spec.get("adapter_patch") or spec.get("workdir") != "/task":
        raise ContractError("C# project/service compatibility adapter pending")
    directory = ctx.task_directory(task, oracle, "csharp")
    project = directory / "oracle/Oracle.csproj"
    if not project.is_file():
        raise ContractError("C# task requires a production-project dependency adapter")
    project_text = project.read_text()
    if "<ProjectReference" in project_text or "<PackageReference" in project_text:
        raise ContractError("C# production-project NuGet dependency adapter pending")
    image, audit = ctx.image(spec["image"])
    registry = json.loads((ctx.output / "toolchains.json").read_text())
    version_match = re.search(r"\.NET SDK:\s*\n\s*Version:\s*(\S+)", registry[spec["image"]]["versions"])
    if not version_match:
        raise ContractError("C# immutable toolchain SDK version evidence is missing")
    baseline = [name.decode() for name in git(ctx.repo(task), "ls-tree", "-r", "--name-only", "-z",
                                            task["start_sha"]).split(b"\0") if name]
    with tempfile.TemporaryDirectory(prefix="csharp-harness-", dir=ctx.scratch) as temporary:
        curated = Path(temporary)
        shutil.copytree(directory / "oracle", curated / "oracle",
                        ignore=shutil.ignore_patterns("bin", "obj"), symlinks=False)
        # The historical harness lived outside the repository. Prevent it
        # from inheriting candidate SDK/build configuration via its new local
        # scratch placement, while preserving all assertion source bytes.
        (curated / "global.json").write_text(json.dumps({"sdk": {"version": version_match[1], "rollForward": "disable"}}))
        (curated / "oracle/Directory.Build.props").write_text("<Project />\n")
        (curated / "oracle/Directory.Build.targets").write_text(additional_sources_targets(baseline))
        (curated / "NuGet.Config").write_text(
            "<configuration><packageSources><clear /></packageSources></configuration>\n")
        harness = ctx.bundle(curated, role="grader")
    expected = oracle.get("pass_to_pass_tests")
    if not expected or expected != spec["pass_to_pass_tests"]:
        raise ContractError("C# frozen test selectors differ")
    grade = [
        "mkdir -p " + RUNTIME + " && cp -R " + PRIVATE + "/. " + RUNTIME +
        " && ln -s /workspace " + RUNTIME + "/repo",
        "cd " + RUNTIME + " && dotnet restore oracle/Oracle.csproj --nologo -v minimal --disable-parallel "
        "-p:NuGetAudit=false && dotnet build oracle/Oracle.csproj --no-restore --nologo -v minimal "
        "-m:1 -p:UseSharedCompilation=false",
    ]
    test_command = spec["test_command"]
    if test_command != "dotnet run --project oracle/Oracle.csproj --no-restore --no-build --nologo":
        raise ContractError("C# test command needs an explicit path adapter")
    recipe = RuntimeRecipe(
        task_id=task["id"], repo=task["repo"], start_sha=task["start_sha"], lane="csharp",
        image=image, image_audit=audit, cpus=2, memory_mb=4096, build_scratch_mb=4096,
        command_timeout_seconds=spec.get("timeout", 1200), submodules=ctx.modules(task),
        env={"TMPDIR": "/workspace/.reviewbench/tmp", "DOTNET_CLI_HOME": "/workspace/.reviewbench/dotnet-home",
             "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1", "DOTNET_NOLOGO": "1",
             "NUGET_PACKAGES": "/workspace/.reviewbench/nuget", "MSBUILDDISABLENODEREUSE": "1", "CI": "true"},
        assets=[{**harness, "kind": "archive", "visibility": "grader", "target": PRIVATE}],
        bootstrap=["mkdir -p /workspace/.reviewbench/tmp /workspace/.reviewbench/dotnet-home"],
        dev_checks=["dotnet /opt/reviewbench-tools/csharp-syntax/CSharpSyntax.dll " +
                    shlex.quote("/workspace/" + repo_path(task["locality"]["path"]))],
        grading={"multifile_build": True, "immutable_paths": ctx.immutable_tests(task),
                 "golden_patch": ctx.golden(task, oracle, "csharp", directory),
                 "bootstrap": grade, "commands": [{
                     "command": "cd " + RUNTIME + " && " + test_command, "parser": "regex",
                     "expected_tests": expected, "success_pattern": completion_pattern(directory, spec, oracle)}]},
    )
    recipe.validate()
    return recipe
