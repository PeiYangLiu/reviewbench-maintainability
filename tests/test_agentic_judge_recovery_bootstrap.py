"""Explicit v6 manifest support; every fixture is local and has zero API calls."""
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from test_agentic_judge_member_recovery import fixture as member_fixture, member
import recover_agentic_judgments as repair
from reviewbench_maintainability.agentic.contracts import (
    ContractError, PROTOCOL, canonical_hash, component, file_hash,
)


ENGINE = "5142d1fec94eb6c5a6177a66ee7ce8533a26f0a2578280075a6947a399c01ca3"
BOOTSTRAP_SCHEMA = "reviewbench-zero-call-bootstrap-recovery/v1"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def lineage(tmp_path, task=None):
    task = task or {"id": "task", "repo": "fixture/project", "start_sha": "c" * 40}
    tasks = {"task": task, "bootstrap-only": {"id": "bootstrap-only"}}
    tasks.update({f"other-{i:03d}": {"id": f"other-{i:03d}"} for i in range(400)})
    source, v4, v5, base = (tmp_path / name for name in ("source", "v4", "v5", "v6"))
    common = {"task_ids": list(tasks), "tasks_fingerprint": canonical_hash(tasks),
              "models": ["solver"], "expected_pairs": 402}
    original = {**common, "protocol": PROTOCOL, "schema_version": "reviewbench-progressive-experiment/v1",
                "engine_fingerprint": ENGINE, "behavior_fingerprint": "b" * 64}
    predecessor = {
        **common, "schema_version": "reviewbench-measurement-recovery/v1",
        "source_run": str(source), "generation_engine_fingerprint": ENGINE,
        "host_recovery_engine_fingerprint": "c" * 64,
    }
    previous = {
        **common, "schema_version": "reviewbench-judge-member-recovery/v1",
        "source_run": str(source), "base_run": str(v4),
        "generation_engine_fingerprint": ENGINE, "base_host_engine_fingerprint": "c" * 64,
    }
    bootstrap = {
        **common, "schema_version": BOOTSTRAP_SCHEMA, "source_run": str(source), "base_run": str(v5),
        "engine_fingerprint": ENGINE, "behavior_fingerprint": "b" * 64,
    }
    graph = SimpleNamespace(**locals())
    persist_lineage(graph)
    return graph


def persist_lineage(graph):
    write(graph.source / "experiment-manifest.json", graph.original)
    digest = file_hash(graph.source / "experiment-manifest.json")
    for value in (graph.predecessor, graph.previous, graph.bootstrap):
        value["source_manifest_sha256"] = digest
    write(graph.v4 / "experiment-manifest.json", graph.predecessor)
    graph.previous["base_manifest_sha256"] = file_hash(graph.v4 / "experiment-manifest.json")
    write(graph.v5 / "experiment-manifest.json", graph.previous)
    write(graph.base / "experiment-manifest.json", graph.bootstrap)


def test_explicit_bootstrap_binding_attests_source_and_v5_predecessor_without_rewriting(tmp_path):
    graph = lineage(tmp_path)
    files = [p / "experiment-manifest.json" for p in (graph.source, graph.v4, graph.v5, graph.base)]
    before = {str(p): p.read_bytes() for p in files}
    bound = repair.base_engine_binding(graph.base, graph.bootstrap, graph.tasks, ["solver"])
    assert bound["generation_engine_fingerprint"] == ENGINE
    assert bound["base_host_engine_fingerprint"] == "c" * 64
    assert bound["bootstrap_base_schema"] == BOOTSTRAP_SCHEMA
    assert bound["bootstrap_lineage"] == [
        {"manifest": str(graph.v5 / "experiment-manifest.json"),
         "sha256": file_hash(graph.v5 / "experiment-manifest.json"),
         "schema_version": "reviewbench-judge-member-recovery/v1"},
        {"manifest": str(graph.v4 / "experiment-manifest.json"),
         "sha256": file_hash(graph.v4 / "experiment-manifest.json"),
         "schema_version": "reviewbench-measurement-recovery/v1"},
    ]
    assert {str(p): p.read_bytes() for p in files} == before
    assert "generation_engine_fingerprint" not in graph.bootstrap


