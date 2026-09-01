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


# The v2 protocol rerun regenerates every self-hosted board row. Its rows do not
# merge into the v1 board -- they build a separate v2 board -- but the whole
# point of the rerun is to attribute the v1/v2 difference to the protocol, which
# only holds if nothing else moved. So it is pinned against wherever each model's
# serving configuration was established.
V2_PROTOCOL_CONFIG = "amlt_openmodel_v2_protocol.yaml"
V2_SERVING_SOURCES = [
    "amlt_openmodel_generation.yaml",
    "amlt_openmodel_crossfamily.yaml",
]


def v2_reference_serving():
    """How each self-hosted model is served by the run v2 is compared against."""
    established = {}
    for name in V2_SERVING_SOURCES:
        for job in load_jobs(CONFIGS / name):
            model, pinned = serving(job)
            previous = established.setdefault(model, pinned)
            assert previous == pinned, (
                f"{model} is served two ways across {V2_SERVING_SOURCES}: "
                f"{previous} vs {pinned}"
            )
    return established


def test_v2_rerun_changes_the_protocol_and_nothing_else():
    established = v2_reference_serving()
    jobs = load_jobs(CONFIGS / V2_PROTOCOL_CONFIG)
    mismatches = []
    for job in jobs:
        model, pinned = serving(job)
        assert model in established, f"{model} has no established serving config"
        if pinned != established[model]:
            mismatches.append(
                (
                    job["name"],
                    model,
                    {
                        key: (established[model][key], pinned[key])
                        for key in pinned
                        if pinned[key] != established[model][key]
                    },
                )
            )
    assert not mismatches, (
        "v2 rerun serves a model differently from the run it is compared against "
        f"(established value, v2 value): {mismatches}"
    )
    assert {job["submit_args"]["env"]["MODEL_ID"] for job in jobs} == set(established)


def test_v2_rerun_declares_the_protocol_on_every_job():
    """A job that leaves PROTOCOL implicit would silently inherit a default."""
    for job in load_jobs(CONFIGS / V2_PROTOCOL_CONFIG):
        env = job["submit_args"]["env"]
        assert env.get("PROTOCOL") == "agentic-line-edit/v2", job["name"]
        assert env.get("MAX_TOOL_RESULT_CHARS") == "24000", job["name"]


def test_v2_rerun_covers_the_audited_scope_for_every_model():
    scopes = {
        job["submit_args"]["env"].get("GENERATION_IDS_FILE")
        for job in load_jobs(CONFIGS / V2_PROTOCOL_CONFIG)
    }
    assert scopes == {"configs/openmodel_scope_ids/audited-402.ids"}
    ids = (CONFIGS / "openmodel_scope_ids" / "audited-402.ids").read_text().split()
    assert len(ids) == len(set(ids)) == 402


# Multi-turn SFT adapters. PEFT does not record the base revision in
# adapter_config.json, so a config that names the wrong revision loads a
# different base without any error -- it only shows up as bad numbers. Each
# adapter ships a TRAIN_META.json recording what it was actually trained on,
# and that is the authority here.
NATIVE_MULTITURN_CONFIG = "amlt_openmodel_native_multiturn.yaml"
MT_SFT_CONFIG = "amlt_openmodel_mt_sft.yaml"
ADAPTER_ROOT = CONFIGS.parent / "sft-adapters-mt"
# Each adapter is compared against its own base rung, so it must be served the
# way that rung is served. Qwen3.6-27B's baseline is its board row, not a ladder
# rung, so it draws from the board config.
MT_SFT_REFERENCE = [
    "amlt_openmodel_qwen_ladder.yaml",
    "amlt_openmodel_generation.yaml",
]


def adapter_dir(job):
    """Return the adapter path from a NAME=PATH LORA_ADAPTER value."""
    value = job["submit_args"]["env"]["LORA_ADAPTER"]
    _, _, path = value.partition("=")
    return CONFIGS.parent / path


