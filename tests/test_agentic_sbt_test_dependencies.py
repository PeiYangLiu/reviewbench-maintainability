from pathlib import Path
import hashlib
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from agentic_runtime_migration.jvm_managed import sbt_test_dependency_arguments
from prepare_agentic_sbt_dependencies import public_build_receipt, preparation_profile, dependency_arguments
from reviewbench_maintainability.agentic.contracts import ContractError


@pytest.mark.parametrize("command,expected", [
    ("/oracle-tools/sbt -batch 'testOnly *PrivateSuite'", ["-batch", "Test/update"]),
    ("/oracle-tools/sbt -batch 'rootJVM/Test/testOnly *.PrivateSuite'",
     ["-batch", "rootJVM/Test/update"]),
    ("/oracle-tools/sbt -batch '++3.3.1' 'cross/Test/testQuick *PrivateSuite'",
     ["-batch", "++3.3.1", "cross/Test/update"]),
    ("/oracle-tools/sbt -batch 'set Global / enableWasmEverywhere := true' "
     "'testSuite2_13/Test/testOnly *ExportsTest *StackTraceTest'",
     ["-batch", "set Global / enableWasmEverywhere := true", "testSuite2_13/Test/update"]),
    ("/oracle-tools/sbt -batch 'p/testOnly *One' 'p/testOnly *Two'",
     ["-batch", "p/Test/update"]),
    ("/oracle-tools/sbt -batch p/compile", []),
])
def test_resolution_uses_original_test_configurations_without_executing_assertions(command, expected):
    assert sbt_test_dependency_arguments(command) == expected
    assert not any("Private" in argument or "testOnly" in argument for argument in expected)


def test_resolution_does_not_convert_arbitrary_runmain_into_a_dependency_task():
    with pytest.raises(ContractError):
        sbt_test_dependency_arguments("/oracle-tools/sbt -batch 'root/Compile/runMain PrivateHarness'")


def test_failure_bound_public_project_updates_add_missing_cross_version_dependencies():
    assert dependency_arguments(
        "/oracle-tools/sbt -batch 'testSuite2_13/Test/testOnly *ExportsTest'",
        ["linker2_12", "scalalibInternal2_12", "linker2_12"],
    ) == ["-batch", "testSuite2_13/Test/update", "linker2_12/update", "scalalibInternal2_12/update"]


@pytest.mark.parametrize("project", ["../private", "name/update", "name; runMain Private", "$PROJECT", ""])
def test_extra_dependency_projects_are_literal_ids_only(project):
    with pytest.raises(ContractError):
        dependency_arguments("/oracle-tools/sbt -batch test", [project])


def previous_build(tmp_path):
    log = tmp_path / "install.log"
    log.write_text("[success] public build\n")
    task = {"id": "task", "start_sha": "a" * 40}
    base = {"path": "/frozen/base.json", "sha256": "b" * 64}
    record = {**task, "status": "prepared", "exit_status": 0,
              "oracle_fingerprint": "oracle", "base_asset": base,
              "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
              "command": "original public compilation", "seconds": 10,
              "input_hashes": {"build.sbt": "c" * 64}}
    return task, base, record


def test_resolve_only_retains_successful_public_build_binding(tmp_path):
    task, base, record = previous_build(tmp_path)
    receipt = public_build_receipt(tmp_path, record, task, "oracle", base)
    assert receipt["command"] == record["command"]
    assert receipt["log_sha256"] == record["log_sha256"]
    assert receipt["input_hashes"] == record["input_hashes"]


def test_dependency_only_update_does_not_occupy_whole_project_build_slots():
    assert preparation_profile(True) == {"cpus": 2, "memory_gib": 4, "scratch_gib": 6, "heavy": False}
    assert preparation_profile(False) == {"cpus": 2, "memory_gib": 8, "scratch_gib": 8, "heavy": True}


@pytest.mark.parametrize("change", [
    {"status": "pending"}, {"exit_status": 1}, {"id": "other"},
    {"start_sha": "d" * 40}, {"oracle_fingerprint": "other"},
    {"base_asset": {"path": "/other"}}, {"log_sha256": "0" * 64}, {"input_hashes": {}},
])
def test_resolve_only_rejects_wrong_or_unproven_public_build(tmp_path, change):
    task, base, record = previous_build(tmp_path)
    record.update(change)
    with pytest.raises(ContractError, match="bound successful public build"):
        public_build_receipt(tmp_path, record, task, "oracle", base)
