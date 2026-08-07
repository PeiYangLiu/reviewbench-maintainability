from pathlib import Path
import sys
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import validate_oracles  # noqa: E402


class ValidateOraclesTests(unittest.TestCase):
    def test_schema_requires_stable_covered_oracle(self) -> None:
        self.assertIn("stable-covered", validate_oracles.ORACLE_SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