def test_mt_sft_pins_the_base_revision_each_adapter_was_trained_on():
    import json

    for job in load_jobs(CONFIGS / MT_SFT_CONFIG):
        env = job["submit_args"]["env"]
        meta_path = adapter_dir(job) / "TRAIN_META.json"
        if not meta_path.exists():
            pytest.skip(f"adapter payload not staged: {meta_path}")
        meta = json.loads(meta_path.read_text())
        assert env["MODEL_ID"] == meta["base_model"], job["name"]
        assert env["MODEL_REVISION"] == meta["base_revision"], (
            f"{job['name']} would load a different base than it was trained on: "
            f"config {env['MODEL_REVISION']} vs TRAIN_META {meta['base_revision']}"
        )


def test_mt_sft_adapter_payloads_match_their_recorded_hash():
    import hashlib
    import json

    for job in load_jobs(CONFIGS / MT_SFT_CONFIG):
        directory = adapter_dir(job)
        meta_path = directory / "TRAIN_META.json"
        weights = directory / "adapter_model.safetensors"
        if not meta_path.exists() or not weights.exists():
            pytest.skip(f"adapter payload not staged: {directory}")
        expected = json.loads(meta_path.read_text()).get("adapter_sha256")
        if not expected:
            continue
        digest = hashlib.sha256(weights.read_bytes()).hexdigest()
        assert digest == expected, f"{job['name']} adapter payload does not match"


def test_mt_sft_serves_each_adapter_like_its_base_rung():
    reference = {}
    for name in MT_SFT_REFERENCE:
        for job in load_jobs(CONFIGS / name):
            if job["submit_args"]["env"].get("LORA_ADAPTER"):
                continue
            model, pinned = serving(job)
            reference.setdefault(model, pinned)
    mismatches = []
    for job in load_jobs(CONFIGS / MT_SFT_CONFIG):
        model, pinned = serving(job)
        base = reference.get(model)
        assert base, f"{model} has no base rung to compare against"
        differing = {
            key: (base[key], pinned[key])
            for key in ("sku", "TENSOR_PARALLEL_SIZE", "MAX_MODEL_LEN", "MODEL_REVISION")
            if base[key] != pinned[key]
        }
        if differing:
            mismatches.append((job["name"], model, differing))
    assert not mismatches, (
        "an adapter is served differently from the base rung it is compared "
        f"against (base value, adapter value): {mismatches}"
    )


def test_mt_sft_runs_the_protocol_the_adapters_were_trained_on():
    """v1 would reproduce the train/inference mismatch that voided two rounds."""
    for job in load_jobs(CONFIGS / MT_SFT_CONFIG):
        env = job["submit_args"]["env"]
        assert env.get("PROTOCOL") == "agentic-line-edit/v2", job["name"]
        assert env.get("MAX_TOOL_RESULT_CHARS") == "24000", job["name"]
        assert env.get("MAX_LORA_RANK") == "32", job["name"]
        assert (
            env.get("GENERATION_IDS_FILE")
            == "configs/openmodel_scope_ids/audited-402.ids"
        ), job["name"]


# The v2 base ladder is the control for the SFT adapters: same protocol, same
# period, same serving. If it drifts from either the v1 ladder's serving or the
# adapter jobs' serving, "SFT helped/hurt" stops being a one-variable claim.
V2_BASE_LADDER = "amlt_openmodel_v2_base_ladder.yaml"


def test_v2_base_ladder_matches_the_v1_ladder_serving():
    reference = {}
    for job in load_jobs(CONFIGS / "amlt_openmodel_qwen_ladder.yaml"):
        model, pinned = serving(job)
        reference.setdefault(model, pinned)
    for job in load_jobs(CONFIGS / V2_BASE_LADDER):
        model, pinned = serving(job)
        assert pinned == reference[model], (
            f"{job['name']} is served differently from the v1 ladder rung it "
            f"replaces: {reference[model]} vs {pinned}"
        )


def test_each_adapter_has_a_base_row_under_the_same_protocol():
    base = {}
    for job in load_jobs(CONFIGS / V2_BASE_LADDER):
        env = job["submit_args"]["env"]
        assert env["PROTOCOL"] == "agentic-line-edit/v2", job["name"]
        assert not env.get("LORA_ADAPTER"), f"{job['name']} is not a base row"
        model, pinned = serving(job)
        base[model] = pinned
    missing = []
    for job in load_jobs(CONFIGS / MT_SFT_CONFIG):
        model, pinned = serving(job)
        if model not in base:
            # Qwen3.6-27B's control is its board row, not a ladder rung.
            continue
        if pinned != base[model]:
            missing.append((job["name"], model, base[model], pinned))
    assert not missing, (
        f"adapter and its v2 base row are served differently: {missing}"
    )


