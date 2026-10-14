#!/usr/bin/env python3
"""Whole-current-candidate JVM build mirrors, private to one resource lease.

Layout: CACHE/{developer,grader}/{mirror,inputs.json,pending.json,staging}, plus
CACHE/build.lock. Metadata never occupies a candidate pathname. Within a mode,
only previously enumerated inputs are removed; unenumerated compiler outputs
survive. A mode transition discards the *other* mode, including its outputs, so
verification cannot leave private grader files for a subsequent developer run.
Do not share CACHE between tasks/leases or mount it as a reference build seed.

Every input is copied, including hidden files, helpers, build scripts, empty
directories and relative links. Content, not source timestamps, determines
updates. Files are independent copies, not hard links. Directory permissions
are private writable 0755; ordinary file permission bits are preserved.

SCM policy: a directory-format, self-contained workspace .git is mirrored as
an exclusive input namespace (also for nested .git directories). Its HEAD,
index, config, objects and refs come ONLY from this invocation's workspace.
Old mirror-only SCM files are removed, including refs/objects made by a build.
No init/add/commit/checkout/fetch is performed and no outputs enter the index.
Gitfiles, SCM symlinks, alternates, worktrees, grafts/replacements and external
config indirection are rejected. An absent .git stays absent. There is no
attempt to reconstruct original repository history/tags from an engine snapshot.

Use candidate_session() to keep synchronization, a foreground build AND report
reading under one lock. synchronize_candidate() alone releases the lock on
return; use run_candidate() or the CLI for an atomic sync+execution operation.
Commands always execute; this helper never caches exit codes or test results.
The caller owns test forcing/cache policy and removal/reading of original JUnit
reports inside the session. Commands must use the mirror cwd/SOURCE_ROOT rather
than hard-coded /workspace paths. No source/test/build configuration is patched.

Linux/POSIX, local filesystem, cooperative foreground writers. The lease's
container/resource/network boundary remains the executor's responsibility.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import signal
import stat
import subprocess
import sys
import uuid

DEFAULT_CACHE = Path("/opt/reviewbench-cache/jvm-build")
MODES = ("developer", "grader")
SCHEMA = "reviewbench-jvm-build-inputs/v1"
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_MISSING = {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}
_MODE_ENTRIES = {"mirror", "inputs.json", "pending.json", "staging"}


class BuildMirrorError(ValueError):
    """An invalid input/cache is not a successful build."""


def _relative(value):
    if (not isinstance(value, str) or not value or "\0" in value or "\\" in value
            or value.startswith("/") or any(p in {"", ".", ".."} for p in value.split("/"))):
        raise BuildMirrorError("non-canonical relative input path")
    return value


def _absolute(value):
    path = Path(os.path.abspath(os.fspath(value)))
    if "\0" in str(path):
        raise BuildMirrorError("invalid tree path")
    return path


@contextmanager
def _directory(path, *, create=False):
    """Open every component without following even an ancestor symlink."""
    fd = os.open("/", _DIR)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, _DIR, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _parent(root, name):
    parts = _relative(name).split("/")
    fd = os.dup(root)
    try:
        for part in parts[:-1]:
            child = os.open(part, _DIR, dir_fd=fd)
            if os.fstat(child).st_dev != os.fstat(root).st_dev:
                os.close(child)
                raise BuildMirrorError("path crosses a filesystem boundary")
            os.close(fd)
            fd = child
        yield fd, parts[-1]
    finally:
        os.close(fd)


def _stat(root, name):
    try:
        with _parent(root, name) as (parent, leaf):
            return os.stat(leaf, dir_fd=parent, follow_symlinks=False)
    except OSError as exc:
        if exc.errno in _MISSING:
            return None
        raise


@contextmanager
def _regular(root, name, *, private=False):
    with _parent(root, name) as (parent, leaf):
        fd = os.open(leaf, _READ, dir_fd=parent)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_dev != os.fstat(root).st_dev
                or (private and info.st_nlink != 1)):
            raise BuildMirrorError("non-regular or hard-linked cache file: " + name)
        with os.fdopen(fd, "rb", closefd=False) as stream:
            yield stream
    finally:
        os.close(fd)


def _digest(stream):
    digest = hashlib.sha256()
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _inventory(root, *, candidate):
    result = {}
    device = os.fstat(root).st_dev

    def walk(fd, prefix):
        for leaf in sorted(os.listdir(fd)):
            name = _relative(prefix + leaf)
            info = os.stat(leaf, dir_fd=fd, follow_symlinks=False)
            if info.st_dev != device:
                raise BuildMirrorError("input/cache crosses a filesystem boundary: " + name)
            if stat.S_ISLNK(info.st_mode):
                result[name] = {"kind": "link", "target": os.readlink(leaf, dir_fd=fd)}
            elif stat.S_ISDIR(info.st_mode):
                result[name] = {"kind": "dir"}
                child = os.open(leaf, _DIR, dir_fd=fd)
                try:
                    walk(child, name + "/")
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                if info.st_mode & 0o7000 or (not candidate and info.st_nlink != 1):
                    raise BuildMirrorError("special permissions or hard-linked cache input: " + name)
                record = {"kind": "file", "mode": stat.S_IMODE(info.st_mode), "size": info.st_size}
                if candidate:
                    with _regular(root, name) as stream:
                        before = os.fstat(stream.fileno())
                        record["sha256"] = _digest(stream)
                        after = os.fstat(stream.fileno())
                    if ((before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                            != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                            or (before.st_ino, before.st_size, before.st_mtime_ns,
                                before.st_ctime_ns, before.st_mode)
                            != (info.st_ino, info.st_size, info.st_mtime_ns,
                                info.st_ctime_ns, info.st_mode)):
                        raise BuildMirrorError("candidate changed during enumeration: " + name)
                result[name] = record
            else:
                raise BuildMirrorError("special file in candidate/cache: " + name)

    walk(root, "")
    _validate_links(result)
    return result


def _validate_links(entries):
    """Resolve components in order: normpath alone misses link/../ escapes."""
    for name, entry in entries.items():
        if entry["kind"] != "link":
            continue
        target = entry["target"]
        if not target or target.startswith("/") or "\0" in target or "\\" in target:
            raise BuildMirrorError("absolute/invalid symbolic link: " + name)
        todo = name.split("/")[:-1] + target.split("/")
        resolved, follows = [], 0
        while todo:
            part, todo = todo[0], todo[1:]
            if part in {"", "."}:
                continue
            if part == "..":
                if not resolved:
                    raise BuildMirrorError("symbolic link escapes mirror: " + name)
                resolved.pop()
                continue
            resolved.append(part)
            link = entries.get("/".join(resolved), {})
            if link.get("kind") == "link":
                follows += 1
                if follows > 40:
                    raise BuildMirrorError("cyclic/overlong symbolic link: " + name)
                value = link["target"]
                if not value or value.startswith("/") or "\0" in value or "\\" in value:
                    raise BuildMirrorError("absolute/invalid symbolic link: " + name)
                resolved.pop()
                todo = value.split("/") + todo


def _git_roots(entries):
    return {name for name in entries if PurePosixPath(name).name == ".git"}


def _git_environment(mirror):
    # Neither inherited Git path/config variables nor parent discovery may
    # redirect SCM to the original workspace or the host repository.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null",
               GIT_CONFIG_SYSTEM="/dev/null", GIT_CEILING_DIRECTORIES=str(mirror.parent))
    return env


def _validate_scm(root, entries):
    for scm in _git_roots(entries):
        if entries[scm]["kind"] != "dir":
            raise BuildMirrorError("SCM requires a self-contained .git directory: " + scm)
        for name, entry in entries.items():
            if not name.startswith(scm + "/"):
                continue
            relative = name[len(scm) + 1:]
            if (entry["kind"] == "link" or relative in {
                    "commondir", "gitdir", "objects/info/alternates", "objects/info/http-alternates",
                    "info/grafts", "config.worktree", "worktrees", "modules", "refs/replace"}
                    or relative.startswith(("worktrees/", "modules/", "refs/replace/"))):
                raise BuildMirrorError("SCM indirection is not a self-contained snapshot: " + name)
        config = scm + "/config"
        if config not in entries:
            continue
        if entries[config]["kind"] != "file":
            raise BuildMirrorError("SCM config must be a regular file")
        with _regular(root, config) as stream:
            result = subprocess.run(
                ["git", "config", "--no-includes", "--null", "--list", "--file", "-"],
                input=stream.read(), capture_output=True, env=_git_environment(Path("/")),
            )
        if result.returncode:
            raise BuildMirrorError("invalid SCM config")
        for item in result.stdout.split(b"\0"):
            key = item.split(b"\n", 1)[0].decode("utf-8", errors="replace").lower()
            if (key.startswith(("include.", "includeif."))
                    or key in {"core.worktree", "core.hookspath", "core.fsmonitor",
                               "core.alternaterefscommand", "extensions.worktreeconfig"}):
                raise BuildMirrorError("SCM config redirects outside the snapshot: " + key)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BuildMirrorError("duplicate cache metadata key")
        result[key] = value
    return result


def _manifest(root, name, mode):
    if _stat(root, name) is None:
        return None
    with _regular(root, name, private=True) as stream:
        raw = stream.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024:
        raise BuildMirrorError("oversized input manifest")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError) as exc:
        raise BuildMirrorError("invalid input manifest") from exc
    if (not isinstance(value, dict) or set(value) != {"schema", "mode", "inputs"}
            or value["schema"] != SCHEMA or value["mode"] != mode
            or not isinstance(value["inputs"], list)):
        raise BuildMirrorError("incompatible input manifest")
    names = [_relative(name) for name in value["inputs"]]
    if names != sorted(set(names)):
        raise BuildMirrorError("input manifest must have sorted unique paths")
    return set(names)


def _erase(root, name, *, recursive=False):
    """Unlink anchored names; never traverse a stale input's symlink parent."""
    try:
        with _parent(root, name) as (parent, leaf):
            info = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                os.unlink(leaf, dir_fd=parent)
                return
            if recursive:
                fd = os.open(leaf, _DIR, dir_fd=parent)
                try:
                    if os.fstat(fd).st_dev != os.fstat(root).st_dev:
                        raise BuildMirrorError("cache removal crosses a filesystem boundary")
                    os.fchmod(fd, stat.S_IMODE(info.st_mode) | 0o700)
                    for child in os.listdir(fd):
                        _erase(fd, child, recursive=True)
                finally:
                    os.close(fd)
            try:
                os.rmdir(leaf, dir_fd=parent)
            except OSError as exc:
                if recursive or exc.errno not in {errno.ENOTEMPTY, errno.EEXIST}:
                    raise
    except OSError as exc:
        if exc.errno not in _MISSING:
            raise


