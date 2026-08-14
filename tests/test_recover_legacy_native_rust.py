import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).parents[1] / "scripts"
SPEC = importlib.util.spec_from_file_location(
    "recover_legacy_native_rust", SCRIPTS_DIR / "recover_legacy_native_rust.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

ADAPTER_SPEC = importlib.util.spec_from_file_location(
    "run_legacy_reserve_correctness",
    SCRIPTS_DIR / "run_legacy_reserve_correctness.py",
)
assert ADAPTER_SPEC and ADAPTER_SPEC.loader
ADAPTER = importlib.util.module_from_spec(ADAPTER_SPEC)
ADAPTER_SPEC.loader.exec_module(ADAPTER)


def git(*args, cwd=None):
    result = subprocess.run(
        ["git", *args], cwd=cwd, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True,
    )
    assert result.returncode == 0, result.stdout
    return result.stdout


def make_start_tree(root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    git("init", "-q", cwd=root)
    git("config", "user.email", "a@example.com", cwd=root)
    git("config", "user.name", "a", cwd=root)
    (root / "file.txt").write_text("x\n", encoding="utf-8")
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "init", cwd=root)
    return git("rev-parse", "HEAD", cwd=root).strip()


# ---------------------------------------------------------------------------
# Scope: the recipe table must exactly cover the 11 in-scope task IDs
# ---------------------------------------------------------------------------


def test_scope_matches_the_adapters_native_rust_published_legacy_scope():
    native_rust = {
        task_id
        for task_id, lane in ADAPTER.EXPECTED_LANE.items()
        if lane in ("native", "rust")
    }
    assert set(MODULE.ALL_TASK_IDS) == native_rust
    assert len(MODULE.ALL_TASK_IDS) == 11
    # every recovered-only task ID the adapter knows about must appear here
    assert set(ADAPTER.RECOVERED_ONLY_TASK_IDS) <= set(MODULE.ALL_TASK_IDS)
    # ... and nothing may be both recovered and declared unrecoverable
    assert not set(MODULE.RECIPES) & set(MODULE.UNRECOVERED)


def test_every_recipe_declares_an_executable_contract():
    for task_id, recipe in MODULE.RECIPES.items():
        assert recipe["lane"] in ("native", "rust"), task_id
        assert recipe["command"].strip(), task_id
        assert recipe["workdir"].startswith("/"), task_id
        assert recipe["patch_subdir"] == "start", task_id
        assert recipe["mount"], task_id
        assert len(recipe["start_sha"]) == 40, task_id
        # rust must never use a login shell: it drops CARGO_HOME/bin from PATH
        if recipe["lane"] == "rust":
            assert recipe["shell"] == ["/bin/sh", "-c"], task_id


def test_unrecovered_tasks_carry_concrete_blockers():
    for task_id, entry in MODULE.UNRECOVERED.items():
        assert entry["blockers"], task_id
        assert all(len(text) > 20 for text in entry["blockers"]), task_id


# ---------------------------------------------------------------------------
# Fail-closed asset verification
# ---------------------------------------------------------------------------


def build_assets(tmp_path, monkeypatch, *, image_id="sha256:abc"):
    task_id = "github__SciCompMod__memilio__pull_690__comment_1394151706__complexity"
    recipe = dict(MODULE.RECIPES[task_id])
    assets = tmp_path / "assets"
    start = assets / recipe["workspace"] / "start"
    head = make_start_tree(start)
    recipe["start_sha"] = head
    golden = assets / recipe["golden_patch"]
    golden.parent.mkdir(parents=True, exist_ok=True)
    golden.write_text(
        "diff --git a/file.txt b/file.txt\n"
        "--- a/file.txt\n+++ b/file.txt\n@@ -1 +1 @@\n-x\n+y\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(MODULE, "docker_image_id", lambda image: image_id)
    return task_id, recipe, assets, head


def proof_runs(*variants, fingerprint=None):
    return [
        {
            "variant": variant, "run": 1, "exit_code": 0, "stage": "execute",
            "recipe_fingerprint": fingerprint,
        }
        for variant in variants
    ]


def current_fingerprint(task_id, recipe, assets):
    return MODULE.recipe_fingerprint(task_id, recipe, assets)


def write_proof(assets, recipe, task_id, runs, *, workspace_state=None):
    proof = assets / "proofs" / f"{recipe['case']}.json"
    proof.parent.mkdir(parents=True, exist_ok=True)
    if workspace_state is None:
        workspace_state = MODULE.workspace_state(
            assets / recipe["workspace"] / recipe["patch_subdir"]
        )
    proof.write_text(
        json.dumps(
            {
                "id": task_id,
                "case": recipe["case"],
                "runs": runs,
                "canonical_workspace_state": workspace_state,
            }
        ),
        encoding="utf-8",
    )


def test_contract_blockers_pass_for_intact_assets(tmp_path, monkeypatch):
    task_id, recipe, assets, _ = build_assets(tmp_path, monkeypatch)
    assert MODULE.contract_blockers(task_id, recipe, assets) == []


def test_contract_blockers_flag_wrong_start_sha(tmp_path, monkeypatch):
    task_id, recipe, assets, _ = build_assets(tmp_path, monkeypatch)
    recipe["start_sha"] = "0" * 40
    blockers = MODULE.contract_blockers(task_id, recipe, assets)
    assert any("not the task start_sha" in b for b in blockers)


def test_contract_blockers_flag_missing_image_and_workspace(tmp_path, monkeypatch):
    task_id, recipe, assets, _ = build_assets(tmp_path, monkeypatch, image_id=None)
    monkeypatch.setattr(MODULE, "docker_image_id", lambda image: None)
    blockers = MODULE.contract_blockers(task_id, recipe, assets)
    assert any("missing Docker image" in b for b in blockers)
    recipe = dict(recipe)
    recipe["workspace"] = "workspaces/gone"
    blockers = MODULE.contract_blockers(task_id, recipe, assets)
    assert any("missing exact-start subtree" in b for b in blockers)


def test_contract_blockers_flag_missing_cargo_home(tmp_path, monkeypatch):
    task_id = "github__FuelLabs__fuel-tx__pull_197__comment_1003742605__complexity"
    recipe = dict(MODULE.RECIPES[task_id])
    assets = tmp_path / "assets"
    start = assets / recipe["workspace"] / "start"
    recipe["start_sha"] = make_start_tree(start)
    monkeypatch.setattr(MODULE, "docker_image_id", lambda image: "sha256:abc")
    blockers = MODULE.contract_blockers(task_id, recipe, assets)
    assert any("missing reconstructed CARGO_HOME" in b for b in blockers)


# ---------------------------------------------------------------------------
# Manifest generation
# ---------------------------------------------------------------------------


def test_manifest_marks_unproven_contracts_blocked_and_writes_checksums(
    tmp_path, monkeypatch, capsys
):
    task_id, recipe, assets, head = build_assets(tmp_path, monkeypatch)
    monkeypatch.setitem(MODULE.RECIPES, task_id, recipe)
    args = MODULE.parse_args(
        ["manifest", "--assets-root", str(assets), "--legacy-root", str(tmp_path)]
    )
    assert MODULE.cmd_manifest(args) == 0
    manifest = json.loads((assets / MODULE.MANIFEST_NAME).read_text())
    entries = {row["id"]: row for row in manifest["tasks"]}
    # no proof run recorded yet -> blocked, never silently "ready"
    assert entries[task_id]["status"] == "blocked"
    assert any(
        "no successful start-tree proof run" in text
        for text in entries[task_id]["blockers"]
    )
    # the un-recoverable task stays present with its recorded blockers
    grpc = entries["github__grpc__grpc__pull_32970__comment_1195793486__duplication"]
    assert grpc["status"] == "blocked" and grpc["recovered"] is False
    assert (assets / MODULE.CHECKSUM_NAME).is_file()

    fingerprint = current_fingerprint(task_id, recipe, assets)
    # a start-only proof is NOT enough for a contract that declares a golden
    write_proof(assets, recipe, task_id, proof_runs("start", fingerprint=fingerprint))
    assert MODULE.cmd_manifest(args) == 0
    manifest = json.loads((assets / MODULE.MANIFEST_NAME).read_text())
    entries = {row["id"]: row for row in manifest["tasks"]}
    assert entries[task_id]["status"] == "blocked"
    assert any(
        "no successful golden proof run" in text
        for text in entries[task_id]["blockers"]
    )

    # a recorded *failing* golden run is still not enough
    write_proof(
        assets, recipe, task_id,
        proof_runs("start", fingerprint=fingerprint) + [
            {"variant": "golden", "run": 1, "exit_code": 1, "stage": "apply",
             "error": "golden patch failed to apply",
             "recipe_fingerprint": fingerprint}
        ],
    )
    assert MODULE.cmd_manifest(args) == 0
    entries = {
        row["id"]: row
        for row in json.loads((assets / MODULE.MANIFEST_NAME).read_text())["tasks"]
    }
    assert entries[task_id]["status"] == "blocked"

    # start + golden -> ready
    write_proof(
        assets, recipe, task_id,
        proof_runs("start", "golden", fingerprint=fingerprint),
    )
    assert MODULE.cmd_manifest(args) == 0
    manifest = json.loads((assets / MODULE.MANIFEST_NAME).read_text())
    entries = {row["id"]: row for row in manifest["tasks"]}
    assert entries[task_id]["status"] == "ready"
    assert entries[task_id]["workspace_state"]["head"] == head
    assert entries[task_id]["replay_argv_example"][:3] == ["docker", "run", "--rm"]


def test_manifest_is_consumable_by_the_replay_adapter(tmp_path, monkeypatch):
    """The bundle this script writes must be exactly what the adapter reads."""
    task_id, recipe, assets, head = build_assets(tmp_path, monkeypatch)
    monkeypatch.setitem(MODULE.RECIPES, task_id, recipe)
    write_proof(
        assets, recipe, task_id,
        proof_runs(
            "start", "golden",
            fingerprint=current_fingerprint(task_id, recipe, assets),
        ),
    )
    args = MODULE.parse_args(
        ["manifest", "--assets-root", str(assets), "--legacy-root", str(tmp_path)]
    )
    MODULE.cmd_manifest(args)

    monkeypatch.setattr(ADAPTER, "docker_image_id", lambda image: "sha256:abc")
    ADAPTER.load_recovery_assets(assets)
    try:
        assert ADAPTER.task_blockers(task_id, "native", {}, tmp_path, head) == []
        argv = ADAPTER.recovered_docker_argv(
            ADAPTER.recovered_entry(task_id), assets, assets / recipe["workspace"]
        )
        assert argv[-1] == recipe["command"]
        assert "-w" in argv and recipe["workdir"] in argv
    finally:
        ADAPTER.load_recovery_assets(None)


def test_docker_argv_matches_the_recipe_mounts(tmp_path, monkeypatch):
    task_id = "github__otter-sec__anchor__pull_2488__comment_1193854703__duplication"
    recipe = MODULE.RECIPES[task_id]
    assets = tmp_path / "assets"
    workspace = assets / recipe["workspace"]
    argv = MODULE.docker_argv(task_id, recipe, assets, workspace)
    joined = " ".join(argv)
    assert "--network=host" in argv
    assert f"{workspace / 'start'}:/work/anchor" in joined
    assert f"{assets / 'caches/cargo-anchor'}:/cargo-cache" in joined
    assert "CARGO_HOME=/cargo-cache" in joined
    assert argv[-3:] == ["/bin/sh", "-c", recipe["command"]]


# ---------------------------------------------------------------------------
# Golden proof is mandatory; smoke must record golden failures explicitly
# ---------------------------------------------------------------------------


def test_proof_blockers_require_start_and_golden():
    recipe = {"golden_patch": "harness/x/golden.patch"}
    assert MODULE.proof_blockers(recipe, []) == [
        "no successful start-tree proof run",
        "contract declares a source-only golden patch but has no successful "
        "golden proof run",
    ]
    start_only = [{"variant": "start", "exit_code": 0}]
    assert any(
        "no successful golden proof" in text
        for text in MODULE.proof_blockers(recipe, start_only)
    )
    assert MODULE.proof_status(recipe, start_only) == "failed"
    failing_golden = start_only + [
        {"variant": "golden", "exit_code": 1, "stage": "apply",
         "error": "golden patch failed to apply"}
    ]
    blockers = MODULE.proof_blockers(recipe, failing_golden)
    assert any("last failure: apply" in text for text in blockers)
    both = start_only + [{"variant": "golden", "exit_code": 0}]
    assert MODULE.proof_blockers(recipe, both) == []
    assert MODULE.proof_status(recipe, both) == "ready"
    # a contract with no golden patch only needs a start proof
    assert MODULE.proof_blockers({}, start_only) == []


def test_smoke_records_a_golden_apply_failure_and_stays_not_ready(
    tmp_path, monkeypatch
):
    task_id, recipe, assets, _ = build_assets(tmp_path, monkeypatch)
    # a golden patch that cannot apply to this tree
    (assets / recipe["golden_patch"]).write_text(
        "diff --git a/absent.txt b/absent.txt\n"
        "--- a/absent.txt\n+++ b/absent.txt\n@@ -1 +1 @@\n-a\n+b\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(MODULE.RECIPES, task_id, recipe)
    runs = []

    real_run = MODULE.subprocess.run

    def fake_run(argv, *args, **kwargs):
        # let git/patch invocations through; never start a container
        if argv and argv[0] == "docker":
            runs.append(argv)

            class Result:
                returncode = 0
            return Result()
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(MODULE.subprocess, "run", fake_run)
    args = MODULE.parse_args(
        [
            "smoke", "--assets-root", str(assets), "--case", recipe["case"],
            "--golden", "--reset",
        ]
    )
    assert MODULE.cmd_smoke(args) == 1
    proof = json.loads((assets / "proofs" / f"{recipe['case']}.json").read_text())
    golden = [row for row in proof["runs"] if row["variant"] == "golden"]
    assert golden and golden[0]["exit_code"] == 1
    assert golden[0]["stage"] == "apply"
    assert Path(golden[0]["log"]).is_file()
    assert proof["status"] != "ready"
    assert any("golden proof run" in text for text in proof["blockers"])


def test_smoke_uses_the_recorded_golden_command_when_declared(tmp_path, monkeypatch):
    task_id = (
        "github__Desbordante__desbordante-core__pull_154__comment_1371183985"
        "__modularity"
    )
    recipe = MODULE.RECIPES[task_id]
    assert recipe["golden_command"], "desbordante records a distinct golden recipe"
    assets = tmp_path / "assets"
    argv_start = MODULE.docker_argv(task_id, recipe, assets, assets / "ws")
    argv_golden = MODULE.docker_argv(
        task_id, recipe, assets, assets / "ws", command=recipe["golden_command"]
    )
    assert argv_start[-1] == recipe["command"]
    assert argv_golden[-1] == recipe["golden_command"]
    # candidate replays must always use the start-side command
    assert recipe["command"] != recipe["golden_command"]


# ---------------------------------------------------------------------------
# Mounted-input binding
# ---------------------------------------------------------------------------


def test_tree_digest_detects_add_remove_and_truncate(tmp_path):
    cache = tmp_path / "cache"
    (cache / "sub").mkdir(parents=True)
    (cache / "sub/a.bin").write_bytes(b"a" * 100)
    (cache / "sub/b.bin").write_bytes(b"b" * 50)
    base = MODULE.tree_digest(cache, use_cache=False)
    assert base["file_count"] == 2
    (cache / "sub/b.bin").write_bytes(b"b" * 49)
    assert MODULE.tree_digest(cache, use_cache=False) != base
    (cache / "sub/b.bin").write_bytes(b"b" * 50)
    assert MODULE.tree_digest(cache, use_cache=False) == base
    (cache / "sub/c.bin").write_bytes(b"c")
    assert MODULE.tree_digest(cache, use_cache=False) != base
    (cache / "sub/c.bin").unlink()
    assert MODULE.tree_digest(cache, use_cache=False) == base
    (cache / "sub/a.bin").unlink()
    assert MODULE.tree_digest(cache, use_cache=False) != base
    assert MODULE.tree_digest(tmp_path / "absent", use_cache=False) is None


def test_mounted_cache_digests_cover_cache_mounts_and_cargo_home(tmp_path):
    task_id = "github__XRPLF__rippled__pull_5143__comment_1786919023__duplication"
    recipe = MODULE.RECIPES[task_id]
    assets = tmp_path / "assets"
    conan = assets / "caches/conan"
    conan.mkdir(parents=True)
    (conan / "blob").write_text("x", encoding="utf-8")
    digests = MODULE.mounted_cache_digests(recipe, assets)
    assert set(digests) == {"caches/conan"}
    assert digests["caches/conan"]["file_count"] == 1

    fuel = "github__FuelLabs__fuel-tx__pull_197__comment_1003742605__complexity"
    cargo = assets / MODULE.RECIPES[fuel]["cargo_cache"]
    (cargo / "bin").mkdir(parents=True)
    (cargo / "bin/cargo").write_text("#!/bin/sh\n", encoding="utf-8")
    digests = MODULE.mounted_cache_digests(MODULE.RECIPES[fuel], assets)
    assert set(digests) == {MODULE.RECIPES[fuel]["cargo_cache"]}


def test_contract_blockers_detect_workspace_drift(tmp_path, monkeypatch):
    task_id, recipe, assets, _ = build_assets(tmp_path, monkeypatch)
    start = assets / recipe["workspace"] / "start"
    expected = MODULE.workspace_state(start)
    assert MODULE.contract_blockers(
        task_id, recipe, assets, expected_state=expected
    ) == []
    (start / "stray.txt").write_text("x\n", encoding="utf-8")
    blockers = MODULE.contract_blockers(
        task_id, recipe, assets, expected_state=expected
    )
    assert any("has drifted" in text and "untracked" in text for text in blockers)


def test_contract_blockers_detect_missing_harness_and_cache_inputs(
    tmp_path, monkeypatch
):
    task_id = "github__otter-sec__anchor__pull_2488__comment_1193854703__duplication"
    recipe = dict(MODULE.RECIPES[task_id])
    assets = tmp_path / "assets"
    start = assets / recipe["workspace"] / "start"
    recipe["start_sha"] = make_start_tree(start)
    monkeypatch.setattr(MODULE, "docker_image_id", lambda image: "sha256:abc")
    blockers = MODULE.contract_blockers(task_id, recipe, assets)
    assert any("missing harness copy source" in text for text in blockers)
    assert any("missing reconstructed CARGO_HOME" in text for text in blockers)
    assert any("missing recovered golden patch" in text for text in blockers)


# ---------------------------------------------------------------------------
# smoke must never mutate the canonical workspace
# ---------------------------------------------------------------------------


def _fake_docker(monkeypatch, side_effect=None, returncode=0):
    """Stub ``docker run`` while letting git/patch/cp calls through."""
    real_run = MODULE.subprocess.run
    calls = []

    def fake_run(argv, *args, **kwargs):
        if argv and argv[0] == "docker":
            calls.append(argv)
            if side_effect is not None:
                side_effect(argv)

            class Result:
                pass
            Result.returncode = returncode
            return Result()
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(MODULE.subprocess, "run", fake_run)
    return calls


def test_smoke_runs_on_disposable_copies_and_leaves_canonical_untouched(
    tmp_path, monkeypatch
):
    task_id, recipe, assets, _ = build_assets(tmp_path, monkeypatch)
    monkeypatch.setitem(MODULE.RECIPES, task_id, recipe)
    canonical = assets / recipe["workspace"]
    start = canonical / recipe["patch_subdir"]
    # golden patch must apply to this tree so the golden variant really runs
    (assets / recipe["golden_patch"]).write_text(
        "diff --git a/file.txt b/file.txt\n--- a/file.txt\n+++ b/file.txt\n"
        "@@ -1 +1 @@\n-x\n+golden\n",
        encoding="utf-8",
    )
    before = MODULE.workspace_state(start)

    mounted = []

    def build_output(argv):
        # emulate a container writing build output into whatever was mounted
        target = Path(argv[argv.index("-v") + 1].split(":")[0])
        mounted.append(target)
        (target / "build-artifact.o").write_text("obj\n", encoding="utf-8")

    _fake_docker(monkeypatch, side_effect=build_output)
    args = MODULE.parse_args(
        [
            "smoke", "--assets-root", str(assets), "--case", recipe["case"],
            "--golden", "--reset", "--scratch-root", str(tmp_path / "scratch"),
        ]
    )
    assert MODULE.cmd_smoke(args) == 0

    # 1) nothing was mounted from the canonical workspace
    assert mounted, "docker was never invoked"
    for target in mounted:
        assert canonical not in target.parents and target != canonical
    # 2) the canonical workspace is byte-for-byte unchanged
    assert MODULE.workspace_state(start) == before
    assert not (start / "build-artifact.o").exists()
    assert not (start / "file.txt").read_text().startswith("golden")
    # 3) the proof attests the canonical state and both variants ran on copies
    proof = json.loads((assets / "proofs" / f"{recipe['case']}.json").read_text())
    assert proof["status"] == "ready"
    assert proof["canonical_workspace_state"] == before
    assert {row["variant"] for row in proof["runs"]} == {"start", "golden"}
    assert all(row["workspace"] == "disposable-copy" for row in proof["runs"])
    # 4) disposable scratch is cleaned up
    assert not (tmp_path / "scratch").exists()


def test_smoke_records_canonical_mutation_as_a_blocker(tmp_path, monkeypatch):
    task_id, recipe, assets, _ = build_assets(tmp_path, monkeypatch)
    monkeypatch.setitem(MODULE.RECIPES, task_id, recipe)
    start = assets / recipe["workspace"] / recipe["patch_subdir"]

    def leak(argv):
        # a recipe that (wrongly) writes into the canonical workspace
        (start / "leaked.o").write_text("leak\n", encoding="utf-8")

    _fake_docker(monkeypatch, side_effect=leak)
    args = MODULE.parse_args(
        [
            "smoke", "--assets-root", str(assets), "--case", recipe["case"],
            "--reset", "--scratch-root", str(tmp_path / "scratch"),
        ]
    )
    assert MODULE.cmd_smoke(args) == 1
    proof = json.loads((assets / "proofs" / f"{recipe['case']}.json").read_text())
    assert proof["status"] == "blocked"
    assert any("canonical workspace was mutated" in b for b in proof["blockers"])


def test_manifest_rejects_proofs_whose_canonical_workspace_since_changed(
    tmp_path, monkeypatch
):
    task_id, recipe, assets, _ = build_assets(tmp_path, monkeypatch)
    monkeypatch.setitem(MODULE.RECIPES, task_id, recipe)
    fingerprint = current_fingerprint(task_id, recipe, assets)
    write_proof(
        assets, recipe, task_id, proof_runs("start", "golden", fingerprint=fingerprint)
    )
    args = MODULE.parse_args(
        ["manifest", "--assets-root", str(assets), "--legacy-root", str(tmp_path)]
    )
    MODULE.cmd_manifest(args)
    entries = {
        row["id"]: row
        for row in json.loads((assets / MODULE.MANIFEST_NAME).read_text())["tasks"]
    }
    assert entries[task_id]["status"] == "ready"

    # a stray artifact appearing in the canonical workspace after the proofs
    (assets / recipe["workspace"] / recipe["patch_subdir"] / "stray.o").write_text(
        "x\n", encoding="utf-8"
    )
    MODULE.cmd_manifest(args)
    entries = {
        row["id"]: row
        for row in json.loads((assets / MODULE.MANIFEST_NAME).read_text())["tasks"]
    }
    assert entries[task_id]["status"] == "blocked"
    assert any(
        "canonical workspace differs" in text or "stale" in text
        for text in entries[task_id]["blockers"]
    )


# ---------------------------------------------------------------------------
# recipe fingerprint binds proofs to the exact recipe/inputs
# ---------------------------------------------------------------------------


def test_recipe_fingerprint_changes_with_command_image_and_inputs(
    tmp_path, monkeypatch
):
    task_id, recipe, assets, _ = build_assets(tmp_path, monkeypatch)
    base = current_fingerprint(task_id, recipe, assets)

    changed = dict(recipe, command=recipe["command"] + " # tweak")
    assert current_fingerprint(task_id, changed, assets) != base

    changed = dict(recipe, golden_command="something else")
    assert current_fingerprint(task_id, changed, assets) != base

    changed = dict(recipe, shell=["/bin/sh", "-c"])
    assert current_fingerprint(task_id, changed, assets) != base

    changed = dict(recipe, mount={"start": "/elsewhere"})
    assert current_fingerprint(task_id, changed, assets) != base

    monkeypatch.setattr(MODULE, "docker_image_id", lambda image: "sha256:other")
    assert current_fingerprint(task_id, recipe, assets) != base
    monkeypatch.setattr(MODULE, "docker_image_id", lambda image: "sha256:abc")
    assert current_fingerprint(task_id, recipe, assets) == base

    # golden patch bytes are an input
    (assets / recipe["golden_patch"]).write_text("changed\n", encoding="utf-8")
    assert current_fingerprint(task_id, recipe, assets) != base


def test_manifest_rejects_proofs_recorded_under_a_different_recipe(
    tmp_path, monkeypatch
):
    task_id, recipe, assets, _ = build_assets(tmp_path, monkeypatch)
    monkeypatch.setitem(MODULE.RECIPES, task_id, recipe)
    fingerprint = current_fingerprint(task_id, recipe, assets)
    write_proof(
        assets, recipe, task_id, proof_runs("start", "golden", fingerprint=fingerprint)
    )
    args = MODULE.parse_args(
        ["manifest", "--assets-root", str(assets), "--legacy-root", str(tmp_path)]
    )
    MODULE.cmd_manifest(args)
    entries = {
        row["id"]: row
        for row in json.loads((assets / MODULE.MANIFEST_NAME).read_text())["tasks"]
    }
    assert entries[task_id]["status"] == "ready"
    assert entries[task_id]["recipe_fingerprint"] == fingerprint

    # change the executed command -> the recorded proofs are stale evidence
    monkeypatch.setitem(
        MODULE.RECIPES, task_id, dict(recipe, command=recipe["command"] + " --now")
    )
    MODULE.cmd_manifest(args)
    entries = {
        row["id"]: row
        for row in json.loads((assets / MODULE.MANIFEST_NAME).read_text())["tasks"]
    }
    assert entries[task_id]["status"] == "blocked"
    assert any("stale" in text for text in entries[task_id]["blockers"])


def test_proof_blockers_ignore_runs_from_another_fingerprint():
    recipe = {"golden_patch": "x.patch"}
    runs = [
        {"variant": "start", "exit_code": 0, "recipe_fingerprint": "old"},
        {"variant": "golden", "exit_code": 0, "recipe_fingerprint": "old"},
    ]
    assert MODULE.proof_blockers(recipe, runs) == []
    blockers = MODULE.proof_blockers(recipe, runs, "new")
    assert any("stale" in text for text in blockers)
    assert MODULE.proof_status(recipe, runs, "new") != "ready"


def test_docker_argv_cache_overlay_never_mounts_the_canonical_cache(tmp_path):
    task_id = "github__FuelLabs__fuel-tx__pull_197__comment_1003742605__complexity"
    recipe = MODULE.RECIPES[task_id]
    assets = tmp_path / "assets"
    cargo = assets / recipe["cargo_cache"]
    (cargo / "bin").mkdir(parents=True)
    (cargo / "bin/cargo").write_text("#!/bin/sh\n", encoding="utf-8")
    overlay = tmp_path / "overlay"
    argv = MODULE.docker_argv(
        task_id, recipe, assets, assets / recipe["workspace"],
        cache_overlay_root=overlay,
    )
    joined = " ".join(argv)
    assert f"{cargo}:/cargo-cache" not in joined
    assert f"{overlay / recipe['cargo_cache']}:/cargo-cache" in joined
    assert (overlay / recipe["cargo_cache"] / "bin/cargo").is_file()


# ---------------------------------------------------------------------------
# Every mounted input is covered, and content (not just size) is bound
# ---------------------------------------------------------------------------


def test_every_mount_source_of_every_recipe_is_covered_by_a_digest(tmp_path):
    """Guard: no replay-mounted input may escape the manifest binding.

    For every recipe, each mount source is either the exact-start subtree
    (verified through git ``workspace_state``) or must appear in
    ``mount_input_digests``/``mounted_cache_digests``.
    """
    for task_id, recipe in MODULE.RECIPES.items():
        assets = tmp_path / task_id[:24]
        workspace = assets / recipe["workspace"]
        start = workspace / recipe["patch_subdir"]
        make_start_tree(start)
        # materialize every declared non-start mount source
        for host in recipe.get("mount", {}):
            if host in (".", recipe["patch_subdir"]):
                continue
            target = workspace / host
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("payload\n", encoding="utf-8")
        for mount in recipe.get("harness_mounts", []):
            source = assets / mount["host"]
            source.mkdir(parents=True, exist_ok=True)
            (source / "harness.txt").write_text("h\n", encoding="utf-8")
        for copy in recipe.get("harness_copies", []):
            source = assets / copy["host"]
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("h\n", encoding="utf-8")
        for relative in list(recipe.get("cache_mounts", {})) + (
            [recipe["cargo_cache"]] if recipe.get("cargo_cache") else []
        ):
            cache = assets / relative
            cache.mkdir(parents=True, exist_ok=True)
            (cache / "blob").write_text("c\n", encoding="utf-8")

        mounts = MODULE.mount_input_digests(recipe, assets)
        caches = MODULE.mounted_cache_digests(recipe, assets)
        covered = set(mounts) | {f"cache:{key}" for key in caches}
        for host in recipe.get("mount", {}):
            if host == recipe["patch_subdir"]:
                continue  # git-verified start tree
            assert f"mount:{host}" in covered, (task_id, host)
            assert mounts[f"mount:{host}"] is not None, (task_id, host)
        for mount in recipe.get("harness_mounts", []):
            assert f"harness_mount:{mount['host']}" in covered, task_id
        for copy in recipe.get("harness_copies", []):
            assert f"harness_copy:{copy['host']}" in covered, task_id
        for relative in list(recipe.get("cache_mounts", {})) + (
            [recipe["cargo_cache"]] if recipe.get("cargo_cache") else []
        ):
            assert f"cache:{relative}" in covered, (task_id, relative)


def test_mount_digest_covers_workspace_siblings_for_dot_mounts(tmp_path, monkeypatch):
    """A ``mount: "."`` recipe exposes oracle.cpp/patches next to start/."""
    task_id, recipe, assets, _ = build_assets(tmp_path, monkeypatch)
    assert recipe["mount"] == {".": "/work"}
    workspace = assets / recipe["workspace"]
    (workspace / "oracle.cpp").write_text("int main(){}\n", encoding="utf-8")
    before = MODULE.mount_input_digests(recipe, assets)
    assert before["mount:."] is not None
    # same-size byte swap in the mounted harness source
    (workspace / "oracle.cpp").write_text("int main(){;}"[:13], encoding="utf-8")
    (workspace / "oracle.cpp").write_text("int main(){}\n".replace("{}", "{;"),
                                          encoding="utf-8")
    after = MODULE.mount_input_digests(recipe, assets)
    assert after != before
    # ... and the start subtree is excluded from that digest (git covers it)
    (workspace / recipe["patch_subdir"] / "file.txt").write_text(
        "y\n", encoding="utf-8"
    )
    assert MODULE.mount_input_digests(recipe, assets) == after


def test_tree_digest_detects_same_size_byte_swap(tmp_path):
    cache = tmp_path / "cache"
    (cache / "sub").mkdir(parents=True)
    blob = cache / "sub/blob.bin"
    blob.write_bytes(b"A" * 4096)
    before = MODULE.tree_digest(cache, use_cache=False)
    # identical size, identical mode, only the bytes differ
    blob.write_bytes(b"A" * 4095 + b"B")
    after = MODULE.tree_digest(cache, use_cache=False)
    assert before["file_count"] == after["file_count"] == 1
    assert before != after
    blob.write_bytes(b"A" * 4096)
    assert MODULE.tree_digest(cache, use_cache=False) == before


def test_file_input_digest_detects_same_size_byte_swap(tmp_path):
    path = tmp_path / "query.sql"
    path.write_text("SELECT 1;\n", encoding="utf-8")
    before = MODULE.file_input_digest(path)
    path.write_text("SELECT 2;\n", encoding="utf-8")
    after = MODULE.file_input_digest(path)
    assert before["size"] == after["size"]
    assert before != after
    assert MODULE.file_input_digest(tmp_path / "absent") is None


def test_manifest_blocks_after_same_size_tamper_of_a_mounted_input(
    tmp_path, monkeypatch
):
    task_id, recipe, assets, _ = build_assets(tmp_path, monkeypatch)
    monkeypatch.setitem(MODULE.RECIPES, task_id, recipe)
    workspace = assets / recipe["workspace"]
    (workspace / "oracle.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
    fingerprint = current_fingerprint(task_id, recipe, assets)
    write_proof(
        assets, recipe, task_id, proof_runs("start", "golden", fingerprint=fingerprint)
    )
    args = MODULE.parse_args(
        ["manifest", "--assets-root", str(assets), "--legacy-root", str(tmp_path)]
    )
    MODULE.cmd_manifest(args)
    entries = {
        row["id"]: row
        for row in json.loads((assets / MODULE.MANIFEST_NAME).read_text())["tasks"]
    }
    assert entries[task_id]["status"] == "ready"
    assert entries[task_id]["mount_input_digests"]["mount:."] is not None

    # same-size edit of the mounted oracle source -> proofs are stale
    (workspace / "oracle.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")
    MODULE.cmd_manifest(args)
    entries = {
        row["id"]: row
        for row in json.loads((assets / MODULE.MANIFEST_NAME).read_text())["tasks"]
    }
    assert entries[task_id]["status"] == "blocked"
    assert any("stale" in text for text in entries[task_id]["blockers"])


# ---------------------------------------------------------------------------
# Directory entries: symlinked dirs and empty dirs must be bound too
# ---------------------------------------------------------------------------


def build_symlinked_tree(root: Path) -> Path:
    (root / "sub").mkdir(parents=True)
    (root / "sub/f.txt").write_text("hello\n", encoding="utf-8")
    (root / "empty").mkdir()
    os.symlink("sub", root / "dirlink")
    os.symlink("sub/f.txt", root / "filelink")
    return root


def test_tree_digest_records_directories_symlinks_and_empty_dirs(tmp_path):
    tree = build_symlinked_tree(tmp_path / "tree")
    digest = MODULE.tree_digest(tree, use_cache=False)
    # 1 file + 2 real dirs (sub, empty) + 2 symlinks (dir + file)
    assert digest["entry_count"] == 5
    assert digest["file_count"] == 1
    assert digest["dir_count"] == 2
    assert digest["symlink_count"] == 2


def test_directory_symlink_retarget_and_removal_change_the_digest(tmp_path):
    tree = build_symlinked_tree(tmp_path / "tree")
    base = MODULE.tree_digest(tree, use_cache=False)

    # retarget the *directory* symlink -- same name, same mode, new target
    (tree / "dirlink").unlink()
    os.symlink("empty", tree / "dirlink")
    assert MODULE.tree_digest(tree, use_cache=False) != base
    (tree / "dirlink").unlink()
    os.symlink("sub", tree / "dirlink")
    assert MODULE.tree_digest(tree, use_cache=False) == base

    # remove it entirely
    (tree / "dirlink").unlink()
    assert MODULE.tree_digest(tree, use_cache=False) != base


def test_empty_directory_add_and_remove_change_the_digest(tmp_path):
    tree = build_symlinked_tree(tmp_path / "tree")
    base = MODULE.tree_digest(tree, use_cache=False)
    (tree / "brand-new-empty").mkdir()
    assert MODULE.tree_digest(tree, use_cache=False) != base
    (tree / "brand-new-empty").rmdir()
    assert MODULE.tree_digest(tree, use_cache=False) == base
    (tree / "empty").rmdir()
    assert MODULE.tree_digest(tree, use_cache=False) != base


def test_directory_symlinks_are_never_followed(tmp_path):
    """A symlink loop or an out-of-tree target must not be traversed."""
    tree = tmp_path / "tree"
    (tree / "real").mkdir(parents=True)
    (tree / "real/a.txt").write_text("a\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s\n", encoding="utf-8")
    os.symlink(str(outside), tree / "escape")
    os.symlink(".", tree / "loop")
    digest = MODULE.tree_digest(tree, use_cache=False)
    assert digest["file_count"] == 1  # only real/a.txt was hashed
    assert digest["symlink_count"] == 2
    # content behind the symlink is irrelevant; only the target string counts
    (outside / "secret.txt").write_text("changed\n", encoding="utf-8")
    assert MODULE.tree_digest(tree, use_cache=False) == digest


def test_manifest_blocks_when_a_mounted_directory_symlink_is_retargeted(
    tmp_path, monkeypatch
):
    task_id, recipe, assets, _ = build_assets(tmp_path, monkeypatch)
    monkeypatch.setitem(MODULE.RECIPES, task_id, recipe)
    workspace = assets / recipe["workspace"]
    (workspace / "deps").mkdir(parents=True, exist_ok=True)
    (workspace / "deps/lib.h").write_text("h\n", encoding="utf-8")
    (workspace / "alt").mkdir(exist_ok=True)
    os.symlink("deps", workspace / "vendor")
    fingerprint = current_fingerprint(task_id, recipe, assets)
    write_proof(
        assets, recipe, task_id, proof_runs("start", "golden", fingerprint=fingerprint)
    )
    args = MODULE.parse_args(
        ["manifest", "--assets-root", str(assets), "--legacy-root", str(tmp_path)]
    )
    MODULE.cmd_manifest(args)
    entries = {
        row["id"]: row
        for row in json.loads((assets / MODULE.MANIFEST_NAME).read_text())["tasks"]
    }
    assert entries[task_id]["status"] == "ready"

    (workspace / "vendor").unlink()
    os.symlink("alt", workspace / "vendor")
    MODULE.cmd_manifest(args)
    entries = {
        row["id"]: row
        for row in json.loads((assets / MODULE.MANIFEST_NAME).read_text())["tasks"]
    }
    assert entries[task_id]["status"] == "blocked"
    assert any("stale" in text for text in entries[task_id]["blockers"])
