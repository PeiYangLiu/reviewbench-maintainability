import importlib.util
import json
from pathlib import Path
import sys

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "package_hard_release.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("package_hard_release", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )


def result(task_id, model, resolved):
    return {
        "id": task_id,
        "repo": "owner/repo",
        "model": model,
        "language": "Python",
        "category": "modularity",
        "difficulty": "hard",
        "generation_protocol": "agentic-line-edit/v1",
        "generation_status": "generated",
        "generation_apply": True,
        "scope_ok": True,
        "correctness_ok": True,
        "correctness_status": "passed",
        "maintainability_pass": resolved,
        "judge_status": "judged",
        "resolved": resolved,
        "failed_tests": [],
        "failure_reasons": [],
        "pass_to_pass_total": 1,
        "pass_to_pass_passed": 1,
    }


def test_packages_portable_hard_release(tmp_path, monkeypatch):
    models = MODULE.MODELS
    published_tasks = tmp_path / "published-tasks.jsonl"
    published_results = tmp_path / "published-results.jsonl"
    published_oracles = tmp_path / "published-oracles.jsonl"
    published_manifest = tmp_path / "manifest.json"
    override = tmp_path / "result-override.jsonl"
    wave = tmp_path / "wave-001"
    out = tmp_path / "release"
    local_root = "/data/private/session"

    write_jsonl(
        published_tasks,
        [
            {
                "id": "published-hard",
                "repo": "owner/repo",
                "release_validation": {
                    "judged_difficulty": "hard",
                    "golden_correctness": True,
                    "pass_to_pass": 1,
                    "changed_lines_covered": True,
                },
            },
            {
                "id": "published-medium",
                "repo": "owner/repo",
                "release_validation": {"judged_difficulty": "medium"},
            },
        ],
    )
    write_jsonl(
        published_results,
        [
            *(result("published-hard", model, True) for model in models),
            *(result("published-medium", model, True) for model in models),
        ],
    )
    write_jsonl(
        published_oracles,
        [
            {
                "id": "published-hard",
                "golden_correctness_ok": True,
                "pass_to_pass": 1,
                "changed_lines_covered": True,
                "command": f"{local_root}/run",
            }
        ],
    )
    published_manifest.write_text(
        json.dumps(
            {
                "complete": True,
                "artifacts": {
                    published_oracles.name: MODULE.digest(published_oracles)
                },
            }
        )
    )
    write_jsonl(
        wave / "tasks.jsonl",
        [
            {
                "id": "new-hard",
                "repo": "owner/new",
                "hard_selection": {
                    "gpt_strict_pass": True,
                    "gpt_hard": True,
                },
            }
        ],
    )
    write_jsonl(
        wave / "oracles.jsonl",
        [
            {
                "id": "new-hard",
                "status": "strict_pass",
                "golden_correctness_ok": True,
                "triple_run_stable": True,
                "stable_p2p": True,
                "pass_to_pass": 1,
                "changed_lines_covered": True,
                "artifacts": f"{local_root}/artifacts",
            }
        ],
    )
    write_jsonl(
        wave / "results.jsonl",
        [result("new-hard", model, model != "gemini-3.5-flash") for model in models],
    )
    (wave / "report.json").write_text(
        json.dumps(
            {
                "complete": True,
                "tasks": 1,
                "expected_task_model_pairs": 3,
            }
        )
    )
    write_jsonl(
        override,
        [result("new-hard", "gemini-3.5-flash", True)],
    )
    monkeypatch.setattr(
        MODULE.wave_validator,
        "validate_summary",
        lambda path: {
            "tasks": 1,
            "task_model_pairs": 3,
            "attestation_modes": {
                "generation": {},
                "correctness": "runtime-derived",
                "judgments": "runtime",
            },
            "summary_attestation_mode": "runtime-derived",
        },
    )
    (wave / "summary.complete.json").write_text("{}\n")

    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--published-tasks",
            str(published_tasks),
            "--published-results",
            str(published_results),
            "--published-oracles",
            str(published_oracles),
            "--wave",
            str(wave),
            "--out-dir",
            str(out),
            "--result-override",
            str(override),
            "--sanitize-root",
            f"REVIEWBENCH_SESSION_FILES={local_root}",
            "--created-at",
            "2026-08-08T00:00:00+00:00",
        ],
    )
    assert MODULE.main() == 0

    tasks = MODULE.load_jsonl(out / "tasks.jsonl")
    oracles = MODULE.load_jsonl(out / "oracles.jsonl")
    results = MODULE.load_jsonl(out / "results.jsonl")
    manifest = json.loads((out / "manifest.json").read_text())
    assert {task["id"] for task in tasks} == {"published-hard", "new-hard"}
    assert len(oracles) == 2
    assert len(results) == 6
    assert all(result["resolved"] for result in results)
    assert {result["difficulty"] for result in results} == {"hard"}
    assert manifest["tasks"] == 2
    assert manifest["published_hard_tasks"] == 1
    assert manifest["new_hard_tasks"] == 1
    assert "${REVIEWBENCH_SESSION_FILES}" in (out / "oracles.jsonl").read_text()
    assert local_root not in "".join(
        path.read_text(errors="replace")
        for path in out.iterdir()
        if path.is_file()
    )
    assert len((out / "SHA256SUMS").read_text().splitlines()) == 8
    certification = MODULE.certify_published_oracle_source(
        out / "oracles.jsonl"
    )
    assert certification["oracles_sha256"] == MODULE.digest(
        out / "oracles.jsonl"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("generation_status", "generation_api_timeout"),
        ("judge_status", "judge_error"),
        ("correctness_status", "missing"),
        (
            "correctness_status",
            "post-failure exact-start baseline did not pass; retry infrastructure",
        ),
    ],
)
def test_rejects_unresolved_infrastructure_result(field, value):
    row = result("task", "gpt-5.5", False)
    row[field] = value
    with pytest.raises(ValueError, match="unresolved|terminal"):
        MODULE.validate_terminal_result(row)


def test_rejects_unexpected_output_files(tmp_path):
    out = tmp_path / "release"
    out.mkdir()
    (out / "stale.log").write_text("machine-specific path")
    with pytest.raises(ValueError, match="unexpected files"):
        MODULE.prepare_output_dir(out)


def test_published_oracle_requires_its_own_gate_evidence():
    task = {
        "id": "task",
        "release_validation": {
            "golden_correctness": True,
            "pass_to_pass": 1,
            "changed_lines_covered": True,
        },
    }
    with pytest.raises(ValueError, match="Oracle lacks"):
        MODULE.validate_published_oracle(task, {"id": "task"})


def test_failed_generation_cannot_pass_downstream_gates():
    row = result("task", "gpt-5.5", False)
    row.update(
        {
            "generation_status": "failed",
            "generation_apply": False,
            "scope_ok": True,
            "correctness_ok": True,
            "maintainability_pass": True,
            "judge_status": "generation_failed",
        }
    )
    with pytest.raises(ValueError, match="downstream passes"):
        MODULE.validate_terminal_result(row)
