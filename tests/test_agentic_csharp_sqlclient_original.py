import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agentic_runtime_migration import csharp_sqlclient_original as adapter
from agentic_runtime_migration import csharp_sqlclient_original_entry as entry
from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash


SETUP_BODY = '''from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()
fixed = text.replace("_isTDSS &&", "_isTDS8 &&")
if fixed != text:
    path.write_text(fixed)
    print("corrected the exact-start _isTDSS identifier typo")
'''


def digest(data):
    return hashlib.sha256(data).hexdigest()


def test_unrelated_tasks_keep_their_existing_routes():
    assert adapter.migrate(None, {"repo": "dotnet/roslyn"}, {"index": 19}) is None
    assert adapter.migrate(None, {"repo": "another/repo"}, {"index": 16}) is None


@pytest.fixture
def original_binding(tmp_path, monkeypatch):
    project = tmp_path / "oracle/Oracle.csproj"
    project.parent.mkdir()
    project.write_text("<Project><ProjectReference Include='../repo/original.csproj' /></Project>")
    program = project.with_name("Program.cs")
    program.write_text("original assertions")
    setup = tmp_path / "start-mssql.sh"
    setup.write_text("original host setup")
    golden = tmp_path / "source-only-golden.patch"
    golden.write_text("original source patch")
    task = {"id": "original-task", "repo": "dotnet/SqlClient", "start_sha": "original-start"}
    spec = {"prepare_command": "original", "pass_to_pass_tests": ["original-test"]}
    profile = {
        **adapter.PROFILE, "id": task["id"], "start": task["start_sha"],
        "spec": canonical_hash(spec), "golden": digest(golden.read_bytes()),
        "files": {p.relative_to(tmp_path).as_posix(): digest(p.read_bytes())
                  for p in (project, program, setup)},
    }
    oracle = {"index": 16, "evidence": {"source_only_patch_sha256": profile["golden"]},
              "pass_to_pass_tests": spec["pass_to_pass_tests"].copy()}
    monkeypatch.setattr(adapter, "PROFILE", profile)
    return task, oracle, spec, tmp_path


def test_complete_original_binding_is_accepted(original_binding):
    adapter.bind_original(*original_binding)


@pytest.mark.parametrize("field", ["id", "start_sha", "repo", "index", "spec", "golden"])
def test_complete_task_spec_golden_bindings_are_not_relabelled(original_binding, field):
    task, oracle, spec, root = original_binding
    if field in task:
        task[field] = "different"
    elif field == "index":
        oracle["index"] = 19
    elif field == "spec":
        spec["prepare_command"] = "easier"
    else:
        oracle["evidence"]["source_only_patch_sha256"] = "0" * 64
    with pytest.raises(ContractError, match="complete task/spec/golden"):
        adapter.bind_original(task, oracle, spec, root)


@pytest.mark.parametrize("name", ["oracle/Oracle.csproj", "oracle/Program.cs", "start-mssql.sh"])
@pytest.mark.parametrize("mutation", ["bytes", "symlink"])
def test_original_harness_and_setup_are_hash_bound(original_binding, name, mutation):
    task, oracle, spec, root = original_binding
    path = root / name
    if mutation == "bytes":
        path.write_text("different")
    else:
        target = root / "replacement"
        target.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(target)
    with pytest.raises(ContractError, match="harness/setup changed"):
        adapter.bind_original(task, oracle, spec, root)


@pytest.mark.parametrize("mutation", ["golden_bytes", "test_id"])
def test_golden_bytes_and_original_test_id_are_preserved(original_binding, mutation):
    task, oracle, spec, root = original_binding
    if mutation == "golden_bytes":
        (root / "source-only-golden.patch").write_text("replacement")
    else:
        oracle["pass_to_pass_tests"] = ["different-test"]
    with pytest.raises(ContractError, match="golden or test identities"):
        adapter.bind_original(task, oracle, spec, root)


