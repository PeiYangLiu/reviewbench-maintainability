#!/usr/bin/env bash
# Stage the RL session's three adapters and submit their evaluation.
#
# The RL arms live outside this repository and are gitignored, so they have to
# be copied in before AMLT uploads the code tree. This does that, verifies each
# payload against its own TRAIN_META.json, and refuses to submit unless all
# three arms are present -- running one arm now and the rest later would
# reintroduce the time-window confound the RL session explicitly asked to avoid
# (P80), and it is exactly the kind of thing that is easy to do by accident when
# only some adapters have landed.
set -euo pipefail

SOURCE="${RL_ADAPTER_SOURCE:-/data/v-peiyangliu/reviewbench-rl/adapters}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
STAGE="$REPO/rl-adapters"
ARMS=(qwen3-8b rft-qwen3-8b rft2-qwen3-8b)
EXPERIMENT="${1:-rl-three-arm-$(date +%Y%m%d)}"

missing=()
for arm in "${ARMS[@]}"; do
  [ -f "$SOURCE/$arm/adapter_model.safetensors" ] || missing+=("$arm")
done
if [ ${#missing[@]} -ne 0 ]; then
  echo "missing arms under $SOURCE: ${missing[*]}" >&2
  echo "all three must be present; a partial run cannot be attributed" >&2
  exit 1
fi

mkdir -p "$STAGE"
for arm in "${ARMS[@]}"; do
  rm -rf "${STAGE:?}/$arm"
  cp -r "$SOURCE/$arm" "$STAGE/$arm"
done

python3 - "$STAGE" "${ARMS[@]}" <<'PY'
import hashlib
import json
from pathlib import Path
import sys

stage = Path(sys.argv[1])
bases = set()
for arm in sys.argv[2:]:
    directory = stage / arm
    meta = json.loads((directory / "TRAIN_META.json").read_text())
    weights = directory / "adapter_model.safetensors"
    digest = hashlib.sha256(weights.read_bytes()).hexdigest()
    recorded = meta.get("adapter_sha256")
    if recorded and digest != recorded:
        raise SystemExit(f"{arm}: payload does not match its recorded hash")
    bases.add((meta["base_model"], meta["base_revision"]))
    print(f"{arm}: base={meta['base_model']}@{meta['base_revision'][:12]} ok")
if len(bases) != 1:
    # A delta across arms only means "training data" if the base is identical.
    raise SystemExit(f"arms disagree on the base model/revision: {sorted(bases)}")
print(f"all arms share {bases.pop()}")
PY

cd "$REPO"
# `amlt` only runs inside the amlt conda env, which has no pytest, so resolve an
# interpreter that can actually run the guards instead of skipping them.
PYTEST_PYTHON=""
for candidate in "${REVIEWBENCH_PYTEST_PYTHON:-}" python3 "$(dirname "$(command -v conda 2>/dev/null || echo /usr/bin)")/../bin/python3"; do
  [ -n "$candidate" ] || continue
  if "$candidate" -c 'import pytest' >/dev/null 2>&1; then
    PYTEST_PYTHON="$candidate"
    break
  fi
done
if [ -z "$PYTEST_PYTHON" ]; then
  echo "no interpreter with pytest found; set REVIEWBENCH_PYTEST_PYTHON" >&2
  echo "refusing to submit without running the single-variable guards" >&2
  exit 1
fi
"$PYTEST_PYTHON" -m pytest tests/test_amlt_serving_parity.py -q
export REVIEWBENCH_OPENMODEL_REPO_CACHE="${REVIEWBENCH_OPENMODEL_REPO_CACHE:-/data/v-peiyangliu/reviewbench-openmodels/repo-cache-minimal}"
amlt run configs/amlt_openmodel_rl_three_arm.yaml "$EXPERIMENT" --yes
