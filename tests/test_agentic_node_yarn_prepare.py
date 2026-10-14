"""Local preparation controls; these never execute a benchmark or model."""
import base64
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration.node_pnpm_prepare import retained_manager
from agentic_runtime_migration.node_yarn_prepare import (
    input_plan, install_command, locked_tarballs, mirror_name, seed_mirror, validate_case,
)
from agentic_runtime_migration.tsjs_pnpm_payload import export_payload
from reviewbench_maintainability.agentic.contracts import ContractError


def case():
    return {"status": "ready", "package_manager": "yarn@1.22.22", "node": "16.20.2",
            "workspace": ".", "install_cwd": ".", "reinstall_golden": False,
            "install": ["corepack", "yarn@1.22.22", "install", "--frozen-lockfile",
                        "--ignore-scripts", "--network-timeout", "600000"],
            "test": ["corepack", "yarn@1.22.22", "vitest", "run", "private.test.ts", "--threads=false"],
            "session_files": [{"source": "session/private.test.ts", "destination": "private.test.ts"}]}


def put(root, name, value):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) if isinstance(value, dict) else value)
    return path


def test_exact_yarn_flags_are_retained_with_bounded_cache_only_options():
    value = case()
    assert validate_case(value) == "1.22.22"
    argv = install_command(value)
    assert argv[2:7] == value["install"][2:]
    assert argv[7:] == ["--network-concurrency", "2", "--prefer-offline",
                        "--cache-folder", "/yarn-cache", "--use-yarnrc", "/install-home/cache.yarnrc"]
    assert not any(flag in argv for flag in ["--ignore-engines", "--ignore-optional", "--force", "--production"])


@pytest.mark.parametrize("key,value", [
    ("package_manager", "yarn@latest"), ("node", "18"), ("reinstall_golden", True),
    ("build", ["yarn", "build"]), ("install_cwd", "packages/core"),
    ("workspace", "packages/core"), ("install", ["yarn", "install"]),
    ("test", ["node", "jest"]),
])
def test_changed_original_policies_are_rejected(key, value):
    original = case()
    original[key] = value
    with pytest.raises(ContractError):
        validate_case(original)


def test_workspace_export_keeps_external_topology_but_excludes_local_source(tmp_path):
    source = tmp_path / "source"
    put(source, "package.json", {"name": "root", "workspaces": ["packages/*"]})
    put(source, "yarn.lock", "# yarn lockfile v1\n")
    put(source, "packages/local/package.json", {"name": "@sample/local", "version": "1.0.0"})
    put(source, "packages/local/index.js", "module.exports = 'original source';")
    put(source, "node_modules/ext/package.json", {"name": "ext", "version": "1.0.0"})
    (source / "node_modules/@sample").mkdir()
    (source / "node_modules/@sample/local").symlink_to("../../packages/local")
    plan = input_plan(source)
    layout, index = export_payload(source, tmp_path / "payload", plan)
    assert layout["workspace_production_files_exported"] == 0
    assert layout["workspace_links"][0]["workspace_target"] == "packages/local"
    assert not (tmp_path / "payload/node_modules/@sample/local").is_symlink()
    assert all("original source" not in p.read_text() for p in (tmp_path / "payload").rglob("*") if p.is_file())
    assert len(index) == 1


def test_copied_local_package_requires_explicit_adapter(tmp_path):
    put(tmp_path, "package.json", {"name": "root", "dependencies": {"local": "file:tools/local"}})
    put(tmp_path, "yarn.lock", "# yarn lockfile v1\n")
    with pytest.raises(ContractError, match="local copied dependencies"):
        input_plan(tmp_path)


def test_workspace_symlink_and_duplicate_names_are_rejected(tmp_path):
    source = tmp_path / "source"
    put(source, "package.json", {"name": "root", "workspaces": ["packages/*"]})
    put(source, "yarn.lock", "# yarn lockfile v1\n")
    put(source, "packages/local/package.json", {"name": "root"})
    with pytest.raises(ContractError, match="duplicate"):
        input_plan(source)
    put(source, "packages/local/package.json", {"name": "local"})
    (source / "packages/link").symlink_to(tmp_path)
    with pytest.raises(ContractError, match="escapes"):
        input_plan(source)


