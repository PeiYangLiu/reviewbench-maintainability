#!/usr/bin/env python3
"""VFS61's first NEW lock: missing-only mirror default and offline graph check.

This is not historical lock recovery. No reference source, API choice, test
result, or runtime resolver participates in dependency selection.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

if __package__:
    from . import rust_test_compat_entry as guard
    from .rust_build_entry import OverlayError, candidate_files, synchronize_candidate, tomllib
else:
    import rust_test_compat_entry as guard
    from build import OverlayError, candidate_files, synchronize_candidate, tomllib

TASK = "github__manuel-woelker__rust-vfs__pull_54__comment_1341909533__modularity"
PLAN = "0170b037305e53006e34f372a0216eb41ac760236402268d9b74d2c7f2c33aff"
MANIFEST = "9f82e493a8913a22291388ebf7ebd6f0bfc2334f566fd6f468df3293311d2672"
LOCK = "e34313290decc0b623f6f314ebaaccc7e9a03595d597943485e4d5847b8014fe"
GRAPH = "97b1b350fb51117df0f6ec6992b43dd223478f60bf8248b423a6a86aa5eeabfb"
PREPARE = "cargo +1.93.0 update -p tokio-stream --precise 0.1.14 >/dev/null"
SCHEMA = "reviewbench-vfs61-first-new-resolution/v2"
MARKER = "vfs_first_resolution"
TOOLS = "/opt/reviewbench-assets/rust-vfs-resolution-tools"
SPEC = "/opt/reviewbench-assets/rust-build-spec.json"
REAL_CARGO = "/opt/reviewbench-assets/rust/bin/cargo"
REAL_RUSTC = "/opt/reviewbench-assets/rust/bin/rustc"
CARGO_SHA = "9548937d530bf439ff1ba47a3b2bd26eeb9c3aff1961c20c01798613de922578"
RUSTC_SHA = "d32249a7c3bfcfc67b471460386e46323accae7125e344567a12d5664d99bb57"
MAX_BYTES = 32 * 1024**2
VENDOR = "/opt/reviewbench-assets/vendor"
PREFIX = ["--offline", "--config", 'source.crates-io.replace-with="reviewbench-vendor"',
          "--config", 'source.reviewbench-vendor.directory="/opt/reviewbench-assets/vendor"']


def require(condition, message):
    if not condition:
        raise OverlayError("VFS61 new-resolution: " + message)


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else (
            hashlib.sha256(stream.read()).hexdigest())


def strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate JSON field")
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs)


def candidate_lock_packages(data):
    """Parse the candidate's own lock, not a reference/default lock identity."""
    require(len(data) <= MAX_BYTES, "candidate lock exceeds byte bound")
    lock = tomllib.loads(data.decode())
    rows = lock.get("package")
    require(isinstance(rows, list) and 1 <= len(rows) <= 4096, "missing/oversize current lock graph")
    require(all(isinstance(p, dict) and isinstance(p.get("name"), str)
                and re.fullmatch(r"[A-Za-z0-9_-]+", p["name"])
                and isinstance(p.get("version"), str)
                and re.fullmatch(r"[A-Za-z0-9_.+-]+", p["version"]) for p in rows),
            "invalid current lock package identity")
    identities = [(p["name"], p["version"], p.get("source")) for p in rows]
    require(len(set(identities)) == len(rows), "duplicate lock identity")
    require([p["version"] for p in rows if p["name"] == "tokio-stream"] == ["0.1.14"],
            "original tokio-stream precise pin changed")
    registry = [p for p in rows if p.get("source")]
    require(all(
        p["source"] == "registry+https://github.com/rust-lang/crates.io-index"
        and re.fullmatch(r"[0-9a-f]{64}", p.get("checksum", "")) for p in registry),
        "unbound registry/Git dependency")
    return rows


