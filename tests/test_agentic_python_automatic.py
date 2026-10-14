"""Synthetic unit/subprocess checks; these are not the three task runtime proofs."""
from dataclasses import replace
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest


def test_automatic_preparation_reuses_shared_blob_cache_and_transfer_budget(tmp_path):
    from agentic_runtime_migration.python_automatic_prepare import preparation_config
    config = {"asset_root": "/assets", "runtime_recipes": "/recipes.jsonl",
              "storage": {"cache_dir": "/shared/cache", "transfers": 2, "max_cache_bytes": 64 * 1024**3},
              "migration": {"verification_engine_fingerprint": "frozen"}}
    result = preparation_config(config, tmp_path, tmp_path / "outputs")
    assert result["storage"] == config["storage"] and result["storage"] is not config["storage"]
    assert result["asset_root"] == "/assets" and result["runtime_recipes"] == "/recipes.jsonl"
    assert result["migration"]["python_automatic_dependency_root"] == str(tmp_path / "outputs")
    assert "python_automatic_dependency_root" not in config["migration"]


def test_image_rootfs_scan_read_capability_never_applies_to_mounted_dependency_inputs(monkeypatch):
    import agentic_runtime_migration.python_automatic_prepare as prep
    class Job:
        def get(self):
            return {"lease": "a" * 32, "mark_container_use": lambda: None}
    monkeypatch.setattr(prep, "CURRENT_JOB", Job())
    monkeypatch.setattr(prep, "bounded_cpu_set", lambda *_: "0,1")
    ordinary = prep.bounded_container("sha256:" + "b" * 64, ["python3", "-c", "pass"])
    assert "--cap-drop=ALL" in ordinary and not any(x.startswith("--cap-add") for x in ordinary)
    scan = prep.bounded_container("sha256:" + "b" * 64, ["bash", "-c", "find / -xdev"], audit_rootfs=True)
    assert "--cap-add=DAC_READ_SEARCH" in scan and "--read-only" in scan
    assert scan[scan.index("--network") + 1] == "none" and "--mount" not in scan
    with pytest.raises(ContractError, match="must not mount"):
        prep.bounded_container("sha256:" + "b" * 64, ["bash"], mounts=[("/host", "/input")], audit_rootfs=True)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from agentic_runtime_migration import python_automatic as automatic
from agentic_runtime_migration import python_automatic_entry as entry
from agentic_runtime_migration import python_automatic_prepare as preparation
from agentic_runtime_migration import python_lane
from agentic_runtime_migration.common import MigrationContext
from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash, file_hash, read_jsonl
from reviewbench_maintainability.agentic.evaluation import parse_tests, validate_grading
from reviewbench_maintainability.agentic.resources import CURRENT_JOB
from reviewbench_maintainability.agentic.snapshots import git, initialize_git


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")
    return file_hash(path)


class Context:
    repo = MigrationContext.repo
    task_directory = MigrationContext.task_directory
    copy_asset = MigrationContext.copy_asset
    golden = MigrationContext.golden

    def __init__(self, tmp_path):
        self.legacy = tmp_path / "legacy"
        self.repo_cache = tmp_path / "repos"
        self.output = tmp_path / "out"
        self.scratch = tmp_path / "scratch"
        self.config = {"migration": {"python_automatic_dependency_root": str(tmp_path / "prepared")}}
        self.scratch.mkdir()
        self.output.mkdir()
        self.image_calls = []

    def modules(self, task):
        return []

    def immutable_tests(self, task):
        return []

    def image(self, *args):
        self.image_calls.append(args)
        pytest.fail("migration must consume prepared audits, not start containers")


