"""Pinned Go runtimes for direct, retained-shell, and automatic public Oracles."""
from __future__ import annotations

import json
from collections import deque
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import urllib.request
import urllib.error
import os
import zipfile

from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, repo_path, atomic_json, file_hash, component,
)
from reviewbench_maintainability.agentic.resources import file_lock
from reviewbench_maintainability.agentic.snapshots import git
from .common import relocate
from .go_contracts import load_contract, transport_wrapper, generation_setup
from .go_dependencies import MAX_MODULES, frozen_inputs, content_sum, parse_mod
from .go_relic import dependency as relic_dependency, cgo_environment

IMAGE = "golang@sha256:3b4a11519ad929d1e1d261a12cff056f0c85b735253d7d861346b9c6f8b36437"
MiB = 1024**2
GiB = 1024**3
PROXY_RESOURCE_SCHEMA = "reviewbench-go-proxy-resources/v1"
MAX_MODULE_ZIP_BYTES = 512 * MiB  # Same bound for downloaded and already cached ZIPs.
MAX_MODULE_EXPANDED_BYTES = 2 * GiB  # Matches go_dependencies.content_sum.
MAX_MODULE_ZIP_ENTRIES = 100000
MAX_GO_RESOURCE_BYTES = 2**63 - 1
GO_BUILD_ALLOWANCE_BYTES = 8 * GiB
GO_PROXY_STAGING_BYTES = 16 * GiB
GO_PREPARATION_BUFFER_BYTES = GiB


def proxy_staging_limit(ctx) -> int:
    migration = getattr(ctx, "config", {}).get("migration", {})
    if not isinstance(migration, dict):
        raise ContractError("Go migration configuration must be an object")
    value = migration.get("go_proxy_staging_bytes", GO_PROXY_STAGING_BYTES)
    if (type(value) is not int or value <= 0
            or value > MAX_GO_RESOURCE_BYTES - GO_PREPARATION_BUFFER_BYTES):
        raise ContractError("Go proxy staging budget is invalid")
    return value


def migration_scratch_bytes(ctx) -> int:
    """Reserve the bounded proxy staging and publication, not a flat 1 GiB."""
    return proxy_staging_limit(ctx) + GO_PREPARATION_BUFFER_BYTES


def public_build_check(ctx, task, contract, go_binary: str, module_root: str) -> str:
    """Allow an explicitly selected whole-package compile without TestMain."""
    migration = getattr(ctx, "config", {}).get("migration", {})
    if not isinstance(migration, dict):
        raise ContractError("Go migration configuration must be an object")
    selected = migration.get("go_compile_only_public_ids", [])
    if (not isinstance(selected, list) or any(not isinstance(value, str) for value in selected)
            or len(selected) != len(set(selected))):
        raise ContractError("Go compile-only public task selection is invalid")
    targets = contract.public_target.replace(",", " ").split()
    command = [go_binary, "test"]
    if task["id"] in selected:
        if len(targets) != 1 or "..." in targets[0]:
            raise ContractError("Go compile-only public check needs one explicit package")
        command += ["-c", "-o", "/workspace/.reviewbench/go-public-compile.test"]
    else:
        command += ["-run", "^$"]
    command += ["-count=1", *contract.build_flags, *targets]
    return "cd " + shlex.quote(module_root) + " && " + shlex.join(command)


def _zip_resource_valid(value):
    # Go accepts an empty ZIP: positive archive bytes, zero entries and expansion.
    limits = {"zip_bytes": (1, MAX_MODULE_ZIP_BYTES), "expanded_bytes": (0, MAX_MODULE_EXPANDED_BYTES),
              "zip_entries": (0, MAX_MODULE_ZIP_ENTRIES)}
    return (isinstance(value, dict) and all(
        type(value.get(key)) is int and minimum <= value[key] <= maximum
        for key, (minimum, maximum) in limits.items())
        and (value["zip_entries"] != 0 or value["expanded_bytes"] == 0))


