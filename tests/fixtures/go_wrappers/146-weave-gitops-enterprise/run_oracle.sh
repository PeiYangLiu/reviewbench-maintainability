#!/usr/bin/env bash
set -euo pipefail

coverage_out="$1"
stub_root="$2"
go mod edit -replace="github.com/weaveworks/weave-gitops-enterprise-credentials=$stub_root"
exec go test \
  -v \
  -run '^TestSessionBootstrapCommandHelp$' \
  -count=1 \
  -coverpkg=github.com/weaveworks/weave-gitops-enterprise/cmd/gitops/app/bootstrap \
  -coverprofile="$coverage_out" \
  ./cmd/gitops/app/bootstrap/sessionoracle
