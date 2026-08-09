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
  --gpu-memory-utilization 0.90 \
  --workers "$GENERATION_WORKERS" \
  --max-rounds 32 \
  --max-output-tokens 4096 \
  --server-timeout 7200 \
  --download-dir "/scratch/hf-cache/${AMLT_JOB_NAME:-job}" \
  --vllm-extra-arg=--enable-prefix-caching \
  --vllm-extra-arg=--max-num-seqs \
  --vllm-extra-arg="$MAX_NUM_SEQS"
