"""A separate, sealed one-case admission preserves the original thirteen owners."""
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


@pytest.fixture
def single(fixture, monkeypatch):
    f = fixture
    f.old_bundle, f.old_input_dir, f.old_owner = f.bundle, f.input_dir, f.output_root
    directory = f.tmp_path / "single-inputs"
    selected = f.selections[0]
    for name, value in (
        ("selection.json", [selected]),
        ("original-case-bindings.json", [{"selection": selected, "row": f.row}]),
        ("judge-configs.json", f.configs),
    ):
        write(directory / name, value)
    pins = {name: file_hash(directory / name) for name in retained.CONTEXT_INPUT_HASHES}
    paths = json.loads((f.old_input_dir / "paths.json").read_text())
    paths["selection_sha256"] = pins["selection.json"]
    write(directory / "paths.json", paths)
    write(directory / "original-bindings-proof.json", {"synthetic_one_case": True})
    profile = {
        "name": "synthetic-sealed-single-case",
        "input_hashes": pins,
        "additional_input_hashes": {name: file_hash(directory / name)
                                   for name in ("paths.json", "original-bindings-proof.json")},
        "selected_originals": 1, "base_manifest_sha256": file_hash(f.base / "experiment-manifest.json"),
        "inherited_context_first_attempts": 39,
    }
    monkeypatch.setattr(retained, "ADDITIONAL_CONTEXT_PROFILES", {profile["name"]: profile})
    report = json.loads((f.base / "report.json").read_text())
    report["historical_first_attempts_consumed"] = 39
    write(f.base / "report.json", report)
    f.input_dir = directory
    f.bundle = context.load_inputs(directory)
    f.output_root = f.tmp_path / "single-owner"
    f.request = owner_request(f, f.output_root)
    f.profile = profile
    return f


def test_old_thirteen_profile_and_policy_are_unchanged(single):
    f = single
    before = copy.deepcopy(retained.CONTEXT_INPUT_HASHES)
    old = context.load_inputs(f.old_input_dir)
    assert len(old["selections"]) == 13 and old["override"] == f.old_bundle["override"]
    assert retained.context_publication_policy(f.old_owner)["selected_originals"] == 13
    assert retained.context_publication_policy(f.old_owner)["max_first_attempts"] == 39
    assert retained.CONTEXT_INPUT_HASHES == before
    assert retained.context_publication_policy(f.output_root, f.bundle["override"])["max_first_attempts"] == 3


def test_profile_lookup_returns_independent_copies(single):
    f = single
    selected = retained.context_profile(f.profile["input_hashes"])
    selected["input_hashes"]["selection.json"] = "changed"
    assert retained.context_profile(f.profile["input_hashes"]) == f.profile


@pytest.mark.parametrize("bad", [None, {}, [], "arbitrary", {"selection.json": "f" * 64}])
def test_unsealed_or_null_profile_metadata_is_rejected(single, bad):
    value = copy.deepcopy(single.bundle["override"])
    value["input_hashes"] = bad
    with pytest.raises(ContractError):
        retained.validate_context_admission_metadata(value)


@pytest.mark.parametrize("name", [
    "selection.json", "original-case-bindings.json", "judge-configs.json",
    "paths.json", "original-bindings-proof.json",
])
def test_all_five_single_case_inputs_are_byte_sealed(single, name):
    path = single.input_dir / name
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ContractError, match="sealed context"):
        context.load_inputs(single.input_dir)


def test_another_original_thirteen_case_is_not_admitted_by_the_single_profile(single):
    f = single
    with pytest.raises(ContractError, match="exact original"):
        context.validate_original(f.bundle, f.selections[1], f.rows[
            (f.selections[1]["id"], f.selections[1]["model"])])


def test_single_case_calls_original_panel_once_and_keeps_negative_vote(single):
    f = single
    calls = []

    def invoke(model, prompt, config):
        calls.append(model)
        return vote(model, model != "gpt-5.6-sol")

    result = context.recover_one(f.request, invoke=invoke)
    assert calls == MODELS and result["judge_request_attempts"] == 3
    assert result["row"]["generation"] == f.row["generation"]
    assert result["row"]["correctness"] == f.row["correctness"]
    assert result["row"]["judgment"]["maintainability_pass"] is False
    again = context.recover_one(f.request, invoke=lambda *_: pytest.fail("cached case called a model"))
    assert again == result and calls == MODELS


