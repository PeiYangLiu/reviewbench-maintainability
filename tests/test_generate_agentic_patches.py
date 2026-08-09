import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "generate_agentic_patches.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("generate_agentic_patches", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_parse_action_extracts_json():
    action = MODULE.parse_action('{"action":"finish"}')
    assert action == {"action": "finish"}


def test_parse_action_uses_last_valid_object_without_greedy_join():
    action = MODULE.parse_action(
        'I will inspect it.\n{"note":"first"}\n'
        '{"action":"read","path":"src/a.py","start_line":1,"end_line":10}\n'
        '{"action":"finish"}'
    )
    assert action["action"] == "finish"


def test_parse_action_preserves_unescaped_regex_backslashes():
    action = MODULE.parse_action(
        '{"action":"edit","edits":[{"start_line":1,"end_line":1,'
        '"content":"return r\'\\Z\'"}]}'
    )
    assert action["edits"][0]["content"] == "return r'\\Z'"


def test_parse_action_accepts_transcript_wrapper_but_ignores_tool_result():
    action = MODULE.parse_action(
        '{"assistant_action":{"action":"find","path":"src/a.py","query":"value"},'
        '"tool_result":{"matches":[]}}'
    )
    assert action == {"action": "find", "path": "src/a.py", "query": "value"}


def test_parse_action_uses_last_action_after_echoed_transcript():
    action = MODULE.parse_action(
        '{"assistant_action":{"action":"read","path":"src/a.py",'
        '"start_line":1,"end_line":10},"tool_result":{"content":"old"}}\n'
        '{"assistant_action":{"action":"edit","edits":['
        '{"start_line":1,"end_line":1,"content":"new"}]},'
        '"tool_result":null}'
    )
    assert action["action"] == "edit"


def test_parse_action_prefers_edit_over_no_change_finish():
    response = (
        '{"assistant_action":{"action":"edit","edits":['
        '{"start_line":1,"end_line":1,"content":"new"}]},'
        '"tool_result":{"status":"ok"}}\n'
        '{"assistant_action":{"action":"finish"},"tool_result":null}'
    )
    assert MODULE.parse_action(response)["action"] == "finish"
    assert MODULE.parse_action(response, prefer_edit=True)["action"] == "edit"


def test_parse_action_recovers_complete_action_from_truncated_wrapper():
    action = MODULE.parse_action(
        '{"assistant_action":{"action":"edit","edits":['
        '{"start_line":1,"end_line":1,"content":"value = 2"}]}'
    )
    assert action["action"] == "edit"
    assert action["edits"][0]["content"] == "value = 2"


def test_apply_line_edits_replaces_and_inserts_without_search():
    original = "one\ntwo\nthree\n"
    after, applied = MODULE.apply_line_edits(
        original,
        [
            {"start_line": 2, "end_line": 2, "content": "TWO"},
            {"start_line": 4, "end_line": 3, "content": "four"},
        ],
    )
    assert after == "one\nTWO\nthree\nfour\n"
    assert len(applied) == 2


def test_apply_line_edits_rejects_overlap():
    with pytest.raises(ValueError, match="overlap"):
        MODULE.apply_line_edits(
            "one\ntwo\nthree\n",
            [
                {"start_line": 1, "end_line": 2, "content": "x"},
                {"start_line": 2, "end_line": 3, "content": "y"},
            ],
        )


def test_apply_line_edits_rejects_same_insertion_point():
    with pytest.raises(ValueError, match="overlap"):
        MODULE.apply_line_edits(
            "one\ntwo\n",
            [
                {"start_line": 2, "end_line": 1, "content": "first"},
                {"start_line": 2, "end_line": 1, "content": "second"},
            ],
        )


def test_prepare_line_edit_atomically_handles_empty_file():
    candidate, tool_result = MODULE.prepare_line_edit(
        "only line\n",
        [{"start_line": 1, "end_line": 1, "content": ""}],
    )
    assert candidate == ""
    assert tool_result["current_file_lines"] == 0
    assert tool_result["updated_excerpt"]["content"] == ""


def test_resume_rejects_result_with_cross_file_edit():
    result = {
        "apply_ok": True,
        "path": "src/a.py",
        "rounds": [
            {
                "action": {
                    "action": "edit",
                    "path": "src/b.py",
                    "edits": [{"start_line": 1, "end_line": 1, "content": "x"}],
                }
            }
        ],
    }
    assert MODULE.reusable_result(result) is False


def test_resume_ignores_rejected_cross_file_edit(tmp_path):
    patch = tmp_path / "candidate.patch"
    patch.write_text("non-empty\n", encoding="utf-8")
    result = {
        "apply_ok": True,
        "protocol": "agentic-line-edit/v1",
        "patch_file": str(patch),
        "patch_sha256": MODULE.hashlib.sha256(patch.read_bytes()).hexdigest(),
        "path": "src/a.py",
        "rounds": [
            {
                "action": {
                    "action": "edit",
                    "path": "src/b.py",
                    "edits": [{"start_line": 1, "end_line": 1, "content": "x"}],
                },
                "error": "edit action targets a non-editable file",
            }
        ],
    }
    assert MODULE.reusable_result(result) is True