@pytest.mark.parametrize("mutation", [
    "v6-engine", "v6-alias", "v6-tasks", "v6-task-fingerprint", "v6-models", "v6-count", "v6-float-count",
    "v6-behavior", "v6-relative-source", "v6-relative-base", "v6-cycle",
    "source-schema", "source-protocol", "source-engine", "source-tasks", "source-models",
    "v5-schema", "v5-engine", "v5-source", "v5-tasks", "v5-task-fingerprint", "v5-models",
    "v5-count", "v5-host", "v5-host-type", "v5-cycle", "v4-schema", "v4-engine", "v4-source",
    "v4-tasks", "v4-models", "v4-count", "v4-host",
])
def test_bootstrap_rejects_changed_explicit_contract_before_any_execution(tmp_path, mutation):
    graph = lineage(tmp_path)
    which, field = mutation.split("-", 1)
    value = {"v6": graph.bootstrap, "source": graph.original,
             "v5": graph.previous, "v4": graph.predecessor}[which]
    if field == "engine":
        value["engine_fingerprint" if which in {"source", "v6"} else "generation_engine_fingerprint"] = "f" * 64
    elif field == "alias":
        value["generation_engine_fingerprint"] = ENGINE
    elif field == "tasks":
        value["task_ids"] = list(reversed(value["task_ids"]))
    elif field == "task-fingerprint":
        value["tasks_fingerprint"] = "f" * 64
    elif field == "models":
        value["models"] = ["foreign-solver"]
    elif field == "count":
        value["expected_pairs"] = 401
    elif field == "float-count":
        value["expected_pairs"] = 402.0
    elif field == "schema":
        value["schema_version"] = "unknown/v1"
    elif field == "protocol":
        value["protocol"] = "foreign-protocol"
    elif field == "behavior":
        value["behavior_fingerprint"] = "f" * 64
    elif field == "source":
        value["source_run"] = str(tmp_path / "foreign-source")
    elif field == "relative-source":
        value["source_run"] = "relative/source"
    elif field == "relative-base":
        value["base_run"] = "relative/v5"
    elif field == "cycle":
        value["base_run"] = str(graph.base if which == "v6" else graph.v5)
    elif field in {"host", "host-type"}:
        value["base_host_engine_fingerprint" if which == "v5" else "host_recovery_engine_fingerprint"] = (
            123 if field == "host-type" else "d" * 64)
    persist_lineage(graph)
    with pytest.raises(ContractError):
        repair.base_engine_binding(graph.base, graph.bootstrap, graph.tasks, ["solver"])


@pytest.mark.parametrize("mutation", ["source-content", "source-pin", "v5-source-pin", "v5-parent-pin"])
def test_bootstrap_rejects_stale_or_unbound_manifest_bytes(tmp_path, mutation):
    graph = lineage(tmp_path)
    if mutation == "source-content":
        path = graph.source / "experiment-manifest.json"
        path.write_bytes(path.read_bytes() + b" ")
    elif mutation == "source-pin":
        graph.bootstrap["source_manifest_sha256"] = "0" * 64
    elif mutation == "v5-source-pin":
        graph.previous["source_manifest_sha256"] = "0" * 64
        write(graph.v5 / "experiment-manifest.json", graph.previous)
    else:
        graph.previous["base_manifest_sha256"] = "0" * 64
        write(graph.v5 / "experiment-manifest.json", graph.previous)
    with pytest.raises(ContractError):
        repair.base_engine_binding(graph.base, graph.bootstrap, graph.tasks, ["solver"])


def test_bootstrap_does_not_support_smaller_scope_or_multiple_models(tmp_path):
    graph = lineage(tmp_path)
    for tasks, models in (({"task": graph.tasks["task"]}, ["solver"]),
                          (graph.tasks, ["solver", "other"]), (graph.tasks, [])):
        with pytest.raises(ContractError):
            repair.base_engine_binding(graph.base, graph.bootstrap, tasks, models)


def test_engine_field_is_not_a_generic_fallback_and_old_manifest_is_unchanged(tmp_path):
    old = {"schema_version": "reviewbench-measurement-recovery/v1",
           "generation_engine_fingerprint": "a" * 64,
           "host_recovery_engine_fingerprint": "b" * 64,
           "base_host_engine_fingerprint": "c" * 64}
    assert repair.base_engine_binding(tmp_path, old, {}, []) == {
        "generation_engine_fingerprint": "a" * 64, "base_host_engine_fingerprint": "b" * 64,
    }
    for schema in ("unknown/v1", "reviewbench-measurement-recovery/v1"):
        with pytest.raises(KeyError):
            repair.base_engine_binding(tmp_path, {"schema_version": schema, "engine_fingerprint": ENGINE}, {}, [])


