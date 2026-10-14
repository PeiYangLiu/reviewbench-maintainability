import copy
import json
import os
from pathlib import Path

import pytest

import context_profile_overlay as overlay
import recover_agentic_context_judgments as context
import recover_agentic_judgments as retained
from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash

PROFILE = "sqlclient16-original-wide-empty-panel-v1"
FINGERPRINT = "24798f40019fd31888e4d50dcb63ad069278af973bbb0821ef9d2e66b5735056"
ORIGINAL = {
    "endpoint": "http://127.0.0.1:4000/v1/chat/completions",
    "models": ["gpt-4.1", "gpt-5.6-sol", "gemini-3.6-flash"],
    "expected_response_models": {
        "gpt-4.1": ["gpt-4.1-2025-04-14"],
        "gpt-5.6-sol": ["gpt-5.6-sol-2026-07-09"],
        "gemini-3.6-flash": ["gemini-3.6-flash"],
    },
    "max_context_chars": 300000, "max_output_tokens": 4096, "timeout_seconds": 180,
}


def metadata(profile, maximum, fingerprint):
    return {
        "schema_version": retained.CONTEXT_OVERRIDE_SCHEMA,
        "input_hashes": copy.deepcopy(profile["input_hashes"]),
        "original_judge_config": copy.deepcopy(ORIGINAL),
        "admitted_judge_config": {**copy.deepcopy(ORIGINAL), "max_context_chars": maximum},
        "original_judge_fingerprint": retained.CONTEXT_OLD_FINGERPRINT,
        "admitted_judge_fingerprint": fingerprint,
        "changed_fields": ["max_context_chars"], "max_first_attempts_per_member": 1,
    }


@pytest.fixture
def wide(monkeypatch):
    profile = {
        "name": PROFILE, "selected_originals": 1, "inherited_context_first_attempts": 42,
        "base_manifest_sha256": "f" * 64,
        "input_hashes": {"selection.json": "a" * 64, "original-case-bindings.json": "b" * 64,
                         "judge-configs.json": "c" * 64},
    }
    monkeypatch.setitem(retained.ADDITIONAL_CONTEXT_PROFILES, PROFILE, profile)
    return metadata(profile, 3200000, FINGERPRINT)


def test_original_profile_caps_and_fingerprints_remain_exact():
    profiles = [retained.context_profile(), *retained.ADDITIONAL_CONTEXT_PROFILES.values()]
    for profile in profiles:
        if profile["name"] == PROFILE:
            continue
        assert retained.context_admitted_settings(profile) == (1600000, retained.CONTEXT_NEW_FINGERPRINT)
        retained.validate_context_admission_metadata(metadata(profile, 1600000, retained.CONTEXT_NEW_FINGERPRINT))


def test_original_model_rubric_and_new_exact_fingerprint_match(wide):
    assert canonical_hash({"rubric": retained.RUBRIC, "version": retained.RUBRIC_VERSION,
                           "config": ORIGINAL}) == retained.CONTEXT_OLD_FINGERPRINT
    assert canonical_hash({"rubric": retained.RUBRIC, "version": retained.RUBRIC_VERSION,
                           "config": wide["admitted_judge_config"]}) == FINGERPRINT
    assert retained.context_admitted_settings(retained.context_profile(wide["input_hashes"])) == (
        3200000, FINGERPRINT)
    retained.validate_context_admission_metadata(wide)


@pytest.mark.parametrize("maximum", [300000, 1600000, 3199999, 6400000, True, "3200000"])
def test_wide_profile_has_no_freeform_cap(wide, maximum):
    wide["admitted_judge_config"]["max_context_chars"] = maximum
    with pytest.raises(ContractError, match="exact 300000-to-3200000"):
        retained.validate_context_admission_metadata(wide)


@pytest.mark.parametrize("field,value", [
    ("models", ["different-model"]),
    ("max_output_tokens", 8192),
    ("timeout_seconds", 360),
    ("endpoint", "http://127.0.0.1:4000/different"),
    ("expected_response_models", {"gpt-4.1": ["different-version"]}),
])
def test_cap_admission_does_not_change_other_judge_settings(wide, field, value):
    wide["admitted_judge_config"][field] = value
    with pytest.raises(ContractError):
        retained.validate_context_admission_metadata(wide)


