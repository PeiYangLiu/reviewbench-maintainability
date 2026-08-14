import json
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import importlib.util

SPEC = importlib.util.spec_from_file_location(
    "run_legacy_gopy_scripted_correctness",
    SCRIPTS_DIR / "run_legacy_gopy_scripted_correctness.py",
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
BASE = MODULE.BASE

REPO_ROOT = Path(__file__).parents[1]
CONTRACT_PATH = REPO_ROOT / "data/legacy_contracts/gopy_scripted_20260808.json"
RELEASE_TASKS = REPO_ROOT / "data/releases/hard/20260808/tasks.jsonl"
RELEASE_ORACLES = REPO_ROOT / "data/releases/hard/20260808/oracles.jsonl"


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def run(*args, cwd=None):
    result = subprocess.run(
        args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    assert result.returncode == 0, result.stdout
    return result.stdout


def make_git_repo(root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    run("git", "init", "-q", cwd=root)
    run("git", "-C", str(root), "config", "user.email", "a@example.com")
    run("git", "-C", str(root), "config", "user.name", "a")
    (root / "main.go").write_text("package main\n", encoding="utf-8")
    run("git", "-C", str(root), "add", "-A")
    run("git", "-C", str(root), "commit", "-q", "-m", "init")
    return run("git", "-C", str(root), "rev-parse", "HEAD").strip()


def base_contract(**overrides):
    entry = {
        "lane": "gopy",
        "repo": "owner/repo",
        "start_sha": overrides.pop("start_sha", "start"),
        "status": "recovered",
        "recipe": {
            "kind": "go_docker_p2p",
            "image": "golang:1.25-bookworm",
            "network": "host",
            "workspace_mount": "/work",
            "module_dir": ".",
            "test_target": "./pkg",
            "selector_field": "test_selector",
            "pass_to_pass_field": "pass_to_pass_tests",
            "env": {"GOTOOLCHAIN": "local"},
            "gomodcache_volume": "vol-mod",
            "gobuildcache_volume": "vol-build",
        },
    }
    entry.update(overrides)
    return {
        "schema_version": MODULE.CONTRACT_SCHEMA,
        "contract_version": "test",
        "tasks": {"task-1": entry},
    }


def strict_task(task_id="task-1", start_sha="start"):
    return {
        "id": task_id,
        "repo": "owner/repo",
        "start_sha": start_sha,
        "target_sha": "target",
        "pr_number": 1,
        "inferred_language": "Go",
    }


def strict_oracle(task_id="task-1", **overrides):
    oracle = {
        "id": task_id,
        "repo": "owner/repo",
        "start_sha": "start",
        "target_sha": "target",
        "pr_number": 1,
        "golden_correctness_ok": True,
        "pass_to_pass": 2,
        "changed_lines_covered": True,
        "language": "Go",
        "test_selector": "^(?:TestA|TestB)$",
        "pass_to_pass_tests": ["TestA", "TestB"],
    }
    oracle.update(overrides)
    return oracle


# ---------------------------------------------------------------------------
# The shipped contract must stay aligned with the published release
# ---------------------------------------------------------------------------


def test_shipped_contract_covers_exactly_the_published_legacy_gopy_scripted_scope():
    contract = MODULE.load_contract(CONTRACT_PATH)
    tasks, oracles, tasks_by_id, oracles_by_id = BASE.load_release(
        RELEASE_TASKS, RELEASE_ORACLES
    )
    plan = BASE.build_task_plan(tasks, oracles_by_id)
    expected = {
        row["id"]
        for row in plan
        if row["source_kind"] == "published_legacy"
        and row["lane"] in MODULE.SUPPORTED_LANES
    }
    assert expected, "release must still contain published-legacy gopy/scripted tasks"
    assert set(contract["tasks"]) == expected
    by_id = {row["id"]: row for row in plan}
    for task_id, entry in contract["tasks"].items():
        assert entry["lane"] == by_id[task_id]["lane"]
        assert entry["repo"] == by_id[task_id]["repo"]
        assert entry["start_sha"] == by_id[task_id]["start_sha"]


def test_shipped_contract_recovered_entries_declare_pinned_assets():
    contract = MODULE.load_contract(CONTRACT_PATH)
    recovered = {
        task_id: entry
        for task_id, entry in contract["tasks"].items()
        if entry["status"] == "recovered"
    }
    assert recovered, "at least one task must be recovered"
    for task_id, entry in recovered.items():
        recipe = entry["recipe"]
        assert recipe["image"], task_id
        assert recipe["kind"] in MODULE.RECIPE_KINDS
        if recipe["kind"] in {
            "go_docker_session_oracle",
            "python_docker_session_oracle",
            "shell_docker_session_oracle",
        }:
            for asset in MODULE.session_assets(recipe):
                assert len(asset["sha256"]) == 64, task_id
                assert asset["destination"], task_id
        if recipe["kind"] == "python_image_pytest":
            assert len(recipe["workspace_anchor"]["sha256"]) == 64
            assert recipe["expected_passed"] > 0


def test_shipped_contract_blocked_entries_carry_evidence():
    contract = MODULE.load_contract(CONTRACT_PATH)
    blocked = [
        (task_id, entry)
        for task_id, entry in contract["tasks"].items()
        if entry["status"] == "blocked"
    ]
    assert blocked
    for task_id, entry in blocked:
        for blocker in entry["blockers"]:
            assert blocker["kind"], task_id
            assert blocker["detail"], task_id
            assert blocker["evidence"], task_id
            assert blocker["resolution"], task_id


# ---------------------------------------------------------------------------
# Contract validation
# ---------------------------------------------------------------------------


def test_load_contract_rejects_unknown_schema(tmp_path):
    path = tmp_path / "contract.json"
    path.write_text(json.dumps({"schema_version": "nope", "tasks": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected contract schema"):
        MODULE.load_contract(path)


def test_load_contract_rejects_unsupported_lane(tmp_path):
    contract = base_contract(lane="rust")
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(contract), encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported contract lane"):
        MODULE.load_contract(path)


def test_load_contract_rejects_unknown_recipe_kind(tmp_path):
    contract = base_contract()
    contract["tasks"]["task-1"]["recipe"]["kind"] = "shell_script"
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(contract), encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported recipe kind"):
        MODULE.load_contract(path)


def test_load_contract_rejects_blocked_entry_without_blockers(tmp_path):
    contract = base_contract(status="blocked", recipe=None, blockers=[])
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(contract), encoding="utf-8")
    with pytest.raises(ValueError, match="carries no blockers"):
        MODULE.load_contract(path)


# ---------------------------------------------------------------------------
# Scope drift detection
# ---------------------------------------------------------------------------


def test_select_scope_rejects_task_ids_outside_the_contract():
    contract = base_contract()
    plan = [
        {"id": "task-1", "lane": "gopy", "start_sha": "start",
         "source_kind": "published_legacy", "repo": "owner/repo"}
    ]
    with pytest.raises(ValueError, match="outside the contract scope"):
        MODULE.select_scope(plan, contract, ["other"])


def test_select_scope_rejects_modern_source_kind_drift():
    contract = base_contract()
    plan = [
        {"id": "task-1", "lane": "gopy", "start_sha": "start",
         "source_kind": "modern", "repo": "owner/repo"}
    ]
    with pytest.raises(ValueError, match="no longer published_legacy"):
        MODULE.select_scope(plan, contract, None)


def test_select_scope_rejects_lane_and_sha_drift():
    contract = base_contract()
    lane_drift = [
        {"id": "task-1", "lane": "scripted", "start_sha": "start",
         "source_kind": "published_legacy", "repo": "owner/repo"}
    ]
    with pytest.raises(ValueError, match="lane classification drifted"):
        MODULE.select_scope(lane_drift, contract, None)
    sha_drift = [
        {"id": "task-1", "lane": "gopy", "start_sha": "moved",
         "source_kind": "published_legacy", "repo": "owner/repo"}
    ]
    with pytest.raises(ValueError, match="start SHA drifted"):
        MODULE.select_scope(sha_drift, contract, None)


def test_select_scope_rejects_missing_task_ids():
    contract = base_contract()
    with pytest.raises(ValueError, match="absent from --tasks"):
        MODULE.select_scope([], contract, None)


# ---------------------------------------------------------------------------
# Oracle result parsing / judging
# ---------------------------------------------------------------------------


def test_go_p2p_outcome_requires_every_expected_test_to_pass():
    stream = "\n".join(
        json.dumps(row)
        for row in (
            {"Action": "pass", "Test": "TestA"},
            {"Action": "pass", "Test": "TestB"},
            {"Action": "fail", "Test": "TestUnrelated"},
        )
    )
    ok, passed, failed = MODULE.go_p2p_outcome(stream, ["TestA", "TestB"])
    assert (ok, passed, failed) == (True, 2, [])


def test_go_p2p_outcome_counts_a_missing_test_as_failed():
    stream = json.dumps({"Action": "pass", "Test": "TestA"})
    ok, passed, failed = MODULE.go_p2p_outcome(stream, ["TestA", "TestB"])
    assert ok is False
    assert passed == 1
    assert failed == ["TestB"]


def test_go_p2p_outcome_treats_a_build_failure_as_failure_not_success():
    build_failure = (
        '{"Action":"build-output","Output":"pkg/x.go:1:1: undefined: y\\n"}\n'
        '{"Action":"build-fail","ImportPath":"pkg"}\n'
    )
    ok, passed, failed = MODULE.go_p2p_outcome(build_failure, ["TestA"])
    assert ok is False
    assert passed == 0
    assert failed == ["TestA"]


def test_go_p2p_outcome_rejects_an_empty_expected_set():
    ok, passed, failed = MODULE.go_p2p_outcome("", [])
    assert ok is False
    assert (passed, failed) == (0, [])


def test_go_p2p_outcome_treats_a_skipped_test_as_not_passing():
    stream = json.dumps({"Action": "skip", "Test": "TestA"})
    ok, _, failed = MODULE.go_p2p_outcome(stream, ["TestA"])
    assert ok is False
    assert failed == ["TestA"]


def test_junit_outcome_counts_passes_failures_and_skips(tmp_path):
    report = tmp_path / "report.xml"
    report.write_text(
        "<testsuites><testsuite>"
        '<testcase classname="m" name="ok"/>'
        '<testcase classname="m" name="bad"><failure>boom</failure></testcase>'
        '<testcase classname="m" name="err"><error>boom</error></testcase>'
        '<testcase classname="m" name="skipped"><skipped/></testcase>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    passed, total, failed = MODULE.junit_outcome(report)
    assert passed == 1
    assert total == 4
    assert failed == ["m::bad", "m::err"]


def test_junit_outcome_handles_a_bare_testsuite_root(tmp_path):
    report = tmp_path / "report.xml"
    report.write_text(
        '<testsuite><testcase classname="m" name="ok"/></testsuite>',
        encoding="utf-8",
    )
    assert MODULE.junit_outcome(report) == (1, 1, [])


def test_candidate_changed_lines_prefers_the_staged_generation_count():
    assert MODULE.candidate_changed_lines({"changed_lines": 12}, ["a", "b"]) == 12
    assert MODULE.candidate_changed_lines({}, ["a", "b"]) == 2


# ---------------------------------------------------------------------------
# Patch handling
# ---------------------------------------------------------------------------


def test_patch_changed_paths_and_scope_safety():
    patch = (
        "diff --git a/src/x.py b/src/x.py\n"
        "diff --git a/../etc/passwd b/../etc/passwd\n"
    )
    changed = MODULE.patch_changed_paths(patch)
    assert changed == ["src/x.py", "../etc/passwd"]
    assert MODULE.scope_is_safe(["src/x.py"]) is True
    assert MODULE.scope_is_safe(changed) is False
    assert MODULE.scope_is_safe(["/etc/passwd"]) is False


def test_apply_patch_round_trip_and_failure(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "file.txt").write_text("a\nb\nc\n", encoding="utf-8")
    good = tmp_path / "good.patch"
    good.write_text(
        "diff --git a/file.txt b/file.txt\n"
        "--- a/file.txt\n+++ b/file.txt\n"
        "@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n",
        encoding="utf-8",
    )
    ok, detail = MODULE.apply_patch(workdir, good)
    assert ok, detail
    assert (workdir / "file.txt").read_text(encoding="utf-8") == "a\nB\nc\n"

    bad = tmp_path / "bad.patch"
    bad.write_text(
        "diff --git a/missing.txt b/missing.txt\n"
        "--- a/missing.txt\n+++ b/missing.txt\n"
        "@@ -1,1 +1,1 @@\n-old\n+new\n",
        encoding="utf-8",
    )
    ok, detail = MODULE.apply_patch(workdir, bad)
    assert ok is False
    assert detail


def test_git_worktree_checkout_materializes_and_verifies_exact_start(tmp_path):
    repo = tmp_path / "repo"
    sha = make_git_repo(repo)
    dest = tmp_path / "worktree"
    MODULE.git_worktree_checkout(repo, sha, dest)
    assert run("git", "-C", str(dest), "rev-parse", "HEAD").strip() == sha
    MODULE.remove_worktree(repo, dest)
    assert not dest.exists()
    with pytest.raises(RuntimeError):
        MODULE.git_worktree_checkout(repo, "0" * 40, dest)


def test_git_blob_sha256_matches_worktree_content(tmp_path):
    repo = tmp_path / "repo"
    sha = make_git_repo(repo)
    import hashlib

    expected = hashlib.sha256((repo / "main.go").read_bytes()).hexdigest()
    assert MODULE.git_blob_sha256(repo, sha, "main.go") == expected
    assert MODULE.git_blob_sha256(repo, sha, "absent.go") is None


def test_commit_present_detects_missing_start_sha(tmp_path):
    repo = tmp_path / "repo"
    sha = make_git_repo(repo)
    assert MODULE.commit_present(repo, sha) is True
    assert MODULE.commit_present(repo, "0" * 40) is False
    assert MODULE.commit_present(tmp_path / "absent", sha) is False


# ---------------------------------------------------------------------------
# Fail-closed blocker detection
# ---------------------------------------------------------------------------


def stub_docker(monkeypatch, *, images=None, volumes=(), files=None):
    images = images or {}
    files = files or {}
    monkeypatch.setattr(MODULE, "docker_available", lambda: True)
    monkeypatch.setattr(
        MODULE, "docker_image_id", lambda image: images.get(image)
    )
    monkeypatch.setattr(
        MODULE, "docker_volume_present", lambda volume: volume in volumes
    )
    monkeypatch.setattr(
        MODULE,
        "docker_volume_identity",
        lambda volume: {"driver": "local"} if volume in volumes else None,
    )
    monkeypatch.setattr(
        MODULE, "docker_file_sha256", lambda image, path: files.get(path)
    )


def test_recovered_blockers_reports_every_missing_asset(tmp_path, monkeypatch):
    stub_docker(monkeypatch)
    contract = base_contract()
    entry = contract["tasks"]["task-1"]
    blockers = MODULE.recovered_blockers(
        "task-1",
        entry,
        strict_oracle(),
        strict_task(),
        legacy_root=tmp_path,
        asset_root=tmp_path,
    )
    joined = " ".join(blockers)
    assert "missing retained repo checkout" in joined
    assert "missing Docker image" in joined
    assert "missing Docker volume: vol-mod" in joined
    # the Go build cache is a derived, self-verifying accelerator and is
    # deliberately not pinned
    assert "vol-build" not in joined


def test_recovered_blockers_pass_when_every_asset_resolves(tmp_path, monkeypatch):
    repo = tmp_path / "repos/owner__repo"
    sha = make_git_repo(repo)
    stub_docker(
        monkeypatch,
        images={"golang:1.25-bookworm": "sha256:aaa"},
        volumes={"vol-mod", "vol-build"},
    )
    MODULE.load_digest_cache(tmp_path)
    contract = base_contract(start_sha=sha)
    blockers = MODULE.recovered_blockers(
        "task-1",
        contract["tasks"]["task-1"],
        strict_oracle(),
        strict_task(start_sha=sha),
        legacy_root=tmp_path,
        asset_root=tmp_path,
    )
    assert blockers == []


def test_recovered_blockers_detect_a_retagged_image(tmp_path, monkeypatch):
    repo = tmp_path / "repos/owner__repo"
    sha = make_git_repo(repo)
    stub_docker(
        monkeypatch,
        images={"golang:1.25-bookworm": "sha256:different"},
        volumes={"vol-mod", "vol-build"},
    )
    contract = base_contract()
    contract["tasks"]["task-1"]["recipe"]["image_id"] = "sha256:recorded"
    blockers = MODULE.recovered_blockers(
        "task-1",
        contract["tasks"]["task-1"],
        strict_oracle(),
        strict_task(start_sha=sha),
        legacy_root=tmp_path,
        asset_root=tmp_path,
    )
    assert any("not the recorded sha256:recorded" in item for item in blockers)


def test_recovered_blockers_detect_a_start_sha_absent_from_the_checkout(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repos/owner__repo"
    make_git_repo(repo)
    stub_docker(
        monkeypatch,
        images={"golang:1.25-bookworm": "sha256:aaa"},
        volumes={"vol-mod", "vol-build"},
    )
    blockers = MODULE.recovered_blockers(
        "task-1",
        base_contract()["tasks"]["task-1"],
        strict_oracle(),
        strict_task(start_sha="0" * 40),
        legacy_root=tmp_path,
        asset_root=tmp_path,
    )
    assert any("does not contain the exact task start_sha" in b for b in blockers)


def test_recovered_blockers_detect_a_tampered_session_oracle(tmp_path, monkeypatch):
    repo = tmp_path / "repos/owner__repo"
    sha = make_git_repo(repo)
    stub_docker(
        monkeypatch,
        images={"golang:1.24.0-bookworm": "sha256:aaa"},
        volumes={"vol-mod", "vol-build"},
    )
    asset = tmp_path / "assets/session_test.go"
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_text("package initrd\n", encoding="utf-8")
    contract = base_contract()
    recipe = contract["tasks"]["task-1"]["recipe"]
    recipe.update(
        {
            "kind": "go_docker_session_oracle",
            "image": "golang:1.24.0-bookworm",
            "run_selector": "^TestRecovery$",
            "expected_tests": ["TestRecovery"],
            "session_test": {
                "asset": "assets/session_test.go",
                "sha256": "0" * 64,
                "destination": "initrd/recovery_oracle_test.go",
            },
        }
    )
    blockers = MODULE.recovered_blockers(
        "task-1",
        contract["tasks"]["task-1"],
        strict_oracle(),
        strict_task(start_sha=sha),
        legacy_root=tmp_path,
        asset_root=tmp_path,
    )
    assert any("has SHA-256" in item for item in blockers)


def test_recovered_blockers_detect_a_drifted_in_image_workspace(tmp_path, monkeypatch):
    repo = tmp_path / "repos/owner__repo"
    sha = make_git_repo(repo)
    stub_docker(
        monkeypatch,
        images={"legacy-image": "sha256:aaa"},
        volumes=set(),
        files={"/work/src/main.go": "0" * 64},
    )
    contract = base_contract()
    contract["tasks"]["task-1"]["recipe"] = {
        "kind": "python_image_pytest",
        "image": "legacy-image",
        "network": "none",
        "workdir": "/work/src",
        "test_path": "tests/test_x.py",
        "expected_passed": 3,
        "workspace_anchor": {"path": "main.go", "sha256": "1" * 64},
    }
    blockers = MODULE.recovered_blockers(
        "task-1",
        contract["tasks"]["task-1"],
        strict_oracle(),
        strict_task(start_sha=sha),
        legacy_root=tmp_path,
        asset_root=tmp_path,
    )
    assert any("in-image workspace anchor" in item for item in blockers)


def test_recovered_blockers_require_the_oracle_selector_and_p2p_list(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repos/owner__repo"
    sha = make_git_repo(repo)
    stub_docker(
        monkeypatch,
        images={"golang:1.25-bookworm": "sha256:aaa"},
        volumes={"vol-mod", "vol-build"},
    )
    oracle = strict_oracle()
    oracle.pop("test_selector")
    oracle["pass_to_pass_tests"] = []
    blockers = MODULE.recovered_blockers(
        "task-1",
        base_contract()["tasks"]["task-1"],
        oracle,
        strict_task(start_sha=sha),
        legacy_root=tmp_path,
        asset_root=tmp_path,
    )
    assert any("test_selector" in item for item in blockers)
    assert any("pass_to_pass_tests" in item for item in blockers)


def test_blocked_task_reports_its_recorded_blockers(tmp_path, monkeypatch):
    stub_docker(monkeypatch)
    entry = {
        "lane": "scripted",
        "repo": "owner/repo",
        "start_sha": "start",
        "status": "blocked",
        "blockers": [
            {
                "kind": "missing_harness",
                "detail": "the session harness does not survive",
                "evidence": ["${SESSION_FILES}/harness.sh"],
                "resolution": "requires new Oracle engineering",
            }
        ],
        "missing_assets": [
            {"kind": "path", "value": "${SESSION_FILES}/harness.sh"}
        ],
    }
    blockers = MODULE.task_blockers(
        "task-1",
        entry,
        strict_oracle(),
        strict_task(),
        legacy_root=tmp_path,
        asset_root=tmp_path,
        session_files=tmp_path / "files",
    )
    assert blockers == [
        "missing_harness: the session harness does not survive"
    ]


def test_blocked_task_flags_a_reappeared_asset(tmp_path, monkeypatch):
    stub_docker(monkeypatch)
    files = tmp_path / "files"
    files.mkdir()
    (files / "harness.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    entry = {
        "lane": "scripted",
        "repo": "owner/repo",
        "start_sha": "start",
        "status": "blocked",
        "blockers": [
            {
                "kind": "missing_harness",
                "detail": "the session harness does not survive",
                "evidence": ["${SESSION_FILES}/harness.sh"],
                "resolution": "requires new Oracle engineering",
            }
        ],
        "missing_assets": [
            {"kind": "path", "value": "${SESSION_FILES}/harness.sh"}
        ],
    }
    blockers = MODULE.task_blockers(
        "task-1",
        entry,
        strict_oracle(),
        strict_task(),
        legacy_root=tmp_path,
        asset_root=tmp_path,
        session_files=files,
    )
    assert any("blocker_possibly_resolved" in item for item in blockers)


# ---------------------------------------------------------------------------
# Provenance / resume
# ---------------------------------------------------------------------------


def provenance_inputs(tmp_path, monkeypatch):
    repo = tmp_path / "repos/owner__repo"
    sha = make_git_repo(repo)
    stub_docker(
        monkeypatch,
        images={"golang:1.25-bookworm": "sha256:aaa"},
        volumes={"vol-mod", "vol-build"},
    )
    contract = base_contract()
    entry = contract["tasks"]["task-1"]
    task = strict_task(start_sha=sha)
    oracle = strict_oracle()
    generation = {"apply_ok": True, "patch_sha256": "abc", "changed_lines": 4}
    return entry, task, oracle, generation


def test_row_is_stale_on_patch_task_oracle_and_asset_changes(tmp_path, monkeypatch):
    entry, task, oracle, generation = provenance_inputs(tmp_path, monkeypatch)
    fields = MODULE.row_provenance_fields(
        task, oracle, "task-1", entry, generation, tmp_path, tmp_path, []
    )
    row = dict(fields)
    row["patch_sha256"] = "abc"
    assert not MODULE.row_is_stale(
        row, task, oracle, "task-1", entry, generation, tmp_path, tmp_path, []
    )

    other_patch = dict(generation, patch_sha256="def")
    assert MODULE.row_is_stale(
        row, task, oracle, "task-1", entry, other_patch, tmp_path, tmp_path, []
    )

    changed_task = dict(task, target_sha="other")
    assert MODULE.row_is_stale(
        row, changed_task, oracle, "task-1", entry, generation, tmp_path,
        tmp_path, [],
    )

    changed_oracle = dict(oracle, pass_to_pass=99)
    assert MODULE.row_is_stale(
        row, task, changed_oracle, "task-1", entry, generation, tmp_path,
        tmp_path, [],
    )

    assert MODULE.row_is_stale(
        row, task, oracle, "task-1", entry, generation, tmp_path, tmp_path,
        ["missing Docker image: golang:1.25-bookworm"],
    )


def test_replay_contract_fingerprint_tracks_adapter_and_asset_state(
    tmp_path, monkeypatch
):
    entry, _, _, _ = provenance_inputs(tmp_path, monkeypatch)
    before = MODULE.replay_contract_fingerprint(
        "task-1", entry, tmp_path, tmp_path, []
    )
    monkeypatch.setattr(MODULE, "docker_image_id", lambda image: "sha256:new")
    after = MODULE.replay_contract_fingerprint(
        "task-1", entry, tmp_path, tmp_path, []
    )
    assert before != after

    monkeypatch.setattr(MODULE, "ADAPTER_CONTRACT_VERSION", "v-next")
    bumped = MODULE.replay_contract_fingerprint(
        "task-1", entry, tmp_path, tmp_path, []
    )
    assert bumped != after


def test_model_component_is_collision_free():
    assert MODULE.model_component("a/b") != MODULE.model_component("a-b")
    assert MODULE.model_component("x") == MODULE.model_component("x")


# ---------------------------------------------------------------------------
# Raw-row persistence
# ---------------------------------------------------------------------------


def test_append_and_retract_raw_rows(tmp_path):
    legacy_dir = tmp_path / "legacy/correctness"
    MODULE.append_raw_row(legacy_dir, "gopy", {"id": "b", "model": "m"})
    MODULE.append_raw_row(legacy_dir, "gopy", {"id": "a", "model": "m"})
    rows = BASE.read_jsonl(legacy_dir / "gopy.jsonl")
    assert [row["id"] for row in rows] == ["a", "b"]

    assert MODULE.retract_raw_row(legacy_dir, "gopy", "a", "m") is True
    assert MODULE.retract_raw_row(legacy_dir, "gopy", "a", "m") is False
    rows = BASE.read_jsonl(legacy_dir / "gopy.jsonl")
    assert [row["id"] for row in rows] == ["b"]


def test_append_raw_row_rereads_from_disk_under_the_lane_lock(tmp_path):
    legacy_dir = tmp_path / "legacy/correctness"
    MODULE.append_raw_row(legacy_dir, "gopy", {"id": "a", "model": "m"})
    # Simulate a second process flushing a row this process never saw.
    write_jsonl(
        legacy_dir / "gopy.jsonl",
        [{"id": "a", "model": "m"}, {"id": "z", "model": "m"}],
    )
    MODULE.append_raw_row(legacy_dir, "gopy", {"id": "b", "model": "m"})
    rows = BASE.read_jsonl(legacy_dir / "gopy.jsonl")
    assert [row["id"] for row in rows] == ["a", "b", "z"]


def test_persist_evidence_copies_logs_and_deletes_the_scratch_tree(tmp_path):
    output = tmp_path / "out"
    scratch = tmp_path / "scratch"
    (scratch / "workspace/deep").mkdir(parents=True)
    (scratch / "run.log").write_text("log\n", encoding="utf-8")
    (scratch / "candidate.xml").write_text("<x/>", encoding="utf-8")
    (scratch / "workspace/deep/huge.bin").write_bytes(b"0" * 1024)
    preserved = MODULE.persist_evidence(output, "gopy", "m/1", "task-1", scratch)
    assert set(preserved) == {"run.log", "candidate.xml"}
    for relative in preserved.values():
        assert (output / relative).is_file()
    assert not scratch.exists()


# ---------------------------------------------------------------------------
# Staged generation loading
# ---------------------------------------------------------------------------


def test_load_staged_generation_requires_every_scoped_id(tmp_path):
    output = tmp_path / "out"
    path = MODULE.staged_generation_path(output, "m")
    write_jsonl(path, [{"id": "task-1", "model": "m", "apply_ok": True}])
    loaded = MODULE.load_staged_generation(output, "m", {"task-1"})
    assert set(loaded) == {"task-1"}
    with pytest.raises(ValueError, match="missing IDs"):
        MODULE.load_staged_generation(output, "m", {"task-1", "task-2"})


def test_load_staged_generation_rejects_a_mislabeled_model(tmp_path):
    output = tmp_path / "out"
    path = MODULE.staged_generation_path(output, "m")
    write_jsonl(path, [{"id": "task-1", "model": "other", "apply_ok": True}])
    with pytest.raises(ValueError, match="embedded model"):
        MODULE.load_staged_generation(output, "m", {"task-1"})


def test_load_staged_generation_requires_prepared_inputs(tmp_path):
    with pytest.raises(ValueError, match="staged generation results not found"):
        MODULE.load_staged_generation(tmp_path, "m", {"task-1"})


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------


def test_docker_run_command_honours_network_env_and_entrypoint():
    recipe = {"image": "img", "network": "host"}
    argv = MODULE.docker_run_command(
        recipe,
        mounts=["-v", "/a:/b"],
        env={"GOTOOLCHAIN": "local"},
        workdir="/work",
        command=["go", "test"],
        entrypoint="/bin/bash",
    )
    assert argv[:5] == ["docker", "run", "--rm", "--network", "host"]
    assert "--entrypoint" in argv and "/bin/bash" in argv
    assert "-e" in argv and "GOTOOLCHAIN=local" in argv
    assert argv[-3:] == ["img", "go", "test"]


def test_docker_run_command_defaults_to_an_isolated_network():
    argv = MODULE.docker_run_command(
        {"image": "img"}, mounts=[], env={}, workdir="/w", command=["x"]
    )
    assert argv[3:5] == ["--network", "none"]


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def test_replay_rejects_an_unsafe_model_id(tmp_path, capsys):
    code = MODULE.main(
        [
            "replay",
            "--tasks", str(RELEASE_TASKS),
            "--oracles", str(RELEASE_ORACLES),
            "--legacy-root", str(tmp_path),
            "--asset-root", str(tmp_path),
            "--output", str(tmp_path / "out"),
            "--model", "../evil",
        ]
    )
    assert code == 2
    assert "unsafe model ID" in capsys.readouterr().err


def preflight_contract(tmp_path):
    """A shipped-shape contract rewritten to reference only tmp_path assets."""
    contract = json.loads(CONTRACT_PATH.read_text())
    for entry in contract["tasks"].values():
        recipe = entry.get("recipe") or {}
        for mount in recipe.get("host_mounts") or []:
            mount["host"] = "${ASSET_ROOT}/absent-mount"
    path = tmp_path / "scoped-contract.json"
    path.write_text(json.dumps(contract), encoding="utf-8")
    return path


def test_preflight_fails_closed_without_assets(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(MODULE, "docker_available", lambda: False)
    monkeypatch.setattr(MODULE, "docker_image_id", lambda image: None)
    monkeypatch.setattr(MODULE, "docker_volume_present", lambda volume: False)
    monkeypatch.setattr(MODULE, "docker_volume_identity", lambda volume: None)
    monkeypatch.setattr(MODULE, "docker_file_sha256", lambda image, path: None)
    code = MODULE.main(
        [
            "preflight",
            "--tasks", str(RELEASE_TASKS),
            "--oracles", str(RELEASE_ORACLES),
            "--legacy-root", str(tmp_path),
            "--asset-root", str(tmp_path),
            "--contract", str(preflight_contract(tmp_path)),
            "--output", str(tmp_path / "out"),
        ]
    )
    assert code == 1
    report = json.loads(
        (tmp_path / "out/legacy-gopy-scripted-preflight.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["ready_task_count"] == 0
    assert report["blocked_task_count"] == report["scoped_task_count"] == 19
    capsys.readouterr()


def test_preflight_allow_blocked_exits_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(MODULE, "docker_available", lambda: False)
    monkeypatch.setattr(MODULE, "docker_image_id", lambda image: None)
    monkeypatch.setattr(MODULE, "docker_volume_present", lambda volume: False)
    monkeypatch.setattr(MODULE, "docker_volume_identity", lambda volume: None)
    monkeypatch.setattr(MODULE, "docker_file_sha256", lambda image, path: None)
    code = MODULE.main(
        [
            "preflight",
            "--tasks", str(RELEASE_TASKS),
            "--oracles", str(RELEASE_ORACLES),
            "--legacy-root", str(tmp_path),
            "--asset-root", str(tmp_path),
            "--contract", str(preflight_contract(tmp_path)),
            "--allow-blocked",
        ]
    )
    assert code == 0
    capsys.readouterr()


def asset_contract(tmp_path, **recipe_overrides):
    """A contract entry whose every asset exists under ``tmp_path``."""
    (tmp_path / "assets/slug").mkdir(parents=True, exist_ok=True)
    oracle_file = tmp_path / "assets/slug/oracle_test.go"
    oracle_file.write_text("package pkg\n", encoding="utf-8")
    mount = tmp_path / "mounts/home"
    (mount / "sub").mkdir(parents=True, exist_ok=True)
    (mount / "sub/file.txt").write_text("cache\n", encoding="utf-8")
    contract = base_contract()
    recipe = contract["tasks"]["task-1"]["recipe"]
    recipe["session_tests"] = [{
        "asset": "assets/slug/oracle_test.go",
        "sha256": MODULE.path_digest(oracle_file),
        "destination": "pkg/oracle_test.go",
    }]
    recipe["host_mounts"] = [{
        "host": "${ASSET_ROOT}/mounts/home",
        "container": "/root",
        "mode": "ro",
        "sha256": MODULE.path_digest(mount),
    }]
    recipe.update(recipe_overrides)
    return contract, oracle_file, mount


def restore_fixture(tmp_path, *, listed=True, digest_ok=True):
    import tarfile as _tarfile
    payload = tmp_path / "payload"
    payload.mkdir(exist_ok=True)
    (payload / "mod.txt").write_text("module\n", encoding="utf-8")
    (tmp_path / "caches").mkdir(exist_ok=True)
    archive = tmp_path / "caches/mod.tar.gz"
    with _tarfile.open(archive, "w:gz") as bundle:
        bundle.add(payload / "mod.txt", arcname="mod.txt")
    contract = base_contract()
    contract["tasks"]["task-1"]["recipe"]["gomodcache_archive"] = "caches/mod.tar.gz"
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    digest = MODULE.path_digest(archive, rehash=True)
    lines = []
    if listed:
        lines.append(f"{digest if digest_ok else '0' * 64}  caches/mod.tar.gz\n")
    (tmp_path / "SHA256SUMS").write_text("".join(lines), encoding="utf-8")
    return contract_path, archive, digest


def test_restore_caches_skips_a_volume_that_already_exists(
    tmp_path, monkeypatch, capsys
):
    contract_path, _, _ = restore_fixture(tmp_path)
    monkeypatch.setattr(MODULE, "docker_volume_present", lambda volume: True)
    code = MODULE.main([
        "restore-caches", "--asset-root", str(tmp_path),
        "--contract", str(contract_path),
    ])
    assert code == 0
    assert '"present"' in capsys.readouterr().out


def test_restore_caches_restores_both_phases_into_a_fresh_directory(
    tmp_path, monkeypatch, capsys
):
    """A fresh host has no Docker volumes: --target-dir must rebuild both the
    start and the golden cache from their pinned archives."""
    import tarfile as _tarfile

    contract_path, archive, digest = restore_fixture(tmp_path)
    golden_payload = tmp_path / "golden.txt"
    golden_payload.write_text("golden-module\n", encoding="utf-8")
    golden_archive = tmp_path / "caches/golden-mod.tar.gz"
    with _tarfile.open(golden_archive, "w:gz") as bundle:
        bundle.add(golden_payload, arcname="golden.txt")
    golden_digest = MODULE.path_digest(golden_archive, rehash=True)
    sums = (tmp_path / "SHA256SUMS").read_text(encoding="utf-8")
    (tmp_path / "SHA256SUMS").write_text(
        sums + f"{golden_digest}  caches/golden-mod.tar.gz\n", encoding="utf-8"
    )
    contract = json.loads(contract_path.read_text())
    entry = contract["tasks"]["task-1"]
    entry["recipe"]["gomodcache_archive_sha256"] = digest
    entry["oracle_validation"] = {
        "golden_source_paths": ["pkg/prod.go"],
        "mode": "start_and_source_only_golden",
        "golden_recipe_overrides": {
            "gomodcache_archive": "caches/golden-mod.tar.gz",
            "gomodcache_archive_sha256": golden_digest,
        },
    }
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    monkeypatch.setattr(MODULE, "docker_volume_present", lambda volume: False)
    monkeypatch.setattr(MODULE, "docker_available", lambda: False)
    target = tmp_path / "restored"
    code = MODULE.main([
        "restore-caches", "--asset-root", str(tmp_path),
        "--contract", str(contract_path), "--target-dir", str(target),
    ])
    assert code == 0
    actions = json.loads(capsys.readouterr().out)["actions"]
    phases = {action["phase"]: action for action in actions}
    assert set(phases) == {"start", "golden"}
    assert all(action["action"] == "restored_to_dir" for action in actions)
    assert (Path(phases["start"]["directory"]) / "mod.txt").is_file()
    assert (Path(phases["golden"]["directory"]) / "golden.txt").is_file()
    assert phases["start"]["sha256"] == digest
    assert phases["golden"]["sha256"] == golden_digest


def test_seed_cache_directory_verifies_the_pinned_digest(tmp_path):
    import tarfile as _tarfile

    payload = tmp_path / "m.txt"
    payload.write_text("m\n", encoding="utf-8")
    archive = tmp_path / "m.tar.gz"
    with _tarfile.open(archive, "w:gz") as bundle:
        bundle.add(payload, arcname="m.txt")
    digest = MODULE.path_digest(archive, rehash=True)
    seeded = tmp_path / "seeded"
    assert MODULE.seed_cache_directory(
        archive, seeded, expected_sha256=digest
    ) == digest
    assert (seeded / "m.txt").read_text(encoding="utf-8") == "m\n"
    with pytest.raises(RuntimeError, match="not the contract-pinned"):
        MODULE.seed_cache_directory(archive, seeded, expected_sha256="0" * 64)


def test_prepare_cache_mounts_seeds_a_disposable_dir_and_forces_go_offline(tmp_path):
    import tarfile as _tarfile

    (tmp_path / "caches").mkdir()
    payload = tmp_path / "m.txt"
    payload.write_text("m\n", encoding="utf-8")
    archive = tmp_path / "caches/mod.tar.gz"
    with _tarfile.open(archive, "w:gz") as bundle:
        bundle.add(payload, arcname="m.txt")
    recipe = {
        "kind": "go_docker_session_oracle",
        "gomodcache_volume": "shared-volume",
        "gomodcache_archive": "caches/mod.tar.gz",
        "gomodcache_archive_sha256": MODULE.path_digest(archive, rehash=True),
        "env": {"CGO_ENABLED": "0", "GOFLAGS": "-mod=mod", "GOPROXY": "https://proxy"},
    }
    prepared = MODULE.prepare_cache_mounts(
        recipe, tmp_path / "scratch", asset_root=tmp_path
    )
    assert prepared["gomodcache_volume"] is None
    seeded = Path(prepared["gomodcache_dir"])
    assert (seeded / "m.txt").is_file()
    # offline enforcement wins over whatever the recipe declared
    assert prepared["env"]["GOPROXY"] == "off"
    assert prepared["env"]["GOSUMDB"] == "off"
    assert prepared["env"]["GOFLAGS"] == "-mod=readonly"
    assert prepared["env"]["CGO_ENABLED"] == "0"
    mounts = MODULE.oracle_mounts(prepared, tmp_path / "ws", tmp_path / "reports")
    assert f"{seeded}:/go/pkg/mod" in mounts
    assert not any("shared-volume" in mount for mount in mounts)


# ---------------------------------------------------------------------------
# Live staged-patch verification
# ---------------------------------------------------------------------------


def staged_generation(tmp_path, body="diff --git a/x b/x\n"):
    patch = tmp_path / "candidate.patch"
    patch.write_text(body, encoding="utf-8")
    return {
        "apply_ok": True,
        "patch_file": str(patch),
        "patch_sha256": MODULE.file_content_digest(patch, rehash=True),
        "changed_lines": 1,
    }, patch


def test_verify_staged_patch_accepts_an_intact_patch(tmp_path):
    generation, _ = staged_generation(tmp_path)
    digest, problem = MODULE.verify_staged_patch(generation)
    assert problem is None
    assert digest == generation["patch_sha256"]


def test_verify_staged_patch_detects_a_rewritten_patch(tmp_path):
    generation, patch = staged_generation(tmp_path)
    patch.write_text("diff --git a/y b/y\n", encoding="utf-8")
    digest, problem = MODULE.verify_staged_patch(generation)
    assert "not the staged patch_sha256" in problem
    assert digest != generation["patch_sha256"]


def test_verify_staged_patch_detects_a_missing_patch(tmp_path):
    generation, patch = staged_generation(tmp_path)
    patch.unlink()
    digest, problem = MODULE.verify_staged_patch(generation)
    assert digest is None
    assert "missing" in problem


def test_verify_staged_patch_requires_a_recorded_digest(tmp_path):
    generation, _ = staged_generation(tmp_path)
    generation.pop("patch_sha256")
    _, problem = MODULE.verify_staged_patch(generation)
    assert "carries no patch_sha256" in problem


def test_verify_staged_patch_ignores_a_candidate_that_never_applied(tmp_path):
    generation, patch = staged_generation(tmp_path)
    generation["apply_ok"] = False
    patch.unlink()
    assert MODULE.verify_staged_patch(generation) == (None, None)


def test_row_is_stale_on_live_patch_drift(tmp_path, monkeypatch):
    entry, task, oracle, _ = provenance_inputs(tmp_path, monkeypatch)
    generation, patch = staged_generation(tmp_path)
    fields = MODULE.row_provenance_fields(
        task, oracle, "task-1", entry, generation, tmp_path, tmp_path, []
    )
    row = dict(fields)
    row["patch_sha256"] = generation["patch_sha256"]
    assert not MODULE.row_is_stale(
        row, task, oracle, "task-1", entry, generation, tmp_path, tmp_path, []
    )
    # the file changes but the generation row still claims the old digest
    patch.write_text("diff --git a/z b/z\n", encoding="utf-8")
    assert MODULE.row_is_stale(
        row, task, oracle, "task-1", entry, generation, tmp_path, tmp_path, []
    )


def test_row_provenance_binds_the_live_patch_digest(tmp_path, monkeypatch):
    entry, task, oracle, _ = provenance_inputs(tmp_path, monkeypatch)
    generation, patch = staged_generation(tmp_path)
    first = MODULE.row_provenance_fields(
        task, oracle, "task-1", entry, generation, tmp_path, tmp_path, []
    )
    assert first["patch_file_sha256"] == generation["patch_sha256"]
    patch.write_text("diff --git a/q b/q\n", encoding="utf-8")
    second = MODULE.row_provenance_fields(
        task, oracle, "task-1", entry, generation, tmp_path, tmp_path, []
    )
    assert second["patch_file_sha256"] != first["patch_file_sha256"]


def test_offline_http_fixtures_are_part_of_the_asset_inventory(tmp_path, monkeypatch):
    MODULE.load_digest_cache(tmp_path)
    contract, _, _ = asset_contract(tmp_path)
    root = tmp_path / "assets/slug/fixtures"
    root.mkdir(parents=True)
    (root / "f.yaml").write_text("k: v\n", encoding="utf-8")
    for name in ("tls.crt", "tls.key", "server.go"):
        (tmp_path / "assets/slug" / name).write_text(name, encoding="utf-8")
    recipe = contract["tasks"]["task-1"]["recipe"]
    recipe["offline_http_fixtures"] = {
        "hosts": ["example.invalid"],
        "root": "assets/slug/fixtures",
        "root_sha256": MODULE.path_digest(root),
        "cert": "assets/slug/tls.crt",
        "cert_sha256": MODULE.path_digest(tmp_path / "assets/slug/tls.crt"),
        "key": "assets/slug/tls.key",
        "key_sha256": MODULE.path_digest(tmp_path / "assets/slug/tls.key"),
        "server": "assets/slug/server.go",
        "server_sha256": MODULE.path_digest(tmp_path / "assets/slug/server.go"),
    }
    inventory = MODULE.asset_inventory(
        contract["tasks"]["task-1"], asset_root=tmp_path, legacy_root=tmp_path
    )
    roles = {record["role"] for record in inventory}
    assert {
        "offline_http_fixture_root", "offline_http_fixture_cert",
        "offline_http_fixture_key", "offline_http_fixture_server",
    } <= roles
    _, problems = MODULE.verify_assets(inventory)
    assert not [p for p in problems if "offline_http_fixture_" in p.split(" asset")[0]]
    argv = MODULE.docker_run_command(
        recipe, mounts=[], env={}, workdir="/w", command=["x"]
    )
    assert "--add-host" in argv and "example.invalid:127.0.0.1" in argv


def test_every_go_recipe_is_pinned_by_an_archive_not_a_volume():
    contract = MODULE.load_contract(CONTRACT_PATH)
    for task_id, entry in contract["tasks"].items():
        if entry["status"] != "recovered":
            continue
        recipe = entry["recipe"]
        if recipe["kind"] not in {"go_docker_p2p", "go_docker_session_oracle"}:
            continue
        assert recipe.get("gomodcache_archive"), task_id
        assert len(recipe.get("gomodcache_archive_sha256") or ""), task_id
        overrides = (entry.get("oracle_validation") or {}).get(
            "golden_recipe_overrides"
        ) or {}
        if overrides.get("gomodcache_volume") or overrides.get("gomodcache_archive"):
            assert overrides.get("gomodcache_archive"), task_id
            assert len(overrides.get("gomodcache_archive_sha256") or ""), task_id


# ---------------------------------------------------------------------------
# Single source of truth for execution-relevant assets
# ---------------------------------------------------------------------------


def test_asset_inventory_covers_every_execution_input(tmp_path):
    MODULE.load_digest_cache(tmp_path)
    contract, _, _ = asset_contract(tmp_path)
    entry = contract["tasks"]["task-1"]
    entry["oracle_validation"] = {
        "golden_source_paths": ["pkg/prod.go"],
        "mode": "start_and_source_only_golden",
        "golden_session_tests": [{
            "asset": "assets/slug/oracle_test.go", "sha256": "x",
            "destination": "pkg/oracle_test.go",
        }],
        "golden_patch_asset": "assets/slug/golden.patch",
        "golden_patch_sha256": "y",
    }
    inventory = MODULE.asset_inventory(
        entry, asset_root=tmp_path, legacy_root=tmp_path
    )
    roles = {record["role"] for record in inventory}
    assert {
        "golden_patch", "host_mount", "docker_image", "gomodcache_volume",
        "repo_checkout",
    } <= roles
    assert "gobuildcache_volume" not in roles
    # the Oracle source is present exactly once even though the golden phase
    # declares the same file (the inventory is deduplicated by location)
    assert roles & {"oracle_source", "golden_oracle_source"}
    locations = [record["location"] for record in inventory]
    assert len(locations) == len(set(locations))


def test_asset_inventory_is_empty_for_a_blocked_entry(tmp_path):
    entry = {"status": "blocked", "blockers": [{"kind": "x", "detail": "y"}]}
    assert MODULE.asset_inventory(
        entry, asset_root=tmp_path, legacy_root=tmp_path
    ) == []


def test_verify_assets_rejects_an_unpinned_asset(tmp_path, monkeypatch):
    MODULE.load_digest_cache(tmp_path)
    stub_docker(monkeypatch, images={"golang:1.25-bookworm": "sha256:aaa"},
                volumes={"vol-mod", "vol-build"})
    contract, oracle_file, _ = asset_contract(tmp_path)
    contract["tasks"]["task-1"]["recipe"]["session_tests"][0].pop("sha256")
    inventory = MODULE.asset_inventory(
        contract["tasks"]["task-1"], asset_root=tmp_path, legacy_root=tmp_path
    )
    _, problems = MODULE.verify_assets(inventory)
    assert any("no recorded SHA-256" in problem for problem in problems)


def test_verify_assets_detects_a_tampered_mount_tree(tmp_path, monkeypatch):
    MODULE.load_digest_cache(tmp_path)
    stub_docker(monkeypatch, images={"golang:1.25-bookworm": "sha256:aaa"},
                volumes={"vol-mod", "vol-build"})
    contract, _, mount = asset_contract(tmp_path)
    entry = contract["tasks"]["task-1"]
    _, clean = MODULE.verify_assets(
        MODULE.asset_inventory(entry, asset_root=tmp_path, legacy_root=tmp_path)
    )
    assert not [p for p in clean if "host_mount" in p]
    (mount / "sub/file.txt").write_text("tampered\n", encoding="utf-8")
    MODULE.load_digest_cache(tmp_path)
    _, problems = MODULE.verify_assets(
        MODULE.asset_inventory(entry, asset_root=tmp_path, legacy_root=tmp_path),
        rehash=True,
    )
    assert any("host_mount asset" in problem and "not the recorded" in problem
               for problem in problems)


def test_verify_assets_detects_a_missing_oracle_source(tmp_path, monkeypatch):
    MODULE.load_digest_cache(tmp_path)
    stub_docker(monkeypatch, images={"golang:1.25-bookworm": "sha256:aaa"},
                volumes={"vol-mod", "vol-build"})
    contract, oracle_file, _ = asset_contract(tmp_path)
    oracle_file.unlink()
    _, problems = MODULE.verify_assets(
        MODULE.asset_inventory(
            contract["tasks"]["task-1"], asset_root=tmp_path, legacy_root=tmp_path
        )
    )
    assert any("missing oracle_source asset" in problem for problem in problems)


def test_tree_digest_is_symlink_aware_and_content_sensitive(tmp_path):
    MODULE.load_digest_cache(tmp_path)
    tree = tmp_path / "tree"
    (tree / "d").mkdir(parents=True)
    (tree / "d/a.txt").write_text("a\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    link = tree / "link"
    link.symlink_to(outside)
    first = MODULE.tree_content_digest(tree)
    # Re-pointing the symlink changes the digest even though the *target*
    # content is identical, and the target is never followed.
    link.unlink()
    link.symlink_to(tmp_path / "other.txt")
    assert MODULE.tree_content_digest(tree) != first
    # Content changes are detected.
    link.unlink()
    link.symlink_to(outside)
    assert MODULE.tree_content_digest(tree) == first
    (tree / "d/a.txt").write_text("b\n", encoding="utf-8")
    assert MODULE.tree_content_digest(tree, rehash=True) != first


def test_file_digest_of_a_symlink_never_follows_it(tmp_path):
    MODULE.load_digest_cache(tmp_path)
    target = tmp_path / "t.txt"
    target.write_text("payload\n", encoding="utf-8")
    link = tmp_path / "l"
    link.symlink_to(target)
    assert MODULE.file_content_digest(link).startswith("symlink:")
    assert MODULE.file_content_digest(link) != MODULE.file_content_digest(target)


def test_asset_state_binds_every_asset_into_the_fingerprint(tmp_path, monkeypatch):
    MODULE.load_digest_cache(tmp_path)
    stub_docker(monkeypatch, images={"golang:1.25-bookworm": "sha256:aaa"},
                volumes={"vol-mod", "vol-build"})
    contract, oracle_file, mount = asset_contract(tmp_path)
    entry = contract["tasks"]["task-1"]
    before = MODULE.replay_contract_fingerprint(
        "task-1", entry, tmp_path, tmp_path, []
    )
    (mount / "sub/file.txt").write_text("changed\n", encoding="utf-8")
    MODULE.load_digest_cache(tmp_path)
    after = MODULE.replay_contract_fingerprint(
        "task-1", entry, tmp_path, tmp_path, []
    )
    assert before != after


def test_isolate_mutable_mounts_copies_only_writable_trees(tmp_path):
    source_ro = tmp_path / "ro"
    source_ro.mkdir()
    (source_ro / "f").write_text("ro\n", encoding="utf-8")
    source_rw = tmp_path / "rw"
    source_rw.mkdir()
    (source_rw / "f").write_text("rw\n", encoding="utf-8")
    recipe = {"host_mounts": [
        {"host": str(source_ro), "container": "/ro"},
        {"host": str(source_rw), "container": "/rw", "mutable": True},
    ]}
    scratch = tmp_path / "scratch"
    isolated = MODULE.isolate_mutable_mounts(
        recipe, scratch, asset_root=tmp_path, legacy_root=tmp_path
    )
    mounts = isolated["host_mounts"]
    assert mounts[0]["host"] == str(source_ro)
    assert mounts[0]["mode"] == "ro"
    assert mounts[1]["host"] != str(source_rw)
    assert Path(mounts[1]["host"], "f").read_text(encoding="utf-8") == "rw\n"
    # Writing through the isolated copy leaves the pinned source untouched.
    Path(mounts[1]["host"], "f").write_text("mutated\n", encoding="utf-8")
    assert (source_rw / "f").read_text(encoding="utf-8") == "rw\n"


def test_immutable_asset_digests_skips_mutable_mounts(tmp_path, monkeypatch):
    MODULE.load_digest_cache(tmp_path)
    stub_docker(monkeypatch, images={"golang:1.25-bookworm": "sha256:aaa"},
                volumes={"vol-mod", "vol-build"})
    contract, _, mount = asset_contract(tmp_path)
    entry = contract["tasks"]["task-1"]
    entry["recipe"]["host_mounts"][0]["mutable"] = True
    digests = MODULE.immutable_asset_digests(
        entry, asset_root=tmp_path, legacy_root=tmp_path
    )
    assert not any(str(mount) in key for key in digests)


def test_infra_row_never_carries_a_correctness_verdict():
    row = MODULE.infra_row("t", "m", "gopy", "sha", {"patch_sha256": "p"}, "boom")
    assert row["status"] == MODULE.INFRA_RETRY
    assert row["correctness_ok"] is None
    normalized = BASE.normalized_record(
        "gopy", row, raw_source="x", source_kind="published_legacy",
        oracle=strict_oracle(), generation={"apply_ok": True, "patch_sha256": "p"},
    )
    assert normalized["status"] == "infra_retry"
    assert normalized["retryable"] is True


# ---------------------------------------------------------------------------
# validate-oracle must never auto-pass a historical mode
# ---------------------------------------------------------------------------


def test_no_contract_entry_claims_a_non_executing_validation_mode():
    contract = MODULE.load_contract(CONTRACT_PATH)
    banned = {
        "historical_harness",
        "historical_published",
        "historical_target_commit_golden",
        "attested_external_validation",
    }
    for task_id, entry in contract["tasks"].items():
        if entry["status"] != "recovered":
            continue
        mode = (entry.get("oracle_validation") or {}).get("mode")
        assert mode not in banned, f"{task_id} claims non-executing mode {mode}"


def test_validate_source_module_has_no_auto_pass_branch():
    source = (SCRIPTS_DIR / "run_legacy_gopy_scripted_correctness.py").read_text()
    assert '"attested": True' not in source
    assert "oracle_ok\": True," not in source.replace(" ", "")


def test_anchor_lines_for_matches_module_relative_coverage_paths():
    anchors = {"pkg/parser/ast/misc.go": [10, 11]}
    assert MODULE.anchor_lines_for(anchors, "ast/misc.go") == [10, 11]
    assert MODULE.anchor_lines_for(anchors, "pkg/parser/ast/misc.go") == [10, 11]
    assert MODULE.anchor_lines_for(anchors, "other.go") == []


def test_every_recovered_entry_declares_measured_coverage():
    contract = MODULE.load_contract(CONTRACT_PATH)
    for task_id, entry in contract["tasks"].items():
        if entry["status"] != "recovered":
            continue
        coverage = entry["recipe"].get("coverage") or {}
        assert coverage.get("kind") in {
            "go_cover", "py_cover", "py_settrace", "bash_xtrace", "attested"
        }, f"{task_id} has unmeasured coverage kind {coverage.get('kind')}"


def test_nomad_is_blocked_and_non_scoring():
    contract = MODULE.load_contract(CONTRACT_PATH)
    entry = contract["tasks"][
        "github__hashicorp__nomad__pull_16383__comment_1137745682__modularity"
    ]
    assert entry["status"] == "blocked"
    assert entry["blockers"][0]["kind"] == "unmeasurable_changed_old_coverage"
    # the engineered Oracle is retained, but not as an executable recipe
    assert "recipe" not in entry
    assert entry["engineered_oracle_not_scoring"]["kind"]


# ---------------------------------------------------------------------------
# restore-caches safety
# ---------------------------------------------------------------------------


def test_restore_caches_requires_sha256sums_membership(tmp_path, monkeypatch, capsys):
    contract_path, _, _ = restore_fixture(tmp_path, listed=False)
    monkeypatch.setattr(MODULE, "docker_volume_present", lambda volume: False)
    code = MODULE.main([
        "restore-caches", "--asset-root", str(tmp_path),
        "--contract", str(contract_path),
    ])
    assert code == 2
    assert "not listed in" in capsys.readouterr().err


def test_restore_caches_rejects_a_tampered_archive(tmp_path, monkeypatch, capsys):
    contract_path, _, _ = restore_fixture(tmp_path, digest_ok=False)
    monkeypatch.setattr(MODULE, "docker_volume_present", lambda volume: False)
    code = MODULE.main([
        "restore-caches", "--asset-root", str(tmp_path),
        "--contract", str(contract_path),
    ])
    assert code == 2
    assert "not the SHA256SUMS entry" in capsys.readouterr().err


def test_restore_caches_rejects_a_contract_digest_mismatch(tmp_path, monkeypatch, capsys):
    contract_path, _, digest = restore_fixture(tmp_path)
    contract = json.loads(contract_path.read_text())
    contract["tasks"]["task-1"]["recipe"]["gomodcache_archive_sha256"] = "1" * 64
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    monkeypatch.setattr(MODULE, "docker_volume_present", lambda volume: False)
    code = MODULE.main([
        "restore-caches", "--asset-root", str(tmp_path),
        "--contract", str(contract_path),
    ])
    assert code == 2
    assert "not the contract-pinned" in capsys.readouterr().err


def test_restore_caches_missing_archive_is_reported(tmp_path, monkeypatch, capsys):
    contract_path, archive, digest = restore_fixture(tmp_path)
    archive.unlink()
    monkeypatch.setattr(MODULE, "docker_volume_present", lambda volume: False)
    code = MODULE.main([
        "restore-caches", "--asset-root", str(tmp_path),
        "--contract", str(contract_path),
    ])
    assert code == 2
    err = capsys.readouterr().err
    assert "missing packaged cache archive" in err or "not listed in" in err


def test_restore_caches_dry_run_needs_no_docker_and_no_network(tmp_path, monkeypatch, capsys):
    contract_path, _, digest = restore_fixture(tmp_path)
    monkeypatch.setattr(MODULE, "docker_volume_present", lambda volume: False)
    monkeypatch.setattr(MODULE, "docker_available", lambda: False)

    def _forbidden(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("restore-caches must not shell out in dry-run")

    monkeypatch.setattr(MODULE.subprocess, "run", _forbidden)
    code = MODULE.main([
        "restore-caches", "--asset-root", str(tmp_path),
        "--contract", str(contract_path), "--dry-run",
    ])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["actions"][0]["action"] == "would_restore"
    assert payload["actions"][0]["sha256"] == digest


def test_safe_extract_tar_rejects_path_traversal(tmp_path):
    import tarfile as _tarfile
    archive = tmp_path / "evil.tar"
    payload = tmp_path / "f.txt"
    payload.write_text("x\n", encoding="utf-8")
    with _tarfile.open(archive, "w") as bundle:
        bundle.add(payload, arcname="../escape.txt")
    with pytest.raises(ValueError, match="unsafe member path"):
        MODULE.safe_extract_tar(archive, tmp_path / "dest")


def test_safe_extract_tar_rejects_absolute_and_escaping_symlinks(tmp_path):
    import tarfile as _tarfile
    archive = tmp_path / "evil2.tar"
    with _tarfile.open(archive, "w") as bundle:
        info = _tarfile.TarInfo("link")
        info.type = _tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        bundle.addfile(info)
    with pytest.raises(ValueError, match="unsafe link target"):
        MODULE.safe_extract_tar(archive, tmp_path / "dest2")


def test_safe_extract_tar_extracts_a_clean_archive(tmp_path):
    import tarfile as _tarfile
    payload = tmp_path / "ok.txt"
    payload.write_text("fine\n", encoding="utf-8")
    archive = tmp_path / "ok.tar"
    with _tarfile.open(archive, "w") as bundle:
        bundle.add(payload, arcname="pkg/ok.txt")
    destination = tmp_path / "dest3"
    assert MODULE.safe_extract_tar(archive, destination) == 1
    assert (destination / "pkg/ok.txt").read_text(encoding="utf-8") == "fine\n"


def test_restore_caches_uses_no_unpinned_container_image():
    source = (SCRIPTS_DIR / "run_legacy_gopy_scripted_correctness.py").read_text()
    assert '"alpine"' not in source
    assert "alpine" not in source


def test_restore_then_run_offline_end_to_end(tmp_path, monkeypatch):
    """Restore a module cache into an empty target and prove a Go build can
    run offline against it.

    This is the fresh-host path end to end: an empty destination, an archive
    verified against SHA256SUMS *and* the contract digest, extraction with the
    safe extractor, then a real ``go build`` in the pinned image with the
    network disabled and ``GOPROXY=off`` -- so a missing module cannot be
    papered over by a live fetch. The module is vendored into the archive by
    the test itself, so no network is needed to build the fixture either.
    """
    import shutil as _shutil
    import tarfile as _tarfile

    if _shutil.which("docker") is None:
        pytest.skip("docker is required for the end-to-end restore check")
    image = "golang:1.25-bookworm"
    if MODULE.docker_image_id(image) is None:
        pytest.skip(f"{image} is not available locally")

    # A tiny module cache holding one dependency, laid out the way Go expects.
    cache = tmp_path / "modcache"
    dep = cache / "cache/download/example.com/dep/@v"
    dep.mkdir(parents=True)
    mod_body = "module example.com/dep\n\ngo 1.16\n"
    src = tmp_path / "depsrc/dep@v1.0.0"
    src.mkdir(parents=True)
    (src / "go.mod").write_text(mod_body, encoding="utf-8")
    (src / "dep.go").write_text(
        "package dep\n\nfunc Answer() int { return 42 }\n", encoding="utf-8"
    )
    import zipfile

    zip_path = dep / "v1.0.0.zip"
    with zipfile.ZipFile(zip_path, "w") as bundle:
        for name in ("go.mod", "dep.go"):
            bundle.write(src / name, f"example.com/dep@v1.0.0/{name}")
    (dep / "v1.0.0.mod").write_text(mod_body, encoding="utf-8")
    (dep / "v1.0.0.info").write_text('{"Version":"v1.0.0"}', encoding="utf-8")
    (dep / "list").write_text("v1.0.0\n", encoding="utf-8")

    (tmp_path / "caches").mkdir()
    archive = tmp_path / "caches/mod.tar.gz"
    with _tarfile.open(archive, "w:gz") as bundle:
        bundle.add(cache, arcname=".")
    digest = MODULE.path_digest(archive, rehash=True)
    (tmp_path / "SHA256SUMS").write_text(
        f"{digest}  caches/mod.tar.gz\n", encoding="utf-8"
    )
    contract = base_contract()
    recipe = contract["tasks"]["task-1"]["recipe"]
    recipe["gomodcache_archive"] = "caches/mod.tar.gz"
    recipe["gomodcache_archive_sha256"] = digest
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")

    monkeypatch.setattr(MODULE, "docker_volume_present", lambda volume: False)
    target = tmp_path / "restored"
    assert MODULE.main([
        "restore-caches", "--asset-root", str(tmp_path),
        "--contract", str(contract_path), "--target-dir", str(target),
    ]) == 0
    restored = next(target.iterdir())
    assert (restored / "cache/download/example.com/dep/@v/v1.0.0.zip").is_file()

    # A workspace that can only build if the restored cache is complete.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "go.mod").write_text(
        "module example.com/app\n\ngo 1.16\n\nrequire example.com/dep v1.0.0\n",
        encoding="utf-8",
    )
    (workspace / "main.go").write_text(
        'package main\n\nimport (\n\t"fmt"\n\n\t"example.com/dep"\n)\n\n'
        'func main() { fmt.Println(dep.Answer()) }\n',
        encoding="utf-8",
    )
    prepared = MODULE.prepare_cache_mounts(
        {"kind": "go_docker_p2p", "image": image, "network": "none",
         "gomodcache_archive": "caches/mod.tar.gz",
         "gomodcache_archive_sha256": digest},
        tmp_path / "scratch", asset_root=tmp_path,
    )
    assert prepared["env"]["GOPROXY"] == "off"
    argv = MODULE.docker_run_command(
        prepared,
        mounts=MODULE.oracle_mounts(
            prepared, workspace, tmp_path / "reports"
        ),
        env={**prepared["env"], "GOFLAGS": "-mod=mod"},
        workdir="/work",
        command=["go", "run", "."],
        entrypoint=None,
    )
    (tmp_path / "reports").mkdir(exist_ok=True)
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=900)
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "42" in completed.stdout