# The deadline probe must differ from its SFT rows in exactly one setting, or it
# cannot attribute a recovered generation rate to the stopping signal.
DEADLINE_CONFIG = "amlt_openmodel_mt_sft_deadline.yaml"


def test_deadline_probe_changes_only_the_edit_deadline():
    sft = {}
    for job in load_jobs(CONFIGS / MT_SFT_CONFIG):
        env = job["submit_args"]["env"]
        sft[env["LORA_ADAPTER"]] = (job["sku"], dict(env))
    for job in load_jobs(CONFIGS / DEADLINE_CONFIG):
        env = job["submit_args"]["env"]
        sku, base_env = sft[env["LORA_ADAPTER"]]
        assert job["sku"] == sku, job["name"]
        differing = {
            key
            for key in set(base_env) | set(env)
            if base_env.get(key) != env.get(key)
        }
        assert differing == {"EDIT_DEADLINE"}, (
            f"{job['name']} differs from its SFT row in more than the deadline: "
            f"{sorted(differing)}"
        )
        assert env["EDIT_DEADLINE"] == "24"


def test_published_runs_left_the_deadline_off():
    """The v2 board and the SFT table were produced without the nudge."""
    for name in (
        "amlt_openmodel_v2_protocol.yaml",
        "amlt_openmodel_v2_base_ladder.yaml",
        MT_SFT_CONFIG,
    ):
        for job in load_jobs(CONFIGS / name):
            env = job["submit_args"]["env"]
            assert not env.get("EDIT_DEADLINE"), f"{name}:{job['name']}"


def test_output_budget_defaults_cover_observed_generations():
    """4096 truncated 10-35% of rounds on prose-first models (P82).

    The budget shares `max_model_len` with the prompt, so it cannot simply be
    maximised: measured prompt_tokens reach 61,425 against a 65,536 window,
    leaving 4,111. 8192 covers every observed generation (longest content was
    12,555 chars, ~3-4k tokens) while leaving the prompt its p99 headroom.
    """
    import re

    runner = (CONFIGS.parent / "scripts" / "run_open_model_job.sh").read_text()
    match = re.search(r"--max-output-tokens \"\$\{MAX_OUTPUT_TOKENS:-(\d+)\}\"", runner)
    assert match, "runner no longer sets a default output budget"
    assert int(match.group(1)) >= 8192, (
        "output budget below 8192 truncates prose-first models mid-action (P82)"
    )

    launcher = (CONFIGS.parent / "scripts" / "run_open_model_agentic.py").read_text()
    match = re.search(
        r'add_argument\("--max-output-tokens", type=int, default=(\d+)\)', launcher
    )
    assert match and int(match.group(1)) >= 8192


# Frozen v1-era configs are records of what actually ran and must not be
# edited. `amlt_openmodel_retries.yaml` pins 1024/2048, which truncates even
# harder than the 4096 that produced P82 -- noted there, not fixed here.
FROZEN_V1_CONFIGS = {"amlt_openmodel_retries.yaml"}


# A budget below 8192 truncates high-reasoning models (P82), but raising it
# also spends the shared `max_model_len` on output the model does not use
# (P88), so a low budget is a deliberate choice a config has to declare.
DECLARED_LOW_BUDGET_CONFIGS = {"amlt_openmodel_rl_three_arm_budget4k.yaml"}


def test_no_current_config_pins_a_truncating_output_budget_by_accident():
    """8192 is the default; anything lower must be an explicit control.

    The first version of this guard simply banned <8192. That was too strong:
    for a model that truncates at ~0% the extra budget is pure cost, and the
    guard would have blocked the control run that measured it. What must not
    happen is a config drifting back to 4096 unnoticed.
    """
    for path in sorted(CONFIGS.glob("amlt_openmodel_*.yaml")):
        if path.name in FROZEN_V1_CONFIGS:
            continue
        declared = path.name in DECLARED_LOW_BUDGET_CONFIGS
        for job in load_jobs(path):
            budget = job["submit_args"]["env"].get("MAX_OUTPUT_TOKENS")
            if budget is None:
                continue
            if declared:
                assert int(budget) < 8192, (
                    f"{path.name} is registered as a low-budget control but "
                    f"{job['name']} pins {budget}"
                )
                continue
            assert int(budget) >= 8192, f"{path.name}:{job['name']} pins {budget}"