@pytest.fixture(params=list(automatic.CASES), ids=["airbyte", "operator", "mindscape"])
def recovered_fixture(tmp_path, monkeypatch, request):
    """Explicitly synthetic pinned inputs, using the same parser as retained logs."""
    ctx = Context(tmp_path)
    ident = request.param
    released = automatic.CASES[ident]
    repo = ctx.repo_cache / released.repo.replace("/", "__")
    project = released.project
    source = repo / project / "pkg"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("")
    (source / "main.py").write_text("def answer(): return 42\n")
    tests = repo / project / "unit_tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("")
    test_file = tests / "test_public.py"
    test_file.write_text("from pkg.main import answer\n\ndef test_answer(): assert answer() == 42\n")
    (tests / "fixture.json").write_text('{"public": true}\n')
    (repo / project / "conftest.py").write_text("# unchanged public fixture\n")
    (repo / released.config_file).write_text(
        "[tool.pytest.ini_options]\n" if released.config_file.endswith(".toml") else "[pytest]\n")
    initialize_git(repo)
    start = git(repo, "rev-parse", "HEAD").decode().strip()
    (source / "main.py").write_text("def answer(): return 40 + 2\n")
    patch = git(repo, "diff", "--binary", "--full-index")
    git(repo, "checkout", "--", ".")
    directory = ctx.legacy / "gopy" / released.directory
    directory.mkdir(parents=True)
    patch_file = directory / "source-only-golden.patch"
    patch_file.write_bytes(patch)
    node = test_file.relative_to(repo).as_posix() + "::test_answer"
    task = {"id": ident, "repo": released.repo, "start_sha": start, "target_sha": "b" * 40,
            "inferred_language": "Python", "locality": {"path": project + "/pkg/main.py"}}
    auto = {"id": ident, "repo": released.repo, "pass_to_pass": 1, "language": "python",
            "golden_correctness_ok": True, "_automatic_shard": 1}
    auto_hash = save_json(directory / "automatic.json", auto)
    recovered = ctx.legacy / automatic.RECOVERED / released.recovered_slug
    recovered.mkdir(parents=True)
    target = node.split("::")[0]
    runner = "/retained/not-executed/venv/bin/python"
    import shlex
    metadata = {"ok": True, "kind": "python", "p2p": [node], "targets": [target],
                "project_relative": project, "runner": runner,
                "command": shlex.join([runner, "-m", "pytest", "-v", "-p", "no:cacheprovider", target])}
    meta_hash = save_json(recovered / "oracle.json", metadata)
    log = "".join(f"=== run {i} target {target} rc=0 ===\n"
                  "platform linux -- Python 3.11.15, pytest-9.1.1, pluggy-1.6.0\n"
                  f"{node} PASSED [100%]\n1 passed in 0.01s\n" for i in (1, 2, 3))
    (recovered / "discovery.log").write_text(log)
    replay = ctx.legacy / automatic.REPLAY
    replay.write_text("# synthetic retained replay fixture; never executed\n")
    monkeypatch.setattr(automatic, "REPLAY_SHA256", file_hash(replay))
    case = replace(released, start_sha=start, count=1, source_packages=("pkg",),
                   golden_sha256=file_hash(patch_file), automatic_sha256=auto_hash,
                   metadata_sha256=meta_hash, log_sha256=file_hash(recovered / "discovery.log"))
    monkeypatch.setattr(automatic, "CASES", {ident: case})
    oracle = {"id": ident, "repo": task["repo"], "start_sha": start, "target_sha": task["target_sha"],
              "reviewed_path": task["locality"]["path"], "result_source": "automatic_strict_validation",
              "lane_results_source": "${SESSION}/gopy/results.jsonl", "pass_to_pass": 1,
              "start_runs": 3, "golden_runs": 3, "release_eligible": True, "triple_run_stable": True,
              "golden_correctness_ok": True, "changed_lines_covered": True,
              "artifacts": {"directory": case.directory}, "automatic_result": auto,
              "source_only_golden": {"patch": case.directory + "/source-only-golden.patch",
                                     "patch_sha256": case.golden_sha256, "target_tests_included": False}}
    # The old descriptive placeholder is deliberately NOT promoted to a node ID.
    save_json(directory / "decision.json", {**oracle, "pass_to_pass_tests": ["count only, identities not retained"]})
    return ctx, task, oracle, case, metadata


