"""reviewbench-agent prepare/run/evaluate/report."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from . import PROTOCOL
from .contracts import (
    ContractError, InfrastructureError, atomic_json, canonical_hash, component, file_hash,
    read_jsonl, selected_tasks, write_jsonl,
)
from .preparation import load_recipes, prepare, repository_path, verify_recipe
from .reporting import make_report
from .judge_policy import policy_from_config
from .scheduling import completed_jobs, output_lock, worker_count


def load_config(path: Path, overrides: dict | None = None) -> dict:
    try:
        import yaml
    except ImportError as exc:
        raise InfrastructureError("install the agentic optional dependency to load YAML configuration") from exc
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict) or raw.get("protocol") != PROTOCOL:
        raise ContractError("configuration must declare reviewbench-agentic-exec/v1")
    raw.update({key: value for key, value in (overrides or {}).items() if value is not None})
    root = (path.resolve().parent / raw.pop("root", ".")).resolve()
    for field in ("tasks", "scope_ids", "oracles", "repo_cache", "asset_root", "runtime_recipes", "out_dir"):
        value = raw.get(field)
        if value is None:
            continue
        expanded = os.path.expanduser(os.path.expandvars(str(value)))
        if "${" in expanded or (expanded.startswith("$") and "/" not in expanded):
            raise ContractError(f"unresolved environment variable in {field}: {value}")
        item = Path(expanded)
        raw[field] = str(item if item.is_absolute() else root / item)
    for required in ("tasks", "oracles", "repo_cache", "asset_root", "out_dir"):
        if not raw.get(required):
            raise ContractError(f"configuration missing {required}")
    for section, fields in (("execution", ("gate_dir",)),
                            ("storage", ("cache_dir", "scratch_dir", "source_catalog"))):
        for field in fields:
            value = raw.get(section, {}).get(field)
            if value:
                expanded = os.path.expanduser(os.path.expandvars(str(value)))
                if "${" in expanded:
                    raise ContractError(f"unresolved environment variable in {section}.{field}")
                item = Path(expanded)
                raw[section][field] = str(item if item.is_absolute() else root / item)
    remote = raw.get("storage", {}).get("remote", {})
    if remote.get("kind") == "directory":
        item = Path(os.path.expanduser(os.path.expandvars(remote["path"])))
        remote["path"] = str(item if item.is_absolute() else root / item)
    return raw


def load_inputs(config: dict):
    tasks = selected_tasks(Path(config["tasks"]), Path(config["scope_ids"]) if config.get("scope_ids") else None)
    oracles = read_jsonl(Path(config["oracles"]))
    recipes = load_recipes(Path(config["runtime_recipes"]) if config.get("runtime_recipes") else None)
    return tasks, oracles, recipes


def prepare_command(config: dict, args) -> dict:
    workers = worker_count(config, args)
    tasks, oracles, recipes = load_inputs(config)
    output = Path(config["out_dir"]) / "preparation"
    report = prepare(tasks, oracles, recipes, Path(config["repo_cache"]), Path(config["asset_root"]), output)
    if args.verify:
        from .execution import ExecutionServices
        services = ExecutionServices(config)
        proofs = []
        def verify(ident):
            recipe = recipes[ident]
            task = tasks[ident]
            repo = repository_path(Path(config["repo_cache"]), task["repo"])
            with services.job(task, recipe, repo, "verify"):
                return verify_recipe(task, recipe, oracles[ident], repo,
                                     Path(config["asset_root"]), output / "verified" / component(ident))
        for _, proof in completed_jobs(verify, recipes, workers):
            proofs.append(proof)
        report["execution_verified"] = sum(p["status"] == "ready" for p in proofs)
        report["complete"] = report["execution_verified"] == len(tasks) and not report["blocked"]
        report["verification_failures"] = {p["id"]: p.get("error") for p in proofs if p["status"] != "ready"}
        atomic_json(output / "readiness.json", report)
        atomic_json(output / "resource-usage.json", services.gate.status())
    return report


def require_ready(tasks: dict, recipes: dict, config: dict, oracles: dict) -> None:
    from .models import engine_identity
    engine = engine_identity()["fingerprint"]
    output = Path(config["out_dir"]) / "preparation"
    for ident, task in tasks.items():
        recipe = recipes.get(ident)
        if not recipe:
            raise ContractError(f"runtime recipe missing: {ident}")
        path = output / "verified" / component(ident) / "verification.json"
        if not path.is_file():
            raise ContractError(f"runtime has not passed prepare --verify: {ident}")
        proof = json.loads(path.read_text())
        if (proof.get("status") != "ready" or proof.get("task_fingerprint") != canonical_hash(task)
                or proof.get("runtime_fingerprint") != recipe.fingerprint
                or proof.get("oracle_fingerprint") != canonical_hash(oracles[ident])
                or proof.get("engine_fingerprint") != engine):
            raise ContractError(f"stale or failed runtime verification: {ident}")


def _model_config(config: dict, model: str) -> dict:
    if model not in config.get("models", {}):
        raise ContractError(f"model is not explicitly configured: {model}")
    record = config["models"][model]
    if "REPLACE_" in json.dumps(record):
        raise ContractError("replace the example model ID before inference")
    # The selected matrix key is authoritative; a copied adapter config's
    # optional id must not collapse two model arms into the same result key.
    return {**record, "id": model}


def _run_manifest(config: dict, tasks: dict, models: list[str], recipes: dict) -> dict:
    from .models import engine_identity
    from .execution import source_identity
    # Physical paths can change when a bundle is moved; task, runtime and
    # behavior-setting identities are content based.
    behavior = {k: config.get(k) for k in ("protocol", "agent", "budget", "observation", "models", "judge")}
    result = {"protocol": PROTOCOL, "tasks_fingerprint": canonical_hash(tasks), "models": models,
            "config_fingerprint": canonical_hash(behavior),
            "engine_fingerprint": engine_identity()["fingerprint"],
            "runtime_fingerprints": {key: value.fingerprint for key, value in recipes.items()},
            "expected_task_model_pairs": len(tasks) * len(models)}
    if source_identity(config) is not None:
        result["source_identity"] = source_identity(config)
    return result


def run_command(config: dict, args) -> dict:
    from .runner import run_episode
    from .models import sanitize_config
    workers = worker_count(config, args)
    tasks, oracles, recipes = load_inputs(config)
    require_ready(tasks, recipes, config, oracles)
    models = args.model or list(config.get("models", {}))
    if not models or len(models) != len(set(models)):
        raise ContractError("provide a nonempty, unique model selection")
    settings = {}
    from .execution import ExecutionServices
    services = ExecutionServices(config)
    for model in models:
        settings[model] = {key: config.get(key, {}) for key in ("agent", "budget", "observation")}
        settings[model]["model"] = _model_config(config, model)
    root = Path(config["out_dir"]) / "run"
    root.mkdir(parents=True, exist_ok=True)
    manifest = _run_manifest(config, tasks, models, recipes)
    existing = root / "run-manifest.json"
    if existing.is_file() and json.loads(existing.read_text()) != manifest:
        raise ContractError("run directory belongs to a different task/model/runtime/configuration matrix")
    atomic_json(existing, manifest)
    atomic_json(root / "run-config.json", sanitize_config(config))
    matrix = [(model, ident) for model in models for ident in tasks]
    completed = {}

    def run_one(pair):
        model, ident = pair
        task = tasks[ident]
        repo = repository_path(Path(config["repo_cache"]), task["repo"])
        episode = root / "models" / component(model) / "episodes" / component(ident)
        with services.job(task, recipes[ident], repo, "run:" + model, episode=episode,
                          resume=args.resume or args.retry_infra):
            result = run_episode(
                task, recipes[ident], settings[model], repo, episode, Path(config["asset_root"]),
                resume=args.resume or args.retry_infra, retry_infra=args.retry_infra,
            )
            if services.cache and config["storage"].get("archive_terminal", False):
                from .retention import archive_episode
                archive_episode(episode, result, services)
            return result

    for pair, result in completed_jobs(run_one, matrix, workers):
        completed[pair] = result
        model_root = root / "models" / component(pair[0])
        write_jsonl(model_root / "generation_results.jsonl",
                    [completed[(pair[0], ident)] for ident in tasks if (pair[0], ident) in completed])
        # Stable task×model ordering independent of worker completion order.
        write_jsonl(root / "generation_results.jsonl", [completed[p] for p in matrix if p in completed])
    all_results = [completed[p] for p in matrix]
    summary = {"protocol": PROTOCOL, "expected": len(tasks) * len(models), "records": len(all_results),
               "generated": sum(r["status"] == "generated" for r in all_results),
               "model_failed": sum(r["status"] == "generation_failed" for r in all_results),
               "infra_errors": sum(r["status"] == "infra_error" for r in all_results)}
    summary["complete"] = not summary["infra_errors"] and summary["records"] == summary["expected"]
    atomic_json(root / "generation-summary.json", summary)
    atomic_json(root / "resource-usage.json", services.gate.status())
    return summary


def read_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def validated_generations(root: Path) -> list[dict]:
    rows = read_rows(root / "generation_results.jsonl")
    for row in rows:
        if row.get("status") == "generated":
            path = Path(row["patch_file"])
            if not path.is_file() or file_hash(path) != row.get("patch_sha256"):
                raise ContractError("generation patch bytes differ from the recorded digest")
        if row.get("trace_sha256"):
            path = Path(row["trace_file"])
            if not path.is_file() or file_hash(path) != row["trace_sha256"]:
                raise ContractError("generation trajectory differs from the recorded digest")
    return rows


def _bound_run(config: dict, *, regrade: bool = False):
    tasks, oracles, recipes = load_inputs(config)
    root = Path(config["out_dir"]) / "run"
    manifest = json.loads((root / "run-manifest.json").read_text())
    expected = _run_manifest(config, tasks, manifest["models"], recipes)
    if regrade:
        # Explicitly allow a new grading implementation, never a new model
        # generation or a different task/runtime/model/rubric configuration.
        expected["engine_fingerprint"] = manifest["engine_fingerprint"]
    if manifest != expected:
        raise ContractError("run manifest no longer matches current inputs")
    if any(row.get("engine_fingerprint") != manifest["engine_fingerprint"] for row in validated_generations(root)):
        raise ContractError("run contains mixed or unbound generation engines")
    return tasks, recipes, root, manifest


def evaluate_command(config: dict, args) -> dict:
    from .evaluation import correctness, judge_candidate
    from .models import engine_identity
    workers = worker_count(config, args)
    regrade = getattr(args, "regrade", False)
    tasks, recipes, root, manifest = _bound_run(config, regrade=regrade)
    engine_record = {
        "generation_engine_fingerprint": manifest["engine_fingerprint"],
        "evaluation_engine": engine_identity(), "run_manifest_fingerprint": canonical_hash(manifest),
    }
    engine_path = root / "evaluation-engine.json"
    if engine_path.is_file():
        previous = json.loads(engine_path.read_text())
        if previous != engine_record:
            atomic_json(root / f"evaluation-engine-{canonical_hash(previous)[:16]}.json", previous)
    atomic_json(engine_path, engine_record)
    generations = [r for r in validated_generations(root) if r["status"] == "generated"]
    from .execution import ExecutionServices
    services = ExecutionServices(config)

    def evaluate_one(generation):
        ident, model = generation["id"], generation["model"]
        task = tasks[ident]
        output = root / "evaluation" / component(model) / component(ident)
        repo = repository_path(Path(config["repo_cache"]), task["repo"])
        with services.job(task, recipes[ident], repo, "evaluate:" + model):
            c = correctness(task, recipes[ident], generation, repo, output, Path(config["asset_root"]),
                            retry_infra=args.retry_infra, regrade=regrade)
            j = judge_candidate(task, generation, repo, config["judge"], output / "judgment.json",
                                retry_infra=args.retry_infra, regrade=regrade)
        return c, j

    completed = {}
    for generation, result in completed_jobs(evaluate_one, generations, workers):
        completed[(generation["id"], generation["model"])] = result
        ordered = [completed[(g["id"], g["model"])] for g in generations
                   if (g["id"], g["model"]) in completed]
        write_jsonl(root / "correctness.jsonl", [c for c, _ in ordered])
        write_jsonl(root / "judgments.jsonl", [j for _, j in ordered])
    atomic_json(root / "evaluation-resource-usage.json", services.gate.status())
    return report_command(config, args)


def report_command(config: dict, args) -> dict:
    tasks, recipes, root, manifest = _bound_run(config, regrade=getattr(args, "regrade", False))
    cs, js = read_rows(root / "correctness.jsonl"), read_rows(root / "judgments.jsonl")
    engine_path = root / "evaluation-engine.json"
    grading_engine = json.loads(engine_path.read_text()) if engine_path.is_file() else None
    if grading_engine and any(row.get("engine_fingerprint") != grading_engine["evaluation_engine"]["fingerprint"]
                              for row in [*cs, *js]):
        raise ContractError("aggregate grading records belong to a different evaluation engine")
    rows, report = make_report(tasks, manifest["models"], validated_generations(root),
                               cs, js, judge_policy=policy_from_config(config["judge"]) if config.get("judge") else None)
    if grading_engine:
        report["generation_engine_fingerprint"] = manifest["engine_fingerprint"]
        report["evaluation_engine_fingerprint"] = grading_engine["evaluation_engine"]["fingerprint"]
    write_jsonl(root / "results.jsonl", rows)
    atomic_json(root / "report.json", report)
    sealed = {}
    for file in root.rglob("*"):
        if (file.is_file() and not file.is_symlink() and file.name != "artifact-manifest.json"
                and not any(p in {"workspace", "start", ".git"} for p in file.relative_to(root).parts)):
            sealed[file.relative_to(root).as_posix()] = file_hash(file)
    atomic_json(root / "artifact-manifest.json", {"protocol": PROTOCOL, "complete": report["complete"], "sha256": sealed})
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "run", "evaluate", "report"):
        command = sub.add_parser(name)
        command.add_argument("--config", type=Path, required=True)
        for option in ("tasks", "scope-ids", "oracles", "repo-cache", "asset-root", "runtime-recipes", "out-dir"):
            command.add_argument(f"--{option}")
        if name != "report":
            command.add_argument("--workers", type=int, help="independent task/model jobs (default: execution.workers or 1)")
        if name == "prepare":
            command.add_argument("--verify", action="store_true", help="run public checks and stable start/golden Oracles")
        if name == "run":
            command.add_argument("--model", action="append")
            command.add_argument("--resume", action="store_true")
            command.add_argument("--retry-infra", action="store_true")
        if name == "evaluate":
            command.add_argument("--retry-infra", action="store_true")
        if name in {"evaluate", "report"}:
            command.add_argument("--regrade", action="store_true",
                                 help="use this grading engine with frozen older-engine generations; preserve valid votes")
    args = parser.parse_args(argv)
    overrides = {key: getattr(args, key, None) for key in
                 ("tasks", "scope_ids", "oracles", "repo_cache", "asset_root", "runtime_recipes", "out_dir")}
    try:
        config = load_config(args.config, overrides)
        with output_lock(Path(config["out_dir"])):
            result = {"prepare": prepare_command, "run": run_command, "evaluate": evaluate_command,
                      "report": report_command}[args.command](config, args)
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0 if result.get("complete") else 2
    except (ContractError, InfrastructureError, OSError, ValueError, KeyError) as exc:
        print(json.dumps({"complete": False, "error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
