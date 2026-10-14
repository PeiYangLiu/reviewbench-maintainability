"""Build the original pinned public Kokkos Serial installation, never copy it."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
import uuid

import yaml

from reviewbench_maintainability.agentic.contracts import (
    ContractError, atomic_json, canonical_hash, file_hash,
)
from reviewbench_maintainability.agentic.execution import ExecutionServices
from reviewbench_maintainability.agentic.models import engine_identity
from reviewbench_maintainability.agentic.resources import ResourceRequest, job_directory
from reviewbench_maintainability.agentic.snapshots import extract_tar, git, start_archive, tree_hash
from .common import MigrationContext, bounded_cpu_set
from .native_submodules import check_repository, object_view, readonly_git, readonly_links
from .native_kokkos import (
    CACHE_SHA256, INSTALL, POLICY, SOURCE_COMMIT, SOURCE_TREE, SOURCE_VERSION,
    build_options, cache_values, configure_argv, load_dependency, same_options,
)


def source_snapshot(repository, commit, target):
    """Export exact bytes and a shallow, real commit object for CMake Git info.

    Only the selected commit, its trees, and blobs are packed. No parent history,
    remote, old index, checkout changes, or compiled output is copied. This Git
    metadata is private to the builder and excluded from published assets.
    """
    repository = check_repository(repository, commit)
    if readonly_links(repository, commit):
        raise ContractError("Kokkos source requires closure of unexpected gitlinks")
    with start_archive(repository, commit, timeout=120) as stream:
        extract_tar(stream, target)
    with object_view(repository) as view:
        root = git(view, "rev-parse", commit + "^{tree}").strip()
        entries = [row for row in git(view, "ls-tree", "-r", "-t", "-z", commit).split(b"\0") if row]
        objects = {commit.encode(), root, *(row.split(b"\t", 1)[0].split()[2] for row in entries)}
        pack = git(view, "pack-objects", "--stdout", input_bytes=b"\n".join(sorted(objects)) + b"\n")
    if len(pack) > 128 * 1024**2:
        raise ContractError("Kokkos pinned source pack exceeds its bound")
    git(target, "init", "--quiet")
    git(target, "index-pack", "--stdin", input_bytes=pack)
    (target / ".git/HEAD").write_text(commit + "\n")
    (target / ".git/shallow").write_text(commit + "\n")
    # The original generated version record uses this 10-character abbreviation.
    # Git still resolves the actual full commit; no synthetic commit is created.
    git(target, "config", "core.abbrev", "10")
    git(target, "read-tree", "--reset", commit)
    git(target, "update-index", "--refresh")
    git(target, "diff-index", "--quiet", "HEAD", "--")
    if git(target, "rev-parse", "HEAD").decode().strip() != commit:
        raise ContractError("Kokkos build source did not preserve the actual Git commit")
    return {"git_tree": root.decode(), "tree_sha256": tree_hash(target), "packed_objects": len(objects)}


def run_container(label, arguments, image, source, build, install, output, lease):
    name = "rb-kokkos-source-" + uuid.uuid4().hex[:16]
    command = [
        "docker", "run", "--rm", "--init", "--name", name, "--read-only", "--network", "none",
        "--user", f"{os.getuid()}:{os.getgid()}", "--cpus", "2",
        "--cpuset-cpus", bounded_cpu_set(2, lease["lease"]), "--memory", "4g", "--pids-limit", "256",
        "--cap-drop=ALL", "--security-opt=no-new-privileges",
        "--label", "reviewbench.resource-lease=" + lease["lease"],
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m",
        "--mount", f"type=bind,source={source},target=/source,readonly",
        "--mount", f"type=bind,source={build},target=/build",
        "--mount", f"type=bind,source={install},target={INSTALL}",
        "--env", "HOME=/tmp", "--env", "CMAKE_BUILD_PARALLEL_LEVEL=2",
        "--env", "GIT_CONFIG_NOSYSTEM=1", "--env", "GIT_CONFIG_GLOBAL=/dev/null",
        "--env", "GIT_NO_REPLACE_OBJECTS=1", "--workdir", "/source",
        "--entrypoint", arguments[0], image, *arguments[1:],
    ]
    stdout, stderr, record = (output / (label + suffix) for suffix in (".stdout", ".stderr", ".json"))
    if any(path.exists() for path in (stdout, stderr, record)):
        raise ContractError("Kokkos build attempt evidence already exists: " + label)
    started = time.monotonic()
    with stdout.open("x") as out, stderr.open("x") as err:
        process = subprocess.Popen(command, stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                                   start_new_session=True)
        value = {"label": label, "command": command, "input": arguments, "pid": process.pid,
                 "status": "running", "started_at": datetime.now(timezone.utc).isoformat()}
        atomic_json(record, value)
        print(json.dumps(value), flush=True)
        try:
            code = process.wait(timeout=1800)
            timed_out = False
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            code, timed_out = process.wait(), True
    value.update(status="completed", exit_status=code, timed_out=timed_out,
                 seconds=time.monotonic() - started,
                 stdout={"path": str(stdout), "sha256": file_hash(stdout)},
                 stderr={"path": str(stderr), "sha256": file_hash(stderr)})
    atomic_json(record, value)
    print(json.dumps({"label": label, "exit_status": code, "seconds": value["seconds"],
                      "record": str(record)}), flush=True)
    if code or timed_out:
        raise ContractError(f"Kokkos {label} failed: exit={code}; record={record}")
    return {"path": str(record), "sha256": file_hash(record)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--source-repository", type=Path, required=True)
    parser.add_argument("--original-cache", type=Path, required=True)
    parser.add_argument("--original-git-info", type=Path, required=True)
    parser.add_argument("--toolchains", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    config = yaml.safe_load(args.config.read_text())
    fingerprint = engine_identity()["fingerprint"]
    if fingerprint != config.get("migration", {}).get("verification_engine_fingerprint"):
        raise ContractError("Kokkos preparation must use the configured frozen generation engine")
    if args.original_cache.is_symlink() or file_hash(args.original_cache) != CACHE_SHA256:
        raise ContractError("Kokkos original build cache differs from its frozen input")
    options = build_options(cache_values(args.original_cache.read_text()))
    registry = json.loads(args.toolchains.read_text())
    toolchain = registry.get("reviewbench-hard-native-cxx23:20260804", {})
    image = toolchain.get("image", "")
    if toolchain.get("status") != "ready" or not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ContractError("Kokkos needs the retained pinned GCC 14 native toolchain")
    original_git_info = args.original_git_info.read_bytes()
    if SOURCE_COMMIT[:10].encode() not in original_git_info or b'GIT_CLEAN_STATUS = "CLEAN"' not in original_git_info:
        raise ContractError("Kokkos original generated Git provenance is missing")
    output = args.out_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    config = {**config, "out_dir": str(output / "execution"), "asset_root": str(output)}
    request = {
        "source_commit": SOURCE_COMMIT, "source_tree": SOURCE_TREE, "source_version": SOURCE_VERSION,
        "cache_sha256": CACHE_SHA256, "git_info_sha256": file_hash(args.original_git_info),
        "image": image, "options": options, "configure": configure_argv(options),
        "producer_sha256": file_hash(Path(__file__)), "engine_fingerprint": fingerprint,
    }
    manifest = output / "preparation.json"
    if manifest.exists():
        config.setdefault("migration", {})["native_kokkos_preparation"] = {
            "path": str(manifest), "sha256": file_hash(manifest)}
        from types import SimpleNamespace
        previous, _ = load_dependency(SimpleNamespace(config=config), image)
        if previous["request"] != request:
            raise ContractError("Kokkos cache identity changed; use a new preparation output")
        print("REUSED_HASH_BOUND_KOKKOS_INSTALL=" + str(manifest))
        return 0
    if (output / "attempt-start.json").exists():
        raise ContractError("Kokkos attempt already started; preserve it and use a new output")
    atomic_json(output / "attempt-start.json", {
        "pid": os.getpid(), "request": request, "request_sha256": canonical_hash(request),
        "source_repository": str(args.source_repository), "model_calls": 0, "registration_performed": False,
    })
    services = ExecutionServices(config)
    reservation = ResourceRequest("prepare-kokkos-serial-from-pinned-source", cpus=2,
                                  memory_bytes=4 * 1024**3, scratch_bytes=6 * 1024**3, heavy=True)
    try:
        with services.gate.acquire(reservation, uses_containers=True) as lease:
            ctx = MigrationContext(config, {}, {}, args.original_cache.parent, output)
            with job_directory(prefix="kokkos-public-source-") as temporary:
                temporary = Path(temporary)
                source, build, install = (temporary / name for name in ("source", "build", "install"))
                build.mkdir()
                install.mkdir()
                source_info = source_snapshot(args.source_repository, SOURCE_COMMIT, source)
                if source_info["git_tree"] != SOURCE_TREE:
                    raise ContractError("Kokkos source tree differs from the pinned public commit")
                cmake = (source / "CMakeLists.txt").read_text()
                for field, value in (("MAJOR", "5"), ("MINOR", "0"), ("PATCH", "99")):
                    if f"set(Kokkos_VERSION_{field} {value})" not in cmake:
                        raise ContractError("Kokkos pinned source version changed")
                source_asset = ctx.bundle(source, role="dependency", git_commit=SOURCE_COMMIT)
                commands = {}
                operations = [
                    ("toolchain", ["sh", "-ec", "g++-14 -dumpfullversion; cmake --version; ninja --version"]),
                    ("configure", configure_argv(options)),
                    ("build", ["cmake", "--build", "/build", "--parallel", "2"]),
                    ("install", ["cmake", "--install", "/build"]),
                ]
                for label, command in operations:
                    commands[label] = run_container(label, command, image, source, build, install, output, lease)
                    if label == "toolchain" and not (output / "toolchain.stdout").read_text().startswith("14.2.0\n"):
                        raise ContractError("Kokkos requires the original GNU C++ 14.2.0 compiler")
                fresh_cache = build / "CMakeCache.txt"
                actual_options = build_options(cache_values(fresh_cache.read_text()))
                if not same_options(options, actual_options):
                    raise ContractError("Kokkos fresh build cache differs from original feature/compiler flags")
                git_info = build / "generated/Kokkos_Version_Info.cpp"
                if git_info.read_bytes() != original_git_info:
                    raise ContractError("Kokkos rebuilt Git version information differs from the actual original commit")
                if tree_hash(source) != source_info["tree_sha256"]:
                    raise ContractError("Kokkos dependency build changed its input source tree")
                for name in ("include/Kokkos_Core.hpp", "include/KokkosCore_config.h",
                             "lib/libkokkoscore.a", "lib/cmake/Kokkos/KokkosConfig.cmake"):
                    path = install / name
                    if path.is_symlink() or not path.is_file() or not path.stat().st_size:
                        raise ContractError("Kokkos fresh installation is incomplete: " + name)
                (output / "fresh-CMakeCache.txt").write_bytes(fresh_cache.read_bytes())
                (output / "fresh-Kokkos_Version_Info.cpp").write_bytes(git_info.read_bytes())
                (output / "fresh-KokkosCore_config.h").write_bytes((install / "include/KokkosCore_config.h").read_bytes())
                install_asset = ctx.bundle(install, role="dependency")
                installation = json.loads(Path(install_asset["path"]).read_text())
                if installation["files"] > 4096 or installation["archive"]["input_bytes"] > 256 * 1024**2:
                    raise ContractError("Kokkos fresh installation exceeds its asset bound")
                value = {
                    "policy": POLICY, "status": "prepared", "request": request,
                    "request_sha256": canonical_hash(request), "source": source_info,
                    "source_asset": source_asset, "install_asset": install_asset,
                    "install_tree_sha256": installation["tree_sha256"], "commands": commands,
                    "checks": {"source_unchanged": True, "configuration_matches": True,
                               "git_metadata_matches": True, "cpu_only": True},
                    "old_installation_copied": False, "model_calls": 0, "registration_performed": False,
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                }
                atomic_json(manifest, value)
        assert json.loads(manifest.read_text()) == value
        print(json.dumps({"status": "prepared", "manifest": str(manifest), "sha256": file_hash(manifest),
                          "source_commit": SOURCE_COMMIT, "runtime_verified": False}), flush=True)
        return 0
    except Exception as exc:
        atomic_json(output / "preparation-failure.json", {
            "status": "failed", "error": f"{type(exc).__name__}: {exc}", "request": request,
            "model_calls": 0, "registration_performed": False,
        })
        raise


if __name__ == "__main__":
    raise SystemExit(main())
