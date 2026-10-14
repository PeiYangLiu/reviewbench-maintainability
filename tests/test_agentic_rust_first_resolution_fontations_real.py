"""Retained Cargo1.93 on tiny LOCAL TOOL workspaces, never original Font SUT.

The synthetic implementation deliberately exposes the unchanged public harness
shape so adapter transport, failures and source synchronization can be tested.
Its results are not Fontations scores, calibration or readiness evidence.
"""
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile

import pytest
import tomli_w

from test_agentic_rust_first_resolution_fontations import (
    font, entry, HARNESS_BYTES, make_lock, write_configuration,
)

TOOL_LIBRARY = r'''
mod current;
pub mod font {
    pub struct FontRef;
    impl FontRef {
        pub fn new(_: &[u8]) -> Result<Self, &'static str> { Ok(Self) }
    }
}
pub struct Context;
impl Context {
    pub fn new() -> Self { Self }
    pub fn new_scaler(&mut self) -> Builder { Builder }
}
pub struct Builder;
impl Builder {
    pub fn font_id(self, _: Option<u32>) -> Self { self }
    pub fn size(self, _: f32) -> Self { self }
    pub fn variations<const N: usize>(self, _: [(&str, f32); N]) -> Self { self }
    pub fn build(self, _: &font::FontRef) -> Scaler { Scaler }
}
pub struct Scaler;
impl Scaler {
    pub fn has_outlines(&self) -> bool { crate::current::outlines() }
}
'''


@pytest.fixture
def real_font(font, monkeypatch):
    name = os.environ.get("FONTATIONS_TEST_CARGO")
    if not name or not Path(name).is_file():
        pytest.skip("explicit retained Cargo1.93 required; never install tools")
    f = font
    cargo = Path(name)
    rustc = cargo.with_name("rustc")
    monkeypatch.setattr(entry, "REAL_CARGO", str(cargo))
    monkeypatch.setattr(entry, "REAL_RUSTC", str(rustc))
    f.spec.update(cargo_sha256=entry.file_hash(cargo), rustc_sha256=entry.file_hash(rustc))
    f.vendor.mkdir(exist_ok=True)
    monkeypatch.setenv("CARGO_HOME", str(f.source.parent / "empty-cargo-home"))
    monkeypatch.setenv("CARGO_NET_OFFLINE", "true")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    monkeypatch.setenv("GIT_NO_LAZY_FETCH", "1")
    monkeypatch.setenv("RUSTFLAGS", "")
    monkeypatch.setenv("CARGO_INCREMENTAL", "0")
    monkeypatch.setenv("RUSTUP_AUTO_INSTALL", "0")
    monkeypatch.delenv("RUSTC_WRAPPER", raising=False)
    monkeypatch.delenv("RUSTC_WORKSPACE_WRAPPER", raising=False)
    p = f.source / "punchcut/Cargo.toml"
    p.write_text(p.read_text().replace('renamed_read={package="read-fonts",',
                                     "read-fonts={"))
    (f.source / "read-fonts/src/lib.rs").write_text(
        'pub mod test_data { pub mod test_fonts { pub const VAZIRMATN_VAR: &[u8] = b"LOCAL TOOL"; } }\n')
    (f.source / "punchcut/src/lib.rs").write_text(TOOL_LIBRARY)
    (f.source / "punchcut/src/current.rs").write_text("pub fn outlines() -> bool { true }\n")
    f.tool_setup = []
    return f


def run_fixture(f, private=True):
    p = entry.profile_for(f.spec)
    command = ["cargo", *p["prefix"], *(p["command"] if private else p["public"])]
    return entry.execute(f.spec, f.source, f.build, command, f.private if private else None,
                         lock_sources=f.locks, vendor=f.vendor, source_config=f.config)


def retain(f, report, label):
    folder = os.environ.get("FONTATIONS_FIXTURE_EVIDENCE")
    if folder:
        path = Path(folder)
        path.mkdir(parents=True, exist_ok=True)
        (path / (label + ".json")).write_text(json.dumps({
            "kind": "synthetic LOCAL TOOL fixture; not original Font SUT/readiness",
            "runtime_report": report, "tool_setup": f.tool_setup,
            "original_harness_sha256": entry.digest(HARNESS_BYTES),
            "network_download_bytes": 0, "original_sut_runs": 0,
        }, sort_keys=True, indent=2) + "\n")


