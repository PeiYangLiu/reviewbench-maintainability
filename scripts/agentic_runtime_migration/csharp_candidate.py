"""Candidate-preserving production ProjectReference and service adapters.

Only private build metadata / service endpoint literals are adapted. Historical
scripts that rewrite production source or remove test setup are never executed.
Dependencies and the source-built staging tool must be prepared and hash bound
before migration; readiness still requires the unchanged engine's six replays.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile

from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, atomic_json, canonical_hash, component, file_hash, repo_path,
)
from reviewbench_maintainability.agentic.snapshots import git, tree_files

PRIVATE = "/opt/reviewbench-assets/csharp-project-tests"
TOOLS = "/opt/reviewbench-assets/csharp-stage"
PACKAGES = "/opt/reviewbench-assets/csharp-nuget"
RUNTIME = "/workspace/.reviewbench/csharp-project"
POLICY = "current-workspace-project-reference/v1"

# Paths and target frameworks are the fixed legacy plans, not variant selectors.
PLANS = {
    16: {"project": "oracle/Oracle.csproj", "kind": "console", "sdk": "rb-csharp-sdk8-cov",
         "service": "sql", "production": ["src/Microsoft.Data.SqlClient/netcore/src/Microsoft.Data.SqlClient.csproj"]},
    18: {"project": "src/Tests/Microsoft.Diagnostics.Monitoring.Tool.UnitTests/Microsoft.Diagnostics.Monitoring.Tool.UnitTests.csproj",
         "kind": "tests", "sdk": "rb-csharp-sdk8-cov", "test_root": "src/Tests",
         "private_setup": {
             "adapter": "host-adapter.sh",
             "sha256": "2e05517a0c7847ce0c3c60e57399a292c280245ce702c0aab295eb4f32aaae4a",
             "helper": "src/Tests/Microsoft.Diagnostics.Monitoring.Tool.UnitTests/TestHostHelper.cs",
             "helper_sha256": "1bcabbe60c7cac5a8d5111c2c50e9e34b67152d8fb560474a20f352990534397",
             "effective_helper_sha256": "391cf11644aae02f7fe0736dfcdd0134ad3116aef4ab0009865abc15cb0f7f65",
             "predicate_source": "src/Tools/dotnet-monitor/ServiceCollectionExtensions.cs",
         },
         "production": ["src/Tools/dotnet-monitor/dotnet-monitor.csproj"]},
    19: {"project": "oracle/Oracle.csproj", "kind": "console", "sdk": "rb-csharp-sdk8-cov",
         "production": ["src/Workspaces/Core/Portable/Microsoft.CodeAnalysis.Workspaces.csproj",
                        "src/Workspaces/CSharp/Portable/Microsoft.CodeAnalysis.CSharp.Workspaces.csproj"]},
    20: {"project": "oracle/Oracle.csproj", "kind": "console", "sdk": "rb-csharp-sdk9-cov",
         "production": ["src/Compilers/CSharp/Portable/Microsoft.CodeAnalysis.CSharp.csproj"]},
    21: {"project": "test/ReverseProxy.Tests/Yarp.ReverseProxy.Tests.csproj", "kind": "tests",
         "sdk": "rb-csharp-sdk8-cov", "test_root": "test",
         "production": ["src/ReverseProxy/Yarp.ReverseProxy.csproj"]},
    24: {"project": "test/Sentry.Tests/Sentry.Tests.csproj", "kind": "tests", "sdk": "rb-csharp-sdk8-cov",
         "test_root": "test", "module": "modules/Ben.Demystifier",
         "auxiliary": {"tools/sentry-cli/2.21.2/sentry-cli-Linux-x86_64":
                       "0016d21b20d6b83fe10d514d10ca1dbed854e8694d71fada9749e702730ed728"},
         "production": ["src/Sentry/Sentry.csproj"]},
    25: {"project": "oracle/Oracle.csproj", "kind": "console", "sdk": "rb-csharp-sdk8-cov",
         "production": ["src/GraphQL/GraphQL.csproj"]},
    28: {"project": "oracle/Oracle.csproj", "kind": "console", "sdk": "rb-csharp-sdk8-cov",
         "service": "postgres", "sdk_roots": ["", "dotnet"],
         "production": ["dotnet/src/Connectors/Connectors.Memory.Postgres/Connectors.Memory.Postgres.csproj"]},
}
# Keep the original eight-task preparation cohort intact while admitting the
# separately hash-bound index26 profile. No old plan or recipe is rewritten.
LEGACY_PROJECT_INDICES = frozenset(PLANS)
from .csharp_graphql3270 import PLAN as GRAPHQL3270_PLAN
PLANS[26] = GRAPHQL3270_PLAN


def plan_for(index, spec):
    if int(index) not in PLANS or spec.get("classification") != "pass":
        raise ContractError("C# project plan is not an admitted fixed legacy plan")
    plan = dict(PLANS[int(index)])
    plan.setdefault("sdk_roots", [""])
    required = "/task" if plan["kind"] == "console" else "/task/repo"
    if spec.get("workdir") != required:
        raise ContractError("C# fixed workdir changed")
    tokens = shlex.split(spec["test_command"])
    if plan["kind"] == "console":
        if tokens != ["dotnet", "run", "--project", plan["project"], "--no-restore", "--no-build", "--nologo"]:
            raise ContractError("C# fixed console command changed")
    elif tokens[:3] != ["dotnet", "test", plan["project"]] or not all(
        token in tokens for token in ("--no-build", "--no-restore", "--filter")
    ):
        raise ContractError("C# fixed suite command changed")
    return plan


def sdk_version(registry, reference):
    row = registry.get(reference, {})
    match = re.search(r"\.NET SDK:\s*\n\s*Version:\s*(\S+)", row.get("versions", ""))
    if row.get("status") != "ready" or not match or not row.get("image", "").startswith("sha256:"):
        raise ContractError("C# pinned SDK/version evidence is absent")
    return match[1]


def service_password(task_id):
    """An isolated fixture credential; never copy a historical login value."""
    return "Rb!" + hashlib.sha256(("private-sql-fixture:" + task_id).encode()).hexdigest()[:28] + "aA1"


def remap_service_program(text, service, task_id):
    """Change transport literals only; every assertion and database operation stays."""
    if service == "postgres":
        if text.count("Host=localhost;") != 2:
            raise ContractError("fixed Postgres endpoint literals changed")
        return text.replace("Host=localhost;", "Host=reviewbench-postgres;")
    if service == "sql":
        if text.count("172.17.0.1,14339") != 1:
            raise ContractError("fixed SQL endpoint literal changed")
        text = text.replace("172.17.0.1,14339", "reviewbench-sql,1433")
        text, count = re.subn(r"(?i)(Password=)[^;\"\\]+", lambda m: m[1] + service_password(task_id), text)
        if count != 1:
            raise ContractError("fixed SQL fixture credential field changed")
        return text
    raise ContractError("unsupported C# private service")


def start_test_inputs(repository, start_sha, prefix):
    """Read only the fixed test subtree, directly from immutable Git objects."""
    rows = git(repository, "ls-tree", "-r", "-z", start_sha, "--", repo_path(prefix))
    entries = []
    for row in rows.split(b"\0"):
        if not row:
            continue
        metadata, name = row.split(b"\t", 1)
        mode, kind, blob = metadata.decode().split()
        name = repo_path(name.decode())
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise ContractError("fixed C# test subtree needs regular, commit-bound files: " + name)
        entries.append((name, mode, blob))
    if not entries:
        raise ContractError("fixed C# test subtree is absent from exact start")
    result = subprocess.run(
        ["git", "-c", "safe.directory=" + str(repository.resolve()), "-C", str(repository), "cat-file", "--batch"],
        input="".join(blob + "\n" for _, _, blob in entries).encode(), capture_output=True, timeout=120,
    )
    if result.returncode:
        raise ContractError("reading fixed C# test Git objects failed")
    contents, offset = {}, 0
    for name, mode, blob in entries:
        end = result.stdout.index(b"\n", offset)
        header = result.stdout[offset:end].decode().split()
        if len(header) != 3 or header[:2] != [blob, "blob"]:
            raise ContractError("fixed C# test Git object response differs")
        size = int(header[2])
        data = result.stdout[end + 1:end + 1 + size]
        if len(data) != size or result.stdout[end + 1 + size:end + 2 + size] != b"\n":
            raise ContractError("fixed C# test Git object is truncated")
        offset = end + 2 + size
        contents[name] = {"bytes": data, "git_blob": blob, "git_mode": mode}
    return contents


def curate_harness(directory, root, task, spec, plan, *, repository=None):
    """Copy only the original grader, never its old production checkout/output."""
    records = {}
    if plan["kind"] == "console":
        source = directory / "oracle"
        if not (source / "Oracle.csproj").is_file():
            raise ContractError("original private ProjectReference harness is absent")
        for name, path in tree_files(source).items():
            if any(part in {"bin", "obj"} for part in Path(name).parts):
                continue
            if path.is_symlink():
                raise ContractError("private assertion source must be a regular file")
            target = root / "oracle" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            records["oracle/" + name] = {"original_sha256": file_hash(path), "sha256": file_hash(target)}
        text = (root / "oracle/Oracle.csproj").read_text(encoding="utf-8-sig")
        for project in plan["production"]:
            if "../repo/" + project not in text:
                raise ContractError("fixed Oracle does not reference the current production project")
        if plan.get("service"):
            program = root / "oracle/Program.cs"
            program.write_text(remap_service_program(program.read_text(), plan["service"], task["id"]))
            records["oracle/Program.cs"]["sha256"] = file_hash(program)
            records["oracle/Program.cs"]["change"] = "private service transport/fixture credential literals only"
        (root / "oracle/Directory.Build.props").write_text("<Project />\n")
        (root / "oracle/Directory.Build.targets").write_text("<Project />\n")
    else:
        source = directory / "repo" / plan["test_root"]
        if repository is None:
            # Standalone synthetic unit fixtures have no task start commit.
            if task.get("start_sha"):
                raise ContractError("real C# private tests require their exact-start Git repository")
            inputs = {}
            for name, path in tree_files(source).items():
                if any(part in {"bin", "obj", ".vs"} for part in Path(name).parts):
                    continue
                if path.is_symlink():
                    raise ContractError("private test fixture must be a regular file")
                inputs[plan["test_root"] + "/" + name] = {
                    "bytes": path.read_bytes(), "git_blob": None,
                    "git_mode": "100755" if path.stat().st_mode & 0o111 else "100644",
                }
        else:
            inputs = start_test_inputs(repository, task["start_sha"], plan["test_root"])
        if plan["project"] not in inputs:
            raise ContractError("original full private test project is absent")
        for relative, item in inputs.items():
            target = root / "tests" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(item["bytes"])
            target.chmod(0o755 if item["git_mode"] == "100755" else 0o644)
            retained = directory / "repo" / relative
            retained_sha = file_hash(retained) if retained.is_file() and not retained.is_symlink() else None
            digest = file_hash(target)
            records[relative] = {
                "original_sha256": digest, "sha256": digest, "retained_sha256": retained_sha,
                "git_blob": item["git_blob"], "git_mode": item["git_mode"],
                "git_commit": task.get("start_sha"), "retained_differed": retained_sha != digest,
                "authority": "exact-start-git" if repository is not None else "synthetic-fixture",
            }
        setup = plan.get("private_setup")
        if setup:
            adapter = directory / setup["adapter"]
            if (spec.get("host_adapter") != setup["adapter"] or adapter.is_symlink()
                    or not adapter.is_file() or file_hash(adapter) != setup["sha256"]):
                raise ContractError("C# declared private setup adapter bytes changed")
            if records[setup["helper"]]["sha256"] != setup["helper_sha256"]:
                raise ContractError("C# declared private setup helper differs from its fixed input")
            shutil.copy2(adapter, root / "private-setup.sh")
    # The fixed selector/test command is retained verbatim, without selection edits.
    atomic_json(root / "fixed-harness.json", {
        "policy": POLICY, "files": records, "test_command": spec["test_command"],
        "pass_to_pass_tests": spec["pass_to_pass_tests"], "plan": plan,
    })
    return records


def private_setup_command(plan, *, runtime=RUNTIME, private=PRIVATE):
    """Replay only the hash-bound, declared test setup in a private copy.

    The predicate reads the current candidate API. There is no phase/variant
    input, and the candidate production file must remain byte-identical.
    """
    setup = plan["private_setup"]
    helper = shlex.quote(runtime + "/repo/" + setup["helper"])
    source = shlex.quote(runtime + "/repo/" + setup["predicate_source"])
    script = shlex.quote(private + "/private-setup.sh")
    return (
        "set -euo pipefail; "
        "test \"$(sha256sum " + script + " | cut -d' ' -f1)\" = " + setup["sha256"] + "; "
        "test \"$(sha256sum " + helper + " | cut -d' ' -f1)\" = " + setup["helper_sha256"] + "; "
        "before=$(sha256sum " + source + "); "
        "REPO_DIR=" + shlex.quote(runtime + "/repo") + " bash " + script + "; "
        "test \"$before\" = \"$(sha256sum " + source + ")\"; "
        "after=$(sha256sum " + helper + " | cut -d' ' -f1); "
        "case \"$after\" in " + setup["helper_sha256"] + "|" + setup["effective_helper_sha256"] +
        ") ;; *) echo 'C# private setup output differs from the declared Oracle input' >&2; exit 2;; esac; "
        "printf 'CSHARP_PRIVATE_SETUP adapter=" + setup["sha256"] + " helper=%s\\n' \"$after\""
    )


def build_commands(spec, plan, *, runtime=RUNTIME):
    """Retain the historical restore/build options, adding bounded offline behavior."""
    original = spec["prepare_command"].split(" && ")
    if len(original) != 2:
        raise ContractError("C# fixed preparation needs exactly restore then build")
    restore, build = original
    if not restore.startswith("dotnet restore " + plan["project"]) or not build.startswith(
        "dotnet build " + plan["project"]
    ):
        raise ContractError("C# fixed preparation project differs")
    # GraphQL's exact local .nupkg is part of the hash-bound offline feed.
    restore = re.sub(r" --source \S+", "", restore)
    restore += " --disable-parallel -p:NuGetAudit=false"
    build += " -p:NuGetAudit=false -m:1 -p:UseSharedCompilation=false -nr:false"
    return restore, build


def dependency_manifest(ctx, task):
    root = Path(ctx.config.get("migration", {}).get("csharp_dependency_root", ctx.output.parent / "dependencies"))
    path = root / component(task["id"]) / "dependencies.json"
    if not path.is_file():
        raise ContractError("C# hash-bound dependencies need preparation: " + str(path))
    record = json.loads(path.read_text())
    if record.get("task_id") != task["id"] or record.get("start_sha") != task["start_sha"] or record.get("policy") != POLICY:
        raise ContractError("C# dependency manifest belongs to different input")
    for name in ("tool", "packages"):
        item = record[name]
        source = Path(item["path"])
        if source.is_symlink() or not source.is_file() or file_hash(source) != item["sha256"]:
            raise ContractError("C# prepared dependency descriptor changed: " + name)
    return path, record


def project_completion_pattern(directory, spec, oracle, plan):
    """Bind native summaries to the whole frozen test set, not a prefix-only regex."""
    from .csharp_lane import completion_pattern
    original = completion_pattern(directory, spec, oracle)
    if plan["kind"] == "console":
        return original
    count = str(oracle["start_signature"])
    if not re.fullmatch(r"[1-9]\d*", count) or str(oracle["golden_signature"]) != count:
        raise ContractError("C# frozen native suite count changed")
    summary = (r"^Passed!\s+-\s*Failed:\s*(\d+),\s*Passed:\s*(\d+),\s*Skipped:\s*(\d+),"
               r"\s*Total:\s*(\d+),\s*Duration:\s+.+\s+-\s+([^\r\n]+)$")
    suffixes = set()
    for variant in ("start", "golden"):
        for repetition in (1, 2, 3):
            text = (directory / f"{variant}-{repetition}.log").read_text(errors="replace")
            rows = re.findall(summary, text, re.MULTILINE)
            if len(rows) != 1 or rows[0][:4] != ("0", count, "0", count):
                raise ContractError("C# frozen native summary has failed/skipped/missing tests")
            suffixes.add(rows[0][4].strip())
    if len(suffixes) != 1:
        raise ContractError("C# frozen native test assembly/framework changed")
    return (r"^Passed!\s+-\s*Failed:\s*0,\s*Passed:\s*" + count +
            r",\s*Skipped:\s*0,\s*Total:\s*" + count + r",\s*Duration:\s+.+\s+-\s+" +
            re.escape(suffixes.pop()) + r"\s*$")


def exact_submodule_asset(ctx, directory, module):
    """Bind the original gitlink using the frozen engine's submodule asset role.

    An older preparation descriptor with the generic dependency role is not
    relabelled. Re-extract its exact declared commit and create a new immutable
    source descriptor; no historical proof or descriptor is edited.
    """
    from reviewbench_maintainability.agentic.environment import resolve_asset
    from reviewbench_maintainability.agentic.snapshots import extract_tar, start_archive
    original = resolve_asset(module["asset"], ctx.output)
    descriptor = json.loads(original.read_text())
    if descriptor.get("git_commit") != module["commit"]:
        raise ContractError("C# submodule asset lacks the declared Git commit")
    if descriptor.get("source_kind") == "submodule":
        return module["asset"]
    if descriptor.get("source_kind") != "dependency":
        raise ContractError("C# submodule asset has an unexpected role")
    retained = directory / "repo" / repo_path(module["path"])
    if git(retained, "cat-file", "-t", module["commit"]).decode().strip() != "commit":
        raise ContractError("exact C# submodule commit is absent")
    with tempfile.TemporaryDirectory(prefix="csharp-submodule-bind-", dir=ctx.scratch) as temporary:
        root = Path(temporary) / "source"
        with start_archive(retained, module["commit"]) as stream:
            extract_tar(stream, root)
        return ctx.bundle(root, role="submodule", git_commit=module["commit"])


def migrate_project(ctx, task, oracle, spec, directory):
    from .csharp_graphql3270 import requested_sdk6_profile
    options = requested_sdk6_profile(ctx.config, task, oracle)
    if options is not None:
        from .csharp_graphql3270_sdk6 import migrate
        return migrate(ctx, task, oracle, spec, directory, options, build_sdk8=_migrate_project_sdk8)
    return _migrate_project_sdk8(ctx, task, oracle, spec, directory)


def _migrate_project_sdk8(ctx, task, oracle, spec, directory):
    plan = plan_for(oracle["index"], spec)
    fixed_inputs = None
    if plan.get("fixed_input_profile") == "graphql3270/v1":
        from .csharp_graphql3270 import bind_original_inputs
        fixed_inputs = bind_original_inputs(directory, task, oracle, spec)
    manifest_path, deps = dependency_manifest(ctx, task)
    image, audit = ctx.image(plan["sdk"])
    registry_path = Path(ctx.config.get("migration", {}).get("toolchain_registry", ctx.output / "toolchains.json"))
    version = sdk_version(json.loads(registry_path.read_text()), plan["sdk"])
    if deps["image"] != image or deps["sdk_version"] != version:
        raise ContractError("C# prepared dependencies use a different pinned SDK")
    expected = oracle.get("pass_to_pass_tests")
    if not expected or expected != spec["pass_to_pass_tests"]:
        raise ContractError("C# frozen test selectors differ")
    with tempfile.TemporaryDirectory(prefix="csharp-project-harness-", dir=ctx.scratch) as temporary:
        curated = Path(temporary)
        hashes = curate_harness(directory, curated, task, spec, plan, repository=ctx.repo(task))
        (curated / "global.json").write_text(json.dumps({"sdk": {"version": version, "rollForward": "disable"}}))
        harness = ctx.bundle(curated, role="grader")
    modules = ctx.modules(task)
    if plan.get("module"):
        found = next((item for item in modules if item["path"] == plan["module"]), None)
        module = deps.get("module")
        if (not found or not module or module.get("path") != plan["module"]
                or module["commit"] != found["commit"]):
            raise ContractError("C# exact candidate submodule dependency is absent")
        module_asset = exact_submodule_asset(ctx, directory, module)
        modules = [({"path": item["path"], "commit": item["commit"], "asset": module_asset}
                    if item["path"] == plan["module"] else item) for item in modules]
    sidecars = []
    if plan.get("service") == "postgres":
        sidecars = [{"name": "reviewbench-postgres", "image": deps["service_image"],
                     "env": {"POSTGRES_HOST_AUTH_METHOD": "trust"},
                     "health_command": "pg_isready -U postgres -d postgres"}]
    elif plan.get("service") == "sql":
        sidecars = [{"name": "reviewbench-sql", "image": deps["service_image"],
                     "env": {"ACCEPT_EULA": "Y", "MSSQL_PID": "Developer",
                             "MSSQL_SA_PASSWORD": service_password(task["id"])},
                     "health_command": "bash -c 'echo > /dev/tcp/127.0.0.1/1433'"}]
    stage = "dotnet " + TOOLS + "/CandidateStage.dll /workspace " + RUNTIME + "/repo " + shlex.quote(version)
    stage += " " + shlex.quote(",".join(plan["sdk_roots"]))
    grade = [
        "mkdir -p " + RUNTIME + " && cp -R " + PRIVATE + "/. " + RUNTIME + " && " + stage,
        "mkdir -p /workspace/.reviewbench/nuget && cp -R " + PACKAGES + "/packages/. /workspace/.reviewbench/nuget/",
    ]
    if plan["kind"] == "tests":
        # The overlay contains full tests/fixtures only; production remains the candidate.
        grade.append("cp -R " + PRIVATE + "/tests/. " + RUNTIME + "/repo/")
    if plan.get("private_setup"):
        grade.append(private_setup_command(plan))
    if deps.get("auxiliary"):
        # These precise source-declared tool files are not production sources.
        for name, digest in deps["auxiliary"].items():
            if plan.get("auxiliary", {}).get(name) != digest:
                raise ContractError("C# auxiliary tool differs from the fixed project declaration")
        grade.append("cp -R " + PACKAGES + "/workspace/. " + RUNTIME + "/repo/")
    workdir = RUNTIME + ("/repo" if plan["kind"] == "tests" else "")
    restore, build = build_commands(spec, plan)
    grade.append("cd " + workdir + " && " + restore + " --source " + PACKAGES + "/feed && " + build)
    dev_checks = ["dotnet /opt/reviewbench-tools/csharp-syntax/CSharpSyntax.dll " +
                  shlex.quote("/workspace/" + repo_path(task["locality"]["path"]))]
    toolchain_evidence = None
    if plan.get("public_project_build"):
        from .csharp_graphql3270 import public_build_command, toolchain_attestation
        dev_checks = [public_build_command(plan, version, tools=TOOLS, packages=PACKAGES)]
        toolchain_evidence = toolchain_attestation(
            json.loads(registry_path.read_text()), plan["sdk"], version,
        )
    golden = ctx.golden(task, oracle, "csharp", directory)
    if fixed_inputs and golden["sha256"] != fixed_inputs["source-only-golden.patch"]["sha256"]:
        raise ContractError("GraphQL3270 resolved golden differs from the fixed original patch")
    recipe = RuntimeRecipe(
        task_id=task["id"], repo=task["repo"], start_sha=task["start_sha"], lane="csharp",
        image=image, image_audit=audit, cpus=4, memory_mb=8192, build_scratch_mb=12288,
        command_timeout_seconds=spec.get("timeout", 2400), submodules=modules, sidecars=sidecars,
        env={"TMPDIR": "/workspace/.reviewbench/tmp", "DOTNET_CLI_HOME": "/workspace/.reviewbench/dotnet-home",
             "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1", "DOTNET_NOLOGO": "1",
             "DOTNET_ROLL_FORWARD": "Major", "DOTNET_PROCESSOR_COUNT": "4",
             "NUGET_PACKAGES": "/workspace/.reviewbench/nuget",
             "NUGET_HTTP_CACHE_PATH": "/workspace/.reviewbench/nuget-http",
             "MSBUILDDISABLENODEREUSE": "1", "CI": "true", **spec.get("environment", {})},
        assets=[{**harness, "kind": "archive", "visibility": "grader", "target": PRIVATE},
                {**deps["tool"], "kind": "archive", "visibility": "developer", "target": TOOLS},
                {**deps["packages"], "kind": "archive", "visibility": "developer", "target": PACKAGES}],
        bootstrap=["mkdir -p /workspace/.reviewbench/tmp /workspace/.reviewbench/dotnet-home"],
        dev_checks=dev_checks,
        grading={"multifile_build": True, "immutable_paths": ctx.immutable_tests(task),
                 "golden_patch": golden, "bootstrap": grade,
                 "commands": [{"command": "cd " + workdir + " && " + spec["test_command"],
                               "parser": "regex", "expected_tests": expected,
                               "success_pattern": project_completion_pattern(directory, spec, oracle, plan)}]},
    )
    recipe.validate()
    atomic_json(ctx.output / "csharp-plans" / (component(task["id"]) + ".json"), {
        "task_id": task["id"], "policy": POLICY, "plan": plan, "sdk_version": version,
        "dependencies": {"path": str(manifest_path), "sha256": file_hash(manifest_path)},
        "harness_files": hashes, "runtime_fingerprint": recipe.fingerprint,
        "production_source_rewrites": [],
        "private_setup_adapter": plan.get("private_setup"),
        "legacy_production_rewrite_adapters_executed": False,
        "dependency_preparation_status": deps.get("status"),
        **({"original_input_bindings": fixed_inputs, "toolchain_attestation": toolchain_evidence,
            "public_check": "full current production ProjectReference project build"}
           if fixed_inputs else {}),
        "readiness": "requires actual current-engine start/golden triple replay",
    })
    return recipe
