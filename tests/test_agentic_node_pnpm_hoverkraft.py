"""Adapter controls, not substitutes for the three original private Vitest tests."""
import base64
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import tsjs_lane, tsjs_pnpm, tsjs_pnpm9
from agentic_runtime_migration.node_pnpm9_prepare import seed_store_v3, reused_node
from agentic_runtime_migration.node_pnpm_prepare import install_command
from agentic_runtime_migration.tsjs_pnpm9_transport import SOURCE, SCHEMA, build_payload
from agentic_runtime_migration.tsjs_pnpm_payload import input_plan, check_inputs
from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash, file_hash


def integrity(data):
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()


def fixed_case():
    return {
        "status": "ready", "node": "22.17.1", "package_manager": "pnpm@9.14.4",
        "workspace": "packages/app", "install_cwd": ".", "reinstall_golden": False,
        "install": ["corepack", "pnpm@9.14.4", "install", "--frozen-lockfile", "--ignore-scripts"],
        "test": ["corepack", "pnpm@9.14.4", "exec", "vitest", "run", "src/original.session.spec.ts"],
        "session_files": [{"source": "session/fixture/original.spec.ts",
                           "destination": "packages/app/src/original.session.spec.ts"}],
        "test_paths": ["packages/app/src/original.session.spec.ts"],
    }


def fixed_oracle(case):
    result = {
        "case_sha256": canonical_hash(case), "test_command": case["test"],
        "install_command": case["install"], "workspace": case["workspace"],
        "historical_node": case["node"], "package_manager": case["package_manager"],
        "start_signature": "vitest:files=1,tests=3", "golden_signature": "vitest:files=1,tests=3",
        "session_files": [{**row, "sha256": "a" * 64} for row in case["session_files"]],
    }
    for key, prefix in (("start_runs", "start"), ("source_only_golden_runs", "source-only-golden")):
        result[key] = [{"run": n, "exit_code": 0, "signature": result["start_signature"],
                        "log": f"tasks/fixture/{prefix}-run{n}.log"} for n in (1, 2, 3)]
    return result


def test_exact_private_session_case_and_all_original_commands_are_bound():
    case = fixed_case()
    version, logs = tsjs_pnpm9.validate_case(case, fixed_oracle(case))
    assert version == "9.14.4" and len(logs) == 6
    assert shlex.split(tsjs_pnpm9.session_command(case))[2:] == case["test"][2:]
    public = shlex.split(tsjs_pnpm9.public_command())
    assert public[2:] == ["exec", "tsc", "--build", "tsconfig.lib.json", "--force", "--pretty", "false"]
    assert "vitest" not in public and ".spec.ts" not in tsjs_pnpm9.public_command()
    argv = install_command(version)
    assert argv[2:5] == ["install", "--frozen-lockfile", "--ignore-scripts"]
    assert "--network-concurrency=2" in argv and "--child-concurrency=2" in argv
    assert "--prefer-offline" in argv and "--filter" not in argv


@pytest.mark.parametrize("change", ["major", "floating", "install_scripts", "unfrozen",
                                   "golden_install", "build", "test_argv", "missing_session",
                                   "session_hash", "destination", "signature", "missing_log"])
def test_session_binding_and_unsupported_case_changes_fail_closed(change):
    case = fixed_case()
    if change == "major":
        case["package_manager"] = "pnpm@10.11.0"
    elif change == "floating":
        case["package_manager"] = "pnpm@latest"
    elif change == "install_scripts":
        case["install"].remove("--ignore-scripts")
    elif change == "unfrozen":
        case["install"].remove("--frozen-lockfile")
    elif change == "golden_install":
        case["reinstall_golden"] = True
    elif change == "build":
        case["build"] = ["pnpm", "build"]
    elif change == "test_argv":
        case["test"][3] = "jest"
    elif change == "missing_session":
        case["session_files"] = []
    elif change == "destination":
        case["session_files"][0]["destination"] = "packages/app/src/main.ts"
    oracle = fixed_oracle(case)
    if change == "session_hash":
        oracle["session_files"][0]["sha256"] = "unbound"
    elif change == "signature":
        oracle["golden_signature"] = "vitest:files=1,tests=2"
    elif change == "missing_log":
        oracle["start_runs"].pop()
    with pytest.raises(ContractError):
        tsjs_pnpm9.validate_case(case, oracle)


@pytest.mark.parametrize("field", ["case_sha256", "test_command", "historical_node", "package_manager"])
def test_released_oracle_identity_is_not_replaced_by_the_opt_in(field):
    case = fixed_case()
    oracle = fixed_oracle(case)
    oracle[field] = "changed"
    with pytest.raises(ContractError, match="original Oracle"):
        tsjs_pnpm9.validate_case(case, oracle)


