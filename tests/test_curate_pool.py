import unittest

from reviewbench_maintainability.curate_pool import (
    cap_per_repo,
    exclusion_reasons,
    ranking_key,
    substantive_hint_score,
)


def candidate(**overrides):
    value = {
        "id": "task-1",
        "repo": "owner/repo",
        "inferred_language": "Python",
        "contains_suggestion": False,
        "start_sha": "start",
        "target_sha": "target",
        "locality": {"path": "src/parser.py", "line": 20},
        "_prefilter": {"bucket": "high_priority"},
        "correctness_oracle_proxy": {"test_change_paths": ["tests/test_parser.py"]},
        "difficulty": {"tier": "hard", "score": 5},
    }
    value.update(overrides)
    return value


class CuratePoolTests(unittest.TestCase):
    def test_keeps_runnable_hard_source_task(self) -> None:
        reasons = exclusion_reasons(
            candidate(),
            tiers={"medium", "hard"},
            languages={"Go", "Python"},
            buckets={"high_priority"},
            allow_unscored=False,
        )

        self.assertEqual(reasons, [])

    def test_excludes_test_anchor(self) -> None:
        value = candidate(locality={"path": "tests/test_parser.py", "line": 20})

        reasons = exclusion_reasons(
            value,
            tiers={"medium", "hard"},
            languages={"Go", "Python"},
            buckets={"high_priority"},
            allow_unscored=False,
        )

        self.assertIn("test_anchor", reasons)

    def test_allows_unscored_task_for_intrinsic_judging(self) -> None:
        value = candidate()
        value.pop("difficulty")

        reasons = exclusion_reasons(
            value,
            tiers={"medium", "hard"},
            languages={"Go", "Python"},
            buckets={"high_priority"},
            allow_unscored=True,
        )

        self.assertEqual(reasons, [])

    def test_hard_annotated_task_ranks_first(self) -> None:
        annotated = candidate(
            human_annotation={
                "label_is_maintainability_comment": "yes",
                "label_keep_for_benchmark": "yes",
                "label_quality_score_1_to_5": "4",
            }
        )
        medium = candidate(
            id="task-2",
            difficulty={"tier": "medium", "score": 3},
        )

        self.assertLess(ranking_key(annotated), ranking_key(medium))

    def test_substantive_refactor_ranks_above_rename_nit(self) -> None:
        refactor = candidate(
            task_instruction="Extract the duplicated parsing branches into a helper.",
            category="duplication",
        )
        rename = candidate(
            task_instruction="Nit: rename this local variable.",
            category="naming",
        )

        self.assertGreater(
            substantive_hint_score(refactor),
            substantive_hint_score(rename),
        )

    def test_caps_tasks_per_repository(self) -> None:
        records = [
            candidate(id="a-1"),
            candidate(id="a-2"),
            candidate(id="b-1", repo="owner/other"),
        ]

        capped = cap_per_repo(records, maximum=1)

        self.assertEqual([item["id"] for item in capped], ["a-1", "b-1"])


if __name__ == "__main__":
    unittest.main()
