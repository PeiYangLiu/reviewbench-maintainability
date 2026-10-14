"""Exhaustive release inventory; readiness requires a real developer contract."""
from __future__ import annotations

from collections import Counter
from contextlib import ExitStack
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import shutil
import tempfile

from . import PROTOCOL
from .contracts import (
    ContractError, InfrastructureError, PublicTaskSpec, RuntimeRecipe, atomic_json,
    canonical_hash, lane_for, read_jsonl, write_jsonl,
)
from .environment import ReviewBenchDockerEnvironment, validate_assets
from .evaluation import parse_tests, validate_grading
from .snapshots import apply_patch, copy_source, extract_start, initialize_git
from .resources import job_directory


def repository_path(cache: Path, repo: str) -> Path:
    if not isinstance(repo, str) or len(repo.split("/")) != 2 or any(x in {"", ".", ".."} for x in repo.split("/")):
        raise ContractError("invalid repository identifier")
    return cache / repo.replace("/", "__")


def load_recipes(path: Path | None) -> dict[str, RuntimeRecipe]:
    if path is None:
        return {}
    return {key: RuntimeRecipe.from_dict(value) for key, value in read_jsonl(path, "task_id").items()}


def _source_check(repository: Path, task: PublicTaskSpec) -> str | None:
    if not repository.is_dir():
        return "source_repository_missing"
    result = subprocess.run(
        ["git", "-C", str(repository), "cat-file", "-e", f"{task.start_sha}:{task.locality['path']}"],
        capture_output=True, timeout=30,
    )
    return "start_source_missing" if result.returncode else None


def prepare(tasks: dict[str, dict], oracles: dict[str, dict], recipes: dict[str, RuntimeRecipe],
            repo_cache: Path, asset_root: Path, output: Path, *, inspect_images: bool = True) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    if set(tasks) - set(oracles) or set(recipes) - set(tasks):
        raise ContractError("missing task Oracle or foreign runtime recipe")
    records = []
    for ident, task in tasks.items():
        public = PublicTaskSpec.from_task(task)
        oracle = oracles[ident]
        lane = lane_for(task, oracle)
        issues = []
        issue = _source_check(repository_path(repo_cache, public.repo), public)
        if issue:
            issues.append(issue)
        recipe = recipes.get(ident)
        assets = None
        if not recipe:
            issues.append("developer_runtime_recipe_missing")
        else:
            try:
                recipe.bind(public)
                if recipe.lane != lane:
                    raise ContractError("recipe lane differs from release lane")
                validate_grading(recipe)
                assets = validate_assets(recipe, asset_root, inspect_images=inspect_images)
            except (ContractError, InfrastructureError, OSError, ValueError) as exc:
                issues.append(f"runtime_contract: {exc}")
        # Historical Oracle metadata is evidence of a scoring environment, not
        # proof of an answer-free, interactive developer environment. Keep these
        # hints outside PublicTaskSpec and never execute them automatically.
        environment = oracle.get("environment")
        image_hint = (environment.get("docker_image") or environment.get("image")) if isinstance(environment, dict) else None
        row = {
            "id": ident, "repo": public.repo, "start_sha": public.start_sha, "lane": lane,
            "source_kind": "modern" if oracle.get("lane_results_source") else "published_legacy",
            "task_fingerprint": canonical_hash(task), "oracle_fingerprint": canonical_hash(oracle),
            "runtime_fingerprint": recipe.fingerprint if recipe else None,
            "status": "configured" if not issues else "blocked",
            "issues": issues, "historical_image_hint": image_hint, "verified_assets": assets,
        }
        records.append(row)
    report = {
        "protocol": PROTOCOL, "expected_tasks": len(tasks),
        "configured": sum(row["status"] == "configured" for row in records),
        "blocked": sum(row["status"] == "blocked" for row in records),
        "execution_verified": 0,
        "complete": False,  # Only execution verification can issue a ready marker.
        "lanes": dict(Counter(row["lane"] for row in records)),
        "source_kinds": dict(Counter(row["source_kind"] for row in records)),
        "issue_counts": dict(Counter(issue.split(":")[0] for row in records for issue in row["issues"])),
        "tasks_fingerprint": canonical_hash(tasks),
    }
    write_jsonl(output / "runtime-inventory.jsonl", records)
    write_jsonl(output / "configured-recipes.jsonl", [asdict(r) for r in recipes.values()])
    atomic_json(output / "readiness.json", report)
    atomic_json(output / "recipe-template.json", {
        "schema_version": "reviewbench-agentic-runtime/v1", "task_id": "TASK_ID",
        "repo": "OWNER/REPOSITORY", "start_sha": "40_HEX_CHARACTERS", "lane": "gopy",
        "image": "IMAGE@sha256:PINNED_DIGEST",
        "image_audit": {"path": "image-audit.json", "sha256": "PINNED_SHA256"},
        "dev_checks": ["PUBLIC_REPOSITORY_TEST_OR_BUILD_COMMAND"],
        "grading": {"multifile_build": True, "immutable_paths": [],
                    "commands": [{"command": "FIXED_ORACLE_COMMAND", "parser": "junit",
                                  "report_path": "results.xml", "expected_tests": ["classname::test"]}]},
    })
    return report


