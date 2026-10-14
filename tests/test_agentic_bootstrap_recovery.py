"""Bounded zero-call recovery controls; synthetic fixtures never count as evaluation."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import recover_agentic_bootstrap as recovery
from agentic_runtime_migration.go_modfile import RUNTIME, repaired_recipe
from reviewbench_maintainability.agentic.contracts import (
    ContractError, InfrastructureError, PROTOCOL, atomic_json, canonical_hash, file_hash, write_jsonl,
)
from test_agentic_exec import recipe_for


@pytest.fixture
def failed(tmp_path):
    task = {"id": "fixture", "repo": "fixture/repo", "start_sha": "0" * 40,
            "task_instruction": "fixture", "locality": {"path": "main.go"}}
    config = {"models": {"solver": {"model_name": "fixture-no-network"}},
              "repo_cache": str(tmp_path / "repos"), "asset_root": str(tmp_path / "assets"),
              "judge": {"models": ["j1", "j2", "j3"]}}
    root = tmp_path / "original"
    generation = {
        "id": task["id"], "model": "solver", "protocol": PROTOCOL, "task_fingerprint": canonical_hash(task),
        "start_sha": task["start_sha"], "engine_fingerprint": "fixture-engine", "runtime_fingerprint": "old-runtime",
        "config_fingerprint": canonical_hash(recovery.model_settings(config, "solver")), "status": "infra_error",
        "stop_reason": "infrastructure_error", "error": "InfrastructureError: bootstrap changed original source: go.mod",
        "usage": {}, "apply_ok": False, "changed_files": [], "changed_lines": 0,
        "patch_file": None, "patch_sha256": None, "trace_file": str(root / "episode/trajectory.json"),
    }
    atomic_json(root / "episode/result.json", generation)
    row = {"id": task["id"], "model": "solver", "generation": generation, "generation_started": True}
    atomic_json(root / "pair-result.json", row)
    selection = {"id": task["id"], "model": "solver", "source_path": "go.mod",
                 "source_generation_fingerprint": canonical_hash(generation),
                 "source_result_sha256": file_hash(root / "episode/result.json"),
                 "original_runtime_fingerprint": "old-runtime"}
    return SimpleNamespace(task=task, config=config, root=root, row=row, selection=selection)


def validate(failed):
    return recovery.validate_zero_call_failure(failed.row, failed.root, failed.task, failed.config,
                                               failed.selection, "fixture-engine")


def recover(failed, recipe, services, root, **kwargs):
    return recovery.recover_first_episode(
        failed.row, failed.task, {}, recipe, services, failed.config, root, failed.selection,
        claim_file=failed.root.parent / "claims/source-pair.json",
        preflight=kwargs.pop("preflight", lambda: validate(failed)), **kwargs)


def generated_record(failed, recipe, episode, status="generated"):
    episode = Path(episode)
    bindings = {"id": failed.task["id"], "model": "solver", "protocol": PROTOCOL,
                "task_fingerprint": canonical_hash(failed.task), "start_sha": failed.task["start_sha"],
                "runtime_fingerprint": recipe.fingerprint, "engine_fingerprint": "fixture-engine",
                "config_fingerprint": canonical_hash(recovery.model_settings(failed.config, "solver"))}
    trace = episode / "trajectory.json"
    atomic_json(trace, {"bindings": bindings, "pending_model_request": False, "inflight": False})
    patch = episode / "candidate.patch"
    patch.write_text("source fixture patch\n")
    value = {**bindings, "status": status, "apply_ok": status == "generated", "usage": {"model_calls": 1},
             "trace_file": str(trace), "trace_sha256": file_hash(trace),
             "patch_file": str(patch) if status == "generated" else None,
             "patch_sha256": file_hash(patch) if status == "generated" else None}
    atomic_json(episode / "result.json", value)
    return value


def complete_correctness(generation, output):
    result = {key: generation[key] for key in (
        "id", "model", "protocol", "task_fingerprint", "patch_sha256", "engine_fingerprint", "runtime_fingerprint")}
    result.update(status="pass", scope_ok=True, correctness_ok=True, oracle_replayed=True)
    atomic_json(Path(output) / "correctness.json", result)
    return result


def complete_negative_panel(generation, config, output):
    result = {key: generation[key] for key in (
        "id", "model", "protocol", "task_fingerprint", "patch_sha256", "engine_fingerprint")}
    verdict = {"addressed": "no", "quality_1_to_5": 1, "introduces_new_debt": True, "focused": True, "reason": "fixture"}
    panel = []
    for model in config["models"]:
        raw = {"model": model, "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(verdict)}}]}
        panel.append({"judge_model": model, "passed": False, "verdict": verdict, "http_status": 200,
                      "raw_response": raw, "raw_response_text": json.dumps(raw)})
    result.update(status="completed", maintainability_pass=False, panel=panel, evidence_complete=True,
                  evidence_sha256="d" * 64,
                  judge_fingerprint=canonical_hash({"rubric": recovery.RUBRIC, "version": recovery.RUBRIC_VERSION, "config": config}))
    atomic_json(Path(output), result)
    return result


def test_exact_pinned_premodel_failure_is_admitted_without_changing_original(failed):
    before = (failed.root / "episode/result.json").read_bytes()
    assert validate(failed) == failed.row["generation"]
    assert (failed.root / "episode/result.json").read_bytes() == before


@pytest.mark.parametrize("change", [
    {"status": "generated"}, {"status": "generation_failed"}, {"usage": {"model_calls": 0}},
    {"usage": {"model_calls": 1}}, {"trace_sha256": "a" * 64}, {"patch_file": "answer.patch"},
    {"apply_ok": True}, {"changed_files": ["answer.go"]},
    {"error": "InfrastructureError: request timed out"},
    {"config_fingerprint": "other-settings"}, {"engine_fingerprint": "other-engine"},
])
def test_empty_usage_alone_does_not_license_a_fresh_episode(failed, change):
    failed.row["generation"].update(change)
    with pytest.raises(ContractError):
        validate(failed)


@pytest.mark.parametrize("name,data", [
    ("episode/trajectory.json", {"pending_model_request": True}),
    ("episode/trajectory.json", {"pending_model_request": False, "messages": []}),
    ("episode/retry-001/trajectory.json", {"messages": []}),
    ("episode-attestation-old/trajectory.json", {"messages": []}),
])
def test_any_original_or_uncertain_trace_blocks_zero_call_recovery(failed, name, data):
    atomic_json(failed.root / name, data)
    with pytest.raises(ContractError, match="episode/trace"):
        validate(failed)


@pytest.mark.parametrize("field", ["correctness", "judgment", "answer_recovery_required", "previous_infrastructure_attempt"])
def test_existing_downstream_or_recovery_evidence_is_not_overwritten(failed, field):
    failed.row[field] = {}
    with pytest.raises(ContractError):
        validate(failed)


def test_changed_original_result_and_archive_bindings_fail_closed(failed):
    atomic_json(failed.root / "episode/terminal-archive.json", {"binding": {
        "result_sha256": failed.selection["source_result_sha256"], "trace_sha256": "a" * 64}})
    with pytest.raises(ContractError, match="archive contradicts"):
        validate(failed)
    (failed.root / "episode/terminal-archive.json").unlink()
    (failed.root / "episode/result.json").write_text("{}")
    with pytest.raises(ContractError, match="result is missing or changed"):
        validate(failed)


def recipes(task):
    old = recipe_for(task)
    old = replace(old, env={"REVIEWBENCH_GO": "/opt/reviewbench-assets/go/bin/go",
                           "REVIEWBENCH_GO_MODULE_DIR": "/workspace", "SOURCE_ROOT": "/workspace",
                           "PATH": "/usr/bin", "GOFLAGS": "-mod=readonly -p=2"},
                  assets=[{"kind": "archive", "visibility": "developer", "target": RUNTIME,
                           "path": "/old-runtime.json", "sha256": "a" * 64}],
                  dev_checks=["/opt/reviewbench-assets/go/bin/go test -run '^$' ./..."])
    new = repaired_recipe(old, {"path": "/new-runtime.json", "sha256": "b" * 64})
    return old, new


def test_repair_requires_same_assertions_goldens_limits_and_exact_six_replays(failed):
    old, new = recipes(failed.task)
    oracle = {"id": failed.task["id"]}
    selection = {"original_runtime_fingerprint": old.fingerprint, "repaired_runtime_fingerprint": new.fingerprint}
    proof = {"status": "ready", "runtime_fingerprint": new.fingerprint,
             "task_fingerprint": canonical_hash(failed.task), "oracle_fingerprint": canonical_hash(oracle),
             "engine_fingerprint": "fixture-engine",
             "runs": [{"variant": v, "repetition": n, "tests": {"original": True}}
                      for v in ("start", "golden") for n in range(3)]}
    recovery.validate_runtime_repair(old, new, proof, failed.task, oracle, selection, "fixture-engine")
    with pytest.raises(ContractError):
        recovery.validate_runtime_repair(old, replace(new, memory_mb=new.memory_mb + 1), proof,
                                         failed.task, oracle, selection, "fixture-engine")
    for invalid in ({**proof, "runs": proof["runs"][:-1]}, {**proof, "engine_fingerprint": "another"},
                    {**proof, "runs": [*proof["runs"][:-1], {**proof["runs"][-1], "tests": {"original": False}}]}):
        with pytest.raises(ContractError):
            recovery.validate_runtime_repair(old, new, invalid, failed.task, oracle, selection, "fixture-engine")


class Services:
    cache = None
    fail_admission = False

    @contextmanager
    def job(self, *args, **kwargs):
        if self.fail_admission:
            raise InfrastructureError("resource admission timed out: scratch_bytes_reservation")
        yield {}


def test_first_episode_runs_once_and_completed_answers_and_negative_votes_are_reused(failed, tmp_path):
    _, recipe = recipes(failed.task)
    calls = []
    root = tmp_path / "recovery"
    def generate(*args, **kwargs):
        assert kwargs == {"resume": True, "retry_infra": False}
        assert json.loads((root / "recovery.json").read_text())["first_generation_started"] is True
        calls.append("generation")
        return generated_record(failed, recipe, args[4])

    def evaluate(*args, **kwargs):
        assert kwargs == {"retry_infra": False}
        calls.append("correctness")
        return complete_correctness(args[2], args[4])

    def judge(*args, **kwargs):
        assert kwargs["retry_infra"] is False and callable(kwargs["invoke"])
        calls.append("judgment")
        return complete_negative_panel(args[1], args[3], args[4])

    for _ in range(2):
        value = recover(failed, recipe, Services(), root, generate=generate, evaluate=evaluate, judge=judge)
    assert calls == ["generation", "correctness", "judgment"]
    assert value["row"]["judgment"]["maintainability_pass"] is False
    assert value["original_generation"] == failed.row["generation"]


def test_resource_wait_does_not_mark_first_episode_started(failed, tmp_path):
    _, recipe = recipes(failed.task)
    services = Services()
    services.fail_admission = True
    calls = []
    root = tmp_path / "recovery"
    value = recover(failed, recipe, services, root, generate=lambda *a, **k: calls.append(1))
    assert not value["first_generation_started"] and calls == []
    assert "resource admission timed out" in value["row"]["error"]


def test_uncertain_first_recovery_episode_is_not_resampled(failed, tmp_path):
    _, recipe = recipes(failed.task)
    root = tmp_path / "recovery"
    calls = []

    def uncertain(*args, **kwargs):
        calls.append(1)
        atomic_json(root / "episode/trajectory.json", {"pending_model_request": True})
        raise InfrastructureError("request outcome uncertain")

    with pytest.raises(InfrastructureError, match="request outcome uncertain"):
        recover(failed, recipe, Services(), root, generate=uncertain)
    first = json.loads((root / "recovery.json").read_text())
    with pytest.raises(ContractError, match="no automatic replay"):
        recover(failed, recipe, Services(), root, generate=uncertain)
    second = json.loads((root / "recovery.json").read_text())
    assert calls == [1] and first["first_generation_started"] and second["first_generation_started"]
    assert second["integrity_error"]


def test_overlay_keeps_full402_denominator_and_rejects_replaced_source(failed, tmp_path):
    base, out = tmp_path / "base", tmp_path / "output"
    tasks = {failed.task["id"]: failed.task, **{
        f"other-{n}": {**failed.task, "id": f"other-{n}"} for n in range(401)}}
    for name, rows in (("generation_results", [failed.row["generation"]]), ("correctness", []), ("judgments", [])):
        write_jsonl(base / (name + ".jsonl"), rows)
    generation = {**failed.row["generation"], "status": "generation_failed"}
    record = {"row": {"id": failed.task["id"], "model": "solver", "generation": generation},
              "first_generation_started": True, "original_generation_fingerprint": canonical_hash(failed.row["generation"])}
    record["validated_row_fingerprint"] = canonical_hash(record["row"])
    report = recovery.publish(base, out, tasks, ["solver"], {"fixture": record})
    assert report["expected_pairs"] == report["task_count"] == 402 and report["complete"] is False
    assert report["resampled_completed_answers"] == 0 and report["first_generation_episodes"] == 1
    write_jsonl(base / "generation_results.jsonl", [generation])
    with pytest.raises(ContractError, match="base answer changed"):
        recovery.publish(base, out, tasks, ["solver"], {"fixture": record})


@pytest.mark.parametrize("existing", ["answer", "accepted_vote"])
def test_base_conflicts_are_checked_before_any_model_or_judge_call(failed, tmp_path, existing):
    _, recipe = recipes(failed.task)
    base = tmp_path / "base"
    generation = copy.deepcopy(failed.row["generation"])
    judgments = []
    if existing == "answer":
        generation["usage"] = {"model_calls": 1}
    else:
        judgments = [{"id": failed.task["id"], "model": "solver", "status": "completed",
                      "panel": [{"judge_model": "old", "passed": False}]}]
    for name, rows in (("generation_results", [generation]), ("correctness", []), ("judgments", judgments)):
        write_jsonl(base / (name + ".jsonl"), rows)
    calls = []
    with pytest.raises(ContractError, match="base"):
        recover(failed, recipe, Services(), tmp_path / "new",
                preflight=lambda: recovery.validate_base_failure(base, failed.row),
                generate=lambda *a, **k: calls.append("generation"),
                judge=lambda *a, **k: calls.append("judge"))
    assert calls == []


def test_base_is_rechecked_after_waiting_for_shared_admission(failed, tmp_path):
    _, recipe = recipes(failed.task)
    calls, checks = [], []

    def preflight():
        checks.append(1)
        if len(checks) >= 3:
            raise ContractError("base answer changed while awaiting admission")

    with pytest.raises(ContractError, match="base answer changed"):
        recover(failed, recipe, Services(), tmp_path / "recovery", preflight=preflight,
                generate=lambda *a, **k: calls.append(1))
    record = json.loads((tmp_path / "recovery/recovery.json").read_text())
    assert calls == [] and not record["first_generation_started"]
    assert "base answer changed" in record["row"]["error"]


def test_same_original_failure_cannot_get_another_chance_in_a_new_output(failed, tmp_path):
    _, recipe = recipes(failed.task)
    calls = []

    def generate(*args, **kwargs):
        calls.append(1)
        return generated_record(failed, recipe, args[4], status="generation_failed")

    recover(failed, recipe, Services(), tmp_path / "first", generate=generate)
    with pytest.raises(ContractError, match="already claimed"):
        recover(failed, recipe, Services(), tmp_path / "second", generate=generate)
    assert calls == [1]


def test_concurrent_output_directories_share_one_source_pair_claim(failed, tmp_path):
    _, recipe = recipes(failed.task)
    calls = []

    def generate(*args, **kwargs):
        calls.append(1)
        return generated_record(failed, recipe, args[4], status="generation_failed")

    def attempt(name):
        try:
            recover(failed, recipe, Services(), tmp_path / name, generate=generate)
            return "admitted"
        except ContractError as exc:
            assert "already claimed" in str(exc)
            return "claimed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ["one", "two"]))
    assert sorted(results) == ["admitted", "claimed"] and calls == [1]


def test_claim_intent_without_local_checkpoint_is_not_reissued(failed, tmp_path):
    _, recipe = recipes(failed.task)
    root = (tmp_path / "recovery").resolve()
    claim = failed.root.parent / "claims/source-pair.json"
    atomic_json(claim, {"binding": {
        "id": failed.task["id"], "model": failed.selection["model"],
        "original_generation_fingerprint": canonical_hash(failed.row["generation"]),
        "selection_fingerprint": canonical_hash(failed.selection), "owner_output": str(root)},
        "first_generation_started": True})
    calls = []
    with pytest.raises(ContractError, match="no automatic replay"):
        recover(failed, recipe, Services(), root, generate=lambda *a, **k: calls.append(1))
    record = json.loads((root / "recovery.json").read_text())
    assert calls == [] and record["first_generation_started"]
    assert "no automatic replay" in record["row"]["error"]


def test_judge_member_intents_preserve_negative_votes_and_uncertain_requests(tmp_path):
    calls = []

    def invoke(model, prompt, config):
        calls.append(model)
        if model == "uncertain":
            raise RuntimeError("response outcome not captured")
        return {"judge_model": model, "passed": False, "raw": "original negative vote"}

    panel = recovery.JournaledFirstPanel(tmp_path, {"id": "candidate"}, invoke=invoke)
    negative = panel("negative", "original prompt", {})
    with pytest.raises(RuntimeError):
        panel("uncertain", "original prompt", {})
    resumed = recovery.JournaledFirstPanel(tmp_path, {"id": "candidate"}, invoke=invoke)
    assert resumed("negative", "original prompt", {}) == negative
    assert resumed("uncertain", "original prompt", {})["error"] == "request outcome not recorded; no automatic repeat"
    assert calls == ["negative", "uncertain"]
    with pytest.raises(ContractError, match="different bindings"):
        resumed("negative", "changed prompt", {})


@pytest.mark.parametrize("changed", ["unmeasured_task", "judge"])
def test_full_task_content_and_judge_configuration_are_bound(failed, changed):
    tasks = {failed.task["id"]: failed.task, **{
        f"other-{n}": {**failed.task, "id": f"other-{n}"} for n in range(401)}}
    config = {**failed.config, "protocol": PROTOCOL}
    manifest = recovery.experiment_manifest(config, tasks, ["solver"])
    recovery.validate_experiment(config, tasks, ["solver"], manifest)
    if changed == "unmeasured_task":
        tasks["other-1"] = {**tasks["other-1"], "task_instruction": "different task"}
    else:
        config = {**config, "judge": {"models": ["different"]}}
    with pytest.raises(ContractError, match="changes original"):
        recovery.validate_experiment(config, tasks, ["solver"], manifest)


@pytest.mark.parametrize("path", ["source", "source/child", ".", "base", "package/child"])
def test_output_does_not_overlap_original_or_base_or_package(tmp_path, path):
    with pytest.raises(ContractError, match="overlaps"):
        recovery.validate_output_location(tmp_path / path, tmp_path / "source",
                                          tmp_path / "base", tmp_path / "package")


@pytest.mark.parametrize("field", [
    "id", "model", "protocol", "task_fingerprint", "start_sha",
    "runtime_fingerprint", "config_fingerprint", "engine_fingerprint",
])
def test_cold_result_recovery_checks_all_bindings_before_downstream_calls(failed, tmp_path, field):
    _, recipe = recipes(failed.task)
    root = tmp_path / "recovery"
    generation = generated_record(failed, recipe, root / "episode")
    generation[field] = "foreign"
    atomic_json(root / "episode/result.json", generation)
    atomic_json(root / "recovery.json", {
        "selection_fingerprint": canonical_hash(failed.selection), "original_generation": failed.row["generation"],
        "original_generation_fingerprint": canonical_hash(failed.row["generation"]),
        "selection": failed.selection, "first_generation_started": True,
        "row": {"id": failed.task["id"], "model": "solver", "status": "incomplete"},
    })
    calls = []
    with pytest.raises(ContractError, match="bindings"):
        recover(failed, recipe, Services(), root, generate=lambda *a, **k: calls.append("generate"),
                evaluate=lambda *a, **k: calls.append("evaluate"), judge=lambda *a, **k: calls.append("judge"))
    value = json.loads((root / "recovery.json").read_text())
    assert calls == [] and value["integrity_error"]


@pytest.mark.parametrize("corruption", ["row_runtime", "row_config", "result", "patch", "trace"])
def test_cached_generation_must_equal_durable_result_and_answer_bytes(failed, tmp_path, corruption):
    _, recipe = recipes(failed.task)
    root = tmp_path / "recovery"
    generation = generated_record(failed, recipe, root / "episode")
    if corruption.startswith("row_"):
        generation[corruption[4:] + "_fingerprint"] = "foreign"
    elif corruption == "result":
        atomic_json(root / "episode/result.json", {**generation, "extra": "different record"})
    else:
        Path(generation[corruption + "_file"]).write_text("changed answer bytes")
    atomic_json(root / "recovery.json", {
        "selection_fingerprint": canonical_hash(failed.selection), "original_generation": failed.row["generation"],
        "original_generation_fingerprint": canonical_hash(failed.row["generation"]), "selection": failed.selection,
        "first_generation_started": True,
        "row": {"id": failed.task["id"], "model": "solver", "status": "incomplete", "generation": generation},
    })
    calls = []
    with pytest.raises(ContractError):
        recover(failed, recipe, Services(), root, generate=lambda *a, **k: calls.append("generate"),
                evaluate=lambda *a, **k: calls.append("evaluate"), judge=lambda *a, **k: calls.append("judge"))
    value = json.loads((root / "recovery.json").read_text())
    assert calls == [] and value["integrity_error"]


@pytest.mark.parametrize("field", ["correctness", "judgment"])
def test_wrong_cached_measurement_binding_is_not_regraded(failed, tmp_path, field):
    _, recipe = recipes(failed.task)
    root = tmp_path / "recovery"
    generation = generated_record(failed, recipe, root / "episode")
    correctness = complete_correctness(generation, root / "evaluation")
    judgment = complete_negative_panel(generation, failed.config["judge"], root / "judgment.json")
    row = {"id": failed.task["id"], "model": "solver", "status": "incomplete",
           "generation": generation, "correctness": correctness, "judgment": judgment}
    row[field] = {**row[field], "patch_sha256": "e" * 64}
    atomic_json(root / "recovery.json", {
        "selection_fingerprint": canonical_hash(failed.selection), "original_generation": failed.row["generation"],
        "original_generation_fingerprint": canonical_hash(failed.row["generation"]), "selection": failed.selection,
        "first_generation_started": True, "row": row,
    })
    calls = []
    with pytest.raises(ContractError, match="measurement differs"):
        recover(failed, recipe, Services(), root, generate=lambda *a, **k: calls.append("generate"),
                evaluate=lambda *a, **k: calls.append("evaluate"), judge=lambda *a, **k: calls.append("judge"))
    value = json.loads((root / "recovery.json").read_text())
    assert calls == [] and value["integrity_error"]


@pytest.mark.parametrize("corruption", ["generation_config", "judgment_config"])
def test_rejected_recovery_never_overlays_base_as_success_or_model_failure(failed, tmp_path, corruption):
    _, recipe = recipes(failed.task)
    root, base, output = tmp_path / "recovery", tmp_path / "base", tmp_path / "report"
    generation = generated_record(failed, recipe, root / "episode")
    row = {"id": failed.task["id"], "model": "solver", "status": "incomplete", "generation": generation}
    if corruption == "generation_config":
        row["generation"] = {**generation, "config_fingerprint": "foreign"}
    else:
        row["correctness"] = complete_correctness(generation, root / "evaluation")
        row["judgment"] = complete_negative_panel(generation, failed.config["judge"], root / "judgment.json")
        row["judgment"]["judge_fingerprint"] = "another-panel"
        atomic_json(root / "judgment.json", row["judgment"])
    record = {"selection_fingerprint": canonical_hash(failed.selection), "original_generation": failed.row["generation"],
              "original_generation_fingerprint": canonical_hash(failed.row["generation"]), "selection": failed.selection,
              "first_generation_started": True, "row": row, "validated_row_fingerprint": canonical_hash(row)}
    atomic_json(root / "recovery.json", record)
    calls = []
    with pytest.raises(ContractError):
        recover(failed, recipe, Services(), root, generate=lambda *a, **k: calls.append("generate"),
                evaluate=lambda *a, **k: calls.append("evaluate"), judge=lambda *a, **k: calls.append("judge"))
    rejected = json.loads((root / "recovery.json").read_text())
    for name, rows in (("generation_results", [failed.row["generation"]]), ("correctness", []), ("judgments", [])):
        write_jsonl(base / (name + ".jsonl"), rows)
    tasks = {failed.task["id"]: failed.task, **{
        f"other-{n}": {**failed.task, "id": f"other-{n}"} for n in range(401)}}
    with pytest.raises(ContractError, match="cannot enter"):
        recovery.publish(base, output, tasks, ["solver"], {"selected": rejected})
    assert calls == [] and not output.exists()
    assert json.loads((base / "generation_results.jsonl").read_text()) == failed.row["generation"]


@pytest.mark.parametrize("corruption", ["result_json", "correctness_json"])
def test_truncated_evidence_never_gets_a_validated_stamp_or_overlay(failed, tmp_path, corruption):
    _, recipe = recipes(failed.task)
    root, base, output = tmp_path / "recovery", tmp_path / "base", tmp_path / "report"
    generation = generated_record(failed, recipe, root / "episode")
    row = {"id": failed.task["id"], "model": "solver", "status": "incomplete", "generation": generation}
    if corruption == "result_json":
        (root / "episode/result.json").write_text('{"truncated":')
    else:
        row["correctness"] = complete_correctness(generation, root / "evaluation")
        row["judgment"] = complete_negative_panel(generation, failed.config["judge"], root / "judgment.json")
        (root / "evaluation/correctness.json").write_text('{"truncated":')
    record = {"selection_fingerprint": canonical_hash(failed.selection), "original_generation": failed.row["generation"],
              "original_generation_fingerprint": canonical_hash(failed.row["generation"]), "selection": failed.selection,
              "first_generation_started": True, "row": row}
    atomic_json(root / "recovery.json", record)
    calls = []
    with pytest.raises(json.JSONDecodeError):
        recover(failed, recipe, Services(), root, generate=lambda *a, **k: calls.append("generate"),
                evaluate=lambda *a, **k: calls.append("evaluate"), judge=lambda *a, **k: calls.append("judge"))
    rejected = json.loads((root / "recovery.json").read_text())
    assert rejected["integrity_error"] and "validated_row_fingerprint" not in rejected
    for name, rows in (("generation_results", [failed.row["generation"]]), ("correctness", []), ("judgments", [])):
        write_jsonl(base / (name + ".jsonl"), rows)
    tasks = {failed.task["id"]: failed.task, **{
        f"other-{n}": {**failed.task, "id": f"other-{n}"} for n in range(401)}}
    with pytest.raises(ContractError, match="cannot enter"):
        recovery.publish(base, output, tasks, ["solver"], {"selected": rejected})
    assert calls == [] and not output.exists()
