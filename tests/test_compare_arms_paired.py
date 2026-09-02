import json
"""Guards for the paired arm comparison.

This exists because marginal counts nearly buried a real effect: three runs of
one unchanged adapter and a genuinely improved arm produce the same magnitude,
and only the direction of the discordant tasks tells them apart (P99).
"""
import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "compare_arms_paired",
    Path(__file__).resolve().parents[1] / "scripts" / "compare_arms_paired.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_one_sided_wins_are_distinguished_from_symmetric_churn():
    """Same marginal gap, opposite meaning."""
    base = {f"t{i}": False for i in range(10)}
    trained = dict(base)
    for i in range(4):
        trained[f"t{i}"] = True
    _, _, only_a, only_b, p_directional = MODULE.compare(base, trained)
    assert (only_a, only_b) == (0, 4)

    noisy_a = {f"t{i}": i < 4 for i in range(10)}
    noisy_b = {f"t{i}": 4 <= i < 8 for i in range(10)}
    a_count, b_count, churn_a, churn_b, p_churn = MODULE.compare(noisy_a, noisy_b)
    assert a_count == b_count == 4
    assert (churn_a, churn_b) == (4, 4)
    assert p_directional < p_churn


def test_a_tie_on_marginal_counts_can_still_be_directional():
    """Equal totals do not mean equal behaviour."""
    a = {"t0": True, "t1": False}
    b = {"t0": False, "t1": True}
    count_a, count_b, only_a, only_b, _ = MODULE.compare(a, b)
    assert count_a == count_b == 1
    assert (only_a, only_b) == (1, 1)


def test_five_one_sided_tasks_cannot_reach_significance():
    """A non-significant p on tiny counts means underpowered, not absent.

    The smallest two-sided exact p with five discordant tasks is 0.0625, so the
    counts have to be reported next to the p or a real one-sided result reads
    as a null.
    """
    assert MODULE.exact_two_sided(0, 5) == 0.0625
    assert MODULE.exact_two_sided(0, 6) < 0.05
    assert MODULE.exact_two_sided(1, 11) < 0.01


def test_no_discordant_tasks_is_not_evidence_of_anything():
    assert MODULE.exact_two_sided(0, 0) == 1.0


def test_only_tasks_scored_for_both_arms_are_compared():
    a = {"t0": True, "t1": True}
    b = {"t0": False}
    count_a, count_b, only_a, only_b, _ = MODULE.compare(a, b)
    assert (count_a, count_b) == (1, 0)
    assert (only_a, only_b) == (1, 0)


def test_graded_scores_are_compared_by_magnitude_not_by_truth():
    """A task where one arm scores 2 and the other 3 is a win, not a tie."""
    a = {"t0": 2, "t1": 3, "t2": 1}
    b = {"t0": 3, "t1": 3, "t2": 0}
    total_a, total_b, only_a, only_b, _ = MODULE.compare(a, b)
    assert (total_a, total_b) == (6, 6)
    assert (only_a, only_b) == (1, 1)


def test_binarising_at_unanimous_can_hide_a_real_difference():
    """This is why the metric matters: same data, opposite readings.

    Every task moves from two votes to three-or-two, so the graded view sees
    consistent movement while the unanimous view sees almost nothing.
    """
    base = {f"t{i}": 1 for i in range(20)}
    tuned = {f"t{i}": 2 for i in range(20)}
    _, _, only_a, only_b, p_graded = MODULE.compare(base, tuned)
    assert (only_a, only_b) == (0, 20)
    assert p_graded < 0.001

    base_binary = {k: int(v >= 3) for k, v in base.items()}
    tuned_binary = {k: int(v >= 3) for k, v in tuned.items()}
    _, _, ba, bb, p_binary = MODULE.compare(base_binary, tuned_binary)
    assert (ba, bb) == (0, 0)
    assert p_binary == 1.0


def test_the_floor_is_reported_through_the_same_test_as_the_comparison(capsys):
    """A floor in one unit cannot bound a comparison in another.

    Totals said the 8B wire-format gap of 15 votes beat a 7-vote floor; the
    paired test on the same data said p=0.83, indistinguishable from noise.
    The 15 votes were scattered in both directions.
    """
    identical = {f"t{i}": i % 4 for i in range(40)}
    shifted = {f"t{i}": (i % 4) + (1 if i % 8 == 0 else 0) for i in range(40)}
    a, b, only_a, only_b, p = MODULE.compare(identical, shifted)
    assert only_a == 0 and only_b == 5
    assert b - a == 5
    # The direction test sees five one-way moves, not a five-vote total.
    assert p < 0.1


def test_symmetric_churn_does_not_become_significant_from_a_total_gap():
    """Votes can differ in total while every direction cancels out."""
    before = {f"t{i}": 2 for i in range(40)}
    after = {f"t{i}": 3 if i < 12 else (1 if i < 22 else 2) for i in range(40)}
    a, b, only_a, only_b, p = MODULE.compare(before, after)
    assert b - a == 2
    assert only_a == 10 and only_b == 12
    assert p > 0.5


def test_a_harness_failure_is_not_scored_against_the_model(tmp_path):
    """infra_retry means no verdict exists; counting it as failure blames the model.

    candidate_missing and scope_error stay failures because the model really
    did not produce a usable patch.
    """
    (tmp_path / "gopy.jsonl").write_text(
        "\n".join(
            json.dumps({"model": "m", "id": t, "status": s})
            for t, s in (
                ("ok", "pass"),
                ("broke", "infra_retry"),
                ("nopatch", "candidate_missing"),
                ("outside", "scope_error"),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    corr = MODULE.load_correctness([str(tmp_path)])
    assert corr["m"]["ok"] is True
    assert "broke" not in corr["m"]
    assert corr["m"]["nopatch"] is False
    assert corr["m"]["outside"] is False
