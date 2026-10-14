from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import ruby_candidate, ruby_candidate_lane, scripted_lane
from reviewbench_maintainability.agentic.contracts import ContractError


@pytest.mark.parametrize("ident", sorted(ruby_candidate.TARGETS))
def test_original_scripted_entry_dispatches_only_the_two_bound_ruby_tasks(monkeypatch, ident):
    observed = []
    expected = object()
    monkeypatch.setattr(ruby_candidate_lane, "migrate",
                        lambda ctx, task, oracle: (observed.append((ctx, task, oracle)) or expected))
    ctx, task, oracle = object(), {"id": ident}, object()
    assert scripted_lane.migrate(ctx, task, oracle) is expected
    assert observed == [(ctx, task, oracle)]


@pytest.mark.parametrize("key", ["ruby_candidate_dependency_root", "scripted_dependency_root"])
def test_dependency_root_is_explicit_and_not_tied_to_an_experiment_date(tmp_path, key):
    ctx = SimpleNamespace(config={"migration": {key: str(tmp_path)}})
    assert ruby_candidate_lane._dependency_root(ctx) == tmp_path


def test_missing_or_relative_dependency_root_is_not_replaced_with_a_historical_path():
    for config in ({}, {"migration": {"ruby_candidate_dependency_root": "relative"}}):
        with pytest.raises(ContractError, match="explicit|absolute"):
            ruby_candidate_lane._dependency_root(SimpleNamespace(config=config))


def test_file_only_asset_rehydration_restores_the_hash_bound_empty_bootstrap(tmp_path):
    assets, cache = tmp_path / "assets", tmp_path / "cache"
    (assets / "gems").mkdir(parents=True)
    (assets / "gems/library.rb").write_text("fixed dependency")
    cache.mkdir()
    expected_empty = tmp_path / "original-empty-bootstrap"
    expected_empty.mkdir()
    commands = ruby_candidate_lane.seed_dependency_commands(str(assets), str(cache), str(cache / "bootstrap"))
    result = subprocess.run(["bash", "-eu", "-c", " && ".join(commands)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (cache / "gems/library.rb").read_text() == "fixed dependency"
    assert ruby_candidate.tree_fingerprint(cache / "bootstrap") == ruby_candidate.tree_fingerprint(expected_empty)
    assert not (assets / "bootstrap").exists()


def test_minitest_workers_are_bounded_without_changing_the_ready_rspec_recipe():
    assert ruby_candidate_lane.framework_environment(ruby_candidate.PROMETHEUS) == {"MT_CPU": "2"}
    assert ruby_candidate_lane.framework_environment(ruby_candidate.KNAPSACK) == {}