def test_changed_owner_or_old_profile_cannot_mint_another_allowance(single):
    f = single
    context.recover_one(f.request, invoke=lambda model, *_: vote(model))
    original = context.source_claim(f.bundle, f.selected, f.row, Path(f.request["output"]), MODELS[0])
    alternate = context.source_claim(f.old_bundle, f.selected, f.row,
                                     f.tmp_path / "another-owner", MODELS[0])
    assert original.root == alternate.root
    with pytest.raises(ContractError):
        alternate.check_owner()


@pytest.mark.parametrize("change", ["partial-panel", "accepted", "evidence", "wrong-error"])
def test_single_case_keeps_the_original_empty_panel_boundary(single, change):
    f = single
    row = copy.deepcopy(f.row)
    if change == "partial-panel":
        row["judgment"]["panel"] = [vote(MODELS[0], False)]
    elif change == "accepted":
        row["judgment"]["status"] = "completed"
    elif change == "evidence":
        row["judgment"]["evidence_sha256"] = "a" * 64
    else:
        row["judgment"]["error"] = "HTTP503"
    with pytest.raises(ContractError):
        context.validate_original(f.bundle, f.selected, row)


def test_cached_single_publication_preserves_prior39_and_adds_only_three(single):
    f = single
    manifest, _, _ = context.controller_manifest(f.bundle, f.config_file, f.base, f.output_root, False)
    first = context.cached_iteration(f.bundle, manifest, f.tasks, f.output_root)
    assert first["historical_first_attempts_consumed"] == 0
    assert first["inherited_context_first_attempts_consumed"] == 39
    assert first["all_context_first_attempts_consumed"] == 39
    assert first["publication_policy"]["selected_originals"] == 1
    assert first["publication_policy"]["max_first_attempts"] == 3
    before = copy.deepcopy(f.rows)
    context.recover_one(f.request, invoke=lambda model, *_: vote(model, model != "gpt-5.6-sol"))
    second = context.cached_iteration(f.bundle, manifest, f.tasks, f.output_root)
    assert second["additional_judge_calls"] == 0
    assert second["historical_first_attempts_consumed"] == 3
    assert second["inherited_context_first_attempts_consumed"] == 39
    assert second["all_context_first_attempts_consumed"] == 42
    rows, _, _ = retained.read_context_publication(
        f.output_root, manifest, f.tasks, ["gpt-5.4-responses"])
    chosen = f.selected["id"], f.selected["model"]
    assert all(rows[pair] == row for pair, row in before.items() if pair != chosen)
    assert rows[chosen]["generation"] == before[chosen]["generation"]
    assert rows[chosen]["correctness"] == before[chosen]["correctness"]
    assert rows[chosen]["judgment"]["maintainability_pass"] is False


def test_missing_upstream_attempt_history_is_not_silently_reset(single):
    f = single
    report = json.loads((f.base / "report.json").read_text())
    report.pop("historical_first_attempts_consumed")
    write(f.base / "report.json", report)
    manifest, _, _ = context.controller_manifest(f.bundle, f.config_file, f.base, f.output_root, False)
    with pytest.raises(ContractError, match="upstream context"):
        context.cached_iteration(f.bundle, manifest, f.tasks, f.output_root)
    assert not (f.output_root / "current").exists()


def test_actual_sealed_scylla_bundle_is_only_read_when_explicitly_supplied():
    configured = os.environ.get("SCYLLA_CONTEXT_ADMISSION_INPUTS")
    if not configured:
        pytest.skip("requires the explicitly sealed completed original Scylla episode")
    bundle = context.load_inputs(Path(configured))
    assert len(bundle["selections"]) == 1
    selected = bundle["selections"][0]
    assert selected["id"] == "github__scylladb__scylla-rust-driver__pull_509__comment_962978126__api_design"
    assert selected["full_evidence_characters"] == 355279
    original = bundle["originals"][(selected["id"], selected["model"])]
    context.validate_original(bundle, selected, original)
    assert original["generation"]["usage"]["model_calls"] == 21
    assert original["correctness"]["correctness_ok"] is True
    assert original["judgment"]["panel"] == []
