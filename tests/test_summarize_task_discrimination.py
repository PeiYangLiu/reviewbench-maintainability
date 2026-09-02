import importlib.util
import json
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "summarize_task_discrimination",
    Path(__file__).resolve().parents[1] / "scripts" / "summarize_task_discrimination.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_a_task_nobody_passed_is_separated_from_one_everybody_passed():
    """Both fail to order models, but only one may be a defect."""
    assert MODULE.classify({f"m{i}": False for i in range(11)}, 8) == "none_passed"
    assert MODULE.classify({f"m{i}": True for i in range(11)}, 8) == "all_passed"
    assert MODULE.classify({"a": True, "b": False, "c": True}, 3) == "discriminating"


def test_thin_coverage_is_not_mistaken_for_a_hard_task():
    """Two models failing says nothing; the task simply was not run enough."""
    assert MODULE.classify({"a": False, "b": False}, 8) == "under_covered"


def test_a_failed_baseline_marks_a_task_as_broken_rather_than_hard():
    """The oracle dying at the unpatched start means no patch could have passed."""
    records = [
        {"failure_root_cause": "post-failure exact-start diagnostic baseline failed"}
        for _ in range(11)
    ]
    assert MODULE.baseline_failure_rate(records) == 1.0


def test_a_task_everyone_failed_on_tests_keeps_a_clean_baseline():
    """These mark the ceiling of current capability and are worth keeping."""
    records = [
        {"failure_root_cause": "candidate failed the frozen P2P Oracle while exact-start diagnostic passed"}
        for _ in range(11)
    ]
    assert MODULE.baseline_failure_rate(records) == 0.0


def test_load_reads_status_and_lane_together(tmp_path):
    d = tmp_path / "correctness-normalized"
    d.mkdir(parents=True)
    (d / "jvm.jsonl").write_text(
        "\n".join(
            json.dumps({"id": "t1", "model": m, "status": s, "lane": "jvm"})
            for m, s in (("a", "pass"), ("b", "fail"))
        )
        + "\n",
        encoding="utf-8",
    )
    status, rows, lane = MODULE.load(str(tmp_path))
    assert status["t1"] == {"a": True, "b": False}
    assert lane["t1"] == "jvm"
    assert len(rows["t1"]) == 2


def test_an_infra_retry_is_not_counted_as_a_model_failure():
    """infra_retry means the harness broke, so no verdict exists to count.

    Three mfscreener tasks returned infra_retry for every model across nine
    runs and never once produced pass or fail. Reading that as unanimous
    failure made them the hardest-looking tasks in the set.
    """
    assert MODULE.classify({f"m{i}": None for i in range(11)}, 8) == "no_verdict"


def test_a_model_that_produced_no_patch_still_counts_as_failing():
    """candidate_missing and scope_error are the model's fault, not the rig's."""
    assert "candidate_missing" not in MODULE.NO_VERDICT
    assert "scope_error" not in MODULE.NO_VERDICT
    assert MODULE.NO_VERDICT == ("infra_retry",)


def test_partial_infra_noise_falls_back_to_the_real_verdicts():
    assert MODULE.classify({"a": True, "b": False, "c": None}, 2) == "discriminating"
