"""Explicit context schema and cap metadata, alongside unchanged legacy behavior."""
import copy
import json
import os
from pathlib import Path

import pytest

from test_agentic_judge_recovery_lineage import extended_lineage, bind
from test_agentic_judge_recovery_bootstrap import write
from test_agentic_context_judgment_recovery import MODELS, IDENTITIES, fixture, manifest_for, add_unselected
import recover_agentic_context_judgments as context
import recover_agentic_judgments as retained
from reviewbench_maintainability.agentic.contracts import ContractError, file_hash


def context_layer(tmp_path, monkeypatch):
    g = extended_lineage(tmp_path)
    old = {"endpoint": "http://127.0.0.1:4000/v1/chat/completions", "models": MODELS,
           "expected_response_models": IDENTITIES, "max_context_chars": 300000,
           "max_output_tokens": 4096, "timeout_seconds": 180}
    override = {"schema_version": retained.CONTEXT_OVERRIDE_SCHEMA,
                "input_hashes": retained.CONTEXT_INPUT_HASHES,
                "original_judge_config": old, "admitted_judge_config": {**old, "max_context_chars": 1600000},
                "original_judge_fingerprint": retained.CONTEXT_OLD_FINGERPRINT,
                "admitted_judge_fingerprint": retained.CONTEXT_NEW_FINGERPRINT,
                "changed_fields": ["max_context_chars"], "max_first_attempts_per_member": 1}
    v9 = json.loads((g.paths["v9"] / "experiment-manifest.json").read_text())
    v10 = {key: v9[key] for key in ("task_ids", "tasks_fingerprint", "models", "expected_pairs",
                                  "source_run", "source_manifest_sha256")}
    v10.update(schema_version=retained.SCHEMA, base_run=str(g.paths["v9"]),
               base_manifest_sha256=file_hash(g.paths["v9"] / "experiment-manifest.json"),
               generation_engine_fingerprint=retained.BOOTSTRAP_GENERATION_ENGINE,
               base_host_engine_fingerprint="c" * 64)
    base = tmp_path / "v10"
    write(base / "experiment-manifest.json", v10)
    digest = file_hash(base / "experiment-manifest.json")
    monkeypatch.setattr(retained, "CONTEXT_BASE_MANIFEST_SHA256", digest)
    bound = retained.base_engine_binding(base, v10, g.tasks, g.models)
    output = tmp_path / "context"
    manifest = {**v10, **bound, "schema_version": retained.CONTEXT_ADMISSION_SCHEMA,
                "base_run": str(base), "base_manifest_sha256": digest, "context_admission": override,
                "selection_sha256": retained.CONTEXT_INPUT_HASHES["selection.json"],
                "judge_fingerprint": retained.CONTEXT_NEW_FINGERPRINT}
    write(output / "experiment-manifest.json", manifest)
    return g, output, manifest


def test_explicit_context_schema_binds_full_v10_lineage_and_current_host(tmp_path, monkeypatch):
    g, output, manifest = context_layer(tmp_path, monkeypatch)
    bound = retained.base_engine_binding(output, manifest, g.tasks, g.models)
    assert len(bound["recovery_lineage"]) == 7
    assert bound["base_host_engine_fingerprint"] == "c" * 64


@pytest.mark.parametrize("change", ["unknown-schema", "old-cap", "new-cap", "endpoint", "output-cap",
                                   "identity", "fingerprint", "selection", "extra", "count", "parent", "cycle"])
def test_context_schema_cannot_smuggle_another_configuration_or_base(tmp_path, monkeypatch, change):
    g, output, manifest = context_layer(tmp_path, monkeypatch)
    value = copy.deepcopy(manifest)
    meta = value["context_admission"]
    if change == "unknown-schema":value["schema_version"] = "unknown-context/v1"
    elif change == "old-cap":meta["original_judge_config"]["max_context_chars"] += 1
    elif change == "new-cap":meta["admitted_judge_config"]["max_context_chars"] += 1
    elif change == "endpoint":meta["admitted_judge_config"]["endpoint"] = "http://different.invalid"
    elif change == "output-cap":meta["admitted_judge_config"]["max_output_tokens"] += 1
    elif change == "identity":meta["admitted_judge_config"]["expected_response_models"]["gpt-5.6-sol"].append("gpt-5.6-sol")
    elif change == "fingerprint":value["judge_fingerprint"] = retained.CONTEXT_OLD_FINGERPRINT
    elif change == "selection":value["selection_sha256"] = "0" * 64
    elif change == "extra":meta["fallback"] = "general"
    elif change == "count":meta["max_first_attempts_per_member"] = True
    elif change == "parent":value["base_manifest_sha256"] = "f" * 64
    else:value["base_run"] = str(output)
    write(output / "experiment-manifest.json", value)
    with pytest.raises(ContractError):
        retained.base_engine_binding(output, value, g.tasks, g.models)


def test_wrong_runtime_row_is_not_overlaid_even_if_old_answer_is_unchanged(tmp_path, monkeypatch):
    g, output, manifest = context_layer(tmp_path, monkeypatch)
    parent = Path(manifest["base_run"]) / "experiment-manifest.json"
    data = json.loads(parent.read_text());data["models"] = ["another-solver"]
    write(parent, data)
    with pytest.raises(ContractError):
        retained.base_engine_binding(output, manifest, g.tasks, g.models)


