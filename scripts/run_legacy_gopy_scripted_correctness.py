#!/usr/bin/env python3
"""Provenance-preserving correctness replay adapter for the 19 published-hard
tasks whose lane is ``gopy`` (15) or ``scripted`` (4) and whose Oracles were
recovered before the modern lane waves, so they carry no
``lane_results_source`` replay contract (``source_kind == "published_legacy"``
in ``run_open_model_correctness.py``'s task plan).

Like its sibling ``run_legacy_reserve_correctness.py`` (which covers the 14
tasks sourced from the 20260728 reserve: native/rust/jvm/tsjs), this script
does NOT reinvent normalization or finalization. It only populates the
extension point that ``run_open_model_correctness.py`` already reads but that
nothing currently writes for these lanes:

    <output>/legacy/correctness/gopy.jsonl
    <output>/legacy/correctness/scripted.jsonl

Those raw rows are merged by ``run_open_model_correctness.py finalize``
alongside ``<output>/modern/correctness/<lane>.jsonl`` and normalized with the
very same ``normalized_record()`` used for the 374 modern tasks.

What is recovered, and how
--------------------------
The per-task replay recipes live in a reviewable data file rather than in
code: ``data/legacy_contracts/gopy_scripted_20260808.json``
(``--contract``). Every one of the 19 published-legacy gopy/scripted tasks
appears there exactly once, either as ``status == "recovered"`` with a fully
pinned recipe, or as ``status == "blocked"`` with the exact lost assets and
the evidence that they are lost.

Three recovered recipe kinds are implemented, each replaying the historical,
previously *proven* behavioral Oracle -- never a source-shape substitute:

``go_docker_p2p``
    A fresh git worktree of the retained ``repos/<org>__<repo>`` clone is
    checked out at the exact task ``start_sha``, the staged candidate patch is
    applied, and the historical container (pinned ``golang:*`` image, task
    dedicated Go module/build cache volumes, recorded network mode) runs
    ``go test -count=1 -json -run <oracle.test_selector> <test_target>`` from
    the recorded ``module_dir``. Correctness requires every name in the
    Oracle's own ``pass_to_pass_tests`` list to report a ``pass`` action in
    the ``go test -json`` event stream.

``go_docker_session_oracle``
    Same materialization, plus the retained, SHA-256-pinned session Oracle
    test file is copied to its recorded destination inside the worktree
    before the historical ``go test -tags ... -run <selector>`` command runs.

``python_image_pytest``
    The historical Docker image itself bakes the exact start checkout (built
    in place with the pinned scientific-Python toolchain). A disposable
    container applies the staged candidate patch to that in-image workspace
    and runs the historical ``pytest`` command with ``--junitxml``; the JUnit
    report is parsed for the recorded expected test count. Preflight verifies
    the in-image workspace anchor file is byte-identical to the repository
    blob at ``start_sha``, so a drifted image can never be replayed as if it
    were the exact start.

Fail-closed guarantees
----------------------
* A task is replayed only if its contract entry is ``recovered`` *and* every
  required live asset resolves right now: repo checkout containing the exact
  ``start_sha``, Docker image present (and matching the recorded image ID when
  one was recorded), Go module/build cache volumes present, session Oracle
  asset present with the recorded SHA-256, in-image workspace anchor matching
  the start revision. Anything missing is a preflight blocker and is never
  scored, never guessed, and never downgraded to a model failure.
* Blocked tasks additionally re-verify their recorded ``missing_assets``: if
  every asset a blocker depends on has reappeared, preflight reports
  ``blocker_possibly_resolved`` and fails, so a recovered asset cannot be
  silently ignored.
* Nothing is ever fetched from the network to make a run succeed. Containers
  run with the recorded network mode only (``none`` unless the historical
  environment recorded ``host``), Go runs with ``GOTOOLCHAIN=local`` and
  ``GOFLAGS=-mod=mod`` disabled, and dependency resolution comes exclusively
  from the retained, ``go.sum``-verified module caches.
* Resume validates provenance, not just presence: every raw row embeds
  ``task_fingerprint``/``oracle_fingerprint``/``replay_contract_fingerprint``
  (which folds in ``ADAPTER_CONTRACT_VERSION``, the contract entry, the
  live asset state and the current blockers) plus the staged
  ``patch_sha256``. Stale rows are rerun; rows whose task became blocked are
  retracted from disk instead of being left behind for ``finalize``.

Typical usage
-------------
    # 1) Stage every model's generation results with the existing
    #    orchestrator, allowing the 40 published-legacy tasks to stay
    #    unresolved for now:
    python3 scripts/run_open_model_correctness.py prepare \\
      --tasks data/releases/hard/20260808/tasks.jsonl \\
      --oracles data/releases/hard/20260808/oracles.jsonl \\
      --repo-cache "$REVIEWBENCH_SESSION_FILES/repos" --output <output> \\
      --model 'my-model=/path/to/generation_results.jsonl' \\
      --allow-partial-modern

    # 2) Preflight the 19 gopy/scripted contracts (executes nothing):
    python3 scripts/run_legacy_gopy_scripted_correctness.py preflight \\
      --tasks data/releases/hard/20260808/tasks.jsonl \\
      --oracles data/releases/hard/20260808/oracles.jsonl \\
      --legacy-root /data/<user>/reviewbench-session-<id> \\
      --asset-root  /data/<user>/reviewbench-openmodels/legacy-recovery-assets/gopy-scripted \\
      --output <output> --allow-blocked

    # 3) Replay only the ready tasks for one already-staged model:
    python3 scripts/run_legacy_gopy_scripted_correctness.py replay \\
      --tasks ... --oracles ... --legacy-root ... --asset-root ... \\
      --output <output> --model my-model --allow-blocked

    # 4) Finalize as usual; still-blocked pairs stay in "missing_pairs".
    python3 scripts/run_open_model_correctness.py finalize \\
      --tasks ... --oracles ... --output <output> --allow-incomplete

``restore-caches`` recreates a lost Go module cache volume from the packaged,
SHA-256-verified archive under ``--asset-root`` (never from the network).
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


def _load_sibling_module():
    path = Path(__file__).resolve().parent / "run_open_model_correctness.py"
    spec = importlib.util.spec_from_file_location(
        "run_open_model_correctness",
        path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BASE = _load_sibling_module()

DEFAULT_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "data/legacy_contracts/gopy_scripted_20260808.json"
)

CONTRACT_SCHEMA = "reviewbench-legacy-replay-contract/v1"
SUPPORTED_LANES = ("gopy", "scripted")
RECIPE_KINDS = (
    "go_docker_p2p",
    "go_docker_session_oracle",
    "python_image_pytest",
)

INFRA_RETRY = "infra_retry"
CANDIDATE_MISSING = "candidate_missing"

# Bump whenever the *behavior* of a replay_* function or a judging rule
# changes in a way that could change the outcome for already-recorded rows.
# Folded into every raw row's ``replay_contract_fingerprint`` so resume reruns
# rows produced under a since-changed contract.
ADAPTER_CONTRACT_VERSION = "v1-2026-08-13-gopy-scripted"


# ---------------------------------------------------------------------------
# Contract loading
# ---------------------------------------------------------------------------


def load_contract(path: Path) -> dict[str, Any]:
    contract = json.loads(path.read_text(encoding="utf-8"))
    if contract.get("schema_version") != CONTRACT_SCHEMA:
        raise ValueError(
            f"unexpected contract schema: {contract.get('schema_version')!r}"
        )
    tasks = contract.get("tasks")
    if not isinstance(tasks, dict) or not tasks:
        raise ValueError("contract carries no tasks")
    for task_id, entry in tasks.items():
        lane = entry.get("lane")
        if lane not in SUPPORTED_LANES:
            raise ValueError(f"{task_id}: unsupported contract lane {lane!r}")
        status = entry.get("status")
        if status not in {"recovered", "blocked"}:
            raise ValueError(f"{task_id}: unsupported contract status {status!r}")
        if status == "recovered":
            recipe = entry.get("recipe") or {}
            if recipe.get("kind") not in RECIPE_KINDS:
                raise ValueError(
                    f"{task_id}: unsupported recipe kind {recipe.get('kind')!r}"
                )
        elif not entry.get("blockers"):
            raise ValueError(f"{task_id}: blocked entry carries no blockers")
    return contract


def contract_task_ids(contract: dict[str, Any]) -> tuple[str, ...]:
    return tuple(sorted(contract["tasks"]))


# ---------------------------------------------------------------------------
# Path/placeholder helpers
# ---------------------------------------------------------------------------


def expand_placeholders(
    value: str,
    *,
    legacy_root: Path,
    asset_root: Path,
    session_files: Path | None,
) -> str:
    replacements = {
        "${LEGACY_ROOT}": str(legacy_root),
        "${ASSET_ROOT}": str(asset_root),
        "${SESSION_FILES}": (
            str(session_files) if session_files is not None else "${SESSION_FILES}"
        ),
    }
    for token, replacement in replacements.items():
        value = value.replace(token, replacement)
    return value


def repos_root(legacy_root: Path) -> Path:
    return legacy_root / "repos"


def repo_checkout_dir(legacy_root: Path, repo: str) -> Path:
    return repos_root(legacy_root) / repo.replace("/", "__")


def asset_path(asset_root: Path, relative: str) -> Path:
    return (asset_root / relative).resolve()


# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------


def docker_available() -> bool:
    return shutil.which("docker") is not None


def docker_image_id(image: str) -> str | None:
    """Content identity of a locally loaded image, or ``None`` when absent.

    Used instead of a plain existence check so a retagged/rebuilt image at the
    same name still invalidates any previously-recorded row that referenced it.
    """
    if not docker_available():
        return None
    result = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def docker_volume_present(volume: str) -> bool:
    if not docker_available():
        return False
    result = subprocess.run(
        ["docker", "volume", "inspect", volume],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def docker_volume_identity(volume: str) -> dict[str, Any] | None:
    """Cheap, bounded identity of a named volume.

    Only the volume's own metadata (driver/mountpoint/creation time) is read;
    a multi-gigabyte Go module cache is never walked. Combined with the
    ``go.sum``-verified nature of the cache contents this is enough to notice
    a recreated/replaced volume without making every resume decision expensive.
    """
    if not docker_available():
        return None
    result = subprocess.run(
        [
            "docker",
            "volume",
            "inspect",
            "--format",
            "{{.Driver}}\t{{.Mountpoint}}\t{{.CreatedAt}}",
            volume,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if result.returncode != 0:
        return None
    parts = result.stdout.strip().split("\t")
    if len(parts) != 3:
        return None
    return {"driver": parts[0], "mountpoint": parts[1], "created_at": parts[2]}


def docker_file_sha256(image: str, path: str) -> str | None:
    """SHA-256 of a file *inside* an image, or ``None`` when unreadable."""
    if not docker_available():
        return None
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "/bin/sh",
            image,
            "-c",
            f"sha256sum {path}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if result.returncode != 0:
        return None
    token = result.stdout.strip().split(" ", 1)[0]
    return token or None


# ---------------------------------------------------------------------------
# Small generic helpers (mirrors of the reserve adapter's, kept local so the
# two adapters can evolve independently)
# ---------------------------------------------------------------------------


def sha256_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return None


def candidate_changed_lines(
    generation: dict[str, Any],
    changed_paths: list[str],
) -> int:
    """Changed-line count for a raw row.

    ``normalized_record()`` surfaces this field verbatim, and the staged
    generation already carries the authoritative per-candidate line count, so
    it is preferred; the number of changed paths is only a fallback for a
    generation row that never recorded one.
    """
    value = generation.get("changed_lines")
    if type(value) is int:
        return value
    return len(changed_paths)


def patch_changed_paths(patch_text: str) -> list[str]:
    return list(
        dict.fromkeys(
            right
            for _, right in re.findall(
                r"^diff --git a/(.+?) b/(.+?)$", patch_text, re.M
            )
        )
    )


def scope_is_safe(paths: list[str]) -> bool:
    for path in paths:
        if path.startswith("/") or ".." in Path(path).parts:
            return False
    return True


def apply_patch(workdir: Path, patch_path: Path) -> tuple[bool, str]:
    git_result = subprocess.run(
        ["git", "apply", "--whitespace=nowarn", "-p1", str(patch_path)],
        cwd=workdir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if git_result.returncode == 0:
        return True, git_result.stdout
    patch_result = subprocess.run(
        ["patch", "-p1", "--fuzz=0", "-i", str(patch_path)],
        cwd=workdir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if patch_result.returncode == 0:
        return True, patch_result.stdout
    return False, (
        f"git apply: {git_result.stdout}\npatch -p1: {patch_result.stdout}"
    )


def commit_present(repo_dir: Path, sha: str) -> bool:
    if not sha or not repo_dir.is_dir():
        return False
    result = subprocess.run(
        ["git", "-C", str(repo_dir), "cat-file", "-e", f"{sha}^{{commit}}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def git_blob_sha256(repo_dir: Path, sha: str, path: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repo_dir), "show", f"{sha}:{path}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        return None
    return hashlib.sha256(result.stdout).hexdigest()


def git_worktree_checkout(repo_source: Path, start_sha: str, dest: Path) -> None:
    subprocess.run(
        ["git", "-C", str(repo_source), "worktree", "remove", "--force", str(dest)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    shutil.rmtree(dest, ignore_errors=True)
    subprocess.run(
        ["git", "-C", str(repo_source), "worktree", "prune"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            "git", "-C", str(repo_source), "worktree", "add", "--force",
            "--detach", str(dest), start_sha,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"exact-start materialization failed: {result.stdout}")
    head = subprocess.check_output(
        ["git", "-C", str(dest), "rev-parse", "HEAD"], text=True
    ).strip()
    if head != start_sha:
        raise RuntimeError(
            f"materialized HEAD {head} is not the exact start {start_sha}"
        )


def remove_worktree(repo_source: Path, dest: Path) -> None:
    subprocess.run(
        ["git", "-C", str(repo_source), "worktree", "remove", "--force", str(dest)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    shutil.rmtree(dest, ignore_errors=True)
    subprocess.run(
        ["git", "-C", str(repo_source), "worktree", "prune"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def run_subprocess(
    command: list[str],
    *,
    cwd: Path | None,
    log_path: Path,
    env: dict[str, str] | None = None,
    timeout: int = 1800,
) -> tuple[int, float, str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    with open(log_path, "w") as stream:
        try:
            proc = subprocess.run(
                command,
                cwd=cwd,
                env=env,
                stdout=stream,
                stderr=subprocess.STDOUT,
                timeout=timeout,
            )
            return proc.returncode, round(time.time() - started, 3), ""
        except subprocess.TimeoutExpired:
            return 124, round(time.time() - started, 3), f"timeout after {timeout}s"
        except Exception as error:  # pragma: no cover - defensive
            return 125, round(time.time() - started, 3), repr(error)


# ---------------------------------------------------------------------------
# Result parsing
# ---------------------------------------------------------------------------


def go_test_results(text: str) -> dict[str, str]:
    """Map test name -> terminal action from a ``go test -json`` stream.

    Non-JSON lines (build failures, panics printed before the event stream)
    are ignored here; the caller still fails closed on them because a missing
    expected test can never be reported as ``pass``.
    """
    results: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = event.get("Test")
        action = event.get("Action")
        if name and action in {"pass", "fail", "skip"}:
            results[str(name)] = str(action)
    return results


def go_p2p_outcome(
    text: str,
    expected: list[str],
) -> tuple[bool, int, list[str]]:
    """PASS_TO_PASS judgement over a ``go test -json`` stream.

    Correctness requires *every* Oracle-recorded PASS_TO_PASS test to report a
    ``pass`` action; a test that vanished from the stream (build failure, or a
    candidate that renamed/removed it) counts as failed, never as absent.
    """
    results = go_test_results(text)
    failed = [name for name in expected if results.get(name) != "pass"]
    passed = len(expected) - len(failed)
    return (not failed and bool(expected)), passed, failed


def junit_outcome(path: Path) -> tuple[int, int, list[str]]:
    """Return ``(passed, total, failed_names)`` from a JUnit XML report."""
    tree = ET.parse(path)
    root = tree.getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    passed = 0
    total = 0
    failed: list[str] = []
    for suite in suites:
        for case in suite.iter("testcase"):
            total += 1
            problems = (
                list(case.iter("failure"))
                + list(case.iter("error"))
            )
            skipped = list(case.iter("skipped"))
            if problems:
                failed.append(
                    f"{case.get('classname') or ''}::{case.get('name') or ''}"
                )
            elif not skipped:
                passed += 1
    return passed, total, failed


# ---------------------------------------------------------------------------
# Blockers / asset state
# ---------------------------------------------------------------------------


def missing_asset_paths(
    entry: dict[str, Any],
    *,
    legacy_root: Path,
    asset_root: Path,
    session_files: Path | None,
) -> list[tuple[str, str, bool]]:
    """Return ``(kind, expanded value, present now)`` for declared losses."""
    resolved = []
    for asset in entry.get("missing_assets") or []:
        kind = str(asset.get("kind"))
        value = expand_placeholders(
            str(asset.get("value")),
            legacy_root=legacy_root,
            asset_root=asset_root,
            session_files=session_files,
        )
        if kind == "docker_image":
            present = docker_image_id(value) is not None
        elif kind == "docker_volume":
            present = docker_volume_present(value)
        else:
            present = (
                Path(value).exists()
                if "${SESSION_FILES}" not in value
                else False
            )
        resolved.append((kind, value, present))
    return resolved


def recovered_blockers(
    task_id: str,
    entry: dict[str, Any],
    oracle: dict[str, Any],
    task: dict[str, Any],
    *,
    legacy_root: Path,
    asset_root: Path,
) -> list[str]:
    recipe = entry["recipe"]
    blockers: list[str] = []

    if not docker_available():
        blockers.append("docker CLI is not available on this host")

    repo = entry["repo"]
    checkout = repo_checkout_dir(legacy_root, repo)
    start_sha = str(task.get("start_sha") or "")
    if not checkout.is_dir():
        blockers.append(f"missing retained repo checkout: {checkout}")
    elif not start_sha:
        blockers.append("task is missing start_sha")
    elif not commit_present(checkout, start_sha):
        blockers.append(
            f"retained repo checkout {checkout} does not contain the exact "
            f"task start_sha {start_sha} as a commit"
        )

    image = recipe.get("image")
    if not image:
        blockers.append("contract recipe is missing an image")
    else:
        live_id = docker_image_id(str(image))
        if live_id is None:
            blockers.append(
                f"missing Docker image: {image} (must be built/imported out "
                "of band; never fabricated)"
            )
        elif recipe.get("image_id") and live_id != recipe["image_id"]:
            blockers.append(
                f"Docker image {image} is {live_id}, not the recorded "
                f"{recipe['image_id']}"
            )

    for key in ("gomodcache_volume", "gobuildcache_volume"):
        volume = recipe.get(key)
        if volume and not docker_volume_present(str(volume)):
            archive = recipe.get("gomodcache_archive")
            hint = (
                f"; restore it with `restore-caches` from {archive}"
                if key == "gomodcache_volume" and archive
                else ""
            )
            blockers.append(f"missing Docker volume: {volume}{hint}")

    session_test = recipe.get("session_test")
    if session_test:
        source = asset_path(asset_root, str(session_test["asset"]))
        digest = sha256_file(source)
        if digest is None:
            blockers.append(f"missing session Oracle asset: {source}")
        elif digest != session_test.get("sha256"):
            blockers.append(
                f"session Oracle asset {source} has SHA-256 {digest}, not the "
                f"recorded {session_test.get('sha256')}"
            )

    kind = recipe["kind"]
    if kind == "go_docker_p2p":
        selector = oracle.get(str(recipe.get("selector_field")))
        if not selector:
            blockers.append(
                f"Oracle is missing {recipe.get('selector_field')!r}"
            )
        expected = oracle.get(str(recipe.get("pass_to_pass_field"))) or []
        if not expected:
            blockers.append(
                f"Oracle is missing {recipe.get('pass_to_pass_field')!r}"
            )
    elif kind == "go_docker_session_oracle":
        if not recipe.get("expected_tests"):
            blockers.append("contract recipe is missing expected_tests")
    elif kind == "python_image_pytest":
        anchor = recipe.get("workspace_anchor") or {}
        if not anchor:
            blockers.append("contract recipe is missing workspace_anchor")
        elif image and docker_image_id(str(image)) is not None:
            workdir = str(recipe.get("workdir") or ".")
            observed = docker_file_sha256(
                str(image), f"{workdir}/{anchor['path']}"
            )
            if observed is None:
                blockers.append(
                    f"cannot read in-image workspace anchor "
                    f"{workdir}/{anchor['path']} from {image}"
                )
            elif observed != anchor.get("sha256"):
                blockers.append(
                    f"in-image workspace anchor {anchor['path']} is {observed}, "
                    f"not the recorded {anchor.get('sha256')}"
                )
            elif checkout.is_dir() and start_sha:
                blob = git_blob_sha256(checkout, start_sha, str(anchor["path"]))
                if blob != anchor.get("sha256"):
                    blockers.append(
                        f"in-image workspace anchor {anchor['path']} does not "
                        f"match the repository blob at start_sha {start_sha}"
                    )
    return blockers


def task_blockers(
    task_id: str,
    entry: dict[str, Any],
    oracle: dict[str, Any],
    task: dict[str, Any],
    *,
    legacy_root: Path,
    asset_root: Path,
    session_files: Path | None,
) -> list[str]:
    if entry["status"] == "recovered":
        return recovered_blockers(
            task_id,
            entry,
            oracle,
            task,
            legacy_root=legacy_root,
            asset_root=asset_root,
        )
    blockers = []
    for blocker in entry.get("blockers") or []:
        blockers.append(f"{blocker.get('kind')}: {blocker.get('detail')}")
    declared = missing_asset_paths(
        entry,
        legacy_root=legacy_root,
        asset_root=asset_root,
        session_files=session_files,
    )
    if declared and all(present for _, _, present in declared):
        blockers.append(
            "blocker_possibly_resolved: every asset this blocker depends on "
            "is present again "
            f"({[value for _, value, _ in declared]}); re-engineer the "
            "contract entry instead of leaving the task unscored"
        )
    return blockers


def asset_state(
    task_id: str,
    entry: dict[str, Any],
    *,
    legacy_root: Path,
    asset_root: Path,
) -> dict[str, Any]:
    """Identity of the live assets a recovered recipe depends on.

    Folded into ``replay_contract_fingerprint`` so a replaced image, recreated
    cache volume, edited session Oracle asset or re-checked-out repository
    invalidates previously-recorded rows instead of being resumed past.
    """
    if entry["status"] != "recovered":
        return {"status": entry["status"]}
    recipe = entry["recipe"]
    state: dict[str, Any] = {
        "image_id": docker_image_id(str(recipe.get("image") or "")),
        "repo_head": None,
    }
    checkout = repo_checkout_dir(legacy_root, entry["repo"])
    if checkout.is_dir():
        state["repo_head"] = commit_present(checkout, entry["start_sha"])
    for key in ("gomodcache_volume", "gobuildcache_volume"):
        volume = recipe.get(key)
        state[key] = docker_volume_identity(str(volume)) if volume else None
    session_test = recipe.get("session_test")
    state["session_test"] = (
        sha256_file(asset_path(asset_root, str(session_test["asset"])))
        if session_test
        else None
    )
    return state


# ---------------------------------------------------------------------------
# Row provenance / staleness
# ---------------------------------------------------------------------------


def replay_contract_fingerprint(
    task_id: str,
    entry: dict[str, Any],
    legacy_root: Path,
    asset_root: Path,
    blockers: list[str],
) -> str:
    payload = {
        "adapter_contract_version": ADAPTER_CONTRACT_VERSION,
        "contract_entry": entry,
        "legacy_root": str(legacy_root),
        "asset_root": str(asset_root),
        "blockers": sorted(blockers),
        "asset_state": asset_state(
            task_id, entry, legacy_root=legacy_root, asset_root=asset_root
        ),
    }
    return BASE.canonical_hash(payload)


def row_provenance_fields(
    task: dict[str, Any],
    oracle: dict[str, Any],
    task_id: str,
    entry: dict[str, Any],
    generation: dict[str, Any],
    legacy_root: Path,
    asset_root: Path,
    blockers: list[str],
) -> dict[str, Any]:
    return {
        "task_fingerprint": BASE.task_fingerprint(task),
        "oracle_fingerprint": BASE.canonical_hash(oracle),
        "replay_contract_fingerprint": replay_contract_fingerprint(
            task_id, entry, legacy_root, asset_root, blockers
        ),
        "generation_apply_ok": generation.get("apply_ok"),
    }


def row_is_stale(
    existing_row: dict[str, Any],
    task: dict[str, Any],
    oracle: dict[str, Any],
    task_id: str,
    entry: dict[str, Any],
    generation: dict[str, Any],
    legacy_root: Path,
    asset_root: Path,
    blockers: list[str],
) -> bool:
    current = row_provenance_fields(
        task,
        oracle,
        task_id,
        entry,
        generation,
        legacy_root,
        asset_root,
        blockers,
    )
    if any(existing_row.get(field) != value for field, value in current.items()):
        return True
    return existing_row.get("patch_sha256") != generation.get("patch_sha256")


# ---------------------------------------------------------------------------
# Concurrency-safe raw-row persistence
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def lane_lock(legacy_dir: Path, lane: str):
    legacy_dir.mkdir(parents=True, exist_ok=True)
    lock_path = legacy_dir / f".{lane}.lock"
    with open(lock_path, "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def existing_raw_rows(
    legacy_dir: Path,
    lane: str,
) -> dict[tuple[str, str], dict[str, Any]]:
    path = legacy_dir / f"{lane}.jsonl"
    if not path.is_file():
        return {}
    return {(row["id"], row["model"]): row for row in BASE.read_jsonl(path)}


def append_raw_row(
    legacy_dir: Path,
    lane: str,
    row: dict[str, Any],
) -> dict[tuple[str, str], dict[str, Any]]:
    key = (row["id"], row["model"])
    with lane_lock(legacy_dir, lane):
        existing = existing_raw_rows(legacy_dir, lane)
        existing[key] = row
        ordered = sorted(
            existing.values(), key=lambda entry: (entry["id"], entry["model"])
        )
        BASE.atomic_jsonl(legacy_dir / f"{lane}.jsonl", ordered)
        return existing


def retract_raw_row(
    legacy_dir: Path,
    lane: str,
    task_id: str,
    model: str,
) -> bool:
    key = (task_id, model)
    with lane_lock(legacy_dir, lane):
        existing = existing_raw_rows(legacy_dir, lane)
        if key not in existing:
            return False
        del existing[key]
        ordered = sorted(
            existing.values(), key=lambda entry: (entry["id"], entry["model"])
        )
        BASE.atomic_jsonl(legacy_dir / f"{lane}.jsonl", ordered)
        return True


# ---------------------------------------------------------------------------
# Evidence handling
# ---------------------------------------------------------------------------


def model_component(model: str) -> str:
    """Collision-free filesystem component for a ``--model`` value.

    ``BASE.safe_component`` alone is a many-to-one slug, so two distinct valid
    model IDs could share (and concurrently clobber) one scratch/evidence
    subtree; a short digest of the exact model string makes it injective.
    """
    slug = BASE.safe_component(model)
    digest = hashlib.sha256(model.encode("utf-8")).hexdigest()[:16]
    return f"{slug}-{digest}"


def evidence_dir(output: Path, lane: str, model: str, task_id: str) -> Path:
    return (
        output
        / "legacy/evidence"
        / lane
        / model_component(model)
        / BASE.safe_component(task_id)
    )


def persist_evidence(
    output: Path,
    lane: str,
    model: str,
    task_id: str,
    scratch: Path,
) -> dict[str, str]:
    """Copy the small text logs out of a disposable scratch workspace.

    The scratch tree holds a full repo worktree (potentially gigabytes) and
    must never linger under ``--output`` where ``finalize``'s
    ``write_artifact_set`` would hash it as evidence. Only ``*.log``/``*.xml``
    reports are copied into a hashed ``legacy/evidence`` subtree; raw rows
    reference those output-relative paths, never an unhashed external path.
    """
    destination = evidence_dir(output, lane, model, task_id)
    preserved: dict[str, str] = {}
    if scratch.is_dir():
        destination.mkdir(parents=True, exist_ok=True)
        for pattern in ("*.log", "*.xml"):
            for artifact in sorted(scratch.glob(pattern)):
                target = destination / artifact.name
                shutil.copy2(artifact, target)
                preserved[artifact.name] = str(target.relative_to(output))
    shutil.rmtree(scratch, ignore_errors=True)
    return preserved


# ---------------------------------------------------------------------------
# Release / plan loading
# ---------------------------------------------------------------------------


def load_release_and_plan(
    tasks_path: Path,
    oracles_path: Path,
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    list[dict[str, Any]],
]:
    tasks, oracles, tasks_by_id, oracles_by_id = BASE.load_release(
        tasks_path, oracles_path
    )
    plan = BASE.build_task_plan(tasks, oracles_by_id)
    return tasks_by_id, oracles_by_id, plan


def select_scope(
    plan: list[dict[str, Any]],
    contract: dict[str, Any],
    task_ids: list[str] | None,
) -> list[dict[str, Any]]:
    known = set(contract_task_ids(contract))
    wanted = set(task_ids) if task_ids else set(known)
    unknown = wanted - known
    if unknown:
        raise ValueError(
            f"--task-id values outside the contract scope: {sorted(unknown)}"
        )
    rows = [row for row in plan if row["id"] in wanted]
    missing = wanted - {row["id"] for row in rows}
    if missing:
        raise ValueError(
            f"task IDs absent from --tasks/--oracles: {sorted(missing)}"
        )
    drift = [row["id"] for row in rows if row["source_kind"] != "published_legacy"]
    if drift:
        raise ValueError(
            "one or more contract task IDs already have a modern "
            f"lane_results_source and are no longer published_legacy: {drift}"
        )
    lane_drift = [
        row["id"]
        for row in rows
        if row["lane"] != contract["tasks"][row["id"]]["lane"]
    ]
    if lane_drift:
        raise ValueError(f"lane classification drifted for: {lane_drift}")
    sha_drift = [
        row["id"]
        for row in rows
        if row["start_sha"] != contract["tasks"][row["id"]]["start_sha"]
    ]
    if sha_drift:
        raise ValueError(f"start SHA drifted for: {sha_drift}")
    return rows


def staged_generation_path(output: Path, model: str) -> Path:
    return (
        output
        / "inputs/models"
        / BASE.safe_component(model)
        / "generation_results.normalized.jsonl"
    )


def load_staged_generation(
    output: Path,
    model: str,
    task_ids: set[str],
) -> dict[str, dict[str, Any]]:
    path = staged_generation_path(output, model)
    if not path.is_file():
        raise ValueError(
            f"staged generation results not found for {model!r}: {path}. "
            "Run `run_open_model_correctness.py prepare "
            "--allow-partial-modern` first."
        )
    by_id: dict[str, dict[str, Any]] = {}
    mislabeled: list[str] = []
    for row in BASE.read_jsonl(path):
        row_id = row.get("id")
        if row_id not in task_ids:
            continue
        # The staging directory is keyed by the many-to-one
        # ``safe_component`` slug, so every row's own embedded model must
        # match exactly or a colliding/mislabeled file could be consumed
        # under the wrong model name.
        if row.get("model") != model:
            mislabeled.append(f"{row_id}: embedded model {row.get('model')!r}")
            continue
        by_id[row_id] = row
    if mislabeled:
        raise ValueError(
            f"staged generation results at {path} contain rows whose embedded "
            f"model does not match the requested --model {model!r}: {mislabeled}"
        )
    missing = task_ids - set(by_id)
    if missing:
        raise ValueError(
            f"staged generation results for {model!r} are missing IDs: "
            f"{sorted(missing)}"
        )
    return by_id


# ---------------------------------------------------------------------------
# Replay recipes
# ---------------------------------------------------------------------------


def docker_run_command(
    recipe: dict[str, Any],
    *,
    mounts: list[str],
    env: dict[str, str],
    workdir: str,
    command: list[str],
    entrypoint: str | None = None,
) -> list[str]:
    argv = [
        "docker", "run", "--rm",
        "--network", str(recipe.get("network") or "none"),
    ]
    if entrypoint is not None:
        argv.extend(["--entrypoint", entrypoint])
    argv.extend(mounts)
    for key, value in sorted(env.items()):
        argv.extend(["-e", f"{key}={value}"])
    argv.extend(["-w", workdir])
    argv.append(str(recipe["image"]))
    argv.extend(command)
    return argv


def apply_error_row(
    task_id: str,
    model: str,
    lane: str,
    start_sha: str,
    generation: dict[str, Any],
    detail: str,
) -> dict[str, Any]:
    return {
        "id": task_id,
        "model": model,
        "lane": lane,
        "source_kind": "published_legacy",
        "start_sha": start_sha,
        "patch_sha256": generation.get("patch_sha256"),
        "status": "apply_error",
        "candidate_result": "apply_error",
        "correctness_ok": False,
        "patch_apply": False,
        "scope_ok": None,
        "error": detail[-4000:],
    }


def replay_go(
    task_id: str,
    model: str,
    task: dict[str, Any],
    oracle: dict[str, Any],
    entry: dict[str, Any],
    generation: dict[str, Any],
    legacy_root: Path,
    asset_root: Path,
    work_root: Path,
    output: Path,
    timeout: int | None,
) -> dict[str, Any]:
    """Replay a ``go_docker_p2p`` or ``go_docker_session_oracle`` recipe."""
    recipe = entry["recipe"]
    lane = entry["lane"]
    start_sha = str(task["start_sha"])
    repo_source = repo_checkout_dir(legacy_root, entry["repo"])
    scratch = work_root / lane / model_component(model) / task_id
    workspace = scratch / "workspace"
    git_worktree_checkout(repo_source, start_sha, workspace)
    result: dict[str, Any]
    try:
        session_test = recipe.get("session_test")
        if session_test:
            source = asset_path(asset_root, str(session_test["asset"]))
            destination = workspace / str(session_test["destination"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

        patch_path = Path(generation["patch_file"])
        ok, detail = apply_patch(workspace, patch_path)
        if not ok:
            scratch.mkdir(parents=True, exist_ok=True)
            (scratch / "apply-error.log").write_text(detail, encoding="utf-8")
            result = apply_error_row(
                task_id, model, lane, start_sha, generation, detail
            )
        else:
            patch_text = patch_path.read_text(errors="replace")
            changed = patch_changed_paths(patch_text)
            scope_ok = scope_is_safe(changed)

            mounts = ["-v", f"{workspace}:{recipe.get('workspace_mount', '/work')}"]
            if recipe.get("gomodcache_volume"):
                mounts += ["-v", f"{recipe['gomodcache_volume']}:/go/pkg/mod"]
            if recipe.get("gobuildcache_volume"):
                mounts += [
                    "-v",
                    f"{recipe['gobuildcache_volume']}:/root/.cache/go-build",
                ]
            module_dir = str(recipe.get("module_dir") or ".")
            workdir = str(recipe.get("workspace_mount", "/work"))
            if module_dir not in {"", "."}:
                workdir = f"{workdir}/{module_dir}"

            command = ["go", "test"]
            if recipe.get("build_tags"):
                command += ["-tags", str(recipe["build_tags"])]
            command += ["-count=1", "-json"]
            if recipe["kind"] == "go_docker_p2p":
                selector = str(oracle[str(recipe["selector_field"])])
                expected = [
                    str(name)
                    for name in oracle[str(recipe["pass_to_pass_field"])]
                ]
            else:
                selector = str(recipe["run_selector"])
                expected = [str(name) for name in recipe["expected_tests"]]
            command += ["-run", selector, str(recipe["test_target"])]

            docker_cmd = docker_run_command(
                recipe,
                mounts=mounts,
                env=dict(recipe.get("env") or {}),
                workdir=workdir,
                command=command,
            )
            log_path = scratch / "run.log"
            exit_code, duration, error = run_subprocess(
                docker_cmd,
                cwd=None,
                log_path=log_path,
                timeout=timeout or int(recipe.get("timeout_seconds") or 1800),
            )
            text = log_path.read_text(errors="replace") if log_path.is_file() else ""
            correctness_ok, passed, failed = go_p2p_outcome(text, expected)
            result = {
                "id": task_id,
                "model": model,
                "lane": lane,
                "source_kind": "published_legacy",
                "start_sha": start_sha,
                "patch_sha256": generation.get("patch_sha256"),
                "status": "pass" if correctness_ok else "fail",
                "candidate_result": "pass" if correctness_ok else "fail",
                "correctness_ok": correctness_ok,
                "patch_apply": True,
                "scope_ok": scope_ok,
                "changed_lines": candidate_changed_lines(generation, changed),
                "changed_paths": len(changed),
                "exit_code": exit_code,
                "duration_seconds": duration,
                "error": error,
                "pass_to_pass_total": len(expected),
                "pass_to_pass_passed": passed,
                "failed_tests": failed,
                "replay_recipe": recipe["kind"],
            }
    finally:
        remove_worktree(repo_source, workspace)
    evidence = persist_evidence(output, lane, model, task_id, scratch)
    if result["status"] == "apply_error":
        result["error_log"] = evidence.get("apply-error.log")
    else:
        result["log"] = evidence.get("run.log")
    return result


def replay_python_image(
    task_id: str,
    model: str,
    task: dict[str, Any],
    oracle: dict[str, Any],
    entry: dict[str, Any],
    generation: dict[str, Any],
    legacy_root: Path,
    asset_root: Path,
    work_root: Path,
    output: Path,
    timeout: int | None,
) -> dict[str, Any]:
    """Replay a ``python_image_pytest`` recipe.

    The workspace is the image's own baked start checkout, so a disposable
    container writable layer is the workspace: nothing is copied out of the
    image and the image itself is never mutated.
    """
    recipe = entry["recipe"]
    lane = entry["lane"]
    start_sha = str(task["start_sha"])
    scratch = work_root / lane / model_component(model) / task_id
    shutil.rmtree(scratch, ignore_errors=True)
    (scratch / "in").mkdir(parents=True, exist_ok=True)
    (scratch / "out").mkdir(parents=True, exist_ok=True)

    patch_path = Path(generation["patch_file"])
    shutil.copy2(patch_path, scratch / "in/candidate.patch")
    patch_text = patch_path.read_text(errors="replace")
    changed = patch_changed_paths(patch_text)
    scope_ok = scope_is_safe(changed)

    workdir = str(recipe.get("workdir") or "/work")
    test_path = str(recipe["test_path"])
    script = (
        "set -o pipefail; "
        f"cd {workdir} && "
        "patch -p1 --fuzz=0 -i /candidate/candidate.patch "
        "> /reports/apply.log 2>&1 || exit 90; "
        f"python -m pytest -q {test_path} "
        "--junitxml=/reports/candidate.xml > /reports/run.log 2>&1"
    )
    docker_cmd = docker_run_command(
        recipe,
        mounts=[
            "-v", f"{scratch / 'in'}:/candidate:ro",
            "-v", f"{scratch / 'out'}:/reports",
        ],
        env={},
        workdir=workdir,
        # The image's own entrypoint runs the historical triple-run harness;
        # a candidate replay must run exactly one scored command instead.
        entrypoint="/bin/bash",
        command=["-lc", script],
    )
    log_path = scratch / "docker.log"
    exit_code, duration, error = run_subprocess(
        docker_cmd,
        cwd=None,
        log_path=log_path,
        timeout=timeout or int(recipe.get("timeout_seconds") or 3600),
    )
    for name in ("apply.log", "run.log"):
        produced = scratch / "out" / name
        if produced.is_file():
            shutil.copy2(produced, scratch / name)
    report = scratch / "out/candidate.xml"
    if exit_code == 90:
        detail = (scratch / "out/apply.log").read_text(errors="replace") if (
            scratch / "out/apply.log"
        ).is_file() else "candidate patch did not apply"
        (scratch / "apply-error.log").write_text(detail, encoding="utf-8")
        result = apply_error_row(
            task_id, model, lane, start_sha, generation, detail
        )
        evidence = persist_evidence(output, lane, model, task_id, scratch)
        result["error_log"] = evidence.get("apply-error.log")
        return result

    expected = int(recipe.get("expected_passed") or 0)
    if report.is_file():
        shutil.copy2(report, scratch / "candidate.xml")
        passed, total, failed = junit_outcome(report)
    else:
        passed, total, failed = 0, 0, ["<no JUnit report produced>"]
    correctness_ok = (
        exit_code == 0 and not failed and bool(expected) and passed >= expected
    )
    result = {
        "id": task_id,
        "model": model,
        "lane": lane,
        "source_kind": "published_legacy",
        "start_sha": start_sha,
        "patch_sha256": generation.get("patch_sha256"),
        "status": "pass" if correctness_ok else "fail",
        "candidate_result": "pass" if correctness_ok else "fail",
        "correctness_ok": correctness_ok,
        "patch_apply": True,
        "scope_ok": scope_ok,
        "changed_lines": candidate_changed_lines(generation, changed),
        "changed_paths": len(changed),
        "exit_code": exit_code,
        "duration_seconds": duration,
        "error": error,
        "pass_to_pass_total": expected or total,
        "pass_to_pass_passed": passed,
        "failed_tests": failed,
        "replay_recipe": recipe["kind"],
    }
    evidence = persist_evidence(output, lane, model, task_id, scratch)
    result["log"] = evidence.get("run.log") or evidence.get("docker.log")
    result["report"] = evidence.get("candidate.xml")
    return result


REPLAY_FUNCTIONS = {
    "go_docker_p2p": replay_go,
    "go_docker_session_oracle": replay_go,
    "python_image_pytest": replay_python_image,
}


def missing_row(
    task_id: str,
    model: str,
    lane: str,
    start_sha: str,
    generation: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": task_id,
        "model": model,
        "lane": lane,
        "source_kind": "published_legacy",
        "start_sha": start_sha,
        "status": CANDIDATE_MISSING,
        "candidate_result": CANDIDATE_MISSING,
        "correctness_ok": None,
        "patch_apply": False,
        "patch_sha256": generation.get("patch_sha256"),
        "scope_ok": None,
    }


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def resolve_roots(args: argparse.Namespace) -> tuple[Path, Path, Path | None]:
    legacy_root = Path(args.legacy_root).expanduser().resolve()
    asset_root = Path(args.asset_root).expanduser().resolve()
    session_files = (
        Path(args.session_files).expanduser().resolve()
        if getattr(args, "session_files", None)
        else None
    )
    return legacy_root, asset_root, session_files


def cmd_preflight(args: argparse.Namespace) -> int:
    legacy_root, asset_root, session_files = resolve_roots(args)
    contract_path = Path(args.contract).resolve()
    contract = load_contract(contract_path)
    tasks_path = Path(args.tasks).resolve()
    oracles_path = Path(args.oracles).resolve()
    tasks_by_id, oracles_by_id, plan = load_release_and_plan(
        tasks_path, oracles_path
    )
    rows = select_scope(plan, contract, args.task_id or None)

    report_rows = []
    ready = 0
    for row in rows:
        task_id = row["id"]
        entry = contract["tasks"][task_id]
        blockers = task_blockers(
            task_id,
            entry,
            oracles_by_id[task_id],
            tasks_by_id[task_id],
            legacy_root=legacy_root,
            asset_root=asset_root,
            session_files=session_files,
        )
        status = "ready" if not blockers else "blocked"
        ready += status == "ready"
        report_rows.append(
            {
                "id": task_id,
                "repo": row["repo"],
                "lane": row["lane"],
                "start_sha": row["start_sha"],
                "contract_status": entry["status"],
                "recipe": (entry.get("recipe") or {}).get("kind"),
                "status": status,
                "blockers": blockers,
            }
        )
    report = {
        "schema_version": "reviewbench-legacy-gopy-scripted-preflight/v1",
        "created_at": BASE.utc_now(),
        "adapter_contract_version": ADAPTER_CONTRACT_VERSION,
        "contract": str(contract_path),
        "contract_sha256": BASE.sha256(contract_path),
        "legacy_root": str(legacy_root),
        "asset_root": str(asset_root),
        "tasks_sha256": BASE.sha256(tasks_path),
        "oracles_sha256": BASE.sha256(oracles_path),
        "contract_task_count": len(contract["tasks"]),
        "scoped_task_count": len(rows),
        "ready_task_count": ready,
        "blocked_task_count": len(rows) - ready,
        "tasks": sorted(report_rows, key=lambda entry: entry["id"]),
    }
    if args.output:
        output = Path(args.output).resolve()
        output.mkdir(parents=True, exist_ok=True)
        BASE.atomic_json(
            output / "legacy-gopy-scripted-preflight.json", report
        )
    print(json.dumps(report, indent=2))
    if report["blocked_task_count"] and not args.allow_blocked:
        return 1
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    legacy_root, asset_root, session_files = resolve_roots(args)
    contract = load_contract(Path(args.contract).resolve())
    tasks_path = Path(args.tasks).resolve()
    oracles_path = Path(args.oracles).resolve()
    output = Path(args.output).resolve()
    model = BASE.validate_model_id(args.model)

    tasks_by_id, oracles_by_id, plan = load_release_and_plan(
        tasks_path, oracles_path
    )
    rows = select_scope(plan, contract, args.task_id or None)
    generations = load_staged_generation(
        output, model, {row["id"] for row in rows}
    )

    legacy_dir = output / "legacy/correctness"
    work_root = (
        Path(args.work_root).resolve()
        if args.work_root
        else output / "legacy/work"
    )

    summary = {
        "ready": 0,
        "skipped_resume": 0,
        "stale_reran": 0,
        "candidate_missing": 0,
        "blocked": 0,
        "retracted_stale_rows": 0,
        "executed": 0,
        "errors": [],
    }
    by_lane = {
        lane: existing_raw_rows(legacy_dir, lane) for lane in SUPPORTED_LANES
    }

    for row in rows:
        task_id = row["id"]
        lane = row["lane"]
        entry = contract["tasks"][task_id]
        task = tasks_by_id[task_id]
        oracle = oracles_by_id[task_id]
        generation = generations[task_id]
        key = (task_id, model)

        # Blockers and live asset provenance are recomputed unconditionally,
        # before any resume decision, so a row recorded while assets were
        # present cannot survive a since-removed/changed asset merely by
        # matching an in-row fingerprint.
        blockers = task_blockers(
            task_id,
            entry,
            oracle,
            task,
            legacy_root=legacy_root,
            asset_root=asset_root,
            session_files=session_files,
        )

        existing_row = by_lane[lane].get(key)
        if existing_row is not None and not args.force:
            if not row_is_stale(
                existing_row,
                task,
                oracle,
                task_id,
                entry,
                generation,
                legacy_root,
                asset_root,
                blockers,
            ):
                summary["skipped_resume"] += 1
                continue
            summary["stale_reran"] += 1

        provenance = row_provenance_fields(
            task,
            oracle,
            task_id,
            entry,
            generation,
            legacy_root,
            asset_root,
            blockers,
        )

        if generation.get("apply_ok") is not True:
            raw_row = missing_row(
                task_id, model, lane, row["start_sha"], generation
            )
            raw_row.update(provenance)
            by_lane[lane] = append_raw_row(legacy_dir, lane, raw_row)
            summary["candidate_missing"] += 1
            continue

        if blockers:
            if existing_row is not None:
                # An old row must not survive a since-missing/changed asset:
                # retract it from disk so `finalize` can never read it as
                # still valid.
                retract_raw_row(legacy_dir, lane, task_id, model)
                by_lane[lane] = existing_raw_rows(legacy_dir, lane)
                summary["retracted_stale_rows"] += 1
            summary["blocked"] += 1
            summary["errors"].append({"id": task_id, "blockers": blockers})
            continue

        if args.dry_run:
            summary["ready"] += 1
            continue

        replay = REPLAY_FUNCTIONS[entry["recipe"]["kind"]]
        try:
            raw_row = replay(
                task_id,
                model,
                task,
                oracle,
                entry,
                generation,
                legacy_root,
                asset_root,
                work_root,
                output,
                args.timeout,
            )
        except LookupError as error:
            # A missing key/index means a code or data-contract bug that would
            # never succeed on retry; it must fail the run loudly instead of
            # being mislabeled as a transient infra failure.
            raise RuntimeError(
                f"contract bug replaying {task_id}: {error!r} (a required "
                "field was missing; this is not a transient infra failure and "
                "will not be recorded as infra_retry)"
            ) from error
        except Exception as error:  # pragma: no cover - defensive
            raw_row = {
                "id": task_id,
                "model": model,
                "lane": lane,
                "source_kind": "published_legacy",
                "start_sha": row["start_sha"],
                "patch_sha256": generation.get("patch_sha256"),
                "status": INFRA_RETRY,
                "candidate_result": INFRA_RETRY,
                "correctness_ok": None,
                "patch_apply": None,
                "scope_ok": None,
                "error": repr(error),
            }
        raw_row.update(provenance)
        by_lane[lane] = append_raw_row(legacy_dir, lane, raw_row)
        summary["executed"] += 1

    print(json.dumps(summary, indent=2))
    return 0 if not summary["errors"] or args.allow_blocked else 1


def cmd_restore_caches(args: argparse.Namespace) -> int:
    """Recreate a lost Go module cache volume from its packaged archive.

    The archive is verified against ``SHA256SUMS`` in ``--asset-root`` before
    anything is created, and an already-present volume is never overwritten:
    reconstruction is only ever performed from pinned, hash-verified content,
    never from the network.
    """
    _, asset_root, _ = resolve_roots(args)
    contract = load_contract(Path(args.contract).resolve())
    sums_path = asset_root / "SHA256SUMS"
    if not sums_path.is_file():
        raise ValueError(f"missing asset checksum file: {sums_path}")
    expected = {}
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, _, relative = line.partition("  ")
        expected[relative.strip()] = digest.strip()

    actions = []
    for task_id, entry in sorted(contract["tasks"].items()):
        recipe = entry.get("recipe") or {}
        volume = recipe.get("gomodcache_volume")
        archive = recipe.get("gomodcache_archive")
        if not volume or not archive:
            continue
        if args.task_id and task_id not in args.task_id:
            continue
        if docker_volume_present(str(volume)):
            actions.append({"volume": volume, "action": "present"})
            continue
        archive_path = asset_path(asset_root, str(archive))
        digest = sha256_file(archive_path)
        if digest is None:
            raise ValueError(f"missing packaged cache archive: {archive_path}")
        if expected.get(str(archive)) not in {None, digest}:
            raise ValueError(
                f"packaged cache archive {archive_path} has SHA-256 {digest}, "
                f"not the recorded {expected.get(str(archive))}"
            )
        if args.dry_run:
            actions.append({"volume": volume, "action": "would_restore"})
            continue
        subprocess.run(
            ["docker", "volume", "create", str(volume)],
            stdout=subprocess.DEVNULL,
            check=True,
        )
        subprocess.run(
            [
                "docker", "run", "--rm", "--network", "none",
                "-v", f"{volume}:/dst",
                "-v", f"{archive_path}:/archive.tar.gz:ro",
                "alpine", "sh", "-c",
                "cd /dst && tar -xzf /archive.tar.gz",
            ],
            check=True,
        )
        actions.append({"volume": volume, "action": "restored"})
    print(json.dumps({"actions": actions}, indent=2))
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--tasks", required=True)
    parser.add_argument("--oracles", required=True)
    parser.add_argument("--legacy-root", required=True)
    parser.add_argument(
        "--asset-root",
        required=True,
        help="Recovered asset root holding session Oracle files and packaged "
        "caches (see docs/OPEN_MODEL_EVALUATION.md)",
    )
    parser.add_argument("--contract", default=str(DEFAULT_CONTRACT))
    parser.add_argument(
        "--session-files",
        help="Historical ${REVIEWBENCH_SESSION_FILES} root; used only to "
        "re-verify that a blocked task's lost assets are still lost",
    )
    parser.add_argument(
        "--task-id",
        action="append",
        help="Restrict to a subset of the contract's task IDs (repeatable)",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    preflight = sub.add_parser(
        "preflight",
        help="Fail-closed check of the 19 gopy/scripted legacy contracts",
    )
    add_common_args(preflight)
    preflight.add_argument("--output")
    preflight.add_argument("--allow-blocked", action="store_true")
    preflight.set_defaults(func=cmd_preflight)

    replay = sub.add_parser(
        "replay",
        help="Replay ready tasks for a staged model into "
        "<output>/legacy/correctness/<lane>.jsonl",
    )
    add_common_args(replay)
    replay.add_argument("--output", required=True)
    replay.add_argument("--model", required=True)
    replay.add_argument(
        "--work-root",
        help="Disposable scratch root (default: <output>/legacy/work). May "
        "point anywhere: only small text/XML reports are copied into the "
        "hashed <output>/legacy/evidence subtree.",
    )
    replay.add_argument("--timeout", type=int, default=None)
    replay.add_argument("--force", action="store_true")
    replay.add_argument("--dry-run", action="store_true")
    replay.add_argument("--allow-blocked", action="store_true")
    replay.set_defaults(func=cmd_replay)

    restore = sub.add_parser(
        "restore-caches",
        help="Recreate missing Go module cache volumes from the packaged, "
        "hash-verified archives under --asset-root",
    )
    restore.add_argument("--asset-root", required=True)
    restore.add_argument("--contract", default=str(DEFAULT_CONTRACT))
    restore.add_argument("--legacy-root", default=".")
    restore.add_argument("--task-id", action="append")
    restore.add_argument("--dry-run", action="store_true")
    restore.set_defaults(func=cmd_restore_caches)

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return args.func(args)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
