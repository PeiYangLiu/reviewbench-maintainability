import hashlib
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import composer_candidate, composer_test_tools
from agentic_runtime_migration import composer_test_tools_lane, composer_test_tools_runtime, scripted_lane


def test_scripted_entry_dispatches_the_exact_kirby_task(monkeypatch):
    calls = []
    expected = object()
    monkeypatch.setattr(composer_test_tools_lane, "migrate",
                        lambda ctx, task, oracle: (calls.append((ctx, task, oracle)) or expected))
    ctx, task, oracle = object(), {"id": composer_test_tools.TASK_ID}, object()
    assert scripted_lane.migrate(ctx, task, oracle) is expected
    assert calls == [(ctx, task, oracle)]


def test_kirby_runtime_stays_separate_from_the_existing_privatebin_asset():
    kirby = composer_test_tools_runtime.php_runtime_source()
    privatebin = composer_candidate.php_runtime_source()
    assert "KT_SCHEMA" in kirby and "CC_SCHEMA" not in kirby
    assert "CC_SCHEMA" in privatebin and "KT_SCHEMA" not in privatebin
    assert hashlib.sha256(kirby.encode()).hexdigest() == (
        "11d1ea11bba925ce14f7f5b950429e78258e6a0fcd710445e01d4aae8344a4be")
    assert hashlib.sha256(privatebin.encode()).hexdigest() == (
        "63d731cc020976615c7695bf3390ae2dcc4834df42b1af01cdb516a93a1d07e9")
