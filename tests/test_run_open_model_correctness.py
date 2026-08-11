import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).parents[1]
    / "scripts"
    / "run_open_model_correctness.py"
)
SPEC = importlib.util.spec_from_file_location(
    "run_open_model_correctness",
    SCRIPT,
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def strict_oracle(task_id, source="/session/gopy/results.jsonl"):
    return {
        "id": task_id,
        "repo": "owner/repo",
        "start_sha": "start",
        "target_sha": "target",
        "pr_number": 1,
        "golden_correctness_ok": True,
        "pass_to_pass": 1,
        "changed_lines_covered": True,
        "lane_results_source": source,
    }


def task(task_id="task", language="Python"):
    return {
        "id": task_id,
        "repo": "owner/repo",
        "start_sha": "start",
        "target_sha": "target",
        "pr_number": 1,
        "inferred_language": language,
        "locality": {"path": "source.py"},
    }


def test_model_parser_accepts_hugging_face_ids_and_rejects_duplicates(
    tmp_path,
):
    path = tmp_path / "generation_results.jsonl"
    parsed = MODULE.parse_models([f"Qwen/Qwen3-Coder-Next-FP8={path}"])
    assert parsed == {"Qwen/Qwen3-Coder-Next-FP8": path.resolve()}
    with pytest.raises(ValueError, match="duplicate model key"):
        MODULE.parse_models([f"Qwen/Qwen={path}", f"Qwen/Qwen={path}"])


def test_load_release_rejects_duplicate_ids_and_accepts_count_object(
    tmp_path,
):
    tasks_path = tmp_path / "tasks.jsonl"
    oracles_path = tmp_path / "oracles.jsonl"
    write_jsonl(tasks_path, [task()])
    oracle = strict_oracle("task")
    oracle["pass_to_pass"] = {"count": 1}
    write_jsonl(oracles_path, [oracle])
    tasks, oracles, _, _ = MODULE.load_release(tasks_path, oracles_path)
    assert len(tasks) == len(oracles) == 1
    write_jsonl(tasks_path, [task(), task()])
    with pytest.raises(ValueError, match="duplicate task ID"):
        MODULE.load_release(tasks_path, oracles_path)


def test_task_plan_assigns_modern_and_published_tasks():
    modern = task("modern", "Python")
    legacy = task("legacy", "Rust")
    oracles = {
        "modern": strict_oracle("modern"),
        "legacy": {
            **strict_oracle("legacy", source=""),
            "language": "rust",
        },
    }
    plan = MODULE.build_task_plan([modern, legacy], oracles)
    assert [(row["lane"], row["source_kind"]) for row in plan] == [
        ("gopy", "modern"),
        ("rust", "published_legacy"),
    ]
    assert plan[1]["runner_ready"] is False


def test_generation_staging_validates_exact_pairs_and_hashes(tmp_path):
    model = "Qwen/Qwen2.5-Coder-7B-Instruct"
    item = task()
    tasks_path = tmp_path / "tasks.jsonl"
    write_jsonl(tasks_path, [item])
    model_root = tmp_path / "source"
    patch = model_root / "patches/task.patch"
    trace = model_root / "traces/task.json"
    patch.parent.mkdir(parents=True)
    trace.parent.mkdir(parents=True)
    patch.write_text("candidate patch", encoding="utf-8")
    trace.write_text("[]\n", encoding="utf-8")
    record = {
        "id": "task",
        "model": model,
        "start_sha": "start",
        "task_fingerprint": MODULE.task_fingerprint(item),
        "protocol": "agentic-line-edit/v1",
        "status": "generated",
        "apply_ok": True,
        "patch_file": "/stale/cluster/path.patch",
        "patch_sha256": MODULE.sha256(patch),
        "trace_file": "/stale/cluster/trace.json",
    }
    results = model_root / "generation_results.jsonl"
    write_jsonl(results, [record])
    patch_set = hashlib.sha256(
        f"task\t{MODULE.sha256(patch)}\n".encode()
    ).hexdigest()
    MODULE.generation_marker(results).write_text(
        json.dumps(
            {
                "model": model,
                "tasks": 1,
                "tasks_sha256": MODULE.sha256(tasks_path),
                "task_ids_sha256": hashlib.sha256(b"task\n").hexdigest(),
                "results_sha256": MODULE.sha256(results),
                "patches_sha256": patch_set,
            }
        ),
        encoding="utf-8",
    )
    staged, manifest = MODULE.validate_and_stage_model(
        model=model,
        results_path=results,
        tasks=[item],
        tasks_path=tasks_path,
        destination=tmp_path / "staged",
        require_marker=True,
    )
    assert Path(staged["task"]["patch_file"]).read_text() == "candidate patch"
    assert Path(staged["task"]["trace_file"]).read_text() == "[]\n"
    assert manifest["generated"] == 1

    write_jsonl(results, [record, record])
    with pytest.raises(ValueError, match="duplicate generation pair"):
        MODULE.validate_and_stage_model(
            model=model,
            results_path=results,
            tasks=[item],
            tasks_path=tasks_path,
            destination=tmp_path / "duplicate",
            require_marker=False,
        )


def test_staging_recovers_failed_record_metadata_from_fingerprint(tmp_path):
    model = "model"
    item = task()
    tasks_path = tmp_path / "tasks.jsonl"
    write_jsonl(tasks_path, [item])
    results = tmp_path / "source/generation_results.jsonl"
    write_jsonl(
        results,
        [
            {
                "id": item["id"],
                "model": model,
                "start_sha": None,
                "task_fingerprint": MODULE.task_fingerprint(item),
                "status": "failed",
                "apply_ok": False,
                "error": "serialization failed",
            }
        ],
    )

    staged, _ = MODULE.validate_and_stage_model(
        model=model,
        results_path=results,
        tasks=[item],
        tasks_path=tasks_path,
        destination=tmp_path / "staged",
        require_marker=False,
    )

    assert staged["task"]["start_sha"] == "start"
    assert staged["task"]["path"] == "source.py"
    assert staged["task"]["metadata_recovered_from_task_fingerprint"] is True


def test_scripted_runner_adaptation_uses_dynamic_models_and_task_count():
    source = """\
import json
from pathlib import Path
MODELS = ("claude-opus-4.8", "gemini-3.5-flash", "gpt-5.5")
class Replay:
    def validate_inputs(self, node_bin):
        if len(self.tasks) != 35:
            raise RuntimeError(f"expected 35 scripted tasks, found {len(self.tasks)}")
"""
    adapted, _ = MODULE.adapt_runner_source("scripted", source)
    assert 'MODELS = tuple(_OPEN_CONFIG["models"])' in adapted
    assert "len(self.tasks) != EXPECTED_TASKS" in adapted
    assert "claude-opus-4.8" not in adapted


def test_config_is_injected_before_csharp_models():
    source = """\
import json
from pathlib import Path
MODELS = ("claude-opus-4.8", "gemini-3.5-flash", "gpt-5.5")
ROOT = Path(__file__).resolve().parent
CSHARP = BASE / "csharp"
REPOS = SESSION / "repos"
EXPECTED_TASKS = 5
x = f"rb-csharp-w3-a"
y = f"rb-csharp-w3-b"
"""
    adapted, _ = MODULE.adapt_runner_source("csharp", source)
    assert adapted.index("_OPEN_CONFIG =") < adapted.index("MODELS =")


def test_rust_runner_adaptation_replaces_completion_count():
    source_path = (
        Path(
            "/data/v-peiyangliu/reviewbench-session-"
            "9a9f50f0-cb30-462e-942e-1e9def267f6d/"
            "hard_oracle_20260804/wave-001/correctness/run_rust.py"
        )
    )
    if not source_path.is_file():
        pytest.skip("proven Rust runner is not available")
    adapted, _ = MODULE.adapt_runner_source(
        "rust",
        source_path.read_text(encoding="utf-8"),
    )
    assert 'len(rows) == EXPECTED_RECORDS' in adapted
    assert 'len(rows) == 42' not in adapted
    assert "remove_pair_state(pair_state)" in adapted
    assert 'cargo_home = pair_state / "cargo-home"' in adapted
    assert "root-owned state cleanup failed" in adapted


def test_gopy_runner_adaptation_preserves_oracle_runtimes():
    source_path = Path(
        "/data/v-peiyangliu/reviewbench-session-"
        "9a9f50f0-cb30-462e-942e-1e9def267f6d/"
        "hard_oracle_20260804/wave-001/correctness/"
        "gopy_candidate_replay.py"
    )
    if not source_path.is_file():
        pytest.skip("proven Go/Python runner is not available")

    adapted, _ = MODULE.adapt_runner_source(
        "gopy",
        source_path.read_text(encoding="utf-8"),
    )

    assert "GO_1230 = LANE / 'toolchains/go1.23.0/bin/go'" in adapted
    assert "'go 1.24.2' in description" in adapted
    assert "if 'venv/bin/python' not in command[0]" in adapted
    assert "Path('/tmp/reviewbench-gopy')" in adapted
    assert "oracle-remapped.sh" in adapted


def test_native_runner_restores_release_placeholders():
    source_path = Path(
        "/data/v-peiyangliu/reviewbench-session-"
        "9a9f50f0-cb30-462e-942e-1e9def267f6d/"
        "hard_oracle_20260804/wave-001/correctness/native_runner.py"
    )
    if not source_path.is_file():
        pytest.skip("proven native runner is not available")

    adapted, _ = MODULE.adapt_runner_source(
        "native",
        source_path.read_text(encoding="utf-8"),
    )

    assert '"${REVIEWBENCH_SESSION_FILES}"' in adapted
    assert '_OPEN_CONFIG["session_root"]' in adapted
    assert 'strict = {**strict, "commands": commands}' in adapted
    assert "conan profile detect --force" in adapted
    assert '"submodule", "deinit", "--all", "--force"' in adapted


def test_modern_generation_artifacts_stay_inside_wave(tmp_path):
    model = "Qwen/Qwen"
    source = tmp_path / "source"
    patch = source / "patch.patch"
    trace = source / "trace.json"
    source.mkdir()
    patch.write_text("patch")
    trace.write_text("[]")
    staged = MODULE.stage_modern_generations(
        modern_root=tmp_path / "modern",
        tasks=[task()],
        models=[model],
        generations={
            model: {
                "task": {
                    "id": "task",
                    "model": model,
                    "apply_ok": True,
                    "patch_file": str(patch),
                    "trace_file": str(trace),
                }
            }
        },
    )
    for field in ("patch_file", "trace_file"):
        Path(staged[model]["task"][field]).relative_to(
            (tmp_path / "modern").resolve()
        )


def test_normalization_keeps_infra_retry_distinct_and_embeds_raw_record():
    raw = {
        "id": "task",
        "model": "model",
        "status": "INFRA_RETRY_REQUIRED",
        "retryable": True,
        "failure_root_cause": "temporary cache lock",
    }
    normalized = MODULE.normalized_record(
        "gopy",
        raw,
        raw_source="modern/correctness/gopy.jsonl",
        source_kind="modern",
    )
    assert normalized["status"] == "infra_retry"
    assert normalized["retryable"] is True
    assert normalized["raw_record"] == raw


def prepare_finalize_fixture(tmp_path):
    output = tmp_path / "output"
    tasks_path = tmp_path / "tasks.jsonl"
    oracles_path = tmp_path / "oracles.jsonl"
    item = task()
    write_jsonl(tasks_path, [item])
    write_jsonl(oracles_path, [strict_oracle("task")])
    model = "Qwen/Qwen"
    MODULE.atomic_jsonl(
        output / "task-plan.jsonl",
        [
            {
                "id": "task",
                "lane": "gopy",
                "source_kind": "modern",
            }
        ],
    )
    atomic_input = {
        "models": [model],
        "tasks_sha256": MODULE.sha256(tasks_path),
        "oracles_sha256": MODULE.sha256(oracles_path),
        "task_plan_sha256": MODULE.sha256(output / "task-plan.jsonl"),
    }
    MODULE.atomic_json(output / "input-manifest.json", atomic_input)
    MODULE.atomic_json(
        output / "modern/correctness/runner-provenance.json",
        {"schema_version": "test"},
    )
    patch_hash = "a" * 64
    MODULE.atomic_jsonl(
        output
        / "inputs/models"
        / MODULE.safe_component(model)
        / "generation_results.normalized.jsonl",
        [
            {
                "id": "task",
                "model": model,
                "start_sha": "start",
                "status": "generated",
                "apply_ok": True,
                "patch_sha256": patch_hash,
            }
        ],
    )
    raw = {
        "id": "task",
        "model": model,
        "start_sha": "start",
        "status": "PASS",
        "patch_apply": True,
        "scope_ok": True,
        "correctness_ok": True,
        "patch_sha256": patch_hash,
        "pass_to_pass_passed": 1,
        "pass_to_pass_total": 1,
        "failed_tests": [],
        "artifacts": {"candidate_log": "gopy-artifacts/task/candidate.log"},
    }
    write_jsonl(output / "modern/correctness/gopy.jsonl", [raw])
    return output, tasks_path, oracles_path


def test_finalize_requires_exact_unique_pairs_and_writes_manifest(tmp_path):
    output, tasks_path, oracles_path = prepare_finalize_fixture(tmp_path)
    manifest = MODULE.finalize(
        tasks_path=tasks_path,
        oracles_path=oracles_path,
        output=output,
        allow_incomplete=False,
    )
    assert manifest["complete"] is True
    assert manifest["unique_task_model_pairs"] == 1
    normalized = MODULE.read_jsonl(
        output / "correctness-normalized/gopy.jsonl"
    )
    assert normalized[0]["raw_record"]["artifacts"]["candidate_log"]
    assert (output / "correctness-manifest.json").is_file()
    assert (output / "SHA256SUMS").is_file()


def test_finalize_sorts_missing_pairs_across_lanes(tmp_path):
    output, tasks_path, oracles_path = prepare_finalize_fixture(tmp_path)
    model = "Qwen/Qwen"
    extra_tasks = [task("z-task", "Java"), task("a-task", "Rust")]
    write_jsonl(tasks_path, [task(), *extra_tasks])
    write_jsonl(
        oracles_path,
        [
            strict_oracle("task"),
            strict_oracle("z-task", "/session/jvm/results.jsonl"),
            strict_oracle("a-task", "/session/rust/results.jsonl"),
        ],
    )
    MODULE.atomic_jsonl(
        output / "task-plan.jsonl",
        [
            {"id": "task", "lane": "gopy", "source_kind": "modern"},
            {"id": "z-task", "lane": "jvm", "source_kind": "modern"},
            {"id": "a-task", "lane": "rust", "source_kind": "modern"},
        ],
    )
    manifest_path = output / "input-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.update(
        {
            "tasks_sha256": MODULE.sha256(tasks_path),
            "oracles_sha256": MODULE.sha256(oracles_path),
            "task_plan_sha256": MODULE.sha256(output / "task-plan.jsonl"),
        }
    )
    MODULE.atomic_json(manifest_path, manifest)
    generation_path = (
        output
        / "inputs/models"
        / MODULE.safe_component(model)
        / "generation_results.normalized.jsonl"
    )
    generations = MODULE.read_jsonl(generation_path)
    generations.extend(
        {
            "id": task_id,
            "model": model,
            "start_sha": "start",
            "status": "failed",
            "apply_ok": False,
            "patch_sha256": None,
        }
        for task_id in ("z-task", "a-task")
    )
    MODULE.atomic_jsonl(generation_path, generations)

    result = MODULE.finalize(
        tasks_path=tasks_path,
        oracles_path=oracles_path,
        output=output,
        allow_incomplete=True,
    )

    assert result["missing_pairs"] == [
        {"id": "a-task", "model": model},
        {"id": "z-task", "model": model},
    ]


