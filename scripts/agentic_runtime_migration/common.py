from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import tempfile
import threading

from reviewbench_maintainability.agentic.assets import ASSET_SCHEMA
from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, atomic_json, canonical_hash, component, file_hash, lane_for, read_jsonl,
)
from reviewbench_maintainability.agentic.environment import docker
from reviewbench_maintainability.agentic.retention import archive_files
from reviewbench_maintainability.agentic.snapshots import git, tree_files
from reviewbench_maintainability.agentic.storage import open_cache, require_local_filesystem


def bounded_cpu_set(cpus, key, *, available=None):
    """Limit visible CPUs as well as CPU time for nested dependency workers."""
    if type(cpus) is not int or cpus < 1:
        raise ContractError("dependency CPU count must be a positive integer")
    available = sorted(os.sched_getaffinity(0) if available is None else set(available))
    if not available or any(type(cpu) is not int or cpu < 0 for cpu in available):
        raise ContractError("dependency preparation needs a nonempty local CPU affinity")
    offset = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big") % len(available)
    selected = [available[(offset + index) % len(available)] for index in range(min(cpus, len(available)))]
    return ",".join(map(str, sorted(selected)))


def retain_preparation_attempt(directory):
    """Keep command evidence before a preparation-only retry replaces it."""
    manifest = directory / "preparation.json"
    if not manifest.exists():
        return
    names = [name for name in ("preparation.json", "install.log") if (directory / name).exists()]
    if any((directory / name).is_symlink() or not (directory / name).is_file() for name in names):
        raise ContractError("preparation evidence must consist of regular files")
    hashes = {name: file_hash(directory / name) for name in names}
    target = directory / "attempts" / canonical_hash(hashes)
    target.mkdir(parents=True, exist_ok=True)
    for name in names:
        if not (target / name).exists():
            shutil.copy2(directory / name, target / name)
        if file_hash(target / name) != hashes[name]:
            raise ContractError("retained preparation evidence changed")
    atomic_json(target / "evidence.json", {"original_directory": str(directory), "sha256": hashes})


def relocate(value: str, context: dict[str, str]) -> str:
    # Replace known path placeholders only; ordinary shell ${...}, awk braces
    # and Python dictionaries must not be interpreted as format expressions.
    value = re.sub(r"\{(" + "|".join(map(re.escape, context)) + r")\}", lambda m: context[m[1]], value)
    return value.replace("{{", "{").replace("}}", "}")


