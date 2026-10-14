"""Durable terminal evidence, then cleanup of this episode's disposable trees."""
from __future__ import annotations

import gzip
import json
from pathlib import Path
import shutil
import tarfile
import tempfile

from .archives import ARCHIVE_SCHEMA, PartWriter, extract_archive
from .contracts import ContractError, InfrastructureError, atomic_json, file_hash
from .resources import CURRENT_JOB, containers_alive
from .snapshots import tree_files
from .storage import MiB


class CountingWriter:
    def __init__(self, stream):
        self.stream, self.count = stream, 0

    def write(self, data):
        self.count += len(data)
        return self.stream.write(data)


def archive_files(root: Path, files: list[str], cache, scratch: Path, *,
                  part_bytes: int = 256 * MiB) -> tuple[dict, list[dict]]:
    writer = PartWriter(cache, scratch, part_bytes)
    try:
        with gzip.GzipFile(fileobj=writer, mode="wb", compresslevel=1, mtime=0, filename="") as compressed:
            counter = CountingWriter(compressed)
            with tarfile.open(fileobj=counter, mode="w|") as tar:
                for name in sorted(files):
                    from .contracts import repo_path
                    repo_path(name)
                    tar.add(root / name, arcname=name, recursive=False)
                    tar.members.clear()
        writer.commit_part()
    finally:
        writer.close()
    return {"schema_version": ARCHIVE_SCHEMA, "codec": "gzip", "parts": writer.parts,
            "compressed_bytes": writer.total, "input_bytes": counter.count}, writer.receipts


def archive_episode(episode: Path, result: dict, services) -> dict:
    """Keep trajectory/patch/result local; pack small command logs remotely.

    Successful final trees are reproducible from archived start + checked
    whole-candidate patch. Failed infrastructure attempts additionally preserve
    their working source/build files before disposal. No original repo/cache,
    completed user run, or unrelated Docker resources are cleanup targets.
    """
    episode = episode.resolve()
    actual = Path(result["trace_file"]).parent.resolve()
    if not actual.is_relative_to(episode):
        raise ContractError("terminal attempt is outside its episode")
    result_file = actual / "result.json"
    if json.loads(result_file.read_text()) != result:
        raise ContractError("terminal result differs from durable episode record")
    binding = {"result_sha256": file_hash(result_file), "trace_sha256": result.get("trace_sha256"),
               "patch_sha256": result.get("patch_sha256")}
    manifest_path = actual / "terminal-archive.json"
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text())
        if previous.get("binding") != binding:
            raise ContractError("terminal evidence differs from existing archive")
        if previous.get("local_disposal_complete"):
            return previous
        record = previous
    else:
        if services.sources is None:
            raise ContractError("terminal disposal requires a remote exact-start source catalog")
        job = CURRENT_JOB.get()
        if job and containers_alive(job["lease"]):
            raise InfrastructureError("terminal sandbox is still running; retaining its files")
        task = job["task"] if job else None
        if task is None:
            raise ContractError("terminal archival requires an admitted task")
        source = services.sources.source(task["repo"], task["start_sha"])
        # Archive all durable evidence and raw command files. The code/base
        # need not be uploaded N times when a validated patch reconstructs it.
        names = [p.name for p in actual.iterdir() if p.is_file() and not p.is_symlink()]
        for directory in ("commands", *(("workspace",) if result["status"] == "infra_error" else ())):
            root = actual / directory
            if root.is_symlink():
                raise ContractError("episode-managed directory became a symlink")
            if root.exists():
                names.extend(f"{directory}/{name}" for name in tree_files(root))
        with tempfile.TemporaryDirectory(prefix="terminal-", dir=services.scratch) as directory:
            archive, receipts = archive_files(actual, names, services.cache, Path(directory))
        record = {
            "schema_version": "reviewbench-terminal-archive/v1", "binding": binding,
            "archive": archive, "receipts": receipts,
            "source_catalog_sha256": services.sources.sha256,
            "source_start_sha": task["start_sha"], "source_tree_id": source["tree_id"],
            "workspace_reconstruction": "archived-working-files" if result["status"] == "infra_error"
                                        else "exact-start-plus-candidate-patch",
            "local_disposal_complete": False,
        }
        atomic_json(manifest_path, record)
    # Commit the remote archive descriptor too: losing local metadata must not
    # make the blob data unusable. Its immutable hash is recorded locally.
    from dataclasses import asdict
    if not record.get("manifest_object"):
        obj, receipt = services.cache.publish(manifest_path)
        record.update(manifest_object=asdict(obj), manifest_receipt=receipt)
        atomic_json(manifest_path, record)
    for name in ("start", "workspace", "commands"):
        path = actual / name
        if path.is_symlink():
            raise ContractError("episode-managed cleanup target became a symlink")
        if path.exists():
            shutil.rmtree(path)
    record["local_disposal_complete"] = True
    atomic_json(manifest_path, record)
    return record


def restore_terminal(manifest: Path, cache, destination: Path) -> None:
    """Explicit diagnostic recovery; never overwrite a live/completed episode."""
    record = json.loads(manifest.read_text())
    if record.get("schema_version") != "reviewbench-terminal-archive/v1":
        raise ContractError("invalid terminal archive")
    extract_archive(record["archive"], cache, destination)
    if file_hash(destination / "result.json") != record["binding"]["result_sha256"]:
        raise ContractError("restored terminal result differs from archive binding")
