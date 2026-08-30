#!/usr/bin/env python3
"""Serve an open-weight model with vLLM and run agentic patch generation."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import time
import urllib.request

# The generator is a sibling module and owns the protocol vocabulary, so it is
# imported rather than mirrored: a launcher that offered a protocol the
# generator does not implement would fail only once a job was already running.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import generate_agentic_patches as agentic  # noqa: E402


def parse_lora_adapter(value: str) -> tuple[str, Path]:
    """Parse a ``NAME=PATH`` LoRA adapter specification.

    The name is what vLLM serves the adapter as, and therefore what the
    generator must request. It is deliberately *not* derived from the path so
    a run's recorded model identity stays a explicit, reviewed choice.
    """
    name, separator, raw_path = value.partition("=")
    if not separator or not name or not raw_path:
        raise ValueError(f"invalid --lora-adapter value: {value!r}")
    return name, Path(raw_path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def lora_provenance(name: str, path: Path, base_model: str, max_rank: int) -> dict:
    """Validate a LoRA adapter against the base model and pin its identity.

    Fails closed on the two mistakes that would silently invalidate an SFT
    evaluation: attaching an adapter trained on a *different* base model, and
    serving it with a rank cap below the trained rank (which vLLM rejects only
    at load time, after the harness has already committed to the run's name).
    """
    config_path = path / "adapter_config.json"
    if not config_path.is_file():
        raise ValueError(f"LoRA adapter has no adapter_config.json: {path}")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    trained_base = str(config.get("base_model_name_or_path") or "")
    if trained_base != base_model:
        raise ValueError(
            f"LoRA adapter {name!r} was trained on {trained_base!r} but is "
            f"being served on {base_model!r}"
        )
    rank = int(config.get("r") or 0)
    if rank <= 0:
        raise ValueError(f"LoRA adapter {name!r} declares no rank")
    if rank > max_rank:
        raise ValueError(
            f"LoRA adapter {name!r} has rank {rank} > --max-lora-rank {max_rank}"
        )
    weights = path / "adapter_model.safetensors"
    if not weights.is_file():
        raise ValueError(f"LoRA adapter has no adapter_model.safetensors: {path}")
    return {
        "name": name,
        "path": str(path.resolve()),
        "base_model_name_or_path": trained_base,
        "r": rank,
        "lora_alpha": config.get("lora_alpha"),
        "target_modules": sorted(config.get("target_modules") or []),
        "adapter_config_sha256": sha256_file(config_path),
        "adapter_model_sha256": sha256_file(weights),
    }


def resolve_served_model_name(
    requested: str | None,
    model: str,
    lora_name: str | None,
) -> str:
    """Return the model identity the generator must request.

    With a LoRA adapter attached, vLLM serves the adapter under its own name
    and continues to serve the untuned base under ``--model``. Requesting the
    base name would therefore silently evaluate the *unadapted* model while the
    run is recorded as an SFT run, so an explicit mismatch is rejected instead
    of being honoured.
    """
    if lora_name is None:
        return requested or model
    if requested is not None and requested != lora_name:
        raise ValueError(
            f"--served-model-name {requested!r} would request the base model "
            f"while LoRA adapter {lora_name!r} is attached; the served name "
            "must be the adapter name"
        )
    return lora_name


def build_server_command(args: argparse.Namespace, served_model_name: str) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--model",
        args.model,
        "--served-model-name",
        args.model if args.lora_adapter else served_model_name,
        "--tensor-parallel-size",
        str(args.tensor_parallel_size),
        "--port",
        str(args.port),
        "--max-model-len",
        str(args.max_model_len),
        "--gpu-memory-utilization",
        str(args.gpu_memory_utilization),
        "--dtype",
        args.dtype,
    ]
    if args.revision:
        command.extend(["--revision", args.revision])
    if args.download_dir:
        command.extend(["--download-dir", str(args.download_dir)])
    if args.trust_remote_code:
        command.append("--trust-remote-code")
    if args.lora_adapter:
        name, path = parse_lora_adapter(args.lora_adapter)
        command.extend(
            [
                "--enable-lora",
                "--max-lora-rank",
                str(args.max_lora_rank),
                "--lora-modules",
                f"{name}={path.resolve()}",
            ]
        )
    command.extend(args.vllm_extra_arg)
    return command


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--revision")
    parser.add_argument("--served-model-name")
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--repo-cache", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument(
        "--resume-from",
        type=Path,
        help="Restore a prior run's generation directory before resuming.",
    )
    parser.add_argument("--tensor-parallel-size", type=int, default=1)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--max-model-len", type=int, default=32768)
    parser.add_argument("--gpu-memory-utilization", type=float, default=0.9)
    parser.add_argument("--dtype", default="auto")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--max-rounds", type=int, default=32)
    parser.add_argument("--max-output-tokens", type=int, default=8192)
    parser.add_argument("--max-transcript-chars", type=int, default=120_000)
    parser.add_argument(
        "--protocol",
        choices=agentic.PROTOCOLS,
        default=agentic.PROTOCOL_V2,
        help="Action-history wire protocol passed to the generator.",
    )
    parser.add_argument(
        "--max-tool-result-chars",
        type=int,
        default=agentic.MAX_TOOL_RESULT_CHARS,
    )
    parser.add_argument(
        "--edit-deadline",
        type=int,
        default=0,
        help="v2 only: round from which a model that has not edited is told to.",
    )
    parser.add_argument("--server-timeout", type=int, default=3600)
    parser.add_argument("--generator-script", type=Path)
    parser.add_argument("--ids-file", type=Path)
    parser.add_argument("--download-dir", type=Path)
    parser.add_argument("--trust-remote-code", action="store_true")
    parser.add_argument(
        "--lora-adapter",
        metavar="NAME=PATH",
        help="Serve a LoRA adapter under NAME in addition to the base model. "
        "The generator then requests NAME, so recorded results belong to the "
        "adapted model rather than the base weights.",
    )
    parser.add_argument("--max-lora-rank", type=int, default=32)
    parser.add_argument("--vllm-extra-arg", action="append", default=[])
    return parser.parse_args()


def wait_for_server(
    endpoint: str,
    served_model_name: str,
    process: subprocess.Popen,
    timeout: int,
) -> dict:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"vLLM server exited with code {process.returncode}"
            )
        try:
            with urllib.request.urlopen(
                f"{endpoint}/models",
                timeout=10,
            ) as response:
                payload = json.load(response)
            model_ids = {
                str(item.get("id") or "")
                for item in payload.get("data") or []
            }
            if served_model_name not in model_ids:
                raise RuntimeError(
                    f"served model {served_model_name!r} not in {model_ids}"
                )
            return payload
        except Exception as exc:
            last_error = str(exc)
            time.sleep(10)
    raise TimeoutError(
        f"vLLM did not become ready within {timeout}s: {last_error}"
    )


def package_versions() -> dict[str, str]:
    code = (
        "import importlib.metadata as m, json; "
        "print(json.dumps({x:m.version(x) for x in "
        "['vllm','torch','transformers','huggingface-hub']}))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        return {"error": completed.stderr[-1000:]}
    return json.loads(completed.stdout)


def terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=60)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=30)


def restore_generation_checkpoint(
    source_root: Path | None,
    destination_root: Path,
) -> int:
    if source_root is None:
        return 0
    source = source_root / "generation"
    destination = destination_root / "generation"
    if not source.is_dir():
        return 0
    if source.resolve() == destination.resolve():
        return 0
    if destination.is_dir() and any(destination.iterdir()):
        return 0
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, dirs_exist_ok=True)
    return sum(path.is_file() for path in source.rglob("*"))


def main() -> int:
    args = parse_args()
    lora_name: str | None = None
    lora_record: dict | None = None
    if args.lora_adapter:
        lora_name, lora_path = parse_lora_adapter(args.lora_adapter)
        lora_record = lora_provenance(
            lora_name,
            lora_path,
            args.model,
            args.max_lora_rank,
        )
    served_model_name = resolve_served_model_name(
        args.served_model_name,
        args.model,
        lora_name,
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    restored_files = restore_generation_checkpoint(
        args.resume_from,
        args.out_dir,
    )
    if restored_files:
        print(
            f"restored {restored_files} generation checkpoint files from "
            f"{args.resume_from}",
            flush=True,
        )
    server_log_path = args.out_dir / "vllm-server.log"
    endpoint = f"http://127.0.0.1:{args.port}/v1"
    generator_script = args.generator_script or (
        Path(__file__).resolve().parent / "generate_agentic_patches.py"
    )
    if args.download_dir:
        args.download_dir.mkdir(parents=True, exist_ok=True)
    server_command = build_server_command(args, served_model_name)

    started_at = dt.datetime.now(dt.timezone.utc)
    models_payload: dict = {}
    returncode = 1
    error = None
    with server_log_path.open("w", encoding="utf-8") as server_log:
        server = subprocess.Popen(
            server_command,
            stdout=server_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            models_payload = wait_for_server(
                endpoint,
                served_model_name,
                server,
                args.server_timeout,
            )
            generation_dir = args.out_dir / "generation"
            generation_command = [
                sys.executable,
                str(generator_script),
                "--tasks",
                str(args.tasks),
                "--repo-cache",
                str(args.repo_cache),
                "--out-dir",
                str(generation_dir),
                "--models",
                served_model_name,
                "--workers",
                str(args.workers),
                "--max-rounds",
                str(args.max_rounds),
                "--max-output-tokens",
                str(args.max_output_tokens),
                "--max-transcript-chars",
                str(args.max_transcript_chars),
                "--protocol",
                args.protocol,
                "--max-tool-result-chars",
                str(args.max_tool_result_chars),
                "--edit-deadline",
                str(args.edit_deadline),
                "--request-timeout",
                "600",
                "--request-retries",
                "2",
                "--chat-endpoint",
                f"{endpoint}/chat/completions",
                "--force-chat",
                "--resume",
            ]
            if args.ids_file:
                ids = [
                    line.strip()
                    for line in args.ids_file.read_text(
                        encoding="utf-8"
                    ).splitlines()
                    if line.strip()
                ]
                if not ids:
                    raise ValueError(
                        f"ids file is empty: {args.ids_file}"
                    )
                generation_command.extend(["--ids", *ids])
            completed = subprocess.run(
                generation_command,
                text=True,
                check=False,
            )
            returncode = completed.returncode
        except Exception as exc:
            error = str(exc)[:4000]
        finally:
            terminate(server)

    manifest = {
        "schema_version": "reviewbench-open-model-generation/v1",
        "model": args.model,
        "revision": args.revision,
        "served_model_name": served_model_name,
        "lora_adapter": lora_record,
        "tensor_parallel_size": args.tensor_parallel_size,
        "max_model_len": args.max_model_len,
        "gpu_memory_utilization": args.gpu_memory_utilization,
        "dtype": args.dtype,
        "workers": args.workers,
        "max_rounds": args.max_rounds,
        "max_output_tokens": args.max_output_tokens,
        "restored_checkpoint_files": restored_files,
        "server_command": server_command,
        "models_response": models_payload,
        "packages": package_versions(),
        "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
        "started_at": started_at.isoformat(),
        "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "returncode": returncode,
        "error": error,
    }
    (args.out_dir / "open_model_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
