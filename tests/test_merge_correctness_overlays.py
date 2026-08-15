import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "merge_correctness_overlays.py"
SPEC = importlib.util.spec_from_file_location("merge_correctness_overlays", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )


def generation(tmp_path, *, apply_ok=True):
    path = tmp_path / "generation.jsonl"
    write_jsonl(
        path,
        [
            {
                "id": "task",
                "model": "model",
                "apply_ok": apply_ok,
                "patch_sha256": "canonical" if apply_ok else None,
            }
        ],
    )
    return path


def correctness(status, patch_sha256, lane="gopy"):
    return {
        "id": "task",
        "model": "model",
        "lane": lane,
        "status": status,
        "patch_sha256": patch_sha256,
    }


def test_merge_applies_matching_overlay(tmp_path):
    base = tmp_path / "base"
    overlay = tmp_path / "overlay"
    write_jsonl(base / "gopy.jsonl", [correctness("fail", "stale")])
    write_jsonl(overlay / "gopy.jsonl", [correctness("pass", "canonical")])

    result = MODULE.merge(
        ids=["task"],
        expected_models=["model"],
        generation_paths={"model": generation(tmp_path)},
        base_directories=[base],
        overlay_directories=[overlay],
        output=tmp_path / "out",
    )

    rows = MODULE.load_jsonl(tmp_path / "out" / "gopy.jsonl")
    assert rows[0]["status"] == "pass"
    assert result["canonical_patch_mismatches"] == 0
    assert result["infra_records"] == 0


def test_merge_allows_intermediate_infra_only_when_later_overlay_resolves_it(
    tmp_path,
):
    base = tmp_path / "base"
    retry = tmp_path / "retry"
    resolved = tmp_path / "resolved"
    write_jsonl(base / "gopy.jsonl", [correctness("fail", "canonical")])
    write_jsonl(retry / "gopy.jsonl", [correctness("infra_retry", "canonical")])
    write_jsonl(resolved / "gopy.jsonl", [correctness("pass", "canonical")])

    MODULE.merge(
        ids=["task"],
        expected_models=["model"],
        generation_paths={"model": generation(tmp_path)},
        base_directories=[base],
        overlay_directories=[retry, resolved],
        output=tmp_path / "out",
    )

    rows = MODULE.load_jsonl(tmp_path / "out" / "gopy.jsonl")
    assert rows[0]["status"] == "pass"


def test_merge_rejects_stale_final_patch(tmp_path):
    base = tmp_path / "base"
    write_jsonl(base / "gopy.jsonl", [correctness("pass", "stale")])

    with pytest.raises(ValueError, match="does not match generation"):
        MODULE.merge(
            ids=["task"],
            expected_models=["model"],
            generation_paths={"model": generation(tmp_path)},
            base_directories=[base],
            overlay_directories=[],
            output=tmp_path / "out",
        )


@pytest.mark.parametrize("status", ["candidate_missing", "apply_error", "infra_retry"])
def test_merge_rejects_nonfinal_generated_status(tmp_path, status):
    base = tmp_path / "base"
    write_jsonl(base / "gopy.jsonl", [correctness(status, "canonical")])

    with pytest.raises(ValueError, match="non-final correctness status"):
        MODULE.merge(
            ids=["task"],
            expected_models=["model"],
            generation_paths={"model": generation(tmp_path)},
            base_directories=[base],
            overlay_directories=[],
            output=tmp_path / "out",
        )


def test_merge_requires_missing_generation_status(tmp_path):
    base = tmp_path / "base"
    write_jsonl(base / "gopy.jsonl", [correctness("fail", None)])

    with pytest.raises(ValueError, match="non-generated candidate"):
        MODULE.merge(
            ids=["task"],
            expected_models=["model"],
            generation_paths={"model": generation(tmp_path, apply_ok=False)},
            base_directories=[base],
            overlay_directories=[],
            output=tmp_path / "out",
        )


def test_merge_rejects_missing_expected_model(tmp_path):
    with pytest.raises(ValueError, match="does not match expected models"):
        MODULE.merge(
            ids=["task"],
            expected_models=["model", "missing-model"],
            generation_paths={"model": generation(tmp_path)},
            base_directories=[],
            overlay_directories=[],
            output=tmp_path / "out",
        )
