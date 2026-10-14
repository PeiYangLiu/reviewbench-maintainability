"""Real local Node transport tests, never benchmark calibration or model calls."""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration.node_yarn_prepare import input_plan
from agentic_runtime_migration.tsjs_pnpm_payload import export_payload, SOURCE as STRICT_TRANSPORT
from agentic_runtime_migration.tsjs_yarn1 import test_command as original_command, test_pattern as pattern
from agentic_runtime_migration.tsjs_yarn1_transport import SOURCE, original_contract
from reviewbench_maintainability.agentic.contracts import ContractError


def put(root, name, value):
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(value) if isinstance(value, dict) else value)
    return p


def node():
    result = os.environ.get("REVIEWBENCH_TEST_NODE") or shutil.which("node")
    if not result:
        pytest.skip("real Node is needed for current-source transport controls")
    return result


@pytest.fixture
def fixture(tmp_path):
    source = tmp_path / "original"
    put(source, "package.json", {
        "name": "root", "version": "1.0.0", "workspaces": ["packages/*"],
        "scripts": {"test": "jest"}, "dependencies": {"external": "1.0.0"},
    })
    put(source, "yarn.lock", '# yarn lockfile v1\nexternal@1.0.0:\n  version "1.0.0"\n')
    put(source, "packages/local/package.json", {
        "name": "@sample/local", "version": "1.0.0", "main": "index.js",
        "scripts": {"build": "node build.js"},
    })
    put(source, "packages/local/index.js", "module.exports='OLD_LOCAL_SOURCE';\n")
    put(source, "node_modules/external/package.json", {"name": "external", "version": "1.0.0", "main": "index.js"})
    external = put(source, "node_modules/external/index.js", "module.exports='ORIGINAL_EXTERNAL_GRAPH';\n")
    external.chmod(0o755)
    (source / "node_modules/@sample").mkdir()
    (source / "node_modules/@sample/local").symlink_to("../../packages/local")
    put(source, "packages/local/node_modules/nested/package.json", {"name": "nested", "version": "1.0.0"})
    put(source, "packages/local/node_modules/nested/index.js", "module.exports='ORIGINAL_NESTED';\n")
    plan = input_plan(source)
    payload = tmp_path / "payload"
    layout, index = export_payload(source, payload, plan)
    record = {**layout, "source_manifest_or_lock_rewritten": False, "yarn_version": "1.22.22"}
    files = {name: (source / name).read_bytes() for name in record["input_hashes"]}
    contract = original_contract(record, files)
    put(tmp_path, "original-install.json", contract)
    entry = put(tmp_path, "transport.cjs", SOURCE)
    candidate = tmp_path / "candidate"
    shutil.copytree(source, candidate, symlinks=True, ignore=shutil.ignore_patterns("node_modules"))
    return source, payload, candidate, entry, record, files


def invoke(fixture):
    _, payload, candidate, entry, _, _ = fixture
    return subprocess.run([node(), str(entry), str(payload), str(candidate)],
                          capture_output=True, text=True, timeout=30)


def test_current_workspace_source_and_new_helpers_execute(fixture):
    source, payload, current, _, _, _ = fixture
    put(current, "packages/local/index.js", "module.exports=require('./new-helper.js');\n")
    put(current, "packages/local/new-helper.js", "module.exports='CURRENT_CANDIDATE_HELPER';\n")
    result = invoke(fixture)
    assert result.returncode == 0, result.stderr
    assert "YARN1_ORIGINAL_FIXED_INSTALL" in result.stdout
    assert "YARN1_CURRENT_WORKSPACE" in result.stdout
    observed = subprocess.check_output([node(), "-e",
        "console.log(require('@sample/local'));console.log(require('external'));"
        "console.log(require('./packages/local/node_modules/nested'));"], cwd=current, text=True)
    assert observed == "CURRENT_CANDIDATE_HELPER\nORIGINAL_EXTERNAL_GRAPH\nORIGINAL_NESTED\n"
    assert (current / "node_modules/@sample/local").resolve() == current / "packages/local"
    assert not (payload / "node_modules/@sample/local").is_symlink()
    assert "OLD_LOCAL_SOURCE" in (source / "packages/local/index.js").read_text()
    assert (current / "node_modules/external/index.js").stat().st_mode & 0o777 == 0o755


