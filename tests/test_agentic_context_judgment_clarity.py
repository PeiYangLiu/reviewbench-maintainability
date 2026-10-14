"""Seal the first Clarity panel without changing the original 42 consumed intents."""
import copy
import json
import os
from pathlib import Path
import shutil

import pytest

from test_agentic_context_judgment_recovery import fixture, no_network, vote, write, MODELS
from test_agentic_context_judgment_scylla import single
import recover_agentic_context_judgments as context
import recover_agentic_judgments as retained
from reviewbench_maintainability.agentic.contracts import ContractError


def test_clarity_profile_preserves_the_two_earlier_cohorts():
    assert retained.context_profile()["selected_originals"] == 13
    old = retained.ADDITIONAL_CONTEXT_PROFILES["scylla96-original-empty-panel-v1"]
    assert old["selected_originals"] == 1 and old["inherited_context_first_attempts"] == 39
    new = retained.ADDITIONAL_CONTEXT_PROFILES["clarity103-original-empty-panel-v1"]
    assert new["selected_originals"] == 1 and new["inherited_context_first_attempts"] == 42
    assert retained.context_profile(new["input_hashes"]) == new
    assert retained.context_profile_for_selection(new["input_hashes"]["selection.json"]) == new
    assert new["base_manifest_sha256"] == "cf7171a9c30cbb8de9f97691df56eb7ce0705e9d08d3db94ab2bc0d71235b893"
    assert retained.MAX_RECOVERY_LINEAGE == 16


def test_recursive_context_history_adds_three_to_the_total_not_the_last_owner(single):
    f = single
    f.profile["inherited_context_first_attempts"] = 42
    report = json.loads((f.base / "report.json").read_text())
    report.update(historical_first_attempts_consumed=3, all_context_first_attempts_consumed=42)
    write(f.base / "report.json", report)
    manifest, _, _ = context.controller_manifest(f.bundle, f.config_file, f.base, f.output_root, False)
    before = context.cached_iteration(f.bundle, manifest, f.tasks, f.output_root)
    assert before["all_context_first_attempts_consumed"] == 42
    calls = []

    def invoke(model, *_):
        calls.append(model)
        return vote(model, model != "gpt-5.6-sol")

    result = context.recover_one(f.request, invoke=invoke)
    assert calls == MODELS and result["row"]["judgment"]["maintainability_pass"] is False
    after = context.cached_iteration(f.bundle, manifest, f.tasks, f.output_root)
    assert after["historical_first_attempts_consumed"] == 3
    assert after["inherited_context_first_attempts_consumed"] == 42
    assert after["all_context_first_attempts_consumed"] == 45
    assert after["additional_judge_calls"] == 0
    assert context.recover_one(f.request, invoke=lambda *_: pytest.fail("resampled first panel")) == result
    assert calls == MODELS


def supplied():
    path = os.environ.get("CLARITY_CONTEXT_ADMISSION_INPUTS")
    if not path:
        pytest.skip("requires explicitly sealed original Clarity input files")
    return Path(path)


def test_actual_clarity_full_source_and_original_empty_panel_are_sealed():
    bundle = context.load_inputs(supplied())
    assert len(bundle["selections"]) == 1
    selected = bundle["selections"][0]
    assert selected["id"] == "github__stx-labs__clarity-wasm__pull_98__comment_1364196603__complexity"
    assert selected["full_evidence_characters"] == 348067
    assert selected["full_evidence_sha256"] == "38f62e01b6c2af43e2cba1ab2675c2992a91f40815f892b487991f5f4fb81922"
    original = bundle["originals"][(selected["id"], selected["model"])]
    context.validate_original(bundle, selected, original)
    assert original["generation"]["usage"]["model_calls"] == 17
    assert original["correctness"]["correctness_ok"] is True
    assert original["judgment"]["panel"] == []
    assert bundle["profile"]["inherited_context_first_attempts"] == 42


@pytest.mark.parametrize("name", [
    "selection.json", "original-case-bindings.json", "judge-configs.json",
    "paths.json", "original-bindings-proof.json",
])
def test_each_actual_clarity_input_rejects_byte_changes(tmp_path, name):
    original = supplied()
    folder = tmp_path / "input-copy"
    folder.mkdir()
    for value in ("selection.json", "original-case-bindings.json", "judge-configs.json",
                  "paths.json", "original-bindings-proof.json"):
        shutil.copyfile(original / value, folder / value)
    (folder / name).write_bytes((folder / name).read_bytes() + b" ")
    with pytest.raises(ContractError, match="sealed context"):
        context.load_inputs(folder)


@pytest.mark.parametrize("field", ["generation", "correctness", "judgment"])
def test_changed_original_is_not_another_first_attempt(field):
    bundle = context.load_inputs(supplied())
    selected = bundle["selections"][0]
    row = copy.deepcopy(bundle["originals"][(selected["id"], selected["model"])])
    row[field]["new_input"] = True
    with pytest.raises(ContractError, match="original context"):
        context.validate_original(bundle, selected, row)