@pytest.mark.parametrize("field,value", [
    ("admitted_judge_fingerprint", retained.CONTEXT_NEW_FINGERPRINT),
    ("original_judge_fingerprint", "0" * 64),
    ("changed_fields", ["max_context_chars", "max_output_tokens"]),
    ("max_first_attempts_per_member", 2),
    ("input_hashes", {"selection.json": "0" * 64}),
])
def test_profile_identity_and_one_shot_budget_stay_bound(wide, field, value):
    wide[field] = value
    with pytest.raises(ContractError):
        retained.validate_context_admission_metadata(wide)


def test_an_old_profile_cannot_acquire_the_wide_cap():
    old = metadata(retained.context_profile(), 3200000, FINGERPRINT)
    with pytest.raises(ContractError, match="exact 300000-to-1600000"):
        retained.validate_context_admission_metadata(old)


def test_a_different_profile_name_does_not_authorize_the_wide_cap(wide, monkeypatch):
    profile = copy.deepcopy(retained.context_profile(wide["input_hashes"]))
    profile["name"] = "different-unadmitted-name"
    monkeypatch.setitem(retained.ADDITIONAL_CONTEXT_PROFILES, PROFILE, profile)
    with pytest.raises(ContractError, match="exact 300000-to-1600000"):
        retained.validate_context_admission_metadata(wide)


def cached_fixture(admission, votes):
    answer = {
        "id": "fixture-original", "model": "gpt-5.4-responses",
        "protocol": "reviewbench-agentic-exec/v1", "task_fingerprint": "d" * 64,
        "patch_sha256": "e" * 64, "status": "generated",
    }
    correctness = {**answer, "status": "pass", "correctness_ok": True}
    previous = {
        **answer, "status": "infra_error", "panel": [], "maintainability_pass": None,
        "error": overlay.ERROR,
    }
    original = {"id": answer["id"], "model": answer["model"], "generation": answer,
                "correctness": correctness, "judgment": previous}
    members = []
    for model, passed in zip(ORIGINAL["models"], votes):
        member = {"judge_model": model}
        if passed is not None:
            member["passed"] = passed
        members.append(member)
    complete = all(value is not None for value in votes)
    judgment = {
        **answer, "status": "completed" if complete else "infra_error", "panel": members,
        "context_admission": admission, "evidence_complete": True,
        "maintainability_pass": all(votes) if complete else None,
    }
    record = {
        "schema_version": "reviewbench-context-admission-result/v1",
        "original_row": original, "row": {**copy.deepcopy(original), "judgment": judgment},
        "context_admission": admission, "judge_request_attempts": 3,
        "additional_generation_calls": 0, "additional_correctness_calls": 0,
    }
    key = answer["id"], answer["model"]
    tables = {field: {key: original[field]} for field in ("generation", "correctness", "judgment")}
    return key, tables, record


@pytest.mark.parametrize("votes,expected", [
    ([True, True, True], True), ([True, False, True], False), ([True, None, True], None),
])
def test_cached_wide_panel_preserves_unanimity_negatives_and_unknowns(wide, votes, expected):
    key, tables, record = cached_fixture(wide, votes)
    original_tables = copy.deepcopy(tables)
    tables["judgment"][("unrelated", "gpt-5.4-responses")] = {"status": "completed", "passed": False}
    combined, selected = overlay.merge(tables, {"original-owner": record})
    assert selected == {key}
    assert combined["judgment"][key]["maintainability_pass"] is expected
    assert combined["generation"] == original_tables["generation"]
    assert combined["correctness"] == original_tables["correctness"]
    assert combined["judgment"][("unrelated", "gpt-5.4-responses")] == {"status": "completed", "passed": False}


@pytest.mark.parametrize("mutation", ["second_owner", "generation", "correctness", "accepted_original",
                                     "member_order", "member_count", "boolean", "negative", "attempts",
                                     "new_generation_call", "new_correctness_call", "changed_cap"])
