import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
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


def test_native_tool_text_converts_one_function_call():
    response = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "find",
                                "arguments": '{"path":"src/a.py","query":"x"}',
                            }
                        }
                    ]
                },
            }
        ]
    }

    assert MODULE.parse_action(MODULE.native_tool_text(response)) == {
        "action": "find",
        "path": "src/a.py",
        "query": "x",
    }


def test_native_tool_text_rejects_multiple_calls():
    response = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {"function": {"name": "read", "arguments": "{}"}},
                        {"function": {"name": "finish", "arguments": "{}"}},
                    ]
                }
            }
        ]
    }

    with pytest.raises(ValueError, match="unsupported action"):
        MODULE.parse_action(MODULE.native_tool_text(response))


def test_append_native_turn_preserves_tool_history():
    history = []
    response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning_opaque": "opaque",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "read",
                                "arguments": "{}",
                            },
                        }
                    ],
                }
            }
        ]
    }

    MODULE.append_native_turn(history, response, {"ok": True})

    assert history[0]["reasoning_opaque"] == "opaque"
    assert history[1] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": '{"ok": true}',
    }


def test_native_edit_deadline_leaves_final_rounds_for_completion():
    assert 1 < MODULE.NATIVE_EDIT_DEADLINE < 32


def test_tool_transcript_keeps_recent_events_within_budget():
    events = [
        {"assistant_action": {"action": "find", "query": str(index)}}
        for index in range(20)
    ]

    rendered = MODULE.render_tool_transcript(events, 500)

    assert len(rendered) <= 500
    assert "Older tool events were omitted" in rendered
    assert '"query": "19"' in rendered
    assert '"query": "0"' not in rendered


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


def test_parse_action_rejects_non_json_ellipsis_values():
    with pytest.raises(ValueError, match="not JSON serializable"):
        MODULE.parse_action(
            "{'action':'edit','edits':[{'start_line':1,"
            "'end_line':1,'content':...}]}"
        )


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


def test_apply_line_edits_preserves_crlf():
    after, _ = MODULE.apply_line_edits(
        "one\r\ntwo\r\nthree\r\n",
        [{"start_line": 2, "end_line": 2, "content": "TWO"}],
    )

    assert after == "one\r\nTWO\r\nthree\r\n"


def test_git_diff_applies_to_crlf_source(tmp_path):
    before = "one\r\ntwo\r\nthree\r\n"
    after, _ = MODULE.apply_line_edits(
        before,
        [{"start_line": 2, "end_line": 2, "content": "TWO"}],
    )
    patch = MODULE.git_diff("src/a.txt", before, after)
    repo = tmp_path / "repo"
    target = repo / "src/a.txt"
    target.parent.mkdir(parents=True)
    target.write_bytes(before.encode())
    subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "start"], cwd=repo, check=True)
    patch_path = tmp_path / "candidate.patch"
    patch_path.write_text(patch)

    subprocess.run(["git", "apply", "--check", str(patch_path)], cwd=repo, check=True)


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
        "protocol": MODULE.PROTOCOL_V1,
        "rounds": [{"round": index} for index in range(1, 33)],
    }
    assert MODULE.previous_result_for_retry(failed, restart_failed=True) is None
    assert MODULE.previous_result_for_retry(failed, restart_failed=False) is failed