def test_changed_candidate_metadata_retained_with_original_installed_graph(fixture):
    _, _, current, _, _, _ = fixture
    value = json.loads((current / "package.json").read_text())
    value["dependencies"]["external"] = "2.0.0"
    value["version"] = "9.0.0"
    put(current, "package.json", value)
    put(current, "yarn.lock", '# yarn lockfile v1\nexternal@2.0.0:\n  version "2.0.0"\n')
    before = {name: (current / name).read_bytes() for name in ["package.json", "yarn.lock"]}
    result = invoke(fixture)
    assert result.returncode == 0, result.stderr
    assert all((current / name).read_bytes() == data for name, data in before.items())
    assert subprocess.check_output([node(), "-e", "console.log(require('external'))"],
                                   cwd=current, text=True) == "ORIGINAL_EXTERNAL_GRAPH\n"


def test_generic_strict_transport_remains_strict(fixture):
    _, _, current, entry, _, _ = fixture
    put(current, "yarn.lock", "different candidate metadata")
    entry.write_text(STRICT_TRANSPORT)
    result = invoke(fixture)
    assert result.returncode == 2 and "frozen pnpm input changed" in result.stderr
    assert not (current / "node_modules").exists()


@pytest.mark.parametrize("change", ["scripts", "prehook", "identity", "workspaces", "bin",
                                  "new_yarnrc", "new_npmrc", "local_scripts", "local_symlink", "lock_symlink"])
def test_original_execution_routing_and_workspace_bindings_are_guarded(fixture, change):
    source, _, current, _, _, _ = fixture
    package = json.loads((current / "package.json").read_text())
    if change in {"scripts", "prehook"}:
        package["scripts"]["jest" if change == "scripts" else "prejest"] = "echo changed"
    elif change == "identity":
        package["name"] = "different"
    elif change == "workspaces":
        package["workspaces"] = []
    elif change == "bin":
        package["bin"] = {"jest": "fake.js"}
    elif change in {"new_yarnrc", "new_npmrc"}:
        put(current, ".yarnrc" if change == "new_yarnrc" else ".npmrc", "changed")
    elif change == "local_scripts":
        local = json.loads((current / "packages/local/package.json").read_text())
        local["scripts"]["build"] = "different"
        put(current, "packages/local/package.json", local)
    elif change == "local_symlink":
        shutil.rmtree(current / "packages/local")
        (current / "packages/local").symlink_to(source / "packages/local")
    else:
        (current / "yarn.lock").unlink()
        (current / "yarn.lock").symlink_to(source / "yarn.lock")
    put(current, "package.json", package)
    result = invoke(fixture)
    assert result.returncode == 2 and "YARN1_TRANSPORT_ERROR" in result.stderr
    assert not (current / "node_modules").exists()


@pytest.mark.parametrize("change", ["hash", "missing", "root", "link", "source", "rewritten", "manager"])
def test_original_contract_rejects_unbound_or_relabelled_inputs(fixture, change):
    _, _, _, _, record, files = fixture
    record, files = copy.deepcopy(record), dict(files)
    if change == "hash":
        files["yarn.lock"] = b"changed"
    elif change == "missing":
        del files["package.json"]
    elif change == "root":
        record["module_roots"].append("unbound/node_modules")
    elif change == "link":
        record["workspace_links"][0]["workspace_target"] = "unbound"
    elif change == "source":
        record["workspace_production_files_exported"] = 1
    elif change == "rewritten":
        record["source_manifest_or_lock_rewritten"] = True
    else:
        record["yarn_version"] = "2.0.0"
    with pytest.raises(ContractError):
        original_contract(record, files)


@pytest.mark.parametrize("name", ["workspace_links", "module_roots", "input_hashes"])
def test_payload_layout_must_match_the_original_contract_before_copy(fixture, name):
    _, payload, current, _, _, _ = fixture
    layout = json.loads((payload / "transport.json").read_text())
    if name == "input_hashes":
        layout[name]["yarn.lock"] = "0" * 64
    elif name == "module_roots":
        layout[name] = ["node_modules"]
    else:
        layout[name] = []
    put(payload, "transport.json", layout)
    result = invoke(fixture)
    assert result.returncode == 2 and "layout binding changed" in result.stderr
    assert not (current / "node_modules").exists()