def test_original_command_options_and_whole_project_are_retained():
    restore = "dotnet restore oracle/Oracle.csproj -p:NoWarn=4014%3B1592 --nologo -v minimal"
    build = "dotnet build oracle/Oracle.csproj --no-restore -p:NoWarn=4014%3B1592 --nologo -v minimal"
    public, private = adapter.original_commands({"prepare_command": restore + " && " + build})
    assert len(public) == len(private) == 2
    assert private[0].startswith(restore.replace("dotnet ", entry.SDK + "/dotnet ", 1))
    assert private[1].startswith(build.replace("dotnet ", entry.SDK + "/dotnet ", 1))
    assert public == [p.replace("oracle/Oracle.csproj", "repo/" + adapter.PROFILE["project"], 1) for p in private]
    assert all("-p:NoWarn=4014%3B1592" in p for p in public + private)
    assert all("-p:TargetFramework=" not in p and "TreatWarningsAsErrors" not in p
               and "RunAnalyzers" not in p for p in public + private)
    assert "--source " + adapter.PACKAGES + "/feed" in private[0]
    assert "--disable-parallel" in private[0] and "-m:1" in private[1]
    assert "--no-restore" in private[1]


@pytest.mark.parametrize("command", ["a", "a && b && c",
                                   "dotnet restore substitute.csproj && dotnet build substitute.csproj"])
def test_original_full_project_boundary_is_required(command):
    with pytest.raises(ContractError):
        adapter.original_commands({"prepare_command": command})


def original_shell():
    return ('before\npython - "$REPO_DIR/' + entry.TARGET + '" <<\'PY\'\n'
            + SETUP_BODY + "PY\nunrelated original service lifecycle\n")


def test_extracts_only_the_exact_original_source_setup_body():
    assert digest(SETUP_BODY.encode()) == entry.SETUP
    assert adapter.original_setup_body(original_shell()) == SETUP_BODY


@pytest.mark.parametrize("mutation", ["boundary", "body", "duplicate"])
def test_setup_body_and_boundary_changes_are_rejected(mutation):
    shell = original_shell()
    if mutation == "boundary":
        shell = shell.replace(entry.TARGET, "another.cs")
    elif mutation == "body":
        shell = shell.replace('"_isTDS8 &&"', '"easier"')
    else:
        shell += shell
    with pytest.raises(ContractError):
        adapter.original_setup_body(shell)


@pytest.fixture
def staged_setup(tmp_path):
    root = tmp_path / "staged"
    target = root / entry.TARGET
    target.parent.mkdir(parents=True)
    target.write_text("if (_isTDSS && enabled) return;\n")
    (root / "NewHelper.cs").write_text("candidate-added production helper\n")
    helper = tmp_path / "original-setup.py"
    helper.write_text(SETUP_BODY)
    return root, target, helper


def test_exact_original_helper_mutates_only_its_staged_target(staged_setup):
    root, target, helper = staged_setup
    result = entry.apply_original_setup(root, helper)
    assert target.read_text() == "if (_isTDS8 && enabled) return;\n"
    assert (root / "NewHelper.cs").read_text() == "candidate-added production helper\n"
    assert result["root_candidate_changed"] is False
    assert result["before"] != result["after"]
    assert result["original_python_setup_sha256"] == entry.SETUP


def test_already_corrected_source_keeps_original_newline_bytes(staged_setup):
    root, target, helper = staged_setup
    original = b"if (_isTDS8 && enabled) return;\r\n"
    target.write_bytes(original)
    result = entry.apply_original_setup(root, helper)
    assert result["before"] == result["after"] and target.read_bytes() == original


@pytest.mark.parametrize("mutation", ["another_source", "new_source", "removed_source"])
def test_setup_cannot_modify_other_candidate_paths(staged_setup, mutation):
    root, target, helper = staged_setup
    def changed(command, **kwargs):
        result = subprocess.run(command, **kwargs)
        if mutation == "another_source":
            (root / "NewHelper.cs").write_text("different")
        elif mutation == "new_source":
            (root / "Extra.cs").write_text("extra")
        else:
            (root / "NewHelper.cs").unlink()
        return result
    with pytest.raises(RuntimeError, match="another candidate|candidate paths"):
        entry.apply_original_setup(root, helper, call=changed)


