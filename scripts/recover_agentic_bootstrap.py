#!/usr/bin/env python3
"""Issue a first model episode only after a hash-bound, proven zero-call bootstrap failure.

Original failures/frozen recipes stay untouched. A separate fixed-denominator
overlay records the repaired pre-inference runtime and its first real answer.
"""
from __future__ import annotations

import argparse
from collections import Counter
import copy
import json
import math
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, os.environ.get("REVIEWBENCH_ENGINE_SRC", str(Path(__file__).resolve().parents[1] / "src")))
from reviewbench_maintainability.agentic.cli import load_config
from reviewbench_maintainability.agentic.contracts import (
    ContractError, InfrastructureError, PROTOCOL, atomic_json, canonical_hash, component, file_hash, read_jsonl,
    selected_tasks, write_jsonl,
)
from reviewbench_maintainability.agentic.evaluation import (
    RUBRIC, RUBRIC_VERSION, correctness, invoke_judge, judge_candidate,
)
from reviewbench_maintainability.agentic.execution import ExecutionServices
from reviewbench_maintainability.agentic.models import engine_identity, sanitize_config
from reviewbench_maintainability.agentic.preparation import load_recipes
from reviewbench_maintainability.agentic.retention import archive_episode
from reviewbench_maintainability.agentic.runner import run_episode
from reviewbench_maintainability.agentic.scheduling import output_lock
from reviewbench_maintainability.agentic.reporting import make_report
from reviewbench_maintainability.agentic.resources import file_lock
from agentic_runtime_migration.go_modfile import RUNTIME, repaired_recipe
from run_agentic_progressive import experiment_manifest
from recover_agentic_measurements import validate_completed_judgment
from recover_agentic_judgments import validate_member

SCHEMA = "reviewbench-zero-call-bootstrap-recovery/v1"
BIGTABLE_ZERO_CALL = {
    "profile": "bigtable6314/nested-shadow-first/v1",
    "id": "github__googleapis__google-cloud-go__pull_6314__comment_916947041__api_design",
    "repo": "googleapis/google-cloud-go",
    "start_sha": "67b9e900945901b6b69c2868d549583d49d19132",
    "model": "gpt-5.4-responses",
    "source_path": "bigtable/go.mod",
    "engine_fingerprint": "5142d1fec94eb6c5a6177a66ee7ce8533a26f0a2578280075a6947a399c01ca3",
    "task_fingerprint": "e39bbfd75e05811b02b0d2de8afaa0702afbe68679026cb3ad885d644ab637af",
    "source_generation_fingerprint": "b42712cb206b5f8e49669342b554c5cbc2969a45c1f610f5f8a5c0d402e1b807",
    "source_result_sha256": "7ca0dbb7b91838d3dba555603575c780b8d0e4439df1df6b164dcae6608339e6",
    "source_pair_sha256": "ea9315087b34a46fa5e5d5aa58d2fac2568eb7f8ec5bad9ebcac1b2469f61a1e",
    "source_archive_sha256": "bd0b86b9cf73a9d7fb6fada95b8d694fe6e9f312c207d356eda9b6adf7274021",
    "original_runtime_fingerprint": "74016b6d12ed433678da79bba8a3d256bde726a94e8994f97e843becb3655514",
    "repaired_runtime_fingerprint": "e53977dd1e0427a149ef74f6c04d13434c87b817df2ab4a6fc1ce964aea396ef",
    "repaired_proof_sha256": "3e6d8f866158ff7fb638dd75102f83c85f07f62932e0519c5fbb4b0522adafc6",
}


