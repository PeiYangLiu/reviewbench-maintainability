import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "summarize_release_evaluation.py"
SPEC = importlib.util.spec_from_file_location("summarize_release_evaluation", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_breakdown_reports_resolved_rate():
    rows = [
        {"language": "Go", "resolved": True},
        {"language": "Go", "resolved": False},
        {"language": "Python", "resolved": True},
    ]

    result = MODULE.breakdown(rows, "language")

    assert result["Go"] == {"resolved": 1, "tasks": 2, "rate": 0.5}
    assert result["Python"] == {"resolved": 1, "tasks": 1, "rate": 1.0}


def test_pairwise_counts_disagreements():
    rows = [
        {"id": "a", "model": "left", "resolved": True},
        {"id": "a", "model": "right", "resolved": False},
        {"id": "b", "model": "left", "resolved": True},
        {"id": "b", "model": "right", "resolved": True},
    ]

    result = MODULE.pairwise(rows, ["left", "right"])

    assert result["left__vs__right"] == {"left_only": 1, "both": 1}


def test_load_correctness_normalizes_nested_executor_schema(tmp_path):
    path = tmp_path / "nested.jsonl"
    path.write_text(
        '{"id":"task","oracle_reused":{"source":"recovery"},'
        '"models":[{"model":"m","apply":{"applied":true},'
        '"tests":{"correctness_pass":true,"total":3,"passed":3}}]}\n',
        encoding="utf-8",
    )

    result = MODULE.load_correctness(tmp_path)[("task", "m")]

    assert result["oracle_replayed"] is True
    assert result["patch_apply"] is True
    assert result["correctness_ok"] is True
    assert result["pass_to_pass_total"] == 3


def test_load_correctness_normalizes_direct_results_schema(tmp_path):
    path = tmp_path / "direct.jsonl"
    path.write_text(
        '{"id":"task","evaluation_mode":"direct",'
        '"policy":{"same_oracle_all_models":true},'
        '"results":[{"model":"m","patch_applied":true,"correct":true}]}\n',
        encoding="utf-8",
    )

    result = MODULE.load_correctness(tmp_path)[("task", "m")]

    assert result["oracle_replayed"] is True
    assert result["patch_apply"] is True
    assert result["correctness_ok"] is True


def test_load_correctness_normalizes_model_results_schema(tmp_path):
    path = tmp_path / "model-results.jsonl"
    path.write_text(
        '{"id":"task","schema_version":"reviewbench-model-correctness/v1",'
        '"patch_policy":{"target_or_golden_applied":false},'
        '"oracle":{"pass_to_pass":2},'
        '"model_results":[{"model":"m","patch_apply_ok":true,'
        '"correctness_pass":true,"actual_p2p":2}]}\n',
        encoding="utf-8",
    )

    result = MODULE.load_correctness(tmp_path)[("task", "m")]

    assert result["oracle_replayed"] is True
    assert result["patch_apply"] is True
    assert result["correctness_ok"] is True
    assert result["pass_to_pass_total"] == 2


def test_load_correctness_normalizes_models_mapping_schema(tmp_path):
    path = tmp_path / "models-map.jsonl"
    path.write_text(
        '{"id":"task","policy":{"same_oracle_all_models":true,'
        '"golden_or_target_applied":false},'
        '"models":{"m":{"model":"m","apply_ok":true,'
        '"correctness":"pass","complete_p2p":true}}}\n',
        encoding="utf-8",
    )

    result = MODULE.load_correctness(tmp_path)[("task", "m")]

    assert result["oracle_replayed"] is True
    assert result["patch_apply"] is True
    assert result["correctness_ok"] is True


def test_load_correctness_normalizes_flat_schema(tmp_path):
    path = tmp_path / "flat.jsonl"
    path.write_text(
        '{"id":"task","model":"m","environment_reused":{"once":true},'
        '"policy":{"same_oracle_all_models":true,"target_applied":false,'
        '"golden_applied":false},"apply":{"applied":true},'
        '"tests":{"correctness_pass":true,"pass_to_pass_total":4,'
        '"pass_to_pass_passed":4}}\n',
        encoding="utf-8",
    )

    result = MODULE.load_correctness(tmp_path)[("task", "m")]

    assert result["oracle_replayed"] is True
    assert result["patch_apply"] is True
    assert result["correctness_ok"] is True


def test_load_correctness_accepts_start_reset_policy(tmp_path):
    path = tmp_path / "start-reset.jsonl"
    path.write_text(
        '{"id":"task","policy":{"start_reset_each_model":true,'
        '"target_or_golden_applied":false},'
        '"models":[{"model":"m","patch_apply":true,"correctness_ok":true}]}\n',
        encoding="utf-8",
    )

    result = MODULE.load_correctness(tmp_path)[("task", "m")]

    assert result["oracle_replayed"] is True


def test_load_correctness_accepts_start_reset_per_model_policy(tmp_path):
    path = tmp_path / "start-reset-per-model.jsonl"
    path.write_text(
        '{"id":"task","policy":{"start_reset_per_model":true,'
        '"target_or_golden_applied":false},'
        '"models":{"m":{"model":"m","apply_ok":true,'
        '"correctness":"pass"}}}\n',
        encoding="utf-8",
    )

    result = MODULE.load_correctness(tmp_path)[("task", "m")]

    assert result["oracle_replayed"] is True


def test_load_correctness_normalizes_jvm_result_schema(tmp_path):
    path = tmp_path / "java.jsonl"
    path.write_text(
        '{"id":"task","model":"m","evaluation_schema_version":'
        '"reviewbench-correctness/java-v1","correctness_result":"pass",'
        '"apply_ok":true,"expected_pass_to_pass":6,'
        '"observed_pass_to_pass":6}\n',
        encoding="utf-8",
    )

    result = MODULE.load_correctness(tmp_path)[("task", "m")]

    assert result["oracle_replayed"] is True
    assert result["patch_apply"] is True
    assert result["correctness_ok"] is True
    assert result["pass_to_pass_total"] == 6
    assert result["pass_to_pass_passed"] == 6


def test_load_correctness_jvm_failure_is_replayed_but_not_correct(tmp_path):
    path = tmp_path / "tsjs.jsonl"
    path.write_text(
        '{"id":"task","model":"m","correctness_result":"fail",'
        '"apply_ok":true,"expected_pass_to_pass":2,'
        '"observed_pass_to_pass":0}\n',
        encoding="utf-8",
    )

    result = MODULE.load_correctness(tmp_path)[("task", "m")]

    assert result["oracle_replayed"] is True
    assert result["correctness_ok"] is False
    assert result["pass_to_pass_passed"] == 0


def test_load_correctness_candidate_missing_is_not_replayed(tmp_path):
    path = tmp_path / "missing.jsonl"
    path.write_text(
        '{"id":"task","model":"m","correctness_result":"candidate_missing",'
        '"apply_ok":false}\n',
        encoding="utf-8",
    )

    result = MODULE.load_correctness(tmp_path)[("task", "m")]

    assert result["oracle_replayed"] is False
    assert result["correctness_ok"] is False


def test_load_correctness_normalizes_native_p2p_schema(tmp_path):
    path = tmp_path / "native.jsonl"
    path.write_text(
        '{"id":"task","model":"m","oracle_replayed":true,"patch_apply":true,'
        '"correctness_ok":true,"p2p_total":3,"p2p_passed":3}\n',
        encoding="utf-8",
    )

    result = MODULE.load_correctness(tmp_path)[("task", "m")]

    assert result["pass_to_pass_total"] == 3
    assert result["pass_to_pass_passed"] == 3


def test_load_correctness_normalizes_rust_status_schema(tmp_path):
    path = tmp_path / "rust.jsonl"
    path.write_text(
        '{"id":"task","model":"m","status":"correctness_pass",'
        '"patch_apply":true,"pass_to_pass_total":10,'
        '"pass_to_pass_passed":10}\n',
        encoding="utf-8",
    )

    result = MODULE.load_correctness(tmp_path)[("task", "m")]

    assert result["oracle_replayed"] is True
    assert result["correctness_ok"] is True


def test_load_correctness_normalizes_pair_status_schema(tmp_path):
    path = tmp_path / "gopy.jsonl"
    path.write_text(
        '{"id":"task","model":"m","evaluation_schema_version":'
        '"reviewbench-correctness/v1-public-oracle","pair_status":"pass",'
        '"apply_ok":true,"correctness_ok":true,"scope_ok":true,'
        '"runs":[{"label":"behavior","exit_code":0}]}\n',
        encoding="utf-8",
    )

    result = MODULE.load_correctness(tmp_path)[("task", "m")]

    assert result["oracle_replayed"] is True
    assert result["patch_apply"] is True
    assert result["correctness_ok"] is True


def test_load_correctness_pair_status_fail_is_replayed(tmp_path):
    path = tmp_path / "gopy-fail.jsonl"
    path.write_text(
        '{"id":"task","model":"m","pair_status":"fail","apply_ok":true,'
        '"correctness_ok":false}\n',
        encoding="utf-8",
    )

    result = MODULE.load_correctness(tmp_path)[("task", "m")]

    assert result["oracle_replayed"] is True
    assert result["correctness_ok"] is False