def add_prepared(ctx, task, oracle):
    recovery = automatic.recover_contract(ctx, task, oracle)
    image = "sha256:" + "1" * 64
    audit = ctx.output / "image-audit.json"
    save_json(audit, {"schema_version": "reviewbench-developer-image-audit/v1", "image": image,
                     "no_reference_assets": True, "no_repository_history": True, "python_version": "3.12.3"})
    site = ctx.output / "site.json"
    save_json(site, {"source_kind": "dependency", "tree_sha256": "d" * 64})
    manifest = {
        "schema_version": automatic.DEPENDENCY_SCHEMA, "status": "prepared", "runtime_verified": False,
        "task_id": task["id"], "start_sha": task["start_sha"], "recovery_sha256": canonical_hash(recovery),
        "excluded_source_packages": list(automatic.CASES[task["id"]].source_packages),
        "python_version": "3.12.3", "image": image,
        "image_audit": {"path": str(audit), "sha256": file_hash(audit)},
        "site_asset": {"path": str(site), "sha256": file_hash(site)}, "site_tree_sha256": "d" * 64,
    }
    root = Path(ctx.config["migration"]["python_automatic_dependency_root"])
    path = root / Path(automatic.CASES[task["id"]].directory).name / "preparation.json"
    save_json(path, manifest)
    return path, manifest


def test_three_case_bindings_match_unchanged_released_scope_and_golden():
    tasks = read_jsonl(ROOT / "data/releases/hard/20260808/tasks.jsonl")
    oracles = read_jsonl(ROOT / "data/releases/hard/20260808/oracles.jsonl")
    scope = (ROOT / "configs/openmodel_scope_ids/audited-402.ids").read_text().splitlines()
    assert len(scope) == 402 and len(automatic.CASES) == 3
    for ident, case in automatic.CASES.items():
        assert ident in scope
        assert (case.repo, case.start_sha) == (tasks[ident]["repo"], tasks[ident]["start_sha"])
        assert case.golden_sha256 == oracles[ident]["source_only_golden"]["patch_sha256"]
        assert case.count == oracles[ident]["pass_to_pass"]
        assert "pass_to_pass_tests" not in oracles[ident]


def test_recovery_binds_explicit_nodes_original_commands_and_public_input_bytes(recovered_fixture):
    ctx, task, oracle, case, metadata = recovered_fixture
    before = json.dumps(oracle, sort_keys=True)
    recovery = automatic.recover_contract(ctx, task, oracle)
    assert recovery["nodes"] == metadata["p2p"]
    assert recovery["groups"] == [metadata["p2p"]]
    assert recovery["runtime_verified"] is False and recovery["limitations"]
    assert len(recovery["recovered_discovery"]["runs"]) == 3
    assert len(recovery["test_files"]) == 4  # selected module, init, fixture, ancestor conftest
    assert recovery["source_only_golden_sha256"] == case.golden_sha256
    assert json.dumps(oracle, sort_keys=True) == before
    assert not (ctx.task_directory(task, oracle, "gopy") / "session-spec.json").exists()
    assert all("count only" not in n for n in recovery["nodes"])


@pytest.mark.parametrize("field,value", [
    ("start_sha", "c" * 40), ("repo", "different/repo"), ("pass_to_pass", 0),
    ("start_runs", 0), ("golden_correctness_ok", False), ("release_eligible", False),
    ("reviewed_path", "other.py"), ("pass_to_pass_tests", ["count only"]),
])
def test_recovery_rejects_changed_released_bindings(recovered_fixture, field, value):
    ctx, task, oracle, *_ = recovered_fixture
    with pytest.raises(ContractError):
        automatic.recover_contract(ctx, task, {**oracle, field: value})


@pytest.mark.parametrize("name", ["oracle.json", "discovery.log"])
@pytest.mark.parametrize("change", ["missing", "mutated", "symlink"])
def test_recovery_never_rediscovers_on_missing_or_changed_evidence(recovered_fixture, name, change):
    ctx, task, oracle, case, _ = recovered_fixture
    path = ctx.legacy / automatic.RECOVERED / case.recovered_slug / name
    if change == "missing":
        path.unlink()
    elif change == "mutated":
        path.write_text(path.read_text() + "changed\n")
    else:
        other = path.with_name("other")
        path.rename(other)
        path.symlink_to(other)
    with pytest.raises(ContractError, match="evidence (missing|changed)"):
        automatic.recover_contract(ctx, task, oracle)


