#!/usr/bin/env python3
"""Serve an open-weight model with vLLM and run agentic patch generation."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import time
import urllib.request


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
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--max-transcript-chars", type=int, default=120_000)
    parser.add_argument("--server-timeout", type=int, default=3600)
    parser.add_argument("--generator-script", type=Path)
    parser.add_argument("--ids-file", type=Path)
    parser.add_argument("--download-dir", type=Path)
    parser.add_argument("--trust-remote-code", action="store_true")
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
    served_model_name = args.served_model_name or args.model
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
    server_command = [
        sys.executable,
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--model",
        args.model,
        "--served-model-name",
        served_model_name,
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
        server_command.extend(["--revision", args.revision])
    if args.download_dir:
        args.download_dir.mkdir(parents=True, exist_ok=True)
        server_command.extend(["--download-dir", str(args.download_dir)])
    if args.trust_remote_code:
        server_command.append("--trust-remote-code")
    server_command.extend(args.vllm_extra_arg)

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
