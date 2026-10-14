"""Font45 source-aware runtime fixtures; no original Font task is executed."""
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import tomli_w

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_first_resolution_entry as entry
from agentic_runtime_migration.rust_vendor import source_configuration
from test_agentic_rust_first_resolution_runtime import record

HARNESS_BYTES = b'''use punchcut::font::FontRef;
use punchcut::Context;

#[test]
fn public_scaler_recognizes_real_truetype_outlines() {
    let font = FontRef::new(read_fonts::test_data::test_fonts::VAZIRMATN_VAR).unwrap();
    let mut context = Context::new();
    let scaler = context
        .new_scaler()
        .font_id(Some(7))
        .size(-16.0)
        .variations([("wght", 400.0)])
        .build(&font);

    assert!(scaler.has_outlines());
}
'''
MEMBERS = ("read-fonts", "font-types", "font-codegen", "write-fonts", "otexplorer", "punchcut")
GIT_SOURCE = "git+https://example.invalid/font-tool-dependency#0123456789abcdef0123456789abcdef01234567"


def make_lock(version="0.0.4", external=()):
    rows = []
    for name in MEMBERS:
        package = {"name": name, "version": version if name == "read-fonts" else "0.1.0"}
        if name == "punchcut":
            package["dependencies"] = ["read-fonts", *[p["name"] for p in external]]
        rows.append(package)
    rows.extend(external)
    return b"version = 4\n" + b"".join(
        ("\n[[package]]\n" + "".join(k + "=" + json.dumps(v) + "\n" for k, v in p.items())).encode()
        for p in rows)


def create_source(root, version="0.0.4"):
    root.mkdir()
    (root / "Cargo.toml").write_text("[workspace]\nmembers=" + json.dumps(list(MEMBERS)) +
                                    '\ndefault-members=["otexplorer"]\n')
    for name in MEMBERS:
        (root / name / "src").mkdir(parents=True)
        (root / name / "Cargo.toml").write_text(
            '[package]\nname=' + json.dumps(name) + '\nversion=' +
            json.dumps(version if name == "read-fonts" else "0.1.0") + '\nedition="2021"\n')
        (root / name / "src/lib.rs").write_text("pub fn current() -> bool { true }\n")
    with (root / "punchcut/Cargo.toml").open("a") as out:
        out.write('[dependencies]\nrenamed_read={package="read-fonts",path="../read-fonts"}\n')
    (root / "punchcut/tests").mkdir()
    assert entry.digest(HARNESS_BYTES) == entry.FONT_HARNESS["sha256"]
    (root / entry.FONT_HARNESS["path"]).write_bytes(HARNESS_BYTES)


def write_configuration(f, sources):
    data, mapping = source_configuration(sources, target=str(f.vendor))
    document = entry.tomllib.loads(data)
    document["net"] = {"offline": True}
    f.config.write_text(tomli_w.dumps(document))
    f.spec["source_config_sha256"] = entry.file_hash(f.config)
    f.spec["vendor_sources"] = mapping
    for name in mapping.values():
        (f.vendor / name).mkdir(parents=True, exist_ok=True)


@pytest.fixture
def font(tmp_path, monkeypatch):
    root = tmp_path / "candidate"
    create_source(root)
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("cargo", "rustc"):
        (tools / name).write_text("authenticated data-fixture tool: " + name)
    monkeypatch.setattr(entry, "REAL_CARGO", str(tools / "cargo"))
    monkeypatch.setattr(entry, "REAL_RUSTC", str(tools / "rustc"))
    config = tmp_path / "sources.toml"
    monkeypatch.setattr(entry, "FONT_SOURCE_CONFIG", str(config))
    defaults, assets = [], {}
    for i, version in enumerate(("0.0.4", "0.0.5")):
        path = tmp_path / ("default-" + version + ".lock")
        path.write_bytes(make_lock(version))
        target = "/opt/reviewbench-assets/rust-new-lock-" + str(i)
        defaults.append({"selector_package": "read-fonts", "selector_version": version,
                         "asset_target": target, "sha256": entry.file_hash(path)})
        assets[target] = path
    spec = {"schema_version": entry.SCHEMA, "task_id": entry.FONT_TASK, "profile": entry.FONT_PROFILE,
            "toolchain": "1.93.0", "cargo_sha256": entry.file_hash(tools / "cargo"),
            "rustc_sha256": entry.file_hash(tools / "rustc"), "cpus": 4,
            "max_log_bytes": 33554432, "metadata_timeout_seconds": 120, "receipt_sha256": "f" * 64,
            "source_config_sha256": "", "vendor_sources": {}, "defaults": defaults}
    private = {"schema_version": entry.PRIVATE_SCHEMA, "task_id": entry.FONT_TASK,
               "command": list(entry.FONT_COMMAND), "harness": dict(entry.FONT_HARNESS),
               "expected_tests": list(entry.FONT_EXPECTED)}
    f = SimpleNamespace(source=root, build=tmp_path / "build", vendor=tmp_path / "vendor",
                        config=config, spec=spec, private=private, locks=assets)
    write_configuration(f, [])
    return f


