"""Offline lock/proxy regressions: no historical production overlay or resolver."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from agentic_runtime_migration import go_lane
from agentic_runtime_migration.go_dependencies import (
    GoInputs, content_sum, frozen_inputs, manifest_snapshots, parse_mod, work_uses,
)
from reviewbench_maintainability.agentic.contracts import ContractError
from test_agentic_go_wrappers import commit, git, write


def context(tmp_path, files):
    repository = tmp_path / "candidate"
    for name, data in files.items():
        write(repository / name, data)
    revision = commit(repository)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    output = tmp_path / "output"
    output.mkdir()

    def bundle(root):
        target = tmp_path / "bundled"
        shutil.copytree(root, target)
        return {"path": str(target), "sha256": "b" * 64}

    return (SimpleNamespace(repo=lambda _: repository, scratch=scratch, output=output, config={},
                            legacy=tmp_path, bundle=bundle),
            {"id": "go-lock-fixture", "start_sha": revision}, repository)


def proxy_object(root, module="example.invalid/lib", version="v1.2.3", requires=""):
    escaped = go_lane.escape_module(module)
    prefix = root / escaped / "@v" / version
    mod = write(Path(str(prefix) + ".mod"), f"module {module}\n\ngo 1.20\n" + requires)
    info = write(Path(str(prefix) + ".info"), json.dumps({"Version": version, "Time": "2020-01-01T00:00:00Z"}))
    archive = Path(str(prefix) + ".zip")
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr(f"{module}@{version}/go.mod", mod.read_bytes())
        stream.writestr(f"{module}@{version}/lib.go", "package lib\nconst Value = 1\n")
    return mod, info, archive


def test_manifest_parser_preserves_require_replacement_and_local_edges():
    requires, replaces = parse_mod("""module example.invalid/main
