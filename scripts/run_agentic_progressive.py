#!/usr/bin/env python3
"""Execute ready tasks while retaining the complete, immutable task/model denominator."""
from __future__ import annotations

import argparse
from collections import Counter
import copy
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict
import json
import math
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, os.environ.get("REVIEWBENCH_ENGINE_SRC", str(Path(__file__).resolve().parents[1]/"src")))
from reviewbench_maintainability.agentic import PROTOCOL
from reviewbench_maintainability.agentic.cli import load_config
from reviewbench_maintainability.agentic.contracts import (
    ContractError, InfrastructureError, atomic_json, canonical_hash, component, file_hash, read_jsonl,
    selected_tasks, write_jsonl,
)
from reviewbench_maintainability.agentic.environment import validate_assets
from reviewbench_maintainability.agentic.evaluation import correctness, judge_candidate
from reviewbench_maintainability.agentic.execution import ExecutionServices, source_identity
from reviewbench_maintainability.agentic.models import engine_identity, sanitize_config
from reviewbench_maintainability.agentic.preparation import load_recipes, verify_recipe
from reviewbench_maintainability.agentic.reporting import make_report
from reviewbench_maintainability.agentic.retention import archive_episode
from reviewbench_maintainability.agentic.runner import run_episode
from reviewbench_maintainability.agentic.scheduling import output_lock


def proof_matches(proof, task, oracle, recipe, engine=None):
    basic = (proof.get("status") == "ready"
            and proof.get("task_fingerprint") == canonical_hash(task)
            and proof.get("runtime_fingerprint") == recipe.fingerprint
            and (engine is None or proof.get("engine_fingerprint") == engine))
    if not basic:
        return False
    if ("original_solr_composition_inputs" in getattr(recipe, "grading", {})
            or "original_solr_resource_composition" in proof):
        from agentic_runtime_migration.solr_resource_composition import proof_identity_matches as composed_matches
        return composed_matches(proof, task, oracle, recipe)
    from agentic_runtime_migration.solr_hibernate_identity import (
        CASES as original_gradle_cases, PROFILE_KEY as original_gradle_profile,
        BRIDGE_KEY as original_gradle_bridge, proof_identity_matches as original_gradle_proof_matches,
    )
    if (task.get("id") in original_gradle_cases
            or original_gradle_profile in getattr(recipe, "grading", {})
            or original_gradle_bridge in proof):
        return original_gradle_proof_matches(proof, task, oracle, recipe)
    if ("original_public_gradle_profile" in getattr(recipe, "grading", {})
            or "original_public_gradle_identity_bridge" in proof):
        from agentic_runtime_migration.public_gradle_identity import proof_identity_matches
        return proof_identity_matches(proof, task, oracle, recipe)
    if ("original_legacy_profile" in getattr(recipe, "grading", {})
            or "original_oracle_identity_bridge" in proof):
        from agentic_runtime_migration.legacy_oracle_identity import proof_identity_matches
        return proof_identity_matches(proof, task, oracle, recipe)
    return proof.get("oracle_fingerprint") == canonical_hash(oracle)


def package_candidates(packages, tasks, oracles):
    """Only previously executed-successful recipes are eligible for admission.

    Older-engine proofs license a fresh verification, never direct inference.
    A recipe becomes frozen for this experiment before its first model call.
    """
    candidates = {}
    for package in packages:
        recipe_file = package/"runtime-recipes.jsonl"
        if not recipe_file.is_file():
            continue
        recipes = load_recipes(recipe_file)
        for ident, recipe in recipes.items():
            if ident not in tasks:
                continue
            proof_file = package/"execution/preparation/verified"/component(ident)/"verification.json"
            if not proof_file.is_file():
                continue
            proof = json.loads(proof_file.read_text())
            if not proof_matches(proof, tasks[ident], oracles[ident], recipe):
                continue
            old = candidates.get(ident)
            if old and old[0].fingerprint != recipe.fingerprint:
                raise ContractError("conflicting successful runtime recipes: "+ident)
            candidates[ident] = (recipe, proof, proof_file)
    return candidates


