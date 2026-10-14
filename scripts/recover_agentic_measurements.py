#!/usr/bin/env python3
"""Overlay bounded correctness-only repairs without changing answers or votes."""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, os.environ.get("REVIEWBENCH_ENGINE_SRC", str(Path(__file__).resolve().parents[1] / "src")))
from reviewbench_maintainability.agentic.cli import load_config
from reviewbench_maintainability.agentic.contracts import (
    ContractError, PROTOCOL, atomic_json, canonical_hash, component, file_hash, selected_tasks, write_jsonl,
)
from reviewbench_maintainability.agentic.evaluation import (
    RUBRIC, RUBRIC_VERSION, correctness, parse_judge_verdict, verdict_passes,
)
from reviewbench_maintainability.agentic.execution import ExecutionServices
from reviewbench_maintainability.agentic.models import engine_identity, sanitize_config
from reviewbench_maintainability.agentic.preparation import load_recipes
from reviewbench_maintainability.agentic.reporting import make_report
from reviewbench_maintainability.agentic.scheduling import output_lock

TERMINAL_CORRECTNESS = frozenset({"pass", "fail", "scope_error", "apply_error"})
HEX256 = re.compile(r"[0-9a-f]{64}")


def validate_answer(generation):
    if generation.get("status") != "generated" or generation.get("protocol") != PROTOCOL:
        raise ContractError("measurement repair needs a completed generated answer")
    for field in ("patch", "trace"):
        checksum = generation.get(field + "_sha256", "")
        path = Path(generation[field + "_file"])
        if not HEX256.fullmatch(checksum) or not path.is_file() or file_hash(path) != checksum:
            raise ContractError("completed answer " + field + " bytes changed")


def validate_completed_judgment(judgment, generation, config, allowed_engines):
    if judgment.get("status") != "completed":
        raise ContractError("source judgment is not complete")
    for key in ("id", "model", "protocol", "task_fingerprint", "patch_sha256"):
        if judgment.get(key) != generation.get(key):
            raise ContractError("source judgment differs in " + key)
    if judgment.get("engine_fingerprint") not in allowed_engines:
        raise ContractError("source judgment has an unbound execution engine")
    expected = canonical_hash({"rubric": RUBRIC, "version": RUBRIC_VERSION, "config": config})
    if judgment.get("judge_fingerprint") != expected:
        raise ContractError("source judgment uses a different frozen panel/rubric configuration")
    if judgment.get("evidence_complete") is not True or not HEX256.fullmatch(judgment.get("evidence_sha256", "")):
        raise ContractError("source judgment lacks its complete evidence binding")
    panel, models = judgment.get("panel", []), config.get("models", [])
    if len(models) != 3 or len(set(models)) != 3 or [member.get("judge_model") for member in panel] != models:
        raise ContractError("source judgment is not the exact three-member panel")
    for member in panel:
        if type(member.get("http_status")) is not int or not 200 <= member["http_status"] < 300:
            raise ContractError("cached judge response is not a successful HTTP response")
        if type(member.get("passed")) is not bool or verdict_passes(member.get("verdict", {})) != member["passed"]:
            raise ContractError("cached vote disagrees with its substantive verdict")
        raw = member.get("raw_response")
        if (not isinstance(raw, dict) or raw.get("model") not in
                config.get("expected_response_models", {}).get(member["judge_model"], [member["judge_model"]])):
            raise ContractError("cached judge response has an unbound model identity")
        if json.loads(member["raw_response_text"]) != raw:
            raise ContractError("cached judge raw response representations disagree")
        choice = raw["choices"][0]
        if choice.get("finish_reason") not in {"stop", "end_turn"}:
            raise ContractError("cached judge response is incomplete")
        if parse_judge_verdict(choice["message"]["content"]) != member["verdict"]:
            raise ContractError("cached raw judge response differs from the retained verdict")
    if type(judgment.get("maintainability_pass")) is not bool or judgment["maintainability_pass"] != all(
            member["passed"] for member in panel):
        raise ContractError("cached panel aggregate differs from its unchanged votes")