def test_cached_publisher_rejects_replacement_or_new_work(wide, mutation):
    key, tables, record = cached_fixture(wide, [True, False, True])
    records = {"owner": record}
    if mutation == "second_owner":
        records["another-owner"] = copy.deepcopy(record)
    elif mutation in ("generation", "correctness"):
        record["row"][mutation]["status"] = "replaced"
    elif mutation == "accepted_original":
        record["original_row"]["judgment"]["panel"] = [{"passed": False}]
    elif mutation == "member_order":
        record["row"]["judgment"]["panel"].reverse()
    elif mutation == "member_count":
        record["row"]["judgment"]["panel"].pop()
    elif mutation == "boolean":
        record["row"]["judgment"]["panel"][0]["passed"] = 1
    elif mutation == "negative":
        record["row"]["judgment"]["maintainability_pass"] = True
    elif mutation == "attempts":
        record["judge_request_attempts"] = 6
    elif mutation == "new_generation_call":
        record["additional_generation_calls"] = 1
    elif mutation == "new_correctness_call":
        record["additional_correctness_calls"] = 1
    else:
        record["context_admission"]["admitted_judge_config"]["max_context_chars"] = 6400000
    with pytest.raises(ContractError):
        overlay.merge(tables, records)


@pytest.fixture
def actual_bundle():
    root = os.environ.get("SQLCLIENT_WIDE_CONTEXT_INPUTS")
    if not root:
        pytest.skip("requires the separately sealed original SQL input bundle")
    return context.load_inputs(Path(root))


def test_actual_original_sql_bundle_retains_whole_evidence_and_original_results(actual_bundle):
    bundle = actual_bundle
    selected, = bundle["selections"]
    original = bundle["originals"][(selected["id"], selected["model"])]
    assert selected["id"] == "github__dotnet__SqlClient__pull_1608__comment_870881699__readability"
    assert selected["full_evidence_characters"] == 2468620
    assert selected["new_max_context_chars"] == 3200000
    assert selected["new_judge_fingerprint"] == FINGERPRINT
    assert original["correctness"]["status"] == "pass"
    assert original["judgment"]["panel"] == []
    assert original["generation"]["usage"]["model_calls"] == 18
    context.validate_original(bundle, selected, original, files=True)


@pytest.mark.parametrize("field,value", [
    ("new_max_context_chars", 1600000), ("new_max_context_chars", 6400000),
    ("new_judge_fingerprint", retained.CONTEXT_NEW_FINGERPRINT),
    ("full_evidence_characters", 3200001), ("judge_models", ["substitute"]),
    ("max_first_attempts_per_member", 2),
])
def test_actual_original_selection_is_not_relabelled(actual_bundle, field, value):
    bundle = copy.deepcopy(actual_bundle)
    selected = copy.deepcopy(bundle["selections"][0])
    original = bundle["originals"][(selected["id"], selected["model"])]
    selected[field] = value
    with pytest.raises(ContractError):
        context.validate_original(bundle, selected, original, files=False)


@pytest.mark.parametrize("entries", [None, {}, True, [{}]])
def test_registry_requires_exact_sealed_entry_shapes(entries):
    with pytest.raises(ContractError):
        overlay.verified_registry_records(entries, {})


def test_empty_registry_does_not_acquire_jobs_or_requests(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("cached registry reader dispatched work")
    monkeypatch.setattr(retained, "invoke_judge", forbidden)
    monkeypatch.setattr(retained, "source_judge_job", forbidden)
    assert overlay.verified_registry_records([], {}) == ({}, {})


def test_existing_original_registry_remains_readable_without_requests(monkeypatch):
    path = os.environ.get("EXISTING_CONTEXT_REGISTRY")
    bundle = os.environ.get("SQLCLIENT_WIDE_CONTEXT_INPUTS")
    if not path or not bundle:
        pytest.skip("requires real sealed original registry and SQL input bundle")
    config = retained.load_config(Path(bundle) / "experiment-original.yaml")
    tasks = retained.selected_tasks(Path(config["tasks"]), Path(config["scope_ids"]))
    def forbidden(*args, **kwargs):
        raise AssertionError("reading existing original panels dispatched work")
    monkeypatch.setattr(retained, "invoke_judge", forbidden)
    monkeypatch.setattr(retained, "source_judge_job", forbidden)
    records, bindings = overlay.verified_registry_records(json.loads(Path(path).read_text()), tasks)
    assert records.keys() == bindings.keys()
    assert sum(record["judge_request_attempts"] for record in records.values()) == 27
    assert all(record["row"]["generation"] == record["original_row"]["generation"]
               and record["row"]["correctness"] == record["original_row"]["correctness"]
               for record in records.values())
