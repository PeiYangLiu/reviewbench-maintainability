"""Original Materialize host opt-in and command-derived feature preservation."""
from pathlib import Path
import shlex
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_materialize as host, rust_lane
from reviewbench_maintainability.agentic.contracts import ContractError


def test_profiles_keep_original_target_filter_compiler_jobs_and_command_features():
    assert sorted(p["index"] for p in host.PROFILES.values()) == [8, 10, 11]
    for profile in host.PROFILES.values():
        command = shlex.split(profile["original_command"])
        assert command[:3] == ["cargo", "+" + profile["toolchain"], "test"]
        assert command[command.index("--test") + 1] == "oracle"
        assert profile["tests"][0] in command and command[-2:] == ["--", "--nocapture"]
        assert "--locked" not in command and profile["timeout_seconds"] == 3600
        assert profile["harness"].endswith("/tests/oracle.rs")
        assert profile["harness_sha256"]["start"] != profile["harness_sha256"]["golden"]
        assert profile["cpus"] == (4 if profile["index"] == 8 else 8)
        if profile["index"] == 8:
            assert profile["feature_args"] == ["--features", "mz-storage/server"]
            assert profile["declared_plan_features"] == []  # The actual argv is authoritative.
        else:
            assert profile["feature_args"] == []


def test_explicit_opt_in_uses_existing_archives_without_fresh_vendor_workspace():
    ident = next(iter(host.PROFILES))
    ctx = SimpleNamespace(config={})
    assert not host.requested(ctx, {"id": ident})
    assert rust_lane.migration_scratch_bytes(ctx, {"id": ident}) == 12 * 1024**3
    ctx.config = {"migration": {host.OPTION: {ident: {}}}}
    assert host.requested(ctx, {"id": ident})
    assert rust_lane.migration_scratch_bytes(ctx, {"id": ident}) == 2 * 1024**3
    ctx.config["migration"][host.OPTION]["foreign"] = {}
    with pytest.raises(ContractError):
        host.requested(ctx, {"id": ident})


@pytest.mark.parametrize("change", ["repo", "start_sha"])
def test_wrong_source_identity_stops_before_prepared_files_are_read(change):
    ident, profile = next(iter(host.PROFILES.items()))
    task = {"id": ident, "repo": profile["repo"], "start_sha": profile["start"]}
    oracle = dict(task)
    task[change] = "changed"
    with pytest.raises(ContractError, match="source identity"):
        host.original_inputs(SimpleNamespace(), task, oracle, {}, {})


def test_host_route_calls_shared_original_adapter_once(monkeypatch):
    ident = next(iter(host.PROFILES))
    ctx = SimpleNamespace(config={"migration": {host.OPTION: {ident: {}}}})
    calls = []
    monkeypatch.setattr(host, "migrate", lambda *args: calls.append(args) or "bound")
    assert rust_lane.migrate(ctx, {"id": ident}, {}) == "bound"
    assert len(calls) == 1 and host.TOOL.endswith("overlay_api.py")
