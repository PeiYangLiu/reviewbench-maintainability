"""Persistent Docker workspaces with real, bounded command observations."""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import time
import uuid

from .contracts import (
    ContractError, InfrastructureError, RuntimeRecipe, SUBMIT_COMMAND,
    atomic_json, canonical_hash, file_hash,
)
from .resources import CURRENT_JOB


def docker(*args: str, timeout: float = 120) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise InfrastructureError(f"docker {args[0]}: {result.stderr[-1500:]}")
    return result.stdout.strip()


def resolve_asset(asset: dict, root: Path) -> Path:
    path = Path(asset["path"])
    path = path if path.is_absolute() else root / path
    if not path.is_file() or path.is_symlink() or file_hash(path) != asset["sha256"]:
        raise ContractError(f"missing or changed pinned asset: {path}")
    return path.resolve()


def validate_assets(recipe: RuntimeRecipe, asset_root: Path, *, inspect_images: bool = True) -> dict:
    audit_path = resolve_asset(recipe.image_audit, asset_root)
    audit = json.loads(audit_path.read_text())
    if (audit.get("image") != recipe.image
            or audit.get("schema_version") != "reviewbench-developer-image-audit/v1"
            or audit.get("no_reference_assets") is not True
            or audit.get("no_repository_history") is not True):
        raise ContractError("developer image lacks a matching reference/history isolation audit")
    resolved = [str(resolve_asset(a, asset_root)) for a in recipe.assets]
    for asset in recipe.assets:
        if asset.get("kind") == "archive":
            from .assets import asset_descriptor
            descriptor = asset_descriptor(asset, asset_root)
            if asset["visibility"] == "developer" and descriptor["source_kind"] == "grader":
                raise ContractError("grader archive cannot be exposed as a developer asset")
    images = [recipe.image, *(s["image"] for s in recipe.sidecars)]
    identities = {}
    if inspect_images:
        for image in images:
            data = json.loads(docker("image", "inspect", image))[0]
            if image == recipe.image and audit.get("expected_image_id") and data["Id"] != audit["expected_image_id"]:
                raise ContractError("resolved image ID differs from developer image audit")
            identities[image] = data["Id"]
    return {"audit_sha256": file_hash(audit_path), "assets": resolved, "images": identities}


