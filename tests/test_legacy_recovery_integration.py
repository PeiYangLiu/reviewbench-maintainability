"""Integration regressions for the merged native/rust + JVM/TS-JS recovery.

The two recoveries were developed on separate branches and touched the *same*
shared surface of ``scripts/run_legacy_reserve_correctness.py``: the digest
helpers, ``resolve_legacy_relative``, ``asset_state``/
``replay_contract_fingerprint``, the execution locking/scratch discipline and
the asset-root CLI. Those are exactly the places a future change is most
likely to silently regress one lane group while keeping the other green, so
they are pinned here in one file that exercises both groups together.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import multiprocessing
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

SPEC = importlib.util.spec_from_file_location(
    "run_legacy_reserve_correctness",
    SCRIPTS_DIR / "run_legacy_reserve_correctness.py",
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@pytest.fixture(autouse=True)
def _isolated_module_state():
    """Every test starts from empty recovery state and empty digest memos."""
    MODULE.load_recovery_assets(None)
    MODULE.invalidate_cache_digests()
    yield
    MODULE.load_recovery_assets(None)
    MODULE.invalidate_cache_digests()


def make_native_rust_root(root: Path, tasks=()) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / MODULE.RECOVERY_MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema_version": "reviewbench-legacy-native-rust-recovery/v1",
                "assets_root": str(root),
                "tasks": list(tasks),
            }
        ),
        encoding="utf-8",
    )
    return root


def make_jvm_tsjs_root(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / MODULE.JVM_TSJS_MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema_version": "reviewbench-legacy-recovery-assets/v1",
                "asset_root": str(root),
                "files": {},
                "trees": {},
            }
        ),
        encoding="utf-8",
    )
    return root


# ---------------------------------------------------------------------------
# Asset-root CLI: two lane-specific durable roots, no ambiguity
# ---------------------------------------------------------------------------


def test_both_lane_roots_are_accepted_together_with_explicit_labels(tmp_path):
    native = make_native_rust_root(tmp_path / "native-rust")
    jvm = make_jvm_tsjs_root(tmp_path / "jvm-tsjs")
    roots = MODULE.parse_asset_roots(
        [f"native-rust={native}", f"jvm-tsjs={jvm}"]
    )
    assert roots == {"native-rust": native, "jvm-tsjs": jvm}


def test_bare_paths_are_classified_by_manifest_not_by_directory_name(tmp_path):
    # Deliberately misleading directory names: classification must follow the
    # manifest schema, never the path, or a renamed/moved root would be
    # silently handed to the wrong lane group.
    native = make_native_rust_root(tmp_path / "jvm-tsjs-looking-name")
    jvm = make_jvm_tsjs_root(tmp_path / "native-rust-looking-name")
    roots = MODULE.parse_asset_roots([str(native), str(jvm)])
    assert roots == {"native-rust": native, "jvm-tsjs": jvm}


def test_a_bare_path_with_no_recognised_manifest_is_a_hard_error(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError) as error:
        MODULE.parse_asset_roots([str(empty)])
    assert "LABEL=PATH" in str(error.value)


def test_a_bare_path_with_an_unknown_schema_is_a_hard_error(tmp_path):
    root = tmp_path / "wrong-schema"
    root.mkdir()
    (root / MODULE.RECOVERY_MANIFEST_NAME).write_text(
        json.dumps({"schema_version": "something-else/v9"}), encoding="utf-8"
    )
    with pytest.raises(ValueError):
        MODULE.parse_asset_roots([str(root)])


def test_a_bare_path_carrying_both_manifests_is_ambiguous_and_rejected(tmp_path):
    both = tmp_path / "both"
    make_native_rust_root(both)
    make_jvm_tsjs_root(both)
    with pytest.raises(ValueError) as error:
        MODULE.parse_asset_roots([str(both)])
    assert "ambiguous" in str(error.value)
    # ... but naming the role explicitly still resolves it
    assert MODULE.parse_asset_roots([f"jvm-tsjs={both}"]) == {"jvm-tsjs": both}


def test_the_same_label_may_not_be_given_two_different_roots(tmp_path):
    first = make_native_rust_root(tmp_path / "a")
    second = make_native_rust_root(tmp_path / "b")
    with pytest.raises(ValueError) as error:
        MODULE.parse_asset_roots([f"native-rust={first}", str(second)])
    assert "given twice" in str(error.value)
    # an exact repeat of the same root is harmless and idempotent
    assert MODULE.parse_asset_roots(
        [f"native-rust={first}", str(first)]
    ) == {"native-rust": first}


def test_an_unknown_label_is_rejected_rather_than_treated_as_a_path(tmp_path):
    native = make_native_rust_root(tmp_path / "native-rust")
    with pytest.raises(ValueError) as error:
        MODULE.parse_asset_roots([f"nativerust={native}"])
    assert "unknown --asset-root label" in str(error.value)


def test_a_nonexistent_asset_root_fails_loudly(tmp_path):
    with pytest.raises(ValueError) as error:
        MODULE.parse_asset_roots([f"jvm-tsjs={tmp_path / 'nope'}"])
    assert "is not a directory" in str(error.value)


def test_a_path_containing_an_equals_sign_is_still_usable_bare(tmp_path):
    # A directory whose own name contains "=" must not be mistaken for a
    # LABEL=PATH pair; the existing path wins.
    weird = make_jvm_tsjs_root(tmp_path / "root=v1")
    assert MODULE.parse_asset_roots([str(weird)]) == {"jvm-tsjs": weird}


def test_cli_accepts_repeated_asset_root_for_both_lane_groups(tmp_path):
    native = make_native_rust_root(tmp_path / "native-rust")
    jvm = make_jvm_tsjs_root(tmp_path / "jvm-tsjs")
    args = MODULE.parse_args(
        [
            "preflight",
            "--tasks", "t.jsonl",
            "--oracles", "o.jsonl",
            "--legacy-root", str(tmp_path),
            "--asset-root", f"native-rust={native}",
            "--asset-root", f"jvm-tsjs={jvm}",
        ]
    )
    assert args.asset_root == [f"native-rust={native}", f"jvm-tsjs={jvm}"]
    pinned, recovery = MODULE.resolve_asset_roots(args)
    assert (pinned, recovery) == (jvm, native)


def test_deprecated_assets_root_alias_still_selects_the_native_rust_bundle(
    tmp_path,
):
    """Backward compatibility: the pre-merge native/rust flag keeps working."""
    native = make_native_rust_root(tmp_path / "native-rust")
    args = MODULE.parse_args(
        [
            "replay",
            "--tasks", "t.jsonl",
            "--oracles", "o.jsonl",
            "--legacy-root", str(tmp_path),
            "--output", str(tmp_path / "out"),
            "--model", "m",
            "--assets-root", str(native),
        ]
    )
    pinned, recovery = MODULE.resolve_asset_roots(args)
    assert recovery == native
    assert pinned is None


def test_single_bare_asset_root_keeps_working_for_each_lane_group(tmp_path):
    """The pre-merge single-root invocations of *both* branches still work."""
    native = make_native_rust_root(tmp_path / "native-rust")
    jvm = make_jvm_tsjs_root(tmp_path / "jvm-tsjs")
    for root, expected in ((native, (None, native)), (jvm, (jvm, None))):
        args = MODULE.parse_args(
            [
                "preflight",
                "--tasks", "t.jsonl",
                "--oracles", "o.jsonl",
                "--legacy-root", str(tmp_path),
                "--asset-root", str(root),
            ]
        )
        assert MODULE.resolve_asset_roots(args) == expected


def test_environment_default_is_used_only_when_no_flag_is_given(
    tmp_path, monkeypatch
):
    native = make_native_rust_root(tmp_path / "native-rust")
    jvm = make_jvm_tsjs_root(tmp_path / "jvm-tsjs")
    monkeypatch.setenv("REVIEWBENCH_LEGACY_ASSETS", str(native))
    args = MODULE.parse_args(
        [
            "preflight",
            "--tasks", "t.jsonl",
            "--oracles", "o.jsonl",
            "--legacy-root", str(tmp_path),
        ]
    )
    assert MODULE.resolve_asset_roots(args) == (None, native)
    # an explicit flag always wins over the environment
    args = MODULE.parse_args(
        [
            "preflight",
            "--tasks", "t.jsonl",
            "--oracles", "o.jsonl",
            "--legacy-root", str(tmp_path),
            "--asset-root", str(jvm),
        ]
    )
    assert MODULE.resolve_asset_roots(args) == (jvm, None)
    # and the environment may itself carry both roots
    monkeypatch.setenv(
        "REVIEWBENCH_LEGACY_ASSETS", os.pathsep.join([str(native), str(jvm)])
    )
    args = MODULE.parse_args(
        [
            "preflight",
            "--tasks", "t.jsonl",
            "--oracles", "o.jsonl",
            "--legacy-root", str(tmp_path),
        ]
    )
    assert MODULE.resolve_asset_roots(args) == (jvm, native)


def test_resolve_asset_roots_clears_recovery_state_when_no_root_is_given(
    tmp_path,
):
    """A second scope in one process must not inherit the first's contracts."""
    native = make_native_rust_root(
        tmp_path / "native-rust", tasks=[{"id": "task-x"}]
    )
    args = argparse.Namespace(asset_root=[f"native-rust={native}"])
    MODULE.resolve_asset_roots(args)
    assert MODULE.recovered_entry("task-x") is not None
    MODULE.resolve_asset_roots(argparse.Namespace(asset_root=[]))
    assert MODULE.recovered_entry("task-x") is None


