import importlib.util
import json
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "summarize_wasted_rounds",
    Path(__file__).resolve().parents[1] / "scripts" / "summarize_wasted_rounds.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_a_rejected_request_and_an_unsendable_conversation_share_a_bucket():
    """Both mean the window was exceeded; separating them implies two fixes."""
    assert MODULE.bucket("HTTP 400: maximum context length is 65536") == "context"
    assert (
        MODULE.bucket("conversation did not fit the served context window after 6 trims")
        == "context"
    )


def test_unparseable_output_is_attributed_to_the_model_not_the_budget():
    assert (
        MODULE.bucket("model did not return a valid JSON action: no object literal found")
        == "malformed"
    )


def test_a_round_carrying_an_action_is_never_counted_as_wasted(tmp_path):
    path = tmp_path / "gen.jsonl"
    path.write_text(
        json.dumps(
            {
                "model": "m",
                "rounds": [
                    {"action": {"action": "edit"}},
                    {"action": None, "error": "HTTP 400: too long"},
                    {"action": None, "error": "model did not return a valid JSON action: x"},
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    per = MODULE.tally([str(path)])
    assert per["m"]["rounds"] == 3
    assert per["m"]["wasted"] == 2
    assert per["m"]["context"] == 1
    assert per["m"]["malformed"] == 1


def test_buckets_sum_to_the_wasted_total(tmp_path):
    """A bucket that silently drops rounds would understate the waste."""
    rounds = [
        {"action": None, "error": e}
        for e in ("HTTP 500", "model did not return a valid JSON action: y", "something else", "")
    ]
    path = tmp_path / "gen.jsonl"
    path.write_text(json.dumps({"model": "m", "rounds": rounds}) + "\n", encoding="utf-8")
    per = MODULE.tally([str(path)])
    assert sum(per["m"][b] for b in MODULE.BUCKETS) == per["m"]["wasted"] == 4
