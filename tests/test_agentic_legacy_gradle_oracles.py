from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from agentic_runtime_migration import jvm_legacy_gradle as adapter
from agentic_runtime_migration import jvm_legacy_gradle_entry as runner
from agentic_runtime_migration import jvm_oracle_entry as reports
from reviewbench_maintainability.agentic.contracts import ContractError


def ref(path):
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


@pytest.fixture(params=["skript17", "reposense41"])
def original(request, external_artifact_root):
    directory = external_artifact_root("LEGACY_GRADLE_PROFILE_ROOT")
    source = directory / "profiles" / (request.param + ".json")
    profile = json.loads(source.read_text())
    task = json.loads(Path(profile["case"]["task"]["path"]).read_text())
    oracle = json.loads(Path(profile["case"]["oracle"]["path"]).read_text())
    return profile, task, oracle, source


def test_real_original_profile_reopens_all_six_logs(original):
    profile, task, oracle, source = original
    assert adapter.load_profile(task, oracle, ref(source)) == profile
    assert len(profile["case"]["original_logs"]) == 6


@pytest.mark.parametrize("mutation", [
    "schema", "scope", "id", "task", "oracle", "junit", "index", "gradle",
    "fixture", "golden", "init", "agent", "missing_log", "log_exit", "log_count",
    "image", "mirror", "outputs", "reports",
])
def test_changed_original_inputs_fail_closed(original, tmp_path, mutation):
    source, task, oracle, _ = original
    value = deepcopy(source)
    case = value["case"]
    if mutation == "schema":
        value["schema_version"] = "other"
    elif mutation == "scope":
        value["full_scope"] = 2
    elif mutation == "id":
        case["id"] = "other"
    elif mutation in {"task", "oracle"}:
        case[mutation + "_sha256"] = "0" * 64
    elif mutation == "junit":
        case["junit_id"] = "invented#test"
    elif mutation == "index":
        case["index"] = "0"
    elif mutation == "gradle":
        case["gradle"] = "9.9"
    elif mutation in {"fixture", "golden"}:
        case["fixture" if mutation == "fixture" else "golden_patch"]["sha256"] = "0" * 64
    elif mutation in {"init", "agent"}:
        value["original_init" if mutation == "init" else "jacoco_agent"]["sha256"] = "0" * 64
    elif mutation == "missing_log":
        case["original_logs"].pop()
    elif mutation == "log_exit":
        case["original_logs"][0]["original_exit_code"] = 1
    elif mutation == "log_count":
        case["original_logs"][0]["original_test_count"] = 0
    elif mutation == "image":
        value["image"]["image"] = "sha256:" + "0" * 64
    elif mutation == "mirror":
        value["mirror"]["full_tree_sha256"] = "0" * 64
    elif mutation == "outputs":
        value["downloads"]["project_outputs_included"] = True
    elif mutation == "reports":
        value["downloads"]["prior_test_results_included"] = True
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ContractError):
        adapter.load_profile(task, oracle, ref(path))


def test_public_mirror_has_no_private_fixture_or_test_execution():
    script = adapter.public_init()
    assert adapter.MIRROR in script and "handler.addFirst(local)" in script
    assert "oracleSessionTest" not in script and "jacoco" not in script
    assert "original.java" not in script and "src/test/java" not in script


def stage(original, tmp_path):
    profile, _, _, _ = original
    case = profile["case"]
    assets = tmp_path / "assets"
    assets.mkdir()
    for name, pointer in [
        ("original.java", case["fixture"]),
        ("oracle.init.gradle", profile["original_init"]),
        ("jacocoagent.jar", profile["jacoco_agent"]),
    ]:
        (assets / name).write_bytes(Path(pointer["path"]).read_bytes())
    root = tmp_path / "workspace"
    root.mkdir()
    spec = {
        "original_fixture": "original.java", "fixture_sha256": case["fixture"]["sha256"],
        "original_init_sha256": profile["original_init"]["sha256"],
        "jacoco_agent_sha256": profile["jacoco_agent"]["sha256"],
        "source_relative_path": case["source_relative_path"], "mirror": adapter.MIRROR,
        "original_command": case["original_command"],
        "expected_tests": [case["junit_id"]], "fresh_private_compile": case["index"] == "41",
    }
    return assets, root, spec


def test_original_execution_and_fresh_fixture_compile(original, tmp_path, monkeypatch):
    assets, root, spec = stage(original, tmp_path)
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("ORACLE_JCENTER_MIRROR", "unbound")
    calls = []

    def run(command, *, cwd, env):
        assert cwd == root and "CI" not in env and "ORACLE_JCENTER_MIRROR" not in env
        calls.append(command)
        if command[:2] == ["bash", "-e"]:
            name = spec["expected_tests"][0].split("#", 1)
            path = root / "build/test-results/test/TEST-oracle.xml"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('<testsuite><testcase classname="' + name[0] + '" name="' + name[1] + '"/></testsuite>')
        return SimpleNamespace(returncode=0)

    code, observed, commands = runner.execute(spec, assets, root, reports, run=run)
    assert code == 0 and observed == {spec["expected_tests"][0]: "PASSED"}
    assert (root / spec["source_relative_path"]).read_bytes() == (assets / "original.java").read_bytes()
    assert len(calls) == (2 if spec["fresh_private_compile"] else 1)
    assert sum(v["stage"] == "original_scored_command" for v in commands) == 1
    if spec["fresh_private_compile"]:
        assert calls[0][-1] == "reviewbenchCompileOriginalOracle"
        assert "-x compileTestJava" in calls[-1][-1]
        assert "--rerun-tasks" in calls[-1][-1]
    else:
        assert "cleanTest testClasses oracleSessionTest" in calls[0][-1]


def test_stale_success_xml_is_never_accepted(original, tmp_path):
    assets, root, spec = stage(original, tmp_path)
    path = root / "build/test-results/test/TEST-stale.xml"
    path.parent.mkdir(parents=True)
    path.write_text("<testsuite/>")
    code, observed, _ = runner.execute(
        spec, assets, root, reports, run=lambda *args, **kwargs: SimpleNamespace(returncode=0))
    assert code == 0 and observed == {} and not path.exists()


def test_changed_fixture_rejected_before_any_process(original, tmp_path):
    assets, root, spec = stage(original, tmp_path)
    (assets / "original.java").write_text("changed")
    with pytest.raises(ValueError, match="fixture changed"):
        runner.execute(spec, assets, root, reports,
                       run=lambda *a, **k: pytest.fail("process started for a changed fixture"))


def test_candidate_fixture_shadow_rejected(original, tmp_path):
    assets, root, spec = stage(original, tmp_path)
    path = root / spec["source_relative_path"]
    path.parent.mkdir(parents=True)
    path.write_text("candidate fixture")
    with pytest.raises(ValueError, match="shadows"):
        runner.install_fixture(root, spec, assets)


def test_exact_init_changes_only_the_three_path_literals(original, tmp_path):
    assets, root, spec = stage(original, tmp_path)
    text = (assets / "oracle.init.gradle").read_text()
    transformed = runner.mapped_original_init(text, "/agent", "/mirror", "/execution")
    assert transformed.replace("'/agent'", "'/oracle-cache/jacocoagent.jar'").replace(
        "'/mirror'", "'/oracle-cache/m2-mirror'").replace(
        "'/execution'", "'/task/start.exec'") == text
    with pytest.raises(ValueError, match="path binding"):
        runner.mapped_original_init(text + "\n'/task/start.exec'\n", "/agent", "/mirror", "/execution")
