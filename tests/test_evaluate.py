import subprocess
import tempfile
from pathlib import Path
import unittest

from reviewbench_maintainability.evaluate import (
    EvaluationOptions,
    evaluate_task,
    oracle_for_task,
)


BEFORE = """
def handle(items):
    total = 0
    for item in items:
        if item > 0:
            if item % 2 == 0:
                total += item
            else:
                total += item * 2
    return total
"""

AFTER = """
def score(item):
    if item <= 0:
        return 0
    if item % 2 == 0:
        return item
    return item * 2


def handle(items):
    return sum(score(item) for item in items)
"""


class EvaluateTests(unittest.TestCase):
    def test_evaluates_correct_patch_with_static_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            patch = root / "solution.patch"
            start_sha = create_repo_and_patch(repo, patch)

            task = task_payload(start_sha)
            oracle = oracle_for_task(
                task,
                {
                    "tasks": {
                        task["id"]: {
                            "test_commands": [
                                {
                                    "name": "behavior",
                                    "command": "python -c \"import sample; assert sample.handle([1, 2, -3]) == 4\"",
                                }
                            ],
                            "allowed_files": ["sample.py"],
                            "max_changed_files": 1,
                            "max_changed_lines": 40,
                        }
                    }
                },
            )

            result = evaluate_task(
                task,
                patch,
                oracle,
                EvaluationOptions(work_dir=root / "work", source_repo=repo),
            )

            self.assertTrue(result["passed"])
            self.assertEqual(result["score"], 1.0)
            self.assertTrue(result["gates"]["correctness"]["passed"])
            self.assertTrue(result["gates"]["scope"]["passed"])
            self.assertTrue(result["gates"]["maintainability"]["passed"])

    def test_missing_correctness_command_blocks_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            patch = root / "solution.patch"
            start_sha = create_repo_and_patch(repo, patch)

            task = task_payload(start_sha)
            result = evaluate_task(
                task,
                patch,
                oracle_for_task(task, {}),
                EvaluationOptions(work_dir=root / "work", source_repo=repo),
            )

            self.assertFalse(result["passed"])
            self.assertEqual(result["failure_reason"], "correctness:missing_correctness_commands")
            self.assertEqual(result["gates"]["maintainability"]["status"], "skipped_correctness_failed")

    def test_scope_rejects_disallowed_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            patch = root / "solution.patch"
            start_sha = create_repo_and_patch(repo, patch, extra_file=True)

            task = task_payload(start_sha)
            oracle = oracle_for_task(
                task,
                {
                    "tasks": {
                        task["id"]: {
                            "test_commands": [
                                "python -c \"import sample; assert sample.handle([1, 2, -3]) == 4\""
                            ],
                            "allowed_files": ["sample.py"],
                        }
                    }
                },
            )

            result = evaluate_task(
                task,
                patch,
                oracle,
                EvaluationOptions(work_dir=root / "work", source_repo=repo),
            )

            self.assertFalse(result["passed"])
            self.assertEqual(result["failure_reason"], "scope:scope_failed")
            self.assertFalse(result["gates"]["scope"]["checks"]["allowed_files"]["passed"])


def create_repo_and_patch(repo: Path, patch: Path, extra_file: bool = False) -> str:
    repo.mkdir()
    run(["git", "init", "--quiet"], repo)
    run(["git", "config", "user.email", "reviewbench@example.com"], repo)
    run(["git", "config", "user.name", "ReviewBench"], repo)
    (repo / "sample.py").write_text(BEFORE, encoding="utf-8")
    run(["git", "add", "sample.py"], repo)
    run(["git", "commit", "--quiet", "-m", "initial"], repo)
    start_sha = run(["git", "rev-parse", "HEAD"], repo).strip()

    (repo / "sample.py").write_text(AFTER, encoding="utf-8")
    if extra_file:
        (repo / "unrelated.py").write_text("VALUE = 1\n", encoding="utf-8")
        run(["git", "add", "--intent-to-add", "unrelated.py"], repo)
    patch.write_text(run(["git", "diff"], repo), encoding="utf-8")
    run(["git", "reset", "--hard", "--quiet", "HEAD"], repo)
    return start_sha


def task_payload(start_sha: str) -> dict[str, object]:
    return {
        "id": "github__owner__repo__pull_1__comment_2__complexity",
        "repo": "owner/repo",
        "category": "complexity",
        "gate_type": "auto_static",
        "start_sha": start_sha,
        "locality": {"path": "sample.py", "line": 2},
    }


def run(command: list[str], cwd: Path) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout


if __name__ == "__main__":
    unittest.main()
