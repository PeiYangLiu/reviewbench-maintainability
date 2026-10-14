"""Real original-Cargo/Redis loopback controls; no original benchmark episode."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from test_agentic_rust_standalone_service import fixture as base_fixture, ref
from agentic_runtime_migration import rust_standalone_service_entry as entry


def real_fixture(tmp_path, monkeypatch, version="0.1.0", private=True):
    registry_path = os.environ.get("LOCKED_WORKSPACE_TOOLCHAINS")
    service_path = os.environ.get("RELAY_REDIS_SERVICE_INPUTS")
    tools_path = os.environ.get("RELAY_PORTABLE_TOOL_SEED")
    if not registry_path or not service_path or not tools_path:
        pytest.skip("original installed Cargo, reconstructed Redis image and pinned portable tools required")
    registry = json.loads(Path(registry_path).read_text())
    compiler = Path(registry["1.73.0"]["root"])
    service = json.loads(Path(service_path).read_text())
    source, public, secret, session = base_fixture(tmp_path, monkeypatch, version, private)
    body = r'''use std::io::{BufRead, BufReader, Write};
use std::net::TcpStream;
#[test]
fn original_behavior() {
    let stream = TcpStream::connect("127.0.0.1:6379").unwrap();
    let mut conn = BufReader::new(stream);
    conn.get_mut().write_all(b"*1\r\n$4\r\nPING\r\n").unwrap();
    let mut line = String::new();
    conn.read_line(&mut line).unwrap();
    assert_eq!(line, "+PONG\r\n");
    conn.get_mut().write_all(b"*3\r\n$3\r\nSET\r\n$5\r\nbench\r\n$1\r\n1\r\n").unwrap();
    line.clear(); conn.read_line(&mut line).unwrap();
    assert_eq!(line, "+OK\r\n");
    conn.get_mut().write_all(b"*2\r\n$3\r\nGET\r\n$5\r\nbench\r\n").unwrap();
    line.clear(); conn.read_line(&mut line).unwrap(); assert_eq!(line, "$1\r\n");
    line.clear(); conn.read_line(&mut line).unwrap(); assert_eq!(line, "1\r\n");
    assert_eq!(production::value(), 1);
}
'''
    session[secret["test_path"]] = body
    if private:
        (source / secret["test_path"]).write_text(body)
    secret["session_files"] = [{"path": n, "sha256": entry.core.digest(data.encode())}
                               for n, data in session.items()]
    home = tmp_path / "host-cargo-home"
    home.mkdir()
    env = {**os.environ, "CARGO_HOME": str(home), "CARGO_NET_OFFLINE": "true",
           "RUSTC": str(compiler / "bin/rustc"), "RUSTUP_AUTO_INSTALL": "0"}
    for number in ("0.1.0", "0.2.0"):
        root = tmp_path / ("reference-" + number)
        shutil.copytree(source, root)
        for name, data in session.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(data)
        manifest = root / "production/Cargo.toml"
        manifest.write_text(manifest.read_text().replace('version="' + version + '"', 'version="' + number + '"'))
        for role in ("public", "private"):
            cwd = root if role == "public" else root / public["private_workdir"]
            (cwd / "Cargo.lock").unlink(missing_ok=True)
            command = [str(compiler / "bin/cargo"), "--offline", "generate-lockfile"]
            result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=30)
            assert result.returncode == 0, result.stderr
            record = next(r for r in public["locks"][role] if number in r["path"])
            Path(record["path"]).write_bytes((cwd / "Cargo.lock").read_bytes())
            record.update(ref(Path(record["path"])))
    (source / "Cargo.lock").write_bytes(Path(public["locks"]["public"][0]["path"]).read_bytes())
    public["public_seed_sha256"] = public["locks"]["public"][0]["sha256"]
    (tmp_path / "sources.toml").write_text("")
    public["source_config"] = ref(tmp_path / "sources.toml")
    public["cargo"], public["rustc"] = ref(compiler / "bin/cargo"), ref(compiler / "bin/rustc")
    for name in ("cargo", "rustc"):
        public[name]["path"] = "/opt/test-rust/bin/" + name
    for record in [public["source_config"], *[r for locks in public["locks"].values() for r in locks]]:
        record["path"] = "/fixtures/" + Path(record["path"]).relative_to(tmp_path).as_posix()
    secret["service_binaries"] = service["service_binaries"]
    # Same original readiness and cleanup sequence; only the fixture's Cargo
    # target/name differs from the actual benchmark's immutable assertion.
    source_plan = json.loads((Path(service_path).parent / "inputs/plan.json").read_text())
    original = source_plan["command"]
    start = original.index("cargo +1.73.0")
    end = original.index("; rc=$?", start)
    secret["original_shell"] = original[:start] + "cargo +1.73.0 " + " ".join(secret["test_command"]) + original[end:]
    secret["original_shell_sha256"] = entry.core.digest(secret["original_shell"].encode())
    tools = tmp_path / "tools"
    shutil.copytree(Path(tools_path), tools)
    origin = Path(entry.__file__).parent
    shutil.copy2(origin / "rust_image_entry.py", tools / "image_locks.py")
    shutil.copy2(origin / "rust_standalone_service_entry.py", tools / "standalone_service.py")
    (tmp_path / "public.json").write_text(json.dumps(public))
    (tmp_path / "private.json").write_text(json.dumps(secret))
    return source, public, secret, service["image"], compiler, tools


def execute(tmp_path, fixture, private=True, label="run"):
    source, public, secret, image, compiler, tools = fixture
    work = tmp_path / label
    work.mkdir(mode=0o777)
    work.chmod(0o777)
    command = [
        "docker", "run", "--rm", "--read-only", "--network", "none", "--cpus", "2", "--memory", "1g",
        "--pids-limit", "128", "--tmpfs", "/tmp:size=64m", "--user", str(os.getuid()) + ":" + str(os.getgid()),
        "--mount", "type=bind,src=" + str(tmp_path) + ",dst=/fixtures,readonly",
        "--mount", "type=bind,src=" + str(work) + ",dst=/work",
        "--mount", "type=bind,src=" + str(compiler) + ",dst=/opt/test-rust,readonly",
        "--env", "CARGO_TARGET_DIR=/work/target", "--env", "CARGO_BUILD_JOBS=2",
        "--env", "PATH=/opt/test-rust/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "--entrypoint", "/bin/bash", image, "-c",
        "mkdir -p /work/target && python3 /fixtures/tools/standalone_service.py --spec /fixtures/public.json "
        "--source /fixtures/candidate --build-root /work/build --task-id standalone-fixture"
        + (" --private /fixtures/private.json" if private else ""),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    lines = [line for line in result.stdout.splitlines() if line.startswith(entry.EVIDENCE_PREFIX)]
    assert len(lines) == 1, result.stdout[-1500:] + result.stderr[-1500:]
    return result, json.loads(lines[0][len(entry.EVIDENCE_PREFIX):])


@pytest.mark.parametrize("version", ["0.1.0", "0.2.0"])
@pytest.mark.parametrize("private", [False, True])
def test_real_full_candidate_roles_and_redis_lifecycle(tmp_path, monkeypatch, version, private):
    fixture = real_fixture(tmp_path, monkeypatch, version, private)
    before = entry.core.snapshot(fixture[0])
    process, row = execute(tmp_path, fixture, private)
    assert process.returncode == row["exit_status"] == 0
    assert row["source_before"] == row["source_after"] == before
    assert sorted(r["command_result"]["returncode"] for r in row["probes"]) == [0, 101]
    assert row["command_executions"] == 1
    if private:
        assert row["test_results"] == {"original_behavior": True}
        assert row["execution"]["command"][0:2] == ["/bin/bash", "-c"]
        assert "kill \"$redis_pid\"" in row["execution"]["command"][2]


def test_real_negative_behavior_is_not_retried(tmp_path, monkeypatch):
    fixture = real_fixture(tmp_path, monkeypatch)
    (fixture[0] / "production/src/lib.rs").write_text("pub fn value() -> u8 { 9 }\n")
    process, row = execute(tmp_path, fixture)
    assert process.returncode == row["exit_status"] == 101
    assert row["test_results"] == {"original_behavior": False} and row["command_executions"] == 1
    assert len(row["probes"]) == 2
    assert "test original_behavior ... FAILED" in row["execution"]["stdout"]


def test_real_new_helper_is_used_and_deletion_is_visible(tmp_path, monkeypatch):
    fixture = real_fixture(tmp_path, monkeypatch)
    source = fixture[0]
    (source / "production/src/lib.rs").write_text("mod helper; pub fn value() -> u8 { helper::value() }\n")
    helper = source / "production/src/helper.rs"
    helper.write_text("pub fn value() -> u8 { 1 }\n")
    first, row = execute(tmp_path, fixture, label="first")
    assert first.returncode == 0 and "production/src/helper.rs" in row["mirror_after"]
    helper.unlink()
    second, row = execute(tmp_path, fixture, label="second")
    assert second.returncode == 101 and "production/src/helper.rs" not in row["mirror_after"]
    assert "file not found for module `helper`" in row["execution"]["stderr"]