def validate_source_failure_profile(pair_root, task, selection, engine):
    """Keep the existing root route; admit one separately reviewed nested failure.

    The exact Bigtable failure was audited against the frozen runner: its source
    integrity exception precedes Budget/model construction, and its retained
    archive contains only the three original bootstrap commands. These pins bind
    that original failure and the separately verified repair, not any new answer.
    """
    if selection.get("source_path") == "go.mod":
        return
    expected = BIGTABLE_ZERO_CALL
    if (any(selection.get(key) != expected[key] for key in (
            "profile", "id", "model", "source_path", "source_generation_fingerprint",
            "source_result_sha256", "original_runtime_fingerprint",
            "repaired_runtime_fingerprint", "repaired_proof_sha256"))
            or any(task.get(key) != expected[key] for key in ("id", "repo", "start_sha"))
            or canonical_hash(task) != expected["task_fingerprint"]
            or engine != expected["engine_fingerprint"]):
        raise ContractError("nested zero-call recovery differs from the exact reviewed Bigtable profile")
    for relative, digest in (("pair-result.json", expected["source_pair_sha256"]),
                             ("episode/terminal-archive.json", expected["source_archive_sha256"])):
        path = Path(pair_root) / relative
        if path.is_symlink() or not path.is_file() or file_hash(path) != digest:
            raise ContractError("nested zero-call original pair/archive bytes changed")


def model_settings(config, model):
    settings = {key: config.get(key, {}) for key in ("agent", "budget", "observation")}
    return {**settings, "model": {**config["models"][model], "id": model}}


def validate_experiment(config, tasks, models, source_manifest):
    if experiment_manifest(config, tasks, models) != source_manifest:
        raise ContractError("bootstrap recovery changes original tasks/model/engine/behavior/source bindings")


def validate_output_location(output, source, base, package):
    output = Path(output).resolve()
    for protected in (source, base, package):
        protected = Path(protected).resolve()
        if output.is_relative_to(protected) or protected.is_relative_to(output):
            raise ContractError("recovery output overlaps source/base/package")


def validate_base_failure(base, original):
    pair = original["id"], original["model"]
    row = load_base_rows(base).get(pair, {})
    if row.get("generation") != original["generation"]:
        raise ContractError("base answer changed before first-episode admission")
    if row.get("correctness") or row.get("judgment"):
        raise ContractError("base has existing downstream measurements before first-episode admission")


class JournaledFirstPanel:
    """Persist each judge request/result; keep accepted positive and negative votes."""

    def __init__(self, directory, generation, *, invoke=invoke_judge):
        self.directory, self.generation, self.invoke = Path(directory), generation, invoke

    def __call__(self, model, prompt, config):
        root = self.directory / component(model)
        binding = {"model": model, "generation_fingerprint": canonical_hash(self.generation),
                   "prompt_fingerprint": canonical_hash(prompt), "config_fingerprint": canonical_hash(config)}
        with file_lock(root / "request.lock"):
            result_file, intent = root / "response.json", root / "intent.json"
            if result_file.exists():
                cached = json.loads(result_file.read_text())
                if cached.get("binding") != binding:
                    raise ContractError("journaled first-panel result has different bindings")
                return copy.deepcopy(cached["member"])
            if intent.exists():
                if json.loads(intent.read_text()) != binding:
                    raise ContractError("journaled first-panel intent has different bindings")
                return {"judge_model": model, "error": "request outcome not recorded; no automatic repeat"}
            atomic_json(intent, binding)
            # If invocation or persistence is interrupted, the intent remains.
            member = self.invoke(model, prompt, config)
            atomic_json(result_file, {"binding": binding, "member": member})
            return copy.deepcopy(member)


