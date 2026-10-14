"""Unit and real-Docker regression tests for the independent execution protocol.

The provider is a deterministic HTTP fixture; the mini agent loop, native model
adapter, Docker commands, source edits, test failures and final grader are real.
"""
from dataclasses import asdict, replace
import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import threading
import time

import pytest

from reviewbench_maintainability.agentic import PROTOCOL
from reviewbench_maintainability.agentic.contracts import (
    ContractError, PublicTaskSpec, RuntimeRecipe, canonical_hash, file_hash, repo_path,
)
from reviewbench_maintainability.agentic.models import Budget, BudgetLimit, load_mini
from reviewbench_maintainability.agentic.snapshots import (
    apply_patch, collect_patch, copy_source, extract_start, git, initialize_git, tree_hash,
)
from reviewbench_maintainability.agentic.evaluation import (
    correctness, judge_candidate, judge_prompt, parse_tests, verdict_passes,
)
from reviewbench_maintainability.agentic.preparation import prepare, verify_recipe
from reviewbench_maintainability.agentic.reporting import make_report


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / "cache" / "fixture__maintainability"
    repo.mkdir(parents=True)
    (repo / "main.py").write_text("def inc(x):\n    return x + 1\n")
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\nbuild/\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_main.py").write_text(
        "import unittest\nfrom main import inc\n"
        "class TestIncrement(unittest.TestCase):\n"
        "    def test_increment(self):\n        self.assertEqual(inc(1), 2)\n"
    )
    initialize_git(repo)
    start = git(repo, "rev-parse", "HEAD").decode().strip()
    (repo / "answer-secret.txt").write_text("GOLDEN_NEVER_VISIBLE")
    git(repo, "add", "--", "answer-secret.txt")
    git(repo, "commit", "--quiet", "-m", "future answer")
    future = git(repo, "rev-parse", "HEAD").decode().strip()
    task = {"id": "fixture-task", "repo": "fixture/maintainability", "start_sha": start,
            "target_sha": future, "inferred_language": "Python",
            "task_instruction": "Extract increment behavior into a reusable helper.",
            "locality": {"path": "main.py", "line": 1},
            "golden_patch": "GOLDEN_NEVER_VISIBLE",
            "quality_evidence": {"secret": "JUDGE_NEVER_VISIBLE"}}
    return repo, task


def recipe_for(task, image="sha256:" + "1" * 64, audit=None):
    return RuntimeRecipe(
        task_id=task["id"], repo=task["repo"], start_sha=task["start_sha"], lane="gopy",
        image=image, image_audit=audit or {"path": "audit.json", "sha256": "a" * 64},
        dev_checks=["python3 -m unittest discover -s tests -v"],
        memory_mb=256, command_timeout_seconds=30,
        grading={"multifile_build": True, "immutable_paths": ["tests/test_main.py"],
                 "commands": [{"command": "python3 -m unittest discover -s tests -v",
                               "parser": "command", "expected_tests": ["increment_behavior"],
                               "success_marker": "OK"}]},
    )


def test_task_projection_never_contains_answers(repository):
    _, task = repository
    prompt = PublicTaskSpec.from_task(task).prompt(["python3 -m unittest"])
    assert "Working directory: /workspace" in prompt
    assert "Working directory: /workspace/subproject" in PublicTaskSpec.from_task(task).prompt(
        ["python3 -m unittest"], workdir="/workspace/subproject")
    for forbidden in ("GOLDEN_NEVER_VISIBLE", "JUDGE_NEVER_VISIBLE", task["target_sha"], "golden_patch"):
        assert forbidden not in prompt


@pytest.mark.parametrize("path", ["", ".", "/tmp/x", "../x", "x/../y", ".git/config", "x\\.git", None])
def test_repo_path_rejects_escape(path):
    with pytest.raises(ContractError):
        repo_path(path)


def test_exact_snapshot_has_no_future_history(repository, tmp_path):
    repo, task = repository
    root = tmp_path / "isolated"
    extract_start(repo, task["start_sha"], root)
    assert not (root / "answer-secret.txt").exists()
    assert not (root / ".git").exists()
    initialize_git(root)
    result = subprocess.run(["git", "-C", str(root), "cat-file", "-e", task["target_sha"]], capture_output=True)
    assert result.returncode != 0


def test_multifile_collection_includes_commits_untracked_and_deletions(repository, tmp_path):
    repo, task = repository
    base, work = tmp_path / "base", tmp_path / "work"
    extract_start(repo, task["start_sha"], base)
    copy_source(base, work)
    initialize_git(work)
    (work / "main.py").write_text("from helper import inc\n")
    (work / "helper.py").write_text("def inc(x):\n    return x + 1\n")
    git(work, "add", "--all")
    git(work, "commit", "--quiet", "-m", "solver committed")
    (work / "extra.py").write_text("VALUE = 1\n")  # never added to solver's index
    (work / "tests" / "test_main.py").unlink()
    (work / "build").mkdir()
    (work / "build" / "ignored.o").write_text("junk")
    patch = tmp_path / "candidate.patch"
    result = collect_patch(base, work, patch)
    assert set(result["changed_files"]) == {"main.py", "helper.py", "extra.py", "tests/test_main.py"}
    assert result["apply_ok"]
    after = tmp_path / "applied"
    apply_patch(base, patch, after)
    assert (after / "extra.py").read_text() == "VALUE = 1\n"
    assert not (after / "tests/test_main.py").exists()


def test_solver_gitignore_cannot_hide_new_source(repository, tmp_path):
    repo, task = repository
    base, work = tmp_path / "base", tmp_path / "work"
    extract_start(repo, task["start_sha"], base)
    copy_source(base, work)
    (work / ".gitignore").write_text("*\n")
    (work / "new.py").write_text("x=1\n")
    assert "new.py" in collect_patch(base, work, tmp_path / "out.patch")["changed_files"]


