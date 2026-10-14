"""Native file-based source configuration for old stable Cargo releases.

This changes only delivery of the vendored-source configuration. Cargo, rustc,
manifest files, project configuration precedence, and test arguments stay native.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

SOURCE_TARGET = "/opt/reviewbench-assets/cargo-sources.toml"
INSTALLER_TARGET = "/opt/reviewbench-assets/rust-cargo-home.py"
HOME_TARGET = "/opt/reviewbench-cache/cargo/config"
LEGACY_STABLE = {"1.59.0", "1.60.0"}
UNSTABLE_ERROR = (
    "error: the `--config` flag is unstable, and only available on the nightly channel of Cargo, "
    "but this is the `stable` channel"
)


def install(source: Path, target: Path) -> str:
    """Atomically replace the runtime-cache config, never a link's destination."""
    source, target = Path(source), Path(target)
    if source.is_symlink() or not source.is_file():
        raise ValueError("source configuration must be a regular immutable asset")
    if any(parent.is_symlink() for parent in (target.parent, *target.parent.parents)):
        raise ValueError("Cargo home configuration has a linked parent")
    if not target.parent.is_dir():
        raise ValueError("Cargo home directory does not exist")
    with source.open("rb") as stream:
        data = stream.read(65537)
    if not data or len(data) > 65536:
        raise ValueError("source configuration exceeds its bounded file size")
    descriptor, temporary = tempfile.mkstemp(prefix=".reviewbench-config-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o644)
        os.replace(temporary, target)
        parent = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    if target.is_symlink() or target.read_bytes() != data:
        raise ValueError("installed source configuration differs from the immutable asset")
    return hashlib.sha256(data).hexdigest()


def needs_home_config(ctx, version):
    """Use an observed CLI limitation, not a compiler/channel substitution."""
    from reviewbench_maintainability.agentic.contracts import ContractError, atomic_json, file_hash

    if version not in LEGACY_STABLE:
        return False
    registry = Path(ctx.config.get("rust_toolchains", ctx.output.parent / "rust-toolchains/toolchains.json"))
    cargo = Path(json.loads(registry.read_text())[version]["root"]) / "bin/cargo"
    probes = []
    with tempfile.TemporaryDirectory(prefix="stable-cargo-config-", dir=ctx.scratch) as temporary:
        root = Path(temporary)
        manifest = root / "Cargo.toml"
        manifest.write_text('[package]\nname="reviewbench-config-probe"\nversion="0.0.0"\n')
        for command in (
            [str(cargo), "--version"],
            [str(cargo), "--offline", "--config", "net.offline=true",
             "locate-project", "--manifest-path", str(manifest)],
        ):
            process = subprocess.run(command, capture_output=True, text=True, timeout=30)
            probes.append({"command": command, "stdout": process.stdout, "stderr": process.stderr,
                           "exit_status": process.returncode})
        first, second = probes
        error_lines = second["stderr"].splitlines()
        if first["exit_status"] or not first["stdout"].startswith("cargo " + version + " "):
            raise ContractError("old stable Cargo version differs from its pinned registry")
        if second["exit_status"] == 0:
            try:
                assert json.loads(second["stdout"])["root"] == str(manifest)
            except (ValueError, KeyError, AssertionError) as error:
                raise ContractError("Cargo source-config probe returned unexpected output") from error
            mode = "cli"
        elif (second["exit_status"] == 101 and second["stdout"] == ""
              and error_lines and error_lines[0] == UNSTABLE_ERROR
              and all(not line or line.startswith(("See https://", "See http://"))
                      for line in error_lines[1:])):
            mode = "cargo-home-file"
        else:
            raise ContractError("Cargo source-config probe failed outside its known CLI boundary")
    atomic_json(ctx.output / "rust-cli-probes" / (version + "-source-config.json"), {
        "toolchain": version, "cargo_sha256": file_hash(cargo), "probes": probes, "mode": mode,
        "compiler_channel_overrides": {}, "source_or_test_commands": 0,
    })
    return mode == "cargo-home-file"


def bind(ctx, version, source_config, original_options):
    """Return native options, immutable file assets, and cache-only bootstrap."""
    from reviewbench_maintainability.agentic.contracts import ContractError, file_hash
    import tomllib

    if not needs_home_config(ctx, version):
        return original_options, [], []
    assets = []
    if source_config is None:
        source = ctx.output / "rust-stable-source-config.toml"
        source.write_text(
            '[source.crates-io]\nreplace-with="reviewbench-vendor"\n'
            '[source.reviewbench-vendor]\ndirectory="/opt/reviewbench-assets/vendor"\n')
        source_config = ctx.copy_asset(source, "cargo-source-config")
        assets.append({**source_config, "kind": "file", "visibility": "developer",
                       "target": SOURCE_TARGET})
    source = Path(source_config["path"])
    if file_hash(source) != source_config["sha256"]:
        raise ContractError("old Cargo source configuration changed")
    data = tomllib.loads(source.read_text())
    if set(data) != {"source"} or not isinstance(data["source"], dict) or not data["source"]:
        raise ContractError("old Cargo source configuration must contain only source mappings")
    installer = ctx.copy_asset(Path(__file__), "rust-cargo-home")
    assets.append({**installer, "kind": "file", "visibility": "developer", "target": INSTALLER_TARGET})
    command = shlex.join(["python3", INSTALLER_TARGET, "--source", SOURCE_TARGET, "--target", HOME_TARGET])
    return ["--offline"], assets, ["mkdir -p /opt/reviewbench-cache/cargo && " + command]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    args = parser.parse_args()
    if str(args.source) != SOURCE_TARGET or str(args.target) != HOME_TARGET:
        parser.error("the runtime installer accepts only its fixed asset/cache paths")
    print("CARGO_SOURCE_CONFIG_INSTALLED sha256=" + install(args.source, args.target), flush=True)


if __name__ == "__main__":
    main()