def validate_recovered_generation(generation, task, recipe, settings, engine, episode):
    bindings = {"id": task["id"], "model": settings["model"]["id"], "protocol": PROTOCOL,
                "task_fingerprint": canonical_hash(task), "runtime_fingerprint": recipe.fingerprint,
                "config_fingerprint": canonical_hash(settings), "start_sha": task["start_sha"],
                "engine_fingerprint": engine}
    if any(generation.get(key) != value for key, value in bindings.items()):
        raise ContractError("recovery generation has different complete episode bindings")
    episode = Path(episode).resolve()
    result_file = episode / "result.json"
    if (not result_file.is_file() or result_file.is_symlink()
            or json.loads(result_file.read_text()) != generation):
        raise ContractError("cached recovery generation differs from its durable episode result")
    if generation.get("status") not in {"generated", "generation_failed", "infra_error"}:
        raise ContractError("unknown recovered generation status")
    trace = Path(generation.get("trace_file") or episode / "trajectory.json")
    if trace.resolve() != episode / "trajectory.json":
        raise ContractError("recovery trace is outside the selected episode")
    if generation.get("trace_sha256"):
        if trace.is_symlink() or not trace.is_file() or file_hash(trace) != generation["trace_sha256"]:
            raise ContractError("recovery trajectory bytes changed")
        if json.loads(trace.read_text()).get("bindings") != bindings:
            raise ContractError("recovery trajectory belongs to another episode")
    elif generation["status"] != "infra_error":
        raise ContractError("terminal recovery answer has no bound trajectory")
    if generation["status"] == "generated":
        patch = Path(generation.get("patch_file") or "")
        if (generation.get("apply_ok") is not True or patch.resolve() != episode / "candidate.patch"
                or patch.is_symlink() or not patch.is_file() or file_hash(patch) != generation.get("patch_sha256")):
            raise ContractError("recovery candidate patch bytes/path changed")


def validate_recovered_measurement(value, generation, config, path, *, judgment=False):
    for key in ("id", "model", "protocol", "task_fingerprint", "patch_sha256", "engine_fingerprint"):
        if value.get(key) != generation.get(key):
            raise ContractError("cached recovery measurement differs in " + key)
    path = Path(path)
    if path.is_symlink() or not path.is_file() or json.loads(path.read_text()) != value:
        raise ContractError("cached recovery measurement differs from its durable record")
    if judgment:
        expected = canonical_hash({"rubric": RUBRIC, "version": RUBRIC_VERSION, "config": config["judge"]})
        if value.get("judge_fingerprint") != expected:
            raise ContractError("cached recovery panel uses another judge configuration")
        if value.get("status") == "completed":
            validate_completed_judgment(value, generation, config["judge"], {generation["engine_fingerprint"]})
        elif value.get("status") == "infra_error":
            for member in value.get("panel", []):
                if "passed" in member:
                    validate_member(member, config["judge"])
        else:
            raise ContractError("unknown recovered judgment status")
    elif (value.get("runtime_fingerprint") != generation.get("runtime_fingerprint")
          or value.get("status") not in {"pass", "fail", "scope_error", "apply_error", "infra_error"}):
        raise ContractError("cached correctness has different runtime/status")


def validate_zero_call_failure(row, pair_root, task, config, selection, engine):
    """The known frozen runner raises this error before constructing its model.

    Empty usage alone is insufficient: require exact error stage, no result/trace/
    candidate evidence, original episode bytes, task/settings/engine bindings and
    a specifically selected original failure fingerprint.
    """
    generation = row.get("generation", {})
    expected_error = "InfrastructureError: bootstrap changed original source: " + selection["source_path"]
    if (generation.get("status") != "infra_error" or generation.get("stop_reason") != "infrastructure_error"
            or generation.get("error") != expected_error or generation.get("usage") != {}
            or generation.get("apply_ok") is not False or generation.get("changed_files") != []
            or generation.get("changed_lines") != 0
            or any(generation.get(key) is not None for key in ("patch_file", "patch_sha256", "trace_sha256"))
            or any(key in row for key in ("correctness", "judgment", "answer_recovery_required", "previous_infrastructure_attempt"))):
        raise ContractError("selected failure does not prove zero model calls before bootstrap rejection")
    if (generation.get("id") != task["id"] or generation.get("model") != selection["model"]
            or generation.get("protocol") != PROTOCOL or generation.get("engine_fingerprint") != engine
            or generation.get("task_fingerprint") != canonical_hash(task)
            or generation.get("start_sha") != task["start_sha"]
            or generation.get("config_fingerprint") != canonical_hash(model_settings(config, selection["model"]))
            or canonical_hash(generation) != selection["source_generation_fingerprint"]
            or generation.get("runtime_fingerprint") != selection["original_runtime_fingerprint"]):
        raise ContractError("zero-call failure differs from its explicit task/model/runtime/engine/settings binding")
    validate_source_failure_profile(pair_root, task, selection, engine)
    pair_root = Path(pair_root)
    episode = pair_root / "episode"
    expected_trace = episode / "trajectory.json"
    if (Path(generation.get("trace_file", "")).resolve() != expected_trace.resolve()
            or pair_root.is_symlink() or episode.is_symlink()
            or any(path.is_symlink() for path in pair_root.rglob("*"))
            or list(pair_root.rglob("trajectory.json")) or list(pair_root.rglob("candidate.patch"))
            or any(path.name.startswith(("retry-", "episode-attestation")) for path in pair_root.rglob("*"))):
        raise ContractError("selected failure has episode/trace/candidate or alternate-attempt evidence")
    result_file = episode / "result.json"
    if (not result_file.is_file() or file_hash(result_file) != selection["source_result_sha256"]
            or json.loads(result_file.read_text()) != generation):
        raise ContractError("original zero-call episode result is missing or changed")
    archive = episode / "terminal-archive.json"
    if archive.exists():
        saved = json.loads(archive.read_text())
        if (saved["binding"]["result_sha256"] != selection["source_result_sha256"]
                or saved["binding"].get("trace_sha256") is not None
                or saved["binding"].get("patch_sha256") is not None):
            raise ContractError("terminal archive contradicts the zero-call failure")
    return copy.deepcopy(generation)