def test_escaping_symlink_rejected(repository, tmp_path):
    repo, task = repository
    base, work = tmp_path / "base", tmp_path / "work"
    extract_start(repo, task["start_sha"], base)
    copy_source(base, work)
    (work / "leak").symlink_to("/etc/passwd")
    with pytest.raises(ContractError, match="symlink"):
        collect_patch(base, work, tmp_path / "out.patch")


def test_budget_counts_reasoning_only_once():
    budget = Budget(max_output_tokens=200, per_call_output_tokens=100)
    budget.charge({"usage": {"prompt_tokens": 10, "completion_tokens": 90,
                           "completion_tokens_details": {"reasoning_tokens": 70}}})
    assert budget.output_tokens == 90
    assert budget.check() == 100
    budget.charge({"usage": {"input_tokens": 12, "output_tokens": 30}})
    assert budget.check() == 80
    budget.charge({"usage": {"input_tokens": 12, "output_tokens": 80}})
    with pytest.raises(BudgetLimit, match="output_token"):
        budget.check()


def test_budget_reconnect_does_not_reset_usage_or_elapsed():
    budget = Budget(max_calls=5, wall_seconds=10)
    budget.restore({"model_calls": 4, "input_tokens": 90, "output_tokens": 10,
                    "transport_retries": 1, "elapsed_seconds": 9})
    assert budget.seconds_remaining <= 1
    budget.model_calls += 1
    with pytest.raises(BudgetLimit, match="model_call"):
        budget.check()


@pytest.mark.parametrize("output,total", [(5, 164), (145, 164)])
def test_gateway_standalone_reasoning_is_measured_without_double_counting(output, total):
    budget = Budget(max_output_tokens=200)
    budget.charge({"usage": {"prompt_tokens": 19, "completion_tokens": output,
                            "reasoning_tokens": 140, "total_tokens": total}})
    assert budget.output_tokens == 145
    assert budget.input_tokens == 19
    assert budget.check() == 55


def test_gateway_ambiguous_reasoning_usage_is_not_silently_undercounted():
    from reviewbench_maintainability.agentic.contracts import InfrastructureError
    budget = Budget()
    for extra in ({}, {"total_tokens": 33}, {"total_tokens": True}):
        with pytest.raises(InfrastructureError, match="reasoning"):
            budget.charge({"usage": {"prompt_tokens": 19, "completion_tokens": 5,
                                    "reasoning_tokens": 140, **extra}})
    assert budget.output_tokens == budget.input_tokens == 0


@pytest.mark.parametrize("parser,text,expected", [
    ("go-json", '{"Package":"p","Test":"A","Action":"pass"}\n', ["p::A", "p::B"]),
    ("junit", '<testsuite><testcase classname="p" name="A"/></testsuite>', ["p::A", "p::B"]),
    ("trx", '<r><UnitTestResult testName="A" outcome="Passed"/></r>', ["A", "B"]),
    ("rust-test", "test A ... ok\n", ["A", "B"]),
    ("ctest", "1/2 Test #1: A .... Passed 0.1 sec\n", ["A", "B"]),
    ("json", '{"schema_version":"reviewbench-exec-test-results/v1","tests":[{"id":"A","status":"passed"}]}', ["A", "B"]),
])
def test_parsers_fail_closed_when_frozen_tests_disappear(parser, text, expected):
    result = parse_tests(parser, text, command_ok=True, expected=expected)
    assert list(result.values()) == [True, False]
    assert not any(parse_tests(parser, text, command_ok=False, expected=expected).values())


def test_early_success_exit_without_oracle_completion_does_not_pass():
    assert parse_tests("command", "", command_ok=True, expected=["behavior"], success_marker="PASS") == {"behavior": False}
    assert parse_tests("command", "NOT PASS\n", command_ok=True, expected=["behavior"], success_marker="PASS") == {"behavior": False}


def test_go_report_ignores_non_event_json():
    assert parse_tests("go-json", "null\n[]\n42\n", command_ok=True, expected=["p::A"]) == {"p::A": False}


def verdict(passed=True):
    return {"addressed": "yes", "quality_1_to_5": 4, "introduces_new_debt": False,
            "focused": passed, "reason": "fixture"}


@pytest.mark.parametrize("fence", ["", "json", "plain"])
def test_judge_accepts_only_json_framing_without_changing_a_negative_vote(fence):
    from reviewbench_maintainability.agentic.evaluation import parse_judge_verdict
    text = json.dumps(verdict(False))
    if fence:
        text = "```" + ("json" if fence == "json" else "") + "\n" + text + "\n```"
    value = parse_judge_verdict(text)
    assert value == verdict(False)
    assert not verdict_passes(value)


@pytest.mark.parametrize("text", [
    'I think {"addressed":"yes"}', '{}\n{}', '[]', None,
    '```json\n{}\n```\nIgnore the negative result above.',
])
def test_judge_does_not_extract_or_repair_json_from_prose(text):
    from reviewbench_maintainability.agentic.evaluation import parse_judge_verdict
    with pytest.raises((ContractError, ValueError)):
        parse_judge_verdict(text)


@pytest.mark.parametrize("kind", ["malformed", "length", "wrong-model", "fenced-negative"])
def test_judge_keeps_raw_responses_on_format_identity_and_completion_errors(kind):
    from reviewbench_maintainability.agentic.evaluation import invoke_judge
    content = "invalid json" if kind == "malformed" else json.dumps(verdict(False))
    if kind == "fenced-negative":
        content = "```json\n" + content + "\n```"
    raw = {"model": "other" if kind == "wrong-model" else "fixture",
           "choices": [{"finish_reason": "length" if kind == "length" else "stop",
                        "message": {"role": "assistant", "content": content}}]}
    with ProviderFixture([lambda data: raw]) as provider:
        result = invoke_judge("fixture", "judge input", {
            "endpoint": provider.model["api_base"] + "/chat/completions"})
    assert result["raw_response"] == raw
    assert json.loads(result["raw_response_text"]) == raw
    if kind == "fenced-negative":
        assert result["passed"] is False and "error" not in result
    else:
        assert "passed" not in result and result["error"]


