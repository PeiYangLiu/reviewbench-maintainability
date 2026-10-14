// A lease-bound transport to the real Docker daemon, not a container emulator.
// Original Testcontainers and Ryuk run unchanged. Each Ryuk receives a separate,
// parent-scoped socket; neither the candidate nor Ryuk receives the host socket.
package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

const parentLabel = "reviewbench.docker-parent"
const profileLabel = "reviewbench.docker-profile"
const roleLabel = "reviewbench.docker-role"
const leaseLabel = "reviewbench.resource-lease"
const maxBody = 1 << 20

var digestRE = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
var idRE = regexp.MustCompile(`^[0-9a-f]{64}$`)
var versionRE = regexp.MustCompile(`^/v[0-9]+\.[0-9]+/`)
var userRE = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]{0,62}$`)

type imageSpec struct {
	ID   string `json:"id"`
	Role string `json:"role"`
	Port int    `json:"port"`
}

type config struct {
	Client         string               `json:"client,omitempty"`
	HTTPSHosts     []string             `json:"https_hosts,omitempty"`
	Listen         string               `json:"listen"`
	DockerSocket   string               `json:"docker_socket"`
	StateDir       string               `json:"state_dir"`
	AuditPath      string               `json:"audit_path"`
	Reservations   string               `json:"reservations"`
	Profile        string               `json:"profile"`
	DeveloperImage string               `json:"developer_image"`
	PreloadTarget  string               `json:"preload_target"`
	PreloadSHA256  string               `json:"preload_sha256"`
	ContractTarget string               `json:"contract_target"`
	MaxParents     int                  `json:"max_parents"`
	Images         map[string]imageSpec `json:"images"`
}

func (c config) validate() error {
	if c.Profile == "" || !digestRE.MatchString(c.DeveloperImage) ||
		c.MaxParents < 1 || c.MaxParents > 2 ||
		(c.Client != "" && c.Client != javaClientProfile) ||
		(c.Client == "" && len(c.Images) != 2) ||
		(c.Client == javaClientProfile && len(c.Images) != 4) {
		return errors.New("invalid bounded Docker profile")
	}
	if _, _, e := net.SplitHostPort(c.Listen); e != nil {
		return e
	}
	for _, p := range []string{c.DockerSocket, c.StateDir, c.AuditPath, c.Reservations} {
		if !filepath.IsAbs(p) || filepath.Clean(p) != p {
			return errors.New("broker paths must be absolute canonical paths")
		}
	}
	if !strings.HasPrefix(c.PreloadTarget, "/opt/reviewbench-assets/") ||
		c.ContractTarget != "/opt/reviewbench-assets/docker-lifecycle-contract.json" ||
		len(c.PreloadSHA256) != 64 || strings.Contains(c.PreloadTarget, "..") {
		return errors.New("preload asset binding missing")
	}
	if c.Client == javaClientProfile {
		if err := c.validateHTTPSHosts(); err != nil {
			return err
		}
		return c.validateJavaImages()
	}
	if len(c.HTTPSHosts) != 0 {
		return errors.New("HTTPS transport requires the original Java profile")
	}
	roles := map[string]bool{}
	for alias, image := range c.Images {
		if alias == "" || !digestRE.MatchString(image.ID) ||
			(image.Role != "postgres" && image.Role != "ryuk") ||
			(image.Role == "postgres" && image.Port != 5432) ||
			(image.Role == "ryuk" && image.Port != 8080) || roles[image.Role] {
			return errors.New("invalid or duplicate pinned service image")
		}
		roles[image.Role] = true
	}
	return nil
}

type parent struct {
	ID        string `json:"id"`
	IP        string `json:"ip"`
	NetworkID string `json:"network_id"`
	Gateway   string `json:"gateway"`
	Identity  string `json:"identity"`
	Lease     string `json:"lease"`
	Socket    string `json:"socket"`
}

type broker struct {
	cfg             config
	client          *http.Client
	transport       *http.Transport
	mu              sync.Mutex
	createMu        sync.Mutex
	parents         map[string]*parent
	sockets         map[string]net.Listener
	relays          map[string]*portRelay
	pendingNetworks map[string]*parent
	configSHA       string
	executableSHA   string
	auditMu         sync.Mutex
	auditFile       *os.File
	httpsMu         sync.Mutex
	httpsClients    map[string]map[net.Conn]bool
}

func newBroker(c config) (*broker, error) {
	if e := c.validate(); e != nil {
		return nil, e
	}
	if e := os.MkdirAll(c.StateDir, 0700); e != nil {
		return nil, e
	}
	st, e := os.Lstat(c.StateDir)
	if e != nil || !st.IsDir() || st.Mode()&os.ModeSymlink != 0 || st.Mode().Perm()&0077 != 0 {
		return nil, errors.New("broker state directory must be private and regular")
	}
	if e = os.MkdirAll(filepath.Dir(c.AuditPath), 0700); e != nil {
		return nil, e
	}
	f, e := os.OpenFile(c.AuditPath, os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0600)
	if e != nil {
		return nil, e
	}
	t := &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return (&net.Dialer{Timeout: 10 * time.Second}).DialContext(ctx, "unix", c.DockerSocket)
		},
		MaxIdleConns: 32, MaxIdleConnsPerHost: 16, MaxConnsPerHost: 32,
		ResponseHeaderTimeout: 30 * time.Second, DisableCompression: true,
	}
	self, e := os.ReadFile("/proc/self/exe")
	if e != nil {
		f.Close()
		return nil, e
	}
	selfSHA := sha256.Sum256(self)
	return &broker{cfg: c, client: &http.Client{Transport: t, Timeout: 30 * time.Second},
		transport: t, parents: map[string]*parent{}, sockets: map[string]net.Listener{},
		relays: map[string]*portRelay{}, pendingNetworks: map[string]*parent{},
		executableSHA: hex.EncodeToString(selfSHA[:]), auditFile: f}, nil
}

func (b *broker) audit(event string, fields map[string]any) {
	fields["event"], fields["time"] = event, time.Now().UTC().Format(time.RFC3339Nano)
	b.auditMu.Lock()
	defer b.auditMu.Unlock()
	_ = json.NewEncoder(b.auditFile).Encode(fields)
	_ = b.auditFile.Sync()
}

func (b *broker) docker(method, path string, body []byte) (int, []byte, error) {
	req, e := http.NewRequest(method, "http://docker"+path, bytes.NewReader(body))
	if e != nil {
		return 0, nil, e
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	res, e := b.client.Do(req)
	if e != nil {
		return 0, nil, e
	}
	defer res.Body.Close()
	data, e := io.ReadAll(io.LimitReader(res.Body, 16<<20))
	return res.StatusCode, data, e
}

func (b *broker) get(path string, out any) error {
	status, body, e := b.docker("GET", path, nil)
	if e != nil {
		return e
	}
	if status != 200 {
		return fmt.Errorf("Docker observation returned %d", status)
	}
	return json.Unmarshal(body, out)
}

type inspected struct {
	ID     string `json:"Id"`
	Image  string
	State  struct{ Running bool }
	Config struct {
		Env    []string
		Labels map[string]string
		User   string
	}
	HostConfig struct{ NetworkMode string }
	Mounts     []struct {
		Source, Destination, Type string
		RW                        bool
	}
	NetworkSettings struct {
		Networks map[string]struct {
			IPAddress, NetworkID, Gateway string
		}
	}
}

func envValue(env []string, name string) string {
	for _, item := range env {
		if strings.HasPrefix(item, name+"=") {
			return strings.TrimPrefix(item, name+"=")
		}
	}
	return ""
}

func scopedQuery(q url.Values, p *parent, profile string) (url.Values, error) {
	filters := map[string][]string{}
	if q.Get("filters") != "" {
		var fields map[string]json.RawMessage
		if e := json.Unmarshal([]byte(q.Get("filters")), &fields); e != nil {
			return nil, errors.New("Docker filters must be an object")
		}
		for key, raw := range fields {
			var values []string
			if e := json.Unmarshal(raw, &values); e == nil {
				filters[key] = values
				continue
			}
			// The original Docker Go SDK used by Ryuk marshals value sets as
			// objects of true booleans. Dockerode uses string arrays. Both
			// encodings retain precisely the same original filter values.
			var set map[string]bool
			if e := json.Unmarshal(raw, &set); e != nil {
				return nil, errors.New("Docker filter values must be arrays or boolean sets")
			}
			for value, enabled := range set {
				if !enabled {
					return nil, errors.New("false Docker filter set entries are unsupported")
				}
				values = append(values, value)
			}
			sort.Strings(values)
			filters[key] = values
		}
	}
	// Distinct label keys are ANDed by Docker. Reject duplicate keys that could
	// turn our ownership condition into an OR alternative on the same key.
	for _, value := range filters["label"] {
		key := strings.SplitN(value, "=", 2)[0]
		if key == parentLabel || key == profileLabel {
			return nil, errors.New("client supplied a broker ownership filter")
		}
	}
	filters["label"] = append(filters["label"], parentLabel+"="+p.ID, profileLabel+"="+profile)
	data, _ := json.Marshal(filters)
	q.Set("filters", string(data))
	return q, nil
}

func (b *broker) children(p *parent) ([]map[string]any, error) {
	q, _ := scopedQuery(url.Values{"all": {"1"}}, p, b.cfg.Profile)
	var rows []map[string]any
	e := b.get("/containers/json?"+q.Encode(), &rows)
	return rows, e
}

func (b *broker) validLease(key string) bool {
	var state struct {
		Leases map[string]struct {
			PID     int `json:"pid"`
			Request struct {
				CPUs   float64 `json:"cpus"`
				Memory int64   `json:"memory_bytes"`
			} `json:"request"`
		} `json:"leases"`
	}
	data, e := os.ReadFile(b.cfg.Reservations)
	if e != nil || json.Unmarshal(data, &state) != nil {
		return false
	}
	lease, ok := state.Leases[key]
	minimumMemory := int64(4 << 30)
	if b.cfg.Client == javaClientProfile {
		minimumMemory = 16 << 30
	}
	return ok && lease.PID > 1 && syscall.Kill(lease.PID, 0) == nil &&
		lease.Request.CPUs >= 4 && lease.Request.Memory >= minimumMemory
}

func (b *broker) admit(ip string) (*parent, error) {
	// Inspect current container ownership on every request, not just a cached IP.
	// Docker may reuse an address immediately after a prior sandbox is removed.
	f, _ := json.Marshal(map[string][]string{
		"label": {"reviewbench.identity"}, "ancestor": {b.cfg.DeveloperImage},
	})
	var rows []struct {
		ID              string `json:"Id"`
		NetworkSettings struct {
			Networks map[string]struct{ IPAddress string }
		}
	}
	if e := b.get("/containers/json?filters="+url.QueryEscape(string(f)), &rows); e != nil {
		return nil, e
	}
	for _, row := range rows {
		matched := false
		for _, n := range row.NetworkSettings.Networks {
			matched = matched || n.IPAddress == ip
		}
		if !matched {
			continue
		}
		var d inspected
		if e := b.get("/containers/"+row.ID+"/json", &d); e != nil {
			return nil, e
		}
		if !d.State.Running || d.Image != b.cfg.DeveloperImage ||
			envValue(d.Config.Env, "REVIEWBENCH_DOCKER_PROFILE") != b.cfg.Profile ||
			d.Config.Labels["reviewbench.identity"] == "" ||
			!b.validLease(d.Config.Labels[leaseLabel]) {
			return nil, errors.New("parent is not a live admitted runtime")
		}
		mountOK, contractOK := false, false
		for _, m := range d.Mounts {
			if m.Destination == b.cfg.PreloadTarget && m.Type == "bind" && !m.RW {
				st, e := os.Lstat(m.Source)
				if e == nil && st.Mode().IsRegular() && st.Size() <= maxBody {
					data, e := os.ReadFile(m.Source)
					h := sha256.Sum256(data)
					mountOK = e == nil && hex.EncodeToString(h[:]) == b.cfg.PreloadSHA256
				}
			}
			if m.Destination == b.cfg.ContractTarget && m.Type == "bind" && !m.RW {
				st, e := os.Lstat(m.Source)
				if e == nil && st.Mode().IsRegular() && st.Size() <= maxBody {
					data, e := os.ReadFile(m.Source)
					contractOK = e == nil && b.contractMatches(data)
				}
			}
		}
		if !mountOK || !contractOK {
			return nil, errors.New("parent transport/controller contract differs from the live broker")
		}
		var network struct {
			ID, Name string
			Internal bool
			IPAM     struct {
				Config []struct{ Subnet, Gateway string }
			}
		}
		if e := b.get("/networks/"+d.HostConfig.NetworkMode, &network); e != nil {
			return nil, e
		}
		if !network.Internal || !strings.HasPrefix(network.Name, "rb-exec-") ||
			!idRE.MatchString(network.ID) || len(network.IPAM.Config) != 1 {
			return nil, errors.New("parent network is not an engine-created internal network")
		}
		gateway := network.IPAM.Config[0].Gateway
		_, subnet, e := net.ParseCIDR(network.IPAM.Config[0].Subnet)
		if e != nil || net.ParseIP(gateway) == nil || !subnet.Contains(net.ParseIP(ip)) ||
			!subnet.Contains(net.ParseIP(gateway)) || ip == gateway {
			return nil, errors.New("parent network address binding differs")
		}
		b.mu.Lock()
		defer b.mu.Unlock()
		if existing := b.parents[d.ID]; existing != nil {
			if existing.Lease != d.Config.Labels[leaseLabel] || existing.IP != ip ||
				existing.NetworkID != network.ID || existing.Gateway != gateway {
				return nil, errors.New("live parent changed identity")
			}
			return existing, nil
		}
		if len(b.parents) >= b.cfg.MaxParents {
			return nil, errors.New("managed parent concurrency is full")
		}
		p := &parent{ID: d.ID, IP: ip, NetworkID: network.ID, Gateway: gateway,
			Identity: d.Config.Labels["reviewbench.identity"], Lease: d.Config.Labels[leaseLabel],
			Socket: filepath.Join(b.cfg.StateDir, d.ID[:16], "docker.sock")}
		if len(p.Socket) > 100 {
			return nil, errors.New("parent socket path exceeds the Unix path bound")
		}
		b.parents[d.ID] = p
		data, _ := json.MarshalIndent(p, "", "  ")
		if e := os.WriteFile(filepath.Join(b.cfg.StateDir, d.ID+".json"), data, 0600); e != nil {
			delete(b.parents, d.ID)
			return nil, e
		}
		b.audit("parent_admitted", map[string]any{"parent": p.ID, "lease": p.Lease,
			"network": p.NetworkID, "gateway": gateway})
		return p, nil
	}
	return nil, errors.New("peer is not an admitted candidate container")
}

func (b *broker) owned(p *parent, id string) error {
	if !regexp.MustCompile(`^[0-9a-f]{12,64}$`).MatchString(id) {
		return errors.New("container operation requires a Docker ID")
	}
	var d inspected
	if e := b.get("/containers/"+id+"/json", &d); e != nil {
		return e
	}
	if d.Config.Labels[parentLabel] != p.ID || d.Config.Labels[profileLabel] != b.cfg.Profile ||
		d.Config.Labels[leaseLabel] != p.Lease {
		return errors.New("container belongs to a different parent")
	}
	return nil
}

func (b *broker) ensureSocket(p *parent) error {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.sockets[p.ID] != nil {
		return nil
	}
	// The outer state directory stays 0700. The directory mounted read-only
	// into capability-less Ryuk needs traversal permission for its image UID.
	if e := os.MkdirAll(filepath.Dir(p.Socket), 0755); e != nil {
		return e
	}
	if _, e := os.Lstat(p.Socket); !os.IsNotExist(e) {
		return errors.New("parent socket path already exists")
	}
	listener, e := net.Listen("unix", p.Socket)
	if e != nil {
		return e
	}
	// The containing directory is mode 0700. Only this exact socket is mounted
	// into the original Ryuk image; the real host Docker socket is never mounted.
	if e = os.Chmod(p.Socket, 0666); e != nil {
		listener.Close()
		return e
	}
	b.sockets[p.ID] = listener
	server := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b.handle(w, r, p)
	}), ReadHeaderTimeout: 10 * time.Second, IdleTimeout: 30 * time.Second}
	go func() { _ = server.Serve(listener) }()
	return nil
}

func empty(v any) bool {
	if v == nil {
		return true
	}
	switch x := v.(type) {
	case string:
		return x == ""
	case []any:
		return len(x) == 0
	case map[string]any:
		return len(x) == 0
	case bool:
		return !x
	case float64:
		return x == 0
	}
	return false
}

func (b *broker) serviceImage(name string) (imageSpec, bool) {
	if image, ok := b.cfg.Images[name]; ok {
		return image, true
	}
	for alias, image := range b.cfg.Images {
		if name == image.ID || name == "docker.io/"+alias ||
			name == "docker.io/library/"+alias {
			return image, true
		}
	}
	return imageSpec{}, false
}

// createPlan changes only Docker transport/isolation/resource fields. The
// service entrypoints, commands and original randomized PostgreSQL credentials
// remain supplied by the unmodified original Testcontainers library.
func (b *broker) createPlan(p *parent, raw []byte) ([]byte, imageSpec, error) {
	if b.cfg.Client == javaClientProfile {
		return b.javaCreatePlan(p, raw)
	}
	var d map[string]any
	if len(raw) > maxBody || json.Unmarshal(raw, &d) != nil {
		return nil, imageSpec{}, errors.New("invalid Docker create body")
	}
	name, _ := d["Image"].(string)
	image, ok := b.serviceImage(name)
	if !ok {
		return nil, imageSpec{}, errors.New("image is not in the pinned original service profile")
	}
	for key, value := range d {
		switch key {
		case "Image", "Env", "Labels", "ExposedPorts", "HostConfig", "name":
		case "Cmd", "Entrypoint", "User", "WorkingDir", "Healthcheck":
			if !empty(value) {
				return nil, image, fmt.Errorf("original service field changed: %s", key)
			}
		default:
			return nil, image, fmt.Errorf("unsupported service create field: %s", key)
		}
	}
	host, _ := d["HostConfig"].(map[string]any)
	if host == nil {
		host = map[string]any{}
	}
	for key, value := range host {
		switch key {
		case "Binds", "PortBindings", "LogConfig", "AutoRemove":
		case "NetworkMode":
			if !empty(value) && value != "bridge" && value != "default" {
				return nil, image, errors.New("client selected a network")
			}
		default:
			if !empty(value) {
				return nil, image, fmt.Errorf("unsupported original HostConfig field: %s", key)
			}
		}
	}
	ports, _ := d["ExposedPorts"].(map[string]any)
	port := strconv.Itoa(image.Port)
	if len(ports) != 1 || (ports[port] == nil && ports[port+"/tcp"] == nil) {
		return nil, image, errors.New("original exposed service port changed")
	}
	bindings, _ := host["PortBindings"].(map[string]any)
	if len(bindings) != 1 {
		return nil, image, errors.New("original ephemeral port binding missing")
	}
	for key, value := range bindings {
		list, ok := value.([]any)
		if (key != port && key != port+"/tcp") || !ok || len(list) != 1 {
			return nil, image, errors.New("unsupported client port binding")
		}
		entry, _ := list[0].(map[string]any)
		if entry["HostPort"] != "0" || (entry["HostIp"] != nil && entry["HostIp"] != "") {
			return nil, image, errors.New("client requested a fixed host port/address")
		}
	}
	env, _ := d["Env"].([]any)
	if image.Role == "postgres" {
		if len(env) != 2 || !empty(host["Binds"]) {
			return nil, image, errors.New("PostgreSQL requires only the original user/password environment")
		}
		values := map[string]string{}
		for _, item := range env {
			s, ok := item.(string)
			parts := strings.SplitN(s, "=", 2)
			if !ok || len(parts) != 2 || values[parts[0]] != "" {
				return nil, image, errors.New("invalid service environment")
			}
			values[parts[0]] = parts[1]
		}
		if !userRE.MatchString(values["POSTGRES_USER"]) ||
			!regexp.MustCompile(`^[0-9a-f]{32}$`).MatchString(values["POSTGRES_PASSWORD"]) {
			return nil, image, errors.New("original randomized PostgreSQL credentials differ")
		}
		d["User"] = "postgres"
		delete(host, "Binds")
		host["Memory"], host["MemorySwap"], host["NanoCpus"] = int64(512<<20), int64(512<<20), int64(750000000)
	} else {
		mounts, ok := host["Binds"].([]any)
		if len(env) != 0 || !ok || len(mounts) != 1 ||
			(mounts[0] != "/var/run/docker.sock:/var/run/docker.sock:rw" &&
				mounts[0] != "/var/run/docker.sock:/var/run/docker.sock") {
			return nil, image, errors.New("Ryuk requires exactly its original Docker socket mount")
		}
		// Bind a private socket directory read-only, not the socket inode. This
		// lets an existing Ryuk reconnect after a broker process recovery while
		// exposing no other host file and granting no filesystem writes.
		host["Binds"] = []string{filepath.Dir(p.Socket) + ":/var/run:ro"}
		host["Memory"], host["MemorySwap"], host["NanoCpus"] = int64(64<<20), int64(64<<20), int64(100000000)
	}
	labels, _ := d["Labels"].(map[string]any)
	if labels == nil {
		return nil, image, errors.New("original Testcontainers labels missing")
	}
	for k, v := range labels {
		if _, ok := v.(string); !ok || strings.HasPrefix(k, "reviewbench.") {
			return nil, image, errors.New("client supplied invalid/broker-reserved labels")
		}
	}
	labels[parentLabel], labels[profileLabel], labels[roleLabel], labels[leaseLabel] =
		p.ID, b.cfg.Profile, image.Role, p.Lease
	host["NetworkMode"], host["PublishAllPorts"] = p.NetworkID, false
	host["PortBindings"] = map[string]any{
		port + "/tcp": []map[string]string{{"HostIp": p.Gateway, "HostPort": "0"}},
	}
	host["CapDrop"], host["SecurityOpt"], host["PidsLimit"] =
		[]string{"ALL"}, []string{"no-new-privileges"}, 128
	host["LogConfig"] = map[string]any{"Type": "json-file",
		"Config": map[string]string{"max-size": "8m", "max-file": "1"}}
	d["Image"], d["HostConfig"], d["ExposedPorts"] = image.ID, host,
		map[string]any{port + "/tcp": map[string]any{}}
	data, e := json.Marshal(d)
	return data, image, e
}

func deny(w http.ResponseWriter, message string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusForbidden)
	_ = json.NewEncoder(w).Encode(map[string]string{"message": message})
}

func isImageInspection(path string) bool {
	return path != "/images/json" && strings.HasPrefix(path, "/images/") &&
		strings.HasSuffix(path, "/json")
}

func (b *broker) handle(w http.ResponseWriter, r *http.Request, fixed *parent) {
	ip, _, e := net.SplitHostPort(r.RemoteAddr)
	if fixed == nil && r.URL.Path == "/_reviewbench/health" &&
		e == nil && net.ParseIP(ip).IsLoopback() {
		b.mu.Lock()
		n := len(b.parents)
		b.mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"status": "ready", "parents": n,
			"profile": b.cfg.Profile, "backend": "real-docker", "synthetic_container_results": false,
			"config_sha256": b.configSHA, "executable_sha256": b.executableSHA})
		return
	}
	p := fixed
	if fixed == nil {
		if e != nil {
			deny(w, "invalid peer address")
			return
		}
		p, e = b.admit(ip)
	} else {
		var d inspected
		e = b.get("/containers/"+p.ID+"/json", &d)
		if e == nil && (!d.State.Running || d.Config.Labels[leaseLabel] != p.Lease) {
			e = errors.New("parent runtime has ended")
		}
	}
	if e != nil {
		b.audit("denied", map[string]any{"peer": ip, "path": r.URL.Path, "reason": e.Error()})
		deny(w, e.Error())
		return
	}
	if r.Method == "CONNECT" {
		if fixed != nil {
			deny(w, "reaper sockets do not provide outbound HTTPS")
			return
		}
		b.serveHTTPS(w, r, p)
		return
	}
	path := versionRE.ReplaceAllString(r.URL.Path, "/")
	if strings.Contains(path, "..") || strings.Contains(path, "//") {
		deny(w, "noncanonical Docker path")
		return
	}
	r.URL.Path, r.URL.RawPath = path, ""
	inputHash := ""
	role := ""
	allowed := false
	switch {
	case (r.Method == "GET" || r.Method == "HEAD") &&
		(path == "/_ping" || path == "/version" || path == "/info"):
		allowed = true
	case r.Method == "GET" && isImageInspection(path):
		name := strings.TrimSuffix(strings.TrimPrefix(path, "/images/"), "/json")
		if image, ok := b.serviceImage(name); ok {
			r.URL.Path, allowed = "/images/"+image.ID+"/json", true
		}
	case r.Method == "POST" && path == "/containers/create":
		b.createMu.Lock()
		defer b.createMu.Unlock()
		var current inspected
		if b.get("/containers/"+p.ID+"/json", &current) != nil ||
			!current.State.Running || current.Config.Labels[leaseLabel] != p.Lease {
			deny(w, "parent ended before service creation")
			return
		}
		raw, readErr := io.ReadAll(io.LimitReader(r.Body, maxBody+1))
		if readErr != nil {
			deny(w, "create body read failed")
			return
		}
		hash := sha256.Sum256(raw)
		inputHash = hex.EncodeToString(hash[:])
		b.audit("original_create_request", map[string]any{"parent": p.ID,
			"input_sha256": inputHash, "request_body": string(raw)})
		body, image, planErr := b.createPlan(p, raw)
		if planErr != nil {
			deny(w, planErr.Error())
			b.audit("create_denied", map[string]any{"parent": p.ID,
				"input_sha256": inputHash, "reason": planErr.Error()})
			return
		}
		rows, countErr := b.children(p)
		sameRole := 0
		for _, row := range rows {
			labels, _ := row["Labels"].(map[string]any)
			if labels[roleLabel] == image.Role {
				sameRole++
			}
		}
		maxSameRole := 2
		if b.cfg.Client == javaClientProfile {
			maxSameRole = 1
		}
		if countErr != nil || len(rows) >= 4 || sameRole >= maxSameRole {
			deny(w, "parent service reservation is full")
			return
		}
		if image.Role == "ryuk" {
			if e := b.ensureSocket(p); e != nil {
				deny(w, e.Error())
				return
			}
		}
		r.Body, r.ContentLength = io.NopCloser(bytes.NewReader(body)), int64(len(body))
		r.Header.Set("Content-Length", strconv.Itoa(len(body)))
		q := r.URL.Query()
		if name := q.Get("name"); name != "" {
			q.Set("name", "rb-"+p.ID[:12]+"-"+name)
			r.URL.RawQuery = q.Encode()
		}
		role, allowed = image.Role, true
	case (r.Method == "GET" && (path == "/containers/json" || path == "/networks" ||
		path == "/volumes" || path == "/images/json")) ||
		(r.Method == "POST" && (path == "/containers/prune" || path == "/networks/prune" ||
			path == "/volumes/prune" || path == "/images/prune")):
		q, err := scopedQuery(r.URL.Query(), p, b.cfg.Profile)
		if err == nil {
			r.URL.RawQuery, allowed = q.Encode(), true
		}
	case strings.HasPrefix(path, "/containers/"):
		parts := strings.Split(strings.TrimPrefix(path, "/containers/"), "/")
		if len(parts) >= 1 && b.owned(p, parts[0]) == nil {
			op := strings.Join(parts[1:], "/")
			switch r.Method + ":" + op {
			case "GET:json", "GET:logs", "GET:top", "GET:stats", "GET:archive",
				"POST:start", "POST:stop", "POST:kill", "POST:wait", "POST:attach",
				"GET:attach/ws", "POST:exec", "DELETE:":
				allowed = true
			}
			if r.Method == "POST" && op == "exec" {
				data, err := io.ReadAll(io.LimitReader(r.Body, maxBody+1))
				var options map[string]any
				if err != nil || len(data) > maxBody || json.Unmarshal(data, &options) != nil ||
					!empty(options["Privileged"]) || !empty(options["User"]) {
					allowed = false
				}
				r.Body, r.ContentLength = io.NopCloser(bytes.NewReader(data)), int64(len(data))
			}
			if r.Method == "DELETE" && op == "" {
				q := r.URL.Query()
				q.Set("v", "true")
				r.URL.RawQuery = q.Encode()
			}
		}
	case strings.HasPrefix(path, "/exec/"):
		parts := strings.Split(strings.TrimPrefix(path, "/exec/"), "/")
		if len(parts) == 2 && idRE.MatchString(parts[0]) {
			var info struct{ ContainerID string }
			if b.get("/exec/"+parts[0]+"/json", &info) == nil && b.owned(p, info.ContainerID) == nil {
				allowed = (r.Method == "POST" && parts[1] == "start") ||
					(r.Method == "GET" && parts[1] == "json")
			}
		}
	case r.Method == "GET" && path == "/networks/"+p.NetworkID:
		allowed = true
	}
	if !allowed {
		b.audit("denied", map[string]any{"parent": p.ID, "path": path, "method": r.Method})
		deny(w, "operation is not in this parent's original Docker lifecycle")
		return
	}
	proxy := &httputil.ReverseProxy{
		Director: func(req *http.Request) {
			req.URL.Scheme, req.URL.Host, req.Host = "http", "docker", "docker"
			req.Header.Del("Authorization")
		},
		Transport: b.transport, FlushInterval: -1,
		ErrorHandler: func(w http.ResponseWriter, _ *http.Request, err error) {
			b.audit("transport_error", map[string]any{"parent": p.ID, "path": path, "error": err.Error()})
			http.Error(w, "Docker transport failed", http.StatusBadGateway)
		},
		ModifyResponse: func(res *http.Response) error {
			event := map[string]any{"parent": p.ID, "lease": p.Lease, "path": path,
				"method": r.Method, "status": res.StatusCode, "backend": "real-docker"}
			if inputHash != "" {
				event["input_sha256"], event["role"] = inputHash, role
			}
			if path == "/containers/create" && res.StatusCode == 201 {
				body, err := io.ReadAll(io.LimitReader(res.Body, maxBody))
				res.Body.Close()
				if err != nil {
					return err
				}
				var reply struct {
					ID string `json:"Id"`
				}
				if err := json.Unmarshal(body, &reply); err != nil || !idRE.MatchString(reply.ID) {
					return errors.New("real Docker create returned an invalid ID")
				}
				event["container_id"] = reply.ID
				res.Body = io.NopCloser(bytes.NewReader(body))
				res.ContentLength = int64(len(body))
			}
			if r.Method == "GET" && strings.HasPrefix(path, "/containers/") &&
				strings.HasSuffix(path, "/json") && path != "/containers/json" && res.StatusCode == 200 {
				raw, err := io.ReadAll(io.LimitReader(res.Body, maxBody+1))
				res.Body.Close()
				if err != nil || len(raw) > maxBody {
					return errors.New("container observation exceeds its bound")
				}
				body, err := b.withActualPort(p, raw)
				if err != nil {
					return err
				}
				res.Body = io.NopCloser(bytes.NewReader(body))
				res.ContentLength = int64(len(body))
				res.Header.Set("Content-Length", strconv.Itoa(len(body)))
			}
			if r.Method == "DELETE" && strings.HasPrefix(path, "/containers/") &&
				(res.StatusCode == 204 || res.StatusCode == 404) {
				b.closeRelay(strings.TrimPrefix(path, "/containers/"))
			}
			b.audit("docker_response", event)
			return nil
		},
	}
	proxy.ServeHTTP(w, r)
}

func (b *broker) cleanup(p *parent) {
	b.createMu.Lock()
	defer b.createMu.Unlock()
	b.closeParentHTTPS(p.ID)
	rows, e := b.children(p)
	if e != nil {
		b.audit("cleanup_retry", map[string]any{"parent": p.ID, "error": e.Error()})
		return
	}
	for _, row := range rows {
		id, _ := row["Id"].(string)
		if idRE.MatchString(id) && b.owned(p, id) == nil {
			status, _, err := b.docker("DELETE", "/containers/"+id+"?force=true&v=true", nil)
			b.audit("parent_end_container_cleanup", map[string]any{"parent": p.ID,
				"container_id": id, "status": status, "error": fmt.Sprint(err)})
			if err != nil || (status != 204 && status != 404) {
				return
			}
		}
	}
	b.mu.Lock()
	if listener := b.sockets[p.ID]; listener != nil {
		listener.Close()
		delete(b.sockets, p.ID)
	}
	delete(b.parents, p.ID)
	b.pendingNetworks[p.ID] = p
	b.mu.Unlock()
	b.closeParentRelays(p.ID)
	_ = os.Remove(p.Socket)
	_ = os.Remove(filepath.Dir(p.Socket))
	b.audit("parent_closed", map[string]any{"parent": p.ID, "owned_containers_remaining": 0})
}

func (b *broker) sweep() {
	b.mu.Lock()
	parents := make([]*parent, 0, len(b.parents))
	for _, p := range b.parents {
		parents = append(parents, p)
	}
	b.mu.Unlock()
	for _, p := range parents {
		status, data, err := b.docker("GET", "/containers/"+p.ID+"/json", nil)
		var d inspected
		ended := status == 404 || (status == 200 && json.Unmarshal(data, &d) == nil && !d.State.Running)
		if err == nil && ended {
			b.cleanup(p)
		}
	}
	b.sweepRelaysAndNetworks()
}

func main() {
	path := flag.String("config", "", "pinned broker configuration")
	flag.Parse()
	data, err := os.ReadFile(*path)
	var c config
	if err != nil || json.Unmarshal(data, &c) != nil {
		log.Fatal("read broker configuration")
	}
	b, err := newBroker(c)
	if err != nil {
		log.Fatal(err)
	}
	configSHA := sha256.Sum256(data)
	b.configSHA = hex.EncodeToString(configSHA[:])
	// Recovery is based on our previously saved exact parent IDs, not names or
	// arbitrary running databases. A live parent's service is never restarted.
	files, _ := filepath.Glob(filepath.Join(c.StateDir, "*.json"))
	sort.Strings(files)
	for _, name := range files {
		var p parent
		body, e := os.ReadFile(name)
		if e != nil || json.Unmarshal(body, &p) != nil || !idRE.MatchString(p.ID) ||
			filepath.Base(name) != p.ID+".json" || p.Socket != filepath.Join(c.StateDir, p.ID[:16], "docker.sock") {
			log.Fatal("invalid retained broker parent")
		}
		b.parents[p.ID] = &p
		// A crashed broker's Unix socket is not a live Docker daemon socket.
		if st, e := os.Lstat(p.Socket); e == nil && st.Mode()&os.ModeSocket != 0 {
			_ = os.Remove(p.Socket)
		}
		if e := b.ensureSocket(&p); e != nil {
			log.Fatal(e)
		}
	}
	server := &http.Server{Addr: c.Listen, ReadHeaderTimeout: 10 * time.Second,
		IdleTimeout: 30 * time.Second, MaxHeaderBytes: 32 << 10,
		Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { b.handle(w, r, nil) })}
	go func() {
		for range time.NewTicker(time.Second).C {
			b.sweep()
		}
	}()
	done := make(chan os.Signal, 1)
	signal.Notify(done, os.Interrupt, syscall.SIGTERM)
	go func() {
		<-done
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = server.Shutdown(ctx)
	}()
	b.audit("broker_started", map[string]any{"profile": c.Profile, "listen": c.Listen,
		"backend": "real-docker", "max_parents": c.MaxParents})
	fmt.Println("REVIEWBENCH_DOCKER_BROKER_READY real_docker=true max_parents=" + strconv.Itoa(c.MaxParents))
	if e := server.ListenAndServe(); e != nil && e != http.ErrServerClosed {
		log.Fatal(e)
	}
	b.audit("broker_stopped", map[string]any{"live_parent_services_preserved": true})
}
