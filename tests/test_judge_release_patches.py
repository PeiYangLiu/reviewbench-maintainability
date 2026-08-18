import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "judge_release_patches.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("judge_release_patches", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_parse_verdict_accepts_strict_schema():
    verdict = MODULE.parse_verdict(
        '{"addressed":"yes","quality_1_to_5":4,'
        '"behavior_preserved":true,"focused":true,"reason":"Complete extraction."}'
    )

    assert verdict["addressed"] == "yes"
    assert verdict["quality_1_to_5"] == 4
    assert verdict["behavior_preserved"] is True
    assert verdict["focused"] is True


def test_parse_verdict_rejects_non_boolean_flags():
    with pytest.raises(ValueError, match="must be booleans"):
        MODULE.parse_verdict(
            '{"addressed":"yes","quality_1_to_5":4,'
            '"behavior_preserved":"yes","focused":true,"reason":"Bad schema."}'
        )


def test_prompt_does_not_include_solver_identity():
    task = {
        "repo": "owner/repo",
        "task_instruction": "Extract the duplicated branch.",
        "locality": {"path": "src/example.py", "line": 1},
    }
    prompt = MODULE.build_prompt(task, "value = 1\n", "@@ -1 +1 @@\n-value = 1\n+value = 2\n", 1000)

    assert "solver_model" not in prompt
    assert "Extract the duplicated branch." in prompt


def test_parse_verdict_v2_requires_new_debt_flag():
    text = (
        '{"addressed":"yes","quality_1_to_5":5,'
        '"introduces_new_debt":false,"reason":"clean extraction"}'
    )

    verdict = MODULE.parse_verdict(text, "v2")

    assert verdict["introduces_new_debt"] is False
    assert "behavior_preserved" not in verdict
    assert "focused" not in verdict


def test_parse_verdict_v2_rejects_missing_new_debt_flag():
    text = '{"addressed":"yes","quality_1_to_5":5,"reason":"x"}'

    with pytest.raises(ValueError, match="introduces_new_debt"):
        MODULE.parse_verdict(text, "v2")


def test_verdict_passes_v2_ignores_behavior_and_focus():
    verdict = {
        "addressed": "yes",
        "quality_1_to_5": 4,
        "introduces_new_debt": False,
        "reason": "x",
    }

    assert MODULE.verdict_passes(verdict, "v2") is True


def test_verdict_passes_v2_rejects_new_debt():
    verdict = {
        "addressed": "yes",
        "quality_1_to_5": 5,
        "introduces_new_debt": True,
        "reason": "x",
    }

    assert MODULE.verdict_passes(verdict, "v2") is False


def test_verdict_passes_v1_still_requires_behavior_and_focus():
    verdict = {
        "addressed": "yes",
        "quality_1_to_5": 5,
        "behavior_preserved": False,
        "focused": True,
        "reason": "x",
    }

    assert MODULE.verdict_passes(verdict, "v1") is False


def test_invoke_judge_rejects_silent_model_fallback(monkeypatch):
    monkeypatch.setattr(
        MODULE.generator,
        "post_json",
        lambda *args, **kwargs: {
            "model": "gpt-5.6-sol",
            "choices": [{"message": {"content": "{}"}}],
            "usage": {},
        },
    )

    with pytest.raises(RuntimeError, match="judge model mismatch"):
        MODULE.invoke_judge(
            "claude-opus-4.8",
            "prompt",
            "http://127.0.0.1:8787/v1/chat/completions",
            128,
            "v2",
        )


def test_invoke_judge_requires_returned_model_identity(monkeypatch):
    monkeypatch.setattr(
        MODULE.generator,
        "post_json",
        lambda *args, **kwargs: {
            "choices": [{"message": {"content": "{}"}}],
            "usage": {},
        },
    )

    with pytest.raises(RuntimeError, match="gateway returned <missing>"):
        MODULE.invoke_judge(
            "claude-sonnet-4.6",
            "prompt",
            "http://127.0.0.1:8787/v1/chat/completions",
            128,
            "v2",
        )


def test_repair_prompt_requires_every_v2_field():
    prompt = MODULE.build_repair_prompt(
        "original prompt",
        '{"addressed":"partial"}',
        "introduces_new_debt must be a boolean",
        "v2",
    )

    assert "original prompt" in prompt
    assert "introduces_new_debt" in prompt
    assert '{"addressed":"partial"}' in prompt
    assert "corrected JSON object only" in prompt


