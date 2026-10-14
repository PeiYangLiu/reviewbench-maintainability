"""Exact Git selectors and SDK documentation survive directory-source transport."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_vendor as vendor
from reviewbench_maintainability.agentic.contracts import ContractError
from test_agentic_rust_vendor import commit, git
from test_agentic_rust_vendor_documents import workspace


def docs_workspace(tmp_path):
    cache, source, lock, metadata = workspace(tmp_path)
    (source / "docs/reference").mkdir(parents=True)
    (source / "docs/reference/api.md").write_text("Pinned API documentation.\n")
    (source / "docs/example.rs").write_text("pub fn example() {}\n")
    (source / "member/docs").symlink_to("../docs")
    # Compilation really reads the transported directory.
    (source / "member/src/lib.rs").write_text(
        '#![doc = include_str!("../docs/reference/api.md")]\npub const VALUE: u8 = 7;\n'
    )
    git(source, "add", ".")
    git(source, "commit", "-qm", "original document directory")
    revision = git(source, "rev-parse", "HEAD")
    identity = "git+" + source.as_uri() + "?branch=fixture%2Fmain#" + revision
    lock["package"][0]["source"] = identity
    metadata["packages"][0]["source"] = identity.replace("%2F", "/")
    return cache, source, lock, metadata


def test_encoded_branch_matches_only_the_same_complete_git_identity():
    source = "git+https://example.invalid/sdk?branch=gear%2Fmain#" + "a" * 40
    assert vendor.git_source_identity(source) == vendor.git_source_identity(source.replace("%2F", "/"))
    for changed in (
        source.replace("example.invalid", "other.invalid"),
        source.replace("/sdk?", "/other?"),
        source.replace("branch=", "tag="),
        source.replace("gear%2Fmain", "gear%252Fmain"),
        source[:-40] + "b" * 40,
    ):
        assert vendor.git_source_identity(changed) != vendor.git_source_identity(source)


@pytest.mark.parametrize("source", [
    "registry+https://github.com/rust-lang/crates.io-index",
    "git+https://example.invalid/sdk#short",
    "git+https://example.invalid/sdk?branch=#" + "a" * 40,
    "git+https://example.invalid/sdk?branch=one&tag=two#" + "a" * 40,
    "git+https://example.invalid/sdk?unknown=x#" + "a" * 40,
])
def test_unbound_git_identities_fail(source):
    with pytest.raises(ContractError):
        vendor.git_source_identity(source)


def test_exact_docs_tree_and_encoded_branch_use_git_objects_not_dirty_files(tmp_path):
    cache, source, lock, metadata = docs_workspace(tmp_path)
    (source / "docs/reference/api.md").write_text("dirty checkout\n")
    destination = tmp_path / "vendor"
    _, rows = vendor.materialize([lock], metadata, cache, destination,
                                 lambda *_: pytest.fail("no registry/network"))
    row = rows[0]
    assert row["source"] == lock["package"][0]["source"]
    assert row["metadata_source_alias"] == metadata["packages"][0]["source"]
    package = destination / row["directory"]
    assert not (package / "docs").is_symlink()
    assert (package / "docs/reference/api.md").read_text() == "Pinned API documentation.\n"
    assert (source / "docs/reference/api.md").read_text() == "dirty checkout\n"
    proof = next(item for item in row["materialized_document_links"] if item["path"] == "docs")
    assert proof["locked_referent"] == "docs" and proof["original_link_target"] == "../docs"
    assert {item["path"] for item in proof["files"]} == {"reference/api.md", "example.rs"}
    checks = json.loads((package / ".cargo-checksum.json").read_text())
    assert all(hashlib.sha256((package / name).read_bytes()).hexdigest() == checksum
               for name, checksum in checks["files"].items())


@pytest.mark.parametrize("change", ["repository", "selector", "commit", "version", "double-encoding"])
def test_metadata_alias_does_not_rebind_another_source(tmp_path, change):
    cache, source, lock, metadata = docs_workspace(tmp_path)
    item = metadata["packages"][0]
    if change == "repository":
        item["source"] = item["source"].replace(source.as_uri(), (source.parent / "foreign").as_uri())
    elif change == "selector":
        item["source"] = item["source"].replace("branch=", "tag=")
    elif change == "commit":
        item["source"] = item["source"][:-40] + "b" * 40
    elif change == "version":
        item["version"] = "9.9.9"
    else:
        item["source"] = item["source"].replace("fixture/main", "fixture%252Fmain")
    target = tmp_path / "vendor"
    with pytest.raises(ContractError, match="absent from Cargo metadata"):
        vendor.materialize([lock], metadata, cache, target,
                           lambda *_: pytest.fail("no registry/network"))
    assert not target.exists()


@pytest.mark.parametrize("kind", [
    "external", "nested-link", "nested-directory-link", "executable", "special",
    "too-many-files", "too-large", "other-directory", "docs-file",
])
def test_document_tree_boundary_is_explicit_and_bounded(tmp_path, monkeypatch, kind):
    snapshot = tmp_path / "snapshot"
    source = snapshot / "member"
    source.mkdir(parents=True)
    docs = snapshot / "docs"
    docs.mkdir()
    (docs / "reference.md").write_text("original\n")
    name, link = "docs", "../docs"
    if kind == "external":
        link = "../../external"
        (tmp_path / "external").mkdir()
    elif kind == "nested-link":
        (docs / "link.md").symlink_to("reference.md")
    elif kind == "nested-directory-link":
        (docs / "nested").symlink_to("../member", target_is_directory=True)
    elif kind == "executable":
        (docs / "reference.md").chmod(0o755)
    elif kind == "special":
        os.mkfifo(docs / "pipe")
    elif kind == "too-many-files":
        monkeypatch.setattr(vendor, "DOCUMENT_TREE_FILES", 0)
    elif kind == "too-large":
        monkeypatch.setattr(vendor, "DOCUMENT_TREE_BYTES", 1)
    elif kind == "other-directory":
        name = "source"
    else:
        shutil.rmtree(docs)
        docs.write_text("not a directory")
    (source / name).symlink_to(link)
    destination = tmp_path / "standalone"
    shutil.copytree(source, destination, symlinks=True)
    with pytest.raises(ContractError):
        vendor.materialize_git_document_links(snapshot, source, destination)
    assert (destination / name).is_symlink()


def test_original_cargo_compiles_a_library_that_includes_the_transported_docs(tmp_path):
    cargo = os.environ.get("WORKSPACE_TEST_CARGO")
    if not cargo:
        pytest.skip("requires the preinstalled original Cargo")
    cache, source, lock, metadata = docs_workspace(tmp_path)
    destination = tmp_path / "vendor"
    _, rows = vendor.materialize([lock], metadata, cache, destination,
                                 lambda *_: pytest.fail("no registry/network"))
    package = destination / rows[0]["directory"]
    command = [cargo, "check", "--offline", "--lib",
               "--manifest-path", str(package / "Cargo.toml")]
    result = subprocess.run(
        command, cwd=package, capture_output=True, text=True, timeout=60,
        env={**os.environ, "CARGO_HOME": str(tmp_path / "empty-home"),
             "CARGO_TARGET_DIR": str(tmp_path / "target"),
             "RUSTC": str(Path(cargo).with_name("rustc")),
             "CARGO_NET_OFFLINE": "true", "RUSTUP_AUTO_INSTALL": "0"},
    )
    assert result.returncode == 0, result.stderr
    assert 'include_str!("../docs/reference/api.md")' in (package / "src/lib.rs").read_text()
