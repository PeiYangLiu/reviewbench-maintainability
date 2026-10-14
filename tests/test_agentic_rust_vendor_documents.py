"""Git workspace documents become self-contained without admitting source escapes."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration.rust_vendor import materialize, materialize_git_document_links
from reviewbench_maintainability.agentic.contracts import ContractError
from test_agentic_rust_vendor import commit, git


def workspace(tmp_path):
    cache = tmp_path / "cargo"
    root = cache / "git/checkouts/doc-workspace/locked"
    (root / "member/src").mkdir(parents=True)
    (root / "Cargo.toml").write_text('[workspace]\nmembers=["member"]\n')
    (root / "member/Cargo.toml").write_text(
        '[package]\nname="member"\nversion="0.1.0"\nedition="2021"\nlicense="MIT"\n')
    (root / "member/src/lib.rs").write_text("pub const VALUE: u8 = 7;\n")
    for name in ("CONTRIBUTING.md", "LICENSE-APACHE", "LICENSE-MIT"):
        (root / name).write_text("locked " + name + "\n")
        (root / "member" / name).symlink_to("../" + name)
    revision = commit(root)
    source = "git+" + root.as_uri() + "?rev=" + revision + "#" + revision
    package = {"name": "member", "version": "0.1.0", "source": source}
    metadata = {"packages": [{**package, "manifest_path": str(root / "member/Cargo.toml")}]}
    return cache, root, {"package": [package]}, metadata


def test_git_member_documents_use_locked_bytes_not_a_dirty_checkout(tmp_path):
    cache, source, lock, metadata = workspace(tmp_path)
    (source / "LICENSE-MIT").write_text("untrusted working copy\n")
    destination = tmp_path / "vendor"
    _, audit = materialize([lock], metadata, cache, destination,
                           lambda *_: pytest.fail("no registry/network"))
    row = audit[0]
    target = destination / row["directory"]
    assert len(row["materialized_document_links"]) == 3
    for record in row["materialized_document_links"]:
        name = record["path"]
        contents = ("locked " + name + "\n").encode()
        assert (target / name).is_file() and not (target / name).is_symlink()
        assert (target / name).read_bytes() == contents
        assert record["sha256"] == hashlib.sha256(contents).hexdigest()
        assert record["original_link_target"] == "../" + name
        assert record["locked_referent"] == name and record["mode"] == 0o644
    assert (target / "src/lib.rs").read_text() == "pub const VALUE: u8 = 7;\n"
    checksums = json.loads((target / ".cargo-checksum.json").read_text())
    assert all(hashlib.sha256((target / name).read_bytes()).hexdigest() == digest
               for name, digest in checksums["files"].items())
    assert (source / "LICENSE-MIT").read_text() == "untrusted working copy\n"


def test_original_cargo_packages_the_same_document_bytes_as_regular_files(tmp_path):
    _, source, _, _ = workspace(tmp_path)
    cargo = os.environ.get("WORKSPACE_TEST_CARGO")
    if not cargo:
        pytest.skip("requires the preinstalled original Cargo")
    cargo = Path(cargo)
    environment = dict(os.environ, CARGO_HOME=str(tmp_path / "empty-cargo"),
                       CARGO_TARGET_DIR=str(tmp_path / "target"),
                       RUSTC=str(cargo.with_name("rustc")), CARGO_NET_OFFLINE="true",
                       RUSTUP_AUTO_INSTALL="0")
    command = [str(cargo), "package", "--offline", "--no-verify", "--allow-dirty",
               "--manifest-path", str(source / "member/Cargo.toml")]
    result = subprocess.run(command, env=environment, cwd=source, capture_output=True,
                            text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    with tarfile.open(tmp_path / "target/package/member-0.1.0.crate") as archive:
        for name in ("CONTRIBUTING.md", "LICENSE-APACHE", "LICENSE-MIT"):
            member = archive.getmember("member-0.1.0/" + name)
            assert member.isfile() and not member.issym()
            assert archive.extractfile(member).read() == ("locked " + name + "\n").encode()


@pytest.mark.parametrize("kind", [
    "source", "manifest", "directory", "external", "absolute", "executable",
    "oversized", "dangling", "cycle",
])
def test_non_document_or_unbound_links_still_fail(tmp_path, kind):
    snapshot = tmp_path / "snapshot"
    package = snapshot / "member"
    package.mkdir(parents=True)
    name, link = "LICENSE", "../shared"
    shared = snapshot / "shared"
    shared.write_bytes(b"locked document\n")
    if kind == "source":
        name = "shared.rs"
    elif kind == "manifest":
        name = "Cargo.toml"
    elif kind == "directory":
        shared.unlink()
        shared.mkdir()
    elif kind == "external":
        link = "../../external"
        (tmp_path / "external").write_bytes(b"not in snapshot")
    elif kind == "absolute":
        link = str(shared)
    elif kind == "executable":
        shared.chmod(0o755)
    elif kind == "oversized":
        shared.write_bytes(b"x" * (1024**2 + 1))
    elif kind == "dangling":
        shared.unlink()
    elif kind == "cycle":
        link = "LICENSE"
    (package / name).symlink_to(link)
    target = tmp_path / "standalone"
    shutil.copytree(package, target, symlinks=True)
    with pytest.raises(ContractError):
        materialize_git_document_links(snapshot, package, target)


def test_internal_links_keep_their_original_type_and_target(tmp_path):
    snapshot = tmp_path / "snapshot"
    package = snapshot / "member"
    package.mkdir(parents=True)
    (package / "README.md").write_text("within-package\n")
    (package / "LICENSE").symlink_to("README.md")
    target = tmp_path / "standalone"
    shutil.copytree(package, target, symlinks=True)
    assert materialize_git_document_links(snapshot, package, target) == []
    assert (target / "LICENSE").is_symlink() and os.readlink(target / "LICENSE") == "README.md"


def test_changed_copied_link_is_not_bound_to_the_original(tmp_path):
    snapshot = tmp_path / "snapshot"
    package = snapshot / "member"
    package.mkdir(parents=True)
    (snapshot / "one").write_text("one")
    (snapshot / "two").write_text("two")
    (package / "LICENSE").symlink_to("../one")
    target = tmp_path / "standalone"
    shutil.copytree(package, target, symlinks=True)
    (target / "LICENSE").unlink()
    (target / "LICENSE").symlink_to("../two")
    with pytest.raises(ContractError, match="differs"):
        materialize_git_document_links(snapshot, package, target)


def test_failed_git_source_link_leaves_no_partial_vendor(tmp_path):
    cache, source, lock, metadata = workspace(tmp_path)
    (source / "shared.rs").write_text("pub const SHARED: u8 = 1;\n")
    (source / "member/source.rs").symlink_to("../shared.rs")
    git(source, "add", ".")
    git(source, "commit", "-qm", "outside-package source link")
    revision = git(source, "rev-parse", "HEAD")
    identity = "git+" + source.as_uri() + "?rev=" + revision + "#" + revision
    lock["package"][0]["source"] = metadata["packages"][0]["source"] = identity
    target = tmp_path / "vendor"
    with pytest.raises(ContractError):
        materialize([lock], metadata, cache, target, lambda *_: pytest.fail("no registry/network"))
    assert not target.exists()
