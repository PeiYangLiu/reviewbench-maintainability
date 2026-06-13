from argparse import Namespace
from pathlib import Path
import unittest

from reviewbench_maintainability.build_dataset import build_dataset, patch_touches_line
from reviewbench_maintainability.github_client import PullRequestMaterials
from reviewbench_maintainability.models import PullRequestRef


class BuildDatasetTests(unittest.TestCase):
    def test_builds_candidate_from_resolved_region_touched_comment(self) -> None:
        materials = PullRequestMaterials(
            ref=PullRequestRef("owner", "repo", 7),
            pull={
                "merged_at": "2026-01-01T00:00:00Z",
                "html_url": "https://github.com/owner/repo/pull/7",
                "head": {"sha": "head-sha"},
                "base": {"sha": "base-sha"},
                "merge_commit_sha": "merge-sha",
            },
            review_comments=[
                {
                    "id": 42,
                    "body": "Could we split this function into smaller helpers?",
                    "user": {"login": "reviewer"},
                    "path": "src/foo.py",
                    "line": 12,
                    "original_line": 12,
                    "diff_hunk": "@@ -10,8 +10,4 @@",
                    "original_commit_id": "commented-sha",
                    "commit_id": "commented-sha",
                    "html_url": "https://github.com/owner/repo/pull/7#discussion_r42",
                    "created_at": "2026-01-01T00:01:00Z",
                    "updated_at": "2026-01-01T00:02:00Z",
                }
            ],
            issue_comments=[],
            files=[
                {
                    "filename": "src/foo.py",
                    "patch": "@@ -10,8 +10,4 @@\n-def bad():\n-    if x:\n-        if y:\n+def better():\n+    return helper()\n",
                }
            ],
            commits=[],
            review_threads=[
                {
                    "isResolved": True,
                    "comments": {"nodes": [{"databaseId": 42}]},
                }
            ],
        )
        args = Namespace(
            addressed_policy="resolved-or-region-touched",
            include_replies=False,
            include_golden_patch=True,
            from_snapshot=Path("snapshots"),
            gh="gh",
        )

        candidates, report = build_dataset(args, [materials])

        self.assertEqual(report["candidates_kept"], 1)
        self.assertEqual(candidates[0].category, "complexity")
        self.assertEqual(candidates[0].gate_type, "auto_static")
        self.assertTrue(candidates[0].addressed_evidence["region_touched_after_comment"])
        self.assertIsNotNone(candidates[0].golden_patch)

    def test_patch_touches_old_line_range(self) -> None:
        patch = "@@ -20,5 +20,3 @@\n-old\n+new\n"
        self.assertTrue(patch_touches_line(patch, 22))
        self.assertFalse(patch_touches_line(patch, 30))


if __name__ == "__main__":
    unittest.main()
