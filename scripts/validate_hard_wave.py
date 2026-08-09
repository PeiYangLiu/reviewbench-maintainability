#!/usr/bin/env python3
"""Validate hard-wave artifact identity and stage canonical correctness."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any

import generate_agentic_patches as agentic
import judge_release_patches as judge
import summarize_release_evaluation as summarizer


LANES = ("gopy", "scripted", "tsjs", "csharp", "jvm", "rust", "native")
MODELS = ("claude-opus-4.8", "gemini-3.5-flash", "gpt-5.5")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        choices=("legacy-attest", "correctness", "ready", "summary"),
    )
    parser.add_argument("--wave", type=Path, required=True)
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


def ids_digest(values: list[tuple[str, ...]]) -> str:
    return hashlib.sha256(
        "".join("\t".join(value) + "\n" for value in sorted(values)).encode()
    ).hexdigest()


def generation_path(wave: Path, model: str) -> Path:
    return wave / "generation" / model / model / "generation_results.jsonl"


def generation_marker(path: Path) -> Path:
    return path.with_name("generation_complete.json")


def load_generations(
    wave: Path,
) -> dict[str, dict[str, dict[str, Any]]]:
    return {
        model: {
            record["id"]: record
            for record in load_jsonl(generation_path(wave, model))
        }
        for model in MODELS
    }


def validate_generation(
    wave: Path,
    task_ids: set[str],
) -> dict[str, dict[str, str]]:
    hashes = {}
    task_hash = ids_digest([(task_id,) for task_id in task_ids])
    tasks_sha256 = digest(wave / "tasks.jsonl")
    tasks_by_id = {
        task["id"]: task
        for task in load_jsonl(wave / "tasks.jsonl")
    }
    for model in MODELS:
        path = generation_path(wave, model)
        marker_path = generation_marker(path)
        if not path.is_file() or not marker_path.is_file():
            raise ValueError(f"incomplete generation artifacts for {model}")
        records = load_jsonl(path)
        keys = {(record.get("id"), record.get("model")) for record in records}
        expected = {(task_id, model) for task_id in task_ids}
        if len(records) != len(keys) or keys != expected:
            raise ValueError(f"generation key mismatch for {model}")
        records_by_id = {record["id"]: record for record in records}
        if any(
            record.get("task_fingerprint")
            != agentic.task_fingerprint(tasks_by_id[task_id])
            for task_id, record in records_by_id.items()
        ):
            raise ValueError(f"generation task fingerprint mismatch for {model}")
        for record in records:
            if record.get("apply_ok") is not True:
                continue
            patch = Path(str(record.get("patch_file") or ""))
            if (
                not patch.is_file()
                or record.get("patch_sha256") != digest(patch)
            ):
                raise ValueError(
                    f"generation patch fingerprint mismatch for "
                    f"{record['id']} {model}"
                )
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if marker.get("results_sha256") != digest(path):
            raise ValueError(f"stale generation marker for {model}")
        if marker.get("task_ids_sha256") != task_hash:
            raise ValueError(f"generation task-set mismatch for {model}")
        if marker.get("tasks_sha256") != tasks_sha256:
            raise ValueError(f"generation task-content mismatch for {model}")
        patch_hash = agentic.patch_set_digest(records_by_id)
        if marker.get("patches_sha256") != patch_hash:
            raise ValueError(f"generation patch-set mismatch for {model}")
        hashes[model] = {
            "results": digest(path),
            "patches": patch_hash,
        }
    return hashes


def task_lanes(wave: Path, task_ids: set[str]) -> dict[str, str]:
    result = {}
    for oracle in load_jsonl(wave / "oracles.jsonl"):
        task_id = str(oracle.get("id") or "")
        source = Path(str(oracle.get("lane_results_source") or ""))
        lane = source.parent.name
        if task_id not in task_ids or lane not in LANES:
            raise ValueError(
                f"cannot derive lane for {task_id}: "
                f"{oracle.get('lane_results_source')!r}"
            )
        if task_id in result:
            raise ValueError(f"duplicate Oracle ID {task_id}")
        result[task_id] = lane
    if set(result) != task_ids:
        raise ValueError("task and Oracle ID sets differ")
    return result


def correctness_patch_sha(record: dict[str, Any]) -> str:
    return str(
        record.get("patch_sha256")
        or record.get("candidate_patch_sha256")
        or (record.get("patch_policy") or {}).get("sha256")
        or ""
    )


def attest_legacy_wave(wave: Path) -> dict[str, Any]:
    tasks = load_jsonl(wave / "tasks.jsonl")
    tasks_by_id = {task["id"]: task for task in tasks}
    if len(tasks_by_id) != len(tasks):
        raise ValueError("duplicate task IDs")
    task_ids = set(tasks_by_id)
    tasks_sha256 = digest(wave / "tasks.jsonl")

    generations: dict[str, dict[str, dict[str, Any]]] = {}
    for model in MODELS:
        path = generation_path(wave, model)
        records = load_jsonl(path)
        keys = {(record.get("id"), record.get("model")) for record in records}
        expected = {(task_id, model) for task_id in task_ids}
        if len(records) != len(keys) or keys != expected:
            raise ValueError(f"generation key mismatch for {model}")
        for record in records:
            record["task_fingerprint"] = agentic.task_fingerprint(
                tasks_by_id[record["id"]]
            )
            if record.get("apply_ok") is True:
                patch = Path(str(record.get("patch_file") or ""))
                if not patch.is_file():
                    raise ValueError(
                        f"missing generated patch for {record['id']} {model}"
                    )
                record["patch_sha256"] = digest(patch)
        write_jsonl(path, records)
        generations[model] = {record["id"]: record for record in records}
        agentic.write_completion_marker(
            path,
            model,
            tasks_by_id,
            generations[model],
            tasks_sha256,
            "legacy-continuity",
        )

    judgment_path = wave / "judgments_v2.jsonl"
    judgments = load_jsonl(judgment_path)
    expected_pairs = {
        (task_id, model)
        for task_id in task_ids
        for model in MODELS
    }
    keys = {
        (record.get("id"), record.get("solver_model"))
        for record in judgments
    }
    if len(judgments) != len(keys) or keys != expected_pairs:
        raise ValueError("judgment key mismatch")
    for record in judgments:
        key = (record["id"], record["solver_model"])
        generation = generations[key[1]][key[0]]
        if record.get("status") == "judged":
            judged_patch = Path(str(record.get("patch_file") or ""))
            generated_patch = Path(str(generation.get("patch_file") or ""))
            if (
                not judged_patch.is_file()
                or not generated_patch.is_file()
                or digest(judged_patch) != digest(generated_patch)
            ):
                raise ValueError(
                    f"judgment patch mismatch for {key[0]} {key[1]}"
                )
        elif (
            record.get("status") == "generation_failed"
            and generation.get("apply_ok") is True
        ):
            raise ValueError(
                f"stale generation_failed judgment for {key[0]} {key[1]}"
            )
        if (
            record.get("status") == "judged"
            and (
                record.get("rubric") != "v2"
                or record.get("judge_model") != "claude-opus-4.8"
            )
        ):
            raise ValueError(
                f"unexpected judgment protocol for {key[0]} {key[1]}"
            )
        record["task_fingerprint"] = agentic.task_fingerprint(
            tasks_by_id[key[0]]
        )
        record["generation_fingerprint"] = judge.generation_fingerprint(
            generation
        )
        record["rubric"] = "v2"
        record["judge_model"] = "claude-opus-4.8"
        record["judge_protocol_fingerprint"] = (
            judge.judge_protocol_fingerprint(
                "v2",
                "claude-opus-4.8",
                30_000,
                None,
            )
        )
    write_jsonl(judgment_path, judgments)
    judgment_map = {
        (record["id"], record["solver_model"]): record
        for record in judgments
    }
    judge.write_completion_marker(
        judgment_path,
        {
            task_id: agentic.task_fingerprint(task)
            for task_id, task in tasks_by_id.items()
        },
        set(MODELS),
        judgment_map,
        tasks_sha256,
        judge.generation_set_digests(generations),
        {
            (task_id, model): judge.generation_fingerprint(record)
            for model, records in generations.items()
            for task_id, record in records.items()
        },
        judge.judge_protocol_fingerprint(
            "v2",
            "claude-opus-4.8",
            30_000,
            None,
        ),
        "v2",
        "claude-opus-4.8",
        30_000,
        None,
        "legacy-continuity",
    )
    return {
        "tasks": len(task_ids),
        "task_model_pairs": len(expected_pairs),
        "generation_sha256": {
            model: digest(generation_path(wave, model))
            for model in MODELS
        },
        "judgments_sha256": digest(judgment_path),
    }


def stage_correctness(wave: Path) -> dict[str, Any]:
    tasks = load_jsonl(wave / "tasks.jsonl")
    task_ids = {task["id"] for task in tasks}
    tasks_by_id = {task["id"]: task for task in tasks}
    if len(task_ids) != len(tasks):
        raise ValueError("duplicate task IDs")
    generation_hashes = validate_generation(wave, task_ids)
    generations = load_generations(wave)
    lanes_by_id = task_lanes(wave, task_ids)

    source_dir = wave / "correctness"
    stage = wave / "correctness-canonical"
    stage.mkdir(parents=True, exist_ok=True)
    unexpected = {
        path.name
        for path in stage.glob("*.jsonl")
    } - {f"{lane}.jsonl" for lane in LANES}
    if unexpected:
        raise ValueError(
            f"canonical correctness contains unexpected files: "
            f"{sorted(unexpected)}"
        )
    for lane in LANES:
        destination = stage / f"{lane}.jsonl"
        source = source_dir / f"{lane}.jsonl"
        expected_count = sum(
            1 for value in lanes_by_id.values() if value == lane
        ) * len(MODELS)
        actual_count = len(load_jsonl(source)) if source.is_file() else 0
        if actual_count != expected_count:
            raise ValueError(
                f"{lane} correctness count {actual_count} != {expected_count}"
            )
        if expected_count:
            shutil.copy2(source, destination)
        else:
            destination.write_text("", encoding="utf-8")

    parsed = summarizer.load_correctness(stage)
    expected_pairs = {
        (task_id, model)
        for task_id in task_ids
        for model in MODELS
    }
    if set(parsed) != expected_pairs:
        missing = sorted(expected_pairs - set(parsed))
        extra = sorted(set(parsed) - expected_pairs)
        raise ValueError(
            f"correctness key mismatch missing={missing[:5]} extra={extra[:5]}"
        )
    for (task_id, model), record in parsed.items():
        expected_source = f"{lanes_by_id[task_id]}.jsonl"
        if record.get("correctness_source") != expected_source:
            raise ValueError(
                f"{task_id} came from {record.get('correctness_source')}, "
                f"expected {expected_source}"
            )
        task = tasks_by_id[task_id]
        if record.get("start_sha") != task.get("start_sha"):
            raise ValueError(
                f"{task_id} correctness start revision mismatch"
            )
        generation = generations[model][task_id]
        if generation.get("apply_ok") is True:
            expected_patch = str(generation.get("patch_sha256") or "")
            if correctness_patch_sha(record) != expected_patch:
                raise ValueError(
                    f"{task_id} {model} correctness patch mismatch"
                )

    correctness_hashes = {
        lane: digest(stage / f"{lane}.jsonl")
        for lane in LANES
    }
    generation_modes = {
        model: json.loads(
            generation_marker(generation_path(wave, model)).read_text(
                encoding="utf-8"
            )
        ).get("attestation_mode")
        for model in MODELS
    }
    marker = {
        "schema_version": "reviewbench-correctness-complete/v1",
        "attestation_mode": (
            "legacy-continuity"
            if "legacy-continuity" in generation_modes.values()
            else "runtime-derived"
        ),
        "tasks": len(task_ids),
        "models": list(MODELS),
        "task_model_pairs": len(parsed),
        "tasks_sha256": digest(wave / "tasks.jsonl"),
        "oracles_sha256": digest(wave / "oracles.jsonl"),
        "pair_ids_sha256": ids_digest(
            [(task_id, model) for task_id, model in parsed]
        ),
        "generation_sha256": generation_hashes,
        "correctness_sha256": correctness_hashes,
    }
    (wave / "correctness.complete.json").write_text(
        json.dumps(marker, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return marker


def validate_ready(wave: Path) -> dict[str, Any]:
    tasks = load_jsonl(wave / "tasks.jsonl")
    task_ids = {task["id"] for task in tasks}
    generation_hashes = validate_generation(wave, task_ids)

    correctness_marker_path = wave / "correctness.complete.json"
    if not correctness_marker_path.is_file():
        raise ValueError("missing correctness completion marker")
    correctness = json.loads(
        correctness_marker_path.read_text(encoding="utf-8")
    )
    if correctness.get("generation_sha256") != generation_hashes:
        raise ValueError("correctness was produced from stale generation")
    if correctness.get("tasks_sha256") != digest(wave / "tasks.jsonl"):
        raise ValueError("correctness was produced from stale tasks")
    if correctness.get("oracles_sha256") != digest(wave / "oracles.jsonl"):
        raise ValueError("correctness was produced from stale Oracles")
    for lane in LANES:
        path = wave / "correctness-canonical" / f"{lane}.jsonl"
        if (
            not path.is_file()
            or correctness["correctness_sha256"].get(lane) != digest(path)
        ):
            raise ValueError(f"stale canonical correctness for {lane}")

    judgment_path = wave / "judgments_v2.jsonl"
    judgment_marker_path = wave / "judgments_v2.complete.json"
    if not judgment_path.is_file() or not judgment_marker_path.is_file():
        raise ValueError("missing judgment completion artifacts")
    judgments = load_jsonl(judgment_path)
    expected_pairs = {
        (task_id, model)
        for task_id in task_ids
        for model in MODELS
    }
    keys = {
        (record.get("id"), record.get("solver_model"))
        for record in judgments
    }
    if len(judgments) != len(keys) or keys != expected_pairs:
        raise ValueError("judgment key mismatch")
    if any(
        record.get("status")
        in {"judge_error", "input_error", "missing_patch"}
        for record in judgments
    ):
        raise ValueError("unresolved judgment errors")
    marker = json.loads(
        judgment_marker_path.read_text(encoding="utf-8")
    )
    if marker.get("results_sha256") != digest(judgment_path):
        raise ValueError("stale judgment completion marker")
    if marker.get("tasks_sha256") != digest(wave / "tasks.jsonl"):
        raise ValueError("judgments were produced from stale tasks")
    current_generations = load_generations(wave)
    if marker.get("generation_sha256") != judge.generation_set_digests(
        current_generations
    ):
        raise ValueError("judgments were produced from stale generation")
    expected_protocol = judge.judge_protocol_fingerprint(
        "v2",
        "claude-opus-4.8",
        30_000,
        marker.get("max_output_tokens"),
    )
    if (
        marker.get("judge_protocol_fingerprint") != expected_protocol
        or marker.get("rubric") != "v2"
        or marker.get("judge_model") != "claude-opus-4.8"
        or marker.get("max_context_chars") != 30_000
    ):
        raise ValueError("judgments use the wrong judge protocol")

    attestation_modes = {
        "generation": {
            model: json.loads(
                generation_marker(generation_path(wave, model)).read_text(
                    encoding="utf-8"
                )
            ).get("attestation_mode")
            for model in MODELS
        },
        "correctness": correctness.get("attestation_mode"),
        "judgments": marker.get("attestation_mode"),
    }
    return {
        "tasks": len(task_ids),
        "task_model_pairs": len(expected_pairs),
        "generation_sha256": generation_hashes,
        "correctness_sha256": correctness["correctness_sha256"],
        "judgments_sha256": digest(judgment_path),
        "attestation_modes": attestation_modes,
    }


def validate_summary(wave: Path) -> dict[str, Any]:
    ready = validate_ready(wave)
    result_path = wave / "results.jsonl"
    report_path = wave / "report.json"
    marker_path = wave / "summary.complete.json"
    if (
        not result_path.is_file()
        or not report_path.is_file()
        or not marker_path.is_file()
    ):
        raise ValueError("missing summary completion artifacts")
    validate_summary_outputs(wave, ready)
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    ready_hash = hashlib.sha256(
        json.dumps(ready, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if (
        marker.get("ready_sha256") != ready_hash
        or marker.get("results_sha256") != digest(result_path)
        or marker.get("report_sha256") != digest(report_path)
    ):
        raise ValueError("stale summary completion marker")
    return {
        **ready,
        "results_sha256": digest(result_path),
        "report_sha256": digest(report_path),
        "summary_attestation_mode": marker.get("attestation_mode"),
    }


def write_summary_marker(wave: Path) -> dict[str, Any]:
    ready = validate_ready(wave)
    result_path = wave / "results.jsonl"
    report_path = wave / "report.json"
    validate_summary_outputs(wave, ready)
    marker = {
        "schema_version": "reviewbench-summary-complete/v1",
        "ready_sha256": hashlib.sha256(
            json.dumps(ready, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "results_sha256": digest(result_path),
        "report_sha256": digest(report_path),
        "attestation_mode": (
            "legacy-continuity"
            if "legacy-continuity"
            in json.dumps(ready.get("attestation_modes"), sort_keys=True)
            else "runtime-derived"
        ),
    }
    (wave / "summary.complete.json").write_text(
        json.dumps(marker, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return validate_summary(wave)


def validate_summary_outputs(
    wave: Path,
    ready: dict[str, Any],
) -> None:
    results = load_jsonl(wave / "results.jsonl")
    tasks = load_jsonl(wave / "tasks.jsonl")
    expected = {
        (task["id"], model)
        for task in tasks
        for model in MODELS
    }
    keys = {(record.get("id"), record.get("model")) for record in results}
    if len(results) != len(keys) or keys != expected:
        raise ValueError("summary result key mismatch")
    report = json.loads(
        (wave / "report.json").read_text(encoding="utf-8")
    )
    expected_results, expected_report = summarizer.build_evaluation(
        tasks,
        {
            model: generation_path(wave, model)
            for model in MODELS
        },
        wave / "correctness-canonical",
        wave / "judgments_v2.jsonl",
    )
    if results != expected_results:
        raise ValueError("summary results do not match upstream artifacts")
    if report != expected_report:
        raise ValueError("summary report does not match upstream artifacts")


def main() -> int:
    args = parse_args()
    actions = {
        "legacy-attest": attest_legacy_wave,
        "correctness": stage_correctness,
        "ready": validate_ready,
        "summary": write_summary_marker,
    }
    result = actions[args.mode](args.wave)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
