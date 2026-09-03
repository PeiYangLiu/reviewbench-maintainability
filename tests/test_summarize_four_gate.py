import collections
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


def test_exact_model_filter_keeps_a_scoped_board_and_its_warnings():
    all_votes = {
        ("keep", "t"): {"a": True, "b": True, "c": True},
        ("drop", "t"): {"a": True, "b": True, "c": True},
    }
    dropped = {("keep", "lost"): ["x"], ("drop", "lost"): ["x"]}
    correctness = {("keep", "t"): "pass", ("drop", "t"): "pass"}
    got_votes, got_dropped, got_correctness = MODULE.filter_models(
        all_votes, dropped, correctness, ["keep"]
    )
    assert set(got_votes) == {("keep", "t")}
    assert set(got_dropped) == {("keep", "lost")}
    assert set(got_correctness) == {("keep", "t")}


def test_exact_model_filter_rejects_a_misspelled_arm():
    try:
        MODULE.filter_models(
            {("real", "t"): {"a": True}},
            {},
            {("real", "t"): "pass"},
            ["reel"],
        )
    except ValueError as error:
        assert "reel" in str(error)
    else:
        raise AssertionError("a misspelled arm silently produced an empty board")


def test_complete_report_gate_rejects_a_partial_correctness_run(tmp_path):
    (tmp_path / "correctness.complete.json").write_text(
        json.dumps(
            {"complete": False, "records": 2352, "expected_records": 2412}
        ),
        encoding="utf-8",
    )
    try:
        MODULE.require_complete_runs([tmp_path])
    except ValueError as error:
        assert "2352/2412" in str(error)
    else:
        raise AssertionError("a partial run was allowed to publish as complete")


def test_complete_report_gate_accepts_an_explicit_complete_marker(tmp_path):
    (tmp_path / "correctness.complete.json").write_text(
        json.dumps(
            {"complete": True, "records": 2412, "expected_records": 2412}
        ),
        encoding="utf-8",
    )
    MODULE.require_complete_runs([tmp_path])


def test_coverage_gate_accepts_all_rows_even_with_infra(tmp_path):
    (tmp_path / "correctness.complete.json").write_text(
        json.dumps(
            {"complete": False, "records": 2412, "expected_records": 2412}
        ),
        encoding="utf-8",
    )
    MODULE.require_covered_runs([tmp_path])


def test_coverage_gate_rejects_missing_rows(tmp_path):
    (tmp_path / "correctness.complete.json").write_text(
        json.dumps(
            {"complete": False, "records": 2352, "expected_records": 2412}
        ),
        encoding="utf-8",
    )
    try:
        MODULE.require_covered_runs([tmp_path])
    except ValueError as error:
        assert "2352/2412" in str(error)
    else:
        raise AssertionError("a board with missing rows claimed full coverage")


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


def test_uneven_correctness_coverage_is_asserted_not_left_to_the_reader(capsys):
    """A lane that never ran shrinks every denominator, which hides itself."""
    per = {
        "full": collections.Counter({"scored": 402}),
        "missing_a_lane": collections.Counter({"scored": 303}),
    }
    MODULE.report_correctness_coverage(per)
    out = capsys.readouterr().out
    assert "different task counts" in out
    assert "303" in out


def test_equal_coverage_stays_quiet():
    per = {m: collections.Counter({"scored": 402}) for m in ("a", "b")}
    MODULE.report_correctness_coverage(per)


def test_infra_retry_leaves_the_pair_out_of_both_numerator_and_denominator(tmp_path):
    """A harness failure produced no verdict, so it cannot count either way."""
    directory = tmp_path / "correctness-normalized"
    directory.mkdir(parents=True)
    (directory / "gopy.jsonl").write_text(
        "\n".join(
            json.dumps({"model": "m", "id": t, "status": s})
            for t, s in (("ok", "pass"), ("broke", "infra_retry"), ("bad", "fail"))
        )
        + "\n",
        encoding="utf-8",
    )
    corr = MODULE.load_correctness([str(tmp_path)])
    assert corr[("m", "ok")] == "pass"
    assert corr[("m", "bad")] == "fail"
    assert ("m", "broke") not in corr


def test_a_model_with_no_correctness_rows_is_not_shown_as_zero(capsys):
    """Zero gates and absent gates read identically on a board.

    An arm whose correctness lane never ran scores 0 on Correct and FINAL,
    which is exactly what an arm that failed every task scores. The RL board
    had six such arms because their data lived in a second directory.
    """
    per = {
        "ran": {"judged": 10, "maint": 5, "correct": 4, "final": 3,
                "votes": 12, "boundary": 1, "scored": 10},
        "never_ran": {"judged": 10, "maint": 5, "correct": 0, "final": 0,
                      "votes": 12, "boundary": 1, "scored": 0},
    }
    MODULE.report_correctness_coverage(per)
    out = capsys.readouterr().out
    assert "no correctness rows at all" in out
    assert "never_ran" in out


def test_a_gutted_arm_is_separable_from_a_complete_one_by_its_denominator():
    """A numerator without its denominator cannot be sanity-checked.

    After the api2 per-task rows were lost, the published top model would have
    printed Correct 13 against a real 291, and 13 out of 13 looks like a score.
    `scored` is what separates it from 291 out of 368, so the board prints it
    in the row rather than only in a footnote.
    """
    votes = {}
    correctness = {}
    for i in range(20):
        votes[("whole", f"t{i}")] = {"a": True, "b": True, "c": True}
        correctness[("whole", f"t{i}")] = "pass"
    votes[("gutted", "t0")] = {"a": True, "b": True, "c": True}
    correctness[("gutted", "t0")] = "pass"
    for i in range(1, 20):
        votes[("gutted", f"t{i}")] = {"a": True, "b": True, "c": True}
    per = MODULE.board(votes, correctness)

    assert per["whole"]["correct"] == 20
    assert per["gutted"]["correct"] == 1
    # Both arms were judged on all 20 tasks, so `judged` hides the gap.
    assert per["whole"]["judged"] == per["gutted"]["judged"] == 20
    # Only the correctness denominator shows it.
    assert (per["whole"]["scored"], per["gutted"]["scored"]) == (20, 1)
