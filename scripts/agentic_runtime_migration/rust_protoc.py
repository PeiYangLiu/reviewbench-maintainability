"""Restore fixed-task Rust protoc from already-cached build-tool artifacts.

This is a preparation-only reader: no network, PATH discovery, package manager,
or mutable version selection. Relocation overrides change paths, never pins.
The same read-only binary/headers serve public development and private grading.
"""
from __future__ import annotations

from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import zipfile

from reviewbench_maintainability.agentic.contracts import ContractError, atomic_json, file_hash, repo_path
from reviewbench_maintainability.agentic.resources import file_lock

TARGET = "/opt/reviewbench-assets/protoc"
VERSION = "3.19.2"
PINS = {
    "binary": {"name": "protoc/3.19.2/protoc-3.19.2-linux-x86_64.exe",
               "sha256": "c91ff126c0ee68bd441ae7df7017e4524c1b59ff15838e1fcc91e91dde8b4287"},
    "headers": {"name": "protobuf-java/3.19.2/protobuf-java-3.19.2.jar",
                "sha256": "3446cbfa13795228bc6549b91a409f27cdf6913d1c8f03e0f99572988623a04b"},
}
HEADER_NAMES = frozenset(
    "google/protobuf/" + name + ".proto" for name in (
        "any", "api", "compiler/plugin", "descriptor", "duration", "empty", "field_mask",
        "source_context", "struct", "timestamp", "type", "wrappers",
    )
)
VECTOR_TASK = "github__vectordotdev__vector__pull_17037__comment_1157500473__duplication"
CHROMA_TASK = "github__chroma-core__chroma__pull_2934__comment_1814200147__api_design"
TASKS = frozenset({VECTOR_TASK, CHROMA_TASK})


def build_asset(ctx):
    """Read fixed local bytes and retain an actual executable version probe."""
    cached = (ctx.legacy / "jvm/cache/home/.cache/coursier/https/repo1.maven.org/maven2/com/google/protobuf")
    overrides = ctx.config.get("rust_protoc_assets", {})
    if not isinstance(overrides, dict) or set(overrides) - PINS.keys():
        raise ContractError("Rust protoc relocation accepts only binary/headers paths")
    inputs = {}
    for key, pin in PINS.items():
        source = Path(overrides.get(key, cached / pin["name"]))
        if not source.is_file() or source.is_symlink() or file_hash(source) != pin["sha256"]:
            raise ContractError("fixed local Rust protoc asset is absent or has changed: " + key)
        inputs[key] = source
    with file_lock(ctx.output / "locks" / ("rust-protoc-" + VERSION)):
        with tempfile.TemporaryDirectory(prefix="rust-protoc-", dir=ctx.scratch) as temporary:
            root = Path(temporary)
            binary = root / "bin/protoc"
            binary.parent.mkdir()
            shutil.copyfile(inputs["binary"], binary)
            binary.chmod(0o755)
            if file_hash(binary) != PINS["binary"]["sha256"]:
                raise ContractError("staged Rust protoc binary differs from its pin")
            headers = {}
            with zipfile.ZipFile(inputs["headers"]) as archive:
                names = [name for name in archive.namelist() if name.startswith("google/protobuf/") and name.endswith(".proto")]
                if len(names) != len(HEADER_NAMES) or set(names) != HEADER_NAMES:
                    raise ContractError("fixed protoc header archive has an unexpected include set")
                for name in sorted(names):
                    target = root / "include" / repo_path(name)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(name))
                    headers[name] = file_hash(target)
            # Check the original inputs again: a concurrent cache writer must
            # not turn an earlier checksum into attestation of different bytes.
            if any(file_hash(inputs[key]) != pin["sha256"] for key, pin in PINS.items()):
                raise ContractError("fixed Rust protoc asset changed during staging")
            command = [str(binary), "--version"]
            result = subprocess.run(command, capture_output=True, text=True, timeout=30,
                                    env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
            probe = {"command": command, "stdout": result.stdout, "stderr": result.stderr,
                     "exit_status": result.returncode, "version": VERSION,
                     "inputs": {key: {"path": str(inputs[key]), **pin} for key, pin in PINS.items()},
                     "header_sha256": headers, "scoring_result": False}
            atomic_json(ctx.output / "rust-protoc" / ("probe-" + VERSION + ".json"), probe)
            if result.returncode or result.stdout.strip() != "libprotoc " + VERSION:
                raise ContractError("fixed Rust protoc executable failed its version probe")
            atomic_json(root / "pins.json", {"version": VERSION, "artifacts": PINS, "header_sha256": headers})
            return ctx.bundle(root, role="toolchain")


def binding(ctx, task):
    if task["id"] not in TASKS:
        return [], {}, []
    if task["id"] == CHROMA_TASK and (
            task.get("repo") != "chroma-core/chroma"
            or task.get("start_sha") != "db611257f776fd05be2b1068d80feabeaa2b9381"):
        raise ContractError("Chroma protoc binding differs from the original task/start")
    asset = build_asset(ctx)
    env = {"PROTOC": TARGET + "/bin/protoc", "PROTOC_INCLUDE": TARGET + "/include"}
    probe = "test \"$(" + shlex.quote(env["PROTOC"]) + " --version)\" = " + shlex.quote("libprotoc " + VERSION)
    return [{**asset, "kind": "archive", "visibility": "developer", "target": TARGET}], env, [probe]