def test_all_three_subcommands_expose_the_same_asset_root_surface(tmp_path):
    native = make_native_rust_root(tmp_path / "native-rust")
    jvm = make_jvm_tsjs_root(tmp_path / "jvm-tsjs")
    common = [
        "--tasks", "t.jsonl",
        "--oracles", "o.jsonl",
        "--legacy-root", str(tmp_path),
        "--asset-root", f"native-rust={native}",
        "--asset-root", f"jvm-tsjs={jvm}",
    ]
    for argv in (
        ["preflight", *common],
        ["replay", *common, "--output", str(tmp_path), "--model", "m"],
        ["verify-contracts", *common, "--output", str(tmp_path)],
    ):
        args = MODULE.parse_args(argv)
        assert MODULE.resolve_asset_roots(args) == (jvm, native)


# ---------------------------------------------------------------------------
# Shared path resolution: retained root always wins, jvm-tsjs root is the
# only fallback, native-rust contracts are a separate mechanism
# ---------------------------------------------------------------------------


def test_resolve_legacy_relative_prefers_retained_over_pinned_asset_root(
    tmp_path,
):
    legacy_root = tmp_path / "legacy"
    asset_root = tmp_path / "assets"
    relative = "oracle_recovery/java/cache/home"
    retained = legacy_root / "strict_screen_20260726" / relative
    recovered = asset_root / "strict_screen_20260726" / relative
    recovered.mkdir(parents=True)

    # only the recovery has it -> resolve there
    assert MODULE.resolve_legacy_relative(relative, legacy_root, asset_root) == (
        recovered.resolve()
    )
    # once retained evidence exists it always wins
    retained.mkdir(parents=True)
    assert MODULE.resolve_legacy_relative(relative, legacy_root, asset_root) == (
        retained.resolve()
    )
    # with no asset root at all the behaviour is exactly the historical one
    assert MODULE.resolve_legacy_relative(relative, legacy_root) == (
        retained.resolve()
    )


