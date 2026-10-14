"""Reusable public caches retain graph namespaces and reject role conflicts early."""
import copy
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import prepare_agentic_build_cache as cli
from agentic_runtime_migration import public_build_cache as cache, rust_libp2p as host
from agentic_runtime_migration import cache_seed_entry
from reviewbench_maintainability.agentic.contracts import ContractError
from test_agentic_public_build_cache import recipe
from test_agentic_rust_manifest_variants import fixture, runner, entry


def session_fixture(monkeypatch):
    session = {host.HARNESS: b"original private assertion",
               host.SESSION_LOCK_PATHS[0]: b"original dependency lock 0",
               host.SESSION_LOCK_PATHS[1]: b"original dependency lock 1"}
    digest = lambda data: hashlib.sha256(data).hexdigest()
    monkeypatch.setitem(host.PROFILE, "harness", digest(session[host.HARNESS]))
    monkeypatch.setitem(host.PROFILE, "locks", [digest(session[n]) for n in host.SESSION_LOCK_PATHS])
    forms = [{"lock": {"path": "/opt/reviewbench-assets/lock-" + str(i), "sha256": value}}
             for i, value in enumerate(host.PROFILE["locks"])]
    calls = []

    def private(name):
        calls.append(name)
        return {"path": "/opt/reviewbench-assets/private-original-test.rs",
                "sha256": host.PROFILE["harness"]}

    return session, forms, calls, private


def test_declared_public_locks_are_reused_but_session_installation_remains_grader_only(monkeypatch):
    session, forms, calls, private = session_fixture(monkeypatch)
    result = host.session_sources(session, forms, private)
    assert calls == [host.HARNESS]
    assert set(result) == set(session)
    for name, form in zip(host.SESSION_LOCK_PATHS, forms):
        assert result[name] == form["lock"]
        assert result[name] is not form["lock"]
    assert result[host.HARNESS]["path"].endswith("/private-original-test.rs")


@pytest.mark.parametrize("fault", ["harness", "lock", "missing", "extra", "public-lock", "form-order"])
def test_reusing_public_bytes_does_not_license_changed_session_inputs(monkeypatch, fault):
    session, forms, calls, private = session_fixture(monkeypatch)
    if fault == "harness":
        session[host.HARNESS] += b" changed"
    elif fault == "lock":
        session[host.SESSION_LOCK_PATHS[0]] += b" changed"
    elif fault == "missing":
        session.pop(host.SESSION_LOCK_PATHS[0])
    elif fault == "extra":
        session["unrelated"] = b"extra"
    elif fault == "public-lock":
        forms[0]["lock"]["sha256"] = "0" * 64
    else:
        forms.reverse()
    with pytest.raises(ContractError):
        host.session_sources(session, forms, private)
    assert calls == []


def test_private_conflict_is_rejected_before_asset_extraction_or_docker(tmp_path, monkeypatch):
    value = recipe()
    value.assets.append({**value.assets[1], "visibility": "developer",
                         "target": "/opt/reviewbench-assets/accidental-private-copy"})
    calls = []
    import reviewbench_maintainability.agentic.environment as environment
    monkeypatch.setattr(environment, "validate_assets", lambda *_: calls.append("validate"))
    monkeypatch.setattr(environment, "docker", lambda *_: calls.append("docker"))
    pool = SimpleNamespace(asset_root=tmp_path, materialize=lambda *_: calls.append("extract"))
    with pytest.raises(ContractError, match="private artifact"):
        cache.bake_public_inputs(value, pool, tmp_path / "output")
    assert calls == [] and not (tmp_path / "output").exists()


@pytest.mark.parametrize("target", ["/workspace/dependency", "/opt/reviewbench-assets/../private"])
def test_invalid_image_target_is_rejected_without_copy(target):
    value = recipe()
    value.assets[0]["target"] = target
    with pytest.raises(ContractError):
        cache.validate_public_image_inputs(value)


def test_private_and_reference_guards_remain_active():
    value = recipe()
    cache.validate_public_image_inputs(value)
    value.grading["golden_patch"] = {"path": "/golden.patch", "sha256": value.assets[0]["sha256"]}
    with pytest.raises(ContractError, match="private artifact"):
        cache.validate_public_image_inputs(value)


def public_evidence(tmp_path, compatible=("old",)):
    source, public, _ = fixture(tmp_path)
    return entry.run_candidate(public, source, tmp_path / "cache", run=runner([], compatible=compatible))


@pytest.mark.parametrize("compatible", [("old",), ("new",), ("old", "new")])
def test_public_metadata_binds_both_checked_forms_without_choosing_a_private_result(tmp_path, compatible):
    evidence = public_evidence(tmp_path, compatible)
    (tmp_path / "stdout").write_text(entry.EVIDENCE_PREFIX + json.dumps(evidence) + "\n")
    metadata = cli.metadata_from_records(tmp_path, [{"stdout_file": "stdout"}])
    assert metadata["schema_version"] == cache.GRAPH_SCHEMA
    assert len(cache.metadata_groups(metadata)) == 2
    assert sorted(graph["packages"][-1]["version"] for _, graph in cache.metadata_groups(metadata)) == ["0.6", "0.7"]