@pytest.mark.parametrize("mutation", ["helper_bytes", "target_symlink"])
def test_setup_rejects_changed_helper_or_redirected_target(staged_setup, mutation):
    root, target, helper = staged_setup
    if mutation == "helper_bytes":
        helper.write_text("different")
    else:
        target.unlink()
        target.symlink_to(root / "NewHelper.cs")
    with pytest.raises(RuntimeError):
        entry.apply_original_setup(root, helper)


@pytest.mark.parametrize("mode", ["public", "grade-build"])
def test_build_cache_does_not_inherit_candidate_ancestors(tmp_path, monkeypatch, mode):
    workspace = tmp_path / "candidate"
    cache = tmp_path / "cache"
    workspace.mkdir()
    cache.mkdir()
    monkeypatch.setattr(entry, "CACHE", str(cache))
    assert entry.staging_area(workspace, mode).parent == cache
    nested = workspace / "nested"
    nested.mkdir()
    monkeypatch.setattr(entry, "CACHE", str(nested))
    with pytest.raises(RuntimeError, match="independent local-disk"):
        entry.staging_area(workspace, mode)


def test_service_remap_changes_no_database_operation_or_assertion():
    program = (
        'string conn = "Server=172.17.0.1,14339;User Id=sa;Password=old-fixture;'
        'Encrypt=False;TrustServerCertificate=True;Connect Timeout=1;";\n'
        'c.Open(); Assert(c.State == Open); c.Execute("SELECT 42"); '
        'Assert(value == 42); c.Close(); Assert(c.State == Closed);'
    )
    result = adapter.remap_service_program(program, "sql", "original-task")
    expected = program.replace("172.17.0.1,14339", "reviewbench-sql,1433").replace(
        "old-fixture", adapter.service_password("original-task"))
    assert result == expected and result.count("Assert(") == 3
    assert adapter.service_password("original-task") != adapter.service_password("another-task")


@pytest.fixture
def input_records(tmp_path, monkeypatch):
    task = {"id": adapter.PROFILE["id"], "start_sha": adapter.PROFILE["start"]}
    packages = {"path": str(tmp_path / "packages.json"), "sha256": "1" * 64}
    records = {
        "dependencies": {
            "status": "original_CSharp_dependency_graphs_prepared", "task_id": task["id"],
            "start_sha": task["start_sha"], "image": adapter.IMAGE, "sdk_version": entry.VERSION,
            "original_source_unchanged": True, "project_build_test_model_judge_calls": 0,
            "restore_attempts": [{"exit_status": 0}, {"exit_status": 0}],
            "packages": packages, "tool": {"path": str(tmp_path / "tool.json"), "sha256": "2" * 64},
            "package_index": [], "package_index_sha256": canonical_hash([]),
        },
        "readback": {
            "status": "independent_Blob_component_readback_verified", "asset": packages,
            "all_paths_bytes_full_modes_verified": True, "gzip_crc_and_eof_verified": True,
        },
        "host": {
            "image": adapter.IMAGE, "original_sdk_version": entry.VERSION, "original_csc_sha256": entry.CSC,
            "original_sdk6_compiler_unchanged": True, "version_check": {"exit_status": 0},
        },
        "image": {"image": adapter.IMAGE, "all_layers_match": True,
                  "gzip_crc_and_eof_verified": True, "load_exit_status": 0},
        "server": {
            "status": "independent_Blob_pinned_SQL_service_image_verified", "image": adapter.SERVER,
            "all_layers_match": True, "gzip_crc_and_eof_verified": True, "load_exit_status": 0,
        },
        "server_host": {
            "status": "SQL_service_file_capability_removed_binary_unchanged",
            "image": adapter.SERVER, "parent_image": adapter.SERVER_PARENT,
            "original_binary_sha256": adapter.SERVER_BINARY, "effective_binary_sha256": adapter.SERVER_BINARY,
            "original_binary_bytes_unchanged": True, "original_config_preserved": True,
            "parent_layers_preserved": True, "extra_runtime_capabilities": [],
            "volumes": [], "non_root_user": "mssql",
            "removed_xattr": "security.capability:cap_net_bind_service=ep",
        },
        "preflight": {
            "status": "pinned_dedicated_SQL_service_preflight_verified",
            "image": adapter.SERVER, "health_command": adapter.HEALTH,
            "server_command": adapter.SERVER_COMMAND, "server_memory_limit_mb": adapter.SERVER_MEMORY_MB,
            "observed": {"healthy": True, "anonymous_or_host_volumes": 0},
            "owned_container_removed": True, "new_SUT_model_judge_calls": 0,
        },
        "original_server_evidence": {
            "status": "original_SQL_external_server_setup_evidence_correlated",
            "task_id": task["id"], "matching_local_remote_artifacts": [{} for _ in range(16)],
        },
    }
    monkeypatch.setattr(adapter, "asset_descriptor", lambda ref, root: {"source_kind": "dependency"})
    def context():
        refs = {}
        for name, record in records.items():
            path = tmp_path / (name + ".json")
            path.write_text(json.dumps(record))
            refs[name] = {"path": str(path), "sha256": digest(path.read_bytes())}
        return SimpleNamespace(config={"migration": {"original_sqlclient": refs}}, output=tmp_path)
    return task, records, context


