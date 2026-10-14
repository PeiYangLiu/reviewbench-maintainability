"""Exact original registry test tools whose names also occur in a workspace."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import tarfile

import yaml

from reviewbench_maintainability.agentic.contracts import ContractError, file_hash, repo_path

SCHEMA = "reviewbench-pinned-registry-test-tool/v1"
TOOLS = {
    "@blaze-cardano/jest-config@0.0.1": {
        "name": "@blaze-cardano/jest-config", "version": "0.0.1",
        "workspace_root": "packages/blaze-jest-config", "root_specifier": "^0.0.1",
        "installed_root": "node_modules/.pnpm/@blaze-cardano+jest-config@0.0.1/node_modules/@blaze-cardano/jest-config",
        "integrity": "sha512-q/0BzXoN+PP4kcwmqthJEYrQ1ee3pZ1Y2dV7X7levyccX8yVMxgMUvJjXhZKl0Bb9g8JidoxanVaBW0d0Y0mSw==",
        "members": {"CHANGELOG.md", "LICENSE", "package.json", "base.jest.config.js"},
        "configuration": "base.jest.config.js",
        "configuration_sha256": "0fbac014321c856426c59213f69274aaf9b4afae737ec5a03afb9c0a2e43db09",
    },
}


def checked_file(pointer, label):
    if not isinstance(pointer, dict) or set(pointer) != {"path", "sha256"}:
        raise ContractError("registry test tool needs a hash-bound " + label)
    path = Path(pointer["path"])
    if (not path.is_absolute() or path.is_symlink() or not path.is_file()
            or file_hash(path) != pointer["sha256"]):
        raise ContractError("registry test tool " + label + " changed")
    return path


def regular_tree(root):
    """No executable examples, links, copied workspace tree or extra files."""
    files = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ContractError("registry test tool contains a redirected entry")
        if path.is_file():
            files[path.relative_to(root).as_posix()] = {
                "sha256": file_hash(path), "bytes": path.stat().st_size,
                "mode": path.stat().st_mode & 0o777,
            }
        elif not path.is_dir():
            raise ContractError("registry test tool contains a special file")
    return files


def verify_registry_test_tools(source, plan, pointers):
    if pointers is None:
        return {}
    if not isinstance(pointers, list) or not pointers:
        raise ContractError("registry test tools need an explicit nonempty proof list")
    source = Path(source).resolve()
    result = {}
    for pointer in pointers:
        path = checked_file(pointer, "proof")
        record = json.loads(path.read_text())
        key = record.get("package_key")
        allowed = TOOLS.get(key)
        if (allowed is None or record.get("schema_version") != SCHEMA
                or record.get("status") != "original_registry_archive_and_installed_bytes_verified"
                or record.get("new_install_build_calibration_model_judge_calls") != 0
                or any(record.get(name) != allowed[name] for name in (
                    "name", "version", "workspace_root", "root_specifier", "installed_root", "integrity"))
                or record.get("source_input_hashes") != plan["input_hashes"]):
            raise ContractError("unattested registry test tool or original source binding")
        name, installed = allowed["name"], allowed["installed_root"]
        if installed in result:
            raise ContractError("duplicate registry test tool proof")
        if plan["workspace_names"].get(name) != allowed["workspace_root"]:
            raise ContractError("registry tool does not bind the original current workspace")
        if plan["packages"].get(key, {}).get("resolution") != {"integrity": allowed["integrity"]}:
            raise ContractError("registry tool differs from the original locked registry graph")
        root_manifest = json.loads((source / "package.json").read_text())
        if (root_manifest.get("devDependencies", {}).get(name) != allowed["root_specifier"]
                or name in root_manifest.get("dependencies", {})
                or name in root_manifest.get("optionalDependencies", {})):
            raise ContractError("registry test tool is not the original root development dependency")
        lock = yaml.safe_load((source / "pnpm-lock.yaml").read_text())
        dependency = lock["importers"]["."].get("devDependencies", {}).get(name)
        if dependency != {"specifier": allowed["root_specifier"], "version": allowed["version"]}:
            raise ContractError("registry test tool importer is not its original non-workspace resolution")
        current = source
        for part in repo_path(installed).split("/"):
            current /= part
            if current.is_symlink() or not current.is_dir():
                raise ContractError("registry test tool root is redirected or absent")
        archive_path = checked_file(record["archive"], "original tarball")
        if archive_path.stat().st_size > 1024**2:
            raise ContractError("registry test tool tarball exceeds the fixed small-tool bound")
        data = archive_path.read_bytes()
        integrity = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
        if integrity != allowed["integrity"]:
            raise ContractError("registry test tool original tarball SRI differs")
        original = {}
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive:
                if member.isdir():
                    continue
                if (not member.isfile() or not member.name.startswith("package/")
                        or member.size > 1024**2):
                    raise ContractError("registry test tool tarball has an unexpected member")
                relative = repo_path(member.name[len("package/"):])
                if relative not in allowed["members"] or relative in original:
                    raise ContractError("registry test tool tarball contains extra or duplicate content")
                body = archive.extractfile(member).read()
                original[relative] = {
                    "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body),
                    "mode": member.mode & 0o777,
                }
        if (set(original) != allowed["members"] or original != record.get("members")
                or regular_tree(current) != original
                or original[allowed["configuration"]]["sha256"] != allowed["configuration_sha256"]):
            raise ContractError("registry test tool installed bytes differ from the exact published configuration")
        manifest = json.loads((current / "package.json").read_text())
        if (manifest.get("name") != name or manifest.get("version") != allowed["version"]
                or any(manifest.get(field) for field in (
                    "scripts", "dependencies", "optionalDependencies", "devDependencies", "bin"))):
            raise ContractError("registry test tool manifest is not the fixed configuration-only package")
        result[installed] = {"package_key": key, "integrity": integrity, "proof": pointer,
                             "members": original, "role": "original-registry-test-configuration"}
    return result