def test_a_low_budget_config_explains_itself():
    """A control that does not say why it is low is indistinguishable from a
    regression, which is the failure this whole guard exists to prevent."""
    for name in DECLARED_LOW_BUDGET_CONFIGS:
        text = (CONFIGS / name).read_text(encoding="utf-8")
        assert "P88" in text, f"{name} must cite why a low budget is correct"


def test_budget8k_rerun_changes_only_the_output_budget():
    """The P82 re-run must isolate the budget, or it cannot be attributed."""
    base = {}
    for job in load_jobs(CONFIGS / "amlt_openmodel_v2_protocol.yaml"):
        env = job["submit_args"]["env"]
        base[env["MODEL_ID"]] = (job["sku"], dict(env))
    jobs = load_jobs(CONFIGS / "amlt_openmodel_v2_budget8k.yaml")
    assert len(jobs) == len(base)
    for job in jobs:
        env = job["submit_args"]["env"]
        sku, base_env = base[env["MODEL_ID"]]
        assert job["sku"] == sku, job["name"]
        differing = {
            key
            for key in set(base_env) | set(env)
            if base_env.get(key) != env.get(key)
        }
        assert differing == {"MAX_OUTPUT_TOKENS"}, (
            f"{job['name']} differs from its 4096 row in more than the budget: "
            f"{sorted(differing)}"
        )
        assert env["MAX_OUTPUT_TOKENS"] == "8192"


# The RL arms differ only in training data, so the config must differ only in
# the adapter. The RL session asked for all three in one time window citing P80;
# this is the machine-checkable half of that request.
RL_CONFIG = "amlt_openmodel_rl_three_arm.yaml"


def test_rl_arms_differ_only_in_the_adapter():
    jobs = load_jobs(CONFIGS / RL_CONFIG)
    assert len(jobs) == 3, "the three-arm comparison needs exactly three arms"
    envs = [job["submit_args"]["env"] for job in jobs]
    assert len({job["sku"] for job in jobs}) == 1
    keys = set().union(*(set(e) for e in envs))
    differing = {k for k in keys if len({e.get(k) for e in envs}) > 1}
    assert differing == {"LORA_ADAPTER"}, (
        f"RL arms differ in more than the adapter: {sorted(differing)}"
    )


def test_rl_arms_match_the_qwen3_8b_base_rung():
    """The untuned base row from the v2 ladder is the fourth reference point."""
    base = None
    for job in load_jobs(CONFIGS / "amlt_openmodel_v2_base_ladder.yaml"):
        if job["submit_args"]["env"]["MODEL_ID"] == "Qwen/Qwen3-8B":
            base = serving(job)[1]
    assert base
    for job in load_jobs(CONFIGS / RL_CONFIG):
        pinned = serving(job)[1]
        assert pinned == base, (
            f"{job['name']} is served differently from the Qwen3-8B base rung: "
            f"{base} vs {pinned}"
        )


def test_rl_arms_use_the_untruncated_output_budget():
    """At 4096 the longest-reasoning arm would lose the most rounds (P82)."""
    for job in load_jobs(CONFIGS / RL_CONFIG):
        env = job["submit_args"]["env"]
        assert env["MAX_OUTPUT_TOKENS"] == "8192", job["name"]
        assert env["PROTOCOL"] == "agentic-line-edit/v2", job["name"]


def test_sft_budget8k_rerun_changes_only_the_output_budget():
    """The SFT 'no effect' verdict was measured at 4096 and needs re-measuring."""
    base = {}
    for job in load_jobs(CONFIGS / MT_SFT_CONFIG):
        env = job["submit_args"]["env"]
        base[env["LORA_ADAPTER"]] = (job["sku"], dict(env))
    jobs = load_jobs(CONFIGS / "amlt_openmodel_mt_sft_budget8k.yaml")
    assert len(jobs) == len(base)
    for job in jobs:
        env = job["submit_args"]["env"]
        sku, base_env = base[env["LORA_ADAPTER"]]
        assert job["sku"] == sku, job["name"]
        differing = {
            key
            for key in set(base_env) | set(env)
            if base_env.get(key) != env.get(key)
        }
        assert differing == {"MAX_OUTPUT_TOKENS"}, job["name"]
        assert env["MAX_OUTPUT_TOKENS"] == "8192"


