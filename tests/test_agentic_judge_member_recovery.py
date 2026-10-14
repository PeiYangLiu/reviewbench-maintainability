from contextlib import contextmanager
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import recover_agentic_judgments as repair
from reviewbench_maintainability.agentic.contracts import ContractError, PROTOCOL, canonical_hash, file_hash


def member(model, passed=True):
    verdict = {"addressed": "yes", "quality_1_to_5": 5 if passed else 3,
               "introduces_new_debt": False, "focused": True, "reason": "unchanged original reason"}
    raw = {"model": model + "-dated",
           "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(verdict)}}]}
    return {"judge_model": model, "http_status": 200, "passed": passed, "verdict": verdict,
            "raw_response": raw, "raw_response_text": json.dumps(raw)}


@pytest.fixture
def fixture(tmp_path):
    task = {"id": "task", "repo": "fixture/project", "start_sha": "c" * 40}
    patch, trace = tmp_path / "candidate.patch", tmp_path / "trajectory.json"
    patch.write_text("original completed candidate")
    trace.write_text('{"original":"trajectory"}')
    generation = {
        "id": "task", "model": "solver", "protocol": PROTOCOL, "status": "generated", "apply_ok": True,
        "task_fingerprint": canonical_hash(task), "runtime_fingerprint": "r" * 64,
        "engine_fingerprint": "a" * 64, "patch_file": str(patch), "patch_sha256": file_hash(patch),
        "trace_file": str(trace), "trace_sha256": file_hash(trace),
    }
    config = {"models": ["one", "two", "three"],
              "expected_response_models": {model: [model + "-dated"] for model in ("one", "two", "three")}}
    prompt = "complete original before/after/diff evidence"
    previous = {
        **{key: generation[key] for key in ("id", "model", "protocol", "task_fingerprint", "patch_sha256")},
        "engine_fingerprint": "a" * 64, "status": "infra_error",
        "judge_fingerprint": canonical_hash({"rubric": repair.RUBRIC, "version": repair.RUBRIC_VERSION,
                                            "config": config}),
        "evidence_complete": True, "evidence_sha256": canonical_hash(prompt),
        "panel": [
            {"judge_model": "one", "http_status": 503, "error": "HTTPError: unavailable"},
            {"judge_model": "two", "http_status": 200,
             "error": "InfrastructureError: judge model identity mismatch",
             "raw_response": {"model": "undated"}},
            member("three", False),
        ],
        "maintainability_pass": None,
    }
    correctness = {
        **{key: generation[key] for key in ("id", "model", "protocol", "task_fingerprint",
                                          "patch_sha256", "runtime_fingerprint")},
        "status": "pass", "correctness_ok": True, "scope_ok": True, "oracle_replayed": True,
    }
    row = {"id": "task", "model": "solver", "generation": generation, "judgment": previous,
           "correctness": correctness}
    selection = {
        "id": "task", "model": "solver", "generation_sha256": canonical_hash(generation),
        "judgment_sha256": canonical_hash(previous), "correctness_sha256": canonical_hash(correctness),
        "judge_engine": "a" * 64, "judge_models": ["one", "two"],
    }
    return SimpleNamespace(**locals())


def test_partial_validation_preserves_valid_negative_vote(fixture):
    f = fixture
    before = copy.deepcopy(f.previous)
    repair.validate_partial(f.previous, f.generation, f.config, "a" * 64)
    assert f.previous == before and f.previous["panel"][-1]["passed"] is False


@pytest.mark.parametrize("mutation", ["completed", "wrong-engine", "configuration", "missing-evidence",
                                    "wrong-patch", "accepted-alias", "accepted-raw", "accepted-vote"])
def test_invalid_partial_or_accepted_member_is_rejected_before_requests(fixture, mutation):
    f = fixture
    j = copy.deepcopy(f.previous)
    if mutation == "completed":
        j["status"] = "completed"
    elif mutation == "wrong-engine":
        j["engine_fingerprint"] = "b" * 64
    elif mutation == "configuration":
        j["judge_fingerprint"] = "f" * 64
    elif mutation == "missing-evidence":
        j.pop("evidence_sha256")
    elif mutation == "wrong-patch":
        j["patch_sha256"] = "f" * 64
    elif mutation == "accepted-alias":
        j["panel"][-1]["raw_response"]["model"] = "three"
    elif mutation == "accepted-raw":
        j["panel"][-1]["raw_response_text"] = "{}"
    else:
        j["panel"][-1]["passed"] = True
    with pytest.raises((ContractError, ValueError)):
        repair.validate_partial(j, f.generation, f.config, "a" * 64)


@pytest.mark.parametrize("value,expected", [
    ({"http_status": 503}, True),
    ({"error": "InfrastructureError: judge model identity mismatch", "http_status": 200}, True),
    ({"error": "TimeoutError: timed out"}, True),
    ({"error": "JSONDecodeError: invalid newline", "http_status": 200}, False),
    ({"error": "judge_context_limit"}, False),
    ({"passed": False, "http_status": 200}, False),
    ({"http_status": 401}, False),
])
def test_retry_policy_does_not_reroll_valid_votes_or_malformed_substantive_json(value, expected):
    assert repair.retryable_member(value) is expected


def test_journal_makes_one_request_and_reuses_its_exact_negative_response_after_restart(fixture):
    f = fixture
    calls = []
    def invoke(model, prompt, config):
        calls.append((model, prompt, config))
        return member(model, False)
    path = f.tmp_path / "journal"
    first = repair.JournaledJudge(f.previous, ["one"], path, invoke=invoke)
    original = first("one", f.prompt, f.config)
    second = repair.JournaledJudge(f.previous, ["one"], path, invoke=invoke)
    assert second("one", f.prompt, f.config) == original
    assert original["passed"] is False and len(calls) == 1


def test_changed_evidence_is_rejected_without_an_invocation_intent(fixture):
    f = fixture
    path = f.tmp_path / "journal"
    callback = repair.JournaledJudge(f.previous, ["one"], path, invoke=lambda *_: pytest.fail("API invoked"))
    with pytest.raises(ContractError, match="evidence differs"):
        callback("one", "truncated or changed evidence", f.config)
    assert not path.exists()


def test_unselected_rejected_member_is_returned_unchanged_without_retry(fixture):
    f = fixture
    callback = repair.JournaledJudge(f.previous, ["one"], f.tmp_path / "journal",
                                     invoke=lambda *_: pytest.fail("API invoked"))
    assert callback("two", f.prompt, f.config) == f.previous["panel"][1]
    with pytest.raises(ContractError, match="accepted judge"):
        callback("three", f.prompt, f.config)


def test_crash_after_intent_without_response_does_not_launch_another_request(fixture):
    f = fixture
    def interrupted(*args):
        raise KeyboardInterrupt
    path = f.tmp_path / "journal"
    callback = repair.JournaledJudge(f.previous, ["one"], path, invoke=interrupted)
    with pytest.raises(KeyboardInterrupt):
        callback("one", f.prompt, f.config)
    second = repair.JournaledJudge(f.previous, ["one"], path, invoke=lambda *_: pytest.fail("request repeated"))
    result = second("one", f.prompt, f.config)
    assert "outcome not recorded" in result["error"] and "passed" not in result


def test_selection_needs_complete_unique_object_bindings(fixture):
    f = fixture
    repair.validate_selections([f.selection], {"task": f.task}, ["solver"])
    for rows in ([], [f.selection, f.selection], [{**f.selection, "generation_sha256": "../../escape"}],
                 [{**f.selection, "judge_models": ["one", "one"]}], [{**f.selection, "model": "another"}]):
        with pytest.raises(ContractError):
            repair.validate_selections(rows, {"task": f.task}, ["solver"])


def configure_recovery(f, monkeypatch, invoke):
    config = {"judge": f.config, "repo_cache": str(f.tmp_path), "asset_root": str(f.tmp_path)}
    monkeypatch.setattr(repair, "load_config", lambda _: copy.deepcopy(config))
    monkeypatch.setattr(repair, "engine_identity", lambda: {"fingerprint": "a" * 64})
    monkeypatch.setattr(repair, "RuntimeRecipe", SimpleNamespace(
        from_dict=lambda _: SimpleNamespace(fingerprint=f.generation["runtime_fingerprint"])))
    monkeypatch.setattr(repair, "ExecutionServices", lambda _: SimpleNamespace(
        gate=SimpleNamespace(status=lambda: {"leases": {}})))
    events = []
    @contextmanager
    def job(*args):
        events.append("admitted")
        yield
        events.append("released")
    monkeypatch.setattr(repair, "source_judge_job", job)
    def judge(task, generation, repository, conf, output, *, invoke, retry_infra):
        assert retry_infra and events == ["admitted"]
        result = copy.deepcopy(f.previous)
        result["panel"] = [
            m if "passed" in m else invoke(m["judge_model"], f.prompt, conf)
            for m in result["panel"]
        ]
        result["status"] = "completed" if all("passed" in m for m in result["panel"]) else "infra_error"
        result["maintainability_pass"] = all(m.get("passed") is True for m in result["panel"])
        return result
    monkeypatch.setattr(repair, "judge_candidate", judge)
    request = {"config": str(f.tmp_path / "config"), "task": f.task, "row": f.row,
               "selection": f.selection, "recipe": {}, "output": str(f.tmp_path / "output")}
    return request, events


def test_recovery_only_fills_missing_members_and_keeps_answer_correctness_and_negative_vote(fixture, monkeypatch):
    f = fixture
    calls = []
    def invoke(model, *args):
        calls.append(model)
        return member(model, True)
    request, events = configure_recovery(f, monkeypatch, invoke)
    before = copy.deepcopy(f.row)
    result = repair.recover_one(request, invoke=invoke)
    assert calls == ["one", "two"] and events == ["admitted", "released"]
    assert result["row"]["generation"] == before["generation"]
    assert result["row"]["correctness"] == before["correctness"]
    assert result["row"]["judgment"]["panel"][-1] == before["judgment"]["panel"][-1]
    assert result["row"]["judgment"]["status"] == "completed"
    assert result["row"]["judgment"]["maintainability_pass"] is False
    assert result["judge_request_attempts"] == 2 and result["additional_generation_calls"] == 0
    assert f.row == before
    assert repair.recover_one(request, invoke=lambda *_: pytest.fail("repeat API call")) == result


@pytest.mark.parametrize("mutation", ["generation", "correctness", "judgment", "runtime", "identity"])
def test_selected_bindings_are_checked_before_any_request(fixture, monkeypatch, mutation):
    f = fixture
    request, _ = configure_recovery(f, monkeypatch, lambda *_: pytest.fail("API invoked"))
    request = copy.deepcopy(request)
    if mutation in {"generation", "correctness", "judgment"}:
        request["selection"][mutation + "_sha256"] = "f" * 64
    elif mutation == "runtime":
        monkeypatch.setattr(repair, "RuntimeRecipe", SimpleNamespace(
            from_dict=lambda _: SimpleNamespace(fingerprint="different")))
    else:
        request["selection"]["model"] = "another"
    with pytest.raises(ContractError):
        repair.recover_one(request, invoke=lambda *_: pytest.fail("API invoked"))


def test_full_402_publish_preserves_denominator_and_false_cached_vote(fixture):
    f = fixture
    tasks = {"task": f.task, **{f"pending-{i}": {"id": f"pending-{i}"} for i in range(401)}}
    repaired = copy.deepcopy(f.row)
    repaired["judgment"]["panel"][:2] = [member("one"), member("two")]
    repaired["judgment"].update(status="completed", maintainability_pass=False)
    record = {"selection": f.selection, "row": repaired, "judge_request_attempts": 2,
              "original_judgment_engine_fingerprint": "a" * 64}
    output = f.tmp_path / "published"
    output.mkdir()
    manifest = {"source_run": "/original", "base_run": "/old-overlay",
                "generation_engine_fingerprint": "a" * 64, "base_host_engine_fingerprint": "b" * 64}
    report = repair.publish(output, tasks, ["solver"], {("task", "solver"): f.row},
                            {("task", "solver"): record}, manifest, {})
    table = [json.loads(line) for line in (output / "results.jsonl").read_text().splitlines()]
    assert report["expected_pairs"] == report["task_count"] == len(table) == 402
    row = next(value for value in table if value["id"] == "task")
    assert row["status"] == "completed" and row["resolved"] is False
    assert report["additional_judge_calls"] == 2 and report["additional_generation_calls"] == 0
    assert report["complete"] is False
