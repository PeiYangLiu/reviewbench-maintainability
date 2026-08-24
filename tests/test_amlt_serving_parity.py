"""Backfill configs must serve a model exactly as the board served it.

Rows produced by a backfill or retry job get merged into an existing board
release, so they only stay interchangeable with the rows they replace if the
model was served the same way. Tensor-parallel size and SKU are the settings
that change the served numerics (and, on FP8 checkpoints, the kernel the
weights get repacked for), so they are pinned per model. Concurrency is not
pinned: the board already mixes GENERATION_WORKERS/MAX_NUM_SEQS across its own
retry waves, and generation is unseeded, so batch composition is accepted
run-to-run variance.
"""

from pathlib import Path

import pytest
import yaml


CONFIGS = Path(__file__).parents[1] / "configs"
BOARD = CONFIGS / "amlt_openmodel_generation.yaml"

# Configs whose rows are merged into the board release built from BOARD.
MERGED_INTO_BOARD = [
    "amlt_openmodel_retries.yaml",
    "amlt_openmodel_uniform_transcript.yaml",
    "amlt_openmodel_context_fit.yaml",
    "amlt_openmodel_context_fit_kimi.yaml",
]

# Every config that regenerates rows for an already-serving model, paired with
# the config that established how that model is served.
CONTEXT_FIT_CONFIGS = [
    "amlt_openmodel_context_fit.yaml",
    "amlt_openmodel_context_fit_kimi.yaml",
]

PARITY_SOURCES = [
    "amlt_openmodel_generation.yaml",
    "amlt_openmodel_qwen_ladder.yaml",
    "amlt_openmodel_kimi.yaml",
    "amlt_openmodel_crossfamily.yaml",
]

# Settings that must match the board for a regenerated row to remain
# interchangeable with the board row it replaces.
PINNED_ENV = ("MODEL_REVISION", "TENSOR_PARALLEL_SIZE", "MAX_MODEL_LEN")


def load_jobs(path):
    return yaml.safe_load(path.read_text())["jobs"]


def serving(job):
    env = job["submit_args"]["env"]
    pinned = {key: env.get(key) for key in PINNED_ENV}
    pinned["sku"] = job["sku"]
    return env["MODEL_ID"], pinned


def board_serving():
    board = {}
    for job in load_jobs(BOARD):
        model, pinned = serving(job)
        previous = board.setdefault(model, pinned)
        assert previous == pinned, f"board serves {model} two ways: {previous} vs {pinned}"
    return board


@pytest.mark.parametrize("config_name", MERGED_INTO_BOARD)
def test_backfill_serves_each_model_exactly_as_the_board_does(config_name):
    board = board_serving()
    mismatches = []
    for job in load_jobs(CONFIGS / config_name):
        model, pinned = serving(job)
        if model not in board:
            continue
        if pinned != board[model]:
            differing = {
                key: (board[model][key], pinned[key])
                for key in pinned
                if pinned[key] != board[model][key]
            }
            mismatches.append((job["name"], model, differing))
    assert not mismatches, (
        f"{config_name} regenerates board rows under a different serving config "
        f"(board value, config value): {mismatches}"
    )


def test_board_pins_a_tensor_parallel_size_for_every_model():
    for model, pinned in board_serving().items():
        assert pinned["TENSOR_PARALLEL_SIZE"], f"{model} has no TENSOR_PARALLEL_SIZE"
        assert pinned["MODEL_REVISION"], f"{model} has no MODEL_REVISION"


def test_size_ladder_serves_every_rung_at_one_context_length():
    """The ladder only isolates size if context length is held constant.

    Giving a rung a shorter window because it would not otherwise fit turns the
    ladder into a size-and-context comparison, and the shortfall would land on
    the largest rung, which is the one the trend rests on.
    """
    jobs = load_jobs(CONFIGS / "amlt_openmodel_qwen_ladder.yaml")
    lengths = {job["submit_args"]["env"]["MAX_MODEL_LEN"] for job in jobs}
    assert len(lengths) == 1, f"ladder rungs serve different windows: {lengths}"

    scopes = {job["submit_args"]["env"]["GENERATION_IDS_FILE"] for job in jobs}
    per_model = {}
    for job in jobs:
        per_model.setdefault(job["submit_args"]["env"]["MODEL_ID"], set()).add(
            job["submit_args"]["env"]["GENERATION_IDS_FILE"]
        )
    assert all(shards == scopes for shards in per_model.values()), (
        f"ladder rungs cover different task sets: {per_model}"
    )


