package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
)

// This is a separate, unadmitted profile for the original installed Java
// client. The original Node profile remains byte-for-byte on its old route.
// Real library lifecycle controls must precede runtime readiness/admission.
const javaClientProfile = "mfscreener-testcontainers-java-1.19.7"

var javaServices = map[string]struct {
	role string
	port int
}{
	"postgres:16-alpine":        {"postgres", 5432},
	"redis:7.2.4-alpine":        {"redis", 6379},
	"openzipkin/zipkin:latest":  {"zipkin", 9411},
	"testcontainers/ryuk:0.6.0": {"ryuk", 8080},
}

func (c config) validateJavaImages() error {
	if c.Profile != "mfscreener133-135-original-testcontainers-v1" {
		return errors.New("Java task profile differs")
	}
	for tag, expected := range javaServices {
		image, ok := c.Images[tag]
		if !ok || !digestRE.MatchString(image.ID) ||
			image.Role != expected.role || image.Port != expected.port {
			return errors.New("Java original service image/port binding differs")
		}
	}
	return nil
}

func stringEnvironment(raw any) (map[string]string, error) {
	rows, ok := raw.([]any)
	if raw != nil && !ok {
		return nil, errors.New("service environment is not an array")
	}
	values := map[string]string{}
	for _, value := range rows {
		text, ok := value.(string)
		pair := strings.SplitN(text, "=", 2)
		if !ok || len(pair) != 2 {
			return nil, errors.New("invalid service environment entry")
		}
		if _, present := values[pair[0]]; present {
			return nil, errors.New("duplicated service environment key")
		}
		values[pair[0]] = pair[1]
	}
	return values, nil
}

