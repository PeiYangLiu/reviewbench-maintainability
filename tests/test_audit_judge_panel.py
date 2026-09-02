import json
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


def test_a_judge_whose_removal_moves_no_rank_is_identified_as_replaceable():
    """Dissent counts say how often a judge objects; this says whether it lands.

    gemini dissents 4 times out of 5136 and moves no model's rank, so its third
    of the judging budget is free to reassign.
    """
    votes = _votes(
        {
            ("strong", f"t{i}"): {"a": True, "b": True, "rubber": True}
            for i in range(10)
        }
        | {
            ("weak", f"t{i}"): {"a": i < 3, "b": i < 3, "rubber": True}
            for i in range(10)
        }
    )
    full = collections.Counter({"strong": 10, "weak": 3})
    after = MODULE.board_without(votes, "rubber")
    assert after["strong"] == 10 and after["weak"] == 3
    assert MODULE.rank_shift(full, after) == 0


def test_removing_a_dissenting_judge_reorders_the_board():
    """The one that actually objects is the one whose removal is visible."""
    votes = _votes(
        {("x", f"t{i}"): {"strict": False, "b": True, "c": True} for i in range(10)}
        | {("y", f"t{i}"): {"strict": True, "b": True, "c": i < 2} for i in range(10)}
    )
    full = collections.Counter({"x": 0, "y": 2})
    after = MODULE.board_without(votes, "strict")
    assert after["x"] == 10
    assert MODULE.rank_shift(full, after) > 0


