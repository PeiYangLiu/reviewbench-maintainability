"""No-reinstall metadata changes retain the original graph and current source links."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration.tsjs_pnpm_fixed_workspaces import (
    CONFIG_NAME, COPY_ROUTINE_SHA256, SOURCE, fixed_contract,
)
from agentic_runtime_migration.tsjs_fixed_install import _INPUT_CHECK
from agentic_runtime_migration.tsjs_pnpm_payload import SOURCE as ORIGINAL
from reviewbench_maintainability.agentic.contracts import ContractError, file_hash


def data_fixture(tmp_path):
    files = {
        "package.json": b'{"name":"original-root","devDependencies":{"tool":"1"}}\n',
        "packages/part/package.json": b'{"name":"part","version":"1","scripts":{"build:wasm":"old"},"dependencies":{"tool":"1"}}\n',
        "pnpm-lock.yaml": b"original-complete-lock\n",
        "pnpm-workspace.yaml": b"packages:\n  - packages/*\n",
    }
    layout = {
        "schema_version": "reviewbench-pnpm-workspace-dependencies/v1",
        "workspace_production_files_exported": 0,
        "input_hashes": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
        "module_roots": ["node_modules", "packages/part/node_modules"],
        "workspace_links": [{"path": "node_modules/part", "workspace_target": "packages/part",
                             "link": "../packages/part", "kind": "directory"}],
        "external_index_sha256": "a" * 64,
    }
    payload = tmp_path / "payload"
    for name in layout["module_roots"]:
        (payload / name).mkdir(parents=True)
    (payload / "node_modules/original-tool.txt").write_text("fixed original dependency bytes")
    (payload / "transport.json").write_text(json.dumps(layout))
    current = tmp_path / "current"
    current.mkdir()
    for name, data in files.items():
        path = current / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (current / "packages/part/current-source.ts").write_text("original-source")
    tool = tmp_path / "tool"
    tool.mkdir()
    (tool / "transport.cjs").write_text(SOURCE)
    (tool / CONFIG_NAME).write_text(json.dumps(fixed_contract(layout, files)))
    return files, layout, payload, current, tool


def run(tool, payload, current):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is not installed on this test host")
    return subprocess.run([node, str(tool / "transport.cjs"), str(payload), str(current)],
                          capture_output=True, text=True, timeout=20)


def test_fixed_contract_retains_all_original_workspace_identities_and_graph(tmp_path):
    files, layout, _, _, _ = data_fixture(tmp_path)
    contract = fixed_contract(layout, files)
    assert contract["manifest_names"] == {"package.json": "original-root", "packages/part/package.json": "part"}
    assert contract["original_input_hashes"] == layout["input_hashes"]
    assert not contract["candidate_metadata_selects_dependencies"]
    assert COPY_ROUTINE_SHA256 == hashlib.sha256(ORIGINAL.replace(_INPUT_CHECK, "").encode()).hexdigest()


@pytest.mark.parametrize("change", ["original", "reference-versions-and-lock", "changed-script", "changed-dependencies"])
def test_real_transport_keeps_current_metadata_and_source_while_reusing_original_install(tmp_path, change):
    files, layout, payload, current, tool = data_fixture(tmp_path)
    path = current / "packages/part/package.json"
    manifest = json.loads(path.read_text())
    if change == "reference-versions-and-lock":
        manifest["version"] = "2"
        (current / "pnpm-lock.yaml").write_text("different original reference lock\n")
    elif change == "changed-script":
        manifest["scripts"]["build:wasm"] = "cd wasm && ./build.sh"
    elif change == "changed-dependencies":
        manifest["dependencies"] = {"tool": "2", "another-original-declaration": "3"}
    path.write_text(json.dumps(manifest))
    (current / "packages/part/current-source.ts").write_text("current-candidate-source")
    before = {name: (current / name).read_bytes() for name in files}
    result = run(tool, payload, current)
    assert result.returncode == 0, result.stderr
    assert {name: (current / name).read_bytes() for name in files} == before
    assert (current / "node_modules/original-tool.txt").read_text() == "fixed original dependency bytes"
    assert (current / "node_modules/part").resolve() == current / "packages/part"
    assert (current / "node_modules/part/current-source.ts").read_text() == "current-candidate-source"
    assert "PNPM_ORIGINAL_FIXED_WORKSPACES" in result.stdout


@pytest.mark.parametrize("change", ["input-bytes", "missing-input", "source-copy", "module-root", "link-target"])
def test_unbound_original_contract_is_rejected(tmp_path, change):
    files, layout, _, _, _ = data_fixture(tmp_path)
    files, layout = dict(files), deepcopy(layout)
    if change == "input-bytes":
        files["package.json"] += b" "
    elif change == "missing-input":
        files.pop("pnpm-workspace.yaml")
    elif change == "source-copy":
        layout["workspace_production_files_exported"] = 1
    elif change == "module-root":
        layout["module_roots"].append("frozen-source/node_modules")
    else:
        layout["workspace_links"][0]["workspace_target"] = "frozen-other-source"
    with pytest.raises(ContractError):
        fixed_contract(layout, files)


@pytest.mark.parametrize("change", ["identity", "topology", "symlink-input", "layout", "contract"])
def test_redirected_or_different_layouts_do_not_get_an_install(tmp_path, change):
    _, _, payload, current, tool = data_fixture(tmp_path)
    if change == "identity":
        p = current / "packages/part/package.json"
        value = json.loads(p.read_text())
        value["name"] = "some-other-source"
        p.write_text(json.dumps(value))
    elif change == "topology":
        (current / "pnpm-workspace.yaml").write_text("packages: [elsewhere]\n")
    elif change == "symlink-input":
        (current / "pnpm-lock.yaml").unlink()
        (current / "pnpm-lock.yaml").symlink_to(payload / "transport.json")
    elif change == "layout":
        p = payload / "transport.json"
        value = json.loads(p.read_text())
        value["external_index_sha256"] = "b" * 64
        p.write_text(json.dumps(value))
    else:
        p = tool / CONFIG_NAME
        value = json.loads(p.read_text())
        value["candidate_metadata_selects_dependencies"] = True
        p.write_text(json.dumps(value))
    result = run(tool, payload, current)
    assert result.returncode == 2
    assert not (current / "node_modules").exists()
