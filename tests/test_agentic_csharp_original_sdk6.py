import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from agentic_runtime_migration import csharp_original_sdk6 as adapter
from agentic_runtime_migration import csharp_original_sdk6_entry as entry
from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash


def test_unrelated_task_does_not_enter_original_sdk6_route():
    assert adapter.migrate(None, {"repo": "other/project"}, {"index": 25}) is None
    assert adapter.migrate(None, {"repo": "ppy/osu"}, {"index": 26}) is None


@pytest.mark.parametrize("field", ["prepare_command", "test_command", "timeout", "host_adapter", "behavior_assertions"])
def test_full_original_spec_is_bound_before_preparation(tmp_path, monkeypatch, field):
    task = {"repo": "project/repo", "id": "original", "start_sha": "start"}
    spec = {"prepare_command": "old restore && old build", "test_command": "old test", "timeout": 10}
    profile = {"repo": task["repo"], "id": task["id"], "start": task["start_sha"],
               "spec": canonical_hash(spec), "golden": "g", "files": {}}
    monkeypatch.setattr(adapter, "PROFILES", {25: profile})
    oracle = {"index": 25, "evidence": {"source_only_patch_sha256": "g"}}
    spec[field] = "changed"
    with pytest.raises(ContractError, match="complete original"):
        adapter.bind_original(task, oracle, spec, tmp_path)


@pytest.mark.parametrize("field", ["id", "start_sha"])
def test_original_task_binding_is_not_inferred_from_repository_only(tmp_path, monkeypatch, field):
    task = {"repo": "project/repo", "id": "original", "start_sha": "start"}
    profile = {"repo": task["repo"], "id": task["id"], "start": task["start_sha"],
               "spec": canonical_hash({}), "golden": "g", "files": {}}
    monkeypatch.setattr(adapter, "PROFILES", {25: profile})
    oracle = {"index": 25, "evidence": {"source_only_patch_sha256": "g"}}
    with pytest.raises(ContractError, match="complete original"):
        adapter.bind_original({**task, field: "different"}, oracle, {}, tmp_path)


def test_graphql_build_retains_original_warning_and_framework_options():
    spec = {"prepare_command": "dotnet restore oracle/Oracle.csproj --source /task/local-nuget "
            "--source https://api.nuget.org/v3/index.json --nologo -v minimal && "
            "dotnet build oracle/Oracle.csproj --no-restore -m:1 -p:UseSharedCompilation=false "
            "-p:TreatWarningsAsErrors=false -p:WarningsAsErrors= -p:NoWarn=CS8600 --nologo -v minimal"}
    public, private = adapter.original_commands(spec, adapter.PROFILES[25])
    for commands in (public, private):
        assert all(command.startswith(entry.SDK6 + "/dotnet ") for command in commands)
        assert "-p:TreatWarningsAsErrors=false -p:WarningsAsErrors= -p:NoWarn=CS8600" in commands[1]
        assert "-p:TargetFramework" not in " ".join(commands) and "https://" not in commands[0]
        assert "--source " + adapter.PACKAGES + "/feed" in commands[0]
    assert "repo/src/GraphQL/GraphQL.csproj" in public[1]
    assert "oracle/Oracle.csproj" in private[1]


def test_osu_original_warnings_are_not_disabled():
    spec = {"prepare_command": "dotnet restore oracle/Oracle.csproj --nologo -v minimal && "
            "dotnet build oracle/Oracle.csproj --no-restore --nologo -v minimal"}
    public, private = adapter.original_commands(spec, adapter.PROFILES[30])
    assert "repo/osu.Game/osu.Game.csproj" in public[1]
    assert "TreatWarningsAsErrors=false" not in " ".join(public + private)
    assert "NoWarn" not in " ".join(public + private)


@pytest.mark.parametrize("value", ["dotnet build original.csproj", "other && other", "a && b && c"])
def test_preparation_command_boundaries_are_explicit(value):
    with pytest.raises(ContractError):
        adapter.original_commands({"prepare_command": value}, adapter.PROFILES[25])