def lock_packages(data):
    """Exact NEW default/receipt inputs, separately from candidate validation."""
    require(hashlib.sha256(data).hexdigest() == LOCK, "first-success lock hash changed")
    rows = candidate_lock_packages(data)
    require(len(rows) == 102 and len([p for p in rows if p.get("source")]) == 101,
            "incomplete first lock")
    return rows


def vendor_packages(packages, vendor):
    """All external current-lock inputs must exist in the provided vendor."""
    vendor = vendor.resolve()
    paths = {}
    for package in packages:
        if package.get("source") is None:
            continue
        folder = vendor / (package["name"] + "-" + package["version"])
        require(folder.is_dir() and not folder.is_symlink(), "current dependency is not provided by vendor")
        manifest, checksums = folder / "Cargo.toml", folder / ".cargo-checksum.json"
        require(manifest.is_file() and checksums.is_file()
                and not manifest.is_symlink() and not checksums.is_symlink()
                and manifest.stat().st_size <= MAX_BYTES and checksums.stat().st_size <= MAX_BYTES,
                "missing/non-regular vendor manifest/checksums")
        value = strict_json(checksums.read_bytes())
        require(value.get("package") == package["checksum"] and isinstance(value.get("files"), dict),
                "candidate dependency checksum differs from provided vendor")
        require(value["files"].get("Cargo.toml") == sha(manifest), "vendor manifest bytes changed")
        data = tomllib.loads(manifest.read_text()).get("package", {})
        require((data.get("name"), data.get("version")) == (package["name"], package["version"]),
                "vendor package manifest identity changed")
        paths[(package["name"], package["version"], package["source"])] = manifest
    return paths


def graph_digest(graph, packages, root, *, current_vendor=None):
    """Full current graph validation; no comparison to a reference graph hash."""
    require(isinstance(graph, dict) and type(graph.get("version")) is int and graph["version"] == 1
            and graph.get("workspace_root") == str(root), "wrong metadata workspace/version")
    rows = graph.get("packages")
    resolve = graph.get("resolve")
    require(isinstance(rows, list) and isinstance(resolve, dict), "missing metadata packages/resolve")
    nodes = resolve.get("nodes")
    require(isinstance(nodes, list) and len(rows) == len(nodes) == len(packages),
            "missing metadata package/node")
    by_id = {p["id"]: (p["name"], p["version"], p.get("source")) for p in rows}
    require(len(by_id) == len(rows) == len({n["id"] for n in nodes})
            and set(by_id) == {n["id"] for n in nodes}, "duplicate or missing graph ID")
    require(set(by_id.values()) == {(p["name"], p["version"], p.get("source")) for p in packages},
            "metadata packages differ from the entire current lock")
    local = [p for p in rows if p.get("source") is None]
    if current_vendor is None:
        # Retained first-resolution evidence still requires its original exact
        # single-package workspace, independently of current-candidate policy.
        require(len(local) == 1 and local[0].get("manifest_path") == str(root / "Cargo.toml")
                and graph.get("workspace_members") == [local[0]["id"]]
                and resolve.get("root") == local[0]["id"], "unbound first-resolution workspace")
    else:
        members = graph.get("workspace_members")
        local_ids = {p["id"] for p in local}
        require(isinstance(members, list) and members and len(set(members)) == len(members)
                and set(members) <= local_ids, "missing/foreign current workspace member")
        roots = [p for p in local if p.get("manifest_path") == str(root / "Cargo.toml")]
        require(len(roots) == 1 and resolve.get("root") == roots[0]["id"],
                "graph root differs from current Cargo package")
        manifests = candidate_inputs(root)["manifests"]
        for package in local:
            manifest = Path(package.get("manifest_path", "")).resolve()
            require(manifest.is_relative_to(root) and manifest.relative_to(root).as_posix() in manifests,
                    "local dependency manifest escapes complete current candidate")
            targets = package.get("targets")
            require(isinstance(targets, list) and targets, "local dependency lacks complete source targets")
            for target in targets:
                path = Path(target.get("src_path", "")).resolve()
                require(path.is_relative_to(root) and path.is_file(),
                        "local dependency target escapes complete current candidate")
        for package in rows:
            if package.get("source") is not None:
                key = package["name"], package["version"], package["source"]
                require(key in current_vendor and package.get("manifest_path") == str(current_vendor[key]),
                        "metadata redirects an external dependency outside provided vendor")
    normalized = []
    for node in nodes:
        dependencies, deps, features = node.get("dependencies"), node.get("deps"), node.get("features")
        require(isinstance(dependencies, list) and len(set(dependencies)) == len(dependencies)
                and set(dependencies) <= set(by_id) and isinstance(deps, list)
                and {d["pkg"] for d in deps} == set(dependencies), "missing/duplicate graph edge")
        require(isinstance(features, list) and all(isinstance(f, str) for f in features)
                and len(set(features)) == len(features), "invalid graph features")
        for dep in deps:
            require(isinstance(dep.get("name"), str) and isinstance(dep.get("dep_kinds"), list)
                    and all(isinstance(k, dict) and set(k) == {"kind", "target"}
                            and k["kind"] in {None, "dev", "build"}
                            and (k["target"] is None or isinstance(k["target"], str))
                            for k in dep["dep_kinds"]), "unbound graph edge kind/target")
        normalized.append({
            "package": by_id[node["id"]], "features": sorted(features),
            "dependencies": sorted((by_id[i] for i in dependencies),
                                   key=lambda p: (p[0], p[1], p[2] or "")),
            "deps": sorted([{"name": d["name"], "package": by_id[d["pkg"]],
                             "dep_kinds": d["dep_kinds"]} for d in deps],
                           key=lambda d: (d["name"], *d["package"][:2], d["package"][2] or "")),
        })
    normalized.sort(key=lambda n: (*n["package"][:2], n["package"][2] or ""))
    digest = hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return digest


