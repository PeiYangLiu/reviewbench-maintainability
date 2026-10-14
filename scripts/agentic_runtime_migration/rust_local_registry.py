"""Keep exact locked registry archives/index entries in Cargo's native format.

An older Cargo scans every manifest in a directory source, even for crates that
are inactive on the original platform. A local registry lets that same Cargo
resolve the original index metadata and unpack only the packages it needs.
No package versions, published JSON fields or crate contents are rewritten.
"""
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tomllib

from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash, file_hash

MAX_INDEX_BYTES = 32 * 1024**2
OPTION = "rust_locked_registry_transports"


def index_path(name):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ContractError("invalid locked local-registry package name")
    name = name.lower()
    return (str(len(name)) + "/" + name if len(name) < 3 else
            "3/" + name[0] + "/" + name if len(name) == 3 else
            name[:2] + "/" + name[2:4] + "/" + name)


def checked_line(line, name, version, checksum):
    if (not isinstance(line, bytes) or not line.endswith(b"\n")
            or line.count(b"\n") != 1 or b"\0" in line or len(line) > 2 * 1024**2):
        raise ContractError("locked registry entry is not one bounded original JSON line")
    try:
        row = json.loads(line)
    except (ValueError, UnicodeError) as error:
        raise ContractError("locked registry entry has invalid JSON") from error
    if (not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum)
            or not isinstance(row, dict) or row.get("name") != name or row.get("vers") != version
            or row.get("cksum") != checksum):
        raise ContractError("locked registry entry differs from its package/version/checksum")
    return row