def validate_runtime_repair(original, new, proof, task, oracle, selection, engine):
    """Only the reviewed Go module-shadow transport may differ."""
    old_asset = next((asset for asset in original.assets if asset.get("target") == RUNTIME), None)
    new_asset = next((asset for asset in new.assets if asset.get("target") == RUNTIME), None)
    if old_asset is None or new_asset is None:
        raise ContractError("runtime repair lacks its original/new public transport")
    expected = repaired_recipe(original, {key: new_asset[key] for key in ("path", "sha256")})
    if (original.fingerprint != selection["original_runtime_fingerprint"]
            or new.fingerprint != selection["repaired_runtime_fingerprint"]
            or new.fingerprint != expected.fingerprint):
        raise ContractError("runtime repair changes fields beyond module-shadow transport")
    if (proof.get("status") != "ready" or proof.get("runtime_fingerprint") != new.fingerprint
            or proof.get("task_fingerprint") != canonical_hash(task)
            or proof.get("oracle_fingerprint") != canonical_hash(oracle)
            or proof.get("engine_fingerprint") != engine
            or Counter((run["variant"], run["repetition"]) for run in proof.get("runs", [])) !=
            Counter((variant, n) for variant in ("start", "golden") for n in range(3))
            or any(not run.get("tests") or not all(run["tests"].values()) for run in proof["runs"])):
        raise ContractError("repaired runtime lacks the exact current-engine six-replay proof")


def recover_first_episode(original, task, oracle, recipe, services, config, root, selection, *,
                          claim_file, preflight, generate=run_episode, evaluate=correctness,
                          judge=judge_candidate, judge_invoke=invoke_judge):
    """Use a source-pair claim across output directories, not merely a local lock."""
    preflight()
    root = Path(root).resolve()
    binding = {"id": task["id"], "model": selection["model"],
               "original_generation_fingerprint": canonical_hash(original["generation"]),
               "selection_fingerprint": canonical_hash(selection), "owner_output": str(root)}
    claim_file = Path(claim_file)
    with file_lock(claim_file.with_suffix(".lock")):
        claim = json.loads(claim_file.read_text()) if claim_file.exists() else {
            "binding": binding, "first_generation_started": False}
        if claim.get("binding") != binding:
            raise ContractError("source pair already claimed by another output/selection; no new model chance")
        preflight()
        atomic_json(claim_file, claim)
        return _recover_claimed_episode(
            original, task, oracle, recipe, services, config, root, selection,
            claim_file=claim_file, claim=claim, preflight=preflight,
            generate=generate, evaluate=evaluate, judge=judge, judge_invoke=judge_invoke)


