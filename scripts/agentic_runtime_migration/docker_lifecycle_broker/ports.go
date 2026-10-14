package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"sync"
	"time"
)

type runtimeContract struct {
	SchemaVersion          string `json:"schema_version"`
	Profile                string `json:"profile"`
	BrokerConfigSHA256     string `json:"broker_config_sha256"`
	BrokerExecutableSHA256 string `json:"broker_executable_sha256"`
	PreloadSHA256          string `json:"preload_sha256"`
}

func (b *broker) contractMatches(data []byte) bool {
	var c runtimeContract
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	return decoder.Decode(&c) == nil && decoder.Decode(new(any)) == io.EOF &&
		c.SchemaVersion == "reviewbench-real-docker-controller-contract/v1" &&
		c.Profile == b.cfg.Profile && c.BrokerConfigSHA256 == b.configSHA &&
		c.BrokerExecutableSHA256 == b.executableSHA &&
		c.PreloadSHA256 == b.cfg.PreloadSHA256
}

// Docker does not publish ports on an internal network. The broker supplies
// real bound gateway sockets, instead of dropping network isolation or
// fabricating readiness. Only NetworkSettings.Ports is augmented, from the
// actual listener address; IDs, processes, log streams and databases are real.
type portRelay struct {
	listener    net.Listener
	parent      *parent
	container   string
	target      string
	port        int
	mu          sync.Mutex
	connections map[net.Conn]bool
	closed      bool
	limit       chan struct{}
}

func startPortRelay(p *parent, container, target string,
	check func() bool, event func(string, map[string]any)) (*portRelay, error) {
	listener, err := net.Listen("tcp4", net.JoinHostPort(p.Gateway, "0"))
	if err != nil {
		return nil, err
	}
	f := &portRelay{listener: listener, parent: p, container: container, target: target,
		port: listener.Addr().(*net.TCPAddr).Port, connections: map[net.Conn]bool{},
		limit: make(chan struct{}, 64)}
	go func() {
		for {
			in, err := listener.Accept()
			if err != nil {
				return
			}
			ip, _, err := net.SplitHostPort(in.RemoteAddr().String())
			if err != nil || ip != p.IP {
				in.Close()
				continue
			}
			select {
			case f.limit <- struct{}{}:
			default:
				in.Close()
				continue
			}
			go func() {
				defer func() { <-f.limit }()
				defer in.Close()
				if !check() {
					return
				}
				out, err := net.DialTimeout("tcp4", target, 5*time.Second)
				if err != nil {
					return
				}
				defer out.Close()
				f.mu.Lock()
				if f.closed {
					f.mu.Unlock()
					return
				}
				f.connections[in], f.connections[out] = true, true
				f.mu.Unlock()
				defer func() {
					f.mu.Lock()
					delete(f.connections, in)
					delete(f.connections, out)
					f.mu.Unlock()
				}()
				done := make(chan int64, 1)
				go func() {
					n, _ := io.Copy(out, in)
					if c, ok := out.(*net.TCPConn); ok {
						_ = c.CloseWrite()
					}
					done <- n
				}()
				n, _ := io.Copy(in, out)
				if c, ok := in.(*net.TCPConn); ok {
					_ = c.CloseWrite()
				}
				sent := <-done
				event("tcp_forward_complete", map[string]any{
					"parent": p.ID, "container_id": container, "gateway": p.Gateway,
					"host_port": f.port, "target": target,
					"bytes_to_service": sent, "bytes_to_parent": n})
			}()
		}
	}()
	return f, nil
}

func (f *portRelay) close() {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.closed {
		return
	}
	f.closed = true
	f.listener.Close()
	for conn := range f.connections {
		conn.Close()
	}
}

func (b *broker) closeRelay(id string) {
	b.mu.Lock()
	f := b.relays[id]
	delete(b.relays, id)
	b.mu.Unlock()
	if f != nil {
		f.close()
		b.audit("tcp_listener_closed", map[string]any{"parent": f.parent.ID,
			"container_id": id, "host_port": f.port})
	}
}

func (b *broker) closeParentRelays(id string) {
	b.mu.Lock()
	var ids []string
	for cid, f := range b.relays {
		if f.parent.ID == id {
			ids = append(ids, cid)
		}
	}
	b.mu.Unlock()
	for _, cid := range ids {
		b.closeRelay(cid)
	}
}

