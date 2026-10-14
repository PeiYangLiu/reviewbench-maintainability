// Run an original automatic Go package command and report its fixed named P2P
// set. Only the event transport (-json) is added; package/flags stay unchanged.
// Original trackA_go/evaluate_e2e selected named results even when a non-P2P
// sibling failed. A missing, skipped, failed, or incomplete selected test fails.
package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
)

type config struct {
	Command  []string `json:"command"`
	Expected []string `json:"expected"`
	Report   string   `json:"report"`
	Log      string   `json:"log"`
}

type event struct {
	Action  string
	Package string
	Test    string
}

type testResult struct {
	ID     string `json:"id"`
	Status string `json:"status"`
}

type report struct {
	Schema string       `json:"schema_version"`
	Tests  []testResult `json:"tests"`
}

func run() int {
	if len(os.Args) != 2 {
		fmt.Fprintln(os.Stderr, "usage: go-public-test-report CONFIG")
		return 2
	}
	data, err := os.ReadFile(os.Args[1])
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 2
	}
	var cfg config
	if err = json.Unmarshal(data, &cfg); err != nil || len(cfg.Command) < 2 ||
		cfg.Command[1] != "test" || len(cfg.Expected) == 0 || cfg.Report == "" || cfg.Log == "" {
		fmt.Fprintln(os.Stderr, "invalid fixed public Go contract")
		return 2
	}
	want := map[string]bool{}
	for _, name := range cfg.Expected {
		if name == "" || want[name] {
			fmt.Fprintln(os.Stderr, "empty or duplicate fixed Go test identity")
			return 2
		}
		want[name] = true
	}
	if err = os.MkdirAll(filepath.Dir(cfg.Report), 0755); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 2
	}
	// Remove a stale report before invoking a test process.
	if err = os.Remove(cfg.Report); err != nil && !os.IsNotExist(err) {
		fmt.Fprintln(os.Stderr, err)
		return 2
	}
	log, err := os.Create(cfg.Log)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 2
	}
	defer log.Close()
	args := append([]string{"test", "-json"}, cfg.Command[2:]...)
	cmd := exec.Command(cfg.Command[0], args...)
	cmd.Stderr = os.Stderr
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 2
	}
	if err = cmd.Start(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 2
	}
	found, complete := map[string]bool{}, map[string]bool{}
	packages := map[string]string{}
	scanner := bufio.NewScanner(stdout)
	scanner.Buffer(make([]byte, 64*1024), 4*1024*1024)
	for scanner.Scan() {
		line := scanner.Bytes()
		if _, err = log.Write(append(append([]byte(nil), line...), '\n')); err != nil {
			break
		}
		var e event
		if json.Unmarshal(line, &e) != nil {
			continue
		}
		if e.Test == "" && (e.Action == "pass" || e.Action == "fail") {
			complete[e.Package] = true
		}
		if e.Test != "" && (e.Action == "pass" || e.Action == "fail" || e.Action == "skip") {
			name := e.Package + "::" + e.Test
			old, exists := found[name]
			found[name] = e.Action == "pass" && (!exists || old)
			packages[name] = e.Package
		}
	}
	readErr := scanner.Err()
	if readErr != nil || err != nil {
		_ = cmd.Process.Kill()
	}
	commandErr := cmd.Wait()
	if readErr != nil || err != nil || (commandErr != nil && cmd.ProcessState.ExitCode() < 0) {
		fmt.Fprintln(os.Stderr, "incomplete Go event stream", readErr, err, commandErr)
		return 2
	}
	result := report{Schema: "reviewbench-exec-test-results/v1"}
	code := 0
	for _, name := range cfg.Expected {
		status := "failed"
		if found[name] && complete[packages[name]] {
			status = "passed"
		} else {
			code = 1
		}
		result.Tests = append(result.Tests, testResult{ID: name, Status: status})
	}
	data, err = json.Marshal(result)
	if err == nil {
		err = os.WriteFile(cfg.Report+".tmp", append(data, '\n'), 0644)
	}
	if err == nil {
		err = os.Rename(cfg.Report+".tmp", cfg.Report)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 2
	}
	fmt.Fprintf(os.Stderr, "fixed Go public tests=%d result=%d command=%v\n", len(cfg.Expected), code, commandErr)
	return code
}

func main() { os.Exit(run()) }
