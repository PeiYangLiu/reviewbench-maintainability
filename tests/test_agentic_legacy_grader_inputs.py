"""Legacy private Java assertions remain fixed inputs, not missing Git files."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from reviewbench_maintainability.agentic.assets import ASSET_SCHEMA, AssetPool
from reviewbench_maintainability.agentic.contracts import ContractError, atomic_json, file_hash
from reviewbench_maintainability.agentic.evaluation import materialize_grader_immutable_inputs
from reviewbench_maintainability.agentic.retention import archive_files
from reviewbench_maintainability.agentic.snapshots import restore_immutable, tree_hash
from reviewbench_maintainability.agentic.storage import DirectoryObjectStore, LocalObjectCache
from test_agentic_grader_immutable_inputs import archive_job


def legacy_fixture(tmp_path, index="17"):
    root = tmp_path / "private"
    root.mkdir()
    (root / "original.java").write_text("public class OriginalOracle { /* fixed assertions */ }\n")
    (root / "runner.py").write_text("# original digest-bound fixture installer\n")
    name = "src/test/java/oracle/OriginalOracle.java"
    ident = "original-legacy-" + index
    case = {
        "id": ident, "index": index, "source_relative_path": name,
        "original_command": "bash ./gradlew --offline test",
        "fixture": {"path": str(root / "original.java"), "sha256": file_hash(root / "original.java")},
        "golden_patch": {"sha256": "a" * 64}, "junit_id": "oracle.OriginalOracle#behavior()",
    }
    profile = {"schema_version": "reviewbench-original-legacy-gradle-profile/v1",
               "full_scope": 402, "case": case,
               "original_init": {"sha256": "b" * 64}, "jacoco_agent": {"sha256": "c" * 64}}
    spec = {
        "schema_version": "reviewbench-original-legacy-gradle-execution/v1",
        "id": ident, "original_command": case["original_command"],
        "original_fixture": "original.java", "fixture_sha256": case["fixture"]["sha256"],
        "source_relative_path": name, "expected_tests": [case["junit_id"]],
        "original_expected_count": 1, "original_init_sha256": "b" * 64,
        "jacoco_agent_sha256": "c" * 64,
        "mirror": "/opt/reviewbench-assets/legacy-gradle-mirror",
        "fresh_private_compile": index == "41",
    }
    atomic_json(root / "spec.json", spec)
    pointer = tmp_path / "profile.json"
    atomic_json(pointer, profile)
    target = "/opt/reviewbench-assets/legacy-gradle-grader"
    recipe = SimpleNamespace(
        task_id=ident, assets=[],
        grading={
            "immutable_paths": [name], "bootstrap": [],
            "original_legacy_profile": {"path": str(pointer), "sha256": file_hash(pointer)},
            "golden_patch": {"sha256": "a" * 64},
            "commands": [{
                "command": "python3 " + target + "/runner.py --spec " + target
                           + "/spec.json --output /workspace/.oracle/results.json",
                "parser": "json", "report_path": ".oracle/results.json",
                "expected_tests": [case["junit_id"]],
            }],
        })
    cache = LocalObjectCache(tmp_path / "cache", DirectoryObjectStore(tmp_path / "remote"), min_free_bytes=0)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    pool = AssetPool(cache, scratch, "a" * 32, tmp_path)
    base, work = tmp_path / "base", tmp_path / "candidate"
    base.mkdir()
    work.mkdir()
    state = SimpleNamespace(root=root, profile=profile, spec=spec, pointer=pointer,
                            recipe=recipe, cache=cache, pool=pool, scratch=scratch,
                            base=base, work=work, name=name, tmp_path=tmp_path)
    repack(state)
    return state


def repack(state):
    archive, _ = archive_files(state.root, ["original.java", "runner.py", "spec.json"],
                              state.cache, state.tmp_path / "compression")
    descriptor = state.tmp_path / "asset.json"
    atomic_json(descriptor, {"schema_version": ASSET_SCHEMA, "source_kind": "grader",
                            "tree_sha256": tree_hash(state.root), "archive": archive})
    state.recipe.assets = [{"path": str(descriptor), "sha256": file_hash(descriptor),
                            "target": "/opt/reviewbench-assets/legacy-gradle-grader",
                            "kind": "archive", "visibility": "grader"}]


@pytest.mark.parametrize("index", ["17", "41"])
def test_legacy_spec_stages_original_assertions_and_preserves_full_recipe(tmp_path, index):
    f = legacy_fixture(tmp_path, index)
    before = deepcopy(f.recipe)
    forged = f.work / f.name
    forged.parent.mkdir(parents=True)
    forged.write_text("candidate bypass\n")
    with archive_job(f.pool, f.scratch):
        records = materialize_grader_immutable_inputs(f.base, f.recipe)
        assert len(records) == 1 and records[0]["archive_member"] == "original.java"
        assert records[0]["origin"] == "fixed-grader-archive-member-not-Git-start"
        assert records[0]["sha256"] == f.profile["case"]["fixture"]["sha256"]
        assert records[0]["original_legacy_profile_sha256"] == file_hash(f.pointer)
        restore_immutable(f.base, f.work, f.recipe.grading["immutable_paths"])
        assert forged.read_bytes() == (f.root / "original.java").read_bytes()
        assert materialize_grader_immutable_inputs(f.base, f.recipe) == []
        assert len(f.pool.ready) == 1
        assert f.recipe == before


@pytest.mark.parametrize("change", [
    "profile-hash", "profile-schema", "task", "scope", "spec-id", "spec-path",
    "spec-count-type", "spec-command", "spec-fixture", "fixture-bytes",
    "command", "expected-test", "golden", "public-asset", "duplicate", "missing-asset",
])
def test_changed_legacy_bindings_fail_before_private_assertions_are_installed(tmp_path, change):
    f = legacy_fixture(tmp_path)
    if change == "profile-hash":
        f.recipe.grading["original_legacy_profile"]["sha256"] = "0" * 64
    elif change in {"profile-schema", "task", "scope"}:
        if change == "profile-schema":
            f.profile["schema_version"] = "unbound"
        elif change == "task":
            f.profile["case"]["id"] = "another-task"
        else:
            f.profile["full_scope"] = 1
        atomic_json(f.pointer, f.profile)
        f.recipe.grading["original_legacy_profile"]["sha256"] = file_hash(f.pointer)
    elif change.startswith("spec-"):
        key, value = {
            "spec-id": ("id", "other"), "spec-path": ("source_relative_path", "../outside"),
            "spec-count-type": ("original_expected_count", True),
            "spec-command": ("original_command", "true"),
            "spec-fixture": ("original_fixture", "../outside"),
        }[change]
        f.spec[key] = value
        atomic_json(f.root / "spec.json", f.spec)
        repack(f)
    elif change == "fixture-bytes":
        (f.root / "original.java").write_text("weakened assertions\n")
        repack(f)
    elif change == "command":
        f.recipe.grading["commands"][0]["command"] += " && true"
    elif change == "expected-test":
        f.recipe.grading["commands"][0]["expected_tests"] = ["invented"]
    elif change == "golden":
        f.recipe.grading["golden_patch"]["sha256"] = "0" * 64
    elif change == "public-asset":
        f.recipe.assets[0]["visibility"] = "developer"
    elif change == "duplicate":
        f.recipe.assets *= 2
    else:
        f.recipe.assets = []
    with archive_job(f.pool, f.scratch), pytest.raises(ContractError):
        materialize_grader_immutable_inputs(f.base, f.recipe)
    assert not (f.base / f.name).exists()


def test_legacy_spec_does_not_replace_git_owned_or_unbound_inputs(tmp_path):
    f = legacy_fixture(tmp_path)
    original = f.base / f.name
    original.parent.mkdir(parents=True)
    original.write_text("Git-owned assertion\n")
    f.recipe.grading["immutable_paths"].append("tests/unbound.java")
    with archive_job(f.pool, f.scratch):
        assert materialize_grader_immutable_inputs(f.base, f.recipe) == []
        with pytest.raises(ContractError, match="immutable test missing at start: tests/unbound.java"):
            restore_immutable(f.base, f.work, f.recipe.grading["immutable_paths"])
    assert original.read_text() == "Git-owned assertion\n"