def _mkdir(root, name):
    with _parent(root, name) as (parent, leaf):
        try:
            os.mkdir(leaf, 0o755, dir_fd=parent)
        except FileExistsError:
            pass
        fd = os.open(leaf, _DIR, dir_fd=parent)
    if os.fstat(fd).st_dev != os.fstat(root).st_dev:
        os.close(fd)
        raise BuildMirrorError("cache directory crosses a filesystem boundary")
    os.fchmod(fd, 0o755)
    return fd


def _write_manifest(mode_fd, stage, name, mode, inputs):
    raw = (json.dumps({"schema": SCHEMA, "mode": mode, "inputs": sorted(inputs)},
                      sort_keys=True) + "\n").encode()
    temporary = uuid.uuid4().hex
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=stage)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=stage, dst_dir_fd=mode_fd)
        os.fsync(mode_fd)
    finally:
        _erase(stage, temporary)


def _install_file(source, mirror, stage, name, record):
    existing = _stat(mirror, name)
    if existing and stat.S_ISREG(existing.st_mode):
        with _regular(mirror, name, private=True) as stream:
            if (existing.st_size == record["size"]
                    and stat.S_IMODE(existing.st_mode) == record["mode"]
                    and _digest(stream) == record["sha256"]):
                return
    temporary = uuid.uuid4().hex
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=stage)
    try:
        digest = hashlib.sha256()
        with os.fdopen(fd, "wb") as output, _regular(source, name) as original:
            while chunk := original.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
            if digest.hexdigest() != record["sha256"]:
                raise BuildMirrorError("candidate changed during synchronization: " + name)
            output.flush()
            os.fchmod(output.fileno(), record["mode"])
            # Even equal-size edits carrying the old source mtime must invalidate
            # a compiler's stat shortcut. Unchanged destination files are untouched.
            now = os.fstat(output.fileno()).st_mtime_ns
            if existing and now == existing.st_mtime_ns:
                os.utime(output.fileno(), ns=(now, now + 1))
        with _parent(mirror, name) as (parent, leaf):
            os.replace(temporary, leaf, src_dir_fd=stage, dst_dir_fd=parent)
    finally:
        _erase(stage, temporary)


