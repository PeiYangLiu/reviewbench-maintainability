#!/usr/bin/env python3
"""Prepare, run, and validate strict correctness for arbitrary solver models."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
from typing import Any, Iterable


LANES = ("gopy", "scripted", "tsjs", "csharp", "jvm", "rust", "native")
RUNNER_FILENAMES = {
    "gopy": "gopy_candidate_replay.py",
    "scripted": "scripted_runner.py",
    "tsjs": "run_tsjs.py",
    "csharp": "csharp_runner.py",
    "jvm": "jvm_candidate_replay.py",
    "rust": "run_rust.py",
    "native": "native_runner.py",
}
RUNNER_WAVE_PREFERENCE = {
    "gopy": ("wave-001", "wave-003", "wave-004"),
    "scripted": ("wave-001", "wave-003", "wave-004"),
    "tsjs": ("wave-001", "wave-003", "wave-004"),
    "csharp": ("wave-001", "wave-003", "wave-004"),
    "jvm": ("wave-003", "wave-001", "wave-004"),
    "rust": ("wave-001", "wave-003", "wave-004"),
    "native": ("wave-001", "wave-003", "wave-004"),
}
DEFAULT_JOBS = {
    "gopy": 8,
    "scripted": 6,
    "tsjs": 1,
    "csharp": 2,
    "jvm": 8,
    "rust": 8,
    "native": 4,
}
INFRA_STATUSES = {
    "infra",
    "infra_retry",
    "infra_unresolved",
    "infra_retry_required",
    "infrastructure_unresolved",
    "overload_retry",
    "runner_exception",
}
FINAL_STATUSES = {
    "pass",
    "fail",
    "candidate_missing",
    "scope_error",
    "apply_error",
    "metadata_error",
}
TRANSIENT_OUTPUT_PARTS = {
    "__pycache__",
    "gopy-cache",
    "gopy-worktrees",
    "gopy-worker-temp",
    "jvm-cache",
    "jvm-worktrees",
    "native-worktrees",
    "rust-worktrees",
    "rust-worker-state",
    "scripted-worktrees",
    "scripted-worker-state",
    "tsjs-cache",
    "tsjs-worktrees",
    "csharp-cache",
    "csharp-workspaces",
    "repo-process-locks",
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def task_fingerprint(task: dict[str, Any]) -> str:
    return canonical_hash(task)


def pair_set_hash(pairs: Iterable[tuple[str, str]]) -> str:
    return hashlib.sha256(
        "".join(
            f"{task_id}\0{model}\n"
            for task_id, model in sorted(set(pairs))
        ).encode()
    ).hexdigest()


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f".{path.name}.{os.getpid()}.new")
    with pending.open("w", encoding="utf-8") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(pending, path)


def atomic_json(path: Path, value: Any) -> None:
    atomic_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def atomic_jsonl(path: Path, values: Iterable[dict[str, Any]]) -> None:
    atomic_text(
        path,
        "".join(
            json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n"
            for value in values
        ),
    )


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise ValueError(f"missing JSONL input: {path}")
    records = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        1,
    ):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number} is not a JSON object")
        records.append(value)
    return records


def unique_records(
    records: list[dict[str, Any]],
    fields: tuple[str, ...],
    label: str,
) -> dict[tuple[Any, ...], dict[str, Any]]:
    result: dict[tuple[Any, ...], dict[str, Any]] = {}
    for record in records:
        key = tuple(record.get(field) for field in fields)
        if None in key or "" in key:
            raise ValueError(f"{label} has an empty key: {key}")
        if key in result:
            raise ValueError(f"duplicate {label}: {key}")
        result[key] = record
    return result


def safe_component(value: str) -> str:
    component = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    if not component:
        raise ValueError(f"value has no safe filesystem component: {value!r}")
    return component


def validate_model_id(model: str) -> str:
    if not model or model.startswith("/") or "\0" in model:
        raise ValueError(f"unsafe model ID: {model!r}")
    pieces = model.split("/")
    if any(
        not piece
        or piece in {".", ".."}
        or not re.fullmatch(r"[A-Za-z0-9._@+-]+", piece)
        for piece in pieces
    ):
        raise ValueError(f"unsafe model ID: {model!r}")
    return model


def parse_key_paths(
    values: list[str],
    *,
    label: str,
    allowed: set[str] | None = None,
) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"{label} must use KEY=/path syntax: {value!r}")
        key, raw_path = value.split("=", 1)
        if not key or not raw_path:
            raise ValueError(f"invalid {label}: {value!r}")
        if allowed is not None and key not in allowed:
            raise ValueError(f"unknown {label} key: {key}")
        if key in result:
            raise ValueError(f"duplicate {label} key: {key}")
        result[key] = Path(raw_path).expanduser().resolve()
    return result


def parse_models(values: list[str]) -> dict[str, Path]:
    parsed = parse_key_paths(values, label="model")
    return {validate_model_id(model): path for model, path in parsed.items()}


def source_lane(oracle: dict[str, Any]) -> str | None:
    value = str(oracle.get("lane_results_source") or "")
    for lane in LANES:
        if f"/{lane}/results.jsonl" in value.replace("\\", "/"):
            return lane
    return None


def pass_to_pass_count(value: Any) -> int:
    if type(value) is int:
        return value
    if isinstance(value, dict) and type(value.get("count")) is int:
        return value["count"]
    return 0


def legacy_lane(
    task: dict[str, Any],
    oracle: dict[str, Any],
) -> str:
    language = " ".join(
        str(value or "")
        for value in (task.get("inferred_language"), oracle.get("language"))
    ).lower()
    if "typescript" in language or "javascript" in language:
        return "tsjs"
    if "c#" in language or "dotnet" in language:
        return "csharp"
    if "rust" in language:
        return "rust"
    if (
        "c/c++" in language
        or "c++" in language
        or "native" in language
        or re.search(r"(^|\W)c($|\W)", language)
    ):
        return "native"
    if "java" in language or "kotlin" in language:
        return "jvm"
    if (
        "python" in language
        or "golang" in language
        or language.strip() == "go"
        or re.search(r"(^|\W)go($|\W)", language)
    ):
        return "gopy"
    return "scripted"


def load_release(
    tasks_path: Path,
    oracles_path: Path,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    tasks = read_jsonl(tasks_path)
    oracles = read_jsonl(oracles_path)
    tasks_by_key = unique_records(tasks, ("id",), "task ID")
    oracles_by_key = unique_records(oracles, ("id",), "Oracle ID")
    tasks_by_id = {str(key[0]): value for key, value in tasks_by_key.items()}
    oracles_by_id = {
        str(key[0]): value for key, value in oracles_by_key.items()
    }
    if set(tasks_by_id) != set(oracles_by_id):
        missing = sorted(set(tasks_by_id) - set(oracles_by_id))
        extra = sorted(set(oracles_by_id) - set(tasks_by_id))
        raise ValueError(
            f"task/Oracle ID mismatch missing={missing[:5]} extra={extra[:5]}"
        )
    for task_id, task in tasks_by_id.items():
        oracle = oracles_by_id[task_id]
        for field in ("repo", "start_sha", "target_sha", "pr_number"):
            left = task.get(field)
            right = oracle.get(field)
            if left is not None and right is not None and left != right:
                raise ValueError(
                    f"{task_id} task/Oracle {field} mismatch: "
                    f"{left!r} != {right!r}"
                )
        if oracle.get("golden_correctness_ok") is not True:
            raise ValueError(f"{task_id} lacks a passing golden Oracle")
        if pass_to_pass_count(oracle.get("pass_to_pass")) <= 0:
            raise ValueError(f"{task_id} has an empty PASS_TO_PASS Oracle")
        if oracle.get("changed_lines_covered") is not True:
            raise ValueError(f"{task_id} lacks changed-line coverage")
    return tasks, oracles, tasks_by_id, oracles_by_id


def resolve_oracle_root(
    repo_cache: Path,
    explicit: Path | None,
) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve()
    candidate = repo_cache.resolve().parent / "hard_oracle_20260804"
    if candidate.is_dir():
        return candidate
    raise ValueError(
        "cannot infer hard Oracle root; pass --oracle-root explicitly"
    )


def resolve_runner_sources(
    oracle_root: Path,
    overrides: dict[str, Path],
) -> dict[str, Path]:
    result = {}
    for lane in LANES:
        if lane in overrides:
            source = overrides[lane]
        else:
            filename = RUNNER_FILENAMES[lane]
            candidates = [
                oracle_root / wave / "correctness" / filename
                for wave in RUNNER_WAVE_PREFERENCE[lane]
            ]
            source = next((path for path in candidates if path.is_file()), None)
            if source is None:
                raise ValueError(
                    f"cannot locate a proven runner for {lane}: {candidates}"
                )
        if not source.is_file():
            raise ValueError(f"missing {lane} runner: {source}")
        result[lane] = source.resolve()
    return result


def lane_support_paths(oracle_root: Path) -> dict[str, list[Path]]:
    return {
        "gopy": [
            oracle_root / "gopy/tasks.jsonl",
            oracle_root / "gopy/results.jsonl",
        ],
        "scripted": [
            oracle_root / "scripted/tasks.jsonl",
            oracle_root / "scripted/results.jsonl",
            oracle_root / "scripted/oracle_config.json",
            oracle_root / "scripted/task_artifacts",
            oracle_root
            / "scripted/runtime/node-v25.6.1-linux-x64/bin/node",
        ],
        "tsjs": [
            oracle_root / "tsjs/tasks.jsonl",
            oracle_root / "tsjs/results.jsonl",
            oracle_root / "tsjs/run_oracle.py",
            oracle_root / "tsjs/cases.json",
        ],
        "csharp": [
            oracle_root / "csharp/tasks.jsonl",
            oracle_root / "csharp/results.jsonl",
            oracle_root / "csharp/specs.json",
        ],
        "jvm": [
            oracle_root / "jvm/tasks.jsonl",
            oracle_root / "jvm/results.jsonl",
            oracle_root / "jvm/inventory.jsonl",
        ],
        "rust": [
            oracle_root / "rust/tasks.jsonl",
            oracle_root / "rust/results.jsonl",
            oracle_root / "rust/plans.jsonl",
            oracle_root / "rust/runner.py",
        ],
        "native": [
            oracle_root / "native/tasks.jsonl",
            oracle_root / "native/results.jsonl",
            oracle_root / "native/plans.jsonl",
            oracle_root / "native/harnesses",
        ],
    }


def modern_lane_validation(
    lane: str,
    task_ids: set[str],
    tasks_by_id: dict[str, dict[str, Any]],
    oracle_root: Path,
) -> list[str]:
    errors = []
    lane_tasks_path = oracle_root / lane / "tasks.jsonl"
    lane_results_path = oracle_root / lane / "results.jsonl"
    lane_tasks = {
        str(key[0]): record
        for key, record in unique_records(
            read_jsonl(lane_tasks_path),
            ("id",),
            f"parent {lane} task ID",
        ).items()
    }
    lane_results = {
        str(key[0]): record
        for key, record in unique_records(
            read_jsonl(lane_results_path),
            ("id",),
            f"parent {lane} result ID",
        ).items()
    }
    for task_id in sorted(task_ids):
        task = tasks_by_id[task_id]
        lane_task = lane_tasks.get(task_id)
        result = lane_results.get(task_id)
        if lane_task is None or result is None:
            errors.append(f"{task_id}: missing parent {lane} task/result")
            continue
        if lane_task.get("start_sha") != task.get("start_sha"):
            errors.append(f"{task_id}: parent {lane} start SHA mismatch")
        if lane != "native":
            strict = (
                result.get("oracle_result") == "PASS_TO_PASS"
                or result.get("status") in {"strict_pass", "PASS", "pass"}
            )
            if (
                not strict
                or result.get("golden_correctness_ok") is not True
                or pass_to_pass_count(result.get("pass_to_pass")) <= 0
                or result.get("changed_lines_covered") is not True
            ):
                errors.append(f"{task_id}: parent {lane} result is not strict")
    return errors


def repo_name(task: dict[str, Any]) -> str:
    value = task.get("repo")
    if isinstance(value, dict):
        value = value.get("full_name")
    if not isinstance(value, str) or "/" not in value:
        raise ValueError(f"invalid task repository: {value!r}")
    return value


def validate_repo_cache(
    tasks: list[dict[str, Any]],
    repo_cache: Path,
    *,
    check_commits: bool,
) -> list[dict[str, str]]:
    failures = []
    checked: set[tuple[str, str]] = set()
    for task in tasks:
        repo = repo_name(task)
        start_sha = str(task.get("start_sha") or "")
        key = (repo, start_sha)
        if key in checked:
            continue
        checked.add(key)
        path = repo_cache / repo.replace("/", "__")
        if not path.is_dir():
            failures.append(
                {"repo": repo, "start_sha": start_sha, "error": "missing repo"}
            )
            continue
        if not check_commits:
            continue
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(path),
                "cat-file",
                "-e",
                f"{start_sha}^{{commit}}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if completed.returncode:
            failures.append(
                {
                    "repo": repo,
                    "start_sha": start_sha,
                    "error": "missing start commit",
                }
            )
    return failures


def build_task_plan(
    tasks: list[dict[str, Any]],
    oracles_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    plan = []
    for task in tasks:
        task_id = task["id"]
        oracle = oracles_by_id[task_id]
        lane = source_lane(oracle)
        source_kind = "modern" if lane else "published_legacy"
        if lane is None:
            lane = legacy_lane(task, oracle)
        plan.append(
            {
                "id": task_id,
                "repo": repo_name(task),
                "start_sha": task["start_sha"],
                "lane": lane,
                "source_kind": source_kind,
                "lane_results_source": oracle.get("lane_results_source"),
                "runner_ready": source_kind == "modern",
                "blocker": (
                    None
                    if source_kind == "modern"
                    else (
                        "portable published Oracle lacks a modern "
                        "lane_results_source/replay contract"
                    )
                ),
            }
        )
    return plan


def replace_required(
    text: str,
    old: str,
    new: str,
    *,
    label: str,
) -> str:
    count = text.count(old)
    if count != 1:
        raise ValueError(
            f"{label}: expected one occurrence of {old!r}, found {count}"
        )
    return text.replace(old, new, 1)


def regex_required(
    text: str,
    pattern: str,
    replacement: str,
    *,
    label: str,
    flags: int = 0,
) -> str:
    updated, count = re.subn(pattern, replacement, text, count=1, flags=flags)
    if count != 1:
        raise ValueError(
            f"{label}: pattern did not match exactly once: {pattern!r}"
        )
    return updated


def adapt_runner_source(
    lane: str,
    source_text: str,
) -> tuple[str, list[str]]:
    """Adapt one copied proven runner without touching its frozen source."""
    changes = []
    anchors = (
        "CORRECTNESS = Path(__file__).resolve().parent",
        "ROOT = Path(__file__).resolve().parent",
        'MODELS = ("claude-opus-4.8", "gemini-3.5-flash", "gpt-5.5")',
    )
    present_anchors = [
        candidate for candidate in anchors if candidate in source_text
    ]
    anchor = (
        min(present_anchors, key=source_text.index)
        if present_anchors
        else None
    )
    if anchor is None:
        raise ValueError(f"{lane}: cannot locate runner config anchor")
    injected = (
        '_OPEN_CONFIG = json.loads((Path(__file__).with_name('
        '"runner-config.json")).read_text(encoding="utf-8"))\n\n'
        + anchor
    )
    text = replace_required(
        source_text,
        anchor,
        injected,
        label=f"{lane} config injection",
    )
    changes.append("load runner-config.json")
    text = regex_required(
        text,
        r"^MODELS = (?:\(.+\)|\[.+\])$",
        'MODELS = tuple(_OPEN_CONFIG["models"])',
        label=f"{lane} dynamic models",
        flags=re.MULTILINE,
    )
    changes.append("replace closed-model tuple with configured models")

    if lane == "gopy":
        text = replace_required(
            text,
            "LANE = BASE / 'gopy'",
            'LANE = Path(_OPEN_CONFIG["lane_roots"]["gopy"])',
            label="gopy lane root",
        )
        text = replace_required(
            text,
            "REPOS = SESSION / 'repos'",
            'REPOS = Path(_OPEN_CONFIG["repo_cache"])',
            label="gopy repo cache",
        )
        text = regex_required(
            text,
            r"^EXPECTED_TASKS = \d+$",
            'EXPECTED_TASKS = int(_OPEN_CONFIG["lane_task_counts"]["gopy"])',
            label="gopy task count",
            flags=re.MULTILINE,
        )
        if "PYTHON_SITE = RUNTIME / 'python3.11-site'" in text:
            text = replace_required(
                text,
                "PYTHON_SITE = RUNTIME / 'python3.11-site'",
                (
                    'PYTHON_SITE = Path(_OPEN_CONFIG["support_paths"]'
                    '["gopy_python_site"])'
                ),
                label="gopy Python runtime",
            )
        text = replace_required(
            text,
            "GO = Path(shutil.which('go') or '/usr/bin/go')",
            (
                "GO = Path(shutil.which('go') or '/usr/bin/go')\n"
                "GO_1230 = LANE / 'toolchains/go1.23.0/bin/go'\n"
                "GO_12410 = next(\n"
                "    iter(sorted(LANE.glob(\n"
                "        'recovery/*/home/go/pkg/mod/'\n"
                "        'golang.org/toolchain@v0.0.1-"
                "go1.24.10.linux-amd64/bin/go'\n"
                "    ))),\n"
                "    LANE / 'missing-go1.24.10',\n"
                ")"
            ),
            label="gopy retained Go toolchains",
        )
        text = replace_required(
            text,
            (
                "    if 'go 1.23.6' in description:\n"
                "        return GO_1236\n"
                "    if 'go 1.25.0' in description:\n"
                "        return GO_125\n"
            ),
            (
                "    if 'go 1.23.0' in description:\n"
                "        return GO_1230\n"
                "    if 'go 1.23.6' in description:\n"
                "        return GO_1236\n"
                "    if 'go 1.24.2' in description or "
                "'go 1.24.10' in description:\n"
                "        return GO_12410\n"
                "    if 'go 1.25.0' in description:\n"
                "        return GO_125\n"
            ),
            label="gopy Oracle Go selection",
        )
        text = replace_required(
            text,
            (
                "    temp = CORRECTNESS / 'gopy-worker-temp' / "
                "hashlib.sha256(\n"
                "        str(artifact).encode()\n"
                "    ).hexdigest()[:12]"
            ),
            (
                "    temp = Path('/tmp/reviewbench-gopy') / "
                "hashlib.sha256(\n"
                "        str(artifact).encode()\n"
                "    ).hexdigest()[:12]"
            ),
            label="gopy short worker temp",
        )
        text = replace_required(
            text,
            (
                "    env.update({\n"
                "        'REVIEWED_PATH': task['locality']['path'],\n"
                "        'COVERAGE_OUT': context['coverage_out'],\n"
                "        'PYTHONPATH': os.pathsep.join([\n"
                "            str(LANE), str(worktree), "
                "str(worktree / 'src'),\n"
                "            interpolate(str(spec.get('pythonpath') "
                "or ''), context),\n"
                "            str(PYTHON_SITE),\n"
                "            os.environ.get('PYTHONPATH', ''),\n"
                "        ]),\n"
                "    })"
            ),
            (
                "    python_paths = [\n"
                "        str(LANE), str(worktree), str(worktree / 'src'),\n"
                "        interpolate(str(spec.get('pythonpath') "
                "or ''), context),\n"
                "    ]\n"
                "    if 'venv/bin/python' not in command[0]:\n"
                "        python_paths.extend([\n"
                "            str(PYTHON_SITE),\n"
                "            os.environ.get('PYTHONPATH', ''),\n"
                "        ])\n"
                "    env.update({\n"
                "        'REVIEWED_PATH': task['locality']['path'],\n"
                "        'COVERAGE_OUT': context['coverage_out'],\n"
                "        'PYTHONPATH': os.pathsep.join(python_paths),\n"
                "    })"
            ),
            label="gopy pinned virtualenv isolation",
        )
        text = replace_required(
            text,
            "    executed = run(command, cwd=cwd, env=env, timeout=timeout)",
            (
                "    if len(command) > 1 and Path(command[0]).name "
                "in {'bash', 'sh'}:\n"
                "        script = Path(command[1])\n"
                "        if script.is_file():\n"
                "            script_text = script.read_text(encoding='utf-8')\n"
                "            remapped = re.sub(\n"
                r"""                r'go_bin="[^"]+/bin/go"',"""
                "\n"
                "                f'go_bin=\"{go_binary}\"',\n"
                "                script_text,\n"
                "            )\n"
                "            if remapped != script_text:\n"
                "                script = artifact / 'oracle-remapped.sh'\n"
                "                script.write_text(remapped, encoding='utf-8')\n"
                "                command[1] = str(script)\n"
                "    executed = run(command, cwd=cwd, env=env, timeout=timeout)"
            ),
            label="gopy shell Oracle Go remap",
        )
        changes.extend(
            [
                "configure lane/repo roots",
                "configure task count",
                "preserve pinned Python and Go runtime contracts",
                "use short multiprocessing temp paths",
            ]
        )
    elif lane == "jvm":
        text = replace_required(
            text,
            "LANE = BASE / 'jvm'",
            'LANE = Path(_OPEN_CONFIG["lane_roots"]["jvm"])',
            label="jvm lane root",
        )
        text = replace_required(
            text,
            "REPOS = SESSION / 'repos'",
            'REPOS = Path(_OPEN_CONFIG["repo_cache"])',
            label="jvm repo cache",
        )
        text = regex_required(
            text,
            r"^EXPECTED_TASKS = \d+$",
            'EXPECTED_TASKS = int(_OPEN_CONFIG["lane_task_counts"]["jvm"])',
            label="jvm task count",
            flags=re.MULTILINE,
        )
        text = replace_required(
            text,
            "ANDROID_EMULATOR_INSTALL = CORRECTNESS / 'android-emulator-install'",
            (
                'ANDROID_EMULATOR_INSTALL = Path(_OPEN_CONFIG'
                '["support_paths"]["jvm_android_emulator"])'
            ),
            label="jvm Android runtime",
        )
        text = replace_required(
            text,
            "JVM_RUNTIME = CORRECTNESS / 'jvm-runtime'",
            (
                'JVM_RUNTIME = Path(_OPEN_CONFIG'
                '["support_paths"]["jvm_runtime"])'
            ),
            label="jvm runtime",
        )
        text = text.replace(
            "task_counts[task_id] == 3",
            "task_counts[task_id] == len(MODELS)",
        )
        text = text.replace(
            "'three_rows_per_task'",
            "'models_per_task'",
        )
        if text.count("'rb-jvm-w3r-'") != 2:
            raise ValueError("jvm container prefix count changed")
        text = text.replace("'rb-jvm-w3r-'", "'rb-open-jvm-'")
        changes.extend(
            [
                "configure lane/repo/runtime roots",
                "make per-task model cardinality dynamic",
                "use an open-model-specific container prefix",
            ]
        )
    elif lane == "scripted":
        text = text.replace(
            'MODELS = tuple(_OPEN_CONFIG["models"])\n',
            (
                'MODELS = tuple(_OPEN_CONFIG["models"])\n'
                'EXPECTED_TASKS = int('
                '_OPEN_CONFIG["lane_task_counts"]["scripted"])\n'
            ),
            1,
        )
        text = replace_required(
            text,
            "if len(self.tasks) != 35:",
            "if len(self.tasks) != EXPECTED_TASKS:",
            label="scripted task check",
        )
        text = replace_required(
            text,
            'raise RuntimeError(f"expected 35 scripted tasks, found {len(self.tasks)}")',
            (
                'raise RuntimeError(f"expected {EXPECTED_TASKS} scripted '
                'tasks, found {len(self.tasks)}")'
            ),
            label="scripted task message",
        )
        changes.append("configure task count")
    elif lane == "csharp":
        text = replace_required(
            text,
            'CSHARP = BASE / "csharp"',
            'CSHARP = Path(_OPEN_CONFIG["lane_roots"]["csharp"])',
            label="csharp lane root",
        )
        text = replace_required(
            text,
            'REPOS = SESSION / "repos"',
            'REPOS = Path(_OPEN_CONFIG["repo_cache"])',
            label="csharp repo cache",
        )
        text = regex_required(
            text,
            r"^EXPECTED_TASKS = \d+$",
            'EXPECTED_TASKS = int(_OPEN_CONFIG["lane_task_counts"]["csharp"])',
            label="csharp task count",
            flags=re.MULTILINE,
        )
        if text.count('f"rb-csharp-w3-') != 2:
            raise ValueError("csharp container prefix count changed")
        text = text.replace('f"rb-csharp-w3-', 'f"rb-open-csharp-')
        changes.extend(
            [
                "configure lane/repo roots",
                "configure task count",
                "use an open-model-specific container prefix",
            ]
        )
    elif lane == "tsjs":
        text = replace_required(
            text,
            'LANE = WAVE.parent / "tsjs"',
            (
                'LANE = Path(_OPEN_CONFIG["lane_roots"]["tsjs"])\n'
                'REPOS = Path(_OPEN_CONFIG["repo_cache"])'
            ),
            label="tsjs roots",
        )
        text = replace_required(
            text,
            "    module.WORKTREE_ROOT = WORKTREES\n",
            "    module.WORKTREE_ROOT = WORKTREES\n    module.REPOS = REPOS\n",
            label="tsjs lane repo override",
        )
        changes.append("configure lane and repo roots")
    elif lane == "native":
        text = replace_required(
            text,
            'NATIVE = WAVE.parent / "native"',
            'NATIVE = Path(_OPEN_CONFIG["lane_roots"]["native"])',
            label="native lane root",
        )
        text = replace_required(
            text,
            'REPOS = SESSION / "repos"',
            'REPOS = Path(_OPEN_CONFIG["repo_cache"])',
            label="native repo cache",
        )
        text = text.replace(
            'MODELS = tuple(_OPEN_CONFIG["models"])\n',
            (
                'MODELS = tuple(_OPEN_CONFIG["models"])\n'
                'EXPECTED_TASKS = int('
                '_OPEN_CONFIG["lane_task_counts"]["native"])\n'
            ),
            1,
        )
        text = replace_required(
            text,
            "if len(strict_rows) != 19:",
            "if len(strict_rows) != EXPECTED_TASKS:",
            label="native task check",
        )
        text = regex_required(
            text,
            (
                r'f"wave-001 must contain exactly 19 strict native tasks, '
                r'found \{len\(strict_rows\)\}"'
            ),
            (
                'f"wave must contain exactly {EXPECTED_TASKS} strict native '
                'tasks, found {len(strict_rows)}"'
            ),
            label="native task message",
        )
        text = regex_required(
            text,
            (
                r'    short_model = \{\n'
                r'        "claude-opus-4\.8": "claude",\n'
                r'        "gemini-3\.5-flash": "gemini",\n'
                r'        "gpt-5\.5": "gpt",\n'
                r'    \}\[model\]'
            ),
            (
                '    short_model = f"{safe_component(model)[:32]}-'
                '{hashlib.sha256(model.encode()).hexdigest()[:12]}"'
            ),
            label="native arbitrary model worktree",
        )
        text = replace_required(
            text,
            'return f"rb-native-w1oa-{digest}"',
            'return f"rb-open-native-{digest}"',
            label="native container prefix",
        )
        text = replace_required(
            text,
            "        commands = strict.get(\"commands\") or {}",
            (
                "        commands = {\n"
                "            name: (\n"
                "                value.replace(\n"
                '                    "${REVIEWBENCH_SESSION_FILES}",\n'
                '                    _OPEN_CONFIG["session_root"],\n'
                "                )\n"
                "                if isinstance(value, str) else value\n"
                "            )\n"
                "            for name, value in "
                "(strict.get(\"commands\") or {}).items()\n"
                "        }"
            ),
            label="native release placeholder restoration",
        )
        changes.extend(
            [
                "configure lane/repo roots",
                "configure task count",
                "derive safe arbitrary-model worktree names",
                "use an open-model-specific container prefix",
                "restore sanitized session paths before plan validation",
            ]
        )
    elif lane == "rust":
        text = replace_required(
            text,
            'LANE = WAVE.parent / "rust"',
            'LANE = Path(_OPEN_CONFIG["lane_roots"]["rust"])',
            label="rust lane root",
        )
        text = replace_required(
            text,
            'REPOS = SESSION / "repos"',
            'REPOS = Path(_OPEN_CONFIG["repo_cache"])',
            label="rust repo cache",
        )
        text = text.replace(
            'MODELS = tuple(_OPEN_CONFIG["models"])\n',
            (
                'MODELS = tuple(_OPEN_CONFIG["models"])\n'
                'EXPECTED_TASKS = int('
                '_OPEN_CONFIG["lane_task_counts"]["rust"])\n'
                'EXPECTED_RECORDS = EXPECTED_TASKS * len(MODELS)\n'
                'EXECUTION_HOST = _OPEN_CONFIG["execution_host"]\n'
            ),
            1,
        )
        replacements = {
            "if len(WAVE_ORACLES) != 14:": (
                "if len(WAVE_ORACLES) != EXPECTED_TASKS:"
            ),
            'f"expected 14 wave Rust Oracles, found {len(WAVE_ORACLES)}"': (
                'f"expected {EXPECTED_TASKS} wave Rust Oracles, found '
                '{len(WAVE_ORACLES)}"'
            ),
            "if len(ordered_pairs()) != 42:": (
                "if len(ordered_pairs()) != EXPECTED_RECORDS:"
            ),
            'raise RuntimeError("Rust replay inventory is not exactly 42 pairs")': (
                'raise RuntimeError(f"Rust replay inventory is not exactly '
                '{EXPECTED_RECORDS} pairs")'
            ),
            '"expected_records": 14,': (
                '"expected_records": EXPECTED_TASKS,'
            ),
            '"expected_records": 42,': (
                '"expected_records": EXPECTED_RECORDS,'
            ),
            '"expected_entry_count": 42,': (
                '"expected_entry_count": EXPECTED_RECORDS,'
            ),
            'len(per_task) == 14': (
                "len(per_task) == EXPECTED_TASKS"
            ),
            "all(count == 3 for count in per_task.values())": (
                "all(count == len(MODELS) for count in per_task.values())"
            ),
            "if len(rows) != 42:": (
                "if len(rows) != EXPECTED_RECORDS:"
            ),
            '"complete" if len(rows) == 42 and unresolved == 0': (
                '"complete" if len(rows) == EXPECTED_RECORDS '
                "and unresolved == 0"
            ),
            'errors.append(f"expected 42 rows, found {len(rows)}")': (
                'errors.append(f"expected {EXPECTED_RECORDS} rows, '
                'found {len(rows)}")'
            ),
            (
                "if len(per_task) != 14 or any(count != 3 "
                "for count in per_task.values()):"
            ): (
                "if len(per_task) != EXPECTED_TASKS or any("
                "count != len(MODELS) for count in per_task.values()):"
            ),
            'errors.append(f"expected 3 rows per task, found {dict(per_task)}")': (
                'errors.append(f"expected {len(MODELS)} rows per task, '
                'found {dict(per_task)}")'
            ),
            "if any(per_model[model] != 14 for model in MODELS):": (
                "if any(per_model[model] != EXPECTED_TASKS "
                "for model in MODELS):"
            ),
            'errors.append(f"expected 14 rows per model, found {dict(per_model)}")': (
                'errors.append(f"expected {EXPECTED_TASKS} rows per model, '
                'found {dict(per_model)}")'
            ),
        }
        for old, new in replacements.items():
            if old not in text:
                raise ValueError(f"rust adaptation missing fragment: {old}")
            text = text.replace(old, new)
        text = text.replace('"three_per_task":', '"models_per_task":')
        text = text.replace('"remote_host": "MSBJ07"', '"remote_host": EXECUTION_HOST')
        text = replace_required(
            text,
            (
                'f"rb-rust-w1-{os.getpid()}-{threading.get_ident()}-'
                '{label}-{inner}"'
            ),
            (
                'f"rb-open-rust-{os.getpid()}-{threading.get_ident()}-'
                '{label}-{inner}"'
            ),
            label="rust container prefix",
        )
        text = replace_required(
            text,
            (
                "def clean_variant(repo: Path, work: Path) -> None:\n"
            ),
            (
                "def remove_pair_state(path: Path) -> None:\n"
                "    if STATE.resolve() not in path.resolve().parents:\n"
                "        raise RuntimeError("
                'f"refusing to remove non-state path: {path}")\n'
                "    shutil.rmtree(path, ignore_errors=True)\n"
                "    if not path.exists():\n"
                "        return\n"
                '    cleanup_name = "rb-open-rust-state-" + '
                "hashlib.sha256(str(path).encode()).hexdigest()[:16]\n"
                "    subprocess.run(\n"
                '        ["docker", "rm", "-f", cleanup_name],\n'
                "        stdout=subprocess.DEVNULL,\n"
                "        stderr=subprocess.DEVNULL,\n"
                "        check=False,\n"
                "    )\n"
                "    removed = subprocess.run(\n"
                "        [\n"
                '            "docker", "run", "--rm", "--name", '
                "cleanup_name,\n"
                '            "-v", "/data:/data", lane.DEFAULT_IMAGE,\n'
                '            "sh", "-c", \'rm -rf -- "$1"\', "sh", '
                "str(path),\n"
                "        ],\n"
                "        stdout=subprocess.PIPE,\n"
                "        stderr=subprocess.STDOUT,\n"
                "        text=True,\n"
                "        check=False,\n"
                "        timeout=300,\n"
                "    )\n"
                "    if removed.returncode != 0 or path.exists():\n"
                "        raise RuntimeError(\n"
                '            "root-owned state cleanup failed: "\n'
                "            + removed.stdout[-1000:]\n"
                "        )\n"
                "\n\n"
                "def clean_variant(repo: Path, work: Path) -> None:\n"
            ),
            label="rust root-owned state cleanup helper",
        )
        text = replace_required(
            text,
            '    cargo_home = pair_state.parents[1] / "cargo-home"',
            '    cargo_home = pair_state / "cargo-home"',
            label="rust attempt-local cargo home",
        )
        text = replace_required(
            text,
            "            shutil.rmtree(pair_state, ignore_errors=True)",
            "            remove_pair_state(pair_state)",
            label="rust stale attempt-state cleanup",
        )
        text = replace_required(
            text,
            (
                "        finally:\n"
                "            clean_variant(repo, work)"
            ),
            (
                "        finally:\n"
                "            clean_variant(repo, work)\n"
                "            remove_pair_state(pair_state)"
            ),
            label="rust attempt-state cleanup",
        )
        changes.extend(
            [
                "configure lane/repo roots",
                "make task/model cardinality dynamic",
                "record the actual execution host",
                "use an open-model-specific container prefix",
                "remove root-owned per-attempt build state after evidence",
            ]
        )
    else:
        raise ValueError(f"unsupported lane: {lane}")

    compile(text, f"<adapted-{lane}-runner>", "exec")
    return text, changes


def runner_config(
    models: list[str],
    repo_cache: Path,
    oracle_root: Path,
    lane_counts: dict[str, int],
    runner_sources: dict[str, Path],
) -> dict[str, Any]:
    jvm_reference = runner_sources["jvm"].parent
    gopy_reference = runner_sources["gopy"].parent
    return {
        "schema_version": "reviewbench-open-correctness-runner-config/v1",
        "models": models,
        "repo_cache": str(repo_cache),
        "session_root": str(oracle_root.parent),
        "lane_roots": {
            lane: str(oracle_root / lane) for lane in LANES
        },
        "lane_task_counts": lane_counts,
        "execution_host": socket.gethostname().split(".", 1)[0],
        "support_paths": {
            "gopy_python_site": str(
                gopy_reference / "gopy-runtime/python3.11-site"
            ),
            "jvm_android_emulator": str(
                jvm_reference / "android-emulator-install"
            ),
            "jvm_runtime": str(jvm_reference / "jvm-runtime"),
        },
    }


def stage_adapted_runners(
    correctness_root: Path,
    sources: dict[str, Path],
    config: dict[str, Any],
) -> dict[str, Any]:
    correctness_root.mkdir(parents=True, exist_ok=True)
    atomic_json(correctness_root / "runner-config.json", config)
    provenance = {}
    for lane in LANES:
        source = sources[lane]
        destination = correctness_root / RUNNER_FILENAMES[lane]
        adapted, changes = adapt_runner_source(
            lane,
            source.read_text(encoding="utf-8"),
        )
        atomic_text(destination, adapted)
        provenance[lane] = {
            "source": str(source),
            "source_sha256": sha256(source),
            "adapted": str(destination),
            "adapted_sha256": sha256(destination),
            "changes": changes,
        }
    value = {
        "schema_version": "reviewbench-open-correctness-runner-provenance/v1",
        "config_sha256": sha256(correctness_root / "runner-config.json"),
        "runners": provenance,
    }
    atomic_json(correctness_root / "runner-provenance.json", value)
    return value


def write_preflight(
    *,
    tasks_path: Path,
    oracles_path: Path,
    repo_cache: Path,
    output: Path,
    model_ids: list[str],
    oracle_root: Path | None,
    runner_overrides: dict[str, Path],
    check_commits: bool,
) -> dict[str, Any]:
    models = [validate_model_id(model) for model in model_ids]
    if not models or len(models) != len(set(models)):
        raise ValueError("preflight requires unique --model-id values")
    model_slugs = [safe_component(model) for model in models]
    if len(model_slugs) != len(set(model_slugs)):
        raise ValueError("model IDs collide after filesystem normalization")
    tasks_path = tasks_path.resolve()
    oracles_path = oracles_path.resolve()
    repo_cache = repo_cache.resolve()
    root = resolve_oracle_root(repo_cache, oracle_root)
    tasks, oracles, tasks_by_id, oracles_by_id = load_release(
        tasks_path,
        oracles_path,
    )
    plan = build_task_plan(tasks, oracles_by_id)
    modern_ids: dict[str, set[str]] = {lane: set() for lane in LANES}
    legacy_ids: dict[str, set[str]] = {lane: set() for lane in LANES}
    for row in plan:
        target = modern_ids if row["source_kind"] == "modern" else legacy_ids
        target[row["lane"]].add(row["id"])

    runner_sources = resolve_runner_sources(root, runner_overrides)
    modern_counts = {lane: len(modern_ids[lane]) for lane in LANES}
    total_counts = {
        lane: len(modern_ids[lane]) + len(legacy_ids[lane])
        for lane in LANES
    }
    config = runner_config(
        models,
        repo_cache,
        root,
        modern_counts,
        runner_sources,
    )
    support = lane_support_paths(root)
    support["gopy"].append(
        Path(config["support_paths"]["gopy_python_site"])
    )
    support["jvm"].extend(
        [
            Path(config["support_paths"]["jvm_android_emulator"]),
            Path(config["support_paths"]["jvm_runtime"]),
        ]
    )
    support_missing = {
        lane: [str(path) for path in support[lane] if not path.exists()]
        for lane in LANES
    }
    parent_errors = {
        lane: modern_lane_validation(
            lane,
            modern_ids[lane],
            tasks_by_id,
            root,
        )
        for lane in LANES
    }
    repo_failures = validate_repo_cache(
        tasks,
        repo_cache,
        check_commits=check_commits,
    )
    output.mkdir(parents=True, exist_ok=True)
    modern_root = output / "modern"
    correctness_root = modern_root / "correctness"
    provenance = stage_adapted_runners(
        correctness_root,
        runner_sources,
        config,
    )
    modern_tasks = [
        task for task in tasks if task["id"] in set().union(*modern_ids.values())
    ]
    modern_oracles = [
        oracle
        for oracle in oracles
        if oracle["id"] in set().union(*modern_ids.values())
    ]
    atomic_jsonl(modern_root / "tasks.jsonl", modern_tasks)
    atomic_jsonl(modern_root / "oracles.jsonl", modern_oracles)
    atomic_jsonl(output / "release/tasks.jsonl", tasks)
    atomic_jsonl(output / "release/oracles.jsonl", oracles)
    atomic_jsonl(output / "task-plan.jsonl", plan)

    legacy_blockers = [
        {
            "id": row["id"],
            "lane": row["lane"],
            "kind": "missing_published_replay_contract",
            "detail": row["blocker"],
        }
        for row in plan
        if row["source_kind"] == "published_legacy"
    ]
    environment_errors = [
        *(
            {
                "lane": lane,
                "kind": "missing_lane_support",
                "path": path,
            }
            for lane, paths in support_missing.items()
            for path in paths
        ),
        *(
            {
                "lane": lane,
                "kind": "parent_lane_mismatch",
                "detail": error,
            }
            for lane, errors in parent_errors.items()
            for error in errors
        ),
        *(
            {
                "kind": "repo_cache",
                **failure,
            }
            for failure in repo_failures
        ),
    ]
    lane_summary = {}
    for lane in LANES:
        lane_summary[lane] = {
            "tasks": total_counts[lane],
            "modern_runner_tasks": modern_counts[lane],
            "published_legacy_tasks": len(legacy_ids[lane]),
            "expected_records": total_counts[lane] * len(models),
            "modern_runnable_records": modern_counts[lane] * len(models),
            "runner_source": str(runner_sources[lane]),
            "runner_source_sha256": sha256(runner_sources[lane]),
            "support_missing": support_missing[lane],
            "parent_validation_errors": parent_errors[lane],
        }
    result = {
        "schema_version": "reviewbench-open-correctness-preflight/v1",
        "created_at": utc_now(),
        "tasks": len(tasks),
        "models": models,
        "task_model_pairs": len(tasks) * len(models),
        "tasks_sha256": sha256(tasks_path),
        "oracles_sha256": sha256(oracles_path),
        "task_pair_set_sha256": pair_set_hash(
            (task["id"], model) for task in tasks for model in models
        ),
        "repo_cache": str(repo_cache),
        "oracle_root": str(root),
        "modern_runner_tasks": sum(modern_counts.values()),
        "published_legacy_tasks": len(legacy_blockers),
        "ready_for_modern_replay": not environment_errors,
        "ready_for_full_replay": (
            not environment_errors and not legacy_blockers
        ),
        "lanes": lane_summary,
        "environment_errors": environment_errors,
        "legacy_blockers": legacy_blockers,
        "runner_provenance_sha256": sha256(
            correctness_root / "runner-provenance.json"
        ),
    }
    atomic_json(output / "preflight.json", result)
    return result


def resolve_generation_artifact(
    record: dict[str, Any],
    results_path: Path,
    field: str,
    fallback_dir: str,
    suffix: str,
) -> Path | None:
    supplied = str(record.get(field) or "")
    candidates = []
    if supplied:
        path = Path(supplied)
        candidates.append(path if path.is_absolute() else results_path.parent / path)
    candidates.append(
        results_path.parent
        / fallback_dir
        / f"{record['id']}{suffix}"
    )
    return next((path.resolve() for path in candidates if path.is_file()), None)


def generation_marker(path: Path) -> Path:
    return path.with_name("generation_complete.json")


def validate_and_stage_model(
    *,
    model: str,
    results_path: Path,
    tasks: list[dict[str, Any]],
    tasks_path: Path,
    destination: Path,
    require_marker: bool,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    records = read_jsonl(results_path)
    by_pair = unique_records(records, ("id", "model"), "generation pair")
    expected = {(task["id"], model) for task in tasks}
    if set(by_pair) != expected:
        missing = sorted(expected - set(by_pair))
        extra = sorted(set(by_pair) - expected)
        raise ValueError(
            f"{model} generation key mismatch "
            f"missing={missing[:5]} extra={extra[:5]}"
        )
    tasks_by_id = {task["id"]: task for task in tasks}
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        results_path,
        destination / "generation_results.source.jsonl",
    )
    staged = {}
    patch_pairs = []
    for task in tasks:
        task_id = task["id"]
        source = dict(by_pair[(task_id, model)])
        if source.get("task_fingerprint") != task_fingerprint(task):
            raise ValueError(
                f"{task_id} {model} generation task fingerprint mismatch"
            )
        if source.get("start_sha") != task.get("start_sha"):
            if (
                source.get("apply_ok") is not True
                and source.get("start_sha") is None
            ):
                source["source_start_sha"] = None
                source["start_sha"] = task.get("start_sha")
                source["path"] = str(
                    (task.get("locality") or {}).get("path") or ""
                )
                source["metadata_recovered_from_task_fingerprint"] = True
            else:
                raise ValueError(
                    f"{task_id} {model} generation start SHA mismatch"
                )
        source["source_generation_results"] = str(results_path)
        source["source_patch_file"] = source.get("patch_file")
        source["source_trace_file"] = source.get("trace_file")
        patch_hash = ""
        if source.get("apply_ok") is True:
            if source.get("status") != "generated":
                raise ValueError(
                    f"{task_id} {model} apply_ok candidate is not generated"
                )
            patch = resolve_generation_artifact(
                source,
                results_path,
                "patch_file",
                "patches",
                ".patch",
            )
            if patch is None:
                raise ValueError(f"{task_id} {model} generated patch is missing")
            patch_hash = sha256(patch)
            if source.get("patch_sha256") != patch_hash:
                raise ValueError(
                    f"{task_id} {model} generated patch checksum mismatch"
                )
            copied_patch = destination / "patches" / f"{task_id}.patch"
            copied_patch.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(patch, copied_patch)
            if sha256(copied_patch) != patch_hash:
                raise ValueError(f"{task_id} {model} patch copy changed")
            source["patch_file"] = str(copied_patch.resolve())
        else:
            source["patch_file"] = None
            source["patch_sha256"] = None
        trace = resolve_generation_artifact(
            source,
            results_path,
            "trace_file",
            "traces",
            ".json",
        )
        if trace is not None:
            copied_trace = destination / "traces" / f"{task_id}.json"
            copied_trace.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(trace, copied_trace)
            source["trace_file"] = str(copied_trace.resolve())
            source["trace_sha256"] = sha256(copied_trace)
        else:
            source["trace_file"] = None
        staged[task_id] = source
        patch_pairs.append((task_id, patch_hash))

    patch_set = hashlib.sha256(
        "".join(
            f"{task_id}\t{patch_hash}\n"
            for task_id, patch_hash in sorted(patch_pairs)
        ).encode()
    ).hexdigest()
    task_ids_hash = hashlib.sha256(
        "".join(f"{task['id']}\n" for task in sorted(tasks, key=lambda x: x["id"])).encode()
    ).hexdigest()
    marker_path = generation_marker(results_path)
    marker = None
    if marker_path.is_file():
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if marker.get("model") != model:
            raise ValueError(f"{model} generation marker model mismatch")
        if marker.get("results_sha256") != sha256(results_path):
            raise ValueError(f"{model} generation marker is stale")
        if marker.get("tasks_sha256") != sha256(tasks_path):
            raise ValueError(f"{model} generation marker task content mismatch")
        if marker.get("tasks") != len(tasks):
            raise ValueError(f"{model} generation marker task count mismatch")
        if marker.get("task_ids_sha256") != task_ids_hash:
            raise ValueError(f"{model} generation marker task-set mismatch")
        if marker.get("patches_sha256") != patch_set:
            raise ValueError(f"{model} generation marker patch-set mismatch")
    elif require_marker:
        raise ValueError(f"{model} is missing generation_complete.json")

    atomic_jsonl(
        destination / "generation_results.normalized.jsonl",
        (staged[task["id"]] for task in tasks),
    )
    return staged, {
        "model": model,
        "source": str(results_path),
        "source_sha256": sha256(results_path),
        "source_marker": str(marker_path) if marker else None,
        "source_marker_sha256": sha256(marker_path) if marker else None,
        "normalized": str(
            destination / "generation_results.normalized.jsonl"
        ),
        "normalized_sha256": sha256(
            destination / "generation_results.normalized.jsonl"
        ),
        "patch_set_sha256": patch_set,
        "generated": sum(
            record.get("apply_ok") is True for record in staged.values()
        ),
        "candidate_missing": sum(
            record.get("apply_ok") is not True for record in staged.values()
        ),
    }


def stage_scripted_input(
    *,
    modern_root: Path,
    tasks_by_id: dict[str, dict[str, Any]],
    task_ids: set[str],
    models: list[str],
    generations: dict[str, dict[str, dict[str, Any]]],
    lane_root: Path,
) -> None:
    input_root = modern_root / "correctness/scripted-input"
    lane_tasks = read_jsonl(lane_root / "tasks.jsonl")
    unique_records(lane_tasks, ("id",), "Scripted parent task ID")
    selected = []
    for index, lane_task in enumerate(lane_tasks, 1):
        if lane_task["id"] not in task_ids:
            continue
        task = dict(tasks_by_id[lane_task["id"]])
        task["_scripted_lane_index"] = index
        selected.append(task)
    if {task["id"] for task in selected} != task_ids:
        raise ValueError("Scripted input task set mismatch")
    atomic_jsonl(input_root / "tasks.jsonl", selected)
    for model in models:
        model_root = input_root / "generation" / model
        rows = []
        for task in selected:
            record = dict(generations[model][task["id"]])
            if record.get("apply_ok") is True:
                patch = Path(record["patch_file"])
                destination = model_root / "patches" / f"{task['id']}.patch"
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(patch, destination)
            trace_file = record.get("trace_file")
            if trace_file and Path(trace_file).is_file():
                destination = model_root / "traces" / f"{task['id']}.json"
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(Path(trace_file), destination)
            rows.append(record)
        atomic_jsonl(model_root / "generation_results.jsonl", rows)
    atomic_json(
        input_root / "manifest.json",
        {
            "schema_version": "reviewbench-open-scripted-input/v1",
            "tasks": len(selected),
            "models": models,
            "records": len(selected) * len(models),
            "tasks_sha256": sha256(input_root / "tasks.jsonl"),
            "generation_sha256": {
                model: sha256(
                    input_root
                    / "generation"
                    / model
                    / "generation_results.jsonl"
                )
                for model in models
            },
        },
    )


def stage_csharp_input(
    *,
    modern_root: Path,
    tasks_by_id: dict[str, dict[str, Any]],
    task_ids: set[str],
    models: list[str],
    generations: dict[str, dict[str, dict[str, Any]]],
    lane_root: Path,
) -> None:
    input_root = modern_root / "correctness/csharp-input"
    lane_tasks = read_jsonl(lane_root / "tasks.jsonl")
    unique_records(lane_tasks, ("id",), "C# parent task ID")
    lane_results = {
        str(key[0]): row
        for key, row in unique_records(
            read_jsonl(lane_root / "results.jsonl"),
            ("id",),
            "C# parent result ID",
        ).items()
    }
    selected = [
        tasks_by_id[row["id"]]
        for row in lane_tasks
        if row["id"] in task_ids
    ]
    if {task["id"] for task in selected} != task_ids:
        raise ValueError("C# input task set mismatch")
    atomic_jsonl(input_root / "tasks.jsonl", selected)
    atomic_jsonl(
        input_root / "oracle/results.jsonl",
        (lane_results[task["id"]] for task in selected),
    )
    (input_root / "oracle").mkdir(parents=True, exist_ok=True)
    shutil.copy2(lane_root / "specs.json", input_root / "oracle/specs.json")
    rows = []
    for model in models:
        for task in selected:
            record = dict(generations[model][task["id"]])
            record["patch_relative"] = None
            record["patch_bytes"] = 0
            if record.get("apply_ok") is True:
                patch = Path(record["patch_file"])
                destination = (
                    input_root
                    / "patches"
                    / model
                    / f"{task['id']}.patch"
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(patch, destination)
                record["patch_relative"] = str(
                    destination.relative_to(input_root)
                )
                record["patch_bytes"] = destination.stat().st_size
                record["patch_sha256"] = sha256(destination)
            rows.append(record)
    atomic_jsonl(input_root / "generations.jsonl", rows)
    atomic_json(
        input_root / "manifest.json",
        {
            "schema_version": "reviewbench-open-csharp-input/v1",
            "tasks": len(selected),
            "models": models,
            "records": len(rows),
            "tasks_sha256": sha256(input_root / "tasks.jsonl"),
            "generations_sha256": sha256(
                input_root / "generations.jsonl"
            ),
            "oracle_results_sha256": sha256(
                input_root / "oracle/results.jsonl"
            ),
            "oracle_specs_sha256": sha256(
                input_root / "oracle/specs.json"
            ),
        },
    )


def stage_modern_generations(
    *,
    modern_root: Path,
    tasks: list[dict[str, Any]],
    models: list[str],
    generations: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, dict[str, dict[str, Any]]]:
    staged = {}
    for model in models:
        destination = (
            modern_root
            / "generation"
            / model
            / model
            / "generation_results.jsonl"
        )
        rows = []
        by_id = {}
        artifact_root = destination.parent
        for task in tasks:
            record = dict(generations[model][task["id"]])
            if record.get("apply_ok") is True:
                source_patch = Path(record["patch_file"])
                patch = artifact_root / "patches" / f"{task['id']}.patch"
                patch.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_patch, patch)
                record["patch_file"] = str(patch.resolve())
            source_trace = record.get("trace_file")
            if source_trace:
                trace = artifact_root / "traces" / f"{task['id']}.json"
                trace.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(Path(source_trace), trace)
                record["trace_file"] = str(trace.resolve())
            rows.append(record)
            by_id[task["id"]] = record
        atomic_jsonl(destination, rows)
        staged[model] = by_id
    return staged


def prepare_inputs(
    *,
    tasks_path: Path,
    oracles_path: Path,
    model_paths: dict[str, Path],
    repo_cache: Path,
    output: Path,
    oracle_root: Path | None,
    runner_overrides: dict[str, Path],
    require_marker: bool,
    allow_partial_modern: bool,
    check_commits: bool,
) -> dict[str, Any]:
    preflight = write_preflight(
        tasks_path=tasks_path,
        oracles_path=oracles_path,
        repo_cache=repo_cache,
        output=output,
        model_ids=list(model_paths),
        oracle_root=oracle_root,
        runner_overrides=runner_overrides,
        check_commits=check_commits,
    )
    if not preflight["ready_for_modern_replay"]:
        raise ValueError("modern replay preflight has environment errors")
    if preflight["legacy_blockers"] and not allow_partial_modern:
        raise ValueError(
            "published-hard replay contracts are missing; "
            "use --allow-partial-modern only to stage the 374 modern tasks"
        )
    tasks, _, tasks_by_id, _ = load_release(tasks_path, oracles_path)
    plan = read_jsonl(output / "task-plan.jsonl")
    modern_ids = {
        row["id"] for row in plan if row["source_kind"] == "modern"
    }
    modern_lane_ids = {
        lane: {
            row["id"]
            for row in plan
            if row["source_kind"] == "modern" and row["lane"] == lane
        }
        for lane in LANES
    }
    staged_generations = {}
    generation_manifest = {}
    for model, path in model_paths.items():
        staged, manifest = validate_and_stage_model(
            model=model,
            results_path=path,
            tasks=tasks,
            tasks_path=tasks_path,
            destination=output / "inputs/models" / safe_component(model),
            require_marker=require_marker,
        )
        staged_generations[model] = staged
        generation_manifest[model] = manifest

    modern_root = output / "modern"
    modern_tasks = [
        task for task in tasks if task["id"] in modern_ids
    ]
    modern_generations = stage_modern_generations(
        modern_root=modern_root,
        tasks=modern_tasks,
        models=list(model_paths),
        generations=staged_generations,
    )
    root = Path(preflight["oracle_root"])
    stage_scripted_input(
        modern_root=modern_root,
        tasks_by_id=tasks_by_id,
        task_ids=modern_lane_ids["scripted"],
        models=list(model_paths),
        generations=modern_generations,
        lane_root=root / "scripted",
    )
    stage_csharp_input(
        modern_root=modern_root,
        tasks_by_id=tasks_by_id,
        task_ids=modern_lane_ids["csharp"],
        models=list(model_paths),
        generations=modern_generations,
        lane_root=root / "csharp",
    )
    manifest = {
        "schema_version": "reviewbench-open-correctness-input/v1",
        "created_at": utc_now(),
        "tasks": len(tasks),
        "models": list(model_paths),
        "task_model_pairs": len(tasks) * len(model_paths),
        "modern_runner_tasks": len(modern_ids),
        "published_legacy_tasks": len(tasks) - len(modern_ids),
        "partial_modern_only": bool(preflight["legacy_blockers"]),
        "tasks_source": str(tasks_path.resolve()),
        "tasks_sha256": sha256(tasks_path),
        "oracles_source": str(oracles_path.resolve()),
        "oracles_sha256": sha256(oracles_path),
        "task_plan_sha256": sha256(output / "task-plan.jsonl"),
        "preflight_sha256": sha256(output / "preflight.json"),
        "generation": generation_manifest,
    }
    atomic_json(output / "input-manifest.json", manifest)
    return manifest


def lane_command(
    lane: str,
    output: Path,
    jobs: int,
) -> list[str]:
    modern = output / "modern"
    correctness = modern / "correctness"
    runner = correctness / RUNNER_FILENAMES[lane]
    if lane == "gopy":
        return [
            sys.executable,
            str(runner),
            "--workers",
            str(jobs),
            "--max-attempts",
            "2",
            "--retry-infra",
        ]
    if lane == "jvm":
        return [
            sys.executable,
            str(runner),
            "--workers",
            str(jobs),
            "--max-attempts",
            "3",
            "--retry-infra",
        ]
    if lane == "rust":
        return [
            sys.executable,
            str(runner),
            "run",
            "--jobs",
            str(jobs),
            "--retry-attempts",
            "4",
        ]
    if lane == "native":
        return [
            sys.executable,
            str(runner),
            "--jobs",
            str(jobs),
            "--attempts-per-run",
            "2",
            "--retry-complete",
        ]
    if lane == "tsjs":
        return [sys.executable, str(runner), "--retry-complete"]
    if lane == "scripted":
        config = json.loads(
            (correctness / "runner-config.json").read_text(encoding="utf-8")
        )
        return [
            sys.executable,
            str(runner),
            "--lane-root",
            config["lane_roots"]["scripted"],
            "--repos-root",
            config["repo_cache"],
            "--input-root",
            str(correctness / "scripted-input"),
            "--output-root",
            str(correctness),
            "--jobs",
            str(jobs),
            "--retry-attempts",
            "3",
        ]
    if lane == "csharp":
        config = json.loads(
            (correctness / "runner-config.json").read_text(encoding="utf-8")
        )
        return [
            sys.executable,
            str(runner),
            "--input-root",
            str(correctness / "csharp-input"),
            "--wave-root",
            str(modern),
            "--source-lane-root",
            config["lane_roots"]["csharp"],
            "--repo-cache",
            config["repo_cache"],
            "--workspace-root",
            str(correctness / "csharp-workspaces"),
            "--nuget-cache",
            str(correctness / "csharp-cache/nuget"),
            "--home-cache",
            str(correctness / "csharp-cache/home"),
            "--output",
            str(correctness / "csharp.jsonl"),
            "--workers",
            str(jobs),
            "--expected-tasks",
            str(config["lane_task_counts"]["csharp"]),
            "--max-attempts-per-pair",
            "3",
        ]
    raise ValueError(f"unsupported lane: {lane}")


def run_lanes(
    *,
    output: Path,
    selected_lanes: list[str],
    jobs: dict[str, int],
    allow_partial_modern: bool,
) -> dict[str, Any]:
    if len(selected_lanes) != len(set(selected_lanes)):
        raise ValueError("run lanes must be unique")
    manifest_path = output / "input-manifest.json"
    if not manifest_path.is_file():
        raise ValueError("run requires a completed prepare step")
    inputs = json.loads(manifest_path.read_text(encoding="utf-8"))
    if inputs.get("partial_modern_only") and not allow_partial_modern:
        raise ValueError(
            "full replay is blocked by published-hard legacy contracts"
        )
    correctness = output / "modern/correctness"
    logs = correctness / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    invocations_path = correctness / "invocations.jsonl"
    invocations = (
        read_jsonl(invocations_path) if invocations_path.is_file() else []
    )
    results = {}
    for lane in selected_lanes:
        existing_output = correctness / f"{lane}.jsonl"
        if existing_output.is_file():
            unique_records(
                read_jsonl(existing_output),
                ("id", "model"),
                f"existing {lane} correctness pair",
            )
        command = lane_command(lane, output, jobs[lane])
        log_path = logs / f"{lane}.log"
        started = utc_now()
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(
                f"\n=== {started} ===\n$ {shlex.join(command)}\n"
            )
            stream.flush()
            completed = subprocess.run(
                command,
                cwd=correctness,
                stdout=stream,
                stderr=subprocess.STDOUT,
                check=False,
            )
        invocation = {
            "lane": lane,
            "started_at": started,
            "completed_at": utc_now(),
            "command": command,
            "returncode": completed.returncode,
            "log": str(log_path),
            "log_sha256": sha256(log_path),
        }
        invocations.append(invocation)
        atomic_jsonl(invocations_path, invocations)
        results[lane] = invocation
    report = {
        "schema_version": "reviewbench-open-correctness-run/v1",
        "completed_at": utc_now(),
        "lanes": results,
        "all_returncodes_zero": all(
            value["returncode"] == 0 for value in results.values()
        ),
    }
    atomic_json(output / "run-report.json", report)
    if not report["all_returncodes_zero"]:
        failed = [
            lane
            for lane, invocation in results.items()
            if invocation["returncode"] != 0
        ]
        raise RuntimeError(f"correctness lane runners failed: {failed}")
    return report


def raw_status(lane: str, record: dict[str, Any]) -> str:
    if lane in {"native", "tsjs"}:
        return str(
            record.get("candidate_result")
            or record.get("status")
            or "UNKNOWN"
        )
    if lane == "csharp":
        return str(record.get("correctness_status") or "UNKNOWN")
    if lane == "scripted":
        if record.get("error_category"):
            return str(record["error_category"])
        return (
            "pass"
            if record.get("correctness_ok") is True
            else "fail"
            if record.get("correctness_ok") is False
            else "UNKNOWN"
        )
    return str(record.get("status") or "UNKNOWN")


def normalized_status(lane: str, record: dict[str, Any]) -> str:
    status = raw_status(lane, record).lower()
    if (
        record.get("retryable")
        or record.get("retry_required")
        or record.get("infra_failure")
        or record.get("infra_error")
        or status in INFRA_STATUSES
    ):
        return "infra_retry"
    if "candidate_missing" in status or "generation_missing" in status:
        return "candidate_missing"
    if "scope" in status:
        return "scope_error"
    if "apply" in status:
        return "apply_error"
    if "metadata" in status:
        return "metadata_error"
    if record.get("correctness_ok") is True or status == "pass":
        return "pass"
    if record.get("correctness_ok") is False or status in {
        "fail",
        "oracle_failure",
    }:
        return "fail"
    return "infra_retry"


def correctness_patch_sha(record: dict[str, Any]) -> str | None:
    value = (
        record.get("patch_sha256")
        or record.get("candidate_patch_sha256")
        or (record.get("patch_policy") or {}).get("sha256")
    )
    return str(value) if value else None


def normalized_record(
    lane: str,
    record: dict[str, Any],
    *,
    raw_source: str,
    source_kind: str,
    oracle: dict[str, Any] | None = None,
    generation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    oracle = oracle or {}
    generation = generation or {}
    status = normalized_status(lane, record)
    retryable = status == "infra_retry"
    coverage_provenance = (
        record.get("coverage_provenance")
        or record.get("coverage")
        or (record.get("oracle") or {}).get("coverage")
        or oracle.get("coverage")
        or {
            "changed_lines_covered": oracle.get("changed_lines_covered"),
            "lane_results_source": oracle.get("lane_results_source"),
            "release_oracle_provenance": oracle.get(
                "release_oracle_provenance"
            ),
        }
    )
    pass_to_pass_total = record.get("pass_to_pass_total")
    if pass_to_pass_total is None:
        pass_to_pass_total = pass_to_pass_count(
            oracle.get("pass_to_pass")
        )
    return {
        "schema_version": "reviewbench-open-correctness-normalized/v1",
        "id": record.get("id"),
        "model": record.get("model"),
        "lane": lane,
        "source_kind": source_kind,
        "status": status,
        "raw_status": raw_status(lane, record),
        "retryable": retryable,
        "generation_status": (
            record.get("generation_status") or generation.get("status")
        ),
        "patch_apply": record.get("patch_apply"),
        "scope_ok": record.get("scope_ok"),
        "correctness_ok": record.get("correctness_ok"),
        "start_sha": record.get("start_sha"),
        "patch_sha256": correctness_patch_sha(record),
        "changed_lines": record.get("changed_lines"),
        "pass_to_pass_passed": record.get("pass_to_pass_passed"),
        "pass_to_pass_total": pass_to_pass_total,
        "failed_tests": list(record.get("failed_tests") or []),
        "failure_root_cause": record.get("failure_root_cause"),
        "coverage_provenance": coverage_provenance,
        "artifacts": record.get("artifacts"),
        "raw_record_source": raw_source,
        "raw_record_sha256": canonical_hash(record),
        "raw_record": record,
    }


def write_artifact_set(output: Path) -> str:
    entries = []
    for path in sorted(output.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(output)
        if path.name in {
            "SHA256SUMS",
            "correctness.complete.json",
            ".native.lock",
            ".tsjs.lock",
        }:
            continue
        if any(part in TRANSIENT_OUTPUT_PARTS for part in relative.parts):
            continue
        if path.name.endswith(".lock") or ".new" in path.name:
            continue
        entries.append(f"{sha256(path)}  {relative}\n")
    atomic_text(output / "SHA256SUMS", "".join(entries))
    return sha256(output / "SHA256SUMS")


def finalize(
    *,
    tasks_path: Path,
    oracles_path: Path,
    output: Path,
    allow_incomplete: bool,
) -> dict[str, Any]:
    inputs = json.loads(
        (output / "input-manifest.json").read_text(encoding="utf-8")
    )
    if inputs.get("tasks_sha256") != sha256(tasks_path):
        raise ValueError("correctness inputs were prepared from stale tasks")
    if inputs.get("oracles_sha256") != sha256(oracles_path):
        raise ValueError("correctness inputs were prepared from stale Oracles")
    if inputs.get("task_plan_sha256") != sha256(
        output / "task-plan.jsonl"
    ):
        raise ValueError("correctness task plan changed after preparation")
    models = list(inputs["models"])
    tasks, _, tasks_by_id, oracles_by_id = load_release(
        tasks_path,
        oracles_path,
    )
    plan = read_jsonl(output / "task-plan.jsonl")
    plan_by_id = {row["id"]: row for row in plan}
    if set(plan_by_id) != set(tasks_by_id):
        raise ValueError("task plan is stale")
    generations = {}
    for model in models:
        path = (
            output
            / "inputs/models"
            / safe_component(model)
            / "generation_results.normalized.jsonl"
        )
        rows = read_jsonl(path)
        by_pair = unique_records(
            rows,
            ("id", "model"),
            f"{model} staged generation pair",
        )
        expected = {(task["id"], model) for task in tasks}
        if set(by_pair) != expected:
            raise ValueError(f"{model} staged generation key mismatch")
        generations[model] = {
            str(key[0]): record for key, record in by_pair.items()
        }

    all_pairs: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
    lane_manifest = {}
    normalized_root = output / "correctness-normalized"
    raw_root = output / "correctness-raw"
    missing_pairs = []
    infra_pairs = []
    global_counts: Counter[str] = Counter()
    for lane in LANES:
        sources = [
            (
                "modern",
                output / "modern/correctness" / f"{lane}.jsonl",
            ),
            (
                "published_legacy",
                output / "legacy/correctness" / f"{lane}.jsonl",
            ),
        ]
        raw_rows = []
        normalized_rows = []
        for source_kind, path in sources:
            if not path.is_file():
                continue
            rows = read_jsonl(path)
            unique_records(
                rows,
                ("id", "model"),
                f"{lane} {source_kind} correctness pair",
            )
            for record in rows:
                key = (record.get("id"), record.get("model"))
                if key in all_pairs:
                    raise ValueError(f"cross-source correctness duplicate: {key}")
                if key[0] not in plan_by_id or key[1] not in models:
                    raise ValueError(f"unexpected correctness pair: {key}")
                expected_plan = plan_by_id[str(key[0])]
                if expected_plan["lane"] != lane:
                    raise ValueError(
                        f"{key} correctness lane {lane} != "
                        f"{expected_plan['lane']}"
                    )
                if expected_plan["source_kind"] != source_kind:
                    raise ValueError(
                        f"{key} correctness source {source_kind} != "
                        f"{expected_plan['source_kind']}"
                    )
                task = tasks_by_id[str(key[0])]
                if record.get("start_sha") != task.get("start_sha"):
                    raise ValueError(f"{key} correctness start SHA mismatch")
                generation = generations[str(key[1])][str(key[0])]
                if generation.get("apply_ok") is True:
                    if correctness_patch_sha(record) != generation.get(
                        "patch_sha256"
                    ):
                        raise ValueError(
                            f"{key} correctness patch checksum mismatch"
                        )
                normalized = normalized_record(
                    lane,
                    record,
                    raw_source=str(path.relative_to(output)),
                    source_kind=source_kind,
                    oracle=oracles_by_id[str(key[0])],
                    generation=generation,
                )
                if generation.get("apply_ok") is True:
                    if normalized["status"] == "candidate_missing":
                        raise ValueError(
                            f"{key} runner lost a staged generated candidate"
                        )
                elif normalized["status"] != "candidate_missing":
                    raise ValueError(
                        f"{key} non-generated candidate was not reported missing"
                    )
                all_pairs[(str(key[0]), str(key[1]))] = (lane, normalized)
                raw_rows.append(record)
                normalized_rows.append(normalized)
                global_counts[normalized["status"]] += 1
                if normalized["retryable"]:
                    infra_pairs.append(key)

        expected = {
            (row["id"], model)
            for row in plan
            if row["lane"] == lane
            for model in models
        }
        actual = {
            (record["id"], record["model"]) for record in normalized_rows
        }
        lane_missing = sorted(expected - actual)
        missing_pairs.extend(lane_missing)
        normalized_rows.sort(
            key=lambda record: (record["id"], models.index(record["model"]))
        )
        raw_rows.sort(
            key=lambda record: (record["id"], models.index(record["model"]))
        )
        atomic_jsonl(raw_root / f"{lane}.jsonl", raw_rows)
        atomic_jsonl(normalized_root / f"{lane}.jsonl", normalized_rows)
        counts = Counter(record["status"] for record in normalized_rows)
        lane_manifest[lane] = {
            "tasks": sum(row["lane"] == lane for row in plan),
            "expected_records": len(expected),
            "records": len(normalized_rows),
            "missing_records": len(lane_missing),
            "status_counts": dict(sorted(counts.items())),
            "raw_sha256": sha256(raw_root / f"{lane}.jsonl"),
            "normalized_sha256": sha256(
                normalized_root / f"{lane}.jsonl"
            ),
        }

    expected_global = {
        (task["id"], model) for task in tasks for model in models
    }
    actual_global = set(all_pairs)
    if missing_pairs != sorted(expected_global - actual_global):
        raise ValueError("lane/global missing pair calculations disagree")
    complete = (
        actual_global == expected_global
        and not infra_pairs
        and all(
            record["status"] in FINAL_STATUSES
            for _, record in all_pairs.values()
        )
    )
    manifest = {
        "schema_version": "reviewbench-open-correctness-manifest/v1",
        "created_at": utc_now(),
        "complete": complete,
        "tasks": len(tasks),
        "models": models,
        "expected_task_model_pairs": len(expected_global),
        "records": len(actual_global),
        "unique_task_model_pairs": len(actual_global),
        "pair_set_sha256": pair_set_hash(actual_global),
        "expected_pair_set_sha256": pair_set_hash(expected_global),
        "tasks_sha256": sha256(tasks_path),
        "oracles_sha256": sha256(oracles_path),
        "task_plan_sha256": sha256(output / "task-plan.jsonl"),
        "input_manifest_sha256": sha256(output / "input-manifest.json"),
        "runner_provenance_sha256": sha256(
            output / "modern/correctness/runner-provenance.json"
        ),
        "generation_inputs": inputs.get("generation", {}),
        "status_counts": dict(sorted(global_counts.items())),
        "missing_pairs": [
            {"id": task_id, "model": model}
            for task_id, model in missing_pairs
        ],
        "infra_retry_pairs": [
            {"id": task_id, "model": model}
            for task_id, model in sorted(infra_pairs)
        ],
        "lanes": lane_manifest,
        "raw_records_preserved": True,
        "normalized_records_embed_raw_record": True,
    }
    atomic_json(output / "correctness-manifest.json", manifest)
    sums_hash = write_artifact_set(output)
    completion = {
        "schema_version": "reviewbench-open-correctness-complete/v1",
        "complete": complete,
        "manifest_sha256": sha256(output / "correctness-manifest.json"),
        "sha256sums_sha256": sums_hash,
        "records": len(actual_global),
        "expected_records": len(expected_global),
        "pair_set_sha256": manifest["pair_set_sha256"],
    }
    atomic_json(output / "correctness.complete.json", completion)
    if not complete and not allow_incomplete:
        raise ValueError(
            f"correctness is incomplete: missing={len(missing_pairs)} "
            f"infra={len(infra_pairs)}"
        )
    return manifest


def parse_jobs(values: list[str]) -> dict[str, int]:
    result = dict(DEFAULT_JOBS)
    seen = set()
    for value in values:
        if "=" not in value:
            raise ValueError(f"lane jobs must use LANE=N syntax: {value!r}")
        key, raw_count = value.split("=", 1)
        if key not in LANES:
            raise ValueError(f"unknown lane jobs key: {key}")
        if key in seen:
            raise ValueError(f"duplicate lane jobs key: {key}")
        seen.add(key)
        try:
            count = int(raw_count)
        except ValueError as exc:
            raise ValueError(
                f"invalid job count for {key}: {raw_count}"
            ) from exc
        if count < 1:
            raise ValueError(f"job count must be positive for {key}")
        result[key] = count
    return result


def add_common_release_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--oracles", type=Path, required=True)
    parser.add_argument("--repo-cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--oracle-root", type=Path)
    parser.add_argument(
        "--runner",
        action="append",
        default=[],
        metavar="LANE=PATH",
    )
    parser.add_argument("--skip-repo-commit-check", action="store_true")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight = subparsers.add_parser(
        "preflight",
        help="validate release/lane compatibility without generation artifacts",
    )
    add_common_release_args(preflight)
    preflight.add_argument("--model-id", action="append", required=True)

    prepare = subparsers.add_parser(
        "prepare",
        help="validate and stage completed arbitrary-model generation",
    )
    add_common_release_args(prepare)
    prepare.add_argument(
        "--model",
        action="append",
        required=True,
        metavar="MODEL=GENERATION_RESULTS_JSONL",
    )
    prepare.add_argument(
        "--allow-missing-generation-marker",
        action="store_true",
    )
    prepare.add_argument("--allow-partial-modern", action="store_true")

    run = subparsers.add_parser("run", help="run prepared modern lane replays")
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--lane", action="append", choices=LANES, default=[])
    run.add_argument(
        "--jobs",
        action="append",
        default=[],
        metavar="LANE=N",
    )
    run.add_argument("--allow-partial-modern", action="store_true")

    finish = subparsers.add_parser(
        "finalize",
        help="normalize and validate raw correctness outputs",
    )
    finish.add_argument("--tasks", type=Path, required=True)
    finish.add_argument("--oracles", type=Path, required=True)
    finish.add_argument("--output", type=Path, required=True)
    finish.add_argument("--allow-incomplete", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command in {"preflight", "prepare"}:
        runner_overrides = parse_key_paths(
            args.runner,
            label="runner",
            allowed=set(LANES),
        )
        common = {
            "tasks_path": args.tasks,
            "oracles_path": args.oracles,
            "repo_cache": args.repo_cache,
            "output": args.output,
            "oracle_root": args.oracle_root,
            "runner_overrides": runner_overrides,
            "check_commits": not args.skip_repo_commit_check,
        }
        if args.command == "preflight":
            result = write_preflight(
                **common,
                model_ids=args.model_id,
            )
        else:
            result = prepare_inputs(
                **common,
                model_paths=parse_models(args.model),
                require_marker=not args.allow_missing_generation_marker,
                allow_partial_modern=args.allow_partial_modern,
            )
    elif args.command == "run":
        selected = args.lane or list(LANES)
        result = run_lanes(
            output=args.output.resolve(),
            selected_lanes=selected,
            jobs=parse_jobs(args.jobs),
            allow_partial_modern=args.allow_partial_modern,
        )
    else:
        result = finalize(
            tasks_path=args.tasks.resolve(),
            oracles_path=args.oracles.resolve(),
            output=args.output.resolve(),
            allow_incomplete=args.allow_incomplete,
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