def test_discovery_never_accepts_summary_counts_without_node_events(recovered_fixture):
    ctx, _, _, case, metadata = recovered_fixture
    log = (ctx.legacy / automatic.RECOVERED / case.recovered_slug / "discovery.log").read_text()
    for text in (log.replace(metadata["p2p"][0] + " PASSED [100%]\n", ""),
                 log.replace("PASSED", "SKIPPED", 1), log.replace("=== run 3", "=== run 2")):
        with pytest.raises(ContractError):
            automatic.audit_discovery(text, metadata)


def test_discovery_preserves_original_nonp2p_group_exit_codes(recovered_fixture):
    ctx, _, _, case, metadata = recovered_fixture
    log = (ctx.legacy / automatic.RECOVERED / case.recovered_slug / "discovery.log").read_text()
    audited = automatic.audit_discovery(log.replace("rc=0", "rc=1"), metadata)
    assert {r["exit_code"] for r in audited["runs"]} == {1}
    # A retained subset can pass in discovery despite other failing tests.
    # This never grants a failing grading subprocess a pass.


def test_automatic_dispatch_produces_json_runtime_without_starting_images(recovered_fixture):
    ctx, task, oracle, case, _ = recovered_fixture
    add_prepared(ctx, task, oracle)
    recipe = python_lane.migrate(ctx, task, oracle)
    validate_grading(recipe)
    assert recipe.start_sha == task["start_sha"] and recipe.repo == task["repo"]
    assert recipe.cpus == 2 and recipe.memory_mb == 4096
    assert recipe.grading["multifile_build"] is True and not ctx.image_calls
    command = recipe.grading["commands"][0]
    assert command["parser"] == "json" and len(command["expected_tests"]) == 1
    assert "-I -B" in command["command"] and "/workspace" in command["command"]
    assert "session-spec" not in command["command"] and "P2P_SIGNATURE" not in command["command"]
    assert recipe.grading["golden_patch"]["sha256"] == case.golden_sha256
    assert all(a["visibility"] == ("developer" if a.get("kind") == "archive" else "grader") for a in recipe.assets)
    assert any(n.endswith("conftest.py") for n in recipe.grading["immutable_paths"])
    assert "/workspace/src" in recipe.env["PYTHONPATH"]
    assert all(ctx.legacy.as_posix() not in str(v) for v in recipe.env.values())
    assert Path(recipe.grading["automatic_python_recovery"]["path"]).is_file()


def test_session_spec_dispatch_remains_unchanged(recovered_fixture, monkeypatch):
    ctx, task, oracle, *_ = recovered_fixture
    path = ctx.task_directory(task, oracle, "gopy") / "session-spec.json"
    save_json(path, {"command": ["unhandled-old-command"]})
    monkeypatch.setattr(automatic, "migrate", lambda *a: pytest.fail("not an automatic task"))
    with pytest.raises(ContractError, match="task-specific Python/shell"):
        python_lane.migrate(ctx, task, oracle)


def test_automatic_dependency_missing_is_explicit_not_a_count_only_recipe(recovered_fixture):
    ctx, task, oracle, *_ = recovered_fixture
    with pytest.raises(ContractError, match="dependency preparation missing"):
        python_lane.migrate(ctx, task, oracle)
    assert not list(ctx.output.glob("**/contract.json"))


@pytest.mark.parametrize("field,value", [
    ("runtime_verified", True), ("recovery_sha256", "0" * 64),
    ("excluded_source_packages", []), ("start_sha", "e" * 40), ("status", "ready"),
])
def test_prepared_dependencies_are_not_historical_or_ready_attestations(recovered_fixture, field, value):
    ctx, task, oracle, *_ = recovered_fixture
    path, manifest = add_prepared(ctx, task, oracle)
    save_json(path, {**manifest, field: value})
    with pytest.raises(ContractError, match="bound to this recovered contract"):
        python_lane.migrate(ctx, task, oracle)