def normalized_graph(graph, packages, root):
    """The NEW default receipt still binds its exact original complete graph."""
    digest = graph_digest(graph, packages, root)
    require(digest == GRAPH, "complete dependency graph/features/edges changed")
    return digest


def marker(receipt_sha, decision_sha):
    return {
        "schema_version": SCHEMA, "task_id": TASK, "plan_sha256": PLAN,
        "origin": "first-successful-new-environment-original-prepare",
        "historical_restoration": False, "receipt_sha256": receipt_sha,
        "decision_sha256": decision_sha, "manifest_sha256": MANIFEST, "lock_sha256": LOCK,
        "graph_sha256": GRAPH, "original_prepare_command": PREPARE,
        "toolchain": "1.93.0", "cargo_sha256": CARGO_SHA, "rustc_sha256": RUSTC_SHA,
        "lock_policy": "only-if-missing; preserve-and-validate-current-candidate-lock",
        "runtime_resolution": "metadata --locked --offline --all-features; never update",
    }


def validate(spec):
    require(set(spec) == {"schema_version", "task_id", "manifests", "locks", "private_members", "cpus", MARKER}
            and spec["schema_version"] == "reviewbench-cargo-build-overlay/v1"
            and spec["task_id"] == TASK and spec["manifests"] == []
            and spec["private_members"] == [] and type(spec["cpus"]) is int and spec["cpus"] == 4,
            "wrong task or non-lock overlay")
    value = spec[MARKER]
    require(isinstance(value, dict) and all(
        isinstance(value.get(k), str) and re.fullmatch(r"[0-9a-f]{64}", value[k])
        for k in ("receipt_sha256", "decision_sha256")), "unbound receipt/decision")
    require(value == marker(value["receipt_sha256"], value["decision_sha256"]),
            "modified first-resolution policy or provenance")
    require(spec["locks"] == [{
        "path": "Cargo.lock", "asset_target": "/opt/reviewbench-assets/rust-lock-0",
        "sha256": LOCK, "allow_missing": True, "replace_if_sha256": [],
    }], "first lock must be an only-if-missing asset")


