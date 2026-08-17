import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "run_open_model_agentic.py"
SPEC = importlib.util.spec_from_file_location("run_open_model_agentic", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_package_versions_fails_closed(monkeypatch):
    monkeypatch.setattr(
        MODULE.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            1,
            stdout="",
            stderr="missing",
        ),
    )
    assert MODULE.package_versions() == {"error": "missing"}


def test_terminate_stops_only_supplied_process():
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"]
    )
    MODULE.terminate(process)
    assert process.poll() is not None


def test_ids_file_is_exposed_by_cli(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--model",
            "model",
            "--tasks",
            str(tmp_path / "tasks.jsonl"),
            "--repo-cache",
            str(tmp_path / "repos"),
            "--out-dir",
            str(tmp_path / "out"),
            "--ids-file",
            str(tmp_path / "ids.txt"),
        ],
    )

    assert MODULE.parse_args().ids_file == tmp_path / "ids.txt"


def test_restore_generation_checkpoint(tmp_path):
    source = tmp_path / "blob-run"
    destination = tmp_path / "local-run"
    result = source / "generation/model/generation_results.jsonl"
    patch = source / "generation/model/patches/task.patch"
    result.parent.mkdir(parents=True)
    patch.parent.mkdir(parents=True)
    result.write_text('{"id":"task","status":"generated"}\n')
    patch.write_text("patch\n")

    restored = MODULE.restore_generation_checkpoint(source, destination)

    assert restored == 2
    assert (
        destination / "generation/model/generation_results.jsonl"
    ).read_bytes() == result.read_bytes()
    assert (
        destination / "generation/model/patches/task.patch"
    ).read_bytes() == patch.read_bytes()


def test_restore_generation_checkpoint_without_source_is_noop(tmp_path):
    destination = tmp_path / "local-run"

    assert MODULE.restore_generation_checkpoint(None, destination) == 0
    assert (
        MODULE.restore_generation_checkpoint(tmp_path / "missing", destination)
        == 0
    )
    assert not destination.exists()


def test_restore_generation_checkpoint_preserves_local_state(tmp_path):
    source = tmp_path / "blob-run"
    destination = tmp_path / "local-run"
    remote = source / "generation/model/generation_results.jsonl"
    local = destination / "generation/model/generation_results.jsonl"
    remote.parent.mkdir(parents=True)
    local.parent.mkdir(parents=True)
    remote.write_text("remote\n")
    local.write_text("local\n")

    assert MODULE.restore_generation_checkpoint(source, destination) == 0
    assert local.read_text() == "local\n"


def lora_adapter_dir(tmp_path, *, base="Qwen/Qwen3.6-27B", rank=32):
    path = tmp_path / "adapter"
    path.mkdir(parents=True, exist_ok=True)
    (path / "adapter_config.json").write_text(
        json.dumps(
            {
                "base_model_name_or_path": base,
                "r": rank,
                "lora_alpha": 64,
                "target_modules": ["q_proj", "v_proj"],
            }
        ),
        encoding="utf-8",
    )
    (path / "adapter_model.safetensors").write_bytes(b"weights")
    return path


def server_args(tmp_path, **overrides):
    values = {
        "model": "Qwen/Qwen3.6-27B",
        "revision": None,
        "served_model_name": None,
        "tensor_parallel_size": 8,
        "port": 8000,
        "max_model_len": 32768,
        "gpu_memory_utilization": 0.9,
        "dtype": "auto",
        "download_dir": None,
        "trust_remote_code": False,
        "lora_adapter": None,
        "max_lora_rank": 32,
        "vllm_extra_arg": [],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_lora_adapter_is_served_under_its_own_name(tmp_path):
    path = lora_adapter_dir(tmp_path)
    args = server_args(tmp_path, lora_adapter=f"stage1-full={path}")

    served = MODULE.resolve_served_model_name(None, args.model, "stage1-full")
    command = MODULE.build_server_command(args, served)

    assert served == "stage1-full"
    assert "--enable-lora" in command
    assert f"stage1-full={path.resolve()}" in command
    assert command[command.index("--max-lora-rank") + 1] == "32"
    # the base model stays served under its own name so the adapter name is
    # unambiguous evidence that the adapted weights answered
    assert command[command.index("--served-model-name") + 1] == "Qwen/Qwen3.6-27B"


def test_requesting_the_base_name_with_an_adapter_fails_closed():
    with pytest.raises(ValueError, match="must be the adapter name"):
        MODULE.resolve_served_model_name(
            "Qwen/Qwen3.6-27B", "Qwen/Qwen3.6-27B", "stage1-full"
        )


def test_served_name_without_adapter_is_unchanged():
    assert MODULE.resolve_served_model_name(None, "base", None) == "base"
    assert MODULE.resolve_served_model_name("alias", "base", None) == "alias"


def test_adapter_trained_on_another_base_is_rejected(tmp_path):
    path = lora_adapter_dir(tmp_path, base="Qwen/Qwen3.6-35B-A3B")
    with pytest.raises(ValueError, match="was trained on"):
        MODULE.lora_provenance("stage1-full", path, "Qwen/Qwen3.6-27B", 32)


def test_adapter_rank_above_the_cap_is_rejected(tmp_path):
    path = lora_adapter_dir(tmp_path, rank=64)
    with pytest.raises(ValueError, match="rank 64"):
        MODULE.lora_provenance("stage1-full", path, "Qwen/Qwen3.6-27B", 32)


def test_adapter_provenance_pins_weight_digest(tmp_path):
    path = lora_adapter_dir(tmp_path)
    record = MODULE.lora_provenance("stage1-full", path, "Qwen/Qwen3.6-27B", 32)

    assert record["r"] == 32
    assert record["adapter_model_sha256"] == hashlib.sha256(b"weights").hexdigest()
    assert record["base_model_name_or_path"] == "Qwen/Qwen3.6-27B"


def test_no_lora_flags_without_an_adapter(tmp_path):
    command = MODULE.build_server_command(server_args(tmp_path), "base")
    assert "--enable-lora" not in command
    assert "--lora-modules" not in command
