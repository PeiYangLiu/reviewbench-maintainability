import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration.python_dependencies import dependency_tree_files
from agentic_runtime_migration.common import MigrationContext
from reviewbench_maintainability.agentic.assets import AssetPool
from reviewbench_maintainability.agentic.contracts import ContractError, file_hash
from reviewbench_maintainability.agentic.snapshots import tree_files
from reviewbench_maintainability.agentic.storage import DirectoryObjectStore, LocalObjectCache


@pytest.mark.parametrize("kind", ["file", "directory", "symlink"])
def test_developer_archive_prunes_only_git_metadata_and_preserves_json_fixtures(tmp_path, kind):
    root = tmp_path / "site"
    originals = {
        "archspec/__init__.py": b"# ordinary external Python source\n",
        "archspec/json/cpu/microarchitectures.json": b'{"microarchitectures":{"test":{"features":["sse2"]}}}\n',
        "archspec/json/cpu/microarchitectures_schema.json": b'{"type":"object","required":["microarchitectures"]}\n',
        "archspec/json/tests/fixture.json": b'{"machine":"test","expected":[1,2,3]}\n',
        "archspec/json/tests/.ordinary-fixture": b"keep this hidden fixture\n",
        "archspec/json/.github/workflows/check.yaml": b"fixture: retained\n",
        "archspec/json/.gitignore": b"*.tmp\n",
        "archspec/json/.gitattributes": b"*.json text\n",
        "archspec/json/LICENSE-MIT": b"retain dependency licensing\n",
    }
    for name, data in originals.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    metadata = root / "archspec/json/.git"
    if kind == "file":
        metadata.write_text("gitdir: ../../../original-cache/.git/modules/json\n")
        # Demonstrate the exact engine-side baseline, not a made-up failure.
        with pytest.raises(ContractError, match="archspec/json/.git"):
            tree_files(root)
    elif kind == "directory":
        (metadata / "objects").mkdir(parents=True)
        (metadata / "objects/secret-history").write_bytes(b"VCS object, not dependency data")
    else:
        outside = tmp_path / "old-checkout-metadata"
        outside.mkdir()
        (outside / "config").write_text("retained and never read into the archive\n")
        metadata.symlink_to(outside, target_is_directory=True)
    files = dependency_tree_files(root)
    assert set(files) == set(originals)
    assert all(files[n].read_bytes() == data for n, data in originals.items())
    output, scratch = tmp_path / "out", tmp_path / "scratch"
    output.mkdir()
    scratch.mkdir()
    cache = LocalObjectCache(tmp_path / "cache", DirectoryObjectStore(tmp_path / "objects"), min_free_bytes=0)
    ctx = SimpleNamespace(output=output, scratch=scratch, cache=cache,
                          _bundle_lock=threading.Lock(), _bundles={})
    asset = MigrationContext.bundle(ctx, root, list(files), role="dependency")
    descriptor = json.loads(Path(asset["path"]).read_text())
    assert descriptor["source_kind"] == "dependency"
    pool = AssetPool(cache, scratch, "d" * 32, output)
    try:
        extracted = pool.materialize(asset)
        assert not list(extracted.rglob(".git"))
        assert set(tree_files(extracted)) == set(originals)
        for name, data in originals.items():
            assert (extracted / name).read_bytes() == data
            if name.endswith(".json"):
                assert json.loads((extracted / name).read_bytes()) == json.loads(data)
        assert {n: file_hash(root / n) for n in originals} == {
            n: file_hash(extracted / n) for n in originals}
    finally:
        pool.close()
    assert metadata.exists()  # The original dependency cache is never cleaned.
    if kind == "file":
        assert metadata.read_text() == "gitdir: ../../../original-cache/.git/modules/json\n"
    elif kind == "directory":
        assert (metadata / "objects/secret-history").read_bytes() == b"VCS object, not dependency data"
    else:
        assert metadata.is_symlink() and (outside / "config").is_file()


def test_dependency_walk_retains_ordinary_relative_symlinks_but_not_escapes(tmp_path):
    (tmp_path / "data.json").write_text('{"retained":true}\n')
    (tmp_path / "fixture-link").symlink_to("data.json")
    assert set(dependency_tree_files(tmp_path)) == {"data.json", "fixture-link"}
    (tmp_path / "foreign").symlink_to("/tmp")
    with pytest.raises(ContractError, match="absolute symlink"):
        dependency_tree_files(tmp_path)
