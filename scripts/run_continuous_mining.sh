#!/usr/bin/env bash
# Keep mining continuously without exhausting the GitHub API rate limit.
#
# Throughput here is bounded by the authenticated core limit (5,000/hour), not
# by CPU: one batch fetches review comments, files and commits for up to
# --limit-per-batch pull requests. Running several miners concurrently simply
# drains the limit between guard checks and every batch then fails with
# "API rate limit exceeded", which is what left 363 batches unfinished in the
# 20260615 run.
#
# So exactly one miner runs at a time, with a conservative reserve, and the
# unfinished original manifest is completed before the expansion starts.
#
# The reserve must cover a whole batch, not just its first call: the guard runs
# before a batch starts, but a 250-pull-request batch then spends nine minutes
# burning through more than a thousand core calls and dies mid-run. A smaller
# batch with a larger reserve turned a 65% failure rate into steady progress.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

BASE_RUN="data/runs/scale-big-20260615"
EXPAND_RUN="data/runs/scale-expand-20260731"
LOG_DIR="${MINER_LOG_DIR:-$ROOT/data/runs/miner-supervisor}"
mkdir -p "$LOG_DIR"

pending_batches() {
  local run="$1"
  python3 - "$run" <<'PY'
import json, pathlib, sys
run = pathlib.Path(sys.argv[1])
manifest = json.loads((run / "manifest.json").read_text())
done = len(list((run / "batches").glob("*/done.json")))
print(len(manifest["queries"]) - done)
PY
}

run_miner() {
  local run="$1"
  python3 scripts/mine_scale.py \
    --out-dir "$run" \
    --resume-manifest \
    --workers 1 \
    --limit-per-batch 80 \
    --min-core-remaining 2000 \
    --min-search-remaining 5 \
    --sleep-safety-seconds 120 \
    >>"$LOG_DIR/$(basename "$run").log" 2>&1
}

while true; do
  for run in "$BASE_RUN" "$EXPAND_RUN"; do
    [ -f "$run/manifest.json" ] || continue
    remaining="$(pending_batches "$run")"
    echo "$(date -Is) $run pending=$remaining" >>"$LOG_DIR/supervisor.log"
    if [ "$remaining" -gt 0 ]; then
      run_miner "$run"
      break
    fi
  done
  sleep 300
done