def _check_mode(mode_fd, mode):
    if set(os.listdir(mode_fd)) - _MODE_ENTRIES:
        raise BuildMirrorError("cache metadata namespace collision")
    previous = _manifest(mode_fd, "inputs.json", mode)
    pending = _manifest(mode_fd, "pending.json", mode)
    for name in ("mirror", "staging"):
        info = _stat(mode_fd, name)
        if info and not stat.S_ISDIR(info.st_mode):
            raise BuildMirrorError("cache directory is not an ordinary directory: " + name)
    if _stat(mode_fd, "mirror"):
        fd = os.open("mirror", _DIR, dir_fd=mode_fd)
        try:
            if previous is None and pending is None and os.listdir(fd):
                raise BuildMirrorError("nonempty mirror has no previous input manifest")
        finally:
            os.close(fd)
    return (previous or set()) | (pending or set())


def _synchronize(source, mode_fd, mode, current):
    previous = _check_mode(mode_fd, mode)
    mirror, stage = _mkdir(mode_fd, "mirror"), None
    try:
        stage = _mkdir(mode_fd, "staging")
        if os.fstat(mirror).st_dev != os.fstat(stage).st_dev:
            raise BuildMirrorError("mirror and metadata must share a filesystem")
        # Only helper-owned staging files can survive an interrupted transaction.
        for name in os.listdir(stage):
            if len(name) != 32 or any(c not in "0123456789abcdef" for c in name):
                raise BuildMirrorError("cache staging namespace collision")
            _erase(stage, name, recursive=True)
        existing = _inventory(mirror, candidate=False)
        scm_roots = _git_roots(existing) | _git_roots(current)
        scm_entries = {name for name in existing
                       if any(name == scm or name.startswith(scm + "/") for scm in scm_roots)}
        stale = (previous | scm_entries) - current.keys()
        # Preflight the resulting link graph, including surviving generated files.
        collisions = {name for name, entry in current.items() if name in existing
                      and existing[name]["kind"] == "dir" and entry["kind"] != "dir"}
        combined = {name: entry for name, entry in existing.items()
                    if name not in stale and not any(str(p) in collisions
                                                     for p in PurePosixPath(name).parents)}
        combined.update(current)
        _validate_links(combined)
        # A journal of all potentially installed inputs prevents a failed sync
        # from later adopting half-copied sources as compiler outputs.
        _write_manifest(mode_fd, stage, "pending.json", mode, previous | current.keys())
        for name in sorted(stale, key=lambda n: (n.count("/"), n), reverse=True):
            _erase(mirror, name)
        for name in sorted(current, key=lambda n: (n.count("/"), n)):
            record, info = current[name], _stat(mirror, name)
            kind = ("dir" if info and stat.S_ISDIR(info.st_mode) else
                    "link" if info and stat.S_ISLNK(info.st_mode) else "file" if info else None)
            if kind is not None and kind != record["kind"]:
                _erase(mirror, name, recursive=True)
            if record["kind"] == "dir":
                fd = _mkdir(mirror, name)
                os.close(fd)
            elif record["kind"] == "file":
                _install_file(source, mirror, stage, name, record)
            else:
                with _parent(mirror, name) as (parent, leaf):
                    if kind == "link" and os.readlink(leaf, dir_fd=parent) == record["target"]:
                        continue
                    temporary = uuid.uuid4().hex
                    try:
                        os.symlink(record["target"], temporary, dir_fd=stage)
                        os.replace(temporary, leaf, src_dir_fd=stage, dst_dir_fd=parent)
                    finally:
                        _erase(stage, temporary)
        _write_manifest(mode_fd, stage, "inputs.json", mode, current)
        _erase(mode_fd, "pending.json")
    finally:
        if stage is not None:
            os.close(stage)
        os.close(mirror)