def test_prepared_dependencies_detect_changed_asset_content(recovered_fixture):
    ctx, task, oracle, *_ = recovered_fixture
    _, manifest = add_prepared(ctx, task, oracle)
    Path(manifest["site_asset"]["path"]).write_text("{}")
    with pytest.raises(ContractError, match="changed pinned asset"):
        python_lane.migrate(ctx, task, oracle)


@pytest.mark.parametrize("nodes", [[], ["test.py::t", "test.py::t"], ["../test.py::t"],
                                 ["test.py"], ["/tmp/test.py::t"], ["test.py::t\nINJECT"]])
def test_selector_validation_is_fail_closed(nodes):
    with pytest.raises(ContractError):
        automatic.node_groups(nodes)


def test_dependency_filter_omits_project_packages_metadata_and_path_hooks(tmp_path):
    case = next(iter(automatic.CASES.values()))
    site = tmp_path / "site"
    for name in ["airbyte_cdk/main.py", "airbyte_cdk.py", "airbyte_cdk-1.dist-info/METADATA",
                 "__editable___airbyte_finder.py", "__editable__.pth", "editable.egg-link",
                 "_virtualenv.py", "pytest/__init__.py", "pytest/__pycache__/old.pyc",
                 "external/__init__.py", "external-1.dist-info/METADATA", "distutils-precedence.pth"]:
        p = site / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("synthetic dependency fixture\n")
    names = automatic.dependency_files(site, case, {"excluded_distribution_dirs": ["airbyte_cdk-1.dist-info"]})
    assert names == ["external-1.dist-info/METADATA", "external/__init__.py", "pytest/__init__.py"]
    (site / "link.py").symlink_to("external/__init__.py")
    with pytest.raises(ContractError, match="symlink"):
        automatic.dependency_files(site, case, {"excluded_distribution_dirs": ["airbyte_cdk-1.dist-info"]})


def test_retained_dependency_metadata_reads_uv_version_info_without_running_venv(tmp_path):
    case = next(iter(automatic.CASES.values()))
    venv = tmp_path / case.venv
    site = venv / "lib/python3.12/site-packages"
    site.mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("version_info = 3.12.3\n")
    for name in ["pytest", "airbyte-cdk"]:
        folder = site / (name + "-1.dist-info")
        folder.mkdir()
        (folder / "METADATA").write_text(f"Name: {name}\nVersion: 1\n")
    inventory = automatic.dependency_inventory(tmp_path, case)
    assert inventory["image_reference"] == "python:3.12.3-bookworm"
    assert inventory["historical_wave001_lock"] is False
    assert [d["name"] for d in inventory["distributions"]] == ["pytest"]
    save_json(site / "pytest-1.dist-info/direct_url.json", {"dir_info": {"editable": True}})
    with pytest.raises(ContractError, match="foreign editable"):
        automatic.dependency_inventory(tmp_path, case)


def runner_fixture(tmp_path, *, body=None, project=".", config="[pytest]\n", groups=1):
    workspace = tmp_path / "candidate"
    package = workspace / project / "src/pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "helper.py").write_text("def double(x): return x * 2 + 2\n")
    (package / "api.py").write_text("from .helper import double\n")
    private = tmp_path / "grader"
    private.mkdir()
    config_file = private / "pytest.ini"
    config_file.write_text(config)
    nodes, files = [], {}
    for index in range(groups):
        test = workspace / project / "tests" / f"test_public_{index}.py"
        test.parent.mkdir(parents=True, exist_ok=True)
        test.write_text(body or "from pkg.api import double\n\ndef test_public(): assert double(20) == 42\n")
        relative = test.relative_to(workspace).as_posix()
        files[relative] = file_hash(test)
        nodes.append(relative + "::test_public")
    contract = {
        "schema_version": automatic.ENTRY_SCHEMA, "nodes": nodes,
        "groups": [[n] for n in nodes], "project_relative": project, "pytest_root_relative": project,
        "test_files": files, "test_roots": [(Path(project) / "tests").as_posix()],
        "config_path": str(config_file), "config_sha256": file_hash(config_file),
        "dependency_site": str(Path(pytest.__file__).resolve().parents[1]), "source_packages": ["pkg"],
        "python_version": ".".join(map(str, sys.version_info[:3])), "timeout_seconds": 20,
    }
    contract_file = private / "contract.json"
    save_json(contract_file, contract)
    report = workspace / ".reviewbench/report.json"
    command = [sys.executable, "-I", "-B", str(Path(entry.__file__)),
               "--contract", str(contract_file), "--workspace", str(workspace), "--report", str(report)]
    return SimpleNamespace(workspace=workspace, package=package, contract=contract, contract_file=contract_file,
                           report=report, command=command)


