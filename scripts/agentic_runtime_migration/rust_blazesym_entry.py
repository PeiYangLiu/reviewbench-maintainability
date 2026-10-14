#!/usr/bin/env python3
"""Blazesym56: authenticated local registry, dual metadata, current native cache.

The modern Cargo observes the complete lock only. The original Cargo alone
executes check/test. No resolver, test outcome or reference label picks inputs.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tarfile
import time
import sys
import uuid

if __package__:
    from . import rust_first_resolution_entry as common
    from . import rust_test_compat_entry as guard
    from .rust_build_entry import OverlayError, candidate_files, synchronize_candidate, tomllib
else:
    import rust_first_resolution_entry as common
    import rust_test_compat_entry as guard
    from build import OverlayError, candidate_files, synchronize_candidate, tomllib

TASK = "github__libbpf__blazesym__pull_70__comment_1134655780__complexity"
PROFILE = "blazesym56-local-registry-native-v1"
SCHEMA = "reviewbench-blazesym-locked-local-registry/v1"
PRIVATE_SCHEMA = "reviewbench-blazesym-fixed-oracle/v1"
REGISTRY_SCHEMA = "reviewbench-locked-local-registry/v1"
REGISTRY_SOURCE = common.REGISTRY
REAL_CARGO = "/opt/reviewbench-assets/rust/bin/cargo"
REAL_RUSTC = "/opt/reviewbench-assets/rust/bin/rustc"
METADATA_CARGO = "/opt/reviewbench-assets/rust-metadata/bin/cargo"
METADATA_RUSTC = "/opt/reviewbench-assets/rust-metadata/bin/rustc"
REGISTRY = "/opt/reviewbench-assets/vendor"
REGISTRY_MANIFEST = "/opt/reviewbench-assets/blazesym-registry-manifest.json"
SOURCE_CONFIG = "/opt/reviewbench-assets/cargo-sources.toml"
DEFAULT_LOCK = "/opt/reviewbench-assets/rust-new-lock-0"
CARGO_HOME = "/opt/reviewbench-cache/cargo"
METADATA_CARGO_HOME = "/opt/reviewbench-cache/cargo-metadata"
NATIVE_PATHS = {"cc": "/usr/bin/cc", "strip": "/usr/bin/strip",
                "llvm-gsymutil-14": "/usr/bin/llvm-gsymutil-14"}
COMMAND = ["test", "--test", "blazesym", "--", "--nocapture"]
HARNESS = {"path": "tests/blazesym.rs",
           "sha256": "e62189d11fda38a6fcbd04f995fe18d36a67746858d5edb3cab13fcdd08db658"}
EXPECTED = ["symbolize_dwarf", "symbolize_gsym"]
FIXTURES = {
    "data/test.c": "4675050e64b9a0ce3fb1c68070d4ef05dfe7fd9568c523746f618a2de47ea2cd",
    "data/test-stable-addresses.c": "d89f28e1b29056b145f9997603ae373b4c70ca0efdb88081a8abe60ef9d10730",
    "data/test-stable-addresses.ld": "62683fcc1a8f0aefeba8faa85491bb96319db63b07133aba3de3aa462d4b6bab",
}
GENERATED = ("data/test-no-debug.bin", "data/test-dwarf-v4.bin",
             "data/test-stable-addresses.bin", "data/test.gsym", "data/test-dwarf.bin")
EVIDENCE_PREFIX = "ReviewBench Blazesym locked registry evidence: "
MAX_BYTES = 33554432
sha = common.digest
file_hash = common.file_hash
strict_json = common.strict_json
input_bytes = common.input_bytes
inside = common.inside


def require(value, message):
    common.require(value, "Blazesym: " + message)


def hash_value(value):
    return sha(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def valid_sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)


def validate(spec, private=None):
    require(isinstance(spec, dict) and set(spec) == {
        "schema_version", "task_id", "profile", "toolchain", "metadata_toolchain",
        "cargo_sha256", "rustc_sha256", "metadata_cargo_sha256", "metadata_rustc_sha256",
        "cpus", "max_log_bytes", "metadata_timeout_seconds", "command_timeout_seconds",
        "receipt_sha256", "default_lock", "registry_manifest_sha256", "source_config_sha256", "native_tools",
    }, "unknown public specification fields")
    require(spec["schema_version"] == SCHEMA and spec["task_id"] == TASK and spec["profile"] == PROFILE
            and spec["toolchain"] == "1.68.2" and spec["metadata_toolchain"] == "1.93.0",
            "wrong task/profile or compiler identities")
    for key, value in (("cpus", 4), ("max_log_bytes", MAX_BYTES),
                       ("metadata_timeout_seconds", 120), ("command_timeout_seconds", 1800)):
        require(type(spec[key]) is int and spec[key] == value, "changed resource bound: " + key)
    require(all(valid_sha(spec[key]) for key in (
        "cargo_sha256", "rustc_sha256", "metadata_cargo_sha256", "metadata_rustc_sha256",
        "receipt_sha256", "registry_manifest_sha256", "source_config_sha256")), "unbound tool/receipt/config")
    default = spec["default_lock"]
    require(isinstance(default, dict) and set(default) == {"asset_target", "sha256"}
            and default["asset_target"] == DEFAULT_LOCK and valid_sha(default["sha256"]),
            "invalid single missing-only default")
    tools = spec["native_tools"]
    require(isinstance(tools, dict) and set(tools) == set(NATIVE_PATHS), "wrong native tool set")
    for name, path in NATIVE_PATHS.items():
        row = tools[name]
        require(isinstance(row, dict) and set(row) == {"path", "sha256"}
                and row["path"] == path and valid_sha(row["sha256"]), "unbound native tool: " + name)
    if private is not None:
        require(private == {"schema_version": PRIVATE_SCHEMA, "task_id": TASK,
                            "command": COMMAND, "harness": HARNESS,
                            "expected_tests": EXPECTED, "fixtures": FIXTURES},
                "changed original private command/harness/fixtures/test identities")


def source_snapshot(root, *, derived=False):
    files = candidate_files(root)
    require("Cargo.toml" in files, "missing complete current root manifest")
    require(not any("target" in Path(n).parts for n in files), "candidate target output is not a dependency")
    result = {}
    for name, row in guard.fingerprint(root).items():
        path = files[name]
        require(row[0] == "file" and not path.is_symlink(), "source/generated symlink is forbidden: " + name)
        inside(root, path)
        if Path(name).name in {"Cargo.toml", "Cargo.lock"}:
            tomllib.loads(input_bytes(path).decode())
        if derived or name not in GENERATED:
            result[name] = {"sha256": row[1], "mode": path.stat().st_mode & 0o777}
    return result


def generated_snapshot(root):
    result = {}
    for name in GENERATED:
        path = root / name
        inside(root, path, regular=False)
        if path.exists():
            require(path.is_file() and not path.is_symlink(), "non-file generated output: " + name)
            result[name] = {"sha256": file_hash(path), "bytes": path.stat().st_size,
                            "mode": path.stat().st_mode & 0o777}
    return result


def verify_private(source, private):
    if private is not None:
        for name, expected in {private["harness"]["path"]: private["harness"]["sha256"],
                               **private["fixtures"]}.items():
            require(file_hash(inside(source, source / name)) == expected, "original fixed input changed: " + name)


def select_package(source):
    document = tomllib.loads(input_bytes(source / "Cargo.toml").decode())
    manifests = (common.workspace_manifests(source)[0] if "workspace" in document
                 else [(source / "Cargo.toml", document)])
    matches = [path for path, doc in manifests if doc.get("package", {}).get("name") == "blazesym"]
    require(len(matches) == 1, "missing/ambiguous current blazesym package")
    return matches[0].relative_to(source).as_posix()


def all_files(root):
    """No ignored asset binders or symlink traversal."""
    result = {}
    require(root.is_dir() and not root.is_symlink(), "missing/non-directory asset root")
    for directory, dirs, files in os.walk(root, followlinks=False):
        require(not any((Path(directory) / d).is_symlink() for d in dirs), "asset directory symlink")
        for name in files:
            path = Path(directory) / name
            require(path.is_file() and not path.is_symlink(), "asset symlink/special file")
            result[path.relative_to(root).as_posix()] = path
    return result


def index_path(name):
    lower = name.lower()
    return (str(len(lower)) + "/" + lower if len(lower) < 3 else
            "3/" + lower[0] + "/" + lower if len(lower) == 3 else lower[:2] + "/" + lower[2:4] + "/" + lower)


def archive_inventory(path, name, version):
    records, expanded = {}, 0
    prefix = name + "-" + version
    with tarfile.open(path, "r:*") as archive:
        for member in archive:
            parts = PurePosixPath(member.name).parts
            require(parts and parts[0] == prefix and ".." not in parts and not member.name.startswith("/"),
                    "archive path escapes its exact package")
            if member.isdir():
                continue
            require(member.isfile() and len(parts) > 1 and member.size <= 128 * 1024**2,
                    "archive link/special/oversize member")
            expanded += member.size
            require(expanded <= 1024**3 and len(records) < 100000, "archive expansion exceeds bound")
            relative = PurePosixPath(*parts[1:]).as_posix()
            require(relative != ".cargo-ok", "archive collides with Cargo's generated unpack marker")
            stream = archive.extractfile(member)
            value = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024**2), b""):
                value.update(chunk)
            row = {"sha256": value.hexdigest(), "bytes": member.size, "mode": member.mode & 0o777}
            # Some retained crates contain identical duplicate tar entries.
            # Cargo handles those; conflicting bytes/modes are still rejected.
            require(relative not in records or records[relative] == row, "conflicting duplicate archive member")
            records[relative] = row
    require("Cargo.toml" in records, "archive lacks its package manifest")
    return records


def verify_registry(spec, registry, manifest_path):
    data = input_bytes(manifest_path)
    require(sha(data) == spec["registry_manifest_sha256"], "registry manifest changed")
    manifest = strict_json(data)
    require(isinstance(manifest, dict) and set(manifest) == {"schema_version", "source", "files", "packages"}
            and manifest["schema_version"] == REGISTRY_SCHEMA and manifest["source"] == REGISTRY_SOURCE,
            "wrong sanitized local-registry manifest")
    rows, declared = manifest["packages"], manifest["files"]
    require(isinstance(rows, list) and len(rows) == 55 and isinstance(declared, dict),
            "all 55 retained registry archives are required")
    actual = all_files(registry)
    require(set(actual) == set(declared), "registry files are missing/extra")
    for name, path in actual.items():
        expected = declared[name]
        require(isinstance(expected, dict) and set(expected) == {"sha256", "bytes", "mode"}
                and valid_sha(expected["sha256"]) and type(expected["bytes"]) is int
                and type(expected["mode"]) is int, "invalid registry file binding")
        require(path.stat().st_size == expected["bytes"] and path.stat().st_mode & 0o777 == expected["mode"]
                and file_hash(path) == expected["sha256"], "registry file bytes/mode changed: " + name)
    packages, expected_files, index_rows = {}, set(), {}
    for row in rows:
        require(isinstance(row, dict) and set(row) == {
            "name", "version", "cksum", "index_path", "original_line_sha256"
        } and re.fullmatch(r"[A-Za-z0-9_-]+", row["name"])
            and re.fullmatch(r"[A-Za-z0-9_.+-]+", row["version"])
            and valid_sha(row["cksum"]) and valid_sha(row["original_line_sha256"]),
            "invalid exact registry package binding")
        name, version = row["name"], row["version"]
        key = name, version, REGISTRY_SOURCE
        require(key not in packages and row["index_path"] == index_path(name), "duplicate/wrong index identity")
        archive_name, entry_name = name + "-" + version + ".crate", "index/" + row["index_path"]
        require(archive_name in declared and entry_name in declared
                and declared[archive_name]["sha256"] == row["cksum"], "archive differs from selected index checksum")
        expected_files.update([archive_name, entry_name])
        lines = index_rows.setdefault(entry_name, actual[entry_name].read_bytes().splitlines(keepends=True))
        matching = [line for line in lines if (lambda v: v.get("name") == name and v.get("vers") == version)(
            strict_json(line))]
        require(len(matching) == 1 and sha(matching[0]) == row["original_line_sha256"]
                and strict_json(matching[0]).get("cksum") == row["cksum"],
                "selected original index JSON bytes changed")
        packages[key] = {"checksum": row["cksum"], "archive": str(actual[archive_name]),
                         "files": archive_inventory(actual[archive_name], name, version)}
    require(expected_files == set(actual), "unbound registry metadata/file")
    for entry_name, lines in index_rows.items():
        keys = [(strict_json(line).get("name"), strict_json(line).get("vers"), REGISTRY_SOURCE) for line in lines]
        require(len(set(keys)) == len(keys) and all(key in packages for key in keys),
                "extra/unavailable registry index version")
    return packages, {"sha256": sha(data), "files": len(actual), "archives": len(packages),
                       "bytes": sum(p.stat().st_size for p in actual.values())}


def verify_configuration(spec, registry, path):
    data = input_bytes(path)
    require(sha(data) == spec["source_config_sha256"], "source TOML bytes changed")
    document = tomllib.loads(data.decode())
    require(document == {
        "source": {"crates-io": {"replace-with": "reviewbench-locked-local"},
                   "reviewbench-locked-local": {"local-registry": str(registry)}},
        "net": {"offline": True},
    } and document["net"]["offline"] is True, "source TOML changes registry identity/offline policy")
    return {"path": str(path), "sha256": sha(data), "document": document}


def unpacked_packages(graph, home, archives, toolchain):
    result = {}
    for package in graph.get("packages", []):
        if package.get("source") is None:
            continue
        key = package["name"], package["version"], package["source"]
        require(key in archives, "current metadata package is absent from provided registry")
        path = inside(home, Path(package.get("manifest_path", "")))
        relative = path.relative_to(home).parts
        require(len(relative) == 5 and relative[:2] == ("registry", "src")
                and re.fullmatch(r"[A-Za-z0-9_-]+", relative[2])
                and relative[3:] == (key[0] + "-" + key[1], "Cargo.toml"),
                "metadata manifest is outside its own Cargo unpacked source")
        folder = path.parent
        files = all_files(folder)
        require(set(files) == set(archives[key]["files"]) | {".cargo-ok"}, "unpacked archive files missing/extra")
        marker = input_bytes(files[".cargo-ok"])
        require(marker == b"ok" if toolchain == "1.68.2" else strict_json(marker) == {"v": 1},
                "invalid Cargo unpack-completion marker")
        for name, expected in archives[key]["files"].items():
            item = files[name]
            require(item.stat().st_size == expected["bytes"] and file_hash(item) == expected["sha256"]
                    and item.stat().st_mode & 0o777 == expected["mode"], "unpacked registry source changed: " + name)
        document = tomllib.loads(input_bytes(path).decode()).get("package", {})
        require((document.get("name"), document.get("version")) == key[:2], "unpacked manifest identity changed")
        result[key] = str(path)
    return result


def keyed_graph(graph):
    require(isinstance(graph, dict) and isinstance(graph.get("resolve"), dict), "malformed metadata object/resolve")
    rows, nodes = graph.get("packages"), graph["resolve"].get("nodes")
    require(isinstance(rows, list) and isinstance(nodes, list) and rows and len(rows) == len(nodes),
            "incomplete metadata package/node arrays")
    require(all(isinstance(p, dict) and isinstance(p.get("id"), str) for p in rows + nodes),
            "malformed metadata package/node")
    ids = {p["id"]: (p["name"], p["version"], p.get("source")) for p in rows}
    require(len(ids) == len(rows) == len(set(ids.values())) == len({n["id"] for n in nodes})
            and set(ids) == {n["id"] for n in nodes}, "duplicate/foreign metadata identities")
    return ids, {ids[p["id"]]: p for p in rows}, {ids[n["id"]]: n for n in nodes}


def edge_set(node, ids):
    require(isinstance(node.get("dependencies"), list) and isinstance(node.get("deps"), list),
            "missing metadata dependency edges")
    result = set()
    for dep in node["deps"]:
        require(isinstance(dep, dict) and dep.get("pkg") in ids and isinstance(dep.get("name"), str)
                and isinstance(dep.get("dep_kinds"), list) and dep["dep_kinds"],
                "invalid selected graph dependency")
        for kind in dep["dep_kinds"]:
            require(isinstance(kind, dict) and set(kind) == {"kind", "target"}
                    and kind["kind"] in {None, "build", "dev"}
                    and (kind["target"] is None or isinstance(kind["target"], str)), "bad selected edge kind")
            result.add((ids[dep["pkg"]], dep["name"], kind["kind"], kind["target"]))
    return result


def selected_graph(graph, full, root, lock, vendor):
    """Validate a selected subgraph explicitly; never call it the full lock."""
    ids, rows, nodes = keyed_graph(graph)
    full_ids, full_rows, full_nodes = keyed_graph(full)
    require(set(rows) <= set(full_rows), "old Cargo selected an identity outside the complete current lock")
    members = {full_ids[p] for p in full["workspace_members"]}
    require({ids[p] for p in graph.get("workspace_members", [])} == members,
            "old Cargo omitted/changed a current workspace member")
    projected = {}
    for key, node in nodes.items():
        row, all_row = rows[key], full_rows[key]
        require(row.get("features") == all_row.get("features"), "old/current feature declarations differ")
        require(set(node.get("features", [])) <= set(full_nodes[key]["features"]),
                "old selected features escape the complete all-features graph")
        active, complete = edge_set(node, ids), edge_set(full_nodes[key], full_ids)
        require(active <= complete, "old selected dependency edge is not in the full graph")
        declarations = all_row.get("dependencies")
        require(isinstance(declarations, list), "complete metadata lacks dependency declarations")
        activated = set()
        for feature in node.get("features", []):
            for value in all_row["features"].get(feature, []):
                if value.startswith("dep:"):
                    activated.add(value[4:].replace("-", "_"))
                elif "/" in value and "?/" not in value:
                    activated.add(value.split("/", 1)[0].replace("-", "_"))
        for dependency, alias, kind, target in complete:
            declared = [d for d in declarations
                        if (d.get("rename") or d.get("name", "")).replace("-", "_") == alias
                        and d.get("kind") == kind and d.get("target") == target]
            require(declared, "complete graph edge has no current dependency declaration")
            required = any(not d.get("optional") or alias in activated for d in declared)
            if kind == "dev" and key not in members:
                required = False
            if required:
                require((dependency, alias, kind, target) in active, "old graph omitted a required current edge")
        projected[key] = dict(lock[key], dependencies=[
            f"{child[0]} {child[1]}" + (" (" + child[2] + ")" if child[2] else "")
            for child in {ids[p] for p in node["dependencies"]}])
    summary = common.verify_graph(graph, root, projected, vendor)
    summary.update(kind="original-toolchain-selected-subgraph-not-full-lock",
                   complete_lock_packages=len(lock), full_graph_packages=len(full_rows))
    return summary


def selected_target(full, old, root, private):
    full_ids, full_rows, _ = keyed_graph(full)
    ids, rows, nodes = keyed_graph(old)
    defaults = full.get("workspace_default_members")
    require(isinstance(defaults, list) and defaults and all(p in full_ids for p in defaults),
            "modern metadata lacks complete current default package selection")
    keys = {full_ids[p] for p in defaults}
    selected = [key for key in keys if key[0] == "blazesym" and key[2] is None]
    require(len(selected) == 1 and selected[0] in rows, "original check/test does not select current blazesym")
    key = selected[0]
    features = set(nodes[key]["features"])
    require(not (features & {"nightly", "dont-generate-test-files"}), "original default features were disabled/changed")
    if private is not None:
        require("generate-test-files" in features, "original self-dev fixture-generation feature is missing")
        matches = [(k, target) for k in keys if k in rows for target in rows[k]["targets"]
                   if target.get("name") == "blazesym" and target.get("kind") == ["test"]]
        require(len(matches) == 1 and matches[0][0] == key and matches[0][1].get("test") is True
                and inside(root, Path(matches[0][1]["src_path"])) == inside(root, root / HARNESS["path"]),
                "original blazesym test target redirected/disabled/omitted")
        verify_private(root, private)
    return {"package": key, "default_packages": sorted(keys), "features": sorted(features)}


def metadata_record(record, report, registry, archives):
    remaining, observed = [], set()
    for line in record.get("stderr", "").splitlines(keepends=True):
        match = re.fullmatch(r"\s*Unpacking ([A-Za-z0-9_-]+) v([A-Za-z0-9_.+-]+) \(registry `([^`]+)`\)\s*", line)
        if match:
            name, version, path = match.groups()
            key = name, version, REGISTRY_SOURCE
            require(key in archives and path == str(registry) and key not in observed,
                    "unbound/duplicate local-registry Unpacking diagnostic")
            observed.add(key)
        else:
            remaining.append(line)
    text = "".join(remaining)
    # Known data-bearing unpack lines are accounted for, not discarded from
    # the record. Actual error/signal lines still reach the shared guard.
    guard.infrastructure({**record, "stdout": "", "stderr": text}, report)
    if common.FONT_RESOLVER_WARNING in text:
        require(text.count(common.FONT_RESOLVER_WARNING) == 1, "duplicate resolver warning")
        text = text.replace(common.FONT_RESOLVER_WARNING, "", 1)
    require(record["returncode"] == 0 and record.get("stdout_utf8_valid", True)
            and record.get("stderr_utf8_valid", True) and common.metadata_stderr_allowed(text),
            "complete original metadata process failed")
    graph = strict_json(record["stdout"])
    keyed_graph(graph)
    return graph


def transport(command, private=None):
    prefix = ["--offline", "--config", SOURCE_CONFIG]
    tail = COMMAND if private is not None else ["check"]
    require(command == ["cargo", *prefix, *tail], "changed original Cargo command/flags")
    index = command.index("--") if "--" in command else len(command)
    return [REAL_CARGO, *command[1:index], "--locked", "--offline", *command[index:]]


def environment(spec, home, target, *, modern=False):
    path = os.pathsep.join(dict.fromkeys([
        *[str(Path(row["path"]).parent) for row in spec["native_tools"].values()],
        str(Path(METADATA_CARGO if modern else REAL_CARGO).parent), "/usr/bin", "/bin"]))
    for name in ("cc", "strip"):
        found = shutil.which(name, path=path)
        require(found and Path(found).resolve() == Path(spec["native_tools"][name]["path"]).resolve(),
                "literal native PATH command is shadowed: " + name)
    require(not any(os.environ.get(k) for k in ("RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER",
                                               "CARGO_BUILD_RUSTC_WRAPPER", "CARGO_BUILD_RUSTC_WORKSPACE_WRAPPER")),
            "unbound compiler wrapper environment")
    result = {**os.environ, "PATH": path, "CARGO_HOME": str(home), "CARGO_TARGET_DIR": str(target),
              "RUSTC": METADATA_RUSTC if modern else REAL_RUSTC, "CARGO_BUILD_JOBS": "4",
              "CARGO_NET_OFFLINE": "true", "CARGO_NET_RETRY": "0", "RUSTUP_AUTO_INSTALL": "0",
              "LLVM_GSYMUTIL": spec["native_tools"]["llvm-gsymutil-14"]["path"]}
    return result


def tools_verified(spec):
    result = {}
    for field, path in (("cargo_sha256", REAL_CARGO), ("rustc_sha256", REAL_RUSTC),
                        ("metadata_cargo_sha256", METADATA_CARGO), ("metadata_rustc_sha256", METADATA_RUSTC)):
        require(file_hash(path) == spec[field], "authenticated Rust tool changed: " + field)
        result[field] = spec[field]
    for name, row in spec["native_tools"].items():
        require(Path(row["path"]).is_file() and os.access(row["path"], os.X_OK)
                and file_hash(row["path"]) == row["sha256"], "native tool changed: " + name)
    return {**result, "native_tools": spec["native_tools"]}


def cache_directory(path, *, within=None):
    """Check every existing component, without accepting a symlink ancestor."""
    path = Path(path).absolute()
    require(".." not in path.parts, "non-normal cache directory")
    if within is not None:
        require(path.is_relative_to(Path(within).absolute()), "cache directory escapes its owned root")
    for component in reversed([path, *path.parents]):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        require(stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode),
                "cache directory component is a symlink/non-directory: " + str(component))
    return path


def bookkeeping_file(path):
    cache_directory(path.parent)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
            "cache bookkeeping path is not an owned regular file: " + str(path))


def cache_paths(build_root, *, create_targets=False):
    cache_directory(build_root)
    current = build_root / "current"
    for path in (current, current / "source", current / "target", current / "metadata-target"):
        cache_directory(path, within=build_root)
    # The legacy fixed temporary name is never written again, but a planted
    # link/special file there must not be silently accepted as bookkeeping.
    for name in ("build.lock", "cache-state.json", "cache-state.tmp"):
        bookkeeping_file(build_root / name)
    if create_targets:
        for name in ("target", "metadata-target"):
            path = current / name
            path.mkdir(exist_ok=True)
            cache_directory(path, within=current)


@contextmanager
def owned_directory(path):
    cache_directory(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(descriptor)
        named = path.lstat()
        require(stat.S_ISDIR(named.st_mode) and (opened.st_dev, opened.st_ino) == (named.st_dev, named.st_ino),
                "owned cache directory changed while opening")
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def cache_lock(build_root):
    cache_paths(build_root)
    with owned_directory(build_root) as directory:
        descriptor = os.open("build.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                             0o600, dir_fd=directory)
        try:
            info = os.fstat(descriptor)
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "non-regular owned cache lock")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            cache_paths(build_root)
            yield
        finally:
            os.close(descriptor)


def read_cache_state(build_root):
    with owned_directory(build_root) as directory:
        descriptor = os.open("cache-state.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                             dir_fd=directory)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= MAX_BYTES,
                    "invalid owned cache stamp")
            data = stream.read(MAX_BYTES + 1)
            require(len(data) <= MAX_BYTES, "cache stamp exceeds bound")
            return strict_json(data)


def atomic_bookkeeping(directory, name, value):
    """Exclusive non-following inode, anchored rename, then owned-inode check."""
    require(name in {"cache-state.json", "record.json"}, "unknown bookkeeping destination")
    bookkeeping_file(directory / name)
    payload = (json.dumps(value, sort_keys=True) + "\n").encode()
    temporary = "." + name + "-" + uuid.uuid4().hex + ".tmp"
    created = None
    with owned_directory(directory) as parent:
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                                 0o600, dir_fd=parent)
            with os.fdopen(descriptor, "wb") as stream:
                created = os.fstat(stream.fileno())
                require(stat.S_ISREG(created.st_mode) and created.st_nlink == 1,
                        "temporary bookkeeping inode is not owned/regular")
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            cache_directory(directory)
            named_parent = directory.lstat()
            opened_parent = os.fstat(parent)
            require((named_parent.st_dev, named_parent.st_ino) == (opened_parent.st_dev, opened_parent.st_ino),
                    "bookkeeping directory was replaced")
            bookkeeping_file(directory / name)
            ready = os.stat(temporary, dir_fd=parent, follow_symlinks=False)
            require(stat.S_ISREG(ready.st_mode) and ready.st_nlink == 1
                    and (ready.st_dev, ready.st_ino) == (created.st_dev, created.st_ino),
                    "temporary bookkeeping pathname was replaced")
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            finished = os.stat(name, dir_fd=parent, follow_symlinks=False)
            require(stat.S_ISREG(finished.st_mode) and finished.st_nlink == 1
                    and (finished.st_dev, finished.st_ino) == (created.st_dev, created.st_ino),
                    "atomic bookkeeping destination is not the owned inode")
            os.fsync(parent)
        finally:
            # Do not unlink an attacker-owned pre-existing collision/replacement.
            if created is not None:
                try:
                    leftover = os.stat(temporary, dir_fd=parent, follow_symlinks=False)
                    if (leftover.st_dev, leftover.st_ino) == (created.st_dev, created.st_ino):
                        os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
    return {"path": str(directory / name), "sha256": sha(payload)}


def validate_cache_layout(source, build_root, registry, homes):
    """No mkdir/rmtree precedes this ownership and overlap validation."""
    cache_paths(build_root)
    current = (build_root / "current").resolve()
    resolved_homes = [home.resolve() for home in homes]
    require(len(set(resolved_homes)) == 2, "old/modern Cargo homes must be separate")
    for index, home in enumerate(homes):
        cache_directory(home)
        resolved = resolved_homes[index]
        require(not resolved.is_relative_to(current) and not current.is_relative_to(resolved),
                "dependency Cargo home overlaps disposable current")
        require(not resolved.is_relative_to(source) and not source.is_relative_to(resolved)
                and not resolved.is_relative_to(registry.resolve()) and not registry.resolve().is_relative_to(resolved),
                "Cargo home collides with source/assets")
    require(not resolved_homes[0].is_relative_to(resolved_homes[1])
            and not resolved_homes[1].is_relative_to(resolved_homes[0]),
            "old/modern Cargo homes overlap")


def prepare_cache(source, build_root, spec, identity, original, lock_source):
    current, state_path = build_root / "current", build_root / "cache-state.json"
    cache_paths(build_root)
    state, reason = None, "no-complete-current-cache"
    expected = {name: dict(row, mode=0o755 if row["mode"] & 0o111 else 0o644) for name, row in original.items()}
    if "Cargo.lock" not in original:
        expected["Cargo.lock"] = {"sha256": spec["default_lock"]["sha256"], "mode": 0o644}
    if state_path.is_file():
        try:
            state = read_cache_state(build_root)
            require(isinstance(state, dict), "malformed private cache record")
            if state.get("identity") != identity:
                reason = "current-input-or-native-tool-spec-identity-changed"
            elif source_snapshot(current / "source") != expected or state.get("mirror_inputs") != expected:
                reason = "private-mirror-inputs-changed"
            elif generated_snapshot(current / "source") != state.get("generated"):
                reason = "recorded-generated-output-missing-or-changed"
            else:
                reason = "identical-current-input-and-recorded-generated-outputs"
        except (OverlayError, OSError, ValueError, TypeError, KeyError):
            reason = "incomplete-or-invalid-private-cache-state"
    reuse = reason == "identical-current-input-and-recorded-generated-outputs"
    cache_paths(build_root)
    state_path.unlink(missing_ok=True)  # An interrupted command never leaves a completed stamp.
    selection = {"kind": "candidate-authored-lock; default bypassed"}
    if not reuse:
        if current.exists():
            require(current.is_dir(), "private cache slot is not a directory")
            shutil.rmtree(current)
        current.mkdir()
        locks = []
        if not (source / "Cargo.lock").exists():
            selection = {"kind": "current-public-package-name", "manifest": select_package(source)}
            locks = [{"path": "Cargo.lock", **spec["default_lock"], "allow_missing": True, "replace_if_sha256": []}]
        synchronize_candidate(source, current / "source", {"manifests": [], "private_members": [], "locks": locks},
                              {DEFAULT_LOCK: lock_source})
        for name in GENERATED:
            (current / "source" / name).unlink(missing_ok=True)
        actual = source_snapshot(current / "source")
        require(actual == expected, "mirror differs beyond missing-only lock and five generated paths")
    cache_paths(build_root, create_targets=True)
    return current, {"reuse": reuse, "reason": reason, "selection": selection}


def execute(spec, source, build_root, command, private=None, *, run=common.run_logged,
            lock_source=None, registry=None, registry_manifest=None, source_config=None,
            cargo_home=None, metadata_home=None):
    report = {"task_id": TASK, "status": "checking", "original_sut_toolchain": "1.68.2",
              "complete_metadata_toolchain": "1.93.0", "historical_restoration": False,
              "selection_uses_test_results": False, "runtime_resolution_commands": 0}
    code, logs = 2, None
    try:
        validate(spec, private)
        effective = transport(command, private)
        source, build_root = source.resolve(), build_root.absolute()
        require(not build_root.is_symlink() and not build_root.resolve().is_relative_to(source)
                and not source.is_relative_to(build_root.resolve()), "private cache/source collision")
        registry = Path(REGISTRY) if registry is None else Path(registry)
        registry_manifest = Path(REGISTRY_MANIFEST) if registry_manifest is None else Path(registry_manifest)
        source_config = Path(SOURCE_CONFIG) if source_config is None else Path(source_config)
        lock_source = Path(DEFAULT_LOCK) if lock_source is None else Path(lock_source)
        homes = [Path(CARGO_HOME) if cargo_home is None else Path(cargo_home),
                 Path(METADATA_CARGO_HOME) if metadata_home is None else Path(metadata_home)]
        validate_cache_layout(source, build_root, registry, homes)
        build_root.mkdir(parents=True, exist_ok=True)
        for home in homes:
            home.mkdir(parents=True, exist_ok=True)
            cache_directory(home)
        with cache_lock(build_root):
            proposed_logs = build_root / ("blazesym-" + str(time.time_ns()))
            proposed_logs.mkdir(mode=0o700)
            cache_directory(proposed_logs, within=build_root)
            logs = proposed_logs
            deadline = time.monotonic() + spec["command_timeout_seconds"]
            tools = tools_verified(spec)
            archive_data, archive_report = verify_registry(spec, registry, registry_manifest)
            report["registry"] = archive_report
            report["source_configuration"] = verify_configuration(spec, registry, source_config)
            data = input_bytes(lock_source)
            require(sha(data) == spec["default_lock"]["sha256"], "shared default lock changed")
            common.parse_lock(data)
            original_full, original = source_snapshot(source, derived=True), source_snapshot(source)
            verify_private(source, private)
            # No ignored package.exclude/gitignore binder can drop committed data.
            require("data/dwarf-example" in original, "committed data/dwarf-example is not scratch")
            report["source_before"] = original
            planned = build_root / "current"
            environments = [environment(spec, homes[0], planned / "target"),
                            environment(spec, homes[1], planned / "metadata-target", modern=True)]
            for env in environments:
                env["SOURCE_ROOT"] = str(planned / "source")
            # Native tools/build scripts may read CPATH, LIBRARY_PATH, or any
            # other inherited value. Bind the actual child environments, not
            # an incomplete prefix whitelist; expose only their digest.
            environment_digest = hash_value(environments)
            identity = hash_value({"inputs": original, "spec": spec, "tools": tools,
                                   "configuration": report["source_configuration"],
                                   "compilation_environment_sha256": environment_digest})
            current, cache = prepare_cache(source, build_root, spec, identity, original, lock_source)
            report["cache"] = dict(cache, identity=identity, private_current=str(current),
                                   compilation_environment_sha256=environment_digest)
            mirror = current / "source"
            mirror_before = source_snapshot(mirror)
            lock_packages = common.parse_lock(input_bytes(mirror / "Cargo.lock"))
            require(all(key[2] is None or key in archive_data and row["checksum"] == archive_data[key]["checksum"]
                        for key, row in lock_packages.items()), "current lock has an unavailable registry identity")
            def unchanged():
                cache_paths(build_root)
                require(source_snapshot(source, derived=True) == original_full
                        and source_snapshot(mirror) == mirror_before, "command changed current source/manifest/lock/fixture")
                generated_snapshot(mirror)
                verify_private(mirror, private)
            report["environment"] = [{k: env[k] for k in ("PATH", "CARGO_HOME", "CARGO_TARGET_DIR", "RUSTC",
                                                         "LLVM_GSYMUTIL", "CARGO_BUILD_JOBS", "CARGO_NET_OFFLINE")}
                                      for env in environments]
            metadata_generated_before = generated_snapshot(mirror)
            graphs, provided = [], []
            for label, cargo, env, home, version, extra in (
                ("metadata_full", METADATA_CARGO, environments[1], homes[1], "1.93.0", ["--all-features"]),
                ("metadata_original", REAL_CARGO, environments[0], homes[0], "1.68.2", []),
            ):
                argv = [cargo, "--offline", "--config", SOURCE_CONFIG, "metadata",
                        "--format-version", "1", "--locked", "--offline", *extra]
                cache_paths(build_root)
                record = run(argv, mirror, env, logs, label,
                             min(deadline, time.monotonic() + spec["metadata_timeout_seconds"]), spec["max_log_bytes"])
                report[label] = record
                unchanged()
                require(generated_snapshot(mirror) == metadata_generated_before,
                        "metadata observer changed a generated native output")
                graph = metadata_record(record, report, registry, archive_data)
                vendors = unpacked_packages(graph, home, archive_data, version)
                if not graphs:
                    report["full_graph"] = common.verify_graph(graph, mirror, lock_packages, vendors)
                else:
                    report["original_selected_graph"] = selected_graph(graph, graphs[0], mirror, lock_packages, vendors)
                graphs.append(graph); provided.append(vendors)
            report["selection"] = selected_target(graphs[0], graphs[1], mirror, private)
            unchanged()
            report["transport"] = {"original_argv": command, "effective_argv": effective}
            cache_paths(build_root)
            record = run(effective, mirror, environments[0], logs, "cargo", deadline,
                         spec["max_log_bytes"], forward=True)
            report["execution"] = record
            unchanged()
            verify_registry(spec, registry, registry_manifest)
            for graph, home, version in [(graphs[0], homes[1], "1.93.0"), (graphs[1], homes[0], "1.68.2")]:
                unpacked_packages(graph, home, archive_data, version)
            code = record["returncode"] if record["returncode"] >= 0 else 128 - record["returncode"]
            try:
                guard.infrastructure(record, report)
            except guard.SelectionError as error:
                report["infrastructure_error"] = str(error)
                code = code or 2
            if not record.get("stdout_utf8_valid", True) or not record.get("stderr_utf8_valid", True):
                report["infrastructure_error"] = "non-UTF8 original Cargo output; raw bytes retained"
                code = code or 2
            if private is not None:
                report["test_results"] = guard.api.test_results(record, EXPECTED)
                if not all(report["test_results"].values()):
                    code = code or 2
            generated = generated_snapshot(mirror)
            if "infrastructure_error" not in report:
                state = {"identity": identity, "mirror_inputs": mirror_before, "generated": generated}
                report["cache_stamp"] = atomic_bookkeeping(build_root, "cache-state.json", state)
            unchanged()
            report.update(status="completed" if code == 0 else "failed", exit_status=code,
                          generated_outputs=generated, source_unchanged=True, mirror_inputs_unchanged=True)
    except (OverlayError, guard.SelectionError, OSError, ValueError, KeyError, TypeError, tarfile.TarError) as error:
        report.update(status="rejected", error=type(error).__name__ + ": " + str(error), exit_status=2)
        code = 2
    finally:
        if logs is not None:
            try:
                atomic_bookkeeping(logs, "record.json", report)
            except (OverlayError, OSError, ValueError, KeyError, TypeError) as error:
                report.update(status="rejected", error="bookkeeping: " + type(error).__name__ + ": " + str(error),
                              exit_status=2)
                code = 2
    return code, report


def publish(report, private=None):
    if report.get("execution", {}).get("stdout") and not report["execution"]["stdout"].endswith("\n"):
        common.write_stream("stdout", b"\n")
    if private is not None:
        print(EVIDENCE_PREFIX + json.dumps(report, sort_keys=True, separators=(",", ":")), flush=True)
    else:
        print("ReviewBench Blazesym: " + json.dumps({
            "status": report["status"], "exit_status": report.get("exit_status", 2),
            "full_packages": report.get("full_graph", {}).get("packages"),
            "selected_packages": report.get("original_selected_graph", {}).get("packages"),
            "cache_reused": report.get("cache", {}).get("reuse"),
        }, sort_keys=True), flush=True)
    if report.get("error"):
        print(report["error"], file=sys.stderr, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("/workspace"))
    parser.add_argument("--build-root", type=Path, default=Path("/opt/reviewbench-cache/rust-build"))
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--cwd", choices=["."], default=".")
    parser.add_argument("--private-spec", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    spec = strict_json(input_bytes(args.spec))
    private = strict_json(input_bytes(args.private_spec)) if args.private_spec else None
    validate(spec, private)
    if hasattr(os, "sched_getaffinity"):
        os.sched_setaffinity(0, set(sorted(os.sched_getaffinity(0))[:spec["cpus"]]))
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    code, report = execute(spec, args.source, args.build_root, command, private)
    publish(report, private)
    return code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OverlayError, OSError, ValueError, KeyError, TypeError) as error:
        print("Blazesym infrastructure: " + str(error), file=sys.stderr, flush=True)
        raise SystemExit(2)