def experiment_manifest(config, tasks, models):
    behavior = {key: config.get(key) for key in ("protocol", "agent", "budget", "observation", "judge")}
    behavior["models"] = {key: config["models"][key] for key in models}
    return {"schema_version": "reviewbench-progressive-experiment/v1", "protocol": PROTOCOL,
            "task_ids": list(tasks), "tasks_fingerprint": canonical_hash(tasks),
            "models": models, "expected_pairs": len(tasks)*len(models),
            "behavior_fingerprint": canonical_hash(behavior),
            "engine_fingerprint": engine_identity()["fingerprint"], "source_identity": source_identity(config)}


def format_equivalent_attestation_update(old, new, models):
    """Allow only spelling-equivalent response aliases, not another model/date.

    Some gateways spell a model's version separator '-' instead of '.'. The
    request model, budget, prompts and judge configuration must stay identical.
    """
    left, right = copy.deepcopy(old), copy.deepcopy(new)
    for config in (left, right):
        config.pop("out_dir", None)
        for model in models:
            values = config["models"][model].get("expected_response_models", [])
            config["models"][model]["expected_response_models"] = sorted({v.replace(".", "-") for v in values})
    return left == right


def pair_complete(row):
    generation = row.get("generation", {})
    if generation.get("status") == "generation_failed":
        return True
    return (generation.get("status") == "generated"
            and row.get("correctness", {}).get("status") in {"pass", "fail", "apply_error", "scope_error"}
            and row.get("judgment", {}).get("status") == "completed")


def admission_slot_available(pending_count, heavy_count, workers, heavy_workers, heavy):
    return pending_count < workers and (not heavy or heavy_count < heavy_workers)


ADMISSION_TIMEOUT_PREFIX = "InfrastructureError: resource admission timed out: "
ADMISSION_TIMEOUT_REASONS = {
    "task_slots", "heavy_slots", "cpus_reservation", "memory_bytes_reservation",
    "scratch_bytes_reservation", "host_disk_headroom", "host_memory_headroom", "host_cpu_pressure",
}
ADMISSION_ONLY_FIELDS = {
    "id", "model", "experiment_fingerprint", "runtime_fingerprint", "status", "error",
    "generation_started", "generation_admission_status", "generation_admission_failure_at",
    "admission_failure_history", "verification_failure_history",
}


def generation_admission_retryable(row, root, runtime_fingerprint):
    """Prove a capacity timeout preceded every episode/model call.

    The old controller persisted generation_started before entering the gate.
    Accept that legacy marker only with the exact gate error, the same runtime,
    no result/recovery fields, and a directory containing only pair-result.json.
    Even an empty episode directory or an unknown field makes this inconclusive.
    """
    if (not row or set(row) - ADMISSION_ONLY_FIELDS or row.get("status") != "incomplete"
            or row.get("runtime_fingerprint") != runtime_fingerprint
            or row.get("generation_admission_status") not in {None, "timed_out"}
            or row.get("error") not in {ADMISSION_TIMEOUT_PREFIX + reason for reason in ADMISSION_TIMEOUT_REASONS}):
        return False
    if row.get("generation_admission_status") == "timed_out" and row.get("generation_started") is not False:
        return False
    if root.is_symlink() or not root.is_dir():
        return False
    children = list(root.iterdir())
    result = root / "pair-result.json"
    return (children == [result] and result.is_file() and not result.is_symlink()
            and json.loads(result.read_text()) == row)


VERIFICATION_FAILURE_PREFIX = "ContractError: current-engine runtime verification failed: "
CALIBRATION_CONTINUATION_SCHEMA = "reviewbench-append-only-calibration-continuation/v1"


