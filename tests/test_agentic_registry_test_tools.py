"""Proof-bound registry configurations remain distinct from current workspace code."""
import base64
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import node_registry_tools as tools
from agentic_runtime_migration.tsjs_pnpm_payload import SOURCE, export_payload, input_plan
from reviewbench_maintainability.agentic.contracts import ContractError, file_hash

KEY = "@blaze-cardano/jest-config@0.0.1"
NAME = "@blaze-cardano/jest-config"


def pointer(path):
    return {"path": str(path), "sha256": file_hash(path)}


def write(path, value):
    path.write_text(json.dumps(value))
    return pointer(path)


def fixture(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    workspace = source / "packages/blaze-jest-config"
    client = source / "packages/client"
    workspace.mkdir(parents=True)
    client.mkdir()
    files = {
        "package.json": json.dumps({"name": NAME, "version": "0.0.1"}).encode(),
        "base.jest.config.js": b"module.exports={preset:'ORIGINAL_REGISTRY_TEST_CONFIG'};\n",
        "LICENSE": b"original registry tool license fixture\n",
        "CHANGELOG.md": b"original registry changelog fixture\n",
    }
    body = tmp_path / "original.tgz"
    with tarfile.open(body, "w:gz") as archive:
        for name, data in files.items():
            member = tarfile.TarInfo("package/" + name)
            member.mode, member.size = 0o644, len(data)
            archive.addfile(member, io.BytesIO(data))
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(body.read_bytes()).digest()).decode()
    allowed = copy.deepcopy(tools.TOOLS[KEY])
    allowed["integrity"] = integrity
    allowed["configuration_sha256"] = hashlib.sha256(files["base.jest.config.js"]).hexdigest()
    monkeypatch.setitem(tools.TOOLS, KEY, allowed)
    write(source / "package.json", {"name": "original-root", "devDependencies": {NAME: "^0.0.1"}})
    write(workspace / "package.json", {"name": NAME, "version": "0.0.1"})
    (workspace / "base.jest.config.ts").write_text("CURRENT_WORKSPACE_CONFIGURATION\n")
    write(client / "package.json", {"name": "client", "devDependencies": {NAME: "workspace:*"}})
    (source / "pnpm-workspace.yaml").write_text(yaml.safe_dump({"packages": ["packages/*"]}))
    lock = {
        "lockfileVersion": "9.0",
        "importers": {
            ".": {"devDependencies": {NAME: {"specifier": "^0.0.1", "version": "0.0.1"}}},
            "packages/blaze-jest-config": {},
            "packages/client": {"devDependencies": {
                NAME: {"specifier": "workspace:*", "version": "link:../blaze-jest-config"}}},
        },
        "packages": {KEY: {"resolution": {"integrity": integrity}}},
    }
    (source / "pnpm-lock.yaml").write_text(yaml.safe_dump(lock))
    installed = source / allowed["installed_root"]
    installed.mkdir(parents=True)
    for name, data in files.items():
        (installed / name).write_bytes(data)
        (installed / name).chmod(0o644)
    top = source / "node_modules/@blaze-cardano"
    top.mkdir()
    (top / "jest-config").symlink_to(os.path.relpath(installed, top))
    module = client / "node_modules/@blaze-cardano"
    module.mkdir(parents=True)
    (module / "jest-config").symlink_to(os.path.relpath(workspace, module))
    plan = input_plan(source)
    record = {
        "schema_version": tools.SCHEMA, "status": "original_registry_archive_and_installed_bytes_verified",
        "package_key": KEY, **{key: allowed[key] for key in (
            "name", "version", "workspace_root", "root_specifier", "installed_root", "integrity")},
        "archive": pointer(body), "members": tools.regular_tree(installed),
        "source_input_hashes": plan["input_hashes"], "new_install_build_calibration_model_judge_calls": 0,
    }
    proof = tmp_path / "proof.json"
    write(proof, record)
    return source, plan, record, proof, installed


def test_default_export_still_rejects_namespace_collisions(tmp_path, monkeypatch):
    source, plan, _, _, _ = fixture(tmp_path, monkeypatch)
    with pytest.raises(ContractError, match="copied workspace production"):
        export_payload(source, tmp_path / "rejected", plan)
    assert not (tmp_path / "rejected").exists()


def test_verified_registry_tool_is_exported_but_current_workspace_link_is_not(tmp_path, monkeypatch):
    source, plan, record, proof, installed = fixture(tmp_path, monkeypatch)
    output = tmp_path / "payload"
    layout, _ = export_payload(source, output, plan, registry_test_tools=[pointer(proof)])
    assert layout["workspace_production_files_exported"] == 0
    assert set(layout["registry_test_tools"]) == {record["installed_root"]}
    assert (output / record["installed_root"] / "base.jest.config.js").read_bytes() == (
        installed / "base.jest.config.js").read_bytes()
    link = output / "packages/client/node_modules/@blaze-cardano/jest-config"
    assert not link.exists() and not link.is_symlink()
    assert any(row["path"] == "packages/client/node_modules/@blaze-cardano/jest-config"
               and row["workspace_target"] == "packages/blaze-jest-config" for row in layout["workspace_links"])
    assert not (output / "packages/blaze-jest-config/base.jest.config.ts").exists()


