import json
from pathlib import Path
import tempfile
import unittest

from reviewbench_maintainability.quality_gate import (
    QualityVerdict,
    aggregate_verdicts,
    build_prompt,
    evidence_is_complete,
    load_external_judgments,
    parse_verdict,
    verdict_passes,
)


PASSING = {
    "decision": "pass",
    "primary_kind": "maintainability",
    "evidence_sufficient": True,
    "explicit_code_smell": True,
    "golden_materially_improves": True,
    "improvement_is_substantive": True,
    "reference_fix_is_attributable": True,
    "primary_goal_is_behavior_change": False,
    "not_style_only": True,
    "no_obvious_quality_regression": True,
    "quality_score_1_to_5": 4,
    "intrinsic_difficulty": "medium",
    "confidence": 0.9,
    "code_smell": "Duplicated parsing branches are difficult to keep consistent.",
    "improvement": "The patch extracts one shared parser.",
    "difficulty_reason": "The refactor requires updating several parsing branches.",
    "reason": "The focused refactor removes concrete duplication without changing behavior.",
}


class QualityGateTests(unittest.TestCase):
    def test_strict_verdict_passes(self) -> None:
        verdict = parse_verdict(json.dumps(PASSING))

        self.assertTrue(verdict_passes(verdict))

    def test_functional_fix_does_not_pass(self) -> None:
        value = dict(PASSING)
        value.update(
            {
                "primary_kind": "functional_bug",
                "primary_goal_is_behavior_change": True,
            }
        )

        self.assertFalse(verdict_passes(parse_verdict(json.dumps(value))))

    def test_style_only_change_does_not_pass(self) -> None:
        value = dict(PASSING)
        value.update(
            {
                "decision": "reject",
                "primary_kind": "style",
                "improvement_is_substantive": False,
                "not_style_only": False,
                "quality_score_1_to_5": 2,
                "intrinsic_difficulty": "easy",
            }
        )

        self.assertFalse(verdict_passes(parse_verdict(json.dumps(value))))

    def test_mechanical_easy_task_does_not_pass(self) -> None:
        value = dict(PASSING)
        value["intrinsic_difficulty"] = "easy"
        value["difficulty_reason"] = "The task is a direct one-line rename."

        self.assertFalse(verdict_passes(parse_verdict(json.dumps(value))))

    def test_partial_or_low_confidence_does_not_pass(self) -> None:
        value = dict(PASSING)
        value["confidence"] = 0.79

        self.assertFalse(verdict_passes(parse_verdict(json.dumps(value))))

    def test_two_judges_must_unanimously_pass(self) -> None:
        passing = QualityVerdict(**PASSING).to_dict()
        rejected = dict(PASSING)
        rejected["decision"] = "reject"
        judgments = [
            {"model": "judge-a", "verdict": passing},
            {"model": "judge-b", "verdict": rejected},
        ]

        result = aggregate_verdicts(judgments)

        self.assertFalse(result["passed"])
        self.assertEqual(result["status"], "rejected")

    def test_judge_error_prevents_pass(self) -> None:
        judgments = [
            {"model": "judge-a", "verdict": dict(PASSING)},
            {"model": "judge-b", "verdict": dict(PASSING)},
            {"model": "judge-c", "verdict": None},
        ]

        result = aggregate_verdicts(judgments)

        self.assertFalse(result["passed"])
        self.assertEqual(result["status"], "judge_error")

    def test_duplicate_model_does_not_satisfy_two_judges(self) -> None:
        judgments = [
            {"model": "same-model", "verdict": dict(PASSING)},
            {"model": "same-model", "verdict": dict(PASSING)},
        ]

        result = aggregate_verdicts(judgments)

        self.assertFalse(result["passed"])
        self.assertEqual(result["status"], "insufficient_judgments")

    def test_loads_external_judgments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "judgments.jsonl"
            path.write_text(
                json.dumps(
                    {"id": "task-1", "model": "judge-a", "verdict": PASSING}
                )
                + "\n",
                encoding="utf-8",
            )

            loaded = load_external_judgments([path])

        self.assertEqual(loaded["task-1"][0]["model"], "judge-a")

    def test_incomplete_focused_patch_is_not_sufficient_evidence(self) -> None:
        record = {
            "quality_evidence": {
                "status": "ready",
                "before": "old",
                "patch": "diff",
                "patch_complete": False,
                "patch_truncated": False,
            }
        }

        self.assertFalse(evidence_is_complete(record))

    def test_prompt_contains_before_and_reference_patch(self) -> None:
        prompt = build_prompt(
            {
                "id": "task-1",
                "repo": "owner/repo",
                "category": "duplication",
                "difficulty": {"tier": "hard", "score": 5},
                "locality": {"path": "src/a.py", "line": 10},
                "task_instruction": "Extract the repeated parser.",
            },
            "parse_a()\\nparse_a()",
            "-parse_a()\\n+parse_common()",
        )

        self.assertIn("Extract the repeated parser.", prompt)
        self.assertIn("parse_a()", prompt)
        self.assertIn("parse_common()", prompt)
        self.assertIn("COMPLETE SAME-FILE REFERENCE DIFF", prompt)


if __name__ == "__main__":
    unittest.main()


class ParseVerdictRobustnessTests(unittest.TestCase):
    def test_reports_a_truncated_response_distinctly(self):
        truncated = '```json\n{\n  "decision": "pass",\n  "primary_kind": "maintain'

        with self.assertRaisesRegex(ValueError, "truncated"):
            parse_verdict(truncated)

    def test_still_rejects_a_response_with_no_json(self):
        with self.assertRaisesRegex(ValueError, "did not return a JSON object"):
            parse_verdict("I cannot judge this patch.")

    def test_accepts_a_fenced_json_object(self):
        payload = dict(PASSING)
        payload["intrinsic_difficulty"] = "hard"
        fenced = "```json\n" + json.dumps(payload) + "\n```"

        verdict = parse_verdict(fenced)

        self.assertEqual(verdict.decision, "pass")
        self.assertEqual(verdict.intrinsic_difficulty, "hard")
