import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration.scripted_lane import completion_pattern
from reviewbench_maintainability.agentic.contracts import ContractError


def frozen(tmp_path, start="OK (1 test, 8 assertions)", golden="OK (1 test, 9 assertions)"):
    root = tmp_path / "scripted"
    root.mkdir()
    runs = {}
    for variant, text in (("start", start), ("golden", golden)):
        (root / (variant + ".log")).write_text(text + "\n")
        runs[variant] = [{"exit_code": 0, "log": variant + ".log"} for _ in range(3)]
    return SimpleNamespace(legacy=tmp_path), {"runs": runs}


def test_original_test_count_contract_allows_variant_specific_mock_assertion_counts(tmp_path):
    ctx, oracle = frozen(tmp_path)
    pattern = completion_pattern(ctx, oracle, {"outcome_regex": r"OK \((\d+) tests?,"})
    assert re.search(pattern, "OK (1 test, 8 assertions)\n")
    assert re.search(pattern, "OK (1 test, 9 assertions)\n")
    assert not re.search(pattern, "OK (0 tests, 9 assertions)\n")
    assert not re.search(pattern, "OK (1 test,")
    assert not re.search(pattern, "OK (1 test, 8 assertions) FAILURES!")


@pytest.mark.parametrize("golden", ["OK (2 tests, 9 assertions)", "OK (1 test,",
                                    "OK (1 test, ignored)", "OK (1 test, 9 assertions) extra"])
def test_frozen_count_changes_and_incomplete_summaries_are_rejected(tmp_path, golden):
    ctx, oracle = frozen(tmp_path, golden=golden)
    with pytest.raises(ContractError):
        completion_pattern(ctx, oracle, {"outcome_regex": r"OK \((\d+) tests?,"})


def test_non_prefix_contract_preserves_the_already_frozen_contao_pattern(tmp_path):
    line = "Tests: 1, Assertions: 3, PHPUnit Deprecations: 1."
    ctx, oracle = frozen(tmp_path, line, line)
    result = completion_pattern(ctx, oracle, {"outcome_regex": r"Tests:\s+(\d+), Assertions:\s+(\d+)"})
    assert result == "^" + re.escape(line) + r"\s*$"