@pytest.mark.parametrize("version", ["0.0.4", "0.0.5"])
@pytest.mark.parametrize("private", [False, True])
def test_real_virtual_workspace_uses_current_selector_and_explicit_punchcut(real_font, version, private):
    f = real_font
    p = f.source / "read-fonts/Cargo.toml"
    p.write_text(p.read_text().replace("0.0.4", version))
    before = entry.snapshot(f.source)
    code, report = run_fixture(f, private)
    retain(f, report, version + ("-private" if private else "-public"))
    assert code == 0, report
    assert report["metadata"]["returncode"] == report["execution"]["returncode"] == 0
    assert report["metadata"]["stderr"] == entry.FONT_RESOLVER_WARNING
    graph = json.loads(report["metadata"]["stdout"])
    selected = next(p for p in graph["packages"] if p["name"] == "punchcut")
    assert graph["resolve"]["root"] is None and len(graph["workspace_members"]) == 6
    assert selected["id"] not in graph["workspace_default_members"]
    assert report["mirror"]["selected_default"]["selector_version"] == version
    assert "-p" in report["transport"]["effective_argv"]
    assert entry.snapshot(f.source) == before and not (f.source / "Cargo.lock").exists()
    if private:
        assert report["test_results"] == {entry.FONT_EXPECTED[0]: True}


def test_real_candidate_authored_different_graph_and_moved_member_keep_original_bytes(real_font, monkeypatch):
    f = real_font
    p = f.source / "read-fonts/Cargo.toml"
    p.write_text(p.read_text().replace("0.0.4", "0.0.9") +
                 '[package.metadata.tool]\nnote="SIGINT in metadata is not a signal"\n')
    (f.source / "members").mkdir()
    (f.source / "read-fonts").rename(f.source / "members/reader")
    p = f.source / "Cargo.toml"
    p.write_text(p.read_text().replace('"read-fonts"', '"./members/reader/"'))
    p = f.source / "punchcut/Cargo.toml"
    p.write_text(p.read_text().replace("../read-fonts", "../members/reader"))
    own = make_lock("0.0.9") + b"\n# candidate-authored serialization, not either default\n"
    (f.source / "Cargo.lock").write_bytes(own)
    monkeypatch.setattr(entry, "select_workspace_default",
                        lambda *a: pytest.fail("candidate-owned lock must bypass selector"))
    before = entry.snapshot(f.source)
    code, report = run_fixture(f)
    retain(f, report, "candidate-owned-current-graph-and-moved-member")
    assert code == 0, report
    assert report["mirror"]["selected_default"] is None
    assert (f.build / "source/Cargo.lock").read_bytes() == own
    assert entry.snapshot(f.source) == before and "SIGINT" in report["metadata"]["stdout"]


@pytest.mark.parametrize("failure", ["assertion", "multifile_compile"])
def test_real_failed_assertion_and_unrelated_current_source_error_are_not_selected_away(real_font, failure):
    f = real_font
    p = f.source / "punchcut/src/current.rs"
    p.write_text("pub fn outlines() -> bool { false }\n" if failure == "assertion" else
                 'compile_error!("unrelated CURRENT multifile error");\npub fn outlines() -> bool { true }\n')
    before = entry.snapshot(f.source)
    code, report = run_fixture(f)
    retain(f, report, failure)
    assert code == 101, report
    assert report["execution"]["returncode"] == 101
    assert report["test_results"] == {entry.FONT_EXPECTED[0]: False}
    assert report["default_resolution_commands"] == 0 and not report["selection_uses_test_results"]
    assert entry.snapshot(f.source) == before
    assert ("assertion failed" if failure == "assertion" else "unrelated CURRENT multifile error") in report["execution"]["stderr"]


def test_real_reused_build_root_recompiles_changed_current_multifile_source(real_font):
    f = real_font
    code, first = run_fixture(f)
    retain(f, first, "synchronization-before")
    assert code == 0, first
    current = f.source / "punchcut/src/current.rs"
    current.write_text("pub fn outlines() -> bool { false }\n")
    (f.source / "punchcut/src/unused.rs").write_text("// current file must be copied too\n")
    before = entry.snapshot(f.source)
    code, second = run_fixture(f)
    retain(f, second, "synchronization-after")
    assert code == 101 and second["test_results"] == {entry.FONT_EXPECTED[0]: False}, second
    assert (f.build / "source/punchcut/src/current.rs").read_bytes() == current.read_bytes()
    assert (f.build / "source/punchcut/src/unused.rs").is_file()
    assert entry.snapshot(f.source) == before


def git_command(f, repository, *arguments):
    command = ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
               "-c", "user.name=LOCAL TOOL", "-c", "user.email=tool@example.invalid", *arguments]
    result = subprocess.run(command, cwd=repository, capture_output=True, text=True, timeout=15,
                            env={**os.environ, "GIT_ALLOW_PROTOCOL": "file", "GIT_NO_LAZY_FETCH": "1",
                                 "GIT_AUTHOR_DATE": "2001-01-01T00:00:00+00:00",
                                 "GIT_COMMITTER_DATE": "2001-01-01T00:00:00+00:00"})
    f.tool_setup.append({"argv": command, "cwd": str(repository), "exit_status": result.returncode,
                         "stdout": result.stdout, "stderr": result.stderr})
    assert result.returncode == 0, result.stderr
    return result.stdout