@pytest.mark.parametrize("fault", [
    "private-results", "source", "status", "tests-select", "resolve", "namespace", "duplicate",
    "compatibility", "probe", "cwd", "limit", "compiler-exit",
])
def test_incomplete_or_ambiguous_variant_evidence_is_not_a_cache_license(tmp_path, fault):
    evidence = public_evidence(tmp_path)
    if fault == "private-results":
        evidence["test_results"] = {"original": True}
    elif fault == "source":
        evidence["source_after"] = {}
    elif fault == "status":
        evidence["status"] = "rejected"
    elif fault == "tests-select":
        evidence["selection_uses_test_results"] = True
    elif fault == "resolve":
        evidence["new_lock_generation_commands"] = 1
    elif fault == "namespace":
        evidence["public_compiles"][0]["target_cache"] = "../other"
    elif fault == "duplicate":
        evidence["public_compiles"].append(copy.deepcopy(evidence["public_compiles"][0]))
    elif fault == "compatibility":
        evidence["compatible_forms"] = ["new"]
    elif fault == "probe":
        evidence["probes"].append(copy.deepcopy(evidence["probes"][0]))
    elif fault == "cwd":
        evidence["public_compiles"][0]["command_result"]["cwd"] = "/other"
    elif fault == "limit":
        evidence["public_compiles"][0]["command_result"]["limit"] = "deadline"
    else:
        evidence["public_compiles"][0]["command_result"]["returncode"] = 1
    with pytest.raises(ContractError):
        cli.variant_metadata(evidence)


def test_cache_capture_uses_each_namespace_graph_not_the_union_of_package_names(tmp_path):
    target = tmp_path / "target"
    namespaces = ["form-" + "a" * 20, "form-" + "b" * 20]
    metadata = {"schema_version": cache.GRAPH_SCHEMA, "cache_groups": {}}
    for i, namespace in enumerate(namespaces):
        graph = {"packages": [{"name": "only-" + str(i), "source": "registry+fixed"},
                               {"name": "workspace", "source": None}], "workspace_members": ["workspace"]}
        metadata["cache_groups"][namespace] = graph
        folder = target / namespace / "debug/deps"
        folder.mkdir(parents=True)
        for name in ("only_0", "only_1", "workspace"):
            (folder / ("lib" + name + "-0123456789abcdef.rlib")).write_text(namespace + name)
        (folder.parent / "original_private_test").write_text("never cache")
    unknown = target / ("form-" + "c" * 20) / "debug/deps"
    unknown.mkdir(parents=True)
    (unknown / "libonly_0-0123456789abcdef.rlib").write_text("unobserved graph")
    manifest = cache.capture(target, metadata, tmp_path / "seed", "a" * 64, producer_receipt={
        "producer": "clean-start-public-check", "grader_assets_visible": False, "public_check_exit_status": 0})
    assert [r["path"] for r in manifest["files"]] == [
        namespace + "/debug/deps/libonly_" + str(i) + "-0123456789abcdef.rlib"
        for i, namespace in enumerate(namespaces)]
    cache_seed_entry.restore(tmp_path / "seed", tmp_path / "restored", "a" * 64)
    assert manifest["excluded_workspace_packages"] == ["workspace"]
    for row in manifest["files"]:
        assert (tmp_path / "restored" / row["path"]).read_bytes() == (target / row["path"]).read_bytes()


def test_cache_namespace_symlinks_are_rejected(tmp_path):
    namespace = "form-" + "a" * 20
    target = tmp_path / "target"
    target.mkdir()
    (tmp_path / "other").mkdir()
    (target / namespace).symlink_to(tmp_path / "other")
    graph = {"packages": [{"name": "dependency", "source": "registry+fixed"}, {"name": "workspace", "source": None}]}
    with pytest.raises(ContractError, match="redirected"):
        cache.external_artifacts(target, {"schema_version": cache.GRAPH_SCHEMA, "cache_groups": {namespace: graph}})


def test_legacy_single_graph_evidence_still_works(tmp_path):
    graph = {"packages": [{"name": "original"}], "workspace_members": ["original"]}
    evidence = {"role": "public", "source_unchanged": True, "exit_status": 0, "selected_lock_sha256": "a" * 64,
                "probes": [{"lock_sha256": "a" * 64,
                            "command_result": {"returncode": 0, "stdout": json.dumps(graph)}}]}
    (tmp_path / "stdout").write_text("ReviewBench retained lock: " + json.dumps(evidence) + "\n")
    assert cli.metadata_from_records(tmp_path, [{"stdout_file": "stdout"}]) == graph