def graph_for(root, f):
    packages = entry.parse_lock((root / "Cargo.lock").read_bytes(), f.spec)
    rows, keys = [], {}
    for i, key in enumerate(packages):
        keys[key] = "package-" + str(i)
        if key[2] is None:
            manifests = [p for p in root.rglob("Cargo.toml")
                         if entry.tomllib.loads(p.read_text()).get("package", {}).get("name") == key[0]]
            assert len(manifests) == 1
            path = manifests[0]
        else:
            path = f.vendor / f.spec["vendor_sources"][key[2]] / (key[0] + "-" + key[1]) / "Cargo.toml"
        targets = [{"kind": ["lib"], "name": key[0].replace("-", "_"),
                    "src_path": str(path.parent / "src/lib.rs"), "test": True}]
        if key[0] == "punchcut":
            targets.append({"kind": ["test"], "name": "oracle_session",
                            "src_path": str(root / entry.FONT_HARNESS["path"]), "test": True})
        rows.append({"id": keys[key], "name": key[0], "version": key[1], "source": key[2],
                     "manifest_path": str(path), "targets": targets, "features": {}})
    nodes = []
    for key, package in packages.items():
        deps = entry.lock_edges(packages, package)
        nodes.append({"id": keys[key], "features": [], "dependencies": [keys[d] for d in deps],
                      "deps": [{"name": d[0].replace("-", "_"), "pkg": keys[d],
                                "dep_kinds": [{"kind": None, "target": None}]} for d in deps]})
    local = [r["id"] for r in rows if r["source"] is None]
    default = next(r["id"] for r in rows if r["name"] == "otexplorer")
    return {"version": 1, "workspace_root": str(root), "workspace_members": local,
            "workspace_default_members": [default], "packages": rows,
            "resolve": {"root": None, "nodes": nodes}}


def execute(f, *, public=False, mutate=None, code=0):
    calls = []
    def run(command, root, environment, logs, name, deadline, limit, **kwargs):
        calls.append(name)
        assert "--locked" in command and "--offline" in command and "--all-targets" not in command
        if name == "metadata":
            assert "--all-features" in command and "--no-deps" not in command and "-p" not in command
            r = record(stdout=json.dumps(graph_for(root, f)), stderr=entry.FONT_RESOLVER_WARNING)
        else:
            assert command[command.index("-p") + 1] == "punchcut"
            r = record(code, "test " + entry.FONT_EXPECTED[0] + " ... " + ("ok" if code == 0 else "FAILED")
                       + "\ntest result: " + ("ok." if code == 0 else "FAILED.") + "\n",
                       stderr=entry.FONT_RESOLVER_WARNING)
        if mutate:
            mutate(name, root, r)
        return r
    p = entry.profile_for({"task_id": entry.FONT_TASK, "profile": entry.FONT_PROFILE})
    command = ["cargo", *p["prefix"], *(p["public"] if public else p["command"])]
    result = entry.execute(f.spec, f.source, f.build, command, None if public else f.private,
                           run=run, lock_sources=f.locks, vendor=f.vendor, source_config=f.config)
    return *result, calls


