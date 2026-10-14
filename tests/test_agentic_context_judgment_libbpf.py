"""One original libbpf first panel; no additional allowance for a consumed member."""
import copy
import json
import os
from pathlib import Path
import shutil

import pytest

from test_agentic_context_judgment_recovery import (
    fixture, no_network, owner_request, vote, write, MODELS,
)
import recover_agentic_context_judgments as context
import recover_agentic_judgments as retained
from reviewbench_maintainability.agentic.contracts import ContractError, file_hash

NAME = "libbpf57-original-empty-panel-v1"


def supplied():
    value = os.environ.get("LIBBPF_CONTEXT_ADMISSION_INPUTS")
    if not value:
        pytest.skip("requires explicitly sealed original libbpf first episode")
    return Path(value)


@pytest.fixture
def one(fixture, monkeypatch):
    f = fixture
    f.old_bundle = f.bundle
    directory = f.tmp_path / "one-input"
    selections = f.selections[:1]
    for name, value in (
        ("selection.json", selections),
        ("original-case-bindings.json", [
            {"selection": selected, "row": f.rows[(selected["id"], selected["model"])]}
            for selected in selections]),
        ("judge-configs.json", f.configs),
    ):
        write(directory / name, value)
    pins = {name: file_hash(directory / name) for name in retained.CONTEXT_INPUT_HASHES}
    paths = json.loads((f.input_dir / "paths.json").read_text())
    paths["selection_sha256"] = pins["selection.json"]
    write(directory / "paths.json", paths)
    write(directory / "original-bindings-proof.json", {"synthetic_one_case": True})
    profile = {
        "name": "synthetic-sealed-libbpf-case", "input_hashes": pins,
        "additional_input_hashes": {name: file_hash(directory / name)
                                   for name in ("paths.json", "original-bindings-proof.json")},
        "selected_originals": 1, "base_manifest_sha256": file_hash(f.base / "experiment-manifest.json"),
        "inherited_context_first_attempts": 42,
    }
    monkeypatch.setattr(retained, "ADDITIONAL_CONTEXT_PROFILES", {profile["name"]: profile})
    report = json.loads((f.base / "report.json").read_text())
    report.update(historical_first_attempts_consumed=3, all_context_first_attempts_consumed=42)
    write(f.base / "report.json", report)
    f.input_dir, f.output_root = directory, f.tmp_path / "one-owner"
    f.bundle = context.load_inputs(directory)
    f.requests = [owner_request(f, f.output_root, selection) for selection in selections]
    return f


def test_all_original_cohorts_and_claim_implementation_are_not_replaced():
    assert retained.context_profile()["selected_originals"] == 13
    old = retained.ADDITIONAL_CONTEXT_PROFILES
    assert old["scylla96-original-empty-panel-v1"]["inherited_context_first_attempts"] == 39
    assert old["clarity103-original-empty-panel-v1"]["inherited_context_first_attempts"] == 42
    assert old["cargo-contract108109-original-empty-panel-v1"]["selected_originals"] == 2
    new = old[NAME]
    assert new["selected_originals"] == 1 and new["inherited_context_first_attempts"] == 42
    assert retained.context_profile(new["input_hashes"]) == new
    assert retained.context_profile_for_selection(new["input_hashes"]["selection.json"]) == new
    assert retained.MAX_RECOVERY_LINEAGE == 16


