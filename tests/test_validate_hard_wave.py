import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "validate_hard_wave.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("validate_hard_wave", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )


def prepare_wave(tmp_path):
    wave = tmp_path / "wave"
    task_id = "task"
    task = {"id": task_id, "start_sha": "start"}
    write_jsonl(wave / "tasks.jsonl", [task])
    write_jsonl(
        wave / "oracles.jsonl",
        [
            {
                "id": task_id,
                "lane_results_source": "/session/gopy/results.jsonl",
            }
        ],
    )
    task_hash = MODULE.ids_digest([(task_id,)])
    tasks_sha256 = MODULE.digest(wave / "tasks.jsonl")
    for model in MODULE.MODELS:
        path = MODULE.generation_path(wave, model)
        patch = path.parent / "candidate.patch"
        patch.parent.mkdir(parents=True, exist_ok=True)
        patch.write_text(f"{model} patch")
        record = {
            "id": task_id,
            "model": model,
            "status": "generated",
            "apply_ok": True,
            "task_fingerprint": MODULE.agentic.task_fingerprint(task),
            "patch_file": str(patch),
            "patch_sha256": MODULE.digest(patch),
        }
        write_jsonl(
            path,
            [record],
        )
        MODULE.generation_marker(path).write_text(
            json.dumps(
                {
                    "results_sha256": MODULE.digest(path),
                    "task_ids_sha256": task_hash,
                    "tasks_sha256": tasks_sha256,
                    "patches_sha256": MODULE.agentic.patch_set_digest(
                        {task_id: record}
                    ),
                }
            )
        )
    for lane in MODULE.LANES:
        rows = []
        if lane == "gopy":
            rows = [
                {
                    "id": task_id,
                    "model": model,
                    "patch_apply": True,
                    "correctness_ok": True,
                    "start_sha": "start",
                    "patch_sha256": MODULE.digest(
                        MODULE.generation_path(wave, model).parent
                        / "candidate.patch"
                    ),
                }
                for model in MODULE.MODELS
            ]
        write_jsonl(wave / "correctness" / f"{lane}.jsonl", rows)
    return wave


def test_stages_and_validates_exact_wave(tmp_path):
    wave = prepare_wave(tmp_path)
    marker = MODULE.stage_correctness(wave)
    assert marker["task_model_pairs"] == 3
    assert set((wave / "correctness-canonical").glob("*.jsonl")) == {
        wave / "correctness-canonical" / f"{lane}.jsonl"
        for lane in MODULE.LANES
    }

    judgments = [
        {
            "id": "task",
            "solver_model": model,
            "status": "judged",
            "task_fingerprint": MODULE.agentic.task_fingerprint(
                {"id": "task", "start_sha": "start"}
            ),
            "generation_fingerprint": MODULE.judge.generation_fingerprint(
                MODULE.load_generations(wave)[model]["task"]
            ),
            "rubric": "v2",
            "judge_model": "claude-opus-4.8",
            "judge_protocol_fingerprint": (
                MODULE.judge.judge_protocol_fingerprint(
                    "v2",
                    "claude-opus-4.8",
                    30_000,
                    8192,
                )
            ),
        }
        for model in MODULE.MODELS
    ]
    judgment_path = wave / "judgments_v2.jsonl"
    write_jsonl(judgment_path, judgments)
    (wave / "judgments_v2.complete.json").write_text(
        json.dumps(
            {
                "results_sha256": MODULE.digest(judgment_path),
                "tasks_sha256": MODULE.digest(wave / "tasks.jsonl"),
                "generation_sha256": MODULE.judge.generation_set_digests(
                    MODULE.load_generations(wave)
                ),
                "judge_protocol_fingerprint": (
                    MODULE.judge.judge_protocol_fingerprint(
                        "v2",
                        "claude-opus-4.8",
                        30_000,
                        8192,
                    )
                ),
                "rubric": "v2",
                "judge_model": "claude-opus-4.8",
                "max_context_chars": 30_000,
                "max_output_tokens": 8192,
                "attestation_mode": "runtime",
            }
        )
    )
    ready = MODULE.validate_ready(wave)
    assert ready["tasks"] == 1
    assert ready["task_model_pairs"] == 3

    results, report = MODULE.summarizer.build_evaluation(
        MODULE.load_jsonl(wave / "tasks.jsonl"),
        {
            model: MODULE.generation_path(wave, model)
            for model in MODULE.MODELS
        },
        wave / "correctness-canonical",
        judgment_path,
    )
    write_jsonl(wave / "results.jsonl", results)
    (wave / "report.json").write_text(json.dumps(report))
    summary = MODULE.write_summary_marker(wave)
    assert summary["results_sha256"] == MODULE.digest(
        wave / "results.jsonl"
    )