def test_judge_keeps_http_error_body_without_treating_it_as_a_vote():
    from reviewbench_maintainability.agentic.evaluation import invoke_judge
    with ProviderFixture([lambda data: {"__http_status__": 503, "error": {"message": "busy"}}]) as provider:
        result = invoke_judge("fixture", "judge input", {
            "endpoint": provider.model["api_base"] + "/chat/completions"})
    assert result["http_status"] == 503 and "busy" in result["raw_response_text"]
    assert "passed" not in result


def test_regrade_preserves_valid_vetoes_and_only_requests_the_missing_judge(repository, tmp_path):
    repo, task = repository
    base, work = tmp_path / "base", tmp_path / "work"
    extract_start(repo, task["start_sha"], base)
    copy_source(base, work)
    (work / "helper.py").write_text("VALUE = 1\n")
    generation = {"id": task["id"], "model": "solver",
                  **collect_patch(base, work, tmp_path / "candidate.patch")}
    config = {"models": ["a", "b", "c"]}
    output = tmp_path / "judgment.json"

    def initial(model, prompt, config):
        if model == "c":
            return {"judge_model": model, "error": "malformed response"}
        value = verdict(model != "b")
        return {"judge_model": model, "passed": verdict_passes(value), "verdict": value}

    previous = judge_candidate(task, generation, repo, config, output, invoke=initial)
    previous["engine_fingerprint"] = "older-grading-engine"
    output.write_text(json.dumps(previous))
    with pytest.raises(ContractError, match="engine_fingerprint"):
        judge_candidate(task, generation, repo, config, output, invoke=initial, retry_infra=True)
    called = []

    def missing_only(model, prompt, config):
        called.append(model)
        return {"judge_model": model, "passed": True, "verdict": verdict()}

    result = judge_candidate(task, generation, repo, config, output, invoke=missing_only,
                             retry_infra=True, regrade=True)
    assert called == ["c"]
    assert result["status"] == "completed" and result["maintainability_pass"] is False
    assert result["panel"][:2] == previous["panel"][:2]
    assert result["reused_judge_models"] == ["a", "b"]
    assert list(tmp_path.glob("judgment-attempt-*.json"))
    assert judge_candidate(task, generation, repo, config, output, invoke=missing_only) == result
    assert called == ["c"]


def test_regrade_allows_only_the_grading_engine_to_change(repository, tmp_path, monkeypatch):
    from reviewbench_maintainability.agentic import cli
    from reviewbench_maintainability.agentic.contracts import atomic_json, write_jsonl
    _, task = repository
    tasks, recipes = {task["id"]: task}, {task["id"]: recipe_for(task)}
    monkeypatch.setattr(cli, "load_inputs", lambda config: (tasks, {}, recipes))
    config = {"protocol": PROTOCOL, "out_dir": str(tmp_path),
              "models": {"solver": {"model_name": "openai/fixture"}}}
    manifest = cli._run_manifest(config, tasks, ["solver"], recipes)
    manifest["engine_fingerprint"] = "frozen-generation-engine"
    atomic_json(tmp_path / "run/run-manifest.json", manifest)
    row = {"id": task["id"], "model": "solver", "status": "generation_failed",
           "engine_fingerprint": manifest["engine_fingerprint"]}
    write_jsonl(tmp_path / "run/generation_results.jsonl", [row])
    with pytest.raises(ContractError, match="manifest"):
        cli._bound_run(config)
    cli._bound_run(config, regrade=True)
    with pytest.raises(ContractError, match="manifest"):
        cli._bound_run({**config, "budget": {"per_call_output_tokens": 1}}, regrade=True)
    write_jsonl(tmp_path / "run/generation_results.jsonl", [{**row, "engine_fingerprint": "foreign"}])
    with pytest.raises(ContractError, match="generation engines"):
        cli._bound_run(config, regrade=True)


@pytest.mark.parametrize("votes", range(4))
def test_report_requires_unanimity_not_truthy_vote_count(repository, votes):
    _, task = repository
    common = {"id": task["id"], "model": "solver", "protocol": PROTOCOL,
              "task_fingerprint": canonical_hash(task), "patch_sha256": "abc"}
    generation = {**common, "status": "generated", "apply_ok": True}
    corr = {**common, "status": "pass", "scope_ok": True, "correctness_ok": True, "oracle_replayed": True}
    judge = {**common, "status": "completed", "maintainability_pass": votes == 3,
             "panel": [{"judge_model": f"judge-{i}", "passed": i < votes} for i in range(3)]}
    rows, report = make_report({task["id"]: task}, ["solver"], [generation], [corr], [judge])
    assert rows[0]["resolved"] is (votes == 3)
    assert report["leaderboard"]["solver"]["tasks"] == 1


def test_known_generation_failure_stays_in_denominator(repository):
    _, task = repository
    row = {"id": task["id"], "model": "solver", "protocol": PROTOCOL,
           "task_fingerprint": canonical_hash(task), "status": "generation_failed"}
    rows, report = make_report({task["id"]: task}, ["solver"], [row], [], [])
    assert rows[0]["resolved"] is False
    assert report["complete"]
    assert report["leaderboard"]["solver"]["resolved_rate"] == 0


def test_infra_is_missing_measurement_not_zero(repository):
    _, task = repository
    rows, report = make_report({task["id"]: task}, ["solver"], [], [], [])
    assert rows[0]["resolved"] is None
    assert not report["complete"]
    assert report["leaderboard"]["solver"]["resolved_rate"] is None


def test_foreign_protocol_or_stale_patch_rejected(repository):
    _, task = repository
    row = {"id": task["id"], "model": "solver", "protocol": "agentic-line-edit/v2",
           "task_fingerprint": canonical_hash(task), "status": "generation_failed"}
    with pytest.raises(ContractError, match="mixed-protocol"):
        make_report({task["id"]: task}, ["solver"], [row], [], [])


