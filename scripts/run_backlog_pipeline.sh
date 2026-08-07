#!/usr/bin/env bash
# Chain the backlog pipeline: evidence -> GPT strict judge -> Claude verification.
#
# The evidence pass is long-running and git-bound while the judging passes are
# API-bound, so this waits for evidence to finish rather than interleaving them,
# then runs the two quality models in the fixed order the release gate expects:
# GPT-5.6 Sol decides the strict pass and Claude Opus 4.8 only reviews what GPT
# already passed, which is what keeps the second model's cost proportional to
# the surviving pool.
#
# Every stage is resumable, so re-running this after an interruption continues
# from the existing checkpoints instead of re-judging.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

BACKLOG="${BACKLOG_DIR:?BACKLOG_DIR must point at the backlog working directory}"
EXPECTED="${EXPECTED_EVIDENCE:-67939}"
LOG="$BACKLOG/pipeline.log"
# The gateway that serves the judge models. It exposes the OpenAI-compatible
# chat route but not the Copilot /token route, so every model is forced through
# chat rather than the responses API.
CHAT_ENDPOINT="${JUDGE_CHAT_ENDPOINT:-http://127.0.0.1:8787/v1/chat/completions}"

log() { echo "$(date -Is) $*" >>"$LOG"; }

evidence_running() {
  pgrep -f "reviewbench_maintainability.evidence --in $BACKLOG/backlog_pool.jsonl" >/dev/null
}

log "waiting for evidence to finish (expected $EXPECTED records)"
while evidence_running; do
  sleep 300
done

count="$(wc -l <"$BACKLOG/evidence.jsonl" 2>/dev/null || echo 0)"
log "evidence stage finished with $count records"
if [ "$count" -lt "$((EXPECTED / 2))" ]; then
  log "ABORT: evidence output is far below the expected size; not judging a partial pool"
  exit 1
fi

python3 - "$BACKLOG" <<'PY'
import json, pathlib, sys
backlog = pathlib.Path(sys.argv[1])
ready = []
for line in (backlog / "evidence.jsonl").read_text().splitlines():
    if not line.strip():
        continue
    record = json.loads(line)
    evidence = record.get("quality_evidence") or {}
    if evidence.get("status") == "ready" and evidence.get("patch_complete") is True:
        ready.append(record)
with (backlog / "evidence_ready.jsonl").open("w") as stream:
    for record in ready:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
print(f"evidence_ready={len(ready)}")
PY
log "evidence_ready=$(wc -l <"$BACKLOG/evidence_ready.jsonl")"

log "starting GPT-5.6 Sol strict judging"
python3 scripts/judge_quality_pool.py \
  --in "$BACKLOG/evidence_ready.jsonl" \
  --out "$BACKLOG/judge_gpt.jsonl" \
  --model gpt-5.6-sol \
  --workers "${JUDGE_WORKERS:-16}" \
  --max-output-tokens "${JUDGE_MAX_OUTPUT_TOKENS:-8192}" \
  --chat-endpoint "$CHAT_ENDPOINT" \
  --force-chat \
  --resume >>"$LOG" 2>&1
log "GPT judging finished with $(wc -l <"$BACKLOG/judge_gpt.jsonl" 2>/dev/null || echo 0) verdicts"

python3 - "$BACKLOG" <<'PY'
import json, pathlib, sys
backlog = pathlib.Path(sys.argv[1])
passed = {
    json.loads(line)["id"]
    for line in (backlog / "judge_gpt.jsonl").read_text().splitlines()
    if line.strip() and json.loads(line).get("strict_pass") is True
}
kept = [
    line
    for line in (backlog / "evidence_ready.jsonl").read_text().splitlines()
    if line.strip() and json.loads(line)["id"] in passed
]
(backlog / "gpt_pass_evidence.jsonl").write_text("\n".join(kept) + ("\n" if kept else ""))
print(f"gpt_strict_pass={len(kept)}")
PY
log "gpt_strict_pass=$(wc -l <"$BACKLOG/gpt_pass_evidence.jsonl")"

log "starting Claude Opus 4.8 verification"
python3 scripts/judge_quality_pool.py \
  --in "$BACKLOG/gpt_pass_evidence.jsonl" \
  --out "$BACKLOG/judge_claude.jsonl" \
  --model claude-opus-4.8 \
  --workers "${JUDGE_WORKERS:-16}" \
  --max-output-tokens "${JUDGE_MAX_OUTPUT_TOKENS:-8192}" \
  --chat-endpoint "$CHAT_ENDPOINT" \
  --force-chat \
  --resume >>"$LOG" 2>&1

python3 - "$BACKLOG" <<'PY'
import json, pathlib, sys
backlog = pathlib.Path(sys.argv[1])
def strict(path):
    return {
        json.loads(line)["id"]
        for line in (backlog / path).read_text().splitlines()
        if line.strip() and json.loads(line).get("strict_pass") is True
    }
dual = strict("judge_gpt.jsonl") & strict("judge_claude.jsonl")
kept = [
    line
    for line in (backlog / "evidence_ready.jsonl").read_text().splitlines()
    if line.strip() and json.loads(line)["id"] in dual
]
(backlog / "dual_strict.jsonl").write_text("\n".join(kept) + ("\n" if kept else ""))
summary = {
    "evidence_ready": sum(
        1 for l in (backlog / "evidence_ready.jsonl").read_text().splitlines() if l.strip()
    ),
    "gpt_strict_pass": len(strict("judge_gpt.jsonl")),
    "dual_strict_pass": len(dual),
}
(backlog / "pipeline_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
print(json.dumps(summary))
PY
log "dual_strict_pass=$(wc -l <"$BACKLOG/dual_strict.jsonl")"
log "pipeline complete; next stage is semantic dedup against the released tasks, then Oracle"
