"""Small offline reproductions of the Bigtable and Optimism proxy gaps."""
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from test_agentic_go_dependencies import context, proxy_object
from test_agentic_go_wrappers import git, write
from agentic_runtime_migration import go_dependencies, go_lane
from agentic_runtime_migration.go_dependencies import content_sum, frozen_inputs
from reviewbench_maintainability.agentic.contracts import ContractError, file_hash


L2GETH = "github.com/ethereum-optimism/optimism/l2geth"
L2GETH_VERSION = "v0.0.0-20220923210602-7121648c1f26"


def contract(*, download_all=False, pins=()):
    return SimpleNamespace(kind="module-pin-test" if download_all else "direct-test",
                           spec={}, module_pins=list(pins))


def checksum(module, version, path, suffix=".mod"):
    return f"{module} {version}{'/go.mod' if suffix == '.mod' else ''} {content_sum(path, suffix)}\n"


def offline_go(tmp_path, repository, proxy, *arguments):
    go = shutil.which("go")
    assert go, "These offline regressions require the preinstalled Go compiler"
    env = dict(os.environ, GOTOOLCHAIN="local", GO111MODULE="on", GOENV="off",
               GOPROXY=proxy.as_uri(), GOSUMDB="off", GOPRIVATE="", GONOPROXY="", GONOSUMDB="",
               GOMODCACHE=str(tmp_path / "fresh-gomodcache"), GOCACHE=str(tmp_path / "fresh-gocache"),
               GOFLAGS="-p=1", GOMAXPROCS="1", GOWORK="auto", CGO_ENABLED="0")
    return subprocess.run([go, *arguments], cwd=repository, env=env, text=True,
                          capture_output=True, timeout=90)


def no_fetch(monkeypatch):
    monkeypatch.setattr(go_lane, "fetch_proxy_file",
                        lambda _, relative: pytest.fail("unexpected proxy fetch: " + relative))


def test_bigtable_download_all_closes_locked_transitive_zip_graph(tmp_path, monkeypatch):
    directory = tmp_path / "original"
    proxy = directory / "home/go/pkg/mod/cache/download"
    locked = "example.invalid/locked"
    child = "cloud.google.com/go/bigquery"
    pin = "example.invalid/wrapper-pin"
    locked_mod, _, locked_zip = proxy_object(
        proxy, locked, "v1.0.0", f"require {child} v1.8.0\n")
    child_mod, _, _ = proxy_object(
        proxy, child, "v1.8.0", "require example.invalid/leaf v1.0.0\n")
    proxy_object(proxy, "example.invalid/leaf", "v1.0.0")
    proxy_object(proxy, pin, "v1.0.0")
    # go.sum is a checksum ledger, not a list of active download-all roots.
    stale_mod, _, _ = proxy_object(
        proxy, "example.invalid/stale", "v1.0.0", "require example.invalid/unreachable v9.9.9\n")
    ctx, task, repository = context(tmp_path, {
        "go.mod": f"module cloud.google.com/go/bigtable\n\ngo 1.15\n"
                  f"require (\n {locked} v1.0.0\n {pin} v1.0.0\n)\n",
        "go.sum": checksum(locked, "v1.0.0", locked_mod)
                  + checksum(locked, "v1.0.0", locked_zip, ".zip")
                  + checksum(child, "v1.8.0", child_mod)
                  + checksum("example.invalid/stale", "v1.0.0", stale_mod),
        "candidate.go": "package bigtable\n",
    })
    write(directory / "session_oracle_test.go", "ORACLE_ONLY_SENTINEL\n")
    write(directory / "golden-1.log", "ORACLE_ONLY_SIGNATURE\n")
    before = {name: (repository / name).read_bytes() for name in ("go.mod", "go.sum")}
    no_fetch(monkeypatch)
    asset = go_lane.module_proxy(ctx, task, directory, "",
                                contract=contract(download_all=True, pins=[pin + "@v1.0.0"]))
    root = Path(asset["path"])
    # A fresh file-only cache proves that a metadata receipt alone is insufficient.
    replay = tmp_path / "replay"
    shutil.copytree(repository, replay, ignore=shutil.ignore_patterns(".git"))
    result = offline_go(tmp_path, replay, root, "mod", "download", "all")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / child / "@v/v1.8.0.zip").is_file()
    assert (root / "example.invalid/leaf/@v/v1.0.0.zip").is_file()
    assert not (root / "example.invalid/stale/@v/v1.0.0.zip").exists()
    assert not (root / "example.invalid/unreachable").exists()
    assert before == {name: (repository / name).read_bytes() for name in before}
    receipt = json.loads((root / "go-proxy-inputs.json").read_text())
    assert receipt["original_mod_download_all"] is True
    child_zip = next(r for r in receipt["objects"] if r["module"] == child and r["object"] == ".zip")
    assert child_zip["h1"].startswith("h1:") and child_zip["frozen_h1"] is None
    assert len(receipt["objects"]) == len({
        (r["module"], r["version"], r["object"]) for r in receipt["objects"]})
    assert all(path.name == "go-proxy-inputs.json" or path.suffix in {".mod", ".info", ".zip"}
               for path in root.rglob("*") if path.is_file())
    assert not any(b"ORACLE_ONLY_" in path.read_bytes() for path in root.rglob("*") if path.is_file())