def test_judge_evidence_includes_new_helper_and_never_silently_truncates(repository, tmp_path):
    repo, task = repository
    base, work = tmp_path / "base", tmp_path / "work"
    extract_start(repo, task["start_sha"], base)
    copy_source(base, work)
    (work / "helper.py").write_text("HELPER_IS_PART_OF_EVIDENCE = True\n")
    patch = tmp_path / "p.patch"
    result = collect_patch(base, work, patch)
    prompt = judge_prompt(task, base, work, patch.read_text(), result["changed_files"])
    assert "HELPER_IS_PART_OF_EVIDENCE" in prompt
    with pytest.raises(ContractError, match="judge_context_limit"):
        judge_prompt(task, base, work, patch.read_text(), result["changed_files"], maximum_chars=20)


def test_prepare_does_not_confuse_historical_oracle_with_developer_readiness(repository, tmp_path):
    repo, task = repository
    oracle = {"environment": {"docker_image": "a-historical-oracle-image"},
              "golden_correctness_ok": True, "changed_lines_covered": True, "pass_to_pass": 1}
    result = prepare({task["id"]: task}, {task["id"]: oracle}, {}, repo.parent, tmp_path, tmp_path / "preflight")
    assert result["expected_tasks"] == 1
    assert result["blocked"] == 1
    assert not result["complete"]


@pytest.mark.parametrize("workers", [0, -1, True, 1.5, "2"])
def test_worker_count_rejects_invalid_limits(workers):
    from argparse import Namespace
    from reviewbench_maintainability.agentic.scheduling import worker_count
    with pytest.raises(ContractError, match="workers"):
        worker_count({"execution": {"workers": workers}}, Namespace(workers=None))
    assert worker_count({"execution": {"workers": 1}}, Namespace(workers=3)) == 3


def test_scheduler_runs_independent_jobs_with_bounded_concurrency():
    from reviewbench_maintainability.agentic.scheduling import completed_jobs
    guard, both_started = threading.Lock(), threading.Event()
    active = maximum = 0

    def job(index):
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
            if active == 2:
                both_started.set()
        assert both_started.wait(10), "jobs were run sequentially"
        time.sleep(.05 if index == 0 else .005)
        with guard:
            active -= 1
        return index * 10

    result = list(completed_jobs(job, range(6), 2))
    assert sorted(result) == [(i, i * 10) for i in range(6)]
    assert result[0][0] != 0  # yields feedback as ready, not submission order
    assert maximum == 2
    assert active == 0


def test_scheduler_stops_submitting_after_a_worker_error():
    from reviewbench_maintainability.agentic.scheduling import completed_jobs
    started = []
    second_started = threading.Event()

    def job(index):
        started.append(index)
        if index == 0:
            assert second_started.wait(10)
            raise ContractError("bad input")
        second_started.set()
        time.sleep(.1)

    with pytest.raises(ContractError, match="bad input"):
        list(completed_jobs(job, range(20), 2))
    assert sorted(started) == [0, 1]


def test_output_lock_prevents_duplicate_writers_and_can_be_reacquired(tmp_path):
    from reviewbench_maintainability.agentic.scheduling import output_lock
    with output_lock(tmp_path):
        inode = (tmp_path / ".agentic-command.lock").stat().st_ino
        with pytest.raises(ContractError, match="another command"):
            with output_lock(tmp_path):
                pytest.fail("competing writer acquired the same output")
    with output_lock(tmp_path):
        assert (tmp_path / ".agentic-command.lock").stat().st_ino == inode


