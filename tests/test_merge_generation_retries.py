import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "merge_generation_retries.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("merge_generation_retries", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
AGENTIC = sys.modules["generate_agentic_patches"]


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def task(task_id):
    return {
        "id": task_id,
        "repo": "owner/repo",
        "start_sha": "a" * 40,
        "locality": {"path": "src/a.py", "line": 1},
        "task_instruction": "Extract the duplicated branch.",
    }


def generation(task_row, model, status, root):
    task_id = task_row["id"]
    trace = root / "traces" / f"{task_id}.json"
    trace.parent.mkdir(parents=True, exist_ok=True)
    trace.write_text("[]\n")
    row = {
        "id": task_id,
        "model": model,
        "status": status,
        "apply_ok": status == "generated",
        "task_fingerprint": AGENTIC.task_fingerprint(task_row),
        "trace_file": str(trace),
    }
    if status == "generated":
        patch = root / "patches" / f"{task_id}.patch"
        patch.parent.mkdir(parents=True, exist_ok=True)
        patch.write_text("--- a/src/a.py\n+++ b/src/a.py\n")
        row["patch_file"] = str(patch)
    else:
        row["error"] = "infrastructure failure"
    return row


def test_merge_retries_only_replaces_failed_records(tmp_path):
    tasks = [task("one"), task("two")]
    tasks_path = tmp_path / "tasks.jsonl"
    oracles_path = tmp_path / "oracles.jsonl"
    write_jsonl(tasks_path, tasks)
    write_jsonl(oracles_path, [{"id": row["id"]} for row in tasks])
    base_path = tmp_path / "base" / "generation_results.jsonl"
    write_jsonl(
        base_path,
        [
            generation(tasks[0], "model", "generated", base_path.parent),
            generation(tasks[1], "model", "failed", base_path.parent),
        ],
    )
    retry_path = tmp_path / "retry" / "generation_results.jsonl"
    write_jsonl(
        retry_path,
        [generation(tasks[1], "model", "generated", retry_path.parent)],
    )

    manifest = MODULE.merge_retries(
        tasks_path=tasks_path,
        oracles_path=oracles_path,
        base_path=base_path,
        retry_paths=[retry_path],
        output=tmp_path / "out",
        repo_cache=None,
    )

    rows = MODULE.load_jsonl(tmp_path / "out/generation_results.jsonl")
    assert manifest["generated"] == 2
    assert manifest["recovered"] == 1
    assert rows[0]["canonical_provenance"]["source_kind"] == "base"
    assert rows[1]["canonical_provenance"]["source_kind"] == "retry"
    assert Path(rows[1]["patch_file"]).is_file()
    assert len(MODULE.load_jsonl(tmp_path / "out/recovered/tasks.jsonl")) == 1
    assert len(
        MODULE.load_jsonl(tmp_path / "out/evaluation_delta/tasks.jsonl")
    ) == 1
    assert (tmp_path / "out/generation_complete.json").is_file()
    assert (tmp_path / "out/recovered/generation_complete.json").is_file()


def test_merge_rejects_retry_of_success(tmp_path):
    task_row = task("one")
    tasks_path = tmp_path / "tasks.jsonl"
    oracles_path = tmp_path / "oracles.jsonl"
    write_jsonl(tasks_path, [task_row])
    write_jsonl(oracles_path, [{"id": "one"}])
    base_path = tmp_path / "base/generation_results.jsonl"
    retry_path = tmp_path / "retry/generation_results.jsonl"
    write_jsonl(
        base_path,
        [generation(task_row, "model", "generated", base_path.parent)],
    )
    write_jsonl(
        retry_path,
        [generation(task_row, "model", "generated", retry_path.parent)],
    )

    with pytest.raises(ValueError, match="replace successful"):
        MODULE.merge_retries(
            tasks_path=tasks_path,
            oracles_path=oracles_path,
            base_path=base_path,
            retry_paths=[retry_path],
            output=tmp_path / "out",
            repo_cache=None,
        )


def test_merge_repairs_line_endings_without_resampling(tmp_path):
    repo_cache = tmp_path / "repos"
    repo = repo_cache / "owner__repo"
    target = repo / "src/a.py"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "start"], cwd=repo, check=True)
    start_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        text=True,
    ).strip()
    task_row = {**task("one"), "start_sha": start_sha}
    tasks_path = tmp_path / "tasks.jsonl"
    oracles_path = tmp_path / "oracles.jsonl"
    write_jsonl(tasks_path, [task_row])
    write_jsonl(oracles_path, [{"id": "one"}])
    base_path = tmp_path / "base/generation_results.jsonl"
    row = generation(task_row, "model", "generated", base_path.parent)
    wrong_patch = AGENTIC.git_diff(
        "src/a.py",
        "one\ntwo\nthree\n",
        "one\nTWO\nthree\n",
    )
    Path(row["patch_file"]).write_text(wrong_patch)
    row["rounds"] = [
        {
            "action": {
                "action": "edit",
                "edits": [
                    {
                        "start_line": 2,
                        "end_line": 2,
                        "content": "TWO",
                    }
                ],
            },
            "tool_result": {"ok": True},
        }
    ]
    write_jsonl(base_path, [row])

    MODULE.merge_retries(
        tasks_path=tasks_path,
        oracles_path=oracles_path,
        base_path=base_path,
        retry_paths=[],
        output=tmp_path / "out",
        repo_cache=repo_cache,
    )

    merged = MODULE.load_jsonl(tmp_path / "out/generation_results.jsonl")[0]
    assert merged["canonical_provenance"]["patch_repaired"] == (
        "preserved-source-line-endings"
    )
    assert MODULE.patch_applies(
        task_row,
        Path(merged["patch_file"]),
        repo_cache,
    )
    assert len(
        MODULE.load_jsonl(tmp_path / "out/evaluation_delta/tasks.jsonl")
    ) == 1
