"""Nested module ZIPs reserve writable cache/build space, not project outputs."""
from contextlib import nullcontext
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from test_agentic_go_dependencies import context, proxy_object
from test_agentic_go_proxy_closure import contract, no_fetch, offline_go
from test_agentic_go_wrappers import fixture
from agentic_runtime_migration import go_lane
from reviewbench_maintainability.agentic.contracts import ContractError
from reviewbench_maintainability.agentic.resources import ResourceGate, ResourceLimits, ResourceRequest

MiB = 1024**2
GiB = 1024**3
SCHEMA = "reviewbench-go-proxy-resources/v1"


def hint(*, count=0, zipped=0, expanded=0, entries=0):
    return {"schema_version": SCHEMA, "zip_count": count, "zip_bytes": zipped,
            "expanded_bytes": expanded, "zip_entries": entries, "build_allowance_bytes": 8 * GiB}


def recipe_with_hint(tmp_path, monkeypatch, resources):
    ctx, task, oracle, _ = fixture(tmp_path, "059-google-cloud-go")
    monkeypatch.setattr(go_lane, "compiler", lambda *args: ({"path": "compiler", "sha256": "a" * 64}, "1.22.2"))
    monkeypatch.setattr(go_lane, "module_proxy", lambda *args, **kwargs: {
        "path": "proxy", "sha256": "f" * 64, "resource_hint": resources})
    return go_lane.migrate(ctx, task, oracle)


def test_nested_zip_expansion_increases_recipe_scratch_and_rounds_up(tmp_path, monkeypatch):
    resources = hint(count=40, zipped=4 * GiB + 1, expanded=12 * GiB, entries=400)
    recipe = recipe_with_hint(tmp_path, monkeypatch, resources)
    assert recipe.build_scratch_mb == 24 * 1024 + 1
    assert recipe.cpus == 2 and recipe.memory_mb == 4096
    assert "go mod download all" in next(
        path.read_text() for path in (tmp_path / "assets").rglob("setup.sh"))
    assert recipe.grading["commands"][0]["expected_tests"] == [
        "public garbage-collection policy formatting behavior"]
    proxy = next(asset for asset in recipe.assets if asset["target"].endswith("/goproxy"))
    assert proxy["resource_hint"] == resources
    assert all("/opt/reviewbench-cache/" not in asset["target"] for asset in recipe.assets)


def test_packaged_proxy_measures_each_nested_zip_once_without_extracting(tmp_path, monkeypatch):
    directory = tmp_path / "original"
    proxy = directory / "home/go/pkg/mod/cache/download"
    archives = []
    for name, child in (("example.invalid/a", "example.invalid/z"), ("example.invalid/z", "example.invalid/a")):
        archives.append(proxy_object(proxy, name, "v1.0.0", f"require {child} v1.0.0\n")[2])
    ctx, task, repository = context(tmp_path, {
        "go.mod": "module example.invalid/main\n\ngo 1.15\nrequire example.invalid/z v1.0.0\n",
        "source.go": "package main\n",
    })
    before = (repository / "go.mod").read_bytes()
    no_fetch(monkeypatch)
    monkeypatch.setattr(zipfile.ZipFile, "extractall", lambda *args, **kwargs: pytest.fail("nested ZIP extraction"))
    asset = go_lane.module_proxy(ctx, task, directory, "", contract=contract(download_all=True))
    root = Path(asset["path"])
    zipped = sum(path.stat().st_size for path in archives)
    expanded = entries = 0
    for path in archives:
        with zipfile.ZipFile(path) as archive:
            expanded += sum(item.file_size for item in archive.infolist())
            entries += len(archive.infolist())
    resources = asset["resource_hint"]
    assert resources == hint(count=2, zipped=zipped, expanded=expanded, entries=entries) | {
        "build_scratch_mb": 16384}
    receipt = json.loads((root / "go-proxy-inputs.json").read_text())
    assert receipt["resource_hint"] == resources
    rows = [row for row in receipt["objects"] if row["object"] == ".zip"]
    assert sum(row["zip_bytes"] for row in rows) == zipped
    assert sum(row["expanded_bytes"] for row in rows) == expanded
    assert sum(row["zip_entries"] for row in rows) == entries
    assert (repository / "go.mod").read_bytes() == before
    assert all(path.suffix in {".mod", ".info", ".zip"} or path.name == "go-proxy-inputs.json"
               for path in root.rglob("*") if path.is_file())
    assert not (root / "source.go").exists()


