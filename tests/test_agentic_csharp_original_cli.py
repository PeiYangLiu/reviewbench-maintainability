import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_runtime_migration import csharp_original_cli as adapter
from agentic_runtime_migration import csharp_original_cli_entry as entry
from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash


def test_unrelated_original_routes_stay_unchanged():
    assert adapter.migrate(None, {"repo": "ppy/osu"}, {"index": 30}) is None
    assert adapter.migrate(None, {"repo": "other/project"}, {"index": 19}) is None


@pytest.mark.parametrize("index", [13, 19])
@pytest.mark.parametrize("field", ["id", "start_sha", "spec", "golden"])
def test_exact_task_start_full_spec_and_golden_are_bound(tmp_path, monkeypatch, index, field):
    spec = {"prepare_command": "original", "test_command": "original", "pass_to_pass_tests": ["one"]}
    task = {"repo": "original/repo", "id": "original-id", "start_sha": "original-start"}
    profile = {"repo": task["repo"], "id": task["id"], "start": task["start_sha"],
               "spec": canonical_hash(spec), "golden": "original-golden", "files": {}}
    oracle = {"index": index, "evidence": {"source_only_patch_sha256": profile["golden"]}}
    monkeypatch.setattr(adapter, "PROFILES", {index: profile})
    if field == "spec":
        spec["prepare_command"] = "easier command"
    elif field == "golden":
        oracle["evidence"]["source_only_patch_sha256"] = "different"
    else:
        task[field] = "different"
    with pytest.raises(ContractError, match="complete task/spec/golden"):
        adapter.bind_original(task, oracle, spec, tmp_path)


@pytest.mark.parametrize("index,sdk", [(13, "6.0.400"), (19, "7.0.410")])
def test_original_commands_keep_whole_projects_and_original_options(index, sdk):
    profile = adapter.PROFILES[index]
    project = profile["project"]
    spec = {"prepare_command": f"dotnet restore {project} --nologo -v minimal && "
                              f"dotnet build {project} --no-restore --nologo -v minimal"}
    public, private = adapter.original_commands(spec, profile)
    assert len(private) == 2 and len(public) == len(profile["production"]) * 2
    for production in profile["production"]:
        assert sum("repo/" + production in cmd for cmd in public) == 2
    assert all(cmd.startswith(entry.PINS[profile["name"]][0] + "/dotnet ") for cmd in public + private)
    assert entry.PINS[profile["name"]][1] == sdk
    assert all("--no-restore" in cmd for cmd in private[1:])
    assert all("--source " + adapter.PACKAGES + "/feed" in cmd for cmd in public[::2] + private[::2])
    text = " ".join(public + private)
    assert "TargetFramework=" not in text and "NoWarn" not in text
    assert "TreatWarningsAsErrors" not in text and "RunAnalyzers" not in text
    assert "--no-restore" in private[1] and "-m:1" in private[1]


@pytest.mark.parametrize("command", ["other", "a && b && c", "dotnet restore replaced.csproj && dotnet build replaced.csproj"])
def test_command_boundaries_cannot_shrink(command):
    with pytest.raises(ContractError):
        adapter.original_commands({"prepare_command": command}, adapter.PROFILES[19])