def candidate_inputs(root, *, missing=False):
    files = candidate_files(root)
    require(len(files) <= 10000 and not any("target" in Path(n).parts for n in files),
            "old build outputs or excessive candidate input")
    manifests, locks = {}, {}
    for name, path in files.items():
        if Path(name).name not in {"Cargo.toml", "Cargo.lock"}:
            continue
        require(not path.is_symlink() and path.stat().st_size <= MAX_BYTES, "non-regular/oversize Cargo input")
        if Path(name).name == "Cargo.toml":
            require(isinstance(tomllib.loads(path.read_text()), dict), "invalid current Cargo manifest")
            manifests[name] = sha(path)
        else:
            locks[name] = sha(path)
    require("Cargo.toml" in manifests, "missing current root manifest")
    require("Cargo.lock" in locks or missing, "missing current root lock")
    if "Cargo.lock" in locks:
        candidate_lock_packages((root / "Cargo.lock").read_bytes())
    return {"manifests": manifests, "locks": locks}


def prepare_mirror(source, mirror, spec, *, lock_sources=None):
    validate(spec)
    before = candidate_inputs(source, missing=True)
    asset = Path((lock_sources or {}).get(spec["locks"][0]["asset_target"], spec["locks"][0]["asset_target"]))
    require(asset.is_file() and not asset.is_symlink() and sha(asset) == LOCK, "missing/changed first-lock asset")
    report = synchronize_candidate(source, mirror, spec, lock_sources)
    after = candidate_inputs(mirror)
    require(candidate_inputs(source, missing=True) == before, "original candidate inputs changed")
    return {"source_before": before, "mirror_after": after, "synchronized": report}


def verify_graph(root, logs, run=guard.api.run_command, *, vendor=Path(VENDOR)):
    packages = candidate_lock_packages((root / "Cargo.lock").read_bytes())
    provided = vendor_packages(packages, vendor)
    command = [REAL_CARGO, *PREFIX, "metadata", "--format-version", "1",
               "--locked", "--offline", "--all-features"]
    before = guard.fingerprint(root)
    with guard.invocation(root, Path(os.environ["CARGO_TARGET_DIR"])):
        record = run(command, logs, "new-lock-metadata", time.monotonic() + 120, MAX_BYTES)
    # Package names/descriptions are JSON data. Compiler/test stream guards are
    # unchanged, and metadata stderr/status/byte-limit guards still apply.
    guard.infrastructure({**record, "stdout": ""}, {"metadata": record})
    require(record["returncode"] == 0 and not record.get("stderr", "").strip(), "offline metadata failed")
    require(guard.fingerprint(root) == before, "metadata changed current candidate source/manifest/lock")
    digest = graph_digest(strict_json(record["stdout"]), packages, root, current_vendor=provided)
    return {"command": command, "record": record, "graph_sha256": digest,
            "current_lock_sha256": sha(root / "Cargo.lock"), "current_packages": len(packages),
            "provided_external_packages": len(provided), "selection_uses_reference_hashes": False}


def locked_argv(arguments):
    """Add resolution transport flags without changing any original test args."""
    require(isinstance(arguments, list) and all(isinstance(a, str) and a and "\0" not in a for a in arguments),
            "invalid Cargo argv")
    require(arguments[:len(PREFIX)] == PREFIX, "unexpected Cargo source/transport prefix")
    tail = arguments[len(PREFIX):]
    require(bool(tail) and tail[0] in {"check", "test", "metadata"}, "runtime resolver/subcommand forbidden")
    end = tail.index("--") if "--" in tail else len(tail)
    require(not any(a in {"--manifest-path", "--config", "-Z"} or a.startswith(
        ("--manifest-path=", "--config=", "-Z", "+")) for a in tail[1:end]),
        "unbound manifest/config/toolchain override")
    return [REAL_CARGO, *arguments[:len(PREFIX) + end],
            *[f for f in ("--locked", "--offline") if f not in tail[:end]],
            *tail[end:]]