def test_completion_marker_requires_resolved_judgments(tmp_path):
    path = tmp_path / "judgments_v2.jsonl"
    path.write_text(
        json.dumps(
            {
                "id": "task",
                "solver_model": "gpt-5.5",
                "status": "judge_error",
                "task_fingerprint": "task-fingerprint",
                "generation_fingerprint": "generation-fingerprint",
                "judge_protocol_fingerprint": "protocol-fingerprint",
            }
        )
        + "\n"
    )
    results = {
        ("task", "gpt-5.5"): {
            "id": "task",
            "solver_model": "gpt-5.5",
            "status": "judge_error",
            "task_fingerprint": "task-fingerprint",
            "generation_fingerprint": "generation-fingerprint",
            "judge_protocol_fingerprint": "protocol-fingerprint",
        }
    }
    MODULE.write_completion_marker(
        path,
        {"task": "task-fingerprint"},
        {"gpt-5.5"},
        results,
        "tasks-sha",
        {"gpt-5.5": "generation-sha"},
        {("task", "gpt-5.5"): "generation-fingerprint"},
        "protocol-fingerprint",
        "v2",
        "claude-opus-4.8",
        30_000,
        8192,
    )
    assert not MODULE.completion_marker(path).exists()

    results[("task", "gpt-5.5")]["status"] = "judged"
    path.write_text(
        json.dumps(results[("task", "gpt-5.5")]) + "\n"
    )
    MODULE.write_completion_marker(
        path,
        {"task": "task-fingerprint"},
        {"gpt-5.5"},
        results,
        "tasks-sha",
        {"gpt-5.5": "generation-sha"},
        {("task", "gpt-5.5"): "generation-fingerprint"},
        "protocol-fingerprint",
        "v2",
        "claude-opus-4.8",
        30_000,
        8192,
    )
    marker = json.loads(MODULE.completion_marker(path).read_text())
    assert marker["task_model_pairs"] == 1


def test_generation_fingerprint_includes_patch_bytes(tmp_path):
    patch = tmp_path / "candidate.patch"
    patch.write_text("first")
    generation = {
        "id": "task",
        "model": "gpt-5.5",
        "patch_file": str(patch),
        "apply_ok": True,
    }
    before = MODULE.generation_fingerprint(generation)
    patch.write_text("second")
    assert MODULE.generation_fingerprint(generation) != before


def test_judge_protocol_fingerprint_changes_with_rubric_or_context():
    baseline = MODULE.judge_protocol_fingerprint(
        "v2",
        "claude-opus-4.8",
        30_000,
        8192,
    )
    assert baseline != MODULE.judge_protocol_fingerprint(
        "v1",
        "claude-opus-4.8",
        30_000,
        8192,
    )
    assert baseline != MODULE.judge_protocol_fingerprint(
        "v2",
        "claude-opus-4.8",
        10_000,
        8192,
    )
    assert baseline != MODULE.judge_protocol_fingerprint(
        "v2",
        "claude-opus-4.8",
        30_000,
        2048,
    )


# ---------------------------------------------------------------------------
# Unanimous judge panel (maintainability gate only; correctness is executed,
# never judged)
# ---------------------------------------------------------------------------


def panel_member(judge, *, status="judged", passed=True, error=None):
    member = {
        "judge_model": judge,
        "status": status,
        "maintainability_pass": passed,
    }
    if error is not None:
        member["error"] = error
    return member


def test_panel_passes_only_when_every_judge_passes():
    panel = [
        panel_member("claude-opus-5"),
        panel_member("gpt-5.6-sol"),
        panel_member("gemini-3.6-flash"),
    ]
    assert MODULE.combine_panel(panel) == ("judged", True)


@pytest.mark.parametrize("dissenter", [0, 1, 2])
def test_a_single_dissenting_judge_blocks_the_pass(dissenter):
    panel = [
        panel_member("claude-opus-5"),
        panel_member("gpt-5.6-sol"),
        panel_member("gemini-3.6-flash"),
    ]
    panel[dissenter]["maintainability_pass"] = False
    assert MODULE.combine_panel(panel) == ("judged", False)


def test_an_unresolved_judge_never_yields_a_pass():
    # A judge that failed to return a parseable verdict must not be silently
    # treated as either a pass or a considered failure.
    panel = [
        panel_member("claude-opus-5"),
        panel_member("gpt-5.6-sol"),
        panel_member("gemini-3.6-flash", status="judge_error", passed=False,
                     error="gateway timeout"),
    ]
    assert MODULE.combine_panel(panel) == ("judge_error", False)


def test_panel_fingerprint_differs_from_each_member_and_from_reordering():
    panel = ["claude-opus-5", "gpt-5.6-sol", "gemini-3.6-flash"]
    combined = MODULE.judge_protocol_fingerprint("v2", panel, 30000, 2048)
    for member in panel:
        assert combined != MODULE.judge_protocol_fingerprint(
            "v2", member, 30000, 2048
        )
    # a single-member panel keeps the historical single-judge fingerprint so
    # prior single-judge runs stay resumable
    assert MODULE.judge_protocol_fingerprint(
        "v2", ["claude-sonnet-4.6"], 30000, 2048
    ) == MODULE.judge_protocol_fingerprint("v2", "claude-sonnet-4.6", 30000, 2048)