def verification_admission_retryable(row, root, task, oracle, recipe, proof, engine):
    """Only a still-zero-call pair may consume an audited same-runtime continuation.

    A ready flag by itself is insufficient. The prior blocked proof must still
    be the actual current proof, all successful old slots must be unchanged,
    and exactly the previously unsuccessful/missing slots must have new records.
    Even an empty episode directory, unknown field, or generation marker closes
    this route. No generation, final correctness, or panel retry is authorized.
    """
    if (not row or set(row) - ADMISSION_ONLY_FIELDS
            or row.get("id") != task["id"] or row.get("status") != "incomplete"
            or row.get("runtime_fingerprint") != recipe.fingerprint
            or row.get("generation_started") is not None and row.get("generation_started") is not False
            or row.get("generation_admission_status") is not None
            or not isinstance(row.get("error"), str)
            or not row["error"].startswith(VERIFICATION_FAILURE_PREFIX)
            or not proof_matches(proof, task, oracle, recipe, engine)
            or root.is_symlink() or not root.is_dir()):
        return False
    pair_file = root / "pair-result.json"
    if list(root.iterdir()) != [pair_file] or not pair_file.is_file() or pair_file.is_symlink():
        return False
    try:
        if json.loads(pair_file.read_text()) != row:
            return False
        continuation = proof["calibration_continuation"]
        if (continuation.get("schema_version") != CALIBRATION_CONTINUATION_SCHEMA
                or continuation.get("successful_calibration_replays") != 0
                or continuation.get("old_failed_attempts_preserved") is not True
                or continuation.get("runtime_and_execution_engine_unchanged") is not True):
            return False
        prior_file = root.parents[2] / "preparation/verified" / component(task["id"]) / "verification.json"
        reference = continuation["original_failed_proof"]
        referenced_file = Path(reference["path"])
        if (set(reference) != {"path", "sha256"} or not referenced_file.is_absolute()
                or referenced_file.is_symlink() or not referenced_file.is_file()
                or prior_file.is_symlink() or not prior_file.is_file()
                or file_hash(prior_file) != reference["sha256"]
                or file_hash(referenced_file) != reference["sha256"]):
            return False
        prior = json.loads(prior_file.read_text())
        if (prior.get("status") != "blocked"
                or row["error"] != VERIFICATION_FAILURE_PREFIX + str(prior.get("error"))
                or any(prior.get(key) != proof.get(key) for key in (
                    "id", "task_fingerprint", "oracle_fingerprint", "runtime_fingerprint", "engine_fingerprint"))):
            return False
        slots = [(variant, number) for variant in ("start", "golden") for number in range(3)]
        expected = {f"{index}::{name}": True for index, spec in enumerate(recipe.grading["commands"])
                    for name in spec["expected_tests"]}
        if not expected:
            return False
        current = {}
        for item in proof["runs"]:
            key = item["variant"], item["repetition"]
            if (type(item["repetition"]) is not int or key not in slots or key in current
                    or item["tests"] != expected
                    or any(type(value) is not bool for value in item["tests"].values())):
                return False
            current[key] = item
        if set(current) != set(slots):
            return False
        old_slots, retained = [], {}
        for item in prior["runs"]:
            key = item["variant"], item["repetition"]
            if type(item["repetition"]) is not int or key in old_slots:
                return False
            old_slots.append(key)
            if not isinstance(item["tests"], dict) or any(type(v) is not bool for v in item["tests"].values()):
                return False
            if item["tests"] and all(item["tests"].values()):
                if item != current.get(key):
                    return False
                retained[key] = item
        needed = set(slots) - set(retained)
        if (old_slots != slots[:len(old_slots)] or not needed
                or continuation["retained_successful_slots"] != [list(key) for key in slots if key in retained]
                or continuation["new_execution_slots"] != [list(key) for key in slots if key in needed]
                or continuation["new_failed_slot_executions"] != len(needed)
                or len(continuation["new_execution_records"]) != len(needed)):
            return False
        from agentic_runtime_migration.calibration_continuation import checked, compose_readiness
        if referenced_file.name != "verification.json":
            return False
        fresh = [checked(reference) for reference in continuation["new_execution_records"]]
        reconstructed = compose_readiness(
            task, oracle, recipe, referenced_file.parent, fresh, engine)
        # A source tree may be relocated to its sealed controller directory.
        # Its code bytes, not its deployment pathname, define this reader.
        given, actual = copy.deepcopy(proof), copy.deepcopy(reconstructed)
        for value in (given, actual):
            reader = value["calibration_continuation"]["reader"]
            value["calibration_continuation"]["reader"] = {"sha256": reader["sha256"]}
        return canonical_hash(given) == canonical_hash(actual)
    except (ContractError, OSError, ValueError, KeyError, TypeError, IndexError):
        return False


