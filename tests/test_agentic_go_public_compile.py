"""Real compiler behavior and staging limits; no benchmark readiness claims."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from test_agentic_go_dependencies import context, proxy_object
from test_agentic_go_proxy_closure import contract, no_fetch
from agentic_runtime_migration import go_lane
from reviewbench_maintainability.agentic.contracts import ContractError
from reviewbench_maintainability.agentic.resources import ResourceGate, ResourceLimits, ResourceRequest

GiB = 1024**3
TID = "public-compile-fixture"


@pytest.fixture(scope="module")
def go_env(tmp_path_factory):
    go = shutil.which("go")
    if not go:
        pytest.skip("real public compile fixture requires Go")
    cache = tmp_path_factory.mktemp("public-go-build-cache")
    return go, {
        **os.environ, "GOTOOLCHAIN": "local", "GOWORK": "off", "GOPROXY": "off",
        "GOSUMDB": "off", "GOMAXPROCS": "2", "GOFLAGS": "-p=2",
        "GOCACHE": str(cache / "build"), "GOMODCACHE": str(cache / "mod"),
    }


def candidate(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "go.mod").write_text("module example.invalid/current\n\ngo 1.20\n")
    (repo / "value.go").write_text("package current\nfunc Value() int { return 42 }\n")
    (repo / "root_test.go").write_text(
        'package current_test\nimport ("fmt"; "os"; "testing")\n'
        'func TestMain(m *testing.M) { fmt.Println("UNRELATED_TESTMAIN_EXECUTED"); os.Exit(19) }\n'
    )
    (repo / "sessionoracle").mkdir()
    (repo / "sessionoracle/oracle_test.go").write_text(
        'package sessionoracle\nimport ("testing"; current "example.invalid/current")\n'
        'func TestFixedBehavior(t *testing.T) { if current.Value()!=42 { t.Fatal("behavior changed") } }\n'
    )
    return repo


def public_command(repo, go, enabled):
    ctx = SimpleNamespace(config={"migration": {"go_compile_only_public_ids": [TID] if enabled else []}})
    spec = SimpleNamespace(public_target="example.invalid/current", build_flags=[])
    command = go_lane.public_build_check(ctx, {"id": TID}, spec, go, str(repo))
    # Only map the engine scratch mount for this host-side compiler fixture.
    output = repo / "scratch"
    output.mkdir(exist_ok=True)
    return command.replace("/workspace/.reviewbench", str(output))


def run(command, repo, environment):
    return subprocess.run(["/bin/bash", "-c", command], cwd=repo, env=environment,
                          capture_output=True, text=True, timeout=120)


def test_real_empty_run_enters_testmain_but_compile_only_preserves_fixed_oracle(tmp_path, go_env):
    go, env = go_env
    repo = candidate(tmp_path)
    baseline = run(public_command(repo, go, False), repo, env)
    assert baseline.returncode != 0 and "UNRELATED_TESTMAIN_EXECUTED" in baseline.stdout
    modified = run(public_command(repo, go, True), repo, env)
    assert modified.returncode == 0, modified.stdout + modified.stderr
    assert "UNRELATED_TESTMAIN_EXECUTED" not in modified.stdout
    assert (repo / "scratch/go-public-compile.test").is_file()
    original_oracle = run(shlex.join([go, "test", "-v", "-count=1", "./sessionoracle"]), repo, env)
    assert original_oracle.returncode == 0
    assert "--- PASS: TestFixedBehavior" in original_oracle.stdout
    rollback = run(public_command(repo, go, False), repo, env)
    assert rollback.returncode != 0 and "UNRELATED_TESTMAIN_EXECUTED" in rollback.stdout


@pytest.mark.parametrize("where", ["production", "test-source"])
def test_compile_only_still_checks_whole_production_and_test_sources(tmp_path, go_env, where):
    go, env = go_env
    repo = candidate(tmp_path)
    name = "broken.go" if where == "production" else "broken_test.go"
    (repo / name).write_text("package current\nvar _ = missingIdentifier\n")
    result = run(public_command(repo, go, True), repo, env)
    assert result.returncode != 0
    assert "missingIdentifier" in result.stderr + result.stdout
    assert not (repo / "scratch/go-public-compile.test").exists()


def test_compilation_success_never_substitutes_for_original_behavior(tmp_path, go_env):
    go, env = go_env
    repo = candidate(tmp_path)
    (repo / "value.go").write_text("package current\nfunc Value() int { return 43 }\n")
    assert run(public_command(repo, go, True), repo, env).returncode == 0
    result = run(shlex.join([go, "test", "-count=1", "./sessionoracle"]), repo, env)
    assert result.returncode != 0 and "behavior changed" in result.stdout


def test_unselected_public_command_keeps_original_flags_and_targets():
    ctx = SimpleNamespace(config={})
    spec = SimpleNamespace(public_target="example.invalid/one,example.invalid/two", build_flags=["-tags", "feature"])
    result = go_lane.public_build_check(ctx, {"id": TID}, spec, "/go/bin/go", "/workspace/sub")
    assert result == "cd /workspace/sub && /go/bin/go test -run '^$' -count=1 -tags feature example.invalid/one example.invalid/two"


@pytest.mark.parametrize("selected", [None, "all", [TID, TID], [1]])
def test_public_compile_selection_requires_explicit_unique_ids(selected):
    ctx = SimpleNamespace(config={"migration": {"go_compile_only_public_ids": selected}})
    spec = SimpleNamespace(public_target="example.invalid/current", build_flags=[])
    with pytest.raises(ContractError, match="selection"):
        go_lane.public_build_check(ctx, {"id": TID}, spec, "go", "/workspace")


@pytest.mark.parametrize("target", ["./...", "example.invalid/one example.invalid/two"])
def test_compile_only_never_narrows_a_wildcard_or_multiple_packages(target):
    ctx = SimpleNamespace(config={"migration": {"go_compile_only_public_ids": [TID]}})
    spec = SimpleNamespace(public_target=target, build_flags=[])
    with pytest.raises(ContractError, match="one explicit package"):
        go_lane.public_build_check(ctx, {"id": TID}, spec, "go", "/workspace")


def test_migration_reserves_staging_and_publication_without_clamping():
    ctx = SimpleNamespace(config={})
    assert go_lane.proxy_staging_limit(ctx) == 16 * GiB
    assert go_lane.migration_scratch_bytes(ctx) == 17 * GiB
    ctx.config = {"migration": {"go_proxy_staging_bytes": 32 * GiB}}
    assert go_lane.migration_scratch_bytes(ctx) == 33 * GiB


@pytest.mark.parametrize("value", [True, 0, -1, 1.5, "1024", 2**63])
def test_invalid_staging_limits_fail_before_work(value):
    ctx = SimpleNamespace(config={"migration": {"go_proxy_staging_bytes": value}})
    with pytest.raises(ContractError, match="staging budget"):
        go_lane.migration_scratch_bytes(ctx)


def test_oversized_migration_budget_is_rejected_by_existing_shared_gate(tmp_path):
    ctx = SimpleNamespace(config={"migration": {"go_proxy_staging_bytes": 96 * GiB}})
    gate = ResourceGate(tmp_path / "gate", tmp_path / "scratch", ResourceLimits(),
                        probe=lambda: pytest.fail("oversized request reached host admission"))
    with pytest.raises(ContractError, match="single task exceeds"):
        with gate.acquire(ResourceRequest("Go preparation", scratch_bytes=go_lane.migration_scratch_bytes(ctx))):
            pytest.fail("oversized Go preparation admitted")


def test_existing_objects_are_bounded_before_copying_the_zip(tmp_path, monkeypatch):
    directory = tmp_path / "original"
    files = proxy_object(directory / "home/go/pkg/mod/cache/download")
    ctx, task, _ = context(tmp_path, {
        "go.mod": "module example.invalid/main\n\ngo 1.20\nrequire example.invalid/lib v1.2.3\n",
    })
    ctx.config = {"migration": {"go_proxy_staging_bytes": sum(p.stat().st_size for p in files) - 1}}
    no_fetch(monkeypatch)
    copied = []
    original_copy = go_lane.shutil.copyfile

    def capture(source, target):
        copied.append(Path(source).suffix)
        return original_copy(source, target)

    monkeypatch.setattr(go_lane.shutil, "copyfile", capture)
    with pytest.raises(ContractError, match="before copying"):
        go_lane.module_proxy(ctx, task, directory, "", contract=contract(download_all=True))
    assert copied == [".mod", ".info"]


def test_uncached_download_is_not_started_without_its_bounded_reservation(tmp_path, monkeypatch):
    ctx, task, _ = context(tmp_path, {
        "go.mod": "module example.invalid/main\n\ngo 1.20\nrequire example.invalid/lib v1.2.3\n",
    })
    ctx.config = {"migration": {"go_proxy_staging_bytes": 1024}}
    monkeypatch.setattr(go_lane, "fetch_proxy_file", lambda *args: pytest.fail("unreserved network transfer"))
    with pytest.raises(ContractError, match="before an uncached download"):
        go_lane.module_proxy(ctx, task, tmp_path / "original", "", contract=contract(download_all=True))


def test_new_downloads_and_staging_are_counted_without_polluting_archive_identity(tmp_path, monkeypatch):
    inputs = tmp_path / "download-source"
    files = proxy_object(inputs)
    ctx, task, _ = context(tmp_path, {
        "go.mod": "module example.invalid/main\n\ngo 1.20\nrequire example.invalid/lib v1.2.3\n",
    })

    def fetch(context, name):
        target = context.output / "go-downloads" / name
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(inputs / name, target)
        return target

    bundles = []

    def bundle(source):
        destination = tmp_path / ("bundle-" + str(len(bundles)))
        shutil.copytree(source, destination)
        bundles.append(destination)
        return {"path": str(destination), "sha256": "b" * 64}

    ctx.bundle = bundle
    monkeypatch.setattr(go_lane, "fetch_proxy_file", fetch)
    go_lane.module_proxy(ctx, task, tmp_path / "original", "", contract=contract(download_all=True))
    receipt_path = next((ctx.output / "go-proxy-preparation").glob("*.json"))
    first = json.loads(receipt_path.read_text())
    expected_bytes = sum(path.stat().st_size for path in files)
    assert first["staged_bytes"] == first["new_download_bytes"] == expected_bytes
    assert first["copy_and_download_bytes"] == expected_bytes * 2
    go_lane.module_proxy(ctx, task, tmp_path / "original", "", contract=contract(download_all=True))
    second = json.loads(receipt_path.read_text())
    assert second["staged_bytes"] == expected_bytes and second["new_download_bytes"] == 0
    assert (bundles[0] / "go-proxy-inputs.json").read_bytes() == (bundles[1] / "go-proxy-inputs.json").read_bytes()
    assert not (bundles[0] / "go-proxy-preparation").exists()
