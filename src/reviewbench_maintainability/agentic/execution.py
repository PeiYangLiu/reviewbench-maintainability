"""Shared admission and local scratch services for all CLI execution phases."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import threading

from .contracts import ContractError, canonical_hash
from .resources import CURRENT_JOB, GiB, ResourceGate, ResourceLimits, ResourceRequest
from .storage import open_cache, require_local_filesystem


class ExecutionServices:
    def __init__(self, config: dict):
        self.config = config
        options, storage = config.get("execution", {}), config.get("storage")
        default_root = Path(tempfile.gettempdir()) / f"reviewbench-agentic-{os.getuid()}"
        self.scratch = Path(storage["scratch_dir"]) if storage else default_root / "scratch"
        require_local_filesystem(self.scratch)
        require_local_filesystem(Path(config["out_dir"]))
        require_local_filesystem(Path(config["repo_cache"]))
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.gate = ResourceGate(Path(options.get("gate_dir", default_root / "gate")), self.scratch,
                                 ResourceLimits(**options.get("resource_limits", {})))
        self.cache = open_cache(storage) if storage else None
        self.sources = None
        if storage and storage.get("source_catalog"):
            from .sources import SourceCatalog
            self.sources = SourceCatalog(Path(storage["source_catalog"]), self.cache)
        self._sizes = {}
        self._size_lock = threading.Lock()

    def source_bytes(self, task: dict, repository: Path) -> int:
        key = (task["repo"], task["start_sha"])
        with self._size_lock:
            if key in self._sizes:
                return self._sizes[key]
        if self.sources:
            size = self.sources.source(*key)["archive"]["input_bytes"]
        else:
            from .snapshots import git
            rows = git(repository, "ls-tree", "-r", "-z", "-l", task["start_sha"]).split(b"\0")
            size = 0
            for row in rows:
                if not row:
                    continue
                value = row.split(b"\t", 1)[0].split()[3]
                if value != b"-":
                    size += int(value) + 1024  # tar/filesystem overhead
        with self._size_lock:
            self._sizes[key] = size
        return size

    @contextmanager
    def job(self, task: dict, recipe, repository: Path, phase: str, *, episode: Path | None = None,
            resume: bool = False):
        options = self.config.get("execution", {})
        size = self.source_bytes(task, repository)
        if recipe.scratch_paths:
            from .snapshots import git
            tracked = [p.decode() for p in git(repository, "ls-tree", "-r", "--name-only", "-z",
                                               task["start_sha"]).split(b"\0") if p]
            for path in recipe.scratch_paths:
                if any(name == path or name.startswith(path + "/") for name in tracked):
                    raise ContractError("declared build scratch overlaps original repository source: " + path)
        containers = 1 + len(recipe.sidecars)
        heavy = recipe.lane in {"rust", "native", "jvm", "csharp"} or size > GiB or recipe.memory_mb > 4096
        # Includes the trusted patch collector's extra copies and private .git.
        # Verification deletes each replay before starting the next one.
        from .assets import AssetPool, asset_descriptor
        asset_bytes = 0
        seen_assets = set()
        for asset in [*(a for a in recipe.assets if a.get("kind") == "archive"),
                      *(m["asset"] for m in recipe.submodules if not m.get("omit"))]:
            if asset["sha256"] not in seen_assets:
                asset_bytes += asset_descriptor(asset, Path(self.config["asset_root"]))["archive"]["input_bytes"]
                seen_assets.add(asset["sha256"])
        scratch = size * 6 + asset_bytes + options.get("build_scratch_bytes", recipe.build_scratch_mb * 1024**2)
        request = ResourceRequest(
            name=phase + ":" + task["id"],
            cpus=recipe.cpus * containers, memory_bytes=recipe.memory_mb * 1024**2 * containers,
            scratch_bytes=scratch, heavy=heavy,
        )
        adopt = None
        if resume and episode and (episode / "trajectory.json").is_file() and not (episode / "result.json").exists():
            trace = json.loads((episode / "trajectory.json").read_text())
            adopt = trace.get("info", {}).get("environment", {}).get("resource_lease")
        with self.gate.acquire(
            request, uses_containers=True, source_catalog=self.sources, task=task, adopt_lease=adopt,
            timeout=options.get("admission_timeout_seconds", 3600),
        ) as lease:
            pool = AssetPool(self.cache, self.scratch, lease["lease"], Path(self.config["asset_root"]))
            job = CURRENT_JOB.get()
            job.update(asset_pool=pool, submodules=recipe.submodules, scratch_paths=recipe.scratch_paths,
                       workspace_exclusions={})
            try:
                yield lease
            finally:
                pool.close()


def source_identity(config: dict) -> dict | None:
    """Storage locations/concurrency do not change behavior; source bytes do."""
    storage = config.get("storage", {})
    if not storage.get("source_catalog"):
        return None
    from .sources import SOURCE_POLICY, SOURCE_SCHEMA
    catalog = json.loads(Path(storage["source_catalog"]).read_text())
    if catalog.get("schema_version") != SOURCE_SCHEMA or catalog.get("policy") != SOURCE_POLICY:
        raise ContractError("invalid source catalog")
    # Timing, receipt ETags and physical object URLs are not source identity.
    identities = {
        key: {"repo": row.get("repo"), "start_sha": row.get("start_sha"),
              "tree_id": row.get("tree_id"), "archive": row.get("archive"),
              "gitlinks": row.get("gitlinks"), "submodules": row.get("submodules")}
        for key, row in catalog["sources"].items()
    }
    return {"policy": SOURCE_POLICY, "sources_sha256": canonical_hash(identities)}
