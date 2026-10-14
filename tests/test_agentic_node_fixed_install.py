import copy
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from agentic_runtime_migration import tsjs_fixed_install as fixed
from agentic_runtime_migration import tsjs_renovate55 as adapter
from agentic_runtime_migration.tsjs_pnpm_payload import input_plan, export_payload
from reviewbench_maintainability.agentic.contracts import ContractError

LOCAL_KEY = "@own/tool@file:tools/local"
LOCAL_PATH = "node_modules/.pnpm/@own+tool@file+tools+local/node_modules/@own/tool"
DECLARATION = {LOCAL_KEY: {"directory": "tools/local", "installed_root": LOCAL_PATH}}


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "original"
    root.mkdir()
    (root / "package.json").write_text(json.dumps({
        "name": "fixture-project", "version": "1.0.0", "packageManager": "pnpm@9.14.4",
        "scripts": {"generate": "node tools/generate.mjs"},
        "devDependencies": {"@alias/tool": "file:tools/local", "external": "1.0.0"},
    }))
    (root / ".npmrc").write_text("enable-pre-post-scripts = true\nshell-emulator = true\n")
    (root / "pnpm-workspace.yaml").write_text("packages: []\n")
    lock = {
        "lockfileVersion": "9.0",
        "importers": {".": {"devDependencies": {
            "@alias/tool": {"specifier": "file:tools/local", "version": LOCAL_KEY},
            "external": {"specifier": "1.0.0", "version": "1.0.0"},
        }}},
        "packages": {
            LOCAL_KEY: {"resolution": {"directory": "tools/local", "type": "directory"}},
            "external@1.0.0": {"resolution": {"integrity": "sha512-retained-original-fixture"}},
        }, "snapshots": {LOCAL_KEY: {}, "external@1.0.0": {}},
    }
    (root / "pnpm-lock.yaml").write_text(yaml.safe_dump(lock))
    local = root / "tools/local"
    local.mkdir(parents=True)
    (local / "package.json").write_text('{"name":"@own/tool","version":"0.0.0","main":"index.js"}\n')
    (local / "index.js").write_text("module.exports = 'OLD_LOCAL_PRODUCTION';\n")
    copied = root / LOCAL_PATH
    copied.parent.mkdir(parents=True)
    shutil.copytree(local, copied)
    outer = root / "node_modules/@alias"
    outer.mkdir(parents=True)
    (outer / "tool").symlink_to("../.pnpm/@own+tool@file+tools+local/node_modules/@own/tool")
    external = root / "node_modules/.pnpm/external@1.0.0/node_modules/external"
    external.mkdir(parents=True)
    (external / "package.json").write_text('{"name":"external","version":"1.0.0","main":"index.js"}\n')
    (external / "index.js").write_text("module.exports = 'ORIGINAL_EXTERNAL_GRAPH';\n")
    (root / "node_modules/external").symlink_to(".pnpm/external@1.0.0/node_modules/external")
    return root


def test_default_planner_still_rejects_unbound_file_packages(source):
    with pytest.raises(ContractError, match="integrity"):
        input_plan(source)


def test_local_package_requires_explicit_source_and_copy_mapping(source):
    plan = input_plan(source, file_sources=DECLARATION)
    assert plan["importers"] == ["."]
    assert plan["workspace_names"]["@own/tool"] == "tools/local"
    assert plan["file_sources"][0]["installed_root"] == LOCAL_PATH
    assert "tools/local/package.json" in plan["input_hashes"]