def _recover_claimed_episode(original, task, oracle, recipe, services, config, root, selection, *,
                             claim_file, claim, preflight, generate, evaluate, judge, judge_invoke):
    """At most one first-episode intent; no retry-infra model path exists."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    record_file = root / "recovery.json"
    binding = canonical_hash(selection)
    record = json.loads(record_file.read_text()) if record_file.exists() else {
        "schema_version": SCHEMA, "selection_fingerprint": binding,
        "original_generation": copy.deepcopy(original["generation"]),
        "original_generation_fingerprint": canonical_hash(original["generation"]),
        "selection": copy.deepcopy(selection), "first_generation_started": False,
        "row": {"id": task["id"], "model": selection["model"], "runtime_fingerprint": recipe.fingerprint,
                "status": "incomplete"},
    }
    if (record.get("selection_fingerprint") != binding
            or record.get("original_generation") != original["generation"]):
        raise ContractError("recovery intent belongs to another original failure or selection")
    record["first_generation_started"] = bool(record["first_generation_started"] or claim["first_generation_started"])
    row = record["row"]
    repository = Path(config["repo_cache"]) / task["repo"].replace("/", "__")
    episode = root / "episode"
    settings = model_settings(config, selection["model"])
    engine = original["generation"]["engine_fingerprint"]
    waiting_for_admission = False
    try:
        if not row.get("generation"):
            if record["first_generation_started"] or claim["first_generation_started"]:
                # A crashed process may have sent a request. Keep its checkpoint
                # for explicit examination, never create another model episode.
                result_file = episode / "result.json"
                if not result_file.is_file():
                    raise ContractError("first-generation intent retained; no automatic replay of uncertain episode")
                generation = json.loads(result_file.read_text())
                validate_recovered_generation(generation, task, recipe, settings, engine, episode)
                row["generation"] = generation
            else:
                if episode.exists():
                    raise ContractError("unbound recovery episode already exists")
                atomic_json(record_file, record)
                waiting_for_admission = True
                with services.job(task, recipe, repository, "bootstrap-first-generation:" + selection["model"]):
                    waiting_for_admission = False
                    preflight()
                    claim["first_generation_started"] = True
                    atomic_json(claim_file, claim)
                    record["first_generation_started"] = True
                    atomic_json(record_file, record)
                    generation = generate(task, recipe, settings, repository,
                                          episode, Path(config["asset_root"]), resume=True, retry_infra=False)
                    validate_recovered_generation(generation, task, recipe, settings, engine, episode)
                    row["generation"] = generation
                    atomic_json(record_file, record)
                    if services.cache and config.get("storage", {}).get("archive_terminal"):
                        archive_episode(episode, generation, services)
        generation = row["generation"]
        validate_recovered_generation(generation, task, recipe, settings, engine, episode)
        if row.get("correctness"):
            validate_recovered_measurement(row["correctness"], generation, config, root / "evaluation/correctness.json")
        if row.get("judgment"):
            validate_recovered_measurement(row["judgment"], generation, config, root / "judgment.json", judgment=True)
        if generation["status"] == "generated":
            waiting_for_admission = True
            with services.job(task, recipe, repository, "bootstrap-first-evaluate:" + selection["model"]):
                waiting_for_admission = False
                preflight()
                if not row.get("correctness"):
                    row["correctness"] = evaluate(task, recipe, generation, repository, root / "evaluation",
                                                  Path(config["asset_root"]), retry_infra=False)
                    validate_recovered_measurement(row["correctness"], generation, config, root / "evaluation/correctness.json")
                    atomic_json(record_file, record)
                if not row.get("judgment"):
                    preflight()
                    invoke = JournaledFirstPanel(root / "judge-requests", generation, invoke=judge_invoke)
                    row["judgment"] = judge(task, generation, repository, config["judge"],
                                            root / "judgment.json", retry_infra=False, invoke=invoke)
                    validate_recovered_measurement(row["judgment"], generation, config, root / "judgment.json", judgment=True)
        row["status"] = "processed"
        row.pop("error", None)
        record.pop("integrity_error", None)
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        if not (waiting_for_admission and isinstance(exc, InfrastructureError)
                and str(exc).startswith("resource admission timed out: ")):
            # JSON/read/shape failures are not valid measurements either.
            # Preserve diagnosis, but never stamp or publish an invalid row.
            record["integrity_error"] = row["error"]
            atomic_json(record_file, record)
            raise
    record["validated_row_fingerprint"] = canonical_hash(row)
    atomic_json(record_file, record)
    return record


def load_base_rows(base):
    rows = {}
    for field, name in (("generation", "generation_results.jsonl"), ("correctness", "correctness.jsonl"),
                        ("judgment", "judgments.jsonl")):
        for line in (Path(base) / name).read_text().splitlines():
            item = json.loads(line)
            key = item["id"], item["model"]
            row = rows.setdefault(key, {"id": key[0], "model": key[1]})
            if field in row:
                raise ContractError("base recovery contains duplicate measurements")
            row[field] = item
    return rows


def publish(base, output, tasks, models, records):
    rows = load_base_rows(base)
    for record in records.values():
        row = record["row"]
        if (record.get("integrity_error")
                or record.get("validated_row_fingerprint") != canonical_hash(row)):
            raise ContractError("unvalidated recovery record cannot enter the result overlay")
        pair = row["id"], row["model"]
        if canonical_hash(rows.get(pair, {}).get("generation", {})) != record["original_generation_fingerprint"]:
            raise ContractError("base answer changed since zero-call selection")
        if rows[pair].get("correctness") or rows[pair].get("judgment"):
            raise ContractError("zero-call overlay would replace existing downstream measurements")
        if row.get("generation"):
            rows[pair] = copy.deepcopy(row)
    columns = {field: [row[field] for row in rows.values() if row.get(field)]
               for field in ("generation", "correctness", "judgment")}
    table, report = make_report(tasks, models, columns["generation"], columns["correctness"], columns["judgment"])
    for name, values in (("generation_results", columns["generation"]), ("correctness", columns["correctness"]),
                         ("judgments", columns["judgment"]), ("results", table)):
        write_jsonl(Path(output) / (name + ".jsonl"), values)
    report.update(expected_pairs=len(tasks) * len(models), measured_generations=len(columns["generation"]),
                  source_recovery_run=str(Path(base).resolve()), zero_call_recovery_records=len(records),
                  first_generation_episodes=sum(record["first_generation_started"] for record in records.values()),
                  first_generation_api_calls=sum(record["row"].get("generation", {}).get("usage", {}).get("model_calls", 0)
                                                 for record in records.values()),
                  resampled_completed_answers=0)
    atomic_json(Path(output) / "report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("config", "source-run", "base-run", "package", "selection", "out-dir"):
        parser.add_argument("--" + flag, type=Path, required=True)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=30)
    args = parser.parse_args()
    if not math.isfinite(args.poll_seconds) or args.poll_seconds <= 0:
        raise ContractError("recovery polling interval must be finite and positive")
    config = load_config(args.config)
    tasks = selected_tasks(Path(config["tasks"]), Path(config["scope_ids"]))
    oracles = read_jsonl(Path(config["oracles"]))
    source = args.source_run.resolve()
    source_manifest = json.loads((source / "experiment-manifest.json").read_text())
    models = source_manifest["models"]
    engine = engine_identity()["fingerprint"]
    if len(tasks) != 402 or source_manifest["task_ids"] != list(tasks) or source_manifest["engine_fingerprint"] != engine:
        raise ContractError("bootstrap recovery requires the original full402 task/model/engine bindings")
    validate_experiment(config, tasks, models, source_manifest)
    validate_output_location(args.out_dir, source, args.base_run, args.package)
    base_report = json.loads((args.base_run / "report.json").read_text())
    if (Path(base_report.get("source_run", "")).resolve() != source
            or base_report.get("expected_pairs") != len(tasks) * len(models)
            or base_report.get("generation_engine_fingerprint") != engine):
        raise ContractError("base recovery report differs from the original experiment")
    selections = json.loads(args.selection.read_text())
    if not isinstance(selections, list) or len(selections) != 1:
        raise ContractError("this recovery requires one explicitly bound zero-call selection")
    selected = selections[0]
    ident, model = selected["id"], selected["model"]
    if ident not in tasks or model not in models:
        raise ContractError("zero-call selection is outside the original matrix")
    pair_root = source / "pairs" / component(model) / component(ident)
    original = json.loads((pair_root / "pair-result.json").read_text())
    validate_zero_call_failure(original, pair_root, tasks[ident], config, selected, engine)
    validate_base_failure(args.base_run, original)
    old = load_recipes(source / "fixed-runtime-recipes.jsonl")[ident]
    recipe = load_recipes(args.package / "runtime-recipes.jsonl")[ident]
    proof_path = args.package / "execution/preparation/verified" / component(ident) / "verification.json"
    if file_hash(proof_path) != selected["repaired_proof_sha256"]:
        raise ContractError("selected repaired proof bytes changed")
    validate_runtime_repair(old, recipe, json.loads(proof_path.read_text()), tasks[ident], oracles[ident], selected, engine)
    output = args.out_dir.resolve()
    runtime_config = {**config, "out_dir": str(output)}
    services = ExecutionServices(runtime_config)
    manifest = {"schema_version": SCHEMA, "source_run": str(source), "base_run": str(args.base_run.resolve()),
                "source_manifest_sha256": file_hash(source / "experiment-manifest.json"),
                "task_ids": list(tasks), "models": models, "expected_pairs": 402 * len(models),
                "engine_fingerprint": engine, "selection_sha256": file_hash(args.selection),
                "settings_fingerprint": canonical_hash(model_settings(config, model)),
                "tasks_fingerprint": canonical_hash(tasks),
                "behavior_fingerprint": source_manifest["behavior_fingerprint"],
                "judge_fingerprint": canonical_hash({"rubric": RUBRIC, "version": RUBRIC_VERSION, "config": config["judge"]})}
    claim_file = source / "zero-call-first-claims" / component(model) / component(ident) / "claim.json"

    def preflight():
        current = json.loads((pair_root / "pair-result.json").read_text())
        validate_experiment(config, tasks, models, json.loads((source / "experiment-manifest.json").read_text()))
        validate_zero_call_failure(current, pair_root, tasks[ident], config, selected, engine)
        validate_base_failure(args.base_run, current)
    with output_lock(output):
        path = output / "experiment-manifest.json"
        if path.exists() and json.loads(path.read_text()) != manifest:
            raise ContractError("existing bootstrap recovery has different bindings")
        atomic_json(path, manifest)
        atomic_json(output / "experiment-config.json", sanitize_config(runtime_config))
        attempted = False
        record = None
        while True:
            current = json.loads((pair_root / "pair-result.json").read_text())
            validate_zero_call_failure(current, pair_root, tasks[ident], config, selected, engine)
            if not attempted:
                record = recover_first_episode(current, tasks[ident], oracles[ident], recipe, services, runtime_config,
                                               output / "repairs" / component(model) / component(ident), selected,
                                               claim_file=claim_file, preflight=preflight)
                capacity_wait = record["row"].get("error", "").startswith(
                    "InfrastructureError: resource admission timed out: ")
                attempted = not (capacity_wait and (
                    not record["first_generation_started"] or record["row"].get("generation")))
            report = publish(args.base_run, output, tasks, models, {(ident, model): record})
            print(json.dumps({"event": "zero-call-recovery-checkpoint", "id": ident,
                              "status": record["row"]["status"], "error": record["row"].get("error"),
                              "measured_generations": report["measured_generations"],
                              "expected_pairs": report["expected_pairs"]}), flush=True)
            if not args.watch or report["complete"]:
                return 0 if report["complete"] else 2
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
