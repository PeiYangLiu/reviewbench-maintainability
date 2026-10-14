package main

import (
	"encoding/json"
	"io"
	"net"
	"os"
	"reflect"
	"strconv"
	"strings"
	"testing"
	"time"
)

func TestRuntimeContractBindsActualExecutableConfigurationAndPreload(t *testing.T) {
	b, _ := fixture()
	b.configSHA, b.executableSHA, b.cfg.PreloadSHA256 = strings.Repeat("a", 64), strings.Repeat("b", 64), strings.Repeat("c", 64)
	c := runtimeContract{
		SchemaVersion: "reviewbench-real-docker-controller-contract/v1",
		Profile:       b.cfg.Profile, BrokerConfigSHA256: b.configSHA,
		BrokerExecutableSHA256: b.executableSHA, PreloadSHA256: b.cfg.PreloadSHA256,
	}
	raw, _ := json.Marshal(c)
	if !b.contractMatches(raw) {
		t.Fatal("bound contract rejected")
	}
	var value map[string]any
	_ = json.Unmarshal(raw, &value)
	for _, field := range []string{"schema_version", "profile", "broker_config_sha256",
		"broker_executable_sha256", "preload_sha256"} {
		copy := map[string]any{}
		for k, v := range value {
			copy[k] = v
		}
		copy[field] = "different"
		data, _ := json.Marshal(copy)
		if b.contractMatches(data) {
			t.Fatal("changed contract accepted", field)
		}
	}
	value["unexpected"] = true
	extra, _ := json.Marshal(value)
	if b.contractMatches(extra) || b.contractMatches(append(raw, raw...)) {
		t.Fatal("expanded/concatenated contract accepted")
	}
}

func echoSocket(t *testing.T) net.Listener {
	t.Helper()
	listener, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	go func() {
		for {
			c, err := listener.Accept()
			if err != nil {
				return
			}
			go func() { defer c.Close(); _, _ = io.Copy(c, c) }()
		}
	}()
	return listener
}

func TestGatewayRelayForwardsActualBytesAndCloses(t *testing.T) {
	server := echoSocket(t)
	defer server.Close()
	_, p := fixture()
	p.Gateway, p.IP = "127.0.0.1", "127.0.0.1"
	events := make(chan map[string]any, 2)
	f, err := startPortRelay(p, strings.Repeat("d", 64), server.Addr().String(),
		func() bool { return true }, func(_ string, value map[string]any) { events <- value })
	if err != nil {
		t.Fatal(err)
	}
	defer f.close()
	address := net.JoinHostPort(p.Gateway, strconv.Itoa(f.port))
	conn, err := net.DialTimeout("tcp4", address, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	_ = conn.SetDeadline(time.Now().Add(2 * time.Second))
	message := "actual gateway transport bytes\n"
	_, _ = io.WriteString(conn, message)
	_ = conn.(*net.TCPConn).CloseWrite()
	got, err := io.ReadAll(conn)
	conn.Close()
	if err != nil || string(got) != message {
		t.Fatal(string(got), err)
	}
	select {
	case event := <-events:
		if event["bytes_to_service"] != int64(len(message)) ||
			event["bytes_to_parent"] != int64(len(message)) {
			t.Fatal(event)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("actual byte counters missing")
	}
	f.close()
	if conn, err := net.DialTimeout("tcp4", address, time.Second); err == nil {
		conn.Close()
		t.Fatal("closed owner port remained reachable")
	}
}

func TestGatewayRelayRejectsOtherParentAddress(t *testing.T) {
	server := echoSocket(t)
	defer server.Close()
	_, p := fixture()
	p.Gateway, p.IP = "127.0.0.1", "127.0.0.2"
	f, err := startPortRelay(p, strings.Repeat("d", 64), server.Addr().String(),
		func() bool { return true }, func(string, map[string]any) {})
	if err != nil {
		t.Fatal(err)
	}
	defer f.close()
	conn, err := net.DialTimeout("tcp4", net.JoinHostPort(p.Gateway, strconv.Itoa(f.port)), time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(time.Second))
	_, _ = io.WriteString(conn, "other parent")
	got, _ := io.ReadAll(conn)
	if len(got) != 0 {
		t.Fatal("foreign peer reached upstream", string(got))
	}
}

func TestInspectionChangesOnlyMappingDerivedFromBoundSocket(t *testing.T) {
	b, p := fixture()
	p.Gateway, p.IP = "127.0.0.1", "127.0.0.3"
	b.relays = map[string]*portRelay{}
	f, err := os.CreateTemp(t.TempDir(), "audit-")
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	b.auditFile = f
	id := strings.Repeat("d", 64)
	original := map[string]any{
		"Id": id, "Image": b.cfg.Images["postgres:14-alpine"].ID,
		"State": map[string]any{"Running": true, "ExitCode": 0},
		"Config": map[string]any{"Labels": map[string]string{
			parentLabel: p.ID, profileLabel: b.cfg.Profile, roleLabel: "postgres", leaseLabel: p.Lease}},
		"NetworkSettings": map[string]any{
			"Ports":    map[string]any{"5432/tcp": nil},
			"Networks": map[string]any{"own": map[string]any{"NetworkID": p.NetworkID, "IPAddress": "127.0.0.2"}}},
	}
	raw, _ := json.Marshal(original)
	body, err := b.withActualPort(p, raw)
	if err != nil {
		t.Fatal(err)
	}
	defer b.closeRelay(id)
	var observed, before map[string]any
	_ = json.Unmarshal(body, &observed)
	_ = json.Unmarshal(raw, &before)
	network := observed["NetworkSettings"].(map[string]any)
	mapping := network["Ports"].(map[string]any)["5432/tcp"].([]any)[0].(map[string]any)
	bound := b.relays[id].listener.Addr().(*net.TCPAddr).Port
	if mapping["HostPort"] != strconv.Itoa(bound) || mapping["HostIp"] != p.Gateway {
		t.Fatal("port was not derived from actual listener", mapping, bound)
	}
	network["Ports"] = before["NetworkSettings"].(map[string]any)["Ports"]
	if !reflect.DeepEqual(observed, before) {
		t.Fatal("container identity/process/other fields changed")
	}
}
