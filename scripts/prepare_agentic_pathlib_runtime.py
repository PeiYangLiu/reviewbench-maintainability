#!/usr/bin/env python3
"""Migrate one real frozen task as a worked, executable runtime recipe.

This is deliberately a named production example, not a claim that all 402
historical Oracle environments have been migrated. The original assertions,
trace helper and source-only golden patch are copied and hash-bound unchanged.
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
    RuntimeRecipe, atomic_json, canonical_hash, file_hash, read_jsonl, write_jsonl,
)
from reviewbench_maintainability.agentic.environment import docker
from reviewbench_maintainability.agentic.preparation import repository_path, verify_recipe

TASK_ID = "github__python__cpython__pull_31691__comment_862144439__readability"
IMAGE = "python@sha256:c7220863385ee39fb6d822da81f4469d0cd33ff893d92ce94105e5c3f4b95fe2"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--legacy-root", type=Path, required=True, help="hard_oracle_20260804 root")
    parser.add_argument("--repo-cache", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    task = read_jsonl(args.release / "tasks.jsonl")[TASK_ID]
    oracle = read_jsonl(args.release / "oracles.jsonl")[TASK_ID]
    output = args.out_dir.resolve()
    assets = output / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    recovery = args.legacy_root / "gopy/recovery/123-cpython"
    for source, name in [
        (recovery / "oracle.py", "pathlib_oracle.py"),
        (args.legacy_root / "gopy/oracle_support.py", "oracle_support.py"),
        (recovery / "dependency-closed-source-only.patch", "golden.patch"),
    ]:
        shutil.copyfile(source, assets / name)
    if file_hash(assets / "golden.patch") != oracle["source_only_golden"]["patch_sha256"]:
        raise ValueError("historical golden patch differs from the frozen Oracle")
    # Load the immutable tracing helper before adding candidate imports. Source
    # helper modules from Lib can then be imported by the historical pathlib.
    (assets / "run_oracle.py").write_text(
        "import os, runpy, sys\nimport oracle_support\n"
        "sys.path.insert(0, os.environ['SOURCE_ROOT'] + '/Lib')\n"
        "runpy.run_path('/opt/reviewbench-assets/pathlib_oracle.py', run_name='__main__')\n"
    )
    inspection = json.loads(docker("image", "inspect", IMAGE))[0]
    scan_command = (
        "find / -xdev \\( -name .git -o -name '*source-only-golden.patch' "
        "-o -name generation_results.jsonl -o -name oracles.jsonl \\) -print 2>/dev/null"
    )
    scanned = docker("run", "--rm", "--read-only", "--network", "none", "--entrypoint",
                     "/bin/bash", IMAGE, "-c", scan_command, timeout=120)
    if scanned.strip():
        raise ValueError("developer base image contains repository/reference artifacts: " + scanned)
    atomic_json(assets / "image-audit.json", {
        "schema_version": "reviewbench-developer-image-audit/v1", "image": IMAGE,
        "expected_image_id": inspection["Id"],
        "no_reference_assets": True, "no_repository_history": True,
        "basis": "public Python image digest; no task source/assets copied into image",
        "rootfs_scan_command": scan_command, "rootfs_scan_output": scanned,
    })
    dev_command = (
        "python3 -c \"import importlib.util; "
        "s=importlib.util.spec_from_file_location('checked_pathlib','Lib/pathlib.py'); "
        "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
        "assert str(m.PurePosixPath('/a') / 'b') == '/a/b'; "
        "assert m.PurePosixPath('/a/b').name == 'b'; "
        "print('public pathlib construction: PASS')\""
    )
    recipe = RuntimeRecipe(
        task_id=TASK_ID, repo=task["repo"], start_sha=task["start_sha"], lane="gopy",
        image=IMAGE, image_audit={"path": "assets/image-audit.json", "sha256": file_hash(assets / "image-audit.json")},
        dev_checks=[dev_command], env={"SOURCE_ROOT": "/workspace"},
        command_timeout_seconds=120, memory_mb=1024,
        assets=[{"path": "assets/" + name, "sha256": file_hash(assets / name),
                 "target": "/opt/reviewbench-assets/" + name, "visibility": "grader"}
                for name in ("pathlib_oracle.py", "oracle_support.py", "run_oracle.py")],
        grading={
            "multifile_build": True,
            "immutable_paths": ["Lib/test/test_pathlib.py"],
            "golden_patch": {"path": "assets/golden.patch", "sha256": file_hash(assets / "golden.patch")},
            "commands": [{"command": "python3 /opt/reviewbench-assets/run_oracle.py", "parser": "command",
                          "expected_tests": oracle["pass_to_pass_tests"],
                          "success_marker": "P2P_SIGNATURE=" + oracle["runs"]["start"][0]["signature"]}],
        },
    )
    recipe.validate()
    write_jsonl(output / "runtime-recipes.jsonl", [asdict(recipe)])
    atomic_json(output / "asset-manifest.json", {
        "task_id": TASK_ID, "source_oracle_fingerprint": canonical_hash(oracle),
        "files": {p.name: file_hash(p) for p in sorted(assets.iterdir()) if p.is_file()},
    })
    result = {"task_id": TASK_ID, "recipe": str(output / "runtime-recipes.jsonl"), "verified": False}
    if args.verify:
        proof = verify_recipe(task, recipe, oracle, repository_path(args.repo_cache, task["repo"]),
                              output, output / "verification")
        result["verified"] = proof["status"] == "ready"
        result["verification"] = proof
    print(json.dumps(result, indent=2))
    return 0 if not args.verify or result["verified"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
