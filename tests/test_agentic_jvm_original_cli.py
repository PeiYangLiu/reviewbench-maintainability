from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import jvm_cli as cli
from agentic_runtime_migration import jvm_cli_entry as entry
from agentic_runtime_migration import jvm_lane as lane
from reviewbench_maintainability.agentic.contracts import ContractError

FIXTURE = Path(__file__).parent / "fixtures/jvm_cli/mapping-template-cli.sh"


def binding(tmp_path):
    task = {"id": cli.TASK, "repo": cli.REPO, "start_sha": cli.START}
    oracle = {
        **task, "command": "bash /task/" + cli.SCRIPT,
        "pass_to_pass_tests": cli.TESTS.copy(),
        "source_only_patch": {"sha256": cli.REFERENCE_SHA256},
        "environment": {"build_system": "maven", "container": "oracle-java-tools:17", "jdk": 17},
    }
    root = tmp_path / "original"
    path = root / cli.SCRIPT
    path.parent.mkdir(parents=True)
    path.write_bytes(FIXTURE.read_bytes())
    return task, oracle, root


def test_literal_original_cli_fixture_is_hash_bound():
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == cli.SCRIPT_SHA256
    assert len(FIXTURE.read_bytes()) == 1073


def test_original_plan_keeps_full_package_build_and_private_cli(tmp_path):
    task, oracle, root = binding(tmp_path)
    original = (root / cli.SCRIPT).read_bytes()
    plan = cli.plan(task, oracle, root)
    assert plan["public"] == (
        lane.TOOLS + "/maven/bin/mvn -o -Dmaven.repo.local=" + lane.MAVEN_CACHE +
        " -B -ntp -Dstyle.color=never -Djacoco.skip=true -Dmaven.test.skip=true package"
    )
    assert plan["command"] == "python3 " + lane.PRIVATE + "/cli_entry.py --spec " + lane.PRIVATE + "/cli-spec.json"
    assert plan["original_test_ids"] == cli.TESTS
    assert (root / cli.SCRIPT).read_bytes() == original
    for secret in ("rml-csv", "grep", "rmlCsvExampleProducesExpectedRdf"):
        assert secret not in plan["public"]


@pytest.mark.parametrize("change", [
    "task-id", "task-start", "oracle-id", "oracle-start", "command", "tests",
    "reference", "build-system", "image", "jdk", "missing-environment",
])
def test_changed_original_bindings_are_rejected(tmp_path, change):
    task, oracle, root = binding(tmp_path)
    if change.startswith("task-"):
        task[{"task-id": "id", "task-start": "start_sha"}[change]] = "different"
    elif change.startswith("oracle-"):
        oracle[{"oracle-id": "id", "oracle-start": "start_sha"}[change]] = "different"
    elif change == "command":
        oracle["command"] += " --different"
    elif change == "tests":
        oracle["pass_to_pass_tests"] = ["Other#test"]
    elif change == "reference":
        oracle["source_only_patch"]["sha256"] = "a" * 64
    elif change == "missing-environment":
        oracle["environment"] = None
    else:
        oracle["environment"][{"build-system": "build_system", "image": "container", "jdk": "jdk"}[change]] = "different"
    with pytest.raises(ContractError):
        cli.plan(task, oracle, root)


@pytest.mark.parametrize("change", ["edit", "delete", "symlink"])
def test_script_is_not_replaced_by_unbound_or_linked_bytes(tmp_path, change):
    task, oracle, root = binding(tmp_path)
    path = root / cli.SCRIPT
    if change == "edit":
        path.write_bytes(path.read_bytes() + b"\ntrue\n")
    elif change == "delete":
        path.unlink()
    else:
        path.unlink()
        path.symlink_to(FIXTURE)
    with pytest.raises(ContractError):
        cli.plan(task, oracle, root)


def test_other_repository_does_not_enter_curated_cli_profile(tmp_path):
    assert cli.plan({"repo": "someone/else"}, {}, tmp_path) is None


def test_transport_changes_only_original_maven_path_and_cache():
    raw = FIXTURE.read_bytes()
    transported = entry.relocated_script(raw, cli.SCRIPT_SHA256,
                                         lane.TOOLS + "/maven/bin/mvn", lane.MAVEN_CACHE)
    expected = raw.decode().replace(entry.ORIGINAL_MAVEN, lane.TOOLS + "/maven/bin/mvn -o "
                                    "-Dmaven.repo.local=" + lane.MAVEN_CACHE)
    assert transported == expected
    assert transported.count("grep -F") == 4
    assert "java -jar" in transported and "rm -rf .oracle target" in transported
    assert raw == FIXTURE.read_bytes()


@pytest.mark.parametrize("raw", [b"true\n", b"/oracle-tools/maven/bin/mvn\n/oracle-tools/maven/bin/mvn\n"])
def test_transport_rejects_missing_or_multiple_maven_invocations(raw):
    with pytest.raises(ValueError, match="exactly one"):
        entry.relocated_script(raw, hashlib.sha256(raw).hexdigest(), "/tools/mvn", "/cache")


