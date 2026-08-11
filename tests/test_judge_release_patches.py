import importlib.util
import json
from pathlib import Path
import sys

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
