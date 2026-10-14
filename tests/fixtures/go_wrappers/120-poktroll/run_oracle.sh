#!/usr/bin/env bash

set -euo pipefail

task_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="${task_dir}/bin:${PATH}"

if [[ ! -f x/application/types/query.pb.go ]]; then
	ignite generate proto-go --yes
	git checkout -- go.mod go.sum
fi

if [[ ! -f testutil/mockclient/block_client_mock.go ||
	! -f testutil/mockrelayer/account/query_client.go ]]; then
	go generate ./pkg/client/interface.go
	go generate ./pkg/relayer/interface.go
fi

trap 'rm -f pkg/relayer/proxy/debug_container.dot' EXIT

go test \
	-v \
	-run '^TestSessionRelayerProxyRejectsMissingChainDependencies$' \
	-count=1 \
	-coverpkg=github.com/pokt-network/poktroll/pkg/relayer/proxy \
	-coverprofile="${COVERAGE_OUT}" \
	./pkg/relayer/proxy