def test_restart_failed_preserves_nonterminal_checkpoint():
    checkpoint = {
        "status": "in_progress",
        "apply_ok": False,
        "protocol": MODULE.PROTOCOL_V1,
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
        "protocol": MODULE.PROTOCOL_V1,
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


# The BSMPT board task was rejected 24 times in a row with this exact message:
# a 120000-char transcript rendered a prompt one token over the served window,
# and the harness resent it every round until the round budget ran out.
BSMPT_REJECTION = (
    "HTTP 400: {\"error\":{\"message\":\"This model's maximum context length is "
    "65536 tokens. However, you requested 4096 output tokens and your prompt "
    "contains at least 61441 input tokens, for a total of at least 65537 "
    "tokens. Please reduce the length of the input prompt or the number of "
    "requested output tokens.\"}}"
)


def test_parse_context_overflow_reads_the_served_window():
    assert MODULE.parse_context_overflow(BSMPT_REJECTION) == (65536, 61441)


def test_parse_context_overflow_ignores_unrelated_failures():
    assert MODULE.parse_context_overflow("HTTP 500: upstream unavailable") is None
    assert MODULE.parse_context_overflow("model did not return a valid JSON action") is None


def test_shrink_transcript_budget_trims_only_what_overflows():
    shrunk = MODULE.shrink_transcript_budget(
        budget=120_000,
        prompt_chars=150_000,
        limit_tokens=65_536,
        prompt_tokens=61_441,
        output_tokens=4_096,
    )
    assert shrunk is not None
    # One token over, so the trim stays far closer to the shared budget than to
    # the hand-set 80000 the retry waves used.
    assert 118_000 < shrunk < 120_000


def test_shrink_transcript_budget_refuses_to_work_blind():
    assert (
        MODULE.shrink_transcript_budget(
            budget=25_000,
            prompt_chars=150_000,
            limit_tokens=4_096,
            prompt_tokens=60_000,
            output_tokens=4_096,
        )
        is None
    )


def test_invoke_retries_within_the_window_instead_of_burning_the_round(monkeypatch):
    calls = []

    def fake_invoke(model, prompt, args, native_messages=None, native_tool_choice=None):
        calls.append(len(prompt))
        if len(calls) == 1:
            raise RuntimeError(BSMPT_REJECTION)
        return "{\"action\":\"finish\"}", {}, {"model": model}

    monkeypatch.setattr(MODULE, "invoke", fake_invoke)
    round_result = {"prompt_sha256": "stale"}
    args = SimpleNamespace(max_output_tokens=4096)

    text, _usage, _raw = MODULE.invoke_within_context_window(
        "Qwen/Qwen3-Coder-Next-FP8",
        "x" * 150_000,
        args,
        native_messages=None,
        native_tool_choice=None,
        round_result=round_result,
        refit=lambda budget: ("x" * budget, budget),
        transcript_budget=120_000,
        transcript_chars=120_000,
    )

    assert text == '{"action":"finish"}'
    assert len(calls) == 2
    assert calls[1] < calls[0]
    assert round_result["transcript_budget_chars"] < 120_000
    assert round_result["transcript_shrinks"][0]["from"] == 120_000
    assert round_result["prompt_sha256"] != "stale"


def test_invoke_does_not_swallow_unrelated_errors(monkeypatch):
    def fake_invoke(model, prompt, args, native_messages=None, native_tool_choice=None):
        raise RuntimeError("HTTP 503: service unavailable")

    monkeypatch.setattr(MODULE, "invoke", fake_invoke)
    with pytest.raises(RuntimeError, match="503"):
        MODULE.invoke_within_context_window(
            "m",
            "prompt",
            SimpleNamespace(max_output_tokens=4096),
            native_messages=None,
            native_tool_choice=None,
            round_result={},
            refit=lambda budget: ("p", 1),
            transcript_budget=120_000,
        )


def test_native_history_models_cannot_be_silently_trimmed(monkeypatch):
    def fake_invoke(model, prompt, args, native_messages=None, native_tool_choice=None):
        raise RuntimeError(BSMPT_REJECTION)

    monkeypatch.setattr(MODULE, "invoke", fake_invoke)
    with pytest.raises(RuntimeError, match="maximum context length"):
        MODULE.invoke_within_context_window(
            "gemini-3.6-flash",
            "prompt",
            SimpleNamespace(max_output_tokens=4096),
            native_messages=[{"role": "user", "content": "hi"}],
            native_tool_choice="auto",
            round_result={},
            refit=None,
            transcript_budget=120_000,
        )


def test_untrimmed_rounds_keep_their_prompt_untouched(monkeypatch):
    def fake_invoke(model, prompt, args, native_messages=None, native_tool_choice=None):
        return "ok", {}, None

    monkeypatch.setattr(MODULE, "invoke", fake_invoke)
    round_result = {"prompt_sha256": "original"}
    MODULE.invoke_within_context_window(
        "m",
        "prompt",
        SimpleNamespace(max_output_tokens=4096),
        native_messages=None,
        native_tool_choice=None,
        round_result=round_result,
        refit=lambda budget: ("p", 1),
        transcript_budget=120_000,
    )
    assert round_result == {"prompt_sha256": "original"}


CHARS_PER_TOKEN = 2.5
KIMI_WINDOW = 32_768


def deployment(window=KIMI_WINDOW, output=4_096):
    """A fake deployment that rejects prompts the way vLLM does."""

    def invoke(model, prompt, args, native_messages=None, native_tool_choice=None):
        tokens = int(len(prompt) / CHARS_PER_TOKEN)
        if tokens + output > window:
            raise RuntimeError(
                "HTTP 400: {\"error\":{\"message\":\"This model's maximum context "
                f"length is {window} tokens. However, you requested {output} output "
                f"tokens and your prompt contains at least {tokens} input tokens, "
                f"for a total of at least {tokens + output} tokens.\"}}}}"
            )
        return '{"action":"finish"}', {"prompt_tokens": tokens}, None

    return invoke


def transcript_refit(base_chars, rendered):
    """Re-render like the harness: the transcript shrinks only to the budget."""

    def refit(budget):
        rendered["chars"] = min(budget, rendered["chars"])
        return "x" * (base_chars + rendered["chars"]), rendered["chars"]

    return refit


def test_trimming_is_anchored_to_the_rendered_transcript_not_the_budget():
    """Kimi burned 29 rounds because the first trims changed nothing.

    Its nominal budget was 120000 chars while the rendered transcript held far
    less, so subtracting the overflow from the budget produced a number still
    above the rendered length, which re-rendered byte-for-byte identically.
    """
    rendered = 60_000
    args = dict(
        prompt_chars=101_040,
        limit_tokens=KIMI_WINDOW,
        prompt_tokens=40_416,
        output_tokens=4_096,
    )
    anchored_on_budget = MODULE.shrink_transcript_budget(120_000, **args)
    anchored_on_rendered = MODULE.shrink_transcript_budget(rendered, **args)

    assert anchored_on_budget > rendered, "this trim would re-render unchanged"
    assert anchored_on_rendered < rendered, "this trim actually removes transcript"


def test_every_trim_makes_the_prompt_strictly_shorter(monkeypatch):
    """The loop must converge instead of spending attempts on identical prompts."""
    rendered = {"chars": 60_000}
    seen = []

    invoke = deployment()

    def counting_invoke(model, prompt, *a, **kw):
        seen.append(len(prompt))
        return invoke(model, prompt, *a, **kw)

    monkeypatch.setattr(MODULE, "invoke", counting_invoke)
    round_result = {}
    text, _usage, _raw = MODULE.invoke_within_context_window(
        "moonshotai/Kimi-K2.5",
        "x" * 90_000,
        SimpleNamespace(max_output_tokens=4_096),
        native_messages=None,
        native_tool_choice=None,
        round_result=round_result,
        refit=transcript_refit(30_000, rendered),
        transcript_budget=120_000,
        transcript_chars=60_000,
    )

    assert text == '{"action":"finish"}'
    assert seen == sorted(seen, reverse=True), seen
    assert len(set(seen)) == len(seen), "a trim left the prompt unchanged"
    assert round_result["transcript_budget_chars"] < 60_000


def test_a_round_that_trims_teaches_the_next_round(monkeypatch):
    """Otherwise every later round repeats the same trims, as Kimi's rounds did."""
    rendered = {"chars": 60_000}
    calls = {"n": 0}
    reject = deployment()

    def flaky(model, prompt, *a, **kw):
        calls["n"] += 1
        reject(model, prompt, *a, **kw)
        raise RuntimeError("HTTP 503: service unavailable")

    monkeypatch.setattr(MODULE, "invoke", flaky)
    round_result = {}
    with pytest.raises(RuntimeError, match="503"):
        MODULE.invoke_within_context_window(
            "moonshotai/Kimi-K2.5",
            "x" * 90_000,
            SimpleNamespace(max_output_tokens=4_096),
            native_messages=None,
            native_tool_choice=None,
            round_result=round_result,
            refit=transcript_refit(30_000, rendered),
            transcript_budget=120_000,
            transcript_chars=60_000,
        )
    # The budget survives the failed round, so the next round starts trimmed.
    assert round_result["transcript_budget_chars"] < 60_000


def test_a_prompt_that_cannot_fit_is_refused_rather_than_sent_blind(monkeypatch):
    """When the fixed part alone overflows, no transcript trim can rescue it."""
    rendered = {"chars": 60_000}
    monkeypatch.setattr(MODULE, "invoke", deployment())
    with pytest.raises(RuntimeError, match="maximum context length"):
        MODULE.invoke_within_context_window(
            "moonshotai/Kimi-K2.5",
            "x" * 150_000,
            SimpleNamespace(max_output_tokens=4_096),
            native_messages=None,
            native_tool_choice=None,
            round_result={},
            refit=transcript_refit(120_000, rendered),
            transcript_budget=120_000,
            transcript_chars=60_000,
        )


# --- agentic-line-edit/v2: structured conversation history ------------------


V2_TASK = {
    "id": "task",
    "repo": "acme/widget",
    "task_instruction": "Extract the duplicated branch into a helper.",
    "locality": {"path": "src/widget.py", "line": 2},
}
V2_SOURCE = "def widget():\n    return 1\n"


def user_event(content, banner=""):
    """Build a user event through the harness so tests track its real shape."""
    events = []
    MODULE.append_conversation_user(events, content, banner)
    return events[0]


def v2_messages(events, tool_result_chars=MODULE.MAX_TOOL_RESULT_CHARS):
    return MODULE.conversation_messages(
        V2_TASK,
        V2_SOURCE,
        events,
        30_000,
        tool_result_chars,
    )


def test_v2_carries_the_harness_system_prompt_unchanged():
    messages = v2_messages([])
    assert messages[0] == {
        "role": "system",
        "content": MODULE.AGENT_INSTRUCTIONS,
    }
    assert messages[0]["content"] is not MODULE.NATIVE_TOOL_INSTRUCTIONS


def test_v2_initial_user_message_is_the_plain_initial_prompt():
    """The training builder uses build_initial_prompt verbatim as user turn one.

    Any suffix the harness adds here would condition the models on an input the
    training data never carries, which is how the previous two SFT rounds were
    lost.
    """
    messages = v2_messages([])
    assert messages[1] == {
        "role": "user",
        "content": MODULE.build_initial_prompt(V2_TASK, V2_SOURCE, 30_000),
    }
    assert "TOOL TRANSCRIPT" not in messages[1]["content"]
    assert "Return the next JSON action only." not in messages[1]["content"]


def test_v2_history_alternates_assistant_and_user_without_a_tool_role():
    events = []
    MODULE.append_conversation_assistant(events, '{"action":"find"}')
    MODULE.append_conversation_user(events, '{"matches":[]}')
    MODULE.append_conversation_assistant(events, '{"action":"finish"}')
    roles = [message["role"] for message in v2_messages(events)]

    assert roles == ["system", "user", "assistant", "user", "assistant"]
    assert "tool" not in roles


def test_v2_assistant_turn_keeps_the_models_own_bytes():
    events = []
    raw = '<think>\nthe branches are identical\n</think>\n\n{"action":"finish"}'
    MODULE.append_conversation_assistant(events, raw)
    assert v2_messages(events)[2] == {"role": "assistant", "content": raw}


def test_v2_tool_content_matches_the_v1_transcript_serialization():
    """v1, v2 and the training builder must carry identical bytes per step."""
    result = {"query": "\u00e1", "ok": True, "matches": [1, 2]}
    assert MODULE.serialize_tool_result(result) == json.dumps(
        result,
        ensure_ascii=False,
        sort_keys=True,
    )
    assert MODULE.serialize_tool_result(result) == MODULE.render_tool_transcript(
        [result],
        0,
    )


def test_v2_truncates_one_over_long_result_instead_of_dropping_the_round():
    events = []
    MODULE.append_conversation_assistant(events, '{"action":"read"}')
    MODULE.append_conversation_user(events, "y" * 50_000)
    MODULE.append_conversation_assistant(events, '{"action":"find"}')
    MODULE.append_conversation_user(events, '{"matches":[]}')
    messages = v2_messages(events, tool_result_chars=24_000)

    assert len(messages) == 6
    assert messages[3]["content"] == "y" * 24_000 + '..."<truncated>"}'
    assert messages[5]["content"] == '{"matches":[]}'


def test_v2_folds_an_unanswered_round_into_the_preceding_user_turn():
    """A round the model never answered leaves no assistant turn to pair with.

    Appending a second user message would break the alternation the training
    builder validates, and sending an unchanged conversation would spend the
    next round on the identical request that just failed.
    """
    events = []
    MODULE.append_conversation_assistant(events, '{"action":"read"}')
    MODULE.append_conversation_user(events, '{"lines":"1: x"}')
    MODULE.append_conversation_user(events, '{"tool_error":"empty response"}')
    roles = [message["role"] for message in v2_messages(events)]
    assert roles == ["system", "user", "assistant", "user"]
    assert (
        MODULE.render_user_event(events[-1], MODULE.MAX_TOOL_RESULT_CHARS)
        == '{"lines":"1: x"}\n{"tool_error":"empty response"}'
    )


def test_v2_prompt_hash_covers_the_whole_message_sequence():
    first = MODULE.serialize_conversation(v2_messages([]))
    events = []
    MODULE.append_conversation_assistant(events, '{"action":"finish"}')
    second = MODULE.serialize_conversation(v2_messages(events))

    assert first != second
    assert json.loads(first)["messages"][0]["role"] == "system"


def test_v2_shrink_removes_at_least_what_overflowed():
    events = [
        user_event("a" * 30_000),
        {"role": "assistant", "content": "ignored"},
        user_event("b" * 30_000),
    ]
    cap = MODULE.shrink_tool_result_cap(events, 24_000, 10_000)

    assert cap is not None and cap < 24_000
    before = MODULE.conversation_tool_chars(events, 24_000)
    assert before - MODULE.conversation_tool_chars(events, cap) >= 10_000


def test_v2_shrink_refuses_when_no_cap_can_free_the_space():
    """Every accepted trim must shorten the prompt, or it burns an attempt (P65)."""
    events = [user_event("a" * 100)]
    assert MODULE.shrink_tool_result_cap(events, 24_000, 10_000) is None
    assert MODULE.shrink_tool_result_cap([], 24_000, 10_000) is None
    assert (
        MODULE.shrink_tool_result_cap(
            [user_event("a" * 100_000)],
            MODULE.MIN_TOOL_RESULT_CHARS,
            10_000,
        )
        is None
    )


def test_v2_shrink_keeps_the_largest_cap_that_fits():
    events = [user_event("a" * 30_000)]
    cap = MODULE.shrink_tool_result_cap(events, 24_000, 1_000)
    assert cap is not None
    assert MODULE.conversation_tool_chars(events, cap + 1) > MODULE.conversation_tool_chars(
        events,
        24_000,
    ) - 1_000


def test_v2_invoke_trims_instead_of_burning_the_round(monkeypatch):
    seen = []

    def fake_invoke(model, prompt, args, native_messages=None,
                    native_tool_choice=None, conversation=None):
        seen.append(len(MODULE.serialize_conversation(conversation)))
        if len(seen) == 1:
            raise RuntimeError(BSMPT_REJECTION)
        return '{"action":"finish"}', {}, {"model": model}

    monkeypatch.setattr(MODULE, "invoke", fake_invoke)
    events = [user_event("a" * 200_000)]
    round_result = {"prompt_sha256": "stale"}

    text, _usage, _raw = MODULE.invoke_conversation_within_context_window(
        "Qwen/Qwen3-Coder-Next-FP8",
        v2_messages(events),
        SimpleNamespace(max_output_tokens=4096),
        round_result=round_result,
        events=events,
        refit=lambda cap: v2_messages(events, cap),
        tool_result_chars=24_000,
    )

    assert text == '{"action":"finish"}'
    assert len(seen) == 2 and seen[1] < seen[0]
    assert round_result["tool_result_chars"] < 24_000
    assert round_result["tool_result_shrinks"][0]["from"] == 24_000
    assert round_result["prompt_sha256"] != "stale"


def test_v2_invoke_does_not_swallow_unrelated_errors(monkeypatch):
    def fake_invoke(model, prompt, args, native_messages=None,
                    native_tool_choice=None, conversation=None):
        raise RuntimeError("HTTP 503: service unavailable")

    monkeypatch.setattr(MODULE, "invoke", fake_invoke)
    with pytest.raises(RuntimeError, match="503"):
        MODULE.invoke_conversation_within_context_window(
            "m",
            v2_messages([]),
            SimpleNamespace(max_output_tokens=4096),
            round_result={},
            events=[],
            refit=lambda cap: v2_messages([], cap),
            tool_result_chars=24_000,
        )


def test_v2_refuses_a_conversation_that_cannot_be_made_to_fit(monkeypatch):
    def fake_invoke(model, prompt, args, native_messages=None,
                    native_tool_choice=None, conversation=None):
        raise RuntimeError(BSMPT_REJECTION)

    monkeypatch.setattr(MODULE, "invoke", fake_invoke)
    events = [user_event("a" * 100)]
    with pytest.raises(RuntimeError, match="maximum context length"):
        MODULE.invoke_conversation_within_context_window(
            "m",
            v2_messages(events),
            SimpleNamespace(max_output_tokens=4096),
            round_result={},
            events=events,
            refit=lambda cap: v2_messages(events, cap),
            tool_result_chars=24_000,
        )


def test_v2_conversation_is_sent_as_messages_never_as_a_tool_role(monkeypatch):
    captured = {}

    def fake_post_json(url, payload, headers, timeout=300, retries=6):
        captured["payload"] = payload
        return {
            "model": "claude-opus-5",
            "choices": [{"message": {"content": '{"action":"finish"}'}}],
            "usage": {},
        }

    monkeypatch.setattr(MODULE.api, "post_json", fake_post_json)
    events = []
    MODULE.append_conversation_assistant(events, '{"action":"read"}')
    MODULE.append_conversation_user(events, '{"lines":"1: x"}')
    args = SimpleNamespace(
        chat_endpoint="http://127.0.0.1:4000/v1/chat/completions",
        responses_endpoint="",
        token_endpoint="",
        max_output_tokens=4096,
        request_timeout=30,
        request_retries=1,
        force_chat=True,
    )

    text, _usage, _raw = MODULE.invoke(
        "claude-opus-5",
        "",
        args,
        conversation=v2_messages(events),
    )

    roles = [message["role"] for message in captured["payload"]["messages"]]
    assert text == '{"action":"finish"}'
    assert roles == ["system", "user", "assistant", "user"]
    assert "tools" not in captured["payload"]
    assert captured["payload"]["messages"][0]["content"] == MODULE.AGENT_INSTRUCTIONS


def test_v2_never_takes_the_native_function_calling_path(monkeypatch):
    """Gemini's special case is a v1 workaround; v2 puts every model on one path."""
    captured = {}

    def fake_post_json(url, payload, headers, timeout=300, retries=6):
        captured["payload"] = payload
        return {
            "model": "gemini-3.6-flash",
            "choices": [{"message": {"content": '{"action":"finish"}'}}],
            "usage": {},
        }

    monkeypatch.setattr(MODULE.api, "post_json", fake_post_json)
    args = SimpleNamespace(
        chat_endpoint="http://127.0.0.1:4000/v1/chat/completions",
        responses_endpoint="",
        token_endpoint="",
        max_output_tokens=4096,
        request_timeout=30,
        request_retries=1,
        force_chat=True,
    )

    MODULE.invoke("gemini-3.6-flash", "", args, conversation=v2_messages([]))

    assert "tools" not in captured["payload"]
    assert captured["payload"]["messages"][0]["content"] == MODULE.AGENT_INSTRUCTIONS


def test_v2_rebuilds_the_conversation_from_a_resumed_trace():
    rounds = [
        {
            "round": 1,
            "raw_response": '<think>look</think>{"action":"read"}',
            "action": {"action": "read"},
            "tool_result": {"lines": "1: x", "ok": True},
        },
        {
            "round": 2,
            "raw_response": "sorry, here is a diff",
            "error": "model did not return a valid JSON action",
        },
        {
            "round": 3,
            "action": {"action": "finish"},
            "tool_result": {"error": "finish rejected", "ok": False},
        },
    ]
    events = MODULE.conversation_events_from_rounds(rounds)

    assert [event["role"] for event in events] == [
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    assert events[0]["content"] == '<think>look</think>{"action":"read"}'
    assert MODULE.render_user_event(
        events[3], MODULE.MAX_TOOL_RESULT_CHARS
    ) == (
        '{"tool_error": "model did not return a valid JSON action"}'
    )
    # No raw response was recorded, so the canonical action stands in for it.
    assert events[4]["content"] == '{"action": "finish"}'


def test_v2_resume_of_an_unanswered_round_keeps_roles_alternating():
    rounds = [
        {"round": 1, "action": {"action": "read"}, "tool_result": {"ok": True}},
        {"round": 2, "error": "HTTP 503: service unavailable"},
    ]
    events = MODULE.conversation_events_from_rounds(rounds)
    assert [event["role"] for event in events] == ["assistant", "user"]
    assert MODULE.render_user_event(
        events[1], MODULE.MAX_TOOL_RESULT_CHARS
    ) == (
        '{"ok": true}\n{"tool_error": "HTTP 503: service unavailable"}'
    )


def test_protocols_are_never_spliced_into_one_trace():
    v1_item = {
        "status": "in_progress",
        "protocol": MODULE.PROTOCOL_V1,
        "rounds": [{"round": 1}],
    }
    assert (
        MODULE.previous_result_for_retry(
            v1_item,
            restart_failed=False,
            protocol=MODULE.PROTOCOL_V2,
        )
        is None
    )
    assert (
        MODULE.previous_result_for_retry(
            v1_item,
            restart_failed=False,
            protocol=MODULE.PROTOCOL_V1,
        )
        is v1_item
    )


def test_v1_results_are_not_reused_by_a_v2_run(tmp_path):
    patch = tmp_path / "task.patch"
    patch.write_text("diff", encoding="utf-8")
    item = {
        "apply_ok": True,
        "protocol": MODULE.PROTOCOL_V1,
        "patch_file": str(patch),
        "patch_sha256": hashlib.sha256(b"diff").hexdigest(),
        "path": "src/widget.py",
        "rounds": [],
    }
    assert MODULE.reusable_result(item, None, MODULE.PROTOCOL_V1) is True
    assert MODULE.reusable_result(item, None, MODULE.PROTOCOL_V2) is False


def test_completion_marker_refuses_a_model_whose_rows_span_protocols(tmp_path):
    results = tmp_path / "generation_results.jsonl"
    results.write_text("{}\n", encoding="utf-8")
    task = {"id": "task", "task_instruction": "x"}
    fingerprint = MODULE.task_fingerprint(task)
    records = {
        "a": {"protocol": MODULE.PROTOCOL_V2, "task_fingerprint": fingerprint},
        "b": {"protocol": MODULE.PROTOCOL_V1, "task_fingerprint": fingerprint},
    }
    tasks = {"a": task, "b": task}

    MODULE.write_completion_marker(
        results,
        "m",
        tasks,
        records,
        "sha",
        protocol=MODULE.PROTOCOL_V2,
    )
    assert not MODULE.completion_marker(results).exists()

    records["b"]["protocol"] = MODULE.PROTOCOL_V2
    MODULE.write_completion_marker(
        results,
        "m",
        tasks,
        records,
        "sha",
        protocol=MODULE.PROTOCOL_V2,
    )
    marker = json.loads(MODULE.completion_marker(results).read_text())
    assert marker["protocol"] == MODULE.PROTOCOL_V2


def test_v1_round_prompt_is_unchanged_by_the_v2_work():
    prompt, chars = MODULE.render_round_prompt(
        V2_TASK,
        V2_SOURCE,
        [{"assistant_action": {"action": "read"}, "tool_result": {"ok": True}}],
        30_000,
        120_000,
    )
    assert "TOOL TRANSCRIPT:" in prompt
    assert prompt.endswith("Return the next JSON action only.")
    assert chars > 0


def test_v2_refuses_a_conversation_with_an_altered_system_prompt():
    """A changed system prompt is what invalidated the previous two SFT rounds."""
    messages = v2_messages([])
    messages[0] = {
        "role": "system",
        "content": MODULE.AGENT_INSTRUCTIONS + "\n\nThink step by step first.",
    }
    args = SimpleNamespace(
        chat_endpoint="",
        responses_endpoint="",
        token_endpoint="",
        max_output_tokens=4096,
        request_timeout=30,
        request_retries=1,
        force_chat=True,
    )
    with pytest.raises(RuntimeError, match="AGENT_INSTRUCTIONS"):
        MODULE.invoke("claude-opus-5", "", args, conversation=messages)


def test_v2_names_injected_tool_calls_instead_of_blaming_the_model(monkeypatch):
    """An empty answer caused by an injected tool is a deployment fact (P78)."""
    def fake_post_json(url, payload, headers, timeout=300, retries=6):
        assert "tools" not in payload
        return {
            "model": "gemini-3.6-flash",
            "choices": [
                {
                    "finish_reason": "function_call",
                    "message": {
                        "content": "",
                        "role": "assistant",
                        "tool_calls": [
                            {"function": {"name": "str_replace_editor"}}
                        ],
                    },
                }
            ],
            "usage": {},
        }

    monkeypatch.setattr(MODULE.api, "post_json", fake_post_json)
    args = SimpleNamespace(
        chat_endpoint="http://127.0.0.1:4000/v1/chat/completions",
        responses_endpoint="",
        token_endpoint="",
        max_output_tokens=4096,
        request_timeout=30,
        request_retries=1,
        force_chat=True,
    )
    with pytest.raises(RuntimeError, match="never declared"):
        MODULE.invoke("gemini-3.6-flash", "", args, conversation=v2_messages([]))


def test_v1_native_tool_calls_are_still_legitimate():
    """v1's Gemini path declares tools, so its tool calls must not be rejected."""
    native = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {"id": "c1", "function": {"name": "read", "arguments": "{}"}}
                    ],
                }
            }
        ]
    }
    MODULE.reject_undeclared_tool_calls(None)
    assert MODULE.undeclared_tool_calls(native)
    assert MODULE.native_tool_text(native)


