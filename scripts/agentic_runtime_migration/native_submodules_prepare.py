"""Acquire missing exact public Git commits under the shared execution gate.

Run as ``python -m agentic_runtime_migration.native_submodules_prepare``.
This prepares source objects only. It neither compiles nor verifies a task.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import posixpath
import signal
import subprocess
import sys
from urllib.parse import urlsplit, urlunsplit

import yaml

from reviewbench_maintainability.agentic.contracts import ContractError, atomic_json, file_hash
from reviewbench_maintainability.agentic.execution import ExecutionServices
from reviewbench_maintainability.agentic.models import engine_identity
from reviewbench_maintainability.agentic.resources import ResourceRequest
from reviewbench_maintainability.agentic.snapshots import git
from .native_submodules import check_repository, closure, readonly_links, selected_paths


def public_url(value, parent):
    """Resolve Git's relative-repository notation; never accept shell/SSH URLs."""
    if value.startswith(("./", "../")):
        base = urlsplit(parent)
        value = urlunsplit((base.scheme, base.netloc, posixpath.normpath(
            posixpath.join(base.path, value)), "", ""))
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname not in {"github.com", "gitlab.com"}
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or parsed.query or parsed.fragment or any(c.isspace() for c in value)
            or not parsed.path.startswith("/") or len(parsed.path.strip("/").split("/")) < 2):
        raise ContractError("native missing source needs an explicit public HTTPS repository URL: " + value)
    return value


