import tempfile
from pathlib import Path
import unittest

from reviewbench_maintainability.static_metrics import compare_paths, compute_file_metrics


BEFORE = """
def handle(items):
    total = 0
    for item in items:
        if item > 0:
            if item % 2 == 0:
                total += item
            else:
                total += item * 2
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


class StaticMetricTests(unittest.TestCase):
    def test_python_metrics_detect_complexity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.py"
            path.write_text(BEFORE, encoding="utf-8")
            metrics = compute_file_metrics(path)
            self.assertTrue(metrics.exists)
            self.assertGreaterEqual(metrics.python_max_cyclomatic_complexity or 0, 5)

    def test_compare_paths_reports_positive_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before_dir = root / "before"
            after_dir = root / "after"
            before_dir.mkdir()
            after_dir.mkdir()
            (before_dir / "sample.py").write_text(BEFORE, encoding="utf-8")
            (after_dir / "sample.py").write_text(AFTER, encoding="utf-8")
            result = compare_paths(before_dir, after_dir, ["sample.py"], "complexity")
            self.assertTrue(result["summary"]["has_any_positive_static_evidence"])


if __name__ == "__main__":
    unittest.main()