class ReviewBenchDockerEnvironment:
    """mini's Environment protocol. Only the workspace is solver-writable on host.

    stdout/stderr and the execution journal live outside all container mounts.
    Final grading uses a different instance with explicitly grader-visible assets.
    """

    def __init__(self, recipe: RuntimeRecipe, workspace: Path, artifacts: Path,
                 asset_root: Path, *, grader: bool = False, container_id: str | None = None,
                 max_output_chars: int = 10000, max_log_bytes: int = 16 * 1024 * 1024,
                 lifetime_seconds: int = 7200):
        self.recipe = recipe
        self.workspace = workspace.resolve()
        self.artifacts = artifacts.resolve()
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.asset_root = asset_root
        self.grader = grader
        if max_output_chars < 2 or max_log_bytes < 1:
            raise ContractError("invalid observation/log size limits")
        self.max_output_chars = max_output_chars
        self.max_log_bytes = max_log_bytes
        self.lifetime_seconds = max(1, int(lifetime_seconds))
        self.deadline: float | None = None
        self.config = recipe
        self.container_id = container_id
        self.network_id: str | None = None
        self.sidecar_ids: list[str] = []
        self.records: list[dict] = []
        self.pending: str | None = None
        self.resource_job = CURRENT_JOB.get()
        if self.resource_job:
            self.resource_job.setdefault("workspace_exclusions", {})[str(self.workspace)] = recipe.scratch_paths
        self.identity = canonical_hash({
            "recipe": recipe.fingerprint, "workspace": str(self.workspace), "grader": grader,
        })
        if self.resource_job:
            self.resource_job["mark_container_use"]()
        if container_id:
            details = json.loads(docker("inspect", container_id))[0]
            if (not details["State"]["Running"]
                    or details["Config"].get("Labels", {}).get("reviewbench.identity") != self.identity):
                raise InfrastructureError("saved sandbox is missing, stopped, or belongs to another episode")
        else:
            self._start()

    def _start(self) -> None:
        validate_assets(self.recipe, self.asset_root)
        token = uuid.uuid4().hex[:16]
        try:
            if self.recipe.sidecars:
                self.network_id = docker("network", "create", "--internal", f"rb-exec-{token}")
                for item in self.recipe.sidecars:
                    args = ["run", "-d", "--network", self.network_id, "--network-alias", item["name"],
                            "--cap-drop=ALL", "--security-opt=no-new-privileges",
                            "--pids-limit", str(self.recipe.pids_limit),
                            "--memory", f"{self.recipe.memory_mb}m", "--cpus", str(self.recipe.cpus),
                            "--label", f"reviewbench.identity={self.identity}"]
                    for key, value in item.get("env", {}).items():
                        args.extend(["-e", f"{key}={value}"])
                    if self.resource_job:
                        args.extend(["--label", "reviewbench.resource-lease=" + self.resource_job["lease"]])
                    args.extend([item["image"], *item.get("command", [])])
                    cid = docker(*args)
                    self.sidecar_ids.append(cid)
                    health = item.get("health_command")
                    if health:
                        for _ in range(30):
                            check = subprocess.run(["docker", "exec", cid, "sh", "-c", health],
                                                   capture_output=True, timeout=10)
                            if check.returncode == 0:
                                break
                            time.sleep(1)
                        else:
                            raise InfrastructureError(f"sidecar never became healthy: {item['name']}")
            args = [
                "run", "-d", "--name", f"rb-exec-{token}", "--read-only", "--init",
                "--user", f"{os.getuid()}:{os.getgid()}", "--cap-drop=ALL",
                "--security-opt=no-new-privileges", "--pids-limit", str(self.recipe.pids_limit),
                "--memory", f"{self.recipe.memory_mb}m", "--cpus", str(self.recipe.cpus),
                # Docker's tmpfs defaults include noexec. Native builds use a
                # private /tmp build tree and must be able to execute their
                # binaries (CMake compiler probes also depend on this).
                "--network", self.network_id or "none", "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=2g",
                "--label", f"reviewbench.identity={self.identity}",
                "--mount", f"type=bind,source={self.workspace},target=/workspace",
                "-w", self.recipe.workdir,
                "-e", "HOME=/tmp/reviewbench-home", "-e", "PAGER=cat",
                # Same-length edits within one filesystem timestamp tick can
                # otherwise reuse stale Python bytecode in fast repair loops.
                "-e", "PYTHONDONTWRITEBYTECODE=1",
                "-e", "GIT_CONFIG_GLOBAL=/dev/null", "-e", "GIT_CONFIG_NOSYSTEM=1",
            ]
            for asset in self.recipe.assets:
                if asset["visibility"] == "grader" and not self.grader:
                    continue
                if asset.get("kind") == "archive":
                    if not self.resource_job or not self.resource_job.get("asset_pool"):
                        raise ContractError("archive assets require an admitted execution job")
                    source = self.resource_job["asset_pool"].materialize(asset)
                else:
                    source = resolve_asset(asset, self.asset_root)
                args.extend(["--mount", f"type=bind,source={source},target={asset['target']},readonly"])
            for name, target in self.recipe.cache_mounts.items():
                if not self.resource_job or not self.resource_job.get("asset_pool"):
                    raise ContractError("task caches require an admitted execution job")
                source = self.resource_job["asset_pool"].mutable_cache(name)
                args.extend(["--mount", f"type=bind,source={source},target={target}"])
            if self.resource_job:
                args.extend(["--label", "reviewbench.resource-lease=" + self.resource_job["lease"]])
            for key, value in self.recipe.env.items():
                args.extend(["-e", f"{key}={value}"])
            args.extend(["--entrypoint", "/bin/bash", self.recipe.image,
                         "-c", f"mkdir -p \"$HOME\"; exec sleep {self.lifetime_seconds}"])
            self.container_id = docker(*args)
        except Exception:
            self.close()
            raise

    def execute(self, action: dict, cwd: str = "", *, timeout: float | None = None) -> dict:
        command = action.get("command")
        if not isinstance(command, str) or not command.strip() or "\0" in command:
            return {"output": "", "returncode": 2, "exception_info": "bash command must be a nonempty string"}
        if not self.container_id:
            raise InfrastructureError("sandbox is closed")
        working_dir = cwd or self.recipe.workdir
        if working_dir != "/workspace" and not working_dir.startswith("/workspace/"):
            raise ContractError("command workdir must remain within /workspace")
        from .contracts import repo_path
        repo_path(working_dir.removeprefix("/workspace").lstrip("/"), allow_root=True)
        allowed = min(timeout or self.recipe.command_timeout_seconds, self.recipe.command_timeout_seconds)
        if self.deadline is not None:
            allowed = min(allowed, self.deadline - time.monotonic())
        if allowed <= 0:
            from .models import BudgetLimit
            raise BudgetLimit("wall_time_limit")
        ident = uuid.uuid4().hex
        out_path, err_path = self.artifacts / f"{ident}.stdout", self.artifacts / f"{ident}.stderr"
        self.pending = ident
        journal = {"id": ident, "tool_call_id": action.get("tool_call_id"),
                   "command": command, "cwd": working_dir, "status": "running",
                   "stdout_file": out_path.name, "stderr_file": err_path.name}
        atomic_json(self.artifacts / f"{ident}.json", journal)
        started = time.monotonic()
        # timeout supervises the shell *inside* the container, including its
        # process group; killing only the docker client leaves commands running.
        argv = ["docker", "exec", "-w", working_dir, self.container_id,
                "timeout", "--signal=TERM", "--kill-after=2s", f"{allowed:.3f}s",
                "/bin/bash", "-c", command]
        exceeded_log_limit = False
        with out_path.open("wb") as stdout, err_path.open("wb") as stderr:
            process = subprocess.Popen(argv, stdout=stdout, stderr=stderr, start_new_session=True)
            try:
                while process.poll() is None:
                    if (self.resource_job and min(shutil.disk_usage(path).free for path in
                            {self.workspace, Path(self.resource_job["scratch_root"])})
                            < self.resource_job["disk_floor_bytes"]):
                        docker("stop", "--time", "1", self.container_id, timeout=10)
                        journal.update(status="terminated", reason="host_disk_headroom")
                        atomic_json(self.artifacts / f"{ident}.json", journal)
                        raise InfrastructureError("host disk floor reached; managed sandbox stopped")
                    if out_path.stat().st_size + err_path.stat().st_size > self.max_log_bytes:
                        exceeded_log_limit = True
                        # A stopped container guarantees a writer cannot continue
                        # filling the host's log files after the client is killed.
                        docker("stop", "--time", "1", self.container_id, timeout=10)
                        journal.update(status="terminated", reason="tool_output_limit")
                        atomic_json(self.artifacts / f"{ident}.json", journal)
                        from .models import BudgetLimit
                        raise BudgetLimit("tool_output_limit")
                    if time.monotonic() - started > allowed + 8:
                        docker("stop", "--time", "1", self.container_id, timeout=10)
                        raise InfrastructureError("container command supervisor did not terminate")
                    time.sleep(0.02)
                code = process.returncode
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        stdout_text = out_path.read_text(errors="replace")
        stderr_text = err_path.read_text(errors="replace")
        raw = stdout_text + stderr_text
        timed_out = code in {124, 137}
        # A shell command may legitimately return 125/126/127 (including a
        # solver-created executable with bad permissions). Those are feedback,
        # not infrastructure failures. Classify actual Docker/supervisor
        # diagnostics instead of reserving ordinary process exit statuses.
        substrate_error = (
            any(line.startswith(("Error response from daemon:", "OCI runtime exec failed:"))
                for line in raw.splitlines())
            or (stderr_text.startswith("timeout: failed to run command") and "/bin/bash" in stderr_text)
        )
        if code and substrate_error:
            raise InfrastructureError(f"execution substrate failed: {raw[-1000:]}")
        submitted = command.strip() == SUBMIT_COMMAND and code == 0
        journal.update(status="completed", returncode=code, timed_out=timed_out,
                       duration_seconds=round(time.monotonic() - started, 6),
                       stdout_sha256=file_hash(out_path), stderr_sha256=file_hash(err_path),
                       submitted=submitted, log_limit_exceeded=exceeded_log_limit)
        atomic_json(self.artifacts / f"{ident}.json", journal)
        self.records.append(journal)
        self.pending = None
        if len(raw) > self.max_output_chars:
            half = self.max_output_chars // 2
            output = raw[:half] + f"\n[... {len(raw)-2*half} characters omitted ...]\n" + raw[-half:]
        else:
            output = raw
        def excerpt(text, maximum):
            if len(text) <= maximum:
                return text
            half = maximum // 2
            return text[:half] + "\n[... truncated; full stream saved ...]\n" + text[-half:]
        # Give an otherwise empty stream's allowance to the nonempty stream.
        stdout_cap = self.max_output_chars - min(len(stderr_text), self.max_output_chars // 2)
        stderr_cap = self.max_output_chars - min(len(stdout_text), stdout_cap)
        return {
            "output": output, "returncode": code,
            "stdout": excerpt(stdout_text, stdout_cap), "stderr": excerpt(stderr_text, stderr_cap),
            "exception_info": "command timed out" if timed_out else "",
            "submitted": submitted,
            "extra": {**journal, "raw_output_chars": len(raw), "stdout_chars": len(stdout_text),
                      "stderr_chars": len(stderr_text)},
        }

    def get_template_vars(self, **kwargs) -> dict:
        # Never expose grading commands/assets or the full RuntimeRecipe to a prompt.
        return {"cwd": self.recipe.workdir, **kwargs}

    def serialize(self) -> dict:
        return {"info": {"environment": {
            "image": self.recipe.image, "runtime_fingerprint": self.recipe.fingerprint,
            "container_id": self.container_id, "identity": self.identity,
            "workspace": "workspace", "network_id": self.network_id,
            "resource_lease": self.resource_job["lease"] if self.resource_job else None,
            "sidecar_ids": self.sidecar_ids, "pending_action": self.pending,
            "commands": self.records,
        }}}

    def close(self) -> None:
        for cid in [self.container_id, *reversed(self.sidecar_ids)]:
            if cid:
                subprocess.run(["docker", "rm", "-f", cid], capture_output=True, timeout=30)
        self.container_id = None
        self.sidecar_ids = []
        if self.network_id:
            subprocess.run(["docker", "network", "rm", self.network_id], capture_output=True, timeout=30)
            self.network_id = None
