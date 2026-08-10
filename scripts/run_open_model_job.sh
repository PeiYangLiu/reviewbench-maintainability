#!/usr/bin/env bash
set -euo pipefail

: "${MODEL_ID:?MODEL_ID is required}"
: "${MODEL_REVISION:?MODEL_REVISION is required}"
: "${TENSOR_PARALLEL_SIZE:?TENSOR_PARALLEL_SIZE is required}"
: "${MAX_MODEL_LEN:?MAX_MODEL_LEN is required}"
: "${GENERATION_WORKERS:?GENERATION_WORKERS is required}"
: "${MAX_NUM_SEQS:?MAX_NUM_SEQS is required}"
: "${AMLT_DATA_DIR:?AMLT_DATA_DIR is required}"
: "${AMLT_OUTPUT_DIR:?AMLT_OUTPUT_DIR is required}"

work="${AMLT_DIRSYNC_DIR:-/scratch/reviewbench-openmodels/${AMLT_JOB_NAME:-job}}"
generation_root="$work/run"
mkdir -p "$generation_root" "$AMLT_OUTPUT_DIR"
# The exact-start cache is a trusted, read-only BlobFuse mount whose ownership
# differs from the container user.
git config --global --add safe.directory '*'
python3 - "$AMLT_DATA_DIR" <<'PY'
import json
from pathlib import Path
import subprocess
import sys

root = Path(sys.argv[1]).resolve()
with Path("data/releases/hard/20260808/tasks.jsonl").open(
    encoding="utf-8"
) as stream:
    task = json.loads(next(stream))
repository = root / str(task["repo"]).replace("/", "__")
path = str((task.get("locality") or {}).get("path") or "")
completed = subprocess.run(
    [
        "git",
        "-c",
        f"safe.directory={repository}",
        "-C",
        str(repository),
        "cat-file",
        "-e",
        f"{task['start_sha']}:{path}",
    ],
    text=True,
    capture_output=True,
    check=False,
)
if completed.returncode:
    raise SystemExit(
        "repo-cache preflight failed for "
        f"{task['repo']}@{task['start_sha']}:{path}: "
        f"{completed.stderr.strip()[-1000:]}"
    )
print(f"repo-cache preflight passed: {task['repo']}@{task['start_sha']}:{path}")
PY
trust_args=()
if [ "${TRUST_REMOTE_CODE:-0}" = "1" ]; then
  trust_args+=(--trust-remote-code)
fi
vllm_args=(
  --vllm-extra-arg=--enable-prefix-caching
  --vllm-extra-arg=--max-num-seqs
  --vllm-extra-arg="$MAX_NUM_SEQS"
)
if [ "${ENABLE_EXPERT_PARALLEL:-0}" = "1" ]; then
  vllm_args+=(--vllm-extra-arg=--enable-expert-parallel)
fi
if [ "${LANGUAGE_MODEL_ONLY:-0}" = "1" ]; then
  vllm_args+=(--vllm-extra-arg=--language-model-only)
fi
if [ "${DISABLE_CUSTOM_ALL_REDUCE:-0}" = "1" ]; then
  vllm_args+=(--vllm-extra-arg=--disable-custom-all-reduce)
fi
if [ "${ENFORCE_EAGER:-0}" = "1" ]; then
  vllm_args+=(--vllm-extra-arg=--enforce-eager)
fi

persist() {
  status="$?"
  set +e
  if [ -d "$generation_root" ]; then
    tar -C "$generation_root" -czf \
      "$AMLT_OUTPUT_DIR/open-model-generation.tar.gz" .
  fi
  cp "$generation_root/open_model_manifest.json" \
    "$AMLT_OUTPUT_DIR/" 2>/dev/null || true
  find "$generation_root/generation" -name generation_summary.json \
    -exec cp {} "$AMLT_OUTPUT_DIR/generation_summary.json" \; \
    2>/dev/null || true
  touch "$AMLT_OUTPUT_DIR/.amlt_flush_upload" 2>/dev/null || true
  exit "$status"
}
trap persist EXIT

python3 scripts/run_open_model_agentic.py \
  --model "$MODEL_ID" \
  --revision "$MODEL_REVISION" \
  --served-model-name "$MODEL_ID" \
  --tasks data/releases/hard/20260808/tasks.jsonl \
  --repo-cache "$AMLT_DATA_DIR" \
  --out-dir "$generation_root" \
  --tensor-parallel-size "$TENSOR_PARALLEL_SIZE" \
  --max-model-len "$MAX_MODEL_LEN" \
  --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION:-0.90}" \
  --workers "$GENERATION_WORKERS" \
  --max-rounds 32 \
  --max-output-tokens 4096 \
  --server-timeout 7200 \
  --download-dir "/scratch/hf-cache/${AMLT_JOB_NAME:-job}" \
  "${trust_args[@]}" \
  "${vllm_args[@]}"
