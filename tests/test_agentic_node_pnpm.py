"""Transport fixtures only; never substitutes for Guardian's original Jest suite."""
from copy import deepcopy
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration.tsjs_pnpm import (
    bind_oracle, jest_pattern, test_command as pnpm_command, validate_case,
)
from agentic_runtime_migration.tsjs_pnpm_payload import SOURCE, check_inputs, export_payload, input_plan
from agentic_runtime_migration.node_pnpm_prepare import install_command, seed_store
from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash, file_hash


def integrity(data):
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()


def case():
    return {"status": "ready", "node": "22.17.1", "package_manager": "pnpm@10.11.0",
            "workspace": "modules/logger", "install_cwd": ".", "reinstall_golden": False,
            "install": ["corepack", "pnpm@10.11.0", "install", "--frozen-lockfile", "--ignore-scripts"],
            "test": ["corepack", "pnpm@10.11.0", "exec", "jest", "--group=-integration",
                     "test/logger.test.ts", "--runInBand", "--coverage=false"]}


def test_exact_original_versions_install_flags_and_exec_tail():
    value = case()
    assert validate_case(value) == "10.11.0"
    import shlex
    assert shlex.split(pnpm_command(value))[2:] == value["test"][2:]
    command = install_command("10.11.0")
    assert command[2:5] == value["install"][2:]
    assert "--network-concurrency=2" in command and "--child-concurrency=2" in command
    assert "--prefer-offline" in command
    assert "npm" not in command and "--filter" not in command


def test_case_and_all_six_logs_must_match_the_frozen_oracle():
    value = case()
    oracle = {"case_sha256": canonical_hash(value), "test_command": value["test"],
              "install_command": value["install"], "workspace": value["workspace"],
              "historical_node": value["node"], "package_manager": value["package_manager"],
              "start_signature": "jest:suites=1,tests=18"}
    for key, prefix in (("start_runs", "start"), ("source_only_golden_runs", "source-only-golden")):
        oracle[key] = [{"run": n, "exit_code": 0, "signature": oracle["start_signature"],
                        "log": f"tasks/40/{prefix}-run{n}.log"} for n in (1, 2, 3)]
    assert len(bind_oracle(value, oracle)) == 6
    for field in ("case_sha256", "test_command", "historical_node"):
        changed = deepcopy(oracle)
        changed[field] = "changed"
        with pytest.raises(ContractError, match="original Oracle"):
            bind_oracle(value, changed)
    del oracle["start_runs"][-1]
    with pytest.raises(ContractError, match="three start"):
        bind_oracle(value, oracle)


@pytest.mark.parametrize("change", ["version", "lock", "scripts", "build", "cwd", "exec"])
def test_unsupported_or_weakened_cases_fail_closed(change):
    value = case()
    if change == "version":
        value["package_manager"] = "pnpm@latest"
    elif change == "lock":
        value["install"].remove("--frozen-lockfile")
    elif change == "scripts":
        value["install"].remove("--ignore-scripts")
    elif change == "build":
        value["build"] = ["pnpm", "build"]
    elif change == "cwd":
        value["install_cwd"] = "modules/logger"
    else:
        value["test"][0] = "npm"
    with pytest.raises(ContractError):
        validate_case(value)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "original"
    root.mkdir()
    packages = {".": "fixture-root", "modules/app": "fixture-app", "modules/helper": "fixture-helper"}
    for name, package in packages.items():
        path = root / name
        path.mkdir(parents=True, exist_ok=True)
        (path / "package.json").write_text(json.dumps({"name": package, "version": "1.0.0",
                                                       "packageManager": "pnpm@10.11.0"}))
    (root / "pnpm-workspace.yaml").write_text("packages:\n  - modules/*\n")
    lock = {"lockfileVersion": "9.0", "importers": {p: {} for p in packages},
            "packages": {"external@1.0.0": {"resolution": {"integrity": integrity(b"fixture-package")}}}}
    (root / "pnpm-lock.yaml").write_text(yaml.safe_dump(lock))
    (root / "modules/app/main.cjs").write_text(
        "console.log('VALUE=' + require('external')());\n")
    (root / "modules/helper/index.js").write_text(
        "// OLD_WORKSPACE_SOURCE_NOT_A_DEPENDENCY\nmodule.exports={value:require('./unrelated.cjs')};\n")
    (root / "modules/helper/unrelated.cjs").write_text("module.exports=7;\n")
    external = root / "node_modules/.pnpm/external@1.0.0/node_modules/external"
    external.mkdir(parents=True)
    (external / "package.json").write_text('{"name":"external","version":"1.0.0","main":"index.js"}')
    (external / "index.js").write_text("module.exports=()=>require('helper').value+1;\n")
    (external.parent / "helper").symlink_to("../../../../modules/helper")
    (root / "node_modules/external").symlink_to(".pnpm/external@1.0.0/node_modules/external")
    modules = root / "modules/app/node_modules"
    modules.mkdir()
    (modules / "external").symlink_to("../../../node_modules/.pnpm/external@1.0.0/node_modules/external")
    (modules / "helper").symlink_to("../../helper")
    return root


