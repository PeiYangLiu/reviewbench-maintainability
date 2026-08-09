#!/usr/bin/env python3
"""Package published and newly evaluated hard tasks into one portable release."""

from __future__ import annotations

import argparse
from collections import Counter
import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any

from collect_hard_oracle_passes import strict_pass
from package_agentic_evaluation import parse_replacements, sanitize
import summarize_release_evaluation as summarizer
import validate_hard_wave as wave_validator


MODELS = ("claude-opus-4.8", "gemini-3.5-flash", "gpt-5.5")
ORACLE_GATE = (
    "exact-start/source-only-golden 3x stable + nonempty PASS_TO_PASS + "
    "changed-old production coverage"
)
RELEASE_FILES = {
    "README.md",
    "tasks.jsonl",
    "oracles.jsonl",
    "published-hard-results.jsonl",
    "result-overrides.jsonl",
    "results.jsonl",
    "report.json",
    "manifest.json",
    "SHA256SUMS",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--published-tasks", type=Path, required=True)
    parser.add_argument("--published-results", type=Path, required=True)
    parser.add_argument(
        "--published-oracles",
        type=Path,
        action="append",
        required=True,
    )
    parser.add_argument("--wave", type=Path, action="append", required=True)
    parser.add_argument(
        "--result-override",
        type=Path,
        action="append",
        default=[],
        help=(
            "Normalized runner-derived correction rows keyed by task ID and "
            "model; each key must already exist in the source matrix."
        ),
    )
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--published-difficulty", default="hard")
    parser.add_argument(
        "--sanitize-root",
        action="append",
        default=[],
        help="LABEL=/absolute/path; repeat for each machine-specific root.",
    )
    parser.add_argument(
        "--created-at",
        help="ISO-8601 timestamp; defaults to the current UTC time.",
    )
    parser.add_argument(
        "--note",
        action="append",
        default=[],
        help="Release note recorded in README and manifest; repeat as needed.",
    )
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def unique_by(
    records: list[dict[str, Any]],
    fields: tuple[str, ...],
    label: str,
) -> None:
    keys = [tuple(record.get(field) for field in fields) for record in records]
    duplicates = [key for key, count in Counter(keys).items() if count > 1]
    if duplicates:
        raise ValueError(f"duplicate {label}: {duplicates[:5]}")


def task_is_published_hard(
    task: dict[str, Any],
    difficulty: str,
) -> bool:
    validation = task.get("release_validation") or {}
    return validation.get("judged_difficulty") == difficulty


def published_task_certifies_oracle(task: dict[str, Any]) -> bool:
    validation = task.get("release_validation") or {}
    return (
        validation.get("golden_correctness") is True
        and type(validation.get("pass_to_pass")) is int
        and validation["pass_to_pass"] > 0
        and validation.get("changed_lines_covered") is True
    )


def pass_to_pass_count(value: Any) -> int:
    if type(value) is int:
        return value
    if isinstance(value, dict) and type(value.get("count")) is int:
        return value["count"]
    return 0


def validate_oracle_identity(
    task: dict[str, Any],
    oracle: dict[str, Any],
) -> None:
    for field in ("repo", "start_sha", "target_sha", "pr_number"):
        task_value = task.get(field)
        oracle_value = oracle.get(field)
        if (
            task_value is not None
            and oracle_value is not None
            and task_value != oracle_value
        ):
            raise ValueError(
                f"{task['id']} task/Oracle {field} mismatch: "
                f"{task_value!r} != {oracle_value!r}"
            )


def validate_published_oracle(
    task: dict[str, Any],
    oracle: dict[str, Any],
) -> None:
    if not published_task_certifies_oracle(task):
        raise ValueError(
            f"{task['id']} lacks strict published release validation"
        )
    if (
        oracle.get("golden_correctness_ok") is not True
        or oracle.get("changed_lines_covered") is not True
        or pass_to_pass_count(oracle.get("pass_to_pass")) <= 0
    ):
        raise ValueError(
            f"{task['id']} Oracle lacks correctness, P2P, or coverage evidence"
        )
    validate_oracle_identity(task, oracle)


def artifact_hash(manifest: dict[str, Any], filename: str) -> str:
    artifacts = manifest.get("artifacts") or {}
    value = artifacts.get(filename)
    if value is None and filename == "oracles.jsonl":
        value = artifacts.get("oracles")
    if isinstance(value, dict):
        return str(value.get("sha256") or "")
    return str(value or "")


def certify_published_oracle_source(path: Path) -> dict[str, str]:
    manifest_path = path.parent / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"missing published manifest for {path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("complete") is not True:
        raise ValueError(f"published release is not complete: {manifest_path}")
    expected = artifact_hash(manifest, path.name)
    actual = digest(path)
    if not expected or expected != actual:
        raise ValueError(
            f"published Oracle hash mismatch for {path}: "
            f"{expected!r} != {actual}"
        )
    return {
        "name": source_label(path.parent),
        "oracles_sha256": actual,
        "manifest_sha256": digest(manifest_path),
    }


def task_is_new_hard(task: dict[str, Any]) -> bool:
    selection = task.get("hard_selection") or {}
    return (
        selection.get("gpt_strict_pass") is True
        and (
            selection.get("gpt_hard") is True
            or selection.get("claude_hard") is True
        )
    )


def validate_terminal_result(result: dict[str, Any]) -> None:
    generation_status = str(result.get("generation_status") or "")
    judge_status = str(result.get("judge_status") or "")
    correctness_status = str(result.get("correctness_status") or "")
    if generation_status not in {"generated", "failed"}:
        raise ValueError(
            f"{result.get('id')} {result.get('model')} has unresolved "
            f"generation status {generation_status!r}"
        )
    if judge_status in {"judge_error", "input_error", "missing_judgment"}:
        raise ValueError(
            f"{result.get('id')} {result.get('model')} has unresolved "
            f"judge status {judge_status!r}"
        )
    if "retry infrastructure" in correctness_status.lower():
        raise ValueError(
            f"{result.get('id')} {result.get('model')} has unresolved "
            "correctness infrastructure failure"
        )
    if result.get("generation_apply") is True and judge_status != "judged":
        raise ValueError(
            f"{result.get('id')} {result.get('model')} applied a patch "
            f"without a final judgment: {judge_status!r}"
        )
    for field in (
        "generation_apply",
        "scope_ok",
        "correctness_ok",
        "maintainability_pass",
        "resolved",
    ):
        if type(result.get(field)) is not bool:
            raise ValueError(
                f"{result.get('id')} {result.get('model')} lacks boolean "
                f"{field}"
            )
    if (
        result["generation_apply"] is True
        and correctness_status in {"", "missing", "generation_missing"}
    ):
        raise ValueError(
            f"{result.get('id')} {result.get('model')} lacks a terminal "
            "correctness result"
        )
    if (
        (generation_status == "generated")
        is not result["generation_apply"]
    ):
        raise ValueError(
            f"{result.get('id')} {result.get('model')} has contradictory "
            "generation status/apply outcome"
        )
    if result["generation_apply"] is False:
        if (
            result["scope_ok"]
            or result["correctness_ok"]
            or result["maintainability_pass"]
            or judge_status != "generation_failed"
        ):
            raise ValueError(
                f"{result.get('id')} {result.get('model')} has downstream "
                "passes after failed generation"
            )
    expected_resolved = all(
        result[field]
        for field in (
            "generation_apply",
            "scope_ok",
            "correctness_ok",
            "maintainability_pass",
        )
    )
    if result["resolved"] is not expected_resolved:
        raise ValueError(
            f"{result.get('id')} {result.get('model')} has contradictory "
            "resolved/gate outcomes"
        )


def source_label(path: Path) -> str:
    return path.name or path.parent.name


def prepare_output_dir(path: Path) -> None:
    if path.exists():
        unexpected = {item.name for item in path.iterdir()} - RELEASE_FILES
        if unexpected:
            raise ValueError(
                f"output directory contains unexpected files: "
                f"{sorted(unexpected)}"
            )
    path.mkdir(parents=True, exist_ok=True)


def build_report(
    results: list[dict[str, Any]],
    sources: list[str],
) -> dict[str, Any]:
    models = sorted({str(result["model"]) for result in results})
    task_ids = {str(result["id"]) for result in results}
    incomplete = [
        task_id
        for task_id in sorted(task_ids)
        if {row["model"] for row in results if row["id"] == task_id}
        != set(models)
    ]
    return {
        "schema_version": "reviewbench-model-evaluation/cumulative-v1",
        "sources": sources,
        "tasks": len(task_ids),
        "task_model_pairs": len(results),
        "models": models,
        "tasks_missing_a_model": incomplete,
        "leaderboard": {
            model: summarizer.summarize_model(results, model)
            for model in models
        },
        "pairwise": summarizer.pairwise(results, models),
    }


def build_readme(
    task_count: int,
    published_count: int,
    wave_counts: list[tuple[str, int]],
    report: dict[str, Any],
    notes: list[str],
) -> str:
    rows = []
    for model in MODELS:
        entry = report["leaderboard"][model]
        rows.append(
            f"| {model} | {entry['generation_apply']} | "
            f"{entry['scope_pass']} | {entry['correctness_pass']} | "
            f"{entry['maintainability_pass']} | "
            f"**{entry['resolved']}/{entry['tasks']} "
            f"({entry['resolved_rate']:.1%})** |"
        )
    sources = "\n".join(
        f"- `{name}`: {count} new hard tasks"
        for name, count in wave_counts
    )
    release_notes = "\n".join(f"- {note}" for note in notes)
    if release_notes:
        release_notes = f"\n## Release notes\n\n{release_notes}\n"
    return f"""# ReviewBench {task_count}-task Hard Release

This directory freezes {task_count} strict hard maintainability tasks and their
complete three-model agentic evaluation.

## Composition

- Previously published hard tasks: {published_count}
{sources}
- Total task-model pairs: {task_count * len(MODELS)}

Every task has an exact start revision, a source-only golden revision, three
stable runs on both sides, a non-empty PASS_TO_PASS oracle, and changed-old
production-line coverage.

The {published_count} previously published hard tasks use the conservative
published difficulty field. The new hard waves require a strict GPT quality
pass and at least one independent hard verdict; this avoids treating
uncalibrated cross-model confidence values as a hard threshold.

## Leaderboard

| Model | Generation | Scope | Correctness | Maintainability | Resolved@1 |
|---|---:|---:|---:|---:|---:|
{chr(10).join(rows)}

Resolved@1 requires generation/apply, scope, executable correctness, and the
human-golden-calibrated maintainability rubric v2.

## Files

| File | Contents |
|---|---|
| `tasks.jsonl` | {task_count} strict hard tasks |
| `oracles.jsonl` | One strict executable Oracle record per task |
| `results.jsonl` | {task_count * len(MODELS):,} normalized task-model gate results |
| `published-hard-results.jsonl` | The calibrated {published_count}-task published-hard slice |
| `result-overrides.jsonl` | Sanitized runner-derived corrections applied during packaging |
| `report.json` | Aggregate leaderboard, breakdowns, and pairwise outcomes |
| `manifest.json` | Release contract, sources, counts, and hashes |
| `SHA256SUMS` | Integrity hashes |

Machine-specific roots in Oracle metadata are replaced with documented
environment-variable placeholders. Large build workspaces, dependency caches,
container layers, and raw logs are intentionally not included.

Verify the package from this directory:

```bash
sha256sum -c SHA256SUMS
```
{release_notes}"""


def main() -> int:
    args = parse_args()
    replacements = parse_replacements(args.sanitize_root)
    published_tasks = [
        task
        for task in load_jsonl(args.published_tasks)
        if task_is_published_hard(task, args.published_difficulty)
    ]
    published_ids = {task["id"] for task in published_tasks}
    published_results = [
        result
        for result in load_jsonl(args.published_results)
        if result["id"] in published_ids
    ]
    published_oracle_map: dict[str, dict[str, Any]] = {}
    published_oracle_sources = []
    for path in args.published_oracles:
        published_oracle_sources.append(
            certify_published_oracle_source(path)
        )
        for oracle in load_jsonl(path):
            if oracle["id"] in published_ids:
                if oracle["id"] in published_oracle_map:
                    raise ValueError(
                        f"duplicate published Oracle ID {oracle['id']} "
                        f"in {path}"
                    )
                published_oracle_map[oracle["id"]] = oracle

    wave_tasks: list[dict[str, Any]] = []
    wave_oracles: list[dict[str, Any]] = []
    wave_results: list[dict[str, Any]] = []
    wave_counts = []
    wave_attestations: dict[Path, dict[str, Any]] = {}
    for wave in args.wave:
        wave_attestations[wave] = wave_validator.validate_summary(wave)
        tasks = load_jsonl(wave / "tasks.jsonl")
        oracles = load_jsonl(wave / "oracles.jsonl")
        results = load_jsonl(wave / "results.jsonl")
        wave_report = json.loads(
            (wave / "report.json").read_text(encoding="utf-8")
        )
        if (
            wave_report.get("complete") is not True
            or wave_report.get("tasks") != len(tasks)
            or wave_report.get("expected_task_model_pairs")
            != len(tasks) * len(MODELS)
        ):
            raise ValueError(f"{wave} does not have a complete wave report")
        invalid_hard = [
            task["id"] for task in tasks if not task_is_new_hard(task)
        ]
        if invalid_hard:
            raise ValueError(
                f"{wave} contains tasks without hard-selection evidence: "
                f"{invalid_hard[:10]}"
            )
        wave_tasks.extend(tasks)
        wave_oracles.extend(oracles)
        wave_results.extend(results)
        wave_counts.append((wave.name, len(tasks)))
        wave_task_map = {task["id"]: task for task in tasks}
        invalid = [
            oracle["id"]
            for oracle in oracles
            if oracle["id"] not in wave_task_map or not strict_pass(oracle)
        ]
        if invalid:
            raise ValueError(
                f"{wave} contains non-strict Oracle records: {invalid[:10]}"
            )
        for oracle in oracles:
            validate_oracle_identity(
                wave_task_map[oracle["id"]],
                oracle,
            )

    if set(published_oracle_map) != published_ids:
        missing = sorted(published_ids - set(published_oracle_map))
        raise ValueError(f"published tasks without an Oracle: {missing[:10]}")
    published_task_map = {task["id"]: task for task in published_tasks}
    for task_id, oracle in published_oracle_map.items():
        validate_published_oracle(published_task_map[task_id], oracle)
    if published_ids & {task["id"] for task in wave_tasks}:
        raise ValueError("published and new wave task IDs overlap")

    tasks = published_tasks + wave_tasks
    oracles = list(published_oracle_map.values()) + wave_oracles
    results = published_results + wave_results
    result_map = {
        (result["id"], result["model"]): result
        for result in results
    }
    if len(result_map) != len(results):
        raise ValueError("source results contain duplicate task-model keys")
    override_records = []
    for path in args.result_override:
        for override in load_jsonl(path):
            key = (override.get("id"), override.get("model"))
            if key not in result_map:
                raise ValueError(
                    f"result override {key} does not exist in source matrix"
                )
            result_map[key] = override
            override_records.append(override)
    results = list(result_map.values())
    published_results = [
        result
        for result in results
        if result["id"] in published_ids
    ]
    unique_by(tasks, ("id",), "task IDs")
    unique_by(oracles, ("id",), "Oracle IDs")
    unique_by(results, ("id", "model"), "task-model results")
    task_ids = {task["id"] for task in tasks}
    if {oracle["id"] for oracle in oracles} != task_ids:
        raise ValueError("task and Oracle ID sets differ")
    if {result["id"] for result in results} != task_ids:
        raise ValueError("task and result ID sets differ")
    if {result["model"] for result in results} != set(MODELS):
        raise ValueError("unexpected model set")
    if len(results) != len(tasks) * len(MODELS):
        raise ValueError("incomplete task-model result matrix")
    for result in results:
        validate_terminal_result(result)

    results = [{**result, "difficulty": "hard"} for result in results]
    published_results = [
        result
        for result in results
        if result["id"] in published_ids
    ]
    override_records = [
        {**record, "difficulty": "hard"}
        for record in override_records
    ]
    tasks = sorted(sanitize(tasks, replacements), key=lambda item: item["id"])
    oracles = sorted(sanitize(oracles, replacements), key=lambda item: item["id"])
    published_results = sorted(
        sanitize(published_results, replacements),
        key=lambda item: (item["id"], item["model"]),
    )
    override_records = sorted(
        sanitize(override_records, replacements),
        key=lambda item: (item["id"], item["model"]),
    )
    results = sorted(
        sanitize(results, replacements),
        key=lambda item: (item["id"], item["model"]),
    )
    sources = [
        f"published:{source_label(args.published_results)}",
        *(f"wave:{wave.name}" for wave in args.wave),
        *(
            f"correction:{source_label(path)}"
            for path in args.result_override
        ),
    ]
    report = build_report(results, sources)
    if report["tasks_missing_a_model"]:
        raise ValueError("some tasks are missing model results")

    prepare_output_dir(args.out_dir)
    write_jsonl(args.out_dir / "tasks.jsonl", tasks)
    write_jsonl(args.out_dir / "oracles.jsonl", oracles)
    write_jsonl(
        args.out_dir / "published-hard-results.jsonl",
        published_results,
    )
    write_jsonl(
        args.out_dir / "result-overrides.jsonl",
        override_records,
    )
    write_jsonl(args.out_dir / "results.jsonl", results)
    (args.out_dir / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    readme = build_readme(
        len(tasks),
        len(published_tasks),
        wave_counts,
        report,
        args.note,
    )
    (args.out_dir / "README.md").write_text(readme, encoding="utf-8")

    created_at = args.created_at or dt.datetime.now(dt.timezone.utc).isoformat()
    core_files = (
        "tasks.jsonl",
        "oracles.jsonl",
        "results.jsonl",
        "published-hard-results.jsonl",
        "result-overrides.jsonl",
        "report.json",
        "README.md",
    )
    manifest = {
        "schema_version": "reviewbench-hard-release/v1",
        "complete": True,
        "created_at": created_at,
        "tasks": len(tasks),
        "published_hard_tasks": len(published_tasks),
        "new_hard_tasks": len(wave_tasks),
        "task_model_pairs": len(results),
        "models": list(MODELS),
        "generation_protocol": "agentic-line-edit/v1",
        "maintainability_rubric": "v2 (human-golden calibrated)",
        "oracle_gate": ORACLE_GATE,
        "sources": {
            "published": {
                "tasks": {
                    "name": source_label(args.published_tasks),
                    "sha256": digest(args.published_tasks),
                },
                "results": {
                    "name": source_label(args.published_results),
                    "sha256": digest(args.published_results),
                },
                "oracles": published_oracle_sources,
            },
            "waves": [
                {
                    "name": wave.name,
                    "tasks": count,
                    "tasks_sha256": digest(wave / "tasks.jsonl"),
                    "oracles_sha256": digest(wave / "oracles.jsonl"),
                    "results_sha256": digest(wave / "results.jsonl"),
                    "report_sha256": digest(wave / "report.json"),
                    "attestation_sha256": digest(
                        wave / "summary.complete.json"
                    ),
                    "attestation_modes": wave_attestations[wave][
                        "attestation_modes"
                    ],
                    "summary_attestation_mode": wave_attestations[wave][
                        "summary_attestation_mode"
                    ],
                }
                for wave, (_, count) in zip(args.wave, wave_counts)
            ],
            "result_overrides": [
                {
                    "name": source_label(path),
                    "sha256": digest(path),
                }
                for path in args.result_override
            ],
        },
        "path_placeholders": sorted(
            {
                replacement
                for replacement in replacements.values()
            }
        ),
        "notes": args.note,
        "leaderboard": {
            model: {
                key: report["leaderboard"][model][key]
                for key in (
                    "tasks",
                    "generation_apply",
                    "scope_pass",
                    "correctness_pass",
                    "maintainability_pass",
                    "resolved",
                    "resolved_rate",
                )
            }
            for model in MODELS
        },
        "hashes": {
            name: digest(args.out_dir / name)
            for name in core_files
        },
        "artifacts": {
            name: digest(args.out_dir / name)
            for name in core_files
        },
    }
    (args.out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    checksum_files = (*core_files, "manifest.json")
    (args.out_dir / "SHA256SUMS").write_text(
        "".join(
            f"{digest(args.out_dir / name)}  {name}\n"
            for name in checksum_files
        ),
        encoding="utf-8",
    )

    for root in replacements:
        if not root.startswith("/"):
            continue
        for name in checksum_files:
            if root in (args.out_dir / name).read_text(
                encoding="utf-8",
                errors="replace",
            ):
                raise ValueError(f"unsanitized root {root!r} remains in {name}")

    print(
        json.dumps(
            {
                "tasks": len(tasks),
                "task_model_pairs": len(results),
                "out_dir": str(args.out_dir),
                "leaderboard": manifest["leaderboard"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
