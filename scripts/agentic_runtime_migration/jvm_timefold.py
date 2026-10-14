"""Original Timefold Wrapper transport; no change to its source, reactor or tests."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shlex

from reviewbench_maintainability.agentic.assets import asset_descriptor
from reviewbench_maintainability.agentic.contracts import (
    ContractError, atomic_json, canonical_hash, component, file_hash, repo_path,
)
from reviewbench_maintainability.agentic.snapshots import git

TASK_ID = "github__TimefoldAI__timefold-solver__pull_1778__comment_2324162662__readability"
START = "a10f680686442cfd519b35f724213b2ad51a783d"
COMMAND_SHA256 = "b85f0465a3db15ea4cdb62705297a115fb9da0f76ea97d797c0901e92acad63e"
WRAPPER_HASHES = {
    "mvnw": "a0408899a3aea3318d0d3d7849dd17d15eb7f679d33a4702de52414cf5923005",
    ".mvn/wrapper/maven-wrapper.properties": "fd1a968765daf133c38836d6ad45b41732e1dd96a207665f271d3e848104b2bf",
}
URL = "https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/3.9.1/apache-maven-3.9.1-bin.zip"
ARCHIVE_SHA512 = (
    "4ae5a0d17f9e6cbe57640c481f426a9184dfb451c2bb7cc7db324da095f616a14"
    "e7c482a79240e5286e241d8cd2805ea1cd9c95e38954101c2fa4088baad9a1a"
)
ASSET = "/opt/reviewbench-assets/timefold-maven391"
ENTRY = "/opt/reviewbench-assets/timefold-wrapper-entry.py"
SPEC = "/opt/reviewbench-assets/timefold-wrapper.json"


def original_commands(task, command):
    """Keep the original private prelude; derive a compile-only public command."""
    if (task.get("id") != TASK_ID or task.get("start_sha") != START
            or task.get("repo") != "TimefoldAI/timefold-solver"
            or hashlib.sha256(command.encode()).hexdigest() != COMMAND_SHA256):
        raise ContractError("Timefold wrapper requires the exact original task and command")
    prelude, separator, suffix = command.partition(" && bash ./mvnw ")
    if not separator:
        raise ContractError("Timefold original wrapper boundary changed")
    from .jvm_lane import MAVEN_CACHE
    args = shlex.split(suffix)
    if args[-1] != "test":
        raise ContractError("Timefold original Maven test goal changed")
    offline = ["-o", "-Dmaven.repo.local=" + MAVEN_CACHE]
    public_args = ["-Dmaven.test.skip=true",
                   *(arg for arg in args[:-1] if not arg.startswith("-Dtest=")), "compile"]
    public = "bash ./mvnw " + shlex.join([*offline, *public_args])
    # In particular, the original rm of one incompatible internal test remains
    # byte-for-byte in the private prelude, never added to the public build.
    private = prelude + separator + shlex.join([*offline, *args])
    return public, private


def distribution(ctx):
    binding = ctx.config.get("migration", {}).get("timefold_maven_distribution")
    if not isinstance(binding, dict) or set(binding) != {"path", "sha256"}:
        raise ContractError("Timefold requires its original Maven391 distribution receipt")
    path = Path(binding["path"])
    if not path.is_absolute() or path.is_symlink() or not path.is_file() or file_hash(path) != binding["sha256"]:
        raise ContractError("Timefold Maven391 receipt changed")
    receipt = json.loads(path.read_text())
    if (receipt.get("status") != "original_Maven391_archive_and_tree_verified"
            or receipt.get("task_id") != TASK_ID or receipt.get("original_distribution_url") != URL
            or receipt.get("official_checksum_url") != URL + ".sha512"):
        raise ContractError("Timefold distribution is not the original Maven391 input")
    checksum = receipt.get("archive_sha512", "")
    if checksum != ARCHIVE_SHA512:
        raise ContractError("Timefold original distribution checksum differs")
    asset = receipt["asset"]
    descriptor = asset_descriptor(asset, ctx.output)
    if (descriptor.get("source_kind") != "toolchain" or not 1 <= descriptor.get("files", 0) <= 256
            or descriptor.get("archive", {}).get("input_bytes", 0) > 32 * 1024**2):
        raise ContractError("Timefold Maven391 asset is not a bounded toolchain")
    records = sorted((repo_path(row["path"]), row["sha256"], row["executable"])
                     for row in receipt.get("entries", []))
    if (len(records) != descriptor["files"] or len({r[0] for r in records}) != len(records)
            or any(type(r[2]) is not bool for r in records)
            or canonical_hash(records) != descriptor["tree_sha256"]):
        raise ContractError("Timefold Maven391 tree differs from the original archive file index")
    return asset, binding


def runtime(ctx, task, command):
    from .jvm_lane import (
        HOME, MAVEN_CACHE, dependency_repository, maven_dependency_assets,
        project_coordinates, remote_maven_files,
    )
    public, private = original_commands(task, command)
    for name, digest in WRAPPER_HASHES.items():
        data = git(ctx.repo(task), "show", task["start_sha"] + ":" + name)
        if hashlib.sha256(data).hexdigest() != digest:
            raise ContractError("Timefold original wrapper bytes changed: " + name)
    toolchain, receipt = distribution(ctx)
    repository = dependency_repository(ctx, task)
    names = remote_maven_files(repository, project_coordinates(ctx.repo(task), task["start_sha"]))
    assets, bootstrap = maven_dependency_assets(ctx, task, repository, names)
    directory = ctx.output / "timefold-wrapper" / component(task["id"])
    directory.mkdir(parents=True, exist_ok=True)
    spec = {"schema_version": "reviewbench-timefold-wrapper/v1", "source": ASSET, "home": HOME,
            "original_distribution_url": URL, "wrapper_files": WRAPPER_HASHES}
    atomic_json(directory / "public.json", spec)
    assets += [
        {**toolchain, "kind": "archive", "visibility": "developer", "target": ASSET},
        {**ctx.copy_asset(Path(__file__).with_name("jvm_timefold_entry.py"), "timefold-tools"),
         "kind": "file", "visibility": "developer", "target": ENTRY},
        {**ctx.copy_asset(directory / "public.json", "timefold-tools"),
         "kind": "file", "visibility": "developer", "target": SPEC},
    ]
    bootstrap.append(shlex.join(["python3", ENTRY, "--spec", SPEC]))
    atomic_json(directory / "admission.json", {
        "id": task["id"], "original_command": command, "command_sha256": COMMAND_SHA256,
        "private_command": private, "public_command": public, "receipt": receipt,
        "wrapper_files": WRAPPER_HASHES, "source_changes": [], "test_changes": [], "golden_changes": [],
        "method": "original shell wrapper and full reactor; only offline cache transport added",
    })
    return {"public": public, "command": private, "assets": assets, "bootstrap": bootstrap,
            "cache_mounts": {"maven": MAVEN_CACHE}, "memory_mb": 8192, "build_scratch_mb": 12288}