func (b *broker) javaCreatePlan(p *parent, raw []byte) ([]byte, imageSpec, error) {
	var d map[string]any
	if len(raw) > maxBody || json.Unmarshal(raw, &d) != nil {
		return nil, imageSpec{}, errors.New("invalid Java Docker create body")
	}
	name, _ := d["Image"].(string)
	image, ok := b.serviceImage(name)
	if !ok {
		return nil, imageSpec{}, errors.New("image is not an original pinned Java service")
	}
	for key, value := range d {
		switch key {
		case "Image", "Env", "Labels", "ExposedPorts", "HostConfig", "Cmd", "name":
		case "AttachStdout", "AttachStderr":
			if value != nil {
				if _, ok := value.(bool); !ok {
					return nil, image, errors.New("stream attachment must be boolean")
				}
			}
		case "Hostname", "Domainname", "User", "WorkingDir", "Entrypoint", "Healthcheck",
			"AttachStdin", "Tty", "OpenStdin", "StdinOnce", "Volumes", "MacAddress",
			"NetworkDisabled", "OnBuild", "StopSignal", "StopTimeout", "Shell",
			"PortSpecs", "ArgsEscaped", "NetworkingConfig":
			if !empty(value) {
				return nil, image, fmt.Errorf("original Java create field differs: %s", key)
			}
		default:
			return nil, image, fmt.Errorf("unknown Java create field: %s", key)
		}
	}
	host, ok := d["HostConfig"].(map[string]any)
	if !ok {
		return nil, image, errors.New("original Java HostConfig is missing")
	}
	for key, value := range host {
		switch key {
		case "Binds", "PortBindings", "LogConfig", "AutoRemove":
		case "NetworkMode":
			if !empty(value) && value != "default" && value != "bridge" {
				return nil, image, errors.New("Java client selected another network")
			}
		case "ShmSize":
			if !empty(value) && value != float64(64<<20) {
				return nil, image, errors.New("Java client expanded shared memory")
			}
		case "Privileged":
			// The retained1.19.7 bytecode requests privileged=true for Ryuk.
			// Its actual Docker operations use our scoped socket, so only this
			// original reaper request is translated to a capability-less child.
			if (image.Role == "ryuk" && value != true) ||
				(image.Role != "ryuk" && !empty(value)) {
				return nil, image, errors.New("original Java privilege request differs")
			}
		default:
			if !empty(value) {
				return nil, image, fmt.Errorf("non-default Java HostConfig field: %s", key)
			}
		}
	}
	port := strconv.Itoa(image.Port) + "/tcp"
	ports, _ := d["ExposedPorts"].(map[string]any)
	if len(ports) != 1 || ports[port] == nil {
		return nil, image, errors.New("original Java exposed port differs")
	}
	bindings, _ := host["PortBindings"].(map[string]any)
	if len(bindings) != 1 {
		return nil, image, errors.New("Java ephemeral service port is missing")
	}
	rows, ok := bindings[port].([]any)
	if !ok || len(rows) != 1 {
		return nil, image, errors.New("Java service port binding differs")
	}
	entry, ok := rows[0].(map[string]any)
	if !ok || (entry["HostPort"] != "" && entry["HostPort"] != "0") ||
		!empty(entry["HostIp"]) || len(entry) > 2 {
		return nil, image, errors.New("Java client requested a fixed port/address")
	}
	env, err := stringEnvironment(d["Env"])
	if err != nil {
		return nil, image, err
	}
	if image.Role == "postgres" {
		// Actual defaults of the retained PostgreSQLContainer, not another DB.
		expected := map[string]string{
			"POSTGRES_DB": "test", "POSTGRES_USER": "test", "POSTGRES_PASSWORD": "test",
		}
		command := []any{"postgres", "-c", "fsync=off"}
		if !reflect.DeepEqual(env, expected) || !reflect.DeepEqual(d["Cmd"], command) {
			return nil, image, errors.New("original Java PostgreSQL configuration differs")
		}
		d["User"] = "postgres"
		host["Memory"], host["NanoCpus"] = int64(512<<20), int64(750000000)
	} else {
		if len(env) != 0 || !empty(d["Cmd"]) {
			return nil, image, errors.New("original Java service command/environment differs")
		}
		switch image.Role {
		case "redis":
			d["User"] = "redis"
			host["Memory"], host["NanoCpus"] = int64(128<<20), int64(250000000)
		case "zipkin":
			host["Memory"], host["NanoCpus"] = int64(768<<20), int64(750000000)
		case "ryuk":
			host["Memory"], host["NanoCpus"] = int64(64<<20), int64(100000000)
		}
	}
	if image.Role == "ryuk" {
		if host["Privileged"] != true {
			return nil, image, errors.New("original Java reaper privilege declaration is missing")
		}
		binds, ok := host["Binds"].([]any)
		if !ok || len(binds) != 1 ||
			(binds[0] != "/var/run/docker.sock:/var/run/docker.sock:rw" &&
				binds[0] != "/var/run/docker.sock:/var/run/docker.sock") {
			return nil, image, errors.New("original Java Ryuk socket declaration differs")
		}
		host["Binds"] = []string{filepath.Dir(p.Socket) + ":/var/run:ro"}
	} else {
		if !empty(host["Binds"]) {
			return nil, image, errors.New("Java service requested a host path")
		}
		delete(host, "Binds")
	}
	labels, ok := d["Labels"].(map[string]any)
	if !ok || labels["org.testcontainers"] != "true" {
		return nil, image, errors.New("original Java Testcontainers labels are missing")
	}
	for key, value := range labels {
		if _, ok := value.(string); !ok || strings.HasPrefix(key, "reviewbench.") {
			return nil, image, errors.New("invalid or reserved Java service labels")
		}
	}
	labels[parentLabel], labels[profileLabel], labels[roleLabel], labels[leaseLabel] =
		p.ID, b.cfg.Profile, image.Role, p.Lease
	host["MemorySwap"], host["NetworkMode"], host["PublishAllPorts"] =
		host["Memory"], p.NetworkID, false
	host["Privileged"] = false
	host["PortBindings"] = map[string]any{
		port: []map[string]string{{"HostIp": p.Gateway, "HostPort": "0"}},
	}
	host["CapDrop"], host["SecurityOpt"], host["PidsLimit"] =
		[]string{"ALL"}, []string{"no-new-privileges"}, 128
	host["LogConfig"] = map[string]any{"Type": "json-file",
		"Config": map[string]string{"max-size": "8m", "max-file": "1"}}
	d["Image"], d["HostConfig"] = image.ID, host
	body, err := json.Marshal(d)
	return body, image, err
}
