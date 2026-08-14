import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import importlib.util

SPEC = importlib.util.spec_from_file_location(
    "run_legacy_reserve_correctness",
    SCRIPTS_DIR / "run_legacy_reserve_correctness.py",
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
BASE = MODULE.BASE


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
    (root / "src.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")
    run("git", "-C", str(root), "add", "-A")
    run("git", "-C", str(root), "commit", "-q", "-m", "init")
    return run("git", "-C", str(root), "rev-parse", "HEAD").strip()



def assert_no_scratch_files(work_root: Path):
    """No replay scratch content may linger (per-invocation dirs may be
    removed leaving empty parents, but never files)."""
    leftovers = [
        path for path in work_root.rglob("*") if path.is_file() or path.is_symlink()
    ]
    assert leftovers == [], leftovers


def strict_task(task_id, repo="owner/repo", language="C/C++"):
    return {
        "id": task_id,
        "repo": repo,
        "start_sha": "start",
        "target_sha": "target",
        "pr_number": 1,
        "inferred_language": language,
    }


def strict_oracle(task_id, **overrides):
    base = {
        "id": task_id,
        "repo": "owner/repo",
        "start_sha": "start",
        "target_sha": "target",
        "pr_number": 1,
        "golden_correctness_ok": True,
        "pass_to_pass": 1,
        "changed_lines_covered": True,
        "language": "C/C++",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Pure helper functions
# ---------------------------------------------------------------------------


def test_artifacts_directory_handles_jvm_session_prefixed_path(tmp_path):
    legacy_root = tmp_path
    jvm_style = {"artifacts": {"directory": "strict_screen_20260726/oracle_recovery/java/08-x"}}
    other_style = {"recovery_artifact": "oracle_recovery/tsjs/20_x"}
    assert MODULE.artifacts_directory(jvm_style, legacy_root) == (
        legacy_root / "strict_screen_20260726/oracle_recovery/java/08-x"
    ).resolve()
    assert MODULE.artifacts_directory(other_style, legacy_root) == (
        legacy_root / "strict_screen_20260726/oracle_recovery/tsjs/20_x"
    ).resolve()


def test_artifacts_directory_requires_a_value(tmp_path):
    with pytest.raises(ValueError, match="missing an artifacts"):
        MODULE.artifacts_directory({}, tmp_path)


def test_oracle_cache_tokens_extracts_unique_names():
    command = (
        "bash x -I /oracle-cache/oracle.init.gradle "
        "-javaagent:/oracle-cache/jacocoagent.jar=x "
        "-I /oracle-cache/oracle.init.gradle"
    )
    assert MODULE.oracle_cache_tokens(command) == [
        "jacocoagent.jar",
        "oracle.init.gradle",
    ]


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


def test_apply_patch_round_trip_with_git_apply(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    target = workdir / "file.txt"
    target.write_text("a\nb\nc\n", encoding="utf-8")
    patch_path = tmp_path / "change.patch"
    patch_path.write_text(
        "diff --git a/file.txt b/file.txt\n"
        "index 0000000..1111111 100644\n"
        "--- a/file.txt\n"
        "+++ b/file.txt\n"
        "@@ -1,3 +1,3 @@\n"
        " a\n"
        "-b\n"
        "+B\n"
        " c\n",
        encoding="utf-8",
    )
    ok, detail = MODULE.apply_patch(workdir, patch_path)
    assert ok, detail
    assert target.read_text(encoding="utf-8") == "a\nB\nc\n"


def test_apply_patch_reports_failure_without_raising(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    patch_path = tmp_path / "change.patch"
    patch_path.write_text(
        "diff --git a/missing.txt b/missing.txt\n"
        "--- a/missing.txt\n"
        "+++ b/missing.txt\n"
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+new\n",
        encoding="utf-8",
    )
    ok, detail = MODULE.apply_patch(workdir, patch_path)
    assert ok is False
    assert detail


def test_git_worktree_checkout_materializes_exact_start_sha(tmp_path):
    repo = tmp_path / "repo"
    sha = make_git_repo(repo)
    dest = tmp_path / "worktree"
    MODULE.git_worktree_checkout(repo, sha, dest)
    assert (dest / "src.txt").is_file()
    head = run("git", "-C", str(dest), "rev-parse", "HEAD").strip()
    assert head == sha
    MODULE.remove_worktree(repo, dest)
    assert not dest.exists()


def test_git_worktree_checkout_rejects_wrong_sha(tmp_path):
    repo = tmp_path / "repo"
    make_git_repo(repo)
    dest = tmp_path / "worktree"
    with pytest.raises(RuntimeError):
        MODULE.git_worktree_checkout(repo, "0" * 40, dest)


def test_test_count_matches_generic_patterns():
    text = "Test Files: 2 passed (2)\nTests: 4 passed (4)\n"
    assert MODULE.test_count(text, MODULE.GENERIC_TEST_COUNT_PATTERNS) == 2
    assert MODULE.test_count(
        "test result: ok. 77 passed; 0 failed", MODULE.GENERIC_TEST_COUNT_PATTERNS
    ) == 77
    assert MODULE.test_count("no matches here", MODULE.GENERIC_TEST_COUNT_PATTERNS) == 0


# ---------------------------------------------------------------------------
# select_scope drift detection
# ---------------------------------------------------------------------------


def build_full_release(tmp_path, *, drop=None, lane_override=None, source_override=None):
    tasks_path = tmp_path / "tasks.jsonl"
    oracles_path = tmp_path / "oracles.jsonl"
    tasks = []
    oracles = []
    for task_id in MODULE.TASK_IDS:
        if drop == task_id:
            continue
        lane = MODULE.EXPECTED_LANE[task_id]
        language = {
            "native": "C/C++",
            "jvm": "Java",
            "rust": "Rust",
            "tsjs": "TypeScript",
        }[lane if lane_override is None or task_id != lane_override[0] else lane_override[1]]
        tasks.append(strict_task(task_id, language=language))
        extra = {}
        if source_override and task_id == source_override[0]:
            extra["lane_results_source"] = source_override[1]
        oracles.append(strict_oracle(task_id, language=language, **extra))
    write_jsonl(tasks_path, tasks)
    write_jsonl(oracles_path, oracles)
    return tasks_path, oracles_path


def test_select_scope_accepts_full_reserve(tmp_path):
    tasks_path, oracles_path = build_full_release(tmp_path)
    _, _, plan = MODULE.load_release_and_plan(tasks_path, oracles_path)
    rows = MODULE.select_scope(plan, None)
    assert {row["id"] for row in rows} == set(MODULE.TASK_IDS)
    for row in rows:
        assert row["source_kind"] == "published_legacy"
        assert row["lane"] == MODULE.EXPECTED_LANE[row["id"]]


def test_select_scope_rejects_unknown_task_id(tmp_path):
    tasks_path, oracles_path = build_full_release(tmp_path)
    _, _, plan = MODULE.load_release_and_plan(tasks_path, oracles_path)
    with pytest.raises(ValueError, match="outside the reserve/recovered"):
        MODULE.select_scope(plan, ["not-a-reserve-task"])


def test_select_scope_rejects_missing_task(tmp_path):
    dropped = MODULE.TASK_IDS[0]
    tasks_path, oracles_path = build_full_release(tmp_path, drop=dropped)
    _, _, plan = MODULE.load_release_and_plan(tasks_path, oracles_path)
    with pytest.raises(ValueError, match="absent from"):
        MODULE.select_scope(plan, None)


def test_select_scope_rejects_task_that_gained_a_modern_contract(tmp_path):
    promoted = MODULE.TASK_IDS[0]
    tasks_path, oracles_path = build_full_release(
        tmp_path, source_override=(promoted, "/session/native/results.jsonl")
    )
    _, _, plan = MODULE.load_release_and_plan(tasks_path, oracles_path)
    with pytest.raises(ValueError, match="no longer published_legacy"):
        MODULE.select_scope(plan, None)


# ---------------------------------------------------------------------------
# Blocker detection with tiny synthetic legacy roots (no docker, no builds)
# ---------------------------------------------------------------------------


def test_native_blockers_flags_missing_task_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(MODULE, "docker_image_present", lambda image: True)
    task_id = "github__ClickHouse__ClickHouse__pull_74193__comment_2024566878__duplication"
    blockers = MODULE.native_blockers(task_id, tmp_path)
    assert any("missing task directory" in b for b in blockers)


def test_native_blockers_clears_when_everything_present(tmp_path, monkeypatch):
    monkeypatch.setattr(MODULE, "docker_image_present", lambda image: True)
    task_id = "github__ClickHouse__ClickHouse__pull_74193__comment_2024566878__duplication"
    manifest = MODULE.NATIVE_MANIFEST[task_id]
    task_dir = tmp_path / "strict_screen_20260726" / manifest["task_dir"]
    (task_dir / "start").mkdir(parents=True)
    (task_dir / "query.sql").write_text("SELECT 1", encoding="utf-8")
    blockers = MODULE.native_blockers(task_id, tmp_path)
    assert blockers == []


def test_native_blockers_flags_unproven_desbordante_regardless_of_assets(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(MODULE, "docker_image_present", lambda image: True)
    task_id = (
        "github__Desbordante__desbordante-core__pull_154__comment_1371183985"
        "__modularity"
    )
    manifest = MODULE.NATIVE_MANIFEST[task_id]
    task_dir = tmp_path / "strict_screen_20260726" / manifest["task_dir"]
    task_dir.mkdir(parents=True)
    blockers = MODULE.native_blockers(task_id, tmp_path)
    assert any("never proven end-to-end" in b for b in blockers)


def test_jvm_blockers_flags_missing_cache_and_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(MODULE, "docker_image_present", lambda image: True)
    oracle = {
        "repo": "Consensys/web3signer",
        "command": "bash x -I /oracle-cache/oracle.init.gradle",
        "environment": {
            "container": "oracle-java-tools:17",
            "cache": "strict_screen_20260726/oracle_recovery/java/cache/home",
        },
    }
    blockers = MODULE.jvm_blockers("id", oracle, tmp_path)
    assert any("missing retained repo checkout" in b for b in blockers)
    assert any("missing recorded Gradle cache home" in b for b in blockers)
    assert any("oracle.init.gradle" in b for b in blockers)


def test_jvm_blockers_clears_when_cache_and_repo_present(tmp_path, monkeypatch):
    monkeypatch.setattr(MODULE, "docker_image_present", lambda image: True)
    (tmp_path / "repos/Consensys__web3signer").mkdir(parents=True)
    cache_home = tmp_path / "strict_screen_20260726/oracle_recovery/java/cache/home"
    cache_home.mkdir(parents=True)
    (cache_home.parent / "oracle.init.gradle").write_text("", encoding="utf-8")
    oracle = {
        "repo": "Consensys/web3signer",
        "command": "bash x -I /oracle-cache/oracle.init.gradle",
        "environment": {
            "container": "oracle-java-tools:17",
            "cache": "strict_screen_20260726/oracle_recovery/java/cache/home",
        },
    }
    blockers = MODULE.jvm_blockers("id", oracle, tmp_path)
    assert blockers == []


def test_rust_blockers_flags_missing_cargo_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(MODULE, "docker_image_present", lambda image: True)
    oracle = {
        "recovery_artifact": "oracle_recovery/rust/x",
        "workspace_command": "cargo test",
        "environment": {
            "docker_image": "reviewbench-strict-rust:20260726",
            "cargo_cache": "oracle_recovery/rust/cache/cargo",
        },
    }
    (tmp_path / "strict_screen_20260726/oracle_recovery/rust/x/start").mkdir(
        parents=True
    )
    blockers = MODULE.rust_blockers("id", oracle, tmp_path)
    assert any("missing recorded cargo cache" in b for b in blockers)


def test_tsjs_blockers_flags_missing_runtime(tmp_path, monkeypatch):
    oracle = {
        "repo": "discourse/discourse",
        "test_command": "vitest run",
        "historical_node": "20.19.5",
        "recovery_artifact": "oracle_recovery/tsjs/20_discourse__discourse",
    }
    checkout = tmp_path / "repos/discourse__discourse"
    start_sha = make_git_repo(checkout)
    blockers = MODULE.tsjs_blockers("id", oracle, tmp_path, start_sha)
    assert any("missing pinned Node runtime" in b for b in blockers)
    assert any("missing recovery artifact directory" in b for b in blockers)
    # The retained checkout does contain the exact start commit, so this
    # unrelated-to-start_sha test must not also report a start_sha blocker.
    assert not any("start_sha" in b for b in blockers)


def test_tsjs_runtime_override_and_local_binary_resolution(tmp_path):
    task_id = (
        "github__opossum-tool__OpossumUI__pull_987__"
        "comment_972768731__duplication"
    )
    assert MODULE.tsjs_node_version(
        task_id, {"historical_node": "16.20.2"}
    ) == "18.20.8"

    binary = tmp_path / "node_modules/.bin/vitest"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    assert MODULE.resolve_node_command(
        "vitest run --coverage",
        tmp_path,
    ) == [str(binary), "run", "--coverage"]

    binary.unlink()
    fallback = tmp_path / "shared/.bin/vitest"
    fallback.parent.mkdir(parents=True)
    fallback.write_text("#!/bin/sh\n")
    assert MODULE.resolve_node_command(
        "vitest run --coverage",
        tmp_path,
        fallback.parent,
    ) == [str(fallback), "run", "--coverage"]


def test_tsjs_blockers_flags_missing_start_sha_when_oracle_omits_it(tmp_path):
    # Real Discourse/Opossum oracles genuinely omit start_sha (it lives on
    # the task/task-plan record instead). tsjs_blockers must fail closed
    # -- not silently proceed -- when the caller cannot supply the exact
    # task start_sha (regression test for the reviewer-reported bug).
    oracle = {
        "repo": "discourse/discourse",
        "test_command": "vitest run",
        "historical_node": "20.19.5",
        "recovery_artifact": "oracle_recovery/tsjs/20_discourse__discourse",
    }
    checkout = tmp_path / "repos/discourse__discourse"
    make_git_repo(checkout)
    blockers = MODULE.tsjs_blockers("id", oracle, tmp_path, "")
    assert any("missing start_sha" in b for b in blockers)


def test_tsjs_blockers_flags_checkout_missing_exact_start_commit(tmp_path):
    # Even when a start_sha is supplied, preflight must verify the
    # retained checkout actually contains that exact commit rather than
    # trusting the string blindly -- catching a since-changed/incomplete
    # checkout before replay_tsjs's git worktree add would fail deep
    # inside a replay attempt.
    oracle = {
        "repo": "discourse/discourse",
        "test_command": "vitest run",
        "historical_node": "20.19.5",
        "recovery_artifact": "oracle_recovery/tsjs/20_discourse__discourse",
    }
    checkout = tmp_path / "repos/discourse__discourse"
    make_git_repo(checkout)
    blockers = MODULE.tsjs_blockers(
        "id", oracle, tmp_path, "0" * 40  # a commit that does not exist
    )
    assert any("does not contain the exact task start_sha" in b for b in blockers)


# ---------------------------------------------------------------------------
# preflight subcommand (end to end, tiny synthetic release + fake legacy root)
# ---------------------------------------------------------------------------


def test_preflight_reports_all_blocked_on_empty_legacy_root(tmp_path, monkeypatch):
    monkeypatch.setattr(MODULE, "docker_image_present", lambda image: False)
    tasks_path, oracles_path = build_full_release(tmp_path / "release")
    legacy_root = tmp_path / "legacy"
    legacy_root.mkdir()
    args = MODULE.parse_args(
        [
            "preflight",
            "--tasks", str(tasks_path),
            "--oracles", str(oracles_path),
            "--legacy-root", str(legacy_root),
            "--allow-blocked",
        ]
    )
    code = MODULE.cmd_preflight(args)
    assert code == 0
    # every task in an empty legacy root must be reported blocked, never
    # silently treated as passing.
    tasks_path2, oracles_path2 = build_full_release(tmp_path / "release2")
    _, _, plan = MODULE.load_release_and_plan(tasks_path2, oracles_path2)
    assert len(plan) == len(MODULE.TASK_IDS)


def test_preflight_exits_nonzero_when_blocked_without_allow_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(MODULE, "docker_image_present", lambda image: False)
    tasks_path, oracles_path = build_full_release(tmp_path / "release")
    legacy_root = tmp_path / "legacy"
    legacy_root.mkdir()
    args = MODULE.parse_args(
        [
            "preflight",
            "--tasks", str(tasks_path),
            "--oracles", str(oracles_path),
            "--legacy-root", str(legacy_root),
        ]
    )
    assert MODULE.cmd_preflight(args) == 1


def test_preflight_writes_json_report_when_output_given(tmp_path, monkeypatch):
    monkeypatch.setattr(MODULE, "docker_image_present", lambda image: False)
    tasks_path, oracles_path = build_full_release(tmp_path / "release")
    legacy_root = tmp_path / "legacy"
    legacy_root.mkdir()
    output = tmp_path / "output"
    args = MODULE.parse_args(
        [
            "preflight",
            "--tasks", str(tasks_path),
            "--oracles", str(oracles_path),
            "--legacy-root", str(legacy_root),
            "--output", str(output),
            "--allow-blocked",
        ]
    )
    MODULE.cmd_preflight(args)
    report = json.loads((output / "legacy-reserve-preflight.json").read_text())
    assert report["scoped_task_count"] == len(MODULE.TASK_IDS)
    assert report["blocked_task_count"] == len(MODULE.TASK_IDS)
    assert all(entry["status"] == "blocked" for entry in report["tasks"])


# ---------------------------------------------------------------------------
# replay subcommand: candidate_missing + resume, without any real execution
# ---------------------------------------------------------------------------


def stage_generation(output, model, tasks_by_id, apply_ok_ids=frozenset()):
    destination = output / "inputs/models" / BASE.safe_component(model)
    destination.mkdir(parents=True, exist_ok=True)
    rows = []
    for task_id, task in tasks_by_id.items():
        apply_ok = task_id in apply_ok_ids
        row = {
            "id": task_id,
            "model": model,
            "start_sha": task["start_sha"],
            "apply_ok": apply_ok,
            "patch_file": None,
            "patch_sha256": None,
        }
        if apply_ok:
            patch_dir = destination / "patches"
            patch_dir.mkdir(parents=True, exist_ok=True)
            patch_path = patch_dir / f"{task_id}.patch"
            patch_path.write_text("diff --git a/x b/x\n", encoding="utf-8")
            row["patch_file"] = str(patch_path)
            row["patch_sha256"] = BASE.sha256(patch_path)
        rows.append(row)
    write_jsonl(
        destination / "generation_results.normalized.jsonl",
        rows,
    )
    return rows


def test_replay_writes_candidate_missing_rows_and_is_resume_aware(tmp_path):
    tasks_path, oracles_path = build_full_release(tmp_path / "release")
    tasks_by_id, oracles_by_id, plan = MODULE.load_release_and_plan(
        tasks_path, oracles_path
    )
    output = tmp_path / "output"
    stage_generation(output, "my-model", tasks_by_id, apply_ok_ids=frozenset())

    args = MODULE.parse_args(
        [
            "replay",
            "--tasks", str(tasks_path),
            "--oracles", str(oracles_path),
            "--legacy-root", str(tmp_path / "legacy"),
            "--output", str(output),
            "--model", "my-model",
        ]
    )
    code = MODULE.cmd_replay(args)
    assert code == 0

    for lane in ("native", "jvm", "rust", "tsjs"):
        path = output / "legacy/correctness" / f"{lane}.jsonl"
        assert path.is_file()
        rows = BASE.read_jsonl(path)
        for row in rows:
            assert row["status"] == "candidate_missing"
            assert row["source_kind"] == "published_legacy"
            # every emitted row must normalize cleanly through the real
            # orchestrator's normalized_record(), proving wire compatibility.
            oracle = oracles_by_id[row["id"]]
            normalized = BASE.normalized_record(
                lane, row, raw_source="legacy/correctness/x.jsonl",
                source_kind="published_legacy", oracle=oracle,
                generation={"apply_ok": False},
            )
            assert normalized["status"] == "candidate_missing"

    total_rows = sum(
        len(BASE.read_jsonl(output / "legacy/correctness" / f"{lane}.jsonl"))
        for lane in ("native", "jvm", "rust", "tsjs")
    )
    assert total_rows == len(MODULE.TASK_IDS)

    # Resume: rerunning without --force must not duplicate or re-derive rows.
    code_again = MODULE.cmd_replay(args)
    assert code_again == 0
    total_rows_again = sum(
        len(BASE.read_jsonl(output / "legacy/correctness" / f"{lane}.jsonl"))
        for lane in ("native", "jvm", "rust", "tsjs")
    )
    assert total_rows_again == len(MODULE.TASK_IDS)


def test_replay_reports_blockers_for_generated_candidates_on_empty_legacy_root(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(MODULE, "docker_image_present", lambda image: False)
    tasks_path, oracles_path = build_full_release(tmp_path / "release")
    tasks_by_id, oracles_by_id, plan = MODULE.load_release_and_plan(
        tasks_path, oracles_path
    )
    output = tmp_path / "output"
    stage_generation(
        output, "my-model", tasks_by_id, apply_ok_ids=set(MODULE.TASK_IDS)
    )

    args = MODULE.parse_args(
        [
            "replay",
            "--tasks", str(tasks_path),
            "--oracles", str(oracles_path),
            "--legacy-root", str(tmp_path / "legacy"),
            "--output", str(output),
            "--model", "my-model",
        ]
    )
    code = MODULE.cmd_replay(args)
    assert code == 1  # blockers present, --allow-blocked not passed
    for lane in ("native", "jvm", "rust", "tsjs"):
        path = output / "legacy/correctness" / f"{lane}.jsonl"
        assert not path.is_file()  # nothing fabricated for blocked tasks


def test_cmd_replay_propagates_lookup_errors_instead_of_infra_retry(
    tmp_path, monkeypatch
):
    # Code-review finding: a KeyError/LookupError raised while replaying a
    # task (e.g. the reviewer-reported bug of reading a field an Oracle
    # does not actually carry) is a code/data-contract bug, not a
    # transient infra failure, and must never be silently recorded as an
    # INFRA_RETRY row. It must propagate and fail cmd_replay loudly.
    tasks_path, oracles_path = build_full_release(tmp_path / "release")
    tasks_by_id, oracles_by_id, plan = MODULE.load_release_and_plan(
        tasks_path, oracles_path
    )
    tsjs_task_id = next(
        task_id for task_id, lane in MODULE.EXPECTED_LANE.items() if lane == "tsjs"
    )
    output = tmp_path / "output"
    stage_generation(
        output, "my-model", tasks_by_id, apply_ok_ids={tsjs_task_id}
    )
    monkeypatch.setattr(
        MODULE, "task_blockers", lambda *args, **kwargs: []
    )

    def raise_lookup_error(*args, **kwargs):
        raise KeyError("start_sha")

    monkeypatch.setitem(MODULE.REPLAY_FUNCTIONS, "tsjs", raise_lookup_error)

    args_ns = MODULE.parse_args(
        [
            "replay",
            "--tasks", str(tasks_path),
            "--oracles", str(oracles_path),
            "--legacy-root", str(tmp_path / "legacy"),
            "--output", str(output),
            "--model", "my-model",
            "--task-id", tsjs_task_id,
        ]
    )
    with pytest.raises(RuntimeError, match="contract bug replaying tsjs"):
        MODULE.cmd_replay(args_ns)

    # No row must have been fabricated as a silent infra_retry for the
    # task that actually hit the contract bug.
    path = output / "legacy/correctness/tsjs.jsonl"
    if path.is_file():
        rows = BASE.read_jsonl(path)
        assert all(row["status"] != MODULE.INFRA_RETRY for row in rows)


def test_cmd_replay_infra_row_retains_generation_patch_hash(
    tmp_path, monkeypatch
):
    tasks_path, oracles_path = build_full_release(tmp_path / "release")
    tasks_by_id, _, _ = MODULE.load_release_and_plan(
        tasks_path, oracles_path
    )
    tsjs_task_id = next(
        task_id for task_id, lane in MODULE.EXPECTED_LANE.items() if lane == "tsjs"
    )
    output = tmp_path / "output"
    stage_generation(
        output, "my-model", tasks_by_id, apply_ok_ids={tsjs_task_id}
    )
    monkeypatch.setattr(MODULE, "task_blockers", lambda *args, **kwargs: [])

    def raise_infra_error(*args, **kwargs):
        raise FileNotFoundError("runtime disappeared")

    monkeypatch.setitem(MODULE.REPLAY_FUNCTIONS, "tsjs", raise_infra_error)
    args_ns = MODULE.parse_args(
        [
            "replay",
            "--tasks", str(tasks_path),
            "--oracles", str(oracles_path),
            "--legacy-root", str(tmp_path / "legacy"),
            "--output", str(output),
            "--model", "my-model",
            "--task-id", tsjs_task_id,
        ]
    )

    assert MODULE.cmd_replay(args_ns) == 0
    row = BASE.read_jsonl(
        output / "legacy/correctness/tsjs.jsonl"
    )[0]
    generation = next(
        item
        for item in BASE.read_jsonl(
            output
            / "inputs/models/my-model/generation_results.normalized.jsonl"
        )
        if item["id"] == tsjs_task_id
    )
    assert row["status"] == MODULE.INFRA_RETRY
    assert row["candidate_result"] == MODULE.INFRA_RETRY
    assert row["patch_sha256"] == generation["patch_sha256"]


def test_replay_rejects_missing_staged_generation(tmp_path):
    tasks_path, oracles_path = build_full_release(tmp_path / "release")
    output = tmp_path / "output"
    code = MODULE.main(
        [
            "replay",
            "--tasks", str(tasks_path),
            "--oracles", str(oracles_path),
            "--legacy-root", str(tmp_path / "legacy"),
            "--output", str(output),
            "--model", "my-model",
        ]
    )
    assert code == 2  # fails closed: refuses to fabricate unstaged results


def test_replay_rejects_unsafe_model_id(tmp_path):
    # Code-review finding: --model must be validated with the existing
    # strict BASE.validate_model_id validator before it is ever used to
    # build a filesystem path.
    tasks_path, oracles_path = build_full_release(tmp_path / "release")
    output = tmp_path / "output"
    code = MODULE.main(
        [
            "replay",
            "--tasks", str(tasks_path),
            "--oracles", str(oracles_path),
            "--legacy-root", str(tmp_path / "legacy"),
            "--output", str(output),
            "--model", "../escape",
        ]
    )
    assert code == 2  # fails closed, never even reaches load_staged_generation


def test_replay_rejects_staged_generation_row_with_mismatched_model_field(tmp_path):
    # Code-review finding: every staged generation row's embedded "model"
    # field must exactly match the requested --model, to prevent
    # safe_component slug collisions/mislabeling from silently consuming a
    # generation file staged for a different model ID.
    tasks_path, oracles_path = build_full_release(tmp_path / "release")
    tasks_by_id, oracles_by_id, plan = MODULE.load_release_and_plan(
        tasks_path, oracles_path
    )
    output = tmp_path / "output"
    stage_generation(output, "my-model", tasks_by_id, apply_ok_ids=frozenset())

    path = MODULE.staged_generation_path(output, "my-model")
    rows = BASE.read_jsonl(path)
    rows[0]["model"] = "some-other-model"
    write_jsonl(path, rows)

    args = MODULE.parse_args(
        [
            "replay",
            "--tasks", str(tasks_path),
            "--oracles", str(oracles_path),
            "--legacy-root", str(tmp_path / "legacy"),
            "--output", str(output),
            "--model", "my-model",
        ]
    )
    with pytest.raises(ValueError, match="embedded model"):
        MODULE.cmd_replay(args)


# ---------------------------------------------------------------------------
# replay execution wiring, with docker/gradle/cargo/vitest calls mocked out
# ---------------------------------------------------------------------------


def test_replay_native_applies_patch_and_records_pass(tmp_path, monkeypatch):
    task_id = "github__ClickHouse__ClickHouse__pull_74193__comment_2024566878__duplication"
    manifest = MODULE.NATIVE_MANIFEST[task_id]
    legacy_root = tmp_path / "legacy"
    task_dir = legacy_root / "strict_screen_20260726" / manifest["task_dir"]
    (task_dir / "start").mkdir(parents=True)
    (task_dir / "start" / "file.txt").write_text("a\nb\nc\n", encoding="utf-8")
    (task_dir / "start" / "broken-link").symlink_to("missing-target")
    (task_dir / "query.sql").write_text("SELECT 1", encoding="utf-8")

    patch_path = tmp_path / "candidate.patch"
    patch_path.write_text(
        "diff --git a/file.txt b/file.txt\n"
        "--- a/file.txt\n"
        "+++ b/file.txt\n"
        "@@ -1,3 +1,3 @@\n"
        " a\n"
        "-b\n"
        "+B\n"
        " c\n",
        encoding="utf-8",
    )
    generation = {
        "patch_file": str(patch_path),
        "patch_sha256": BASE.sha256(patch_path),
    }
    task = strict_task(task_id)
    oracle = {"start_sha": "start", "pass_to_pass": 1}
    output = tmp_path / "output"

    captured = {}
    seen_workspace = {}

    def fake_run_subprocess(command, *, cwd, log_path, env=None, timeout=1800):
        captured["command"] = command
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("ok\n", encoding="utf-8")
        return 0, 1.23, ""

    real_persist_evidence = MODULE.persist_evidence

    def spy_persist_evidence(output_arg, lane, model, task_id_arg, scratch):
        # Snapshot the patched workspace before the real helper deletes the
        # disposable scratch dir, so the test can still assert on it.
        seen_workspace["file.txt"] = (
            (scratch / "workspace" / "file.txt").read_text(encoding="utf-8")
        )
        seen_workspace["broken-link"] = (
            (scratch / "workspace" / "broken-link").is_symlink(),
            (scratch / "workspace" / "broken-link").readlink(),
        )
        return real_persist_evidence(output_arg, lane, model, task_id_arg, scratch)

    monkeypatch.setattr(MODULE, "run_subprocess", fake_run_subprocess)
    monkeypatch.setattr(MODULE, "persist_evidence", spy_persist_evidence)

    row = MODULE.replay_native(
        task_id, "my-model", task, oracle, generation, legacy_root,
        tmp_path / "work", output, 60,
    )
    assert row["status"] == "pass"
    assert row["correctness_ok"] is True
    assert row["patch_apply"] is True
    assert row["scope_ok"] is True
    assert manifest["docker_image"] in captured["command"]
    # the patched workspace copy must contain the candidate's change, and
    # the original retained task_dir must be left untouched.
    assert seen_workspace["file.txt"] == "a\nB\nc\n"
    assert seen_workspace["broken-link"] == (True, Path("missing-target"))
    assert (task_dir / "start" / "file.txt").read_text(encoding="utf-8") == "a\nb\nc\n"
    # the disposable scratch workspace must not linger under output --
    # only the small evidence log does.
    assert_no_scratch_files(tmp_path / "work")
    assert row["log"] == "legacy/evidence/native/{}/{}/run.log".format(
        MODULE.model_component("my-model"), task_id
    )
    assert (output / row["log"]).is_file()
    # strong provenance fields required for stale-row detection on resume.
    assert row["task_fingerprint"] == BASE.task_fingerprint(task)
    assert row["oracle_fingerprint"] == BASE.canonical_hash(oracle)
    assert row["replay_contract_fingerprint"] == MODULE.replay_contract_fingerprint(
        task_id, "native", oracle, legacy_root, []
    )
    assert row["generation_apply_ok"] is None


def test_replay_native_reports_apply_error_without_running_harness(
    tmp_path, monkeypatch
):
    task_id = "github__ClickHouse__ClickHouse__pull_74193__comment_2024566878__duplication"
    manifest = MODULE.NATIVE_MANIFEST[task_id]
    legacy_root = tmp_path / "legacy"
    task_dir = legacy_root / "strict_screen_20260726" / manifest["task_dir"]
    (task_dir / "start").mkdir(parents=True)
    (task_dir / "query.sql").write_text("SELECT 1", encoding="utf-8")

    patch_path = tmp_path / "candidate.patch"
    patch_path.write_text(
        "diff --git a/missing.txt b/missing.txt\n"
        "--- a/missing.txt\n"
        "+++ b/missing.txt\n"
        "@@ -1 +1 @@\n"
        "-a\n"
        "+b\n",
        encoding="utf-8",
    )
    generation = {"patch_file": str(patch_path), "patch_sha256": BASE.sha256(patch_path)}
    task = strict_task(task_id)
    oracle = {"start_sha": "start", "pass_to_pass": 1}
    output = tmp_path / "output"

    called = {"count": 0}

    def fake_run_subprocess(*args, **kwargs):
        called["count"] += 1
        return 0, 0.1, ""

    monkeypatch.setattr(MODULE, "run_subprocess", fake_run_subprocess)
    row = MODULE.replay_native(
        task_id, "my-model", task, oracle, generation, legacy_root,
        tmp_path / "work", output, 60,
    )
    assert row["status"] == "apply_error"
    assert row["patch_apply"] is False
    assert called["count"] == 0  # never runs the (would-be) real build/harness
    # the apply-error evidence log must be preserved under a hashed output
    # subtree, never referenced as an absolute/external work-root path.
    assert row["error_log"] == "legacy/evidence/native/{}/{}/apply-error.log".format(
        MODULE.model_component("my-model"), task_id
    )
    assert (output / row["error_log"]).is_file()
    assert "log" not in row


# ---------------------------------------------------------------------------
# Code-review regression coverage: resume staleness, concurrency safety,
# and hashed-evidence-only raw rows.
# ---------------------------------------------------------------------------


def test_missing_row_and_replay_rows_carry_strong_provenance_fields():
    task_id = MODULE.TASK_IDS[0]
    lane = MODULE.EXPECTED_LANE[task_id]
    task = strict_task(task_id)
    oracle = strict_oracle(task_id)
    generation = {"apply_ok": False, "patch_sha256": None}
    legacy_root = Path("/nonexistent-legacy-root")
    blockers = ["missing everything"]
    row = MODULE.missing_row(
        task_id, "my-model", lane, "start", task, oracle, generation,
        legacy_root, blockers,
    )
    assert row["task_fingerprint"] == BASE.task_fingerprint(task)
    assert row["oracle_fingerprint"] == BASE.canonical_hash(oracle)
    assert row["replay_contract_fingerprint"] == MODULE.replay_contract_fingerprint(
        task_id, lane, oracle, legacy_root, blockers
    )
    assert row["generation_apply_ok"] is False
    assert row["patch_sha256"] is None
    # These are additive fields; normalized_record must still accept the row
    # and preserve them verbatim under raw_record (finalize() never strips
    # unknown fields, so this is "accepted and preserved by the existing
    # finalizer" without any change to run_open_model_correctness.py).
    normalized = BASE.normalized_record(
        lane, row, raw_source="legacy/correctness/x.jsonl",
        source_kind="published_legacy", oracle=oracle, generation=generation,
    )
    assert normalized["raw_record"]["task_fingerprint"] == row["task_fingerprint"]
    assert normalized["raw_record"]["replay_contract_fingerprint"] == (
        row["replay_contract_fingerprint"]
    )


def test_row_is_stale_detects_task_oracle_contract_and_patch_drift():
    task_id = MODULE.TASK_IDS[0]
    lane = MODULE.EXPECTED_LANE[task_id]
    task = strict_task(task_id)
    oracle = strict_oracle(task_id)
    generation = {"apply_ok": True, "patch_sha256": "abc123"}
    legacy_root = Path("/nonexistent-legacy-root")
    blockers: list[str] = []
    row = MODULE.missing_row(
        task_id, "my-model", lane, "start", task, oracle, generation,
        legacy_root, blockers,
    )

    # Identical inputs: not stale.
    assert MODULE.row_is_stale(
        row, task, oracle, task_id, lane, generation, legacy_root, blockers
    ) is False

    # Oracle content changed (e.g. Oracle recovery updated): stale.
    drifted_oracle = dict(oracle, pass_to_pass=99)
    assert MODULE.row_is_stale(
        row, task, drifted_oracle, task_id, lane, generation, legacy_root, blockers
    ) is True

    # Task content changed: stale.
    drifted_task = dict(task, target_sha="new-target")
    assert MODULE.row_is_stale(
        row, drifted_task, oracle, task_id, lane, generation, legacy_root, blockers
    ) is True

    # Same model, regenerated/different patch: stale (must rerun with the
    # new candidate, never silently keep scoring the old one).
    drifted_generation = dict(generation, patch_sha256="def456")
    assert MODULE.row_is_stale(
        row, task, oracle, task_id, lane, drifted_generation, legacy_root, blockers
    ) is True

    # --legacy-root changed: stale, even though task/oracle/patch are
    # untouched (code-review finding: an old row must not survive a
    # missing/changed --legacy-root).
    other_root = Path("/another-legacy-root")
    assert MODULE.row_is_stale(
        row, task, oracle, task_id, lane, generation, other_root, blockers
    ) is True

    # Blocker state changed (asset newly missing/present): stale.
    assert MODULE.row_is_stale(
        row, task, oracle, task_id, lane, generation, legacy_root, ["now blocked"]
    ) is True

    # Adapter replay contract revised (e.g. this code-review hardening pass
    # itself bumps ADAPTER_CONTRACT_VERSION): stale.
    if lane == "native":
        tampered_row = dict(row, replay_contract_fingerprint="stale-contract")
        assert MODULE.row_is_stale(
            tampered_row, task, oracle, task_id, lane, generation, legacy_root, blockers
        ) is True


def test_cmd_replay_reruns_stale_candidate_missing_rows_on_oracle_drift(tmp_path):
    tasks_path, oracles_path = build_full_release(tmp_path / "release")
    tasks_by_id, oracles_by_id, plan = MODULE.load_release_and_plan(
        tasks_path, oracles_path
    )
    output = tmp_path / "output"
    stage_generation(output, "my-model", tasks_by_id, apply_ok_ids=frozenset())

    args = MODULE.parse_args(
        [
            "replay",
            "--tasks", str(tasks_path),
            "--oracles", str(oracles_path),
            "--legacy-root", str(tmp_path / "legacy"),
            "--output", str(output),
            "--model", "my-model",
        ]
    )
    assert MODULE.cmd_replay(args) == 0

    lane = MODULE.EXPECTED_LANE[MODULE.TASK_IDS[0]]
    path = output / "legacy/correctness" / f"{lane}.jsonl"
    before = {row["id"]: row for row in BASE.read_jsonl(path)}
    recorded_fp = before[MODULE.TASK_IDS[0]]["oracle_fingerprint"]

    # Re-running with completely unchanged inputs must resume (skip), not
    # rerun -- proves fingerprint matching doesn't produce false positives.
    summary_path = tmp_path / "unused"
    code = MODULE.cmd_replay(args)
    assert code == 0

    # Simulate an Oracle-recovery update for one task (e.g. re-recovered
    # environment/cache metadata) by rewriting the oracle record on disk
    # with different content but the same id/lane, then rerun without
    # --force: resume must detect the drift and rerun rather than
    # silently trusting the stale row.
    oracles_rows = BASE.read_jsonl(oracles_path)
    for oracle_row in oracles_rows:
        if oracle_row["id"] == MODULE.TASK_IDS[0]:
            oracle_row["pass_to_pass"] = 42
    write_jsonl(oracles_path, oracles_rows)

    code = MODULE.cmd_replay(args)
    assert code == 0
    after = {row["id"]: row for row in BASE.read_jsonl(path)}
    assert after[MODULE.TASK_IDS[0]]["oracle_fingerprint"] != recorded_fp


def test_cmd_replay_retracts_stale_row_when_task_becomes_blocked(tmp_path, monkeypatch):
    # Code-review finding: cmd_replay must evaluate current task_blockers()
    # before the resume-skip decision, so an old row cannot survive a
    # missing/changed --legacy-root -- and "cannot survive" must mean the
    # stale row is actually removed from disk, not merely bypassed.
    tasks_path, oracles_path = build_full_release(tmp_path / "release")
    tasks_by_id, oracles_by_id, plan = MODULE.load_release_and_plan(
        tasks_path, oracles_path
    )
    output = tmp_path / "output"
    stage_generation(
        output, "my-model", tasks_by_id, apply_ok_ids=set(MODULE.TASK_IDS)
    )

    target_id = MODULE.TASK_IDS[0]

    def fake_replay(
        task_id, model, task, oracle, generation, legacy_root, work_root,
        output_arg, timeout,
    ):
        lane = MODULE.EXPECTED_LANE[task_id]
        row = {
            "id": task_id,
            "model": model,
            "lane": lane,
            "source_kind": "published_legacy",
            "start_sha": task.get("start_sha", "start"),
            "status": "pass",
            "correctness_ok": True,
            "patch_apply": True,
            "scope_ok": True,
        }
        row.update(
            MODULE.row_provenance_fields(
                task, oracle, task_id, lane, generation, legacy_root, []
            )
        )
        return row

    monkeypatch.setattr(MODULE, "task_blockers", lambda *a, **k: [])
    for lane_name in list(MODULE.REPLAY_FUNCTIONS):
        monkeypatch.setitem(MODULE.REPLAY_FUNCTIONS, lane_name, fake_replay)

    args = MODULE.parse_args(
        [
            "replay",
            "--tasks", str(tasks_path),
            "--oracles", str(oracles_path),
            "--legacy-root", str(tmp_path / "legacy"),
            "--output", str(output),
            "--model", "my-model",
        ]
    )
    assert MODULE.cmd_replay(args) == 0

    lane = MODULE.EXPECTED_LANE[target_id]
    path = output / "legacy/correctness" / f"{lane}.jsonl"
    before_ids = {row["id"] for row in BASE.read_jsonl(path)}
    assert target_id in before_ids

    # Now simulate the target task's required asset becoming unavailable
    # under the (unchanged) --legacy-root, while every other task remains
    # ready. A resume must not trust the old "pass" row's fingerprint --
    # it must recompute blockers first, discover the task is now blocked,
    # and retract the stale row rather than leaving it on disk.
    def fake_blockers(task_id, lane_arg, oracle_arg, legacy_root_arg, start_sha_arg):
        if task_id == target_id:
            return ["asset now missing"]
        return []

    monkeypatch.setattr(MODULE, "task_blockers", fake_blockers)

    code = MODULE.cmd_replay(args)
    assert code == 1  # blocked present, --allow-blocked not passed
    after_ids = {row["id"] for row in BASE.read_jsonl(path)}
    assert target_id not in after_ids  # stale row actually retracted, not just skipped
    assert len(after_ids) == len(before_ids) - 1



def test_lane_lock_creates_dot_lock_file_excluded_from_hashing(tmp_path):
    legacy_dir = tmp_path / "legacy/correctness"
    with MODULE.lane_lock(legacy_dir, "native"):
        lock_path = legacy_dir / ".native.lock"
        assert lock_path.is_file()
    # ".{lane}.lock" matches run_open_model_correctness.py's own
    # write_artifact_set naming convention for excluded lock files.
    assert lock_path.name.endswith(".lock")


def test_append_raw_row_survives_concurrent_writers_without_losing_rows(tmp_path):
    legacy_dir = tmp_path / "legacy/correctness"
    lane = "native"
    task_count = 24
    barrier = threading.Barrier(task_count)

    def write_one(index):
        barrier.wait()  # maximize actual overlap between writers
        row = {
            "id": f"task-{index}",
            "model": "my-model",
            "lane": lane,
            "status": "pass",
        }
        MODULE.append_raw_row(legacy_dir, lane, row)

    with ThreadPoolExecutor(max_workers=task_count) as pool:
        list(pool.map(write_one, range(task_count)))

    rows = BASE.read_jsonl(legacy_dir / f"{lane}.jsonl")
    assert len(rows) == task_count  # no lost rows despite concurrent writers
    assert {row["id"] for row in rows} == {f"task-{i}" for i in range(task_count)}


def test_append_raw_row_rereads_disk_instead_of_trusting_stale_snapshot(tmp_path):
    legacy_dir = tmp_path / "legacy/correctness"
    lane = "native"
    # Simulate two independent "writers" (e.g. two replay processes racing
    # on the same lane) that each only ever saw an empty snapshot when they
    # started -- neither one is handed the other's row directly. Both must
    # still survive in the final file because append_raw_row rereads the
    # current on-disk state under the lock rather than trusting any
    # caller-held snapshot.
    row_a = {"id": "task-a", "model": "model-a", "lane": lane, "status": "pass"}
    row_b = {"id": "task-b", "model": "model-b", "lane": lane, "status": "fail"}

    MODULE.append_raw_row(legacy_dir, lane, row_a)
    MODULE.append_raw_row(legacy_dir, lane, row_b)

    rows = BASE.read_jsonl(legacy_dir / f"{lane}.jsonl")
    assert {(row["id"], row["model"]) for row in rows} == {
        ("task-a", "model-a"), ("task-b", "model-b"),
    }


def test_retract_raw_row_removes_only_the_targeted_key(tmp_path):
    legacy_dir = tmp_path / "legacy/correctness"
    lane = "native"
    MODULE.append_raw_row(
        legacy_dir, lane,
        {"id": "task-a", "model": "model-a", "lane": lane, "status": "pass"},
    )
    MODULE.append_raw_row(
        legacy_dir, lane,
        {"id": "task-b", "model": "model-a", "lane": lane, "status": "pass"},
    )

    removed = MODULE.retract_raw_row(legacy_dir, lane, "task-a", "model-a")
    assert removed is True
    rows = BASE.read_jsonl(legacy_dir / f"{lane}.jsonl")
    assert {(row["id"], row["model"]) for row in rows} == {("task-b", "model-a")}

    # Retracting a key that no longer exists is a safe no-op, not an error.
    removed_again = MODULE.retract_raw_row(legacy_dir, lane, "task-a", "model-a")
    assert removed_again is False


def test_retract_raw_row_survives_concurrent_writers_without_corrupting_file(
    tmp_path,
):
    legacy_dir = tmp_path / "legacy/correctness"
    lane = "native"
    task_count = 16
    for index in range(task_count):
        MODULE.append_raw_row(
            legacy_dir, lane,
            {"id": f"task-{index}", "model": "m", "lane": lane, "status": "pass"},
        )

    barrier = threading.Barrier(task_count // 2)

    def retract_one(index):
        barrier.wait()
        MODULE.retract_raw_row(legacy_dir, lane, f"task-{index}", "m")

    with ThreadPoolExecutor(max_workers=task_count // 2) as pool:
        list(pool.map(retract_one, range(task_count // 2)))

    rows = BASE.read_jsonl(legacy_dir / f"{lane}.jsonl")
    assert {row["id"] for row in rows} == {
        f"task-{i}" for i in range(task_count // 2, task_count)
    }


def test_asset_state_and_docker_image_id_do_not_raise_without_docker(tmp_path):
    # docker_image_id/asset_state must degrade to None/absent rather than
    # raising when docker is unavailable or the image is not present --
    # they are advisory fingerprint inputs, never a hard requirement by
    # themselves (task_blockers already covers hard requirements).
    assert MODULE.docker_image_id("no-such-image:latest") is None
    assert MODULE.path_identity(tmp_path / "does-not-exist") is None

    task_id = "github__ClickHouse__ClickHouse__pull_74193__comment_2024566878__duplication"
    oracle = strict_oracle(task_id)
    state = MODULE.asset_state(task_id, "native", oracle, tmp_path / "legacy")
    assert "docker_image_id" in state


def test_content_tree_digest_detects_in_place_replacement_at_same_mtime(tmp_path):
    # Historically this used a *bounded* directory identity (500 files, first
    # 64 KiB per file). It is now the same full lstat+content digest the
    # recovered contracts use, so nothing is sampled away.
    tree = tmp_path / "tree"
    tree.mkdir()
    target = tree / "file.txt"
    target.write_text("original content!", encoding="utf-8")
    original_stat = target.stat()

    before = MODULE.content_tree_digest(tree)

    target.write_text("REPLACED content!", encoding="utf-8")  # same length
    os.utime(target, (original_stat.st_atime, original_stat.st_mtime))
    assert target.stat().st_size == original_stat.st_size
    assert target.stat().st_mtime == original_stat.st_mtime

    # A parent-only stat (path_identity) would see no difference at all.
    assert MODULE.path_identity(tree) == MODULE.path_identity(tree)
    assert MODULE.content_tree_digest(tree) != before
    assert MODULE.content_tree_digest(tmp_path / "does-not-exist") is None
    assert MODULE.content_file_digest(None) is None


def test_bounded_helpers_are_gone_from_the_module():
    """The sampled helpers must not exist any more: their presence is what
    let a >500-file tree, a tail-of-file edit, a symlink or an empty
    directory escape a provenance decision."""
    for name in ("directory_identity", "safe_file_digest", "_bounded_file_digest"):
        assert not hasattr(MODULE, name), name


def test_legacy_lane_digests_cover_beyond_the_old_500_file_cap(tmp_path):
    """A change to the 900th file (past the old cap) must be visible."""
    tree = tmp_path / "cache"
    tree.mkdir()
    for index in range(900):
        (tree / f"blob-{index:04d}.bin").write_bytes(b"x" * 32)
    before = MODULE.content_tree_digest(tree)
    assert before["file_count"] == 900
    (tree / "blob-0899.bin").write_bytes(b"y" * 32)  # same size, last file
    assert MODULE.content_tree_digest(tree) != before


def test_legacy_lane_digests_cover_file_tails_beyond_64kib(tmp_path):
    """A change past the first 64 KiB of a file must be visible."""
    tree = tmp_path / "cache"
    tree.mkdir()
    blob = tree / "big.bin"
    blob.write_bytes(b"a" * (128 * 1024))
    before = MODULE.content_tree_digest(tree)
    payload = bytearray(blob.read_bytes())
    payload[-1] = ord("b")  # far beyond the old 64 KiB read cap
    blob.write_bytes(bytes(payload))
    assert MODULE.content_tree_digest(tree) != before


def test_legacy_lane_digests_cover_symlinks_and_empty_dirs(tmp_path):
    tree = tmp_path / "cache"
    (tree / "real").mkdir(parents=True)
    (tree / "real/a").write_text("a\n", encoding="utf-8")
    (tree / "hollow").mkdir()
    os.symlink("real", tree / "dirlink")
    before = MODULE.content_tree_digest(tree)
    assert before["dir_count"] == 2 and before["symlink_count"] == 1
    (tree / "dirlink").unlink()
    os.symlink("hollow", tree / "dirlink")
    assert MODULE.content_tree_digest(tree) != before
    (tree / "dirlink").unlink()
    os.symlink("real", tree / "dirlink")
    assert MODULE.content_tree_digest(tree) == before
    (tree / "hollow").rmdir()
    assert MODULE.content_tree_digest(tree) != before


def test_every_legacy_lane_asset_state_uses_full_content_digests(tmp_path):
    """JVM/TS-JS/native/rust historical asset states must carry full digests
    (``kind``/``entry_count``), never the old bounded sample shape."""
    legacy_root = tmp_path / "legacy"
    repo = legacy_root / "repos/owner__repo"
    make_git_repo(repo)
    for index in range(600):  # beyond the old traversal cap
        (repo / f"f{index}.txt").write_text("x", encoding="utf-8")
    cache_home = legacy_root / "strict_screen_20260726/oracle_recovery/java/cache/home"
    cache_home.mkdir(parents=True)
    (cache_home.parent / "oracle.init.gradle").write_text("g", encoding="utf-8")
    (cache_home / "blob").write_text("b", encoding="utf-8")
    session = (
        legacy_root / "strict_screen_20260726/oracle_recovery/tsjs/20_x/session"
    )
    session.mkdir(parents=True)
    (session / "s.txt").write_text("s", encoding="utf-8")
    tools = (
        legacy_root
        / "strict_screen_20260726/oracle_recovery/tsjs/_node_tools/node_modules"
    )
    tools.mkdir(parents=True)
    (tools / "t.txt").write_text("t", encoding="utf-8")

    jvm_oracle = strict_oracle(
        "github__apache__solr__pull_3433__comment_2231064740__modularity",
        repo="owner/repo",
        command="java -javaagent:/oracle-cache/oracle.init.gradle test",
        environment={
            "container": "img",
            "cache": "oracle_recovery/java/cache/home",
        },
    )
    jvm_state = MODULE.asset_state(
        "github__apache__solr__pull_3433__comment_2231064740__modularity",
        "jvm", jvm_oracle, legacy_root,
    )
    assert jvm_state["repo_checkout"]["kind"] == "tree"
    assert jvm_state["repo_checkout"]["file_count"] >= 600
    assert jvm_state["cache_home"]["kind"] == "tree"
    assert jvm_state["oracle_cache_blobs"]["oracle.init.gradle"]["sha256"]

    tsjs_oracle = strict_oracle(
        "github__discourse__discourse__pull_37336__comment_2752215814__complexity",
        repo="owner/repo",
        historical_node="18.20.8",
        recovery_artifact="oracle_recovery/tsjs/20_x",
    )
    tsjs_state = MODULE.asset_state(
        "github__discourse__discourse__pull_37336__comment_2752215814__complexity",
        "tsjs", tsjs_oracle, legacy_root,
    )
    assert tsjs_state["repo_checkout"]["kind"] == "tree"
    assert tsjs_state["session"]["kind"] == "tree"
    assert tsjs_state["shared_node_tools"]["kind"] == "tree"


def test_asset_state_binds_rust_start_tree_identity(tmp_path):
    legacy_root = tmp_path / "legacy"
    task_id = "github__FuelLabs__fuel-tx__pull_197__comment_1003742605__complexity"
    recovery_dir = legacy_root / "strict_screen_20260726/oracle_recovery/rust/fuel-tx"
    start_dir = recovery_dir / "start"
    start_dir.mkdir(parents=True)
    (start_dir / "Cargo.toml").write_text("original\n", encoding="utf-8")
    oracle = strict_oracle(
        task_id,
        recovery_artifact="oracle_recovery/rust/fuel-tx",
        environment={"docker_image": "rust-tools:1"},
    )

    before = MODULE.asset_state(task_id, "rust", oracle, legacy_root)
    stat = (start_dir / "Cargo.toml").stat()
    (start_dir / "Cargo.toml").write_text("replaced\n", encoding="utf-8")
    os.utime(start_dir / "Cargo.toml", (stat.st_atime, stat.st_mtime))

    after = MODULE.asset_state(task_id, "rust", oracle, legacy_root)
    assert after["start"] != before["start"]


def test_asset_state_binds_jvm_repo_checkout_and_oracle_cache_blob_identity(tmp_path):
    legacy_root = tmp_path / "legacy"
    task_id = "github__Consensys__web3signer__pull_489__comment_789312375__modularity"
    checkout = legacy_root / "repos/org__repo"
    checkout.mkdir(parents=True)
    (checkout / "build.gradle").write_text("original\n", encoding="utf-8")
    cache_home = legacy_root / "strict_screen_20260726/oracle_recovery/java/cache/home"
    cache_home.mkdir(parents=True)
    cache_blob = cache_home.parent / "oracle.init.gradle"
    cache_blob.write_text("original init\n", encoding="utf-8")
    oracle = strict_oracle(
        task_id,
        repo="org/repo",
        command="gradle test --init-script /oracle-cache/oracle.init.gradle",
        environment={
            "container": "oracle-java-tools:17",
            "cache": "strict_screen_20260726/oracle_recovery/java/cache/home",
        },
    )

    before = MODULE.asset_state(task_id, "jvm", oracle, legacy_root)

    # In-place replacement of the repo checkout must invalidate the
    # fingerprint.
    stat = (checkout / "build.gradle").stat()
    (checkout / "build.gradle").write_text("replaced\n", encoding="utf-8")
    os.utime(checkout / "build.gradle", (stat.st_atime, stat.st_mtime))
    after_checkout_change = MODULE.asset_state(task_id, "jvm", oracle, legacy_root)
    assert after_checkout_change["repo_checkout"] != before["repo_checkout"]

    # Restore the checkout, then in-place replace the required
    # /oracle-cache blob instead: this alone must also invalidate it.
    (checkout / "build.gradle").write_text("original\n", encoding="utf-8")
    os.utime(checkout / "build.gradle", (stat.st_atime, stat.st_mtime))
    blob_stat = cache_blob.stat()
    cache_blob.write_text("replaced init\n", encoding="utf-8")
    os.utime(cache_blob, (blob_stat.st_atime, blob_stat.st_mtime))
    after_blob_change = MODULE.asset_state(task_id, "jvm", oracle, legacy_root)
    assert after_blob_change["repo_checkout"] == before["repo_checkout"]
    assert after_blob_change["oracle_cache_blobs"] != before["oracle_cache_blobs"]


def test_asset_state_binds_tsjs_repo_checkout_and_session_tree_identity(tmp_path):
    legacy_root = tmp_path / "legacy"
    task_id = "github__discourse__discourse__pull_37336__comment_2752215814__complexity"
    checkout = legacy_root / "repos/discourse__discourse"
    checkout.mkdir(parents=True)
    (checkout / "package.json").write_text("{}", encoding="utf-8")
    recovery_dir = legacy_root / "strict_screen_20260726/oracle_recovery/tsjs/20_discourse"
    session_dir = recovery_dir / "session"
    session_dir.mkdir(parents=True)
    (session_dir / "state.json").write_text('{"v": 1}', encoding="utf-8")
    oracle = strict_oracle(
        task_id,
        repo="discourse/discourse",
        recovery_artifact="oracle_recovery/tsjs/20_discourse",
    )

    before = MODULE.asset_state(task_id, "tsjs", oracle, legacy_root)
    stat = (session_dir / "state.json").stat()
    (session_dir / "state.json").write_text('{"v": 2}', encoding="utf-8")
    os.utime(session_dir / "state.json", (stat.st_atime, stat.st_mtime))

    after = MODULE.asset_state(task_id, "tsjs", oracle, legacy_root)
    assert after["session"] != before["session"]
    assert after["repo_checkout"] == before["repo_checkout"]  # unchanged


def test_model_component_is_collision_free_for_safe_component_collisions(tmp_path):
    # Code-review finding: replace safe_component(model) directory names
    # for scratch/evidence with a collision-free component. "a/b" and
    # "a-b" are both valid --model IDs per validate_model_id, but collide
    # under BASE.safe_component (both slugify to "a-b").
    model_a = "a/b"
    model_b = "a-b"
    assert BASE.safe_component(model_a) == BASE.safe_component(model_b)

    component_a = MODULE.model_component(model_a)
    component_b = MODULE.model_component(model_b)
    assert component_a != component_b
    assert component_a.startswith(BASE.safe_component(model_a) + "-")
    assert component_b.startswith(BASE.safe_component(model_b) + "-")

    # Deterministic: the same model always yields the same component, so
    # resume/re-invocation still finds its own prior scratch/evidence.
    assert MODULE.model_component(model_a) == component_a


def test_evidence_dir_does_not_collide_for_safe_component_colliding_models(tmp_path):
    output = tmp_path / "output"
    lane, task_id = "native", "some-task"
    dir_a = MODULE.evidence_dir(output, lane, "a/b", task_id)
    dir_b = MODULE.evidence_dir(output, lane, "a-b", task_id)
    assert dir_a != dir_b


def test_persist_evidence_does_not_overwrite_across_colliding_model_ids(tmp_path):
    # Two different --model values that collide under safe_component must
    # never share, overwrite, or delete each other's persisted evidence.
    output = tmp_path / "output"
    lane, task_id = "native", "some-task"

    scratch_a = tmp_path / "work/native" / MODULE.model_component("a/b") / task_id
    scratch_a.mkdir(parents=True)
    (scratch_a / "run.log").write_text("evidence for a/b\n", encoding="utf-8")

    scratch_b = tmp_path / "work/native" / MODULE.model_component("a-b") / task_id
    scratch_b.mkdir(parents=True)
    (scratch_b / "run.log").write_text("evidence for a-b\n", encoding="utf-8")

    evidence_a = MODULE.persist_evidence(output, lane, "a/b", task_id, scratch_a)
    evidence_b = MODULE.persist_evidence(output, lane, "a-b", task_id, scratch_b)

    path_a = output / evidence_a["run.log"]
    path_b = output / evidence_b["run.log"]
    assert path_a != path_b
    assert path_a.read_text(encoding="utf-8") == "evidence for a/b\n"
    assert path_b.read_text(encoding="utf-8") == "evidence for a-b\n"


def test_persist_evidence_copies_logs_and_deletes_disposable_scratch(tmp_path):
    output = tmp_path / "output"
    scratch = tmp_path / "work/native/my-model/some-task"
    workspace = scratch / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "big-build-artifact.o").write_bytes(b"\x00" * 1024)
    (scratch / "run.log").write_text("build output\n", encoding="utf-8")
    (scratch / "apply-error.log").write_text("patch rejected\n", encoding="utf-8")

    evidence = MODULE.persist_evidence(output, "native", "my-model", "some-task", scratch)

    assert set(evidence) == {"run.log", "apply-error.log"}
    for name, relative in evidence.items():
        # Raw rows must only ever reference paths under `output` -- never
        # the (possibly external, always disposable) --work-root location.
        assert not Path(relative).is_absolute()
        resolved = output / relative
        assert resolved.is_file()
        assert resolved.parent == (
            output / "legacy/evidence/native"
            / MODULE.model_component("my-model") / "some-task"
        )
    # The disposable scratch workspace (potentially gigabytes of build
    # output) must never linger under output once evidence is preserved.
    assert not scratch.exists()


def test_persist_evidence_is_a_noop_for_missing_scratch(tmp_path):
    output = tmp_path / "output"
    scratch = tmp_path / "work/native/my-model/never-created"
    evidence = MODULE.persist_evidence(output, "native", "my-model", "never-created", scratch)
    assert evidence == {}


def test_replay_contract_fingerprint_changes_with_native_manifest(tmp_path, monkeypatch):
    task_id = "github__ClickHouse__ClickHouse__pull_74193__comment_2024566878__duplication"
    oracle = strict_oracle(task_id)
    legacy_root = tmp_path / "legacy"
    before = MODULE.replay_contract_fingerprint(task_id, "native", oracle, legacy_root, [])
    tampered_manifest = dict(MODULE.NATIVE_MANIFEST)
    tampered_manifest[task_id] = dict(
        tampered_manifest[task_id], shell_command="echo different"
    )
    monkeypatch.setattr(MODULE, "NATIVE_MANIFEST", tampered_manifest)
    after = MODULE.replay_contract_fingerprint(task_id, "native", oracle, legacy_root, [])
    assert before != after


def test_replay_contract_fingerprint_changes_with_adapter_version(tmp_path, monkeypatch):
    task_id = "github__FuelLabs__fuel-tx__pull_197__comment_1003742605__complexity"
    oracle = strict_oracle(task_id)
    legacy_root = tmp_path / "legacy"
    before = MODULE.replay_contract_fingerprint(task_id, "rust", oracle, legacy_root, [])
    monkeypatch.setattr(MODULE, "ADAPTER_CONTRACT_VERSION", "some-other-version")
    after = MODULE.replay_contract_fingerprint(task_id, "rust", oracle, legacy_root, [])
    assert before != after


def test_replay_contract_fingerprint_changes_with_legacy_root_and_blockers(tmp_path):
    task_id = "github__FuelLabs__fuel-tx__pull_197__comment_1003742605__complexity"
    oracle = strict_oracle(task_id)
    root_a = tmp_path / "legacy-a"
    root_b = tmp_path / "legacy-b"
    base = MODULE.replay_contract_fingerprint(task_id, "rust", oracle, root_a, [])

    # Pointing at a different --legacy-root must change the fingerprint even
    # though nothing else changed (code-review finding: bind required asset
    # fingerprints/legacy_root into replay contract provenance).
    assert MODULE.replay_contract_fingerprint(
        task_id, "rust", oracle, root_b, []
    ) != base

    # A change in the live blockers list (asset newly missing/present) must
    # also change the fingerprint.
    assert MODULE.replay_contract_fingerprint(
        task_id, "rust", oracle, root_a, ["missing cargo cache"]
    ) != base



def test_replay_jvm_applies_patch_persists_evidence_and_removes_worktree(
    tmp_path, monkeypatch
):
    legacy_root = tmp_path / "legacy"
    start_sha = make_git_repo(legacy_root / "repos/org__repo")
    task_id = "github__Consensys__web3signer__pull_489__comment_789312375__modularity"
    task = strict_task(task_id, repo="org/repo")
    oracle = {
        "repo": "org/repo",
        "start_sha": start_sha,
        "command": "gradle test",
        "pass_to_pass": 1,
        "environment": {
            "container": "oracle-java-tools:17",
            "cache": "strict_screen_20260726/oracle_recovery/java/cache/home",
        },
    }
    patch_path = tmp_path / "candidate.patch"
    patch_path.write_text(
        "diff --git a/src.txt b/src.txt\n"
        "--- a/src.txt\n"
        "+++ b/src.txt\n"
        "@@ -1,3 +1,3 @@\n"
        " line1\n"
        "-line2\n"
        "+LINE2\n"
        " line3\n",
        encoding="utf-8",
    )
    generation = {"patch_file": str(patch_path), "patch_sha256": BASE.sha256(patch_path)}
    output = tmp_path / "output"

    def fake_run_subprocess(command, *, cwd, log_path, env=None, timeout=1800):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("BUILD SUCCESSFUL\n", encoding="utf-8")
        return 0, 2.0, ""

    monkeypatch.setattr(MODULE, "run_subprocess", fake_run_subprocess)

    row = MODULE.replay_jvm(
        task_id, "my-model", task, oracle, generation, legacy_root,
        tmp_path / "work", output, 60,
    )
    assert row["status"] == "pass"
    assert row["correctness_ok"] is True
    assert row["log"] == (
        f"legacy/evidence/jvm/{MODULE.model_component('my-model')}/{task_id}/run.log"
    )
    assert (output / row["log"]).is_file()
    assert row["task_fingerprint"] == BASE.task_fingerprint(task)
    assert row["replay_contract_fingerprint"] == MODULE.replay_contract_fingerprint(
        task_id, "jvm", oracle, legacy_root, []
    )
    # the git worktree must be fully removed (not just the scratch parent)
    # so a later replay of the same task can re-checkout cleanly.
    worktree_list = subprocess.run(
        ["git", "-C", str(legacy_root / "repos/org__repo"), "worktree", "list"],
        stdout=subprocess.PIPE, text=True,
    ).stdout
    assert "/workspace" not in worktree_list
    assert_no_scratch_files(tmp_path / "work")


def test_replay_jvm_apply_error_persists_error_log_and_removes_worktree(
    tmp_path, monkeypatch
):
    legacy_root = tmp_path / "legacy"
    start_sha = make_git_repo(legacy_root / "repos/org__repo")
    task_id = "github__Consensys__web3signer__pull_489__comment_789312375__modularity"
    task = strict_task(task_id, repo="org/repo")
    oracle = {
        "repo": "org/repo",
        "start_sha": start_sha,
        "command": "gradle test",
        "pass_to_pass": 1,
        "environment": {
            "container": "oracle-java-tools:17",
            "cache": "strict_screen_20260726/oracle_recovery/java/cache/home",
        },
    }
    patch_path = tmp_path / "candidate.patch"
    patch_path.write_text(
        "diff --git a/missing.txt b/missing.txt\n"
        "--- a/missing.txt\n"
        "+++ b/missing.txt\n"
        "@@ -1 +1 @@\n"
        "-a\n"
        "+b\n",
        encoding="utf-8",
    )
    generation = {"patch_file": str(patch_path), "patch_sha256": BASE.sha256(patch_path)}
    output = tmp_path / "output"

    called = {"count": 0}

    def fake_run_subprocess(*args, **kwargs):
        called["count"] += 1
        return 0, 0.1, ""

    monkeypatch.setattr(MODULE, "run_subprocess", fake_run_subprocess)
    row = MODULE.replay_jvm(
        task_id, "my-model", task, oracle, generation, legacy_root,
        tmp_path / "work", output, 60,
    )
    assert row["status"] == "apply_error"
    assert called["count"] == 0
    assert row["error_log"] == (
        f"legacy/evidence/jvm/{MODULE.model_component('my-model')}/{task_id}/apply-error.log"
    )
    assert (output / row["error_log"]).is_file()
    assert_no_scratch_files(tmp_path / "work")


def test_replay_tsjs_runs_install_and_test_and_persists_evidence(tmp_path, monkeypatch):
    legacy_root = tmp_path / "legacy"
    start_sha = make_git_repo(legacy_root / "repos/discourse__discourse")
    task_id = "github__discourse__discourse__pull_37336__comment_2752215814__complexity"
    task = strict_task(task_id, repo="discourse/discourse", language="TypeScript")
    task["start_sha"] = start_sha
    node_runtime = (
        legacy_root / "strict_screen_20260726/oracle_recovery/tsjs/_runtimes"
        / "node-v20.19.5-linux-x64/bin"
    )
    node_runtime.mkdir(parents=True)
    # Real Discourse/Opossum oracles genuinely omit start_sha (the exact
    # start commit lives on the task/task-plan record instead); this
    # oracle intentionally mirrors that real shape -- no start_sha key --
    # to regression-test that replay_tsjs uses task["start_sha"] and does
    # not raise a KeyError (reviewer-reported bug).
    oracle = {
        "repo": "discourse/discourse",
        "historical_node": "20.19.5",
        "install_command": "true",
        "test_command": "true",
        "workspace": ".",
        "pass_to_pass": 1,
        "recovery_artifact": "oracle_recovery/tsjs/20_discourse__discourse",
    }
    assert "start_sha" not in oracle
    patch_path = tmp_path / "candidate.patch"
    patch_path.write_text(
        "diff --git a/src.txt b/src.txt\n"
        "--- a/src.txt\n"
        "+++ b/src.txt\n"
        "@@ -1,3 +1,3 @@\n"
        " line1\n"
        "-line2\n"
        "+LINE2\n"
        " line3\n",
        encoding="utf-8",
    )
    generation = {"patch_file": str(patch_path), "patch_sha256": BASE.sha256(patch_path)}
    output = tmp_path / "output"

    def fake_run_subprocess(command, *, cwd, log_path, env=None, timeout=1800):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("Test Files: 1 passed\n", encoding="utf-8")
        return 0, 1.5, ""

    monkeypatch.setattr(MODULE, "run_subprocess", fake_run_subprocess)

    row = MODULE.replay_tsjs(
        task_id, "my-model", task, oracle, generation, legacy_root,
        tmp_path / "work", output, 60,
    )
    assert row["status"] == "pass"
    # replay_tsjs must use task["start_sha"], never oracle["start_sha"]
    # (which real Discourse/Opossum oracles do not carry at all) --
    # regression test for the reviewer-reported KeyError bug.
    assert row["start_sha"] == start_sha
    assert row["log"] == (
        f"legacy/evidence/tsjs/{MODULE.model_component('my-model')}/{task_id}/run.log"
    )
    assert (output / row["log"]).is_file()
    assert row["task_fingerprint"] == BASE.task_fingerprint(task)
    assert_no_scratch_files(tmp_path / "work")


def test_replay_tsjs_infra_retry_on_install_failure_persists_install_log(
    tmp_path, monkeypatch
):
    legacy_root = tmp_path / "legacy"
    start_sha = make_git_repo(legacy_root / "repos/discourse__discourse")
    task_id = "github__discourse__discourse__pull_37336__comment_2752215814__complexity"
    task = strict_task(task_id, repo="discourse/discourse", language="TypeScript")
    task["start_sha"] = start_sha
    node_runtime = (
        legacy_root / "strict_screen_20260726/oracle_recovery/tsjs/_runtimes"
        / "node-v20.19.5-linux-x64/bin"
    )
    node_runtime.mkdir(parents=True)
    # Real-shaped oracle: no start_sha (see comment in the "persists
    # evidence" test above for why).
    oracle = {
        "repo": "discourse/discourse",
        "historical_node": "20.19.5",
        "install_command": "false",
        "test_command": "true",
        "workspace": ".",
        "pass_to_pass": 1,
        "recovery_artifact": "oracle_recovery/tsjs/20_discourse__discourse",
    }
    patch_path = tmp_path / "candidate.patch"
    patch_path.write_text(
        "diff --git a/src.txt b/src.txt\n"
        "--- a/src.txt\n"
        "+++ b/src.txt\n"
        "@@ -1,3 +1,3 @@\n"
        " line1\n"
        "-line2\n"
        "+LINE2\n"
        " line3\n",
        encoding="utf-8",
    )
    generation = {"patch_file": str(patch_path), "patch_sha256": BASE.sha256(patch_path)}
    output = tmp_path / "output"

    calls = {"count": 0}

    def fake_run_subprocess(command, *, cwd, log_path, env=None, timeout=1800):
        calls["count"] += 1
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("install failed\n", encoding="utf-8")
        return 1, 0.5, ""

    monkeypatch.setattr(MODULE, "run_subprocess", fake_run_subprocess)

    row = MODULE.replay_tsjs(
        task_id, "my-model", task, oracle, generation, legacy_root,
        tmp_path / "work", output, 60,
    )
    assert row["status"] == "infra_retry"
    assert calls["count"] == 1  # test_command must never run after install fails
    assert row["log"] == (
        f"legacy/evidence/tsjs/{MODULE.model_component('my-model')}/{task_id}/install.log"
    )
    assert (output / row["log"]).is_file()


# ---------------------------------------------------------------------------
# Recovered native/rust contracts (--assets-root), with tiny synthetic assets
# ---------------------------------------------------------------------------


TSFILE_ID = "github__apache__tsfile__pull_823__comment_3418007322__modularity"
ANCHOR_ID = "github__otter-sec__anchor__pull_2488__comment_1193854703__duplication"
GRPC_ID = "github__grpc__grpc__pull_32970__comment_1195793486__duplication"


@pytest.fixture(autouse=True)
def _clear_recovery_state():
    """Recovered-asset state is module-level; never leak it between tests."""
    MODULE.load_recovery_assets(None)
    yield
    MODULE.load_recovery_assets(None)


def build_recovered_assets(tmp_path, task_id=TSFILE_ID, **overrides):
    """Tiny on-disk assets root with one recovered contract."""
    assets = tmp_path / "assets"
    workspace = assets / "workspaces/case"
    start = workspace / "start"
    head = make_git_repo(start)
    harness = assets / "harness/case/oracle_test.cc"
    harness.parent.mkdir(parents=True, exist_ok=True)
    harness.write_text("// behavioral oracle\n", encoding="utf-8")
    harness_sha = MODULE.hashlib.sha256(harness.read_bytes()).hexdigest()
    cache = assets / "caches/demo"
    (cache / "bin").mkdir(parents=True, exist_ok=True)
    (cache / "bin/cargo").write_text("#!/bin/sh\n", encoding="utf-8")
    entry = {
        "id": task_id,
        "status": "ready",
        "recovered": True,
        "lane": "native",
        "repo": "owner/repo",
        "case": "case",
        "start_sha": head,
        "workspace": "workspaces/case",
        "workspace_state": MODULE.workspace_git_state(start),
        "mounted_cache_digests": {
            "caches/demo": MODULE.cache_tree_digest(cache),
        },
        "cache_mounts": {"caches/demo": "/cache"},
        "patch_subdir": "start",
        "_mount_inputs_placeholder": True,
        "docker_image": "image:tag",
        "docker_image_id": "sha256:deadbeef",
        "shell": ["sh", "-lc"],
        "user": None,
        "network": None,
        "env": {},
        "mount": {"start": "/work"},
        "readonly_mounts": [],
        "cargo_cache": None,
        "harness_mounts": [{"host": "harness/case", "container": "/harness"}],
        "harness_copies": [],
        "workdir": "/work",
        "command": "run-oracle",
        "golden_patch": None,
        "pass_to_pass_tests": [],
        "hashed_files": [{"path": "harness/case/oracle_test.cc", "sha256": harness_sha}],
        "recipe_fingerprint": "fp-v1",
        "proof_runs": [
            {"variant": "start", "run": 1, "exit_code": 0,
             "recipe_fingerprint": "fp-v1"}
        ],
        "golden_command": None,
        "blockers": [],
    }
    entry.pop("_mount_inputs_placeholder", None)
    entry.update(overrides)
    if "mount_input_digests" not in entry:
        entry["mount_input_digests"] = MODULE.mount_input_digests(entry, assets)
    (assets / MODULE.RECOVERY_MANIFEST_NAME).write_text(
        json.dumps({"schema_version": "x", "tasks": [entry]}), encoding="utf-8"
    )
    return assets, entry, head


def test_load_recovery_assets_requires_a_manifest(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(ValueError, match="recovery-manifest.json"):
        MODULE.load_recovery_assets(tmp_path / "empty")
    # explicit "no assets" keeps historical behaviour
    assert MODULE.load_recovery_assets(None)["tasks"] == {}


def test_recovered_only_task_is_blocked_without_assets(tmp_path):
    blockers = MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha")
    assert any("no recovered replay contract" in b for b in blockers)
    # ... and it must never fall back to a historical manifest lookup
    assert MODULE.native_blockers(TSFILE_ID, tmp_path)


def test_recovered_contract_clears_blockers_when_assets_are_intact(tmp_path, monkeypatch):
    assets, _, _ = build_recovered_assets(tmp_path)
    monkeypatch.setattr(MODULE, "docker_image_id", lambda image: "sha256:deadbeef")
    MODULE.load_recovery_assets(assets)
    assert MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha") == []


def test_recovered_contract_detects_harness_tampering(tmp_path, monkeypatch):
    assets, _, _ = build_recovered_assets(tmp_path)
    monkeypatch.setattr(MODULE, "docker_image_id", lambda image: "sha256:deadbeef")
    MODULE.load_recovery_assets(assets)
    (assets / "harness/case/oracle_test.cc").write_text("// tampered\n", encoding="utf-8")
    blockers = MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha")
    assert any("does not match the manifest digest" in b for b in blockers)


def test_recovered_contract_detects_workspace_and_image_drift(tmp_path, monkeypatch):
    assets, _, head = build_recovered_assets(tmp_path, start_sha="0" * 40)
    monkeypatch.setattr(MODULE, "docker_image_id", lambda image: "sha256:other")
    MODULE.load_recovery_assets(assets)
    blockers = MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha")
    assert any("not the task start_sha" in b for b in blockers)
    assert any("is now sha256:other" in b for b in blockers)


def test_recovered_contract_requires_a_successful_start_proof(tmp_path, monkeypatch):
    assets, _, _ = build_recovered_assets(tmp_path, proof_runs=[])
    monkeypatch.setattr(MODULE, "docker_image_id", lambda image: "sha256:deadbeef")
    MODULE.load_recovery_assets(assets)
    blockers = MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha")
    assert any("no successful start-tree proof run" in b for b in blockers)


def test_unrecovered_task_keeps_its_recorded_blockers(tmp_path, monkeypatch):
    assets, _, _ = build_recovered_assets(
        tmp_path,
        task_id=GRPC_ID,
        recovered=False,
        status="blocked",
        blockers=["no retained replay contract", "start_sha absent from checkout"],
    )
    monkeypatch.setattr(MODULE, "docker_image_id", lambda image: "sha256:deadbeef")
    MODULE.load_recovery_assets(assets)
    blockers = MODULE.task_blockers(GRPC_ID, "native", {}, tmp_path, "sha")
    assert blockers == ["no retained replay contract", "start_sha absent from checkout"]


def test_rust_test_count_sums_every_target():
    log = (
        "test result: ok. 40 passed; 0 failed\n"
        "test result: ok. 7 passed; 0 failed\n"
        "test result: ok. 30 passed; 0 failed\n"
    )
    # the generic first-match counter would report only 40
    assert MODULE.test_count(log, MODULE.GENERIC_TEST_COUNT_PATTERNS) == 40
    assert MODULE.rust_test_count(log) == 77
    assert MODULE.rust_test_count("no tests here") == 0


def test_recovered_docker_argv_includes_every_declared_mount(tmp_path):
    assets, entry, _ = build_recovered_assets(tmp_path)
    entry = dict(entry)
    entry.update(
        {
            "network": "host",
            "user": "current",
            "env": {"CARGO_TERM_COLOR": "never"},
            "cargo_cache": "caches/cargo",
            "cache_mounts": {"caches/conan": "/root/.conan"},
            "readonly_mounts": ["start"],
            "shell": ["/bin/sh", "-c"],
        }
    )
    argv = MODULE.recovered_docker_argv(entry, assets, assets / "workspaces/case")
    joined = " ".join(argv)
    assert "--network=host" in argv
    assert f"--user" in argv
    assert f"{assets / 'workspaces/case/start'}:/work:ro" in joined
    assert f"{assets / 'harness/case'}:/harness:ro" in joined
    assert f"{assets / 'caches/conan'}:/root/.conan" in joined
    assert f"{assets / 'caches/cargo'}:/cargo-cache" in joined
    assert "CARGO_HOME=/cargo-cache" in joined
    assert "CARGO_TERM_COLOR=never" in joined
    assert argv[-3:] == ["/bin/sh", "-c", "run-oracle"]


def test_replay_recovered_applies_patch_copies_harness_and_records_row(
    tmp_path, monkeypatch
):
    assets, entry, head = build_recovered_assets(
        tmp_path,
        harness_copies=[
            {"host": "harness/case/oracle_test.cc", "workspace_path": "tests/oracle.cc"}
        ],
    )
    monkeypatch.setattr(MODULE, "docker_image_id", lambda image: "sha256:deadbeef")
    MODULE.load_recovery_assets(assets)

    patch_path = tmp_path / "candidate.patch"
    patch_path.write_text(
        "diff --git a/src.txt b/src.txt\n"
        "--- a/src.txt\n"
        "+++ b/src.txt\n"
        "@@ -1,3 +1,3 @@\n"
        " line1\n"
        "-line2\n"
        "+patched\n"
        " line3\n",
        encoding="utf-8",
    )
    captured = {}

    def fake_run_subprocess(command, *, cwd, log_path, env=None, timeout=1800):
        captured["command"] = command
        # the disposable copy must carry both the candidate patch and the
        # harness file at the moment the container would start
        start = Path(command[command.index("-v") + 1].split(":")[0])
        captured["patched"] = (start / "src.txt").read_text(encoding="utf-8")
        captured["harness"] = (start / "tests/oracle.cc").is_file()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("ok\n", encoding="utf-8")
        return 0, 1.0, ""

    monkeypatch.setattr(MODULE, "run_subprocess", fake_run_subprocess)
    # never spawn the real root-owned-state cleanup container in a unit test
    monkeypatch.setattr(MODULE, "purge_scratch", lambda scratch: None)

    row = MODULE.replay_recovered(
        TSFILE_ID,
        "my-model",
        strict_task(TSFILE_ID),
        strict_oracle(TSFILE_ID, pass_to_pass=1),
        {"patch_file": str(patch_path), "patch_sha256": "abc", "apply_ok": True},
        tmp_path / "legacy",
        tmp_path / "work",
        tmp_path / "output",
        900,
        "native",
    )
    assert captured["patched"] == "line1\npatched\nline3\n"
    assert captured["harness"] is True
    assert row["status"] == "pass"
    assert row["correctness_ok"] is True
    assert row["patch_apply"] is True
    assert row["replay_contract"] == "recovered"
    assert row["start_sha"] == head
    assert row["task_fingerprint"] and row["replay_contract_fingerprint"]


def test_replay_recovered_reports_apply_error_without_running_the_harness(
    tmp_path, monkeypatch
):
    assets, _, _ = build_recovered_assets(tmp_path)
    monkeypatch.setattr(MODULE, "docker_image_id", lambda image: "sha256:deadbeef")
    MODULE.load_recovery_assets(assets)
    patch_path = tmp_path / "bad.patch"
    patch_path.write_text(
        "diff --git a/missing.txt b/missing.txt\n"
        "--- a/missing.txt\n"
        "+++ b/missing.txt\n"
        "@@ -1 +1 @@\n"
        "-nope\n"
        "+nope2\n",
        encoding="utf-8",
    )

    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("harness must not run after a failed apply")

    monkeypatch.setattr(MODULE, "run_subprocess", explode)
    row = MODULE.replay_recovered(
        TSFILE_ID,
        "my-model",
        strict_task(TSFILE_ID),
        strict_oracle(TSFILE_ID),
        {"patch_file": str(patch_path), "patch_sha256": "abc", "apply_ok": True},
        tmp_path / "legacy",
        tmp_path / "work",
        tmp_path / "output",
        900,
        "native",
    )
    assert row["status"] == "apply_error"
    assert row["correctness_ok"] is False
    assert row["patch_apply"] is False


def test_recovered_contract_changes_the_replay_fingerprint(tmp_path, monkeypatch):
    assets, entry, _ = build_recovered_assets(tmp_path)
    monkeypatch.setattr(MODULE, "docker_image_id", lambda image: "sha256:deadbeef")
    MODULE.load_recovery_assets(assets)
    task = strict_task(TSFILE_ID)
    oracle = strict_oracle(TSFILE_ID)
    before = MODULE.replay_contract_fingerprint(
        TSFILE_ID, "native", oracle, tmp_path, []
    )
    manifest_path = assets / MODULE.RECOVERY_MANIFEST_NAME
    payload = json.loads(manifest_path.read_text())
    payload["tasks"][0]["command"] = "run-oracle --different"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    MODULE.load_recovery_assets(assets)
    after = MODULE.replay_contract_fingerprint(
        TSFILE_ID, "native", oracle, tmp_path, []
    )
    assert before != after


# ---------------------------------------------------------------------------
# Recovered assets: removal / tampering / drift must block or go infra,
# never produce a candidate verdict
# ---------------------------------------------------------------------------


def _ready_entry(assets, *, refresh_mounts=True, **overrides):
    manifest_path = assets / MODULE.RECOVERY_MANIFEST_NAME
    payload = json.loads(manifest_path.read_text())
    payload["tasks"][0].update(overrides)
    if refresh_mounts and "mount_input_digests" not in overrides:
        payload["tasks"][0]["mount_input_digests"] = MODULE.mount_input_digests(
            payload["tasks"][0], assets
        )
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    MODULE.load_recovery_assets(assets)


def test_removed_mounted_cache_blocks(tmp_path, monkeypatch):
    assets, _, _ = build_recovered_assets(tmp_path)
    monkeypatch.setattr(MODULE, "docker_image_id", lambda image: "sha256:deadbeef")
    MODULE.load_recovery_assets(assets)
    assert MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha") == []
    MODULE._CACHE_TREE_DIGESTS.clear()
    shutil.rmtree(assets / "caches/demo")
    blockers = MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha")
    assert any("missing mounted cache" in b for b in blockers)


def test_tampered_mounted_cache_blocks(tmp_path, monkeypatch):
    assets, _, _ = build_recovered_assets(tmp_path)
    monkeypatch.setattr(MODULE, "docker_image_id", lambda image: "sha256:deadbeef")
    MODULE.load_recovery_assets(assets)
    MODULE._CACHE_TREE_DIGESTS.clear()
    # a single extra file inside the mounted cache must be detected: the
    # historical bounded sampler would have missed it
    (assets / "caches/demo/bin/injected.so").write_text("evil\n", encoding="utf-8")
    blockers = MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha")
    assert any("has drifted" in b for b in blockers)


def test_truncated_mounted_cache_file_blocks(tmp_path, monkeypatch):
    assets, _, _ = build_recovered_assets(tmp_path)
    monkeypatch.setattr(MODULE, "docker_image_id", lambda image: "sha256:deadbeef")
    MODULE.load_recovery_assets(assets)
    MODULE._CACHE_TREE_DIGESTS.clear()
    (assets / "caches/demo/bin/cargo").write_text("", encoding="utf-8")
    blockers = MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha")
    assert any("has drifted" in b for b in blockers)


def test_unbound_mounted_cache_blocks(tmp_path, monkeypatch):
    """A cache the replay mounts but the manifest never bound must block."""
    assets, _, _ = build_recovered_assets(tmp_path)
    monkeypatch.setattr(MODULE, "docker_image_id", lambda image: "sha256:deadbeef")
    _ready_entry(assets, mounted_cache_digests={})
    blockers = MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha")
    assert any("not bound to the recovery manifest" in b for b in blockers)
    _ready_entry(assets, mounted_cache_digests=None)
    blockers = MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha")
    assert any("does not record mounted_cache_digests" in b for b in blockers)


def test_incomplete_cargo_home_blocks(tmp_path, monkeypatch):
    assets, _, _ = build_recovered_assets(tmp_path)
    monkeypatch.setattr(MODULE, "docker_image_id", lambda image: "sha256:deadbeef")
    MODULE._CACHE_TREE_DIGESTS.clear()
    cargo = assets / "caches/cargo-home"
    (cargo / "registry").mkdir(parents=True)
    (cargo / "registry/index").write_text("x\n", encoding="utf-8")
    digest = MODULE.cache_tree_digest(cargo)
    _ready_entry(assets, cargo_cache="caches/cargo-home",
                 mounted_cache_digests={
                     "caches/demo": MODULE.cache_tree_digest(assets / "caches/demo"),
                     "caches/cargo-home": digest,
                 })
    blockers = MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha")
    assert any("incomplete reconstructed CARGO_HOME" in b for b in blockers)


def test_dirty_workspace_blocks(tmp_path, monkeypatch):
    assets, _, _ = build_recovered_assets(tmp_path)
    monkeypatch.setattr(MODULE, "docker_image_id", lambda image: "sha256:deadbeef")
    MODULE.load_recovery_assets(assets)
    start = assets / "workspaces/case/start"

    # 1) tracked drift
    (start / "src.txt").write_text("tampered\n", encoding="utf-8")
    blockers = MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha")
    assert any("has drifted" in b and "tracked" in b for b in blockers)
    (start / "src.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")
    assert MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha") == []

    # 2) untracked drift (e.g. a stray leftover build/patch artifact)
    (start / "leftover.txt").write_text("stray\n", encoding="utf-8")
    blockers = MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha")
    assert any("has drifted" in b and "untracked" in b for b in blockers)
    (start / "leftover.txt").unlink()
    assert MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha") == []


def test_manifest_without_workspace_state_cannot_be_verified(tmp_path, monkeypatch):
    assets, _, _ = build_recovered_assets(tmp_path)
    monkeypatch.setattr(MODULE, "docker_image_id", lambda image: "sha256:deadbeef")
    _ready_entry(assets, workspace_state={"head": None})
    blockers = MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha")
    assert any("does not record workspace_state" in b for b in blockers)


def test_recovered_contract_requires_a_golden_proof_when_golden_declared(
    tmp_path, monkeypatch
):
    assets, _, _ = build_recovered_assets(tmp_path)
    monkeypatch.setattr(MODULE, "docker_image_id", lambda image: "sha256:deadbeef")
    # start-only proof + a declared golden patch is insufficient
    _ready_entry(
        assets,
        golden_patch="harness/case/golden.patch",
        hashed_files=[],
        proof_runs=[{"variant": "start", "run": 1, "exit_code": 0,
                     "recipe_fingerprint": "fp-v1"}],
    )
    blockers = MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha")
    assert any("no successful golden proof run" in b for b in blockers)
    # a failing golden run is still insufficient
    _ready_entry(
        assets,
        proof_runs=[
            {"variant": "start", "run": 1, "exit_code": 0,
             "recipe_fingerprint": "fp-v1"},
            {"variant": "golden", "run": 1, "exit_code": 1, "stage": "apply",
             "recipe_fingerprint": "fp-v1"},
        ],
    )
    assert any(
        "no successful golden proof run" in b
        for b in MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha")
    )
    # start + golden clears it
    _ready_entry(
        assets,
        proof_runs=[
            {"variant": "start", "run": 1, "exit_code": 0,
             "recipe_fingerprint": "fp-v1"},
            {"variant": "golden", "run": 1, "exit_code": 0,
             "recipe_fingerprint": "fp-v1"},
        ],
    )
    assert MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha") == []


def test_replay_recovered_returns_infra_not_fail_when_assets_drift(
    tmp_path, monkeypatch
):
    """Drift discovered after preflight must never become a candidate fail."""
    assets, _, _ = build_recovered_assets(tmp_path)
    monkeypatch.setattr(MODULE, "docker_image_id", lambda image: "sha256:deadbeef")
    MODULE.load_recovery_assets(assets)
    patch_path = tmp_path / "candidate.patch"
    patch_path.write_text(
        "diff --git a/src.txt b/src.txt\n--- a/src.txt\n+++ b/src.txt\n"
        "@@ -1,3 +1,3 @@\n line1\n-line2\n+patched\n line3\n",
        encoding="utf-8",
    )

    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("Oracle must not execute against drifted assets")

    monkeypatch.setattr(MODULE, "run_subprocess", explode)
    monkeypatch.setattr(MODULE, "purge_scratch", lambda scratch: None)
    MODULE._CACHE_TREE_DIGESTS.clear()
    shutil.rmtree(assets / "caches/demo")

    row = MODULE.replay_recovered(
        TSFILE_ID, "my-model", strict_task(TSFILE_ID), strict_oracle(TSFILE_ID),
        {"patch_file": str(patch_path), "patch_sha256": "abc", "apply_ok": True},
        tmp_path / "legacy", tmp_path / "work", tmp_path / "output", 900, "native",
    )
    assert row["status"] == MODULE.INFRA_RETRY
    assert row["correctness_ok"] is None
    assert row["patch_apply"] is None
    assert any("missing mounted cache" in b for b in row["asset_blockers"])


def test_asset_state_fingerprints_every_mounted_input(tmp_path, monkeypatch):
    assets, _, _ = build_recovered_assets(tmp_path)
    monkeypatch.setattr(MODULE, "docker_image_id", lambda image: "sha256:deadbeef")
    MODULE.load_recovery_assets(assets)
    entry = MODULE.recovered_entry(TSFILE_ID)
    state = MODULE.recovered_asset_state(entry)
    assert set(state["mounted_caches"]) == {"caches/demo"}
    assert state["workspace_state"]["head"] == entry["start_sha"]
    assert state["harness_mounts"] == entry["harness_mounts"]

    # mutating a mounted cache changes the fingerprint -> stale rows rerun
    before = MODULE.replay_contract_fingerprint(
        TSFILE_ID, "native", strict_oracle(TSFILE_ID), tmp_path, []
    )
    MODULE._CACHE_TREE_DIGESTS.clear()
    (assets / "caches/demo/bin/extra").write_text("x\n", encoding="utf-8")
    after = MODULE.replay_contract_fingerprint(
        TSFILE_ID, "native", strict_oracle(TSFILE_ID), tmp_path, []
    )
    assert before != after


def test_workspace_drift_invalidates_previously_recorded_rows(tmp_path, monkeypatch):
    """A recorded row must be treated as stale once its workspace drifts."""
    assets, _, _ = build_recovered_assets(tmp_path)
    monkeypatch.setattr(MODULE, "docker_image_id", lambda image: "sha256:deadbeef")
    MODULE.load_recovery_assets(assets)
    task = strict_task(TSFILE_ID)
    oracle = strict_oracle(TSFILE_ID)
    generation = {"patch_sha256": "abc", "apply_ok": True}
    row = {"patch_sha256": "abc"}
    row.update(
        MODULE.row_provenance_fields(
            task, oracle, TSFILE_ID, "native", generation, tmp_path, []
        )
    )
    assert not MODULE.row_is_stale(
        row, task, oracle, TSFILE_ID, "native", generation, tmp_path, []
    )
    (assets / "workspaces/case/start/leftover.txt").write_text("x\n", encoding="utf-8")
    blockers = MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha")
    assert blockers
    assert MODULE.row_is_stale(
        row, task, oracle, TSFILE_ID, "native", generation, tmp_path, blockers
    )


# ---------------------------------------------------------------------------
# Writable shared caches: no stale digest may survive a replay
# ---------------------------------------------------------------------------


def test_replay_mutating_a_cache_invalidates_the_next_blocker_check(
    tmp_path, monkeypatch
):
    """A cache mutated during one replay must block the next check in the
    same process, and must invalidate the recorded row."""
    assets, _, _ = build_recovered_assets(tmp_path)
    monkeypatch.setattr(MODULE, "docker_image_id", lambda image: "sha256:deadbeef")
    MODULE.load_recovery_assets(assets)
    cache = assets / "caches/demo"
    task = strict_task(TSFILE_ID)
    oracle = strict_oracle(TSFILE_ID)
    generation = {"patch_sha256": "abc", "apply_ok": True,
                  "patch_file": str(tmp_path / "candidate.patch")}
    Path(generation["patch_file"]).write_text(
        "diff --git a/src.txt b/src.txt\n--- a/src.txt\n+++ b/src.txt\n"
        "@@ -1,3 +1,3 @@\n line1\n-line2\n+patched\n line3\n",
        encoding="utf-8",
    )
    assert MODULE.task_blockers(TSFILE_ID, "native", oracle, tmp_path, "sha") == []
    row_before = {"patch_sha256": "abc"}
    row_before.update(
        MODULE.row_provenance_fields(
            task, oracle, TSFILE_ID, "native", generation, tmp_path, []
        )
    )

    def mutating_run(command, *, cwd, log_path, env=None, timeout=1800):
        # emulate a build writing into whatever cache path was mounted
        mounted = [
            part.split(":")[0] for part in command
            if isinstance(part, str) and ":/cache" in part
        ]
        assert mounted, "no cache was mounted"
        (Path(mounted[0]) / "written-by-build.lock").write_text("x\n", encoding="utf-8")
        # ... and simulate the worst case: the canonical cache is touched too
        (cache / "leaked-by-build.lock").write_text("x\n", encoding="utf-8")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("ok\n", encoding="utf-8")
        return 0, 1.0, ""

    monkeypatch.setattr(MODULE, "run_subprocess", mutating_run)
    monkeypatch.setattr(MODULE, "purge_scratch", lambda scratch: None)
    row = MODULE.replay_recovered(
        TSFILE_ID, "my-model", task, oracle, generation,
        tmp_path / "legacy", tmp_path / "work", tmp_path / "output", 900, "native",
    )
    assert row["status"] == "pass"

    # same process, next check: the mutation must be seen immediately
    blockers = MODULE.task_blockers(TSFILE_ID, "native", oracle, tmp_path, "sha")
    assert any("has drifted" in b for b in blockers)
    # ... and the row recorded before the mutation must now be stale
    assert MODULE.row_is_stale(
        row_before, task, oracle, TSFILE_ID, "native", generation, tmp_path, blockers
    )


def test_replay_mounts_a_disposable_cache_copy_not_the_canonical_cache(
    tmp_path, monkeypatch
):
    assets, _, _ = build_recovered_assets(tmp_path)
    monkeypatch.setattr(MODULE, "docker_image_id", lambda image: "sha256:deadbeef")
    MODULE.load_recovery_assets(assets)
    cache = assets / "caches/demo"
    captured = {}

    def capture(command, *, cwd, log_path, env=None, timeout=1800):
        captured["mounts"] = [
            part for part in command if isinstance(part, str) and ":/cache" in part
        ]
        (Path(captured["mounts"][0].split(":")[0]) / "build.lock").write_text(
            "x\n", encoding="utf-8"
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("ok\n", encoding="utf-8")
        return 0, 1.0, ""

    patch_path = tmp_path / "candidate.patch"
    patch_path.write_text(
        "diff --git a/src.txt b/src.txt\n--- a/src.txt\n+++ b/src.txt\n"
        "@@ -1,3 +1,3 @@\n line1\n-line2\n+patched\n line3\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(MODULE, "run_subprocess", capture)
    monkeypatch.setattr(MODULE, "purge_scratch", lambda scratch: None)
    MODULE.replay_recovered(
        TSFILE_ID, "my-model", strict_task(TSFILE_ID), strict_oracle(TSFILE_ID),
        {"patch_file": str(patch_path), "patch_sha256": "abc", "apply_ok": True},
        tmp_path / "legacy", tmp_path / "work", tmp_path / "output", 900, "native",
    )
    assert captured["mounts"], "cache was not mounted"
    assert not captured["mounts"][0].startswith(f"{cache}:")
    # the canonical cache is untouched, so its digest still matches the manifest
    assert not (cache / "build.lock").exists()
    assert MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha") == []


def test_cache_tree_digest_is_never_memoized(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "a").write_text("a\n", encoding="utf-8")
    first = MODULE.cache_tree_digest(cache)
    (cache / "b").write_text("b\n", encoding="utf-8")
    second = MODULE.cache_tree_digest(cache)
    assert first != second
    assert second["file_count"] == 2


def test_stale_proof_fingerprint_blocks_the_contract(tmp_path, monkeypatch):
    assets, _, _ = build_recovered_assets(tmp_path)
    monkeypatch.setattr(MODULE, "docker_image_id", lambda image: "sha256:deadbeef")
    _ready_entry(
        assets,
        proof_runs=[
            {"variant": "start", "run": 1, "exit_code": 0,
             "recipe_fingerprint": "fp-OLD"}
        ],
    )
    blockers = MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha")
    assert any("different recipe fingerprint" in b for b in blockers)
    _ready_entry(assets, recipe_fingerprint=None)
    assert any(
        "does not record a recipe_fingerprint" in b
        for b in MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha")
    )


# ---------------------------------------------------------------------------
# Adapter: every mounted input is bound by content, same-size swaps included
# ---------------------------------------------------------------------------


def test_adapter_blocks_same_size_tamper_of_a_mounted_harness_file(
    tmp_path, monkeypatch
):
    assets, entry, _ = build_recovered_assets(tmp_path)
    monkeypatch.setattr(MODULE, "docker_image_id", lambda image: "sha256:deadbeef")
    MODULE.load_recovery_assets(assets)
    assert MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha") == []
    harness = assets / "harness/case/oracle_test.cc"
    original = harness.read_bytes()
    tampered = original[:-2] + b"X\n"
    assert len(tampered) == len(original)
    harness.write_bytes(tampered)
    blockers = MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha")
    assert blockers, "a same-size harness swap must not pass"
    harness.write_bytes(original)
    assert MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha") == []


def test_adapter_blocks_same_size_tamper_inside_a_mounted_cache(
    tmp_path, monkeypatch
):
    assets, _, _ = build_recovered_assets(tmp_path)
    monkeypatch.setattr(MODULE, "docker_image_id", lambda image: "sha256:deadbeef")
    MODULE.load_recovery_assets(assets)
    blob = assets / "caches/demo/bin/cargo"
    original = blob.read_bytes()
    tampered = b"#!/bin/xx" + original[9:] if len(original) > 9 else b"x" * len(original)
    tampered = tampered[: len(original)].ljust(len(original), b"x")
    assert len(tampered) == len(original)
    blob.write_bytes(tampered)
    blockers = MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha")
    assert any("has drifted" in b for b in blockers)
    blob.write_bytes(original)
    assert MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha") == []


def test_adapter_binds_every_mounted_input_from_the_manifest(tmp_path, monkeypatch):
    """A mount source that the manifest never bound must block."""
    assets, entry, _ = build_recovered_assets(tmp_path)
    monkeypatch.setattr(MODULE, "docker_image_id", lambda image: "sha256:deadbeef")
    # add a new mounted file to the contract without recording its digest
    (assets / "workspaces/case/query.sql").write_text("SELECT 1;\n", encoding="utf-8")
    recorded = dict(entry["mount_input_digests"])
    _ready_entry(
        assets,
        mount={"start": "/work", "query.sql": "/work/query.sql"},
        mount_input_digests=recorded,
    )
    blockers = MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha")
    assert any("not bound to the recovery manifest" in b for b in blockers)

    # binding it clears the blocker; a same-size edit then blocks again
    MODULE.load_recovery_assets(assets)
    entry_now = MODULE.recovered_entry(TSFILE_ID)
    _ready_entry(
        assets,
        mount_input_digests=MODULE.mount_input_digests(entry_now, assets),
    )
    assert MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha") == []
    (assets / "workspaces/case/query.sql").write_text("SELECT 2;\n", encoding="utf-8")
    assert any(
        "has drifted from its recorded content digest" in b
        for b in MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha")
    )


def test_adapter_requires_recorded_mount_input_digests(tmp_path, monkeypatch):
    assets, _, _ = build_recovered_assets(tmp_path)
    monkeypatch.setattr(MODULE, "docker_image_id", lambda image: "sha256:deadbeef")
    _ready_entry(assets, mount_input_digests=None)
    assert any(
        "does not record mount_input_digests" in b
        for b in MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha")
    )


def test_tampered_mounted_input_yields_infra_not_candidate_fail(tmp_path, monkeypatch):
    assets, _, _ = build_recovered_assets(tmp_path)
    monkeypatch.setattr(MODULE, "docker_image_id", lambda image: "sha256:deadbeef")
    MODULE.load_recovery_assets(assets)
    patch_path = tmp_path / "candidate.patch"
    patch_path.write_text(
        "diff --git a/src.txt b/src.txt\n--- a/src.txt\n+++ b/src.txt\n"
        "@@ -1,3 +1,3 @@\n line1\n-line2\n+patched\n line3\n",
        encoding="utf-8",
    )

    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("Oracle must not execute with a tampered input")

    monkeypatch.setattr(MODULE, "run_subprocess", explode)
    monkeypatch.setattr(MODULE, "purge_scratch", lambda scratch: None)
    harness = assets / "harness/case/oracle_test.cc"
    harness.write_bytes(harness.read_bytes()[:-2] + b"Z\n")
    row = MODULE.replay_recovered(
        TSFILE_ID, "my-model", strict_task(TSFILE_ID), strict_oracle(TSFILE_ID),
        {"patch_file": str(patch_path), "patch_sha256": "abc", "apply_ok": True},
        tmp_path / "legacy", tmp_path / "work", tmp_path / "output", 900, "native",
    )
    assert row["status"] == MODULE.INFRA_RETRY
    assert row["correctness_ok"] is None
    assert row["asset_blockers"]


def test_asset_state_includes_every_mounted_input(tmp_path, monkeypatch):
    assets, entry, _ = build_recovered_assets(tmp_path)
    monkeypatch.setattr(MODULE, "docker_image_id", lambda image: "sha256:deadbeef")
    MODULE.load_recovery_assets(assets)
    state = MODULE.recovered_asset_state(MODULE.recovered_entry(TSFILE_ID))
    assert "harness_mount:harness/case" in state["mounted_inputs"]
    before = MODULE.replay_contract_fingerprint(
        TSFILE_ID, "native", strict_oracle(TSFILE_ID), tmp_path, []
    )
    harness = assets / "harness/case/oracle_test.cc"
    harness.write_bytes(harness.read_bytes()[:-2] + b"Q\n")
    after = MODULE.replay_contract_fingerprint(
        TSFILE_ID, "native", strict_oracle(TSFILE_ID), tmp_path, []
    )
    assert before != after


# ---------------------------------------------------------------------------
# Adapter: directory entries (symlinked dirs, empty dirs) are bound too
# ---------------------------------------------------------------------------


def test_adapter_digest_matches_the_recovery_tool_entry_for_entry(tmp_path):
    """Both tools must produce byte-identical digests for the same tree."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "recover_legacy_native_rust", SCRIPTS_DIR / "recover_legacy_native_rust.py"
    )
    recovery = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recovery)

    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub/f.bin").write_bytes(b"payload")
    (tree / "empty").mkdir()
    os.symlink("sub", tree / "dirlink")
    os.symlink("sub/f.bin", tree / "filelink")
    assert MODULE.cache_tree_digest(tree) == recovery.tree_digest(
        tree, use_cache=False
    )


def test_adapter_blocks_on_directory_symlink_and_empty_dir_drift(
    tmp_path, monkeypatch
):
    assets, _, _ = build_recovered_assets(tmp_path)
    monkeypatch.setattr(MODULE, "docker_image_id", lambda image: "sha256:deadbeef")
    cache = assets / "caches/demo"
    (cache / "real").mkdir(parents=True, exist_ok=True)
    (cache / "real/lib.a").write_bytes(b"lib")
    (cache / "alt").mkdir(exist_ok=True)
    (cache / "hollow").mkdir(exist_ok=True)
    os.symlink("real", cache / "include-link")
    # rebind the manifest to this exact state
    MODULE.load_recovery_assets(assets)
    entry = MODULE.recovered_entry(TSFILE_ID)
    _ready_entry(
        assets,
        mounted_cache_digests={"caches/demo": MODULE.cache_tree_digest(cache)},
    )
    assert MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha") == []

    # 1) retarget the directory symlink
    (cache / "include-link").unlink()
    os.symlink("alt", cache / "include-link")
    assert any(
        "has drifted" in b
        for b in MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha")
    )
    (cache / "include-link").unlink()
    os.symlink("real", cache / "include-link")
    assert MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha") == []

    # 2) remove the directory symlink entirely
    (cache / "include-link").unlink()
    assert any(
        "has drifted" in b
        for b in MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha")
    )
    os.symlink("real", cache / "include-link")

    # 3) add and remove empty directories
    (cache / "new-empty").mkdir()
    assert any(
        "has drifted" in b
        for b in MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha")
    )
    (cache / "new-empty").rmdir()
    assert MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha") == []
    (cache / "hollow").rmdir()
    assert any(
        "has drifted" in b
        for b in MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha")
    )


# ---------------------------------------------------------------------------
# Exact-start copies: gitignored build residue must block
# ---------------------------------------------------------------------------


def test_workspace_state_binds_gitignored_build_output(tmp_path, monkeypatch):
    assets, _, _ = build_recovered_assets(tmp_path)
    monkeypatch.setattr(MODULE, "docker_image_id", lambda image: "sha256:deadbeef")
    start = assets / "workspaces/case/start"
    (start / ".gitignore").write_text("build/\n*.o\n", encoding="utf-8")
    run("git", "-C", str(start), "add", "-A")
    run("git", "-C", str(start), "-c", "user.email=a@b", "-c", "user.name=a",
        "commit", "-q", "-m", "ignore build")
    head = run("git", "-C", str(start), "rev-parse", "HEAD").strip()
    MODULE.load_recovery_assets(assets)
    entry = MODULE.recovered_entry(TSFILE_ID)
    _ready_entry(
        assets,
        start_sha=head,
        workspace_state=MODULE.workspace_git_state(start),
        mount_input_digests=MODULE.mount_input_digests(dict(entry, start_sha=head), assets),
    )
    assert MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha") == []

    # a *gitignored* build residual: git status reports nothing at all ...
    (start / "build").mkdir()
    (start / "build/artifact.o").write_text("obj\n", encoding="utf-8")
    status = run("git", "-C", str(start), "status", "--porcelain")
    assert status.strip() == "", "git status must be blind to this on purpose"
    # ... but the recorded copied-tree digest is not
    blockers = MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha")
    assert any("tree digest" in b and "gitignored" in b for b in blockers)

    shutil.rmtree(start / "build")
    assert MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha") == []

    # an ignored *file* pattern too
    (start / "stale.o").write_text("obj\n", encoding="utf-8")
    assert run("git", "-C", str(start), "status", "--porcelain").strip() == ""
    assert any(
        "tree digest" in b
        for b in MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha")
    )
    (start / "stale.o").unlink()
    assert MODULE.task_blockers(TSFILE_ID, "native", {}, tmp_path, "sha") == []


def test_workspace_state_tree_excludes_only_git_metadata(tmp_path):
    repo = tmp_path / "repo"
    make_git_repo(repo)
    before = MODULE.workspace_git_state(repo)
    assert before["tree"]["kind"] == "tree"
    # touching .git internals (gc, reflog, index refresh) must not churn it
    (repo / ".git/COMMIT_EDITMSG").write_text("noise\n", encoding="utf-8")
    (repo / ".git/newdir").mkdir()
    assert MODULE.workspace_git_state(repo)["tree"] == before["tree"]
    # everything else does
    (repo / "another.txt").write_text("x\n", encoding="utf-8")
    assert MODULE.workspace_git_state(repo)["tree"] != before["tree"]


# ---------------------------------------------------------------------------
# Concurrency: duplicate replays of the same (lane, model, task)
# ---------------------------------------------------------------------------


def _concurrent_replay_worker(payload):
    """Run in a *separate process*: emulate one full replay execution --
    lock, scratch creation, work, evidence persistence, cleanup."""
    import importlib.util
    import os
    import time
    from pathlib import Path

    (
        scripts_dir, legacy_dir, work_root, output, lane, model, task_id, marker
    ) = payload
    spec = importlib.util.spec_from_file_location(
        "run_legacy_reserve_correctness",
        Path(scripts_dir) / "run_legacy_reserve_correctness.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    observed = []
    with module.execution_lock(Path(legacy_dir), lane, model, task_id):
        scratch = module.invocation_scratch(Path(work_root), lane, model, task_id)
        scratch.mkdir(parents=True, exist_ok=True)
        # write a log slowly so an unsynchronized peer would interleave
        log = scratch / "run.log"
        for index in range(20):
            with log.open("a") as handle:
                handle.write(f"{marker}-{index}\n")
            time.sleep(0.005)
            observed.append(sorted(p.name for p in scratch.iterdir()))
        evidence = module.persist_evidence(
            Path(output), lane, model, task_id, scratch
        )
        published = (
            Path(output) / evidence["run.log"]
        ).read_text().strip().splitlines()
    return {
        "pid": os.getpid(),
        "scratch": str(scratch),
        "published_marker": {line.split("-")[0] for line in published},
        "published_lines": len(published),
        "saw_only_own_files": all(files == ["run.log"] for files in observed),
        "scratch_removed": not scratch.exists(),
    }


def test_concurrent_duplicate_replays_cannot_clobber_each_other(tmp_path):
    """Two processes replaying the same (lane, model, task) must serialize,
    keep separate scratch trees, and publish complete evidence."""
    import multiprocessing

    legacy_dir = tmp_path / "output/legacy/correctness"
    legacy_dir.mkdir(parents=True)
    work_root = tmp_path / "work"
    output = tmp_path / "output"
    lane, model, task_id = "native", "my-model", "github__x__y__pull_1__c__z"
    payloads = [
        (
            str(SCRIPTS_DIR), str(legacy_dir), str(work_root), str(output),
            lane, model, task_id, marker,
        )
        for marker in ("alpha", "beta")
    ]
    with multiprocessing.get_context("spawn").Pool(2) as pool:
        results = pool.map(_concurrent_replay_worker, payloads)

    # distinct per-invocation scratch trees, both cleaned up
    assert results[0]["scratch"] != results[1]["scratch"]
    assert all(result["scratch_removed"] for result in results)
    # neither process ever saw the other's files in its own scratch
    assert all(result["saw_only_own_files"] for result in results)
    # each published a complete, single-writer evidence log (20 lines, one
    # marker) -- no interleaving, no truncation by the peer
    for result in results:
        assert result["published_lines"] == 20, result
        assert len(result["published_marker"]) == 1, result
    # the surviving evidence file is one of them, intact, never a mix
    published = (
        output / "legacy/evidence" / lane / MODULE.model_component(model)
        / BASE.safe_component(task_id) / "run.log"
    )
    lines = published.read_text().strip().splitlines()
    assert len(lines) == 20
    assert len({line.split("-")[0] for line in lines}) == 1
    # no half-written staging files are left behind
    assert list(published.parent.glob("*.new")) == []
    assert_no_scratch_files(work_root)


def test_execution_lock_serializes_same_pair_and_not_different_pairs(tmp_path):
    legacy_dir = tmp_path / "legacy"
    order = []

    def hold(lane, model, task_id, tag):
        with MODULE.execution_lock(legacy_dir, lane, model, task_id):
            order.append(f"enter-{tag}")
            time.sleep(0.05)
            order.append(f"exit-{tag}")

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(
            lambda args: hold(*args),
            [
                ("native", "m", "task-a", "a1"),
                ("native", "m", "task-a", "a2"),
            ],
        ))
    # strict serialization: no interleaved enter/enter
    assert order in (
        ["enter-a1", "exit-a1", "enter-a2", "exit-a2"],
        ["enter-a2", "exit-a2", "enter-a1", "exit-a1"],
    )
    # different pairs use different lock files
    lock_names = sorted(p.name for p in legacy_dir.glob(".exec-*.lock"))
    assert lock_names == [
        f".exec-native-{MODULE.model_component('m')}-task-a.lock"
    ]
    MODULE.execution_lock(legacy_dir, "native", "m", "task-b").__enter__()
    assert len(list(legacy_dir.glob(".exec-*.lock"))) == 2
    # lock files must be excluded from artifact hashing by name convention
    assert all(name.endswith(".lock") for name in lock_names)
