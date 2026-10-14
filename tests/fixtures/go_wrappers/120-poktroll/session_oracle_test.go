package proxy_test

import (
	"fmt"
	"testing"

	"cosmossdk.io/depinject"
	"github.com/pokt-network/poktroll/pkg/relayer/proxy"
)

func TestSessionRelayerProxyRejectsMissingChainDependencies(t *testing.T) {
	relayerProxy, err := proxy.NewRelayerProxy(depinject.Configs())
	if err == nil {
		t.Fatal("constructor accepted an empty dependency graph")
	}
	if relayerProxy != nil {
		t.Fatalf("constructor returned proxy %#v with unresolved dependencies",
			relayerProxy)
	}

	fmt.Println(
		`P2P_SIGNATURE={"missing_dependencies_rejected":true,"proxy_nil":true}`,
	)
}