def test_one_case_adds_three_first_intents_and_keeps_negative(one):
    f = one
    manifest, _, _ = context.controller_manifest(f.bundle, f.config_file, f.base, f.output_root, False)
    before = context.cached_iteration(f.bundle, manifest, f.tasks, f.output_root)
    assert before["all_context_first_attempts_consumed"] == 42
    assert before["publication_policy"]["max_first_attempts"] == 3
    calls = []
    def invoke(model, *_):
        calls.append(model)
        return vote(model, model != "gpt-5.6-sol")
    for request in f.requests:
        result = context.recover_one(request, invoke=invoke)
        assert result["row"]["generation"] == request["row"]["generation"]
        assert result["row"]["correctness"] == request["row"]["correctness"]
        assert result["row"]["judgment"]["maintainability_pass"] is False
        assert context.recover_one(request, invoke=lambda *_: pytest.fail("resampled")) == result
    assert calls == MODELS
    after = context.cached_iteration(f.bundle, manifest, f.tasks, f.output_root)
    assert after["historical_first_attempts_consumed"] == 3
    assert after["inherited_context_first_attempts_consumed"] == 42
    assert after["all_context_first_attempts_consumed"] == 45
    assert after["additional_judge_calls"] == 0


def test_failed_first_response_is_not_another_attempt_even_at_another_owner(one):
    f = one
    calls = []
    def invoke(model, *_):
        calls.append(model)
        return ({"judge_model": model, "http_status": 503, "error": "original first error"}
                if model == MODELS[0] else vote(model, False))
    request = f.requests[0]
    result = context.recover_one(request, invoke=invoke)
    assert result["row"]["judgment"]["status"] == "infra_error"
    assert context.recover_one(request, invoke=lambda *_: pytest.fail("error was retried")) == result
    selected, original = request["selection"], request["row"]
    first = context.source_claim(f.bundle, selected, original, Path(request["output"]), MODELS[0])
    alternate = context.source_claim(f.old_bundle, selected, original, f.tmp_path / "other", MODELS[0])
    assert first.root == alternate.root
    with pytest.raises(ContractError):
        alternate.check_owner()
    assert calls == MODELS


def test_an_unselected_case_does_not_gain_allowance(one):
    f = one
    other = f.selections[2]
    with pytest.raises(ContractError, match="exact original"):
        context.validate_original(f.bundle, other, f.rows[(other["id"], other["model"])])


def test_actual_libbpf_whole_patch_and_original_result_are_sealed():
    directory = supplied()
    bundle = context.load_inputs(directory)
    assert bundle["profile"] == json.loads((directory / "new-profile-pins.json").read_text())
    assert len(bundle["selections"]) == 1
    for index, selected, expected_size, correct in zip(
            ("57",), bundle["selections"], (462992,), (True,)):
        original = bundle["originals"][(selected["id"], selected["model"])]
        context.validate_original(bundle, selected, original)
        assert original["judgment"]["panel"] == []
        assert selected["full_evidence_characters"] == expected_size
        assert original["correctness"]["correctness_ok"] is correct
        assert original["generation"]["stop_reason"] == "format_error_limit"
        text = (directory / f"full-evidence{index}.txt").read_text()
        assert retained.canonical_hash(text) == selected["full_evidence_sha256"]
        assert len(text) == expected_size


@pytest.mark.parametrize("name", [
    "selection.json", "original-case-bindings.json", "judge-configs.json",
    "paths.json", "original-bindings-proof.json",
])
def test_every_libbpf_input_rejects_byte_changes(tmp_path, name):
    original, directory = supplied(), tmp_path / "changed-input"
    directory.mkdir()
    for value in ("selection.json", "original-case-bindings.json", "judge-configs.json",
                  "paths.json", "original-bindings-proof.json"):
        shutil.copyfile(original / value, directory / value)
    (directory / name).write_bytes((directory / name).read_bytes() + b" ")
    with pytest.raises(ContractError, match="sealed context"):
        context.load_inputs(directory)


@pytest.mark.parametrize("index", [0])
@pytest.mark.parametrize("field", ["generation", "correctness", "judgment"])
def test_changed_original_objects_are_not_new_first_attempts(index, field):
    bundle = context.load_inputs(supplied())
    selected = bundle["selections"][index]
    row = copy.deepcopy(bundle["originals"][(selected["id"], selected["model"])])
    row[field]["changed"] = True
    with pytest.raises(ContractError, match="original context"):
        context.validate_original(bundle, selected, row)
