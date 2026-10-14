"""Controller lifecycle controls; callbacks never contact models or execute SUTs."""
from copy import deepcopy
from pathlib import Path
import json
import sys
import threading
import hashlib

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from recover_agentic_progressive import (
    complete_measurements, requires_recovery, recovered_generation, first_panel_invoker,
)
import host_recovery_ownership as ownership
from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash, component


def source_row(status="processed"):
    return {
        "id": "case", "model": "model", "status": status,
        "generation": {
            "id": "case", "model": "model", "status": "generated", "protocol": "reviewbench-agentic-exec/v1",
            "engine_fingerprint": "a" * 64, "runtime_fingerprint": "b" * 64,
            "task_fingerprint": "c" * 64, "patch_sha256": "d" * 64,
            "usage": {"model_calls": 20, "input_tokens": 448806, "output_tokens": 8884, "transport_retries": 0},
        },
        "correctness": {"status": "infra_error",
                        "error": "ContractError: immutable test missing at start: tests/original-link",
                        "oracle_replayed": False},
    }


def panel(source, status="completed", votes=(True, False, False)):
    return {
        **{k: source["generation"][k] for k in ("id", "model", "protocol", "task_fingerprint", "patch_sha256")},
        "status": status,
        "panel": [{"judge_model": "judge-" + str(i), "passed": passed} for i, passed in enumerate(votes)],
    }


def call(original, row, root, evaluate, judge):
    return complete_measurements(original, row, {}, object(), root, root,
                                 {"asset_root": str(root), "judge": {"models": ["a", "b", "c"]}},
                                 evaluate=evaluate, judge=judge)


@pytest.mark.parametrize("status", [None, "", "incomplete", "running", "waiting", "failed"])
def test_nonterminal_generated_pair_is_not_admitted(status):
    source = source_row(status)
    assert not requires_recovery(source)


def test_paused_primary_is_not_mistaken_for_an_absent_panel(tmp_path):
    original = source_row("incomplete")
    written, release = threading.Event(), threading.Event()
    callbacks = {"correctness": 0, "judges": 0}
    def primary():
        written.set()
        assert release.wait(5)
        original["judgment"] = panel(original)
        original["status"] = "processed"
    thread = threading.Thread(target=primary)
    thread.start()
    assert written.wait(5)
    assert not requires_recovery(original)
    def evaluate(*args, **kwargs):
        callbacks["correctness"] += 1
        return {"status": "pass"}
    def judge(*args, **kwargs):
        callbacks["judges"] += 3
        raise AssertionError("correctness worker issued a judge request")
    with pytest.raises(ContractError, match="terminal"):
        call(original, deepcopy(original), tmp_path, evaluate, judge)
    assert callbacks == {"correctness": 0, "judges": 0}
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert requires_recovery(original)
    before, repaired = deepcopy(original), deepcopy(original)
    call(original, repaired, tmp_path, evaluate, judge)
    assert callbacks == {"correctness": 1, "judges": 0}
    assert repaired["judgment"] == before["judgment"]
    assert original == before and repaired["generation"] == original["generation"]


@pytest.mark.parametrize("kind", ["completed-negative", "completed-positive", "partial", "rejected", "absent"])
def test_correctness_only_never_fills_or_replaces_a_primary_panel(tmp_path, kind):
    original = source_row()
    if kind == "completed-negative":
        original["judgment"] = panel(original)
    elif kind == "completed-positive":
        original["judgment"] = panel(original, votes=(True, True, True))
    elif kind == "partial":
        original["judgment"] = panel(original, status="infra_error")
        original["judgment"]["panel"] = original["judgment"]["panel"][:1]
    elif kind == "rejected":
        original["judgment"] = {"status": "infra_error", "panel": [], "error": "context admission rejected"}
    before = deepcopy(original)
    repaired = deepcopy(original)
    repaired["judgment"] = {"status": "completed", "panel": [{"passed": True}], "wrong_owner": True}
    calls = []
    def evaluate(*args, **kwargs):
        calls.append("correctness")
        return {"status": "pass"}
    def judge(*args, **kwargs):
        calls.append("judge")
        raise AssertionError("unexpected judge dispatch")
    call(original, repaired, tmp_path, evaluate, judge)
    assert calls == ["correctness"]
    assert repaired.get("judgment") == original.get("judgment")
    assert original == before and repaired["generation"] == original["generation"]