@contextmanager
def candidate_session(workspace, cache=DEFAULT_CACHE, *, mode="developer"):
    """Yield the stable mirror while holding the cache-wide sync/build lock.

    Read original JUnit XML before leaving this context when other invocations
    may run concurrently. Independent leases must supply independent caches.
    """
    if mode not in MODES:
        raise BuildMirrorError("mode must be developer or grader")
    workspace, cache = _absolute(workspace), _absolute(cache)
    if workspace == cache or workspace in cache.parents or cache in workspace.parents:
        raise BuildMirrorError("workspace and cache must be disjoint trees")
    with _directory(workspace) as source, _directory(cache, create=True) as root:
        lock = os.open("build.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                       0o600, dir_fd=root)
        try:
            info = os.fstat(lock)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_dev != os.fstat(root).st_dev):
                raise BuildMirrorError("cache lock must be an independent regular file")
            fcntl.flock(lock, fcntl.LOCK_EX)
            if set(os.listdir(root)) - {"build.lock", *MODES}:
                raise BuildMirrorError("cache root metadata namespace collision")
            current = _inventory(source, candidate=True)
            _validate_scm(source, current)
            # Separate paths alone do not hide inactive grader data in the same
            # lease mount. Mode transitions invalidate the *inactive* namespace.
            for other in MODES:
                if other == mode or _stat(root, other) is None:
                    continue
                other_fd = os.open(other, _DIR, dir_fd=root)
                try:
                    if os.fstat(other_fd).st_dev != os.fstat(root).st_dev:
                        raise BuildMirrorError("inactive mode crosses a filesystem boundary")
                    _check_mode(other_fd, other)
                    if _stat(other_fd, "mirror"):
                        fd = os.open("mirror", _DIR, dir_fd=other_fd)
                        try:
                            _inventory(fd, candidate=False)
                        finally:
                            os.close(fd)
                finally:
                    os.close(other_fd)
                _erase(root, other, recursive=True)
            mode_fd = _mkdir(root, mode)
            try:
                _synchronize(source, mode_fd, mode, current)
                yield cache / mode / "mirror"
            finally:
                os.close(mode_fd)
        finally:
            os.close(lock)


