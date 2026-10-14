"""Bind automatic Go selection to named original runs, never to a test count."""
from __future__ import annotations

import json
from pathlib import Path
import re

from reviewbench_maintainability.agentic.contracts import ContractError, file_hash
from reviewbench_maintainability.agentic.snapshots import git
from .go_contracts import GoContract, command_list, is_go, module_directory, test_flags, working_directory

SCHEMA = "reviewbench-go-original-runs/v1"
VERBOSE_RESULT = re.compile(r"^\s*--- (PASS|FAIL|SKIP): (\S+)", re.M)


def pinned_file(ref: dict, role: str) -> tuple[Path, dict]:
    if not isinstance(ref, dict):
        raise ContractError("go_original_evidence_reference_missing: " + role)
    path = Path(ref.get("path", ""))
    digest = ref.get("sha256")
    if (not path.is_absolute() or path.is_symlink() or not path.is_file()
            or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
        raise ContractError("go_original_evidence_reference_invalid: " + role)
    if file_hash(path) != digest:
        raise ContractError("go_original_evidence_digest_differs: " + role)
    return path, {"path": str(path), "sha256": digest, "role": role}


def load_original_runs(ctx, task: dict, oracle: dict, directory: Path) -> GoContract:
    references = ctx.config.get("migration", {}).get("go_original_runs", {})
    ref = references.get(task["id"])
    if not ref:
        count = oracle.get("pass_to_pass", "unknown")
        raise ContractError(
            f"go_automatic_original_runs_missing: retained count={count}; "
            "need original harness and three start/three golden named logs "
            "via migration.go_original_runs; no synthetic session-spec"
        )
    path, record_ref = pinned_file(ref, "original-go-runs")
    record = json.loads(path.read_text())
    if record.get("schema_version") != SCHEMA:
        raise ContractError("go_original_run_schema_differs")
    for key in ("id", "repo", "start_sha"):
        if record.get(key) != task[key]:
            raise ContractError("go_original_run_task_binding_differs: " + key)
    golden = oracle.get("golden_patch_sha256") or oracle.get("source_only_golden", {}).get("patch_sha256")
    if not golden or record.get("golden_patch_sha256") != golden:
        raise ContractError("go_original_run_golden_binding_differs")
    kind = record.get("evidence_kind")
    if kind not in {"retained-original-runs", "original-harness-replay"}:
        raise ContractError("go_original_run_evidence_kind_missing")
    if kind == "original-harness-replay":
        expected_engine = ctx.config.get("migration", {}).get("verification_engine_fingerprint")
        if not expected_engine or record.get("engine_fingerprint") != expected_engine:
            raise ContractError("go_original_replay_engine_binding_differs")
    provenance = [record_ref]
    harness = record.get("harness", [])
    if not harness:
        raise ContractError("go_original_harness_evidence_missing")
    for reference in harness:
        _, evidence = pinned_file(reference, "original-automatic-harness")
        provenance.append(evidence)
    command = command_list(record.get("command"))
    if len(command) < 2 or not is_go(command[0]) or command[1] != "test":
        raise ContractError("go_original_public_command_requires_go_test")
    if "-json" in command or "-v" not in command or "-count=1" not in command:
        raise ContractError("go_original_public_command_not_verbose_uncached")
    if any(arg in {"-run", "-list", "-args", "--"} or arg.startswith(("-run=", "-list=")) for arg in command):
        raise ContractError("go_original_discovery_command_has_a_selector")
    cwd = working_directory(record.get("cwd", "{repo}"))
    module_cwd = module_directory(ctx, task, cwd)
    if module_cwd != cwd:
        raise ContractError("go_original_public_module_context_differs")
    flags, targets = test_flags(command[2:])
    if len(targets) != 1 or not targets[0].startswith("./") or "..." in targets[0]:
        raise ContractError("go_original_public_single_package_required")
    relative_package = targets[0][2:].strip("/")
    from reviewbench_maintainability.agentic.contracts import repo_path
    repo_path(relative_package, allow_root=True)
    text = git(ctx.repo(task), "show", task["start_sha"] + ":" +
               (cwd + "/" if cwd else "") + "go.mod").decode()
    module = re.search(r'^module\s+"?([^\s"]+)"?', text, re.M)
    if not module:
        raise ContractError("go_original_module_name_missing")
    package = module[1] + ("/" + relative_package if relative_package else "")
    sets = []
    for phase in ("start", "golden"):
        rows = record.get("runs", {}).get(phase, [])
        if len(rows) != 3 or sorted(r.get("run", 0) for r in rows) != [1, 2, 3]:
            raise ContractError("go_original_named_runs_incomplete: " + phase)
        for row in rows:
            if row.get("command") != command or row.get("cwd") != record.get("cwd", "{repo}"):
                raise ContractError("go_original_run_command_differs: " + phase)
            if type(row.get("exit_code")) is not int:
                raise ContractError("go_original_run_exit_status_missing")
            log, log_ref = pinned_file(row.get("log"), "original-" + phase + "-run")
            # This is the original trackA_go parser's last-result-per-name
            # rule, not a new selection based on a candidate being graded.
            parsed = {match[2]: match[1] for match in VERBOSE_RESULT.finditer(log.read_text())}
            if not parsed:
                raise ContractError("go_original_named_test_results_missing")
            sets.append({name for name, status in parsed.items() if status == "PASS"})
            provenance.append(log_ref)
    selected = sorted(set.intersection(*sets))
    if not selected or len(selected) != oracle.get("pass_to_pass"):
        raise ContractError("go_original_p2p_count_differs: selected=" + str(len(selected)))
    expected = [package + "::" + name for name in selected]
    if oracle.get("pass_to_pass_tests") not in (None, expected):
        raise ContractError("go_original_p2p_identities_differ")
    version = record.get("go_version", "")
    if (not re.fullmatch(r"1\.\d+\.\d+", version)
            or record.get("go_version_output") != f"go version go{version} linux/amd64"):
        raise ContractError("go_original_compiler_observation_missing")
    spec = {"command": command, "cwd": record.get("cwd", "{repo}"),
            "timeout": record.get("timeout", 1800), "environment": record.get("environment", {}),
            "environment_description": {"toolchain": "Original observed Go " + version}}
    return GoContract(
        "automatic-public-tests", spec, command, cwd, module_cwd,
        expected, None, targets[0], build_flags=flags, provenance=provenance,
    )
