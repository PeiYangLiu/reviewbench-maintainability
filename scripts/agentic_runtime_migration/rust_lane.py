"""Frozen Cargo tests with a checksum-verified directory source, not online resolution."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import tarfile
import time
import tomllib
import urllib.request

from reviewbench_maintainability.agentic.contracts import ContractError, RuntimeRecipe, atomic_json, canonical_hash, file_hash, repo_path
from reviewbench_maintainability.agentic.resources import file_lock
from reviewbench_maintainability.agentic.snapshots import git, start_archive, extract_tar, tree_files

IMAGE = "sha256:8268d755008211c1e35be7b2387dca7498d460008febae21e6af1ce0394094b5"


def load_plan(ctx, oracle):
    index = oracle["input_index"]
    # The historical runner updates its index for every row: the last record,
    # not the first recovery attempt, is the frozen executable specification.
    candidates = [r for r in map(json.loads, (ctx.legacy/"rust/plans.jsonl").read_text().splitlines())
                  if r["input_index"] == index]
    if not candidates:
        raise ContractError("Rust plan is absent for the frozen input index")
    row = candidates[-1]
    if row.get("plan_file"):
        path = ctx.legacy/"rust"/repo_path(row["plan_file"])
        if file_hash(path) != row["plan_file_sha256"]:
            raise ContractError("referenced Rust plan changed")
        row = {**json.loads(path.read_text()), **row}
        row.pop("plan_file", None)
        row.pop("plan_file_sha256", None)
    environment = oracle.get("environment", {})
    expected_toolchain = environment.get("variant_toolchains", {}).get("start", environment.get("toolchain"))
    if expected_toolchain and row["toolchain"] != expected_toolchain:
        raise ContractError("Rust plan toolchain differs from the frozen Oracle")
    command = environment.get("variant_commands", {}).get("start")
    if command and shlex.split(row.get("start_command", row["command"])) != shlex.split(command):
        raise ContractError("Rust plan command differs from the frozen Oracle")
    return row


def _same_crate_file(stream, member, target):
    """Compare a repeated regular member without buffering a whole file."""
    if target.stat().st_size != member.size:
        return False
    with stream.extractfile(member) as incoming, target.open("rb") as existing:
        while chunk := incoming.read(1024**2):
            if existing.read(len(chunk)) != chunk:
                return False
        return existing.read(1) == b""


def unpack_crate(archive, destination, prefix):
    """Keep published code bytes but omit ancillary .git directories in crates.

    The original archive checksum is verified before this function is called.
    Identical regular-file repetitions are idempotent; conflicting bytes,
    modes, names or types are rejected. All path/type/link checks still apply.
    """
    if destination.exists():
        raise ContractError("crate extraction destination already exists")
    destination.mkdir(parents=True)
    omitted, regular_members = [], {}
    try:
        with tarfile.open(archive, "r|gz") as stream:
            for member in stream:
                stream.members.clear()
                relative = PurePosixPath(member.name)
                if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] != prefix:
                    raise ContractError("crate member escapes its package identity")
                if ".git" in relative.parts:
                    omitted.append(member.name)
                    continue
                repo_path(member.name.rstrip("/"))
                if not (member.isfile() or member.isdir() or member.issym()):
                    raise ContractError("unsupported crate member type")
                target = destination / relative
                if any(parent.is_symlink() for parent in target.parents if parent != destination.parent):
                    raise ContractError("crate descends through a symlink")
                signature = member.name, member.type, member.size, member.mode
                if target.is_symlink():
                    raise ContractError("duplicate crate archive member: " + member.name)
                if target.exists() and not (target.is_dir() and member.isdir()):
                    if (member.isfile() and target.is_file()
                            and regular_members.get(relative) == signature
                            and _same_crate_file(stream, member, target)):
                        continue
                    raise ContractError("duplicate crate archive member: " + member.name)
                stream.extract(member, destination, filter="data")
                if member.isfile():
                    regular_members[relative] = signature
    except BaseException:
        shutil.rmtree(destination)
        raise
    return omitted


def missing_lock_assets(ctx, task, plan, oracle):
    """Admit only a symmetric, checksum-attested lock absent from the start."""
    locks = {}
    for replacement in plan.get("environment_file_replacements", []):
        relative = repo_path(replacement["path"])
        if (Path(relative).name != "Cargo.lock" or not replacement.get("allow_missing")
                or any(key.startswith(("start_", "golden_")) for key in replacement)):
            raise ContractError("Rust lock replacement needs a candidate-aware dependency overlay")
        try:
            git(ctx.repo(task), "show", task["start_sha"] + ":" + relative)
        except ContractError:
            pass
        else:
            raise ContractError("runtime lock injection must not overwrite a start-tree file")
        source = ctx.legacy / "rust" / repo_path(replacement["source"])
        expected = replacement.get("source_sha256")
        observed = file_hash(source)
        if not expected or observed != expected:
            raise ContractError("Rust lock asset differs from its declared checksum")
        for variant in ("start", "golden"):
            evidence = [item for item in oracle.get("environment_replacements", {}).get(variant, [])
                        if item.get("path") == relative]
            if not evidence or any(item.get("sha256") != observed for item in evidence):
                raise ContractError("Rust lock asset differs from frozen start/golden environment evidence")
        locks[relative] = ctx.copy_asset(source, "rust-locks")
    return locks


def compiler(ctx, version):
    registry = Path(ctx.config.get("rust_toolchains", ctx.output.parent/"rust-toolchains/toolchains.json"))
    data = json.loads(registry.read_text()) if registry.is_file() else {}
    record = data.get(version)
    if not record or record["status"] != "ready":
        raise ContractError("pinned Rust toolchain is not yet ready: "+version)
    source = Path(record["root"])
    pointer = ctx.output/"rust-compiler-assets"/(version+".json")
    with file_lock(ctx.output/"locks"/("rust-"+version+".lock")):
        if pointer.is_file():
            return json.loads(pointer.read_text())
        names = [name for name in tree_files(source) if Path(name).parts[0] in {"bin", "lib", "libexec", "etc"}]
        result = ctx.bundle(source, names, role="toolchain")
        atomic_json(pointer, result)
        return result


def crate_archive(ctx, name, version, checksum):
    filename = name+"-"+version+".crate"
    roots = [ctx.legacy/"rust/cache/cargo/registry/cache", Path("/home/v-peiyangliu/.cargo/registry/cache")]
    for root in roots:
        for path in root.glob("*/"+filename):
            if file_hash(path) == checksum:
                return path
    target = ctx.output/"crate-downloads"/filename
    with file_lock(ctx.output/"locks"/("crate-"+checksum)):
        if target.is_file() and file_hash(target) == checksum:
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        pending = target.with_suffix(".partial")
        try:
            with ctx.cache.transfers.acquire():
                with urllib.request.urlopen("https://static.crates.io/crates/"+name+"/"+filename, timeout=120) as response:
                    with pending.open("wb") as stream:
                        size = 0
                        while chunk := response.read(1024**2):
                            size += len(chunk)
                            if size > 256*1024**2:
                                raise ContractError("crate archive exceeds staging bound")
                            stream.write(chunk)
            if file_hash(pending) != checksum:
                raise ContractError("downloaded Rust crate differs from Cargo.lock checksum")
            pending.replace(target)
        finally:
            pending.unlink(missing_ok=True)
    return target


def missing_git_build_scripts(vendor):
    missing = []
    for checksum in vendor.glob("*/.cargo-checksum.json"):
        manifest = checksum.parent/"Cargo.toml"
        if not manifest.is_file() or json.loads(checksum.read_text()).get("package") is not None:
            continue
        package = tomllib.loads(manifest.read_text())["package"]
        build = package.get("build")
        if isinstance(build, str) and not (checksum.parent/repo_path(build)).is_file():
            missing.append((checksum, package, build))
    return missing


def restore_git_build_scripts(vendor, metadata, lock, cargo_cache):
    """Restore declared build scripts omitted by Git package include lists.

    Cargo's publication file selection may exclude an explicitly declared
    build.rs. Fetch only that tracked file from the dependency's locked commit,
    never from mutable checkout contents or the benchmark reference revision.
    """
    audit = []
    locked = {}
    for package in lock.get("package", []):
        source = package.get("source", "")
        if source.startswith("git+"):
            locked.setdefault((package["name"], package["version"]), set()).add(source.rsplit("#", 1)[-1])
    for checksum, package, build in missing_git_build_scripts(vendor):
        key = package["name"], package["version"]
        candidates = [item for item in metadata.get("packages", [])
                      if (item.get("name"), item.get("version")) == key
                      and (item.get("source") or "").startswith("git+")
                      and item["source"].rsplit("#", 1)[-1] in locked.get(key, set())]
        if len(candidates) != 1:
            raise ContractError("missing Git build script has ambiguous/unbound package identity")
        original = Path(candidates[0]["manifest_path"]).resolve()
        if not original.is_relative_to((cargo_cache/"git/checkouts").resolve()):
            raise ContractError("Git build-script metadata points outside its isolated checkout cache")
        commit = candidates[0]["source"].rsplit("#", 1)[-1]
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ContractError("Git build script has no immutable commit")
        repository = Path(git(original.parent, "rev-parse", "--show-toplevel").decode().strip())
        if git(repository, "rev-parse", "HEAD").decode().strip() != commit:
            raise ContractError("Git checkout revision differs from the frozen dependency")
        relative = (original.parent.relative_to(repository)/repo_path(build)).as_posix()
        entry = git(repository, "ls-tree", commit, "--", relative)
        if not entry.startswith((b"100644 ", b"100755 ")):
            raise ContractError("declared Git build script is not a tracked regular file")
        content = git(repository, "show", commit + ":" + relative)
        target = checksum.parent/repo_path(build)
        if not target.resolve().is_relative_to(checksum.parent.resolve()):
            raise ContractError("Git build-script destination escapes its package")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        target.chmod(0o755 if entry.startswith(b"100755 ") else 0o644)
        digest = hashlib.sha256(content).hexdigest()
        data = json.loads(checksum.read_text())
        data.setdefault("files", {})[build] = digest
        atomic_json(checksum, data)
        audit.append({"package": package["name"], "version": package["version"], "commit": commit,
                      "restored_file": build, "sha256": digest})
    return audit


def external_lock_packages(lock):
    result = {}
    for package in lock.get("package", []):
        source = package.get("source")
        if source:
            key = package["name"], package["version"], source
            result[key] = package.get("checksum") or lock.get("metadata", {}).get(
                f"checksum {key[0]} {key[1]} ({source})")
    return result


def validate_lock_normalization(before, after):
    old, new = external_lock_packages(before), external_lock_packages(after)
    if any(key not in old or old[key] != checksum for key, checksum in new.items()):
        raise ContractError("Cargo overlay normalization changed an external dependency identity/checksum")
    return {"external_before": len(old), "external_after": len(new),
            "external_added_or_changed": 0, "external_pruned": len(set(old) - set(new))}


def cargo_vendor(ctx, task, plan, cwd, locks, lock_path, *, build_spec=None, oracle=None):
    """Let the pinned Cargo implement Git/workspace source normalization."""
    registry = Path(ctx.config.get("rust_toolchains", ctx.output.parent/"rust-toolchains/toolchains.json"))
    tool = Path(json.loads(registry.read_text())[plan["toolchain"]]["root"])
    cargo = tool/"bin/cargo"
    cache = ctx.output/"cargo-vendor-cache"/task["start_sha"]
    cache.mkdir(parents=True, exist_ok=True)
    key = task["start_sha"] + "-" + str(plan["input_index"])
    logs = ctx.output/"cargo-vendor-logs"
    logs.mkdir(exist_ok=True)
    with file_lock(ctx.output/"locks"/("cargo-vendor-"+key)):
        with tempfile.TemporaryDirectory(prefix="git-cargo-vendor-", dir=ctx.scratch) as temporary:
            root = Path(temporary)
            workspace = root/"workspace"
            with start_archive(ctx.repo(task), task["start_sha"]) as stream:
                extract_tar(stream, workspace)
            if build_spec:
                from .rust_build_entry import apply_in_place
                from .rust_overlay import session_bytes
                for entry in plan.get("session_files", []):
                    path = workspace/repo_path(entry["path"])
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(session_bytes(ctx, plan, oracle, entry))
                lock_sources = {record["asset_target"]: locks[record["path"]]["path"]
                                for record in build_spec["locks"]}
                apply_in_place(workspace, build_spec, lock_sources)
            else:
                for relative, asset in locks.items():
                    destination = workspace/relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(asset["path"], destination)
            before = file_hash(workspace/lock_path)
            original_lock = tomllib.loads((workspace/lock_path).read_text())
            normalize = bool(build_spec and not build_spec.get("frozen_lock_recovery")
                             and "--locked" not in shlex.split(plan["command"]))
            destination = root/"vendor"
            environment = {key: value for key, value in os.environ.items()
                           if key in {"PATH", "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTP_PROXY", "HTTPS_PROXY",
                                      "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy"}}
            environment.update(HOME=str(cache/"home"), CARGO_HOME=str(cache),
                               RUSTC=str(tool/"bin/rustc"), CARGO_NET_GIT_FETCH_WITH_CLI="true",
                               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", GIT_TERMINAL_PROMPT="0",
                               CARGO_HTTP_MULTIPLEXING="false", CARGO_BUILD_JOBS="2",
                               GIT_CONFIG_COUNT="2", GIT_CONFIG_KEY_0="pack.threads", GIT_CONFIG_VALUE_0="2",
                               GIT_CONFIG_KEY_1="index.threads", GIT_CONFIG_VALUE_1="2")
            (cache/"home").mkdir(exist_ok=True)
            sync = []
            if build_spec:
                for entry in plan.get("session_files", []):
                    if Path(entry["path"]).name == "Cargo.toml":
                        sync.extend(["--sync", str(workspace/repo_path(entry["path"]))])
            from .rust_vendor import source_collisions, vendor_by_source
            source_locks = [original_lock]
            supplemental_locks = []
            if build_spec:
                source_locks.extend(tomllib.loads((workspace/repo_path(entry["path"])).read_text())
                                    for entry in plan.get("session_files", [])
                                    if Path(entry["path"]).name == "Cargo.lock")
                for record in build_spec["locks"]:
                    if record.get("when") is not None:
                        source = Path(locks[record["path"]]["path"])
                        if file_hash(source) != record["sha256"]:
                            raise ContractError("conditional Cargo vendor lock changed")
                        supplemental_locks.append(tomllib.loads(source.read_text()))
            from .rust_local_registry import requested as local_registry_requested
            if (source_collisions([*source_locks, *supplemental_locks]) or supplemental_locks
                    or local_registry_requested(ctx, task)):
                return vendor_by_source(
                    ctx, task, plan, workspace, lock_path,
                    [workspace/(cwd or ".")/"Cargo.toml", *map(Path, sync[1::2])],
                    cargo, environment, normalize=normalize,
                    **({"extra_locks": supplemental_locks} if supplemental_locks else {}),
                )
            command = [str(cargo), "vendor", *([] if normalize else ["--locked"]),
                       "--versioned-dirs", *sync, str(destination)]
            config_file, log = logs/(key+".toml"), logs/(key+".log")
            with config_file.open("w") as stdout, log.open("w") as stderr:
                process = subprocess.Popen(command, cwd=workspace/(cwd or "."), env=environment,
                                           stdout=stdout, stderr=stderr, start_new_session=True)
                started = time.monotonic()
                try:
                    while process.poll() is None:
                        if time.monotonic() - started > 1200:
                            raise ContractError("Cargo vendoring exceeded its 1200-second preparation budget")
                        if shutil.disk_usage(ctx.scratch).free < 80*1024**3:
                            raise ContractError("Cargo vendoring reached the 80-GiB disk floor")
                        time.sleep(.2)
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGTERM)
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait()
            if process.returncode:
                raise ContractError("pinned Cargo vendoring failed: " + log.read_text(errors="replace")[-1400:])
            after = file_hash(workspace/lock_path)
            normalized = (validate_lock_normalization(original_lock, tomllib.loads((workspace/lock_path).read_text()))
                          if normalize else None)
            if not normalize and after != before:
                raise ContractError("Cargo vendoring changed the frozen dependency lock")
            restored_build_scripts = []
            if missing_git_build_scripts(destination):
                metadata_command = [str(cargo), "metadata", "--locked", "--offline", "--format-version", "1"]
                result = subprocess.run(metadata_command, cwd=workspace/(cwd or "."), env=environment,
                                        capture_output=True, text=True, timeout=120)
                if result.returncode:
                    raise ContractError("Cargo could not attest missing Git build scripts: " + result.stderr[-1000:])
                restored_build_scripts = restore_git_build_scripts(
                    destination, json.loads(result.stdout), tomllib.loads((workspace/lock_path).read_text()), cache)
                if file_hash(workspace/lock_path) != after:
                    raise ContractError("Cargo metadata changed the frozen dependency lock")
            config = config_file.read_text().replace(str(destination), "/opt/reviewbench-assets/vendor")
            parsed = tomllib.loads(config)
            if not parsed.get("source"):
                raise ContractError("Cargo did not emit its required source replacement configuration")
            # Cargo has verified the original package archives. Exclude only
            # ancillary VCS metadata, preserving all vendored code hashes.
            for checksum in destination.glob("*/.cargo-checksum.json"):
                data = json.loads(checksum.read_text())
                data["files"] = {name: digest for name, digest in data.get("files", {}).items()
                                 if ".git" not in PurePosixPath(name).parts}
                atomic_json(checksum, data)
            config_file.write_text(config)
            audit = {"task": task["id"], "toolchain": plan["toolchain"], "lock_sha256": before,
                     "normalized_lock_sha256": after, "normalization": normalized,
                     "command": command, "exit_status": process.returncode, "scoring_result": False,
                     "config_sha256": file_hash(config_file), "restored_build_scripts": restored_build_scripts}
            atomic_json(logs/(key+".json"), audit)
            return ctx.bundle(destination), ctx.copy_asset(config_file, "cargo-source-config")


def vendor(ctx, task, plan, cwd, locks=None, *, build_spec=None, oracle=None):
    # Cargo uses the enclosing workspace lockfile, even for a nested package.
    directories = [Path(cwd or "."), *Path(cwd or ".").parents]
    conditional = {record["path"] for record in (build_spec or {}).get("locks", []) if record.get("when") is not None}
    lock = None
    for relative in directories:
        name = (relative/"Cargo.lock").as_posix()
        if name in (locks or {}) and name not in conditional:
            lock = tomllib.loads(Path(locks[name]["path"]).read_text())
            break
        try:
            lock = tomllib.loads(git(ctx.repo(task), "show", task["start_sha"]+":"+name).decode())
            break
        except ContractError:
            continue
    if lock is None:
        raise ContractError("Rust task needs a frozen lockfile asset")
    all_locks = [lock]
    if build_spec:
        if build_spec.get("frozen_lock_recovery"):
            # Include the unchanged committed public-entry lock as well as the
            # frozen private-session lock. Neither graph is re-resolved/pruned.
            for record in build_spec["locks"]:
                asset = Path(locks[record["path"]]["path"])
                if file_hash(asset) != record["sha256"]:
                    raise ContractError("recovered Rust vendor lock hash changed")
                all_locks.append(tomllib.loads(asset.read_text()))
        from .rust_overlay import session_bytes
        for entry in plan.get("session_files", []):
            if Path(entry["path"]).name == "Cargo.lock":
                all_locks.append(tomllib.loads(session_bytes(ctx, plan, oracle, entry).decode()))
    if conditional or any(p.get("source", "").startswith("git+") for data in all_locks for p in data.get("package", [])):
        if build_spec:
            return cargo_vendor(ctx, task, plan, cwd, locks or {}, name, build_spec=build_spec, oracle=oracle)
        return cargo_vendor(ctx, task, plan, cwd, locks or {}, name)
    packages = []
    seen = {}
    for data in all_locks:
        for package in data.get("package", []):
            source = package.get("source")
            if not source:
                continue
            if not source.startswith("registry+"):
                raise ContractError("Rust lockfile needs a pinned git-source vendor: "+source)
            name, version = package["name"], package["version"]
            if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                raise ContractError("invalid Cargo package name")
            repo_path(version)
            checksum = package.get("checksum") or data.get("metadata", {}).get(
                f"checksum {name} {version} ({source})")
            if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
                raise ContractError("frozen Cargo package checksum missing: "+name+" "+version)
            identity = name, version
            if identity in seen:
                if seen[identity] != checksum:
                    raise ContractError("conflicting Cargo checksums for one vendor package identity")
                continue
            seen[identity] = checksum
            packages.append((name, version, checksum))
    with tempfile.TemporaryDirectory(prefix="cargo-vendor-", dir=ctx.scratch) as directory:
        root = Path(directory)
        first_archives = None
        if (build_spec or {}).get("vfs_first_resolution"):
            from .rust_vfs_resolution import retained_archives
            first_archives = retained_archives(ctx, build_spec, locks)
        for name, version, checksum in packages:
            archive = (first_archives[(name, version, checksum)] if first_archives is not None
                       else crate_archive(ctx, name, version, checksum))
            temporary = root/(".extract-"+checksum[:12])
            omitted = unpack_crate(archive, temporary, name+"-"+version)
            if omitted:
                atomic_json(ctx.output/"crate-extraction-audits"/(checksum+".json"),
                            {"archive_sha256": checksum, "omitted_vcs_metadata": omitted})
            children = list(temporary.iterdir())
            if len(children) != 1 or children[0].name != name+"-"+version or not children[0].is_dir():
                raise ContractError("crate archive layout differs from Cargo package identity")
            destination = root/(name+"-"+version)
            children[0].replace(destination)
            temporary.rmdir()
            hashes = {p: file_hash(f) for p,f in tree_files(destination).items()
                      if p != ".cargo-checksum.json" and not f.is_symlink()}
            atomic_json(destination/".cargo-checksum.json", {"package": checksum, "files": hashes})
        if not packages:
            (root/"EMPTY_VENDOR").write_text("No registry packages in frozen lockfile.\n")
        return ctx.bundle(root), None


def migration_scratch_bytes(ctx, task):
    from . import rust_grpc, rust_zcash, rust_composable, rust_vector, rust_materialize, rust_tikv, rust_kamu, rust_gear, rust_fuel, rust_anchor
    # This adapter reuses existing archives and reads the retained native tree;
    # it only stages small root checkouts, the metadata binary and Python tools.
    # Actual public builds/calibration retain their separate recipe reservation.
    return (2 if (rust_grpc.requested(ctx, task) or rust_zcash.requested(ctx, task)
                  or rust_composable.requested(ctx, task) or rust_vector.requested(ctx, task)
                  or rust_materialize.requested(ctx, task) or rust_tikv.requested(ctx, task)
                  or rust_kamu.requested(ctx, task) or rust_gear.requested(ctx, task)
                  or rust_fuel.requested(ctx, task) or rust_anchor.requested(ctx, task)) else 12) * 1024**3


def migrate(ctx, task, oracle):
    from . import rust_anchor
    if rust_anchor.requested(ctx, task):
        return rust_anchor.migrate(ctx, task, oracle)
    from . import rust_fuel
    if rust_fuel.requested(ctx, task):
        return rust_fuel.migrate(ctx, task, oracle)
    from . import rust_gear
    if rust_gear.requested(ctx, task):
        return rust_gear.migrate(ctx, task, oracle)
    from . import rust_kamu
    if rust_kamu.requested(ctx, task):
        return rust_kamu.migrate(ctx, task, oracle)
    from . import rust_tikv
    if rust_tikv.requested(ctx, task):
        return rust_tikv.migrate(ctx, task, oracle)
    from . import rust_materialize
    if rust_materialize.requested(ctx, task):
        return rust_materialize.migrate(ctx, task, oracle)
    from . import rust_vector
    if rust_vector.requested(ctx, task):
        return rust_vector.migrate(ctx, task, oracle)
    from . import rust_composable
    if rust_composable.requested(ctx, task):
        return rust_composable.migrate(ctx, task, oracle)
    from . import rust_zcash
    if rust_zcash.requested(ctx, task):
        return rust_zcash.migrate(ctx, task, oracle)
    from . import rust_grpc
    if rust_grpc.requested(ctx, task):
        return rust_grpc.migrate(ctx, task, oracle)
    from . import rust_bitcoin
    if rust_bitcoin.requested(ctx, task):
        return rust_bitcoin.migrate(ctx, task, oracle)
    from . import rust_libp2p
    if rust_libp2p.requested(ctx, task):
        return rust_libp2p.migrate(ctx, task, oracle)
    from . import rust_project_toolchains
    if rust_project_toolchains.requested(ctx, task):
        return rust_project_toolchains.migrate(ctx, task, oracle)
    from . import rust_vcx
    if rust_vcx.requested(ctx, task):
        return rust_vcx.migrate(ctx, task, oracle)
    from . import rust_manifest_service
    if rust_manifest_service.requested(ctx, task):
        return rust_manifest_service.migrate(ctx, task, oracle)
    from . import rust_relay_service
    if rust_relay_service.requested(ctx, task):
        return rust_relay_service.migrate(ctx, task, oracle)
    from . import rust_session_overlay
    if rust_session_overlay.requested(ctx, task):
        return rust_session_overlay.migrate(ctx, task, oracle)
    from . import rust_locked_workspace
    if rust_locked_workspace.requested(ctx, task):
        return rust_locked_workspace.migrate(ctx, task, oracle)
    from . import rust_cargo_contract
    if rust_cargo_contract.requested(ctx, task):
        return rust_cargo_contract.migrate(ctx, task, oracle)
    from . import rust_clarity_submodule
    if rust_clarity_submodule.requested(ctx, task):
        return rust_clarity_submodule.migrate(ctx, task, oracle)
    from . import rust_rpgp_locks
    if rust_rpgp_locks.requested(ctx, task):
        return rust_rpgp_locks.migrate(ctx, task, oracle)
    from . import rust_image_locks
    if rust_image_locks.requested(ctx, task):
        return rust_image_locks.migrate(ctx, task, oracle)
    from . import rust_blazesym
    if rust_blazesym.requested(ctx, task):
        return rust_blazesym.migrate(ctx, task, oracle)
    plan = load_plan(ctx, oracle)
    if plan.get("test_compatibility_replacements"):
        from .rust_test_compat import validate_profile
        validate_profile(task, plan, oracle)
    arguments = shlex.split(plan["command"])
    if not arguments or arguments[0] != "cargo":
        raise ContractError("Rust shell/service command needs a private service adapter")
    arguments = [a for a in arguments[1:] if not a.startswith("+")]
    if not arguments or arguments[0] != "test":
        raise ContractError("Rust fixed command is not a Cargo test")
    from .rust_api import specification as api_specification
    api_spec = api_specification(ctx, task, plan, oracle)
    cwd = plan.get("workdir", ".")
    cwd = "" if cwd == "." else repo_path(cwd)
    directory = ctx.task_directory(task, oracle, "rust")
    patch = ctx.golden(task, oracle, "rust", directory)
    compatibility = None
    if plan.get("test_compatibility_replacements"):
        from .rust_test_compat import specification
        compatibility = specification(ctx, task, plan, oracle, patch)
    from .rust_lock_assets import recover
    from . import rust_first_resolution
    first_new = rust_first_resolution.requested(ctx, task)
    recovered = None if first_new else recover(ctx, task, plan, oracle, patch)
    needs_overlay = bool(plan.get("environment_replacements") or plan.get("prepare_command")
                         or any(not item.get("allow_missing") for item in plan.get("environment_file_replacements", [])))
    build_spec = None
    if first_new:
        build_spec, locks = rust_first_resolution.build_specification(ctx, task, plan, oracle, patch)
    elif recovered:
        build_spec, locks = recovered
        if plan.get("prepare_command"):
            # A retained lock is not an adapter for gRPC's recursive native
            # submodule preparation. Keep this separate input gap explicit.
            raise ContractError("Rust lock recovered; original non-lock preparation still needs an adapter: "
                                + plan["prepare_command"])
    elif needs_overlay:
        from .rust_overlay import build_specification
        build_spec, locks = build_specification(ctx, task, plan, oracle, patch)
    else:
        locks = missing_lock_assets(ctx, task, plan, oracle)
    toolchain = compiler(ctx, plan["toolchain"])
    if first_new:
        vendored, source_config = rust_first_resolution.vendor(ctx, build_spec, locks)
    elif build_spec:
        vendored, source_config = vendor(ctx, task, plan, cwd, locks, build_spec=build_spec, oracle=oracle)
    else:
        vendored, source_config = vendor(ctx, task, plan, cwd, locks)
    native_image, required_submodules = IMAGE, None
    if first_new and rust_first_resolution.profile_for(task["id"]).get("repository_lib_tests"):
        from .rust_libsbp_resolution import platform
        native_image, required_submodules = platform(ctx, build_spec)
    image, audit = ctx.image(native_image)
    grade_bootstrap, assets = [], [
        {**toolchain, "kind": "archive", "visibility": "developer", "target": "/opt/reviewbench-assets/rust"},
        {**vendored, "kind": "archive", "visibility": "developer", "target": "/opt/reviewbench-assets/vendor"},
    ]
    bootstrap = ["mkdir -p /workspace/.reviewbench/tmp"]
    for index, (relative, lock) in enumerate(sorted(locks.items())):
        target = (rust_first_resolution.LOCK_PREFIX if first_new else "/opt/reviewbench-assets/rust-lock-") + str(index)
        assets.append({**lock, "kind": "file", "visibility": "developer", "target": target})
        destination = "/workspace/" + relative
        if not build_spec:
            bootstrap.append("mkdir -p " + shlex.quote(str(Path(destination).parent)) +
                             " && cp " + shlex.quote(target) + " " + shlex.quote(destination))
    with tempfile.TemporaryDirectory(prefix="rust-tests-", dir=ctx.scratch) as directory:
        root = Path(directory)
        for entry in plan.get("session_files", []):
            if any(k in entry for k in ("start_content", "golden_content", "start_source", "golden_source")):
                raise ContractError("Rust variant test source requires a compatibility adapter")
            relative = repo_path(entry["path"])
            path = root/relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if "content" in entry:
                path.write_text(entry["content"])
            elif entry.get("source"):
                shutil.copyfile(ctx.legacy/"rust"/repo_path(entry["source"]), path)
            else:
                raise ContractError("Rust fixed test source missing")
            for variant in ("start", "golden"):
                evidence = [item for item in oracle.get("session_files", {}).get(variant, [])
                            if item.get("path") == relative]
                if not evidence or any(item.get("sha256") != file_hash(path) for item in evidence):
                    raise ContractError("Rust session assertion source differs from the frozen Oracle")
            destination = "/workspace/"+relative
            grade_bootstrap.append("mkdir -p "+shlex.quote(str(Path(destination).parent))+" && cp "
                                   +shlex.quote("/opt/reviewbench-assets/rust-tests/"+relative)+" "+shlex.quote(destination))
        if plan.get("session_files"):
            tests = ctx.bundle(root, role="grader")
            assets.append({**tests, "kind": "archive", "visibility": "grader",
                           "target": "/opt/reviewbench-assets/rust-tests"})
    expected = oracle.get("pass_to_pass_tests")
    if not expected:
        raise ContractError("frozen Rust test identities missing")
    config_args = ["--offline", "--config", 'source.crates-io.replace-with="reviewbench-vendor"',
                   "--config", 'source.reviewbench-vendor.directory="/opt/reviewbench-assets/vendor"']
    if source_config:
        target = "/opt/reviewbench-assets/cargo-sources.toml"
        assets.append({**source_config, "kind": "file", "visibility": "developer", "target": target})
        config_args = ["--offline", "--config", target]
    if not build_spec and plan["toolchain"] in {"1.59.0", "1.60.0"}:
        from .rust_cargo_home import bind as stable_source_config
        config_args, config_assets, config_bootstrap = stable_source_config(
            ctx, plan["toolchain"], source_config, config_args)
        assets.extend(config_assets)
        bootstrap.extend(config_bootstrap)
    if build_spec:
        from .rust_overlay import cargo_cli_prefix
        config_args = [*cargo_cli_prefix(ctx, plan["toolchain"]), *config_args]
    execute = ["cargo", *config_args, *arguments]
    if api_spec:
        from .rust_api import binding as api_binding
        execute, api_assets = api_binding(ctx, api_spec, config_args)
        assets.extend(api_assets)
    public = ["cargo", *config_args, "check"]
    if not build_spec or "--locked" in arguments:
        public.append("--locked")
    for option in ("-p", "--package", "--features", "--manifest-path"):
        if option in arguments:
            i = arguments.index(option)
            public += arguments[i:i+2]
    for option in ("--no-default-features", "--all-features"):
        if option in arguments:
            public.append(option)
    bounded_wash = compatibility is not None and compatibility.get("profile") == "wash569-public-test-import-v1"
    bounded_first_resolution = bool((build_spec or {}).get("vfs_first_resolution"))
    env = {
        "PATH": "/opt/reviewbench-assets/rust/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "RUSTC": "/opt/reviewbench-assets/rust/bin/rustc",
        "CARGO_HOME": "/opt/reviewbench-cache/cargo", "CARGO_TARGET_DIR": "/opt/reviewbench-cache/target",
        "CARGO_BUILD_JOBS": "2" if bounded_wash else "4", "CARGO_TERM_COLOR": "never", "CARGO_INCREMENTAL": "0",
        "CARGO_NET_OFFLINE": "true", "CARGO_PROFILE_DEV_DEBUG": "0", "CARGO_PROFILE_TEST_DEBUG": "0",
        "RUSTFLAGS": plan.get("rustflags", "--cap-lints=allow"), "RUST_BACKTRACE": "1",
        "SKIP_WASM_BUILD": "1", "RUST_BUILD_DOCKER": "1", "GIT_COMMIT": "reviewbench-hard-oracle",
        "GIT_DIRTY": "false", "SOURCE_TIMESTAMP": "0", "TMPDIR": "/workspace/.reviewbench/tmp",
        "PROTOC_INCLUDE": "/usr/include",
    }
    from .rust_protoc import binding as protoc_binding, TARGET as PROTOC_TARGET
    tool_assets, tool_env, tool_bootstrap = protoc_binding(ctx, task)
    if tool_assets:
        assets.extend(tool_assets)
        env.update(tool_env)
        env["PATH"] = PROTOC_TARGET + "/bin:" + env["PATH"]
        bootstrap.extend(tool_bootstrap)
    from .rust_v8 import binding as v8_binding
    v8_assets, v8_env, v8_bootstrap = v8_binding(ctx, task)
    if v8_assets:
        assets.extend(v8_assets)
        env.update(v8_env)
        bootstrap.extend(v8_bootstrap)
    if plan.get("env"):
        if not build_spec:
            raise ContractError("Rust environment paths need an explicit portable adapter")
        from .rust_overlay import portable_environment
        additions, setup = portable_environment(ctx, plan, env)
        env.update(additions)
        bootstrap.extend(setup)
    cache_mounts = {"cargo": "/opt/reviewbench-cache/cargo", "target": "/opt/reviewbench-cache/target"}
    public_cwd = cwd
    if build_spec:
        from . import rust_overlay
        private_manifests = {item["path"] for item in plan.get("session_files", [])
                             if Path(item["path"]).name == "Cargo.toml"}
        private_packages = {
            tomllib.loads(rust_overlay.session_bytes(ctx, plan, oracle, item).decode()).get("package", {}).get("name")
            for item in plan.get("session_files", []) if item["path"] in private_manifests}
        private_selection = any(option in arguments and arguments[arguments.index(option)+1] in values
                                for option, values in [("--manifest-path", private_manifests),
                                                       ("-p", private_packages), ("--package", private_packages)])
        if private_selection:
            public = ["cargo", *config_args, "check", "-p", rust_overlay.production_package(ctx, task)]
            if "--locked" in arguments:
                public.append("--locked")
        if recovered:
            recovery = build_spec["frozen_lock_recovery"]
            public_cwd = recovery["public_cwd"]
            if recovery.get("public_package"):
                public = ["cargo", *config_args, "check", "--locked", "-p", recovery["public_package"]]
        tools = rust_overlay.build_tools(ctx)
        spec_file = ctx.output/"rust-build-specs"/(canonical_hash(build_spec)+".json")
        atomic_json(spec_file, build_spec)
        assets.extend([
            {**tools, "kind": "archive", "visibility": "developer", "target": rust_overlay.TOOLS},
            {**ctx.copy_asset(spec_file, "rust-build-spec"), "kind": "file", "visibility": "developer",
             "target": rust_overlay.SPEC},
        ])
        cache_mounts["rust-build"] = rust_overlay.BUILD_CACHE
        if compatibility:
            from .rust_test_compat import binding
            execute, public, compatibility_assets = binding(ctx, compatibility, execute, public)
            assets.extend(compatibility_assets)
        if first_new:
            execute, public_command, resolution_assets = rust_first_resolution.binding(
                ctx, build_spec, execute, public)
            assets.extend(resolution_assets)
        elif bounded_first_resolution:
            from .rust_vfs_resolution import binding
            execute, public_command, resolution_assets = binding(ctx, build_spec, execute, public)
            assets.extend(resolution_assets)
        elif recovered:
            def lock_wrapper(command, directory):
                return shlex.join(["python3", rust_overlay.TOOLS + "/locks.py", "--spec", rust_overlay.SPEC,
                                   "--task-id", task["id"], "--cwd", directory or ".", "--", *command])
            execute = lock_wrapper(execute, cwd)
            public_command = lock_wrapper(public, public_cwd)
        else:
            execute = rust_overlay.command_wrapper(execute, cwd)
            public_command = rust_overlay.command_wrapper(public, cwd)
    else:
        execute = shlex.join(execute)
        public_command = shlex.join(public)
    recipe = RuntimeRecipe(
        task_id=task["id"], repo=task["repo"], start_sha=task["start_sha"], lane="rust",
        image=image, image_audit=audit, workdir="/workspace"+("/"+cwd if cwd and not recovered else ""),
        cpus=2 if bounded_wash else 4, memory_mb=4096 if bounded_wash else 8192,
        build_scratch_mb=8192 if bounded_wash else 24576,
        command_timeout_seconds=plan.get("timeout_seconds",1800),
        env=env, cache_mounts=cache_mounts,
        bootstrap=bootstrap, dev_checks=[public_command], scratch_paths=[] if build_spec else sorted(locks),
        assets=assets, submodules=(required_submodules if required_submodules is not None else ctx.modules(task)),
        grading={"multifile_build": True, "immutable_paths": (
                    sorted(set(ctx.immutable_tests(task)) | {row["path"] for row in compatibility["files"]})
                    if compatibility else ctx.immutable_tests(task)),
                 "golden_patch": patch,
                 "bootstrap": grade_bootstrap,
                 "commands": [{"command": execute, "parser": "rust-test", "expected_tests": expected}]},
    )
    recipe.validate()
    return recipe