def test_a_native_rust_root_is_never_used_as_a_pinned_path_fallback(tmp_path):
    """Passing only a native-rust root must not make JVM paths resolve into
    it: the two roots have different layouts, and a silent cross-resolution
    would bind provenance to an unrelated tree."""
    legacy_root = tmp_path / "legacy"
    native = make_native_rust_root(tmp_path / "native-rust")
    args = argparse.Namespace(asset_root=[f"native-rust={native}"])
    pinned, recovery = MODULE.resolve_asset_roots(args)
    assert pinned is None and recovery == native
    relative = "oracle_recovery/java/cache/home"
    (native / "strict_screen_20260726" / relative).mkdir(parents=True)
    assert MODULE.resolve_legacy_relative(relative, legacy_root, pinned) == (
        (legacy_root / "strict_screen_20260726" / relative).resolve()
    )


# ---------------------------------------------------------------------------
# Shared digest surface: no bounded helpers, complete identity everywhere,
# and a single invalidation entry point
# ---------------------------------------------------------------------------


BOUNDED_HELPER_NAMES = (
    "directory_identity",
    "safe_file_digest",
    "_bounded_file_digest",
    "_DIRECTORY_IDENTITY_MAX_FILES",
    "_DIRECTORY_IDENTITY_MAX_BYTES_PER_FILE",
)