def test_exports_whole_relative_topology_but_no_workspace_production(source, tmp_path):
    plan = input_plan(source)
    payload = tmp_path / "payload"
    layout, index = export_payload(source, payload, plan)
    assert set(layout["module_roots"]) == {"node_modules", "modules/app/node_modules"}
    assert len(layout["workspace_links"]) == 2
    assert layout["workspace_production_files_exported"] == 0
    assert os.readlink(payload / "modules/app/node_modules/external").startswith("../../../node_modules/.pnpm/")
    assert not (payload / "modules/helper").exists()
    assert not (payload / "modules/app/node_modules/helper").is_symlink()
    assert all("OLD_WORKSPACE_SOURCE" not in p.read_text(errors="ignore")
               for p in payload.rglob("*") if p.is_file() and not p.is_symlink())
    assert all("node_modules/" in row["path"] for row in index)
    check_inputs(source, plan["input_hashes"])


@pytest.mark.parametrize("change", ["injected", "local_file", "copied_workspace", "escaping_link",
                                   "absolute_link", "lock_changed", "hook"])
def test_no_source_copies_injection_or_unbound_topology(source, tmp_path, change):
    plan = input_plan(source)
    if change == "injected":
        (source / "node_modules/.modules.yaml").write_text("injectedDeps:\n  modules/helper: [helper]\n")
    elif change == "local_file":
        lock = yaml.safe_load((source / "pnpm-lock.yaml").read_text())
        lock["packages"]["helper@file:modules/helper"] = {"resolution": {"directory": "modules/helper"}}
        (source / "pnpm-lock.yaml").write_text(yaml.safe_dump(lock))
        with pytest.raises(ContractError, match="integrity"):
            input_plan(source)
        return
    elif change == "copied_workspace":
        path = source / "node_modules/copied"
        path.mkdir()
        (path / "package.json").write_text('{"name":"fixture-helper"}')
        (path / "old.js").write_text("old production")
    elif change in {"escaping_link", "absolute_link"}:
        (source / "node_modules/bad").symlink_to("/etc/passwd" if change == "absolute_link" else "../../escape")
    elif change == "hook":
        (source / ".pnpmfile.cjs").write_text("module.exports={hooks:{}}")
        with pytest.raises(ContractError, match="hooks"):
            input_plan(source)
        return
    else:
        (source / "pnpm-lock.yaml").write_text("changed")
    with pytest.raises(ContractError):
        export_payload(source, tmp_path / "bad", plan)


def test_exact_jest_counts_and_suite_totals_are_not_weakened():
    pattern = jest_pattern("jest:suites=1,tests=18")
    good = "Test Suites: 1 passed, 1 total\nTests:       18 passed, 18 total\n"
    assert re.search(pattern, good, re.MULTILINE)
    for bad in (good.replace("18 total", "19 total"), good.replace("1 total", "2 total"),
                good.replace("18 passed", "17 passed, 1 skipped"), good.replace("18 passed", "17 passed, 1 failed")):
        assert not re.search(pattern, bad, re.MULTILINE)


