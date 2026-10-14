"""Preserve the original Scylla preparation, rather than substitute a resolver."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_first_resolution as first
from agentic_runtime_migration import rust_scylla_resolution as scylla
from reviewbench_maintainability.agentic.contracts import ContractError


def reference(path):
    data = path.read_bytes()
    return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def test_scylla_profile_keeps_the_exact_original_prepare_and_one_default_policy():
    old = deepcopy(first.DEFAULTS)
    profile = first.profile_for(first.SCYLLA_TASK)
    assert profile["prepare_command"] == scylla.PREPARE
    assert profile["toolchain"] == "1.64.0" and profile["shared_default"] is True
    assert profile["virtual_workspace"] is True and profile["local_packages"] == 4
    assert profile["command_timeout_seconds"] == 2400
    assert profile["command"] == ["test", "-p", "scylla", "--test", "oracle_session", "--", "--nocapture"]
    assert profile["defaults"]["start"]["manifests"] == profile["defaults"]["golden"]["manifests"]
    assert first.DEFAULTS == old
    assert first.profile_for(first.TASK)["profile"] == "prometheus82-first-new-locks-v1"
    assert first.profile_for(first.FONT_TASK)["profile"] == "fontations45-first-new-locks-v1"


@pytest.mark.parametrize("rows", [
    [],
    [{"name": "twox-hash", "version": "1.6.3"}],
    [{"name": "twox-hash", "version": "1.6.2"}, {"name": "thiserror", "version": "1.0.35"}],
    [{"name": "twox-hash", "version": "1.6.3"}, {"name": "thiserror", "version": "1.0.36"}],
    [{"name": "twox-hash", "version": "1.6.3"}, {"name": "twox-hash", "version": "1.6.2"},
     {"name": "thiserror", "version": "1.0.35"}],
])
def test_final_lock_requires_both_exact_original_pins(rows):
    with pytest.raises(ContractError):
        scylla.validate_pins({"package": rows})


def test_exact_final_pins_pass_without_rewriting_lock():
    lock = {"package": [{"name": name, "version": version} for name, version in scylla.PINS.items()]}
    before = deepcopy(lock)
    scylla.validate_pins(lock)
    assert lock == before


def valid_prepare(tmp_path):
    stdout, stderr = tmp_path / "stdout", tmp_path / "stderr"
    stdout.write_bytes(b"")
    stderr.write_text("original minimum-version resolution and precise updates\n")
    raw = {
        "argv": ["/bin/bash", "--noprofile", "--norc", "-c", scylla.PREPARE],
        "exit_status": 0, "natural_exit": True,
        "stdout_sha256": reference(stdout)["sha256"], "stderr_sha256": reference(stderr)["sha256"],
        "completed_at": "2026-09-11T05:00:02-07:00",
    }
    path = tmp_path / "raw.json"
    path.write_text(json.dumps(raw))
    record = {
        "command": scylla.PREPARE, "exit_status": 0, "natural_exit": True, "timed_out": False,
        "source_before_lock_sha256": None, "final_lock_sha256": "a" * 64,
        "completed_steps": [
            ["cargo", "+nightly-2022-12-20", "-Z", "minimal-versions", "generate-lockfile"],
            ["cargo", "+1.64.0", "update", "-p", "twox-hash", "--precise", "1.6.3"],
            ["cargo", "+1.64.0", "update", "-p", "thiserror", "--precise", "1.0.35"],
        ],
        "started_at": "2026-09-11T05:00:01-07:00", "completed_at": raw["completed_at"],
        "raw_record": reference(path), "raw_argv": raw["argv"],
        "stdout": reference(stdout), "stderr": reference(stderr),
    }
    return record, {"recorded_at": "2026-09-11T05:00:00-07:00"}


def test_complete_original_command_is_bound_to_actual_raw_record(tmp_path):
    record, decision = valid_prepare(tmp_path)
    assert scylla.prepare_record(record, decision, "a" * 64)["exit_status"] == 0


@pytest.mark.parametrize("executable", ["cargo", "/home/v-peiyangliu/.cargo/bin/cargo"])
@pytest.mark.parametrize("requested_present", [False, True])
def test_metadata_accepts_literal_or_absolute_cargo_without_rewriting_record(executable, requested_present):
    expected = ["cargo", "+1.64.0", "metadata", "--format-version", "1",
                "--locked", "--all-features"]
    record = {"argv": [executable, *expected[1:]]}
    if requested_present:
        record["requested_argv"] = expected
    before = deepcopy(record)
    scylla.validate_metadata_command(record)
    assert record == before


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(argv=None),
    lambda r: r.update(argv=[]),
    lambda r: r.update(argv="cargo +1.64.0 metadata --format-version 1 --locked --all-features"),
    lambda r: r["argv"].__setitem__(0, None),
    lambda r: r["argv"].__setitem__(0, "/usr/bin/rustc"),
    lambda r: r["argv"].__setitem__(0, "bin/cargo"),
    lambda r: r["argv"].__setitem__(1, "+1.93.0"),
    lambda r: r["argv"].remove("--locked"),
    lambda r: r["argv"].remove("--all-features"),
    lambda r: r["argv"].append("--no-deps"),
    lambda r: r["argv"].__setitem__(-1, "--no-default-features"),
    lambda r: r.update(requested_argv=None),
    lambda r: r["requested_argv"].__setitem__(1, "+1.93.0"),
])
def test_metadata_rejects_changed_tool_flags_or_requested_command(mutate):
    expected = ["cargo", "+1.64.0", "metadata", "--format-version", "1",
                "--locked", "--all-features"]
    record = {"argv": ["/home/v-peiyangliu/.cargo/bin/cargo", *expected[1:]],
              "requested_argv": expected}
    mutate(record)
    with pytest.raises(ContractError):
        scylla.validate_metadata_command(record)


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(command="cargo +1.93.0 generate-lockfile"),
    lambda r: r.update(exit_status=1),
    lambda r: r.update(timed_out=True),
    lambda r: r.update(natural_exit=False),
    lambda r: r.update(source_before_lock_sha256="b" * 64),
    lambda r: r.update(final_lock_sha256="b" * 64),
    lambda r: r["completed_steps"].pop(),
    lambda r: r["completed_steps"][0].remove("minimal-versions"),
    lambda r: r["completed_steps"][1].__setitem__(-1, "1.6.2"),
    lambda r: r["completed_steps"][2].__setitem__(-1, "1.0.36"),
    lambda r: r.update(started_at="2026-09-11T04:59:59-07:00"),
    lambda r: r.update(raw_argv=["cargo", "+1.93.0", "generate-lockfile"]),
])
def test_partial_replaced_or_reordered_prepare_is_rejected(tmp_path, mutate):
    record, decision = valid_prepare(tmp_path)
    mutate(record)
    with pytest.raises(ContractError):
        scylla.prepare_record(record, decision, "a" * 64)


def test_actual_original_prepare_envelope_when_explicitly_supplied():
    configured = os.environ.get("SCYLLA_FIRST_RESOLUTION_INPUTS")
    if not configured or not Path(configured).is_file():
        pytest.skip("requires the explicit original-prepare first-success envelope")
    result = first.prepared_inputs(reference(Path(configured)))
    assert result["locks"]["start"]["sha256"] == result["locks"]["golden"]["sha256"]
    assert result["graph_audits"]["start"]["semantic_graph_sha256"] == (
        result["graph_audits"]["golden"]["semantic_graph_sha256"])
