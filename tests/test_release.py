import unittest

from reviewbench_maintainability.release import (
    coverage_result,
    judged_difficulty,
    merge_evaluation_records,
    pass_to_pass_count,
    release_failure_reasons,
    select_diverse,
    task_fingerprint,
)


def task(task_id: str, repo: str, difficulty: str = "hard"):
    verdict = {
        "intrinsic_difficulty": difficulty,
        "quality_score_1_to_5": 4,
    }
    return {
        "id": task_id,
        "repo": repo,
        "quality_gate": {"passed": True, "version": "v1"},
        "quality_judgments": [
            {"model": "a", "verdict": dict(verdict)},
            {"model": "b", "verdict": dict(verdict)},
        ],
    }


class ReleaseTests(unittest.TestCase):
    def test_requires_quality_and_golden_correctness(self) -> None:
        value = task("task-1", "owner/repo")
        evaluation = {
            "pass_to_pass": 3,
            "changed_lines_covered": True,
            "evals": [{"mode": "golden", "correctness_ok": True}],
        }

        self.assertEqual(release_failure_reasons(value, evaluation, 1), [])

        evaluation["evals"][0]["correctness_ok"] = False
        self.assertIn(
            "golden_correctness_failed",
            release_failure_reasons(value, evaluation, 1),
        )

    def test_disagreement_downgrades_hard_to_medium(self) -> None:
        value = task("task-1", "owner/repo")
        value["quality_judgments"][1]["verdict"]["intrinsic_difficulty"] = "medium"

        self.assertEqual(judged_difficulty(value), "medium")

    def test_selection_caps_repository_dominance(self) -> None:
        records = [
            task("a-1", "owner/a"),
            task("a-2", "owner/a"),
            task("b-1", "owner/b"),
            task("c-1", "owner/c"),
        ]

        selected = select_diverse(records, target=3, max_per_repo=1)

        self.assertEqual(len(selected), 3)
        self.assertEqual(len({item["repo"] for item in selected}), 3)

    def test_selection_deduplicates_identical_local_fixes(self) -> None:
        first = task("a-1", "owner/a")
        second = task("a-2", "owner/a")
        for item in (first, second):
            item["pr_number"] = 1
            item["locality"] = {"path": "src/a.py"}
            item["quality_evidence"] = {
                "patch": "@@ -10,2 +10,2 @@\n-old\n+new"
            }

        selected = select_diverse([first, second], target=2, max_per_repo=2)

        self.assertEqual(len(selected), 1)
        self.assertEqual(task_fingerprint(first), task_fingerprint(second))

    def test_requires_changed_line_coverage(self) -> None:
        value = task("task-1", "owner/repo")
        evaluation = {
            "pass_to_pass": 3,
            "changed_lines_covered": False,
            "evals": [{"mode": "golden", "correctness_ok": True}],
        }

        reasons = release_failure_reasons(value, evaluation, 1)

        self.assertIn("changed_lines_not_covered", reasons)

    def test_merges_coverage_from_funnel_record(self) -> None:
        leaderboard = {
            "id": "task-1",
            "pass_to_pass": 3,
            "evals": [{"mode": "golden", "correctness_ok": True}],
        }
        funnel = {
            "id": "task-1",
            "changed_lines_covered": True,
            "coverage": {"covered": True},
        }

        merged = merge_evaluation_records(leaderboard, funnel)

        self.assertTrue(coverage_result(merged))

    def test_later_equally_rich_record_wins(self) -> None:
        old = {"id": "task-1", "setup_error": "old failure"}
        new = {"id": "task-1", "golden_correctness_ok": True}

        merged = merge_evaluation_records(old, new)

        self.assertTrue(merged["golden_correctness_ok"])

    def test_successful_recovery_clears_stale_setup_error(self) -> None:
        old = {"id": "task-1", "setup_error": "env: failed"}
        recovered = {
            "id": "task-1",
            "golden_correctness_ok": True,
            "pass_to_pass": 2,
            "changed_lines_covered": True,
        }

        merged = merge_evaluation_records(old, recovered)

        self.assertIsNone(merged["setup_error"])

    def test_reads_structured_pass_to_pass_count(self) -> None:
        self.assertEqual(
            pass_to_pass_count({"pass_to_pass": {"count": 27, "tests": ["a"]}}),
            27,
        )


if __name__ == "__main__":
    unittest.main()
