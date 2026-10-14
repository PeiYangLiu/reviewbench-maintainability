"""Resumable exact-start source catalogs, separate from runtime readiness."""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import tempfile
import time

from .archives import archive_stream, extract_archive, validate_archive
from .contracts import ContractError, atomic_json, canonical_hash, file_hash, repo_path
from .preparation import repository_path
from .resources import ResourceGate, ResourceRequest
from .scheduling import completed_jobs
from .snapshots import git, gitlinks, start_archive, tree_hash
from .storage import LocalObjectCache, MiB, require_local_filesystem

SOURCE_SCHEMA = "reviewbench-source-catalog/v1"
SOURCE_POLICY = "exact-git-tree-no-export-attributes/v1"


def source_key(repo: str, sha: str) -> str:
    return canonical_hash({"repo": repo, "start_sha": sha})


def stage_sources(tasks: dict, repo_cache: Path, cache: LocalObjectCache, scratch: Path,
                  output: Path, gate: ResourceGate, *, workers: int = 8,
                  part_bytes: int = 256 * MiB) -> dict:
    """Bounded source streaming; no source working tree is duplicated here."""
    require_local_filesystem(output)
    require_local_filesystem(scratch)
    output.mkdir(parents=True, exist_ok=True)
    scratch.mkdir(parents=True, exist_ok=True)
    pairs = {source_key(t["repo"], t["start_sha"]): (t["repo"], t["start_sha"]) for t in tasks.values()}
    bindings = {"schema_version": SOURCE_SCHEMA, "policy": SOURCE_POLICY,
                "tasks_fingerprint": canonical_hash(tasks), "expected_tasks": len(tasks),
                "expected_snapshots": len(pairs),
                "task_sources": {ident: source_key(t["repo"], t["start_sha"]) for ident, t in tasks.items()}}
    catalog_path = output / "source-catalog.json"
    records = {}
    if catalog_path.exists():
        old = json.loads(catalog_path.read_text())
        if any(old.get(key) != value for key, value in bindings.items()):
            raise ContractError("source output belongs to a different task/start/policy catalog")
        records = old["sources"]
        if set(records) - set(pairs):
            raise ContractError("foreign snapshot in source catalog")
        for key, record in records.items():
            if record.get("status") == "archived":
                validate_source(record, *pairs[key])

    def save():
        catalog = {**bindings, "sources": {key: records[key] for key in pairs if key in records}}
        atomic_json(catalog_path, catalog)

    def stage(key):
        repo, sha = pairs[key]
        existing = records.get(key)
        if existing and existing.get("status") == "archived":
            return existing
        started = time.monotonic()
        row = {"repo": repo, "start_sha": sha, "policy": SOURCE_POLICY, "status": "error"}
        try:
            repository = repository_path(repo_cache, repo)
            sizes = [int(row.split(b"\t", 1)[0].split()[3])
                     for row in git(repository, "ls-tree", "-r", "-z", "-l", sha).split(b"\0")
                     if row and row.split(b"\t", 1)[0].split()[3] != b"-"]
            # git archive itself can materialize a large individual blob even
            # though the controller streams the tar. Reserve for that child,
            # not just the Python compression buffers.
            request = ResourceRequest("source:" + repo + "@" + sha[:12], cpus=1,
                                      memory_bytes=512 * MiB + 2 * max(sizes, default=0),
                                      scratch_bytes=2 * part_bytes)
            with gate.acquire(request) as admission:
                links = gitlinks(repository, sha)
                tree_id = git(repository, "rev-parse", sha + "^{tree}").decode().strip()
                with tempfile.TemporaryDirectory(prefix="source-", dir=scratch) as directory:
                    with start_archive(repository, sha) as stream:
                        archive, receipts = archive_stream(stream, cache, Path(directory), part_bytes=part_bytes)
                row.update(status="archived", tree_id=tree_id, gitlinks=links, archive=archive,
                           receipts=receipts, source_closure="root-only" if links else "complete",
                           seconds=round(time.monotonic() - started, 3), admission=admission)
        except (ContractError, OSError, RuntimeError, ValueError) as exc:
            row.update(error=f"{type(exc).__name__}: {exc}", seconds=round(time.monotonic() - started, 3))
        return row

    save()
    for key, row in completed_jobs(stage, pairs, workers):
        records[key] = row
        save()
        print(json.dumps({"source": row["repo"], "status": row["status"], "completed": len(records),
                          "expected": len(pairs), "seconds": row.get("seconds")}), flush=True)
    archived = [r for r in records.values() if r["status"] == "archived"]
    # One aggregate metadata object, not a remote small-file tree.
    descriptor, proof = cache.publish(catalog_path)
    from dataclasses import asdict
    summary = {
        "stage": "source-archival", "expected_tasks": len(tasks), "expected_snapshots": len(pairs),
        "archived_snapshots": len(archived),
        "closed_snapshots": sum(r["source_closure"] == "complete" for r in archived),
        "root_only_snapshots": sum(r["source_closure"] == "root-only" for r in archived),
        "errors": {key: r.get("error") for key, r in records.items() if r["status"] != "archived"},
        "tar_bytes": sum(r["archive"]["input_bytes"] for r in archived),
        "compressed_bytes": sum(r["archive"]["compressed_bytes"] for r in archived),
        "parts": sum(len(r["archive"]["parts"]) for r in archived),
        "cache": cache.status(), "resources": gate.status(),
        "catalog_sha256": file_hash(catalog_path), "catalog_object": asdict(descriptor), "catalog_receipt": proof,
        "complete": len(archived) == len(pairs),
        "runtime_ready": False,  # data archival is never execution verification
    }
    atomic_json(output / "source-summary.json", summary)
    return summary