@pytest.mark.parametrize("status", ["pass", "fail", "scope_error", "apply_error"])
def test_completed_correctness_verdict_is_not_executed_again(tmp_path, status):
    original = source_row()
    original["judgment"] = panel(original)
    repaired = deepcopy(original)
    repaired["correctness"] = {"status": status, "tests": {"original": status == "pass"}}
    expected = deepcopy(repaired["correctness"])
    def bomb(*args, **kwargs):
        raise AssertionError("completed work was replayed")
    call(original, repaired, tmp_path, bomb, bomb)
    assert repaired["correctness"] == expected
    assert repaired["judgment"] == original["judgment"]


def serialized_row():
    original = source_row("incomplete")
    original["generation"]["status"] = "infra_error"
    original["generation"]["error"] = "completed response retained; host serialization failed"
    original.pop("correctness")
    row = deepcopy(original)
    row["generation"] = recovered_generation(
        original["generation"], {"apply_ok": True},
        {"messages": [{"extra": {"exit_status": "RepeatedFormatError"}}]},
        "host-engine", {"method": "byte-exact-archived-terminal-workspace"})
    return original, row


def write_panel_marker(root, original, repaired):
    value = {"source_generation_fingerprint": canonical_hash(original["generation"]),
             "recovered_generation_fingerprint": canonical_hash(repaired["generation"]),
             "judge_fingerprint": canonical_hash({"models": ["a", "b", "c"]}),
             "generation_calls": 0, "maximum_first_panel_starts": 1}
    (root / "first-panel-started.json").write_text(json.dumps(value))
    return value


def test_serialization_has_one_first_panel_and_preserves_model_budget(tmp_path):
    original, repaired = serialized_row()
    before = deepcopy(original)
    calls = {"correctness": 0, "judges": 0}
    def evaluate(*args, **kwargs):
        calls["correctness"] += 1
        return {"status": "pass"}
    def judge(task, generation, repo, config, path, **kwargs):
        calls["judges"] += 3
        assert kwargs["retry_infra"] is False
        value = panel({"generation": generation})
        path.write_text(json.dumps(value))
        return value
    call(original, repaired, tmp_path, evaluate, judge)
    first = deepcopy(repaired)
    call(original, repaired, tmp_path, evaluate, judge)
    assert calls == {"correctness": 1, "judges": 3}
    assert repaired == first and original == before
    assert repaired["generation"]["usage"] == original["generation"]["usage"]
    assert repaired["generation"]["engine_fingerprint"] == original["generation"]["engine_fingerprint"]
    assert (tmp_path / "first-panel-started.json").is_file()


def test_started_without_response_stays_consumed(tmp_path):
    original, repaired = serialized_row()
    repaired["correctness"] = {"status": "pass"}
    write_panel_marker(tmp_path, original, repaired)
    def bomb(*args, **kwargs):
        raise AssertionError("ambiguous first panel was retried")
    with pytest.raises(ContractError, match="stays consumed"):
        call(original, repaired, tmp_path, bomb, bomb)


def test_cached_partial_serialization_panel_is_not_retried(tmp_path):
    original, repaired = serialized_row()
    repaired["correctness"] = {"status": "pass"}
    partial = panel(repaired, status="infra_error")
    partial["panel"] = partial["panel"][:1]
    write_panel_marker(tmp_path, original, repaired)
    (tmp_path / "judgment.json").write_text(json.dumps(partial))
    def bomb(*args, **kwargs):
        raise AssertionError("cached partial panel was resampled")
    call(original, repaired, tmp_path, bomb, bomb)
    assert repaired["judgment"] == partial


