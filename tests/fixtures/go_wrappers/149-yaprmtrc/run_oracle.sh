#!/usr/bin/env bash
set -euo pipefail

coverage_out="$1"
go_bin="/home/v-peiyangliu/go/pkg/mod/golang.org/toolchain@v0.0.1-go1.24.10.linux-amd64/bin/go"
binary="${coverage_out}.agent"
coverage_dir="${coverage_out}.covdata"

rm -rf "$coverage_dir"
mkdir -p "$coverage_dir"
"$go_bin" build -cover -o "$binary" ./cmd/agent

set +e
output="$(GOCOVERDIR="$coverage_dir" "$binary" unexpected 2>&1)"
status=$?
set -e

if [[ $status -eq 0 ]]; then
  echo "agent accepted an unexpected positional argument" >&2
  exit 1
fi
if [[ "$output" != *"unknown flags: [unexpected]"* ]]; then
  echo "unexpected diagnostic: $output" >&2
  exit 1
fi

"$go_bin" tool covdata textfmt -i="$coverage_dir" -o="$coverage_out"
echo 'P2P_SIGNATURE={"exit_status":"nonzero","rejected_arguments":["unexpected"]}'
