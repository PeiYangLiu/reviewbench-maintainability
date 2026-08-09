#!/usr/bin/env bash
# Generate a frozen Oracle-passing hard wave with all three solver models.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WAVE="${1:?usage: run_hard_wave_generation.sh WAVE_DIR REPO_CACHE}"
REPOS="${2:?usage: run_hard_wave_generation.sh WAVE_DIR REPO_CACHE}"
mkdir -p "$WAVE/generation"

DIRECT_ENDPOINT="${REVIEWBENCH_DIRECT_CHAT_ENDPOINT:-http://127.0.0.1:4141/v1/chat/completions}"
GPT_ENDPOINT="${REVIEWBENCH_GPT_CHAT_ENDPOINT:-http://127.0.0.1:8787/v1/chat/completions}"
WORKERS="${REVIEWBENCH_GENERATION_WORKERS:-8}"
restart_args=()
if [ "${REVIEWBENCH_RESTART_FAILED:-0}" = "1" ]; then
  restart_args+=(--restart-failed)
fi

run_model() {
  local model="$1" endpoint="$2"
  python3 "$ROOT/scripts/generate_agentic_patches.py" \
    --tasks "$WAVE/tasks.jsonl" \
    --repo-cache "$REPOS" \
    --out-dir "$WAVE/generation/$model" \
    --models "$model" \
    --workers "$WORKERS" \
    --max-rounds 32 \
    --max-output-tokens 32768 \
    --request-timeout 600 \
    --request-retries 6 \
    --chat-endpoint "$endpoint" \
    --force-chat \
    --resume \
    "${restart_args[@]}"
}

# Generation reads the shared repository cache with `git show`; edits live in
# isolated temporary directories. Claude and GPT can therefore run in parallel.
# Gemini follows Claude because both use the same direct endpoint by default.
failed=0
run_model "claude-opus-4.8" "$DIRECT_ENDPOINT" &
claude_pid="$!"
run_model "gpt-5.5" "$GPT_ENDPOINT" &
gpt_pid="$!"

wait "$claude_pid" || failed=1
wait "$gpt_pid" || failed=1
run_model "gemini-3.5-flash" "$DIRECT_ENDPOINT" || failed=1
exit "$failed"