@pytest.mark.parametrize("mutation", ["missing", "digest", "relative", "symlink"])
def test_input_reference_must_be_immutable(tmp_path, mutation):
    p = tmp_path / "input.json"
    p.write_text("{}")
    ref = {"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
    if mutation == "missing":
        ref = None
    elif mutation == "digest":
        ref["sha256"] = "0" * 64
    elif mutation == "relative":
        ref["path"] = "input.json"
    else:
        q = tmp_path / "link.json"
        q.symlink_to(p)
        ref["path"] = str(q)
    with pytest.raises(ContractError):
        adapter.checked(ref, "fixture")


def test_seed_is_a_mutable_copy_not_a_shared_link(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "dependency.dll").write_bytes(b"external dependency")
    target = tmp_path / "cache/nuget"
    entry.seed(source, target, {"sha256": "original"})
    assert (target / "dependency.dll").stat().st_ino != (source / "dependency.dll").stat().st_ino
    (target / "dependency.dll").write_bytes(b"local modification")
    assert (source / "dependency.dll").read_bytes() == b"external dependency"
    assert entry.seed(source, target, {"sha256": "original"}) == target
    with pytest.raises(RuntimeError, match="identity differs"):
        entry.seed(source, target, {"sha256": "another"})


@pytest.mark.parametrize("mode", ["public", "grade-build"])
def test_stage_is_outside_candidate_globalconfig_ancestors(tmp_path, monkeypatch, mode):
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    cache = tmp_path / "local-disk-cache"
    cache.mkdir()
    monkeypatch.setattr(entry, "CACHE", str(cache))
    area = entry.staging_area(workspace, mode)
    assert area.parent == cache and not area.is_relative_to(workspace)
    monkeypatch.setattr(entry, "CACHE", str(workspace / "nested"))
    (workspace / "nested").mkdir()
    with pytest.raises(RuntimeError, match="independent local-disk"):
        entry.staging_area(workspace, mode)


def test_redirected_cache_is_rejected(tmp_path, monkeypatch):
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    cache = tmp_path / "cache"
    cache.symlink_to(workspace, target_is_directory=True)
    monkeypatch.setattr(entry, "CACHE", str(cache))
    with pytest.raises(RuntimeError):
        entry.staging_area(workspace, "public")


@pytest.fixture
def roslyn_source(tmp_path, monkeypatch):
    root = tmp_path / "staged"
    root.mkdir()
    original = {"sdk": {"version": "7.0.410", "rollForward": "disable"},
                "tools": {"dotnet": "retained-tool"},
                "msbuild-sdks": {"Microsoft.DotNet.Arcade.Sdk": "old", "Microsoft.DotNet.Helix.Sdk": "unchanged"}}
    (root / "global.json").write_text(json.dumps(original))
    for rel in [entry.ROSLYN_ROOTS[0] + "Original.cs", entry.ROSLYN_ROOTS[1] + "NewHelper.cs", "other/Keep.cs"]:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("throw ExceptionUtilities.Unreachable();\n")
    helper = tmp_path / "original-setup.sh"
    helper.write_text("original fixture helper")
    monkeypatch.setattr(entry, "ROSLYN_SETUP", hashlib.sha256(helper.read_bytes()).hexdigest())

    def invoke(command, **kwargs):
        assert command == ["bash", str(helper)]
        assert kwargs["env"]["REPO_DIR"] == str(root)
        for rel in entry.ROSLYN_ROOTS:
            for p in (root / rel).rglob("*.cs"):
                p.write_text(p.read_text().replace("ExceptionUtilities.Unreachable()", "ExceptionUtilities.Unreachable"))
        data = json.loads((root / "global.json").read_text())
        data["sdk"].update(version="7.0.100", allowPrerelease=False, rollForward="latestFeature")
        data["msbuild-sdks"]["Microsoft.DotNet.Arcade.Sdk"] = "8.0.0-beta.22466.3"
        (root / "global.json").write_text(json.dumps(data, indent=2) + "\n")
        return SimpleNamespace(returncode=0)
    return root, helper, invoke


def test_original_setup_changes_only_two_staged_roots_and_expected_metadata(roslyn_source):
    root, helper, invoke = roslyn_source
    result = entry.apply_original_roslyn_setup(root, helper, call=invoke)
    assert len(result["changed_paths"]) == 3
    assert (root / "other/Keep.cs").read_text() == "throw ExceptionUtilities.Unreachable();\n"
    assert json.loads((root / "global.json").read_text())["tools"] == {"dotnet": "retained-tool"}


@pytest.mark.parametrize("mutation", ["extra_source", "delete_source", "other_sdk_metadata"])
def test_setup_rejects_unaccounted_production_or_metadata_change(roslyn_source, mutation):
    root, helper, invoke = roslyn_source

    def changed(*args, **kwargs):
        r = invoke(*args, **kwargs)
        if mutation == "extra_source":
            (root / "other/Keep.cs").write_text("rewritten")
        elif mutation == "delete_source":
            (root / "other/Keep.cs").unlink()
        else:
            data = json.loads((root / "global.json").read_text())
            data["tools"]["dotnet"] = "different"
            (root / "global.json").write_text(json.dumps(data, indent=2) + "\n")
        return r
    with pytest.raises(RuntimeError, match="unaccounted"):
        entry.apply_original_roslyn_setup(root, helper, call=changed)


def test_build_exit_is_not_a_signal_to_try_another_sdk(tmp_path):
    calls = []
    def failed(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=23)
    with pytest.raises(SystemExit) as caught:
        entry.run(["original-sdk", "build"], cwd=tmp_path, env={}, call=failed)
    assert caught.value.code == 23 and calls == [["original-sdk", "build"]]