def merge_completed_source_judgment(original, selected, config, allowed_engines):
    """A stale recovery row must not hide an exactly bound completed panel."""
    row = copy.deepcopy(selected)
    source_judgment = original.get("judgment", {})
    previous = row.get("judgment", {})
    if previous.get("status") == "completed" or source_judgment.get("status") != "completed":
        return row, False
    if original.get("generation") != row.get("generation"):
        # A host-serialized generation is a separately attested object. Do not
        # infer identity merely from a shared task name or similar patch.
        return row, False
    generation = row["generation"]
    validate_answer(generation)
    validate_completed_judgment(source_judgment, generation, config, allowed_engines)
    accepted = {member["judge_model"]: member for member in previous.get("panel", [])
                if "passed" in member}
    incoming = {member["judge_model"]: member for member in source_judgment["panel"]}
    if any(name not in incoming or incoming[name] != member for name, member in accepted.items()):
        raise ContractError("new source panel would replace an already accepted cached vote")
    row["judgment"] = copy.deepcopy(source_judgment)
    return row, True


def load_repair_selection(path, tasks, models):
    value = json.loads(Path(path).read_text())
    if not isinstance(value, list) or not value:
        raise ContractError("measurement repair requires an explicit nonempty token selection")
    result = {}
    for row in value:
        key = row["id"], row["model"], row["source_generation_fingerprint"]
        if (key[0] not in tasks or key[1] not in models or key in result
                or any(not HEX256.fullmatch(row.get(field, "")) for field in
                       ("source_generation_fingerprint", "patch_sha256", "trace_sha256", "judgment_sha256"))):
            raise ContractError("measurement selection contains an unknown, duplicate, or unbound token")
        result[key] = row
    return result


def validate_base_manifest(base, source, source_manifest, engine, judge_config):
    path = Path(base) / "experiment-manifest.json"
    manifest = json.loads(path.read_text())
    expected = {
        "schema_version": "reviewbench-host-recovery/v1",
        "source_run": str(Path(source).resolve()),
        "source_manifest_sha256": file_hash(Path(source) / "experiment-manifest.json"),
        "generation_engine_fingerprint": source_manifest["engine_fingerprint"],
        "host_recovery_engine_fingerprint": engine,
        "task_ids": source_manifest["task_ids"], "models": source_manifest["models"],
        "tasks_fingerprint": source_manifest["tasks_fingerprint"],
        "expected_pairs": len(source_manifest["task_ids"]) * len(source_manifest["models"]),
        "judge_fingerprint": canonical_hash(judge_config), "additional_generation_calls": 0,
    }
    if any(manifest.get(key) != value for key, value in expected.items()):
        raise ContractError("base recovery manifest differs from the exact source/grading experiment")
    return file_hash(path)


def load_rows(source, tasks, models, generation_engine):
    rows = {}
    for path in (Path(source) / "pairs").glob("*/*/pair-result.json"):
        row = json.loads(path.read_text())
        key = row["id"], row["model"]
        if key in rows or key[0] not in tasks or key[1] not in models:
            raise ContractError("source contains a duplicate or foreign pair")
        generation = row.get("generation")
        if generation and generation.get("engine_fingerprint") != generation_engine:
            raise ContractError("source generation engine changed")
        rows[key] = row
    return rows


def load_repairs(root, tasks, models, engine):
    records = {}
    for path in (Path(root) / "repairs").glob("*/*/*/result.json"):
        record = json.loads(path.read_text())
        row = record["row"]
        key = row["id"], row["model"], record["source_generation_fingerprint"]
        if (key in records or key[0] not in tasks or key[1] not in models or not HEX256.fullmatch(key[2])
                or record.get("host_engine_fingerprint") != engine):
            raise ContractError("recovery record has a foreign or duplicate binding")
        records[key] = record
    return records


def overlay_rows(originals, imported, local, config, engines):
    result, errors, merges = {}, {}, 0
    for pair, original in originals.items():
        key = (*pair, canonical_hash(original.get("generation", {})))
        repaired = local.get(key) or imported.get(key)
        chosen = repaired["row"] if repaired else original
        try:
            row, merged = merge_completed_source_judgment(original, chosen, config, engines)
            merges += int(merged)
        except (ContractError, OSError, ValueError, KeyError, TypeError, IndexError) as exc:
            row = copy.deepcopy(chosen)
            errors["::".join(pair)] = type(exc).__name__ + ": " + str(exc)
        result[pair] = row
    return result, errors, merges