def cache_fixture(root, data):
    sha1 = hashlib.sha1(data).hexdigest()
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
    url = f"https://registry.yarnpkg.com/@sample/pkg/-/pkg-1.2.3.tgz#{sha1}"
    package = root / "npm-sample-pkg/node_modules/@sample/pkg"
    put(package, ".yarn-metadata.json", {"remote": {"resolved": url}})
    (package / ".yarn-tarball.tgz").write_bytes(data)
    put(package, "index.js", "untrusted extracted cache bytes must not be reused")
    lock = f'"@sample/pkg@^1.2.3":\n  version "1.2.3"\n  resolved "{url}"\n  integrity {integrity}\n'
    return lock, package, url


def test_only_exact_locked_compressed_tarballs_are_reused(tmp_path):
    lock, package, url = cache_fixture(tmp_path / "cache", b"test archive bytes")
    seed = seed_mirror(lock, tmp_path / "cache", tmp_path / "mirror")
    assert seed["reused_tarballs"] == 1 and seed["extracted_cache_files_reused"] == 0
    assert (tmp_path / "mirror/@sample-pkg-1.2.3.tgz").read_bytes() == b"test archive bytes"
    assert len(list((tmp_path / "mirror").iterdir())) == 1
    assert mirror_name(url) == "@sample-pkg-1.2.3.tgz"
    assert len(locked_tarballs(lock)) == 1
    empty = seed_mirror("# empty lock\n", tmp_path / "cache", tmp_path / "empty")
    assert empty["reused_tarballs"] == 0


def test_damaged_retained_tarball_is_rejected_before_install(tmp_path):
    lock, package, _ = cache_fixture(tmp_path / "cache", b"original bytes")
    (package / ".yarn-tarball.tgz").write_bytes(b"damaged bytes")
    with pytest.raises(ContractError, match="locked SHA1"):
        seed_mirror(lock, tmp_path / "cache", tmp_path / "mirror")


def test_lock_sri_is_verified_independently_of_cache_metadata(tmp_path):
    lock, _, _ = cache_fixture(tmp_path / "cache", b"original bytes")
    lock = lock.split("  integrity")[0] + "  integrity sha512-" + base64.b64encode(b"x" * 64).decode()
    with pytest.raises(ContractError, match="locked SRI"):
        seed_mirror(lock, tmp_path / "cache", tmp_path / "mirror")


def manager_fixture(root, name):
    package = json.dumps({"name": name, "version": "1.22.22"}).encode()
    archive = root / "manager.tgz"
    root.mkdir()
    with tarfile.open(archive, "w:gz") as stream:
        member = tarfile.TarInfo("package/package.json")
        member.size = len(package)
        stream.addfile(member, io.BytesIO(package))
    manager = root / "manager"
    put(manager, "package.json", package.decode())
    put(manager, ".corepack", {"hash": "sha512." + hashlib.sha512(archive.read_bytes()).hexdigest()})
    return manager, archive


def test_yarn_manager_is_bound_to_retained_corepack_tar_bytes(tmp_path):
    root, archive = manager_fixture(tmp_path / "fixture", "yarn")
    proof = retained_manager(root, "1.22.22", archive, package_name="yarn")
    assert not proof["downloaded_tar_for_independent_byte_verification"]
    assert "/yarn/-/yarn-1.22.22.tgz" in proof["url"]
    with pytest.raises(ContractError, match="version differs"):
        retained_manager(root, "1.22.22", archive)
    put(root, "package.json", {"name": "yarn", "version": "1.22.22", "changed": True})
    with pytest.raises(ContractError, match="pinned distribution"):
        retained_manager(root, "1.22.22", archive, package_name="yarn")


def test_default_pnpm_manager_contract_is_unchanged(tmp_path):
    root, archive = manager_fixture(tmp_path / "fixture", "pnpm")
    proof = retained_manager(root, "1.22.22", archive)
    assert "/pnpm/-/pnpm-1.22.22.tgz" in proof["url"]
    with pytest.raises(ContractError, match="supported exact identity"):
        retained_manager(root, "latest", archive)
