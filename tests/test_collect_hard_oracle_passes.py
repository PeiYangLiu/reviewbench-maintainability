import importlib.util
import json
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "collect_hard_oracle_passes.py"
SPEC = importlib.util.spec_from_file_location("collect_hard_oracle_passes", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_strict_pass_requires_every_release_gate():
    oracle = {
        "oracle_result": "PASS_TO_PASS",
        "golden_correctness_ok": True,
        "triple_run_stable": True,
        "stable_p2p": True,
        "pass_to_pass": 2,
        "changed_lines_covered": True,
    }

    assert MODULE.strict_pass(oracle)

    for field in (
        "golden_correctness_ok",
        "triple_run_stable",
        "stable_p2p",
        "changed_lines_covered",
    ):
        invalid = dict(oracle)
        invalid[field] = False
        assert not MODULE.strict_pass(invalid)


def test_strict_pass_rejects_empty_p2p():
    oracle = {
        "oracle_result": "PASS_TO_PASS",
        "golden_correctness_ok": True,
        "triple_run_stable": True,
        "stable_p2p": True,
        "pass_to_pass": 0,
        "changed_lines_covered": True,
    }

    assert not MODULE.strict_pass(oracle)


def test_strict_pass_accepts_lane_specific_stability_fields():
    oracle = {
        "status": "strict_pass",
        "golden_correctness_ok": True,
        "stable_start_pass": True,
        "stable_golden_pass": True,
        "pass_to_pass": 1,
        "changed_lines_covered": True,
    }

    assert MODULE.strict_pass(oracle)


def test_strict_pass_accepts_tsjs_run_records():
    runs = [{"exit_code": 0}, {"exit_code": 0}, {"exit_code": 0}]
    oracle = {
        "disposition": "accept",
        "golden_correctness_ok": True,
        "start_runs": runs,
        "source_only_golden_runs": runs,
        "stable_p2p": True,
        "pass_to_pass": 2,
        "changed_lines_covered": True,
    }

    assert MODULE.strict_pass(oracle)