def sparse_index_entry(index, name, version, checksum):
    """Read one exact version from an existing Cargo v3 sparse-cache record."""
    index = Path(index)
    relative = index_path(name)
    if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9.+_-]+", version):
        raise ContractError("invalid locked sparse-index version")
    path = index / ".cache" / relative
    if (index.is_symlink() or not path.is_file() or path.is_symlink()
            or not path.resolve().is_relative_to(index.resolve())
            or any(p.is_symlink() for p in path.parents if p.is_relative_to(index))):
        raise ContractError("locked sparse index entry is absent or not an owned regular input")
    before = path.stat()
    with path.open("rb") as stream:
        data = stream.read(MAX_INDEX_BYTES + 1)
    after = path.stat()
    if (len(data) > MAX_INDEX_BYTES or
            (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
            (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
        raise ContractError("locked sparse index input changed or exceeds its bound")
    if data[:5] != b"\x03\x02\0\0\0":
        raise ContractError("unsupported Cargo sparse-index cache framing")
    fields = data[5:].split(b"\0")
    if (len(fields) < 4 or fields[-1] != b"" or (len(fields) - 2) % 2
            or not fields[0].startswith((b"etag:", b"last-modified:"))):
        raise ContractError("invalid Cargo sparse-index cache framing")
    wanted = version.encode()
    candidates = [fields[i + 1] for i in range(1, len(fields) - 1, 2) if fields[i] == wanted]
    if len(candidates) != 1:
        raise ContractError("locked sparse index version is missing or ambiguous")
    payload = candidates[0]
    line = payload + b"\n"  # Restore only the index line separator removed by Cargo's cache.
    checked_line(line, name, version, checksum)
    return line, {
        "source_cache_file": str(path.resolve()), "source_cache_sha256": hashlib.sha256(data).hexdigest(),
        "source_cache_bytes": len(data), "cache_framing": "cargo-sparse-cache-v3/index-v2",
        "original_json_sha256": hashlib.sha256(payload).hexdigest(),
        "original_line_sha256": hashlib.sha256(line).hexdigest(),
        "index_path": relative, "JSON_fields_rewritten": False,
    }


def materialize_package(root, name, version, checksum, archive, entry):
    """Append an exact locked archive and its byte-preserved index line."""
    root, archive = Path(root), Path(archive)
    if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9.+_-]+", version):
        raise ContractError("invalid locked local-registry version")
    line, provenance = entry
    checked_line(line, name, version, checksum)
    if not archive.is_file() or archive.is_symlink() or file_hash(archive) != checksum:
        raise ContractError("local-registry archive differs from its frozen checksum")
    target = root / (name + "-" + version + ".crate")
    index = root / "index" / index_path(name)
    if (root.is_symlink() or target.exists() or target.is_symlink() or index.is_symlink()
            or not index.resolve().is_relative_to(root.resolve())
            or any(p.is_symlink() for p in index.parents if p.is_relative_to(root))):
        raise ContractError("local-registry output already exists or is a link")
    index.parent.mkdir(parents=True, exist_ok=True)
    if index.exists():
        previous = [json.loads(value) for value in index.read_bytes().splitlines()]
        if any(row.get("name") != name or row.get("vers") == version for row in previous):
            raise ContractError("local-registry index has a duplicate or foreign version")
    shutil.copyfile(archive, target)
    target.chmod(0o644)
    with index.open("ab") as stream:
        stream.write(line)
    index.chmod(0o644)
    if file_hash(target) != checksum:
        raise ContractError("copied local-registry archive changed")
    return {
        "format": "locked-local-registry/v1", "archive": target.relative_to(root).as_posix(),
        "archive_sha256": checksum, "archive_bytes": target.stat().st_size,
        "index_path": index.relative_to(root).as_posix(),
        "index_line_sha256": hashlib.sha256(line).hexdigest(),
        "original_index": provenance, "files": 2,
    }


def requested(ctx, task):
    profiles = getattr(ctx, "config", {}).get("migration", {}).get(OPTION, {})
    if not isinstance(profiles, dict) or any(not isinstance(key, str) for key in profiles):
        raise ContractError("locked-registry transport profiles must be a task-keyed object")
    return task.get("id") in profiles


def profile(ctx, task, plan, workspace, manifests, environment):
    """Admit an explicit native-platform transport, not a compiler/feature change."""
    if not requested(ctx, task):
        return None
    value = ctx.config["migration"][OPTION][task["id"]]
    if (not isinstance(value, dict) or set(value) != {"kind", "platform", "toolchain", "plan_sha256"}
            or value["kind"] != "locked-local-registry/v1"
            or value["toolchain"] != plan.get("toolchain")
            or value["plan_sha256"] != canonical_hash(plan)
            or not isinstance(value["platform"], str)
            or not re.fullmatch(r"[A-Za-z0-9_]+(?:-[A-Za-z0-9_]+){2,}", value["platform"])):
        raise ContractError("locked-registry transport differs from its original plan/platform binding")
    words = shlex.split(plan["command"])
    if "--locked" not in words:
        raise ContractError("local registry transport requires the original locked command")
    targets = []
    for index, word in enumerate(words):
        if word == "--target":
            if index + 1 == len(words):
                raise ContractError("original Cargo target is missing")
            targets.append(words[index + 1])
        elif word.startswith("--target="):
            targets.append(word.split("=", 1)[1])
    for settings in (plan.get("env", {}), environment):
        if settings.get("CARGO_BUILD_TARGET"):
            targets.append(settings["CARGO_BUILD_TARGET"])
    configuration = {Path(environment["CARGO_HOME"]) / name for name in ("config", "config.toml")}
    workspace = Path(workspace).resolve()
    for manifest in manifests:
        for parent in [Path(manifest).parent, *Path(manifest).parent.parents]:
            if not parent.resolve().is_relative_to(workspace):
                break
            configuration.update(parent / ".cargo" / name for name in ("config", "config.toml"))
    for path in configuration:
        if path.is_symlink():
            raise ContractError("Cargo platform configuration must be a regular file")
        if path.is_file():
            document = tomllib.loads(path.read_text())
            if "include" in document:
                raise ContractError("Cargo included configuration needs its own platform attestation")
            target = document.get("build", {}).get("target")
            if target is not None:
                targets.extend(target if isinstance(target, list) else [target])
    command = [environment["RUSTC"], "-vV"]
    observed = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=30)
    hosts = [line.removeprefix("host: ") for line in observed.stdout.splitlines() if line.startswith("host: ")]
    if (observed.returncode or hosts != [value["platform"]]
            or any(target != value["platform"] for target in targets)):
        raise ContractError("metadata transport must use the original native compiler/build target")
    return {
        "profile": dict(value), "original_plan_sha256": canonical_hash(plan),
        "metadata_arguments": ["--filter-platform", value["platform"], "--offline"],
        "rustc_host_probe": {"command": command, "stdout": observed.stdout,
                             "stderr": observed.stderr, "exit_status": observed.returncode},
        "complete_locked_registry_graph_retained": True, "version_or_compiler_changes": False,
    }


def sparse_index_root(cache):
    candidates = []
    for path in (Path(cache) / "registry/index").glob("index.crates.io-*"):
        config = path / "config.json"
        if path.is_symlink() or config.is_symlink() or not config.is_file():
            continue
        document = json.loads(config.read_text())
        if (path / ".cache").is_dir() and document.get("dl") == "https://static.crates.io/crates":
            candidates.append(path)
    if len(candidates) != 1:
        raise ContractError("local registry needs one unambiguous original crates.io sparse index")
    return candidates[0]


def graph_signature(metadata):
    """Compare resolved identities/features, not transport-specific source paths."""
    packages = metadata.get("packages", [])
    resolve = metadata.get("resolve")
    if not packages or not isinstance(resolve, dict) or not resolve.get("nodes"):
        raise ContractError("native registry metadata has no resolved package graph")
    nodes = []
    for node in resolve["nodes"]:
        deps = [{**dep, "dep_kinds": sorted(dep["dep_kinds"], key=canonical_hash)}
                for dep in node.get("deps", [])]
        nodes.append({
            "id": node["id"], "dependencies": sorted(node["dependencies"]),
            "deps": sorted(deps, key=canonical_hash), "features": sorted(node["features"]),
        })
    return canonical_hash({
        "packages": sorted((row["id"], row["name"], row["version"], row.get("source")) for row in packages),
        "root": resolve.get("root"), "nodes": sorted(nodes, key=lambda row: row["id"]),
    })