def synchronize_candidate(workspace, cache=DEFAULT_CACHE, *, mode="developer"):
    """Synchronize once, returning a Path; no build or result is synthesized."""
    with candidate_session(workspace, cache, mode=mode) as mirror:
        return mirror


def execution_environment(mirror, *, mode, cwd="."):
    if mode not in MODES:
        raise BuildMirrorError("mode must be developer or grader")
    if cwd != ".":
        _relative(cwd)
    workdir = mirror if cwd == "." else mirror / cwd
    env = _git_environment(mirror)
    env.update(SOURCE_ROOT=str(mirror), REVIEWBENCH_JVM_BUILD_MIRROR=str(mirror),
               REVIEWBENCH_JVM_BUILD_MODE=mode, PWD=str(workdir))
    return env


def run_candidate(workspace, cache=DEFAULT_CACHE, *, mode="developer", command, cwd="."):
    """Always execute argv, returning (mirror Path, literal subprocess status)."""
    if (isinstance(command, (str, bytes)) or not command
            or any(not isinstance(item, str) or "\0" in item for item in command)):
        raise BuildMirrorError("command must be a nonempty argv sequence")
    if cwd != ".":
        _relative(cwd)
    with candidate_session(workspace, cache, mode=mode) as mirror:
        workdir = mirror if cwd == "." else mirror / cwd
        env = execution_environment(mirror, mode=mode, cwd=cwd)
        print("JVM build mirror: " + json.dumps({"mirror": str(mirror), "mode": mode}),
              file=sys.stderr, flush=True)
        # A /proc fd cwd prevents a late symlink substitution from redirecting
        # the child. Keep the descriptor valid through exec on Linux.
        with _directory(workdir) as fd:
            process = subprocess.Popen(command, cwd=f"/proc/self/fd/{fd}", env=env,
                                       pass_fds=(fd,), start_new_session=True)
            try:
                return mirror, process.wait()
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path("/workspace"))
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--mode", choices=MODES, required=True)
    parser.add_argument("--cwd", default=".")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--sync-only", action="store_true", help="print mirror path; release lock")
    group.add_argument("--command", nargs=argparse.REMAINDER, help="foreground argv (no implicit shell)")
    args = parser.parse_args(argv)
    # Clean up the child before releasing the lock on ordinary CLI cancellation.
    def terminated(signum, frame):
        raise SystemExit(128 + signum)
    old = signal.signal(signal.SIGTERM, terminated)
    try:
        if args.sync_only:
            print(synchronize_candidate(args.workspace, args.cache, mode=args.mode))
            return 0
        _, status = run_candidate(args.workspace, args.cache, mode=args.mode,
                                  command=args.command, cwd=args.cwd)
        return status if status >= 0 else 128 - status
    except KeyboardInterrupt:
        return 130
    except (BuildMirrorError, OSError) as exc:
        print("JVM build mirror error: " + str(exc), file=sys.stderr)
        return 2
    finally:
        signal.signal(signal.SIGTERM, old)


if __name__ == "__main__":
    raise SystemExit(main())
