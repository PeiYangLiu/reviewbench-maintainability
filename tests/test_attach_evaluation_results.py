import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "attach_evaluation_results.py"
SPEC = importlib.util.spec_from_file_location("attach_evaluation_results", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_attach_results_replaces_baseline_model_results():
    items = [{"task": {"id": "task", "model_results": {"model:old": {}}}}]
    rows = [
        {
            "id": "task",
            "model": "gpt-5.5",
            "resolved": True,
            "correctness_ok": True,
            "scope_ok": True,
            "maintainability_pass": True,
            "generation_apply": True,
            "generation_protocol": "agentic-line-edit/v1",
            "changed_lines": 8,
            "failure_reasons": [],
            "judge_verdict": {
                "addressed": "yes",
                "quality_1_to_5": 5,
                "reason": "Resolved cleanly.",
            },
        }
    ]

    attached = MODULE.attach_results(items, rows)

    task = attached[0]["task"]
    assert set(task["model_results"]) == {"model:gpt-5.5"}
    assert task["model_results"]["model:gpt-5.5"]["resolved"] is True
    assert task["evaluation_protocols"] == ["agentic-line-edit/v1"]


def test_attach_results_rejects_task_mismatch():
    with pytest.raises(ValueError, match="mismatch"):
        MODULE.attach_results(
            [{"task": {"id": "task-a"}}],
            [{"id": "task-b", "model": "gpt-5.5"}],
        )