def test_original_node_manager_and_complete_test_argv_are_preserved():
    case = {"status": "ready", "package_manager": "yarn@1.22.22", "node": "16.20.2",
            "install_cwd": ".", "workspace": ".", "reinstall_golden": False,
            "install": ["corepack", "yarn@1.22.22", "install", "--frozen-lockfile",
                        "--ignore-scripts", "--network-timeout", "600000"],
            "test": ["corepack", "yarn@1.22.22", "vitest", "run", "test.ts", "--threads=false"]}
    assert shlex.split(original_command(case))[2:] == case["test"][2:]
    case["node"] = "18.20.8"
    case["test"] = ["corepack", "yarn@1.22.22", "jest", "--config", "packages/core/jest.config.ts",
                    "packages/core/test.ts", "--runInBand"]
    assert shlex.split(original_command(case))[2:] == case["test"][2:]
    assert not any(value in original_command(case) for value in ["--passWithNoTests", "--forceExit"])


@pytest.mark.parametrize("text", [
    "Test Files 1 passed (1)\nTests 2 passed (2)\n",
    "[log] Test Files 1 passed (1)\n[log]     Tests 2 passed (2)\n",
    "\x1b[2mTest Files \x1b[22m \x1b[32m1 passed\x1b[39m (1)\n"
    "\x1b[2m     Tests \x1b[22m \x1b[32m2 passed\x1b[39m (2)\n",
])
def test_original_complete_vitest_summaries_match_without_weakening_counts(text):
    assert re.search(pattern("vitest:files=1,tests=2"), text, re.M)
    assert not re.search(pattern("vitest:files=1,tests=3"), text, re.M)


@pytest.mark.parametrize("text", [
    "Test Files 1 passed (1)\nTests 2 passed | 1 failed (3)\n",
    "Test Files 1 passed (1)\nTests 2 passed (3)\n",
    "Test Files 1 passed (2)\nTests 2 passed (2)\n",
    "Test Files 1 passed (1)\nTests 1 passed | 1 skipped (2)\n",
])
def test_partial_or_changed_vitest_counts_are_not_calibration_success(text):
    assert not re.search(pattern("vitest:files=1,tests=2"), text, re.M)


def test_complete_original_jest_signature_stays_strict():
    assert re.search(pattern("jest:suites=1,tests=3"),
                     "Test Suites: 1 passed, 1 total\nTests: 3 passed, 3 total\n", re.M)
    assert not re.search(pattern("jest:suites=1,tests=3"),
                         "Test Suites: 1 passed, 1 total\nTests: 2 passed, 1 skipped, 3 total\n", re.M)


def test_original_non_ascii_test_filenames_are_not_git_display_strings(tmp_path):
    from types import SimpleNamespace
    from agentic_runtime_migration.tsjs_yarn1 import source_names
    from reviewbench_maintainability.agentic.contracts import repo_path
    repository = tmp_path / "repository"
    repository.mkdir()
    names = {
        "packages/core/src/modules/message-pìckup/__tests__/MessagePickupModule.test.ts",
        "packages/core/__tests__/normal.test.ts",
    }
    for name in names:
        put(repository, name, "export const fixture = true;\n")
    commands = [
        ["git", "init", "-q"],
        ["git", "add", "."],
        ["git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
         "commit", "-qm", "original Unicode filename fixture"],
    ]
    for command in commands:
        subprocess.run(command, cwd=repository, check=True, capture_output=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
    ctx = SimpleNamespace(repo=lambda task: repository)
    observed = source_names(ctx, {"start_sha": commit})
    assert set(observed) == names
    assert all(repo_path(name) == name for name in observed)
    displayed = subprocess.check_output(
        ["git", "-c", "core.quotePath=true", "ls-tree", "-r", "--name-only", commit],
        cwd=repository, text=True)
    assert "\\303\\254" in displayed
