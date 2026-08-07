#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WAVE="${1:?usage: run_hard_wave_summary_when_ready.sh WAVE_DIR}"
EXPECTED=(
  "gopy.jsonl:156"
  "scripted.jsonl:105"
  "tsjs.jsonl:51"
  "csharp.jsonl:39"
  "jvm.jsonl:27"
  "rust.jsonl:42"
  "native.jsonl:57"
)

while true; do
  ready=true
  for spec in "${EXPECTED[@]}"; do
    file="${spec%%:*}"
    count="${spec##*:}"
    path="$WAVE/correctness/$file"
    actual=0
    [ -f "$path" ] && actual="$(wc -l <"$path")"
    if [ "$actual" -ne "$count" ]; then
      ready=false
      break
    fi
  done
  "$ready" && break
  sleep 120
done

stage="$WAVE/correctness-canonical"
mkdir -p "$stage"
for spec in "${EXPECTED[@]}"; do
  file="${spec%%:*}"
  cp "$WAVE/correctness/$file" "$stage/$file"
done

python3 "$ROOT/scripts/summarize_release_evaluation.py" \
  --tasks "$WAVE/tasks.jsonl" \
  --generation "claude-opus-4.8=$WAVE/generation/claude-opus-4.8/claude-opus-4.8/generation_results.jsonl" \
  --generation "gemini-3.5-flash=$WAVE/generation/gemini-3.5-flash/gemini-3.5-flash/generation_results.jsonl" \
  --generation "gpt-5.5=$WAVE/generation/gpt-5.5/gpt-5.5/generation_results.jsonl" \
  --correctness-dir "$stage" \
  --judgments "$WAVE/judgments_v2.jsonl" \
  --out "$WAVE/results.jsonl" \
  --report "$WAVE/report.json"
