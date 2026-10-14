#!/usr/bin/env python3
"""Merge independent migration lanes without discarding the full task scope or proof identity."""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import sys

sys.path.insert(0, os.environ.get("REVIEWBENCH_ENGINE_SRC", str(Path(__file__).resolve().parents[1]/"src")))
from reviewbench_maintainability.agentic.cli import load_config
from reviewbench_maintainability.agentic.contracts import (
    ContractError, atomic_json, canonical_hash, component, file_hash, read_jsonl, selected_tasks, write_jsonl,
)
from reviewbench_maintainability.agentic.models import engine_identity
from reviewbench_maintainability.agentic.preparation import load_recipes, prepare
from reviewbench_maintainability.agentic.scheduling import output_lock


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--package", type=Path, action="append", required=True)
    p.add_argument("--out-dir", type=Path, required=True)
    a = p.parse_args()
    config = load_config(a.config)
    tasks = selected_tasks(Path(config["tasks"]), Path(config["scope_ids"]))
    oracles = read_jsonl(Path(config["oracles"]))
    output = a.out_dir.resolve()
    fingerprint = engine_identity()["fingerprint"]
    with output_lock(output):
        recipes, proofs, origins = {}, {}, {}
        for package in a.package:
            package = package.resolve()
            path = package/"runtime-recipes.jsonl"
            if not path.exists():
                continue
            for ident, recipe in load_recipes(path).items():
                if ident not in tasks:
                    raise ContractError("foreign task in runtime package")
                if ident in recipes and recipe.fingerprint != recipes[ident].fingerprint:
                    raise ContractError("different runtime recipes for the same task: "+ident)
                recipes[ident] = recipe
                proof_path = package/"execution/preparation/verified"/component(ident)/"verification.json"
                if not proof_path.is_file():
                    continue
                proof = json.loads(proof_path.read_text())
                if (proof.get("status") != "ready" or proof.get("runtime_fingerprint") != recipe.fingerprint
                        or proof.get("task_fingerprint") != canonical_hash(tasks[ident])
                        or proof.get("oracle_fingerprint") != canonical_hash(oracles[ident])
                        or proof.get("engine_fingerprint") != fingerprint):
                    continue
                proofs[ident] = proof
                origins[ident] = {"proof": str(proof_path), "sha256": file_hash(proof_path)}
        recipe_path = output/"runtime-recipes.jsonl"
        write_jsonl(recipe_path, [asdict(recipes[i]) for i in tasks if i in recipes])
        config.update(asset_root=str(output), out_dir=str(output/"execution"), runtime_recipes=str(recipe_path))
        prep = output/"execution/preparation"
        report = prepare(tasks, oracles, recipes, Path(config["repo_cache"]), output, prep)
        for ident, proof in proofs.items():
            destination = prep/"verified"/component(ident)
            atomic_json(destination/"verification.json", proof)
            atomic_json(destination/"evidence-source.json", origins[ident])
        report.update(execution_verified=len(proofs), complete=len(proofs)==len(tasks) and report["blocked"]==0)
        report["unverified_configured_tasks"] = [i for i in recipes if i not in proofs]
        atomic_json(prep/"readiness.json", report)
        atomic_json(output/"merge-evidence.json", {"engine_fingerprint": fingerprint,
                    "expected_tasks": len(tasks), "verified_origins": origins})
        import yaml
        (output/"experiment.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
