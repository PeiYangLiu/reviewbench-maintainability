package main

import (
	"bufio"
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"sort"
	"strings"
	"time"
)

var originalHTTPSHosts = []string{"api.mfapi.in", "portal.amfiindia.com", "www.amfiindia.com"}

func (c config) validateHTTPSHosts() error {
	if len(c.HTTPSHosts) == 0 {
		return nil // The already-deployed service-only profile remains valid.
	}
	values := append([]string(nil), c.HTTPSHosts...)
	sort.Strings(values)
	if strings.Join(values, ",") != strings.Join(originalHTTPSHosts, ",") {
		return errors.New("HTTPS destinations differ from original application hosts")
	}
	return nil
}

func (c config) httpsAuthority(authority string) (string, error) {
	host, port, err := net.SplitHostPort(authority)
	if err != nil || port != "443" || authority != host+":443" {
		return "", errors.New("HTTPS requires a canonical original host and port443")
	}
	for _, allowed := range c.HTTPSHosts {
		if host == allowed {
			return host, nil
		}
	}
	return "", errors.New("HTTPS destination is not an original application host")
}

func publicHTTPSAddress(ip net.IP) bool {
	if ip == nil || !ip.IsGlobalUnicast() || ip.IsPrivate() || ip.IsLoopback() ||
		ip.IsLinkLocalUnicast() || ip.IsLinkLocalMulticast() {
		return false
	}
	for _, text := range []string{
		"0.0.0.0/8", "100.64.0.0/10", "192.0.0.0/24", "192.0.2.0/24",
		"198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "240.0.0.0/4", "2001:db8::/32",
		"64:ff9b::/96", "64:ff9b:1::/48",
	} {
		_, subnet, _ := net.ParseCIDR(text)
		if subnet.Contains(ip) {
			return false
		}
	}
	return true
}

// clientHelloName parses only a bounded ClientHello. No TLS session is
// terminated or decrypted; the original JVM validates the actual origin cert.
func clientHelloName(body []byte) (string, error) {
	take := func(n int) ([]byte, bool) {
		if n < 0 || len(body) < n {
			return nil, false
		}
		value := body[:n]
		body = body[n:]
		return value, true
	}
	length8 := func() (int, bool) {
		b, ok := take(1)
		if !ok {
			return 0, false
		}
		return int(b[0]), true
	}
	length16 := func() (int, bool) {
		b, ok := take(2)
		if !ok {
			return 0, false
		}
		return int(binary.BigEndian.Uint16(b)), true
	}
	fail := errors.New("invalid bounded TLS ClientHello")
	if _, ok := take(34); !ok {
		return "", fail
	}
	n, ok := length8()
	if !ok {
		return "", fail
	}
	if _, ok = take(n); !ok {
		return "", fail
	}
	n, ok = length16()
	if !ok || n < 2 || n%2 != 0 {
		return "", fail
	}
	if _, ok = take(n); !ok {
		return "", fail
	}
	n, ok = length8()
	if !ok || n == 0 {
		return "", fail
	}
	if _, ok = take(n); !ok {
		return "", fail
	}
	n, ok = length16()
	if !ok || n != len(body) {
		return "", fail
	}
	name := ""
	for len(body) > 0 {
		kind, ok := length16()
		if !ok {
			return "", fail
		}
		size, ok := length16()
		if !ok {
			return "", fail
		}
		extension, ok := take(size)
		if !ok {
			return "", fail
		}
		if kind != 0 {
			continue
		}
		if name != "" || len(extension) < 5 ||
			int(binary.BigEndian.Uint16(extension)) != len(extension)-2 ||
			extension[2] != 0 || int(binary.BigEndian.Uint16(extension[3:])) != len(extension)-5 {
			return "", fail
		}
		name = string(extension[5:])
		if name == "" || strings.ContainsAny(name, "\x00/\\:@ ") {
			return "", fail
		}
	}
	if name == "" {
		return "", errors.New("original HTTPS requires visible matching TLS SNI")
	}
	return name, nil
}

func readClientHello(reader io.Reader) ([]byte, string, error) {
	var wire, handshake []byte
	for len(wire) < 64<<10 {
		header := make([]byte, 5)
		if _, err := io.ReadFull(reader, header); err != nil {
			return nil, "", err
		}
		size := int(binary.BigEndian.Uint16(header[3:]))
		if header[0] != 22 || header[1] != 3 || header[2] > 3 || size == 0 || size > 16<<10 {
			return nil, "", errors.New("HTTPS tunnel must start with a bounded TLS handshake")
		}
		block := make([]byte, size)
		if _, err := io.ReadFull(reader, block); err != nil {
			return nil, "", err
		}
		wire = append(wire, header...)
		wire = append(wire, block...)
		handshake = append(handshake, block...)
		if len(handshake) < 4 {
			continue
		}
		length := int(handshake[1])<<16 | int(handshake[2])<<8 | int(handshake[3])
		if handshake[0] != 1 || length > 60<<10 {
			return nil, "", errors.New("TLS ClientHello exceeds its bound")
		}
		if len(handshake) >= length+4 {
			name, err := clientHelloName(handshake[4 : 4+length])
			return wire, name, err
		}
	}
	return nil, "", errors.New("TLS ClientHello exceeds its wire bound")
}