def cargo_shim(arguments):
    """Keep original diagnostic stdout/stderr untouched, including signal codes."""
    spec = strict_json(Path(SPEC).read_bytes())
    validate(spec)
    root = Path.cwd().resolve()
    candidate_inputs(root)
    before = guard.fingerprint(root)
    command = locked_argv(arguments)
    require(sha(REAL_CARGO) == CARGO_SHA and sha(REAL_RUSTC) == RUSTC_SHA, "pinned compiler bytes changed")
    result = subprocess.run(command, env={**os.environ, "CARGO_NET_OFFLINE": "true"})
    require(guard.fingerprint(root) == before, "Cargo invocation changed current source/lock/manifests")
    directory = Path(os.environ["VFS_FIRST_RESOLUTION_LOGS"])
    path = directory / ("cargo-transport-" + str(os.getpid()) + ".json")
    path.write_text(json.dumps({"original_argv": ["cargo", *arguments], "effective_argv": command,
                               "cwd": str(root), "returncode": result.returncode,
                               "input_fingerprint": before}, sort_keys=True) + "\n")
    return result.returncode if result.returncode >= 0 else 128 - result.returncode


def main():
    if sys.argv[1:2] == ["--cargo-shim"]:
        return cargo_shim(sys.argv[2:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("/workspace"))
    parser.add_argument("--build-root", type=Path, default=Path("/opt/reviewbench-cache/rust-build"))
    parser.add_argument("--spec", type=Path, default=Path(SPEC))
    parser.add_argument("--cwd", choices=["."], default=".")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    require(bool(command), "missing original public/grading command")
    spec = strict_json(args.spec.read_bytes())
    validate(spec)
    if hasattr(os, "sched_getaffinity"):
        os.sched_setaffinity(0, set(sorted(os.sched_getaffinity(0))[:spec["cpus"]]))
    require(sha(REAL_CARGO) == CARGO_SHA and sha(REAL_RUSTC) == RUSTC_SHA, "pinned compiler bytes changed")
    args.build_root.mkdir(parents=True, exist_ok=True)
    with (args.build_root / "build.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        logs = args.build_root / ("first-resolution-" + str(time.time_ns()))
        logs.mkdir()
        report = {"task_id": TASK, "historical_restoration": False, "status": "checking"}
        try:
            source, mirror = args.source.resolve(), args.build_root / "source"
            before = guard.fingerprint(source)
            report["mirror"] = prepare_mirror(source, mirror, spec)
            mirror_before = guard.fingerprint(mirror)
            report["metadata"] = verify_graph(mirror, logs)
            require(guard.fingerprint(source) == before, "metadata changed original candidate")
            os.environ.update(SOURCE_ROOT=str(mirror), CARGO_NET_OFFLINE="true",
                              VFS_FIRST_RESOLUTION_LOGS=str(logs),
                              PATH=TOOLS + "/bin:" + os.environ["PATH"])
            result = subprocess.run(command, cwd=mirror)
            report.update(command=command, returncode=result.returncode, status="completed")
            require(guard.fingerprint(mirror) == mirror_before and guard.fingerprint(source) == before,
                    "public/grading command changed source, manifest or first lock")
            return result.returncode if result.returncode >= 0 else 128 - result.returncode
        except Exception as exc:
            report.update(status="rejected", error=type(exc).__name__ + ": " + str(exc))
            raise
        finally:
            (logs / "record.json").write_text(json.dumps(report, sort_keys=True) + "\n")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OverlayError, guard.SelectionError, OSError, ValueError, KeyError, TypeError) as exc:
        print("VFS61 first-resolution infrastructure error: " + str(exc), file=sys.stderr, flush=True)
        raise SystemExit(2)