@pytest.fixture
def package_seed(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "public-packages/packages/example/1.0"
    source.mkdir(parents=True)
    (source / "library.dll").write_bytes(b"original external dependency")
    return workspace, {"packages": str(tmp_path / "public-packages"), "packages_asset_sha256": "a" * 64}, source


def test_package_updates_never_write_into_original_inputs(package_seed):
    work, spec, source = package_seed
    home = entry.seed_packages(spec, work)
    target = home / "example/1.0/library.dll"
    assert target.stat().st_ino != (source / "library.dll").stat().st_ino
    target.write_bytes(b"task-local mutation")
    assert (source / "library.dll").read_bytes() == b"original external dependency"
    assert entry.seed_packages(spec, work) == home


@pytest.mark.parametrize("mutation", ["redirected-home", "unowned-home", "different-marker", "redirected-marker"])
def test_package_home_binding(package_seed, tmp_path, mutation):
    work, spec, source = package_seed
    parent = work / ".reviewbench"
    parent.mkdir()
    if mutation == "redirected-home":
        (parent / "nuget").symlink_to(tmp_path)
    elif mutation == "unowned-home":
        (parent / "nuget").mkdir()
    elif mutation == "redirected-marker":
        (parent / "sdk6-packages.json").symlink_to(tmp_path / "other")
    else:
        entry.seed_packages(spec, work)
        spec["packages_asset_sha256"] = "b" * 64
    with pytest.raises(RuntimeError):
        entry.seed_packages(spec, work)


ADAPTER = '''#!/usr/bin/env bash
set -euo pipefail

file="${REPO_DIR:?}/src/GraphQL/Validation/ValidationOptions.cs"

if ! grep -q 'public ValidationOptions()' "$file"; then
  python3 - "$file" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()
text = text.replace(
    "    public readonly struct ValidationOptions\\n    {\\n",
    "    public readonly struct ValidationOptions\\n    {\\n"
    "        public ValidationOptions()\\n"
    "        {\\n"
    "            Rules = null;\\n"
    "            CancellationToken = default;\\n"
    "        }\\n\\n",
    1,
)
path.write_text(text)
PY
fi
'''


@pytest.mark.parametrize("has_constructor", [False, True])
def test_real_original_setup_only_changes_the_staged_candidate(tmp_path, has_constructor):
    root = tmp_path / "solver"
    target = root / entry.ADAPTER_FILE
    target.parent.mkdir(parents=True)
    text = entry.MARKER + ("        public ValidationOptions() { }\n" if has_constructor else "") + "    }\n"
    target.write_text(text)
    (root / "new-candidate-helper.cs").write_text("class CandidateAdded { }\n")
    staged = tmp_path / "staged"
    shutil.copytree(root, staged)
    helper = tmp_path / "build-adapter.sh"
    helper.write_text(ADAPTER)
    assert entry.digest(helper) == entry.ADAPTER_SHA256
    ledger = {p.relative_to(staged).as_posix(): entry.digest(p) for p in staged.rglob("*.cs")}
    result = entry.apply_original_adapter(staged, helper, ledger)
    assert target.read_text() == text
    expected = text if has_constructor else text.replace(entry.MARKER, entry.INSERT, 1)
    assert (staged / entry.ADAPTER_FILE).read_text() == expected
    assert (staged / "new-candidate-helper.cs").read_bytes() == (root / "new-candidate-helper.cs").read_bytes()
    assert not result["root_candidate_changed"]


def test_original_setup_cannot_touch_another_source(tmp_path):
    root = tmp_path / "staged"
    target = root / entry.ADAPTER_FILE
    target.parent.mkdir(parents=True)
    target.write_text(entry.MARKER + "    }\n")
    other = root / "Other.cs"
    other.write_text("class Original { }")
    helper = tmp_path / "adapter.sh"
    helper.write_text(ADAPTER)
    ledger = {entry.ADAPTER_FILE: entry.digest(target), "Other.cs": entry.digest(other)}
    def unexpected(command, **kwargs):
        target.write_text(target.read_text().replace(entry.MARKER, entry.INSERT, 1))
        other.write_text("class Modified { }")
        return SimpleNamespace(returncode=0)
    with pytest.raises(RuntimeError, match="another production"):
        entry.apply_original_adapter(root, helper, ledger, call=unexpected)


@pytest.mark.parametrize("mutation", ["bytes", "missing", "symlink", "extra-key"])
def test_inputs_require_immutable_file_hash_references(tmp_path, mutation):
    path = tmp_path / "input.json"
    path.write_text("{}")
    ref = {"path": str(path), "sha256": entry.digest(path)}
    if mutation == "bytes":
        path.write_text('{"different":true}')
    elif mutation == "missing":
        path.unlink()
    elif mutation == "symlink":
        other = tmp_path / "other"
        other.write_text("{}")
        path.unlink()
        path.symlink_to(other)
    else:
        ref["extra"] = "unexpected"
    with pytest.raises(ContractError):
        adapter.checked(ref, "input")


@pytest.mark.parametrize("mode", ["public", "grade-build"])
def test_osu_staging_has_no_candidate_ancestor_globalconfig(tmp_path, monkeypatch, mode):
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    (workspace / ".globalconfig").write_text("is_global=true\n")
    isolated = tmp_path / "isolated-local-disk"
    isolated.mkdir()
    monkeypatch.setattr(entry, "EXTERNAL_STAGE", str(isolated))
    spec = {"profile": "osu30", "staging_root": str(isolated)}
    area = entry.staging_area(spec, workspace, mode)
    assert not area.is_relative_to(workspace)
    assert area == isolated / ("public" if mode == "public" else "grading")
    assert (workspace / ".globalconfig").read_text() == "is_global=true\n"


def test_graphql_existing_stage_placement_is_unchanged(tmp_path):
    assert entry.staging_area({"profile": "graphql25"}, tmp_path, "public") == tmp_path / ".reviewbench/sdk6-public"
    assert entry.staging_area({"profile": "graphql25"}, tmp_path, "grade-build") == tmp_path / ".reviewbench/sdk6-grading"


@pytest.mark.parametrize("mutation", ["inside-candidate", "symlink", "wrong-root"])
def test_isolated_stage_mount_is_explicit_and_not_redirected(tmp_path, monkeypatch, mutation):
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    root = tmp_path / "disk"
    root.mkdir()
    if mutation == "inside-candidate":
        root = workspace / "wrong"
        root.mkdir()
    elif mutation == "symlink":
        root.rmdir()
        root.symlink_to(workspace)
    monkeypatch.setattr(entry, "EXTERNAL_STAGE", str(root))
    spec = {"profile": "osu30", "staging_root": str(root) if mutation != "wrong-root" else "/unbound"}
    with pytest.raises(RuntimeError):
        entry.staging_area(spec, workspace, "public")
