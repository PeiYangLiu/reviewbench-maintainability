"""Named original automatic evidence and fixed P2P reporter subprocess tests."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from agentic_runtime_migration.go_contracts import load_contract
from agentic_runtime_migration.go_public_inputs import SCHEMA
from reviewbench_maintainability.agentic.contracts import ContractError
from reviewbench_maintainability.agentic.evaluation import parse_tests, validate_grading
from test_agentic_go_wrappers import assembled, fixture, write


def reference(path):
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def original_runs(tmp_path):
    ctx, task, oracle, directory = fixture(tmp_path, "067-go-azure-sdk")
    (directory / "session-spec.json").unlink()
    command = ["go", "test", "-v", "-count=1", "./pkg/"]
    harness = write(tmp_path / "original-harness.py", "# Original-harness fixture, not a session-spec.\n")
    golden_hash = "d" * 64
    oracle = {"pass_to_pass": 2, "source_only_golden": {"patch_sha256": golden_hash}}
    record = {
        "schema_version": SCHEMA, "id": task["id"], "repo": task["repo"], "start_sha": task["start_sha"],
        "golden_patch_sha256": golden_hash, "evidence_kind": "retained-original-runs",
        "harness": [reference(harness)], "command": command, "cwd": "{repo}",
        "go_version": "1.22.2", "go_version_output": "go version go1.22.2 linux/amd64", "runs": {},
    }
    for phase in ("start", "golden"):
        record["runs"][phase] = []
        for number in (1, 2, 3):
            log = write(tmp_path / f"original-{phase}-{number}.log",
                        "--- PASS: TestFixed (0.00s)\n    --- PASS: TestOther/subcase (0.00s)\n"
                        "--- FAIL: TestOther (0.01s)\n")
            record["runs"][phase].append({"run": number, "command": command, "cwd": "{repo}",
                                          "exit_code": 1, "log": reference(log)})
    path = write(tmp_path / "original-go-runs.json", json.dumps(record))
    ctx.config = {"migration": {"go_original_runs": {task["id"]: reference(path)}}}
    return ctx, task, oracle, directory, record, path


def update(ctx, task, record, path):
    path.write_text(json.dumps(record))
    ctx.config["migration"]["go_original_runs"][task["id"]] = reference(path)


@pytest.mark.parametrize("count", [6, 8, 29, 20, 265])
def test_automatic_counts_never_manufacture_named_selectors(tmp_path, count):
    ctx, task, oracle, directory = fixture(tmp_path, "067-go-azure-sdk")
    (directory / "session-spec.json").unlink()
    oracle["pass_to_pass"] = count
    with pytest.raises(ContractError, match=f"retained count={count}"):
        load_contract(ctx, task, oracle)
    assert not (directory / "session-spec.json").exists()


def test_automatic_recipe_binds_six_original_named_runs_and_harness(tmp_path, monkeypatch):
    ctx, task, oracle, directory, record, path = original_runs(tmp_path)
    contract = load_contract(ctx, task, oracle)
    assert contract.kind == "automatic-public-tests"
    assert contract.command == record["command"]
    assert contract.expected == ["example.invalid/current/pkg::TestFixed", "example.invalid/current/pkg::TestOther/subcase"]
    assert contract.signature is None
    assert all(p["role"] != "original-session-spec" for p in contract.provenance)
    recipe = assembled(monkeypatch, ctx, task, oracle)
    validate_grading(recipe)
    assert recipe.grading["commands"][0]["parser"] == "json"
    assert recipe.grading["commands"][0]["expected_tests"] == contract.expected
    assert not (directory / "session-spec.json").exists()


@pytest.mark.parametrize("field,value,error", [
    ("start_sha", "a" * 40, "task_binding"),
    ("golden_patch_sha256", "a" * 64, "golden_binding"),
    ("go_version_output", "go version go1.25.0 linux/amd64", "compiler_observation"),
    ("harness", [], "harness_evidence"),
    ("evidence_kind", "synthetic-session-spec", "evidence_kind"),
])
def test_automatic_evidence_binding_errors(tmp_path, field, value, error):
    ctx, task, oracle, directory, record, path = original_runs(tmp_path)
    record[field] = value
    update(ctx, task, record, path)
    with pytest.raises(ContractError, match=error):
        load_contract(ctx, task, oracle)


def test_original_log_digest_and_stable_set_are_both_enforced(tmp_path):
    ctx, task, oracle, directory, record, path = original_runs(tmp_path)
    last = record["runs"]["golden"][2]
    log = Path(last["log"]["path"])
    log.write_text("--- PASS: TestFixed (0.01s)\n--- FAIL: TestOther/subcase (0.01s)\n")
    with pytest.raises(ContractError, match="digest_differs"):
        load_contract(ctx, task, oracle)
    last["log"] = reference(log)
    update(ctx, task, record, path)
    with pytest.raises(ContractError, match="p2p_count_differs"):
        load_contract(ctx, task, oracle)


def test_three_golden_runs_cannot_be_replaced_by_a_single_success(tmp_path):
    ctx, task, oracle, directory, record, path = original_runs(tmp_path)
    record["runs"]["golden"] = record["runs"]["golden"][:1]
    update(ctx, task, record, path)
    with pytest.raises(ContractError, match="named_runs_incomplete"):
        load_contract(ctx, task, oracle)


def test_new_original_harness_capture_requires_frozen_engine_binding(tmp_path):
    ctx, task, oracle, directory, record, path = original_runs(tmp_path)
    record["evidence_kind"] = "original-harness-replay"
    ctx.config["migration"]["verification_engine_fingerprint"] = "a" * 64
    record["engine_fingerprint"] = "b" * 64
    update(ctx, task, record, path)
    with pytest.raises(ContractError, match="engine_binding_differs"):
        load_contract(ctx, task, oracle)
    record["engine_fingerprint"] = "a" * 64
    update(ctx, task, record, path)
    assert load_contract(ctx, task, oracle).kind == "automatic-public-tests"


@pytest.fixture(scope="module")
def reporter(tmp_path_factory):
    go = shutil.which("go")
    if not go:
        pytest.skip("preinstalled Go required for reporter subprocess tests")
    root = tmp_path_factory.mktemp("go-reporter")
    output = root / "go-public-test-report"
    env = dict(os.environ, GO111MODULE="off", GOTOOLCHAIN="local", GOPROXY="off",
               GOCACHE=str(root / "build-cache"), GOFLAGS="-p=2", GOMAXPROCS="2", CGO_ENABLED="0")
    result = subprocess.run([go, "build", "-o", str(output),
                             str(ROOT / "scripts/agentic_runtime_migration/go_public_test_report.go")],
                            env=env, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr
    return output


def run_reporter(tmp_path, reporter, actions, *, command_exit=0, complete=True):
    package = "example.invalid/current/pkg"
    events = [{"Package": package, "Test": name, "Action": action} for name, action in actions]
    if complete:
        events.append({"Package": package, "Action": "pass" if command_exit == 0 else "fail"})
    tool = write(tmp_path / "fake-go", "#!" + sys.executable + "\nimport json,sys\n"
                 "assert sys.argv[1:]==['test','-json','-v','-count=1','./pkg/']\n"
                 f"events={events!r}\nfor event in events: print(json.dumps(event))\n"
                 f"sys.exit({command_exit})\n", True)
    path = write(tmp_path / "contract.json", json.dumps({
        "command": [str(tool), "test", "-v", "-count=1", "./pkg/"],
        "expected": [package + "::TestFixed", package + "::TestOther/subcase"],
        "report": str(tmp_path / "report.json"), "log": str(tmp_path / "events.jsonl"),
    }))
    result = subprocess.run([str(reporter), str(path)], capture_output=True, text=True, timeout=30)
    report = tmp_path / "report.json"
    parsed = parse_tests("json", report.read_text(), command_ok=result.returncode == 0,
                         expected=[package + "::TestFixed", package + "::TestOther/subcase"])
    return result, parsed


def test_reporter_preserves_original_non_p2p_failure_semantics(tmp_path, reporter):
    result, parsed = run_reporter(tmp_path, reporter, [
        ("TestFixed", "pass"), ("TestOther/subcase", "pass"), ("TestOther", "fail")], command_exit=1)
    assert result.returncode == 0
    assert all(parsed.values())


@pytest.mark.parametrize("actions,complete", [
    ([("TestFixed", "pass")], True),
    ([("TestFixed", "pass"), ("TestOther/subcase", "skip")], True),
    ([("TestFixed", "pass"), ("TestOther/subcase", "fail")], True),
    ([("TestFixed", "pass"), ("TestOther/subcase", "pass"), ("TestOther/subcase", "fail")], True),
    ([("TestFixed", "pass"), ("TestOther/subcase", "pass")], False),
])
def test_reporter_requires_every_original_test_and_package_completion(tmp_path, reporter, actions, complete):
    result, parsed = run_reporter(tmp_path, reporter, actions, complete=complete)
    assert result.returncode == 1
    assert not all(parsed.values())


def test_reporter_drops_a_stale_report_before_spawn_error(tmp_path, reporter):
    report = write(tmp_path / "report.json", '{"stale":"success"}')
    contract = write(tmp_path / "contract.json", json.dumps({
        "command": [str(tmp_path / "missing-go"), "test"], "expected": ["pkg::TestFixed"],
        "report": str(report), "log": str(tmp_path / "events.jsonl"),
    }))
    result = subprocess.run([str(reporter), str(contract)], capture_output=True, timeout=30)
    assert result.returncode == 2
    assert not report.exists()
