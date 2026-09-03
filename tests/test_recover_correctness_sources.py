import importlib.util
import json
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "recover_correctness_sources",
    REPO / "scripts" / "recover_correctness_sources.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def seed(tmp_path, *, tamper=False):
    raw = {"id": "task", "model": "model", "status": "PASS"}
    normalized = {
        "id": "task",
        "model": "model",
        "raw_record": raw,
        "raw_record_source": "modern/correctness/gopy.jsonl",
        "raw_record_sha256": MODULE.CORRECTNESS.canonical_hash(raw),
    }
    if tamper:
        normalized["raw_record"]["status"] = "FAIL"
    root = tmp_path / "run"
    (root / "correctness-normalized").mkdir(parents=True)
    (root / "correctness-normalized/gopy.jsonl").write_text(
        json.dumps(normalized) + "\n", encoding="utf-8"
    )
    (root / "correctness-manifest.json").write_text(
        json.dumps(
            {
                "records": 1,
                "runner_provenance_sha256": "original-runner-hash",
            }
        ),
        encoding="utf-8",
    )
    return root, raw


def test_recovers_raw_rows_and_records_that_provenance_was_not_recovered(
    tmp_path,
):
    root, raw = seed(tmp_path)
    result = MODULE.recover(root)
    recovered = json.loads(
        (root / "modern/correctness/gopy.jsonl").read_text(encoding="utf-8")
    )
    assert recovered == raw
    assert result["recovered_records"] == 1
    assert result["original_runner_provenance_sha256"] == "original-runner-hash"
    assert "unavailable" in result["note"]


def test_rejects_a_tampered_embedded_raw_record(tmp_path):
    root, _ = seed(tmp_path, tamper=True)
    with pytest.raises(ValueError, match="SHA-256"):
        MODULE.recover(root)
    assert not (root / "modern/correctness/gopy.jsonl").exists()


def test_refuses_to_overwrite_an_existing_source_lane(tmp_path):
    root, _ = seed(tmp_path)
    lane = root / "modern/correctness/gopy.jsonl"
    lane.parent.mkdir(parents=True)
    lane.write_text("user data\n", encoding="utf-8")
    with pytest.raises(ValueError, match="overwrite"):
        MODULE.recover(root)
    assert lane.read_text(encoding="utf-8") == "user data\n"
