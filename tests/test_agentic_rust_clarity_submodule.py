import copy
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_clarity_submodule as clarity
from reviewbench_maintainability.agentic.contracts import ContractError


def recipe():
    return {
        "task_id": clarity.TASK, "repo": clarity.REPO, "start_sha": clarity.START,
        "submodules": [{"path": clarity.MODULE_PATH, "commit": clarity.MODULE,
                        "omit": True, "reason": "original omitted policy"}],
        "assets": [{"kind": "archive", "path": "/retained/compiler", "sha256": "a" * 64}],
        "env": {"RUSTC": "/original/rustc", "CARGO_BUILD_JOBS": "4"},
        "grading": {"commands": [{"command": "original real WASM command"}], "immutable_paths": ["tests/original"]},
        "dev_checks": ["original public check"], "cpus": 4, "memory_mb": 8192,
    }


def test_replacement_changes_only_original_gitlink_policy():
    original = recipe()
    saved = copy.deepcopy(original)
    asset = {"path": "/exact-module.json", "sha256": "b" * 64}
    result = clarity.replace_submodule(original, asset)
    assert original == saved
    assert result["submodules"] == [{"path": clarity.MODULE_PATH, "commit": clarity.MODULE, "asset": asset}]
    assert {k: v for k, v in result.items() if k != "submodules"} == {
        k: v for k, v in original.items() if k != "submodules"}


@pytest.mark.parametrize("change", ["task", "repo", "start", "path", "pin", "extra", "not-omitted"])
def test_foreign_or_reinterpreted_submodule_binding_rejected(change):
    original = recipe()
    if change in {"task", "repo", "start"}:
        original[{"task": "task_id", "repo": "repo", "start": "start_sha"}[change]] = "foreign"
    elif change == "path":
        original["submodules"][0]["path"] = "another"
    elif change == "pin":
        original["submodules"][0]["commit"] = "0" * 40
    elif change == "extra":
        original["submodules"].append(copy.deepcopy(original["submodules"][0]))
    else:
        original["submodules"][0]["omit"] = False
    with pytest.raises(ContractError):
        clarity.replace_submodule(original, {"path": "/asset", "sha256": "b" * 64})


def test_task_optin_leaves_all_other_rust_tasks_unchanged():
    ctx = SimpleNamespace(config={clarity.OPTION: {clarity.TASK: "/original-inputs.json"}})
    assert clarity.requested(ctx, {"id": clarity.TASK})
    assert not clarity.requested(ctx, {"id": "other"})
    assert not clarity.requested(SimpleNamespace(), {"id": clarity.TASK})
    with pytest.raises(ContractError):
        clarity.requested(SimpleNamespace(config={clarity.OPTION: "not-a-map"}), {"id": clarity.TASK})


@pytest.mark.parametrize("field,value", [("id", "other"), ("repo", "other/repo"), ("start_sha", "0" * 40)])
def test_wrong_identity_rejected_before_any_asset_access(field, value):
    task = {"id": clarity.TASK, "repo": clarity.REPO, "start_sha": clarity.START}
    oracle = copy.deepcopy(task)
    task[field] = value
    with pytest.raises(ContractError, match="wrong original"):
        clarity.migrate(SimpleNamespace(), task, oracle)
