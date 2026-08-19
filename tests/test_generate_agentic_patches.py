import importlib.util
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