def recover_correctness(original, selected, selection, task, recipe, services, config, output, engine,
                        *, evaluate=correctness):
    """This function has no judge or generation invocation path."""
    row = copy.deepcopy(selected)
    generation = row["generation"]
    validate_answer(generation)
    if original.get("generation") != generation or canonical_hash(generation) != selection["source_generation_fingerprint"]:
        raise ContractError("selected completed answer differs from the explicit repair token")
    for key in ("id", "model", "patch_sha256", "trace_sha256"):
        if selection[key] != generation[key]:
            raise ContractError("selected answer differs in " + key)
    if (recipe.fingerprint != generation["runtime_fingerprint"]
            or canonical_hash(task) != generation["task_fingerprint"]):
        raise ContractError("correctness repair changes the original runtime or task")
    judgment = selected.get("judgment", {})
    base_engine = config.get("measurement_recovery", {}).get("base_host_engine_fingerprint", engine)
    validate_completed_judgment(judgment, generation, config["judge"],
                               {generation["engine_fingerprint"], base_engine, engine})
    if canonical_hash(judgment) != selection["judgment_sha256"]:
        raise ContractError("selected complete panel changed")
    if row.get("correctness", {}).get("status") in TERMINAL_CORRECTNESS:
        raise ContractError("completed correctness measurements are not replayed")
    if row.get("correctness", {}).get("status") != "infra_error":
        raise ContractError("correctness-only recovery requires an explicit infrastructure failure")
    key = row["id"], row["model"], selection["source_generation_fingerprint"]
    root = Path(output) / "repairs" / component(key[1]) / component(key[0]) / key[2][:16]
    root.mkdir(parents=True, exist_ok=True)
    before_generation, before_judgment = canonical_hash(generation), canonical_hash(judgment)
    record = {
        "source_generation_fingerprint": key[2], "host_engine_fingerprint": engine, "row": row,
        "measurement_recovery": {
            "method": "correctness-only-with-exact-cached-panel",
            "selection": copy.deepcopy(selection), "preserved_generation_sha256": before_generation,
            "preserved_judgment_sha256": before_judgment, "additional_generation_calls": 0,
            "additional_judge_calls": 0,
        },
    }
    repository = Path(config["repo_cache"]) / task["repo"].replace("/", "__")
    try:
        with services.job(task, recipe, repository, "measurement-correctness:" + key[1]):
            row["correctness"] = evaluate(task, recipe, generation, repository, root / "evaluation",
                                           Path(config["asset_root"]), retry_infra=True)
        row["status"] = "processed"
        row.pop("error", None)
        row.pop("answer_recovery_required", None)
    except Exception as exc:
        row["error"] = type(exc).__name__ + ": " + str(exc)
    if canonical_hash(row["generation"]) != before_generation or canonical_hash(row["judgment"]) != before_judgment:
        raise ContractError("correctness recovery altered a completed answer or cached panel")
    validate_answer(row["generation"])
    atomic_json(root / "result.json", record)
    return record