def test_empty_proxy_has_zero_nested_zip_totals_and_retains_16_gib_floor(tmp_path, monkeypatch):
    ctx, task, _ = context(tmp_path, {"go.mod": "module example.invalid/main\n\ngo 1.20\n"})
    no_fetch(monkeypatch)
    asset = go_lane.module_proxy(ctx, task, tmp_path / "original", "", contract=contract())
    assert asset["resource_hint"] == hint() | {"build_scratch_mb": 16384}


def test_real_empty_module_zip_downloads_with_zero_expansion_and_entry_totals(tmp_path, monkeypatch):
    directory = tmp_path / "original"
    module = "example.invalid/empty"
    archive = proxy_object(directory / "home/go/pkg/mod/cache/download", module, "v1.0.0")[2]
    with zipfile.ZipFile(archive, "w"):
        pass
    assert archive.stat().st_size == 22
    ctx, task, repository = context(tmp_path, {
        "go.mod": f"module example.invalid/main\n\ngo 1.20\nrequire {module} v1.0.0\n",
    })
    no_fetch(monkeypatch)
    asset = go_lane.module_proxy(ctx, task, directory, "", contract=contract(download_all=True))
    root = Path(asset["path"])
    assert asset["resource_hint"] == hint(count=1, zipped=22) | {"build_scratch_mb": 16384}
    result = offline_go(tmp_path, repository, root, "mod", "download", "-json", module + "@v1.0.0")
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["Sum"] == "h1:47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU="
    receipt = json.loads((root / "go-proxy-inputs.json").read_text())
    assert receipt["resource_hint"] == asset["resource_hint"]
    row = next(row for row in receipt["objects"] if row["object"] == ".zip")
    assert row["zip_entries"] == row["expanded_bytes"] == 0


@pytest.mark.parametrize("entries", [0, 1])
def test_empty_and_zero_length_only_archives_aggregate_consistently(entries):
    objects = [{"zip_bytes": 22, "expanded_bytes": 0, "zip_entries": entries}] * 2
    resources = go_lane.proxy_resource_hint(objects)
    assert resources == hint(count=2, zipped=44, entries=2 * entries) | {"build_scratch_mb": 16384}


def test_metadata_only_proxy_does_not_reserve_a_local_workspace_zip(tmp_path, monkeypatch):
    directory = tmp_path / "original"
    proxy_object(directory / "home/go/pkg/mod/cache/download", "example.invalid/neighbor", "v1.2.3")
    ctx, task, _ = context(tmp_path, {
        "go.work": "go 1.20\nuse (\n ./app\n ./neighbor\n)\n",
        "app/go.mod": "module example.invalid/app\n\ngo 1.20\nrequire example.invalid/neighbor v1.2.3\n",
        "neighbor/go.mod": "module example.invalid/neighbor\n\ngo 1.20\n",
    })
    no_fetch(monkeypatch)
    asset = go_lane.module_proxy(ctx, task, directory, "app", contract=contract())
    assert asset["resource_hint"] == hint() | {"build_scratch_mb": 16384}