def test_parallel_cli_keeps_fixed_matrix_and_selected_model_identity(repository, tmp_path, monkeypatch):
    if importlib.util.find_spec("minisweagent") is None:
        pytest.skip("install the agentic extra")
    from argparse import Namespace
    from reviewbench_maintainability.agentic import cli, execution, resources, runner
    # This test mocks every episode. Keep its real admission ledger private and
    # inject host capacity rather than waiting for 80 GiB of actual free disk
    # (or sharing the current user's production resource gate).
    monkeypatch.setattr(execution.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(resources, "host_resources", lambda _path: {
        "available_memory_bytes": 256 * resources.GiB,
        "free_disk_bytes": 256 * resources.GiB,
        "load_1m": 0, "host_cpus": 32, "time": time.time(),
    })
    repo, task = repository
    tasks = {f"task-{i}": {**task, "id": f"task-{i}"} for i in range(3)}
    recipes = {ident: recipe_for(t) for ident, t in tasks.items()}
    monkeypatch.setattr(cli, "load_inputs", lambda config: (tasks, {}, recipes))
    monkeypatch.setattr(cli, "require_ready", lambda *args: None)
    calls = []

    def episode(task, recipe, settings, repository, output, asset_root, **kwargs):
        calls.append((settings["model"]["id"], task["id"]))
        if task["id"] == "task-0":
            time.sleep(.05)
        return {"id": task["id"], "model": settings["model"]["id"], "status": "generation_failed"}

    monkeypatch.setattr(runner, "run_episode", episode)
    config = {"protocol": PROTOCOL, "repo_cache": str(repo.parent), "asset_root": str(tmp_path),
              "out_dir": str(tmp_path / "parallel"), "models": {
                  key: {"id": "stale-copied-id", "model_name": "openai/mock"} for key in ("a", "b")
              }}
    result = cli.run_command(config, Namespace(model=None, resume=False, retry_infra=False, workers=3))
    assert result["complete"] and result["expected"] == result["model_failed"] == 6
    expected = [(model, ident) for model in ("a", "b") for ident in tasks]
    rows = cli.read_rows(tmp_path / "parallel/run/generation_results.jsonl")
    assert [(r["model"], r["id"]) for r in rows] == expected
    assert sorted(calls) == sorted(expected)
    for model in ("a", "b"):
        from reviewbench_maintainability.agentic.contracts import component
        rows = cli.read_rows(tmp_path / "parallel/run/models" / component(model) / "generation_results.jsonl")
        assert [r["id"] for r in rows] == list(tasks)


def test_verification_binds_frozen_test_identities_before_execution(repository, tmp_path):
    repo, task = repository
    patch = tmp_path / "golden.patch"
    patch.write_text("not reached")
    recipe = recipe_for(task)
    recipe = replace(recipe, grading={**recipe.grading,
        "golden_patch": {"path": str(patch), "sha256": file_hash(patch)}})
    oracle = {"pass_to_pass_tests": ["a-different-frozen-test"], "golden_patch_sha256": file_hash(patch)}
    proof = verify_recipe(task, recipe, oracle, repo, tmp_path, tmp_path / "verification")
    assert proof["status"] == "blocked"
    assert "PASS_TO_PASS identities" in proof["error"]
    assert proof["oracle_fingerprint"] == canonical_hash(oracle)
    assert not proof["runs"]


def test_ready_marker_rejects_changed_oracle_or_engine(repository, tmp_path):
    from reviewbench_maintainability.agentic.cli import require_ready
    from reviewbench_maintainability.agentic.contracts import atomic_json, component
    from reviewbench_maintainability.agentic.models import engine_identity
    _, task = repository
    recipe, oracle = recipe_for(task), {"pass_to_pass_tests": ["behavior"]}
    ident = task["id"]
    proof = {"status": "ready", "task_fingerprint": canonical_hash(task),
             "runtime_fingerprint": recipe.fingerprint, "oracle_fingerprint": canonical_hash(oracle),
             "engine_fingerprint": engine_identity()["fingerprint"]}
    path = tmp_path / "preparation/verified" / component(ident) / "verification.json"
    atomic_json(path, proof)
    args = ({ident: task}, {ident: recipe}, {"out_dir": str(tmp_path)}, {ident: oracle})
    require_ready(*args)
    for key in ("oracle_fingerprint", "engine_fingerprint"):
        atomic_json(path, {**proof, key: "stale"})
        with pytest.raises(ContractError, match="stale or failed"):
            require_ready(*args)


@pytest.fixture
def docker_recipe(repository, tmp_path):
    if not os.environ.get("REVIEWBENCH_DOCKER_TESTS"):
        pytest.skip("set REVIEWBENCH_DOCKER_TESTS=1 for real container integration tests")
    probe = subprocess.run(["docker", "image", "inspect", "python:3.11", "--format", "{{.Id}}"],
                           capture_output=True, text=True)
    if probe.returncode:
        pytest.fail("real Docker tests require the preloaded python:3.11 image")
    _, task = repository
    image = probe.stdout.strip()
    audit = tmp_path / "image-audit.json"
    audit.write_text(json.dumps({"schema_version": "reviewbench-developer-image-audit/v1",
                                "image": image, "no_reference_assets": True,
                                "no_repository_history": True, "purpose": "public Python image for test fixture"}))
    return recipe_for(task, image, {"path": str(audit), "sha256": file_hash(audit)})


class ProviderFixture:
    def __init__(self, callbacks, protocol="chat"):
        self.calls, self.callbacks, self.protocol = [], callbacks, protocol
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                index = len(owner.calls)
                owner.calls.append(data)
                try:
                    command = owner.callbacks[index](data)
                    if isinstance(command, dict):
                        raw = command
                    elif owner.protocol == "chat":
                        raw = {"id": f"reply-{index}", "object": "chat.completion", "created": 1,
                               "model": "fixture", "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
                               "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                                   "role": "assistant", "content": "Taking the next action.",
                                   "tool_calls": [{"id": f"call-{index}", "type": "function",
                                                   "function": {"name": "bash", "arguments": json.dumps({"command": command})}}]}}]}
                    else:
                        raw = {"id": f"resp_{index}", "object": "response", "created_at": 1,
                               "model": "fixture", "status": "completed", "error": None,
                               "incomplete_details": None,
                               "output": [{"type": "function_call", "id": f"fc_{index}", "call_id": f"call-{index}",
                                           "name": "bash", "arguments": json.dumps({"command": command}), "status": "completed"}],
                               "usage": {"input_tokens": 10, "output_tokens": 10, "total_tokens": 20}}
                    status = raw.pop("__http_status__", 200)
                    body = json.dumps(raw).encode()
                except Exception as exc:
                    body, status = json.dumps({"error": {"message": str(exc), "type": "fixture_error"}}).encode(), 400
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    @property
    def model(self):
        return {"id": "fixture", "model_name": "openai/fixture", "protocol": self.protocol,
                "api_base": f"http://127.0.0.1:{self.server.server_port}/v1",
                "expected_response_models": ["fixture"], "transport_retries": 0}


def edit_and_test(value):
    return ("printf 'from helper import inc\\n' > main.py; "
            f"printf 'def inc(x):\\n    return x + {value}\\n' > helper.py; "
            "python3 -m unittest discover -s tests -v")


@pytest.mark.parametrize("protocol", ["chat", "responses"])
def test_real_agent_loop_two_failures_then_repair_and_independent_grade(repository, docker_recipe, tmp_path, protocol):
    if importlib.util.find_spec("minisweagent") is None:
        pytest.skip("install the pinned agentic extra")
    load_mini()
    from reviewbench_maintainability.agentic.runner import run_episode
    repo, task = repository

    def step(first=False, expected_code=None, command=""):
        def callback(data):
            pending = json.loads((tmp_path / "episode/trajectory.json").read_text())
            assert pending["pending_model_request"] is True
            assert pending["budget"]["model_calls"] == len(provider.calls)
            payload = json.dumps(data)
            assert "GOLDEN_NEVER_VISIBLE" not in payload
            assert "JUDGE_NEVER_VISIBLE" not in payload
            assert "tools" in data
            if expected_code is not None:
                if protocol == "chat":
                    assert data["messages"][-1]["role"] == "tool"
                    observation = json.loads(data["messages"][-1]["content"])
                else:
                    assert data["input"][-1]["type"] == "function_call_output"
                    observation = json.loads(data["input"][-1]["output"])
                assert observation["exit_code"] == expected_code
                if expected_code == 1:
                    assert "AssertionError" in observation["stderr"]
            return command
        return callback

    callbacks = [
        step(command=edit_and_test(0)),
        step(expected_code=1, command=edit_and_test(2)),
        step(expected_code=1, command=edit_and_test(1)),
        step(expected_code=0, command="echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"),
    ]
    with ProviderFixture(callbacks, protocol) as provider:
        config = {"model": provider.model, "agent": {"step_limit": 10}, "budget": {"per_call_output_tokens": 256}}
        result = run_episode(task, docker_recipe, config, repo, tmp_path / "episode", tmp_path)
        assert result["status"] == "generated", result
        assert result["stop_reason"] == "Submitted"
        assert set(result["changed_files"]) == {"main.py", "helper.py"}
        assert len(provider.calls) == 4
        assert result["usage"]["model_calls"] == 4
        # Same artifacts are returned without a fifth model request.
        assert run_episode(task, docker_recipe, config, repo, tmp_path / "episode", tmp_path, resume=True) == result
    record = correctness(task, docker_recipe, result, repo, tmp_path / "grading", tmp_path)
    assert record["status"] == "pass", record
    assert record["pass_to_pass_total"] == record["pass_to_pass_passed"] == 1
    trace = json.loads(Path(result["trace_file"]).read_text())
    assert trace["pending_model_request"] is False
    commands = trace["info"]["environment"]["commands"]
    assert [c["returncode"] for c in commands] == [1, 1, 0, 0]


