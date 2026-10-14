"""Original SqlClient16 full-project SDK6 workflow and dedicated SQL service."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shlex
import shutil
import tempfile

from reviewbench_maintainability.agentic.assets import asset_descriptor
from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, atomic_json, canonical_hash, component, file_hash,
)
from .csharp_candidate import remap_service_program, service_password
from .csharp_lane import completion_pattern
from .csharp_original_cli import checked
from .csharp_sqlclient_original_entry import SDK, VERSION, CSC, CACHE, SETUP, TARGET

TOOLS = "/opt/reviewbench-assets/original-sqlclient"
STAGER = "/opt/reviewbench-assets/original-sqlclient-stage"
PACKAGES = "/opt/reviewbench-assets/original-sqlclient-packages"
PRIVATE = "/opt/reviewbench-assets/original-sqlclient-tests"
IMAGE = "sha256:e52f047393b4186864e293ed466ffa90e633482c94aa5bc7c67876ea47692188"
SERVER = "sha256:a64d04da3dedcc1c03ea93e4105cd4702790d97076cef5e6e55c9d31144b3f63"
SERVER_PARENT = "sha256:90488d58c6a5c19f24ff716e14330b85b5b26ee54b44a36ea24e6206533e7edd"
SERVER_BINARY = "6c3337173260b8df7a6fc3029c04dd22ed14db7be1f7fb361104af847496fa6a"
HEALTH = "bash -c 'echo > /dev/tcp/127.0.0.1/1433'"
SERVER_CPU_AWK = (
    '{n=0; for(i=1;i<=NF && n<2;i++) {split($i,r,"-"); '
    'last=(r[2]=="" ? r[1] : r[2]); for(c=r[1];c<=last && n<2;c++) '
    '{printf "%s%d",(n ? "," : ""),c; n++}} print ""}'
)
SERVER_COMMAND = [
    "bash", "-e", "-o", "pipefail", "-c",
    "cpus=$(awk '/^Cpus_allowed_list:/{print $2}' /proc/self/status | awk -F, "
    + shlex.quote(SERVER_CPU_AWK)
    + '); test -n "$cpus"; exec taskset -c "$cpus" /opt/mssql/bin/sqlservr',
]
SERVER_MEMORY_MB = 2048
PROFILE = {
    "repo": "dotnet/SqlClient",
    "id": "github__dotnet__SqlClient__pull_1608__comment_870881699__readability",
    "start": "50245ab962edbfb076ee254e5101217ca77aa3c6",
    "spec": "b1b06fa878e44a6a77db2d15471795802259e2c5bfd493067e9ed341b61e2b51",
    "golden": "daf09fc235e1d6e33e7df9ef4104d73f5c0efa8b1bab1f07055e998d9f8ff477",
    "project": "src/Microsoft.Data.SqlClient/netcore/src/Microsoft.Data.SqlClient.csproj",
    "files": {
        "oracle/Oracle.csproj": "04a8425888fdeb2a30b99b2294c738786de102628c5a394dcc53868d5f75470c",
        "oracle/Program.cs": "43e73b1e38ae19789fd4c58eac920136fe37a2e19d56080072fe5d6dd5632491",
        "start-mssql.sh": "1a22ad5b92ff5925e3b07070bf160f57337c9e312deb8472e669051224477a00",
    },
}


def bind_original(task, oracle, spec, directory):
    if (oracle.get("index") != 16 or task.get("repo") != PROFILE["repo"]
            or task.get("id") != PROFILE["id"] or task.get("start_sha") != PROFILE["start"]
            or canonical_hash(spec) != PROFILE["spec"]
            or oracle.get("evidence", {}).get("source_only_patch_sha256") != PROFILE["golden"]):
        raise ContractError("original SqlClient needs the complete task/spec/golden binding")
    for name, sha256 in PROFILE["files"].items():
        p = directory / name
        if p.is_symlink() or not p.is_file() or file_hash(p) != sha256:
            raise ContractError("original SqlClient harness/setup changed: " + name)
    if file_hash(directory / "source-only-golden.patch") != PROFILE["golden"] or spec["pass_to_pass_tests"] != oracle["pass_to_pass_tests"]:
        raise ContractError("original SqlClient golden or test identities changed")


def original_setup_body(shell):
    """Use only the original source-fix Python; service lifecycle belongs to the runner."""
    prefix = 'python - "$REPO_DIR/' + TARGET + '" <<\'PY\'\n'
    if shell.count(prefix) != 1:
        raise ContractError("original SqlClient setup boundary changed")
    body = shell.split(prefix, 1)[1].split("\nPY\n", 1)[0] + "\n"
    if hashlib.sha256(body.encode()).hexdigest() != SETUP:
        raise ContractError("original SqlClient source-fix body changed")
    return body


def original_commands(spec):
    parts = spec["prepare_command"].split(" && ")
    if len(parts) != 2:
        raise ContractError("original SqlClient prepare boundary changed")
    restore, build = parts
    if not restore.startswith("dotnet restore oracle/Oracle.csproj ") or not build.startswith("dotnet build oracle/Oracle.csproj "):
        raise ContractError("original SqlClient full project boundary changed")
    restore += " --source " + PACKAGES + "/feed --disable-parallel -p:NuGetAudit=false"
    build += " -m:1 -p:UseSharedCompilation=false -nr:false -p:NuGetAudit=false"
    private = [c.replace("dotnet ", SDK + "/dotnet ", 1) for c in (restore, build)]
    public = [c.replace("oracle/Oracle.csproj", "repo/" + PROFILE["project"], 1) for c in private]
    return public, private


def inputs(ctx, task):
    refs = ctx.config.get("migration", {}).get("original_sqlclient", {})
    deps = checked(refs.get("dependencies"), "SqlClient original NuGet graph")
    readback = checked(refs.get("readback"), "SqlClient dependency readback")
    host = checked(refs.get("host"), "SqlClient SDK6 host")
    image = checked(refs.get("image"), "SqlClient SDK6 image recovery")
    service = checked(refs.get("server"), "SqlClient dedicated server recovery")
    service_host = checked(refs.get("server_host"), "SqlClient byte-identical server host")
    preflight = checked(refs.get("preflight"), "SqlClient dedicated server preflight")
    evidence = checked(refs.get("original_server_evidence"), "original dedicated-server evidence")
    if (deps.get("status") != "original_CSharp_dependency_graphs_prepared"
            or deps.get("task_id") != task["id"] or deps.get("start_sha") != task["start_sha"]
            or deps.get("image") != IMAGE or deps.get("sdk_version") != VERSION
            or not deps.get("original_source_unchanged") or deps.get("project_build_test_model_judge_calls") != 0
            or len(deps.get("restore_attempts", [])) != 2
            or any(r["exit_status"] != 0 for r in deps["restore_attempts"])):
        raise ContractError("original SqlClient dependencies are incomplete or relabelled")
    if (readback.get("status") != "independent_Blob_component_readback_verified"
            or readback.get("asset") != deps["packages"] or not readback.get("all_paths_bytes_full_modes_verified")
            or not readback.get("gzip_crc_and_eof_verified")):
        raise ContractError("original SqlClient dependency readback missing")
    if (host.get("image") != IMAGE or host.get("original_sdk_version") != VERSION
            or host.get("original_csc_sha256") != CSC or not host.get("original_sdk6_compiler_unchanged")
            or host.get("version_check", {}).get("exit_status") != 0
            or image.get("image") != IMAGE or not image.get("all_layers_match")
            or not image.get("gzip_crc_and_eof_verified") or image.get("load_exit_status") != 0):
        raise ContractError("original SqlClient compiler/image identity changed")
    if (service.get("status") != "independent_Blob_pinned_SQL_service_image_verified"
            or service.get("image") != SERVER or not service.get("all_layers_match")
            or not service.get("gzip_crc_and_eof_verified") or service.get("load_exit_status") != 0
            or service_host.get("status") != "SQL_service_file_capability_removed_binary_unchanged"
            or service_host.get("image") != SERVER or service_host.get("parent_image") != SERVER_PARENT
            or service_host.get("original_binary_sha256") != SERVER_BINARY
            or service_host.get("effective_binary_sha256") != SERVER_BINARY
            or not service_host.get("original_binary_bytes_unchanged")
            or not service_host.get("original_config_preserved") or not service_host.get("parent_layers_preserved")
            or service_host.get("extra_runtime_capabilities") != [] or service_host.get("volumes") != []
            or service_host.get("non_root_user") != "mssql"
            or service_host.get("removed_xattr") != "security.capability:cap_net_bind_service=ep"
            or preflight.get("status") != "pinned_dedicated_SQL_service_preflight_verified"
            or preflight.get("image") != SERVER or preflight.get("health_command") != HEALTH
            or preflight.get("server_command") != SERVER_COMMAND
            or preflight.get("server_memory_limit_mb") != SERVER_MEMORY_MB
            or not preflight.get("observed", {}).get("healthy") or not preflight.get("owned_container_removed")
            or preflight.get("observed", {}).get("anonymous_or_host_volumes") != 0
            or preflight.get("new_SUT_model_judge_calls") != 0
            or evidence.get("status") != "original_SQL_external_server_setup_evidence_correlated"
            or evidence.get("task_id") != task["id"] or len(evidence.get("matching_local_remote_artifacts", [])) != 16):
        raise ContractError("original dedicated SQL scenario lacks its complete evidence/host binding")
    if canonical_hash(deps["package_index"]) != deps["package_index_sha256"]:
        raise ContractError("original SqlClient package index changed")
    for key in ("packages", "tool"):
        if asset_descriptor(deps[key], ctx.output)["source_kind"] != "dependency":
            raise ContractError("SqlClient public dependency contains reference material")
    return deps, refs


def migrate(ctx, task, oracle):
    if oracle.get("index") != 16 or task.get("repo") != PROFILE["repo"]:
        return None
    spec = json.loads((ctx.legacy / "csharp/specs.json").read_text())["16"]
    directory = ctx.task_directory(task, oracle, "csharp")
    bind_original(task, oracle, spec, directory)
    deps, refs = inputs(ctx, task)
    image, audit = ctx.image(IMAGE)
    if image != IMAGE:
        raise ContractError("SqlClient SDK6 host identity changed")
    public, private = original_commands(spec)
    setup_file = ctx.output / "original-sqlclient/setup.py"
    setup_file.parent.mkdir(parents=True, exist_ok=True)
    setup_file.write_text(original_setup_body((directory / "start-mssql.sh").read_text()))
    with tempfile.TemporaryDirectory(prefix="original-sqlclient-harness-", dir=ctx.scratch) as temporary:
        root = Path(temporary)
        (root / "oracle").mkdir()
        shutil.copy2(directory / "oracle/Oracle.csproj", root / "oracle/Oracle.csproj")
        original_program = (directory / "oracle/Program.cs").read_text()
        remapped = remap_service_program(original_program, "sql", task["id"])
        (root / "oracle/Program.cs").write_text(remapped)
        for name in ("Directory.Build.props", "Directory.Build.targets"):
            (root / "oracle" / name).write_text("<Project />\n")
        harness = ctx.bundle(root, role="grader")
        effective_program_sha256 = file_hash(root / "oracle/Program.cs")
    runtime_spec = {"schema_version": "reviewbench-original-sqlclient/v1", "profile": "sqlclient16",
                    "packages": PACKAGES, "packages_asset_sha256": deps["packages"]["sha256"],
                    "tool": STAGER, "private": PRIVATE, "setup": TOOLS + "/setup.py",
                    "public_commands": public, "private_commands": private}
    config_file = ctx.output / "original-sqlclient" / (component(task["id"]) + "-spec.json")
    atomic_json(config_file, runtime_spec)
    assets = [
        {**deps["packages"], "kind": "archive", "visibility": "developer", "target": PACKAGES},
        {**deps["tool"], "kind": "archive", "visibility": "developer", "target": STAGER},
        {**ctx.copy_asset(Path(__file__).with_name("csharp_sqlclient_original_entry.py"), "original-sqlclient"),
         "kind": "file", "visibility": "developer", "target": TOOLS + "/entry.py"},
        {**ctx.copy_asset(Path(__file__).with_name("csharp_original_cli_entry.py"), "original-sqlclient"),
         "kind": "file", "visibility": "developer", "target": TOOLS + "/csharp_original_cli_entry.py"},
        {**ctx.copy_asset(setup_file, "original-sqlclient"), "kind": "file", "visibility": "developer", "target": TOOLS + "/setup.py"},
        {**ctx.copy_asset(config_file, "original-sqlclient"), "kind": "file", "visibility": "developer", "target": TOOLS + "/spec.json"},
        {**harness, "kind": "archive", "visibility": "grader", "target": PRIVATE},
    ]
    command = shlex.join(["python3", TOOLS + "/entry.py", "--spec", TOOLS + "/spec.json", "--workspace", "/workspace"])
    recipe = RuntimeRecipe(
        task_id=task["id"], repo=task["repo"], start_sha=task["start_sha"], lane="csharp",
        image=image, image_audit=audit, cpus=2, memory_mb=4096, build_scratch_mb=4096,
        command_timeout_seconds=spec["timeout"], submodules=ctx.modules(task), assets=assets,
        cache_mounts={"original-sqlclient": CACHE},
        sidecars=[{"name": "reviewbench-sql", "image": SERVER, "health_command": HEALTH,
                   "command": SERVER_COMMAND,
                   "env": {"ACCEPT_EULA": "Y", "MSSQL_PID": "Developer",
                           "MSSQL_MEMORY_LIMIT_MB": str(SERVER_MEMORY_MB),
                           "MSSQL_SA_PASSWORD": service_password(task["id"])}}],
        env={"TMPDIR": "/workspace/.reviewbench/tmp", "DOTNET_CLI_HOME": "/workspace/.reviewbench/dotnet-home",
             "PATH": SDK + ":/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
             "DOTNET_ROOT": SDK, "DOTNET_ROOT_X64": SDK, "DOTNET_HOST_PATH": SDK + "/dotnet",
             "DOTNET_MULTILEVEL_LOOKUP": "0", "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
             "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1", "DOTNET_NOLOGO": "1", "DOTNET_PROCESSOR_COUNT": "2",
             "NUGET_PACKAGES": CACHE + "/nuget", "MSBUILDDISABLENODEREUSE": "1", "CI": "true"},
        bootstrap=["mkdir -p /workspace/.reviewbench/tmp /workspace/.reviewbench/dotnet-home"],
        dev_checks=[command + " --mode public"],
        grading={"multifile_build": True, "immutable_paths": ctx.immutable_tests(task),
                 "golden_patch": ctx.golden(task, oracle, "csharp", directory),
                 "bootstrap": [command + " --mode grade-build"],
                 "commands": [{"command": "cd " + CACHE + "/grading && " + spec["test_command"].replace("dotnet ", SDK + "/dotnet ", 1),
                               "parser": "regex", "expected_tests": oracle["pass_to_pass_tests"],
                               "success_pattern": completion_pattern(directory, spec, oracle)}]},
    )
    recipe.validate()
    atomic_json(ctx.output / "original-sqlclient/binding.json", {
        "id": task["id"], "full_scope": 402, "input_references": refs,
        "original_spec_sha256": PROFILE["spec"], "original_files": PROFILE["files"],
        "original_golden_sha256": PROFILE["golden"], "original_setup_python_sha256": SETUP,
        "effective_program_sha256": effective_program_sha256,
        "harness_changes": ["dedicated service endpoint", "task-specific fixture credential"],
        "assertion_changes": [], "original_root_source_changes": [], "golden_changes": [],
        "sdk": VERSION, "compiler_sha256": CSC, "server_image": SERVER,
        "server_parent_image": SERVER_PARENT, "server_binary_sha256": SERVER_BINARY,
        "server_content_changes": [], "server_removed_file_capability": "cap_net_bind_service=ep",
        "server_cpu_affinity_count": 2, "server_memory_limit_mb": SERVER_MEMORY_MB,
        "historical_whole_image_identity_claimed": False, "unrelated_live_server_used": False,
        "effective_public_commands": public, "effective_private_commands": private,
    })
    return recipe