def test_only_exact_lock_selected_cache_files_are_seeded(tmp_path):
    store, dest = tmp_path / "old/v10", tmp_path / "new/v10"
    package_integrity = integrity(b"original package tar")
    key = hashlib.sha512(b"original package tar").hexdigest()[:64]
    data = b"original external bytes"
    digest = hashlib.sha512(data).hexdigest()
    content = store / "files" / digest[:2] / digest[2:]
    content.parent.mkdir(parents=True)
    content.write_bytes(data)
    index = store / "index" / key[:2] / (key[2:] + "-example@1.0.0.json")
    index.parent.mkdir(parents=True)
    index.write_text(json.dumps({"name": "example", "version": "1.0.0",
                                "files": {"index.js": {"integrity": integrity(data), "mode": 0o644}},
                                "sideEffects": {"not-reused": {"added": {}}}}))
    original_hash = file_hash(index)
    result = seed_store({"example@1.0.0": {"resolution": {"integrity": package_integrity}}}, store, dest)
    assert result["indexes_reused"] == 1 and result["files_reused"] == 1
    assert result["side_effects_reused"] is False and file_hash(index) == original_hash
    copied = json.loads((dest / index.relative_to(store)).read_text())
    assert "sideEffects" not in copied
    content.write_bytes(b"changed")
    with pytest.raises(ContractError, match="integrity"):
        seed_store({"example@1.0.0": {"resolution": {"integrity": package_integrity}}}, store, tmp_path / "bad")


def test_real_node_current_workspace_helper_error_and_restoration(source, tmp_path):
    node = os.environ.get("REVIEWBENCH_PNPM_FIXTURE_NODE") or shutil.which("node")
    if not node:
        pytest.skip("real Node executable is required for the transport fixture")
    payload = tmp_path / "payload"
    plan = input_plan(source)
    layout, _ = export_payload(source, payload, plan)
    current = tmp_path / "current"
    shutil.copytree(source, current, ignore=shutil.ignore_patterns("node_modules"))
    # The retained source is removed. Only the fresh candidate can provide helper.
    shutil.rmtree(source)
    helper = current / "modules/helper/unrelated.cjs"
    helper.write_text("module.exports=41;\n")
    transport = tmp_path / "transport.cjs"
    transport.write_text(SOURCE)
    env = {**os.environ, "NODE_OPTIONS": "--max-old-space-size=1536",
           "COREPACK_ENABLE_NETWORK": "0", "npm_config_offline": "true",
           "PATH": str(Path(node).parent) + ":" + os.environ.get("PATH", "/usr/bin:/bin")}
    receipts = []

    def run(label, argv, cwd=current):
        done = subprocess.run(argv, cwd=cwd, env=env, text=True, capture_output=True, timeout=30)
        receipts.append({"label": label, "command": argv, "cwd": str(cwd),
                         "exit_code": done.returncode, "stdout": done.stdout, "stderr": done.stderr})
        return done

    staged = run("transport", [node, str(transport), str(payload), str(current)])
    assert staged.returncode == 0, staged.stderr
    for row in layout["workspace_links"]:
        assert (current / row["path"]).resolve() == (current / row["workspace_target"]).resolve()
    assert (current / "modules/app/node_modules/external").resolve().is_relative_to(current)
    manager = os.environ.get("REVIEWBENCH_PNPM_FIXTURE_MANAGER")
    command = ([node, manager, "exec", "node", "main.cjs"] if manager else [node, "main.cjs"])
    cwd = current / "modules/app"
    good = run("current_changed_workspace", command, cwd)
    assert good.returncode == 0 and good.stdout.strip() == "VALUE=42", good.stderr
    helper.write_text("module.exports = ;\n")
    bad = run("unrelated_helper_compile_error", command, cwd)
    assert bad.returncode != 0 and "SyntaxError" in bad.stderr
    helper.write_text("module.exports=41;\n")
    restored = run("restored_current_source", command, cwd)
    assert restored.returncode == 0 and restored.stdout.strip() == "VALUE=42"
    check_inputs(current, plan["input_hashes"])
    evidence = os.environ.get("REVIEWBENCH_PNPM_FIXTURE_EVIDENCE")
    if evidence:
        path = Path(evidence)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"kind": "synthetic-transport-not-Guardian-proof",
                                    "node_version": subprocess.check_output([node, "--version"], text=True).strip(),
                                    "receipts": receipts, "lock_unchanged": True,
                                    "workspace_production_files_exported": 0}, indent=2) + "\n")