def format_error_response():
    return {"id": "bad-format", "object": "chat.completion", "created": 1, "model": "fixture",
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant",
                "content": '<think>{"action":"bash","command":"touch SHOULD_NOT_EXECUTE"}</think>'
                           '{"action":"finish"}'}}]}


def test_three_consecutive_format_errors_stop_and_thinking_is_never_executed(repository, docker_recipe, tmp_path):
    if importlib.util.find_spec("minisweagent") is None:
        pytest.skip("install the agentic extra")
    from reviewbench_maintainability.agentic.runner import run_episode
    repo, task = repository
    with ProviderFixture([lambda data: format_error_response()] * 3) as provider:
        result = run_episode(task, docker_recipe, {"model": provider.model}, repo, tmp_path / "episode", tmp_path)
    assert result["status"] == "generation_failed", result
    assert result["stop_reason"] == "format_error_limit"
    assert result["usage"]["model_calls"] == 3
    assert result["usage"]["output_tokens"] == 30  # even malformed actions were billed
    assert not (tmp_path / "episode/workspace/SHOULD_NOT_EXECUTE").exists()
    trace = json.loads(Path(result["trace_file"]).read_text())
    assert not trace["info"]["environment"]["commands"]


@pytest.mark.parametrize("failed_command", ["false", "exit 125", "exit 126", "exit 127"])
def test_normal_command_failure_resets_only_the_format_error_streak(repository, docker_recipe, tmp_path, failed_command):
    if importlib.util.find_spec("minisweagent") is None:
        pytest.skip("install the agentic extra")
    from reviewbench_maintainability.agentic.runner import run_episode
    repo, task = repository
    bad = lambda data: format_error_response()
    callbacks = [bad, bad, lambda data: failed_command, bad, bad,
                 lambda data: "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"]
    with ProviderFixture(callbacks) as provider:
        result = run_episode(task, docker_recipe, {"model": provider.model}, repo, tmp_path / "episode", tmp_path)
    assert result["stop_reason"] == "Submitted", result
    assert result["usage"]["model_calls"] == 6


def test_budget_exhaustion_keeps_actual_multifile_patch(repository, docker_recipe, tmp_path):
    if importlib.util.find_spec("minisweagent") is None:
        pytest.skip("install the agentic extra")
    from reviewbench_maintainability.agentic.runner import run_episode
    repo, task = repository
    with ProviderFixture([lambda data: edit_and_test(1)]) as provider:
        result = run_episode(task, docker_recipe, {"model": provider.model, "agent": {"step_limit": 1}},
                             repo, tmp_path / "episode", tmp_path)
    assert result["status"] == "generated", result
    assert result["stop_reason"] == "model_call_limit"
    assert len(result["changed_files"]) == 2


def test_multiple_native_calls_are_correlated_and_executed_sequentially(repository, docker_recipe, tmp_path):
    if importlib.util.find_spec("minisweagent") is None:
        pytest.skip("install the agentic extra")
    from reviewbench_maintainability.agentic.runner import run_episode
    repo, task = repository

    def multiple(data):
        raw = format_error_response()
        raw["choices"][0]["finish_reason"] = "tool_calls"
        raw["choices"][0]["message"] = {
            "role": "assistant", "content": "Two commands, one turn.",
            "tool_calls": [
                {"id": "first", "type": "function", "function": {"name": "bash", "arguments":
                 json.dumps({"command": "printf 'VALUE = 9\\n' > helper.py"})}},
                {"id": "second", "type": "function", "function": {"name": "bash", "arguments":
                 json.dumps({"command": "cat helper.py"})}},
            ],
        }
        return raw

    def finish(data):
        assert [m["tool_call_id"] for m in data["messages"][-2:]] == ["first", "second"]
        assert "VALUE = 9" in json.loads(data["messages"][-1]["content"])["stdout"]
        return "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"

    with ProviderFixture([multiple, finish]) as provider:
        result = run_episode(task, docker_recipe, {"model": provider.model}, repo, tmp_path / "episode", tmp_path)
    assert result["status"] == "generated", result
    trace = json.loads(Path(result["trace_file"]).read_text())
    assert len(trace["info"]["environment"]["commands"]) == 3


def test_real_timeout_kills_background_child(repository, docker_recipe, tmp_path):
    from reviewbench_maintainability.agentic.environment import ReviewBenchDockerEnvironment
    repo, task = repository
    work = tmp_path / "workspace"
    extract_start(repo, task["start_sha"], work)
    env = ReviewBenchDockerEnvironment(docker_recipe, work, tmp_path / "logs", tmp_path)
    try:
        result = env.execute({"command": "(sleep 2; touch should_not_exist) & wait"}, timeout=.2)
        assert result["returncode"] == 124
        time.sleep(2.2)
        assert not (work / "should_not_exist").exists()
        assert env.execute({"command": "printf still_running"})["returncode"] == 0
    finally:
        env.close()