def module_zip_resources(path: Path) -> dict:
    """Size the nested ZIP without extracting it or staging project outputs."""
    size = path.stat().st_size
    if not 0 < size <= MAX_MODULE_ZIP_BYTES:
        raise ContractError("go_module_zip_resource_compressed_size_invalid")
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_MODULE_ZIP_ENTRIES:
                raise ContractError("go_module_zip_resource_entry_count_invalid")
            if any(type(item.file_size) is not int or type(item.compress_size) is not int
                   or item.file_size < 0 or item.compress_size < 0 for item in entries):
                raise ContractError("go_module_zip_resource_entry_size_invalid")
            result = {"zip_bytes": size, "expanded_bytes": sum(item.file_size for item in entries),
                      "zip_entries": len(entries)}
            if (not _zip_resource_valid(result) or sum(item.compress_size for item in entries) > size
                    or len({item.filename for item in entries}) != len(entries)):
                raise ContractError("go_module_zip_resource_layout_or_size_invalid")
            return result
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise ContractError("go_module_zip_resource_archive_invalid") from exc


def proxy_scratch_mb(hint: dict) -> int:
    """Writable ZIP cache + expanded modules + builds; readonly asset bytes are separate."""
    if not isinstance(hint, dict) or hint.get("schema_version") != PROXY_RESOURCE_SCHEMA:
        raise ContractError("go_proxy_resource_hint_missing_or_invalid")
    count = hint.get("zip_count")
    if type(count) is not int or not 0 <= count <= MAX_MODULES:
        raise ContractError("go_proxy_resource_zip_count_invalid")
    for key, maximum in (("zip_bytes", MAX_MODULE_ZIP_BYTES),
                         ("expanded_bytes", MAX_MODULE_EXPANDED_BYTES),
                         ("zip_entries", MAX_MODULE_ZIP_ENTRIES)):
        value = hint.get(key)
        minimum = count if key == "zip_bytes" else 0
        if type(value) is not int or not minimum <= value <= count * maximum:
            raise ContractError("go_proxy_resource_total_invalid: " + key)
    if hint["zip_entries"] == 0 and hint["expanded_bytes"] != 0:
        raise ContractError("go_proxy_resource_expansion_without_entries")
    allowance = hint.get("build_allowance_bytes")
    if type(allowance) is not int or allowance != GO_BUILD_ALLOWANCE_BYTES:
        raise ContractError("go_proxy_resource_build_allowance_invalid")
    total = hint["zip_bytes"] + hint["expanded_bytes"] + allowance
    if total > MAX_GO_RESOURCE_BYTES - (MiB - 1):
        raise ContractError("go_proxy_resource_scratch_overflow")
    result = max(16384, (total + MiB - 1) // MiB)
    if "build_scratch_mb" in hint and (type(hint["build_scratch_mb"]) is not int
                                      or hint["build_scratch_mb"] != result):
        raise ContractError("go_proxy_resource_scratch_hint_differs")
    return result


def proxy_resource_hint(zip_objects: list[dict]) -> dict:
    """Aggregate unique staged archives, including superseded frozen versions."""
    if len(zip_objects) > MAX_MODULES or any(not _zip_resource_valid(row) for row in zip_objects):
        raise ContractError("go_proxy_resource_archive_inputs_invalid")
    result = {"schema_version": PROXY_RESOURCE_SCHEMA, "zip_count": len(zip_objects),
              "build_allowance_bytes": GO_BUILD_ALLOWANCE_BYTES}
    for key in ("zip_bytes", "expanded_bytes", "zip_entries"):
        result[key] = sum(row[key] for row in zip_objects)
    result["build_scratch_mb"] = proxy_scratch_mb(result)
    return result


def escape_module(value: str) -> str:
    return "".join("!" + c.lower() if c.isupper() else c for c in value)


def compiler_version(ctx, spec, task, cwd):
    command = spec["command"][0]
    description = spec.get("environment_description", {}).get("toolchain", "")
    found = re.search(r"\b[Gg]o\s*(1\.\d+\.\d+)", description)
    if not found:
        found = re.search(r"go(1\.\d+\.\d+)", command)
    version = found[1] if found else None
    if version is None:
        mod = git(ctx.repo(task), "show", task["start_sha"] + ":" + (cwd + "/" if cwd else "") + "go.mod").decode()
        required = re.search(r"^go\s+(\S+)", mod, re.M)
        version = required[1] if required else "1.22.2"
        if tuple(int(x) for x in version.split(".")) <= (1, 22, 2):
            version = "1.22.2"
    if not re.fullmatch(r"1\.\d+\.\d+", version):
        raise ContractError("Go compiler requires an observed exact patch version: " + version)
    return version


def find_compiler(ctx, directory, spec, version):
    command = spec["command"][0]
    candidates = []
    if version == "1.22.2":
        candidates.append(Path("/usr/bin/go").resolve().parent.parent)
    if "/" in command:
        binary = Path(relocate(command, {"root": str(ctx.legacy/"gopy"), "task_dir": str(directory)}))
        candidates.append(binary.parent.parent)
    candidates += [ctx.legacy/"gopy/toolchains"/("go"+version)]
    candidates += list(Path("/home/v-peiyangliu/go/pkg/mod").glob(f"golang.org/toolchain@v0.0.1-go{version}.linux-amd64"))
    candidates += list((ctx.legacy/"gopy/recovery").glob(f"*/home/go/pkg/mod/golang.org/toolchain@v0.0.1-go{version}.linux-amd64"))
    source = next((p for p in candidates if (p/"bin/go").is_file()), None)
    if source is None:
        raise ContractError("pinned Go compiler missing: " + version)
    observed = subprocess.check_output([str(source/"bin/go"), "version"], text=True, timeout=15,
                                      env={**os.environ, "GOTOOLCHAIN": "local"}).strip()
    if "go"+version+" " not in observed:
        raise ContractError("Go compiler version differs from frozen selection: " + observed)
    return source, observed


def compiler(ctx, directory, spec, task, cwd):
    version = compiler_version(ctx, spec, task, cwd)
    # A previously attested immutable compiler archive may outlive its original
    # extraction. Explicit shared descriptors do not select a newer compiler.
    configured = ctx.config.get("migration", {}).get("go_toolchains", {}).get(version)
    if configured:
        from .go_public_inputs import pinned_file
        pinned_file(configured, "pinned-go-toolchain-" + version)
        return dict(configured), version
    source, _ = find_compiler(ctx, directory, spec, version)
    pointer = ctx.output/"go-toolchains"/(version+".json")
    with file_lock(ctx.output/"locks"/("go-"+version+".lock")):
        if pointer.is_file():
            return json.loads(pointer.read_text()), version
        # Debian's GOROOT uses symlinks into /usr/share/go-VERSION. Flatten
        # those distribution-owned paths in a new staging copy, not in place.
        with tempfile.TemporaryDirectory(prefix="go-toolchain-", dir=ctx.scratch) as temporary:
            staged = Path(temporary)
            for name in ("bin", "pkg", "src", "lib", "go.env", "VERSION"):
                path = source/name
                if path.is_dir():
                    shutil.copytree(path, staged/name, symlinks=False)
                elif path.is_file():
                    shutil.copyfile(path, staged/name)
            asset = ctx.bundle(staged, role="toolchain")
        atomic_json(pointer, asset)
        return asset, version


def fetch_proxy_file(ctx, relative: str) -> Path | None:
    """Reuse a managed local mirror; anonymous exact-version fetch on misses."""
    target = ctx.output/"go-downloads"/relative
    with file_lock(ctx.output/"locks"/("proxy-"+__import__("hashlib").sha256(relative.encode()).hexdigest())):
        if target.is_file():
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name+".partial")
        try:
            with ctx.cache.transfers.acquire():
                with urllib.request.urlopen("https://proxy.golang.org/"+relative, timeout=120) as response:
                    with temporary.open("wb") as stream:
                        total = 0
                        while data := response.read(1024**2):
                            total += len(data)
                            if total > MAX_MODULE_ZIP_BYTES:
                                raise ContractError("individual Go proxy object exceeds staging bound")
                            stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
            temporary.replace(target)
            return target
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 410):
                return None
            raise
        finally:
            temporary.unlink(missing_ok=True)


