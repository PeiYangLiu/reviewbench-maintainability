#!/usr/bin/env bash
# Generate a frozen Oracle-passing hard wave with all three solver models.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WAVE="${1:?usage: run_hard_wave_generation.sh WAVE_DIR REPO_CACHE}"
REPOS="${2:?usage: run_hard_wave_generation.sh WAVE_DIR REPO_CACHE}"
mkdir -p "$WAVE/generation"

run_model() {
  local model="$1" endpoint="$2" workers="$3"
  python3 "$ROOT/scripts/generate_agentic_patches.py" \
    --tasks "$WAVE/tasks.jsonl" \
    --repo-cache "$REPOS" \
    --out-dir "$WAVE/generation/$model" \
    --models "$model" \
    --workers "$workers" \
    --max-rounds 32 \
    --max-output-tokens 32768 \
    --request-timeout 600 \
    --request-retries 6 \
    --chat-endpoint "$endpoint" \
    --force-chat \
    --resume
}

# Sequential model waves avoid concurrent git-worktree mutations in the shared
# repository cache. Each model internally runs repo-balanced task workers.
for spec in \
  "claude-opus-4.8 http://127.0.0.1:8787/v1/chat/completions 6" \
  "gemini-3.5-flash http://127.0.0.1:4141/v1/chat/completions 6" \
  "gpt-5.5 http://127.0.0.1:8787/v1/chat/completions 6"; do
  read -r model endpoint workers <<<"$spec"
  if ! run_model "$model" "$endpoint" "$workers"; then
    echo "$model completed with generation failures; continuing to next model" >&2
  fi
done