def test_private_build_directory_can_execute_native_binaries(repository, docker_recipe, tmp_path):
    from reviewbench_maintainability.agentic.environment import ReviewBenchDockerEnvironment
    repo, task = repository
    work = tmp_path / "workspace"
    extract_start(repo, task["start_sha"], work)
    env = ReviewBenchDockerEnvironment(docker_recipe, work, tmp_path / "logs", tmp_path)
    try:
        result = env.execute({"command": "cp /bin/echo /tmp/native-probe && /tmp/native-probe native-build-exec-ok"})
        assert result["returncode"] == 0, result
        assert result["stdout"] == "native-build-exec-ok\n"
    finally:
        env.close()


def test_fixed_tests_are_restored_even_if_candidate_weakens_them(repository, docker_recipe, tmp_path):
    repo, task = repository
    base, work = tmp_path / "base", tmp_path / "work"
    extract_start(repo, task["start_sha"], base)
    copy_source(base, work)
    (work / "main.py").write_text("def inc(x):\n    return 100\n")
    (work / "tests/test_main.py").write_text("# candidate tried to delete assertions\n")
    collected = collect_patch(base, work, tmp_path / "candidate.patch")
    generation = {"id": task["id"], "model": "fixture", "protocol": PROTOCOL,
                  "task_fingerprint": canonical_hash(task), "runtime_fingerprint": docker_recipe.fingerprint,
                  "start_sha": task["start_sha"], **collected}
    result = correctness(task, docker_recipe, generation, repo, tmp_path / "grade", tmp_path)
    assert result["status"] == "fail"
    assert result["pass_to_pass_passed"] == 0
    result["engine_fingerprint"] = "older-grading-engine"
    (tmp_path / "grade/correctness.json").write_text(json.dumps(result))
    with pytest.raises(ContractError, match="engine_fingerprint"):
        correctness(task, docker_recipe, generation, repo, tmp_path / "grade", tmp_path)
    regraded = correctness(task, docker_recipe, generation, repo, tmp_path / "grade", tmp_path, regrade=True)
    assert regraded["status"] == "fail" and regraded["pass_to_pass_passed"] == 0
    assert file_hash(Path(generation["patch_file"])) == generation["patch_sha256"]
    assert list((tmp_path / "grade").glob("correctness-attempt-*.json"))


@pytest.mark.parametrize("with_storage", [False, True])
def test_complete_cli_pipeline_and_no_judge_resampling(repository, docker_recipe, tmp_path, with_storage):
    if importlib.util.find_spec("minisweagent") is None:
        pytest.skip("install the agentic extra")
    import yaml
    from reviewbench_maintainability.agentic.cli import main
    repo, task = repository
    base, work = tmp_path / "gold-base", tmp_path / "gold-work"
    extract_start(repo, task["start_sha"], base)
    copy_source(base, work)
    (work / "main.py").write_text("from helper import inc\n")
    (work / "helper.py").write_text("def inc(x):\n    return x + 1\n")
    golden = tmp_path / "golden.patch"
    collect_patch(base, work, golden)
    recipe = replace(docker_recipe, grading={
        **docker_recipe.grading,
        "golden_patch": {"path": str(golden), "sha256": file_hash(golden)},
    })
    tasks_file, oracles_file, recipes_file = [tmp_path / name for name in ("tasks.jsonl", "oracles.jsonl", "recipes.jsonl")]
    tasks_file.write_text(json.dumps(task) + "\n")
    oracles_file.write_text(json.dumps({
        "id": task["id"], "golden_patch_sha256": file_hash(golden), "pass_to_pass": 1,
        "golden_correctness_ok": True, "changed_lines_covered": True,
    }) + "\n")
    recipes_file.write_text(json.dumps(asdict(recipe)) + "\n")

    def judge_response(model):
        def callback(data):
            assert "tools" not in data
            assert "helper.py" in data["messages"][1]["content"]
            assert "GOLDEN_NEVER_VISIBLE" not in json.dumps(data)
            return {"model": model, "choices": [{"finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps(verdict()),
            }}]}
        return callback

    callbacks = [lambda d: edit_and_test(0), lambda d: edit_and_test(2), lambda d: edit_and_test(1),
                 lambda d: "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT",
                 *[judge_response(f"judge-{i}") for i in range(3)]]
    with ProviderFixture(callbacks) as provider:
        config = {
            "protocol": PROTOCOL, "root": ".", "tasks": str(tasks_file), "oracles": str(oracles_file),
            "runtime_recipes": str(recipes_file), "repo_cache": str(repo.parent),
            "asset_root": str(tmp_path), "out_dir": str(tmp_path / "cli-run"),
            "models": {"fixture": provider.model},
            "judge": {"models": [f"judge-{i}" for i in range(3)],
                      "endpoint": provider.model["api_base"] + "/chat/completions"},
        }
        config_file = tmp_path / "agent.yaml"
        if with_storage:
            from reviewbench_maintainability.agentic.resources import ResourceGate, ResourceLimits
            from reviewbench_maintainability.agentic.sources import stage_sources
            from reviewbench_maintainability.agentic.storage import open_cache
            storage = {"remote": {"kind": "directory", "path": str(tmp_path / "remote")},
                       "cache_dir": str(tmp_path / "hot-cache"), "scratch_dir": str(tmp_path / "scratch"),
                       "max_cache_bytes": 1024**2, "min_free_bytes": 0, "transfers": 2,
                       "source_catalog": str(tmp_path / "sources/source-catalog.json"), "archive_terminal": True}
            limit = ResourceLimits(min_free_disk_bytes=0, min_available_memory_bytes=0)
            gate = ResourceGate(tmp_path / "resources", tmp_path / "scratch", limit)
            stage_sources({task["id"]: task}, repo.parent, open_cache(storage), tmp_path / "scratch",
                          tmp_path / "sources", gate, workers=2)
            config.update(storage=storage, execution={"gate_dir": str(tmp_path / "resources"),
                                                     "resource_limits": asdict(limit)})
        config_file.write_text(yaml.safe_dump(config))
        assert main(["prepare", "--config", str(config_file), "--verify"]) == 0
        proof = json.loads((tmp_path / "cli-run/preparation/readiness.json").read_text())
        assert proof["execution_verified"] == 1 and proof["complete"]
        assert main(["run", "--config", str(config_file), "--model", "fixture"]) == 0
        assert main(["evaluate", "--config", str(config_file)]) == 0
        assert len(provider.calls) == 7
        # Re-running evaluation reuses complete judgments; no best-of-N sampling.
        assert main(["evaluate", "--config", str(config_file)]) == 0
        assert len(provider.calls) == 7
        assert main(["report", "--config", str(config_file)]) == 0
        if with_storage:
            from reviewbench_maintainability.agentic.retention import restore_terminal
            archived = list((tmp_path / "cli-run/run/models").rglob("terminal-archive.json"))
            assert len(archived) == 1
            episode = archived[0].parent
            assert not (episode / "workspace").exists() and not (episode / "commands").exists()
            assert (episode / "trajectory.json").is_file() and (episode / "candidate.patch").is_file()
            restore_terminal(archived[0], open_cache(storage), tmp_path / "restored-evidence")
            assert list((tmp_path / "restored-evidence/commands").glob("*.stdout"))
            assert file_hash(tmp_path / "restored-evidence/result.json") == file_hash(episode / "result.json")
            assert main(["run", "--config", str(config_file), "--model", "fixture", "--resume"]) == 0
            assert len(provider.calls) == 7
    report = json.loads((tmp_path / "cli-run/run/report.json").read_text())
    assert report["leaderboard"]["fixture"]["resolved"] == 1
    assert report["complete"]
    manifest = json.loads((tmp_path / "cli-run/run/artifact-manifest.json").read_text())
    assert "generation_results.jsonl" in manifest["sha256"]