def test_all_external_inputs_have_verified_original_bindings(input_records):
    task, records, context = input_records
    deps, refs = adapter.inputs(context(), task)
    assert deps == records["dependencies"] and len(refs) == 8


@pytest.mark.parametrize("record,field,value", [
    ("dependencies", "sdk_version", "8.0.0"),
    ("dependencies", "project_build_test_model_judge_calls", 1),
    ("dependencies", "restore_attempts", [{"exit_status": 0}]),
    ("dependencies", "package_index_sha256", "0" * 64),
    ("readback", "all_paths_bytes_full_modes_verified", False),
    ("host", "original_csc_sha256", "0" * 64),
    ("image", "load_exit_status", 1),
    ("server", "image", adapter.SERVER_PARENT),
    ("server_host", "effective_binary_sha256", "0" * 64),
    ("server_host", "extra_runtime_capabilities", ["NET_BIND_SERVICE"]),
    ("server_host", "volumes", ["/var/opt/mssql"]),
    ("server_host", "non_root_user", "root"),
    ("preflight", "status", "failed"),
    ("preflight", "server_command", ["/opt/mssql/bin/sqlservr"]),
    ("preflight", "server_memory_limit_mb", 100000),
    ("preflight", "owned_container_removed", False),
    ("preflight", "observed", {"healthy": True, "anonymous_or_host_volumes": 1}),
    ("original_server_evidence", "matching_local_remote_artifacts", []),
])
def test_rehashed_but_incorrect_input_evidence_is_rejected(input_records, record, field, value):
    task, records, context = input_records
    records[record][field] = value
    with pytest.raises(ContractError):
        adapter.inputs(context(), task)


def test_original_inputs_are_not_exposed_as_public_reference_material(input_records, monkeypatch):
    task, records, context = input_records
    monkeypatch.setattr(adapter, "asset_descriptor", lambda ref, root: {"source_kind": "grader"})
    with pytest.raises(ContractError, match="public dependency"):
        adapter.inputs(context(), task)