@pytest.mark.parametrize("version", ["0.0.4", "0.0.5"])
@pytest.mark.parametrize("layout", ["unchanged", "moved", "glob", "attributes"])
def test_current_complete_virtual_workspace_selects_by_read_fonts_identity(font, version, layout, monkeypatch):
    f = font
    manifest = f.source / "read-fonts/Cargo.toml"
    manifest.write_text(manifest.read_text().replace("0.0.4", version))
    if layout in {"moved", "glob"}:
        (f.source / "members").mkdir()
        (f.source / "read-fonts").rename(f.source / "members/reader")
        path = f.source / "Cargo.toml"
        path.write_text(path.read_text().replace('"read-fonts"', '"./members/reader/"' if layout == "moved" else '"members/*"'))
        path = f.source / "punchcut/Cargo.toml"
        path.write_text(path.read_text().replace("../read-fonts", "../members/reader"))
    elif layout == "attributes":
        manifest.write_text(manifest.read_text() + '[package.metadata.current]\nnote="harmless candidate data"\n')
    monkeypatch.setenv("ORACLE_VARIANT", "ignored-opposite-label")
    old = copy.deepcopy((entry.TASK, entry.PROFILE, entry.PREFIX, entry.HARNESS, entry.EXPECTED))
    before = entry.snapshot(f.source)
    code, report, calls = execute(f)
    assert code == 0, report
    assert calls == ["metadata", "cargo"] and report["task_id"] == entry.FONT_TASK
    assert report["mirror"]["selection"]["selector_version"] == version
    assert report["metadata"]["stderr"] == entry.FONT_RESOLVER_WARNING
    assert json.loads(report["metadata"]["stdout"])["resolve"]["root"] is None
    assert entry.snapshot(f.source) == before and not (f.source / "Cargo.lock").exists()
    assert (entry.TASK, entry.PROFILE, entry.PREFIX, entry.HARNESS, entry.EXPECTED) == old


def test_authored_current_lock_bypasses_versions_without_replacing_bytes(font, monkeypatch):
    f = font
    path = f.source / "read-fonts/Cargo.toml"
    path.write_text(path.read_text().replace("0.0.4", "0.0.9"))
    own = make_lock("0.0.9") + b"\n# current authored graph/serialization\n"
    (f.source / "Cargo.lock").write_bytes(own)
    monkeypatch.setattr(entry, "select_workspace_default", lambda *a: pytest.fail("authored lock was selected"))
    before = entry.snapshot(f.source)
    code, report, calls = execute(f)
    assert code == 0 and report["mirror"]["selected_default"] is None
    assert (f.build / "source/Cargo.lock").read_bytes() == own and entry.snapshot(f.source) == before
    assert calls == ["metadata", "cargo"]


@pytest.mark.parametrize("problem", ["missing", "both", "version", "escape", "symlink", "asset"])
def test_bad_missing_lock_selector_aborts_before_metadata(font, problem):
    f = font
    if problem == "missing":
        p = f.source / "read-fonts/Cargo.toml"
        p.write_text(p.read_text().replace('name="read-fonts"', 'name="unmatched"'))
    elif problem == "both":
        (f.source / "other").mkdir()
        (f.source / "other/Cargo.toml").write_text('[package]\nname="read-fonts"\nversion="0.0.5"\n')
        p = f.source / "Cargo.toml"
        p.write_text(p.read_text().replace('"read-fonts"', '"read-fonts","other"'))
    elif problem == "version":
        p = f.source / "read-fonts/Cargo.toml"
        p.write_text(p.read_text().replace("0.0.4", "0.0.9"))
    elif problem == "escape":
        p = f.source / "Cargo.toml"
        p.write_text(p.read_text().replace('"read-fonts"', '"../escape"'))
    elif problem == "symlink":
        p = f.source / "read-fonts/Cargo.toml"
        p.unlink()
        p.symlink_to("../font-types/Cargo.toml")
    else:
        next(iter(f.locks.values())).write_text("changed authenticated default")
    code, report, calls = execute(f)
    assert code == 2 and calls == [] and report["status"] == "rejected"


def add_external(f, *, git=True):
    source = GIT_SOURCE if git else entry.REGISTRY
    external = {"name": "fixture-external", "version": "0.1.0", "source": source}
    if not git:
        external["checksum"] = "a" * 64
    write_configuration(f, [source])
    folder = f.vendor / f.spec["vendor_sources"][source] / "fixture-external-0.1.0"
    (folder / "src").mkdir(parents=True)
    (folder / "Cargo.toml").write_text('[package]\nname="fixture-external"\nversion="0.1.0"\n')
    (folder / "src/lib.rs").write_text("// provided public source bytes\n")
    checksums = {"package": None if git else "a" * 64,
                 "files": {str(p.relative_to(folder)): entry.file_hash(p) for p in folder.rglob("*") if p.is_file()}}
    (folder / ".cargo-checksum.json").write_text(json.dumps(checksums))
    (f.source / "Cargo.lock").write_bytes(make_lock(external=[external]))
    return source, folder