def publish(output, tasks, models, rows, source_manifest, base, engine, local, imported, errors, merges, services):
    columns = {name: [row[name] for row in rows.values() if row.get(name)]
               for name in ("generation", "correctness", "judgment")}
    table, report = make_report(tasks, models, columns["generation"], columns["correctness"], columns["judgment"])
    for filename, values in (("generation_results", columns["generation"]), ("correctness", columns["correctness"]),
                             ("judgments", columns["judgment"]), ("results", table)):
        write_jsonl(Path(output) / (filename + ".jsonl"), values)
    report.update(
        expected_pairs=len(tasks) * len(models), measured_generations=len(columns["generation"]),
        source_run=source_manifest["source_run"], source_recovery_run=str(base),
        generation_engine_fingerprint=source_manifest["generation_engine_fingerprint"],
        host_recovery_engine_fingerprint=engine, host_repair_records=len({*imported, *local}),
        source_host_recovery_engine_fingerprint=source_manifest.get("base_host_engine_fingerprint", engine),
        measurement_repair_records=len(local), source_panel_merges=merges,
        additional_generation_calls=0, additional_judge_calls=0,
        recovery_admission_errors=errors,
    )
    atomic_json(Path(output) / "report.json", report)
    atomic_json(Path(output) / "resource-usage.json", services.gate.status())
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--base-recovery-run", type=Path, required=True)
    parser.add_argument("--repair-selection", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=30)
    args = parser.parse_args()
    source, base, output = (path.resolve() for path in (args.source_run, args.base_recovery_run, args.out_dir))
    if args.poll_seconds < 1 or any(
            left == right or left.is_relative_to(right) or right.is_relative_to(left)
            for left, right in ((source, output), (base, output))):
        raise ContractError("measurement recovery needs a separate output and bounded polling")
    config = load_config(args.config)
    source_config = json.loads((source / "experiment-config.json").read_text())
    if source_config["judge"] != sanitize_config(config)["judge"]:
        raise ContractError("measurement recovery changes the frozen judge configuration")
    tasks = selected_tasks(Path(config["tasks"]), Path(config["scope_ids"]) if config.get("scope_ids") else None)
    original_manifest = json.loads((source / "experiment-manifest.json").read_text())
    if original_manifest["task_ids"] != list(tasks) or original_manifest["tasks_fingerprint"] != canonical_hash(tasks):
        raise ContractError("measurement recovery changes the original task scope")
    models = original_manifest["models"]
    engine = engine_identity()["fingerprint"]
    base_engine = config.get("measurement_recovery", {}).get("base_host_engine_fingerprint", engine)
    if not HEX256.fullmatch(base_engine):
        raise ContractError("base grading engine must be an explicit fingerprint")
    base_hash = validate_base_manifest(base, source, original_manifest, base_engine, config["judge"])
    selection = load_repair_selection(args.repair_selection, tasks, models)
    selection_hash = file_hash(args.repair_selection)
    source_hash = file_hash(source / "experiment-manifest.json")
    config = {**config, "out_dir": str(output)}
    services = ExecutionServices(config)
    manifest = {
        "schema_version": "reviewbench-measurement-recovery/v1", "source_run": str(source),
        "source_manifest_sha256": source_hash, "base_recovery_run": str(base),
        "base_recovery_manifest_sha256": base_hash, "generation_engine_fingerprint": original_manifest["engine_fingerprint"],
        "host_recovery_engine_fingerprint": engine, "controller_sha256": file_hash(Path(__file__)),
        "base_host_engine_fingerprint": base_engine,
        "repair_selection_sha256": selection_hash, "task_ids": list(tasks),
        "tasks_fingerprint": canonical_hash(tasks), "models": models, "expected_pairs": len(tasks) * len(models),
        "additional_generation_calls": 0, "additional_judge_calls": 0,
    }
    with output_lock(output):
        path = output / "experiment-manifest.json"
        if path.exists() and json.loads(path.read_text()) != manifest:
            raise ContractError("measurement recovery bindings changed; use a new output")
        atomic_json(path, manifest)
        local = load_repairs(output, tasks, models, engine)
        attempted = set(local)
        admission_errors = {}
        while True:
            if (file_hash(source / "experiment-manifest.json") != source_hash
                    or file_hash(base / "experiment-manifest.json") != base_hash
                    or file_hash(args.repair_selection) != selection_hash):
                raise ContractError("live source or explicit repair selection changed")
            originals = load_rows(source, tasks, models, original_manifest["engine_fingerprint"])
            imported = load_repairs(base, tasks, models, base_engine)
            rows, errors, merges = overlay_rows(originals, imported, local, config["judge"],
                                                {original_manifest["engine_fingerprint"], base_engine, engine})
            recipes = load_recipes(source / "fixed-runtime-recipes.jsonl")
            for key, requested in selection.items():
                if key in attempted or key[:2] not in originals:
                    continue
                attempted.add(key)
                try:
                    if key[:2] in rows and rows[key[:2]].get("correctness", {}).get("status") in TERMINAL_CORRECTNESS:
                        continue
                    record = recover_correctness(originals[key[:2]], rows[key[:2]], requested, tasks[key[0]],
                                                   recipes[key[0]], services, config, output, engine,
                                                   evaluate=correctness)
                    local[key] = record
                    print(json.dumps({"event": "measurement-correctness-recovered", "id": key[0], "model": key[1],
                                      "correctness": record["row"].get("correctness", {}).get("status"),
                                      "judgment": record["row"].get("judgment", {}).get("status"),
                                      "additional_generation_calls": 0, "additional_judge_calls": 0,
                                      "error": record["row"].get("error")}), flush=True)
                except (ContractError, OSError, ValueError, KeyError, TypeError) as exc:
                    admission_errors["::".join(key[:2])] = type(exc).__name__ + ": " + str(exc)
            # Reload live sources after potentially long correctness execution.
            if (file_hash(source / "experiment-manifest.json") != source_hash
                    or file_hash(base / "experiment-manifest.json") != base_hash
                    or file_hash(args.repair_selection) != selection_hash):
                raise ContractError("live source or repair selection changed during correctness recovery")
            originals = load_rows(source, tasks, models, original_manifest["engine_fingerprint"])
            imported = load_repairs(base, tasks, models, base_engine)
            rows, overlay_errors, merges = overlay_rows(originals, imported, local, config["judge"],
                                                        {original_manifest["engine_fingerprint"], base_engine, engine})
            report = publish(output, tasks, models, rows, manifest, base, engine, local, imported,
                             {**admission_errors, **errors, **overlay_errors}, merges, services)
            if not args.watch or report["complete"]:
                print(json.dumps(report, indent=2), flush=True)
                return 0 if report["complete"] else 2
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
