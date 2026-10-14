"""Real pinned-Cargo TOOL fixtures, not the original wash task or its proof."""
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import sys

import pytest

from test_agentic_rust_wash_compat import BLOCK, LEXICAL_PREFIXES, fixture_source, macro_container

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_wash_compat_entry as entry


@pytest.fixture
def cargo165(tmp_path, monkeypatch):
    value = os.environ.get("WASH_TEST_CARGO")
    if not value or not Path(value).is_file():
        pytest.skip("explicit retained Cargo1.65.0 is required; never install a toolchain")
    cargo = Path(value)
    version = subprocess.check_output([str(cargo), "--version"], text=True).strip()
    assert version.startswith("cargo 1.65.0 ")
    monkeypatch.setenv("PATH", str(cargo.parent) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("RUSTC", str(cargo.parent / "rustc"))
    monkeypatch.setenv("CARGO_HOME", str(tmp_path / "cargo-home"))
    monkeypatch.setenv("CARGO_TARGET_DIR", str(tmp_path / "outside-source-target"))
    monkeypatch.setenv("CARGO_NET_OFFLINE", "true")
    monkeypatch.setenv("CARGO_BUILD_JOBS", "2")
    monkeypatch.setenv("CARGO_TERM_COLOR", "never")
    monkeypatch.setenv("RUSTFLAGS", "")
    monkeypatch.setenv("CARGO_PROFILE_DEV_DEBUG", "0")
    monkeypatch.setenv("CARGO_PROFILE_TEST_DEBUG", "0")
    return cargo


def save(request, evidence):
    root = os.environ.get("WASH_FIXTURE_EVIDENCE")
    if not root:
        return
    directory = Path(root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (request.node.name.replace("/", "_") + ".json")
    value = {
        "kind": "small dependency-free TOOL fixture; not original wash source/tests/calibration",
        "original_runtime_replays": 0, "model_calls": 0, "evidence": evidence,
    }
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def setup_lock(cargo, source):
    command = [str(cargo), "generate-lockfile", "--offline"]
    result = subprocess.run(command, cwd=source, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return {"command": command, "cwd": str(source), "returncode": result.returncode,
            "stdout": result.stdout, "stderr": result.stderr,
            "lock_sha256": entry.guard.digest((source / "Cargo.lock").read_bytes())}


@pytest.mark.parametrize("style,expected", [
    ("legacy", ["as-is"]), ("moved", ["with-import"]), ("both", ["as-is", "with-import"]),
])
def test_real_pinned_cargo_compiles_complete_fixture_and_runs_all_forms(
        tmp_path, cargo165, request, style, expected):
    source = tmp_path / "source"
    spec = fixture_source(source, style)
    setup = setup_lock(cargo165, source)
    before = entry.candidate_snapshot(source)
    try:
        evidence = entry.select(spec, source, tmp_path / "forms")
    except entry.SelectionError as exc:
        save(request, {"setup": setup, "error": str(exc), "records": exc.evidence})
        raise
    save(request, {"setup": setup, "records": evidence})
    assert evidence["selected"] == expected
    assert all(evidence["results"].values())
    assert len(evidence["tests"]) == len(expected)
    assert before == entry.candidate_snapshot(source)


def test_real_changed_multifile_production_fails_original_fixture_assertion(tmp_path, cargo165, request):
    source = tmp_path / "source"
    spec = fixture_source(source, "both")
    (source / "src/behavior.rs").write_text("pub fn value() -> u8 { 99 }\n")
    setup_lock(cargo165, source)
    evidence = entry.select(spec, source, tmp_path / "forms")
    save(request, evidence)
    assert evidence["selected"] == ["as-is", "with-import"]
    assert len(evidence["tests"]) == 2
    assert not any(evidence["results"].values())
    assert all(row["returncode"] != 0 for row in evidence["tests"])
    assert all("assertion failed" in row["stdout"] + row["stderr"] for row in evidence["tests"])


def test_real_unrelated_production_error_never_discards_an_import_form(tmp_path, cargo165, request):
    source = tmp_path / "source"
    spec = fixture_source(source, "moved")
    (source / "src/behavior.rs").write_text('compile_error!("unrelated production failure");\n')
    setup_lock(cargo165, source)
    with pytest.raises(entry.SelectionError) as raised:
        entry.select(spec, source, tmp_path / "forms")
    save(request, {"error": str(raised.value), "records": raised.value.evidence})
    assert len(raised.value.evidence["probes"]) == 1
    assert raised.value.evidence["tests"] == []


def test_real_import_baseline_modified_independent_rollback(tmp_path, cargo165, request):
    original = tmp_path / "original"
    spec = fixture_source(original, "moved")
    setup_lock(cargo165, original)
    before = entry.candidate_snapshot(original)
    rows = []
    for name in ("BASELINE", "MODIFIED", "ROLLBACK"):
        source = tmp_path / name.lower()
        shutil.copytree(original, source)
        if name == "MODIFIED":
            file = source / entry.PATH
            file.write_bytes(entry.transformed(file.read_bytes(), spec, "with-import"))
        command = [str(cargo165), "--offline", "test", "--locked", "--bin", "wash",
                   "test_ctl_comprehensive", "--", "--nocapture"]
        result = subprocess.run(command, cwd=source,
            env=dict(os.environ, CARGO_TARGET_DIR=str(tmp_path / (name.lower() + "-fresh-target"))),
            capture_output=True, text=True, timeout=60)
        rows.append({"stage": name, "command": command, "cwd": str(source),
                     "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr,
                     "source_fingerprint": entry.candidate_snapshot(source)})
    save(request, rows)
    assert [row["returncode"] for row in rows] == [101, 0, 101]
    assert rows[0]["source_fingerprint"] == rows[2]["source_fingerprint"] == before
    assert "cannot find struct" in rows[0]["stderr"] and "LinkPutCommand" in rows[0]["stderr"]
    assert "test ctl::test::test_ctl_comprehensive ... ok" in rows[1]["stdout"]
    assert before == entry.candidate_snapshot(original)


@pytest.mark.parametrize("opening,closing", [(b"(", b")"), (b"[", b"]")])
def test_real_macro_can_discard_small_tool_test_but_guard_stops_before_any_command(
        tmp_path, cargo165, request, opening, closing):
    source = tmp_path / "source"
    spec = fixture_source(source, "moved")
    path = source / entry.PATH
    path.write_bytes(macro_container(path.read_bytes(), opening, closing))
    assert path.read_bytes().count(BLOCK) == 1
    setup = setup_lock(cargo165, source)
    before = entry.candidate_snapshot(source)
    # Demonstrate only the small TOOL fixture's token erasure, not an original
    # wash task bypass. The real adapter must reject before even Cargo metadata.
    command = [str(cargo165), "--offline", "test", "--locked", "--bin", "wash",
               "test_ctl_comprehensive", "--", "--nocapture"]
    raw = subprocess.run(command, cwd=source, capture_output=True, text=True, timeout=60)
    calls = []
    def forbidden(*args):
        calls.append(args[0])
        pytest.fail("hidden test reached a Cargo command through the adapter")
    with pytest.raises(entry.SelectionError, match="enclosing/conditional") as raised:
        entry.select(spec, source, tmp_path / "forms", run=forbidden)
    save(request, {"setup": setup, "raw_small_tool_only": {
        "command": command, "cwd": str(source), "returncode": raw.returncode,
        "stdout": raw.stdout, "stderr": raw.stderr,
    }, "guard_error": str(raised.value), "adapter_commands": calls,
        "source_unchanged": entry.candidate_snapshot(source) == before})
    assert raw.returncode == 0, raw.stderr
    assert "running 0 tests" in raw.stdout
    assert "test ctl::test::test_ctl_comprehensive ... ok" not in raw.stdout
    assert calls == []
    assert not (tmp_path / "forms").exists()
    assert entry.candidate_snapshot(source) == before


def test_real_matching_literals_comments_attributes_and_nested_delimiters_keep_current_api(
        tmp_path, cargo165, request):
    source = tmp_path / "source"
    spec = fixture_source(source, "moved")
    path = source / entry.PATH
    path.write_bytes(b"".join(LEXICAL_PREFIXES) + path.read_bytes())
    setup = setup_lock(cargo165, source)
    before = entry.candidate_snapshot(source)
    evidence = entry.select(spec, source, tmp_path / "forms")
    save(request, {"setup": setup, "records": evidence})
    assert evidence["selected"] == ["with-import"]
    assert len(evidence["tests"]) == 1
    assert all(evidence["results"].values())
    assert before == entry.candidate_snapshot(source)


@pytest.mark.parametrize("mode", ["public", "grading"])
@pytest.mark.parametrize("signal_number", [6, 9, 11, 15])
def test_real_compiler_child_signal_aborts_after_first_probe(
        tmp_path, monkeypatch, cargo165, request, mode, signal_number):
    source = tmp_path / "source"
    spec = fixture_source(source, "moved", mode)
    setup_lock(cargo165, source)
    rustc = os.environ["RUSTC"]
    events = tmp_path / "rustc-events.jsonl"
    wrapper = tmp_path / "rustc-wrapper"
    wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "import json,os,resource,subprocess,sys\n"
        "resource.setrlimit(resource.RLIMIT_CORE,(0,0))\n"
        "args=sys.argv[1:]\n"
        f"compiler={rustc!r}\n"
        "target=args[args.index('--crate-name')+1] if '--crate-name' in args else None\n"
        "if target != 'wash': os.execv(compiler,[compiler,*args])\n"
        "result=subprocess.run([compiler,*args],stdout=subprocess.PIPE,stderr=subprocess.PIPE)\n"
        "sys.stdout.buffer.write(result.stdout);sys.stdout.buffer.flush()\n"
        "sys.stderr.buffer.write(result.stderr);sys.stderr.buffer.flush()\n"
        f"with open({str(events)!r},'a') as f:\n"
        f" f.write(json.dumps({{'signal':{signal_number},'original_rustc_exit':result.returncode,"
        "'forwarded_stderr':result.stderr.decode(errors='replace')})+'\\n')\n"
        f"os.kill(os.getpid(),{signal_number})\n"
    )
    compile(wrapper.read_text(), str(wrapper), "exec")
    wrapper.chmod(0o755)
    monkeypatch.setenv("RUSTC", str(wrapper))
    with pytest.raises(entry.SelectionError) as raised:
        entry.select(spec, source, tmp_path / "forms")
    rows = [json.loads(line) for line in events.read_text().splitlines()]
    save(request, {"mode": mode, "signal": signal_number, "rustc_child_events": rows,
                   "error": str(raised.value), "records": raised.value.evidence})
    assert len(rows) == 1
    assert len(raised.value.evidence["probes"]) == 1
    assert raised.value.evidence["tests"] == []
    assert "infrastructure failure" in str(raised.value)
    if mode == "grading":
        assert '"code":"E0422"' in rows[0]["forwarded_stderr"] or '"code": "E0422"' in rows[0]["forwarded_stderr"]
