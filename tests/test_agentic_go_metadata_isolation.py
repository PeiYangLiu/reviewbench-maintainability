"""Manifest patching must not inherit an unrelated enclosing Git worktree."""
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration.go_dependencies import metadata_patch_environment, manifest_snapshots
from test_agentic_go_dependencies import context
from test_agentic_go_wrappers import commit, git, write
from reviewbench_maintainability.agentic.contracts import ContractError


@pytest.mark.parametrize("delete", [False, True])
def test_real_nested_git_root_cannot_silently_skip_original_manifest_patch(tmp_path, delete):
    enclosing = tmp_path / "enclosing"
    write(enclosing / "untouched", "outside metadata projection\n")
    commit(enclosing)
    ctx, task, source = context(enclosing / "nested", {
        "go.mod": "module example.invalid/original\n\ngo 1.20\n",
        "source.go": "package original\n",
    })
    if delete:
        (source / "go.mod").unlink()
    else:
        (source / "go.mod").write_text("module example.invalid/changed\n\ngo 1.20\n")
    patch = write(enclosing / "metadata.patch", git(source, "diff", "--binary", "--full-index") + "\n")
    if delete:
        with pytest.raises(ContractError, match="local_replacement_manifest_missing"):
            manifest_snapshots(ctx, task, "", patch)
    else:
        phases = dict(manifest_snapshots(ctx, task, "", patch))
        assert b"example.invalid/original" in phases["start"]["go.mod"]
        assert b"example.invalid/changed" in phases["golden"]["go.mod"]
    assert (enclosing / "untouched").read_text() == "outside metadata projection\n"
    assert (source / "source.go").read_text() == "package original\n"


def test_metadata_environment_removes_inherited_repository_and_config_selectors(tmp_path, monkeypatch):
    for name, value in {
        "GIT_DIR": "/foreign/.git", "GIT_WORK_TREE": "/foreign",
        "GIT_INDEX_FILE": "/foreign/index", "GIT_OBJECT_DIRECTORY": "/foreign/objects",
        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.bare", "GIT_CONFIG_VALUE_0": "true",
        "GIT_CEILING_DIRECTORIES": "/unrelated",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("KEEP_NON_GIT_ENV", "unchanged")
    root = tmp_path / "projection"
    root.mkdir()
    env = metadata_patch_environment(root)
    assert env["KEEP_NON_GIT_ENV"] == "unchanged"
    assert {k for k in env if k.startswith("GIT_")} == {
        "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL",
    }
    assert env["GIT_CEILING_DIRECTORIES"] == str(tmp_path.resolve())
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"


def test_actual_git_discovery_stops_before_the_parent_repository(tmp_path):
    write(tmp_path / "tracked", "outside\n")
    commit(tmp_path)
    root = tmp_path / "staging" / "projection"
    root.mkdir(parents=True)
    ordinary = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root,
                              capture_output=True, text=True)
    assert ordinary.returncode == 0 and ordinary.stdout.strip() == str(tmp_path)
    isolated = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root,
                              env=metadata_patch_environment(root), capture_output=True, text=True)
    assert isolated.returncode != 0
