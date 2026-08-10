import importlib.util
import json
from pathlib import Path
import subprocess
import sys


SCRIPT = Path(__file__).parents[1] / "scripts" / "build_minimal_repo_cache.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("build_minimal_repo_cache", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def git(cwd, *args):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def test_builds_exact_tree_without_parent_history(tmp_path):
    source = tmp_path / "source" / "owner__repo"
    source.mkdir(parents=True)
    git(source, "init")
    git(source, "config", "user.email", "test@example.com")
    git(source, "config", "user.name", "Test")
    (source / "file.txt").write_text("old\n")
    git(source, "add", "file.txt")
    git(source, "commit", "-m", "first")
    parent = git(source, "rev-parse", "HEAD")
    (source / "file.txt").write_text("new\n")
    git(source, "commit", "-am", "second")
    start = git(source, "rev-parse", "HEAD")

    task = {
        "id": "task",
        "repo": "owner/repo",
        "start_sha": start,
        "locality": {"path": "file.txt"},
    }
    out = tmp_path / "out"
    record = MODULE.build_repo(
        "owner/repo",
        [task],
        tmp_path / "source",
        out,
    )
    bare = out / "owner__repo"
    assert record["tasks"] == 1
    assert git(bare, "show", f"{start}:file.txt") == "new"
    missing_parent = subprocess.run(
        ["git", "cat-file", "-e", f"{parent}^{{commit}}"],
        cwd=bare,
        capture_output=True,
    )
    assert missing_parent.returncode != 0
    assert json.loads((bare / MODULE.MARKER).read_text())["start_commits"] == 1
    assert (
        bare / ".git" / MODULE.CACHE_REF
    ).read_text(encoding="ascii") == f"{start}\n"