def test_serialization_does_not_claim_an_existing_source_panel(tmp_path):
    original, repaired = serialized_row()
    original["judgment"] = {"status": "infra_error", "panel": [], "error": "already consumed"}
    repaired["correctness"] = {"status": "pass"}
    def bomb(*args, **kwargs):
        raise AssertionError("source-owned judging was repeated")
    with pytest.raises(ContractError, match="already owns"):
        call(original, repaired, tmp_path, bomb, bomb)


def test_cached_panel_cannot_switch_answer(tmp_path):
    original, repaired = serialized_row()
    repaired["correctness"] = {"status": "pass"}
    bad = panel(repaired)
    bad["patch_sha256"] = "e" * 64
    write_panel_marker(tmp_path, original, repaired)
    (tmp_path / "judgment.json").write_text(json.dumps(bad))
    def bomb(*args, **kwargs):
        raise AssertionError("different-answer cache was dispatched")
    with pytest.raises(ContractError, match="differs"):
        call(original, repaired, tmp_path, bomb, bomb)


def test_inherited_budget_and_runtime_are_not_rebound():
    source = source_row()
    key = ("case", "model", canonical_hash(source["generation"]))
    inherited = {key: {"row": deepcopy(source)}}
    ownership.validate_inherited_sources(inherited, {key[:2]: source}, source["generation"]["engine_fingerprint"])
    inherited[key]["row"]["generation"]["usage"]["model_calls"] = 0
    with pytest.raises(ContractError, match="consumed budget"):
        ownership.validate_inherited_sources(inherited, {key[:2]: source}, source["generation"]["engine_fingerprint"])


@pytest.mark.parametrize("votes", [(False, False, False), (True, False, True), (True, True, True)])
def test_primary_panel_ownership_does_not_select_by_votes(monkeypatch, votes):
    source = source_row()
    source["judgment"] = panel(source, votes=votes)
    duplicate = panel(source, votes=tuple(not v for v in votes))
    base = {"generation": [source["generation"]], "correctness": [{"id": "case", "model": "model", "status": "pass"}],
            "judgment": [duplicate]}
    monkeypatch.setattr(ownership, "validate_completed_judgment", lambda *args: None)
    tables, proof = ownership.effective_tables(base, {("case", "model"): source}, {}, {}, "a" * 64)
    assert tables["judgment"][("case", "model")] == source["judgment"]
    assert proof["changed_primary_panels"] == [["case", "model"]]
    assert base["judgment"] == [duplicate]


def test_new_correctness_merge_keeps_downstream_partial_panel(monkeypatch):
    source = source_row()
    source["judgment"] = panel(source, status="infra_error")
    source["judgment"]["panel"] = source["judgment"]["panel"][:1]
    downstream = panel(source, votes=(True, False, True))
    base = {"generation": [source["generation"]], "correctness": [{"id": "case", "model": "model", "status": "infra_error"}],
            "judgment": [downstream]}
    selected = deepcopy(source)
    selected["correctness"] = {"id": "case", "model": "model", "status": "pass"}
    token = ("case", "model", canonical_hash(source["generation"]))
    tables, proof = ownership.effective_tables(
        base, {token[:2]: source}, {token: {"row": selected, "completed": True}}, {}, "a" * 64)
    assert tables["correctness"][token[:2]] == selected["correctness"]
    assert tables["judgment"][token[:2]] == downstream
    assert proof["changed_primary_panels"] == []


def test_unfinished_local_record_does_not_replace_base():
    source = source_row()
    base = {"generation": [source["generation"]], "correctness": [], "judgment": []}
    token = ("case", "model", canonical_hash(source["generation"]))
    tables, proof = ownership.effective_tables(
        base, {token[:2]: source}, {token: {"row": deepcopy(source), "completed": False}}, {}, "a" * 64)
    assert tables["correctness"] == tables["judgment"] == {}
    assert len(proof["pending"]) == 1 and proof["merged_local"] == []