def optimism_files():
    return {
        "go.work": "go 1.20\nuse (\n ./app\n ./l2geth\n ./batch\n)\n",
        "app/go.mod": f"module example.invalid/app\n\ngo 1.20\nrequire {L2GETH} {L2GETH_VERSION}\n",
        "app/source.go": f'package app\nimport lib "{L2GETH}"\nvar Value string = lib.Value\n',
        "l2geth/go.mod": f"module {L2GETH}\n\ngo 1.20\n",
        "l2geth/source.go": 'package lib\nconst Value = "current candidate, not the remote ZIP"\n',
        "batch/go.mod": f"module example.invalid/batch\n\ngo 1.20\nrequire {L2GETH} v0.0.0\n"
                        f"replace {L2GETH} v0.0.0 => ../l2geth\n",
    }


def test_optimism_workspace_keeps_versioned_remote_mod_but_uses_candidate_source(tmp_path, monkeypatch):
    directory = tmp_path / "original"
    proxy = directory / "home/go/pkg/mod/cache/download"
    proxy_object(proxy, L2GETH, L2GETH_VERSION)
    ctx, task, repository = context(tmp_path, optimism_files())
    before = {p.relative_to(repository): p.read_bytes() for p in repository.rglob("*")
              if p.is_file() and ".git" not in p.parts}
    no_fetch(monkeypatch)
    asset = go_lane.module_proxy(ctx, task, directory, "app", contract=contract())
    root = Path(asset["path"])
    result = offline_go(tmp_path, repository / "app", root, "list", "-m", "all")
    assert result.returncode == 0, result.stdout + result.stderr
    result = offline_go(tmp_path, repository / "app", root, "test", "-count=1", ".")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / L2GETH / f"@v/{L2GETH_VERSION}.mod").is_file()
    # A workspace's current source and a required version's graph are distinct.
    assert not (root / L2GETH / f"@v/{L2GETH_VERSION}.zip").exists()
    assert not (root / L2GETH / "@v/v0.0.0.mod").exists()
    assert all((repository / name).read_bytes() == data for name, data in before.items())
    receipt = json.loads((root / "go-proxy-inputs.json").read_text())
    assert L2GETH in receipt["candidate_local_modules"]
    assert not any(r["object"] == ".zip" for r in receipt["objects"])


@pytest.mark.parametrize("required,remote", [("v0.0.0", False), ("v1.2.3", True)])
def test_version_specific_local_replace_does_not_hide_other_remote_versions(tmp_path, required, remote):
    module = "example.invalid/neighbor"
    ctx, task, _ = context(tmp_path, {
        "go.mod": f"module example.invalid/main\n\ngo 1.20\nrequire {module} {required}\n"
                  f"replace {module} v0.0.0 => ./neighbor\n",
        "neighbor/go.mod": f"module {module}\n\ngo 1.20\n",
    })
    inputs = frozen_inputs(ctx, task, tmp_path, "")
    assert ((module, required) in inputs.modules) is remote
    if remote:
        assert inputs.modules[(module, required)].zip_required is True


