"""Prepare native API adapters' bounded, source-only dependencies."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import posixpath
import shutil
import tarfile
import tempfile
from urllib.request import urlopen

import yaml

from reviewbench_maintainability.agentic.cli import load_config
from reviewbench_maintainability.agentic.contracts import ContractError, atomic_json, file_hash
from reviewbench_maintainability.agentic.execution import ExecutionServices
from reviewbench_maintainability.agentic.models import engine_identity
from reviewbench_maintainability.agentic.resources import ResourceRequest, GiB
from reviewbench_maintainability.agentic.snapshots import extract_tar, start_archive
from .common import MigrationContext
from .native_api import attested, FMT9_SHA512, CMRC_SHA512
from .native_submodules import check_repository, closure, object_view
from .native_submodules_prepare import PinnedSources


ARCHIVES = {
    "fmt9": ("https://github.com/fmtlib/fmt/archive/refs/tags/9.1.0.tar.gz", FMT9_SHA512),
    "fmt11": ("https://github.com/fmtlib/fmt/archive/refs/tags/11.0.2.tar.gz", None),
    "cmrc": ("https://github.com/vector-of-bool/cmrc/archive/refs/tags/2.0.1.tar.gz", CMRC_SHA512),
}
MAX_ARCHIVE = 32 * 1024**2
MAX_SOURCE = 256 * 1024**2
MAX_FILES = 20000
FAST_FLOAT = "00c8c7b0d5c722d2212568d915a39ea73b08b973"


def unpack_source(archive, destination):
    """Preserve the complete public archive, checking every entry before writes."""
    with tarfile.open(archive, "r:gz") as stream:
        members = stream.getmembers()
        roots = {Path(member.name).parts[0] for member in members if Path(member.name).parts}
        if (len(roots) != 1 or len(members) > MAX_FILES
                or sum(member.size for member in members) > MAX_SOURCE):
            raise ContractError("native API release archive exceeds its bounded source closure")
        seen = set()
        entries = []
        for member in members:
            name = Path(member.name)
            if name.is_absolute() or ".." in name.parts or not (member.isfile() or member.isdir() or member.issym()):
                raise ContractError("native API release archive has an escaping or special entry")
            relative = Path(*name.parts[1:]) if len(name.parts) > 1 else Path(".")
            if str(relative) in seen:
                raise ContractError("native API release archive has duplicate entries")
            seen.add(str(relative))
            if member.issym():
                link = Path(member.linkname)
                resolved = posixpath.normpath(str(relative.parent / link))
                if link.is_absolute() or resolved == ".." or resolved.startswith("../"):
                    raise ContractError("native API release archive has an escaping source symlink")
            entries.append((member, relative))
        links = {str(relative) for member, relative in entries if member.issym()}
        if any(any(str(parent) in links for parent in relative.parents) for _, relative in entries):
            raise ContractError("native API release archive writes through a source symlink")
        destination.mkdir(parents=True, exist_ok=False)
        for member, relative in entries:
            target = destination / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with stream.extractfile(member) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(member.linkname)


def header_closure(fresh, retained):
    def files(root):
        if root.is_symlink() or not root.is_dir():
            raise ContractError("native API original fmt11 header directory is missing")
        result = {}
        for path in root.rglob("*"):
            if path.is_symlink():
                raise ContractError("native API fmt11 header input contains a symlink")
            if path.is_file():
                result[str(path.relative_to(root))] = file_hash(path)
        return result
    original, current = files(retained), files(fresh)
    if len(original) < 5 or current != original:
        raise ContractError("native API fresh fmt11 source differs from the original header closure")
    return current


def release_source(ctx, name, output):
    url, expected = ARCHIVES[name]
    archive = output / "downloads" / (name + ".tar.gz")
    archive.parent.mkdir(parents=True, exist_ok=True)
    if archive.exists():
        raise ContractError("native API source download attempt already exists; preserve it")
    receipt = {"name": name, "url": url, "started_at": datetime.now(timezone.utc).isoformat(),
               "expected_sha512": expected, "pid": os.getpid()}
    try:
        with urlopen(url, timeout=120) as response, archive.open("xb") as stream:
            size = 0
            while block := response.read(1024**2):
                size += len(block)
                if size > MAX_ARCHIVE:
                    raise ContractError("native API public release archive exceeds its download bound")
                stream.write(block)
        observed = hashlib.sha512(archive.read_bytes()).hexdigest()
        if expected and observed != expected:
            raise ContractError("native API release bytes differ from the original CMake URL_HASH")
        with tempfile.TemporaryDirectory(prefix="native-api-release-", dir=ctx.scratch) as temporary:
            source = Path(temporary) / "source"
            unpack_source(archive, source)
            if name == "fmt11":
                headers = header_closure(
                    source / "include", ctx.legacy / "native/cache/microsoft__vcpkg-tool/home/fmt-11.0.2/include")
                receipt["original_headers"] = headers
                receipt["original_headers_verified"] = True
            asset = ctx.bundle(source, role="dependency")
        descriptor = json.loads(Path(asset["path"]).read_text())
        receipt.update(status="source_ready", archive_sha256=file_hash(archive),
                       archive_sha512=observed, archive_bytes=size, asset=asset,
                       source_tree_sha256=descriptor["tree_sha256"], source_pin="sha512:" + observed)
    except Exception as exc:
        receipt.update(status="blocked", reason=type(exc).__name__ + ": " + str(exc))
        raise
    finally:
        receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
        atomic_json(output / "source-receipts" / (name + ".json"), receipt)
    evidence = ctx.copy_asset(output / "source-receipts" / (name + ".json"), "native-api-source-receipts")
    return {key: receipt[key] for key in ("asset", "source_pin", "source_tree_sha256")} | {
        "receipt": evidence, "original_headers_verified": receipt.get("original_headers_verified", False)}


def fast_float_source(ctx, output):
    repository = check_repository(ctx.legacy / "native/cache/fast_float-v6.1.6", FAST_FLOAT)
    with tempfile.TemporaryDirectory(prefix="native-api-fast-float-", dir=ctx.scratch) as temporary:
        source = Path(temporary) / "source"
        with object_view(repository) as view, start_archive(view, FAST_FLOAT, timeout=120) as archive:
            extract_tar(archive, source)
        asset = ctx.bundle(source, role="dependency", git_commit=FAST_FLOAT)
    descriptor = json.loads(Path(asset["path"]).read_text())
    receipt = {"status": "source_ready", "repository": str(repository), "git_commit": FAST_FLOAT,
               "source_pin": "git:" + FAST_FLOAT, "asset": asset, "source_tree_sha256": descriptor["tree_sha256"],
               "method": "read-only exact Git object view; complete fresh source export, no compiled cache"}
    path = output / "source-receipts/fast-float.json"
    atomic_json(path, receipt)
    return {key: receipt[key] for key in ("asset", "source_pin", "source_tree_sha256")} | {
        "receipt": ctx.copy_asset(path, "native-api-source-receipts")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--legacy-root", type=Path, required=True)
    parser.add_argument("--ids", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    args = parser.parse_args(argv)
    config = load_config(args.config)
    tasks = {r["id"]: r for r in map(json.loads, Path(config["tasks"]).read_text().splitlines())}
    scope = set(Path(config["scope_ids"]).read_text().splitlines())
    tasks = {key: value for key, value in tasks.items() if key in scope}
    oracles = {r["id"]: r for r in map(json.loads, Path(config["oracles"]).read_text().splitlines())}
    plans = {r["input_index"]: r for r in map(
        json.loads, (args.legacy_root / "native/plans.jsonl").read_text().splitlines())}
    ids = args.ids.read_text().splitlines()
    if len(ids) != len(set(ids)) or not ids or not set(ids) <= tasks.keys():
        raise ContractError("native API source cohort has duplicate, missing or foreign task IDs")
    engine = engine_identity()["fingerprint"]
    if engine != config.get("migration", {}).get("verification_engine_fingerprint"):
        raise ContractError("native API source preparation requires the frozen configured generation engine")
    output = args.out_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / "source-preparation.json").exists():
        raise ContractError("native API source preparation already completed; inspect the retained attempt")
    config["out_dir"] = str(output / "execution")
    services = ExecutionServices(config)
    ctx = MigrationContext(config, tasks, oracles, args.legacy_root, output)
    sources = config.setdefault("migration", {}).setdefault("native_api_sources", {})
    locations = config["migration"].setdefault("native_submodule_repositories", {})
    fetcher = PinnedSources(config["repo_cache"], args.source_root, output)
    rows = []
    print(json.dumps({"phase": "waiting_shared_gate", "pid": os.getpid(), "ids": ids}), flush=True)
    with services.gate.acquire(ResourceRequest("prepare-native-api-sources", cpus=1,
                                               memory_bytes=GiB, scratch_bytes=6 * GiB)):
        for ident in ids:
            task, oracle = tasks[ident], oracles[ident]
            row = {"id": ident, "runtime_verified": False}
            try:
                plan = plans[oracle["input_index"]]
                admission = attested(ctx, task, plan)
                if admission is None:
                    raise ContractError("native API source cohort contains an unsupported plan")
                profile = admission[0]
                if profile in {"vcpkg-message", "vcpkg-binary-info"}:
                    names = ("fmt9", "fmt11", "cmrc") if profile == "vcpkg-message" else ("fmt9", "cmrc")
                    for name in names:
                        if name not in sources:
                            sources[name] = release_source(ctx, name, output)
                elif profile == "numere-formats":
                    if "fast-float" not in sources:
                        sources["fast-float"] = fast_float_source(ctx, output)
                    repository = ctx.repo(task)
                    fetcher.urls[str(repository.resolve())] = "https://github.com/" + task["repo"] + ".git"
                    configured = locations.setdefault(task["repo"], {})
                    records, totals = closure(repository, task["start_sha"], ("externals/date",),
                                               configured, resolver=fetcher.resolve)
                    for record in records:
                        configured.setdefault(record["path"], {})[record["commit"]] = record["repository"]
                    row.update(submodule_closure=records, submodule_totals=totals)
                row.update(status="source_inputs_ready", profile=profile)
            except Exception as exc:
                row.update(status="blocked", reason=type(exc).__name__ + ": " + str(exc))
            rows.append(row)
            atomic_json(output / "source-progress.json", {"tasks": rows, "events": fetcher.events})
            print(json.dumps(row), flush=True)
    destination = output / "prepared-config.yaml"
    destination.write_text(yaml.safe_dump(config, sort_keys=False))
    report = {"status": "sources_ready" if all(r["status"] == "source_inputs_ready" for r in rows) else "partial",
              "tasks": rows, "expected_tasks": len(tasks), "engine_fingerprint": engine,
              "config": str(destination), "config_sha256": file_hash(destination),
              "sources": sources, "events": fetcher.events, "workers": 1, "model_calls": 0,
              "registration_performed": False, "runtime_verified": False}
    atomic_json(output / "source-preparation.json", report)
    return 0 if report["status"] == "sources_ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
