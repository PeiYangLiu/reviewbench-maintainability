import hashlib
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration.scripted_lane import (
    RESOLVED_COMPOSER_LOCK, UNLOCKED_COMPOSER_WORKSPACES,
    composer_locked_install_command, composer_source_ancestor_links,
)
from reviewbench_maintainability.agentic.contracts import ContractError


def test_nested_view_keeps_current_siblings_and_rebuilds_aliases(tmp_path):
    root, view = tmp_path / "candidate", tmp_path / "view"
    source = root / "Spanner"
    source.mkdir(parents=True)
    (root / "Core").mkdir()
    (root / "Core/helper.php").write_text("first helper")
    (root / ".git").mkdir()
    (root / ".reviewbench").mkdir()
    (root / ".public-config").write_text("retained")
    view.mkdir()
    commands = composer_source_ancestor_links(str(source), str(view), "Spanner")
    result = subprocess.run(["bash", "-c", "set -eu; fail() { exit 2; }; " + "\n".join(commands)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (view / "Spanner/../Core/helper.php").read_text() == "first helper"
    (root / "Core/helper.php").write_text("current helper")
    assert (view / "Core/helper.php").read_text() == "current helper"
    assert (view / ".public-config").read_text() == "retained"
    assert not (view / ".git").exists() and not (view / ".reviewbench").exists()
    (root / "Core/helper.php").unlink()
    assert not (view / "Core/helper.php").exists()


def test_root_workspace_command_has_no_extra_view_or_sibling_copies(tmp_path):
    assert composer_source_ancestor_links(str(tmp_path), str(tmp_path / "view"), ".") == []
    lock = b'{"packages":[]}\n'
    command = composer_locked_install_command(
        {"workspace": "Spanner", "install_flags": ["--no-scripts"]},
        {"lock_origin": RESOLVED_COMPOSER_LOCK, "lock_sha256": hashlib.sha256(lock).hexdigest()},
        "/workspace/Spanner", "/workspace/.reviewbench/scripted")
    assert "view=/workspace/.reviewbench/scripted/composer-source/Spanner" in command
    assert "Spanner/vendor" in command


def test_explicit_new_cases_remain_bound_to_their_original_workspaces():
    assert UNLOCKED_COMPOSER_WORKSPACES[
        "github__googleapis__google-cloud-php__pull_6708__comment_1397564001__api_design"] == "Spanner"
    assert UNLOCKED_COMPOSER_WORKSPACES[
        "github__spryker-sdk__integrator__pull_34__comment_957304473__complexity"] == "."
    with pytest.raises(ContractError):
        composer_source_ancestor_links("/workspace/Other", "/state/view", "Spanner")
