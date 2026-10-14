"""Explicit Vector host opt-in and preservation of the original root test."""
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_lane, rust_vector as host
from reviewbench_maintainability.agentic.contracts import ContractError


def test_opt_in_reuses_archives_instead_of_requesting_a_fresh_vendor_workspace():
    ctx = SimpleNamespace(config={})
    assert not host.requested(ctx, {"id": host.ID})
    assert rust_lane.migration_scratch_bytes(ctx, {"id": host.ID}) == 12 * 1024**3
    ctx.config = {"migration": {host.OPTION: {host.ID: {}}}}
    assert host.requested(ctx, {"id": host.ID})
    assert rust_lane.migration_scratch_bytes(ctx, {"id": host.ID}) == 2 * 1024**3
    assert rust_lane.migration_scratch_bytes(ctx, {"id": "other"}) == 12 * 1024**3
    ctx.config["migration"][host.OPTION]["foreign"] = {}
    with pytest.raises(ContractError, match="unsupported"):
        host.requested(ctx, {"id": host.ID})


@pytest.mark.parametrize("field", ["id", "repo", "start_sha"])
def test_changed_identity_stops_before_reading_prepared_inputs(field):
    task = {"id": host.ID, "repo": host.PROFILE["repo"], "start_sha": host.PROFILE["start"]}
    oracle = dict(task)
    task[field] = "changed"
    with pytest.raises(ContractError, match="task/start"):
        host.original_inputs(SimpleNamespace(), task, oracle, {}, {})


def test_unbound_original_plan_stops_before_source_or_dependency_work():
    task = {"id": host.ID, "repo": host.PROFILE["repo"], "start_sha": host.PROFILE["start"]}
    with pytest.raises(ContractError, match="original plan"):
        host.original_inputs(SimpleNamespace(), task, dict(task), {"input_index": 111}, {})


def test_original_private_argv_has_no_invented_package_or_removed_filter():
    assert host.COMMAND == [
        "test", "--locked", "--no-default-features", "--features", "sinks-datadog_logs",
        "--test", "oracle_datadog", "generated_datadog_logs_configuration_deserializes",
        "--", "--nocapture",
    ]
    assert host.TOOLCHAIN == "1.72.1" and host.HARNESS == "tests/oracle_datadog.rs"
    assert host.PUBLIC == [
        "build", "--locked", "--no-default-features", "--features", "sinks-datadog_logs",
        "--lib", "--message-format=json",
    ]
    assert "-p" not in host.COMMAND and "test" not in host.PUBLIC


def test_host_route_delegates_to_existing_shared_root_variant_wrapper(monkeypatch):
    ctx = SimpleNamespace(config={"migration": {host.OPTION: {host.ID: {}}}})
    task = {"id": host.ID}
    seen = []
    monkeypatch.setattr(host, "migrate", lambda *args: seen.append(args) or "original-root-recipe")
    assert rust_lane.migrate(ctx, task, {"id": host.ID}) == "original-root-recipe"
    assert len(seen) == 1
    assert host.TOOL.endswith("/manifest_variants.py")