def verify_recipe(task: dict, recipe: RuntimeRecipe, oracle: dict, repository: Path,
                  asset_root: Path, output: Path, *, environment_factory=ReviewBenchDockerEnvironment) -> dict:
    """Public developer check + 3x start/golden fixed-Oracle replay.

    Golden input must be an explicit audited source-only patch asset supplied by
    the recipe, not task.target_sha (which may include unrelated PR changes).
    """
    from .environment import resolve_asset
    from .models import engine_identity
    validate_grading(recipe)
    proof = {"id": task["id"], "runtime_fingerprint": recipe.fingerprint,
             "task_fingerprint": canonical_hash(task), "oracle_fingerprint": canonical_hash(oracle),
             "engine_fingerprint": engine_identity()["fingerprint"], "status": "blocked", "runs": []}
    output.mkdir(parents=True, exist_ok=True)
    env = None
    try:
        golden_spec = recipe.grading.get("golden_patch")
        if not golden_spec:
            raise ContractError("runtime verification needs a pinned source-only golden_patch asset")
        golden = resolve_asset(golden_spec, asset_root)
        source_only = oracle.get("source_only_golden")
        declared = oracle.get("golden_patch_sha256")
        if declared is None and isinstance(source_only, dict):
            declared = source_only.get("patch_sha256")
        if declared and declared != golden_spec["sha256"]:
            raise ContractError("golden patch digest differs from frozen Oracle")
        expected = oracle.get("pass_to_pass_tests")
        actual = [test for command in recipe.grading["commands"] for test in command["expected_tests"]]
        if expected is not None and sorted(expected) != sorted(actual):
            raise ContractError("runtime PASS_TO_PASS identities differ from frozen Oracle")
        with job_directory(prefix="reviewbench-verify-runtime-") as directory, ExitStack() as cleanup:
            root = Path(directory)
            base = root / "start"
            extract_start(repository, task["start_sha"], base)
            developer = root / "developer"
            copy_source(base, developer)
            initialize_git(developer)
            env = environment_factory(recipe, developer, output / "developer", asset_root)
            cleanup.callback(env.close)
            for command in [*recipe.bootstrap, *recipe.dev_checks]:
                result = env.execute({"command": command})
                if result["returncode"]:
                    raise InfrastructureError(f"public development check failed: {command}")
            env.close()
            env = None
            shutil.rmtree(developer)
            for variant in ("start", "golden"):
                for repetition in range(3):
                    work = root / f"{variant}-{repetition}"
                    if variant == "golden":
                        apply_patch(base, golden, work, ignore_space_change=recipe.grading.get(
                            "golden_apply_ignore_space_change", False))
                    else:
                        copy_source(base, work)
                        initialize_git(work)
                    env = environment_factory(recipe, work, output / f"{variant}-{repetition}",
                                              asset_root, grader=True)
                    cleanup.callback(env.close)
                    for command in [*recipe.bootstrap, *recipe.grading.get("bootstrap", [])]:
                        if env.execute({"command": command})["returncode"]:
                            raise InfrastructureError(f"{variant} bootstrap failed")
                    run = {"variant": variant, "repetition": repetition, "tests": {}}
                    for index, spec in enumerate(recipe.grading["commands"]):
                        report = work / spec["report_path"] if spec.get("report_path") else None
                        if report:
                            if not report.resolve().is_relative_to(work.resolve()):
                                raise ContractError("verification report escapes workspace")
                            report.unlink(missing_ok=True)
                        observed = env.execute({"command": spec["command"]})
                        raw = output / f"{variant}-{repetition}" / observed["extra"]["stdout_file"]
                        text = report.read_text() if report and report.is_file() else raw.read_text()
                        if not report:
                            text += (output / f"{variant}-{repetition}" / observed["extra"]["stderr_file"]).read_text()
                        tests = parse_tests(spec["parser"], text, command_ok=observed["returncode"] == 0,
                                            expected=spec["expected_tests"], success_marker=spec.get("success_marker"),
                                            success_pattern=spec.get("success_pattern"))
                        run["tests"].update({f"{index}::{key}": val for key, val in tests.items()})
                    proof["runs"].append(run)
                    if not run["tests"] or not all(run["tests"].values()):
                        raise InfrastructureError(f"{variant} fixed PASS_TO_PASS replay failed")
                    env.close()
                    env = None
                    shutil.rmtree(work)
        proof["status"] = "ready"
    except Exception as exc:
        proof["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if env:
            env.close()
        atomic_json(output / "verification.json", proof)
    return proof