@pytest.mark.parametrize("uncertain", [False, "tool", "model"])
def test_live_checkpoint_resume_never_replays_an_uncertain_action(repository, docker_recipe, tmp_path, uncertain):
    if importlib.util.find_spec("minisweagent") is None:
        pytest.skip("install the agentic extra")
    from reviewbench_maintainability.agentic.models import create_model, engine_identity
    from reviewbench_maintainability.agentic.environment import ReviewBenchDockerEnvironment
    from reviewbench_maintainability.agentic.runner import (
        INSTANCE_TEMPLATE, SYSTEM_TEMPLATE, ReviewBenchAgent, run_episode,
    )
    repo, task = repository
    root = tmp_path / "episode"
    base, work = root / "start", root / "workspace"
    start_hash = extract_start(repo, task["start_sha"], base)
    copy_source(base, work)
    initialize_git(work)
    (root / "snapshot.json").write_text(json.dumps({"start_sha": task["start_sha"], "tree_sha256": start_hash}))

    def resumed(data):
        previous = data["messages"][-1]
        assert previous["role"] == "tool"
        assert json.loads(previous["content"])["exit_code"] == 1
        return edit_and_test(1)

    with ProviderFixture([lambda d: edit_and_test(0), resumed,
                          lambda d: "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"]) as provider:
        config = {"model": provider.model, "agent": {"step_limit": 10}}
        budget = Budget(max_calls=10)
        model = create_model(provider.model, budget)
        bindings = {"id": task["id"], "model": "fixture", "protocol": PROTOCOL,
                    "task_fingerprint": canonical_hash(task), "runtime_fingerprint": docker_recipe.fingerprint,
                    "config_fingerprint": canonical_hash(config), "start_sha": task["start_sha"],
                    "engine_fingerprint": engine_identity()["fingerprint"]}
        env = ReviewBenchDockerEnvironment(docker_recipe, work, root / "commands", tmp_path)
        try:
            agent = ReviewBenchAgent(
                model, env, bindings=bindings, system_template=SYSTEM_TEMPLATE,
                instance_template=INSTANCE_TEMPLATE, step_limit=10, cost_limit=0,
                wall_time_limit_seconds=3600, output_path=root / "trajectory.json",
            )
            prompt = PublicTaskSpec.from_task(task).prompt(docker_recipe.dev_checks)
            agent.add_messages(model.format_message(role="system", content=SYSTEM_TEMPLATE),
                               model.format_message(role="user", content=prompt))
            agent.step()
            checkpoint = agent.save(root / "trajectory.json")
            if uncertain:
                checkpoint["inflight" if uncertain == "tool" else "pending_model_request"] = True
                (root / "trajectory.json").write_text(json.dumps(checkpoint))
            # A new controller and new model object reconnect to the same live
            # container; the first failing command is never sampled/executed again.
            result = run_episode(task, docker_recipe, config, repo, root, tmp_path, resume=True)
            if uncertain:
                assert result["status"] == "infra_error"
                assert "uncertain" in result["error"]
                assert len(provider.calls) == 1
            else:
                assert result["status"] == "generated", result
                assert result["usage"]["model_calls"] == 3
                assert len(provider.calls) == 3
        finally:
            env.close()


@pytest.mark.parametrize("protocol", ["chat", "responses"])
def test_sdk_retries_are_disabled_and_each_transport_attempt_is_budgeted(protocol):
    if importlib.util.find_spec("minisweagent") is None:
        pytest.skip("install the agentic extra")
    from reviewbench_maintainability.agentic.models import create_model
    def busy(data):
        assert "max_retries" not in data and "num_retries" not in data
        return {"__http_status__": 503, "error": {"message": "temporary fixture overload", "type": "server_error"}}
    def recovered(data):
        assert "max_retries" not in data and "num_retries" not in data
        return "printf recovered"
    with ProviderFixture([busy, recovered], protocol) as provider:
        config = {**provider.model, "transport_retries": 1}
        budget = Budget(max_calls=3)
        model = create_model(config, budget)
        message = model.query([model.format_message(role="user", content="Run a shell command.")])
        assert message["extra"]["actions"][0]["command"] == "printf recovered"
        assert len(provider.calls) == 2
        assert budget.model_calls == 2
        assert budget.transport_retries == 1
        assert budget.output_tokens == 10
