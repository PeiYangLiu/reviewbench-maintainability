import importlib.util
import json
from pathlib import Path
import sys


SCRIPT = Path(__file__).parents[1] / "scripts" / "package_agentic_evaluation.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("package_agentic_evaluation", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_copy_generation_makes_patch_and_trace_paths_portable(tmp_path):
    patch = tmp_path / "source.patch"
    patch.write_text("diff --git a/a b/a\n", encoding="utf-8")
    trace = tmp_path / "source.json"
    trace.write_text(
        json.dumps([{"tool_result": {"patch_file": str(patch)}}]) + "\n",
        encoding="utf-8",
    )
    source = tmp_path / "generation.jsonl"
    source.write_text(
        json.dumps(
            {
                "id": "task",
                "repo": "owner/repo",
                "model": "gpt-5.5",
                "protocol": "agentic-line-edit/v1",
                "status": "generated",
                "apply_ok": True,
                "changed_lines": 2,
                "patch_sha256": "canonical",
                "patch_file": str(patch),
                "trace_file": str(trace),
                "error": f"cache at {tmp_path}/cache",
                "rounds": [
                    {
                        "round": 1,
                        "raw_api_response": {"model": "gpt-5.5-2026-04-23"},
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "release"
    out.mkdir()

    records = MODULE.copy_generation(
        {"gpt-5.5": source},
        out,
        {"task": 0},
        {str(tmp_path): "${SESSION}"},
    )

    assert records[0]["patch_file"] == "patches/gpt-5.5/task.patch"
    assert records[0]["trace_file"] == "traces/gpt-5.5/task.json"
    assert records[0]["round_count"] == 1
    assert records[0]["patch_sha256"] == "canonical"
    assert records[0]["response_model_attested"] is True
    assert records[0]["response_models"] == ["gpt-5.5-2026-04-23"]
    assert records[0]["error"] == "cache at ${SESSION}/cache"
    assert "rounds" not in records[0]
    assert (out / records[0]["patch_file"]).is_file()
    assert (out / records[0]["trace_file"]).is_file()
    packaged_trace = json.loads((out / records[0]["trace_file"]).read_text())
    assert packaged_trace[0]["tool_result"]["patch_file"] == "${SESSION}/source.patch"


def test_parse_replacements_covers_paths_without_leading_slash():
    replacements = MODULE.parse_replacements(["SESSION=/home/user/session"])
    assert MODULE.sanitize(
        "home/user/session/file.log",
        replacements,
    ) == "${SESSION}/file.log"


def test_normalize_correctness_preserves_patch_identity(tmp_path):
    correctness = tmp_path / "correctness"
    correctness.mkdir()
    (correctness / "gopy.jsonl").write_text(
        json.dumps(
            {
                "schema_version": "reviewbench-open-correctness-normalized/v1",
                "id": "task",
                "model": "model",
                "lane": "gopy",
                "source_kind": "modern",
                "status": "pass",
                "patch_sha256": "canonical",
                "patch_apply": True,
                "correctness_ok": True,
                "scope_ok": True,
                "retryable": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    records = MODULE.normalize_correctness(
        correctness,
        {"task": 0},
        {"model": 0},
    )

    assert records[0]["status"] == "pass"
    assert records[0]["lane"] == "gopy"
    assert records[0]["patch_sha256"] == "canonical"