def test_v2_names_a_bare_server_error_finish_reason():
    """The common shape of P78 carries no tool calls at all, only the flag."""
    with pytest.raises(RuntimeError, match="finish_reason=error"):
        MODULE.reject_undeclared_tool_calls(
            {
                "choices": [
                    {"finish_reason": "error", "message": {"content": "", "role": "assistant"}}
                ]
            }
        )
    MODULE.reject_undeclared_tool_calls(
        {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}
    )
    MODULE.reject_undeclared_tool_calls(
        {"choices": [{"finish_reason": "length", "message": {"content": "{"}}]}
    )


def test_edit_deadline_is_off_by_default_so_the_v2_board_is_reproducible():
    """The published v2 board ran without it; enabling it changes the prompt."""
    parser_default = MODULE.parse_args.__defaults__
    args = SimpleNamespace(edit_deadline=0)
    assert not getattr(args, "edit_deadline")
    baseline = v2_messages([])
    assert MODULE.conversation_messages(
        V2_TASK, V2_SOURCE, [], 30_000, MODULE.MAX_TOOL_RESULT_CHARS, ""
    ) == baseline


def test_edit_deadline_notice_rides_the_trailing_user_turn():
    """It must not touch the system prompt, which is byte-pinned to training."""
    events = []
    MODULE.append_conversation_assistant(events, '{"action":"find"}')
    MODULE.append_conversation_user(events, '{"matches":[]}')
    notice = MODULE.EDIT_DEADLINE_NOTICE.format(used=24, total=32)

    messages = MODULE.conversation_messages(
        V2_TASK, V2_SOURCE, events, 30_000, MODULE.MAX_TOOL_RESULT_CHARS, notice
    )

    assert messages[0]["content"] == MODULE.AGENT_INSTRUCTIONS
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[-1]["content"].endswith(notice)
    assert messages[-1]["content"].startswith('{"matches":[]}')


