package main

import (
	"encoding/json"
	"fmt"
	"os"
	"reflect"
	"strings"
	"testing"
)

func javaFixture() (*broker, *parent) {
	b, p := fixture()
	b.cfg.Client = javaClientProfile
	b.cfg.Profile = "mfscreener133-135-original-testcontainers-v1"
	b.cfg.Images = map[string]imageSpec{}
	for tag, value := range javaServices {
		b.cfg.Images[tag] = imageSpec{
			ID: "sha256:" + strings.Repeat("1", 64), Role: value.role, Port: value.port,
		}
	}
	return b, p
}

func javaRequest(tag string) map[string]any {
	service := javaServices[tag]
	port := fmt.Sprintf("%d/tcp", service.port)
	row := map[string]any{
		"Image": tag, "Env": []any{}, "Cmd": nil, "WorkingDir": "",
		"Hostname": nil, "AttachStdout": true, "AttachStderr": true,
		"ExposedPorts": map[string]any{port: map[string]any{}},
		"Labels": map[string]any{
			"org.testcontainers": "true", "org.testcontainers.sessionId": "original-java-session",
		},
		"HostConfig": map[string]any{
			"NetworkMode": "default", "Privileged": false, "CapAdd": []any{},
			"Binds": nil, "Memory": float64(0),
			"PortBindings": map[string]any{port: []any{map[string]any{"HostPort": "", "HostIp": ""}}},
		},
	}
	if service.role == "postgres" {
		row["Cmd"] = []any{"postgres", "-c", "fsync=off"}
		row["Env"] = []any{"POSTGRES_DB=test", "POSTGRES_USER=test", "POSTGRES_PASSWORD=test"}
	}
	if service.role == "ryuk" {
		row["HostConfig"].(map[string]any)["Binds"] =
			[]any{"/var/run/docker.sock:/var/run/docker.sock:rw"}
		row["HostConfig"].(map[string]any)["Privileged"] = true
	}
	return row
}

func TestJavaFourOriginalServicesKeepTheirActualCommandsAndEnvironment(t *testing.T) {
	for tag := range javaServices {
		t.Run(tag, func(t *testing.T) {
			b, p := javaFixture()
			input := javaRequest(tag)
			output, image, err := plan(t, b, p, input)
			if err != nil {
				t.Fatal(err)
			}
			if output["Image"] != image.ID ||
				!reflect.DeepEqual(input["Cmd"], output["Cmd"]) ||
				!reflect.DeepEqual(input["Env"], output["Env"]) {
				t.Fatal("original library command/environment changed", output)
			}
			host := output["HostConfig"].(map[string]any)
			if host["NetworkMode"] != p.NetworkID || host["Memory"] != host["MemorySwap"] ||
				host["PublishAllPorts"] != false || host["PidsLimit"] != float64(128) ||
				host["Privileged"] != false {
				t.Fatal("unbounded service", host)
			}
			binds, _ := host["Binds"].([]any)
			if image.Role == "ryuk" {
				if len(binds) != 1 || binds[0] != "/tmp/fixture/aaaaaaaaaaaaaaaa:/var/run:ro" {
					t.Fatal("Ryuk socket isolation changed", binds)
				}
			} else if len(binds) != 0 {
				t.Fatal("service has an external path", binds)
			}
		})
	}
}

func TestJavaProfilesRejectAnotherLibraryOrImageSet(t *testing.T) {
	b, _ := javaFixture()
	if err := b.cfg.validateJavaImages(); err != nil {
		t.Fatal(err)
	}
	delete(b.cfg.Images, "testcontainers/ryuk:0.6.0")
	b.cfg.Images["testcontainers/ryuk:0.7.0"] = imageSpec{
		ID: "sha256:" + strings.Repeat("2", 64), Role: "ryuk", Port: 8080}
	if b.cfg.validateJavaImages() == nil {
		t.Fatal("another reaper was admitted")
	}
}

