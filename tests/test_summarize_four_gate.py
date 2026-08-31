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