def test_transport_rejects_wrong_original_hash():
    with pytest.raises(ValueError, match="bound bytes"):
        entry.relocated_script(FIXTURE.read_bytes(), "0" * 64, "/tools/mvn", "/cache")


@pytest.mark.parametrize("path", ["relative", "/a/../b", "/bad\npath", "/bad\0path"])
def test_transport_rejects_non_literal_paths(path):
    with pytest.raises(ValueError, match="absolute literal"):
        entry.relocated_script(FIXTURE.read_bytes(), cli.SCRIPT_SHA256, path, "/cache")


@pytest.mark.parametrize("code", [0, 17])
def test_actual_shell_executes_tool_and_preserves_failure_without_editing_source(tmp_path, code):
    root = tmp_path / "a directory"
    root.mkdir()
    tool = root / "maven;literal"
    tool.write_text(f"#!{sys.executable}\nimport json,sys\nprint(json.dumps(sys.argv[1:]),flush=True)\n"
                    f"raise SystemExit({code})\n")
    tool.chmod(0o755)
    script = root / "original.sh"
    raw = b"#!/usr/bin/env bash\nset -euo pipefail\n/oracle-tools/maven/bin/mvn -B package\nprintf 'AFTER_BUILD\\n'\n"
    script.write_bytes(raw)
    cache = root / "cache;literal"
    spec = root / "spec.json"
    spec.write_text(json.dumps({"original_script": str(script),
        "original_sha256": hashlib.sha256(raw).hexdigest(), "maven": str(tool), "repository": str(cache)}))
    result = subprocess.run([sys.executable, "-B", entry.__file__, "--spec", str(spec)],
                            cwd=root, capture_output=True, text=True, timeout=15)
    assert result.returncode == code, result.stderr
    assert json.loads(result.stdout.splitlines()[0]) == ["-o", "-Dmaven.repo.local=" + str(cache), "-B", "package"]
    assert ("AFTER_BUILD" in result.stdout) is (code == 0)
    assert script.read_bytes() == raw


def test_full_lane_dispatch_keeps_original_private_script_and_common_runner(tmp_path, monkeypatch):
    task, oracle, root = binding(tmp_path)
    output = tmp_path / "package"
    output.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    dep = tmp_path / "repository"
    dep.mkdir()
    snapshots = []
    dummy = {"path": str(output / "external.json"), "sha256": "a" * 64}
    monkeypatch.setattr(lane, "dependency_repository", lambda ctx, task: dep)
    monkeypatch.setattr(lane, "remote_maven_files", lambda root, excluded: ["external.jar"])
    monkeypatch.setattr(lane, "project_coordinates", lambda repo, sha: {("com.cefriel", "mapping-template")})
    monkeypatch.setattr(lane, "maven_tools", lambda ctx: dummy.copy())
    monkeypatch.setattr(lane, "maven_dependency_assets", lambda ctx, task, root, names: (
        [{**dummy, "kind": "archive", "visibility": "developer", "target": lane.DEPENDENCIES}],
        ["cp -a " + lane.DEPENDENCIES + "/. " + lane.MAVEN_CACHE]))
    def bundle(directory, **kwargs):
        snapshots.append({p.relative_to(directory).as_posix(): p.read_bytes()
                          for p in directory.rglob("*") if p.is_file()})
        return dummy.copy()
    ctx = SimpleNamespace(
        config={},
        image=lambda reference: ("sha256:" + "b" * 64, dummy.copy()),
        task_directory=lambda task, oracle, lane: root,
        golden=lambda task, oracle, lane, directory: {"path": str(root / "golden.patch"), "sha256": cli.REFERENCE_SHA256},
        repo=lambda task: tmp_path / "source-repository",
        immutable_tests=lambda task: ["src/test/OriginalTest.java"],
        modules=lambda task: [], output=output, scratch=scratch, bundle=bundle,
    )
    recipe = lane.migrate(ctx, task, oracle)
    assert recipe.dev_checks == [cli.plan(task, oracle, root)["public"]]
    assert recipe.grading["commands"][0]["expected_tests"] == cli.TESTS
    assert recipe.grading["golden_patch"]["sha256"] == cli.REFERENCE_SHA256
    assert recipe.grading["immutable_paths"] == ["src/test/OriginalTest.java"]
    assert recipe.memory_mb == recipe.build_scratch_mb == 4096
    private = snapshots[-1]
    assert private[cli.SCRIPT] == FIXTURE.read_bytes()
    assert private["cli_entry.py"] == Path(entry.__file__).read_bytes()
    assert json.loads(private["cli-spec.json"]) == cli.plan(task, oracle, root)["spec"]
    spec = json.loads(private["spec.json"])
    assert spec["command"] == cli.plan(task, oracle, root)["command"]
    assert spec["expected_tests"] == cli.TESTS
    assert "runner.py" in private
