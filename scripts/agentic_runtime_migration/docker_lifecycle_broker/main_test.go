package main

import (
	"encoding/json"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func fixture() (*broker, *parent) {
	b := &broker{cfg: config{
		Profile: "hiero42-original-testcontainers-v1",
		Images: map[string]imageSpec{
			"postgres:14-alpine":        {ID: "sha256:" + strings.Repeat("1", 64), Role: "postgres", Port: 5432},
			"testcontainers/ryuk:0.4.0": {ID: "sha256:" + strings.Repeat("2", 64), Role: "ryuk", Port: 8080},
		},
	}}
	p := &parent{ID: strings.Repeat("a", 64), IP: "172.27.0.2", Gateway: "172.27.0.1",
		NetworkID: strings.Repeat("b", 64), Lease: strings.Repeat("c", 32),
		Socket: "/tmp/fixture/aaaaaaaaaaaaaaaa/docker.sock"}
	return b, p
}

func request(role string) map[string]any {
	name, port := "postgres:14-alpine", "5432"
	env := []any{"POSTGRES_USER=mirror_api_admin", "POSTGRES_PASSWORD=" + strings.Repeat("f", 32)}
	binds := []any{}
	if role == "ryuk" {
		name, port, env = "testcontainers/ryuk:0.4.0", "8080", []any{}
		binds = []any{"/var/run/docker.sock:/var/run/docker.sock:rw"}
	}
	return map[string]any{
		"Image": name, "Env": env,
		"ExposedPorts": map[string]any{port: map[string]any{}},
		"Labels":       map[string]any{"org.testcontainers": "true", "org.testcontainers.session-id": "original-session"},
		"HostConfig": map[string]any{
			"AutoRemove": false, "Binds": binds, "CapAdd": []any{}, "CapDrop": []any{},
			"ExtraHosts": []any{}, "Tmpfs": map[string]any{}, "Privileged": false,
			"PortBindings": map[string]any{port: []any{map[string]any{"HostPort": "0"}}},
		},
	}
}

func plan(t *testing.T, b *broker, p *parent, d map[string]any) (map[string]any, imageSpec, error) {
	t.Helper()
	data, _ := json.Marshal(d)
	raw, image, err := b.createPlan(p, data)
	var out map[string]any
	if err == nil {
		if err := json.Unmarshal(raw, &out); err != nil {
			t.Fatal(err)
		}
	}
	return out, image, err
}

func TestOriginalPostgresUsesRealPinnedImageAndPreservesEnvironment(t *testing.T) {
	b, p := fixture()
	input := request("postgres")
	out, image, err := plan(t, b, p, input)
	if err != nil {
		t.Fatal(err)
	}
	if image.Role != "postgres" || out["Image"] != image.ID || out["User"] != "postgres" {
		t.Fatal(out)
	}
	if !reflect.DeepEqual(input["Env"], out["Env"]) || out["Cmd"] != nil || out["Entrypoint"] != nil {
		t.Fatal("original environment/entrypoint changed")
	}
	host := out["HostConfig"].(map[string]any)
	if host["NetworkMode"] != p.NetworkID || host["Memory"] != float64(512<<20) ||
		host["MemorySwap"] != float64(512<<20) || host["NanoCpus"] != float64(750000000) ||
		host["Binds"] != nil {
		t.Fatal(host)
	}
	binding := host["PortBindings"].(map[string]any)["5432/tcp"].([]any)[0].(map[string]any)
	if binding["HostIp"] != p.Gateway || binding["HostPort"] != "0" {
		t.Fatal(binding)
	}
	labels := out["Labels"].(map[string]any)
	if labels[parentLabel] != p.ID || labels[leaseLabel] != p.Lease ||
		labels["org.testcontainers.session-id"] != "original-session" {
		t.Fatal(labels)
	}
}

func TestOriginalRyukGetsOnlyScopedSocketDirectory(t *testing.T) {
	b, p := fixture()
	out, image, err := plan(t, b, p, request("ryuk"))
	if err != nil || image.Role != "ryuk" {
		t.Fatal(err, image)
	}
	host := out["HostConfig"].(map[string]any)
	binds := host["Binds"].([]any)
	if len(binds) != 1 || binds[0] != filepath.Dir(p.Socket)+":/var/run:ro" ||
		strings.Contains(binds[0].(string), "/var/run/docker.sock:/var/run/docker.sock") {
		t.Fatal("real host Docker socket exposed", binds)
	}
	if host["Memory"] != float64(64<<20) || host["NanoCpus"] != float64(100000000) {
		t.Fatal("reaper limits missing", host)
	}
}

func TestRejectsChangedContainerBoundary(t *testing.T) {
	cases := map[string]func(map[string]any){
		"foreign-image": func(d map[string]any) { d["Image"] = "postgres:latest" },
		"command":       func(d map[string]any) { d["Cmd"] = []any{"sh"} },
		"entrypoint":    func(d map[string]any) { d["Entrypoint"] = []any{"/bin/sh"} },
		"user":          func(d map[string]any) { d["User"] = "root" },
		"workdir":       func(d map[string]any) { d["WorkingDir"] = "/host" },
		"health":        func(d map[string]any) { d["Healthcheck"] = map[string]any{"Test": []any{"CMD", "sh"}} },
		"volume":        func(d map[string]any) { d["Volumes"] = map[string]any{"/host": map[string]any{}} },
		"networking":    func(d map[string]any) { d["NetworkingConfig"] = map[string]any{"EndpointsConfig": "foreign"} },
		"privileged":    func(d map[string]any) { d["HostConfig"].(map[string]any)["Privileged"] = true },
		"caps":          func(d map[string]any) { d["HostConfig"].(map[string]any)["CapAdd"] = []any{"SYS_ADMIN"} },
		"pid":           func(d map[string]any) { d["HostConfig"].(map[string]any)["PidMode"] = "host" },
		"ipc":           func(d map[string]any) { d["HostConfig"].(map[string]any)["IpcMode"] = "host" },
		"network":       func(d map[string]any) { d["HostConfig"].(map[string]any)["NetworkMode"] = "host" },
		"bind":          func(d map[string]any) { d["HostConfig"].(map[string]any)["Binds"] = []any{"/:/host:rw"} },
		"tmpfs":         func(d map[string]any) { d["HostConfig"].(map[string]any)["Tmpfs"] = map[string]any{"/host": "rw"} },
		"device":        func(d map[string]any) { d["HostConfig"].(map[string]any)["Devices"] = []any{"/dev/sda"} },
		"extra-host":    func(d map[string]any) { d["HostConfig"].(map[string]any)["ExtraHosts"] = []any{"host:1.2.3.4"} },
		"env":           func(d map[string]any) { d["Env"] = []any{"PGDATA=/host"} },
		"password": func(d map[string]any) {
			d["Env"] = []any{"POSTGRES_USER=mirror_api_admin", "POSTGRES_PASSWORD=not-original"}
		},
		"duplicate-env": func(d map[string]any) {
			d["Env"] = []any{"POSTGRES_USER=mirror_api_admin", "POSTGRES_USER=another"}
		},
		"foreign-label": func(d map[string]any) { d["Labels"].(map[string]any)[parentLabel] = "foreign" },
		"label-type":    func(d map[string]any) { d["Labels"].(map[string]any)["unexpected"] = true },
		"extra-port":    func(d map[string]any) { d["ExposedPorts"].(map[string]any)["22"] = map[string]any{} },
		"fixed-port": func(d map[string]any) {
			d["HostConfig"].(map[string]any)["PortBindings"] =
				map[string]any{"5432": []any{map[string]any{"HostPort": "5432"}}}
		},
		"foreign-binding": func(d map[string]any) {
			d["HostConfig"].(map[string]any)["PortBindings"] =
				map[string]any{"5432": []any{map[string]any{"HostPort": "0", "HostIp": "0.0.0.0"}}}
		},
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			b, p := fixture()
			input := request("postgres")
			mutate(input)
			if _, _, err := plan(t, b, p, input); err == nil {
				t.Fatal("changed boundary accepted")
			}
		})
	}
}