def test_rl_three_arm_budget_control_changes_only_the_output_budget():
    """The 8192 run costs these arms prompt room for no truncation gain (P88).

    The control has to differ in exactly one field or it cannot attribute the
    difference to the budget.
    """
    base = {}
    for job in load_jobs(CONFIGS / "amlt_openmodel_rl_three_arm.yaml"):
        env = job["submit_args"]["env"]
        base[env["LORA_ADAPTER"]] = (job["sku"], dict(env))
    jobs = load_jobs(CONFIGS / "amlt_openmodel_rl_three_arm_budget4k.yaml")
    assert len(jobs) == len(base) == 3
    for job in jobs:
        env = job["submit_args"]["env"]
        sku, base_env = base[env["LORA_ADAPTER"]]
        assert job["sku"] == sku, job["name"]
        differing = {
            key
            for key in set(base_env) | set(env)
            if base_env.get(key) != env.get(key)
        }
        assert differing == {"MAX_OUTPUT_TOKENS"}, job["name"]
        assert env["MAX_OUTPUT_TOKENS"] == "4096"
        assert env["MAX_MODEL_LEN"] == base_env["MAX_MODEL_LEN"]


def test_sampling_noise_replicates_are_identical_to_each_other():
    """Their whole purpose is to differ in nothing at all.

    Arm-vs-arm claims are only readable against a measured noise floor, and a
    replicate set that differs in any field measures something else.
    """
    jobs = load_jobs(CONFIGS / "amlt_openmodel_sampling_noise.yaml")
    assert len(jobs) == 3
    environments = [job["submit_args"]["env"] for job in jobs]
    first = environments[0]
    for env in environments[1:]:
        assert env == first
    assert len({job["sku"] for job in jobs}) == 1
    assert len({job["name"] for job in jobs}) == 3


def test_native_multiturn_pairs_each_checkpoint_with_its_own_base():
    """A tuned model is only readable against its base in the same window (P80)."""
    jobs = {job["name"]: job for job in load_jobs(CONFIGS / NATIVE_MULTITURN_CONFIG)}
    assert set(jobs) == {
        "native-qwen3-8b",
        "base-qwen3-8b",
        "native-qwen3-14b",
        "base-qwen3-14b",
    }
    for size in ("8b", "14b"):
        tuned = jobs[f"native-qwen3-{size}"]["submit_args"]["env"]
        base = jobs[f"base-qwen3-{size}"]["submit_args"]["env"]
        assert jobs[f"native-qwen3-{size}"]["sku"] == jobs[f"base-qwen3-{size}"]["sku"]
        differing = {
            key
            for key in set(tuned) | set(base)
            if tuned.get(key) != base.get(key)
        }
        assert differing == {"MODEL_ID", "MODEL_REVISION"}, size


def test_native_multiturn_uses_the_trained_protocol_and_round_budget():
    """The wrapper and `OF 16` are part of the trained format, not preferences."""
    for job in load_jobs(CONFIGS / NATIVE_MULTITURN_CONFIG):
        env = job["submit_args"]["env"]
        assert env["PROTOCOL"] == "agentic-line-edit/v2-tool-response", job["name"]
        assert env["MAX_ROUNDS"] == "16", job["name"]
        assert env["MAX_OUTPUT_TOKENS"] == "8192", job["name"]


def test_a_local_checkpoint_pins_no_revision_and_a_hub_id_always_does():
    """A revision on a local directory is a made-up value that reads as provenance."""
    for job in load_jobs(CONFIGS / NATIVE_MULTITURN_CONFIG):
        env = job["submit_args"]["env"]
        model = env["MODEL_ID"]
        revision = env.get("MODEL_REVISION", "")
        if model.startswith("/"):
            assert revision == "", job["name"]
        else:
            assert len(revision) == 40, job["name"]
            assert set(revision) <= set("0123456789abcdef"), job["name"]


