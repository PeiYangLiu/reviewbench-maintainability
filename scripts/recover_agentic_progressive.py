#!/usr/bin/env python3
"""Repair host-side measurements without changing or resampling agent answers.

The live generation experiment remains read-only. An independently versioned
output overlays only exact-workspace serialization repairs and failed frozen
symlink restoration. Completed correctness verdicts and judge votes are reused.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, os.environ.get("REVIEWBENCH_ENGINE_SRC", str(Path(__file__).resolve().parents[1] / "src")))
from reviewbench_maintainability.agentic.cli import load_config
from reviewbench_maintainability.agentic.contracts import (
    ContractError, atomic_json, canonical_hash, component, file_hash, selected_tasks, write_jsonl,
)
from reviewbench_maintainability.agentic.evaluation import correctness, judge_candidate, invoke_judge
from reviewbench_maintainability.agentic.execution import ExecutionServices
from reviewbench_maintainability.agentic.models import engine_identity, sanitize_config
from reviewbench_maintainability.agentic.preparation import load_recipes
from reviewbench_maintainability.agentic.reporting import make_report
from reviewbench_maintainability.agentic.resources import job_directory
from reviewbench_maintainability.agentic.retention import restore_terminal
from reviewbench_maintainability.agentic.scheduling import output_lock
from reviewbench_maintainability.agentic.snapshots import collect_patch, extract_start, tree_hash
from run_agentic_progressive import completed_answer_needs_host_recovery, pair_complete
from host_recovery_ownership import (
    load_inherited, original_owned_row, publish_combined, validate_inherited_sources, TERMINAL_CHECKS,
)


def requires_recovery(row):
    generation = row.get("generation", {})
    if completed_answer_needs_host_recovery(generation):
        manifest = Path(generation["trace_file"]).parent / "terminal-archive.json"
        return manifest.is_file() and json.loads(manifest.read_text()).get("local_disposal_complete") is True
    check = row.get("correctness", {})
    # A completed model response is not a terminal source pair. The primary
    # runner still owns correctness and its three judge attempts until it
    # publishes the final pair. In particular, a pending panel is not empty.
    return (row.get("status") == "processed" and
            generation.get("status") == "generated" and check.get("status") == "infra_error"
            and check.get("error", "").startswith("ContractError: immutable test missing at start: "))


def recovered_generation(original, collected, trace, engine, evidence):
    result = copy.deepcopy(original)
    result.update(collected, status="generated" if collected["apply_ok"] else "generation_failed", error=None)
    ending = trace["messages"][-1]["extra"]["exit_status"]
    result["stop_reason"] = {"RepeatedFormatError": "format_error_limit", "LimitsExceeded": "model_call_limit",
                             "TimeExceeded": "wall_time_limit"}.get(ending, ending)
    # The agent/inference engine and every consumed token/call remain original.
    # The separately named serializer identifies the host-only code repair.
    result.update(serialization_engine_fingerprint=engine, host_recovery=evidence,
                  additional_generation_calls=0)
    return result


def first_panel_invoker(root, binding, *, invoke=invoke_judge):
    """Durable member claims reuse replies and do not reset ambiguous requests."""
    def request(model, prompt, config):
        if model not in config["models"]:
            raise ContractError("first serialization panel requested a foreign model")
        claim = {**binding, "judge_model": model,
                 "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()}
        directory = root / "first-panel-members" / component(model)
        directory.mkdir(parents=True, exist_ok=True)
        if any(path.is_symlink() for path in (directory, *directory.parents)
               if path == root or path.is_relative_to(root)):
            raise ContractError("first serialization claim path became a symlink")
        started, response = directory / "started.json", directory / "response.json"
        if started.is_symlink() or response.is_symlink():
            raise ContractError("first serialization claim file became a symlink")
        if response.exists() and not started.exists():
            raise ContractError("orphan first serialization response has no original start receipt")
        if started.exists():
            if json.loads(started.read_text()) != claim:
                raise ContractError("first serialization member changed its original identity or prompt")
            if not response.is_file() or response.is_symlink():
                raise ContractError("first serialization member already started; response pending or attempt consumed")
            cached = json.loads(response.read_text())
            if cached["binding"] != claim:
                raise ContractError("first serialization cached member binding changed")
            if "error" in cached:
                raise ContractError("cached first serialization member failure: " + cached["error"])
            if cached["response"].get("judge_model") != model:
                raise ContractError("cached first serialization member belongs to another model")
            return cached["response"]
        with started.open("x") as stream:
            json.dump(claim, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            value = invoke(model, prompt, config)
        except Exception as exc:
            atomic_json(response, {"binding": claim, "error": type(exc).__name__ + ": " + str(exc)})
            raise
        atomic_json(response, {"binding": claim, "response": value})
        return value
    return request


def complete_measurements(original, row, task, recipe, repo, root, config, *, retry_infra=False,
                          evaluate=correctness, judge=judge_candidate, invoke=invoke_judge,
                          persist=lambda: None):
    """Correctness-only work cannot judge; serialization has one owned first panel."""
    generation = row["generation"]
    if generation["status"] != "generated":
        return
    generated_source = original["generation"].get("status") == "generated"
    if generated_source:
        if original.get("status") != "processed" or generation != original["generation"]:
            raise ContractError("correctness recovery requires an unchanged terminal original pair")
        owned = original_owned_row(original, row)
        if "judgment" in owned:
            row["judgment"] = owned["judgment"]
        else:
            row.pop("judgment", None)
    elif not generation.get("host_recovery") or generation.get("additional_generation_calls") != 0:
        raise ContractError("serialization recovery lacks the original completed-answer binding")
    if row.get("correctness", {}).get("status") not in TERMINAL_CHECKS:
        row["correctness"] = evaluate(task, recipe, generation, repo, root / "evaluation",
                                     Path(config["asset_root"]), retry_infra=retry_infra)
        persist()
    if generated_source:
        # Preserve completed, rejected, partial and absent primary panels alike.
        # Other explicitly budgeted recovery owners handle missing judge work.
        return
    if original.get("judgment"):
        raise ContractError("serialized source already owns judge evidence; no new panel is admitted")
    if row.get("judgment", {}).get("status") == "completed":
        return
    marker = root / "first-panel-started.json"
    panel_file = root / "judgment.json"
    binding = {"source_generation_fingerprint": canonical_hash(original["generation"]),
               "recovered_generation_fingerprint": canonical_hash(generation),
               "judge_fingerprint": canonical_hash(config["judge"]),
               "generation_calls": 0, "maximum_first_panel_starts": 1}
    if marker.exists():
        if marker.is_symlink() or json.loads(marker.read_text()) != binding:
            raise ContractError("first serialization panel binding changed; existing attempt stays consumed")
        if panel_file.is_file():
            cached = json.loads(panel_file.read_text())
            if any(cached.get(key) != generation.get(key) for key in
                   ("id", "model", "protocol", "task_fingerprint", "patch_sha256")):
                raise ContractError("cached serialization panel differs from the original recovered answer")
            row["judgment"] = cached
            return
        if not (root / "first-panel-members").is_dir():
            raise ContractError("first serialization panel already started without member receipts; attempt stays consumed")
    else:
        with marker.open("x") as stream:
            json.dump(binding, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
    # This is only the first panel for a byte-recovered answer which the primary
    # runner never judged. It is not a retry of any accepted or failed panel.
    row["judgment"] = judge(task, generation, repo, config["judge"], panel_file, retry_infra=False,
                            invoke=first_panel_invoker(root, binding, invoke=invoke))
    persist()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--retry-infra", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=30)
    parser.add_argument("--inheritance", type=Path,
                        help="immutable imported attempts; none receive another allowance")
    parser.add_argument("--base-publication", type=Path,
                        help="current full-scope publication; never replaced by an intermediate host table")
    parser.add_argument("--publication-dir", type=Path,
                        help="new owner for combined cached-only epochs")
    parser.add_argument("--cached-only", action="store_true",
                        help="validate/import/publish without dispatching any repair")
    args = parser.parse_args()
    if args.workers < 1:
        raise ContractError("recovery workers must be positive")
    source, output = args.source_run.resolve(), args.out_dir.resolve()
    if source == output or output.is_relative_to(source):
        raise ContractError("host recovery needs its own output, not an in-place experiment rewrite")
    config = load_config(args.config)
    tasks = selected_tasks(Path(config["tasks"]), Path(config["scope_ids"]) if config.get("scope_ids") else None)
    source_manifest = json.loads((source / "experiment-manifest.json").read_text())
    source_config = json.loads((source / "experiment-config.json").read_text())
    models = source_manifest["models"]
    if source_manifest["tasks_fingerprint"] != canonical_hash(tasks) or source_manifest["task_ids"] != list(tasks):
        raise ContractError("host recovery task scope differs from the generation experiment")
    if source_config["judge"] != sanitize_config(config)["judge"]:
        raise ContractError("host recovery must retain the original judge panel and configuration")
    config = {**config, "out_dir": str(output)}
    services = ExecutionServices(config)
    engine = engine_identity()["fingerprint"]
    manifest = {"schema_version": "reviewbench-host-recovery/v1", "source_run": str(source),
                "source_manifest_sha256": file_hash(source / "experiment-manifest.json"),
                "generation_engine_fingerprint": source_manifest["engine_fingerprint"],
                "host_recovery_engine_fingerprint": engine, "controller_sha256": file_hash(Path(__file__)),
                "task_ids": list(tasks), "tasks_fingerprint": canonical_hash(tasks), "models": models,
                "expected_pairs": len(tasks) * len(models), "judge_fingerprint": canonical_hash(config["judge"]),
                "additional_generation_calls": 0}
    inherited, inheritance = ({}, None)
    if args.inheritance:
        inherited, inheritance = load_inherited(args.inheritance, source, source_manifest, tasks, models)
        manifest["inheritance"] = inheritance
    if bool(args.base_publication) != bool(args.publication_dir):
        raise ContractError("base publication and new publication owner must be supplied together")
    if args.base_publication:
        if not inheritance:
            raise ContractError("combined publication requires explicit consumed-attempt inheritance")
        base_publication = args.base_publication.resolve()
        publication = args.publication_dir.resolve()
        if publication == output or source == publication or publication.is_relative_to(source):
            raise ContractError("combined publication must have an independent owner")
        manifest.update(base_publication=str(base_publication),
                        base_publication_manifest_sha256=file_hash(base_publication / "experiment-manifest.json"),
                        publication_owner=str(publication))
    manifest["lifecycle"] = {
        "generated_sources_wait_for_processed_pair": True,
        "correctness_recovery_judge_calls": 0,
        "original_panels_reused_even_if_incomplete": True,
        "serialization_first_panel_starts_max": 1,
        "serialization_first_calls_per_member_max": 1,
    }
    manifest["cached_only"] = args.cached_only
    with output_lock(output):
        manifest_file = output / "experiment-manifest.json"
        if manifest_file.exists() and json.loads(manifest_file.read_text()) != manifest:
            raise ContractError("host recovery bindings changed; use a new output directory")
        atomic_json(manifest_file, manifest)
        updates = {}
        for path in (output / "repairs").glob("*/*/*/result.json"):
            record = json.loads(path.read_text())
            updates[(record["row"]["id"], record["row"]["model"], record["source_generation_fingerprint"])] = record
        attempted = set(inherited) | {
            key for key, value in updates.items() if not args.retry_infra or pair_complete(value["row"])}
        admission_errors = {}

        def source_rows():
            if file_hash(source / "experiment-manifest.json") != manifest["source_manifest_sha256"]:
                raise ContractError("source experiment identity changed during recovery")
            rows = {}
            for path in (source / "pairs").glob("*/*/pair-result.json"):
                row = json.loads(path.read_text())
                key = row["id"], row["model"]
                if key[0] not in tasks or key[1] not in models or key in rows:
                    raise ContractError("foreign or duplicate generation experiment pair")
                generation = row.get("generation", {})
                if generation and generation["engine_fingerprint"] != manifest["generation_engine_fingerprint"]:
                    raise ContractError("generation engine changed during host recovery")
                rows[key] = row
            return rows

        def save_status(rows):
            gs, cs, js = [], [], []
            for key, original in rows.items():
                generation = original.get("generation", {})
                token = (*key, canonical_hash(generation))
                repaired = updates.get(token) or inherited.get(token)
                row = repaired["row"] if repaired else original
                if token in updates:
                    row = original_owned_row(original, row)
                for field, records in (("generation", gs), ("correctness", cs), ("judgment", js)):
                    if row.get(field):
                        records.append(row[field])
            table, report = make_report(tasks, models, gs, cs, js)
            for name, records in (("generation_results", gs), ("correctness", cs), ("judgments", js), ("results", table)):
                write_jsonl(output / (name + ".jsonl"), records)
            report.update(expected_pairs=manifest["expected_pairs"], source_run=str(source),
                          measured_generations=len(gs), additional_generation_calls=0,
                          host_repair_records=len(inherited) + len(updates),
                          inherited_host_repair_records=len(inherited),
                          new_host_repair_records=len(updates),
                          correctness_recovery_judge_calls=0,
                          waiting_for_original_terminal=sum(
                              row.get("status") != "processed" and
                              row.get("generation", {}).get("status") == "generated" and
                              row.get("correctness", {}).get("error", "").startswith(
                                  "ContractError: immutable test missing at start: ")
                              for row in rows.values()),
                          recovery_admission_errors=dict(admission_errors),
                          host_recovery_engine_fingerprint=engine,
                          generation_engine_fingerprint=manifest["generation_engine_fingerprint"])
            atomic_json(output / "report.json", report)
            atomic_json(output / "resource-usage.json", services.gate.status())
            if args.base_publication:
                published = publish_combined(
                    publication, base_publication, manifest["base_publication_manifest_sha256"],
                    tasks, models, rows, updates, inherited, manifest, config["judge"])
                atomic_json(output / "publication-status.json", published)
            return report

        def recover(original, recipe):
            ident, model = original["id"], original["model"]
            old = original["generation"]
            token = canonical_hash(old)
            root = output / "repairs" / component(model) / component(ident) / token[:16]
            root.mkdir(parents=True, exist_ok=True)
            saved = updates.get((ident, model, token))
            row = copy.deepcopy(saved["row"] if saved else original)
            record = {"source_generation_fingerprint": token, "host_engine_fingerprint": engine, "row": row,
                      "source_pair_status_at_admission": original.get("status"), "completed": False,
                      "mode": ("terminal-correctness-only" if old.get("status") == "generated"
                               else "archived-answer-serialization")}
            task, repo = tasks[ident], Path(config["repo_cache"]) / tasks[ident]["repo"].replace("/", "__")
            try:
                if old["runtime_fingerprint"] != recipe.fingerprint:
                    raise ContractError("recovery runtime differs from the original agent runtime")
                with services.job(task, recipe, repo, "host-recovery:" + model):
                    if old["status"] == "infra_error" and not row.get("generation", {}).get("host_recovery"):
                        episode = Path(old["trace_file"]).parent.resolve()
                        if not episode.is_relative_to(source):
                            raise ContractError("archived episode is outside the source experiment")
                        archive_path = episode / "terminal-archive.json"
                        archive = json.loads(archive_path.read_text())
                        if (archive["workspace_reconstruction"] != "archived-working-files"
                                or archive["source_catalog_sha256"] != services.sources.sha256):
                            raise ContractError("terminal archive/source catalog bindings differ")
                        with job_directory(prefix="reviewbench-host-recovery-") as temporary:
                            scratch = Path(temporary)
                            restore_terminal(archive_path, services.cache, scratch / "restored")
                            restored = scratch / "restored"
                            if json.loads((restored / "result.json").read_text()) != old:
                                raise ContractError("archived result is not the original failed measurement")
                            if file_hash(restored / "trajectory.json") != old["trace_sha256"]:
                                raise ContractError("archived agent trajectory differs")
                            trace = json.loads((restored / "trajectory.json").read_text())
                            if tree_hash(restored / "workspace") != trace["workspace_sha256"]:
                                raise ContractError("archived workspace differs from the terminal agent checkpoint")
                            extract_start(repo, task["start_sha"], scratch / "base")
                            if tree_hash(scratch / "base") != json.loads((restored / "snapshot.json").read_text())["tree_sha256"]:
                                raise ContractError("recovered exact-start snapshot differs")
                            collected = collect_patch(scratch / "base", restored / "workspace", root / "candidate.patch")
                            evidence = {"method": "byte-exact-archived-terminal-workspace",
                                        "archive_manifest": str(archive_path), "archive_sha256": file_hash(archive_path),
                                        "original_result_sha256": file_hash(restored / "result.json"),
                                        "workspace_sha256": trace["workspace_sha256"]}
                            row["generation"] = recovered_generation(old, collected, trace, engine, evidence)
                        row.pop("correctness", None)
                        row.pop("judgment", None)
                        atomic_json(root / "result.json", record)
                    complete_measurements(
                        original, row, task, recipe, repo, root, config, retry_infra=args.retry_infra,
                        persist=lambda: atomic_json(root / "result.json", record))
                    row.update(status="processed")
                    row.pop("error", None)
                    row.pop("answer_recovery_required", None)
            except Exception as exc:
                row["error"] = f"{type(exc).__name__}: {exc}"
            record["completed"] = True
            atomic_json(root / "result.json", record)
            return record

        with ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix="host-recovery") as executor:
            pending = {}
            while True:
                rows = source_rows()
                validate_inherited_sources(inherited, rows, manifest["generation_engine_fingerprint"])
                if inheritance and file_hash(args.inheritance) != inheritance["sha256"]:
                    raise ContractError("inherited attempt set changed during recovery")
                recipes = load_recipes(source / "fixed-runtime-recipes.jsonl")
                for key, row in rows.items():
                    if len(pending) >= args.workers:
                        break
                    token = (*key, canonical_hash(row.get("generation", {})))
                    if token in attempted or args.cached_only:
                        continue
                    try:
                        eligible = requires_recovery(row)
                        admission_errors.pop(key[0] + "::" + key[1], None)
                    except ContractError as exc:
                        # One corrupt/unbound attempt remains an explicit
                        # missing measurement; it must not stop other tasks.
                        admission_errors[key[0] + "::" + key[1]] = str(exc)
                        continue
                    if not eligible:
                        continue
                    attempted.add(token)
                    pending[executor.submit(recover, row, recipes[key[0]])] = token
                if pending:
                    done, _ = wait(pending, timeout=args.poll_seconds, return_when=FIRST_COMPLETED)
                    for future in done:
                        key = pending.pop(future)
                        updates[key] = future.result()
                        row = updates[key]["row"]
                        print(json.dumps({"event": "host-recovered", "id": key[0], "model": key[1],
                                          "correctness": row.get("correctness", {}).get("status"),
                                          "judgment": row.get("judgment", {}).get("status"),
                                          "additional_generation_calls": 0, "error": row.get("error")}), flush=True)
                report = save_status(source_rows())
                if not pending:
                    if report["complete"] or not args.watch:
                        print(json.dumps(report, indent=2))
                        return 0 if report["complete"] else 2
                    time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
