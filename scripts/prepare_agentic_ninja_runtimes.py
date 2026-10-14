#!/usr/bin/env python3
"""Migrate three frozen Ninja tasks using complete, offline CMake builds.

The developer image contains only OS packages/toolchains, not a repository or
an Oracle. The unchanged private depfile harness is mounted only when grading.
Developer feedback uses the full production build and a different public test.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, atomic_json, canonical_hash, component, file_hash, read_jsonl, write_jsonl,
)
from reviewbench_maintainability.agentic.environment import docker
from reviewbench_maintainability.agentic.preparation import repository_path, verify_recipe
from reviewbench_maintainability.agentic.scheduling import completed_jobs, output_lock

TASK_IDS = (
    "github__ninja-build__ninja__pull_2680__comment_2599809943__naming",
    "github__ninja-build__ninja__pull_2680__comment_3033139230__readability",
    "github__ninja-build__ninja__pull_2680__comment_3087575043__readability",
)
IMAGE = "sha256:8268d755008211c1e35be7b2387dca7498d460008febae21e6af1ce0394094b5"
BUILD = (
    "cmake -S /workspace -B /tmp/reviewbench-build -G Ninja "
    "-DBUILD_TESTING=OFF -DCMAKE_BUILD_TYPE=Debug "
    "-DCMAKE_CXX_COMPILER=g++-11 "
    "\"-DCMAKE_CXX_FLAGS=-O0 -g --coverage\" -DCMAKE_EXE_LINKER_FLAGS=--coverage "
    "&& cmake --build /tmp/reviewbench-build --target ninja -j4"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--legacy-root", type=Path, required=True)
    parser.add_argument("--repo-cache", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    if args.workers <= 0:
        parser.error("--workers must be positive")
    tasks = read_jsonl(args.release / "tasks.jsonl")
    oracles = read_jsonl(args.release / "oracles.jsonl")
    output, legacy = args.out_dir.resolve(), args.legacy_root / "native"
    with output_lock(output):
        assets = output / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        # Pin the exact previously inspected toolchain image, never its mutable
        # historical tag. Its history is Ubuntu + apt packages + CC/CXX env.
        inspection = json.loads(docker("image", "inspect", IMAGE))[0]
        history = docker("history", "--no-trunc", "--format", "{{.CreatedBy}}", IMAGE)
        if any("COPY " in line or line.startswith("COPY") for line in history.splitlines()):
            raise ContractError("unexpected COPY layer in the fixed compiler-only image")
        scan_command = (
            "find / -xdev \\( -name .git -o -name '*source-only-golden.patch' "
            "-o -name generation_results.jsonl -o -name oracles.jsonl "
            "-o -name ninja_depfile.sh \\) -print 2>/dev/null"
        )
        scan = docker("run", "--rm", "--read-only", "--network", "none",
                      "--entrypoint", "/bin/bash", IMAGE, "-c", scan_command)
        if scan:
            raise ContractError("reference/history artifacts in the developer image: " + scan)
        shutil.copyfile(legacy / "Dockerfile", assets / "Dockerfile.historical-toolchain")
        atomic_json(assets / "image-audit.json", {
            "schema_version": "reviewbench-developer-image-audit/v1", "image": IMAGE,
            "expected_image_id": inspection["Id"], "no_reference_assets": True,
            "no_repository_history": True,
            "basis": "fixed compiler-only Ubuntu/apt image; image history and rootfs inspected",
            "image_history": history, "rootfs_scan_command": scan_command, "rootfs_scan_output": scan,
            "historical_dockerfile_sha256": file_hash(assets / "Dockerfile.historical-toolchain"),
        })
        shutil.copyfile(legacy / "harnesses/ninja_depfile.sh", assets / "ninja_depfile.sh")
        recipes = {}
        for ident in TASK_IDS:
            task, oracle = tasks[ident], oracles[ident]
            if task["repo"] != "ninja-build/ninja":
                raise ContractError("unexpected repository in named migration")
            golden = assets / (component(ident) + ".patch")
            shutil.copyfile(legacy / oracle["artifacts"]["patch"], golden)
            if file_hash(golden) != oracle["golden_patch_sha256"]:
                raise ContractError("golden bytes differ from the frozen release")
            repository = repository_path(args.repo_cache, task["repo"])
            paths = subprocess.check_output(
                ["git", "-C", str(repository), "ls-tree", "-rz", "--name-only", task["start_sha"]],
            ).decode().split("\0")
            fixed_tests = [p for p in paths if p.endswith(("_test.cc", "_test.py", "_test.h"))]
            recipe = RuntimeRecipe(
                task_id=ident, repo=task["repo"], start_sha=task["start_sha"], lane="native", image=IMAGE,
                image_audit={"path": "assets/image-audit.json", "sha256": file_hash(assets / "image-audit.json")},
                bootstrap=[BUILD], dev_checks=[BUILD, "python3 misc/ninja_syntax_test.py"],
                cpus=4, memory_mb=2048, command_timeout_seconds=300,
                assets=[{"path": "assets/ninja_depfile.sh", "sha256": file_hash(assets / "ninja_depfile.sh"),
                         "target": "/opt/reviewbench-assets/ninja_depfile.sh", "visibility": "grader"}],
                grading={
                    "multifile_build": True, "immutable_paths": fixed_tests,
                    "golden_patch": {"path": "assets/" + golden.name, "sha256": file_hash(golden)},
                    "commands": [{
                        "command": "cd /tmp && bash /opt/reviewbench-assets/ninja_depfile.sh reviewbench-build/ninja",
                        "parser": "command", "expected_tests": oracle["pass_to_pass_tests"],
                        "success_marker": "PASS: Ninja public depfile rebuild behavior",
                    }],
                },
            )
            recipe.validate()
            recipes[ident] = recipe
        write_jsonl(output / "runtime-recipes.jsonl", [asdict(r) for r in recipes.values()])
        atomic_json(output / "asset-manifest.json", {
            "source_oracle_fingerprints": {ident: canonical_hash(oracles[ident]) for ident in TASK_IDS},
            "files": {p.name: file_hash(p) for p in sorted(assets.iterdir()) if p.is_file()},
        })
        proofs = {}
        if args.verify:
            def verify(ident):
                task = tasks[ident]
                return verify_recipe(
                    task, recipes[ident], oracles[ident], repository_path(args.repo_cache, task["repo"]),
                    output, output / "verification" / component(ident),
                )
            for ident, proof in completed_jobs(verify, TASK_IDS, args.workers):
                proofs[ident] = proof
        result = {
            "configured": len(recipes), "execution_verified": sum(p["status"] == "ready" for p in proofs.values()),
            "recipe": str(output / "runtime-recipes.jsonl"),
            "verification_failures": {ident: p.get("error") for ident, p in proofs.items() if p["status"] != "ready"},
        }
        result["complete"] = bool(args.verify and result["execution_verified"] == len(recipes))
        atomic_json(output / "migration-result.json", result)
        print(json.dumps(result, indent=2))
        return 0 if not args.verify or result["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