@pytest.mark.parametrize("change", ["unknown", "owner", "allow-dispatch", "attempt-count", "mutable-files",
                                   "missing-tasks", "missing-source-config", "controller", "relative", "fingerprint"])
def test_explicit_v2_lineage_validates_policy_owner_and_immutable_inputs(fixture, change):
    f = fixture
    manifest = manifest_for(f)
    value = copy.deepcopy(manifest)
    if change == "unknown":
        value["publication_policy"]["schema_version"] = "other/v2"
    elif change == "owner":
        value["publication_policy"]["owner_output"] = str(f.tmp_path / "other")
    elif change == "allow-dispatch":
        value["publication_policy"]["watch_mode"] = "admit-and-publish"
    elif change == "attempt-count":
        value["publication_policy"]["max_first_attempts"] = 40
    elif change == "mutable-files":
        value["bound_inputs"][str(f.base / "generation_results.jsonl")] = file_hash(f.base / "generation_results.jsonl")
    elif change == "missing-tasks":
        value["bound_inputs"].pop(str(f.tasks_file))
    elif change == "missing-source-config":
        value["bound_inputs"].pop(str(f.source / "experiment-config.json"))
    elif change == "controller":
        value["controller_sha256"] = "0" * 64
    elif change == "relative":
        value["input_bundle"] = "relative"
    else:
        value["judge_fingerprint"] = retained.CONTEXT_OLD_FINGERPRINT
    write(f.output_root / "experiment-manifest.json", value)
    with pytest.raises(ContractError):
        retained.base_engine_binding(f.output_root, value, f.tasks, ["gpt-5.4-responses"])


def test_v2_reader_pins_one_immutable_epoch_during_pointer_swap(fixture, monkeypatch):
    f = fixture
    manifest = manifest_for(f)
    first = context.cached_iteration(f.bundle, manifest, f.tasks, f.output_root)
    first_target = os.readlink(f.output_root / "current")
    add_unselected(f)
    second = context.cached_iteration(f.bundle, manifest, f.tasks, f.output_root)
    context.switch_publication(f.output_root, first["publication_key"])
    selected_file = f.output_root / first_target / "generation_results.jsonl"
    read = Path.read_bytes
    swaps = []
    def swapped(path):
        value = read(path)
        if path == selected_file and not swaps:
            context.switch_publication(f.output_root, second["publication_key"])
            swaps.append(True)
        return value
    monkeypatch.setattr(Path, "read_bytes", swapped)
    pinned = retained.read_base_rows(f.output_root, f.tasks, ["gpt-5.4-responses"])
    assert swaps and len(pinned) == 13
    assert len(retained.read_base_rows(f.output_root, f.tasks, ["gpt-5.4-responses"])) == 14


@pytest.mark.parametrize("change", ["root-alias", "pointer", "epoch-symlink", "file-hash", "receipt-owner",
                                   "receipt-manifest", "receipt-identity", "drop-policy", "rewritten-hashes", "base-receipt"])
def test_v2_snapshot_reader_fails_closed_on_tampered_layout_or_receipts(fixture, change):
    f = fixture
    manifest = manifest_for(f)
    report = context.cached_iteration(f.bundle, manifest, f.tasks, f.output_root)
    epoch = f.output_root / "publications" / report["publication_key"]
    if change == "root-alias":
        alias = f.output_root / "judgments.jsonl"
        alias.unlink()
        alias.symlink_to(f.base / "judgments.jsonl")
    elif change == "pointer":
        (f.output_root / "current").unlink()
        (f.output_root / "current").symlink_to(f.base)
    elif change == "epoch-symlink":
        moved = f.tmp_path / "moved-epoch"
        epoch.rename(moved)
        epoch.symlink_to(moved)
    elif change in ("file-hash", "rewritten-hashes"):
        target = epoch / "judgments.jsonl"
        target.write_bytes(target.read_bytes() + b" ")
        if change == "rewritten-hashes":
            receipt_file = epoch / "snapshot.json"
            receipt = json.loads(receipt_file.read_text())
            receipt["output_hashes"]["judgments.jsonl"] = file_hash(target)
            receipt["identity"]["output_hashes"]["judgments.jsonl"] = file_hash(target)
            write(receipt_file, receipt)
    elif change == "drop-policy":
        value = copy.deepcopy(manifest)
        value.pop("publication_policy")
        write(f.output_root / "experiment-manifest.json", value)
    else:
        receipt_file = epoch / "snapshot.json"
        receipt = json.loads(receipt_file.read_text())
        if change == "receipt-owner":
            receipt["owner_output"] += "-foreign"
        elif change == "receipt-manifest":
            receipt["manifest_sha256"] = "0" * 64
        elif change == "base-receipt":
            receipt["base_snapshot"]["file_sha256"]["judgments.jsonl"] = "0" * 64
        else:
            receipt["identity"]["fixture_tamper"] = True
        write(receipt_file, receipt)
    with pytest.raises(ContractError):
        retained.read_base_rows(f.output_root, f.tasks, ["gpt-5.4-responses"])
