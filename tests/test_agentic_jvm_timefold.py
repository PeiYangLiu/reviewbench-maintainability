"""No benchmark episodes: exact shell projection and local distribution seeding."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import jvm_timefold as adapter, jvm_timefold_entry as entry
from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash, file_hash

COMMAND = (
    "rm -f core/src/test/java/ai/timefold/solver/core/impl/domain/variable/declarative/"
    "SingleDirectionalParentVariableReferenceGraphTest.java && bash ./mvnw -B -ntp -Dstyle.color=never "
    "-Djacoco.skip=true -pl core -am "
    "-Dtest=DeclarativeShadowVariablesMethodVariablesTest,SimpleListTest,DeclarativeShadowVariablesExtendedValuesTest "
    "-Dsurefire.failIfNoSpecifiedTests=false -DfailIfNoTests=false test"
)
TASK = {"id": adapter.TASK_ID, "repo": "TimefoldAI/timefold-solver", "start_sha": adapter.START}


def test_exact_original_command_keeps_private_prelude_and_all_reactor_arguments():
    public, private = adapter.original_commands(TASK, COMMAND)
    assert hashlib.sha256(COMMAND.encode()).hexdigest() == adapter.COMMAND_SHA256
    assert private.replace(" -o -Dmaven.repo.local=/opt/reviewbench-cache/maven", "", 1) == COMMAND
    assert "rm " not in public and "-Dtest=" not in public and "src/test" not in public
    assert public.endswith("compile") and "-Dmaven.test.skip=true" in public
    assert "-pl core -am" in public and "-Djacoco.skip=true" in public
    assert private.endswith(" test")


@pytest.mark.parametrize("field,value", [
    ("id", "another-task"), ("start_sha", "0" * 40), ("repo", "TimefoldAI/other"),
])
def test_other_task_or_revision_rejected(field, value):
    with pytest.raises(ContractError, match="exact original"):
        adapter.original_commands({**TASK, field: value}, COMMAND)


@pytest.mark.parametrize("command", [
    COMMAND + "; true", COMMAND.replace(" -am ", " "), COMMAND.replace("test", "verify"),
    COMMAND.split(" && ", 1)[1], COMMAND.replace("rm -f", "true"),
])
def test_any_original_command_drift_rejected(command):
    with pytest.raises(ContractError, match="exact original"):
        adapter.original_commands(TASK, command)


def fixture(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    wrapper = workspace / "mvnw"
    wrapper.write_text("#!/bin/sh\nprintf '%s\\n' synthetic\n")
    source = tmp_path / "distribution"
    (source / "bin").mkdir(parents=True)
    (source / "bin/mvn").write_text("#!/bin/sh\nexit 0\n")
    (source / "bin/mvn").chmod(0o755)
    spec = {"schema_version": "reviewbench-timefold-wrapper/v1", "source": str(source),
            "home": str(workspace / ".reviewbench/jvm/home"), "original_distribution_url": adapter.URL,
            "wrapper_files": {"mvnw": file_hash(wrapper)}}
    return workspace, source, spec


def test_hash_matches_original_shell_algorithm():
    shell = r'''str="$1"; h=0
while [ -n "$str" ]; do
  h=$(( ( h * 31 + $(LC_CTYPE=C printf %d "'$str") ) % 4294967296 ))
  str="${str#?}"
done
printf %x "$h"'''
    actual = subprocess.check_output(["bash", "-c", shell, "wrapper", adapter.URL], text=True)
    assert entry.wrapper_hash(adapter.URL) == actual


def test_seed_is_idempotent_preserves_bytes_modes_and_source(tmp_path):
    workspace, source, spec = fixture(tmp_path)
    original = file_hash(workspace / "mvnw")
    destination = entry.seed(spec, workspace)
    assert entry.seed(spec, workspace) == destination
    assert (destination / "bin/mvn").read_bytes() == (source / "bin/mvn").read_bytes()
    assert (destination / "bin/mvn").stat().st_mode & 0o111
    assert file_hash(workspace / "mvnw") == original


@pytest.mark.parametrize("mode", ["wrapper-change", "wrapper-link", "home-escape", "source-link"])
def test_invalid_input_fails_before_copy(tmp_path, mode):
    workspace, source, spec = fixture(tmp_path)
    if mode == "wrapper-change":
        (workspace / "mvnw").write_text("changed")
    elif mode == "wrapper-link":
        (workspace / "mvnw").unlink()
        (workspace / "mvnw").symlink_to(source / "bin/mvn")
    elif mode == "home-escape":
        spec["home"] = str(tmp_path / "outside")
    else:
        (source / "alias").symlink_to(source / "bin/mvn")
    with pytest.raises(ValueError):
        entry.seed(spec, workspace)
    assert not (Path(spec["home"]) / ".m2").exists()


@pytest.mark.parametrize("mode", ["file-drift", "new-file", "mode-drift", "file-link"])
def test_existing_destination_drift_is_not_overwritten(tmp_path, mode):
    workspace, source, spec = fixture(tmp_path)
    target = entry.seed(spec, workspace)
    path = target / "bin/mvn"
    if mode == "file-drift":
        path.write_text("candidate data")
    elif mode == "new-file":
        (target / "new").write_text("candidate data")
    elif mode == "mode-drift":
        path.chmod(0o644)
    else:
        path.unlink()
        path.symlink_to(source / "bin/mvn")
    with pytest.raises(ValueError):
        entry.seed(spec, workspace)
    assert (target / "new").exists() if mode == "new-file" else path.exists()


def test_receipt_binds_exact_original_distribution_and_tree(tmp_path, monkeypatch):
    records = [("bin/mvn", "a" * 64, True)]
    descriptor = {"source_kind": "toolchain", "files": 1, "archive": {"input_bytes": 1024},
                  "tree_sha256": canonical_hash(records)}
    receipt = {"status": "original_Maven391_archive_and_tree_verified", "task_id": adapter.TASK_ID,
               "original_distribution_url": adapter.URL, "official_checksum_url": adapter.URL + ".sha512",
               "archive_sha512": adapter.ARCHIVE_SHA512, "asset": {"path": "/original", "sha256": "b" * 64},
               "entries": [{"path": "bin/mvn", "sha256": "a" * 64, "executable": True}]}
    path = tmp_path / "receipt.json"
    monkeypatch.setattr(adapter, "asset_descriptor", lambda *args: descriptor)
    def context(value):
        path.write_text(json.dumps(value))
        return SimpleNamespace(output=tmp_path, config={"migration": {"timefold_maven_distribution": {
            "path": str(path), "sha256": file_hash(path)}}})
    assert adapter.distribution(context(receipt))[0] == receipt["asset"]
    for key, value in [("task_id", "another"), ("archive_sha512", "0" * 128),
                       ("original_distribution_url", adapter.URL.replace("3.9.1", "3.9.9"))]:
        with pytest.raises(ContractError):
            adapter.distribution(context({**receipt, key: value}))
    bad = copy.deepcopy(receipt)
    bad["entries"][0]["sha256"] = "c" * 64
    with pytest.raises(ContractError, match="tree differs"):
        adapter.distribution(context(bad))
