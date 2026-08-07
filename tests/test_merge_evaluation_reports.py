import importlib.util
import json
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts/merge_evaluation_reports.py"
SPEC = importlib.util.spec_from_file_location("merge_evaluation_reports", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def row(task_id, model, resolved, difficulty="medium"):
    return {
        "id": task_id,
        "model": model,
        "repo": "owner/repo",
        "language": "Go",
        "category": "complexity",
        "difficulty": difficulty,
        "generation_apply": True,
        "scope_ok": True,
        "correctness_ok": resolved,
        "maintainability_pass": resolved,
        "resolved": resolved,
        "failure_reasons": [] if resolved else ["correctness"],
    }


def write(path, rows):
    path.write_text(
        "".join(json.dumps(item) + "\n" for item in rows), encoding="utf-8"
    )


def test_load_rows_rejects_duplicate_task_model_pairs(tmp_path):
    first = tmp_path / "a.jsonl"
    second = tmp_path / "b.jsonl"
    write(first, [row("t1", "m", True)])
    write(second, [row("t1", "m", False)])

    with pytest.raises(ValueError, match="duplicate result"):
        MODULE.load_rows([first, second])


def test_load_rows_concatenates_disjoint_releases(tmp_path):
    first = tmp_path / "a.jsonl"
    second = tmp_path / "b.jsonl"
    write(first, [row("t1", "m", True)])
    write(second, [row("t2", "m", False)])

    rows = MODULE.load_rows([first, second])

    assert [item["id"] for item in rows] == ["t1", "t2"]


def test_main_merges_two_releases_into_one_leaderboard(tmp_path, monkeypatch):
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    write(
        first,
        [row("t1", "a", True), row("t1", "b", False),
         row("t2", "a", True, "hard"), row("t2", "b", True, "hard")],
    )
    write(second, [row("t3", "a", False), row("t3", "b", True)])
    out = tmp_path / "out.jsonl"
    report = tmp_path / "report.json"
    monkeypatch.setattr(
        "sys.argv",
        ["merge", "--results", str(first), "--results", str(second),
         "--out", str(out), "--report", str(report)],
    )

    assert MODULE.main() == 0

    payload = json.loads(report.read_text())
    assert payload["tasks"] == 3
    assert payload["task_model_pairs"] == 6
    assert payload["tasks_missing_a_model"] == []
    assert payload["leaderboard"]["a"]["resolved"] == 2
    assert payload["leaderboard"]["b"]["resolved"] == 2
    assert payload["leaderboard"]["a"]["by_difficulty"]["hard"]["tasks"] == 1
    assert len(out.read_text().splitlines()) == 6


def test_main_reports_tasks_missing_a_model(tmp_path, monkeypatch):
    results = tmp_path / "partial.jsonl"
    write(results, [row("t1", "a", True), row("t1", "b", True), row("t2", "a", True)])
    monkeypatch.setattr(
        "sys.argv",
        ["merge", "--results", str(results), "--out", str(tmp_path / "o.jsonl"),
         "--report", str(tmp_path / "r.json")],
    )

    MODULE.main()

    payload = json.loads((tmp_path / "r.json").read_text())
    assert payload["tasks_missing_a_model"] == ["t2"]