class PinnedSources:
    def __init__(self, repo_cache, source_root, output):
        self.repo_cache, self.source_root, self.output = map(Path, (repo_cache, source_root, output))
        if not self.source_root.is_absolute() or self.source_root.is_symlink():
            raise ContractError("native missing Git objects require a private absolute source directory")
        self.source_root.mkdir(parents=True, exist_ok=True)
        self.urls = {}
        self.events = []

    def fetch(self, url, commit, repository):
        # The only writer is this private bare repository. Never fetch into a
        # shared original repository or copy its checkout/compiled products.
        if repository.parent != self.source_root or repository.is_symlink():
            raise ContractError("native pinned fetch target is not a private source repository")
        if not repository.exists():
            repository.mkdir()
            git(repository, "init", "--bare", "--quiet")
        command = [
            "git", "-c", "gc.auto=0", "-c", "maintenance.auto=false", "-c", "pack.threads=1",
            "-c", "pack.windowMemory=64m", "-c", "pack.deltaCacheSize=16m",
            "-C", str(repository), "fetch", "--no-tags", "--no-recurse-submodules",
            "--depth", "1", url, commit,
        ]
        event = {
            "kind": "fetch_exact_commit", "url": url, "commit": commit,
            "repository": str(repository), "command": command,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        log = self.output / "source-logs" / (hashlib.sha256(url.encode()).hexdigest()[:24] + "-" + commit + ".log")
        log.parent.mkdir(parents=True, exist_ok=True)
        # Do not replace evidence of a previous attempt.
        if log.exists():
            raise ContractError("native pinned fetch already has attempt evidence: " + str(log))
        with log.open("x") as stream:
            process = subprocess.Popen(
                command, stdout=stream, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                start_new_session=True,
                env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                     "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1"},
            )
            event["pid"] = process.pid
            print(json.dumps({"phase": "fetch_exact_commit", **event}), flush=True)
            try:
                code = process.wait(timeout=900)
                event["timed_out"] = False
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                code = process.wait()
                event["timed_out"] = True
        event.update(returncode=code, log=str(log), log_sha256=file_hash(log),
                     finished_at=datetime.now(timezone.utc).isoformat())
        self.events.append(event)
        atomic_json(log.with_suffix(".json"), event)
        if code or event["timed_out"]:
            raise ContractError(f"native exact-commit fetch failed: {url}@{commit}; log={log}; exit={code}")
        check_repository(repository, commit)
        git(repository, "update-ref", "refs/pinned/" + commit, commit)

    def resolve(self, parent, declaration, relative, absolute, commit, configured):
        url = public_url(declaration["url"], self.urls[str(Path(parent).resolve())])
        selected = configured.get(absolute)
        if isinstance(selected, dict):
            selected = selected.get(commit)
        candidates = [Path(selected)] if isinstance(selected, str) else []
        candidates.extend([
            Path(parent) / relative,
            Path(git(parent, "rev-parse", "--path-format=absolute", "--git-path",
                     "modules/" + declaration["name"]).decode().strip()),
        ])
        remote_path = urlsplit(url).path.strip("/").removesuffix(".git")
        candidates.append(self.repo_cache / remote_path.replace("/", "__"))
        private = self.source_root / (hashlib.sha256(url.encode()).hexdigest()[:24] + ".git")
        candidates.append(private)
        source = None
        for candidate in dict.fromkeys(candidates):
            try:
                source = check_repository(candidate, commit)
                self.events.append({"kind": "reuse_exact_local_commit", "path": absolute,
                                    "commit": commit, "repository": str(source), "url": url})
                break
            except ContractError:
                continue
        if source is None:
            self.fetch(url, commit, private)
            source = private
        self.urls[str(source.resolve())] = url
        return source


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--legacy-root", type=Path, required=True)
    parser.add_argument("--ids", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    args = parser.parse_args(argv)
    config = yaml.safe_load(args.config.read_text())
    root = Path(config["root"])
    for key in ("tasks", "oracles", "scope_ids"):
        if not Path(config[key]).is_absolute():
            config[key] = str(root / config[key])
    scope = set(Path(config["scope_ids"]).read_text().splitlines())
    tasks = {row["id"]: row for row in map(json.loads, Path(config["tasks"]).read_text().splitlines())
             if row["id"] in scope}
    oracles = {row["id"]: row for row in map(json.loads, Path(config["oracles"]).read_text().splitlines())}
    plans = {row["input_index"]: row for row in map(json.loads,
             (args.legacy_root / "native/plans.jsonl").read_text().splitlines())}
    ids = args.ids.read_text().splitlines()
    if len(ids) != len(set(ids)) or not ids or not set(ids) <= tasks.keys():
        raise ContractError("native pinned-source cohort has duplicate/missing/foreign task IDs")
    fingerprint = engine_identity()["fingerprint"]
    if fingerprint != config.get("migration", {}).get("verification_engine_fingerprint"):
        raise ContractError("native pinned-source preparation must use the frozen configured engine")
    output = args.out_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / "source-preparation.json").exists():
        raise ContractError("native pinned-source preparation already completed; inspect retained evidence")
    config["out_dir"] = str(output / "execution")
    services = ExecutionServices(config)
    sources = PinnedSources(config["repo_cache"], args.source_root, output)
    locations = config.setdefault("migration", {}).setdefault("native_submodule_repositories", {})
    rows = []
    original_affinity = os.sched_getaffinity(0)
    request = ResourceRequest("prepare-native-pinned-submodules", cpus=1,
                              memory_bytes=1024**3, scratch_bytes=6 * 1024**3)
    print(json.dumps({"phase": "waiting_shared_gate", "pid": os.getpid(), "cohort": ids}), flush=True)
    with services.gate.acquire(request) as lease:
        os.sched_setaffinity(0, {min(original_affinity)})
        try:
            for ident in ids:
                task = tasks[ident]
                row = {"id": ident, "repo": task["repo"], "start_sha": task["start_sha"]}
                repository = Path(config["repo_cache"]) / task["repo"].replace("/", "__")
                sources.urls[str(repository.resolve())] = "https://github.com/" + task["repo"] + ".git"
                try:
                    plan = plans[oracles[ident]["input_index"]]
                    if plan["repo"] != task["repo"]:
                        raise ContractError("native source preparation plan belongs to another repository")
                    paths = selected_paths(task, plan, readonly_links(repository, task["start_sha"]))
                    if paths is None:
                        raise ContractError("native source preparation profile is not supported")
                    configured = locations.setdefault(task["repo"], {})
                    records, totals = closure(repository, task["start_sha"], paths, configured,
                                               resolver=sources.resolve)
                    for record in records:
                        previous = configured.setdefault(record["path"], {})
                        if not isinstance(previous, dict):
                            raise ContractError("native source preparer requires per-pin source mappings")
                        previous[record["commit"]] = record["repository"]
                    row.update(status="sources_ready", roots=list(paths), closure=records, totals=totals,
                               runtime_verified=False)
                except Exception as exc:
                    row.update(status="blocked", reason=f"{type(exc).__name__}: {exc}", runtime_verified=False)
                rows.append(row)
                atomic_json(output / "source-progress.json", {
                    "pid": os.getpid(), "engine_fingerprint": fingerprint, "lease": lease,
                    "expected_tasks": len(tasks), "cohort_size": len(ids), "tasks": rows,
                    "events": sources.events,
                })
                print(json.dumps({k: v for k, v in row.items() if k not in {"closure", "totals"}}), flush=True)
        finally:
            os.sched_setaffinity(0, original_affinity)
    configured_path = output / "prepared-config.yaml"
    configured_path.write_text(yaml.safe_dump(config, sort_keys=False))
    report = {
        "status": "sources_prepared" if all(row["status"] == "sources_ready" for row in rows) else "partial",
        "expected_tasks": len(tasks), "cohort_size": len(ids), "engine_fingerprint": fingerprint,
        "tasks": rows, "events": sources.events, "config": str(configured_path),
        "config_sha256": file_hash(configured_path), "workers": 1, "model_calls": 0,
        "registration_performed": False, "runtime_verified": False,
    }
    atomic_json(output / "source-preparation.json", report)
    assert json.loads((output / "source-preparation.json").read_text()) == report
    return 0 if report["status"] == "sources_prepared" else 2


if __name__ == "__main__":
    raise SystemExit(main())