def test_original_vitest_three_total_and_one_file_are_required():
    pattern = tsjs_lane.success_pattern("vitest:files=1,tests=3")
    good = " Test Files  1 passed (1)\n      Tests  3 passed (3)\n"
    assert re.search(pattern, good, re.MULTILINE)
    for bad in (good.replace("(3)", "(4)"), good.replace("3 passed", "2 passed | 1 skipped"),
                good.replace("1 passed", "2 passed"), "RUN v3.2.4\n"):
        assert not re.search(pattern, bad, re.MULTILINE)


@pytest.mark.parametrize("route", ["pnpm9", "guardian", "ordinary"])
def test_opt_in_dispatch_does_not_change_guardian_or_existing_paths(tmp_path, monkeypatch, route):
    legacy = tmp_path / "legacy/tsjs"
    legacy.mkdir(parents=True)
    (legacy / "cases.json").write_text(json.dumps({"cases": {"fixture": fixed_case()}}))
    config = {"migration": {}}
    if route != "ordinary":
        key = "tsjs_pnpm9_dependencies" if route == "pnpm9" else "tsjs_pnpm_dependencies"
        config["migration"][key] = {"fixture": {"path": "/explicit", "sha256": "a" * 64}}
    ctx = SimpleNamespace(legacy=legacy.parent, config=config)
    monkeypatch.setattr(tsjs_pnpm9, "migrate", lambda *args: "pnpm9")
    monkeypatch.setattr(tsjs_pnpm, "migrate", lambda *args: "guardian")
    def ordinary(*args):
        raise ContractError("unchanged ordinary route")
    monkeypatch.setattr(tsjs_lane, "node_asset", ordinary)
    if route == "ordinary":
        with pytest.raises(ContractError, match="unchanged ordinary route"):
            tsjs_lane.migrate(ctx, {"id": "fixture"}, {})
    else:
        assert tsjs_lane.migrate(ctx, {"id": "fixture"}, {}) == route


def test_v3_uses_full_lock_sri_named_content_and_excludes_side_effects(tmp_path):
    retained, dest = tmp_path / "retained/v3", tmp_path / "new/v3"
    package_integrity = integrity(b"original tarball")
    key = hashlib.sha512(b"original tarball").hexdigest()
    content = b"external executable bytes"
    digest = hashlib.sha512(content).hexdigest()
    original = retained / "files" / digest[:2] / (digest[2:] + "-exec")
    original.parent.mkdir(parents=True)
    original.write_bytes(content)
    original.chmod(0o755)
    index = retained / "files" / key[:2] / (key[2:] + "-index.json")
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(json.dumps({"name": "external", "version": "1.0.0",
                                "files": {"bin/tool": {"integrity": integrity(content), "mode": 0o755}},
                                "sideEffects": {"not-reused": {}}}))
    before = file_hash(index)
    packages = {"external@1.0.0": {"resolution": {"integrity": package_integrity}}}
    result = seed_store_v3(packages, retained, dest)
    assert (result["indexes_reused"], result["files_reused"], result["side_effects_reused"]) == (1, 1, False)
    assert file_hash(index) == before
    assert "sideEffects" not in json.loads((dest / index.relative_to(retained)).read_text())
    assert (dest / original.relative_to(retained)).stat().st_mode & 0o111
    assert seed_store_v3(packages, retained, dest)["files_reused"] == 0
    (dest / original.relative_to(retained)).write_bytes(b"tampered seeded file")
    with pytest.raises(ContractError, match="seeded content"):
        seed_store_v3(packages, retained, dest)
    original.write_bytes(b"tampered original file")
    with pytest.raises(ContractError, match="retained content"):
        seed_store_v3(packages, retained, tmp_path / "bad")


def test_node_reuse_requires_the_immutable_existing_receipt(tmp_path):
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({"status": "prepared", "node_version": "18.0.0"}))
    with pytest.raises(ContractError, match="different version"):
        reused_node(receipt, file_hash(receipt), "22.17.1", tmp_path)
    with pytest.raises(ContractError, match="receipt changed"):
        reused_node(receipt, "f" * 64, "22.17.1", tmp_path)


def real_tools():
    node = os.environ.get("REVIEWBENCH_PNPM_FIXTURE_NODE") or shutil.which("node")
    parser = os.environ.get("REVIEWBENCH_PNPM9_FIXTURE_YAML")
    manager = os.environ.get("REVIEWBENCH_PNPM9_FIXTURE_MANAGER")
    if not node or not parser or not manager:
        pytest.skip("real Node, pinned YAML2 and pnpm9 paths required for transport controls")
    return node, Path(parser), manager