@pytest.mark.parametrize("field,value", [
    ("package_key", "@blaze-cardano/core@0.0.1"), ("name", "@blaze-cardano/core"),
    ("version", "0.0.2"), ("integrity", "sha512-unbound"),
    ("workspace_root", "packages/client"), ("root_specifier", "workspace:*"),
    ("status", "prepared"), ("source_input_hashes", {}),
    ("new_install_build_calibration_model_judge_calls", 1),
])
def test_unbound_relabelled_or_different_tools_are_rejected(tmp_path, monkeypatch, field, value):
    source, plan, record, proof, _ = fixture(tmp_path, monkeypatch)
    record[field] = value
    write(proof, record)
    with pytest.raises(ContractError):
        export_payload(source, tmp_path / "rejected", plan, registry_test_tools=[pointer(proof)])
    assert not (tmp_path / "rejected").exists()


@pytest.mark.parametrize("change", ["config", "extra-file", "symlink", "mode", "tarball", "proof-hash", "duplicate"])
def test_every_original_tool_byte_mode_member_and_pointer_is_bound(tmp_path, monkeypatch, change):
    source, plan, record, proof, installed = fixture(tmp_path, monkeypatch)
    if change == "config":
        (installed / "base.jest.config.js").write_text("module.exports={};")
    elif change == "extra-file":
        (installed / "candidate-production.js").write_text("frozen candidate source")
    elif change == "symlink":
        (installed / "base.jest.config.js").unlink()
        (installed / "base.jest.config.js").symlink_to(source / "packages/blaze-jest-config/base.jest.config.ts")
    elif change == "mode":
        (installed / "base.jest.config.js").chmod(0o755)
    elif change == "tarball":
        path = Path(record["archive"]["path"])
        path.write_bytes(path.read_bytes() + b"modified")
        record["archive"] = pointer(path)
        write(proof, record)
    pointers = [pointer(proof)]
    if change == "proof-hash":
        pointers[0]["sha256"] = "0" * 64
    elif change == "duplicate":
        pointers *= 2
    with pytest.raises(ContractError):
        export_payload(source, tmp_path / "rejected", plan, registry_test_tools=pointers)
    assert not (tmp_path / "rejected").exists()


@pytest.mark.parametrize("change", ["runtime-dependency", "workspace-resolution", "other-copy", "redirected-root"])
def test_registry_proof_never_authorizes_runtime_source_or_a_different_install_root(tmp_path, monkeypatch, change):
    source, plan, record, proof, installed = fixture(tmp_path, monkeypatch)
    if change == "runtime-dependency":
        manifest = json.loads((source / "package.json").read_text())
        manifest["dependencies"] = {NAME: "^0.0.1"}
        write(source / "package.json", manifest)
    elif change == "workspace-resolution":
        lock = yaml.safe_load((source / "pnpm-lock.yaml").read_text())
        lock["importers"]["."]["devDependencies"][NAME] = {
            "specifier": "workspace:*", "version": "link:packages/blaze-jest-config"}
        (source / "pnpm-lock.yaml").write_text(yaml.safe_dump(lock))
    elif change == "other-copy":
        other = source / "node_modules/another-copy"
        shutil.copytree(installed, other)
    else:
        saved = installed.with_name("retained")
        installed.rename(saved)
        installed.symlink_to(saved, target_is_directory=True)
    if change in {"runtime-dependency", "workspace-resolution"}:
        plan = input_plan(source)
        record["source_input_hashes"] = plan["input_hashes"]
        write(proof, record)
    with pytest.raises(ContractError):
        export_payload(source, tmp_path / "rejected", plan, registry_test_tools=[pointer(proof)])


def test_real_transport_uses_current_workspace_configuration_not_registry_or_old_copy(tmp_path, monkeypatch):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is not installed on the unit-test host")
    source, plan, record, proof, _ = fixture(tmp_path, monkeypatch)
    payload = tmp_path / "payload"
    export_payload(source, payload, plan, registry_test_tools=[pointer(proof)])
    current = tmp_path / "current"
    current.mkdir()
    for name in plan["input_hashes"]:
        target = current / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, target)
    target = current / "packages/blaze-jest-config/base.jest.config.ts"
    target.write_text("CURRENT_MODIFIED_WORKSPACE_CONFIGURATION\n")
    script = tmp_path / "transport.cjs"
    script.write_text(SOURCE)
    result = subprocess.run([node, str(script), str(payload), str(current)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    workspace_link = current / "packages/client/node_modules/@blaze-cardano/jest-config"
    assert workspace_link.resolve() == current / "packages/blaze-jest-config"
    assert (workspace_link / "base.jest.config.ts").read_text() == "CURRENT_MODIFIED_WORKSPACE_CONFIGURATION\n"
    registry = current / record["installed_root"]
    assert (registry / "base.jest.config.js").read_bytes() == (payload / record["installed_root"] / "base.jest.config.js").read_bytes()
