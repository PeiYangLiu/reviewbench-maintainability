import importlib.util
import json
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
    MODULE._TREE_DIGEST_CACHE.clear()
    return task_id, recipe, assets, head


def proof_runs(*variants):
    return [
        {"variant": variant, "run": 1, "exit_code": 0, "stage": "execute"}
        for variant in variants
    ]


def write_proof(assets, recipe, task_id, runs):
    proof = assets / "proofs" / f"{recipe['case']}.json"
    proof.parent.mkdir(parents=True, exist_ok=True)
    proof.write_text(
        json.dumps({"id": task_id, "case": recipe["case"], "runs": runs}),
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

    # a start-only proof is NOT enough for a contract that declares a golden
    write_proof(assets, recipe, task_id, proof_runs("start"))
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
        proof_runs("start") + [
            {"variant": "golden", "run": 1, "exit_code": 1, "stage": "apply",
             "error": "golden patch failed to apply"}
        ],
    )
    assert MODULE.cmd_manifest(args) == 0
    entries = {
        row["id"]: row
        for row in json.loads((assets / MODULE.MANIFEST_NAME).read_text())["tasks"]
    }
    assert entries[task_id]["status"] == "blocked"

    # start + golden -> ready
    write_proof(assets, recipe, task_id, proof_runs("start", "golden"))
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
    write_proof(assets, recipe, task_id, proof_runs("start", "golden"))
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
    MODULE._TREE_DIGEST_CACHE.clear()
    digests = MODULE.mounted_cache_digests(recipe, assets)
    assert set(digests) == {"caches/conan"}
    assert digests["caches/conan"]["file_count"] == 1

    fuel = "github__FuelLabs__fuel-tx__pull_197__comment_1003742605__complexity"
    cargo = assets / MODULE.RECIPES[fuel]["cargo_cache"]
    (cargo / "bin").mkdir(parents=True)
    (cargo / "bin/cargo").write_text("#!/bin/sh\n", encoding="utf-8")
    MODULE._TREE_DIGEST_CACHE.clear()
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