func (b *broker) reserveHTTPS(parentID string, connection net.Conn) bool {
	b.httpsMu.Lock()
	defer b.httpsMu.Unlock()
	if b.httpsClients == nil {
		b.httpsClients = map[string]map[net.Conn]bool{}
	}
	total := 0
	for _, connections := range b.httpsClients {
		total += len(connections)
	}
	if total >= 8 || len(b.httpsClients[parentID]) >= 4 {
		return false
	}
	if b.httpsClients[parentID] == nil {
		b.httpsClients[parentID] = map[net.Conn]bool{}
	}
	b.httpsClients[parentID][connection] = true
	return true
}

func (b *broker) releaseHTTPS(parentID string, connection net.Conn) {
	b.httpsMu.Lock()
	defer b.httpsMu.Unlock()
	delete(b.httpsClients[parentID], connection)
	if len(b.httpsClients[parentID]) == 0 {
		delete(b.httpsClients, parentID)
	}
}

func (b *broker) closeParentHTTPS(parentID string) {
	b.httpsMu.Lock()
	defer b.httpsMu.Unlock()
	for connection := range b.httpsClients[parentID] {
		_ = connection.Close()
	}
	delete(b.httpsClients, parentID)
}

func (b *broker) serveHTTPS(w http.ResponseWriter, r *http.Request, p *parent) {
	host, err := b.cfg.httpsAuthority(r.Host)
	if err != nil || r.URL.Host != r.Host || r.ContentLength > 0 {
		b.audit("https_denied", map[string]any{"parent": p.ID, "authority": r.Host})
		deny(w, "HTTPS destination/request differs from original application transport")
		return
	}
	hijacker, ok := w.(http.Hijacker)
	if !ok {
		http.Error(w, "HTTPS transport unavailable", http.StatusBadGateway)
		return
	}
	client, buffered, err := hijacker.Hijack()
	if err != nil {
		return
	}
	defer client.Close()
	if !b.reserveHTTPS(p.ID, client) {
		_, _ = io.WriteString(client, "HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n")
		return
	}
	defer b.releaseHTTPS(p.ID, client)
	_ = client.SetDeadline(time.Now().Add(10 * time.Second))
	if _, err = buffered.WriteString("HTTP/1.1 200 Connection Established\r\n\r\n"); err != nil ||
		buffered.Flush() != nil {
		return
	}
	hello, name, err := readClientHello(buffered.Reader)
	if err != nil || name != host {
		b.audit("https_tls_rejected", map[string]any{"parent": p.ID, "authority": host,
			"sni": name, "error": fmt.Sprint(err)})
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	addresses, err := net.DefaultResolver.LookupIPAddr(ctx, host)
	if err != nil {
		b.audit("https_dns_error", map[string]any{"parent": p.ID, "authority": host, "error": err.Error()})
		return
	}
	var upstream net.Conn
	for _, address := range addresses {
		if !publicHTTPSAddress(address.IP) {
			continue
		}
		upstream, err = (&net.Dialer{Timeout: 5 * time.Second}).DialContext(
			ctx, "tcp", net.JoinHostPort(address.IP.String(), "443"))
		if err == nil {
			break
		}
	}
	if upstream == nil {
		b.audit("https_connect_error", map[string]any{"parent": p.ID, "authority": host, "error": fmt.Sprint(err)})
		return
	}
	defer upstream.Close()
	deadline := time.Now().Add(120 * time.Second)
	_ = client.SetDeadline(deadline)
	_ = upstream.SetDeadline(deadline)
	if _, err = upstream.Write(hello); err != nil {
		return
	}
	b.audit("https_original_tls_connected", map[string]any{
		"parent": p.ID, "lease": p.Lease, "authority": host, "sni": name,
		"remote": upstream.RemoteAddr().String(), "tls_terminated": false, "response_fabrication": false})
	done := make(chan int64, 1)
	go func() {
		count, _ := io.Copy(upstream, io.LimitReader(bufio.NewReader(buffered.Reader), 2<<20))
		_ = upstream.Close()
		done <- count
	}()
	received, _ := io.Copy(client, io.LimitReader(upstream, 64<<20))
	_ = client.Close()
	sent := <-done
	b.audit("https_original_tls_closed", map[string]any{
		"parent": p.ID, "authority": host, "bytes_to_origin": sent + int64(len(hello)),
		"bytes_to_parent": received, "bounded_seconds": 120})
}
