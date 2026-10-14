#!/usr/bin/env python3
"""First judge attempts for separately sealed original empty-panel cap failures."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import copy
from dataclasses import asdict
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import recover_agentic_judgments as retained
from reviewbench_maintainability.agentic.contracts import (
    ContractError, PROTOCOL, RuntimeRecipe, atomic_json, canonical_hash, component, file_hash,
)
from reviewbench_maintainability.agentic.evaluation import judge_prompt, invoke_judge
from reviewbench_maintainability.agentic.resources import file_lock, job_directory
from reviewbench_maintainability.agentic.snapshots import extract_start, apply_patch

SCHEMA = retained.CONTEXT_ADMISSION_SCHEMA
RESULT_SCHEMA = "reviewbench-context-admission-result/v1"
ORIGINAL_ERROR = "ContractError: judge_context_limit: complete evidence does not fit"
ENGINE = retained.BOOTSTRAP_GENERATION_ENGINE
ADDITIONAL_INPUT_HASHES = {
    "paths.json": "ba9c89fc4cadd7da0315c054710000cdd12b39cb4a5a97416813552f0f0041e0",
    "original-bindings-proof.json": "d6273bd77494e25f2fd246b33090ce7526fd2c47c5e8c0def787c5d4307f6a3c",
}
FILES = retained.CONTEXT_PUBLICATION_FILES
UNKNOWN_OUTCOME = "request outcome not recorded; no automatic repeat"


class SnapshotUnavailable(ContractError):
    """A writer is between coherent versions; retain the previous publication."""


class CachePending(ContractError):
    """A source journal is locked by its original owner, not a retry allowance."""


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def checked_json(path, expected):
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ContractError("sealed context-admission input changed: " + str(path))
    return json.loads(raw)


def load_inputs(directory):
    directory = Path(directory).resolve()
    profile = retained.context_profile_for_selection(file_hash(directory / "selection.json"))
    hashes = {**profile["input_hashes"], **profile.get("additional_input_hashes", ADDITIONAL_INPUT_HASHES)}
    data = {name: checked_json(directory / name, digest) for name, digest in hashes.items()}
    selections, cases = data["selection.json"], data["original-case-bindings.json"]
    configs, paths = data["judge-configs.json"], data["paths.json"]
    if (type(selections) is not list or len(selections) != profile["selected_originals"]
            or len(cases) != profile["selected_originals"]
            or list(configs) != ["admission", "original"]
            or paths["selection_sha256"] != hashes["selection.json"]):
        raise ContractError("context admission requires its exact sealed original cohort")
    override = {
        "schema_version": retained.CONTEXT_OVERRIDE_SCHEMA,
        "input_hashes": dict(profile["input_hashes"]),
        "original_judge_config": configs["original"], "admitted_judge_config": configs["admission"],
        "original_judge_fingerprint": retained.CONTEXT_OLD_FINGERPRINT,
        "admitted_judge_fingerprint": retained.context_admitted_settings(profile)[1],
        "changed_fields": ["max_context_chars"], "max_first_attempts_per_member": 1,
    }
    retained.validate_context_admission_metadata(override)
    pins, originals = {}, {}
    for selected, case in zip(selections, cases):
        pair = selected["id"], selected["model"]
        if pair in pins or canonical_hash(selected) != canonical_hash(case["selection"]):
            raise ContractError("context admission selection/case ordering or identity differs")
        pins[pair], originals[pair] = selected, case["row"]
    bundle = {"directory": str(directory), "hashes": hashes, "paths": paths, "selections": selections,
              "pins": pins, "originals": originals, "override": override, "configs": configs,
              "profile": profile}
    for selected in selections:
        validate_original(bundle, selected, originals[(selected["id"], selected["model"])], files=False)
    return bundle


def recheck_inputs(bundle):
    for name, digest in bundle["hashes"].items():
        if file_hash(Path(bundle["directory"]) / name) != digest:
            raise ContractError("sealed context inputs changed during recovery")


def validate_original(bundle, selected, row, *, task=None, files=True):
    pair = selected.get("id"), selected.get("model")
    expected = bundle["pins"].get(pair)
    if expected is None or canonical_hash(selected) != canonical_hash(expected):
        raise ContractError("only an exact original context-admission selection is allowed")
    retained.validate_context_admission_metadata(bundle["override"])
    for field in ("generation", "correctness", "judgment"):
        if canonical_hash(row.get(field, {})) != selected[field + "_sha256"]:
            raise ContractError("original context " + field + " object changed")
    generation, correctness, previous = row["generation"], row["correctness"], row["judgment"]
    if (row.get("id") != pair[0] or row.get("model") != pair[1]
            or generation.get("status") != "generated" or generation.get("protocol") != PROTOCOL
            or generation.get("engine_fingerprint") != ENGINE
            or previous.get("status") != "infra_error" or previous.get("panel") != []
            or previous.get("error") != ORIGINAL_ERROR
            or previous.get("maintainability_pass") is not None
            or any(key in previous for key in ("evidence_sha256", "evidence_complete", "passed", "verdict"))
            or previous.get("engine_fingerprint") != ENGINE
            or previous.get("judge_fingerprint") != retained.CONTEXT_OLD_FINGERPRINT
            or correctness.get("status") not in retained.TERMINAL_CORRECTNESS):
        raise ContractError("requires an original empty-panel context failure, not an accepted/evidenced judgment")
    for field in ("id", "model", "protocol", "task_fingerprint", "patch_sha256"):
        if previous.get(field) != generation.get(field) or correctness.get(field) != generation.get(field):
            raise ContractError("original context answer/measurement binding differs in " + field)
    if correctness.get("runtime_fingerprint") != generation.get("runtime_fingerprint"):
        raise ContractError("original context correctness runtime differs")
    if (selected["judge_engine"] != ENGINE
            or selected["judge_models"] != bundle["configs"]["original"]["models"]
            or type(selected["max_first_attempts_per_member"]) is not int
            or selected["max_first_attempts_per_member"] != 1
            or selected["old_judge_fingerprint"] != retained.CONTEXT_OLD_FINGERPRINT
            or selected["new_judge_fingerprint"] != bundle["override"]["admitted_judge_fingerprint"]
            or selected["old_max_context_chars"] != 300000
            or selected["new_max_context_chars"] != bundle["configs"]["admission"]["max_context_chars"]
            or type(selected["full_evidence_characters"]) is not int
            or not 300000 < selected["full_evidence_characters"] <= bundle["configs"]["admission"]["max_context_chars"]
            or not retained.HEX256.fullmatch(selected["full_evidence_sha256"])):
        raise ContractError("context admission changes the fixed panel/cap/evidence contract")
    if task is not None and (task["id"] != pair[0] or canonical_hash(task) != generation["task_fingerprint"]):
        raise ContractError("context admission task changed")
    if files:
        retained.validate_answer(generation)


def source_claim(bundle, selected, original, output, model):
    # Absence is an original input, not a new cap/profile. Neither the key nor
    # this absence sentinel depends on revised config, output or profile version.
    absent_member = {"judge_model": model, "original_panel": []}
    binding = {
        "judge_model": model, "original_member_sha256": canonical_hash(absent_member),
        "generation_sha256": selected["generation_sha256"],
        "judgment_sha256": selected["judgment_sha256"],
        "evidence_sha256": selected["full_evidence_sha256"],
        "evidence_characters": selected["full_evidence_characters"],
        "judge_fingerprint": selected["new_judge_fingerprint"],
        "context_admission": copy.deepcopy(bundle["override"]),
    }
    return retained.SourceMemberClaim(original["generation"], original["judgment"],
                                      Path(output) / "members", binding)


def pinned_prompt(bundle, selected, task, original, repository):
    """Only full source extraction in the admitted job; diagnostic trees are unused."""
    with job_directory(prefix="judge-context-complete-source-") as directory:
        root = Path(directory).resolve()
        before, after = root / "before", root / "after"
        extract_start(repository, task["start_sha"], before)
        patch = Path(original["generation"]["patch_file"])
        changed = apply_patch(before, patch, after)
        prompt = judge_prompt(task, before, after, patch.read_text(), changed,
                              maximum_chars=bundle["configs"]["admission"]["max_context_chars"])
        if (len(prompt) != selected["full_evidence_characters"]
                or canonical_hash(prompt) != selected["full_evidence_sha256"]):
            raise ContractError("full source evidence SHA/character count differs before any first attempt")
        return prompt


def validate_record(bundle, selected, original, record):
    """Pure result binding checks, shared by replay and publication."""
    validate_original(bundle, selected, original)
    if (record.get("schema_version") != RESULT_SCHEMA
            or canonical_hash(record.get("selection")) != canonical_hash(selected)
            or record.get("context_admission") != bundle["override"]
            or canonical_hash(record.get("original_row")) != canonical_hash(original)
            or record.get("original_judgment_engine_fingerprint") != ENGINE
            or type(record.get("judge_request_attempts")) is not int
            or record["judge_request_attempts"] != 3
            or record.get("additional_generation_calls") != 0
            or record.get("additional_correctness_calls") != 0):
        raise ContractError("cached context result/provenance differs")
    row = record["row"]
    if (row.get("id") != original["id"] or row.get("model") != original["model"]
            or canonical_hash(row.get("generation")) != canonical_hash(original["generation"])
            or canonical_hash(row.get("correctness")) != canonical_hash(original["correctness"])):
        raise ContractError("context result changed original answer/correctness")
    judgment = row["judgment"]
    for key in ("id", "model", "protocol", "task_fingerprint", "patch_sha256"):
        if judgment.get(key) != original["generation"].get(key):
            raise ContractError("context result changed original " + key)
    if (judgment.get("engine_fingerprint") != ENGINE
            or judgment.get("judge_fingerprint") != selected["new_judge_fingerprint"]
            or judgment.get("context_admission") != bundle["override"]
            or judgment.get("evidence_complete") is not True
            or judgment.get("evidence_sha256") != selected["full_evidence_sha256"]
            or judgment.get("evidence_characters") != selected["full_evidence_characters"]
            or [m.get("judge_model") for m in judgment.get("panel", [])] != selected["judge_models"]):
        raise ContractError("cached context panel/config/evidence differs")
    members = judgment["panel"]
    for member in members:
        if "passed" in member:
            retained.validate_member(member, bundle["configs"]["admission"])
    complete = all("passed" in m for m in members)
    if (judgment.get("status") != ("completed" if complete else "infra_error")
            or judgment.get("maintainability_pass") != (all(m["passed"] for m in members) if complete else None)):
        raise ContractError("cached context completion/negative verdict changed")


def validate_cached_claims(bundle, selected, original, record, output):
    validate_record(bundle, selected, original, record)
    if record.get("owner_output") != str(Path(output).resolve()):
        raise ContractError("context result belongs to another owner_output")
    for member in record["row"]["judgment"]["panel"]:
        state = read_claim_state(source_claim(bundle, selected, original, output, member["judge_model"]))
        if not state["started"] or state["member"] != member:
            raise ContractError("cached context result differs from source response/started record")


def read_claim_state(claim):
    """No mkdir, O_CREAT, journal repair or blocking wait on the cache-only path."""
    claim._check_paths()
    if not claim.root.exists():
        if claim.local.exists():
            raise ContractError("context local journal has no original source claim")
        return {"started": False, "member": None}
    lock = claim.root / "claim.lock"
    try:
        descriptor = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError as exc:
        if any((claim.root / name).exists() for name in ("claim.json", "started.json", "response.json")):
            raise ContractError("context source journal lost its durable lock") from exc
        raise CachePending("source claim is being initialized") from exc
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CachePending("original source member is still locked/in-flight") from exc
        claim._check_paths()
        owner = claim.root / "claim.json"
        shared_start, shared_response = claim.root / "started.json", claim.root / "response.json"
        local_start, local_response = claim.local / "started.json", claim.local / "response.json"
        if not owner.exists():
            if any(p.exists() for p in (shared_start, shared_response, local_start, local_response)):
                raise ContractError("context source journal contains unowned invocation records")
            return {"started": False, "member": None}
        claim._check_owner(required=True)
        if shared_start.exists() and json.loads(shared_start.read_text()) != claim.owner:
            raise ContractError("context source started owner/binding changed")
        if local_start.exists() and json.loads(local_start.read_text()) != claim.binding:
            raise ContractError("context local started binding changed")
        if not shared_start.exists():
            if any(p.exists() for p in (shared_response, local_start, local_response)):
                raise ContractError("context journal lacks the original source intent")
            return {"started": False, "member": None}
        responses = []
        for path, started in ((shared_response, shared_start), (local_response, local_start)):
            if path.exists():
                cached = json.loads(path.read_text())
                if (not started.exists() or set(cached) != {"binding", "member"}
                        or cached["binding"] != claim.binding
                        or cached["member"].get("judge_model") != claim.binding["judge_model"]):
                    raise ContractError("context recorded response provenance differs")
                responses.append(cached["member"])
        if len(responses) == 2 and responses[0] != responses[1]:
            raise ContractError("context source/local responses differ")
        member = responses[0] if responses else {"judge_model": claim.binding["judge_model"], "error": UNKNOWN_OUTCOME}
        return {"started": True, "member": member}
    finally:
        os.close(descriptor)


def record_from_members(bundle, selected, original, output, members):
    complete = all("passed" in m for m in members)
    judgment = {
        **{key: original["generation"][key] for key in ("id", "model", "protocol", "task_fingerprint", "patch_sha256")},
        "engine_fingerprint": ENGINE, "judge_fingerprint": selected["new_judge_fingerprint"],
        "context_admission": copy.deepcopy(bundle["override"]),
        "status": "completed" if complete else "infra_error", "panel": members,
        "maintainability_pass": all(m["passed"] for m in members) if complete else None,
        "evidence_complete": True, "evidence_sha256": selected["full_evidence_sha256"],
        "evidence_characters": selected["full_evidence_characters"],
    }
    record = {
        "schema_version": RESULT_SCHEMA, "selection": selected, "original_row": original,
        "context_admission": bundle["override"], "owner_output": str(Path(output).resolve()),
        "row": {**original, "judgment": judgment},
        "original_judgment_engine_fingerprint": ENGINE, "judge_request_attempts": 3,
        "additional_generation_calls": 0, "additional_correctness_calls": 0,
    }
    validate_record(bundle, selected, original, record)
    return record


def write_record(output, record):
    for name, value in (("original-row.json", record["original_row"]),
                        ("original-judgment.json", record["original_row"]["judgment"]),
                        ("context-admission.json", record["context_admission"]),
                        ("judgment.json", record["row"]["judgment"]), ("result.json", record)):
        path = Path(output) / name
        if path.is_symlink():
            raise ContractError("context owner evidence must not be a symlink")
        if path.exists():
            if json.loads(path.read_text()) != value:
                raise ContractError("retained context result/original/provenance changed")
        else:
            retained.SourceMemberClaim._write(path, value)


def cached_record(bundle, selected, original, output):
    """Reassemble only fully consumed panels; unstarted members remain unstarted."""
    output = Path(output)
    result_file = output / "result.json"
    if result_file.is_symlink():
        raise ContractError("context result must not be a symlink")
    states = [read_claim_state(source_claim(bundle, selected, original, output, model))
              for model in selected["judge_models"]]
    consumed = sum(state["started"] for state in states)
    if result_file.exists():
        record = json.loads(result_file.read_text())
        validate_cached_claims(bundle, selected, original, record, output)
        return record, consumed
    if consumed != 3:
        return None, consumed
    record = record_from_members(bundle, selected, original, output, [state["member"] for state in states])
    validate_cached_claims(bundle, selected, original, record, output)
    write_record(output, record)
    return record, consumed


def recover_one(request, *, invoke=invoke_judge):
    if set(request) != {"input_bundle", "config", "task", "row", "selection", "recipe", "output"}:
        raise ContractError("context child request has unknown override fields")
    bundle = load_inputs(request["input_bundle"])
    original, selected, task = copy.deepcopy(request["row"]), request["selection"], request["task"]
    validate_original(bundle, selected, original, task=task)
    config = retained.load_config(Path(request["config"]))
    if config["judge"] != bundle["configs"]["original"] or retained.engine_identity()["fingerprint"] != ENGINE:
        raise ContractError("context admission requires the unchanged original config and frozen engine5142")
    recipe = RuntimeRecipe.from_dict(request["recipe"])
    if recipe.fingerprint != original["generation"]["runtime_fingerprint"]:
        raise ContractError("context source/submodule recipe differs from the completed answer")
    output = Path(request["output"]).resolve()
    owner_root = output.parents[3]
    if output_directory(owner_root, selected) != output:
        raise ContractError("context child output is not the fixed owner repair directory")
    owner_manifest, tasks, _ = controller_manifest(bundle, Path(request["config"]),
                                                  Path(bundle["paths"]["base_run"]), owner_root, False)
    check_owner_manifest(owner_root, owner_manifest, required=True)
    def live_guard():
        recheck_manifest(bundle, owner_manifest)
        current = coherent_snapshot(bundle, Path(owner_manifest["base_run"]), tasks, owner_manifest["models"])
        validate_original(bundle, selected, current["rows"][(selected["id"], selected["model"])], task=task)
        check_owner_manifest(owner_root, owner_manifest, required=True)
    live_guard()
    claims = [source_claim(bundle, selected, original, output, model) for model in selected["judge_models"]]
    for claim in claims:
        claim.check_owner()
    result, _ = cached_record(bundle, selected, original, output)
    if result is not None:
        recheck_inputs(bundle)
        return result  # No ExecutionServices, extraction, compilation or invocation.
    output.mkdir(parents=True, exist_ok=True)
    for name, value in (("original-row.json", original), ("original-judgment.json", original["judgment"]),
                        ("context-admission.json", bundle["override"])):
        path = output / name
        if path.exists() and json.loads(path.read_text()) != value:
            raise ContractError("retained context original/provenance changed")
        if not path.exists():
            retained.SourceMemberClaim._write(path, value)
    repository = Path(config["repo_cache"]) / task["repo"].replace("/", "__")
    services = retained.ExecutionServices({**config, "out_dir": str(output)})
    with retained.source_judge_job(services, task, recipe, repository, "judge-context-admission"):
        prompt = pinned_prompt(bundle, selected, task, original, repository)
        recheck_inputs(bundle)
        members = []
        for model, claim in zip(selected["judge_models"], claims):
            # Recheck the complete immutable inputs before each sequential call.
            validate_original(bundle, selected, original, task=task)
            recheck_inputs(bundle)
            live_guard()
            member = claim.invoke_once(model, prompt, bundle["configs"]["admission"], invoke)
            if "passed" in member:
                retained.validate_member(member, bundle["configs"]["admission"])
            members.append(member)
    record = record_from_members(bundle, selected, original, output, members)
    validate_cached_claims(bundle, selected, original, record, output)
    recheck_inputs(bundle)
    write_record(output, record)
    return record


def output_directory(output, selected):
    return (Path(output) / "repairs" / component(selected["model"]) / component(selected["id"])
            / selected["generation_sha256"][:16])


def validate_base(bundle, base, tasks):
    base = Path(base).resolve()
    expected = Path(bundle["paths"]["base_run"]).resolve()
    manifest = checked_json(base / "experiment-manifest.json", bundle["paths"]["base_manifest_sha256"])
    profile = retained.context_profile(bundle["override"]["input_hashes"])
    if base != expected or bundle["paths"]["base_manifest_sha256"] != profile["base_manifest_sha256"]:
        label = "exact latest V10 base" if profile["name"] == "original-thirteen" else "exact sealed context base"
        raise ContractError("context admission must use the " + label + ", never an ancestor")
    binding = retained.base_engine_binding(base, manifest, tasks, manifest["models"])
    if (len(tasks) != 402 or manifest["models"] != ["gpt-5.4-responses"]
            or manifest["generation_engine_fingerprint"] != ENGINE):
        raise ContractError("context admission requires original full402/one solver/engine5142")
    return manifest, binding


def publish(output, bundle, tasks, models, base_rows, repairs, manifest, errors, *, owner=None):
    owner = Path(owner) if owner is not None else Path(output)
    for selected in bundle["selections"]:
        pair = selected["id"], selected["model"]
        validate_original(bundle, selected, base_rows[pair], task=tasks[pair[0]])
        if pair in repairs:
            validate_cached_claims(bundle, selected, base_rows[pair], repairs[pair],
                                   output_directory(owner, selected))
    report = retained.publish(output, tasks, models, base_rows, repairs, manifest, errors)
    report.update(schema_version=SCHEMA, context_admission=manifest["context_admission"],
                  validation_only=manifest["validation_only"])
    atomic_json(Path(output) / "report.json", report)
    return report


def controller_manifest(bundle, config_file, base, output, validation_only):
    """Bind immutable inputs once. Volatile full-table files belong to each epoch."""
    config_file, base, output = Path(config_file).resolve(), Path(base).resolve(), Path(output).resolve()
    config = retained.load_config(config_file)
    if config["judge"] != bundle["configs"]["original"]:
        raise ContractError("original live config must remain unchanged; cap override is separate")
    if retained.engine_identity()["fingerprint"] != ENGINE:
        raise ContractError("context publication requires frozen engine5142")
    tasks = retained.selected_tasks(Path(config["tasks"]), Path(config["scope_ids"]))
    source = Path(bundle["paths"]["source_run"]).resolve()
    if any(root == output or root.is_relative_to(output) or output.is_relative_to(root) for root in (base, source)):
        raise ContractError("context recovery output must be independent of original source/base")
    base_manifest, binding = validate_base(bundle, base, tasks)
    if (Path(base_manifest["source_run"]).resolve() != source
            or json.loads((source / "experiment-config.json").read_text())["judge"] != bundle["configs"]["original"]):
        raise ContractError("context admission changed the original source/grading configuration")
    immutable = [config_file, Path(config["tasks"]).resolve(), Path(config["scope_ids"]).resolve(),
                 source / "experiment-config.json", Path(__file__).resolve(), Path(retained.__file__).resolve()]
    bound = {str(p): file_hash(p) for p in immutable}
    bound.update({str(Path(bundle["directory"]) / name): digest for name, digest in bundle["hashes"].items()})
    # Preserve hashes read during the actual lineage walk, not refreshed replacements.
    bound[str(base / "experiment-manifest.json")] = bundle["paths"]["base_manifest_sha256"]
    bound[str(source / "experiment-manifest.json")] = base_manifest["source_manifest_sha256"]
    for field in ("bootstrap_lineage", "recovery_lineage"):
        for row in binding.get(field, []):
            bound[row["manifest"]] = row["sha256"]
    manifest = {
        "schema_version": SCHEMA, "base_run": str(base), "source_run": str(source),
        "base_manifest_sha256": bundle["paths"]["base_manifest_sha256"],
        "source_manifest_sha256": base_manifest["source_manifest_sha256"],
        "task_ids": list(tasks), "tasks_fingerprint": canonical_hash(tasks), "models": base_manifest["models"],
        "expected_pairs": 402, "selection_sha256": bundle["hashes"]["selection.json"],
        "context_admission": bundle["override"], "judge_fingerprint": bundle["override"]["admitted_judge_fingerprint"],
        "controller_sha256": file_hash(Path(__file__)), "retained_helper_sha256": file_hash(Path(retained.__file__)),
        "controller_file": str(Path(__file__).resolve()), "retained_helper_file": str(Path(retained.__file__).resolve()),
        "input_bundle": bundle["directory"], "original_config": str(config_file),
        "tasks_file": str(Path(config["tasks"]).resolve()), "scope_ids_file": str(Path(config["scope_ids"]).resolve()),
        "bound_inputs": bound, "validation_only": validation_only,
        "publication_policy": retained.context_publication_policy(output, bundle["override"]),
        "additional_generation_calls": 0, "additional_correctness_calls": 0, **binding,
    }
    recheck_manifest(bundle, manifest)
    return manifest, tasks, config


def recheck_manifest(bundle, manifest):
    recheck_inputs(bundle)
    retained.validate_context_publication_policy(manifest, Path(manifest["publication_policy"]["owner_output"]))


def check_owner_manifest(output, manifest, *, required=False):
    path = Path(output) / "experiment-manifest.json"
    if path.is_symlink():
        raise ContractError("context owner manifest must not be a symlink")
    if path.exists():
        if json.loads(path.read_text()) != manifest:
            raise ContractError("context output immutable bindings/owner changed")
    elif required:
        raise ContractError("context child requires the original owner publication manifest")
    else:
        retained.SourceMemberClaim._write(path, manifest)


def stat_version(stat):
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def read_snapshot_files(base):
    data, versions = {}, {}
    for name in FILES:
        path = Path(base) / name
        with path.open("rb") as stream:
            before = stat_version(os.fstat(stream.fileno()))
            data[name] = stream.read()
            after = stat_version(os.fstat(stream.fileno()))
        if before != after or after != stat_version(path.stat()):
            raise SnapshotUnavailable("base file changed during read: " + name)
        versions[name] = after
    return data, versions


def validate_selected_rows(bundle, tasks, rows):
    for selected in bundle["selections"]:
        pair = selected["id"], selected["model"]
        if pair not in rows:
            raise ContractError("original selected context row disappeared")
        validate_original(bundle, selected, rows[pair], task=tasks[pair[0]])


def rows_hash(rows):
    return canonical_hash([rows[pair] for pair in sorted(rows)])


def coherent_snapshot(bundle, base, tasks, models, *, attempts=3):
    """Two equal reads plus recomputed full402 table/report; never mix columns."""
    last = "no coherent base snapshot"
    for _ in range(attempts):
        try:
            first, first_versions = read_snapshot_files(base)
            raw, versions = read_snapshot_files(base)
            if first != raw or first_versions != versions:
                raise SnapshotUnavailable("base changed between complete snapshot reads")
            rows = retained.measurement_rows(raw, tasks, models)
            # A selected change is terminal, not a polling/automatic retry event.
            validate_selected_rows(bundle, tasks, rows)
            try:
                _, report = retained.checked_measurement_report(raw, tasks, models)
            except ContractError as exc:
                raise SnapshotUnavailable(str(exc)) from exc
            return {
                "rows": rows, "report": report,
                "receipt": {"base_run": str(Path(base).resolve()), "captured_at_utc": utc_now(),
                            "file_sha256": {name: hashlib.sha256(value).hexdigest() for name, value in raw.items()},
                            "file_versions": versions, "rows_sha256": rows_hash(rows)},
            }
        except (SnapshotUnavailable, FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            last = str(exc)
    raise SnapshotUnavailable(last)


def collect_cached(bundle, snapshot, output, *, validation_only=False):
    repairs, pending, consumed = {}, {}, 0
    if validation_only:
        return repairs, pending, consumed
    for selected in bundle["selections"]:
        pair = selected["id"], selected["model"]
        try:
            record, count = cached_record(bundle, selected, snapshot["rows"][pair], output_directory(output, selected))
            consumed += count
            if record is not None:
                repairs[pair] = record
            else:
                pending["::".join(pair)] = {"state": "unstarted", "consumed_members": count}
        except CachePending as exc:
            pending["::".join(pair)] = {"state": "in-flight", "reason": str(exc), "consumed_members": None}
    return repairs, pending, consumed


def fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publication_aliases(output):
    epochs = output / "publications"
    if epochs.is_symlink():
        raise ContractError("context publications must be owner-local")
    epochs.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        path = output / name
        if path.exists() or path.is_symlink():
            if not path.is_symlink() or os.readlink(path) != "current/" + name:
                raise ContractError("context root files cannot replace another publication layout")
        else:
            path.symlink_to("current/" + name)
    fsync_directory(output)
    return epochs


def switch_publication(output, key):
    pointer = output / "current"
    if pointer.exists() or pointer.is_symlink():
        if not pointer.is_symlink():
            raise ContractError("context current pointer is not an owned symlink")
        parts = Path(os.readlink(pointer)).parts
        if len(parts) != 2 or parts[0] != "publications" or not retained.HEX256.fullmatch(parts[1]):
            raise ContractError("context current pointer has an unknown owner/target")
    temporary = output / (".current-" + str(os.getpid()) + "-" + uuid.uuid4().hex)
    temporary.symlink_to("publications/" + key, target_is_directory=True)
    os.replace(temporary, pointer)
    fsync_directory(output)


def publish_snapshot(output, bundle, tasks, snapshot, repairs, manifest, errors, pending, activity, consumed):
    output = Path(output)
    recheck_manifest(bundle, manifest)
    check_owner_manifest(output, manifest, required=True)
    validate_selected_rows(bundle, tasks, snapshot["rows"])
    epochs = publication_aliases(output)
    inputs = {
        "manifest_sha256": file_hash(output / "experiment-manifest.json"),
        "base_rows_sha256": rows_hash(snapshot["rows"]),
        "repairs_sha256": canonical_hash([repairs[pair] for pair in sorted(repairs)]),
        "errors": errors, "pending": pending, "activity": activity, "historical_consumed": consumed,
    }
    key = None
    if (output / "current").is_symlink():
        _, _, previous = retained.read_context_publication(output, manifest, tasks, manifest["models"])
        if previous["identity"]["inputs"] == inputs:
            key = previous["publication_key"]
    if key is None:
        stage = epochs / (".stage-" + str(os.getpid()) + "-" + uuid.uuid4().hex)
        stage.mkdir()
        report = publish(stage, bundle, tasks, manifest["models"], snapshot["rows"], repairs, manifest,
                         dict(errors), owner=output)
        report.update(publication_policy=manifest["publication_policy"], publication_inputs_sha256=canonical_hash(inputs),
                      historical_first_attempts_consumed=consumed,
                      historical_count_complete=all(p["consumed_members"] is not None for p in pending.values()),
                      cache_pending=pending, activity=activity,
                      additional_judge_calls=activity["new_first_attempt_intents"])
        profile = retained.context_profile(bundle["override"]["input_hashes"])
        if "inherited_context_first_attempts" in profile:
            inherited = snapshot["report"].get("all_context_first_attempts_consumed",
                                                snapshot["report"].get("historical_first_attempts_consumed"))
            if inherited != profile["inherited_context_first_attempts"]:
                raise ContractError("upstream context first-attempt history changed or disappeared")
            report.update(
                historical_first_attempts_scope="current_owner",
                inherited_context_first_attempts_consumed=inherited,
                all_context_first_attempts_consumed=None if consumed is None else inherited + consumed,
            )
        atomic_json(stage / "report.json", report)
        output_hashes = {name: file_hash(stage / name) for name in FILES}
        identity = {
            "inputs": inputs, "base_snapshot": {k: v for k, v in snapshot["receipt"].items() if k != "captured_at_utc"},
            "output_hashes": output_hashes,
        }
        key = canonical_hash(identity)
        destination = epochs / key
        receipt = {
            "schema_version": retained.CONTEXT_PUBLICATION_RECEIPT, "publication_key": key,
            "owner_output": str(output.resolve()), "manifest_sha256": inputs["manifest_sha256"],
            "identity": identity, "base_snapshot": snapshot["receipt"],
            "output_hashes": output_hashes,
        }
        retained.SourceMemberClaim._write(stage / "snapshot.json", receipt)
        fsync_directory(stage)
        if destination.exists():
            _, _, existing = retained.read_context_publication(output, manifest, tasks, manifest["models"], epoch=key)
            if existing["identity"] != identity:
                raise ContractError("interrupted context epoch differs from the same content key")
            for name in (*FILES, "snapshot.json"):
                (stage / name).unlink()
            stage.rmdir()
        else:
            os.rename(stage, destination)
        fsync_directory(epochs)
    _, report, receipt = retained.read_context_publication(output, manifest, tasks, manifest["models"], epoch=key)
    if receipt["identity"]["inputs"] != inputs:
        raise ContractError("existing context epoch has different publication inputs")
    # Validate the live selected originals again, but do not reject unrelated
    # progress since the coherent as-of snapshot. A later poll propagates it.
    coherent_snapshot(bundle, Path(manifest["base_run"]), tasks, manifest["models"])
    recheck_manifest(bundle, manifest)
    check_owner_manifest(output, manifest, required=True)
    for selected in bundle["selections"]:
        pair = selected["id"], selected["model"]
        if pair in repairs:
            validate_cached_claims(bundle, selected, snapshot["rows"][pair], repairs[pair],
                                   output_directory(output, selected))
    switch_publication(output, key)
    return report


def zero_activity(mode):
    return {"mode": mode, "child_dispatches": 0, "new_first_attempt_intents": 0,
            "generation_calls": 0, "correctness_calls": 0, "source_jobs": 0}


def cached_iteration(bundle, manifest, tasks, output):
    """Watch calls this only: no recipes, requests, services, jobs or invocation."""
    recheck_manifest(bundle, manifest)
    check_owner_manifest(output, manifest, required=True)
    snapshot = coherent_snapshot(bundle, Path(manifest["base_run"]), tasks, manifest["models"])
    repairs, pending, consumed = collect_cached(bundle, snapshot, output, validation_only=manifest["validation_only"])
    activity = zero_activity("validate-only" if manifest["validation_only"] else "cached-only")
    report = publish_snapshot(output, bundle, tasks, snapshot, repairs, manifest, {}, pending, activity, consumed)
    atomic_json(Path(output) / "CURRENT.json", {
        "updated_at_utc": utc_now(), "status": "published", "publication_key": report["publication_key"],
        "base_snapshot_observed": snapshot["receipt"], "activity": activity, "cache_pending": pending,
    })
    return report


def admit_once(bundle, manifest, tasks, config, output, workers):
    recheck_manifest(bundle, manifest)
    snapshot = coherent_snapshot(bundle, Path(manifest["base_run"]), tasks, manifest["models"])
    repairs, pending, before = collect_cached(bundle, snapshot, output)
    history_known_before = all(p["consumed_members"] is not None for p in pending.values())
    selected_pending = [selected for selected in bundle["selections"]
                        if pending.get(selected["id"] + "::" + selected["model"], {}).get("state") == "unstarted"]
    recipes = retained.load_recipes(Path(manifest["source_run"]) / "fixed-runtime-recipes.jsonl") if selected_pending else {}
    errors, dispatched = {}, []
    def dispatch(selected):
        recheck_manifest(bundle, manifest)
        current = coherent_snapshot(bundle, Path(manifest["base_run"]), tasks, manifest["models"])
        pair = selected["id"], selected["model"]
        directory = output_directory(output, selected)
        request_file = directory / "request.json"
        request = {"input_bundle": bundle["directory"], "config": manifest["original_config"],
                   "task": tasks[pair[0]], "row": current["rows"][pair], "selection": selected,
                   "recipe": asdict(recipes[pair[0]]), "output": str(directory)}
        if request_file.exists() and json.loads(request_file.read_text()) != request:
            raise ContractError("retained context child request changed")
        if not request_file.exists():
            retained.SourceMemberClaim._write(request_file, request)
        command = [sys.executable, str(Path(__file__).resolve()), "--child-request", str(request_file)]
        engine_source = bundle["paths"]["engine_src"]
        with (directory / "child.log").open("a") as log:
            child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env={
                **os.environ, "REVIEWBENCH_ENGINE_SRC": engine_source, "PYTHONPATH": engine_source,
                "PYTHONDONTWRITEBYTECODE": "1"})
            dispatched.append(child.pid)
            atomic_json(directory / "child-process.json", {"pid": child.pid, "command": command, "started_at_utc": utc_now()})
            status = child.wait()  # Never restart for timeout, response errors, or an uncertain outcome.
            atomic_json(directory / "child-exit.json", {"pid": child.pid, "exit_status": status, "ended_at_utc": utc_now()})
        if status:
            raise ContractError("context child exit " + str(status) + "; retain its intents/logs")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [(selected, pool.submit(dispatch, selected)) for selected in selected_pending]
        for selected, future in futures:
            try:
                future.result()
            except (ContractError, OSError, ValueError, KeyError, TypeError) as exc:
                errors[selected["id"] + "::" + selected["model"]] = type(exc).__name__ + ": " + str(exc)
    recheck_manifest(bundle, manifest)
    snapshot = coherent_snapshot(bundle, Path(manifest["base_run"]), tasks, manifest["models"])
    repairs, pending, consumed = collect_cached(bundle, snapshot, output)
    activity = zero_activity("initial-admission")
    history_known_after = all(p["consumed_members"] is not None for p in pending.values())
    activity.update(child_dispatches=len(dispatched),
                    new_first_attempt_intents=max(0, consumed - before) if history_known_before and history_known_after else None,
                    source_jobs=None if dispatched else 0)
    return publish_snapshot(output, bundle, tasks, snapshot, repairs, manifest, errors, pending, activity, consumed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-bundle", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--base-run", type=Path)
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--workers", type=int, choices=(1, 2), default=1)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--cached-only", action="store_true")
    parser.add_argument("--watch", action="store_true", help="cached-only from the first iteration; never admits members")
    parser.add_argument("--poll-seconds", type=float, default=30)
    parser.add_argument("--child-request", type=Path)
    args = parser.parse_args()
    if args.child_request:
        if args.watch or args.cached_only or args.validate_only:
            parser.error("cache/validation flags cannot dispatch a child")
        result = recover_one(json.loads(args.child_request.read_text()))
        print(json.dumps({"id": result["row"]["id"], "judgment": result["row"]["judgment"]["status"],
                          "first_attempts_consumed": result["judge_request_attempts"]}), flush=True)
        return 0
    if (any(x is None for x in (args.input_bundle, args.config, args.base_run, args.out_dir))
            or not 1 <= args.poll_seconds <= 86400):
        parser.error("sealed inputs, original config, exact V10, independent output and bounded polling are required")
    bundle = load_inputs(args.input_bundle)
    output = args.out_dir.resolve()
    manifest, tasks, config = controller_manifest(bundle, args.config, args.base_run, output, args.validate_only)
    with retained.output_lock(output):
        check_owner_manifest(output, manifest)
        while True:
            try:
                if args.watch or args.cached_only or args.validate_only:
                    report = cached_iteration(bundle, manifest, tasks, output)
                else:
                    report = admit_once(bundle, manifest, tasks, config, output, args.workers)
                print(json.dumps(report, indent=2), flush=True)
                if not args.watch:
                    return 0 if report["complete"] else 2
            except (SnapshotUnavailable, CachePending) as exc:
                atomic_json(output / "CURRENT.json", {"updated_at_utc": utc_now(), "status": "waiting-for-consistency",
                                                     "reason": str(exc), "activity": zero_activity("cached-only")})
                if not args.watch:
                    raise
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
