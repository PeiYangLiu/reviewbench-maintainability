"""SWE-bench-style evaluator for ReviewBench-Maintainability tasks."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import fnmatch
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any

from .jsonl import read_jsonl
from .static_metrics import compare_paths


AUTO_STATIC_CATEGORIES = {"complexity", "duplication", "dead_code"}
DEFAULT_TIMEOUT_SECONDS = 600
MAX_CAPTURED_OUTPUT = 20_000


@dataclass(frozen=True)
class CommandSpec:
    name: str
    command: str
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    required: bool = True


@dataclass(frozen=True)
class EvaluationOracle:
    setup_commands: tuple[CommandSpec, ...] = ()
    test_commands: tuple[CommandSpec, ...] = ()
    allowed_files: tuple[str, ...] = ()
    denied_files: tuple[str, ...] = ()
    max_changed_files: int | None = None
    max_changed_lines: int | None = None
    require_locality_touched: bool = True
    maintainability_paths: tuple[str, ...] = ()
    require_static_evidence: bool | None = None


@dataclass(frozen=True)
class EvaluationOptions:
    work_dir: Path
    source_repo: Path | None = None
    keep_worktree: bool = False
    git_binary: str = "git"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    task_source = parser.add_mutually_exclusive_group(required=True)
    task_source.add_argument("--task", type=Path, help="Single task JSON object.")
    task_source.add_argument("--tasks", type=Path, help="Task JSONL file.")
    patch_source = parser.add_mutually_exclusive_group(required=True)
    patch_source.add_argument("--patch", type=Path, help="Single model patch file.")
    patch_source.add_argument("--patch-dir", type=Path, help="Directory containing <task_id>.patch or <task_id>.diff files.")
    parser.add_argument("--oracle-config", type=Path, help="JSON config with per-task or per-repo commands and scope rules.")
    parser.add_argument("--source-repo", type=Path, help="Local git repo to clone instead of https://github.com/<repo>.git.")
    parser.add_argument("--work-dir", type=Path, required=True, help="Directory for temporary worktrees and snapshots.")
    parser.add_argument("--out", type=Path, help="Output JSON for single task or JSONL for batch mode.")
    parser.add_argument("--keep-worktree", action="store_true", help="Keep evaluated worktrees for debugging.")
    parser.add_argument("--git", default="git", help="Git binary.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    tasks = load_tasks(args.task, args.tasks)
    config = load_oracle_config(args.oracle_config)
    options = EvaluationOptions(
        work_dir=args.work_dir,
        source_repo=args.source_repo,
        keep_worktree=args.keep_worktree,
        git_binary=args.git,
    )

    results = []
    for task in tasks:
        patch_path = resolve_patch_path(task, args.patch, args.patch_dir)
        oracle = oracle_for_task(task, config)
        results.append(evaluate_task(task, patch_path, oracle, options))

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        if args.tasks:
            args.out.write_text(
                "\n".join(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in results) + "\n",
                encoding="utf-8",
            )
        else:
            args.out.write_text(
                json.dumps(results[0], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
    else:
        if args.tasks:
            for result in results:
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:
            print(json.dumps(results[0], ensure_ascii=False, indent=2, sort_keys=True))

    return 0 if all(result["passed"] for result in results) else 1


def load_tasks(task_path: Path | None, tasks_path: Path | None) -> list[dict[str, Any]]:
    if task_path:
        return [json.loads(task_path.read_text(encoding="utf-8"))]
    if not tasks_path:
        raise ValueError("Either --task or --tasks is required")
    return list(read_jsonl(tasks_path))


def load_oracle_config(path: Path | None) -> dict[str, Any]:
    if not path:
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_patch_path(task: dict[str, Any], patch: Path | None, patch_dir: Path | None) -> Path:
    if patch:
        return patch
    if not patch_dir:
        raise ValueError("Either --patch or --patch-dir is required")
    task_id = str(task.get("id") or "")
    for suffix in (".patch", ".diff"):
        candidate = patch_dir / f"{task_id}{suffix}"
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"No patch found for task {task_id!r} in {patch_dir}")


def evaluate_task(
    task: dict[str, Any],
    patch_path: Path,
    oracle: EvaluationOracle,
    options: EvaluationOptions,
) -> dict[str, Any]:
    task_id = str(task.get("id") or "unknown-task")
    run_root = options.work_dir / safe_path_component(task_id)
    worktree = run_root / "repo"
    before_snapshot = run_root / "before"
    result: dict[str, Any] = {
        "task_id": task_id,
        "repo": task.get("repo"),
        "patch_path": str(patch_path),
        "passed": False,
        "score": 0.0,
        "gates": {},
    }

    if run_root.exists():
        shutil.rmtree(run_root)
    run_root.mkdir(parents=True, exist_ok=True)

    try:
        checkout_gate = prepare_worktree(task, worktree, options)
        result["gates"]["checkout"] = checkout_gate
        if not checkout_gate["passed"]:
            return finalize_result(result)

        metric_paths = maintainability_paths(task, oracle)
        snapshot_paths(worktree, before_snapshot, metric_paths)

        patch_gate = apply_patch(worktree, patch_path, options.git_binary)
        result["gates"]["patch_apply"] = patch_gate
        if not patch_gate["passed"]:
            return finalize_result(result)

        scope_gate = check_scope(worktree, task, oracle, options.git_binary)
        result["gates"]["scope"] = scope_gate

        setup_gate = run_command_group(worktree, "setup", oracle.setup_commands)
        result["gates"]["setup"] = setup_gate

        if setup_gate["passed"]:
            correctness_gate = run_correctness_gate(worktree, oracle.test_commands)
        else:
            correctness_gate = {
                "passed": False,
                "status": "skipped_setup_failed",
                "commands": [],
            }
        result["gates"]["correctness"] = correctness_gate

        if correctness_gate["passed"]:
            result["gates"]["maintainability"] = run_maintainability_gate(
                task=task,
                oracle=oracle,
                before_dir=before_snapshot,
                after_dir=worktree,
                paths=metric_paths,
            )
        else:
            result["gates"]["maintainability"] = {
                "passed": False,
                "status": "skipped_correctness_failed",
            }
        return finalize_result(result)
    finally:
        if not options.keep_worktree and run_root.exists():
            shutil.rmtree(run_root)


def prepare_worktree(
    task: dict[str, Any],
    worktree: Path,
    options: EvaluationOptions,
) -> dict[str, Any]:
    repo = str(task.get("repo") or "")
    start_sha = str(task.get("start_sha") or "")
    if not start_sha:
        return {"passed": False, "status": "missing_start_sha"}

    clone_source = str(options.source_repo) if options.source_repo else f"https://github.com/{repo}.git"
    clone = run_process([options.git_binary, "clone", "--quiet", clone_source, str(worktree)], cwd=None)
    if not clone["passed"]:
        return {"passed": False, "status": "clone_failed", "command": clone}

    checkout = run_process([options.git_binary, "checkout", "--quiet", start_sha], cwd=worktree)
    if not checkout["passed"]:
        return {"passed": False, "status": "checkout_failed", "command": checkout}
    return {"passed": True, "status": "passed", "source": clone_source, "start_sha": start_sha}


def apply_patch(worktree: Path, patch_path: Path, git_binary: str) -> dict[str, Any]:
    if not patch_path.exists():
        return {"passed": False, "status": "missing_patch", "patch_path": str(patch_path)}
    command = run_process(
        [git_binary, "apply", "--whitespace=nowarn", str(patch_path)],
        cwd=worktree,
    )
    if not command["passed"]:
        return {"passed": False, "status": "git_apply_failed", "command": command}
    return {"passed": True, "status": "passed"}


def run_correctness_gate(worktree: Path, commands: tuple[CommandSpec, ...]) -> dict[str, Any]:
    if not commands:
        return {
            "passed": False,
            "status": "missing_correctness_commands",
            "commands": [],
        }
    return run_command_group(worktree, "correctness", commands)


def run_command_group(worktree: Path, name: str, commands: tuple[CommandSpec, ...]) -> dict[str, Any]:
    results = [run_shell_command(worktree, spec) for spec in commands]
    required_results = [item for item in results if item["required"]]
    passed = all(item["passed"] for item in required_results)
    return {
        "passed": passed,
        "status": "passed" if passed else f"{name}_failed",
        "commands": results,
    }


def run_shell_command(worktree: Path, spec: CommandSpec) -> dict[str, Any]:
    start = time.monotonic()
    try:
        completed = subprocess.run(
            spec.command,
            cwd=worktree,
            shell=True,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=spec.timeout_seconds,
        )
        elapsed = round(time.monotonic() - start, 3)
        return {
            "name": spec.name,
            "command": spec.command,
            "required": spec.required,
            "timeout_seconds": spec.timeout_seconds,
            "exit_code": completed.returncode,
            "passed": completed.returncode == 0,
            "duration_seconds": elapsed,
            "stdout": truncate_output(completed.stdout),
            "stderr": truncate_output(completed.stderr),
        }
    except subprocess.TimeoutExpired as exc:
        elapsed = round(time.monotonic() - start, 3)
        return {
            "name": spec.name,
            "command": spec.command,
            "required": spec.required,
            "timeout_seconds": spec.timeout_seconds,
            "exit_code": None,
            "passed": False,
            "duration_seconds": elapsed,
            "stdout": truncate_output(exc.stdout or ""),
            "stderr": truncate_output(exc.stderr or ""),
            "timed_out": True,
        }


def check_scope(
    worktree: Path,
    task: dict[str, Any],
    oracle: EvaluationOracle,
    git_binary: str,
) -> dict[str, Any]:
    changed_files = git_changed_files(worktree, git_binary)
    changed_lines = git_changed_lines(worktree, git_binary)
    locality_path = str((task.get("locality") or {}).get("path") or "")
    checks: dict[str, Any] = {}

    if oracle.allowed_files:
        disallowed = [
            path
            for path in changed_files
            if not any(fnmatch.fnmatch(path, pattern) for pattern in oracle.allowed_files)
        ]
        checks["allowed_files"] = {
            "passed": not disallowed,
            "patterns": list(oracle.allowed_files),
            "disallowed": disallowed,
        }
    if oracle.denied_files:
        denied = [
            path
            for path in changed_files
            if any(fnmatch.fnmatch(path, pattern) for pattern in oracle.denied_files)
        ]
        checks["denied_files"] = {
            "passed": not denied,
            "patterns": list(oracle.denied_files),
            "denied": denied,
        }
    if oracle.max_changed_files is not None:
        checks["max_changed_files"] = {
            "passed": len(changed_files) <= oracle.max_changed_files,
            "limit": oracle.max_changed_files,
            "actual": len(changed_files),
        }
    if oracle.max_changed_lines is not None:
        total_changed = changed_lines["additions"] + changed_lines["deletions"]
        checks["max_changed_lines"] = {
            "passed": total_changed <= oracle.max_changed_lines,
            "limit": oracle.max_changed_lines,
            "actual": total_changed,
        }
    if oracle.require_locality_touched:
        checks["locality_touched"] = {
            "passed": bool(locality_path and locality_path in changed_files),
            "path": locality_path,
        }

    passed = all(item["passed"] for item in checks.values())
    return {
        "passed": passed,
        "status": "passed" if passed else "scope_failed",
        "changed_files": changed_files,
        "changed_lines": changed_lines,
        "checks": checks,
    }


def run_maintainability_gate(
    task: dict[str, Any],
    oracle: EvaluationOracle,
    before_dir: Path,
    after_dir: Path,
    paths: list[str],
) -> dict[str, Any]:
    category = str(task.get("category") or "")
    gate_type = str(task.get("gate_type") or "")
    if not paths:
        return {"passed": False, "status": "missing_maintainability_paths"}

    metrics = compare_paths(before_dir, after_dir, paths, category)
    if category in AUTO_STATIC_CATEGORIES or gate_type == "auto_static":
        require_static = oracle.require_static_evidence
        if require_static is None:
            require_static = True
        has_evidence = metrics["summary"]["has_any_positive_static_evidence"]
        return {
            "passed": bool(has_evidence) if require_static else True,
            "status": "passed" if has_evidence or not require_static else "static_evidence_missing",
            "gate_type": "auto_static",
            "static_metrics": metrics,
        }
    return {
        "passed": False,
        "status": "manual_or_llm_quality_gate_required",
        "gate_type": gate_type or "manual_or_llm_calibrated",
        "static_metrics": metrics,
    }


def finalize_result(result: dict[str, Any]) -> dict[str, Any]:
    gates = result["gates"]
    required_gate_names = ("checkout", "patch_apply", "setup", "correctness", "scope", "maintainability")
    passed = all(gates.get(name, {}).get("passed") is True for name in required_gate_names)
    result["passed"] = passed
    result["score"] = 1.0 if passed else 0.0
    if passed:
        result["failure_reason"] = None
    else:
        result["failure_reason"] = first_failure_reason(gates, required_gate_names)
    return result


def first_failure_reason(gates: dict[str, Any], gate_names: tuple[str, ...]) -> str:
    for name in gate_names:
        gate = gates.get(name)
        if gate is None:
            return f"{name}_not_run"
        if gate.get("passed") is not True:
            status = gate.get("status") or "failed"
            return f"{name}:{status}"
    return "unknown"


def oracle_for_task(task: dict[str, Any], config: dict[str, Any]) -> EvaluationOracle:
    merged: dict[str, Any] = {}
    merge_dict(merged, config.get("defaults", {}))
    repo = str(task.get("repo") or "")
    task_id = str(task.get("id") or "")
    merge_dict(merged, (config.get("repos") or {}).get(repo, {}))
    merge_dict(merged, inline_oracle_config(task.get("oracles", {})))
    merge_dict(merged, (config.get("tasks") or {}).get(task_id, {}))

    default_timeout = int(merged.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    setup = normalize_commands(
        merged.get("setup_commands", merged.get("setup", [])),
        default_prefix="setup",
        default_timeout=default_timeout,
    )
    tests = normalize_commands(
        merged.get("test_commands", merged.get("checks", merged.get("commands", []))),
        default_prefix="test",
        default_timeout=default_timeout,
    )
    return EvaluationOracle(
        setup_commands=tuple(setup),
        test_commands=tuple(tests),
        allowed_files=tuple(str(item) for item in merged.get("allowed_files", [])),
        denied_files=tuple(str(item) for item in merged.get("denied_files", [])),
        max_changed_files=parse_optional_int(merged.get("max_changed_files")),
        max_changed_lines=parse_optional_int(merged.get("max_changed_lines")),
        require_locality_touched=parse_bool(merged.get("require_locality_touched", True)),
        maintainability_paths=tuple(str(item) for item in merged.get("maintainability_paths", [])),
        require_static_evidence=parse_optional_bool(merged.get("require_static_evidence")),
    )


def inline_oracle_config(oracles: Any) -> dict[str, Any]:
    if not isinstance(oracles, dict):
        return {}
    result: dict[str, Any] = {}
    for source_key, target_key in (
        ("setup_commands", "setup_commands"),
        ("test_commands", "test_commands"),
        ("check_commands", "test_commands"),
        ("commands", "test_commands"),
        ("allowed_files", "allowed_files"),
        ("denied_files", "denied_files"),
        ("max_changed_files", "max_changed_files"),
        ("max_changed_lines", "max_changed_lines"),
        ("require_locality_touched", "require_locality_touched"),
        ("maintainability_paths", "maintainability_paths"),
        ("require_static_evidence", "require_static_evidence"),
    ):
        if source_key in oracles:
            result[target_key] = oracles[source_key]
    return result


def normalize_commands(value: Any, default_prefix: str, default_timeout: int) -> list[CommandSpec]:
    if not value:
        return []
    raw_items = value if isinstance(value, list) else [value]
    commands = []
    for index, raw in enumerate(raw_items, start=1):
        if isinstance(raw, str):
            commands.append(
                CommandSpec(
                    name=f"{default_prefix}_{index}",
                    command=raw,
                    timeout_seconds=default_timeout,
                    required=True,
                )
            )
        elif isinstance(raw, dict):
            command = raw.get("command")
            if not command:
                continue
            commands.append(
                CommandSpec(
                    name=str(raw.get("name") or f"{default_prefix}_{index}"),
                    command=str(command),
                    timeout_seconds=int(raw.get("timeout_seconds", default_timeout)),
                    required=parse_bool(raw.get("required", True)),
                )
            )
        else:
            raise ValueError(f"Unsupported command spec: {raw!r}")
    return commands


def merge_dict(target: dict[str, Any], source: Any) -> None:
    if isinstance(source, dict):
        target.update(source)


def maintainability_paths(task: dict[str, Any], oracle: EvaluationOracle) -> list[str]:
    if oracle.maintainability_paths:
        return list(oracle.maintainability_paths)
    locality_path = (task.get("locality") or {}).get("path")
    if locality_path:
        return [str(locality_path)]
    return [str(path) for path in task.get("changed_files", []) if path]


def snapshot_paths(source_root: Path, snapshot_root: Path, paths: list[str]) -> None:
    snapshot_root.mkdir(parents=True, exist_ok=True)
    for rel_path in paths:
        source = source_root / rel_path
        destination = snapshot_root / rel_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.exists() and source.is_file():
            shutil.copy2(source, destination)


def git_changed_files(worktree: Path, git_binary: str) -> list[str]:
    tracked = subprocess.run(
        [git_binary, "diff", "--name-only"],
        cwd=worktree,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    files: list[str] = []
    if tracked.returncode == 0:
        files.extend(line.strip() for line in tracked.stdout.splitlines() if line.strip())

    untracked = subprocess.run(
        [git_binary, "ls-files", "--others", "--exclude-standard"],
        cwd=worktree,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if untracked.returncode == 0:
        files.extend(line.strip() for line in untracked.stdout.splitlines() if line.strip())
    return sorted(set(files))


def git_changed_lines(worktree: Path, git_binary: str) -> dict[str, int]:
    completed = subprocess.run(
        [git_binary, "diff", "--numstat"],
        cwd=worktree,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    additions = 0
    deletions = 0
    if completed.returncode != 0:
        return {"additions": additions, "deletions": deletions}
    for line in completed.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        if parts[0].isdigit():
            additions += int(parts[0])
        if parts[1].isdigit():
            deletions += int(parts[1])
    for rel_path in git_untracked_files(worktree, git_binary):
        path = worktree / rel_path
        if path.is_file():
            additions += len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    return {"additions": additions, "deletions": deletions}


def git_untracked_files(worktree: Path, git_binary: str) -> list[str]:
    completed = subprocess.run(
        [git_binary, "ls-files", "--others", "--exclude-standard"],
        cwd=worktree,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def run_process(command: list[str], cwd: Path | None) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return {
        "command": command,
        "exit_code": completed.returncode,
        "passed": completed.returncode == 0,
        "stdout": truncate_output(completed.stdout),
        "stderr": truncate_output(completed.stderr),
    }


def truncate_output(text: Any) -> str:
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    if text is None:
        return ""
    if len(text) <= MAX_CAPTURED_OUTPUT:
        return text
    return text[:MAX_CAPTURED_OUTPUT] + "\n...[truncated]"


def safe_path_component(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in "._-" else "_" for char in value)
    return safe[:180] or "task"


def parse_optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def parse_optional_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    return parse_bool(value)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