def test_resume_accepts_valid_generated_result(tmp_path):
    patch = tmp_path / "candidate.patch"
    patch.write_text("non-empty\n", encoding="utf-8")
    result = {
        "apply_ok": True,
        "protocol": "agentic-line-edit/v1",
        "patch_file": str(patch),
        "patch_sha256": MODULE.hashlib.sha256(patch.read_bytes()).hexdigest(),
        "path": "src/a.py",
        "rounds": [
            {
                "action": {
                    "action": "edit",
                    "edits": [{"start_line": 1, "end_line": 1, "content": "x"}],
                }
            },
            {"action": {"action": "finish"}},
        ],
    }
    assert MODULE.reusable_result(result) is True


def test_resume_rejects_changed_task_or_patch(tmp_path):
    task = {
        "id": "task",
        "repo": "owner/repo",
        "start_sha": "start",
        "locality": {"path": "src/a.py"},
    }
    patch = tmp_path / "candidate.patch"
    patch.write_text("first")
    result = {
        "apply_ok": True,
        "protocol": "agentic-line-edit/v1",
        "patch_file": str(patch),
        "patch_sha256": MODULE.hashlib.sha256(patch.read_bytes()).hexdigest(),
        "task_fingerprint": MODULE.task_fingerprint(task),
        "path": "src/a.py",
        "rounds": [],
    }
    assert MODULE.reusable_result(result, task) is True
    changed_task = {**task, "task_instruction": "different"}
    assert MODULE.reusable_result(result, changed_task) is False
    patch.write_text("second")
    assert MODULE.reusable_result(result, task) is False


def test_restart_failed_discards_exhausted_trace():
    failed = {
        "status": "failed",
        "apply_ok": False,
        "rounds": [{"round": index} for index in range(1, 33)],
    }
    assert MODULE.previous_result_for_retry(failed, restart_failed=True) is None
    assert MODULE.previous_result_for_retry(failed, restart_failed=False) is failed


def test_restart_failed_preserves_nonterminal_checkpoint():
    checkpoint = {
        "status": "in_progress",
        "apply_ok": False,
        "rounds": [{"round": 1}],
    }
    assert (
        MODULE.previous_result_for_retry(checkpoint, restart_failed=True)
        is checkpoint
    )


def test_changed_task_does_not_resume_prior_trace():
    task = {"id": "task", "task_instruction": "new"}
    previous = {
        "status": "in_progress",
        "task_fingerprint": MODULE.task_fingerprint(
            {"id": "task", "task_instruction": "old"}
        ),
        "rounds": [{"round": 1}],
    }
    assert (
        MODULE.previous_result_for_retry(
            previous,
            restart_failed=False,
            task=task,
        )
        is None
    )


def test_resume_restores_edits_and_transcript_without_resampling():
    previous = {
        "apply_ok": False,
        "rounds": [
            {
                "round": 1,
                "action": {
                    "action": "edit",
                    "edits": [{"start_line": 2, "end_line": 2, "content": "TWO"}],
                },
                "tool_result": {"applied": [{"start_line": 2, "end_line": 2}]},
            }
        ],
    }
    state = MODULE.resume_agent_state(
        "one\ntwo\n",
        "src/a.py",
        previous,
        max_rounds=1,
    )
    assert state is not None
    current, transcript, edited, rounds = state
    assert current == "one\nTWO\n"
    assert edited is True
    assert len(transcript) == len(rounds) == 1


def test_resume_restarts_trace_with_invalid_cross_file_edit():
    previous = {
        "apply_ok": False,
        "rounds": [
            {
                "round": 1,
                "action": {
                    "action": "edit",
                    "path": "src/b.py",
                    "edits": [{"start_line": 1, "end_line": 1, "content": "x"}],
                },
                "tool_result": {"applied": [{"start_line": 1, "end_line": 1}]},
            }
        ],
    }
    assert (
        MODULE.resume_agent_state("x\n", "src/a.py", previous, max_rounds=2)
        is None
    )


def test_git_diff_is_generated_by_git_and_applies(tmp_path):
    patch = MODULE.git_diff("src/example.py", "value = 1\n", "value = 2\n")
    target = tmp_path / "src/example.py"
    target.parent.mkdir(parents=True)
    target.write_text("value = 1\n", encoding="utf-8")
    completed = subprocess.run(
        ["git", "apply", "--check", "-"],
        cwd=tmp_path,
        input=patch,
        text=True,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "-value = 1" in patch
    assert "+value = 2" in patch