go 1.20
require (
    example.invalid/One v1.2.3 // indirect
    example.invalid/two v0.0.0-20200101000000-123456789abc
)
replace example.invalid/One v1.2.3 => example.invalid/Fork v1.4.5
replace (
    example.invalid/two => ../current-helper
)
""")
    assert requires == [("example.invalid/One", "v1.2.3"), ("example.invalid/two", "v0.0.0-20200101000000-123456789abc")]
    assert replaces == [("example.invalid/One", "v1.2.3", "example.invalid/Fork", "v1.4.5"),
                        ("example.invalid/two", None, "../current-helper", None)]


@pytest.mark.parametrize("version", ["latest", "main", "../escape", "v1.0.0/../escape"])
def test_no_floating_or_traversing_proxy_inputs(version):
    with pytest.raises(ContractError):
        GoInputs().add("example.invalid/mod", version)


def test_lock_union_rejects_two_hashes_for_the_same_version():
    inputs = GoInputs()
    inputs.add("example.invalid/mod", "v1.2.3", checksum="h1:" + "a" * 43 + "=", suffix=".mod")
    with pytest.raises(ContractError, match="checksum_conflict"):
        inputs.add("example.invalid/mod", "v1.2.3", checksum="h1:" + "b" * 43 + "=", suffix=".mod")


def test_hash1_matches_real_offline_go_download(tmp_path):
    go = shutil.which("go")
    if not go:
        pytest.skip("preinstalled Go required")
    proxy = tmp_path / "proxy"
    mod, _, archive = proxy_object(proxy)
    env = dict(os.environ, GOMODCACHE=str(tmp_path / "gomodcache"), GOTOOLCHAIN="local", GOWORK="off",
               GOPROXY=proxy.as_uri(), GOSUMDB="off", GO111MODULE="on", GOPRIVATE="", GONOPROXY="",
               GOFLAGS="-p=2", GOMAXPROCS="2")
    result = subprocess.run([go, "mod", "download", "-json", "example.invalid/lib@v1.2.3"],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    downloaded = json.loads(result.stdout)
    assert content_sum(mod, ".mod") == downloaded["GoModSum"]
    assert content_sum(archive, ".zip") == downloaded["Sum"]


def test_start_and_golden_metadata_only_include_local_candidate_modules(tmp_path):
    ctx, task, repository = context(tmp_path, {
        "go.mod": "module example.invalid/main\n\ngo 1.20\nreplace example.invalid/shared => ./shared\n",
        "main.go": "package main\nconst Version = 1\n",
        "shared/go.mod": "module example.invalid/shared\n\ngo 1.20\n",
        "shared/production.go": "package shared\nconst Version = 1\n",
    })
    (repository / "go.mod").write_text(
        "module example.invalid/main\n\ngo 1.20\nreplace example.invalid/shared => ./newshared\n")
    write(repository / "newshared/go.mod", "module example.invalid/shared\n\ngo 1.20\nrequire example.invalid/lib v1.2.3\n")
    write(repository / "newshared/production.go", "package shared\nconst Version = 999\n")
    (repository / "main.go").write_text("package main\nconst Version = 999\n")
    git(repository, "add", "-N", ".")
    patch = write(tmp_path / "golden.patch", git(repository, "diff", "--binary", "--full-index") + "\n")
    snapshots = dict(manifest_snapshots(ctx, task, "", patch))
    assert set(snapshots["start"]) == {"go.mod", "shared/go.mod"}
    assert set(snapshots["golden"]) == {"go.mod", "newshared/go.mod"}
    assert b"v1.2.3" in snapshots["golden"]["newshared/go.mod"]
    assert not any(name.endswith(".go") for files in snapshots.values() for name in files)
    assert (repository / "main.go").read_text() == "package main\nconst Version = 999\n"


def test_deleted_golden_module_is_not_silently_restored_from_start(tmp_path):
    ctx, task, repository = context(tmp_path, {"go.mod": "module example.invalid/main\n\ngo 1.20\n"})
    (repository / "go.mod").unlink()
    patch = write(tmp_path / "golden.patch", git(repository, "diff", "--binary", "--full-index") + "\n")
    with pytest.raises(ContractError, match="local_replacement_manifest_missing"):
        manifest_snapshots(ctx, task, "", patch)


def workspace_files():
    return {
        "go.work": "go 1.20\nuse (\n ./app\n ./neighbor\n)\n",
        "go.work.sum": "example.invalid/external v1.7.0 h1:" + "a" * 43 + "=\n",
        "app/go.mod": "module example.invalid/app\n\ngo 1.20\nrequire example.invalid/neighbor v1.2.3\n",
        "app/main.go": "package app\n",
        "neighbor/go.mod": "module example.invalid/neighbor\n\ngo 1.20\n",
        "neighbor/production.go": "package neighbor\n",
    }


def test_ancestor_workspace_keeps_all_current_use_modules_and_work_sum(tmp_path):
    ctx, task, _ = context(tmp_path, workspace_files())
    files = dict(manifest_snapshots(ctx, task, "app", None))["start"]
    assert set(files) == {"go.work", "go.work.sum", "app/go.mod", "neighbor/go.mod"}
    inputs = frozen_inputs(ctx, task, tmp_path, "app")
    assert {"example.invalid/app", "example.invalid/neighbor"} <= inputs.local_modules
    # Main workspace source is local, but its explicitly required version's
    # module graph is still a remote metadata input to Go.
    assert inputs.modules[("example.invalid/neighbor", "v1.2.3")].zip_required is False
    assert ("example.invalid/external", "v1.7.0") in inputs.modules
    assert inputs.modules[("example.invalid/external", "v1.7.0")].origins == ["start:go.work.sum"]


def test_only_an_original_explicit_gowork_off_disables_workspace(tmp_path):
    ctx, task, _ = context(tmp_path, workspace_files())
    contract = SimpleNamespace(kind="direct-test", spec={"environment": {"GOWORK": "off"}}, module_pins=[])
    inputs = frozen_inputs(ctx, task, tmp_path, "app", contract=contract)
    assert {m["path"] for m in inputs.manifests} == {"app/go.mod"}
    assert ("example.invalid/neighbor", "v1.2.3") in inputs.modules
    assert ("example.invalid/external", "v1.7.0") not in inputs.modules


def test_golden_workspace_lock_hunks_are_frozen_without_source_copy(tmp_path):
    ctx, task, repository = context(tmp_path, workspace_files())
    (repository / "go.work").write_text("go 1.20\nuse (\n ./app\n ./new-neighbor\n)\n")
    (repository / "go.work.sum").write_text("example.invalid/external v1.8.0 h1:" + "b" * 43 + "=\n")
    write(repository / "new-neighbor/go.mod", "module example.invalid/neighbor\n\ngo 1.20\n")
    write(repository / "new-neighbor/production.go", "package neighbor\nconst New = true\n")
    git(repository, "add", "-N", ".")
    patch = write(tmp_path / "golden.patch", git(repository, "diff", "--binary", "--full-index") + "\n")
    snapshots = dict(manifest_snapshots(ctx, task, "app", patch))
    assert set(snapshots["golden"]) == {"go.work", "go.work.sum", "app/go.mod", "new-neighbor/go.mod"}
    assert b"v1.7.0" in snapshots["start"]["go.work.sum"]
    assert b"v1.8.0" in snapshots["golden"]["go.work.sum"]
    assert not any(name.endswith(".go") for files in snapshots.values() for name in files)


@pytest.mark.parametrize("use,error", [
    ("./missing", "local_replacement_manifest_missing"),
    ("../../outside", "invalid repository path"),
    ("/absolute/host/module", "external_local_replacement_pending"),
])
def test_workspace_holes_remain_gaps_instead_of_turning_workspace_off(tmp_path, use, error):
    files = workspace_files()
    files["go.work"] = "go 1.20\nuse (\n ./app\n " + use + "\n)\n"
    ctx, task, _ = context(tmp_path, files)
    with pytest.raises(ContractError, match=error):
        frozen_inputs(ctx, task, tmp_path, "app")


def test_workspace_use_parsing_preserves_quoted_paths_and_single_directive():
    assert work_uses('go 1.20\nuse "./one module"\nuse (\n ./two\n)\nreplace a => ./not-a-use\n') == [
        "./one module", "./two"]


def test_external_local_replacement_remains_an_exact_gap(tmp_path):
    ctx, task, repository = context(tmp_path, {
        "go.mod": "module example.invalid/main\n\ngo 1.20\nreplace example.invalid/dep => ../../outside\n",
    })
    with pytest.raises(ContractError):
        frozen_inputs(ctx, task, tmp_path, "")


def test_proxy_checks_original_go_sum_and_freezes_local_objects(tmp_path, monkeypatch):
    directory = tmp_path / "original"
    proxy = directory / "home/go/pkg/mod/cache/download"
    mod, _, archive = proxy_object(proxy)
    checksum = f"example.invalid/lib v1.2.3 {content_sum(archive, '.zip')}\n"
    checksum += f"example.invalid/lib v1.2.3/go.mod {content_sum(mod, '.mod')}\n"
    ctx, task, repository = context(tmp_path, {
        "go.mod": "module example.invalid/main\n\ngo 1.20\nrequire example.invalid/lib v1.2.3\n",
        "go.sum": checksum,
    })
    monkeypatch.setattr(go_lane, "fetch_proxy_file", lambda *args: pytest.fail("no network for a cached exact graph"))
    asset = go_lane.module_proxy(ctx, task, directory, "")
    root = Path(asset["path"])
    receipt = json.loads((root / "go-proxy-inputs.json").read_text())
    assert {r["object"] for r in receipt["objects"]} == {".mod", ".info", ".zip"}
    assert all(r["h1"] == r["frozen_h1"] for r in receipt["objects"] if r["object"] != ".info")
    assert (repository / "go.sum").read_text() == checksum


@pytest.mark.parametrize("suffix", [".mod", ".zip"])
def test_bad_cached_checksum_never_becomes_a_new_lock(tmp_path, monkeypatch, suffix):
    directory = tmp_path / "original"
    mod, _, archive = proxy_object(directory / "home/go/pkg/mod/cache/download")
    checksum = f"example.invalid/lib v1.2.3 {content_sum(archive, '.zip')}\n"
    checksum += f"example.invalid/lib v1.2.3/go.mod {content_sum(mod, '.mod')}\n"
    ctx, task, _ = context(tmp_path, {
        "go.mod": "module example.invalid/main\n\ngo 1.20\nrequire example.invalid/lib v1.2.3\n",
        "go.sum": checksum,
    })
    if suffix == ".mod":
        mod.write_text("module altered.invalid/lib\n")
    else:
        with zipfile.ZipFile(archive, "a") as stream:
            stream.writestr("example.invalid/lib@v1.2.3/changed.go", "package lib\n")
    monkeypatch.setattr(go_lane, "fetch_proxy_file", lambda *args: pytest.fail("mismatch is not a cache miss"))
    with pytest.raises(ContractError, match="checksum differs"):
        go_lane.module_proxy(ctx, task, directory, "")


def test_explicit_wrapper_pin_follows_only_exact_new_transitive_metadata(tmp_path, monkeypatch):
    directory = tmp_path / "original"
    proxy = directory / "home/go/pkg/mod/cache/download"
    proxy_object(proxy, "example.invalid/pinned", "v2.0.0+incompatible", "require example.invalid/child v1.7.0\n")
    proxy_object(proxy, "example.invalid/child", "v1.7.0")
    ctx, task, _ = context(tmp_path, {"go.mod": "module example.invalid/main\n\ngo 1.20\n"})
    inputs = GoInputs(download_all=True)
    inputs.add("example.invalid/pinned", "v2.0.0+incompatible", zipped=True, origin="original-wrapper:require")
    inputs.expand.add(("example.invalid/pinned", "v2.0.0+incompatible"))
    monkeypatch.setattr(go_lane, "frozen_inputs", lambda *args, **kwargs: inputs)
    monkeypatch.setattr(go_lane, "fetch_proxy_file", lambda *args: pytest.fail("floating/network resolution"))
    asset = go_lane.module_proxy(ctx, task, directory, "")
    receipt = json.loads((Path(asset["path"]) / "go-proxy-inputs.json").read_text())
    assert {(r["module"], r["version"]) for r in receipt["objects"]} == {
        ("example.invalid/pinned", "v2.0.0+incompatible"), ("example.invalid/child", "v1.7.0")}
    assert (Path(asset["path"]) / "example.invalid/child/@v/v1.7.0.zip").is_file()


def test_wrapper_pin_revisits_an_already_staged_child_to_close_its_graph(tmp_path, monkeypatch):
    directory = tmp_path / "original"
    proxy = directory / "home/go/pkg/mod/cache/download"
    proxy_object(proxy, "example.invalid/z-pin", "v1.0.0", "require example.invalid/a-child v1.0.0\n")
    proxy_object(proxy, "example.invalid/a-child", "v1.0.0", "require example.invalid/grandchild v1.0.0\n")
    proxy_object(proxy, "example.invalid/grandchild", "v1.0.0")
    ctx, task, _ = context(tmp_path, {"go.mod": "module example.invalid/main\n\ngo 1.20\n"})
    inputs = GoInputs(download_all=True)
    for name in ("example.invalid/z-pin", "example.invalid/a-child"):
        inputs.add(name, "v1.0.0", zipped=True)
    inputs.expand.add(("example.invalid/z-pin", "v1.0.0"))
    monkeypatch.setattr(go_lane, "frozen_inputs", lambda *args, **kwargs: inputs)
    monkeypatch.setattr(go_lane, "fetch_proxy_file", lambda *args: pytest.fail("unexpected network"))
    asset = go_lane.module_proxy(ctx, task, directory, "")
    assert (Path(asset["path"]) / "example.invalid/grandchild/@v/v1.0.0.zip").is_file()


def test_compiler_respects_original_exact_version_not_latest_available(tmp_path):
    ctx, task, _ = context(tmp_path, {"go.mod": "module example.invalid/main\n\ngo 1.20\n"})
    spec = {"command": ["bash", "{task_dir}/run_oracle.sh"],
            "environment_description": {"toolchain": "Pinned cached Go 1.24.10"}}
    assert go_lane.compiler_version(ctx, spec, task, "") == "1.24.10"
