from contextlib import contextmanager
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import recover_agentic_measurements as repair
from reviewbench_maintainability.agentic.contracts import ContractError, PROTOCOL, canonical_hash, component, file_hash


@pytest.fixture
def fixture(tmp_path):
    task = {"id": "task", "repo": "owner/project", "start_sha": "c" * 40}
    patch, trace = tmp_path / "candidate.patch", tmp_path / "trajectory.json"
    patch.write_text("fixed completed answer\n")
    trace.write_text('{"messages":[]}\n')
    generation = {
        "id": "task", "model": "model", "protocol": PROTOCOL, "status": "generated",
        "apply_ok": True,
        "task_fingerprint": canonical_hash(task), "engine_fingerprint": "a" * 64,
        "runtime_fingerprint": "r" * 64, "patch_file": str(patch), "patch_sha256": file_hash(patch),
        "trace_file": str(trace), "trace_sha256": file_hash(trace), "usage": {"model_calls": 12},
    }
    config = {"models": ["one", "two", "three"],
              "expected_response_models": {name: [name + "-version"] for name in ("one", "two", "three")}}
    members = []
    for index, model in enumerate(config["models"]):
        verdict = {"addressed": "yes", "quality_1_to_5": 5 if index == 0 else 3,
                   "introduces_new_debt": False, "focused": True, "reason": "retained literal reason"}
        raw = {"model": model + "-version",
               "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(verdict)}}]}
        members.append({"judge_model": model, "passed": index == 0, "verdict": verdict,
                        "raw_response": raw, "raw_response_text": json.dumps(raw), "http_status": 200})
    judgment = {
        **{key: generation[key] for key in ("id", "model", "protocol", "task_fingerprint", "patch_sha256")},
        "engine_fingerprint": "a" * 64, "status": "completed", "panel": members,
        "evidence_complete": True, "evidence_sha256": "e" * 64, "maintainability_pass": False,
        "judge_fingerprint": canonical_hash({"rubric": repair.RUBRIC, "version": repair.RUBRIC_VERSION,
                                            "config": config}),
    }
    original = {"id": "task", "model": "model", "generation": generation, "judgment": judgment,
                "correctness": {"status": "infra_error", "error": "old immutable symlink failure"}}
    stale = copy.deepcopy(original)
    stale.pop("judgment")
    stale["error"] = "resource admission timed out: heavy_slots"
    selection = {"id": "task", "model": "model", "source_generation_fingerprint": canonical_hash(generation),
                 "patch_sha256": generation["patch_sha256"], "trace_sha256": generation["trace_sha256"],
                 "judgment_sha256": canonical_hash(judgment)}
    return SimpleNamespace(**locals())


def test_stale_overlay_imports_exact_cached_panel_without_mutating_inputs(fixture):
    f = fixture
    originals = copy.deepcopy((f.original, f.stale))
    row, merged = repair.merge_completed_source_judgment(f.original, f.stale, f.config, {"a" * 64, "b" * 64})
    assert merged and row["judgment"] == f.judgment and row["generation"] == f.generation
    assert [member["passed"] for member in row["judgment"]["panel"]] == [True, False, False]
    assert row["correctness"]["status"] == "infra_error"
    assert (f.original, f.stale) == originals


def test_completed_negative_panel_is_never_replaced(fixture):
    f = fixture
    saved = copy.deepcopy(f.original)
    f.original["judgment"]["panel"][0]["verdict"]["reason"] = "a later different response"
    result, merged = repair.merge_completed_source_judgment(f.original, saved, f.config, {"a" * 64})
    assert not merged and result == saved


def test_host_serialized_or_other_generation_is_not_inferred_identical(fixture):
    f = fixture
    f.stale["generation"]["serialization_engine_fingerprint"] = "b" * 64
    result, merged = repair.merge_completed_source_judgment(f.original, f.stale, f.config, {"a" * 64})
    assert not merged and result == f.stale


def test_partial_accepted_votes_must_match_byte_equivalent_records(fixture):
    f = fixture
    f.stale["judgment"] = {"status": "infra_error", "panel": [copy.deepcopy(f.judgment["panel"][0])]}
    row, merged = repair.merge_completed_source_judgment(f.original, f.stale, f.config, {"a" * 64})
    assert merged and row["judgment"] == f.judgment
    f.stale["judgment"]["panel"][0]["verdict"]["reason"] = "different accepted reason"
    with pytest.raises(ContractError, match="accepted cached vote"):
        repair.merge_completed_source_judgment(f.original, f.stale, f.config, {"a" * 64})


@pytest.mark.parametrize("field", ["patch", "trace"])
def test_completed_answer_bytes_are_verified_even_before_merge(fixture, field):
    f = fixture
    Path(f.generation[field + "_file"]).write_text("changed")
    with pytest.raises(ContractError, match="bytes changed"):
        repair.merge_completed_source_judgment(f.original, f.stale, f.config, {"a" * 64})


@pytest.mark.parametrize("mutation", [
    "identity", "configuration", "engine", "partial", "duplicate", "raw-model", "raw-json",
    "unfinished", "verdict", "aggregate", "evidence", "http",
])
def test_completed_panel_validation_rejects_unbound_or_reinterpreted_votes(fixture, mutation):
    f = fixture
    j = copy.deepcopy(f.judgment)
    if mutation == "identity":
        j["patch_sha256"] = "f" * 64
    elif mutation == "configuration":
        j["judge_fingerprint"] = "f" * 64
    elif mutation == "engine":
        j["engine_fingerprint"] = "f" * 64
    elif mutation == "partial":
        j["panel"].pop()
    elif mutation == "duplicate":
        j["panel"][-1] = j["panel"][0]
    elif mutation == "raw-model":
        j["panel"][0]["raw_response"]["model"] = "undated-alias"
    elif mutation == "raw-json":
        j["panel"][0]["raw_response_text"] = "{}"
    elif mutation == "unfinished":
        raw = j["panel"][0]["raw_response"]
        raw["choices"][0]["finish_reason"] = "length"
        j["panel"][0]["raw_response_text"] = json.dumps(raw)
    elif mutation == "verdict":
        j["panel"][0]["verdict"]["reason"] = "altered response"
    elif mutation == "aggregate":
        j["maintainability_pass"] = True
    elif mutation == "evidence":
        j["evidence_complete"] = False
    else:
        j["panel"][0]["http_status"] = 503
    with pytest.raises((ContractError, ValueError)):
        repair.validate_completed_judgment(j, f.generation, f.config, {"a" * 64, "b" * 64})


def test_explicit_selection_requires_all_answer_and_panel_hashes(fixture):
    f = fixture
    path = f.tmp_path / "selection.json"
    path.write_text(json.dumps([f.selection]))
    result = repair.load_repair_selection(path, {"task": f.task}, ["model"])
    assert list(result) == [("task", "model", canonical_hash(f.generation))]
    for values in ([], [f.selection, f.selection], [{**f.selection, "model": "other"}],
                   [{**f.selection, "judgment_sha256": "not-a-hash"}]):
        path.write_text(json.dumps(values))
        with pytest.raises(ContractError):
            repair.load_repair_selection(path, {"task": f.task}, ["model"])


def test_full_overlay_preserves_unselected_pairs_and_completed_local_repairs(fixture):
    f = fixture
    pair, token = ("task", "model"), canonical_hash(f.generation)
    originals = {pair: f.original, ("other", "model"): {"id": "other", "model": "model"}}
    imported = {(*pair, token): {"row": f.stale}}
    rows, errors, count = repair.overlay_rows(originals, imported, {}, f.config, {"a" * 64})
    assert not errors and count == 1 and rows[("other", "model")] == originals[("other", "model")]
    local = copy.deepcopy(f.original)
    local["correctness"] = {"status": "fail"}
    rows, errors, count = repair.overlay_rows(originals, imported, {(*pair, token): {"row": local}},
                                             f.config, {"a" * 64})
    assert not errors and count == 0 and rows[pair] == local


def services(events):
    @contextmanager
    def job(*args):
        events.append(("admitted", args))
        yield
        events.append(("released", args))
    return SimpleNamespace(job=job)


def test_correctness_only_replay_keeps_answer_and_all_three_negative_or_positive_votes(fixture):
    f = fixture
    selected, _ = repair.merge_completed_source_judgment(f.original, f.stale, f.config, {"a" * 64})
    events, calls = [], []
    before = copy.deepcopy((f.original, selected))

    def evaluate(task, recipe, generation, repository, output, assets, **kwargs):
        assert events and events[-1][0] == "admitted"
        calls.append((task, generation, kwargs))
        return {"status": "pass", "correctness_ok": True}

    record = repair.recover_correctness(
        f.original, selected, f.selection, f.task, SimpleNamespace(fingerprint="r" * 64),
        services(events), {"judge": f.config, "repo_cache": str(f.tmp_path), "asset_root": str(f.tmp_path)},
        f.tmp_path / "output", "b" * 64, evaluate=evaluate)
    assert len(calls) == 1 and calls[0][2] == {"retry_infra": True}
    assert record["row"]["correctness"]["status"] == "pass"
    assert record["row"]["judgment"] == f.judgment and record["row"]["judgment"]["maintainability_pass"] is False
    assert record["row"]["generation"] == f.generation
    assert (f.original, selected) == before
    assert record["measurement_recovery"]["additional_judge_calls"] == 0
    saved = next((f.tmp_path / "output/repairs").glob("*/*/*/result.json"))
    assert json.loads(saved.read_text()) == record


@pytest.mark.parametrize("status", sorted(repair.TERMINAL_CORRECTNESS))
def test_completed_correctness_is_not_reexecuted(fixture, status):
    f = fixture
    selected = copy.deepcopy(f.original)
    selected["correctness"]["status"] = status
    with pytest.raises(ContractError, match="not replayed"):
        repair.recover_correctness(
            f.original, selected, f.selection, f.task, SimpleNamespace(fingerprint="r" * 64), None,
            {"judge": f.config}, f.tmp_path / "output", "b" * 64,
            evaluate=lambda *args, **kwargs: pytest.fail("completed result was replayed"))


def test_correctness_callback_cannot_mutate_completed_generation(fixture):
    f = fixture
    before = copy.deepcopy(f.original)
    def mutate(task, recipe, generation, *args, **kwargs):
        generation["usage"]["model_calls"] += 1
        return {"status": "pass"}
    with pytest.raises(ContractError, match="altered a completed answer"):
        repair.recover_correctness(
            f.original, f.original, f.selection, f.task, SimpleNamespace(fingerprint="r" * 64), services([]),
            {"judge": f.config, "repo_cache": str(f.tmp_path), "asset_root": str(f.tmp_path)},
            f.tmp_path / "output", "b" * 64, evaluate=mutate)
    assert f.original == before
    assert not list((f.tmp_path / "output").glob("repairs/*/*/*/result.json"))


@pytest.mark.parametrize("grading_engine", ["b" * 64, "d" * 64])
def test_cli_preserves_full_402_scope_and_reuses_its_one_completed_repair(fixture, monkeypatch, grading_engine):
    f = fixture
    source, base, output = (f.tmp_path / name for name in ("source", "base", "output"))
    source.mkdir()
    base.mkdir()
    tasks = {"task": f.task, **{f"unselected-{index}": {"id": f"unselected-{index}"} for index in range(401)}}
    manifest = {"task_ids": list(tasks), "models": ["model"], "tasks_fingerprint": canonical_hash(tasks),
                "engine_fingerprint": "a" * 64}
    (source / "experiment-manifest.json").write_text(json.dumps(manifest))
    (source / "experiment-config.json").write_text(json.dumps({"judge": f.config}))
    original = copy.deepcopy(f.original)
    original["correctness"].update({key: f.generation[key] for key in
                                    ("id", "model", "protocol", "task_fingerprint", "patch_sha256")})
    pair = source / "pairs/model/task/pair-result.json"
    pair.parent.mkdir(parents=True)
    pair.write_text(json.dumps(original))
    stale = copy.deepcopy(original)
    stale.pop("judgment")
    saved = base / "repairs/model/task/token/result.json"
    saved.parent.mkdir(parents=True)
    saved.write_text(json.dumps({"host_engine_fingerprint": "b" * 64,
                                 "source_generation_fingerprint": canonical_hash(f.generation), "row": stale}))
    base_manifest = {
        "schema_version": "reviewbench-host-recovery/v1", "source_run": str(source.resolve()),
        "source_manifest_sha256": file_hash(source / "experiment-manifest.json"),
        "generation_engine_fingerprint": "a" * 64, "host_recovery_engine_fingerprint": "b" * 64,
        "task_ids": list(tasks), "models": ["model"], "tasks_fingerprint": canonical_hash(tasks),
        "expected_pairs": 402, "judge_fingerprint": canonical_hash(f.config), "additional_generation_calls": 0,
    }
    (base / "experiment-manifest.json").write_text(json.dumps(base_manifest))
    selection = f.tmp_path / "selection.json"
    selection.write_text(json.dumps([f.selection]))
    config = {"judge": f.config, "tasks": "fixture", "repo_cache": str(f.tmp_path), "asset_root": str(f.tmp_path),
              "measurement_recovery": {"base_host_engine_fingerprint": "b" * 64}}
    svc = services([])
    svc.gate = SimpleNamespace(status=lambda: {"fixture": True})
    calls = []
    def evaluate(task, recipe, generation, *args, **kwargs):
        calls.append(copy.deepcopy(generation))
        return {
            **{key: generation[key] for key in ("id", "model", "protocol", "task_fingerprint", "patch_sha256")},
            "status": "pass", "scope_ok": True, "correctness_ok": True, "oracle_replayed": True,
        }
    monkeypatch.setattr(repair, "load_config", lambda _: copy.deepcopy(config))
    monkeypatch.setattr(repair, "selected_tasks", lambda *args: tasks)
    monkeypatch.setattr(repair, "engine_identity", lambda: {"fingerprint": grading_engine})
    monkeypatch.setattr(repair, "ExecutionServices", lambda _: svc)
    monkeypatch.setattr(repair, "load_recipes", lambda _: {"task": SimpleNamespace(fingerprint="r" * 64)})
    monkeypatch.setattr(repair, "correctness", evaluate)
    monkeypatch.setattr(sys, "argv", [
        "recover", "--config", str(f.tmp_path / "config.yaml"), "--source-run", str(source),
        "--base-recovery-run", str(base), "--repair-selection", str(selection), "--out-dir", str(output)])
    before = {path: path.read_bytes() for path in (pair, saved, f.patch, f.trace)}
    assert repair.main() == 2
    assert repair.main() == 2
    assert len(calls) == 1
    report = json.loads((output / "report.json").read_text())
    rows = [json.loads(line) for line in (output / "results.jsonl").read_text().splitlines()]
    assert report["expected_pairs"] == report["task_count"] == len(rows) == 402
    assert report["measurement_repair_records"] == 1 and report["complete"] is False
    assert report["host_recovery_engine_fingerprint"] == grading_engine
    assert report["source_host_recovery_engine_fingerprint"] == "b" * 64
    assert report["additional_generation_calls"] == report["additional_judge_calls"] == 0
    row = next(row for row in rows if row["id"] == "task")
    assert row["status"] == "completed" and row["resolved"] is False and row["votes"] == 1
    assert all(path.read_bytes() == data for path, data in before.items())