def test_edit_deadline_notice_appends_a_turn_after_an_assistant_message():
    events = []
    MODULE.append_conversation_assistant(events, '{"action":"find"}')
    notice = MODULE.EDIT_DEADLINE_NOTICE.format(used=24, total=32)

    messages = MODULE.conversation_messages(
        V2_TASK, V2_SOURCE, events, 30_000, MODULE.MAX_TOOL_RESULT_CHARS, notice
    )

    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[-1]["content"] == notice


def test_edit_deadline_notice_changes_the_prompt_hash():
    """A deadline run must not be mistaken for a plain one when auditing."""
    plain = MODULE.serialize_conversation(v2_messages([]))
    nudged = MODULE.serialize_conversation(
        MODULE.conversation_messages(
            V2_TASK,
            V2_SOURCE,
            [],
            30_000,
            MODULE.MAX_TOOL_RESULT_CHARS,
            MODULE.EDIT_DEADLINE_NOTICE.format(used=24, total=32),
        )
    )
    assert plain != nudged


def test_tool_response_protocol_matches_the_training_side_rendering():
    """The wrapper is pinned against `mt_train_v5`: banner inside, one \\n each.

    The training side renders this exact string, and a difference of one byte
    is invisible in a diff but shows up as an adapter that behaves worse than
    its base model.
    """
    events = []
    MODULE.append_conversation_user(
        events,
        '{"ok": true}',
        MODULE.tool_round_banner(MODULE.PROTOCOL_V2_TOOL_RESPONSE, 1, 16),
    )
    assert MODULE.render_user_event(events[0], MODULE.MAX_TOOL_RESULT_CHARS) == (
        '<tool_response>\nROUND 2 OF 16\n{"ok": true}\n</tool_response>'
    )


