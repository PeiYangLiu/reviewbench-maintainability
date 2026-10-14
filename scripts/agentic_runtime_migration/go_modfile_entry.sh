#!/usr/bin/env bash
# Trusted transport for dependency-only module edits. No test or solution code.
set -euo pipefail
: "${REVIEWBENCH_REAL_GO:?pinned real Go required}"
: "${REVIEWBENCH_GO_MODULE_DIR:?current candidate module required}"
: "${REVIEWBENCH_GO_SHADOW_DIR:?private module metadata directory required}"
: "${REVIEWBENCH_GO_SHADOW_EDITS:?pinned module-edit arguments required}"
: "${SOURCE_ROOT:?current candidate root required}"

# An agent's explicit manifest-editing command is an intentional source edit.
# Only the retained bootstrap/Oracle transport sets PROTECTED_MODULES=1.
if [[ "${REVIEWBENCH_GO_PROTECTED_MODULES:-0}" != 1 ]]; then
    if [[ "${1:-}" == get || "${1:-}" == work ||
          ( "${1:-}" == mod && "${2:-}" =~ ^(edit|tidy|init|vendor)$ ) ]]; then
        exec "$REVIEWBENCH_REAL_GO" "$@"
    fi
fi
# Explicit alternate files belong to the caller; never silently replace them.
for argument in "$@"; do
    if [[ "$argument" == -modfile || "$argument" == -modfile=* ]]; then
        exec "$REVIEWBENCH_REAL_GO" "$@"
    fi
done
if [[ " ${GOFLAGS:-} " == *" -modfile"* ]]; then
    echo 'GO_MODULE_SHADOW_ERROR conflicting GOFLAGS -modfile' >&2
    exit 2
fi

source_root="$(readlink -f -- "$SOURCE_ROOT")"
module_root="$(readlink -f -- "$REVIEWBENCH_GO_MODULE_DIR")"
shadow="$REVIEWBENCH_GO_SHADOW_DIR"
[[ "$module_root" == "$source_root" || "$module_root" == "$source_root/"* ]]
[[ "$shadow" == "$source_root/.reviewbench/go-module-shadow" ]]
[[ "$shadow" != *[[:space:]]* ]]
[[ -f "$module_root/go.mod" && ! -L "$module_root/go.mod" ]]
[[ ! -L "$module_root/go.sum" ]]
[[ ! -e "$module_root/go.sum" || -f "$module_root/go.sum" ]]
[[ -f "$REVIEWBENCH_GO_SHADOW_EDITS" && ! -L "$REVIEWBENCH_GO_SHADOW_EDITS" ]]
[[ ! -L "$source_root/.reviewbench" && ! -L "$shadow" ]]
mkdir -p -- "$shadow"
for name in go.mod go.sum basis.sha256 refresh.lock; do
    [[ ! -L "$shadow/$name" ]]
done
exec 9>"$shadow/refresh.lock"
flock -x 9
basis="$(
    {
        sha256sum -- "$module_root/go.mod" "$REVIEWBENCH_GO_SHADOW_EDITS"
        if [[ -f "$module_root/go.sum" ]]; then
            sha256sum -- "$module_root/go.sum"
        else
            printf '%s\n' 'go.sum:absent'
        fi
    } | sha256sum | cut -d ' ' -f 1
)"
if [[ ! -f "$shadow/go.mod" || ! -f "$shadow/basis.sha256" ||
      "$(<"$shadow/basis.sha256")" != "$basis" ]]; then
    temporary="$(mktemp -d "$shadow/.refresh.XXXXXXXX")"
    trap 'rm -rf -- "$temporary"' EXIT
    cp -- "$module_root/go.mod" "$temporary/go.mod"
    if [[ -f "$module_root/go.sum" ]]; then
        cp -- "$module_root/go.sum" "$temporary/go.sum"
    fi
    mapfile -t edits < "$REVIEWBENCH_GO_SHADOW_EDITS"
    [[ "${#edits[@]}" -gt 0 ]]
    for argument in "${edits[@]}"; do
        [[ "$argument" == -replace=* || "$argument" == -require=* ]]
    done
    (
        cd -- "$module_root"
        "$REVIEWBENCH_REAL_GO" mod edit -modfile="$temporary/go.mod" "${edits[@]}"
    )
    mv -- "$temporary/go.mod" "$shadow/go.mod"
    if [[ -f "$temporary/go.sum" ]]; then
        mv -- "$temporary/go.sum" "$shadow/go.sum"
    else
        rm -f -- "$shadow/go.sum"
    fi
    printf '%s\n' "$basis" > "$temporary/basis.sha256"
    mv -- "$temporary/basis.sha256" "$shadow/basis.sha256"
    rm -rf -- "$temporary"
    trap - EXIT
fi
# Do not hold the refresh lock across the Go command: tests may start another Go.
flock -u 9
exec 9>&-
export GOFLAGS="${GOFLAGS:+$GOFLAGS }-modfile=$shadow/go.mod"
exec "$REVIEWBENCH_REAL_GO" "$@"
