"""Small, explicit public/private contracts and content-addressed artifacts."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any

from . import PROTOCOL, RUNTIME_SCHEMA

LANES = ("gopy", "scripted", "tsjs", "csharp", "jvm", "rust", "native")
SUBMIT_COMMAND = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"


class ContractError(ValueError):
    pass


class InfrastructureError(RuntimeError):
    pass


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()).hexdigest()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    atomic_text(path, json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with pending.open("w", encoding="utf-8") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    pending.replace(path)


def read_jsonl(path: Path, key: str = "id") -> dict[str, dict]:
    result = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        ident = row.get(key) if isinstance(row, dict) else None
        if not isinstance(ident, str) or not ident or ident in result:
            raise ContractError(f"{path}:{number}: missing/duplicate {key}: {ident!r}")
        result[ident] = row
    return result


def write_jsonl(path: Path, rows: list[dict]) -> None:
    atomic_text(path, "".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in rows))


def component(value: str) -> str:
    # A digest prevents collisions between, for example, provider/a and provider_a.
    readable = re.sub(r"[^a-zA-Z0-9._-]", "_", value).strip(".")[:100]
    return f"{readable or 'item'}-{hashlib.sha256(value.encode()).hexdigest()[:12]}"


def repo_path(value: str, *, allow_root: bool = False) -> str:
    if not isinstance(value, str):
        raise ContractError(f"invalid repository path: {value!r}")
    path = PurePosixPath(value)
    if allow_root and value in {"", "."}:
        return "."
    if (not value or not path.parts or path.is_absolute()
            or any(p in {"..", ".git", ".reviewbench"} for p in path.parts)
            or "\0" in value or "\\" in value):
        raise ContractError(f"invalid repository path: {value!r}")
    return path.as_posix()


def lane_for(task: dict, oracle: dict) -> str:
    source = str(oracle.get("lane_results_source") or "").replace("\\", "/")
    for lane in LANES:
        if f"/{lane}/results.jsonl" in source:
            return lane
    language = str(task.get("inferred_language") or oracle.get("language") or "").lower()
    if language in {"go", "golang", "python"}:
        return "gopy"
    if language in {"typescript", "javascript"}:
        return "tsjs"
    if language in {"c#", "csharp"}:
        return "csharp"
    if language in {"java", "scala", "kotlin"}:
        return "jvm"
    if language == "rust":
        return "rust"
    if language in {"c", "c++", "c/c++", "native"}:
        return "native"
    return "scripted"


@dataclass(frozen=True)
class PublicTaskSpec:
    id: str
    repo: str
    start_sha: str
    instruction: str
    locality: dict[str, Any]

    @classmethod
    def from_task(cls, task: dict) -> "PublicTaskSpec":
        # Deliberate projection: never pass the source task dictionary to mini.
        locality = task.get("locality") or {}
        path = repo_path(str(locality.get("path") or ""))
        for key in ("id", "repo", "start_sha"):
            if not isinstance(task.get(key), str) or not task[key]:
                raise ContractError(f"task missing {key}")
        if not re.fullmatch(r"[0-9a-f]{40}", task["start_sha"]):
            raise ContractError("start_sha must be a full Git commit ID")
        return cls(task["id"], task["repo"], task["start_sha"],
                   str(task.get("task_instruction") or ""),
                   {k: v for k, v in {"path": path, "line": locality.get("line"),
                                      "start_line": locality.get("start_line")}.items() if v is not None})

    def prompt(self, dev_checks: list[str], *, workdir: str = "/workspace") -> str:
        return (f"Working directory: {workdir}\nRepository: {self.repo}\n"
                f"Review location: {json.dumps(self.locality)}\n"
                f"Reviewer concern:\n{self.instruction}\n\n"
                "Public development checks (not final grading):\n"
                + "\n".join(f"- {c}" for c in dev_checks))


@dataclass(frozen=True)
class RuntimeRecipe:
    task_id: str
    repo: str
    start_sha: str
    lane: str
    image: str
    # Audit is a content-addressed file describing a clean developer image, not
    # a historical Oracle's "golden correctness" attestation.
    image_audit: dict[str, str]
    dev_checks: list[str]
    bootstrap: list[str] = field(default_factory=list)
    workdir: str = "/workspace"
    env: dict[str, str] = field(default_factory=dict)
    assets: list[dict[str, Any]] = field(default_factory=list)
    sidecars: list[dict[str, Any]] = field(default_factory=list)
    command_timeout_seconds: int = 600
    cpus: float = 4
    memory_mb: int = 8192
    pids_limit: int = 512
    build_scratch_mb: int = 8192
    scratch_paths: list[str] = field(default_factory=list)
    cache_mounts: dict[str, str] = field(default_factory=dict)
    # Every root gitlink is explicitly either populated at its pinned commit,
    # or left unmaterialized as an optional checkout component. A root-only
    # archive never silently acquires a "complete submodule closure" label.
    submodules: list[dict[str, Any]] = field(default_factory=list)
    grading: dict[str, Any] = field(default_factory=dict)
    schema_version: str = RUNTIME_SCHEMA

    @classmethod
    def from_dict(cls, value: dict) -> "RuntimeRecipe":
        try:
            result = cls(**value)
        except (TypeError, ValueError) as exc:
            raise ContractError(f"invalid runtime recipe: {exc}") from exc
        result.validate()
        return result

    def validate(self) -> None:
        if self.schema_version != RUNTIME_SCHEMA or self.lane not in LANES:
            raise ContractError("unsupported runtime schema/lane")
        if not re.fullmatch(r"(?:[^@\s]+@)?sha256:[0-9a-f]{64}", self.image):
            raise ContractError("developer image must use an immutable image digest")
        if not re.fullmatch(r"[0-9a-f]{40}", self.start_sha):
            raise ContractError("recipe start_sha must be a full commit ID")
        if not self.dev_checks or any(not isinstance(c, str) or not c.strip() for c in self.dev_checks):
            raise ContractError("recipe requires nonempty public development checks")
        if self.workdir != "/workspace" and not self.workdir.startswith("/workspace/"):
            raise ContractError("workdir must be within /workspace")
        repo_path(self.workdir.removeprefix("/workspace").lstrip("/"), allow_root=True)
        if (self.command_timeout_seconds <= 0 or self.cpus <= 0 or self.memory_mb < 64
                or self.pids_limit < 16 or self.build_scratch_mb < 0):
            raise ContractError("invalid runtime resource limits")
        for k, v in self.env.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) or not isinstance(v, str):
                raise ContractError("invalid environment entry")
            if any(word in k.upper() for word in ("TOKEN", "SECRET", "PASSWORD", "API_KEY")):
                raise ContractError("model/service credentials do not belong in developer environments")
        for asset in self.assets:
            if asset.get("visibility") not in {"developer", "grader"}:
                raise ContractError("asset visibility must be explicit")
            if not re.fullmatch(r"[0-9a-f]{64}", str(asset.get("sha256", ""))):
                raise ContractError("assets require sha256")
            target = str(asset.get("target", ""))
            if not target.startswith("/opt/reviewbench-assets/"):
                raise ContractError("assets must be mounted under /opt/reviewbench-assets/")
            repo_path(target.lstrip("/"))
            if asset.get("kind", "file") not in {"file", "archive"}:
                raise ContractError("unsupported runtime asset kind")
        seen = set()
        for path in self.scratch_paths:
            repo_path(path)
        for name, target in self.cache_mounts.items():
            if (not re.fullmatch(r"[A-Za-z0-9_-]+", name)
                    or not target.startswith("/opt/reviewbench-cache/")):
                raise ContractError("invalid per-task mutable cache mount")
            repo_path(target.lstrip("/"))
        for module in self.submodules:
            path = repo_path(module.get("path", ""))
            if path in seen or not re.fullmatch(r"[0-9a-f]{40}", module.get("commit", "")):
                raise ContractError("duplicate/invalid pinned submodule")
            seen.add(path)
            if module.get("omit") is True:
                if not isinstance(module.get("reason"), str) or not module["reason"].strip() or module.get("asset"):
                    raise ContractError("unmaterialized submodules require an explicit reason, not an asset")
            elif not isinstance(module.get("asset"), dict):
                raise ContractError("required submodule needs a pinned source asset")
        for sidecar in self.sidecars:
            if not re.fullmatch(r"[a-z][a-z0-9-]*", sidecar.get("name", "")):
                raise ContractError("invalid sidecar name")
            if not re.fullmatch(r"(?:[^@\s]+@)?sha256:[0-9a-f]{64}", sidecar.get("image", "")):
                raise ContractError("sidecar image must be pinned")
            if any(k not in {"name", "image", "command", "env", "health_command"} for k in sidecar):
                raise ContractError("unsupported sidecar option")

    @property
    def fingerprint(self) -> str:
        return canonical_hash(asdict(self))

    def bind(self, task: PublicTaskSpec) -> None:
        if (self.task_id, self.repo, self.start_sha) != (task.id, task.repo, task.start_sha):
            raise ContractError("runtime recipe does not match task")


@dataclass
class EpisodeResult:
    id: str
    model: str
    task_fingerprint: str
    runtime_fingerprint: str
    config_fingerprint: str
    start_sha: str
    engine_fingerprint: str = ""
    status: str = "infra_error"
    stop_reason: str = ""
    apply_ok: bool = False
    patch_file: str | None = None
    patch_sha256: str | None = None
    changed_files: list[str] = field(default_factory=list)
    changed_lines: int = 0
    usage: dict = field(default_factory=dict)
    trace_file: str | None = None
    error: str | None = None
    protocol: str = PROTOCOL


def selected_tasks(tasks_path: Path, ids_path: Path | None) -> dict[str, dict]:
    tasks = read_jsonl(tasks_path)
    if ids_path:
        lines = [x.strip() for x in ids_path.read_text().splitlines() if x.strip()]
        if len(set(lines)) != len(lines) or set(lines) - set(tasks):
            raise ContractError("scope IDs are duplicated or missing from tasks")
        tasks = {ident: tasks[ident] for ident in lines}
    if not tasks:
        raise ContractError("empty task scope")
    for task in tasks.values():
        PublicTaskSpec.from_task(task)
    return tasks