@pytest.mark.parametrize("git", [False, True])
def test_source_aware_registry_and_pinned_git_use_same_shared_validation(font, git):
    source, folder = add_external(font, git=git)
    code, report, calls = execute(font)
    assert code == 0, report
    assert calls == ["metadata", "cargo"]
    assert report["source_configuration"]["vendor_sources"][source] == "source-" + entry.digest(source.encode())
    assert (folder / "src/lib.rs").read_text() == "// provided public source bytes\n"
    if git:
        with pytest.raises(entry.OverlayError):
            entry.parse_lock((font.source / "Cargo.lock").read_bytes())  # Prom default stays registry-only.


@pytest.mark.parametrize("problem", [
    "unpinned", "extra_checksum", "unknown_source", "config_hash", "config_mapping",
    "config_extra", "config_escape", "config_offline", "config_missing_offline", "config_offline_integer",
    "vendor_checksum", "vendor_file", "vendor_extra",
    "vendor_missing", "vendor_escape", "metadata_source", "metadata_manifest",
])
def test_git_config_vendor_and_source_errors_never_reach_sut(font, problem):
    f = font
    source, folder = add_external(f)
    if problem in {"unpinned", "unknown_source"}:
        p = f.source / "Cargo.lock"
        p.write_text(p.read_text().replace(source, source.split("#")[0] if problem == "unpinned"
                                           else source.replace("01234567", "11234567")))
    elif problem == "extra_checksum":
        p = f.source / "Cargo.lock"
        p.write_text(p.read_text() + 'checksum="' + "0" * 64 + '"\n')
    elif problem.startswith("config"):
        if problem == "config_hash":
            f.spec["source_config_sha256"] = "0" * 64
        elif problem == "config_mapping":
            f.spec["vendor_sources"][source] = "not-source-hash"
        else:
            document = entry.tomllib.loads(f.config.read_text())
            if problem == "config_extra":
                document["net"]["retry"] = 2
            elif problem == "config_offline":
                document["net"]["offline"] = False
            elif problem == "config_offline_integer":
                document["net"]["offline"] = 1
            elif problem == "config_missing_offline":
                document.pop("net")
            text = tomli_w.dumps(document)
            f.config.write_text(text.replace(str(f.vendor), "/outside") if problem == "config_escape" else text)
            f.spec["source_config_sha256"] = entry.file_hash(f.config)
    elif problem == "vendor_checksum":
        p = folder / ".cargo-checksum.json"
        d = json.loads(p.read_text()); d["package"] = "a" * 64; p.write_text(json.dumps(d))
    elif problem == "vendor_file":
        (folder / "src/lib.rs").write_text("changed implementation")
    elif problem == "vendor_extra":
        (folder / "extra.rs").write_text("unbound file")
    elif problem == "vendor_missing":
        (folder / "src/lib.rs").unlink()
    elif problem == "vendor_escape":
        p = folder / "src/lib.rs"; p.unlink(); p.symlink_to(f.source / "punchcut/src/lib.rs")
    def mutate(phase, root, record):
        if problem.startswith("metadata") and phase == "metadata":
            g = json.loads(record["stdout"]); p = g["packages"][-1]
            if problem == "metadata_source":
                p["source"] = source.replace("01234567", "11234567")
            else:
                p["manifest_path"] = str(root / "punchcut/Cargo.toml")
            record["stdout"] = json.dumps(g)
    code, report, calls = execute(f, mutate=mutate)
    assert code == 2 and calls == (["metadata"] if problem.startswith("metadata") else [])


@pytest.mark.parametrize("problem", ["node", "edge", "feature", "member", "local_path", "redirect",
                                  "disabled", "wrong_package", "prefix", "suffix", "truncated",
                                  "signal", "error", "unknown_warning", "partial_warning", "duplicate_warning",
                                  "source_mutation"])