def test_refinalize_keeps_checksums_current(tmp_path):
    output, tasks_path, oracles_path = prepare_finalize_fixture(tmp_path)
    MODULE.finalize(
        tasks_path=tasks_path,
        oracles_path=oracles_path,
        output=output,
        allow_incomplete=False,
    )
    MODULE.finalize(
        tasks_path=tasks_path,
        oracles_path=oracles_path,
        output=output,
        allow_incomplete=False,
    )
    for line in (output / "SHA256SUMS").read_text().splitlines():
        expected, relative = line.split("  ", 1)
        assert MODULE.sha256(output / relative) == expected


def test_finalize_rejects_duplicate_raw_pairs(tmp_path):
    output, tasks_path, oracles_path = prepare_finalize_fixture(tmp_path)
    path = output / "modern/correctness/gopy.jsonl"
    path.write_text(path.read_text() + path.read_text(), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate gopy modern"):
        MODULE.finalize(
            tasks_path=tasks_path,
            oracles_path=oracles_path,
            output=output,
            allow_incomplete=False,
        )


def test_finalize_rejects_stale_release_inputs(tmp_path):
    output, tasks_path, oracles_path = prepare_finalize_fixture(tmp_path)
    tasks_path.write_text(tasks_path.read_text() + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="stale tasks"):
        MODULE.finalize(
            tasks_path=tasks_path,
            oracles_path=oracles_path,
            output=output,
            allow_incomplete=False,
        )


def test_finalize_rejects_success_for_missing_generation(tmp_path):
    output, tasks_path, oracles_path = prepare_finalize_fixture(tmp_path)
    path = (
        output
        / "inputs/models"
        / MODULE.safe_component("Qwen/Qwen")
        / "generation_results.normalized.jsonl"
    )
    generation = MODULE.read_jsonl(path)[0]
    generation["status"] = "failed"
    generation["apply_ok"] = False
    generation["patch_sha256"] = None
    MODULE.atomic_jsonl(path, [generation])
    with pytest.raises(ValueError, match="not reported missing"):
        MODULE.finalize(
            tasks_path=tasks_path,
            oracles_path=oracles_path,
            output=output,
            allow_incomplete=False,
        )


def test_run_rejects_existing_duplicate_pairs_before_dispatch(tmp_path):
    output = tmp_path / "output"
    MODULE.atomic_json(
        output / "input-manifest.json",
        {"partial_modern_only": False},
    )
    row = {"id": "task", "model": "Qwen/Qwen"}
    MODULE.atomic_jsonl(
        output / "modern/correctness/gopy.jsonl",
        [row, row],
    )
    with pytest.raises(ValueError, match="duplicate existing gopy"):
        MODULE.run_lanes(
            output=output,
            selected_lanes=["gopy"],
            jobs=MODULE.DEFAULT_JOBS,
            allow_partial_modern=False,
        )


def test_run_rejects_duplicate_lane_selection(tmp_path):
    with pytest.raises(ValueError, match="lanes must be unique"):
        MODULE.run_lanes(
            output=tmp_path,
            selected_lanes=["gopy", "gopy"],
            jobs=MODULE.DEFAULT_JOBS,
            allow_partial_modern=False,
        )


def test_parse_jobs_is_strict():
    assert MODULE.parse_jobs(["gopy=3"])["gopy"] == 3
    with pytest.raises(ValueError, match="duplicate lane jobs"):
        MODULE.parse_jobs(["gopy=3", "gopy=4"])
