#!/usr/bin/env bash
set -euo pipefail

coverage_out="$1"
fake_bin="$2"
chmod +x "$fake_bin/az"
PATH="$fake_bin:/usr/bin:/bin" \
  go test \
    -v \
    -run '^TestSessionAzureCLIToken$' \
    -count=1 \
    -coverpkg=github.com/hashicorp/go-azure-sdk/sdk/auth \
    -coverprofile="$coverage_out" \
    ./sdk/auth/sessionoracle
