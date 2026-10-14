"""Native old-stable Cargo configuration, with no compiler/channel overrides."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_cargo_home as home
from reviewbench_maintainability.agentic.contracts import ContractError


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def context(tmp_path, version="1.59.0", result="unstable"):
    tool = tmp_path / "tool/bin"
    tool.mkdir(parents=True)
    script = (
        "#!" + sys.executable + "\nimport json,sys,os,signal\n"
        "if '--version' in sys.argv:\n print('cargo " + version + " (fixture)');sys.exit(0)\n"
    )
    if result == "unstable":
        script += "print(" + repr(home.UNSTABLE_ERROR) + ",file=sys.stderr);sys.exit(101)\n"
    elif result == "supported":
        script += "print(json.dumps({'root':sys.argv[sys.argv.index('--manifest-path')+1]}))\n"
    elif result == "signal":
        script += "os.kill(os.getpid(),signal.SIGTERM)\n"
    else:
        script += "print(" + repr(result) + ",file=sys.stderr);sys.exit(101)\n"
    (tool / "cargo").write_text(script)
    (tool / "cargo").chmod(0o755)
    registry = tmp_path / "toolchains.json"
    registry.write_text(json.dumps({version: {"root": str(tool.parent)}}))
    output, scratch = tmp_path / "assets", tmp_path / "scratch"
    output.mkdir()
    scratch.mkdir()
    def copy_asset(source, category):
        target = output / (category + "-" + digest(source))
        shutil.copyfile(source, target)
        return {"path": str(target), "sha256": digest(target)}
    return SimpleNamespace(config={"rust_toolchains": str(registry)}, output=output,
                           scratch=scratch, copy_asset=copy_asset)


@pytest.mark.parametrize("version", ["1.59.0", "1.60.0"])
def test_observed_old_stable_boundary_uses_native_home_file(tmp_path, version):
    ctx = context(tmp_path, version)
    options, assets, bootstrap = home.bind(ctx, version, None, ["--offline", "--config", "source"])
    assert options == ["--offline"]
    assert {row["target"] for row in assets} == {home.SOURCE_TARGET, home.INSTALLER_TARGET}
    assert home.HOME_TARGET in bootstrap[0] and "/workspace" not in bootstrap[0]
    record = json.loads((ctx.output / "rust-cli-probes" / (version + "-source-config.json")).read_text())
    assert record["mode"] == "cargo-home-file" and record["compiler_channel_overrides"] == {}
    assert all("-Z" not in row["command"] for row in record["probes"])


def test_supported_cli_is_not_changed(tmp_path):
    ctx = context(tmp_path, result="supported")
    options = ["--offline", "--config", "original-file"]
    assert home.bind(ctx, "1.59.0", None, options) == (options, [], [])


@pytest.mark.parametrize("version", ["1.65.0", "1.93.0", "nightly-2023-05-04"])
def test_other_versions_and_nightly_profiles_are_untouched(tmp_path, version):
    ctx = SimpleNamespace()
    options = ["--offline", "--config", "original-file"]
    assert home.bind(ctx, version, None, options) == (options, [], [])


@pytest.mark.parametrize("result", ["", "signal", "unrelated error", home.UNSTABLE_ERROR + "\nerror: another fault"])
def test_no_general_error_or_signal_fallback(tmp_path, result):
    ctx = context(tmp_path, result=result)
    with pytest.raises(ContractError, match="outside its known CLI"):
        home.bind(ctx, "1.59.0", None, ["--offline", "--config", "source"])


def test_existing_git_source_mappings_are_copied_without_rewriting(tmp_path):
    ctx = context(tmp_path)
    source = tmp_path / "sources.toml"
    source.write_text('[source.example]\ngit="https://example.invalid/repo"\n'
                      'tag="original-tag"\nreplace-with="vendor"\n'
                      '[source.vendor]\ndirectory="/opt/reviewbench-assets/vendor"\n')
    asset = {"path": str(source), "sha256": digest(source)}
    options, assets, bootstrap = home.bind(ctx, "1.59.0", asset, ["--offline", "--config", str(source)])
    assert options == ["--offline"] and len(assets) == 1
    assert digest(source) == asset["sha256"]
    assert "CARGO_TEST_CHANNEL" not in bootstrap[0] and "RUSTC_BOOTSTRAP" not in bootstrap[0]


@pytest.mark.parametrize("payload", ['[env]\nRUSTC_BOOTSTRAP="1"\n', 'source=1\n'])
def test_source_config_cannot_carry_unrelated_settings(tmp_path, payload):
    ctx = context(tmp_path)
    source = tmp_path / "sources.toml"
    source.write_text(payload)
    with pytest.raises(ContractError, match="only source mappings"):
        home.bind(ctx, "1.59.0", {"path": str(source), "sha256": digest(source)}, [])


def test_install_exact_bytes_and_replace_old_config(tmp_path):
    source, target = tmp_path / "source", tmp_path / "config"
    source.write_bytes(b"[source.vendor]\ndirectory='original'\n")
    target.write_bytes(b"old")
    assert home.install(source, target) == digest(source)
    assert target.read_bytes() == source.read_bytes()
    assert target.stat().st_mode & 0o777 == 0o644
    assert not list(tmp_path.glob(".reviewbench-config-*"))


@pytest.mark.parametrize("link", ["symlink", "hardlink"])
def test_install_does_not_modify_existing_link_referent(tmp_path, link):
    source, target, referent = [tmp_path / name for name in ("source", "config", "referent")]
    source.write_bytes(b"[source.vendor]\ndirectory='new'\n")
    referent.write_bytes(b"KEEP_ORIGINAL_SOURCE")
    if link == "symlink":
        target.symlink_to(referent)
    else:
        os.link(referent, target)
    home.install(source, target)
    assert referent.read_bytes() == b"KEEP_ORIGINAL_SOURCE"
    assert target.read_bytes() == source.read_bytes() and not target.is_symlink()
    assert referent.stat().st_ino != target.stat().st_ino


@pytest.mark.parametrize("size", [0, 65537])
def test_configuration_size_bound(tmp_path, size):
    source = tmp_path / "source"
    source.write_bytes(b"x" * size)
    with pytest.raises(ValueError, match="bounded file size"):
        home.install(source, tmp_path / "config")
    assert not (tmp_path / "config").exists()


def test_linked_source_and_parent_are_rejected(tmp_path):
    source, alias = tmp_path / "source", tmp_path / "alias"
    source.write_bytes(b"x")
    alias.symlink_to(source)
    with pytest.raises(ValueError, match="regular immutable"):
        home.install(alias, tmp_path / "config")
    parent = tmp_path / "parent"
    parent.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="linked parent"):
        home.install(source, parent / "config")


def test_failed_rename_preserves_old_configuration(tmp_path, monkeypatch):
    source, target = tmp_path / "source", tmp_path / "config"
    source.write_bytes(b"new")
    target.write_bytes(b"old")
    def fail(*_):
        raise OSError("fixture")
    monkeypatch.setattr(home.os, "replace", fail)
    with pytest.raises(OSError):
        home.install(source, target)
    assert target.read_bytes() == b"old"
    assert not list(tmp_path.glob(".reviewbench-config-*"))


def test_real_159_native_config_keeps_project_settings_and_locked_dependency(tmp_path):
    configured = os.environ.get("OLD_STABLE_TEST_CARGO")
    if configured is None:
        pytest.skip('set OLD_STABLE_TEST_CARGO to the original Cargo 1.59 binary for this optional offline integration')
    assert configured, "supply the retained original Cargo1.59 binary for this real offline test"
    cargo = Path(configured)
    project, cache, vendor = [tmp_path / name for name in ("project", "cargo-home", "vendor")]
    (project / "src").mkdir(parents=True)
    (project / ".cargo").mkdir()
    cache.mkdir()
    dependency = vendor / "legacy-cargo-dep-1.0.0"
    (dependency / "src").mkdir(parents=True)
    (dependency / "Cargo.toml").write_text(
        '[package]\nname="legacy-cargo-dep"\nversion="1.0.0"\nedition="2018"\n')
    (dependency / "src/lib.rs").write_text("pub fn value() -> u32 { 42 }\n")
    archive = tmp_path / "dependency.crate"
    with tarfile.open(archive, "w:gz") as tar:
        for name in ("Cargo.toml", "src/lib.rs"):
            data = (dependency / name).read_bytes()
            info = tarfile.TarInfo("legacy-cargo-dep-1.0.0/" + name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    (dependency / ".cargo-checksum.json").write_text(json.dumps({
        "package": digest(archive), "files": {name: digest(dependency / name)
                                             for name in ("Cargo.toml", "src/lib.rs")}}))
    (project / "Cargo.toml").write_text(
        '[package]\nname="config-consumer"\nversion="0.0.0"\nedition="2018"\n'
        '[dependencies]\nlegacy-cargo-dep="=1.0.0"\n')
    (project / ".cargo/config").write_text('[env]\nREVIEWBENCH_PROJECT_SETTING="kept"\n')
    (project / "src/lib.rs").write_text(
        '#[test] fn real_transport() { assert_eq!(legacy_cargo_dep::value(), 42);'
        'assert_eq!(std::env::var("REVIEWBENCH_PROJECT_SETTING").unwrap(), "kept"); }\n')
    source = tmp_path / "sources.toml"
    source.write_text('[source.crates-io]\nreplace-with="vendor"\n'
                      '[source.vendor]\ndirectory=' + json.dumps(str(vendor)) + "\n")
    home.install(source, cache / "config")
    environment = dict(os.environ, CARGO_HOME=str(cache), RUSTC=str(cargo.parent / "rustc"),
                       CARGO_TARGET_DIR=str(tmp_path / "target"), CARGO_NET_OFFLINE="true")
    for name in ("RUSTC_BOOTSTRAP", "__CARGO_TEST_CHANNEL_OVERRIDE_DO_NOT_USE_THIS", "RUSTUP_TOOLCHAIN"):
        environment.pop(name, None)
    def run(arguments):
        return subprocess.run([str(cargo), *arguments], cwd=project, env=environment,
                              text=True, capture_output=True, timeout=60)
    setup = run(["generate-lockfile", "--offline"])
    assert setup.returncode == 0, setup.stderr
    tracked = {str(p.relative_to(project)): p.read_bytes() for p in project.rglob("*") if p.is_file()}
    (cache / "config").unlink()
    baseline = run(["--offline", "--config", str(source), "test", "--locked", "--lib"])
    assert baseline.returncode == 101 and home.UNSTABLE_ERROR in baseline.stderr
    home.install(source, cache / "config")
    modified = run(["--offline", "test", "--locked", "--lib"])
    assert modified.returncode == 0, modified.stderr
    assert "1 passed; 0 failed" in modified.stdout
    assert {str(p.relative_to(project)): p.read_bytes() for p in project.rglob("*") if p.is_file()} == tracked
    (cache / "config").unlink()
    rollback = run(["--offline", "--config", str(source), "test", "--locked", "--lib"])
    assert rollback.returncode == 101 and home.UNSTABLE_ERROR in rollback.stderr
