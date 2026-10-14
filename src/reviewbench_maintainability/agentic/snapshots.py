"""Exact-start snapshots and host-owned, multi-file patch collection.

Neither the shared repository's history nor its Git metadata enter a sandbox.
The solver's index, commits and replacement .gitignore are not authoritative.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading

from .contracts import ContractError, canonical_hash, file_hash, repo_path
from .resources import CURRENT_JOB, job_directory


def git(root: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={root.resolve()}", "-c", "gc.auto=0",
         "-c", "maintenance.auto=false", "-C", str(root), *args],
        input=input_bytes, capture_output=True, timeout=300,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
             "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode:
        raise ContractError(f"git {args[0]} failed: {result.stderr.decode(errors='replace')[-1200:]}")
    return result.stdout


def validate_link(path: str, target: str) -> None:
    if os.path.isabs(target):
        raise ContractError(f"absolute symlink: {path}")
    resolved = os.path.normpath(os.path.join(os.path.dirname(path), target))
    repo_path(resolved, allow_root=True)


def tree_files(root: Path) -> dict[str, Path]:
    result = {}
    job = CURRENT_JOB.get()
    excluded = job.get("workspace_exclusions", {}).get(str(root.resolve()), []) if job else []
    def omitted(name):
        # Keep the original iterable/error behavior for non-list metadata.
        if type(excluded) is list and not excluded:
            return False
        return any(name == prefix or name.startswith(prefix + "/") for prefix in excluded)
    for directory, dirs, files in os.walk(root, followlinks=False):
        # os.walk supplies basenames; normalize their parent only once.
        directory_path = Path(directory)
        relative_directory = directory_path.relative_to(root).as_posix()
        directory_prefix = "" if relative_directory == "." else relative_directory + "/"
        dirs[:] = sorted(d for d in dirs if d not in {".git", ".reviewbench"}
                         and not omitted(directory_prefix + d))
        for name in sorted(files + [d for d in dirs if (directory_path / d).is_symlink()]):
            relative = directory_prefix + name
            if omitted(relative):
                continue
            path = directory_path / name
            repo_path(relative)
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                validate_link(relative, os.readlink(path))
            elif not stat.S_ISREG(mode):
                raise ContractError(f"non-regular repository object: {relative}")
            result[relative] = path
    return result


def _tree_hash_with_paths(root: Path) -> str:
    """Preserve the original path/platform and unusual exclusion inputs."""
    return canonical_hash([
        (name, os.readlink(p) if p.is_symlink() else file_hash(p),
         "symlink" if p.is_symlink() else bool(p.stat().st_mode & 0o111))
        for name, p in sorted(tree_files(root).items())
    ])


def _tree_hash_open_directory(root_fd: int, relative: str) -> int:
    """Open one descendant without following any descendant directory links.

    Only the root and at most two working directory descriptors are live here;
    no descriptor cache grows with tree width or depth.
    """
    current = os.dup(root_fd)
    try:
        if relative:
            for part in relative.split("/"):
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=current,
                )
                os.close(current)
                current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _tree_hash_entries(root_fd: int, excluded: list[str] | tuple[str, ...]) -> list[tuple[str, int]]:
    """Validate before reading contents, in tree_files' directory/file order.

    DirEntry metadata lives for one directory only. The retained records contain
    just a relative name and mode, not Path objects or file bytes. Like os.walk,
    failed directory scans are omitted, and directory links are never traversed.
    """
    result = []
    pending = [""]

    def omitted(name):
        return any(name == prefix or name.startswith(prefix + "/") for prefix in excluded)

    while pending:
        directory = pending.pop()
        try:
            directory_fd = _tree_hash_open_directory(root_fd, directory)
        except OSError:
            continue
        try:
            try:
                with os.scandir(directory_fd) as scan:
                    entries = list(scan)
            except OSError:
                continue
            prefix = directory + "/" if directory else ""
            directories, files = [], []
            for entry in entries:
                try:
                    is_directory = entry.is_dir()
                except OSError:
                    is_directory = False
                if is_directory:
                    # Follow for classification only: a .git link to a directory
                    # is omitted by tree_files; a .git file/dangling link is not.
                    if entry.name in {".git", ".reviewbench"}:
                        continue
                    if excluded and omitted(prefix + entry.name):
                        continue
                    directories.append(entry)
                else:
                    files.append(entry)
            descend = []
            for entry in sorted(directories, key=lambda entry: entry.name):
                if entry.is_symlink():
                    files.append(entry)
                else:
                    descend.append(prefix + entry.name)
            for entry in sorted(files, key=lambda entry: entry.name):
                relative = prefix + entry.name
                if excluded and omitted(relative):
                    continue
                repo_path(relative)
                mode = entry.stat(follow_symlinks=False).st_mode
                if stat.S_ISLNK(mode):
                    validate_link(relative, os.readlink(entry.name, dir_fd=directory_fd))
                elif not stat.S_ISREG(mode):
                    raise ContractError(f"non-regular repository object: {relative}")
                result.append((relative, mode))
            pending.extend(reversed(descend))
        finally:
            os.close(directory_fd)
    return result


def _tree_hash_regular(directory_fd: int, name: str, relative: str) -> tuple[str, bool]:
    """Read every byte on every call; fstat binds mode/type to the opened file."""
    descriptor = os.open(
        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd,
    )
    try:
        mode = os.fstat(descriptor).st_mode
        if not stat.S_ISREG(mode):
            raise ContractError(f"non-regular repository object: {relative}")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest(), bool(mode & 0o111)
    finally:
        os.close(descriptor)


def tree_hash(root: Path) -> str:
    # Keep the established behavior for string/bytes/custom Path inputs and
    # platforms without descriptor-relative, no-follow traversal.
    if (type(root) is not type(Path()) or os.name != "posix"
            or os.open not in os.supports_dir_fd or os.readlink not in os.supports_dir_fd
            or os.scandir not in os.supports_fd
            or not all(hasattr(os, flag) for flag in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK"))):
        return _tree_hash_with_paths(root)
    job = CURRENT_JOB.get()
    excluded = job.get("workspace_exclusions", {}).get(str(root.resolve()), []) if job else []
    if (type(excluded) not in (list, tuple)
            or any(type(prefix) is not str for prefix in excluded)):
        return _tree_hash_with_paths(root)
    # os.walk follows a symlink supplied as the root, but not descendant links.
    # Preserve that distinction and its empty result on an unscannable root.
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return canonical_hash([])
    directory_fd = None
    try:
        entries = _tree_hash_entries(root_fd, excluded)
        entries.sort()
        rows = []
        previous_directory = None
        for relative, mode in entries:
            directory, _, name = relative.rpartition("/")
            if directory != previous_directory:
                if directory_fd is not None:
                    os.close(directory_fd)
                    directory_fd = None
                directory_fd = _tree_hash_open_directory(root_fd, directory)
                previous_directory = directory
            if stat.S_ISLNK(mode):
                target = os.readlink(name, dir_fd=directory_fd)
                validate_link(relative, target)
                rows.append((relative, target, "symlink"))
            else:
                content_hash, executable = _tree_hash_regular(directory_fd, name, relative)
                rows.append((relative, content_hash, executable))
        return canonical_hash(rows)
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
        os.close(root_fd)


def gitlinks(repository: Path, start_sha: str) -> list[dict]:
    entries = git(repository, "ls-tree", "-r", "-z", start_sha).split(b"\0")
    return [{"path": repo_path(value.split(b"\t", 1)[1].decode()),
             "commit": value.split(b"\t", 1)[0].split()[2].decode()}
            for value in entries if value.startswith(b"160000 ")]


@contextmanager
def start_archive(repository: Path, start_sha: str, *, timeout: float = 3600):
    """Stream exact Git files without loading a multi-GB tar in controller RAM.

    A private attributes file disables export-ignore/export-subst: distribution
    archives can omit tests or expand placeholders, but task snapshots must
    preserve every byte at start. Original Git metadata is never modified.
    """
    objects = git(repository, "rev-parse", "--path-format=absolute", "--git-path", "objects").decode().strip()
    with job_directory(prefix="reviewbench-archive-git-") as directory:
        private = Path(directory)
        git(private, "init", "--bare", "--quiet")
        (private / "objects/info/alternates").write_text(objects + "\n")
        (private / "info/attributes").write_text("* -export-ignore -export-subst\n")
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(
                ["git", "-C", str(private), "archive", "--format=tar", start_sha],
                stdout=subprocess.PIPE, stderr=errors,
                env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                     "GIT_TERMINAL_PROMPT": "0"},
            )
            timer = threading.Timer(timeout, process.kill)
            timer.daemon = True
            timer.start()
            try:
                yield process.stdout
                process.stdout.close()
                code = process.wait(timeout=30)
                if code:
                    errors.seek(0)
                    raise ContractError(f"streamed git archive failed ({code}): "
                                        + errors.read(4096).decode(errors="replace"))
            finally:
                timer.cancel()
                process.stdout.close()
                if process.poll() is None:
                    process.kill()
                process.wait()


def extract_tar(stream, destination: Path) -> None:
    """Sequential extraction; archive-created links are never traversed."""
    if destination.exists() or destination.is_symlink():
        raise ContractError(f"snapshot destination already exists: {destination}")
    destination.mkdir(parents=True)
    try:
        with tarfile.open(fileobj=stream, mode="r|", bufsize=1024 * 1024) as tar:
            while member := tar.next():
                # TarFile otherwise retains all headers even in stream mode.
                tar.members.clear()
                name = repo_path(member.name.rstrip("/"))
                path = destination / name
                if any(parent.is_symlink() for parent in path.parents if parent != destination.parent):
                    raise ContractError(f"archive descends through symlink: {name}")
                if path.is_symlink() or (path.exists() and not (path.is_dir() and member.isdir())):
                    raise ContractError(f"duplicate archive entry: {name}")
                if member.isdir():
                    path.mkdir(parents=True, exist_ok=True)
                elif member.issym():
                    validate_link(name, member.linkname)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.symlink_to(member.linkname)
                elif member.isfile():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    data = tar.extractfile(member)
                    assert data is not None
                    with path.open("xb") as output:
                        shutil.copyfileobj(data, output, length=1024 * 1024)
                    path.chmod(0o755 if member.mode & 0o111 else 0o644)
                else:
                    raise ContractError(f"unsupported archive entry: {name}")
    except BaseException:
        shutil.rmtree(destination)
        raise


def extract_start(repository: Path, start_sha: str, destination: Path) -> str:
    if destination.exists() or destination.is_symlink():
        raise ContractError(f"snapshot destination already exists: {destination}")
    job = CURRENT_JOB.get()
    if job and job.get("source_catalog"):
        task = job["task"]
        if task["start_sha"] != start_sha:
            raise ContractError("requested source differs from admitted task")
        return job["source_catalog"].extract(
            task["repo"], start_sha, destination, submodules=job.get("submodules"),
            asset_pool=job.get("asset_pool"),
        )
    if gitlinks(repository, start_sha):
        raise ContractError("snapshot needs explicit pinned submodule assets; shared history is not mounted")
    try:
        with start_archive(repository, start_sha) as stream:
            extract_tar(stream, destination)
    except BaseException:
        if destination.is_dir() and not destination.is_symlink():
            shutil.rmtree(destination)
        raise
    return tree_hash(destination)


def initialize_git(root: Path, *, byte_exact: bool = False) -> None:
    git(root, "init", "--quiet")
    git(root, "config", "user.email", "reviewbench@example.invalid")
    git(root, "config", "user.name", "ReviewBench")
    git(root, "config", "core.autocrlf", "false")
    if byte_exact:
        # Host serialization/replay represents actual filesystem bytes, not
        # Git's clean-filter/EOL/encoding view of those bytes. Keep ordinary
        # repository attributes in the developer's Git, but disable conversion
        # in the private collector and independent patch-application index.
        (root / ".git/info/attributes").write_text(
            "* -text -eol -working-tree-encoding -ident -filter\n")
    # Large real repositories otherwise start background auto-GC during commit,
    # which races temporary-directory teardown and outlives the episode.
    git(root, "config", "gc.auto", "0")
    git(root, "config", "maintenance.auto", "false")
    git(root, "add", "--all", "--force", "--", ".")
    git(root, "commit", "--quiet", "--allow-empty", "-m", "isolated task start")


def copy_source(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for name, path in tree_files(source).items():
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            destination.symlink_to(os.readlink(path))
        else:
            shutil.copy2(path, destination)


def restore_immutable_gitlink(base: Path, work: Path, name: str, entry: dict, job: dict) -> None:
    """Restore the declared source-catalog state, including intentional omission."""
    catalog, task = job.get("source_catalog"), job.get("task")
    if not catalog or not task:
        raise ContractError("immutable gitlink needs its admitted source catalog")
    source_record = catalog.source(task["repo"], task["start_sha"])
    pins = [link for link in source_record.get("gitlinks", []) if link["path"] == name]
    if len(pins) != 1 or entry.get("commit") != pins[0]["commit"]:
        raise ContractError("immutable gitlink policy differs from the exact Git pin")
    source, destination = base / name, work / name
    if source.is_symlink() or (source.exists() and not source.is_dir()):
        raise ContractError("immutable gitlink start is not a directory or omitted path")
    omitted = entry.get("omit") is True
    if omitted:
        if not entry.get("reason") or (source.is_dir() and any(source.iterdir())):
            raise ContractError("omitted immutable gitlink contains unexpected source")
    else:
        if entry.get("omit") or not entry.get("asset") or not source.is_dir():
            raise ContractError("required immutable gitlink source asset is missing")
        tree_files(source)  # Validate regular entries and link boundaries first.
    if destination.is_symlink():
        destination.unlink()
    elif destination.is_dir():
        shutil.rmtree(destination)
    elif destination.exists():
        destination.unlink()
    if omitted:
        if source.is_dir():
            destination.mkdir(parents=True, exist_ok=False)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination, symlinks=True,
                        ignore=shutil.ignore_patterns(".git", ".reviewbench"))


def restore_immutable(base: Path, work: Path, paths: list[str]) -> None:
    job = CURRENT_JOB.get() or {}
    modules = job.get("submodules", [])
    declared = {entry["path"]: entry for entry in modules}
    if len(declared) != len(modules):
        raise ContractError("duplicate immutable submodule policy")
    for name in paths:
        repo_path(name)
        source = base / name
        destination = work / name
        # A patch may replace a directory by a symlink. Do not follow it while
        # restoring frozen tests or while copying their parent directories.
        if any(p.is_symlink() for p in destination.parents if p != work.parent):
            raise ContractError(f"immutable file parent became a symlink: {name}")
        if name in declared:
            restore_immutable_gitlink(base, work, name, declared[name], job)
            continue
        if not source.is_file() and not source.is_symlink():
            raise ContractError(f"immutable test missing at start: {name}")
        if destination.is_dir() and not destination.is_symlink():
            shutil.rmtree(destination)
        if destination.is_symlink():
            destination.unlink()
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            target = os.readlink(source)
            validate_link(name, target)
            if destination.exists():
                destination.unlink()
            # Preserve the link itself, including intentionally dangling test
            # fixtures. Never copy the bytes of its production-file target.
            destination.symlink_to(target)
        else:
            shutil.copy2(source, destination)


def collect_patch(base: Path, workspace: Path, output: Path) -> dict:
    final_files = tree_files(workspace)
    base_files = tree_files(base)
    with job_directory(prefix="reviewbench-patch-") as directory:
        trusted = Path(directory)
        copy_source(base, trusted)
        initialize_git(trusted, byte_exact=True)
        new_names = sorted(set(final_files) - set(base_files))
        ignored = set()
        if new_names:
            result = subprocess.run(
                ["git", "-C", str(trusted), "check-ignore", "--no-index", "-z", "--stdin"],
                input=b"".join(n.encode() + b"\0" for n in new_names),
                capture_output=True, timeout=60,
            )
            if result.returncode not in {0, 1}:
                raise ContractError("could not evaluate original ignore rules")
            ignored = {p.decode() for p in result.stdout.split(b"\0") if p}
        # Remove source tree only from this private temporary Git repository.
        for child in trusted.iterdir():
            if child.name == ".git":
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        for name, source in final_files.items():
            if name in ignored:
                continue
            target = trusted / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_symlink():
                target.symlink_to(os.readlink(source))
            else:
                shutil.copy2(source, target)
        # Copying back a same-size edit can reuse an inode and preserve the
        # timestamp granularity Git recorded at the baseline commit. Rebuild
        # the private index from bytes; never trust that stat-cache coincidence.
        git(trusted, "read-tree", "--empty")
        git(trusted, "add", "--all", "--force", "--", ".")
        patch = git(trusted, "diff", "--cached", "--binary", "--full-index", "--no-ext-diff", "HEAD")
        names = [n.decode() for n in git(
            trusted, "diff", "--cached", "--name-only", "--no-renames", "-z", "HEAD"
        ).split(b"\0") if n]
        stats = git(trusted, "diff", "--cached", "--numstat", "--no-renames", "HEAD")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(patch)
    # Validate against a new pristine copy, not the solver's index/HEAD.
    with job_directory(prefix="reviewbench-apply-") as directory:
        check = Path(directory)
        copy_source(base, check)
        if patch:
            git(check, "apply", "--check", str(output.resolve()))
    changed_lines = 0
    for line in stats.splitlines():
        a, d, _ = line.split(b"\t", 2)
        if a.isdigit() and d.isdigit():
            changed_lines += int(a) + int(d)
    return {
        "patch_file": str(output.resolve()),
        "patch_sha256": file_hash(output),
        "changed_files": names,
        "changed_lines": changed_lines,
        "apply_ok": bool(patch),
    }


def apply_patch(base: Path, patch: Path, destination: Path, *, ignore_space_change: bool = False) -> list[str]:
    copy_source(base, destination)
    initialize_git(destination, byte_exact=True)
    extra = ["--ignore-space-change"] if ignore_space_change else []
    git(destination, "apply", "--check", "--index", *extra, str(patch.resolve()))
    git(destination, "apply", "--index", *extra, str(patch.resolve()))
    names = [n.decode() for n in git(
        destination, "diff", "--cached", "--name-only", "--no-renames", "-z", "HEAD"
    ).split(b"\0") if n]
    for name in names:
        repo_path(name)
    tree_files(destination)  # includes checks on newly added symlinks/special files
    if not names:
        raise ContractError("empty candidate patch")
    return names