def run_entry(fixture, *, extra_env=None):
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **(extra_env or {})}
    process = subprocess.run(fixture.command, env=env, capture_output=True, text=True, timeout=90)
    data = json.loads(fixture.report.read_text()) if fixture.report.is_file() else None
    return process, data


@pytest.mark.parametrize("project", [".", "monorepo/sdk"])
def test_real_pytest_uses_complete_candidate_and_new_helper_not_a_historical_anchor(tmp_path, project):
    fixture = runner_fixture(tmp_path, project=project)
    first, report = run_entry(fixture, extra_env={"PYTEST_ADDOPTS": "--collect-only",
                                                 "PYTEST_PLUGINS": "nonexistent_host_plugin"})
    assert first.returncode == 0, first.stdout + first.stderr
    assert all(t["status"] == "passed" for t in report["tests"])
    assert len(report["groups"]) == 1
    (fixture.package / "helper.py").write_text("def double(x): return x * 2 - 2\n")
    second, report = run_entry(fixture)
    assert second.returncode != 0
    assert all(t["status"] == "failed" for t in report["tests"])
    assert parse_tests("json", json.dumps(report), command_ok=False,
                       expected=fixture.contract["nodes"]) == {fixture.contract["nodes"][0]: False}


@pytest.mark.parametrize("body", [
    "import pytest\n\ndef test_public(): pytest.skip('dependency unavailable')\n",
    "import pytest\n@pytest.mark.xfail(reason='not a pass')\ndef test_public(): assert False\n",
    "import pytest\n@pytest.mark.xfail(reason='unexpected pass still not original pass')\ndef test_public(): pass\n",
    "import pytest\n@pytest.fixture(autouse=True)\ndef broken(): raise RuntimeError('setup')\ndef test_public(): pass\n",
    "import pytest\n@pytest.fixture(autouse=True)\ndef broken():\n    yield\n    raise RuntimeError('teardown')\ndef test_public(): pass\n",
    "import os\n\ndef test_public():\n    print('18 passed\\nP2P_SIGNATURE=ok')\n    os._exit(0)\n",
])
def test_pytest_skips_xfails_phase_errors_and_forged_stdout_do_not_pass(tmp_path, body):
    fixture = runner_fixture(tmp_path, body=body)
    process, data = run_entry(fixture)
    assert process.returncode != 0, process.stdout + process.stderr
    assert data and all(t["status"] == "failed" for t in data["tests"])


@pytest.mark.parametrize("options", ["--collect-only", "-k nonexistent"])
def test_collect_only_deselection_and_missing_execution_are_not_success(tmp_path, options):
    fixture = runner_fixture(tmp_path, config=f"[pytest]\naddopts = {options}\n")
    process, data = run_entry(fixture)
    assert process.returncode != 0 and data["tests"][0]["status"] == "failed"


def test_each_original_test_file_gets_a_fresh_pytest_process(tmp_path):
    body = ("import builtins\n\ndef test_public():\n"
            "    assert not getattr(builtins, 'previous_oracle_file', False)\n"
            "    builtins.previous_oracle_file = True\n")
    fixture = runner_fixture(tmp_path, body=body, groups=2)
    process, data = run_entry(fixture)
    assert process.returncode == 0, process.stdout + process.stderr
    assert len(data["groups"]) == 2
    assert {g["exit_code"] for g in data["groups"]} == {0}
    assert all(t["status"] == "passed" for t in data["tests"])