def module_proxy(ctx, task, directory, cwd, *, oracle=None, contract=None):
    inputs = frozen_inputs(ctx, task, directory, cwd, oracle=oracle, contract=contract)
    staging_limit = proxy_staging_limit(ctx)
    staged_bytes, new_download_bytes = 0, 0
    roots = [
        directory/"home/go/pkg/mod/cache/download",
        Path("/home/v-peiyangliu/go/pkg/mod/cache/download"),
    ]
    roots += [p for p in (ctx.legacy/"gopy/recovery").glob("*/home/go/pkg/mod/cache/download") if p not in roots]
    roots += list((ctx.legacy/"wave-001/correctness/gopy-cache/tasks").glob("*/home/go/pkg/mod/cache/download"))
    missing, receipts = set(), {}
    with tempfile.TemporaryDirectory(prefix="go-proxy-", dir=ctx.scratch) as temporary:
        root = Path(temporary)
        queue = deque(sorted(inputs.modules))
        queued = set(queue)
        processed, expanded = {}, {}
        while queue:
            name, version = queue.popleft()
            queued.remove((name, version))
            item = inputs.modules[(name, version)]
            zip_required = item.zip_required
            scopes = item.graph_scopes or {""}
            pending = scopes - expanded.get((name, version), set()) if (name, version) in inputs.expand else set()
            done = (processed.get((name, version)) is True
                    or (processed.get((name, version)) is False and not zip_required))
            if done and not pending:
                continue
            relative = Path(escape_module(name))/"@v"/escape_module(version)
            for suffix in (".mod", ".info", ".zip"):
                if suffix == ".zip" and not zip_required:
                    continue
                destination = root/(str(relative)+suffix)
                if not destination.is_file():
                    source = next((p / (str(relative)+suffix) for p in roots
                                   if (p / (str(relative)+suffix)).is_file()), None)
                    if source is None:
                        cached = ctx.output / "go-downloads" / (str(relative) + suffix)
                        new_download = not cached.is_file()
                        # A miss can create both a retained download and its
                        # staging copy. Check the worst-case transfer bound
                        # before writing either, rather than after disk fills.
                        if (new_download and staged_bytes + new_download_bytes
                                + 2 * MAX_MODULE_ZIP_BYTES > staging_limit):
                            raise ContractError("Go proxy staging budget exhausted before an uncached download")
                        source = fetch_proxy_file(ctx, str(relative)+suffix)
                        if source is not None and new_download:
                            new_download_bytes += source.stat().st_size
                    if source is None:
                        if suffix == ".mod" or zip_required:
                            missing.add(name+"@"+version+suffix)
                        continue
                    source_bytes = source.stat().st_size
                    if staged_bytes + new_download_bytes + source_bytes > staging_limit:
                        raise ContractError("Go proxy staging budget exhausted before copying an object")
                    sizes = module_zip_resources(source) if suffix == ".zip" else {}
                    digest = content_sum(source, suffix) if suffix in {".mod", ".zip"} else None
                    if suffix in item.sums and digest != item.sums[suffix]:
                        raise ContractError("frozen Go checksum differs: " + name + "@" + version + suffix)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, destination)
                    staged_bytes += source_bytes
                    receipts[(name, version, suffix)] = {
                        "module": name, "version": version, "object": suffix,
                        "sha256": file_hash(destination), "h1": digest, "frozen_h1": item.sums.get(suffix),
                        **sizes,
                    }
                # download all must traverse locked roots too, not only a
                # wrapper's new pin. Keep per-phase replacement contexts and
                # exact versions; neither dependency replace directives nor
                # unrelated historical go.sum entries become graph roots.
                if suffix == ".mod" and pending:
                    requires, _ = parse_mod(destination.read_text())
                    expanded.setdefault((name, version), set()).update(pending)
                    basis = "go-mod-download-all-graph:" if inputs.download_all else "frozen-require-graph:"
                    for scope in sorted(pending):
                        for dependency, pinned in requires:
                            key = inputs.require(dependency, pinned, scope=scope, expand=True,
                                                 origin=basis + name + "@" + version)
                            if key is not None and key not in queued:
                                queued.add(key)
                                queue.append(key)
            processed[(name, version)] = zip_required
        if missing:
            raise ContractError("frozen Go proxy inputs missing: " + "; ".join(sorted(missing)[:12])
                                + f" (total={len(missing)})")
        if not inputs.modules:
            (root/"EMPTY_PROXY").write_text("No third-party modules in frozen go.sum.\n")
        resource_hint = proxy_resource_hint([row for row in receipts.values() if row["object"] == ".zip"])
        atomic_json(root/"go-proxy-inputs.json", {
            "schema_version": "reviewbench-go-proxy-inputs/v1",
            "manifest_inputs": inputs.manifests,
            "objects": [dict(row, origins=list(inputs.modules[key[:2]].origins))
                        for key, row in sorted(receipts.items())],
            "candidate_local_modules": sorted(inputs.local_modules),
            "original_mod_download_all": inputs.download_all,
            "resource_hint": resource_hint,
        })
        # Physical reuse statistics stay outside the immutable archive so
        # warm/cold preparation does not change its content-addressed identity.
        atomic_json(ctx.output / "go-proxy-preparation" / (component(task["id"]) + ".json"), {
            "task_id": task["id"], "staging_limit_bytes": staging_limit,
            "reserved_scratch_bytes": migration_scratch_bytes(ctx),
            "staged_bytes": staged_bytes, "new_download_bytes": new_download_bytes,
            "copy_and_download_bytes": staged_bytes + new_download_bytes,
        })
        return {**ctx.bundle(root), "resource_hint": resource_hint}


