#!/usr/bin/env bash
# Runtime-only transport. The original wrapper owns every behavioral assertion.
set -euo pipefail

: "${REVIEWBENCH_GO:?pinned Go compiler required}"
: "${REVIEWBENCH_GO_MODULE_DIR:?whole candidate module required}"
: "${TMPDIR:?task-local temporary directory required}"
export PATH="$(dirname "$REVIEWBENCH_GO"):${PATH}"

# A retained CLI-boundary wrapper sets PATH for its fake `az`. An exported Bash
# function preserves that subprocess PATH while keeping Go on the pinned asset.
# Login-shell inline contracts also inherit this function.
go() { "$REVIEWBENCH_GO" "$@"; }
export -f go

snapshot="$(mktemp -d "${TMPDIR}/go-candidate-modules.XXXXXX")"
export REVIEWBENCH_GO_CANDIDATE_MODULES="$snapshot"
for name in go.mod go.sum; do
    if [[ -f "${REVIEWBENCH_GO_MODULE_DIR}/$name" ]]; then
        cp -- "${REVIEWBENCH_GO_MODULE_DIR}/$name" "$snapshot/$name"
    fi
done

reviewbench_restore_candidate_modules() {
    local name
    for name in go.mod go.sum; do
        if [[ -f "${REVIEWBENCH_GO_CANDIDATE_MODULES}/$name" ]]; then
            cp -- "${REVIEWBENCH_GO_CANDIDATE_MODULES}/$name" "${REVIEWBENCH_GO_MODULE_DIR}/$name"
        else
            rm -f -- "${REVIEWBENCH_GO_MODULE_DIR}/$name"
        fi
    done
}
export -f reviewbench_restore_candidate_modules
trap 'rm -rf -- "$snapshot"' EXIT

"$@"