def test_guard_catches_a_downsized_backfill(tmp_path):
    board_job = load_jobs(BOARD)[0]
    model = board_job["submit_args"]["env"]["MODEL_ID"]
    shrunk = yaml.safe_load(yaml.safe_dump(board_job))
    shrunk["sku"] = "40G2-A100"
    shrunk["submit_args"]["env"]["TENSOR_PARALLEL_SIZE"] = "2"

    _, pinned = serving(shrunk)
    assert pinned != board_serving()[model]


def established_serving():
    """How every model is served by the config that first established it."""
    established = {}
    for name in PARITY_SOURCES:
        for job in load_jobs(CONFIGS / name):
            model, pinned = serving(job)
            if not pinned["MODEL_REVISION"]:
                continue
            established.setdefault(model, pinned)
    return established


SFT_LADDER_CONFIGS = [
    "amlt_openmodel_sft_ladder.yaml",
    "amlt_openmodel_sft_mixed.yaml",
]


def test_sft_ladder_adapters_are_served_like_their_own_baseline():
    """An adapter is measured against its base rung, so serving must not differ.

    A rank or window difference between an adapter and its baseline would show
    up as an effect of fine-tuning, which is the one thing this comparison is
    supposed to isolate.
    """
    rungs = {}
    for job in load_jobs(CONFIGS / "amlt_openmodel_qwen_ladder.yaml"):
        model, pinned = serving(job)
        rungs.setdefault(model, pinned)

    mismatches = []
    for name in SFT_LADDER_CONFIGS:
      for job in load_jobs(CONFIGS / name):
        model, pinned = serving(job)
        assert model in rungs, f"{model} has no baseline rung"
        if pinned != rungs[model]:
            mismatches.append((job["name"], rungs[model], pinned))
    assert not mismatches, f"adapters drift from their baseline rung: {mismatches}"


def test_sft_ladder_adapters_are_served_under_their_own_name():
    """Serving an adapter under the base name would score the wrong model."""
    for name in SFT_LADDER_CONFIGS:
      for job in load_jobs(CONFIGS / name):
        env = job["submit_args"]["env"]
        adapter = env["LORA_ADAPTER"]
        served, _, path = adapter.partition("=")
        assert served and path, adapter
        assert served != env["MODEL_ID"], f"{job['name']} serves the base name"
        assert (CONFIGS.parent / path).is_dir(), path
        assert env["MAX_LORA_RANK"], job["name"]


def test_sft_ladder_covers_the_same_scope_as_the_baseline_ladder():
    def scopes(name):
        return {
            job["submit_args"]["env"]["GENERATION_IDS_FILE"]
            for job in load_jobs(CONFIGS / name)
        }

    assert scopes("amlt_openmodel_sft_ladder.yaml") == scopes(
        "amlt_openmodel_qwen_ladder.yaml"
    )


def test_context_fit_regeneration_matches_each_model_established_serving():
    """The regenerated rows replace rows in place, so serving must not drift.

    This config spans the board, the size ladder and Kimi at once, and those
    three are served at three different window sizes, so a single shared
    setting here would silently retire whichever window it did not match.
    """
    established = established_serving()
    mismatches = []
    for name in CONTEXT_FIT_CONFIGS:
      for job in load_jobs(CONFIGS / name):
        model, pinned = serving(job)
        assert model in established, f"{model} has no established serving config"
        if pinned != established[model]:
            mismatches.append((job["name"], model, established[model], pinned))
    assert not mismatches, f"context-fit jobs drift from established serving: {mismatches}"


def test_context_fit_drops_the_hand_set_transcript_budgets():
    """Those budgets only existed to dodge the overflow the harness now handles."""
    for name in CONTEXT_FIT_CONFIGS:
      for job in load_jobs(CONFIGS / name):
        env = job["submit_args"]["env"]
        assert "MAX_TRANSCRIPT_CHARS" not in env, job["name"]
        assert env["GENERATION_IDS_FILE"].startswith(
            "configs/openmodel_scope_ids/context-fit/"
        ), job["name"]


def test_context_fit_scopes_exist_and_are_not_empty():
    for name in CONTEXT_FIT_CONFIGS:
      for job in load_jobs(CONFIGS / name):
        scope = CONFIGS.parent / job["submit_args"]["env"]["GENERATION_IDS_FILE"]
        assert scope.exists(), scope
        ids = [line for line in scope.read_text().splitlines() if line.strip()]
        assert ids, scope
        assert len(ids) == len(set(ids)), f"duplicate ids in {scope}"