def migrate(ctx, task, oracle):
    directory = ctx.task_directory(task, oracle, "gopy")
    contract = load_contract(ctx, task, oracle, directory)
    spec = contract.spec
    relic = relic_dependency(ctx, task, contract)
    if relic:
        contract.provenance.append(relic["provenance"])
    go, version = compiler(ctx, directory, spec, task, contract.module_cwd)
    proxy = module_proxy(ctx, task, directory, contract.module_cwd, oracle=oracle, contract=contract)
    build_scratch_mb = proxy_scratch_mb(proxy.get("resource_hint"))
    image, audit = ctx.image(IMAGE)
    module_root = "/workspace" + ("/" + contract.module_cwd if contract.module_cwd else "")
    task_root = "/workspace/.reviewbench/go-oracle"
    adapter_root = "/opt/reviewbench-assets/go-runtime"
    support_root = "/opt/reviewbench-assets/go-support"
    go_binary = "/opt/reviewbench-assets/go/bin/go"
    context = {"repo": "/workspace", "root": "/opt/reviewbench-assets/gopy", "task_dir": task_root,
               "coverage_out": "/workspace/.reviewbench/coverage.out"}
    command = [relocate(value, context) for value in contract.command]
    if contract.kind in {"direct-test", "automatic-public-tests"}:
        command[0] = go_binary
    entry = ["bash", adapter_root + "/go_oracle_entry.sh"]
    grade = ["mkdir -p " + task_root,
             "cp -R /opt/reviewbench-assets/go-oracle/. " + task_root + "/"]
    for item in spec.get("inject_files", []):
        destination = "/workspace/" + repo_path(item["destination"])
        source = task_root + "/" + repo_path(item["source"])
        grade.append("mkdir -p " + shlex.quote(str(Path(destination).parent))
                     + " && if [ -e " + shlex.quote(destination) + " ] || [ -L " + shlex.quote(destination)
                     + " ]; then cmp -s " + shlex.quote(source) + " " + shlex.quote(destination)
                     + "; else cp " + shlex.quote(source) + " " + shlex.quote(destination) + "; fi")
    assets = [
        {**go, "kind": "archive", "visibility": "developer", "target": "/opt/reviewbench-assets/go"},
        {**proxy, "kind": "archive", "visibility": "developer", "target": "/opt/reviewbench-assets/goproxy"},
    ]
    if relic:
        assets.append(relic["asset"])
    bootstrap = ["mkdir -p /workspace/.reviewbench/tmp /workspace/.reviewbench/home",
                 'test "$(' + shlex.quote(go_binary) + ' version)" = '
                 + shlex.quote("go version go" + version + " linux/amd64")]
    with tempfile.TemporaryDirectory(prefix="go-fixed-wrapper-", dir=ctx.scratch) as temporary:
        staging = Path(temporary)
        harness_root, adapter = staging/"grader", staging/"adapter"
        harness_root.mkdir()
        adapter.mkdir()
        shutil.copyfile(Path(__file__).with_name("go_oracle_entry.sh"), adapter/"go_oracle_entry.sh")
        for name in sorted(set(contract.grader_files)):
            target = harness_root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(directory/name, target)
        if contract.wrapper:
            original = harness_root/"original"/contract.wrapper
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(directory/contract.wrapper, original)
            adapted, changes = transport_wrapper(contract)
            (harness_root/contract.wrapper).write_text(adapted)
            contract.transport_changes = changes
        if contract.support_files:
            support = ctx.bundle(directory, contract.support_files, role="dependency")
            assets.append({**support, "kind": "archive", "visibility": "developer", "target": support_root})
            for name in sorted({Path(n).parts[0] for n in contract.support_files if len(Path(n).parts) > 1}):
                grade.append("ln -s " + shlex.quote(support_root + "/" + name)
                             + " " + shlex.quote(task_root + "/" + name))
        setup = [*contract.setup]
        for item in contract.compile_injections:
            destination = "/workspace/" + repo_path(item["destination"])
            source = support_root + "/" + repo_path(item["source"])
            setup.insert(0, "mkdir -p " + shlex.quote(str(Path(destination).parent))
                         + " && if [ -e " + shlex.quote(destination) + " ] || [ -L " + shlex.quote(destination)
                         + " ]; then cmp -s " + shlex.quote(source) + " " + shlex.quote(destination)
                         + "; else cp " + shlex.quote(source) + " " + shlex.quote(destination) + "; fi")
        if generated := generation_setup(contract):
            setup.append(generated)
        if setup:
            (adapter/"setup.sh").write_text("#!/usr/bin/env bash\nset -euo pipefail\n"
                                            + "cd " + shlex.quote(module_root) + "\n" + "\n".join(setup) + "\n")
            bootstrap.append(shlex.join([*entry, "bash", adapter_root + "/setup.sh"]))
        from .go_modfile import install_shadow_transport, module_edits
        shadow_edits = module_edits(contract, support_root)
        if shadow_edits:
            install_shadow_transport(adapter, shadow_edits)
        if contract.kind == "automatic-public-tests":
            shutil.copyfile(Path(__file__).with_name("go_public_test_report.go"),
                            harness_root/"go_public_test_report.go")
            atomic_json(harness_root/"public-tests.json", {
                "command": command, "expected": contract.expected,
                "report": "/workspace/reviewbench-go-public-report.json",
                "log": "/workspace/.reviewbench/go-public-events.jsonl",
            })
            grade.append(shlex.join(["env", "GO111MODULE=off", "GOFLAGS=-p=2", go_binary, "build",
                                     "-o", "/workspace/.reviewbench/go-public-test-report",
                                     task_root + "/go_public_test_report.go"]))
            grading_command = {
                "command": shlex.join(["/workspace/.reviewbench/go-public-test-report",
                                       task_root + "/public-tests.json"]),
                "parser": "json", "report_path": "reviewbench-go-public-report.json",
                "expected_tests": contract.expected,
            }
        else:
            grading_command = {
                "command": shlex.join([*entry, *command]), "parser": "regex",
                "expected_tests": contract.expected,
                "success_pattern": "^P2P_SIGNATURE=" + re.escape(contract.signature) + "$",
            }
        atomic_json(harness_root/"go-contract.json", {
            "schema_version": "reviewbench-fixed-go-contract/v1", "kind": contract.kind,
            "command": contract.command, "cwd": contract.cwd, "module_cwd": contract.module_cwd,
            "compiler_version": version, "provenance": contract.provenance,
            "transport_changes": contract.transport_changes,
            "whole_candidate_packages": True, "historical_production_overlay": False,
        })
        harness = ctx.bundle(harness_root, role="grader")
        runtime = ctx.bundle(adapter, role="toolchain")
        assets += [
            {**harness, "kind": "archive", "visibility": "grader", "target": "/opt/reviewbench-assets/go-oracle"},
            {**runtime, "kind": "archive", "visibility": "developer", "target": adapter_root},
        ]
    flags = relocate(spec.get("environment", {}).get("GOFLAGS", "-mod=readonly"), context)
    env = {"GOROOT": "/opt/reviewbench-assets/go",
           "PATH": "/opt/reviewbench-assets/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
           "GOTOOLCHAIN": "local", "GOPROXY": "file:///opt/reviewbench-assets/goproxy", "GOSUMDB": "off",
           "GOMODCACHE": "/opt/reviewbench-cache/gomod", "GOCACHE": "/opt/reviewbench-cache/gobuild",
           "GOMAXPROCS": "2", "GOFLAGS": flags + " -p=2", "SOURCE_ROOT": "/workspace",
           "TMPDIR": "/workspace/.reviewbench/tmp", "GOTMPDIR": "/workspace/.reviewbench/tmp",
           "HOME": "/workspace/.reviewbench/home", "COVERAGE_OUT": context["coverage_out"],
           "REVIEWED_PATH": task["locality"]["path"], "REVIEWBENCH_GO": go_binary,
           "REVIEWBENCH_GO_MODULE_DIR": module_root, "REVIEWBENCH_GO_SUPPORT": support_root}
    for key, value in spec.get("environment", {}).items():
        if key not in env:
            env[key] = relocate(str(value), context)
    if shadow_edits:
        from .go_modfile import shadow_environment
        env = shadow_environment(env)
        public_go_binary = adapter_root + "/go"
    else:
        public_go_binary = go_binary
    if relic:
        env = cgo_environment(env)
        bootstrap.append('test "$(uname -m)" = x86_64')
    recipe = RuntimeRecipe(
        task_id=task["id"], repo=task["repo"], start_sha=task["start_sha"], lane="gopy",
        image=image, image_audit=audit, workdir="/workspace"+("/"+contract.cwd if contract.cwd else ""),
        cpus=2, memory_mb=8192 if relic else 4096, build_scratch_mb=build_scratch_mb,
        command_timeout_seconds=spec.get("timeout", 1800),
        env=env, cache_mounts={"gomod": "/opt/reviewbench-cache/gomod", "gobuild": "/opt/reviewbench-cache/gobuild"},
        bootstrap=bootstrap,
        dev_checks=[public_build_check(ctx, task, contract, public_go_binary, module_root)],
        submodules=ctx.modules(task),
        assets=assets,
        grading={"multifile_build": True, "immutable_paths": ctx.immutable_tests(task),
                 "golden_patch": ctx.golden(task, oracle, "gopy", directory), "bootstrap": grade,
                 "commands": [grading_command]},
    )
    recipe.validate()
    return recipe
