import concurrent.futures
import json
from pathlib import Path
import sys

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import enqueue_agentic_rust_preparation as feeder
import run_agentic_prepare_queue as queue
from reviewbench_maintainability.agentic.contracts import ContractError, file_hash


def inputs(count=6):
    tasks = {str(i): {"id": str(i), "repo": "fixture/project", "start_sha": "a" * 40,
                     "locality": {"path": "src/lib.rs"}, "inferred_language": "Rust"}
             for i in range(count)}
    oracles = {str(i): {"id": str(i), "input_index": i,
                       "lane_results_source": "${REVIEWBENCH_SESSION_FILES}/hard/rust/results.jsonl"}
               for i in range(count)}
    rows = [{"id": str(i), "status": "incomplete", "missing_reason": "generation_not_measured"}
            for i in range(count)]
    return tasks, oracles, rows


def select(tasks, oracles, rows, jobs=(), limit=8, loader=lambda *args: {"command": "cargo test"}):
    return feeder.pending_inputs(tasks, oracles, rows, jobs, Path("/original"), limit=limit, loader=loader)


def test_completed_negative_and_partial_measurements_are_not_new_work():
    tasks, oracles, rows = inputs()
    rows[0].update(status="completed", missing_reason=None, resolved=False)
    rows[1].update(missing_reason="correctness_or_judge_incomplete")
    rows[2].update(status="model_failed", missing_reason=None)
    selected, decisions = select(tasks, oracles, rows)
    assert [x[0]["id"] for x in selected] == ["3", "4", "5"]
    assert all(x["status"] == "measurement_preserved" for x in decisions[:3])


def test_all_existing_attempts_are_preserved_including_failures():
    tasks, oracles, rows = inputs(3)
    jobs = [{"id": state, "command": ["/python", "/old/capture_agentic_rust_inputs.py",
                                     "--task-id", str(i)], "status": state}
            for i, state in enumerate(["succeeded", "running", "failed"])]
    selected, decisions = select(tasks, oracles, rows, jobs)
    assert not selected
    assert all(d["status"] == "existing_attempt_preserved" for d in decisions)


def test_missing_original_lane_is_not_a_failed_automatic_attempt():
    tasks, oracles, rows = inputs(3)
    tasks["0"]["inferred_language"] = "Java"
    oracles["1"]["lane_results_source"] = None
    selected, decisions = select(tasks, oracles, rows)
    assert [x[0]["id"] for x in selected] == ["2"]
    assert [x["status"] for x in decisions] == ["other_lane", "requires_original_lane_adapter", "selected"]


def test_limit_is_bounded_and_other_candidates_remain_in_inventory():
    selected, decisions = select(*inputs(6), limit=2)
    assert len(selected) == 2 and len(decisions) == 6
    assert [r["status"] for r in decisions].count("next_batch") == 4
    for invalid in (0, 17, True):
        with pytest.raises(ContractError, match="batch limit"):
            select(*inputs(), limit=invalid)


def test_broken_original_input_does_not_block_independent_preparation():
    def loader(ctx, oracle):
        if oracle["input_index"] == 0:
            raise ContractError("missing original retained input")
        return {"command": "cargo test"}
    selected, decisions = select(*inputs(2), loader=loader)
    assert len(selected) == 1 and selected[0][0]["id"] == "1"
    assert decisions[0]["status"] == "input_diagnosis_required"


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "foreign"])
def test_full_publication_identity_is_required(mutation):
    tasks, oracles, rows = inputs(3)
    if mutation == "missing":
        rows.pop()
    elif mutation == "duplicate":
        rows[1] = rows[0]
    else:
        rows[0]["id"] = "foreign"
    with pytest.raises(ContractError, match="every original task"):
        select(tasks, oracles, rows)


def fixture402(tmp_path):
    tasks, oracles, rows = inputs(402)
    for row in rows[2:]:
        row.update(status="completed", missing_reason=None, resolved=False)
    paths = {}
    for name, values in (("tasks", tasks.values()), ("oracles", oracles.values())):
        path = tmp_path / (name + ".jsonl")
        path.write_text("".join(json.dumps(row) + "\n" for row in values))
        paths[name] = str(path)
    ids = tmp_path / "scope.ids"
    ids.write_text("".join(ident + "\n" for ident in tasks))
    paths["scope_ids"] = str(ids)
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({
        "protocol": "reviewbench-agentic-exec/v1", **paths,
        **{n: str(tmp_path / n) for n in ("repo_cache", "asset_root", "out_dir")},
    }))
    publication = tmp_path / "publication"
    publication.mkdir()
    (publication / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    legacy = tmp_path / "legacy"
    (legacy / "rust").mkdir(parents=True)
    (legacy / "rust/plans.jsonl").write_text("".join(json.dumps({
        "input_index": i, "command": "cargo test", "toolchain": "1.77.2",
    }) + "\n" for i in range(2)))
    plan_path = tmp_path / "queue-plan.json"
    plan_path.write_text(json.dumps({
        "schema_version": queue.SCHEMA, "scope": {"expected_tasks": 402,
            "inputs": [{"path": p, "sha256": file_hash(Path(p))} for p in paths.values()]},
        "workers": queue.DEFAULT_WORKERS, "jobs": [],
    }))
    return config, legacy, publication, plan_path


def test_dry_run_and_apply_twice_reuse_the_same_two_attempts(tmp_path):
    args = fixture402(tmp_path)
    before = args[-1].read_bytes()
    preview = feeder.enqueue(*args, tmp_path / "preview")
    assert preview["selected"] == 2 and preview["appended"] == 0
    assert args[-1].read_bytes() == before
    first = feeder.enqueue(*args, tmp_path / "first", apply=True)
    body = args[-1].read_bytes()
    second = feeder.enqueue(*args, tmp_path / "second", apply=True)
    assert first["appended"] == 2 and second["appended"] == 0
    assert second["decisions"]["existing_attempt_preserved"] == 2
    assert second["decisions"]["measurement_preserved"] == 400
    assert args[-1].read_bytes() == body


def test_independent_feeders_serialize_append_and_do_not_duplicate(tmp_path):
    args = fixture402(tmp_path)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(feeder.enqueue, *args, tmp_path / f"feeder{i}", apply=True)
                   for i in range(2)]
        records = [f.result() for f in futures]
    assert sorted(r["appended"] for r in records) == [0, 2]
    jobs = json.loads(args[-1].read_text())["jobs"]
    assert len(jobs) == len({j["id"] for j in jobs}) == 2
    assert all(j["resource_owner"] == "queue" and j["resources"]["cpus"] == 1 for j in jobs)


def test_config_cannot_substitute_another_unpinned402_scope(tmp_path):
    args = fixture402(tmp_path)
    config = yaml.safe_load(args[0].read_text())
    other = tmp_path / "other-tasks.jsonl"
    other.write_bytes(Path(config["tasks"]).read_bytes())
    config["tasks"] = str(other)
    args[0].write_text(yaml.safe_dump(config))
    with pytest.raises(ContractError, match="not bound"):
        feeder.enqueue(*args, tmp_path / "bad", apply=True)
