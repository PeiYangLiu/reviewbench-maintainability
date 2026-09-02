import collections
import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "audit_judge_panel",
    Path(__file__).resolve().parents[1] / "scripts" / "audit_judge_panel.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _votes(rows):
    """rows: {(solver, task): {judge: passed}}"""
    return collections.defaultdict(dict, rows)


def test_a_judge_that_never_dissents_is_flagged_as_having_no_influence():
    """Three names on the panel, two opinions: removing it changes nothing."""
    votes = _votes(
        {
            ("m", f"t{i}"): {"strict": i % 2 == 0, "rubber": True, "mid": True}
            for i in range(20)
        }
    )
    infl = MODULE.marginal_influence(votes)
    assert infl["judges"]["rubber"]["solo_veto"] == 0
    assert infl["judges"]["rubber"]["delta"] == 0
    assert infl["judges"]["strict"]["solo_veto"] == 10


def test_veto_rate_only_counts_tasks_the_other_judges_passed():
    """Otherwise a weak solver's own failures would look like judge severity."""
    votes = _votes(
        {
            ("weak", "t0"): {"a": False, "b": False, "c": False},
            ("weak", "t1"): {"a": False, "b": True, "c": True},
            ("weak", "t2"): {"a": True, "b": True, "c": True},
        }
    )
    veto, chance = MODULE.veto_rates(votes, "a")["weak"]
    assert chance == 2
    assert veto == 1


def test_leniency_is_measured_against_co_judges_on_the_same_solver():
    """A judge that passes everything scores positive; the strict one negative."""
    votes = _votes(
        {
            ("m", f"t{i}"): {"soft": True, "hard": False, "mid": i % 2 == 0}
            for i in range(10)
        }
    )
    lenient = MODULE.leniency(votes)
    assert lenient["soft"]["m"] > 0
    assert lenient["hard"]["m"] < 0


def test_self_preference_is_distinguishable_from_family_style():
    """Same self-rate, opposite verdicts, depending on how siblings are treated.

    A judge easy on itself and hard on its siblings is self-preferring; one
    equally easy on both is showing house style and must not be accused.
    """
    rows = {}
    # judge "sp" passes its own patches, vetoes its sibling's
    for i in range(10):
        rows[("sp", f"t{i}")] = {"sp": True, "x": True, "y": True}
        rows[("sp_sib", f"t{i}")] = {"sp": False, "x": True, "y": True}
    votes = _votes(rows)
    self_v, self_c = MODULE.veto_rates(votes, "sp")["sp"]
    sib_v, sib_c = MODULE.veto_rates(votes, "sp")["sp_sib"]
    assert self_v / self_c == 0.0
    assert sib_v / sib_c == 1.0