def test_banner_ablation_changes_only_the_served_format():
    """It must differ from the trained format in the format alone.

    The point is to tell "learned when to stop" apart from "is being told how
    much budget is left"; any other difference makes the answer unreadable.
    """
    trained = {
        job["submit_args"]["env"]["MODEL_ID"]: job
        for job in load_jobs(CONFIGS / NATIVE_MULTITURN_CONFIG)
        if job["name"].startswith("native-")
    }
    ablation = load_jobs(CONFIGS / "amlt_openmodel_native_banner_ablation.yaml")
    assert len(ablation) == 2
    for job in ablation:
        env = job["submit_args"]["env"]
        base = trained[env["MODEL_ID"]]
        assert job["sku"] == base["sku"], job["name"]
        differing = {
            key
            for key in set(env) | set(base["submit_args"]["env"])
            if env.get(key) != base["submit_args"]["env"].get(key)
        }
        assert differing == {"PROTOCOL", "MAX_ROUNDS"}, job["name"]
        assert env["PROTOCOL"] == "agentic-line-edit/v2"
        assert env["MAX_ROUNDS"] == "32"


def test_the_two_ablation_arms_differ_only_in_the_round_cap():
    """The first ablation moved format and cap together, so it cannot attribute.

    This pair holds the format fixed and moves only the cap, which is what
    separates "the banner mattered" from "the extra rounds mattered".
    """
    wide = {
        job["submit_args"]["env"]["MODEL_ID"]: job
        for job in load_jobs(CONFIGS / "amlt_openmodel_native_banner_ablation.yaml")
    }
    narrow = load_jobs(CONFIGS / "amlt_openmodel_native_banner_ablation_r16.yaml")
    assert len(narrow) == 2
    for job in narrow:
        env = job["submit_args"]["env"]
        other = wide[env["MODEL_ID"]]["submit_args"]["env"]
        differing = {
            key for key in set(env) | set(other) if env.get(key) != other.get(key)
        }
        assert differing == {"MAX_ROUNDS"}, job["name"]
        assert env["MAX_ROUNDS"] == "16"
        assert env["PROTOCOL"] == "agentic-line-edit/v2"


def test_rl_replicates_differ_only_in_job_name_within_an_arm():
    """Three runs of one arm must be identical or they measure something else.

    A single run put the arms at 34/33/40 correct while three runs of one
    unchanged adapter spanned 29-39, so these replicates are the only thing
    that can say whether the arm ordering is real.
    """
    jobs = load_jobs(CONFIGS / "amlt_openmodel_rl_three_arm_replicates.yaml")
    assert len(jobs) == 9
    by_arm = {}
    for job in jobs:
        arm = job["submit_args"]["env"]["LORA_ADAPTER"]
        by_arm.setdefault(arm, []).append(job)
    assert len(by_arm) == 3
    for arm, group in by_arm.items():
        assert len(group) == 3, arm
        first = group[0]
        for other in group[1:]:
            assert other["submit_args"]["env"] == first["submit_args"]["env"], arm
            assert other["sku"] == first["sku"], arm
        assert len({job["name"] for job in group}) == 3, arm


def test_rl_replicates_match_the_original_three_arm_run():
    """They have to be comparable with the single run already measured."""
    original = {
        job["submit_args"]["env"]["LORA_ADAPTER"]: job
        for job in load_jobs(CONFIGS / "amlt_openmodel_rl_three_arm.yaml")
    }
    for job in load_jobs(CONFIGS / "amlt_openmodel_rl_three_arm_replicates.yaml"):
        env = job["submit_args"]["env"]
        assert env == original[env["LORA_ADAPTER"]]["submit_args"]["env"], job["name"]


def test_every_serving_config_drops_the_cuda_mismatched_video_decoder():
    """vllm imports a video decoder at startup and torchcodec kills the server.

    Its current build links libnvrtc.so.13 against an image shipping CUDA 12.6,
    so the server dies before loading weights and the job fails with no output.
    Nine replicate jobs were lost to this. Nothing here decodes video, so the
    dependency has to be gone from every config that installs vllm.
    """
    for path in sorted(CONFIGS.glob("amlt_openmodel_*.yaml")):
        text = path.read_text(encoding="utf-8")
        if "pip install" not in text or "vllm==" not in text:
            continue
        assert "uninstall -y torchcodec" in text, path.name
