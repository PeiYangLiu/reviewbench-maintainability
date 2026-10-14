"""Directory symlink fixtures remain symlinks, with all referent bytes retained."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest
import tomli_w

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_vendor as vendor
from reviewbench_maintainability.agentic.contracts import ContractError
from test_agentic_rust_vendor import commit, git


def fixture(tmp_path, kind="internal"):
    cache = tmp_path / "cargo"
    repository = cache / "git/checkouts/link-fixture/locked"
    (repository / "src").mkdir(parents=True)
    (repository / "fixtures/real").mkdir(parents=True)
    (repository / "fixtures/real/value.txt").write_text("original\n")
    (repository / "fixtures/link").symlink_to("real", target_is_directory=True)
    (repository / "Cargo.toml").write_text('[package]\nname="dirlink"\nversion="0.1.0"\nedition="2021"\n')
    (repository / "src/lib.rs").write_text(
        'pub const VALUE: &str = include_str!("../fixtures/link/value.txt");\n'
    )
    if kind == "ancestor":
        (repository / "fixtures/link").unlink()
        (repository / "fixtures/link").symlink_to("..", target_is_directory=True)
    elif kind == "nested":
        (repository / "fixtures/real/nested").symlink_to("value.txt")
    revision = commit(repository)
    source = "git+" + repository.as_uri() + "?rev=" + revision + "#" + revision
    package = {"name": "dirlink", "version": "0.1.0", "source": source}
    return cache, repository, {"package": [package]}, {
        "packages": [{**package, "manifest_path": str(repository / "Cargo.toml")}],
    }


def test_internal_directory_fixture_preserves_original_type_target_and_bytes(tmp_path):
    cache, original, lock, metadata = fixture(tmp_path)
    (original / "fixtures/real/value.txt").write_text("dirty cache\n")
    target = tmp_path / "vendor"
    _, rows = vendor.materialize([lock], metadata, cache, target,
                                 lambda *_: pytest.fail("no registry/network"))
    package = target / rows[0]["directory"]
    path = package / "fixtures/link"
    assert path.is_symlink() and path.is_dir() and os.readlink(path) == "real"
    assert (path / "value.txt").read_text() == "original\n"
    assert (original / "fixtures/real/value.txt").read_text() == "dirty cache\n"
    links = rows[0]["internal_directory_links"]
    assert len(links) == 1 and links[0]["link_type_preserved"]
    assert links[0]["path"] == "fixtures/link" and links[0]["target"] == "real"
    assert links[0]["referent_files"] == {
        "value.txt": hashlib.sha256(b"original\n").hexdigest(),
    }
    checks = json.loads((package / ".cargo-checksum.json").read_text())
    assert "fixtures/link" not in checks["files"]
    assert checks["files"]["fixtures/real/value.txt"] == links[0]["referent_files"]["value.txt"]


@pytest.mark.parametrize("kind", ["ancestor", "nested"])
def test_cycles_or_unbound_referents_leave_no_partial_vendor(tmp_path, kind):
    cache, _, lock, metadata = fixture(tmp_path, kind)
    target = tmp_path / "vendor"
    with pytest.raises(ContractError, match="directory"):
        vendor.materialize([lock], metadata, cache, target,
                           lambda *_: pytest.fail("no registry/network"))
    assert not target.exists()


@pytest.mark.parametrize("bound", ["DOCUMENT_TREE_FILES", "DOCUMENT_TREE_BYTES"])
def test_directory_referent_has_a_fixed_payload_bound(tmp_path, monkeypatch, bound):
    cache, _, lock, metadata = fixture(tmp_path)
    monkeypatch.setattr(vendor, bound, 0)
    target = tmp_path / "vendor"
    with pytest.raises(ContractError, match="directory"):
        vendor.materialize([lock], metadata, cache, target,
                           lambda *_: pytest.fail("no registry/network"))
    assert not target.exists()


def test_native_cargo_compiles_through_the_original_directory_link(tmp_path):
    cargo = os.environ.get("REVIEWBENCH_TEST_CARGO")
    if not cargo:
        pytest.skip("requires a preinstalled Cargo for this synthetic fixture")
    cache, original, lock, metadata = fixture(tmp_path)
    target = tmp_path / "vendor"
    text, _ = vendor.materialize([lock], metadata, cache, target,
                                 lambda *_: pytest.fail("no registry/network"))
    sources = tmp_path / "sources.toml"
    sources.write_text(text.replace(vendor.TARGET, str(target)))
    consumer = tmp_path / "consumer"
    (consumer / "src").mkdir(parents=True)
    revision = git(original, "rev-parse", "HEAD")
    (consumer / "Cargo.toml").write_text(
        '[package]\nname="consumer"\nversion="0.1.0"\nedition="2021"\n'
        '[dependencies]\ndirlink={git=' + json.dumps(original.as_uri()) + ',rev="' + revision + '"}\n'
    )
    (consumer / "src/lib.rs").write_text("pub const VALUE: &str = dirlink::VALUE;\n")
    full_lock = {"version": 3, "package": [
        {"name": "consumer", "version": "0.1.0", "dependencies": ["dirlink"]}, *lock["package"],
    ]}
    (consumer / "Cargo.lock").write_text(tomli_w.dumps(full_lock))
    before = (consumer / "Cargo.lock").read_bytes()
    result = subprocess.run(
        [cargo, "--config", str(sources), "check", "--locked", "--offline", "--lib"], cwd=consumer,
        env={**os.environ, "CARGO_HOME": str(tmp_path / "empty-home"),
             "CARGO_TARGET_DIR": str(tmp_path / "target"), "RUSTUP_AUTO_INSTALL": "0",
             "RUSTC": str(Path(cargo).with_name("rustc"))},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert (consumer / "Cargo.lock").read_bytes() == before
