import unittest

from reviewbench_maintainability.evidence import (
    before_window,
    focus_patch,
    two_dot_file_diff,
)


class EvidenceTests(unittest.TestCase):
    def test_extracts_numbered_before_window(self) -> None:
        text = "\n".join(f"line {number}" for number in range(1, 11))

        window = before_window(text, anchor=5, context=2)

        self.assertEqual(window.splitlines()[0], "3: line 3")
        self.assertEqual(window.splitlines()[-1], "7: line 7")

    def test_focuses_reference_patch_near_anchor(self) -> None:
        patch = (
            "@@ -10,2 +10,2 @@\n-old\n+new\n"
            "@@ -100,2 +100,2 @@\n-unrelated\n+other\n"
        )

        focused = focus_patch(patch, anchor=10, window=5)

        self.assertTrue(focused["focused"])
        self.assertIn("+new", focused["text"])
        self.assertNotIn("+other", focused["text"])
        self.assertFalse(focused["complete"])
        self.assertEqual(focused["dropped_hunks"], 1)

    def test_two_dot_diff_omits_file_headers(self) -> None:
        patch = two_dot_file_diff("old\n", "new\n")

        self.assertIn("-old", patch)
        self.assertIn("+new", patch)
        self.assertNotIn("--- ", patch)


if __name__ == "__main__":
    unittest.main()