class MigrationContext:
    def __init__(self, config: dict, tasks: dict, oracles: dict, legacy: Path, output: Path):
        self.config, self.tasks, self.oracles = config, tasks, oracles
        self.legacy, self.output = legacy, output.resolve()
        require_local_filesystem(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.cache = open_cache(config["storage"])
        self.repo_cache = Path(config["repo_cache"])
        self.scratch = Path(config["storage"]["scratch_dir"])
        self.scratch.mkdir(parents=True, exist_ok=True)
        self._bundle_lock, self._image_lock = threading.Lock(), threading.Lock()
        self._bundles, self._images = {}, {}
        self.catalog = json.loads(Path(config["storage"]["source_catalog"]).read_text())

    def repo(self, task):
        return self.repo_cache / task["repo"].replace("/", "__")

    def modules(self, task):
        from reviewbench_maintainability.agentic.sources import source_key
        source = self.catalog["sources"][source_key(task["repo"], task["start_sha"])]
        return [{**link, "omit": True,
                 "reason": "optional external checkout component; task public checks and frozen Oracle must verify without it"}
                for link in source["gitlinks"]]

    def bundle(self, root: Path, names: list[str] | None = None, *, role="dependency", git_commit=None):
        names = sorted(names if names is not None else tree_files(root))
        files = tree_files(root) if names is None else {n: root / n for n in names}
        records = []
        for name in names:
            path = files[name]
            records.append((name, os.readlink(path) if path.is_symlink() else file_hash(path),
                            "symlink" if path.is_symlink() else bool(path.stat().st_mode & 0o111)))
        digest = canonical_hash(records)
        key = canonical_hash({"tree": digest, "role": role, "git_commit": git_commit})
        path = self.output / "trees" / (key + ".json")
        with self._bundle_lock:
            if key not in self._bundles:
                if not path.exists():
                    with tempfile.TemporaryDirectory(prefix="runtime-bundle-", dir=self.scratch) as directory:
                        archive, receipts = archive_files(root, names, self.cache, Path(directory))
                    atomic_json(path, {"schema_version": ASSET_SCHEMA, "source_kind": role,
                                       "tree_sha256": digest, "archive": archive, "receipts": receipts,
                                       "files": len(names), "git_commit": git_commit})
                self._bundles[key] = {"path": str(path), "sha256": file_hash(path)}
            return dict(self._bundles[key])

    def copy_asset(self, source: Path, category: str) -> dict:
        if not source.is_file() or source.is_symlink():
            raise ContractError(f"missing original asset: {source}")
        digest = file_hash(source)
        target = self.output / "files" / category / (digest + "-" + source.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copyfile(source, target)
        if file_hash(target) != digest:
            raise ContractError("copied original asset changed")
        return {"path": str(target), "sha256": digest}

    def image(self, reference: str) -> tuple[str, dict]:
        with self._image_lock:
            if reference in self._images:
                return self._images[reference]
            configured = self.config.get("migration", {}).get("toolchain_registry")
            registry_file = Path(configured) if configured else self.output / "toolchains.json"
            if configured and not registry_file.is_absolute():
                raise ContractError("shared toolchain registry must be absolute")
            registry = json.loads(registry_file.read_text()) if registry_file.is_file() else {}
            mapped = registry.get(reference, {})
            resolved = mapped["image"] if mapped.get("status") == "ready" else reference
            data = json.loads(docker("image", "inspect", resolved))[0]
            image = data["Id"]
            audit_path = self.output / "image-audits" / (image.split(":")[1] + ".json")
            if not audit_path.exists():
                command = ("find / -xdev \\( -name .git -o -name '*source-only-golden.patch' "
                           "-o -name '*golden*.patch' -o -name oracles.jsonl "
                           "-o -name generation_results.jsonl \\) -print 2>/dev/null")
                result = docker("run", "--rm", "--read-only", "--network", "none", "--entrypoint",
                                "/bin/bash", image, "-c", command, timeout=180)
                if result.strip():
                    raise ContractError(f"toolchain image contains repository/reference material: {reference}: {result[:1000]}")
                history = json.loads("[" + ",".join(docker("history", "--no-trunc", "--format", "{{json .}}",
                                                            image).splitlines()) + "]")
                atomic_json(audit_path, {
                    "schema_version": "reviewbench-developer-image-audit/v1", "image": image,
                    "expected_image_id": image, "no_reference_assets": True, "no_repository_history": True,
                    "basis": "immutable toolchain image with retained history and executed rootfs scan",
                    "rootfs_scan_command": command, "rootfs_scan_output": result, "history": history,
                })
            result = image, {"path": str(audit_path), "sha256": file_hash(audit_path)}
            self._images[reference] = result
            return result

    def immutable_tests(self, task) -> list[str]:
        names = git(self.repo(task), "ls-tree", "-r", "--name-only", "-z", task["start_sha"]).split(b"\0")
        pattern = re.compile(r"(^|/)(tests?|testdata|fixtures?)(/|$)|(^|/)(test_[^/]*|[^/]*_test)\.[^/]+$", re.I)
        return [name.decode() for name in names if name and pattern.search(name.decode())]

    def task_directory(self, task, oracle, lane) -> Path:
        artifacts = oracle.get("artifacts")
        if isinstance(artifacts, dict):
            directory = artifacts.get("directory")
        else:
            directory = artifacts
        if not directory:
            raise ContractError("historical task artifact directory is absent")
        path = self.legacy / lane / directory
        if not path.is_dir():
            raise ContractError(f"historical task directory missing: {path}")
        return path

    def golden(self, task, oracle, lane, directory) -> dict:
        candidates = []
        if oracle.get("source_only_patch"):
            candidates.append(self.legacy / lane / oracle["source_only_patch"]["artifact"])
        source = oracle.get("source_only_golden")
        if isinstance(source, dict) and source.get("patch"):
            candidates.append(self.legacy / lane / source["patch"])
        artifacts = oracle.get("artifacts")
        if isinstance(artifacts, dict):
            candidates += [self.legacy / lane / artifacts[k] for k in ("patch", "golden_patch") if artifacts.get(k)]
        candidates += [directory / name for name in
                       ("dependency-closed-source-only.patch", "accepted-source-only-golden.patch", "source-only-golden.patch")]
        path = next((p for p in candidates if p.is_file()), None)
        if path is None:
            raise ContractError("frozen source-only golden asset is missing")
        result = self.copy_asset(path, "golden")
        expected = oracle.get("golden_patch_sha256") or (source.get("patch_sha256") if isinstance(source, dict) else None)
        if expected and result["sha256"] != expected:
            raise ContractError("golden differs from frozen Oracle digest")
        return result
