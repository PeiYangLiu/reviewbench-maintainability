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