def test_remote_replacement_target_can_also_be_a_workspace_module(tmp_path, monkeypatch):
    directory = tmp_path / "original"
    mod, _, archive = proxy_object(directory / "home/go/pkg/mod/cache/download", L2GETH, L2GETH_VERSION)
    files = optimism_files()
    files["app/go.mod"] = (
        "module example.invalid/app\n\ngo 1.20\nrequire example.invalid/alias v1.0.0\n"
        f"replace example.invalid/alias v1.0.0 => {L2GETH} {L2GETH_VERSION}\n")
    files["app/go.sum"] = (checksum(L2GETH, L2GETH_VERSION, mod)
                           + checksum(L2GETH, L2GETH_VERSION, archive, ".zip"))
    ctx, task, _ = context(tmp_path, files)
    no_fetch(monkeypatch)
    asset = go_lane.module_proxy(ctx, task, directory, "app", contract=contract())
    root = Path(asset["path"])
    assert (root / L2GETH / f"@v/{L2GETH_VERSION}.zip").read_bytes() == archive.read_bytes()
    receipt = json.loads((root / "go-proxy-inputs.json").read_text())
    assert all(r["h1"] == r["frozen_h1"] for r in receipt["objects"] if r["object"] != ".info")


def test_download_all_revisits_mod_only_objects_and_terminates_cycles(tmp_path, monkeypatch):
    directory = tmp_path / "original"
    proxy = directory / "home/go/pkg/mod/cache/download"
    a = "example.invalid/a-child"
    z = "example.invalid/z-root"
    child_mod, _, _ = proxy_object(proxy, a, "v1.0.0", f"require {z} v1.0.0\n")
    root_mod, _, root_zip = proxy_object(proxy, z, "v1.0.0", f"require {a} v1.0.0\n")
    ctx, task, _ = context(tmp_path, {
        "go.mod": f"module example.invalid/main\n\ngo 1.15\nrequire {z} v1.0.0\n",
        "go.sum": checksum(z, "v1.0.0", root_mod) + checksum(z, "v1.0.0", root_zip, ".zip")
                  + checksum(a, "v1.0.0", child_mod),
    })
    no_fetch(monkeypatch)
    asset = go_lane.module_proxy(ctx, task, directory, "", contract=contract(download_all=True))
    root = Path(asset["path"])
    assert (root / a / "@v/v1.0.0.zip").is_file()
    receipt = json.loads((root / "go-proxy-inputs.json").read_text())
    assert len(receipt["objects"]) == 6
    assert all(any("require-graph:" in origin or "download-all-graph:" in origin for origin in r["origins"])
               for r in receipt["objects"])


def test_download_all_honors_main_replacements_not_dependency_replacements(tmp_path, monkeypatch):
    directory = tmp_path / "original"
    proxy = directory / "home/go/pkg/mod/cache/download"
    root_mod, _, _ = proxy_object(
        proxy, "example.invalid/lib", "v1.0.0",
        "require example.invalid/child v0.0.0\nreplace example.invalid/child => ../ignored\n")
    proxy_object(proxy, "example.invalid/fork", "v1.7.0",
                 "require example.invalid/leaf v1.0.0\nreplace example.invalid/leaf => ../also-ignored\n")
    proxy_object(proxy, "example.invalid/leaf", "v1.0.0")
    ctx, task, repository = context(tmp_path, {
        "go.mod": "module example.invalid/main\n\ngo 1.15\nrequire example.invalid/lib v1.0.0\n"
                  "replace example.invalid/child v0.0.0 => example.invalid/fork v1.7.0\n",
        "go.sum": checksum("example.invalid/lib", "v1.0.0", root_mod),
    })
    no_fetch(monkeypatch)
    asset = go_lane.module_proxy(ctx, task, directory, "", contract=contract(download_all=True))
    root = Path(asset["path"])
    result = offline_go(tmp_path, repository, root, "mod", "download", "all")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / "example.invalid/fork/@v/v1.7.0.zip").is_file()
    assert (root / "example.invalid/leaf/@v/v1.0.0.zip").is_file()
    assert not (root / "example.invalid/child").exists()


