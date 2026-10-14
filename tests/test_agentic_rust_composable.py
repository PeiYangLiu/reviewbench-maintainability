"""Original unlocked Git workspaces reuse the selector, not a task-specific loop."""
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_image_entry as entry, rust_composable as host, rust_lane
from reviewbench_maintainability.agentic.contracts import ContractError
from test_agentic_rust_cargo_contract import nested_fixture, runner, MANIFEST, MEMBER
from test_agentic_rust_image import output, ref


def fixture(tmp_path, version="0.1.0"):
    source, public, private = nested_fixture(tmp_path, version)
    public.update(schema_version=entry.CATALOG_WORKSPACE_SCHEMA, cpus=8,
                  source_config_mode="isolated-cargo-home", git_dependencies="full-locked-source-identities",
                  public_command=["build", "--locked", "-p", "image"],
                  metadata_timeout_seconds=180, max_log_bytes=32 * 1024**2)
    private["schema_version"] = entry.CATALOG_WORKSPACE_PRIVATE_SCHEMA
    for row in public["locks"]:
        p = Path(row["path"])
        p.write_text(p.read_text().replace(entry.CRATES_IO, "git+https://example.invalid/dep#" + "a" * 40))
        row.update(ref(p))
    public["reference_seed_sha256"] = public["locks"][0]["sha256"]
    (source / "Cargo.lock").write_bytes(Path(public["locks"][0]["path"]).read_bytes())
    return source, public, private


def diagnostic(cwd):
    return ("warning: Patch `unused v1.0.0` was not used in the crate graph.\n"
            "error: the lock file " + str(cwd / "Cargo.lock") +
            " needs to be updated but --locked was passed to prevent this\n"
            "If you want to try to generate the lock file without accessing the network, "
            "remove the --locked flag and use --offline instead.\n")


def original_runner(calls, **options):
    delegate = runner(calls, **options)

    def invoke(command, cwd, env, folder, name, deadline, limit):
        assert env["CARGO_BUILD_JOBS"] == "8" and env["CARGO_NET_OFFLINE"] == "true"
        assert (Path(env["CARGO_HOME"]) / "config.toml").is_file()
        assert "--config" not in command
        row = delegate(command, cwd, env, folder, name, deadline, limit)
        if "metadata" in command and row["returncode"] == 101:
            return output(command, cwd, code=101, stderr=diagnostic(cwd))
        return row
    return invoke


@pytest.mark.parametrize("version", ["0.1.0", "0.2.0"])
@pytest.mark.parametrize("grading", [False, True])
def test_full_current_workspace_selects_original_lock_and_executes_once(tmp_path, version, grading):
    source, public, private = fixture(tmp_path, version)
    before, calls = entry.snapshot(source), []
    result = entry.run_candidate(public, source, tmp_path / "build",
                                 private if grading else None, run=original_runner(calls))
    assert result["exit_status"] == 0 and result["command_executions"] == 1
    assert result["source_before"] == result["source_after"] == before
    assert result["graph"]["all_manifest_files"] == 4
    assert result["selected_lock_sha256"] == public["locks"][version == "0.2.0"]["sha256"]
    assert len(calls) == 3
    assert all("metadata" in c and "--all-features" in c and "--no-deps" not in c for c in calls[:-1])
    if grading:
        assert calls[-1] == [public["cargo"]["path"], *private["command"]]
        assert "--locked" not in calls[-1] and "--offline" not in calls[-1]
    else:
        assert calls[-1] == [public["cargo"]["path"], "--offline", *public["public_command"]]
        assert "build" in calls[-1] and "test" not in calls[-1]


def test_negative_original_assertion_does_not_try_another_graph(tmp_path):
    source, public, private = fixture(tmp_path, "0.2.0")
    calls = []
    result = entry.run_candidate(public, source, tmp_path / "build", private,
                                 run=original_runner(calls, passed=False))
    assert result["exit_status"] == 101 and result["test_results"] == {"original_behavior": False}
    assert len(calls) == 3 and sum("test" in c for c in calls) == 1


@pytest.mark.parametrize("owned", ["compatible", "incompatible", "missing"])
def test_candidate_owned_lock_is_not_overwritten(tmp_path, owned):
    source, public, private = fixture(tmp_path, "0.2.0")
    lock = source / "Cargo.lock"
    if owned == "missing":
        lock.unlink()
    elif owned == "compatible":
        lock.write_bytes(Path(public["locks"][1]["path"]).read_bytes() + b"\n# candidate\n")
    else:
        lock.write_bytes(lock.read_bytes() + b"\n# candidate\n")
    before, calls = entry.snapshot(source), []
    if owned == "compatible":
        result = entry.run_candidate(public, source, tmp_path / "build", private, run=original_runner(calls))
        assert result["candidate_lock_preserved"] and len(calls) == 2
    else:
        with pytest.raises(entry.VariantError):
            entry.run_candidate(public, source, tmp_path / "build", private, run=original_runner(calls))
        assert len(calls) == (0 if owned == "missing" else 1)
    assert entry.snapshot(source) == before