func (b *broker) withActualPort(p *parent, raw []byte) ([]byte, error) {
	var d inspected
	if err := json.Unmarshal(raw, &d); err != nil {
		return nil, err
	}
	if d.Config.Labels[parentLabel] != p.ID || d.Config.Labels[profileLabel] != b.cfg.Profile {
		return nil, errors.New("container port observation belongs to another parent")
	}
	if !d.State.Running {
		return raw, nil
	}
	var spec imageSpec
	for _, candidate := range b.cfg.Images {
		if candidate.ID == d.Image && candidate.Role == d.Config.Labels[roleLabel] {
			spec = candidate
		}
	}
	if spec.ID == "" {
		return nil, errors.New("port observation uses an unbound image")
	}
	targetIP := ""
	for _, n := range d.NetworkSettings.Networks {
		if n.NetworkID == p.NetworkID {
			targetIP = n.IPAddress
		}
	}
	if net.ParseIP(targetIP) == nil || targetIP == p.IP || targetIP == p.Gateway {
		return nil, errors.New("service has no distinct address on its own internal network")
	}
	var value map[string]any
	_ = json.Unmarshal(raw, &value)
	network, _ := value["NetworkSettings"].(map[string]any)
	if network == nil {
		return nil, errors.New("real Docker network observation is missing")
	}
	ports, _ := network["Ports"].(map[string]any)
	if ports == nil {
		ports = map[string]any{}
		network["Ports"] = ports
	}
	key := strconv.Itoa(spec.Port) + "/tcp"
	if existing, ok := ports[key].([]any); ok && len(existing) > 0 {
		return raw, nil
	}
	target := net.JoinHostPort(targetIP, strconv.Itoa(spec.Port))
	b.mu.Lock()
	f := b.relays[d.ID]
	if f != nil && (f.target != target || f.parent.ID != p.ID) {
		b.mu.Unlock()
		return nil, errors.New("running service endpoint changed")
	}
	if f == nil {
		check := func() bool {
			var current inspected
			if b.get("/containers/"+d.ID+"/json", &current) != nil || !current.State.Running ||
				current.Config.Labels[parentLabel] != p.ID ||
				current.Config.Labels[profileLabel] != b.cfg.Profile ||
				current.Config.Labels[leaseLabel] != p.Lease {
				return false
			}
			for _, n := range current.NetworkSettings.Networks {
				if n.NetworkID == p.NetworkID && n.IPAddress == targetIP {
					return true
				}
			}
			return false
		}
		var err error
		f, err = startPortRelay(p, d.ID, target, check, b.audit)
		if err != nil {
			b.mu.Unlock()
			return nil, err
		}
		b.relays[d.ID] = f
		b.audit("tcp_listener_bound", map[string]any{"parent": p.ID, "container_id": d.ID,
			"gateway": p.Gateway, "host_port": f.port, "target": target,
			"docker_port_binding_absent": true, "actual_bound_listener": true})
	}
	b.mu.Unlock()
	ports[key] = []map[string]string{{"HostIp": p.Gateway, "HostPort": strconv.Itoa(f.port)}}
	body, err := json.Marshal(value)
	hash := sha256.Sum256(raw)
	b.audit("inspect_transport_mapping", map[string]any{"parent": p.ID, "container_id": d.ID,
		"raw_docker_reply_sha256": hex.EncodeToString(hash[:]), "changed_fields": []string{"NetworkSettings.Ports"},
		"gateway": p.Gateway, "host_port": f.port, "target": target, "bound_socket_derived": true})
	return body, err
}

func (b *broker) sweepRelaysAndNetworks() {
	b.mu.Lock()
	var relays []*portRelay
	var pending []*parent
	for _, f := range b.relays {
		relays = append(relays, f)
	}
	for _, p := range b.pendingNetworks {
		pending = append(pending, p)
	}
	b.mu.Unlock()
	for _, f := range relays {
		status, raw, err := b.docker("GET", "/containers/"+f.container+"/json", nil)
		var d inspected
		if err == nil && (status == 404 || (status == 200 && json.Unmarshal(raw, &d) == nil && !d.State.Running)) {
			b.closeRelay(f.container)
		}
	}
	for _, p := range pending {
		status, raw, err := b.docker("GET", "/networks/"+p.NetworkID, nil)
		if err != nil {
			continue
		}
		var network struct {
			ID, Name   string
			Internal   bool
			Containers map[string]any
		}
		if status == 200 && json.Unmarshal(raw, &network) == nil && network.Internal &&
			network.ID == p.NetworkID && len(network.Containers) == 0 {
			status, _, err = b.docker("DELETE", "/networks/"+p.NetworkID, nil)
		}
		if err == nil && (status == 204 || status == 404) {
			b.mu.Lock()
			delete(b.pendingNetworks, p.ID)
			b.mu.Unlock()
			_ = os.Remove(filepath.Join(b.cfg.StateDir, p.ID+".json"))
			b.audit("owned_internal_network_removed", map[string]any{
				"parent": p.ID, "network": p.NetworkID, "status": status, "error": fmt.Sprint(err)})
		}
	}
}