def test_stale_report_does_not_survive_a_changed_fixed_test(tmp_path):
    fixture = runner_fixture(tmp_path)
    assert run_entry(fixture)[0].returncode == 0
    name = next(iter(fixture.contract["test_files"]))
    (fixture.workspace / name).write_text("def test_public(): pass\n")
    process, data = run_entry(fixture)
    assert process.returncode == 2 and data["tests"][0]["status"] == "failed"
    assert "test/fixture changed" in data["error"]


def test_changed_or_missing_fixed_pytest_config_is_a_failure(tmp_path):
    fixture = runner_fixture(tmp_path)
    Path(fixture.contract["config_path"]).unlink()
    process, data = run_entry(fixture)
    assert process.returncode == 2 and data["tests"][0]["status"] == "failed"
    assert "configuration changed or missing" in data["error"]


def test_candidate_pytest_config_cannot_silently_turn_grading_into_collection(tmp_path):
    fixture = runner_fixture(tmp_path)
    (fixture.workspace / "pytest.ini").write_text("[pytest]\naddopts = --collect-only\n")
    process, data = run_entry(fixture)
    assert process.returncode == 0, process.stdout + process.stderr
    assert data["tests"][0]["status"] == "passed"


def test_public_fixture_symlink_keeps_candidate_production_target_live(tmp_path):
    fixture = runner_fixture(tmp_path)
    link = fixture.workspace / "tests/production-link"
    link.symlink_to("../src/pkg", target_is_directory=True)
    fixture.contract["test_symlinks"] = {"tests/production-link": "../src/pkg"}
    save_json(fixture.contract_file, fixture.contract)
    process, _ = run_entry(fixture)
    assert process.returncode == 0, process.stdout + process.stderr
    (fixture.package / "helper.py").write_text("def double(x): return 0\n")
    process, data = run_entry(fixture)
    assert process.returncode != 0 and data["tests"][0]["status"] == "failed"
    assert "fixture symlink changed" not in (data.get("error") or "")
    link.unlink()
    link.symlink_to("/tmp")
    process, data = run_entry(fixture)
    assert process.returncode == 2 and "fixture symlink changed" in data["error"]


def test_preparation_container_requires_shared_gate_and_has_hard_bounds(monkeypatch):
    token = CURRENT_JOB.set(None)
    try:
        with pytest.raises(ContractError, match="shared resource gate"):
            preparation.bounded_container("sha256:" + "a" * 64, ["python3"])
    finally:
        CURRENT_JOB.reset(token)
    calls = []
    token = CURRENT_JOB.set({"lease": "a" * 32, "mark_container_use": lambda: calls.append(True)})
    try:
        args = preparation.bounded_container("sha256:" + "a" * 64, ["python3", "-I"])
    finally:
        CURRENT_JOB.reset(token)
    assert calls == [True]
    for option, value in [("--cpus", "2"), ("--memory", "4g"), ("--memory-swap", "4g"),
                          ("--network", "none"), ("--pids-limit", "256")]:
        assert args[args.index(option) + 1] == value
    assert "reviewbench.resource-lease=" + "a" * 32 in args
    assert "--read-only" in args


def test_readonly_git_disables_promisor_fetch_and_index_mutation(monkeypatch, tmp_path):
    observed = []
    monkeypatch.setattr(automatic.subprocess, "run",
                        lambda command, **kwargs: observed.append((command, kwargs))
                        or SimpleNamespace(returncode=0, stdout=b"blob", stderr=b""))
    assert automatic.git(tmp_path, "show", "a" * 40 + ":test.py") == b"blob"
    env = observed[0][1]["env"]
    assert env["GIT_NO_LAZY_FETCH"] == "1" and env["GIT_OPTIONAL_LOCKS"] == "0"
    assert "maintenance.auto=false" in observed[0][0]