def record_evidence(name, value):
    base = os.environ.get("REVIEWBENCH_PNPM9_FIXTURE_EVIDENCE")
    if base:
        path = Path(base) / (name + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2) + "\n")


def make_source(root, parser):
    manifests = {
        ".": {"name": "fixture-root", "packageManager": "pnpm@9.14.4"},
        "packages/app": {"name": "fixture-app", "dependencies": {"external": "1.0.0", "unused": "^1.0.0"},
                         "devDependencies": {"another-unused": "1.0.0"}},
        "packages/helper": {"name": "fixture-helper", "main": "index.cjs"},
    }
    for name, manifest in manifests.items():
        directory = root / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "package.json").write_text(json.dumps(manifest))
    parser_version = json.loads((parser / "package.json").read_text())["version"]
    lock = {
        "lockfileVersion": "9.0", "settings": {"autoInstallPeers": True},
        "importers": {key: {kind: {name: {"specifier": spec, "version": "1.0.0"}
                                      for name, spec in manifest[kind].items()}
                           for kind in ("dependencies", "devDependencies") if kind in manifest}
                      for key, manifest in manifests.items()},
        "packages": {"external@1.0.0": {"resolution": {"integrity": integrity(b"fixture external")}},
                     "yaml@" + parser_version: {"resolution": {"integrity": integrity(b"fixture parser binding")}}},
        "snapshots": {"external@1.0.0": {}, "yaml@" + parser_version: {}},
    }
    (root / "pnpm-workspace.yaml").write_text("packages:\n  - packages/*\n")
    (root / "pnpm-lock.yaml").write_text(yaml.safe_dump(lock))
    (root / ".npmrc").write_text("strict-peer-dependencies=false\n")
    (root / "packages/app/main.cjs").write_text("console.log('VALUE='+require('external')());\n")
    (root / "packages/helper/index.cjs").write_text(
        "// OLD_FIXTURE_WORKSPACE_SOURCE_NOT_A_DEPENDENCY\nmodule.exports={value:require('./unrelated.cjs')};\n")
    (root / "packages/helper/unrelated.cjs").write_text("module.exports=7;\n")
    external = root / "node_modules/.pnpm/external@1.0.0/node_modules/external"
    external.mkdir(parents=True)
    (external / "package.json").write_text('{"name":"external","version":"1.0.0","main":"index.cjs"}')
    (external / "index.cjs").write_text("module.exports=()=>require('helper').value+1;\n")
    (external.parent / "helper").symlink_to("../../../../packages/helper")
    (root / "node_modules/external").symlink_to(".pnpm/external@1.0.0/node_modules/external")
    destination = root / ("node_modules/.pnpm/yaml@" + parser_version + "/node_modules/yaml")
    shutil.copytree(parser, destination)
    modules = root / "packages/app/node_modules"
    modules.mkdir()
    (modules / "external").symlink_to("../../../node_modules/.pnpm/external@1.0.0/node_modules/external")
    (modules / "helper").symlink_to("../../helper")