def test_tool_response_banner_counts_the_round_the_model_will_produce():
    banners = [
        MODULE.tool_round_banner(MODULE.PROTOCOL_V2_TOOL_RESPONSE, n, 16)
        for n in (1, 2, 15)
    ]
    assert banners == ["ROUND 2 OF 16", "ROUND 3 OF 16", "ROUND 16 OF 16"]


def test_v2_never_emits_the_tool_response_wrapper():
    """The published board and every mt7 adapter were produced without it."""
    assert MODULE.tool_round_banner(MODULE.PROTOCOL_V2, 1, 16) == ""
    events = []
    MODULE.append_conversation_user(
        events,
        '{"ok": true}',
        MODULE.tool_round_banner(MODULE.PROTOCOL_V2, 1, 16),
    )
    rendered = MODULE.render_user_event(events[0], MODULE.MAX_TOOL_RESULT_CHARS)
    assert rendered == '{"ok": true}'
    assert MODULE.TOOL_RESPONSE_OPEN not in rendered


def test_capping_a_wrapped_result_keeps_the_wrapper_intact():
    """Capping the rendered block instead would strip the closing tag.

    Qwen decides which turns keep thinking by looking at the *start* of the
    content, so a truncated close tag still passes that check and the damage
    stays silent until the adapter underperforms.
    """
    events = []
    MODULE.append_conversation_user(
        events,
        '{"data": "' + "x" * 50_000 + '"}',
        MODULE.tool_round_banner(MODULE.PROTOCOL_V2_TOOL_RESPONSE, 3, 16),
    )
    rendered = MODULE.render_user_event(events[0], 500)
    assert rendered.startswith(
        f"{MODULE.TOOL_RESPONSE_OPEN}\nROUND 4 OF 16\n"
    )
    assert rendered.endswith(f"\n{MODULE.TOOL_RESPONSE_CLOSE}")
    assert rendered.count(MODULE.TOOL_RESPONSE_OPEN) == 1
    assert rendered.count(MODULE.TOOL_RESPONSE_CLOSE) == 1