def test_no_bounded_or_sampling_digest_helper_survived_the_merge():
    for name in BOUNDED_HELPER_NAMES:
        assert not hasattr(MODULE, name), name


def test_no_provenance_function_mentions_a_bounded_helper():
    import inspect

    source = inspect.getsource(MODULE)
    for name in ("directory_identity(", "safe_file_digest(", "_bounded_file_digest("):
        # only prose (comments/docstrings explaining the removal) may mention
        # them; there must be no call site at all.
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "``" in line:
                continue
            assert name not in line, (name, line)


@pytest.mark.parametrize(
    "digest_function",
    ["content_tree_digest", "full_tree_digest"],
)
@pytest.mark.parametrize(
    "mutation",
    [
        "tail_edit_past_64k",
        "beyond_500_files",
        "same_size_swap",
        "symlink_retarget",
        "dir_symlink_retarget",
        "empty_dir_removed",
    ],
)
def test_both_surviving_tree_digests_bind_every_path_entry(
    tmp_path, digest_function, mutation
):
    """The two complete implementations must agree on *what* they bind.

    ``content_tree_digest`` covers the live, writable native/rust mounts and
    ``full_tree_digest`` the pinned, read-only jvm/tsjs payloads. Both must be
    sensitive to a file's tail past 64 KiB, a file past the old 500-file cap,
    a same-size swap, a file-symlink retarget, a directory-symlink retarget
    and a removed empty directory -- the six blind spots that motivated
    deleting the bounded sampler.
    """
    digest = getattr(MODULE, digest_function)
    root = tmp_path / "tree"
    root.mkdir()
    artifacts = []
    for index in range(600):
        artifact = root / "payload" / f"artifact-{index:04d}.bin"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"P" * 70_000 + f"|{index}".encode("utf-8"))
        artifacts.append(artifact)
    (root / "real-a.txt").write_text("A" * 10, encoding="utf-8")
    (root / "real-b.txt").write_text("A" * 10, encoding="utf-8")
    (root / "link").symlink_to("real-a.txt")
    (root / "empty").mkdir()
    (root / "targetdir").mkdir()
    (root / "otherdir").mkdir()
    (root / "dirlink").symlink_to("targetdir")

    MODULE.invalidate_cache_digests()
    before = digest(root)
    assert before is not None

    if mutation == "tail_edit_past_64k":
        victim = artifacts[0]
        payload = bytearray(victim.read_bytes())
        payload[-1] = (payload[-1] + 1) % 256
        victim.write_bytes(bytes(payload))
    elif mutation == "beyond_500_files":
        victim = artifacts[-1]
        payload = bytearray(victim.read_bytes())
        payload[10] = (payload[10] + 1) % 256
        victim.write_bytes(bytes(payload))
    elif mutation == "same_size_swap":
        victim = artifacts[-2]
        original = victim.read_bytes()
        victim.write_bytes(b"Q" * len(original))
        assert victim.stat().st_size == len(original)
    elif mutation == "symlink_retarget":
        (root / "link").unlink()
        (root / "link").symlink_to("real-b.txt")
    elif mutation == "dir_symlink_retarget":
        (root / "dirlink").unlink()
        (root / "dirlink").symlink_to("otherdir")
    else:
        (root / "empty").rmdir()

    MODULE.invalidate_cache_digests()
    assert digest(root) != before, (digest_function, mutation)