def test_real_transport_compatibility_private_inputs_and_current_source(tmp_path):
    node, parser, manager = real_tools()
    source, payload = tmp_path / "original", tmp_path / "payload"
    make_source(source, parser)
    plan = input_plan(source)
    layout, index, _ = build_payload(source, payload, plan)
    assert layout["workspace_production_files_exported"] == 0 and len(layout["workspace_links"]) == 2
    base = json.loads((payload / "compatibility-base.json").read_text())
    assert set(base) == {"schema_version", "workspace_text", "lock_text", "manifest_texts",
                         "exact_other_inputs", "yaml_parser"}
    assert base["schema_version"] == SCHEMA
    assert all("node_modules/" in row["path"] for row in index)
    assert not (payload / "packages/helper").exists()
    assert not (payload / "packages/app/node_modules/helper").is_symlink()
    assert "OLD_FIXTURE_WORKSPACE_SOURCE" not in json.dumps(base)
    template = tmp_path / "template"
    shutil.copytree(source, template, ignore=shutil.ignore_patterns("node_modules"))
    shutil.rmtree(source)  # A retained workspace can no longer satisfy the runtime.
    (template / "packages/helper/unrelated.cjs").write_text("module.exports=41;\n")
    transport = tmp_path / "transport.cjs"
    transport.write_text(SOURCE)
    receipts = []
    env = {**os.environ, "PATH": str(Path(node).parent) + ":" + os.environ.get("PATH", "/usr/bin:/bin"),
           "COREPACK_ENABLE_NETWORK": "0", "COREPACK_ENABLE_PROJECT_SPEC": "0",
           "npm_config_offline": "true", "NODE_OPTIONS": "--max-old-space-size=1536"}
    def run(label, argv, cwd):
        done = subprocess.run(argv, cwd=cwd, env=env, text=True, capture_output=True, timeout=30)
        receipts.append({"label": label, "command": argv, "cwd": str(cwd),
                         "exit_code": done.returncode, "stdout": done.stdout, "stderr": done.stderr})
        return done

    modes = ["same", "yaml_format", "remove_dependency", "remove_dev_dependency", "remove_both",
             "add_dependency", "upgrade", "specifier", "mismatched_removal", "manifest_only",
             "external_graph", "snapshot", "settings", "workspace", "new_importer", "override",
             "peer_metadata", "null_mapping", "nonfinite", "duplicate_yaml", "npmrc"]
    for mode in modes:
        current = tmp_path / mode
        shutil.copytree(template, current)
        manifest_path = current / "packages/app/package.json"
        manifest = json.loads(manifest_path.read_text())
        lock = yaml.safe_load((current / "pnpm-lock.yaml").read_text())
        importer = lock["importers"]["packages/app"]
        if mode in ("remove_dependency", "remove_both", "mismatched_removal"):
            del importer["dependencies"]["unused"]
            if mode != "mismatched_removal":
                del manifest["dependencies"]["unused"]
        if mode in ("remove_dev_dependency", "remove_both"):
            del importer["devDependencies"]["another-unused"]
            del manifest["devDependencies"]["another-unused"]
        if mode in ("add_dependency", "upgrade", "specifier"):
            name = "new-package" if mode == "add_dependency" else "external"
            manifest["dependencies"][name] = "2.0.0"
            importer["dependencies"][name] = {"specifier": "2.0.0",
                                            "version": "1.0.0" if mode == "specifier" else "2.0.0"}
        elif mode == "manifest_only":
            del manifest["dependencies"]["unused"]
        elif mode == "external_graph":
            lock["packages"]["extra@1.0.0"] = {"resolution": {"integrity": integrity(b"new")}}
        elif mode == "snapshot":
            lock["snapshots"]["external@1.0.0"] = {"dependencies": {"new": "1.0.0"}}
        elif mode == "settings":
            lock["settings"]["autoInstallPeers"] = False
        elif mode == "workspace":
            (current / "pnpm-workspace.yaml").write_text("packages: [packages/app]\n")
        elif mode == "new_importer":
            lock["importers"]["packages/new"] = {}
        elif mode == "override":
            lock["overrides"] = {"external": "2.0.0"}
        elif mode == "peer_metadata":
            manifest["peerDependencies"] = {"external": "1.0.0"}
        elif mode == "null_mapping":
            importer["dependencies"] = None
        elif mode == "nonfinite":
            lock["settings"]["autoInstallPeers"] = float("nan")
        elif mode == "npmrc":
            (current / ".npmrc").write_text("strict-peer-dependencies=true\n")
        manifest_path.write_text(json.dumps(manifest))
        # Same-input branch retains original bytes; every other branch exercises
        # real YAML parsing, not a mocked parser or a target-hash allowlist.
        if mode != "same":
            (current / "pnpm-lock.yaml").write_text(yaml.safe_dump(lock))
        if mode == "yaml_format":
            (current / "pnpm-workspace.yaml").write_text("---\n# formatting only\npackages: [packages/*]\n")
            (current / "pnpm-lock.yaml").write_text("---\n" + yaml.safe_dump(lock, sort_keys=False))
        if mode == "duplicate_yaml":
            with (current / "pnpm-lock.yaml").open("a") as stream:
                stream.write("settings: {}\n")
        done = run(mode, [node, str(transport), str(payload), str(current)], current)
        expected = 0 if mode in modes[:5] else 2
        assert done.returncode == expected, (mode, done.stdout, done.stderr)
        if expected:
            assert not (current / "node_modules").exists()
        elif mode == "same":
            check_inputs(current, plan["input_hashes"])
            for row in layout["workspace_links"]:
                assert (current / row["path"]).resolve() == (current / row["workspace_target"]).resolve()
            command = [node, manager, "exec", "node", "main.cjs"]
            good = run("changed_current_workspace", command, current / "packages/app")
            assert good.returncode == 0 and good.stdout.strip() == "VALUE=42", good.stderr
            helper = current / "packages/helper/unrelated.cjs"
            helper.write_text("module.exports = ;\n")
            bad = run("unrelated_current_helper_error", command, current / "packages/app")
            assert bad.returncode != 0 and "SyntaxError" in bad.stderr
            helper.write_text("module.exports=41;\n")
            restored = run("restored_current_helper", command, current / "packages/app")
            assert restored.returncode == 0 and restored.stdout.strip() == "VALUE=42"
    private = tmp_path / "private"
    destination = "packages/app/src/original.session.spec.ts"
    session = b"// fixture-only fixed private bytes; never public dependency metadata\n"
    (private / destination).parent.mkdir(parents=True)
    (private / destination).write_bytes(session)
    (private / "session-inputs.json").write_text(json.dumps([
        {"destination": destination, "sha256": hashlib.sha256(session).hexdigest()}]))
    inject = private / "inject.cjs"
    inject.write_text(tsjs_pnpm9.SESSION_SOURCE)
    current = tmp_path / "same"
    argv = [node, str(inject), str(private), str(current)]
    assert run("private_exact_bytes", argv, current).returncode == 0
    assert (current / destination).read_bytes() == session
    assert run("private_refuses_overwrite", argv, current).returncode == 2
    (current / destination).unlink()
    (current / "packages/app/src").rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (current / "packages/app/src").symlink_to(outside, target_is_directory=True)
    assert run("private_refuses_parent_link", argv, current).returncode == 2
    assert list(outside.iterdir()) == []
    record_evidence("transport", {"kind": "synthetic-controls-not-task-proof", "receipts": receipts,
                                 "source_checkout_removed": True, "production_exported": 0,
                                 "unchanged_start_lock_verified": True, "original_private_bytes_verified": True})