@pytest.mark.parametrize("golden_expression,status", [("x + x + 2", "ready"), ("x + x - 2", "blocked")])
def test_frozen_engine_replays_real_pytest_reports_on_synthetic_start_and_golden(
        tmp_path, golden_expression, status):
    """Real subprocesses, synthetic repository; not a released-task attestation."""
    import shlex
    from reviewbench_maintainability.agentic.contracts import RuntimeRecipe
    from reviewbench_maintainability.agentic.preparation import verify_recipe
    fixture = runner_fixture(tmp_path)
    repo = fixture.workspace
    initialize_git(repo)
    start = git(repo, "rev-parse", "HEAD").decode().strip()
    helper = fixture.package / "helper.py"
    helper.write_text(f"def double(x): return {golden_expression}\n")
    golden = tmp_path / "synthetic-golden.patch"
    golden.write_bytes(git(repo, "diff", "--binary", "--full-index"))
    git(repo, "checkout", "--", ".")
    task = {"id": "synthetic-python-automatic", "repo": "fixture/python-automatic",
            "start_sha": start, "locality": {"path": "src/pkg/api.py"}}
    command = [*fixture.command]
    command[command.index("--workspace") + 1] = "/workspace"
    command[command.index("--report") + 1] = "/workspace/" + automatic.REPORT_PATH
    recipe = RuntimeRecipe(
        task_id=task["id"], repo=task["repo"], start_sha=start, lane="gopy",
        image="sha256:" + "a" * 64, image_audit={}, cpus=2, memory_mb=4096,
        scratch_paths=[str(Path(automatic.REPORT_PATH).parent)],
        dev_checks=[shlex.join([sys.executable, "-I", "-B", "-c",
                               "from pathlib import Path; p=Path('src/pkg/api.py'); "
                               "compile(p.read_bytes(),str(p),'exec')"])],
        grading={"multifile_build": True, "immutable_paths": list(fixture.contract["test_files"]),
                 "golden_patch": {"path": str(golden), "sha256": file_hash(golden)},
                 "commands": [{"command": shlex.join(command), "parser": "json",
                               "report_path": automatic.REPORT_PATH,
                               "expected_tests": fixture.contract["nodes"]}]},
    )

    class SubprocessFixtureEnvironment:
        """Test-only host implementation of the frozen environment protocol."""
        def __init__(self, recipe, workspace, artifacts, asset_root, **kwargs):
            self.workspace, self.artifacts = workspace, artifacts
            artifacts.mkdir(parents=True, exist_ok=True)
            self.sequence = 0

        def execute(self, action):
            command = action["command"].replace("/workspace", str(self.workspace))
            process = subprocess.run(command, shell=True, cwd=self.workspace, text=True,
                                     capture_output=True, timeout=60)
            self.sequence += 1
            stdout, stderr = f"{self.sequence}.stdout", f"{self.sequence}.stderr"
            (self.artifacts / stdout).write_text(process.stdout)
            (self.artifacts / stderr).write_text(process.stderr)
            return {"returncode": process.returncode, "extra": {"stdout_file": stdout, "stderr_file": stderr}}

        def close(self):
            pass

    oracle = {"source_only_golden": {"patch_sha256": file_hash(golden)},
              "pass_to_pass_tests": fixture.contract["nodes"]}
    proof = verify_recipe(task, recipe, oracle, repo, tmp_path, tmp_path / "synthetic-proof",
                          environment_factory=SubprocessFixtureEnvironment)
    assert proof["status"] == status, proof
    assert [r["variant"] for r in proof["runs"][:3]] == ["start"] * 3
    assert all(all(r["tests"].values()) for r in proof["runs"][:3])
    if status == "ready":
        assert [r["variant"] for r in proof["runs"][3:]] == ["golden"] * 3
        assert all(all(r["tests"].values()) for r in proof["runs"])
    else:
        assert proof["runs"][-1]["variant"] == "golden"
        assert not all(proof["runs"][-1]["tests"].values())