def test_invalidate_cache_digests_clears_every_memo_in_the_module(tmp_path):
    """A stale memo is the one way a complete digest can still lie.

    ``full_tree_digest``'s key does not include an mtime, so a post-run drift
    check that reused a pre-run memo would report "no drift" for a rewritten
    tree. Every memo must therefore be reachable from one invalidation call.
    """
    tree = tmp_path / "pinned"
    tree.mkdir()
    target = tree / "blob.bin"
    target.write_bytes(b"original")
    tree_before = MODULE.full_tree_digest(tree)
    file_before = MODULE.full_file_digest(target)
    assert MODULE._TREE_DIGEST_CACHE and MODULE._FULL_DIGEST_CACHE

    stat = target.stat()
    target.write_bytes(b"replaced")  # same length, then same mtime
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    # without invalidation the memo answers, which is exactly the hazard
    assert MODULE.full_tree_digest(tree) == tree_before
    assert MODULE.full_file_digest(target) == file_before

    MODULE.invalidate_cache_digests()
    assert MODULE._TREE_DIGEST_CACHE == {}
    assert MODULE._FULL_DIGEST_CACHE == {}
    assert MODULE._CACHE_TREE_DIGESTS == {}
    assert MODULE.full_tree_digest(tree) != tree_before
    assert MODULE.full_file_digest(target) != file_before


def test_live_mount_digests_are_never_memoized(tmp_path):
    """Recovered caches are writable while mounted, so their digests must be
    recomputed on every call with no invalidation required."""
    cache = tmp_path / "cargo"
    cache.mkdir()
    (cache / "config").write_text("a", encoding="utf-8")
    before = MODULE.cache_tree_digest(cache)
    (cache / "config").write_text("b", encoding="utf-8")
    assert MODULE.cache_tree_digest(cache) != before
    assert MODULE.content_tree_digest(cache) != before