def checked_verification_history(history, output, model, ident):
    if not isinstance(history, list):
        raise ContractError("verification failure history is not a list")
    boundary = (output / "verification-admission-failures" / component(model) / component(ident)).resolve()
    for entry in history:
        if not isinstance(entry, dict) or set(entry) != {"pair", "proof"}:
            raise ContractError("verification failure history has unexpected fields")
        for reference in entry.values():
            path = Path(reference["path"])
            if (set(reference) != {"path", "sha256"} or path.is_symlink() or not path.is_file()
                    or not path.resolve().is_relative_to(boundary) or file_hash(path) != reference["sha256"]):
                raise ContractError("original verification failure history changed")
    return copy.deepcopy(history)


def preserve_verification_failure(row, root, output, task, oracle, recipe, proof, engine):
    if not verification_admission_retryable(row, root, task, oracle, recipe, proof, engine):
        raise ContractError("zero-call verification continuation evidence changed before admission")
    history = checked_verification_history(row.get("verification_failure_history", []),
                                           output, row["model"], row["id"])
    sources = {
        "pair": root / "pair-result.json",
        "proof": output / "preparation/verified" / component(task["id"]) / "verification.json",
    }
    digest = canonical_hash({name: file_hash(path) for name, path in sources.items()})
    destination = output / "verification-admission-failures" / component(row["model"]) / component(row["id"]) / digest
    destination.mkdir(parents=True, exist_ok=True)
    retained = {}
    for name, source in sources.items():
        target = destination / (name + ".json")
        if target.is_symlink():
            raise ContractError("verification failure archive became a symlink")
        if not target.exists():
            with target.open("xb") as stream:
                stream.write(source.read_bytes())
                stream.flush()
                os.fsync(stream.fileno())
            target.chmod(source.stat().st_mode & 0o777)
        if target.read_bytes() != source.read_bytes():
            raise ContractError("retained verification failure differs from the original")
        retained[name] = {"path": str(target.resolve()), "sha256": file_hash(target)}
    return [*history, retained]


def admission_retry_at(row, poll_seconds):
    # Back off repeated capacity failures, independently of the agent budget.
    # Legacy timeouts have no timestamp and may be queued immediately.
    failed_at = row.get("generation_admission_failure_at", 0)
    if not isinstance(failed_at, (int, float)) or not math.isfinite(failed_at) or failed_at < 0:
        raise ContractError("invalid generation admission failure timestamp")
    if not failed_at:
        return 0
    delay = min(300.0, max(1.0, poll_seconds) * 2 ** min(len(row.get("admission_failure_history", [])), 8))
    return failed_at + delay