@pytest.mark.parametrize("change", ["unknown", "escape", "overlap", "name", "resolution", "redirect"])
def test_source_mapping_cannot_point_at_unbound_or_redirected_input(source, change):
    mapping = copy.deepcopy(DECLARATION)
    if change == "unknown":
        mapping["unknown@file:unknown"] = mapping.pop(LOCAL_KEY)
    elif change == "escape":
        mapping[LOCAL_KEY]["directory"] = "../outside"
    elif change == "overlap":
        mapping[LOCAL_KEY]["directory"] = "node_modules/copied"
    elif change == "name":
        (source / "tools/local/package.json").write_text('{"name":"other"}')
    elif change == "resolution":
        lock = yaml.safe_load((source / "pnpm-lock.yaml").read_text())
        lock["packages"][LOCAL_KEY]["resolution"]["type"] = "tarball"
        (source / "pnpm-lock.yaml").write_text(yaml.safe_dump(lock))
    else:
        shutil.rmtree(source / "tools/local")
        (source / "tools/local").symlink_to(source / LOCAL_PATH)
    with pytest.raises(ContractError):
        input_plan(source, file_sources=mapping)


def test_export_omits_all_old_local_code_and_retains_alias_topology(source, tmp_path):
    payload = tmp_path / "payload"
    layout, index = fixed.build_payload(source, payload, input_plan(source, file_sources=DECLARATION))
    assert layout["workspace_production_files_exported"] == 0
    assert not (payload / LOCAL_PATH).exists()
    assert not (payload / LOCAL_PATH).is_symlink()
    assert len(layout["workspace_links"]) == 1
    assert layout["workspace_links"][0]["workspace_target"] == "tools/local"
    assert os.readlink(payload / "node_modules/@alias/tool") == os.readlink(source / "node_modules/@alias/tool")
    assert all(not item["path"].startswith(LOCAL_PATH) for item in index)
    assert all("OLD_LOCAL_PRODUCTION" not in p.read_text(errors="ignore")
               for p in payload.rglob("*") if p.is_file() and not p.is_symlink())
    assert (source / LOCAL_PATH / "index.js").read_text() == "module.exports = 'OLD_LOCAL_PRODUCTION';\n"


@pytest.mark.parametrize("change", ["copied_bytes", "copied_extra_file", "duplicate_copy"])
def test_export_rejects_changed_or_additional_copied_source(source, tmp_path, change):
    plan = input_plan(source, file_sources=DECLARATION)
    if change == "copied_bytes":
        (source / LOCAL_PATH / "index.js").write_text("different")
    elif change == "copied_extra_file":
        (source / LOCAL_PATH / "extra.js").write_text("extra")
    else:
        duplicate = source / "node_modules/unexpected-copy"
        shutil.copytree(source / "tools/local", duplicate)
    with pytest.raises(ContractError, match="local package differs|copied workspace"):
        export_payload(source, tmp_path / "bad", plan)


def node():
    executable = os.environ.get("REVIEWBENCH_TEST_NODE") or shutil.which("node")
    if not executable:
        pytest.skip("a real Node executable is required")
    return executable


def transport_fixture(source, tmp_path):
    payload = tmp_path / "payload"
    fixed.build_payload(source, payload, input_plan(source, file_sources=DECLARATION))
    current = tmp_path / "candidate"
    shutil.copytree(source, current, symlinks=True, ignore=shutil.ignore_patterns("node_modules"))
    entry = tmp_path / "transport.cjs"
    entry.write_text(fixed.SOURCE)
    return payload, current, entry


def invoke(payload, current, entry):
    return subprocess.run([node(), str(entry), str(payload), str(current)],
                          capture_output=True, text=True, timeout=30)


def test_current_candidate_local_source_and_new_helpers_are_executed(source, tmp_path):
    payload, current, entry = transport_fixture(source, tmp_path)
    (current / "tools/local/index.js").write_text("module.exports = require('./new-helper.js');\n")
    (current / "tools/local/new-helper.js").write_text("module.exports = 'CURRENT_CANDIDATE_HELPER';\n")
    result = invoke(payload, current, entry)
    assert result.returncode == 0, result.stderr
    checked = subprocess.run([node(), "-e", "console.log(require('@alias/tool')); console.log(require('external'));"],
                             cwd=current, capture_output=True, text=True, check=True)
    assert checked.stdout == "CURRENT_CANDIDATE_HELPER\nORIGINAL_EXTERNAL_GRAPH\n"
    assert (current / LOCAL_PATH).resolve() == current / "tools/local"
    assert (payload / LOCAL_PATH).exists() is False