def test_attest_migrates_and_marks_legacy_wave(tmp_path):
    wave = prepare_wave(tmp_path)
    judgments = []
    for model in MODULE.MODELS:
        generation = MODULE.load_generations(wave)[model]["task"]
        judgments.append(
            {
                "id": "task",
                "solver_model": model,
                "judge_model": "claude-opus-4.8",
                "rubric": "v2",
                "status": "judged",
                "patch_file": generation["patch_file"],
            }
        )
    write_jsonl(wave / "judgments_v2.jsonl", judgments)
    result = MODULE.attest_legacy_wave(wave)
    assert result["task_model_pairs"] == 3
    assert (wave / "judgments_v2.complete.json").is_file()
    for record in MODULE.load_jsonl(wave / "judgments_v2.jsonl"):
        assert record["task_fingerprint"]
        assert record["generation_fingerprint"]
        assert record["judge_protocol_fingerprint"]


def test_ready_rejects_generation_changed_after_correctness(tmp_path):
    wave = prepare_wave(tmp_path)
    MODULE.stage_correctness(wave)
    path = MODULE.generation_path(wave, "gpt-5.5")
    path.write_text(path.read_text() + "\n")
    with pytest.raises(ValueError, match="stale generation marker"):
        MODULE.validate_ready(wave)


def test_generation_rejects_duplicate_task_model_rows(tmp_path):
    wave = prepare_wave(tmp_path)
    path = MODULE.generation_path(wave, "gpt-5.5")
    path.write_text(path.read_text() + path.read_text())
    with pytest.raises(ValueError, match="generation key mismatch"):
        MODULE.stage_correctness(wave)


def test_correctness_rejects_wrong_task_model_set(tmp_path):
    wave = prepare_wave(tmp_path)
    path = wave / "correctness" / "gopy.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0]["id"] = "wrong-task"
    write_jsonl(path, rows)
    with pytest.raises(ValueError, match="key mismatch"):
        MODULE.stage_correctness(wave)


def test_correctness_rejects_extra_canonical_jsonl(tmp_path):
    wave = prepare_wave(tmp_path)
    stage = wave / "correctness-canonical"
    stage.mkdir()
    (stage / "backup.jsonl").write_text("{}\n")
    with pytest.raises(ValueError, match="unexpected files"):
        MODULE.stage_correctness(wave)


def test_ready_rejects_duplicate_judgment_rows(tmp_path):
    wave = prepare_wave(tmp_path)
    MODULE.stage_correctness(wave)
    judgments = [
        {
            "id": "task",
            "solver_model": model,
            "status": "judged",
        }
        for model in MODULE.MODELS
    ]
    judgments.append(dict(judgments[0]))
    path = wave / "judgments_v2.jsonl"
    write_jsonl(path, judgments)
    (wave / "judgments_v2.complete.json").write_text(
        json.dumps(
            {
                "results_sha256": MODULE.digest(path),
                "tasks_sha256": MODULE.digest(wave / "tasks.jsonl"),
                "generation_sha256": MODULE.judge.generation_set_digests(
                    MODULE.load_generations(wave)
                ),
                "judge_protocol_fingerprint": (
                    MODULE.judge.judge_protocol_fingerprint(
                        "v2",
                        "claude-opus-4.8",
                        30_000,
                        8192,
                    )
                ),
                "rubric": "v2",
                "judge_model": "claude-opus-4.8",
                "max_context_chars": 30_000,
                "max_output_tokens": 8192,
                "attestation_mode": "runtime",
            }
        )
    )
    with pytest.raises(ValueError, match="judgment key mismatch"):
        MODULE.validate_ready(wave)