def test_switching_to_a_panel_invalidates_single_judge_results():
    # The resume path keys off this fingerprint, so a protocol change must
    # force a full re-judge rather than reusing single-judge verdicts.
    single = MODULE.judge_protocol_fingerprint("v2", "claude-sonnet-4.6", 30000, 2048)
    panel = MODULE.judge_protocol_fingerprint(
        "v2", ["claude-opus-5", "gpt-5.6-sol", "gemini-3.6-flash"], 30000, 2048
    )
    assert single != panel


def test_duplicate_panel_member_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--tasks", str(tmp_path / "t.jsonl"),
            "--repo-cache", str(tmp_path),
            "--generation", "m=" + str(tmp_path / "g.jsonl"),
            "--out", str(tmp_path / "o.jsonl"),
            "--raw-dir", str(tmp_path / "raw"),
            "--judge-model", "claude-opus-5",
            "--judge-model", "claude-opus-5",
        ],
    )
    with pytest.raises(ValueError, match="duplicate judge"):
        MODULE.parse_args()


# ---------------------------------------------------------------------------
# Truncation-aware output budget escalation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "finish_reason", "expected"),
    [
        ("", "stop", True),
        ("   \n ", "stop", True),
        ('{"addressed":"yes"}', "length", True),
        ('{"addressed":"yes"}', "stop", False),
    ],
)
def test_truncation_detection(text, finish_reason, expected):
    assert MODULE.response_was_truncated(text, finish_reason) is expected


def judge_args(tmp_path, **overrides):
    values = {
        "chat_endpoint": "http://127.0.0.1:8787/v1/chat/completions",
        "rubric": "v2",
        "max_output_tokens": 4096,
        "max_output_token_ceiling": 16384,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_truncated_judge_is_retried_at_a_doubled_budget(tmp_path, monkeypatch):
    # A reasoning judge that exhausts its budget must not be recorded as a
    # failure: that would drop the hardest patches, not the wrong ones.
    budgets = []

    def fake_invoke(model, prompt, endpoint, maximum_tokens, rubric):
        budgets.append(maximum_tokens)
        if maximum_tokens < 8192:
            return "", {"completion_tokens": maximum_tokens}, "length"
        return (
            '{"addressed":"yes","quality_1_to_5":5,'
            '"introduces_new_debt":false,"reason":"ok"}',
            {"completion_tokens": 900},
            "stop",
        )

    monkeypatch.setattr(MODULE, "invoke_judge", fake_invoke)
    result = MODULE.judge_with_one_model(
        "claude-opus-5",
        "prompt",
        tmp_path / "raw.txt",
        judge_args(tmp_path),
    )

    assert budgets == [4096, 8192]
    assert result["status"] == "judged"
    assert result["maintainability_pass"] is True
    assert result["output_token_budget"] == 8192
    assert result["output_token_budgets"] == [4096, 8192]


def test_budget_escalation_respects_the_ceiling(tmp_path, monkeypatch):
    budgets = []

    def always_truncated(model, prompt, endpoint, maximum_tokens, rubric):
        budgets.append(maximum_tokens)
        return "", {}, "length"

    monkeypatch.setattr(MODULE, "invoke_judge", always_truncated)
    result = MODULE.judge_with_one_model(
        "claude-opus-5",
        "prompt",
        tmp_path / "raw.txt",
        judge_args(tmp_path, max_output_tokens=4096, max_output_token_ceiling=8192),
    )

    assert budgets == [4096, 8192, 8192]
    assert result["status"] == "judge_error"
    assert result["maintainability_pass"] is False


def test_malformed_but_complete_output_is_repaired_not_escalated(
    tmp_path, monkeypatch
):
    budgets = []
    prompts = []

    def fake_invoke(model, prompt, endpoint, maximum_tokens, rubric):
        budgets.append(maximum_tokens)
        prompts.append(prompt)
        if len(budgets) == 1:
            return "not json at all", {}, "stop"
        return (
            '{"addressed":"no","quality_1_to_5":2,'
            '"introduces_new_debt":true,"reason":"x"}',
            {},
            "stop",
        )

    monkeypatch.setattr(MODULE, "invoke_judge", fake_invoke)
    result = MODULE.judge_with_one_model(
        "gpt-5.6-sol", "prompt", tmp_path / "raw.txt", judge_args(tmp_path)
    )

    # budget unchanged, and the second call carries a repair prompt
    assert budgets == [4096, 4096]
    assert prompts[1] != prompts[0]
    assert result["status"] == "judged"
    assert result["maintainability_pass"] is False


def test_escalation_ceiling_is_part_of_the_protocol_fingerprint():
    panel = ["claude-opus-5", "gpt-5.6-sol", "gemini-3.6-flash"]
    a = MODULE.judge_protocol_fingerprint("v2", panel, 30000, 4096, 16384)
    b = MODULE.judge_protocol_fingerprint("v2", panel, 30000, 4096, 8192)
    assert a != b