def validate_source(record: dict, repo: str, sha: str) -> None:
    if (record.get("status") != "archived" or record.get("repo") != repo or record.get("start_sha") != sha
            or record.get("policy") != SOURCE_POLICY):
        raise ContractError("source archive is missing or bound to a different exact start")
    validate_archive(record["archive"])
    if not isinstance(record.get("gitlinks"), list):
        raise ContractError("source archive lacks a submodule closure inventory")
    for link in record["gitlinks"]:
        repo_path(link["path"])
        if not isinstance(link.get("commit"), str) or len(link["commit"]) != 40:
            raise ContractError("invalid pinned submodule commit")


class SourceCatalog:
    def __init__(self, path: Path, cache: LocalObjectCache):
        self.data = json.loads(path.read_text())
        if self.data.get("schema_version") != SOURCE_SCHEMA or self.data.get("policy") != SOURCE_POLICY:
            raise ContractError("unsupported source catalog")
        self.cache = cache
        self.sha256 = file_hash(path)

    def source(self, repo: str, sha: str) -> dict:
        record = self.data["sources"].get(source_key(repo, sha), {})
        validate_source(record, repo, sha)
        return record

    def extract(self, repo: str, sha: str, destination: Path, *,
                submodules: list[dict] | None = None, asset_pool=None) -> str:
        record = self.source(repo, sha)
        declared = {item["path"]: item for item in submodules or []}
        if len(declared) != len(submodules or []):
            raise ContractError("duplicate submodule source policy")
        if record["gitlinks"]:
            if set(declared) != {item["path"] for item in record["gitlinks"]}:
                raise ContractError("source archive is root-only; explicit pinned submodule policy is required")
            for link in record["gitlinks"]:
                entry = declared[link["path"]]
                if entry["commit"] != link["commit"]:
                    raise ContractError("submodule source policy differs from the Git pin")
                if entry.get("omit"):
                    if entry.get("omit") is not True or not entry.get("reason"):
                        raise ContractError("optional unmaterialized submodule needs a reason")
                elif not asset_pool or not entry.get("asset"):
                    raise ContractError("required submodule source asset is missing")
        elif declared:
            raise ContractError("submodule source policy contains foreign paths")
        extract_archive(record["archive"], self.cache, destination)
        try:
            for name, entry in declared.items():
                if entry.get("omit"):
                    continue
                from .assets import asset_descriptor
                from .snapshots import copy_source
                descriptor = asset_descriptor(entry["asset"], asset_pool.asset_root)
                if (descriptor["source_kind"] != "submodule"
                        or descriptor.get("git_commit") != entry["commit"]):
                    raise ContractError("submodule asset is not bound to its exact Git commit")
                root = destination / repo_path(name)
                if root.is_symlink() or (root.exists() and (not root.is_dir() or any(root.iterdir()))):
                    raise ContractError("submodule extraction target is not an empty gitlink")
                copy_source(asset_pool.materialize(entry["asset"]), root)
        except BaseException:
            import shutil
            shutil.rmtree(destination)
            raise
        return tree_hash(destination)
