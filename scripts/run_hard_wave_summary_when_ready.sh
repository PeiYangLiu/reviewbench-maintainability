#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WAVE="${1:?usage: run_hard_wave_summary_when_ready.sh WAVE_DIR}"
POLL_SECONDS="${HARD_WAVE_POLL_SECONDS:-120}"
TIMEOUT_SECONDS="${HARD_WAVE_WAIT_TIMEOUT_SECONDS:-86400}"

started_at="$(date +%s)"
while true; do
  if python3 "$ROOT/scripts/validate_hard_wave.py" ready \
    --wave "$WAVE" >/dev/null 2>&1; then
    break
  fi
  if [ "$(($(date +%s) - started_at))" -ge "$TIMEOUT_SECONDS" ]; then
    echo "timed out waiting for wave artifacts after ${TIMEOUT_SECONDS}s" >&2
    exit 1
  fi
  sleep "$POLL_SECONDS"
done

stage="$WAVE/correctness-canonical"

python3 "$ROOT/scripts/summarize_release_evaluation.py" \
  --tasks "$WAVE/tasks.jsonl" \
  --generation "claude-opus-4.8=$WAVE/generation/claude-opus-4.8/claude-opus-4.8/generation_results.jsonl" \
  --generation "gemini-3.5-flash=$WAVE/generation/gemini-3.5-flash/gemini-3.5-flash/generation_results.jsonl" \
  --generation "gpt-5.5=$WAVE/generation/gpt-5.5/gpt-5.5/generation_results.jsonl" \
  --correctness-dir "$stage" \
  --judgments "$WAVE/judgments_v2.jsonl" \
  --out "$WAVE/results.jsonl" \
  --report "$WAVE/report.json"

python3 "$ROOT/scripts/validate_hard_wave.py" summary --wave "$WAVE"