def test_folded_wrapped_results_each_keep_their_own_wrapper():
    """A round the model never answered folds two results into one turn."""
    events = []
    for round_number in (1, 2):
        MODULE.append_conversation_user(
            events,
            '{"ok": true}',
            MODULE.tool_round_banner(
                MODULE.PROTOCOL_V2_TOOL_RESPONSE, round_number, 16
            ),
        )
    rendered = MODULE.render_user_event(events[0], MODULE.MAX_TOOL_RESULT_CHARS)
    assert rendered == (
        '<tool_response>\nROUND 2 OF 16\n{"ok": true}\n</tool_response>\n'
        '<tool_response>\nROUND 3 OF 16\n{"ok": true}\n</tool_response>'
    )
    assert rendered.startswith(MODULE.TOOL_RESPONSE_OPEN)


def test_shrink_accounts_for_the_wrapper_overhead():
    """The solver must measure rendered bytes, not payload bytes."""
    events = []
    MODULE.append_conversation_user(
        events,
        "a" * 30_000,
        MODULE.tool_round_banner(MODULE.PROTOCOL_V2_TOOL_RESPONSE, 1, 16),
    )
    cap = MODULE.shrink_tool_result_cap(events, 24_000, 5_000)
    assert cap is not None
    before = len(MODULE.render_user_event(events[0], 24_000))
    after = len(MODULE.render_user_event(events[0], cap))
    assert before - after >= 5_000


