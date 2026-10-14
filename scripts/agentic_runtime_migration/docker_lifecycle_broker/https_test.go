package main

import (
	"bytes"
	"crypto/tls"
	"io"
	"net"
	"testing"
	"time"
)

func TestOnlyOriginalHTTPSHostsAnd443AreAllowed(t *testing.T) {
	c := config{HTTPSHosts: originalHTTPSHosts}
	if err := c.validateHTTPSHosts(); err != nil {
		t.Fatal(err)
	}
	for _, host := range originalHTTPSHosts {
		if got, err := c.httpsAuthority(host + ":443"); err != nil || got != host {
			t.Fatal(got, err)
		}
	}
	for _, authority := range []string{"api.mfapi.in:80", "example.com:443", "127.0.0.1:443",
		"api.mfapi.in.:443", "api.mfapi.in:0443", "api.mfapi.in", "api.mfapi.in:443@127.0.0.1"} {
		if _, err := c.httpsAuthority(authority); err == nil {
			t.Fatal("non-original authority allowed", authority)
		}
	}
	if _, err := (config{}).httpsAuthority("api.mfapi.in:443"); err == nil {
		t.Fatal("old service-only profile gained HTTPS")
	}
	for _, hosts := range [][]string{{"api.mfapi.in"}, {"api.mfapi.in", "portal.amfiindia.com", "evil.invalid"},
		{"api.mfapi.in", "portal.amfiindia.com", "www.amfiindia.com", "www.amfiindia.com"}} {
		if (config{HTTPSHosts: hosts}).validateHTTPSHosts() == nil {
			t.Fatal("changed host list allowed", hosts)
		}
	}
}

func TestResolvedHTTPSAddressesExcludeLocalAndMetadataNetworks(t *testing.T) {
	for _, address := range []string{"127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.0.1",
		"169.254.169.254", "100.100.100.200", "0.0.0.0", "192.0.2.1", "198.18.0.1", "224.1.1.1",
		"::1", "fc00::1", "fe80::1", "2001:db8::1", "64:ff9b::a00:1", "::ffff:127.0.0.1"} {
		if publicHTTPSAddress(net.ParseIP(address)) {
			t.Fatal("non-public address allowed", address)
		}
	}
	for _, address := range []string{"1.1.1.1", "8.8.8.8", "2606:4700:4700::1111"} {
		if !publicHTTPSAddress(net.ParseIP(address)) {
			t.Fatal("public address lost", address)
		}
	}
}

func realHello(t *testing.T, name string) []byte {
	t.Helper()
	client, server := net.Pipe()
	defer server.Close()
	defer client.Close()
	done := make(chan error, 1)
	go func() {
		connection := tls.Client(client, &tls.Config{ServerName: name, MinVersion: tls.VersionTLS12})
		done <- connection.Handshake()
	}()
	server.SetReadDeadline(time.Now().Add(5 * time.Second))
	raw, actual, err := readClientHello(server)
	server.Close()
	<-done
	if err != nil || actual != name {
		t.Fatal(actual, err)
	}
	return raw
}

func TestRealTLSClientHelloIsInspectedWithoutChangingItsBytes(t *testing.T) {
	raw := realHello(t, "api.mfapi.in")
	out, name, err := readClientHello(bytes.NewReader(raw))
	if err != nil || name != "api.mfapi.in" || !bytes.Equal(raw, out) {
		t.Fatal("TLS bytes changed", name, err)
	}
	// Split the same handshake into two TLS records; no synthetic TLS server.
	first := append([]byte{22, 3, 1, 0, 9}, raw[5:14]...)
	remainder := raw[14:]
	second := append([]byte{22, 3, 1, byte(len(remainder) >> 8), byte(len(remainder))}, remainder...)
	fragmented := append(first, second...)
	out, name, err = readClientHello(bytes.NewReader(fragmented))
	if err != nil || name != "api.mfapi.in" || !bytes.Equal(out, fragmented) {
		t.Fatal(name, err)
	}
}

func TestMalformedOrMissingTLSNameIsRejected(t *testing.T) {
	raw := realHello(t, "api.mfapi.in")
	for _, value := range [][]byte{nil, []byte("GET / HTTP/1.1\r\n"), {23, 3, 3, 0, 1, 1},
		{22, 3, 3, 255, 255}, raw[:len(raw)-1], {22, 3, 3, 0, 4, 1, 255, 255, 255}} {
		if _, _, err := readClientHello(bytes.NewReader(value)); err == nil {
			t.Fatal("malformed TLS accepted")
		}
	}
	changed := bytes.Replace(raw, []byte("api.mfapi.in"), []byte("api\x00mfapi.in"), 1)
	if _, _, err := readClientHello(bytes.NewReader(changed)); err == nil {
		t.Fatal("invalid TLS name accepted")
	}
}

func TestHTTPSConnectionReservationsAndParentEndAreBounded(t *testing.T) {
	b := &broker{}
	var clients, peers []net.Conn
	defer func() {
		for _, c := range append(clients, peers...) {
			c.Close()
		}
	}()
	for _, parent := range []string{"one", "two"} {
		for i := 0; i < 4; i++ {
			client, peer := net.Pipe()
			clients, peers = append(clients, client), append(peers, peer)
			if !b.reserveHTTPS(parent, client) {
				t.Fatal("bounded reservation failed")
			}
		}
	}
	client, peer := net.Pipe()
	defer client.Close()
	defer peer.Close()
	if b.reserveHTTPS("three", client) || b.reserveHTTPS("one", client) {
		t.Fatal("HTTPS reservation exceeded")
	}
	b.closeParentHTTPS("one")
	peers[0].SetReadDeadline(time.Now().Add(time.Second))
	if _, err := peers[0].Read(make([]byte, 1)); err != io.EOF {
		t.Fatal("parent end did not close its tunnel", err)
	}
	if !b.reserveHTTPS("one", client) {
		t.Fatal("ended reservation not released")
	}
	b.releaseHTTPS("one", client)
}
