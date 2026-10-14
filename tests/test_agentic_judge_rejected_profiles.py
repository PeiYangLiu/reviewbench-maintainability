"""Only two frozen-response profiles may authorize one rejected-member intent."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_agentic_judge_member_recovery import fixture as original_fixture, configure_recovery
import recover_agentic_judgments as repair
from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash, component


PROFILE_NAMES = (
    "rippled5996-gemini-length-v1",
    "openthread8966-gemini-control-character-v1",
)
MODELS = ["gpt-4.1", "gpt-5.6-sol", "gemini-3.6-flash"]
RESPONSE_MODELS = {
    "gpt-4.1": ["gpt-4.1-2025-04-14"],
    "gpt-5.6-sol": ["gpt-5.6-sol-2026-07-09"],
    "gemini-3.6-flash": ["gemini-3.6-flash"],
}


def vote(model, passed):
    verdict = {"addressed": "yes" if passed else "no", "quality_1_to_5": 5 if passed else 1,
               "introduces_new_debt": not passed, "focused": True,
               "reason": "unchanged fixture accepted vote, including this negative"}
    raw = {"model": RESPONSE_MODELS[model][0],
           "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(verdict)}}]}
    return {"judge_model": model, "http_status": 200, "passed": passed, "verdict": verdict,
            "raw_response": raw, "raw_response_text": json.dumps(raw)}


@pytest.fixture(params=["length", "control"])
def profiled(request, tmp_path, monkeypatch):
    """Use local fixture pins; production profiles are checked separately below."""
    f = original_fixture.__wrapped__(tmp_path)
    kind = request.param
    name = PROFILE_NAMES[kind == "control"]
    config = {"models": MODELS, "expected_response_models": RESPONSE_MODELS,
              "max_context_chars": 300000, "max_output_tokens": 4096, "timeout_seconds": 180}
    f.config.clear()
    f.config.update(copy.deepcopy(config))
    if kind == "length":
        content = '{"addressed":"yes","quality_1_to_5":5,'
        error = "InfrastructureError: judge response did not complete"
        finish = "length"
    else:
        content = ('{"addressed":"yes","quality_1_to_5":5,"introduces_new_debt":false,'
                   '"focused":true,"reason":"retained\nunescaped control character"}')
        with pytest.raises(json.JSONDecodeError) as caught:
            repair.parse_judge_verdict(content)
        error, finish = f"JSONDecodeError: {caught.value}", "stop"
    raw = {"model": "gemini-3.6-flash",
           "choices": [{"finish_reason": finish, "message": {"content": content}}]}
    failed = {"judge_model": "gemini-3.6-flash", "http_status": 200, "error": error,
              "raw_response": raw, "raw_response_text": json.dumps(raw)}
    f.previous.update(
        panel=[vote("gpt-4.1", True), vote("gpt-5.6-sol", kind != "control"), failed],
        judge_fingerprint=canonical_hash({"rubric": repair.RUBRIC, "version": repair.RUBRIC_VERSION,
                                          "config": f.config}))
    f.selection.update(
        generation_sha256=canonical_hash(f.generation), judgment_sha256=canonical_hash(f.previous),
        correctness_sha256=canonical_hash(f.correctness), judge_models=["gemini-3.6-flash"])
    profile = {
        **{key: f.selection[key] for key in (
            "id", "model", "generation_sha256", "judgment_sha256", "correctness_sha256",
            "judge_engine", "judge_models")},
        "error": error, "member_sha256": canonical_hash(failed),
        "raw_response_text_sha256": hashlib.sha256(failed["raw_response_text"].encode()).hexdigest(),
        "finish_reason": finish,
    }
    monkeypatch.setattr(repair, "REJECTED_MEMBER_PROFILES", {name: copy.deepcopy(profile)})
    f.selection["rejected_member_profile"] = repair.rejected_profile_descriptor(name, profile)
    f.kind, f.profile_name, f.profile, f.failed = kind, name, profile, failed
    return f


def configure(f, monkeypatch):
    request, events = configure_recovery(f, monkeypatch, None)
    original_judge = repair.judge_candidate

    def retaining_judge(*args, **kwargs):
        output = Path(args[4])
        previous = json.loads(output.read_text())
        repair.atomic_json(output.with_name(f"judgment-attempt-{canonical_hash(previous)[:16]}.json"), previous)
        result = original_judge(*args, **kwargs)
        result["reused_judge_models"] = sorted(m["judge_model"] for m in previous["panel"] if "passed" in m)
        repair.atomic_json(output, result)
        return result

    monkeypatch.setattr(repair, "judge_candidate", retaining_judge)
    return request, events


def test_production_profiles_are_only_the_two_exact_original_unaccepted_responses():
    assert set(repair.REJECTED_MEMBER_PROFILES) == set(PROFILE_NAMES)
    expected = {
        PROFILE_NAMES[0]: (
            "github__XRPLF__rippled__pull_5996__comment_2635153432__dead_code",
            "0d5532fa7b5138ba2b13d25d5a5e41b1c6e4a8ebc57fe6ad0eeb2c1e3d9d70c4",
            "133e1152a5b4365c39290bed7c7a9c248de1d69afb1e1f803e38b1cee28a7165",
            "440c165a8259420d565747a7e46aa66c0ab8d0c9d24d07c7e900d9ef16694eb1",
            "InfrastructureError: judge response did not complete", "length"),
        PROFILE_NAMES[1]: (
            "github__openthread__openthread__pull_8966__comment_1171970977__modularity",
            "201acaa191d6bf383d06e9d07126b874a492f61ead3da23b36c2d1101cb82886",
            "e39696123fab2626121b880d427397909fc8eda47bd04f09c43fda94f006808c",
            "9dab649f413c737943ec0f2b668b34063e7f093c08e96a4d7dadbe2606d6aa91",
            "JSONDecodeError: Invalid control character at: line 6 column 161 (char 256)", "stop"),
    }
    for name, wanted in expected.items():
        p = repair.REJECTED_MEMBER_PROFILES[name]
        assert tuple(p[k] for k in ("id", "generation_sha256", "judgment_sha256",
                                    "member_sha256", "error", "finish_reason")) == wanted
        assert p["judge_engine"] == "5142d1fec94eb6c5a6177a66ee7ce8533a26f0a2578280075a6947a399c01ca3"
        assert p["model"] == "gpt-5.4-responses" and p["judge_models"] == ["gemini-3.6-flash"]


def test_default_retry_predicate_still_rejects_both_kinds_and_accepted_negative_votes(profiled):
    f = profiled
    assert repair.retryable_member(f.failed) is False
    assert repair.retryable_member(vote("gpt-5.6-sol", False)) is False
    assert repair.validate_rejected_member_profile(f.selection, f.generation, f.previous, f.config) == f.profile


def test_parent_selection_validation_requires_the_exact_profile_descriptor(profiled):
    f = profiled
    repair.validate_selections([f.selection], {"task": f.task}, ["solver"])
    wrong = copy.deepcopy(f.selection)
    wrong["rejected_member_profile"]["error"] = "JSONDecodeError: another unaccepted response"
    with pytest.raises(ContractError, match="profile"):
        repair.validate_selections([wrong], {"task": f.task}, ["solver"])


def test_no_profile_means_no_intent_or_invocation(profiled, monkeypatch):
    f = profiled
    request, _ = configure(f, monkeypatch)
    request["selection"] = {k: v for k, v in f.selection.items() if k != "rejected_member_profile"}
    with pytest.raises(ContractError, match="accepted/unretryable"):
        repair.recover_one(request, invoke=lambda *_: pytest.fail("API called"))
    assert not (f.tmp_path / "output/members").exists()


@pytest.mark.parametrize("field", [
    "id", "model", "generation_sha256", "judgment_sha256", "correctness_sha256",
    "judge_engine", "judge_models", "profile-name", "profile-error", "profile-member",
    "profile-schema", "profile-model", "profile-count", "profile-count-bool", "profile-extra", "profile-null",
])
def test_changed_explicit_selector_cannot_obtain_an_intent(profiled, monkeypatch, field):
    f = profiled
    request, _ = configure(f, monkeypatch)
    request = copy.deepcopy(request)
    selected = request["selection"]
    if not field.startswith("profile-"):
        selected[field] = ["gpt-4.1"] if field == "judge_models" else "foreign"
    else:
        key = field.removeprefix("profile-")
        if key == "null":
            selected["rejected_member_profile"] = None
        else:
            target = {"name": "name", "error": "error", "member": "original_member_sha256",
                      "schema": "schema_version", "model": "judge_model", "count": "max_new_attempts",
                      "count-bool": "max_new_attempts", "extra": "extra"}[key]
            selected["rejected_member_profile"][target] = True if key == "count-bool" else 2 if key == "count" else "foreign"
    with pytest.raises(ContractError):
        repair.recover_one(request, invoke=lambda *_: pytest.fail("API called"))
    assert not (f.tmp_path / "output/members").exists()


@pytest.mark.parametrize("field", ["generation", "judgment", "correctness", "member-error",
                                  "member-passed", "member-verdict", "raw-text", "finish-reason"])
def test_changed_original_input_is_not_authorized(profiled, monkeypatch, field):
    f = profiled
    request, _ = configure(f, monkeypatch)
    request = copy.deepcopy(request)
    if field in ("generation", "judgment", "correctness"):
        request["row"][field]["changed"] = True
    else:
        value = request["row"]["judgment"]["panel"][-1]
        if field == "member-error":
            value["error"] += " same class, different response"
        elif field == "member-passed":
            value["passed"] = False
        elif field == "member-verdict":
            value["verdict"] = {}
        elif field == "raw-text":
            value["raw_response_text"] += " "
        else:
            value["raw_response"]["choices"][0]["finish_reason"] = "end_turn"
    with pytest.raises((ContractError, ValueError)):
        repair.recover_one(request, invoke=lambda *_: pytest.fail("API called"))
    assert not (f.tmp_path / "output/members").exists()


@pytest.mark.parametrize("change", ["context-cap", "output-cap", "rubric", "identity"])
def test_fixed_request_rules_are_not_relaxed_by_the_profile(profiled, monkeypatch, change):
    f = profiled
    config = copy.deepcopy(f.config)
    if change == "context-cap":
        config["max_context_chars"] += 1
    elif change == "output-cap":
        config["max_output_tokens"] += 1
    elif change == "identity":
        config["expected_response_models"]["gpt-5.6-sol"].append("gpt-5.6-sol")
    else:
        monkeypatch.setattr(repair, "RUBRIC", repair.RUBRIC + "\nchanged")
    with pytest.raises(ContractError):
        repair.validate_rejected_member_profile(f.selection, f.generation, f.previous, config)
    assert not (f.tmp_path / "output/members").exists()


@pytest.mark.parametrize("new_passed", [True, False])
def test_profile_makes_one_intent_preserves_old_invalid_body_and_two_votes(profiled, monkeypatch, new_passed):
    f = profiled
    request, events = configure(f, monkeypatch)
    before = copy.deepcopy(f.row)
    calls = []

    def invoke(model, prompt, config):
        calls.append(model)
        assert model == "gemini-3.6-flash" and prompt == f.prompt and config == f.config
        return vote(model, new_passed)

    record = repair.recover_one(request, invoke=invoke)
    assert calls == ["gemini-3.6-flash"] and events == ["admitted", "released"]
    assert record["row"]["generation"] == before["generation"]
    assert record["row"]["correctness"] == before["correctness"]
    assert record["row"]["judgment"]["panel"][:2] == before["judgment"]["panel"][:2]
    assert record["original_judgment"] == before["judgment"]
    assert record["row"]["judgment"]["panel"][-1]["passed"] is new_passed
    assert record["row"]["judgment"]["status"] == "completed"
    assert record["row"]["judgment"]["maintainability_pass"] is (new_passed and f.kind == "length")
    assert record["judge_request_attempts"] == 1 and f.row == before
    journal = Path(request["output"]) / "members" / component("gemini-3.6-flash")
    binding = json.loads((journal / "started.json").read_text())
    assert binding["rejected_member_profile"] == f.selection["rejected_member_profile"]
    assert binding["generation_sha256"] == canonical_hash(f.generation)
    assert binding["judgment_sha256"] == canonical_hash(f.previous)
    assert json.loads((journal / "response.json").read_text())["binding"] == binding
    archive = Path(request["output"]) / f"judgment-attempt-{canonical_hash(f.previous)[:16]}.json"
    assert json.loads(archive.read_text()) == before["judgment"]
    assert repair.recover_one(request, invoke=lambda *_: pytest.fail("second API request")) == record


def test_unfinished_intent_is_not_repeated(profiled, monkeypatch):
    f = profiled
    request, events = configure(f, monkeypatch)
    calls = []

    def interrupted(*args):
        calls.append(True)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        repair.recover_one(request, invoke=interrupted)
    journal = Path(request["output"]) / "members" / component("gemini-3.6-flash")
    original_intent = (journal / "started.json").read_bytes()
    # A resumed process has a fresh resource mock, but retains the same disk journal.
    events.clear()
    record = repair.recover_one(request, invoke=lambda *_: pytest.fail("request repeated after missing response"))
    assert len(calls) == 1
    assert (journal / "started.json").read_bytes() == original_intent
    assert not (journal / "response.json").exists()
    assert record["judge_request_attempts"] == 1
    assert record["row"]["judgment"]["status"] == "infra_error"
    assert "no automatic repeat" in record["row"]["judgment"]["panel"][-1]["error"]


def test_another_invalid_response_is_retained_without_a_second_call(profiled, monkeypatch):
    f = profiled
    request, _ = configure(f, monkeypatch)
    calls = []

    def invalid(*args):
        calls.append(True)
        return copy.deepcopy(f.failed)

    record = repair.recover_one(request, invoke=invalid)
    assert len(calls) == 1 and record["row"]["judgment"]["status"] == "infra_error"
    assert "passed" not in record["row"]["judgment"]["panel"][-1]
    assert record["row"]["judgment"]["panel"][:2] == f.previous["panel"][:2]
    assert repair.recover_one(request, invoke=lambda *_: pytest.fail("invalid response retried")) == record
    with pytest.raises(ContractError, match="generation/judgment changed"):
        repair.validate_rejected_member_profile(
            f.selection, f.generation, record["row"]["judgment"], f.config)


def test_accepted_member_never_gets_an_intent_even_with_profile(profiled):
    f = profiled
    callback = repair.JournaledJudge(f.previous, MODELS, f.tmp_path / "journal",
                                     invoke=lambda *_: pytest.fail("accepted vote invoked"),
                                     selection=f.selection, generation=f.generation)
    for model in MODELS[:2]:
        with pytest.raises(ContractError, match="accepted judge"):
            callback(model, f.prompt, f.config)
    assert not (f.tmp_path / "journal").exists()


def test_profile_publish_changes_only_target_of_full402_and_keeps_negative_vote(profiled, monkeypatch):
    f = profiled
    request, _ = configure(f, monkeypatch)
    record = repair.recover_one(request, invoke=lambda model, *_: vote(model, True))
    tasks = {"task": f.task, **{f"other-{i}": {"id": f"other-{i}"} for i in range(401)}}
    rows = {}
    for ident, task in tasks.items():
        row = copy.deepcopy(f.row)
        row["id"] = ident
        for key in ("generation", "correctness", "judgment"):
            row[key].update(id=ident, task_fingerprint=canonical_hash(task))
        if ident != "task":
            row["judgment"].update(status="completed", maintainability_pass=True,
                                   panel=[vote(model, True) for model in MODELS])
        rows[(ident, "solver")] = row
    before = copy.deepcopy(rows)
    output = f.tmp_path / "full402-fixture-NOT-PUBLISHABLE"
    output.mkdir()
    manifest = {"source_run": "/fixture/original", "base_run": "/fixture/full402",
                "generation_engine_fingerprint": "a" * 64, "base_host_engine_fingerprint": "b" * 64}
    report = repair.publish(output, tasks, ["solver"], rows, {("task", "solver"): record}, manifest, {})
    after = repair.read_base_rows(output, tasks, ["solver"])
    assert len(after) == report["expected_pairs"] == report["task_count"] == 402
    for pair, row in before.items():
        assert after[pair]["generation"] == row["generation"]
        assert after[pair]["correctness"] == row["correctness"]
        if pair[0] != "task":
            assert after[pair]["judgment"] == row["judgment"]
    assert after[("task", "solver")]["judgment"]["panel"][:2] == f.previous["panel"][:2]
    if f.kind == "control":
        assert after[("task", "solver")]["judgment"]["panel"][1]["passed"] is False
        assert after[("task", "solver")]["judgment"]["maintainability_pass"] is False
    assert rows == before and report["additional_generation_calls"] == report["additional_correctness_calls"] == 0