def test_first_serialization_members_reuse_exact_negative_responses(tmp_path):
    config = {"models": ["a", "b", "c"]}
    binding = {"original": "a" * 64, "recovered": "b" * 64}
    calls = []
    def invoke(model, prompt, config):
        calls.append(model)
        return {"judge_model": model, "passed": False, "raw_response": {"id": model + "-first"}}
    request = first_panel_invoker(tmp_path, binding, invoke=invoke)
    expected = {model: request(model, "fixed full prompt", config) for model in config["models"]}
    for model in config["models"]:
        assert request(model, "fixed full prompt", config) == expected[model]
    assert calls == config["models"]


def test_crashed_member_stays_consumed_while_unstarted_sibling_can_run(tmp_path):
    config, binding, calls = {"models": ["a", "b"]}, {"original": "fixed"}, []
    def invoke(model, prompt, config):
        calls.append(model)
        if model == "a":
            raise KeyboardInterrupt("process ended after first member start")
        return {"judge_model": model, "passed": True}
    request = first_panel_invoker(tmp_path, binding, invoke=invoke)
    with pytest.raises(KeyboardInterrupt):
        request("a", "prompt", config)
    with pytest.raises(ContractError, match="consumed"):
        request("a", "prompt", config)
    assert request("b", "prompt", config)["passed"] is True
    assert calls == ["a", "b"]


@pytest.mark.parametrize("change", ["prompt", "binding", "response-model", "orphan-response", "response-link"])
def test_first_member_claim_does_not_reset_identity(tmp_path, change):
    config, binding, calls = {"models": ["a"]}, {"original": "first"}, []
    def invoke(model, prompt, config):
        calls.append(model)
        return {"judge_model": model, "passed": False}
    request = first_panel_invoker(tmp_path, binding, invoke=invoke)
    request("a", "prompt", config)
    folder = tmp_path / "first-panel-members" / component("a")
    if change == "prompt":
        prompt = "different prompt"
    else:
        prompt = "prompt"
    if change == "binding":
        request = first_panel_invoker(tmp_path, {"original": "other"}, invoke=invoke)
    elif change == "response-model":
        value = json.loads((folder / "response.json").read_text())
        value["response"]["judge_model"] = "foreign"
        (folder / "response.json").write_text(json.dumps(value))
    elif change == "orphan-response":
        (folder / "started.json").unlink()
    elif change == "response-link":
        (folder / "response.json").rename(folder / "response-original.json")
        (folder / "response.json").symlink_to("response-original.json")
    with pytest.raises(ContractError):
        request("a", prompt, config)
    assert calls == ["a"]


def test_completed_original_member_survives_failed_siblings(monkeypatch):
    source = source_row()
    full = panel(source)
    full.update(judge_fingerprint="e" * 64, evidence_sha256="f" * 64)
    source["judgment"] = deepcopy(full)
    source["judgment"]["status"] = "infra_error"
    for number in (0, 1):
        source["judgment"]["panel"][number] = {"judge_model": "judge-" + str(number), "error": "rejected"}
    current = panel(source, votes=(True, True, True))
    current.update(judge_fingerprint="e" * 64, evidence_sha256="f" * 64, maintainability_pass=True)
    base = {"generation": [source["generation"]], "correctness": [], "judgment": [current]}
    monkeypatch.setattr(ownership, "accepted_primary_members",
                        lambda *args: {"judge-2": source["judgment"]["panel"][2]})
    tables, proof = ownership.effective_tables(
        base, {("case", "model"): source}, {}, {"models": ["judge-0", "judge-1", "judge-2"]}, "a" * 64)
    actual = tables["judgment"][("case", "model")]
    assert actual["panel"][:2] == current["panel"][:2]
    assert actual["panel"][2] == source["judgment"]["panel"][2]
    assert actual["maintainability_pass"] is False
    assert proof["primary_member_corrections"][0]["judge_models"] == ["judge-2"]
