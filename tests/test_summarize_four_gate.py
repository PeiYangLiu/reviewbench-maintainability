import json
"""Guards for the board summary.

The panel decision rule is the single most dangerous knob here: switching from
unanimous to majority moved every model by 40-70 tasks, which reads as a real
jump and would have been published as one.
"""
import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "summarize_four_gate",
    Path(__file__).resolve().parents[1] / "scripts" / "summarize_four_gate.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def votes(*per_task):
    return {("m", f"t{i}"): dict(zip("abc", v)) for i, v in enumerate(per_task)}


def test_unanimous_is_the_default_and_needs_every_judge():
    split = votes((True, True, False))
    board = MODULE.board(split, {("m", "t0"): "pass"})
    assert board["m"]["maint"] == 0
    assert board["m"]["final"] == 0


def test_majority_accepts_two_of_three():
    split = votes((True, True, False))
    board = MODULE.board(split, {("m", "t0"): "pass"}, decision="majority")
    assert board["m"]["maint"] == 1
    assert board["m"]["final"] == 1


def test_correctness_and_maintainability_must_both_hold():
    board = MODULE.board(
        votes((True, True, True)),
        {("m", "t0"): "fail"},
    )
    assert board["m"]["maint"] == 1
    assert board["m"]["correct"] == 0
    assert board["m"]["final"] == 0


def test_a_short_panel_is_excluded_rather_than_counted_as_a_failure():
    """Two judges out of three is missing data, not a rejection."""
    board = MODULE.board(
        {("m", "t0"): {"a": True, "b": True}},
        {("m", "t0"): "pass"},
    )
    assert board["m"]["short_panel"] == 1
    assert board["m"]["judged"] == 0
    assert board["m"]["final"] == 0


def test_boundary_tasks_are_counted_so_score_stability_is_visible():
    """One vote short is the population that makes `final` unstable (P104).

    A model with more boundary tasks than scoring ones swings widely on
    resampling, so the ratio has to be on the board rather than inferred.
    """
    board = MODULE.board(
        {
            ("m", "t0"): {"a": True, "b": True, "c": True},
            ("m", "t1"): {"a": True, "b": True, "c": False},
            ("m", "t2"): {"a": True, "b": True, "c": False},
        },
        {("m", t): "pass" for t in ("t0", "t1", "t2")},
    )
    assert board["m"]["maint"] == 1
    assert board["m"]["boundary"] == 2
    assert board["m"]["final"] == 1
    assert board["m"]["votes"] == 3 + 2 + 2


def test_votes_separate_models_that_tie_on_the_strict_gate():
    """Two models can both score zero unanimously and differ a lot by votes."""
    almost = {("a", f"t{i}"): {"x": True, "y": True, "z": False} for i in range(5)}
    nowhere = {("b", f"t{i}"): {"x": False, "y": False, "z": False} for i in range(5)}
    correctness = {(m, f"t{i}"): "pass" for m in ("a", "b") for i in range(5)}
    board = MODULE.board({**almost, **nowhere}, correctness)
    assert board["a"]["final"] == board["b"]["final"] == 0
    assert board["a"]["votes"] == 10
    assert board["b"]["votes"] == 0


def _write(tmp_path, name, records):
    directory = tmp_path / name
    directory.mkdir()
    path = directory / "judgements.jsonl"
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8"
    )
    return path


def test_a_judge_refusing_is_recorded_but_a_missing_patch_is_not(tmp_path):
    """Only a lost judgement is data loss; a solver that never edited is a real zero."""
    _write(
        tmp_path,
        "panel",
        [
            {"solver_model": "m", "id": "refused", "verdict": None, "error": "filter"},
            {"solver_model": "m", "id": "nopatch", "verdict": None,
             "generation_error": "no patch"},
        ],
    )
    dropped = {}
    MODULE.load_votes(str(tmp_path / "*" / "judgements.jsonl"), dropped)
    assert list(dropped) == [("m", "refused")]
    assert dropped[("m", "refused")] == "filter"


def test_a_refusal_that_a_retry_covered_is_not_reported_as_a_gap(tmp_path):
    """The retry restores a full panel, so the stale refusal must not warn."""
    verdict = {"addressed": "yes", "introduces_new_debt": False, "quality_1_to_5": 5}
    _write(
        tmp_path,
        "panel",
        [{"solver_model": "m", "id": "t", "verdict": None, "error": "filter"}]
        + [
            {"solver_model": "m", "id": "t", "judge_model": j, "verdict": verdict}
            for j in ("a", "b", "c")
        ],
    )
    dropped = {}
    votes = MODULE.load_votes(str(tmp_path / "*" / "judgements.jsonl"), dropped)
    assert ("m", "t") in dropped
    assert len(votes[("m", "t")]) == 3
