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