func TestRejectsChangedRyukMountAndEnvironment(t *testing.T) {
	for _, change := range []func(map[string]any){
		func(d map[string]any) { d["HostConfig"].(map[string]any)["Binds"] = []any{} },
		func(d map[string]any) {
			d["HostConfig"].(map[string]any)["Binds"] = []any{"/host:/var/run/docker.sock:rw"}
		},
		func(d map[string]any) { d["Env"] = []any{"DOCKER_HOST=unix:///real-host.sock"} },
	} {
		b, p := fixture()
		input := request("ryuk")
		change(input)
		if _, _, err := plan(t, b, p, input); err == nil {
			t.Fatal("changed reaper accepted")
		}
	}
}

func TestMalformedRequests(t *testing.T) {
	b, p := fixture()
	for _, data := range [][]byte{nil, []byte("[]"), []byte("{"), bytesOf(maxBody + 1)} {
		if _, _, err := b.createPlan(p, data); err == nil {
			t.Fatal("invalid request accepted")
		}
	}
}

func bytesOf(n int) []byte { return []byte(strings.Repeat(" ", n)) }

func TestFilterScopeRetainsOriginalSessionAndAddsOwnerIntersection(t *testing.T) {
	_, p := fixture()
	q := url.Values{"all": {"1"}, "filters": {`{"label":["org.testcontainers.session-id=original"]}`}}
	out, err := scopedQuery(q, p, "profile")
	if err != nil {
		t.Fatal(err)
	}
	var filters map[string][]string
	_ = json.Unmarshal([]byte(out.Get("filters")), &filters)
	want := []string{"org.testcontainers.session-id=original", parentLabel + "=" + p.ID, profileLabel + "=profile"}
	if !reflect.DeepEqual(filters["label"], want) || out.Get("all") != "1" {
		t.Fatal(filters)
	}
}