def test_no_reinstall_mode_keeps_current_version_metadata_without_selecting_a_new_graph(source, tmp_path):
    payload, current, entry = transport_fixture(source, tmp_path)
    package = json.loads((current / "package.json").read_text())
    package["packageManager"] = "pnpm@9.15.0"
    package["devDependencies"]["external"] = "2.0.0"
    (current / "package.json").write_text(json.dumps(package))
    lock = yaml.safe_load((current / "pnpm-lock.yaml").read_text())
    lock["importers"]["."]["devDependencies"]["external"] = {"specifier": "2.0.0", "version": "2.0.0"}
    lock["packages"]["external@2.0.0"] = {"resolution": {"integrity": "new-declaration-not-an-install"}}
    (current / "pnpm-lock.yaml").write_text(yaml.safe_dump(lock))
    before = {name: (current / name).read_bytes() for name in ["package.json", "pnpm-lock.yaml"]}
    result = invoke(payload, current, entry)
    assert result.returncode == 0, result.stderr
    assert all((current / name).read_bytes() == value for name, value in before.items())
    observed = subprocess.check_output([node(), "-e", "console.log(require('external'))"], cwd=current, text=True)
    assert observed.strip() == "ORIGINAL_EXTERNAL_GRAPH"


@pytest.mark.parametrize("change", ["generate", "jest", "prehook", "pnpm", "npmrc", "local_manifest", "local_symlink"])
def test_current_source_cannot_redefine_original_command_routing_or_local_identity(source, tmp_path, change):
    payload, current, entry = transport_fixture(source, tmp_path)
    package = json.loads((current / "package.json").read_text())
    if change in {"generate", "jest", "prehook"}:
        package["scripts"][{"generate": "generate", "jest": "jest", "prehook": "prejest"}[change]] = "echo fake"
        (current / "package.json").write_text(json.dumps(package))
    elif change == "pnpm":
        package["pnpm"] = {"shellEmulator": False}
        (current / "package.json").write_text(json.dumps(package))
    elif change == "npmrc":
        (current / ".npmrc").write_text("enable-pre-post-scripts = false\n")
    elif change == "local_manifest":
        (current / "tools/local/package.json").write_text('{"name":"other","main":"index.js"}')
    else:
        shutil.rmtree(current / "tools/local")
        (current / "tools/local").symlink_to(source / "tools/local")
    result = invoke(payload, current, entry)
    assert result.returncode == 2 and "PNPM_TRANSPORT_ERROR" in result.stderr


def test_original_command_only_relocates_the_exact_manager():
    import shlex
    for args in (["corepack", "pnpm@9.14.4", "generate"],
                 ["corepack", "pnpm@9.14.4", "jest", "lib/logger/index.spec.ts", "--runInBand", "--coverage=false"]):
        assert shlex.split(adapter.command(args))[2:] == args[2:]
    with pytest.raises(ContractError):
        adapter.command(["corepack", "pnpm@9.15.0", "generate"])


def test_other_original_routes_are_unchanged():
    assert adapter.migrate(None, {"id": "unrelated"}, {}, {}) is None


@pytest.mark.parametrize("field", ["repo", "start_sha", "index", "case"])
def test_exact_renovate_contract_rejects_relabelled_inputs(field):
    task = {"id": adapter.ID, "repo": "renovatebot/renovate", "start_sha": adapter.START}
    oracle = {"index": 55, "pass_to_pass": 16}
    case = {}
    if field in task:
        task[field] = "different"
    elif field == "index":
        oracle[field] = 54
    else:
        case["build"] = ["skip"]
    with pytest.raises(ContractError, match="complete original"):
        adapter.migrate(None, task, oracle, case)