def test_real_public_build_covers_complete_current_project_reference_closure(tmp_path):
    node, _, manager = real_tools()
    tsc = os.environ.get("REVIEWBENCH_PNPM9_FIXTURE_TSC")
    if not tsc:
        pytest.skip("retained TypeScript executable required for full-project control")
    root = tmp_path / "current"
    app, helper = root / "packages/app", root / "packages/helper"
    for directory in (app, helper):
        (directory / "src").mkdir(parents=True)
        (directory / "package.json").write_text(json.dumps({"name": directory.name, "private": True}))
        (directory / "tsconfig.lib.json").write_text(json.dumps({
            "compilerOptions": {"composite": True, "strict": True, "skipLibCheck": False,
                                "rootDir": "src", "outDir": "dist", "emitDeclarationOnly": True},
            "include": ["src/**/*.ts"], "exclude": ["src/**/*.spec.ts"],
            **({"references": [{"path": "../helper/tsconfig.lib.json"}]} if directory == app else {}),
        }))
    (root / "package.json").write_text('{"name":"fixture","packageManager":"pnpm@9.14.4","private":true}')
    (app / "src/main.ts").write_text("export const value: number = 42;\n")
    other = helper / "src/UnrelatedHelper.ts"
    other.write_text("export const value: number = 7;\n")
    binary = root / "node_modules/.bin/tsc"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexec " + shlex.join([node, tsc]) + ' "$@"\n')
    binary.chmod(0o755)
    env = {**os.environ, "PATH": str(Path(node).parent) + ":" + str(binary.parent) + ":/usr/bin:/bin",
           "NODE_OPTIONS": "--max-old-space-size=1536", "COREPACK_ENABLE_NETWORK": "0",
           "COREPACK_ENABLE_PROJECT_SPEC": "0", "npm_config_offline": "true"}
    command = shlex.split(tsjs_pnpm9.public_command())
    command[:2] = [node, manager]
    receipts = []
    for phase in ("complete_current_projects", "unrelated_helper_type_error", "restored_current_projects"):
        other.write_text("export const value: number = " +
                         ("'genuine type error'" if phase == "unrelated_helper_type_error" else "7") + ";\n")
        done = subprocess.run(command, cwd=app, env=env, text=True, capture_output=True, timeout=60)
        receipts.append({"label": phase, "command": command, "cwd": str(app),
                         "exit_code": done.returncode, "stdout": done.stdout, "stderr": done.stderr})
        if phase == "unrelated_helper_type_error":
            assert done.returncode != 0 and "UnrelatedHelper.ts" in done.stdout and "TS2322" in done.stdout
        else:
            assert done.returncode == 0, done.stdout + done.stderr
            assert (helper / "dist/UnrelatedHelper.d.ts").exists()
    record_evidence("public-project", {"kind": "synthetic-full-project-build-not-task-proof",
                                      "compiler_version": subprocess.check_output([node, tsc, "--version"], text=True).strip(),
                                      "retained_compiler_sha256": file_hash(Path(tsc)),
                                      "receipts": receipts})