def test_complete_graph_private_package_and_diagnostic_guards(font, problem):
    def mutate(phase, root, r):
        if phase != "metadata":
            pytest.fail("invalid graph reached SUT")
        g = json.loads(r["stdout"])
        punch = next(p for p in g["packages"] if p["name"] == "punchcut")
        node = next(n for n in g["resolve"]["nodes"] if n["id"] == punch["id"])
        if problem == "node":
            g["resolve"]["nodes"].pop()
        elif problem == "edge":
            node["dependencies"] = []; node["deps"] = []
        elif problem == "feature":
            node["features"] = ["undeclared"]
        elif problem == "member":
            g["workspace_members"].remove(punch["id"])
        elif problem == "local_path":
            punch["manifest_path"] = "/outside/Cargo.toml"
        elif problem == "redirect":
            punch["targets"][1]["src_path"] = str(root / "otexplorer/src/lib.rs")
        elif problem == "disabled":
            punch["targets"][1]["test"] = False
        elif problem == "wrong_package":
            punch["name"] = "not-punchcut"
        elif problem == "truncated":
            r["stdout_total_bytes"] += 1
        elif problem == "signal":
            r["stderr"] += "process terminated by SIGSEGV\n"
        elif problem == "error":
            r["returncode"] = 101
        elif problem == "unknown_warning":
            r["stderr"] += "warning: unaccounted current process message\n"
        elif problem == "partial_warning":
            r["stderr"] = entry.FONT_RESOLVER_WARNING.splitlines()[0] + "\n"
        elif problem == "duplicate_warning":
            r["stderr"] += entry.FONT_RESOLVER_WARNING
        elif problem == "source_mutation":
            (root / "punchcut/src/lib.rs").write_text("// changed by metadata child")
        r["stdout"] = json.dumps(g)
        if problem == "prefix":
            r["stdout"] = "SIGINT\n" + r["stdout"]
        elif problem == "suffix":
            r["stdout"] += "\n{}"
    code, _, calls = execute(font, mutate=mutate)
    assert code == 2 and calls == ["metadata"]


def test_metadata_signal_text_is_data_and_selected_punchcut_not_default_member(font, capsys):
    def mutate(phase, root, r):
        if phase == "metadata":
            g = json.loads(r["stdout"])
            g["packages"][0]["description"] = "SIGINT / signal: 11"
            assert next(p for p in g["packages"] if p["name"] == "punchcut")["id"] not in g["workspace_default_members"]
            r["stdout"] = json.dumps(g)
    code, report, calls = execute(font, mutate=mutate)
    assert code == 0 and calls == ["metadata", "cargo"]
    entry.publish(report, font.private)
    line = capsys.readouterr().out
    assert line.count(entry.EVIDENCE_PREFIX) == 1
    decoded = json.loads(line.split(entry.EVIDENCE_PREFIX, 1)[1])
    assert decoded["metadata"]["stderr"] == entry.FONT_RESOLVER_WARNING
    assert "SIGINT / signal: 11" in decoded["metadata"]["stdout"]


@pytest.mark.parametrize("code", [1, 101, 124, 137, -6, -9, -11, -15])
def test_actual_failure_codes_are_retained_without_reselecting(font, code):
    result, report, calls = execute(font, code=code)
    assert result == (code if code >= 0 else 128 - code) and calls == ["metadata", "cargo"]
    assert report["execution"]["returncode"] == code and report["default_resolution_commands"] == 0


def test_prom_exports_and_default_transport_are_unchanged(font):
    assert entry.TASK.startswith("github__prometheus__")
    assert entry.PRIVATE_COMMAND == ["test", "--test", "oracle_session", "--", "--nocapture"]
    command = ["cargo", *entry.PREFIX, *entry.PRIVATE_COMMAND]
    assert entry.transport(command, {}) == [
        entry.REAL_CARGO, *entry.PREFIX, "test", "--test", "oracle_session", "--locked", "--offline", "--", "--nocapture"]
    with pytest.raises(entry.OverlayError):
        entry.transport(command, font.private, font.spec)
    p = entry.profile_for(font.spec)
    command = ["cargo", *p["prefix"], *p["command"]]
    assert entry.transport(command, font.private, font.spec) == [
        entry.REAL_CARGO, *p["prefix"], "test", "-p", "punchcut", "--test", "oracle_session",
        "--locked", "--offline", "--", "--nocapture"]


def test_workspace_inherited_selector_version_and_git_vendor_version_are_current_inputs(font):
    f = font
    path = f.source / "read-fonts/Cargo.toml"
    path.write_text(path.read_text().replace('version="0.0.4"', "version.workspace=true"))
    path = f.source / "Cargo.toml"
    path.write_text(path.read_text() + '[workspace.package]\nversion="0.0.4"\n')
    code, report, _ = execute(f)
    assert code == 0 and report["mirror"]["selection"]["selector_version"] == "0.0.4", report
    _, folder = add_external(f)
    path = folder / "Cargo.toml"
    path.write_text(path.read_text().replace('version="0.1.0"', "version.workspace=true")
                    + '[workspace.package]\nversion="0.1.0"\n')
    path = folder / ".cargo-checksum.json"
    document = json.loads(path.read_text())
    document["files"]["Cargo.toml"] = entry.file_hash(folder / "Cargo.toml")
    path.write_text(json.dumps(document))
    code, report, _ = execute(f)
    assert code == 0 and report["mirror"]["selected_default"] is None, report