@pytest.mark.parametrize("mode", ["public", "grade-build"])
def test_entry_stages_whole_candidate_and_executes_only_original_commands(tmp_path, monkeypatch, mode):
    workspace, cache, sdk = [tmp_path / n for n in ("candidate", "cache", "sdk6")]
    workspace.mkdir()
    cache.mkdir()
    (workspace / "Added.cs").write_text("candidate added helper")
    compiler = sdk / "sdk" / entry.VERSION / "Roslyn/bincore/csc.dll"
    compiler.parent.mkdir(parents=True)
    compiler.write_bytes(b"original compiler fixture")
    packages = tmp_path / "packages"
    (packages / "packages").mkdir(parents=True)
    (packages / "packages/pkg.dll").write_text("dependency")
    private = tmp_path / "private"
    private.mkdir()
    (private / "original-assertions.cs").write_text("private original assertions")
    helper = tmp_path / "setup.py"
    helper.write_text(SETUP_BODY)
    monkeypatch.setattr(entry, "SDK", str(sdk))
    monkeypatch.setattr(entry, "CSC", digest(compiler.read_bytes()))
    monkeypatch.setattr(entry, "CACHE", str(cache))
    spec = {
        "schema_version": "reviewbench-original-sqlclient/v1", "profile": "sqlclient16",
        "packages": str(packages), "packages_asset_sha256": "original-packages",
        "private": str(private), "tool": "/generic-stage", "setup": str(helper),
        "public_commands": ["original restore full-production", "original build full-production"],
        "private_commands": ["original restore full-project-reference", "original build full-project-reference"],
    }
    calls = []
    def call(command, **kwargs):
        calls.append(command)
        if command[0] == sys.executable:
            return subprocess.run(command, **kwargs)
        if command[:2] == [entry.SDK8 + "/dotnet", "/generic-stage/CandidateStage.dll"]:
            source = Path(command[3])
            target = source / entry.TARGET
            target.parent.mkdir(parents=True)
            target.write_text("_isTDSS && enabled\n")
            (source / "Added.cs").write_bytes((workspace / "Added.cs").read_bytes())
            (source.parent / "candidate-source-ledger.json").write_text('{"source_whitelist_used": false}')
            assert kwargs["env"]["DOTNET_ROOT"] == entry.SDK8
        if command == [str(sdk) + "/dotnet", "--version"]:
            return SimpleNamespace(returncode=0, stdout=entry.VERSION + "\n")
        return SimpleNamespace(returncode=0)
    assert entry.execute(spec, workspace, mode, call=call) == 0
    area = cache / ("public" if mode == "public" else "grading")
    expected = spec["public_commands"] if mode == "public" else spec["private_commands"]
    assert [c[-1] for c in calls if c[0] == "bash"] == expected
    assert (area / "repo/Added.cs").read_bytes() == (workspace / "Added.cs").read_bytes()
    assert (area / "original-assertions.cs").exists() == (mode == "grade-build")
    assert not (workspace / entry.TARGET).exists()
    result = json.loads((area / "original-build-binding.json").read_text())
    assert result["mode"] == mode and result["source_whitelist_used"] is False


def test_build_error_keeps_original_failure_without_sdk_or_source_substitution(tmp_path):
    calls = []
    def failed(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=27)
    with pytest.raises(SystemExit) as result:
        entry.run(["original-sdk", "build"], cwd=tmp_path, env={}, call=failed)
    assert result.value.code == 27 and calls == [["original-sdk", "build"]]


@pytest.mark.parametrize("allowed,expected", [
    ("0-255", "0,1"), ("12-15,32-63", "12,13"), ("4,8,32-63", "4,8"), ("7", "7"),
])
def test_service_affinity_selects_at_most_two_currently_allowed_cpus(allowed, expected):
    result = subprocess.run(["awk", "-F,", adapter.SERVER_CPU_AWK],
                            input=allowed + "\n", capture_output=True, text=True, check=True)
    assert result.stdout.strip() == expected
    assert len(result.stdout.strip().split(",")) <= 2
    assert adapter.SERVER_COMMAND[:5] == ["bash", "-e", "-o", "pipefail", "-c"]
    assert adapter.SERVER_COMMAND[-1].endswith('/opt/mssql/bin/sqlservr')
