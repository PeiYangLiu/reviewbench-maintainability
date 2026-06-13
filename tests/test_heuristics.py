import unittest

from reviewbench_maintainability.heuristics import classify_comment, normalize_instruction


class HeuristicTests(unittest.TestCase):
    def test_classifies_complexity_and_strips_suggestion(self) -> None:
        body = """Could we split this function into smaller helpers?

```suggestion
def exact_answer():
    pass
```
"""
        classifications = classify_comment(body, "alice")
        self.assertTrue(classifications)
        self.assertEqual(classifications[0].category, "complexity")
        self.assertTrue(classifications[0].contains_suggestion)
        self.assertNotIn("exact_answer", classifications[0].sanitized_body)

    def test_ignores_bot_comments(self) -> None:
        self.assertEqual(classify_comment("Please remove unused code", "renovate[bot]"), [])
        self.assertEqual(classify_comment("Please remove unused code", "Copilot"), [])

    def test_normalizes_instruction(self) -> None:
        self.assertEqual(normalize_instruction("Please   simplify\nthis."), "Please simplify this.")


if __name__ == "__main__":
    unittest.main()