def preserve_admission_failure(row, root, output):
    """Retain the exact old row before a retry replaces its pair checkpoint."""
    if not generation_admission_retryable(row, root, row.get("runtime_fingerprint")):
        raise ContractError("pre-generation admission evidence changed before retry")
    history = copy.deepcopy(row.get("admission_failure_history", []))
    for record in history:
        path = Path(record["path"])
        archive_root = output / "admission-failures" / component(row["model"]) / component(row["id"])
        if (path.is_symlink() or not path.resolve().is_relative_to(archive_root.resolve())
                or not path.is_file() or file_hash(path) != record["sha256"]):
            raise ContractError("previous admission failure evidence is missing or changed")
    source = root / "pair-result.json"
    digest = file_hash(source)
    target = output / "admission-failures" / component(row["model"]) / component(row["id"]) / (digest + ".json")
    if target.is_symlink():
        raise ContractError("admission failure archive became a symlink")
    if target.exists():
        if file_hash(target) != digest:
            raise ContractError("admission failure archive changed")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation preserves old bytes, including their formatting.
        with target.open("xb") as stream:
            stream.write(source.read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
    if file_hash(target) != digest or json.loads(target.read_text()) != row:
        raise ContractError("admission failure archive differs from the previous checkpoint")
    return [*history, {"path": str(target), "sha256": digest}]


def completed_answer_needs_host_recovery(generation):
    """A submitted answer is not an invitation to resample after a host error."""
    if generation.get("status") != "infra_error" or not generation.get("trace_file"):
        return False
    path = Path(generation["trace_file"])
    if not generation.get("trace_sha256") and not path.exists():
        # Environment/bootstrap failures can precede construction of an agent.
        # A planned trajectory pathname is not evidence of a completed answer.
        return False
    if not path.is_file() or file_hash(path) != generation.get("trace_sha256"):
        raise ContractError("infrastructure attempt trajectory is missing or changed")
    trace = json.loads(path.read_text())
    for key in ("id", "model", "task_fingerprint", "runtime_fingerprint", "engine_fingerprint"):
        if trace.get("bindings", {}).get(key) != generation.get(key):
            raise ContractError("infrastructure trajectory belongs to another episode")
    messages = trace.get("messages", [])
    return bool(messages and messages[-1].get("role") == "exit"
                and messages[-1].get("extra", {}).get("exit_status") in {
                    "Submitted", "LimitsExceeded", "TimeExceeded", "RepeatedFormatError"}
                and not trace.get("inflight") and not trace.get("pending_model_request"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--package", type=Path, action="append", required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--model", action="append", required=True)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--heavy-workers", type=int,
                        help="limit heavy admissions so waiting builds do not occupy light-worker slots")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=30)
    parser.add_argument("--retry-infra", action="store_true")
    parser.add_argument("--retry-admission-only", action="store_true",
                        help="retain observed pairs except proven zero-call capacity or audited verification continuations; "
                             "requires --watch --retry-infra (measurement recovery stays separate)")
    args = parser.parse_args()
    if args.workers < 1:
        raise ContractError("workers must be positive")
    if not math.isfinite(args.poll_seconds) or args.poll_seconds <= 0:
        raise ContractError("poll seconds must be finite and positive")
    if args.retry_admission_only and not (args.watch and args.retry_infra):
        raise ContractError("admission-only continuation requires --watch --retry-infra")
    config = load_config(args.config)
    tasks = selected_tasks(Path(config["tasks"]), Path(config["scope_ids"]) if config.get("scope_ids") else None)
    oracles = read_jsonl(Path(config["oracles"]))
    models = list(dict.fromkeys(args.model))
    if len(models) != len(args.model) or any(m not in config["models"] for m in models):
        raise ContractError("models must be unique, explicitly configured experiment arms")
    output = args.out_dir.resolve()
    config = {**config, "out_dir": str(output)}
    services = ExecutionServices(config)
    heavy_workers = args.heavy_workers if args.heavy_workers is not None else min(
        args.workers, services.gate.limits.heavy_tasks)
    if heavy_workers < 1 or heavy_workers > min(args.workers, services.gate.limits.heavy_tasks):
        raise ContractError("heavy workers must fit the worker and global heavy-task limits")
    engine = engine_identity()["fingerprint"]
    matrix = [(ident, model) for ident in tasks for model in models]
    packages = [p.resolve() for p in args.package]
    with output_lock(output):
        manifest = experiment_manifest(config, tasks, models)
        manifest_file = output/"experiment-manifest.json"
        accepted_manifests = {canonical_hash(manifest)}
        history_file = output/"equivalent-attestation-history.json"
        history = json.loads(history_file.read_text()) if history_file.exists() else []
        accepted_manifests.update(row["previous_fingerprint"] for row in history)
        if manifest_file.exists() and json.loads(manifest_file.read_text()) != manifest:
            previous = json.loads(manifest_file.read_text())
            old_config = json.loads((output/"experiment-config.json").read_text())
            same = all(previous.get(k) == manifest[k] for k in manifest if k != "behavior_fingerprint")
            if not same or not format_equivalent_attestation_update(old_config, sanitize_config(config), models):
                raise ContractError("existing progressive experiment has different task/model/engine/configuration bindings")
            previous_fingerprint = canonical_hash(previous)
            accepted_manifests.add(previous_fingerprint)
            history.append({"previous_fingerprint": previous_fingerprint, "previous_manifest": previous,
                            "new_fingerprint": canonical_hash(manifest), "previous_config": old_config,
                            "reason": "response model spelling aliases only; identical model/date/request/budget/rubric"})
            atomic_json(history_file, history)
        atomic_json(manifest_file, manifest)
        atomic_json(output/"experiment-config.json", sanitize_config(config))
        package_file = output/"runtime-packages.json"
        if not package_file.exists():
            atomic_json(package_file, [str(p) for p in packages])
        results, fixed = {}, {}
        fixed_file = output/"fixed-runtime-recipes.jsonl"
        if fixed_file.exists():
            fixed = load_recipes(fixed_file)
        for ident, model in matrix:
            file = output/"pairs"/component(model)/component(ident)/"pair-result.json"
            if file.is_file():
                row = json.loads(file.read_text())
                if row["experiment_fingerprint"] not in accepted_manifests:
                    raise ContractError("pair result belongs to another experiment")
                results[(ident, model)] = row

        def save_status():
            generation, cs, js = [], [], []
            for pair in matrix:
                row = results.get(pair, {})
                if row.get("generation"):
                    g = row["generation"]
                    if g.get("patch_file") and file_hash(Path(g["patch_file"])) != g["patch_sha256"]:
                        raise ContractError("recorded generated patch changed")
                    generation.append(g)
                if row.get("correctness"):
                    cs.append(row["correctness"])
                if row.get("judgment"):
                    js.append(row["judgment"])
            rows, report = make_report(tasks, models, generation, cs, js)
            write_jsonl(output/"generation_results.jsonl", generation)
            write_jsonl(output/"correctness.jsonl", cs)
            write_jsonl(output/"judgments.jsonl", js)
            write_jsonl(output/"results.jsonl", rows)
            report["expected_pairs"] = len(matrix)
            report["scheduler"] = {"workers": args.workers, "heavy_workers": heavy_workers}
            report["runtime_bound_tasks"] = len(fixed)
            report["measured_generations"] = len(generation)
            report["observed_pair_jobs"] = len(results)
            report["completed_pair_jobs"] = sum(pair_complete(row) for row in results.values())
            report["generation_status_counts"] = dict(Counter(row["status"] for row in generation))
            report["answer_recovery_required"] = [ident+"::"+model for (ident,model),row in results.items()
                                                   if row.get("answer_recovery_required")]
            report["runtime_or_pipeline_errors"] = {i+"::"+m: r.get("error") for (i,m),r in results.items()
                                                   if r.get("error")}
            atomic_json(output/"report.json", report)
            atomic_json(output/"resource-usage.json", services.gate.status())
            return report

        def execute(pair, recipe, old_proof, old_path):
            ident, model = pair
            task, oracle = tasks[ident], oracles[ident]
            root = output/"pairs"/component(model)/component(ident)
            root.mkdir(parents=True, exist_ok=True)
            previous = results.get(pair, {})
            history = previous.get("admission_failure_history", [])
            verification_history = checked_verification_history(
                previous.get("verification_failure_history", []), output, model, ident)
            if generation_admission_retryable(previous, root, recipe.fingerprint):
                # Fail before replacing the old checkpoint if its evidence
                # cannot be preserved. No model call has been admitted yet.
                history = preserve_admission_failure(previous, root, output)
            elif verification_admission_retryable(previous, root, task, oracle, recipe, old_proof, engine):
                verification_history = preserve_verification_failure(
                    previous, root, output, task, oracle, recipe, old_proof, engine)
            row = {"id": ident, "model": model, "experiment_fingerprint": canonical_hash(manifest),
                   "runtime_fingerprint": recipe.fingerprint, "status": "incomplete"}
            if history:
                row["admission_failure_history"] = history
            if verification_history:
                row["verification_failure_history"] = verification_history
            repo = Path(config["repo_cache"])/task["repo"].replace("/", "__")
            try:
                proof_dir = output/"preparation/verified"/component(ident)
                current_proof_file = proof_dir/"verification.json"
                proof = json.loads(current_proof_file.read_text()) if current_proof_file.is_file() else {}
                if not proof_matches(proof, task, oracle, recipe, engine):
                    if proof_matches(old_proof, task, oracle, recipe, engine):
                        atomic_json(current_proof_file, old_proof)
                        atomic_json(proof_dir/"evidence-source.json",
                                    {"path": str(old_path), "sha256": file_hash(old_path)})
                        proof = old_proof
                    else:
                        with services.job(task, recipe, repo, "progressive-verify"):
                            if "original_legacy_profile" in recipe.grading:
                                from agentic_runtime_migration.legacy_oracle_identity import verify_original_schema
                                proof = verify_original_schema(
                                    task, recipe, oracle, repo, Path(config["asset_root"]), proof_dir,
                                    verify=verify_recipe)
                            else:
                                proof = verify_recipe(task, recipe, oracle, repo, Path(config["asset_root"]), proof_dir)
                    if not proof_matches(proof, task, oracle, recipe, engine):
                        raise ContractError("current-engine runtime verification failed: "+str(proof.get("error")))
                settings = {key: config.get(key, {}) for key in ("agent", "budget", "observation")}
                settings["model"] = {**config["models"][model], "id": model}
                episode = root/"episode"
                old_result_file = episode/"result.json"
                if old_result_file.is_file():
                    old_result = json.loads(old_result_file.read_text())
                    if (old_result.get("status") == "infra_error"
                            and old_result.get("config_fingerprint") != canonical_hash(settings)):
                        # Keep the original uncertain/infrastructure attempt.
                        # Only transport-attestation spelling was permitted to
                        # change above; completed model answers are never rerun.
                        episode = root/("episode-attestation-"+canonical_hash(settings)[:16])
                        row["previous_infrastructure_attempt"] = str(old_result_file)
                previous_generation = results.get(pair, {}).get("generation", {})
                if previous_generation.get("status") in {"generated", "generation_failed"}:
                    generation = previous_generation
                    if (generation.get("engine_fingerprint") != engine
                            or generation.get("runtime_fingerprint") != recipe.fingerprint):
                        raise ContractError("cached generation engine/runtime differs from admitted experiment")
                    row["reused_generation"] = True
                    row["generation_started"] = True
                elif completed_answer_needs_host_recovery(previous_generation):
                    row["generation"] = previous_generation
                    row["generation_started"] = True
                    row["answer_recovery_required"] = True
                    raise ContractError("completed model answer retained; repair host serialization without model resampling")
                else:
                    row["generation_started"] = False
                    row["generation_admission_status"] = "waiting"
                    atomic_json(root/"pair-result.json", row)
                    entered = False
                    try:
                        with services.job(task, recipe, repo, "progressive-run:"+model, episode=episode, resume=True):
                            entered = True
                            row["generation_started"] = True
                            row["generation_admission_status"] = "admitted"
                            atomic_json(root/"pair-result.json", row)
                            generation = run_episode(task, recipe, settings, repo, episode, Path(config["asset_root"]),
                                                     resume=True, retry_infra=args.retry_infra)
                            row["generation"] = generation
                            atomic_json(root/"pair-result.json", row)
                            if services.cache and config.get("storage", {}).get("archive_terminal"):
                                archive_episode(episode, generation, services)
                    except InfrastructureError as exc:
                        if (not entered and f"InfrastructureError: {exc}" in {
                                ADMISSION_TIMEOUT_PREFIX + reason for reason in ADMISSION_TIMEOUT_REASONS}):
                            row["generation_admission_status"] = "timed_out"
                            row["generation_admission_failure_at"] = time.time()
                        raise
                row["generation"] = generation
                if generation["status"] == "generated":
                    with services.job(task, recipe, repo, "progressive-evaluate:"+model):
                        row["correctness"] = correctness(task, recipe, generation, repo, root/"evaluation",
                                                          Path(config["asset_root"]), retry_infra=args.retry_infra)
                        atomic_json(root/"pair-result.json", row)
                        row["judgment"] = judge_candidate(task, generation, repo, config["judge"],
                                                         root/"judgment.json", retry_infra=args.retry_infra)
                row["status"] = "processed"
            except Exception as exc:
                row["error"] = f"{type(exc).__name__}: {exc}"
            atomic_json(root/"pair-result.json", row)
            return row

        attempted = set(results) if not args.retry_infra or args.retry_admission_only else {
            key for key,row in results.items() if pair_complete(row)}
        save_status()
        with ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix="progressive-agent") as executor:
            pending = {}
            pending_heavy = set()
            while True:
                declared_packages = json.loads(package_file.read_text())
                if (not isinstance(declared_packages, list)
                        or any(not isinstance(p, str) or not Path(p).is_absolute() for p in declared_packages)):
                    raise ContractError("runtime-packages.json must list absolute local package directories")
                packages = [Path(p) for p in declared_packages]
                candidates = package_candidates(packages, tasks, oracles)
                inflight_ids = {pair[0] for pair in pending.values()}
                for pair in matrix:
                    if len(pending) >= args.workers:
                        break
                    ident, model = pair
                    if ident in inflight_ids or ident not in candidates:
                        continue
                    recipe, proof, proof_file = candidates[ident]
                    heavy = (recipe.lane in {"native", "rust", "jvm", "csharp"} or recipe.memory_mb > 4096
                             or services.source_bytes(tasks[ident], Path(config["repo_cache"]) /
                                                      tasks[ident]["repo"].replace("/", "__")) > 1024**3)
                    if not admission_slot_available(len(pending), len(pending_heavy), args.workers,
                                                    heavy_workers, heavy):
                        continue
                    previous = results.get(pair, {})
                    if pair in attempted:
                        pair_root = output/"pairs"/component(model)/component(ident)
                        if (args.watch and args.retry_infra
                                and generation_admission_retryable(previous, pair_root, recipe.fingerprint)):
                            if time.time() < admission_retry_at(previous, args.poll_seconds):
                                continue
                        elif (args.watch and args.retry_infra
                              and verification_admission_retryable(
                                  previous, pair_root, tasks[ident], oracles[ident], recipe, proof, engine)):
                            pass
                        elif (args.retry_admission_only or previous.get("generation_started") or previous.get("generation")
                                or previous.get("runtime_fingerprint") == recipe.fingerprint):
                            continue
                    if ident in fixed and fixed[ident].fingerprint != recipe.fingerprint:
                        # Once a model call can exist, never switch to an easier
                        # runtime behind an existing result.
                        used = any(row.get("generation_started") or row.get("generation")
                                   for (i, _),row in results.items() if i == ident)
                        traces = list((output/"pairs").glob("*/"+component(ident)+"/episode/trajectory.json"))
                        if used or traces:
                            raise ContractError("runtime changed after first generation admission: "+ident)
                    fixed[ident] = recipe
                    write_jsonl(fixed_file, [asdict(fixed[i]) for i in tasks if i in fixed])
                    attempted.add(pair)
                    inflight_ids.add(ident)
                    future = executor.submit(execute, pair, recipe, proof, proof_file)
                    pending[future] = pair
                    if heavy:
                        pending_heavy.add(future)
                    print(json.dumps({"event": "admitted", "id": ident, "model": model,
                                      "expected_pairs": len(matrix)}), flush=True)
                if pending:
                    atomic_json(output/"watch-status.json",
                                {"state": "running_or_waiting_for_resources", "observed_candidates": len(candidates),
                                 "attempted_pairs": len(attempted), "expected_pairs": len(matrix),
                                 "pending_pairs": [i+"::"+m for i, m in pending.values()], "time": time.time()})
                    done, _ = wait(pending, timeout=args.poll_seconds, return_when=FIRST_COMPLETED)
                    for future in done:
                        pending_heavy.discard(future)
                        pair = pending.pop(future)
                        results[pair] = future.result()
                        report = save_status()
                        print(json.dumps({"event": "pair-checkpoint", "id": pair[0], "model": pair[1],
                                          "status": results[pair]["status"], "error": results[pair].get("error"),
                                          "measured_generations": report["measured_generations"],
                                          "expected_pairs": len(matrix)}), flush=True)
                else:
                    report = save_status()
                    if report["complete"] or not args.watch:
                        print(json.dumps(report, indent=2))
                        return 0 if report["complete"] else 2
                    retries = {
                        i+"::"+m: admission_retry_at(row, args.poll_seconds)
                        for (i, m), row in results.items()
                        if args.retry_infra and i in candidates and generation_admission_retryable(
                            row, output/"pairs"/component(m)/component(i), candidates[i][0].fingerprint)
                    }
                    atomic_json(output/"watch-status.json",
                                {"state": "waiting_for_resource_admission_retry" if retries else "waiting_for_more_verified_tasks",
                                 "observed_candidates": len(candidates), "attempted_pairs": len(attempted),
                                 "expected_pairs": len(matrix), "admission_retry_pairs": retries, "time": time.time()})
                    time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
