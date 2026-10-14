"""The known committed seed is distinct from a genuinely supplied candidate lock."""
import copy
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_image_entry as entry, rust_rpgp_locks as host
from reviewbench_maintainability.agentic.contracts import ContractError
from test_agentic_rust_image import fixture, fake_runner


def reference_fixture(tmp_path, version="0.1.0"):
    source, public, private = fixture(tmp_path, version=version)
    public.update(schema_version=entry.REFERENCE_SCHEMA,
                  reference_seed_sha256=public["locks"][0]["sha256"],
                  reference_seed_policy="replace-only-identical-committed-seed-in-private-mirror",
                  package="image", public_command=["check", "--locked", "-p", "image"],
                  timeout_seconds=2400)
    private["command"].remove("--locked")
    private["command"][1:1] = ["-p", "image"]
    private["original_unlocked"] = True
    (source / "Cargo.lock").write_bytes(Path(public["locks"][0]["path"]).read_bytes())
    return source, public, private


@pytest.mark.parametrize("version", ["0.1.0", "0.2.0"])
@pytest.mark.parametrize("grading", [False, True])
def test_committed_seed_is_selected_by_current_graph_without_editing_source(tmp_path, version, grading):
    source, public, private = reference_fixture(tmp_path, version)
    before, calls = entry.snapshot(source), []
    result = entry.run_candidate(public, source, tmp_path / "build", private if grading else None,
                                 run=fake_runner(calls))
    assert result["status"] == "completed" and result["exit_status"] == 0
    assert result["schema_version"] == entry.REFERENCE_SCHEMA
    assert result["candidate_lock_supplied"] and result["candidate_has_original_reference_seed"]
    assert not result["candidate_lock_preserved"]
    assert result["source_before"] == result["source_after"] == before
    assert len(calls) == 3 and len(result["probes"]) == 2
    expected = next(row["sha256"] for row in public["locks"] if version in row["path"])
    assert result["selected_lock_sha256"] == expected
    if version == "0.2.0":
        assert result["mirror_before"]["Cargo.lock"][1] != before["Cargo.lock"][1]
    if grading:
        assert calls[-1][0][-len(private["command"]):] == private["command"]
        assert "--locked" not in calls[-1][0]
    else:
        assert calls[-1][0][-4:] == ["check", "--locked", "-p", "image"]


def test_reference_mode_preserves_new_candidate_lock_without_probing_defaults(tmp_path):
    source, public, private = reference_fixture(tmp_path, "0.2.0")
    supplied = Path(public["locks"][1]["path"]).read_bytes()
    (source / "Cargo.lock").write_bytes(supplied)
    calls = []
    result = entry.run_candidate(public, source, tmp_path / "build", private, run=fake_runner(calls))
    assert result["candidate_lock_preserved"] and not result["candidate_has_original_reference_seed"]
    assert len(result["probes"]) == 1 and len(calls) == 2
    assert (source / "Cargo.lock").read_bytes() == supplied


def test_only_byte_identical_reference_seed_can_be_replaced(tmp_path):
    source, public, private = reference_fixture(tmp_path, "0.2.0")
    authored = (source / "Cargo.lock").read_bytes() + b"\n# candidate-authored lock\n"
    (source / "Cargo.lock").write_bytes(authored)
    calls = []
    with pytest.raises(entry.VariantError, match="no unique compatible"):
        entry.run_candidate(public, source, tmp_path / "build", private, run=fake_runner(calls))
    assert len(calls) == 1 and (source / "Cargo.lock").read_bytes() == authored


def test_original_assertion_failure_stays_negative_without_switching_locks(tmp_path):
    source, public, private = reference_fixture(tmp_path, "0.2.0")
    calls = []
    result = entry.run_candidate(public, source, tmp_path / "build", private,
                                 run=fake_runner(calls, test_pass=False))
    assert result["exit_status"] == 101 and result["test_results"] == {"original_behavior": False}
    assert len(calls) == 3 and result["command_executions"] == 1


@pytest.mark.parametrize("change", ["unbound-seed", "bad-policy", "missing-unlocked",
                                  "locked-test", "changed-package", "changed-public", "old-schema"])
def test_reference_override_is_explicit_and_does_not_loosen_original_schema(tmp_path, change):
    _, public, private = reference_fixture(tmp_path)
    if change == "unbound-seed":
        public["reference_seed_sha256"] = "0" * 64
    elif change == "bad-policy":
        public["reference_seed_policy"] = "replace-every-lock"
    elif change == "missing-unlocked":
        private.pop("original_unlocked")
    elif change == "locked-test":
        private["command"].insert(1, "--locked")
    elif change == "changed-package":
        private["command"][2] = "different"
    elif change == "changed-public":
        public["public_command"] = ["check", "--locked"]
    else:
        public["schema_version"] = entry.SCHEMA
    with pytest.raises(entry.VariantError):
        entry.validate(public, private)


def test_rpgp_optin_and_identity_cannot_affect_another_task():
    context = SimpleNamespace(config={host.OPTION: {host.TASK: "/prepared.json"}})
    assert host.requested(context, {"id": host.TASK})
    assert not host.requested(context, {"id": "another"})
    assert not host.requested(SimpleNamespace(), {"id": host.TASK})
    task = {"id": host.TASK, "repo": host.REPO, "start_sha": host.START}
    for name, value in (("id", "other"), ("repo", "other/repo"), ("start_sha", "0" * 40)):
        changed = {**task, name: value}
        with pytest.raises(ContractError, match="wrong task"):
            host.original_inputs(SimpleNamespace(), changed, copy.deepcopy(task), {}, {})


def test_large_file_snapshot_uses_bounded_reads(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    path = source / "large-module-fixture.bin"
    path.write_bytes(b"x" * (3 * 1024 * 1024 + 1))
    expected = entry.file_digest(path)
    monkeypatch.setattr(Path, "read_bytes", lambda self: pytest.fail("snapshot buffered a whole file"))
    assert entry.snapshot(source) == {"large-module-fixture.bin": ["file", expected, False]}


def test_cargo193_lock_refusal_is_exact_and_bound_to_current_workspace():
    row = {"returncode": 101, "cwd": "/current/source",
           "stderr": "error: cannot update the lock file /current/source/Cargo.lock because --locked was passed to prevent this\n"
                     "help: to generate the lock file without accessing the network, remove the --locked flag and use --offline instead.\n"}
    assert entry.lock_incompatible(row)
    assert not entry.lock_incompatible({**row, "cwd": "/other/source"})
    assert not entry.lock_incompatible({**row, "returncode": 1})
    assert not entry.lock_incompatible({**row, "stderr": row["stderr"].replace("because --locked", "because --frozen")})
