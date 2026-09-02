import importlib.util
import json
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "summarize_correctness_failures",
    Path(__file__).resolve().parents[1] / "scripts" / "summarize_correctness_failures.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_a_baseline_confirmed_break_is_the_patch_not_a_broken_task():
    """The baseline clause is the whole point: it proves the task was solvable."""
    assert (
        MODULE.classify(
            {
                "status": "fail",
                "failure_root_cause": (
                    "candidate failed the frozen P2P Oracle while exact-start "
                    "diagnostic passed"
                ),
            }
        )
        == "regressed"
    )


def test_an_oracle_failure_without_a_clean_baseline_is_not_called_a_regression():
    """Without the baseline clause the task may simply have been broken already."""
    assert (
        MODULE.classify(
            {"status": "fail", "failure_root_cause": "candidate failed the validated P2P Oracle"}
        )
        == "tests_failed"
    )


def test_a_run_that_never_produced_a_patch_is_not_reported_as_an_apply_failure():
    """Both are true of the record; only the first sends someone to the right place."""
    assert (
        MODULE.classify(
            {
                "status": "fail",
                "failure_root_cause": "agent did not finish with a patch",
                "patch_apply": False,
            }
        )
        == "no_patch"
    )


def test_unrecognised_build_output_is_kept_separate_from_real_failure_modes():
    """Around a sixth of records dump raw logs here; folding them in would inflate."""
    assert (
        MODULE.classify({"status": "fail", "failure_root_cause": "Compiling relay-monitors v23"})
        == "unclassified"
    )


def test_every_record_lands_in_exactly_one_declared_bucket(tmp_path):
    records = [
        {"model": "m", "status": "pass"},
        {"model": "m", "status": "fail", "failure_root_cause": "x", "patch_apply": False},
        {"model": "m", "status": "fail", "failure_root_cause": "candidate_oracle_prepare_failed"},
        {"model": "m", "status": "fail"},
    ]
    path = tmp_path / "lane.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    per = MODULE.tally([str(path)])
    assert set(per["m"]) <= set(MODULE.BUCKETS)
    assert sum(per["m"].values()) == len(records)


def test_editing_outside_the_anchor_is_not_reported_as_an_apply_failure():
    """Both set patch_apply false; only one is about the model's judgement."""
    assert (
        MODULE.classify(
            {
                "status": "fail",
                "scope_ok": False,
                "patch_apply": False,
                "failure_root_cause": (
                    "candidate must change only the anchor path and at most 400 lines"
                ),
            }
        )
        == "out_of_scope"
    )


def test_a_genuine_apply_failure_still_lands_in_apply_fail():
    assert (
        MODULE.classify(
            {"status": "fail", "scope_ok": True, "patch_apply": False,
             "failure_root_cause": "git apply returned 1"}
        )
        == "apply_fail"
    )


def test_a_failure_with_test_evidence_but_no_root_cause_is_still_a_test_failure():
    """The legacy lane fills failed_tests and the P2P counts but not the cause."""
    assert (
        MODULE.classify(
            {
                "status": "fail",
                "failure_root_cause": None,
                "failed_tests": ["TestDefaultGenesis"],
                "pass_to_pass_passed": 7,
                "pass_to_pass_total": 17,
            }
        )
        == "tests_failed"
    )


def test_a_failure_with_no_evidence_at_all_stays_unclassified():
    """Guessing here would hide a lane that reports nothing."""
    assert MODULE.classify({"status": "fail", "failure_root_cause": None}) == "unclassified"