func TestFilterScopeRejectsOwnershipORAndMalformedFilters(t *testing.T) {
	_, p := fixture()
	for _, value := range []string{
		`{"label":["reviewbench.docker-parent=foreign"]}`,
		`{"label":["reviewbench.docker-profile=foreign"]}`,
		`{"label":["reviewbench.docker-parent"]}`,
		`{"label":"not-an-array"}`, "[]", "{",
		`{"label":{"org.testcontainers.session-id=original":false}}`,
		`{"label":{"reviewbench.docker-parent=foreign":true}}`,
		`{"label":{"org.testcontainers.session-id=original":"true"}}`,
	} {
		if _, err := scopedQuery(url.Values{"filters": {value}}, p, "profile"); err == nil {
			t.Fatal("invalid scope accepted", value)
		}
	}
}

func TestOriginalRyukBooleanFilterSetsPreserveEverySelector(t *testing.T) {
	_, p := fixture()
	raw := `{"label":{"org.testcontainers.session-id=original":true},"dangling":{"false":true}}`
	q, err := scopedQuery(url.Values{"filters": {raw}}, p, "profile")
	if err != nil {
		t.Fatal(err)
	}
	var filters map[string][]string
	if err := json.Unmarshal([]byte(q.Get("filters")), &filters); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(filters["label"], []string{"org.testcontainers.session-id=original",
		parentLabel + "=" + p.ID, profileLabel + "=profile"}) ||
		!reflect.DeepEqual(filters["dangling"], []string{"false"}) {
		t.Fatal("original filter values changed", filters)
	}
}

func TestServiceImageAliasesNeverChooseAnotherVersion(t *testing.T) {
	b, _ := fixture()
	for _, name := range []string{"postgres:14-alpine", "docker.io/library/postgres:14-alpine",
		"sha256:" + strings.Repeat("1", 64), "testcontainers/ryuk:0.4.0"} {
		if _, ok := b.serviceImage(name); !ok {
			t.Fatal(name)
		}
	}
	for _, name := range []string{"postgres", "postgres:latest", "postgres:15-alpine", "testcontainers/ryuk:latest"} {
		if _, ok := b.serviceImage(name); ok {
			t.Fatal(name)
		}
	}
}

func TestScopedSocketDirectoryWorksWithoutDacOverride(t *testing.T) {
	root, err := os.MkdirTemp("/tmp", "rb-uds-test-")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(root)
	b, p := fixture()
	b.sockets = map[string]net.Listener{}
	p.Socket = filepath.Join(root, "owner", "docker.sock")
	if err := b.ensureSocket(p); err != nil {
		t.Fatal(err)
	}
	defer b.sockets[p.ID].Close()
	outer, _ := os.Stat(root)
	directory, _ := os.Stat(filepath.Dir(p.Socket))
	socket, _ := os.Stat(p.Socket)
	if outer.Mode().Perm() != 0700 || directory.Mode().Perm() != 0755 ||
		socket.Mode().Perm() != 0666 || socket.Mode()&os.ModeSocket == 0 {
		t.Fatal("socket boundary permissions differ", outer.Mode(), directory.Mode(), socket.Mode())
	}
}
