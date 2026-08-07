from argparse import Namespace
import json
from pathlib import Path
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import mine_scale  # noqa: E402


class MineScaleTests(unittest.TestCase):
    def test_resume_manifest_restores_original_batch_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "manifest.json").write_text(
                json.dumps(
                    {
                        "target_candidates": 200_000,
                        "limit_per_batch": 250,
                        "workers": 1,
                        "orgs": ["*"],
                        "queries": ["is:pr is:merged refactor language:go"],
                    }
                ),
                encoding="utf-8",
            )
            args = Namespace(
                out_dir=root,
                target_candidates=10,
                limit_per_batch=1,
                workers=8,
            )

            orgs, queries = mine_scale.apply_resume_manifest(args)

        self.assertEqual(orgs, ("*",))
        self.assertEqual(queries, ("is:pr is:merged refactor language:go",))
        self.assertEqual(args.target_candidates, 200_000)
        self.assertEqual(args.limit_per_batch, 250)
        self.assertEqual(args.workers, 1)

    def test_query_filter_preserves_original_batch_indexes(self) -> None:
        batches = [
            {"index": 11, "query": "refactor language:go"},
            {"index": 12, "query": "rename language:python"},
            {"index": 13, "query": "extract language:python"},
        ]

        selected = mine_scale.filter_batches(
            batches,
            r"(?:refactor|extract) language:(?:go|python)",
        )

        self.assertEqual([item["index"] for item in selected], [11, 13])


if __name__ == "__main__":
    unittest.main()


class ResumeWorkerOverrideTests(unittest.TestCase):
    def test_explicit_workers_survive_a_manifest_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            out_dir = Path(directory)
            (out_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "orgs": ["*"],
                        "queries": ["is:pr is:merged refactor"],
                        "target_candidates": 10,
                        "limit_per_batch": 5,
                        "workers": 1,
                    }
                ),
                encoding="utf-8",
            )
            args = Namespace(
                out_dir=out_dir, workers=4, workers_explicit=True
            )

            mine_scale.apply_resume_manifest(args)

            self.assertEqual(args.workers, 4)

    def test_manifest_workers_apply_when_not_given_on_the_command_line(self):
        with tempfile.TemporaryDirectory() as directory:
            out_dir = Path(directory)
            (out_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "orgs": ["*"],
                        "queries": ["is:pr is:merged refactor"],
                        "target_candidates": 10,
                        "limit_per_batch": 5,
                        "workers": 3,
                    }
                ),
                encoding="utf-8",
            )
            args = Namespace(
                out_dir=out_dir, workers=1, workers_explicit=False
            )

            mine_scale.apply_resume_manifest(args)

            self.assertEqual(args.workers, 3)

    def test_explicit_limit_per_batch_survives_a_manifest_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            out_dir = Path(directory)
            (out_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "orgs": ["*"],
                        "queries": ["is:pr is:merged refactor"],
                        "target_candidates": 10,
                        "limit_per_batch": 250,
                        "workers": 1,
                    }
                ),
                encoding="utf-8",
            )
            args = Namespace(
                out_dir=out_dir,
                workers=1,
                workers_explicit=False,
                limit_per_batch=80,
                limit_per_batch_explicit=True,
            )

            mine_scale.apply_resume_manifest(args)

            self.assertEqual(args.limit_per_batch, 80)
