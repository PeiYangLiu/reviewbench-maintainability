import importlib.util
import json
from pathlib import Path
import subprocess
import sys


SCRIPT = Path(__file__).parents[1] / "scripts" / "stage_generation_subset.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("stage_generation_subset", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
AGENTIC = sys.modules["generate_agentic_patches"]


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_stage_subset_preserves_requested_order_and_artifacts(tmp_path):
    repo = tmp_path / "repos/owner__repo"
    target = repo / "src/a.py"
    target.parent.mkdir(parents=True)
    target.write_text("value = 1\n")
    subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "start"], cwd=repo, check=True)
    sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        text=True,
    ).strip()
    tasks = [
        {
            "id": task_id,
            "repo": "owner/repo",
            "start_sha": sha,
            "locality": {"path": "src/a.py", "line": 1},
            "task_instruction": "Rename the value.",
        }
        for task_id in ["one", "two"]
    ]
    tasks_path = tmp_path / "tasks.jsonl"
    oracles_path = tmp_path / "oracles.jsonl"
    write_jsonl(tasks_path, tasks)
    write_jsonl(oracles_path, [{"id": task["id"]} for task in tasks])
    source_root = tmp_path / "generation"
    records = []
    for task in tasks:
        patch = source_root / "patches" / f"{task['id']}.patch"
        trace = source_root / "traces" / f"{task['id']}.json"
        patch.parent.mkdir(parents=True, exist_ok=True)
        trace.parent.mkdir(parents=True, exist_ok=True)
        patch.write_text(
            AGENTIC.git_diff("src/a.py", "value = 1\n", "value = 2\n")
        )
        trace.write_text("[]\n")
        records.append(
            {
                "id": task["id"],
                "model": "model",
                "status": "generated",
                "apply_ok": True,
                "patch_file": str(patch),
                "trace_file": str(trace),
                "task_fingerprint": AGENTIC.task_fingerprint(task),
            }
        )
    source_path = source_root / "generation_results.jsonl"
    write_jsonl(source_path, records)

    manifest = MODULE.stage_subset(
        tasks_path=tasks_path,
        oracles_path=oracles_path,
        repo_cache=tmp_path / "repos",
        ids=["two"],
        generation_paths={"model": source_path},
        output=tmp_path / "out",
    )

    assert manifest["tasks"] == 1
    assert manifest["models"]["model"]["generated"] == 1
    assert [
        row["id"]
        for row in MODULE.merge.load_jsonl(tmp_path / "out/tasks.jsonl")
    ] == ["two"]
    staged = MODULE.merge.load_jsonl(
        tmp_path / "out/models/model/generation_results.jsonl"
    )[0]
    assert Path(staged["patch_file"]).is_file()
    assert (tmp_path / "out/models/model/generation_complete.json").is_file()
