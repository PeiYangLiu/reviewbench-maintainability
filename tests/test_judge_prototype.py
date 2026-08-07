from pathlib import Path
import sys
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import judge_prototype  # noqa: E402


class JudgePrototypeTests(unittest.TestCase):
    def test_parses_strict_quality_verdict(self) -> None:
        result = judge_prototype.parse_verdict(
            '{"addressed":"yes","quality_1_5":4,"reason":"Substantive refactor."}'
        )

        self.assertEqual(result["addressed"], "yes")
        self.assertEqual(result["quality_1_5"], 4)

    def test_rejects_out_of_range_quality(self) -> None:
        result = judge_prototype.parse_verdict(
            '{"addressed":"yes","quality_1_5":7,"reason":"Invalid score."}'
        )

        self.assertEqual(result["quality_1_5"], 0)


if __name__ == "__main__":
    unittest.main()