def test_oversized_nested_cache_is_not_clamped_below_global_admission(tmp_path, monkeypatch):
    resources = hint(count=100, zipped=20 * GiB, expanded=100 * GiB, entries=1000)
    recipe = recipe_with_hint(tmp_path / "recipe", monkeypatch, resources)
    assert recipe.build_scratch_mb == 128 * 1024
    gate = ResourceGate(tmp_path / "private-gate", tmp_path / "private-scratch", ResourceLimits(),
                        probe=lambda: pytest.fail("oversized request must fail before probing admission"))
    with pytest.raises(ContractError, match="single task exceeds configured reservation capacity"):
        with gate.acquire(ResourceRequest("go-nested-cache", scratch_bytes=recipe.build_scratch_mb * MiB)):
            pytest.fail("128 GiB admitted under a 96 GiB limit")


@pytest.mark.parametrize("field,value", [
    ("zip_count", True), ("zip_count", -1), ("zip_count", 12001),
    ("zip_bytes", 0), ("zip_bytes", -1), ("zip_bytes", "1024"), ("zip_bytes", 2**63),
    ("expanded_bytes", False), ("expanded_bytes", -1), ("expanded_bytes", 2**63),
    ("zip_entries", 0), ("zip_entries", -1), ("zip_entries", True), ("zip_entries", 2**63),
    ("build_allowance_bytes", 0), ("build_allowance_bytes", float(8 * GiB)),
    ("schema_version", "different-schema"), ("build_scratch_mb", 1),
])
def test_invalid_proxy_resource_hints_fail_closed(field, value):
    resources = hint(count=1, zipped=1024, expanded=4096, entries=2)
    resources[field] = value
    with pytest.raises(ContractError, match="go_proxy_resource"):
        go_lane.proxy_scratch_mb(resources)


@pytest.mark.parametrize("resources", [None, {}, hint(zipped=1), hint(expanded=1), hint(entries=1)])
def test_absent_or_inconsistent_empty_resource_hint_is_not_a_fixed_scratch_fallback(resources):
    with pytest.raises(ContractError, match="go_proxy_resource"):
        go_lane.proxy_scratch_mb(resources)


def test_resource_sum_overflow_is_rejected_before_mib_rounding(monkeypatch):
    resources = hint(count=40, zipped=4 * GiB, expanded=12 * GiB, entries=400)
    monkeypatch.setattr(go_lane, "MAX_GO_RESOURCE_BYTES", 24 * GiB - 1)
    with pytest.raises(ContractError, match="go_proxy_resource.*overflow"):
        go_lane.proxy_scratch_mb(resources)


@pytest.mark.parametrize("kind", ["compressed", "expanded", "entries", "negative-entry", "zero-byte-file", "malformed"])
def test_invalid_or_oversized_nested_zip_is_bounded_before_content_hashing(tmp_path, monkeypatch, kind):
    path = proxy_object(tmp_path / "proxy")[2]
    if kind == "compressed":
        monkeypatch.setattr(go_lane, "MAX_MODULE_ZIP_BYTES", path.stat().st_size - 1)
    elif kind == "expanded":
        monkeypatch.setattr(go_lane, "MAX_MODULE_EXPANDED_BYTES", 1)
    elif kind == "entries":
        monkeypatch.setattr(go_lane, "MAX_MODULE_ZIP_ENTRIES", 1)
    elif kind == "negative-entry":
        entry = SimpleNamespace(filename="example.invalid/lib@v1.2.3/lib.go", file_size=-1, compress_size=1)
        monkeypatch.setattr(go_lane.zipfile, "ZipFile", lambda _: nullcontext(
            SimpleNamespace(infolist=lambda: [entry])))
    elif kind == "zero-byte-file":
        path.write_bytes(b"")
    else:
        path.write_bytes(b"not a ZIP")
    with pytest.raises(ContractError, match="go_module_zip_resource"):
        go_lane.module_zip_resources(path)


def test_resource_hint_archive_count_has_the_same_closure_bound():
    resources = {"zip_bytes": 128, "expanded_bytes": 256, "zip_entries": 1}
    with pytest.raises(ContractError, match="go_proxy_resource"):
        go_lane.proxy_resource_hint([resources] * 12001)
