"""TiKV reuse is opt-in and preserves the original command and full graph."""
import copy
import json
from pathlib import Path
import shlex
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_lane, rust_tikv as host
from agentic_runtime_migration.rust_local_registry import graph_signature
from reviewbench_maintainability.agentic.contracts import ContractError, file_hash


def plan():
    return {"command": host.ORIGINAL_COMMAND, "toolchain": host.TOOLCHAIN,
            "features": [], "env": dict(host.ENV)}


def test_original_test_command_filter_thread_count_and_compiler_are_exact():
    public, private = host.commands(plan())
    assert private[1:] == shlex.split(host.ORIGINAL_COMMAND)[2:]
    assert private[1:5] == ["test", "--locked", "-p", "engine_rocks"]
    assert private[-3:] == ["--", "--nocapture", "--test-threads=1"]
    assert private.count(host.TEST) == 1
    assert host.TOOLCHAIN == "nightly-2022-05-01"
    assert public == [private[0], "build", "--locked", "-p", "engine_rocks", "--lib"]
    assert host.TEST not in public and "--test" not in public


@pytest.mark.parametrize("changed", ["compiler", "unlocked", "test-filter", "threads", "env", "features"])
def test_changed_original_execution_inputs_are_rejected(changed):
    value = plan()
    if changed == "compiler":
        value["toolchain"] = "stable"
    elif changed == "unlocked":
        value["command"] = value["command"].replace(" --locked", "")
    elif changed == "test-filter":
        value["command"] = value["command"].replace(host.TEST, "easier")
    elif changed == "threads":
        value["command"] = value["command"].replace("--test-threads=1", "--test-threads=8")
    elif changed == "env":
        value["env"] = {}
    else:
        value["features"] = ["replacement"]
    with pytest.raises(ContractError, match="command, compiler, flags or features"):
        host.commands(value)


def test_only_explicit_original_task_uses_two_gib_prepared_input_reservation():
    context = SimpleNamespace(config={})
    assert not host.requested(context, {"id": host.TASK})
    assert rust_lane.migration_scratch_bytes(context, {"id": host.TASK}) == 12 * 1024**3
    context.config = {"migration": {host.OPTION: {host.TASK: {}}}}
    assert host.requested(context, {"id": host.TASK})
    assert not host.requested(context, {"id": "different"})
    assert rust_lane.migration_scratch_bytes(context, {"id": host.TASK}) == 2 * 1024**3


@pytest.mark.parametrize("choices", [[], {"different": {}}, None])
def test_unbound_profiles_do_not_fall_through_to_this_adapter(choices):
    context = SimpleNamespace(config={"migration": {host.OPTION: choices}})
    with pytest.raises(ContractError):
        host.requested(context, {"id": host.TASK})


@pytest.mark.parametrize("changed", ["id", "repo", "start_sha"])
def test_wrong_original_identity_stops_before_prepared_file_access(changed):
    task = {"id": host.TASK, "repo": host.REPO, "start_sha": host.START}
    oracle = dict(task)
    task[changed] = "changed"
    with pytest.raises(ContractError, match="task identity"):
        host.original_inputs(SimpleNamespace(), task, oracle, {}, {})


def test_existing_dispatch_calls_new_adapter_once(monkeypatch):
    context = SimpleNamespace(config={"migration": {host.OPTION: {host.TASK: {}}}})
    calls = []
    monkeypatch.setattr(host, "migrate", lambda *args: calls.append(args) or "prepared")
    assert rust_lane.migrate(context, {"id": host.TASK}, {}) == "prepared"
    assert len(calls) == 1


def graphs_fixture(tmp_path):
    # Synthetic graph fixture, not an additional benchmark observation.
    packages = [{"id": "p" + str(i), "name": "p" + str(i), "version": "1.0.0", "source": None}
                for i in range(596)]
    graph = {"packages": packages, "resolve": {"root": "p0", "nodes": [
        {"id": p["id"], "dependencies": [], "deps": [], "features": []} for p in packages]}}
    def emit(name, value):
        path = tmp_path / name
        path.write_text(json.dumps(value))
        return {"path": str(path), "sha256": file_hash(path)}
    native, current, errors = emit("native.json", graph), emit("current.json", graph), emit("stderr", "")
    observations = {}
    for variant in ("start", "golden"):
        for role in ("public", "private"):
            observations[variant + "-" + role] = {
                "exit_status": 0, "source_and_lock_unchanged": True, "empty_cargo_home": True,
                "original_git_cache_mounted": False, "packages": 596, "nodes": 596,
                "workspace_members": 74, "complete_original_lock_match": True,
                "full_edges_features_identical": True, "stdout": current, "stderr": errors,
                "original_graph": native, "graph_signature": graph_signature(graph)}
    document = {"status": "all_original_offline_graphs_verified", "complete": True,
                "full_scope": 402, "successful_graphs": 4, "matching_full_graphs": 4,
                "observations": emit("observations.json", observations)}
    expected = {(p["name"], p["version"], None) for p in packages}
    return document, observations, expected, graph, emit


def test_all_four_full_graphs_are_rechecked_from_hashed_native_metadata(tmp_path):
    document, _, expected, _, _ = graphs_fixture(tmp_path)
    host.verified_graphs(document, expected)


@pytest.mark.parametrize("change", ["missing-view", "source", "cache", "nodes", "features", "package", "hash"])
def test_counts_or_success_labels_do_not_replace_dependency_graph_evidence(tmp_path, change):
    document, observations, expected, graph, emit = graphs_fixture(tmp_path)
    row = observations["start-private"]
    if change == "missing-view":
        del observations["golden-public"]
    elif change == "source":
        row["source_and_lock_unchanged"] = False
    elif change == "cache":
        row["original_git_cache_mounted"] = True
    elif change == "nodes":
        row["nodes"] = 595
    elif change in {"features", "package"}:
        altered = copy.deepcopy(graph)
        if change == "features":
            altered["resolve"]["nodes"][0]["features"] = ["not-original"]
        else:
            altered["packages"][0]["version"] = "2.0.0"
        row["stdout"] = emit("changed.json", altered)
        row["graph_signature"] = graph_signature(altered)
    else:
        Path(row["stdout"]["path"]).write_text("{}")
    document["observations"] = emit("observations.json", observations)
    with pytest.raises(ContractError):
        host.verified_graphs(document, expected)
