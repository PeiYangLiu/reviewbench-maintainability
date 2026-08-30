"""Guards for the give-up-vs-failed split the RL session reports against."""
import importlib.util
import json
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "summarize_generation_health",
    Path(__file__).resolve().parents[1] / "scripts" / "summarize_generation_health.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def write(tmp_path, records):
    path = tmp_path / "generation_results.jsonl"
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )
    return path


def round_with(action=None, finish_reason=None):
    entry = {}
    if action:
        entry["action"] = {"action": action}
    if finish_reason:
        entry["raw_api_response"] = {"choices": [{"finish_reason": finish_reason}]}
    return entry


def test_a_run_that_never_edited_is_not_counted_as_a_failed_edit(tmp_path):
    """These two want opposite fixes, so they must not share a bucket."""
    path = write(
        tmp_path,
        [
            {"status": "failed", "rounds": [round_with("read"), round_with("find")]},
            {"status": "failed", "rounds": [round_with("read"), round_with("edit")]},
        ],
    )
    stats = MODULE.summarize(path)
    assert stats["candidate_missing"] == 2
    assert stats["never_edited"] == 1
    assert stats["edited_no_patch"] == 1


def test_a_generated_patch_is_never_counted_as_missing(tmp_path):
    path = write(
        tmp_path,
        [{"status": "generated", "apply_ok": True, "rounds": [round_with("edit")]}],
    )
    stats = MODULE.summarize(path)
    assert stats["generated"] == 1
    assert stats["candidate_missing"] == 0
    assert stats["never_edited"] == 0


def test_generated_without_apply_ok_still_counts_as_missing(tmp_path):
    """`status` alone is not enough: a patch that did not apply is not a patch."""
    path = write(
        tmp_path,
        [{"status": "generated", "apply_ok": False, "rounds": [round_with("edit")]}],
    )
    stats = MODULE.summarize(path)
    assert stats["generated"] == 0
    assert stats["candidate_missing"] == 1
    assert stats["edited_no_patch"] == 1


def test_truncation_is_measured_by_finish_reason(tmp_path):
    """A cut-off round looks like giving up, so it is reported separately."""
    path = write(
        tmp_path,
        [
            {
                "status": "failed",
                "rounds": [
                    round_with("read", "stop"),
                    round_with("read", "length"),
                    round_with("read", "stop"),
                    round_with("read", "stop"),
                ],
            }
        ],
    )
    stats = MODULE.summarize(path)
    assert stats["scored_rounds"] == 4
    assert stats["truncated_rounds"] == 1
    assert stats["tasks_with_truncation"] == 1


def test_rounds_without_a_finish_reason_do_not_dilute_the_rate(tmp_path):
    """Some deployments omit it; counting them as clean would understate P82."""
    path = write(
        tmp_path,
        [{"status": "failed", "rounds": [round_with("read"), round_with("read", "length")]}],
    )
    stats = MODULE.summarize(path)
    assert stats["scored_rounds"] == 1
    assert stats["truncated_rounds"] == 1


def test_responses_api_truncation_is_read_from_status(tmp_path):
    """Reading only `finish_reason` makes every responses-API model look clean."""
    path = write(
        tmp_path,
        [
            {
                "status": "failed",
                "rounds": [
                    {"raw_api_response": {"status": "completed"}},
                    {
                        "raw_api_response": {
                            "status": "incomplete",
                            "incomplete_details": {"reason": "max_output_tokens"},
                        }
                    },
                ],
            }
        ],
    )
    stats = MODULE.summarize(path)
    assert stats["scored_rounds"] == 2
    assert stats["truncated_rounds"] == 1


def test_a_deployment_that_reports_nothing_is_not_counted_as_clean(tmp_path):
    path = write(
        tmp_path,
        [{"status": "failed", "rounds": [{"raw_api_response": {"id": "x"}}]}],
    )
    stats = MODULE.summarize(path)
    assert stats["scored_rounds"] == 0
    assert stats["truncated_rounds"] == 0