def test_resumed_tool_response_run_replays_the_same_banners():
    rounds = [
        {"round": 1, "raw_response": "R1", "tool_result": {"ok": True}},
        {"round": 2, "raw_response": "R2", "tool_result": {"ok": True}},
    ]
    events = MODULE.conversation_events_from_rounds(
        rounds, MODULE.PROTOCOL_V2_TOOL_RESPONSE, 16
    )
    rendered = [
        MODULE.render_user_event(event, MODULE.MAX_TOOL_RESULT_CHARS)
        for event in events
        if event["role"] == "user"
    ]
    assert rendered[0].startswith("<tool_response>\nROUND 2 OF 16\n")
    assert rendered[1].startswith("<tool_response>\nROUND 3 OF 16\n")


def test_unsolved_tasks_do_not_make_the_job_exit_non_zero(tmp_path, monkeypatch):
    """The benchmark exists to count unsolved tasks; they are not job failures.

    Exiting non-zero for them marked every cluster job "failed", which hid the
    jobs that failed for real -- a three-arm run whose third arm had genuinely
    completed looked identical to one that had crashed.
    """
    summary = {"m": {"tasks": 3, "generated": 1, "failed": 2}}
    assert MODULE.exit_code_for_summary(summary, requested_tasks=3) == 0


def test_a_run_that_skipped_tasks_still_exits_non_zero(tmp_path):
    """A crash halfway through must stay visible."""
    summary = {"m": {"tasks": 2, "generated": 2, "failed": 0}}
    assert MODULE.exit_code_for_summary(summary, requested_tasks=3) == 1