def _write_verdicts(tmp_path, judge, rows):
    d = tmp_path / f"{judge}__solver"
    d.mkdir(parents=True, exist_ok=True)
    (d / "judgements.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )


def test_a_judge_reusing_one_reason_is_flagged_as_not_reading(tmp_path):
    """Near-duplicate reasons mean a template, which is a different fault."""
    _write_verdicts(
        tmp_path,
        "lazy",
        [
            {"verdict": {"quality_1_to_5": 5, "addressed": "yes", "reason": "Looks fine."}}
            for _ in range(20)
        ],
    )
    shape = MODULE.verdict_shape(str(tmp_path / "*" / "judgements.jsonl"))
    top = shape["lazy"]["reasons"].most_common(1)[0][1]
    assert top == 20


def test_contradictory_fields_are_counted(tmp_path):
    """Calling a patch fully addressed at 1/5 means the fields disagree."""
    _write_verdicts(
        tmp_path,
        "sloppy",
        [{"verdict": {"quality_1_to_5": 1, "addressed": "yes", "reason": f"r{i}"}} for i in range(5)]
        + [{"verdict": {"quality_1_to_5": 5, "addressed": "no", "reason": f"s{i}"}} for i in range(5)],
    )
    shape = MODULE.verdict_shape(str(tmp_path / "*" / "judgements.jsonl"))
    assert shape["sloppy"]["contradict"] == 10


def test_a_calibrated_judge_uses_the_middle_grade(tmp_path):
    """The collapsed middle is what separates miscalibration from not reading."""
    _write_verdicts(
        tmp_path,
        "calibrated",
        [
            {"verdict": {"quality_1_to_5": (i % 5) + 1, "addressed": ["yes", "partial", "no"][i % 3],
                         "reason": f"reason {i}"}}
            for i in range(30)
        ],
    )
    shape = MODULE.verdict_shape(str(tmp_path / "*" / "judgements.jsonl"))
    d = shape["calibrated"]
    assert d["addressed"]["partial"] / d["n"] > 0.3
    assert d["contradict"] < d["n"]


def test_a_candidate_is_measured_by_dissent_not_by_its_pass_rate():
    """A judge can be lenient overall and still stop what the others waved through.

    deepseek-v3.2 passes more often than gpt-5.6-sol yet vetoed 20 of the 161
    tasks both incumbents approved, where gemini vetoed 1. Distribution shape
    would have rejected it.
    """
    votes = _votes(
        {("m", f"t{i}"): {"a": True, "b": True} for i in range(10)}
    )
    lenient_but_useful = {f"t{i}": i >= 3 for i in range(10)}
    rubber_stamp = {f"t{i}": True for i in range(10)}
    assert MODULE.would_dissent(votes, lenient_but_useful, ("a", "b"))["vetoed"] == 3
    assert MODULE.would_dissent(votes, rubber_stamp, ("a", "b"))["vetoed"] == 0


def test_dissent_only_counts_tasks_the_incumbents_both_accepted():
    """Rejecting what they already rejected adds nothing to the panel."""
    votes = _votes(
        {
            ("m", "agreed"): {"a": True, "b": True},
            ("m", "disputed"): {"a": True, "b": False},
        }
    )
    result = MODULE.would_dissent(votes, {"agreed": False, "disputed": False}, ("a", "b"))
    assert result["both_ok"] == 1
    assert result["vetoed"] == 1


def test_same_family_control_manufactures_favoritism_that_is_not_there():
    """A judge that is uniformly strict looks biased when compared to its kin.

    The kin model's patches are worse, so the judge vetoes them more, and a
    same-family comparison reads that quality gap as favoritism. Holding the
    reference judge fixed removes it.
    """
    # The judge is uniformly 20pp stricter than the reference on everyone.
    own_judge = {f"t{i}": i >= 2 for i in range(10)}      # 80% pass
    own_ref = {f"t{i}": True for i in range(10)}          # 100% pass
    kin_judge = {f"t{i}": i >= 6 for i in range(10)}      # 40% -- worse patches
    kin_ref = {f"t{i}": i >= 4 for i in range(10)}        # 60% pass

    raw_gap = sum(own_judge.values()) / 10 - sum(kin_judge.values()) / 10
    assert raw_gap == 0.4, "the naive same-family comparison sees a 40pp gap"

    result = MODULE.self_favoritism(
        {"self": (own_judge, own_ref)}, {"kin": (kin_judge, kin_ref)}
    )
    assert abs(result["favoritism"]) < 1e-9, "against a fixed reference it vanishes"


def test_self_favoritism_still_reports_a_real_effect():
    """Removing the confound must not blind the measure to genuine bias."""
    own_judge = {f"t{i}": True for i in range(10)}        # passes itself always
    own_ref = {f"t{i}": i >= 5 for i in range(10)}        # neutral says 50%
    other_judge = {f"t{i}": i >= 5 for i in range(10)}
    other_ref = {f"t{i}": i >= 5 for i in range(10)}
    result = MODULE.self_favoritism(
        {"self": (own_judge, own_ref)}, {"other": (other_judge, other_ref)}
    )
    assert result["favoritism"] == 0.5


def test_it_counts_how_many_judges_crown_themselves():
    """The question that needed no contested yardstick.

    Both judges sitting on the board ranked themselves first; the neutral judge
    outside it did not.
    """
    scores = {
        "opus": {"opus": 186, "gpt": 162, "qwen": 90},
        "gpt": {"opus": 186, "gpt": 234, "qwen": 90},
        "neutral": {"opus": 233, "gpt": 228, "qwen": 222},
    }
    out = MODULE.each_judge_ranks_first(scores)
    assert out["picks"]["opus"]["picked_self"] is True
    assert out["picks"]["gpt"]["picked_self"] is True
    assert out["picks"]["neutral"]["picked_self"] is False
    assert (out["picked_themselves"], out["judges_on_the_board"]) == (2, 2)


def test_a_judge_that_does_not_crown_itself_is_not_counted():
    """Sitting on the board is not the finding; crowning yourself is."""
    scores = {
        "lenient": {"lenient": 100, "other": 325},
        "other": {"lenient": 100, "other": 200},
    }
    out = MODULE.each_judge_ranks_first(scores)
    assert out["picks"]["lenient"]["top"] == "other"
    assert out["picked_themselves"] == 1
