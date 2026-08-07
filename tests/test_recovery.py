import unittest

from reviewbench_maintainability.recovery import recovery_status


class RecoveryTests(unittest.TestCase):
    def test_environment_failure_is_queued(self) -> None:
        status, _ = recovery_status(
            {"setup_error": "env: pip-install-fail: missing numpy"}
        )

        self.assertEqual(status, "environment_recovery")

    def test_uncovered_task_is_queued(self) -> None:
        status, _ = recovery_status(
            {
                "golden_correctness_ok": True,
                "pass_to_pass": 10,
                "changed_lines_covered": False,
            }
        )

        self.assertEqual(status, "coverage_recovery")

    def test_complete_task_is_not_queued(self) -> None:
        status, _ = recovery_status(
            {
                "golden_correctness_ok": True,
                "pass_to_pass": 10,
                "changed_lines_covered": True,
            }
        )

        self.assertEqual(status, "strict_pass")


if __name__ == "__main__":
    unittest.main()
