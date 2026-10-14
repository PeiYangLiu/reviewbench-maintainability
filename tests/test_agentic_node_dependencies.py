from pathlib import Path
from types import SimpleNamespace
import json
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"scripts"))
from prepare_agentic_node_dependencies import minimal_packages, relocate_workspace_links
from agentic_runtime_migration.tsjs_lane import (
    prepared_dependencies, babel_public_check, typescript_public_check, NODE_CACHE_TARGET,
)
from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash, component, file_hash


def test_minimal_npm_dependency_parser_preserves_exact_pins():
    command = ["npm", "install", "--prefix", "node_modules/.oracle-deps",
               "--no-save", "--ignore-scripts", "typescript@4.9.5", "date-fns@4.1.0"]
    assert minimal_packages(command) == {"typescript": "4.9.5", "date-fns": "4.1.0"}
    assert minimal_packages(["npm", "ci", "--ignore-scripts"]) is None
    assert minimal_packages("SKIP") is None
    with pytest.raises(ContractError, match="exact"):
        minimal_packages(command[:-2] + ["typescript@^4.9.5"])


def test_workspace_dependency_links_follow_candidate_sources_not_a_frozen_copy(tmp_path):
    source = tmp_path/"source"
    modules = source/"node_modules"
    (modules/"external").mkdir(parents=True)
    (modules/".bin").mkdir()
    (modules/"external/cli.js").write_text("external")
    (modules/".bin/cli").symlink_to("../external/cli.js")
    (source/"packages/local").mkdir(parents=True)
    (modules/"local").symlink_to("../packages/local")
    assert relocate_workspace_links(modules, source, "frontend") == [
        {"path": "local", "workspace_target": "frontend/packages/local"}]
    (modules/"escape").symlink_to(tmp_path/"elsewhere")
    with pytest.raises(ContractError, match="escapes"):
        relocate_workspace_links(modules, source, "frontend")


def test_prepared_dependencies_bind_case_node_version_and_lock(tmp_path):
    task, case = {"id": "task"}, {"node": "16.20.2", "install": ["npm", "ci", "--ignore-scripts"]}
    root = tmp_path/component("task")
    (root/"node_modules").mkdir(parents=True)
    (root/"package-lock.json").write_text('{"lockfileVersion":2}')
    record = {"id": "task", "status": "prepared", "kind": "npm-ci", "node_version": "16.20.2",
              "case_fingerprint": canonical_hash(case), "package_lock_sha256": file_hash(root/"package-lock.json")}
    (root/"preparation.json").write_text(json.dumps(record))
    ctx = SimpleNamespace(config={"migration": {"node_dependency_root": str(tmp_path)}})
    assert prepared_dependencies(ctx, task, case)[0] == root/"node_modules"
    (root/"package-lock.json").write_text("changed")
    with pytest.raises(ContractError, match="bindings"):
        prepared_dependencies(ctx, task, case)


def test_jsx_public_check_uses_frozen_project_parser_without_recovery_mode():
    command = babel_public_check("components/navbar.js")
    assert "@babel/parser" in command
    assert "jsx" in command
    assert "/workspace/components/navbar.js" in command
    assert "errorRecovery" not in command
    typed = typescript_public_check("components/navbar.js")
    assert "allowJs:true" in typed
    assert "'.jsx'" in typed
    assert "reportDiagnostics:true" in typed


def test_runtime_module_directory_preserves_esm_sibling_resolution():
    assert Path(NODE_CACHE_TARGET).name == "node_modules"
