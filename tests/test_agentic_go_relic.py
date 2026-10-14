"""Dependency binding and real offline cgo fixtures; not benchmark readiness."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from agentic_runtime_migration import go_lane, go_relic
from agentic_runtime_migration.go_relic_prepare import checked_crypto_zip, extract_git_export
from reviewbench_maintainability.agentic.assets import ASSET_SCHEMA
from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash
from test_agentic_go_wrappers import assembled, commit, fixture, write


def pinned(path, value):
    path.write_text(json.dumps(value))
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def selected(tmp_path):
    repo = tmp_path / "repo"
    write(repo / "go.mod", f"module example.invalid/main\n\ngo 1.20\nrequire {go_relic.MODULE} {go_relic.VERSION}\n")
    write(repo / "go.sum", f"{go_relic.MODULE} {go_relic.VERSION} {go_relic.ZIP_H1}\n")
    task = {"id": "fixture", "start_sha": commit(repo)}
    ctx = SimpleNamespace(repo=lambda _: repo, config={})
    contract = SimpleNamespace(build_flags=["-tags", "relic"], module_cwd="")
    files = [{"path": name, "bytes": 1, "sha256": "a" * 64}
             for name in ("include/relic.h", "generated/relic_conf.h", "lib/librelic_s.a")]
    asset = {
        "schema_version": ASSET_SCHEMA, "source_kind": "dependency",
        "git_commit": go_relic.RELIC_COMMIT, "files": len(files),
        "tree_sha256": canonical_hash(go_relic.runtime_file_records(files)),
    }
    descriptor = {
        "schema_version": go_relic.SCHEMA, "platform": "linux/amd64",
        "crypto": go_relic.frozen_crypto_binding(ctx, task, ""),
        "relic_commit": go_relic.RELIC_COMMIT, "relic_tree": go_relic.RELIC_TREE,
        "cmake_definitions": go_relic.cmake_definitions(),
        "build_status": "built", "candidate_inputs_used": False,
        "runtime_files": files, "runtime_asset": pinned(tmp_path / "asset.json", asset),
    }
    ctx.config = {"migration": {"go_relic_dependencies": {
        task["id"]: pinned(tmp_path / "dependency.json", descriptor),
    }}}
    return ctx, task, contract, descriptor, asset


def repin(ctx, task, path, value):
    ctx.config["migration"]["go_relic_dependencies"][task["id"]] = pinned(path, value)


def test_unselected_tasks_never_read_a_dependency_or_candidate(tmp_path):
    ctx = SimpleNamespace(config={}, repo=lambda _: pytest.fail("unselected source read"))
    assert go_relic.dependency(ctx, {"id": "unselected"}, None) is None


def test_selected_dependency_is_readonly_developer_header_library_asset(tmp_path):
    ctx, task, contract, _, _ = selected(tmp_path)
    value = go_relic.dependency(ctx, task, contract)
    assert value["asset"]["target"] == go_relic.MOUNT
    assert value["asset"]["visibility"] == "developer"
    assert value["provenance"]["role"] == "external-relic-dependency"


@pytest.mark.parametrize("field,value", [
    ("schema_version", "other"), ("platform", "linux/arm64"),
    ("relic_commit", "1" * 40), ("relic_tree", "2" * 40),
    ("cmake_definitions", ["-DFP_PRIME=254"]), ("build_status", "pending"),
    ("candidate_inputs_used", True), ("crypto", {}),
])
def test_other_pins_flags_or_sut_inputs_are_rejected(tmp_path, field, value):
    ctx, task, contract, descriptor, _ = selected(tmp_path)
    descriptor[field] = value
    repin(ctx, task, tmp_path / "dependency.json", descriptor)
    with pytest.raises(ContractError, match="binding_differs"):
        go_relic.dependency(ctx, task, contract)


@pytest.mark.parametrize("field,value", [
    ("source_kind", "grader"), ("git_commit", "f" * 40),
    ("tree_sha256", "0" * 64), ("files", 100),
])
def test_asset_descriptor_must_bind_the_actual_dependency_manifest(tmp_path, field, value):
    ctx, task, contract, descriptor, asset = selected(tmp_path)
    asset[field] = value
    descriptor["runtime_asset"] = pinned(tmp_path / "asset.json", asset)
    repin(ctx, task, tmp_path / "dependency.json", descriptor)
    with pytest.raises(ContractError, match="archive_binding_differs"):
        go_relic.dependency(ctx, task, contract)


def test_changed_descriptor_is_rejected_before_it_is_used(tmp_path):
    ctx, task, contract, descriptor, _ = selected(tmp_path)
    (tmp_path / "dependency.json").write_text(json.dumps({**descriptor, "relic_commit": "f" * 40}))
    with pytest.raises(ContractError, match="digest_differs"):
        go_relic.dependency(ctx, task, contract)


@pytest.mark.parametrize("name", [
    "flow-go.test", "lib/old_sut.a", "include/../../candidate.go",
    "include/current.go", "/input/relic.h", "include/relic.h/../bad.h",
])
def test_runtime_file_allowlist_rejects_candidate_outputs_and_traversal(tmp_path, name):
    _, _, _, descriptor, _ = selected(tmp_path)
    descriptor["runtime_files"].append({"path": name, "bytes": 1, "sha256": "a" * 64})
    with pytest.raises(ContractError, match="runtime_file"):
        go_relic.runtime_file_records(descriptor["runtime_files"])


@pytest.mark.parametrize("change", ["version", "replacement", "checksum"])
def test_frozen_module_graph_not_a_reference_variant_selects_dependency(tmp_path, change):
    ctx, task, contract, _, _ = selected(tmp_path)
    repo = ctx.repo(task)
    if change == "version":
        (repo / "go.mod").write_text((repo / "go.mod").read_text().replace("v0.24.9", "v0.24.8"))
    elif change == "replacement":
        with (repo / "go.mod").open("a") as stream:
            stream.write(f"replace {go_relic.MODULE} => ./old-sut\n")
    else:
        (repo / "go.sum").write_text(f"{go_relic.MODULE} {go_relic.VERSION} h1:other\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "different input"], check=True)
    task["start_sha"] = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    with pytest.raises(ContractError, match="frozen_crypto"):
        go_relic.dependency(ctx, task, contract)


@pytest.mark.parametrize("flags,expected", [
    (["-tags", "relic"], True), (["-tags=netgo,relic"], True),
    (["-tags", "netgo relic"], True), (["-tags", "norelic"], False), ([], False),
])
def test_tag_parsing_never_uses_a_test_outcome(flags, expected):
    assert go_relic.has_relic_tag(flags) is expected


def test_no_selected_original_tag_is_rejected(tmp_path):
    ctx, task, contract, _, _ = selected(tmp_path)
    contract.build_flags = []
    with pytest.raises(ContractError, match="original_build_tag"):
        go_relic.dependency(ctx, task, contract)


def test_environment_appends_paths_without_replacing_original_flags():
    original = {"CGO_CPPFLAGS": "-DKEEP=1", "CGO_LDFLAGS": "-lm", "CGO_CFLAGS": "-O1",
                "GOFLAGS": "-mod=readonly -p=2", "OTHER": "unchanged"}
    before = dict(original)
    result = go_relic.cgo_environment(original)
    assert original == before
    assert result["CGO_CPPFLAGS"].startswith("-DKEEP=1 ")
    assert result["CGO_LDFLAGS"].startswith("-lm ")
    assert result["CGO_CFLAGS"] == "-O1" and result["GOFLAGS"] == original["GOFLAGS"]
    assert go_relic.cgo_environment({})["CGO_LDFLAGS"].startswith("-O2 -g ")
    with pytest.raises(ContractError, match="cgo_disabled"):
        go_relic.cgo_environment({"CGO_ENABLED": "0"})


@pytest.mark.parametrize("mount", ["relative/path", "/a/../b", "/a;exit", "/a b", "/a$(id)"])
def test_cgo_path_rejects_unresolved_or_shell_syntax(mount):
    with pytest.raises(ContractError, match="mount_invalid"):
        go_relic.cgo_environment({}, mount)


def test_recipe_keeps_public_check_original_assertions_and_golden(tmp_path, monkeypatch):
    ctx, task, oracle, _ = fixture(tmp_path, "059-google-cloud-go")
    baseline = assembled(monkeypatch, ctx, task, oracle)
    monkeypatch.setattr(go_lane, "relic_dependency", lambda *_: {
        "asset": {"path": "relic-archive", "sha256": "a" * 64, "kind": "archive",
                  "visibility": "developer", "target": go_relic.MOUNT},
        "provenance": {"path": "dependency.json", "sha256": "b" * 64, "role": "external-relic-dependency"},
    })
    changed = assembled(monkeypatch, ctx, task, oracle)
    assert changed.dev_checks == baseline.dev_checks
    assert changed.grading["commands"] == baseline.grading["commands"]
    assert changed.grading["golden_patch"] == baseline.grading["golden_patch"]
    assert changed.grading["bootstrap"] == baseline.grading["bootstrap"]
    assert changed.memory_mb == 8192 and changed.cpus == 2
    assert changed.build_scratch_mb == baseline.build_scratch_mb


def test_nonpinned_crypto_bytes_are_not_accepted(tmp_path):
    path = tmp_path / "crypto.zip"
    path.write_bytes(b"not the pinned public module")
    with pytest.raises(ContractError, match="crypto_zip_differs"):
        checked_crypto_zip(path)


def test_export_rejects_symlink_instead_of_following_old_checkout(tmp_path):
    import tarfile
    path = tmp_path / "source.tar"
    with tarfile.open(path, "w") as archive:
        item = tarfile.TarInfo("include/relic.h")
        item.type = tarfile.SYMTYPE
        item.linkname = "/old/candidate"
        archive.addfile(item)
    with pytest.raises(ContractError, match="special_file"):
        extract_git_export(path, tmp_path / "export")


@pytest.fixture(scope="module")
def real_go(tmp_path_factory):
    if not all(shutil.which(tool) for tool in ("go", "cc", "ar")):
        pytest.skip("real cgo fixture requires Go, cc, and ar")
    cache = tmp_path_factory.mktemp("relic-cgo-cache")
    return {
        **os.environ, "GOTOOLCHAIN": "local", "GOWORK": "off", "GOPROXY": "off",
        "GOSUMDB": "off", "GOMAXPROCS": "2", "GOFLAGS": "-p=2", "CGO_ENABLED": "1",
        "GOCACHE": str(cache / "build"), "GOMODCACHE": str(cache / "mod"),
        "CGO_CPPFLAGS": "-DKEEP=1", "CGO_LDFLAGS": "",
    }


def test_real_cgo_missing_header_modified_current_source_and_rollback(tmp_path, real_go):
    dependency, candidate = tmp_path / "external", tmp_path / "candidate"
    write(dependency / "include/relic.h", "#if KEEP != 1\n#error original CPPFLAGS lost\n#endif\nint external_value(void);\n")
    write(dependency / "library.c", "int external_value(void) { return 40; }\n")
    (dependency / "lib").mkdir()
    subprocess.run(["cc", "-c", str(dependency / "library.c"), "-o", str(dependency / "library.o")], check=True)
    subprocess.run(["ar", "rcs", str(dependency / "lib/librelic_s.a"), str(dependency / "library.o")], check=True)
    write(candidate / "go.mod", "module example.invalid/current\n\ngo 1.20\n")
    write(candidate / "current.go",
          'package current\n// #cgo LDFLAGS: -lrelic_s\n// #include "relic.h"\nimport "C"\n'
          'func Value() int { return int(C.external_value()) + extra() }\n')
    write(candidate / "extra.go", "package current\nfunc extra() int { return 2 }\n")
    write(candidate / "original_test.go",
          'package current\nimport "testing"\nfunc TestOriginalBehavior(t *testing.T) { if Value()!=42 { t.Fatal("current source behavior changed") } }\n')

    def run(root, env):
        return subprocess.run(["go", "test", "-v", "-count=1", "."], cwd=root,
                              env=env, capture_output=True, text=True, timeout=120)

    before = {p.name: p.read_bytes() for p in candidate.iterdir()}
    baseline = run(candidate, real_go)
    assert baseline.returncode != 0 and "relic.h" in baseline.stderr
    environment = go_relic.cgo_environment(real_go, str(dependency))
    modified = run(candidate, environment)
    assert modified.returncode == 0, modified.stdout + modified.stderr
    assert "PASS: TestOriginalBehavior" in modified.stdout
    rollback = tmp_path / "rollback-candidate"
    shutil.copytree(candidate, rollback)
    restored = run(rollback, real_go)
    assert restored.returncode != 0 and "relic.h" in restored.stderr
    assert {p.name: p.read_bytes() for p in candidate.iterdir()} == before
    (candidate / "extra.go").write_text("package current\nfunc extra() int { return 3 }\n")
    wrong_behavior = run(candidate, environment)
    assert wrong_behavior.returncode != 0 and "current source behavior changed" in wrong_behavior.stdout
    (candidate / "extra.go").write_text("package current\nfunc extra() int { return missingCurrentSymbol }\n")
    broken = run(candidate, environment)
    assert broken.returncode != 0 and "missingCurrentSymbol" in broken.stderr
