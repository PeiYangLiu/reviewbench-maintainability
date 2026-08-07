import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "merge_hard_oracle_results.py"
SPEC = importlib.util.spec_from_file_location("merge_hard_oracle_results", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_pass_beats_recoverable():
    passed = {"id": "t", "oracle_result": "PASS_TO_PASS", "pass_to_pass": 1}
    recovery = {"id": "t", "status": "recoverable_environment"}

    assert MODULE.preference(passed) > MODULE.preference(recovery)


def test_richer_pass_wins_a_tie():
    lean = {"id": "t", "oracle_result": "PASS_TO_PASS", "pass_to_pass": 1}
    rich = {
        **lean,
        "golden_correctness_ok": True,
        "changed_lines_covered": True,
        "stable_p2p": True,
    }

    assert MODULE.preference(rich) > MODULE.preference(lean)


def test_structural_beats_stale_recoverable_but_not_pass():
    structural = {"id": "t", "oracle_result": "STRUCTURAL_REJECT"}
    recovery = {"id": "t", "status": "recoverable"}
    passed = {"id": "t", "status": "strict_pass", "pass_to_pass": 1}

    assert MODULE.preference(structural) > MODULE.preference(recovery)
    assert MODULE.preference(passed) > MODULE.preference(structural)