@pytest.mark.parametrize("problem", ["metadata_rc_signal", "metadata_shell_signal", "stdout_utf8",
                                  "stderr_utf8", "deadline", "output_limit", "malformed"])
def test_font_metadata_stream_status_and_bounds_abort_before_actual_command(font, problem):
    def mutate(phase, root, r):
        assert phase == "metadata", "invalid metadata reached check/test"
        if problem == "metadata_rc_signal":
            r["returncode"] = -2
        elif problem == "metadata_shell_signal":
            r["returncode"] = 137
        elif problem in {"stdout_utf8", "stderr_utf8"}:
            r[problem + "_valid"] = False
        elif problem in {"deadline", "output_limit"}:
            r["limit"] = problem
        else:
            r["stdout"] = "{"
    code, report, calls = execute(font, mutate=mutate)
    assert code == 2 and calls == ["metadata"] and report["status"] == "rejected"


@pytest.mark.parametrize("problem", ["stdout_signal", "stderr_signal", "limit", "missing_id", "source_mutation"])
def test_font_zero_cargo_status_cannot_hide_signal_incomplete_tests_or_mutation(font, problem):
    def mutate(phase, root, r):
        if phase != "cargo":
            return
        if problem == "stdout_signal":
            r["stdout"] += "process killed by SIGABRT\n"
        elif problem == "stderr_signal":
            r["stderr"] += "signal: 11, SIGSEGV: invalid memory reference\n"
        elif problem == "limit":
            r["limit"] = "output_limit"
        elif problem == "missing_id":
            r["stdout"] = "test result: ok. 0 passed;\n"
        else:
            (root / "punchcut/src/lib.rs").write_text("// mutated current source")
    code, report, calls = execute(font, mutate=mutate)
    assert code == 2 and calls == ["metadata", "cargo"]
    assert report["execution"]["returncode"] == 0 and not report["selection_uses_test_results"]


@pytest.mark.parametrize("kind,test", [(["bench"], False), (["bench"], True),
                                    (["lib"], False), (["proc-macro"], False), (["custom-build"], False)])
def test_only_unselected_test_false_bench_can_lack_its_source_file(font, kind, test):
    def mutate(phase, root, r):
        if phase == "metadata":
            graph = json.loads(r["stdout"])
            package = next(p for p in graph["packages"] if p["name"] == "punchcut")
            package["targets"].append({"name": "missing", "kind": kind, "test": test,
                                       "src_path": str(root / "punchcut/missing.rs")})
            r["stdout"] = json.dumps(graph)
    code, _, calls = execute(font, mutate=mutate)
    permitted = kind == ["bench"] and test is False
    assert code == (0 if permitted else 2)
    assert calls == (["metadata", "cargo"] if permitted else ["metadata"])


@pytest.mark.parametrize("field", ["task_id", "profile", "source_config_sha256", "vendor_sources", "defaults"])
def test_font_task_profile_asset_and_source_schema_are_not_general_opt_in(font, field):
    value = font.spec
    if field == "vendor_sources":
        value[field] = {"unbound": "source-" + "1" * 64}
    elif field == "defaults":
        value[field][0]["selector_version"] = "0.0.9"
    else:
        value[field] = "unbound"
    code, _, calls = execute(font)
    assert code == 2 and calls == []


def test_private_contract_and_public_transport_cannot_drop_explicit_package_or_original_harness(font):
    profile = entry.profile_for(font.spec)
    with pytest.raises(entry.OverlayError):
        entry.transport(["cargo", *profile["prefix"], "check"], spec=font.spec)
    private = copy.deepcopy(font.private)
    private["command"] = ["test", "--test", "oracle_session", "--", "--nocapture"]
    with pytest.raises(entry.OverlayError):
        entry.validate(font.spec, private)
    path = font.source / entry.FONT_HARNESS["path"]
    path.write_bytes(path.read_bytes() + b"\n// changed original private bytes\n")
    code, _, calls = execute(font)
    assert code == 2 and calls == []
