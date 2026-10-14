"""Byte-preserving native registry transport, with real original-Cargo checks."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tomllib
from types import SimpleNamespace

import pytest
import tomli_w

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_local_registry as local
from agentic_runtime_migration.rust_vendor import CRATES_IO, materialize, source_configuration
from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash


def digest(data):
    return hashlib.sha256(data).hexdigest()


def line(name="normal", version="0.1.0", checksum="a" * 64):
    return (json.dumps({"name": name, "vers": version, "deps": [], "features": {},
                        "cksum": checksum, "yanked": False}, separators=(",", ":")) + "\n").encode()


def cached(tmp_path, data=None):
    root = tmp_path / "index"
    target = root / ".cache" / local.index_path("normal")
    target.parent.mkdir(parents=True)
    payload = line().rstrip(b"\n")
    target.write_bytes(data if data is not None else
                       b'\x03\x02\0\0\0etag: "fixture"\0' + b"0.1.0\0" + payload + b"\0")
    return root, target


@pytest.mark.parametrize("name,expected", [
    ("a", "1/a"), ("AB", "2/ab"), ("AbC", "3/a/abc"), ("normal", "no/rm/normal"),
])
def test_index_paths_follow_original_registry_layout(name, expected):
    assert local.index_path(name) == expected


def test_sparse_payload_is_not_reserialized(tmp_path):
    payload = b'{ "name":"normal", "vers":"0.1.0", "deps":[], "features":{}, "cksum":"' + b"a" * 64 + b'","yanked":false }'
    root, target = cached(tmp_path, b'\x03\x02\0\0\0etag: "fixture"\0' + b"0.1.0\0" + payload + b"\0")
    original = target.read_bytes()
    observed, record = local.sparse_index_entry(root, "normal", "0.1.0", "a" * 64)
    assert observed == payload + b"\n"
    assert record["source_cache_sha256"] == digest(original)
    assert record["original_json_sha256"] == digest(payload)
    assert record["JSON_fields_rewritten"] is False and target.read_bytes() == original


@pytest.mark.parametrize("change", [
    "header", "trailing_nul", "duplicate", "missing", "json", "name", "checksum", "symlink",
])
def test_unbound_sparse_records_fail(tmp_path, change):
    root, target = cached(tmp_path)
    data = target.read_bytes()
    if change == "header":
        target.write_bytes(b"\x04" + data[1:])
    elif change == "trailing_nul":
        target.write_bytes(data[:-1])
    elif change == "duplicate":
        target.write_bytes(data + b"0.1.0\0" + line().rstrip(b"\n") + b"\0")
    elif change == "missing":
        target.write_bytes(data.replace(b"0.1.0\0", b"0.2.0\0"))
    elif change == "json":
        target.write_bytes(b'\x03\x02\0\0\0etag: "fixture"\0' + b"0.1.0\0{\0")
    elif change == "name":
        target.write_bytes(data.replace(b'"name":"normal"', b'"name":"other"'))
    elif change == "checksum":
        target.write_bytes(data.replace(b"a" * 64, b"b" * 64))
    else:
        outside = tmp_path / "outside"
        target.rename(outside)
        target.symlink_to(outside)
    with pytest.raises(ContractError):
        local.sparse_index_entry(root, "normal", "0.1.0", "a" * 64)


def test_local_package_keeps_the_archive_and_exact_index_line(tmp_path):
    archive = tmp_path / "normal.crate"
    archive.write_bytes(b"original archive")
    checksum = digest(archive.read_bytes())
    root = tmp_path / "local"
    row = local.materialize_package(root, "normal", "0.1.0", checksum, archive,
                                    (line(checksum=checksum), {"source": "fixture"}))
    assert (root / row["archive"]).read_bytes() == archive.read_bytes()
    assert (root / row["index_path"]).read_bytes() == line(checksum=checksum)
    with pytest.raises(ContractError):
        local.materialize_package(root, "normal", "0.1.0", checksum, archive,
                                  (line(checksum=checksum), {}))


def test_local_index_output_cannot_escape_through_a_parent_link(tmp_path):
    root, outside = tmp_path / "local", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "index").symlink_to(outside)
    archive = tmp_path / "a.crate"
    archive.write_bytes(b"archive")
    checksum = digest(archive.read_bytes())
    with pytest.raises(ContractError):
        local.materialize_package(root, "normal", "0.1.0", checksum, archive,
                                  (line(checksum=checksum), {}))
    assert list(outside.iterdir()) == []


def test_source_configuration_changes_only_the_registry_transport():
    sources = [CRATES_IO, "git+https://example.invalid/repo?rev=abc#" + "a" * 40]
    old, roots = source_configuration(sources)
    new, new_roots = source_configuration(sources, registry_transport="local-registry")
    assert roots == new_roots
    a, b = [tomllib.loads(x) for x in (old, new)]
    key = a["source"]["crates-io"]["replace-with"]
    original = a["source"][key].pop("directory")
    a["source"][key]["local-registry"] = original
    assert a == b
    with pytest.raises(ContractError):
        source_configuration(sources, registry_transport="unbound")


def crate(path, name, version, edition):
    with tarfile.open(path, "w:gz") as archive:
        for relative, data in [
            ("Cargo.toml", f'[package]\nname="{name}"\nversion="{version}"\nedition="{edition}"\n'.encode()),
            ("src/lib.rs", b"pub const VALUE: u8 = 1;\n"),
        ]:
            member = tarfile.TarInfo(name + "-" + version + "/" + relative)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))


def test_original_cargo_uses_complete_local_index_without_parsing_inactive_edition(tmp_path):
    cargo = os.environ.get("WORKSPACE_TEST_CARGO")
    if not cargo:
        pytest.skip("requires the original preinstalled Cargo")
    cargo = Path(cargo)
    consumer = tmp_path / "consumer"
    (consumer / "src").mkdir(parents=True)
    (consumer / "Cargo.toml").write_text(
        '[package]\nname="consumer"\nversion="0.1.0"\nedition="2021"\n'
        '[dependencies]\nnormal="=0.1.0"\n'
        '[target.\'cfg(target_arch = "wasm32")\'.dependencies]\nfuture="=0.1.0"\n')
    (consumer / "src/lib.rs").write_text("pub const VALUE:u8=normal::VALUE;\n")
    packages, archives = [], {}
    for name, edition in (("normal", "2021"), ("future", "2024")):
        path = tmp_path / (name + ".crate")
        crate(path, name, "0.1.0", edition)
        packages.append({"name": name, "version": "0.1.0", "source": CRATES_IO,
                         "checksum": digest(path.read_bytes())})
        archives[name] = path
    lock = {"version": 3, "package": [
        {"name": "consumer", "version": "0.1.0", "dependencies": ["future", "normal"]}, *packages]}
    (consumer / "Cargo.lock").write_text(tomli_w.dumps(lock))
    before = (consumer / "Cargo.lock").read_bytes()
    provider = lambda name, *_: archives[name]
    directory = tmp_path / "directory"
    directory_config, _ = materialize([lock], {"packages": []}, tmp_path / "cache", directory, provider)
    native = tmp_path / "native"
    native_config, audit = materialize(
        [lock], {"packages": []}, tmp_path / "cache", native, provider,
        registry_index_provider=lambda name, version, checksum: (line(name, version, checksum), {}))
    assert len(audit) == 2 and all(row["format"] == "locked-local-registry/v1" for row in audit)
    for label, root, config, expected in [
        ("directory", directory, directory_config, 101), ("native", native, native_config, 0),
    ]:
        path = tmp_path / (label + ".toml")
        path.write_text(config.replace("/opt/reviewbench-assets/vendor", str(root)))
        result = subprocess.run(
            [str(cargo), "--config", str(path), "metadata", "--locked", "--offline",
             "--filter-platform", "x86_64-unknown-linux-gnu", "--format-version", "1"],
            cwd=consumer, env=dict(os.environ, CARGO_HOME=str(tmp_path / (label + "-empty")),
                                   CARGO_NET_OFFLINE="true", RUSTUP_AUTO_INSTALL="0",
                                   RUSTC=str(cargo.with_name("rustc"))),
            capture_output=True, text=True, timeout=60)
        assert result.returncode == expected, result.stderr
        if expected:
            assert "2024" in result.stderr and "edition" in result.stderr
        else:
            data = json.loads(result.stdout)
            assert {row["name"] for row in data["packages"]} == {"consumer", "normal"}
        assert (consumer / "Cargo.lock").read_bytes() == before


def bound_profile(tmp_path):
    work = tmp_path / "workspace"
    work.mkdir()
    (work / "Cargo.toml").write_text('[workspace]\nmembers=[]\n')
    plan = {"toolchain": "fixture", "command": "cargo test --locked"}
    entry = {"kind": "locked-local-registry/v1", "platform": "x86_64-unknown-linux-gnu",
             "toolchain": "fixture", "plan_sha256": canonical_hash(plan)}
    ctx = SimpleNamespace(config={"migration": {local.OPTION: {"task": entry}}})
    environment = {"CARGO_HOME": str(tmp_path / "cargo"), "RUSTC": "fixture-rustc"}
    return ctx, {"id": "task"}, plan, work, entry, environment


def test_native_profile_keeps_default_behavior_and_attests_real_host(tmp_path, monkeypatch):
    ctx, task, plan, work, entry, env = bound_profile(tmp_path)
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="rustc fixture\nhost: x86_64-unknown-linux-gnu\n", stderr="")
    monkeypatch.setattr(local.subprocess, "run", run)
    result = local.profile(ctx, task, plan, work, [work / "Cargo.toml"], env)
    assert result["profile"] == entry and result["version_or_compiler_changes"] is False
    assert result["metadata_arguments"] == ["--filter-platform", entry["platform"], "--offline"]
    assert calls == [["fixture-rustc", "-vV"]]
    assert local.profile(SimpleNamespace(config={}), task, plan, work, [], env) is None
    assert calls == [["fixture-rustc", "-vV"]]


@pytest.mark.parametrize("change", ["plan", "compiler", "kind", "extra", "cross", "env-target",
                                  "config-target", "include", "unlocked"])
def test_native_profile_rejects_changed_or_cross_target_inputs(tmp_path, monkeypatch, change):
    ctx, task, plan, work, entry, env = bound_profile(tmp_path)
    if change == "plan":
        plan["command"] += " --all-features"
    elif change == "compiler":
        entry["toolchain"] = "new-compiler"
    elif change == "kind":
        entry["kind"] = "online-resolution"
    elif change == "extra":
        entry["unbound"] = True
    elif change == "cross":
        entry["platform"] = "aarch64-unknown-linux-gnu"
    elif change == "env-target":
        env["CARGO_BUILD_TARGET"] = "wasm32-unknown-unknown"
    elif change in ("config-target", "include"):
        (work / ".cargo").mkdir()
        (work / ".cargo/config.toml").write_text(
            '[build]\ntarget="wasm32-unknown-unknown"\n' if change == "config-target"
            else 'include=["another.toml"]\n')
    else:
        plan["command"] = "cargo test"
        entry["plan_sha256"] = canonical_hash(plan)
    monkeypatch.setattr(local.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout="host: x86_64-unknown-linux-gnu\n", stderr=""))
    with pytest.raises(ContractError):
        local.profile(ctx, task, plan, work, [work / "Cargo.toml"], env)