@pytest.mark.parametrize("problem", ["network", "missing-package", "disk", "extra-error"])
def test_catalog_transport_errors_are_not_lock_selection_fallbacks(tmp_path, problem):
    source, public, private = fixture(tmp_path)
    calls = []
    messages = {"network": "error: failed to download; offline\n",
                "missing-package": "error: no matching package named absent\n",
                "disk": "No space left on device\n"}

    def fail(command, cwd, env, folder, name, deadline, limit):
        calls.append(command)
        text = (diagnostic(cwd) + "error: unrelated failure\n"
                if problem == "extra-error" else messages[problem])
        return output(command, cwd, code=101, stderr=text)

    with pytest.raises(entry.VariantError, match="outside dependency"):
        entry.run_candidate(public, source, tmp_path / "build", private, run=fail)
    assert len(calls) == 1 and "test" not in calls[0]


@pytest.mark.parametrize("change", ["target", "owner", "harness-disabled", "harness-bytes"])
def test_original_test_identity_remains_bound(tmp_path, change):
    source, public, private = fixture(tmp_path)
    calls, options = [], {}
    if change == "target":
        options["target_override"] = MEMBER + "/tests/other.rs"
    elif change == "owner":
        options["owner_override"] = "other/Cargo.toml"
    elif change == "harness-disabled":
        with (source / MANIFEST).open("a") as stream:
            stream.write('\n[[test]]\nname="oracle_pnm"\nharness=false\n')
    else:
        (source / private["harness"]["path"]).write_text("changed")
    with pytest.raises(entry.VariantError):
        entry.run_candidate(public, source, tmp_path / "build", private, run=original_runner(calls, **options))
    assert all("metadata" in c for c in calls)


@pytest.mark.parametrize("change", ["old-schema", "locked-test", "cpu9", "cpu-bool", "upgrade-cargo", "unbound-git"])
def test_catalog_does_not_relax_old_schema_or_original_contract(tmp_path, change):
    _, public, private = fixture(tmp_path)
    if change == "old-schema":
        public["schema_version"] = entry.WORKSPACE_REFERENCE_SCHEMA
    elif change == "locked-test":
        private["command"].insert(1, "--locked")
    elif change == "cpu9":
        public["cpus"] = 9
    elif change == "cpu-bool":
        public["cpus"] = True
    elif change == "upgrade-cargo":
        public["source_config_mode"] = "new-reader"
    else:
        public["git_dependencies"] = "anything"
    with pytest.raises(entry.VariantError):
        entry.validate(public, private)


def test_missing_original_test_is_failure_even_if_process_exits_zero(tmp_path):
    source, public, private = fixture(tmp_path)
    calls = []
    delegate = original_runner(calls)

    def missing(command, cwd, env, folder, name, deadline, limit):
        result = delegate(command, cwd, env, folder, name, deadline, limit)
        return output(command, cwd, stdout="test result: ok.\n") if "test" in command else result

    result = entry.run_candidate(public, source, tmp_path / "build", private, run=missing)
    assert result["exit_status"] == 1 and result["test_results"] == {"original_behavior": False}
    assert sum("test" in c for c in calls) == 1


def test_successful_public_build_exposes_one_full_graph_to_dependency_prewarmer(tmp_path):
    from prepare_agentic_build_cache import metadata_from_records
    source, public, _ = fixture(tmp_path)
    result = entry.run_candidate(public, source, tmp_path / "build", run=original_runner([]))
    log = tmp_path / "public.stdout"
    log.write_text(entry.EVIDENCE_PREFIX + json.dumps(result) + "\n")
    graph = metadata_from_records(tmp_path, [{"stdout_file": log.name}])
    assert len(graph["packages"]) == len(graph["resolve"]["nodes"]) == 2
    assert result["test_results"] == {} and result["command_executions"] == 1


def test_original_profile_is_opt_in_and_input_scratch_does_not_rebuild_dependencies():
    ctx = SimpleNamespace(config={})
    assert not host.requested(ctx, {"id": host.ID})
    assert rust_lane.migration_scratch_bytes(ctx, {"id": host.ID}) == 12 * 1024**3
    ctx.config = {"migration": {host.OPTION: {host.ID: {}}}}
    assert host.requested(ctx, {"id": host.ID})
    assert rust_lane.migration_scratch_bytes(ctx, {"id": host.ID}) == 2 * 1024**3
    assert rust_lane.migration_scratch_bytes(ctx, {"id": "other"}) == 12 * 1024**3
    ctx.config["migration"][host.OPTION]["foreign"] = {}
    with pytest.raises(ContractError):
        host.requested(ctx, {"id": host.ID})


@pytest.mark.parametrize("field", ["repo", "start_sha"])
def test_wrong_identity_stops_before_reading_any_prepared_input(field):
    task = {"id": host.ID, "repo": host.PROFILE["repo"], "start_sha": host.PROFILE["start"]}
    oracle = dict(task)
    task[field] = "changed"
    with pytest.raises(ContractError, match="identity"):
        host.original_inputs(SimpleNamespace(), task, oracle, {}, {})