def test_go_work_wildcard_overrides_version_specific_local_rules_before_traversal(tmp_path, monkeypatch):
    directory = tmp_path / "original"
    proxy_object(directory / "home/go/pkg/mod/cache/download", "example.invalid/fork", "v1.7.0")
    ctx, task, _ = context(tmp_path, {
        "go.work": "go 1.20\nuse (\n ./app\n ./neighbor\n)\n"
                   "replace example.invalid/lib => example.invalid/fork v1.7.0\n",
        "app/go.mod": "module example.invalid/app\n\ngo 1.20\nrequire example.invalid/lib v1.0.0\n"
                      "replace example.invalid/lib v1.0.0 => ../../outside\n",
        "neighbor/go.mod": "module example.invalid/neighbor\n\ngo 1.20\n"
                           "replace example.invalid/lib v1.0.0 => ../absent\n",
    })
    no_fetch(monkeypatch)
    asset = go_lane.module_proxy(ctx, task, directory, "app", contract=contract())
    assert (Path(asset["path"]) / "example.invalid/fork/@v/v1.7.0.zip").is_file()
    assert not (Path(asset["path"]) / "example.invalid/lib").exists()


def test_local_dependency_replacements_are_ignored_like_remote_dependency_replacements(tmp_path):
    ctx, task, _ = context(tmp_path, {
        "go.mod": "module example.invalid/main\n\ngo 1.20\nrequire example.invalid/local v0.0.0\n"
                  "replace example.invalid/local => ./local\n",
        "local/go.mod": "module example.invalid/local\n\ngo 1.20\nrequire example.invalid/lib v1.2.3\n"
                        "replace example.invalid/lib => ../../outside\n",
    })
    inputs = frozen_inputs(ctx, task, tmp_path, "")
    assert inputs.modules[("example.invalid/lib", "v1.2.3")].zip_required
    assert ("example.invalid/local", "v0.0.0") not in inputs.modules


def test_start_and_golden_graphs_keep_their_own_replacement_context(tmp_path, monkeypatch):
    directory = tmp_path / "original"
    proxy = directory / "home/go/pkg/mod/cache/download"
    sums = ""
    for phase in ("start", "golden"):
        mod, _, _ = proxy_object(proxy, f"example.invalid/{phase}-lib", "v1.0.0",
                                 f"require example.invalid/{phase}-child v0.0.0\n")
        sums += checksum(f"example.invalid/{phase}-lib", "v1.0.0", mod)
        proxy_object(proxy, f"example.invalid/{phase}-fork", "v1.0.0")

    def manifest(phase):
        return ("module example.invalid/main\n\ngo 1.15\n"
                f"require example.invalid/{phase}-lib v1.0.0\n"
                f"replace example.invalid/{phase}-child v0.0.0 => example.invalid/{phase}-fork v1.0.0\n")

    # A retained checksum in another phase is not a require edge in that phase.
    ctx, task, repository = context(tmp_path, {"go.mod": manifest("start"), "go.sum": sums})
    (repository / "go.mod").write_text(manifest("golden"))
    golden = write(directory / "dependency-closed-source-only.patch",
                   git(repository, "diff", "--binary", "--full-index") + "\n")
    no_fetch(monkeypatch)
    asset = go_lane.module_proxy(ctx, task, directory, "", oracle={"golden_patch_sha256": file_hash(golden)},
                                contract=contract(download_all=True))
    root = Path(asset["path"])
    assert (repository / "go.mod").read_text() == manifest("golden")
    receipt = json.loads((root / "go-proxy-inputs.json").read_text())
    assert {r["module"] for r in receipt["objects"]} == {
        f"example.invalid/{phase}-{part}" for phase in ("start", "golden") for part in ("lib", "fork")}
    assert {r["phase"] for r in receipt["manifest_inputs"]} == {"start", "golden"}


def test_transitive_download_all_expansion_enforces_the_existing_module_bound(tmp_path, monkeypatch):
    directory = tmp_path / "original"
    proxy = directory / "home/go/pkg/mod/cache/download"
    proxy_object(proxy, "example.invalid/one", "v1.0.0", "require example.invalid/two v1.0.0\n")
    proxy_object(proxy, "example.invalid/two", "v1.0.0", "require example.invalid/three v1.0.0\n")
    ctx, task, _ = context(tmp_path, {
        "go.mod": "module example.invalid/main\n\ngo 1.15\nrequire example.invalid/one v1.0.0\n",
    })
    monkeypatch.setattr(go_dependencies, "MAX_MODULES", 2)
    no_fetch(monkeypatch)
    with pytest.raises(ContractError, match="go_dependency_closure_exceeds_bound"):
        go_lane.module_proxy(ctx, task, directory, "", contract=contract(download_all=True))
    assert not (tmp_path / "bundled").exists()