def external_sources(f, kinds):
    """Fixed tiny archive/Git objects; Cargo never resolves or fetches a lock."""
    rows, definitions, sources, contents = [], [], [], {}
    manifest = b'[package]\nname="fixture-external"\nversion="0.1.0"\nedition="2021"\n'
    source_bytes = b"pub fn bound() -> bool { true }\n"
    for kind in kinds:
        if kind == "git":
            repository = f.source.parent / "local-git-origin"
            (repository / "src").mkdir(parents=True)
            (repository / "Cargo.toml").write_bytes(manifest)
            (repository / "src/lib.rs").write_bytes(source_bytes)
            git_command(f, repository, "-c", "init.defaultBranch=main", "init", ".")
            git_command(f, repository, "add", "Cargo.toml", "src/lib.rs")
            git_command(f, repository, "commit", "-m", "fixed synthetic public dependency")
            revision = git_command(f, repository, "rev-parse", "HEAD").strip()
            url = repository.as_uri()
            source = "git+" + url + "#" + revision
            package = {"name": "fixture-external", "version": "0.1.0", "source": source}
            definitions.append('git_dep={package="fixture-external",git=' + json.dumps(url) + "}\n")
            contents[source] = {
                name: git_command(f, repository, "show", revision + ":" + name).encode()
                for name in ("Cargo.toml", "src/lib.rs")}
            # No checkout/cache can hide missing source-replacement transport.
            repository.rename(repository.with_name("local-git-origin-not-available"))
        else:
            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode="w") as tar:
                for name, value in (("Cargo.toml", manifest), ("src/lib.rs", source_bytes)):
                    info = tarfile.TarInfo("fixture-external-0.1.0/" + name)
                    info.size, info.mode = len(value), 0o644
                    tar.addfile(info, io.BytesIO(value))
            fixed_archive = f.source.parent / "fixed-local-tool.crate"
            fixed_archive.write_bytes(archive.getvalue())
            source = entry.REGISTRY
            package = {"name": "fixture-external", "version": "0.1.0", "source": source,
                       "checksum": entry.file_hash(fixed_archive)}
            definitions.append('registry_dep={package="fixture-external",version="=0.1.0"}\n')
            contents[source] = {"Cargo.toml": manifest, "src/lib.rs": source_bytes}
        rows.append(package)
        sources.append(source)
    write_configuration(f, sources)
    for row in rows:
        folder = f.vendor / f.spec["vendor_sources"][row["source"]] / "fixture-external-0.1.0"
        (folder / "src").mkdir(parents=True)
        for name, value in contents[row["source"]].items():
            (folder / name).write_bytes(value)
        (folder / ".cargo-checksum.json").write_text(json.dumps({
            "package": row.get("checksum"),
            "files": {name: entry.file_hash(folder / name) for name in contents[row["source"]]},
        }))
    with (f.source / "punchcut/Cargo.toml").open("a") as stream:
        stream.writelines(definitions)
    document = entry.tomllib.loads(make_lock(external=rows).decode())
    punchcut = next(row for row in document["package"] if row["name"] == "punchcut")
    punchcut["dependencies"] = ["read-fonts", *[
        "fixture-external 0.1.0 (" + row["source"] + ")" for row in rows]]
    (f.source / "Cargo.lock").write_text(tomli_w.dumps(document))
    body = " && ".join(kind + "_dep::bound()" for kind in kinds)
    (f.source / "punchcut/src/current.rs").write_text("pub fn outlines() -> bool { " + body + " }\n")


@pytest.mark.parametrize("kinds", [("registry",), ("git",), ("registry", "git")])
def test_real_source_aware_offline_registry_and_immutable_git_can_share_name_version(real_font, kinds):
    f = real_font
    external_sources(f, kinds)
    before = entry.snapshot(f.source)
    code, report = run_fixture(f)
    retain(f, report, "provided-sources-" + "-".join(kinds))
    assert code == 0, report
    assert report["test_results"] == {entry.FONT_EXPECTED[0]: True}
    assert report["mirror"]["selected_default"] is None
    graph = json.loads(report["metadata"]["stdout"])
    external = [p for p in graph["packages"] if p["source"] is not None]
    assert len(external) == len(kinds) and len({p["manifest_path"] for p in external}) == len(kinds)
    assert all(p["source"] in f.spec["vendor_sources"] for p in external)
    assert entry.snapshot(f.source) == before and report["default_resolution_commands"] == 0
    assert not (Path(os.environ["CARGO_HOME"]) / "git/checkouts").exists()