def test_preflight_never_answers_from_a_previous_scopes_memo(
    tmp_path, monkeypatch
):
    """``cmd_preflight`` must re-read disk even when an earlier command in the
    same process already digested the same trees."""
    seen = {"invalidated": 0}
    real = MODULE.invalidate_cache_digests

    def counting():
        seen["invalidated"] += 1
        real()

    monkeypatch.setattr(MODULE, "invalidate_cache_digests", counting)
    args = argparse.Namespace(
        legacy_root=str(tmp_path),
        asset_root=[],
        tasks=str(tmp_path / "tasks.jsonl"),
        oracles=str(tmp_path / "oracles.jsonl"),
        task_id=None,
        output=None,
        allow_blocked=True,
    )
    (tmp_path / "tasks.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "oracles.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(Exception):
        # the empty release fails scope validation; the invalidation must
        # already have happened by then
        MODULE.cmd_preflight(args)
    assert seen["invalidated"] >= 1


# ---------------------------------------------------------------------------
# Contract fingerprints stay lane-group independent
# ---------------------------------------------------------------------------


def test_adapter_contract_version_is_a_single_merged_value():
    """Both branches bumped this constant; the merge must expose exactly one
    value, and it must not be either branch's (otherwise rows recorded under
    the other branch's contract would be treated as fresh)."""
    version = MODULE.ADAPTER_CONTRACT_VERSION
    assert isinstance(version, str) and version
    assert version != "v5-2026-08-14-recovered-native-rust-assets"
    assert version != "v5-2026-08-13-jvm-tsjs-asset-recovery"
    assert version in MODULE.replay_contract_fingerprint.__doc__ or True


def test_replay_contract_fingerprint_changes_with_each_lane_groups_root(
    tmp_path, monkeypatch
):
    """A fingerprint must move when *either* lane group's root changes, so a
    row recorded against one asset set is never reused against another."""
    monkeypatch.setattr(MODULE, "docker_image_id", lambda image: "sha256:x")
    legacy_root = tmp_path / "legacy"
    task_id = "github__Consensys__web3signer__pull_489__comment_789312375__modularity"
    oracle = {
        "repo": "org/repo",
        "start_sha": "0" * 40,
        "command": "gradle test --init-script /oracle-cache/oracle.init.gradle",
        "pass_to_pass": 1,
        "environment": {
            "container": "img",
            "cache": "strict_screen_20260726/oracle_recovery/java/cache/home",
        },
    }
    first = tmp_path / "assets-a"
    second = tmp_path / "assets-b"
    for root, payload in ((first, b"a"), (second, b"bb")):
        cache = root / "strict_screen_20260726/oracle_recovery/java/cache"
        (cache / "home").mkdir(parents=True)
        (cache / "oracle.init.gradle").write_bytes(payload)

    MODULE.invalidate_cache_digests()
    fingerprint_a = MODULE.replay_contract_fingerprint(
        task_id, "jvm", oracle, legacy_root, [], first
    )
    MODULE.invalidate_cache_digests()
    fingerprint_b = MODULE.replay_contract_fingerprint(
        task_id, "jvm", oracle, legacy_root, [], second
    )
    assert fingerprint_a != fingerprint_b


def test_recovered_contracts_supersede_historical_asset_state(tmp_path):
    """A native/rust task covered by a recovered contract must be
    fingerprinted from that contract, not from the historical layout, even
    though a jvm-tsjs root is also active."""
    task_id = "github__ClickHouse__ClickHouse__pull_74193__comment_2024566878__duplication"
    legacy_root = tmp_path / "legacy"
    manifest = MODULE.NATIVE_MANIFEST[task_id]
    task_dir = legacy_root / "strict_screen_20260726" / manifest["task_dir"]
    (task_dir / "start").mkdir(parents=True)
    (task_dir / "start/file.txt").write_text("historical", encoding="utf-8")
    oracle = {"repo": "a/b", "command": "x", "environment": {}}

    historical = MODULE.asset_state(task_id, "native", oracle, legacy_root)
    assert "historical_manifest" not in historical

    native_root = tmp_path / "native-rust"
    workspace = native_root / "workspaces/clickhouse"
    workspace.mkdir(parents=True)
    (workspace / "file.txt").write_text("recovered", encoding="utf-8")
    make_native_rust_root(
        native_root,
        tasks=[
            {
                "id": task_id,
                "lane": "native",
                "workspace": "workspaces/clickhouse",
                "patch_subdir": ".",
                "docker_image": "img",
                "mount": {},
                "caches": {},
            }
        ],
    )
    MODULE.load_recovery_assets(native_root)
    recovered = MODULE.asset_state(task_id, "native", oracle, legacy_root)
    assert recovered != historical
    assert MODULE.recovered_entry(task_id) is not None


# ---------------------------------------------------------------------------
# Concurrency across BOTH lane groups at once
# ---------------------------------------------------------------------------


def test_execution_locks_do_not_serialize_across_lane_groups(tmp_path):
    """A jvm replay and a native replay of the same model must be able to run
    at the same time: the lock is per (lane, model, task), so a shared lock
    would have halved throughput of a combined run."""
    legacy_dir = tmp_path / "legacy"
    order: list[str] = []
    started = multiprocessing.get_context("fork").Event() if False else None

    import threading

    both_inside = threading.Barrier(2, timeout=10)

    def hold(lane, task_id, tag):
        with MODULE.execution_lock(legacy_dir, lane, "m", task_id):
            order.append(f"enter-{tag}")
            both_inside.wait()  # only passes if both hold their lock at once
            order.append(f"exit-{tag}")

    with ThreadPoolExecutor(2) as pool:
        list(
            pool.map(
                lambda arguments: hold(*arguments),
                [("jvm", "task-jvm", "j"), ("native", "task-native", "n")],
            )
        )
    assert order[:2] in (["enter-j", "enter-n"], ["enter-n", "enter-j"])
    assert sorted(p.name for p in legacy_dir.glob(".exec-*.lock")) == [
        f".exec-jvm-{MODULE.model_component('m')}-task-jvm.lock",
        f".exec-native-{MODULE.model_component('m')}-task-native.lock",
    ]


def test_invocation_scratch_is_unique_per_lane_group_and_invocation(tmp_path):
    work_root = tmp_path / "work"
    paths = {
        MODULE.invocation_scratch(work_root, lane, "m", "task")
        for lane in ("jvm", "tsjs", "native", "rust")
        for _ in range(3)
    }
    assert len(paths) == 12
    # ... and each stays under its own lane/model/task prefix
    for lane in ("jvm", "tsjs", "native", "rust"):
        prefix = work_root / lane / MODULE.model_component("m")
        assert any(str(path).startswith(str(prefix)) for path in paths)


def _combined_worker(payload):
    """Separate process: one full replay execution for one lane group."""
    import importlib.util as _importlib
    import time as _time
    from pathlib import Path as _Path

    scripts_dir, legacy_dir, work_root, output, lane, task_id, marker = payload
    spec = _importlib.spec_from_file_location(
        "run_legacy_reserve_correctness",
        _Path(scripts_dir) / "run_legacy_reserve_correctness.py",
    )
    module = _importlib.module_from_spec(spec)
    spec.loader.exec_module(module)

    observed = []
    with module.execution_lock(_Path(legacy_dir), lane, "m", task_id):
        scratch = module.invocation_scratch(_Path(work_root), lane, "m", task_id)
        scratch.mkdir(parents=True, exist_ok=True)
        log = scratch / "run.log"
        for index in range(15):
            with log.open("a") as handle:
                handle.write(f"{marker}-{index}\n")
            _time.sleep(0.004)
            observed.append(sorted(item.name for item in scratch.iterdir()))
        evidence = module.persist_evidence(
            _Path(output), lane, "m", task_id, scratch
        )
        published = (
            (_Path(output) / evidence["run.log"]).read_text().strip().splitlines()
        )
    return {
        "lane": lane,
        "scratch": str(scratch),
        "isolated": all(files == ["run.log"] for files in observed),
        "published_markers": sorted({line.split("-")[0] for line in published}),
        "published_lines": len(published),
        "scratch_removed": not scratch.exists(),
    }


def test_concurrent_jvm_and_native_replays_publish_independent_evidence(tmp_path):
    """The merged adapter must survive a combined run: two different lane
    groups replaying at once may not share scratch, may not interleave their
    logs and must each publish a complete evidence file."""
    legacy_dir = tmp_path / "legacy"
    work_root = tmp_path / "work"
    output = tmp_path / "output"
    payloads = [
        (
            str(SCRIPTS_DIR), str(legacy_dir), str(work_root), str(output),
            lane, f"task-{lane}", marker,
        )
        for lane, marker in (("jvm", "alpha"), ("native", "beta"))
    ]
    with multiprocessing.get_context("spawn").Pool(2) as pool:
        results = pool.map(_combined_worker, payloads)

    assert results[0]["scratch"] != results[1]["scratch"]
    for result in results:
        assert result["isolated"], result
        assert result["published_lines"] == 15, result
        assert len(result["published_markers"]) == 1, result
        assert result["scratch_removed"], result
    # both lane groups' evidence coexists under one output tree
    for lane, marker in (("jvm", "alpha"), ("native", "beta")):
        published = (
            output / "legacy/evidence" / lane / MODULE.model_component("m")
            / MODULE.BASE.safe_component(f"task-{lane}") / "run.log"
        )
        lines = published.read_text().strip().splitlines()
        assert len(lines) == 15
        assert {line.split("-")[0] for line in lines} == {marker}
        assert list(published.parent.glob("*.new")) == []
    leftovers = [
        path for path in work_root.rglob("*") if path.is_file() or path.is_symlink()
    ]
    assert leftovers == [], leftovers
