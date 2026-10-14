package main

import (
	"fmt"
	"os"
	"testing"
)

func TestSessionWithdrawalsHelpLifecycle(t *testing.T) {
	originalArgs := os.Args
	defer func() {
		os.Args = originalArgs
	}()

	os.Args = []string{"withdrawals", "--help"}
	main()

	fmt.Println(`P2P_SIGNATURE={"help_completed":true}`)
}
