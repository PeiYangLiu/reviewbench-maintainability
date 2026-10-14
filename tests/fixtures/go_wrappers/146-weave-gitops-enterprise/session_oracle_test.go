package sessionoracle

import (
	"bytes"
	"fmt"
	"strings"
	"testing"

	bootstrapcmd "github.com/weaveworks/weave-gitops-enterprise/cmd/gitops/app/bootstrap"
	"github.com/weaveworks/weave-gitops/cmd/gitops/config"
)

func TestSessionBootstrapCommandHelp(t *testing.T) {
	command := bootstrapcmd.Command(&config.Options{})
	var output bytes.Buffer
	command.SetOut(&output)
	command.SetErr(&output)
	command.SetArgs([]string{"--help"})

	if err := command.Execute(); err != nil {
		t.Fatalf("execute help: %v", err)
	}
	if command.Use != "bootstrap" {
		t.Fatalf("command use = %q, want bootstrap", command.Use)
	}
	help := output.String()
	if !strings.Contains(help, "Usage:") || !strings.Contains(help, "bootstrap") {
		t.Fatalf("unexpected help output: %q", help)
	}

	fmt.Println(`P2P_SIGNATURE={"command":"bootstrap","help_contains_bootstrap":true,"help_succeeded":true}`)
}