func TestJavaResourceServiceAndOwnershipChangesAreRejected(t *testing.T) {
	cases := map[string]func(map[string]any){
		"image":       func(d map[string]any) { d["Image"] = "postgres:latest" },
		"command":     func(d map[string]any) { d["Cmd"] = []any{"sh", "-c", "true"} },
		"credentials": func(d map[string]any) { d["Env"] = []any{"POSTGRES_DB=another"} },
		"duplicate-env": func(d map[string]any) {
			d["Env"] = append(d["Env"].([]any), "POSTGRES_DB=test")
		},
		"host":       func(d map[string]any) { d["HostConfig"].(map[string]any)["NetworkMode"] = "host" },
		"privileged": func(d map[string]any) { d["HostConfig"].(map[string]any)["Privileged"] = true },
		"memory":     func(d map[string]any) { d["HostConfig"].(map[string]any)["Memory"] = float64(1 << 30) },
		"host-bind": func(d map[string]any) {
			d["HostConfig"].(map[string]any)["Binds"] = []any{"/:/host:ro"}
		},
		"fixed-port": func(d map[string]any) {
			d["HostConfig"].(map[string]any)["PortBindings"].(map[string]any)["5432/tcp"] =
				[]any{map[string]any{"HostPort": "5432", "HostIp": ""}}
		},
		"owner-label": func(d map[string]any) {
			d["Labels"].(map[string]any)[parentLabel] = "other"
		},
		"unknown-field": func(d map[string]any) { d["unknown"] = nil },
		"entrypoint":    func(d map[string]any) { d["Entrypoint"] = []any{"other"} },
	}
	for name, change := range cases {
		t.Run(name, func(t *testing.T) {
			b, p := javaFixture()
			input := javaRequest("postgres:16-alpine")
			change(input)
			if _, _, err := plan(t, b, p, input); err == nil {
				t.Fatal("changed Java service accepted")
			}
		})
	}
}

func TestJavaRyukRejectsOtherMountsAndOtherServiceOverrides(t *testing.T) {
	for _, tag := range []string{"testcontainers/ryuk:0.6.0", "redis:7.2.4-alpine", "openzipkin/zipkin:latest"} {
		b, p := javaFixture()
		d := javaRequest(tag)
		d["Env"] = []any{"ALTERED=true"}
		if _, _, err := plan(t, b, p, d); err == nil {
			t.Fatal("service environment override accepted", tag)
		}
	}
	b, p := javaFixture()
	d := javaRequest("testcontainers/ryuk:0.6.0")
	d["HostConfig"].(map[string]any)["Binds"] = []any{"/tmp/other.sock:/var/run/docker.sock:rw"}
	if _, _, err := plan(t, b, p, d); err == nil {
		t.Fatal("another host socket accepted")
	}
	d = javaRequest("testcontainers/ryuk:0.6.0")
	d["HostConfig"].(map[string]any)["Privileged"] = false
	if _, _, err := plan(t, b, p, d); err == nil {
		t.Fatal("a changed original Java reaper declaration was accepted")
	}
}

func TestNodeProfileDoesNotImplicitlyAcceptJavaServices(t *testing.T) {
	b, p := fixture()
	for tag := range javaServices {
		if _, _, err := plan(t, b, p, javaRequest(tag)); err == nil {
			t.Fatal("Java route selected for original Node profile", tag)
		}
	}
}

func TestJavaImageListIsNotShadowedBySingleImageInspection(t *testing.T) {
	if isImageInspection("/images/json") || isImageInspection("/containers/json") {
		t.Fatal("image list was misclassified as image inspection")
	}
	for _, path := range []string{"/images/postgres:16-alpine/json",
		"/images/testcontainers/ryuk:0.6.0/json", "/images/sha256:" + strings.Repeat("a", 64) + "/json"} {
		if !isImageInspection(path) {
			t.Fatal("original image inspection was lost", path)
		}
	}
}

func TestActualJava197ReaperRequestRetainsOnlyEmptyLegacyFields(t *testing.T) {
	raw, err := os.ReadFile("testdata/java197-ryuk-create.json")
	if err != nil {
		t.Fatal(err)
	}
	b, p := javaFixture()
	if _, _, err = b.createPlan(p, raw); err != nil {
		t.Fatal("original captured Java request rejected", err)
	}
	for _, key := range []string{"PortSpecs", "ArgsEscaped", "NetworkingConfig"} {
		var body map[string]any
		if err := json.Unmarshal(raw, &body); err != nil {
			t.Fatal(err)
		}
		body[key] = true
		if _, _, err := plan(t, b, p, body); err == nil {
			t.Fatal("nonempty legacy field accepted", key)
		}
	}
}
