"""Source-aware Cargo directory assets for colliding name/version pairs.

The normal Cargo vendor path remains preferable when it can represent the
graph. Here the *full* locked source, not just (name, version), owns a directory.
Registry bytes come from checksum-verified .crate archives; Git bytes come from
the locked Git objects, including pinned submodules, never mutable checkouts.
The pinned Cargo must resolve the result offline with an unchanged lock.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import tomllib
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import tomli_w

from reviewbench_maintainability.agentic.contracts import ContractError, atomic_json, file_hash, repo_path
from reviewbench_maintainability.agentic.snapshots import extract_tar, git, start_archive, tree_files, validate_link

TARGET = "/opt/reviewbench-assets/vendor"
CRATES_IO = "registry+https://github.com/rust-lang/crates.io-index"
DOCUMENT_TREE_FILES = 1024
DOCUMENT_TREE_BYTES = 16 * 1024**2


def git_source_identity(source):
    """Compare Cargo's two URL spellings without changing a pinned identity.

    Cargo.lock percent-encodes branch slashes that cargo metadata may render
    literally. Only the parsed selector is decoded: repository path, selector
    kind/value, and the complete commit must still match.
    """
    if not isinstance(source, str) or not source.startswith("git+"):
        raise ContractError("Git source identity requires an immutable Git source")
    url = urlsplit(source[4:])
    try:
        query = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as error:
        raise ContractError("invalid Cargo Git selector") from error
    if (not re.fullmatch(r"[0-9a-f]{40}", url.fragment) or len(query) > 1
            or any(key not in {"branch", "tag", "rev"} or not value for key, value in query)
            or not url.path or url.scheme not in {"https", "http", "ssh", "git", "file"}
            or (url.scheme != "file" and not url.netloc)):
        raise ContractError("invalid immutable Cargo Git identity")
    return url.scheme, url.netloc, url.path, tuple(query), url.fragment


def locked_packages(locks):
    packages = {}
    for lock in locks:
        for package in lock.get("package", []):
            source = package.get("source")
            if not source:
                continue
            name, version = package["name"], package["version"]
            if not re.fullmatch(r"[A-Za-z0-9_-]+", name) or not re.fullmatch(r"[A-Za-z0-9.+_-]+", version):
                raise ContractError("invalid Cargo vendor package name/version")
            key = name, version, source
            checksum = package.get("checksum") or lock.get("metadata", {}).get(
                f"checksum {name} {version} ({source})")
            if source.startswith("registry+"):
                if source != CRATES_IO:
                    raise ContractError("source-aware vendor has no attested archive provider for " + source)
                if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
                    raise ContractError("frozen Cargo package checksum missing: " + name + " " + version)
            elif not source.startswith("git+"):
                raise ContractError("unsupported locked Cargo source: " + source)
            elif checksum not in (None, "<none>"):
                raise ContractError("unexpected checksum for a locked Git package")
            checksum = None if checksum == "<none>" else checksum
            if key in packages and packages[key] != checksum:
                raise ContractError("conflicting checksums for one full Cargo source identity")
            packages[key] = checksum
    return packages


def source_collisions(locks):
    """Detection alone must not impose new rules on the ordinary vendor path."""
    identities = {}
    for lock in locks:
        for package in lock.get("package", []):
            if package.get("source"):
                identities.setdefault((package["name"], package["version"]), set()).add(package["source"])
    return [{"name": name, "version": version, "sources": sorted(sources)}
            for (name, version), sources in sorted(identities.items()) if len(sources) > 1]


def source_configuration(sources, target=TARGET, *, registry_transport="directory"):
    if registry_transport not in {"directory", "local-registry"}:
        raise ContractError("unsupported registry transport")
    config, roots, selectors = {}, {}, {}
    for source in sorted(set(sources)):
        digest = hashlib.sha256(source.encode()).hexdigest()
        directory = "source-" + digest
        replacement = "reviewbench-" + directory
        if source == CRATES_IO:
            name, record = "crates-io", {}
        elif source.startswith("git+"):
            url = urlsplit(source[4:])
            if not re.fullmatch(r"[0-9a-f]{40}", url.fragment):
                raise ContractError("Git vendor source has no immutable full commit")
            try:
                query = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True)
            except ValueError as error:
                raise ContractError("invalid Cargo Git selector") from error
            if len(query) > 1 or any(key not in {"branch", "tag", "rev"} or not value for key, value in query):
                raise ContractError("unsupported/ambiguous Cargo Git selector")
            base = urlunsplit((url.scheme, url.netloc, url.path, "", ""))
            if (not url.path or url.scheme not in {"https", "http", "ssh", "git", "file"}
                    or (url.scheme != "file" and not url.netloc)):
                raise ContractError("invalid Cargo Git source URL")
            selector = (base, tuple(query))
            if selector in selectors and selectors[selector] != source:
                raise ContractError("one Cargo Git selector is locked to multiple commits across manifests")
            selectors[selector] = source
            name, record = "reviewbench-original-" + digest, {"git": base, **dict(query)}
        else:
            raise ContractError("unsupported source-aware Cargo source: " + source)
        roots[source] = directory
        config[name] = {**record, "replace-with": replacement}
        kind = registry_transport if source == CRATES_IO else "directory"
        config[replacement] = {kind: str(Path(target) / directory)}
    return tomli_w.dumps({"source": config}), roots


def _snapshot(repository, revision, destination, *, depth=0):
    if depth > 16:
        raise ContractError("Git vendor submodule nesting exceeds its bound")
    with start_archive(repository, revision) as stream:
        extract_tar(stream, destination)
    for entry in git(repository, "ls-tree", "-r", "-z", revision).split(b"\0"):
        if not entry or not entry.startswith(b"160000 "):
            continue
        fields, name = entry.split(b"\t", 1)
        relative = repo_path(name.decode())
        commit = fields.split()[2].decode()
        child = repository / relative
        if not child.is_dir() or git(child, "rev-parse", "HEAD").decode().strip() != commit:
            raise ContractError("pinned Git vendor submodule is absent or at a different commit: " + relative)
        target = destination / relative
        if target.exists():
            target.rmdir()  # git archive creates an empty gitlink directory.
        _snapshot(child, commit, target, depth=depth + 1)


def _workspace(snapshot, manifest, document):
    explicit = document["package"].get("workspace")
    if explicit:
        candidates = [(manifest.parent / explicit / "Cargo.toml").resolve()]
    else:
        candidates = [parent / "Cargo.toml" for parent in [manifest.parent, *manifest.parent.parents]
                      if parent.is_relative_to(snapshot)]
    for candidate in candidates:
        if not candidate.is_relative_to(snapshot):
            raise ContractError("Git package workspace escapes its locked repository")
        if candidate.is_file():
            data = tomllib.loads(candidate.read_text())
            if "workspace" in data:
                return candidate.parent, data["workspace"]
    if explicit:
        raise ContractError("Git package has no locked workspace manifest")
    return manifest.parent, {}


def standalone_manifest(snapshot, manifest, destination):
    """Keep Cargo in charge of workspace inheritance and feature merging.

    Each Git package receives its original workspace's inheritance tables (not
    its member list). Cargo thus resolves `workspace = true` exactly as before,
    including renamed/optional/target dependencies, without guessing versions
    or converting Git path edges into crates.io edges.
    """
    document = tomllib.loads(manifest.read_text())
    owner, workspace = _workspace(snapshot, manifest, document)
    inherited = {key: copy.deepcopy(workspace[key]) for key in ("package", "dependencies", "lints")
                 if key in workspace}
    # These two inherited package fields name workspace-relative files. Carry
    # the same tracked bytes with the package instead of reading a host path.
    for key in ("readme", "license-file"):
        value = inherited.get("package", {}).get(key)
        if not isinstance(value, str) or document["package"].get(key) != {"workspace": True}:
            continue
        original = (owner / value).resolve()
        if not original.is_relative_to(snapshot) or not original.is_file():
            raise ContractError("inherited Git package file is outside its locked snapshot")
        relative = "reviewbench-workspace-files/" + key
        target = destination / relative
        if target.exists() or target.is_symlink():
            raise ContractError("Git package collides with its inherited-file staging path")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, target)
        inherited["package"][key] = relative
    document["package"].pop("workspace", None)
    document.pop("workspace", None)
    if inherited:
        document["workspace"] = inherited
    return tomli_w.dumps(document)


def _materialize_git_document_tree(snapshot, referent, target, original_link):
    """Copy a bounded, link-free, non-executable original ``docs`` tree."""
    if not referent.is_relative_to(snapshot) or not referent.is_dir():
        raise ContractError("Git docs directory escapes its locked snapshot")
    entries, total = [], 0
    for directory, dirs, files in os.walk(referent, followlinks=False):
        directory = Path(directory)
        if len(directory.relative_to(referent).parts) > 32:
            raise ContractError("Git docs directory exceeds its depth bound")
        for name in sorted(dirs):
            path = directory / name
            if path.is_symlink() or not path.is_dir() or name in {".git", ".reviewbench"}:
                raise ContractError("Git docs directory has an unbound nested directory")
        for name in sorted(files):
            path = directory / name
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o111:
                raise ContractError("Git docs directory has a link, special or executable file")
            total += info.st_size
            if len(entries) >= DOCUMENT_TREE_FILES or total > DOCUMENT_TREE_BYTES:
                raise ContractError("Git docs directory exceeds its payload bound")
            entries.append({
                "path": path.relative_to(referent).as_posix(),
                "sha256": file_hash(path), "bytes": info.st_size, "mode": info.st_mode & 0o777,
            })
    target.unlink()
    shutil.copytree(referent, target, symlinks=False)
    for row in entries:
        path = target / row["path"]
        if file_hash(path) != row["sha256"] or path.stat().st_mode & 0o777 != row["mode"]:
            raise ContractError("Git docs directory changed during materialization")
    return {
        "path": target.name, "original_link_target": original_link,
        "locked_referent": referent.relative_to(snapshot).as_posix(),
        "files": sorted(entries, key=lambda row: row["path"]), "bytes": total,
        "transport": "locked-workspace-document-tree-bytes",
    }


def materialize_git_document_links(snapshot, original, destination):
    """Carry root package documents from their immutable workspace referents.

    A Git workspace can share its license/readme with a member using ``../``.
    Once that member becomes a standalone directory source, such a link would
    leave its package. Copy only exact non-executable document bytes or a
    bounded root ``docs`` tree inside the locked Git snapshot. Other directory,
    source/build-script/external links still fail; internal links stay unchanged.
    """
    snapshot, original, destination = [Path(p).resolve() for p in (snapshot, original, destination)]
    if not original.is_relative_to(snapshot):
        raise ContractError("Git document owner escapes its locked snapshot")
    audit = []
    documents = re.compile(
        r"(?:(?:LICENSE|LICENCE|COPYING|NOTICE)(?:[.-][A-Za-z0-9_.-]+)?"
        r"|(?:README|CONTRIBUTING)(?:\.(?:md|rst|txt))?)", re.I)
    for path in sorted(destination.iterdir()):
        if not path.is_symlink():
            continue
        try:
            if path.resolve().is_relative_to(destination):
                continue
        except (OSError, RuntimeError) as error:
            raise ContractError("Git package document has an unresolved link") from error
        if not documents.fullmatch(path.name) and path.name != "docs":
            raise ContractError("Git package link outside its package is not a root document: " + path.name)
        source = original / path.name
        link = os.readlink(path)
        validate_link((original.relative_to(snapshot) / path.name).as_posix(), link)
        if not source.is_symlink() or os.readlink(source) != link:
            raise ContractError("Git package document link differs from its locked input")
        try:
            referent = source.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ContractError("Git package document has no regular locked referent") from error
        if path.name == "docs":
            audit.append(_materialize_git_document_tree(snapshot, referent, path, link))
            continue
        if (not referent.is_relative_to(snapshot) or not referent.is_file()
                or referent.stat().st_mode & 0o111 or referent.stat().st_size > 1024**2):
            raise ContractError("Git package document referent is outside/beyond its locked document bound")
        with referent.open("rb") as stream:
            data = stream.read(1024**2 + 1)
        if len(data) > 1024**2:
            raise ContractError("Git package document exceeds its bound")
        path.unlink()
        path.write_bytes(data)
        path.chmod(referent.stat().st_mode & 0o777)
        audit.append({
            "path": path.name, "original_link_target": link,
            "locked_referent": referent.relative_to(snapshot).as_posix(),
            "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
            "mode": path.stat().st_mode & 0o777,
            "transport": "locked-workspace-document-bytes",
        })
    return audit


def materialize(locks, metadata, cargo_cache, destination, archive_provider, *, registry_index_provider=None):
    """Return config/audit only after every full locked identity was copied."""
    from .rust_lane import unpack_crate
    packages = locked_packages(locks)
    config, roots = source_configuration(
        (key[2] for key in packages),
        registry_transport="local-registry" if registry_index_provider is not None else "directory")
    if destination.exists():
        raise ContractError("source-aware vendor destination already exists")
    observed = {}
    aliases = {}
    for name, version, source in packages:
        if not source.startswith("git+"):
            continue
        key = name, version, git_source_identity(source)
        if key in aliases and aliases[key] != source:
            raise ContractError("ambiguous equivalent Cargo Git source spellings in locked inputs")
        aliases[key] = source
    for item in metadata.get("packages", []):
        source = item.get("source")
        if not source:
            continue
        if source.startswith("git+"):
            source = aliases.get(
                (item["name"], item["version"], git_source_identity(source)), source
            )
        key = item["name"], item["version"], source
        if key in observed and observed[key]["manifest_path"] != item["manifest_path"]:
            raise ContractError("ambiguous metadata for a full Cargo source identity")
        observed[key] = item
    destination.mkdir(parents=True)
    audit, snapshots = [], {}
    try:
        with tempfile.TemporaryDirectory(prefix="locked-git-", dir=destination.parent) as temporary:
            for (name, version, source), checksum in sorted(packages.items()):
                target = destination / roots[source] / (name + "-" + version)
                target.parent.mkdir(exist_ok=True)
                row = {"name": name, "version": version, "source": source, "package_checksum": checksum,
                       "directory": target.relative_to(destination).as_posix()}
                if source == CRATES_IO:
                    archive = archive_provider(name, version, checksum)
                    if file_hash(archive) != checksum:
                        raise ContractError("registry archive differs from its full locked identity checksum")
                    if registry_index_provider is not None:
                        from .rust_local_registry import materialize_package
                        row.update(materialize_package(
                            target.parent, name, version, checksum, archive,
                            registry_index_provider(name, version, checksum)))
                        row["directory"] = target.parent.relative_to(destination).as_posix()
                        audit.append(row)
                        continue
                    staging = Path(temporary) / ("crate-" + checksum)
                    row["omitted_vcs_metadata"] = unpack_crate(archive, staging, name + "-" + version)
                    children = list(staging.iterdir())
                    if len(children) != 1 or children[0].name != name + "-" + version or not children[0].is_dir():
                        raise ContractError("crate archive layout differs from Cargo package identity")
                    children[0].replace(target)
                    staging.rmdir()
                else:
                    item = observed.get((name, version, source))
                    if item is None:
                        raise ContractError("locked Git package is absent from Cargo metadata: " + name + " " + source)
                    if item["source"] != source:
                        row["metadata_source_alias"] = item["source"]
                    manifest = Path(item["manifest_path"]).resolve()
                    if not manifest.is_relative_to((cargo_cache / "git/checkouts").resolve()):
                        raise ContractError("Git vendor metadata points outside its isolated checkout cache")
                    repository = Path(git(manifest.parent, "rev-parse", "--show-toplevel").decode().strip()).resolve()
                    revision = source.rsplit("#", 1)[-1]
                    if (not repository.is_relative_to((cargo_cache / "git/checkouts").resolve())
                            or git(repository, "rev-parse", "HEAD").decode().strip() != revision):
                        raise ContractError("Git vendor checkout revision differs from its locked source")
                    if source not in snapshots:
                        snapshot = Path(temporary) / roots[source]
                        _snapshot(repository, revision, snapshot)
                        snapshots[source] = snapshot
                    snapshot = snapshots[source]
                    original = snapshot / manifest.relative_to(repository)
                    if not original.is_file():
                        raise ContractError("Git package manifest is absent from the locked commit")
                    # Files (including build.rs excluded from publication) come
                    # from immutable Git objects, not the working checkout.
                    shutil.copytree(original.parent, target, symlinks=True,
                                    ignore=shutil.ignore_patterns(".git", ".cargo-checksum.json"))
                    row["materialized_document_links"] = materialize_git_document_links(
                        snapshot, original.parent, target)
                    generated = standalone_manifest(snapshot, original, target)
                    row["original_manifest_sha256"] = file_hash(original)
                    (target / "Cargo.toml").write_text(generated)
                    row["vendored_manifest_sha256"] = file_hash(target / "Cargo.toml")
                    row["commit"] = revision
                hashes = {}
                for relative, path in tree_files(target).items():
                    if relative == ".cargo-checksum.json":
                        continue
                    if path.is_symlink() and path.is_dir():
                        # A directory is not a file and has no Cargo file
                        # checksum. Preserve the original internal link (for
                        # example SQLx's migration-symlink fixture), while the
                        # real referent files remain individually checksummed.
                        resolved = path.resolve(strict=True)
                        if (not resolved.is_relative_to(target.resolve())
                                or path.parent.resolve().is_relative_to(resolved)):
                            raise ContractError("Git vendor directory link escapes or cycles")
                        referents = tree_files(resolved)
                        if (not referents or len(referents) > DOCUMENT_TREE_FILES
                                or any(p.is_symlink() or not p.is_file() for p in referents.values())
                                or sum(p.stat().st_size for p in referents.values()) > DOCUMENT_TREE_BYTES):
                            raise ContractError("Git vendor directory-link referent is unbound or too large")
                        row.setdefault("internal_directory_links", []).append({
                            "path": relative, "target": os.readlink(path),
                            "locked_referent": resolved.relative_to(target.resolve()).as_posix(),
                            "referent_files": {name: file_hash(p) for name, p in referents.items()},
                            "link_type_preserved": True,
                        })
                        continue
                    # A checksum must cover linked code too. Reject a link
                    # that would read outside the self-contained package.
                    if path.is_symlink() and (not path.resolve().is_relative_to(target.resolve())
                                              or not path.is_file()):
                        raise ContractError("Git/registry vendor symlink escapes its package or is not a file")
                    hashes[relative] = file_hash(path)
                atomic_json(target / ".cargo-checksum.json", {"package": checksum, "files": hashes})
                row["files"] = len(hashes)
                audit.append(row)
    except BaseException:
        shutil.rmtree(destination)
        raise
    return config, audit


def _run(command, *, cwd, env, prefix, deadline, scratch):
    stdout, stderr = prefix.with_suffix(".stdout"), prefix.with_suffix(".stderr")
    record = {"command": command, "cwd": str(cwd), "stdout": str(stdout), "stderr": str(stderr),
              "exit_status": None}
    process = None
    try:
        with stdout.open("w") as out, stderr.open("w") as err:
            process = subprocess.Popen(command, cwd=cwd, env=env, stdout=out, stderr=err, start_new_session=True)
            try:
                while process.poll() is None:
                    if time.monotonic() >= deadline:
                        raise ContractError("source-aware Cargo preparation exceeded its 1200-second budget")
                    if shutil.disk_usage(scratch).free < 80 * 1024**3:
                        raise ContractError("source-aware Cargo preparation reached the 80-GiB disk floor")
                    time.sleep(.1)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
    except BaseException as error:
        record["error"] = type(error).__name__ + ": " + str(error)
        raise
    finally:
        record["exit_status"] = process.returncode if process else None
        atomic_json(prefix.with_suffix(".json"), record)
    if process.returncode:
        raise ContractError("source-aware Cargo metadata failed: " + stderr.read_text(errors="replace")[-1400:])
    return json.loads(stdout.read_text()), record


def vendor_by_source(ctx, task, plan, workspace, lock_path, manifests, cargo, environment, *, normalize, extra_locks=()):
    """Prepare all synced graphs; validate each using an empty offline cache."""
    from .rust_lane import crate_archive, validate_lock_normalization
    from .rust_overlay import cargo_cli_prefix
    key = task["start_sha"] + "-" + str(plan["input_index"]) + "-sources"
    logs = ctx.output / "cargo-vendor-logs"
    manifests = list(dict.fromkeys(manifests))
    from . import rust_local_registry
    transport = rust_local_registry.profile(ctx, task, plan, workspace, manifests, environment)
    metadata_arguments = transport["metadata_arguments"] if transport else []
    if transport:
        if normalize:
            raise ContractError("local registry transport must not perform a new resolution")
        environment = dict(environment, CARGO_NET_OFFLINE="true")
        atomic_json(logs / (key + "-transport.json"), transport)
    # Snapshot all lock inputs before Cargo can normalize any workspace.
    paths = {workspace / lock_path}
    for manifest in manifests:
        for parent in [manifest.parent, *manifest.parent.parents]:
            if not parent.is_relative_to(workspace):
                break
            if (parent / "Cargo.lock").is_file():
                paths.add(parent / "Cargo.lock")
                break
    before = {path: path.read_bytes() for path in paths}
    deadline = time.monotonic() + 1200
    metadata, commands, graph_signatures = {"packages": []}, [], []
    for index, manifest in enumerate(manifests):
        command = [str(cargo), "metadata", "--format-version", "1", "--all-features",
                   *([] if normalize else ["--locked"]), *metadata_arguments,
                   "--manifest-path", str(manifest)]
        result, record = _run(command, cwd=manifest.parent, env=environment, deadline=deadline,
                              scratch=ctx.scratch, prefix=logs / (key + "-prepare-" + str(index)))
        metadata["packages"].extend(result["packages"])
        if transport:
            graph_signatures.append(rust_local_registry.graph_signature(result))
        commands.append(record)
    normalization = {}
    for path, data in before.items():
        old, new = tomllib.loads(data.decode()), tomllib.loads(path.read_text())
        if not normalize and path.read_bytes() != data:
            raise ContractError("source-aware Cargo changed the frozen dependency lock")
        normalization[path.relative_to(workspace).as_posix()] = validate_lock_normalization(old, new)
    after = {path: path.read_bytes() for path in paths}
    locks = [tomllib.loads(data.decode()) for data in after.values()]
    original_packages = locked_packages(locks)
    # Conditional runtime defaults need both graphs present offline. Merely
    # vendoring the active preparation candidate drops the other resolution.
    locks.extend(extra_locks)
    supplemental = sorted(set(locked_packages(locks)) - set(original_packages))
    destination = workspace.parent / "vendor"
    cache = Path(environment["CARGO_HOME"])

    def archive_provider(name, version, checksum):
        # Metadata/fetch has already populated this isolated cache. Avoid a
        # second network request or falling back to mutable unpacked sources.
        for archive in (cache / "registry/cache").glob("*/" + name + "-" + version + ".crate"):
            if file_hash(archive) == checksum:
                return archive
        return crate_archive(ctx, name, version, checksum)

    index_provider = None
    if transport:
        index = rust_local_registry.sparse_index_root(cache)
        index_provider = lambda name, version, checksum: rust_local_registry.sparse_index_entry(
            index, name, version, checksum)
    config, packages = materialize(
        locks, metadata, cache, destination, archive_provider,
        **({"registry_index_provider": index_provider} if transport else {}))
    local_config = workspace.parent / "cargo-sources-local.toml"
    local_config.write_text(config.replace(TARGET, str(destination)))
    # No fetched cache can mask a missing package or a wrong source replacement.
    offline = dict(environment, CARGO_HOME=str(workspace.parent / "offline-cargo"), CARGO_NET_OFFLINE="true")
    cli = cargo_cli_prefix(ctx, plan["toolchain"])
    for index, manifest in enumerate(manifests):
        command = [str(cargo), *cli, "--config", str(local_config), "metadata", "--locked", "--offline",
                   "--all-features", "--format-version", "1",
                   *(["--filter-platform", transport["profile"]["platform"]] if transport else []),
                   "--manifest-path", str(manifest)]
        result, record = _run(command, cwd=manifest.parent, env=offline, deadline=deadline,
                              scratch=ctx.scratch, prefix=logs / (key + "-offline-" + str(index)))
        if transport and rust_local_registry.graph_signature(result) != graph_signatures[index]:
            raise ContractError("local registry transport changed the original resolved native graph")
        commands.append(record)
    if any(path.read_bytes() != data for path, data in after.items()):
        raise ContractError("source-aware offline validation changed the frozen dependency lock")
    config_file = logs / (key + ".toml")
    config_file.write_text(config)
    audit = {
        "task": task["id"], "toolchain": plan["toolchain"], "layout": "source-aware/v1",
        "scoring_result": False, "commands": commands, "packages": packages, "normalization": normalization,
        "locks": {path.relative_to(workspace).as_posix(): {
            "before": hashlib.sha256(before[path]).hexdigest(), "after": hashlib.sha256(data).hexdigest()
        } for path, data in after.items()},
        "config_sha256": file_hash(config_file), "offline_metadata_verified": True,
        "additional_locked_packages": [{"name": name, "version": version, "source": source}
                                       for name, version, source in supplemental],
    }
    if transport:
        audit.update(transport=transport, native_graph_signatures=graph_signatures,
                     original_native_graphs_unchanged=True)
    atomic_json(logs / (key + ".json"), audit)
    return ctx.bundle(destination), ctx.copy_asset(config_file, "cargo-source-config")