def cached_full402_fixture(tmp_path, monkeypatch):
    """Run the real parent CLI on 402 local rows, with a single cached member repair."""
    f = member_fixture.__wrapped__(tmp_path / "answer")
    graph = lineage(tmp_path / "lineage", f.task)
    config = {"tasks": str(tmp_path / "tasks.jsonl"), "judge": f.config}
    write(graph.source / "experiment-config.json", {"judge": repair.sanitize_config(config)["judge"]})
    f.generation["engine_fingerprint"] = ENGINE
    f.previous["engine_fingerprint"] = ENGINE
    f.previous["panel"] = [member("one", False),
                           {"judge_model": "two", "error": "TimeoutError: timed out"},
                           member("three", True)]
    f.previous["maintainability_pass"] = None
    f.selection.update(
        generation_sha256=canonical_hash(f.generation), judgment_sha256=canonical_hash(f.previous),
        correctness_sha256=canonical_hash(f.correctness), judge_engine=ENGINE, judge_models=["two"])
    rows = {}
    for ident, task in graph.tasks.items():
        row = copy.deepcopy(f.row)
        row["id"] = ident
        for key in ("generation", "correctness", "judgment"):
            row[key].update(id=ident, task_fingerprint=canonical_hash(task))
        if ident != "task":
            row["judgment"].update(status="completed", maintainability_pass=True,
                                   panel=[member(name) for name in f.config["models"]])
        if ident == "bootstrap-only":
            row["generation"]["bootstrap_recovery"] = {"preserve_v6_only_answer": True}
        rows[(ident, "solver")] = row
    for key, name in (("generation", "generation_results"), ("correctness", "correctness"),
                      ("judgment", "judgments")):
        repair.write_jsonl(graph.base / (name + ".jsonl"), [row[key] for row in rows.values()])
    repaired = copy.deepcopy(rows[("task", "solver")])
    repaired["judgment"]["panel"][1] = member("two", True)
    repaired["judgment"].update(status="completed", maintainability_pass=False)
    record = {"selection": f.selection, "row": repaired, "judge_request_attempts": 1,
              "original_judgment_engine_fingerprint": ENGINE}
    output = tmp_path / "output"
    directory = output / "repairs" / component("solver") / component("task") / f.selection["generation_sha256"][:16]
    write(directory / "result.json", record)
    selection_file, engines_file, config_file = (tmp_path / name for name in ("selection.json", "engines.json", "config.json"))
    write(selection_file, [f.selection])
    write(engines_file, {ENGINE: str(tmp_path / "unused-frozen-engine")})
    write(config_file, config)
    monkeypatch.setattr(repair, "load_config", lambda _: copy.deepcopy(config))
    monkeypatch.setattr(repair, "selected_tasks", lambda *_: copy.deepcopy(graph.tasks))
    monkeypatch.setattr(repair, "load_recipes", lambda _: {})
    def forbidden(*args, **kwargs):
        pytest.fail("cached fixture invoked a child/API/build/ExecutionServices")
    monkeypatch.setattr(repair.subprocess, "Popen", forbidden)
    monkeypatch.setattr(repair, "invoke_judge", forbidden)
    monkeypatch.setattr(repair, "ExecutionServices", forbidden)
    monkeypatch.setattr(sys, "argv", [
        "recover_agentic_judgments.py", "--config", str(config_file), "--base-run", str(graph.base),
        "--repair-selection", str(selection_file), "--engine-sources", str(engines_file),
        "--out-dir", str(output),
    ])
    source_files = [p / "experiment-manifest.json" for p in (graph.source, graph.v4, graph.v5, graph.base)]
    source_files += [graph.base / (name + ".jsonl") for name in ("generation_results", "correctness", "judgments")]
    before_files = {str(p): file_hash(p) for p in source_files}
    return SimpleNamespace(**locals())


def assert_full402_preserved(f):
    assert {str(p): file_hash(p) for p in f.source_files} == f.before_files
    published = repair.read_base_rows(f.output, f.graph.tasks, ["solver"])
    assert len(published) == 402
    for pair, original in f.rows.items():
        assert published[pair]["generation"] == original["generation"]
        assert published[pair]["correctness"] == original["correctness"]
        if pair != ("task", "solver"):
            assert published[pair]["judgment"] == original["judgment"]
        else:
            before, after = original["judgment"]["panel"], published[pair]["judgment"]["panel"]
            assert after[0] == before[0] and after[2] == before[2]
            assert after[0]["passed"] is False and after[1]["passed"] is True
    assert published[("bootstrap-only", "solver")]["generation"]["bootstrap_recovery"] == {
        "preserve_v6_only_answer": True}
    table = [json.loads(line) for line in (f.output / "results.jsonl").read_text().splitlines()]
    assert len(table) == 402
    assert next(x for x in table if x["id"] == "task")["resolved"] is False
    manifest = json.loads((f.output / "experiment-manifest.json").read_text())
    assert Path(manifest["base_run"]) == f.graph.base.resolve()
    assert manifest["generation_engine_fingerprint"] == ENGINE
    assert len(manifest["bootstrap_lineage"]) == 2
    return {"full402": 402, "changed_judgment_pairs": 1, "changed_members": 1,
            "unchanged_generations": 402, "unchanged_correctness": 402,
            "other_judgments_unchanged": 401, "accepted_selected_votes_unchanged": 2,
            "negative_vote_preserved": True, "actual_API_calls": 0}


def test_bootstrap_full402_main_reuses_only_selected_cached_repair(tmp_path, monkeypatch):
    (tmp_path / "answer").mkdir()
    f = cached_full402_fixture(tmp_path, monkeypatch)
    assert repair.main() == 0
    assert_full402_preserved(f)


@pytest.mark.parametrize("changed", ["v5", "v4", "source", "base"])
def test_bootstrap_lineage_bytes_are_checked_again_before_publish(tmp_path, monkeypatch, changed):
    (tmp_path / "answer").mkdir()
    f = cached_full402_fixture(tmp_path, monkeypatch)
    original = repair.read_base_rows
    def changed_lineage(*args):
        values = original(*args)
        path = getattr(f.graph, changed) / "experiment-manifest.json"
        path.write_bytes(path.read_bytes() + b" ")
        return values
    monkeypatch.setattr(repair, "read_base_rows", changed_lineage)
    with pytest.raises(ContractError, match="bindings changed"):
        repair.main()
    assert not (f.output / "report.json").exists()
