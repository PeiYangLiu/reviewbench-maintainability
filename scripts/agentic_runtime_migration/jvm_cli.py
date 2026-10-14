"""Whole-project Maven CLI Oracles whose original scripts remain unchanged."""
from __future__ import annotations

import hashlib
from pathlib import Path
import shlex

from reviewbench_maintainability.agentic.contracts import ContractError, atomic_json, canonical_hash

TASK = "github__cefriel__mapping-template__pull_27__comment_1693146095__modularity"
REPO = "cefriel/mapping-template"
START = "e8ae166d7dd2b58995e69b5f954349656cdfb73a"
SCRIPT = "session-tests/mapping-template-cli.sh"
SCRIPT_SHA256 = "aa432cba0255f23e7fea7f8127ffa45e7d0b24de94d6355c1d4cb94c163061c8"
REFERENCE_SHA256 = "52d46640f21d56e1c280c3973613384839f9c48a5a8b30c6f31d349de99e667b"
TESTS = ["MappingTemplatePublicCli#rmlCsvExampleProducesExpectedRdf"]
BUILD_ARGUMENTS = [
    "-B", "-ntp", "-Dstyle.color=never", "-Djacoco.skip=true",
    "-Dmaven.test.skip=true", "package",
]


def plan(task, oracle, directory):
    from .jvm_lane import TOOLS, PRIVATE, MAVEN_CACHE
    if task.get("repo") != REPO:
        return None
    environment = oracle.get("environment", {})
    reference = oracle.get("source_only_patch", {})
    if not isinstance(environment, dict) or not isinstance(reference, dict):
        raise ContractError("original Maven CLI environment/reference binding is absent")
    if (task.get("id") != TASK or task.get("start_sha") != START
            or oracle.get("id") != TASK or oracle.get("start_sha") != START
            or oracle.get("command") != "bash /task/" + SCRIPT
            or oracle.get("pass_to_pass_tests") != TESTS
            or reference.get("sha256") != REFERENCE_SHA256
            or environment.get("build_system") != "maven"
            or environment.get("container") != "oracle-java-tools:17"
            or environment.get("jdk") != 17):
        raise ContractError("original whole-project Maven CLI binding differs")
    path = Path(directory) / SCRIPT
    if (not path.is_file() or path.is_symlink()
            or hashlib.sha256(path.read_bytes()).hexdigest() != SCRIPT_SHA256):
        raise ContractError("original Maven CLI test script differs")
    public = shlex.join([TOOLS + "/maven/bin/mvn", "-o",
                        "-Dmaven.repo.local=" + MAVEN_CACHE, *BUILD_ARGUMENTS])
    return {
        "public": public,
        "command": "python3 " + PRIVATE + "/cli_entry.py --spec " + PRIVATE + "/cli-spec.json",
        "spec": {
            "original_script": PRIVATE + "/" + SCRIPT, "original_sha256": SCRIPT_SHA256,
            "maven": TOOLS + "/maven/bin/mvn", "repository": MAVEN_CACHE,
        },
        "original_script": str(path), "original_script_sha256": SCRIPT_SHA256,
        "original_test_ids": TESTS.copy(), "memory_mb": 4096, "build_scratch_mb": 4096,
    }


def runtime(ctx, task, oracle, directory):
    from .jvm_lane import (
        TOOLS, MAVEN_CACHE, dependency_repository, maven_dependency_assets,
        maven_tools, project_coordinates, remote_maven_files,
    )
    profile = plan(task, oracle, directory)
    if profile is None:
        raise ContractError("original Maven CLI profile is not implemented")
    repository = dependency_repository(ctx, task)
    names = remote_maven_files(repository, project_coordinates(ctx.repo(task), task["start_sha"]))
    assets, bootstrap = maven_dependency_assets(ctx, task, repository, names)
    assets.append({**maven_tools(ctx), "kind": "archive", "visibility": "developer", "target": TOOLS})
    atomic_json(ctx.output / "original-cli-binding.json", {
        "schema_version": "reviewbench-original-maven-cli/v1",
        "id": task["id"], "task_fingerprint": canonical_hash(task),
        "oracle_fingerprint": canonical_hash(oracle), **profile,
        "original_script_bytes_unchanged": True,
        "transport_only": "one original Maven executable path plus offline external repository location",
        "full_candidate_package_and_original_cli_assertions_required": True,
    })
    return {**profile, "assets": assets, "bootstrap": bootstrap, "cache_mounts": {"maven": MAVEN_CACHE}}
