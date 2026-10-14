#!/usr/bin/env bash
set -euo pipefail

coverage_out="$1"
go mod edit -require=google.golang.org/genproto@v0.0.0-20220725144611-272f38e5d71b
go mod download all
exec go test \
  -v \
  -run '^TestSessionGarbageCollectionPolicyBehavior$' \
  -count=1 \
  -coverpkg=cloud.google.com/go/bigtable \
  -coverprofile="$coverage_out" \
  ./sessionoracle
