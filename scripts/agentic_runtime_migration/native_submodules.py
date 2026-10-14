"""Bounded, source-only closure of literal native submodule preparation steps.

Only Git objects at the superproject's recorded pins are read. Checkouts,
export-ignore, branch tips, compiled caches, and arbitrary submodule shell
commands are not dependency inputs.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import re
import shlex
import tempfile

from reviewbench_maintainability.agentic.contracts import (
    ContractError, atomic_json, component, repo_path,
)
from reviewbench_maintainability.agentic.snapshots import (
    extract_tar, git, gitlinks, start_archive,
)
from reviewbench_maintainability.agentic.resources import job_directory


MAX_DEPTH = 8
MAX_MODULES = 64
MAX_FILES = 100_000
# Migration has a 1 GiB scratch reservation. Extracted sources plus the
# streaming archive fit below that reservation, including tar/file overhead.
MAX_INPUT_BYTES = 384 * 1024**2


@contextmanager
def object_view(repository):
    """Read alternate objects without inheriting remotes or replacement refs.

    Even `ls-tree -l` can write missing blobs into a promisor clone. A bare
    object-only view has no remote to lazy-fetch from; missing objects fail
    locally and the preparer may acquire them in a separate private repository.
    """
    objects = git(repository, "rev-parse", "--path-format=absolute", "--git-path", "objects").decode().strip()
    with job_directory(prefix="native-readonly-git-") as temporary:
        view = Path(temporary)
        git(view, "init", "--bare", "--quiet")
        (view / "objects/info/alternates").write_text(objects + "\n")
        yield view


def readonly_git(repository, *arguments):
    with object_view(repository) as view:
        return git(view, *arguments)


def readonly_links(repository, commit):
    with object_view(repository) as view:
        return gitlinks(view, commit)


@dataclass(frozen=True)
class Preparation:
    field: str
    prefix: str
    operation: str
    paths: tuple[str, ...] | None


PROFILES = {
    ("ad-freiburg/qlever", 30): Preparation(
        "configure_command", "set -e; ",
        "git submodule update --init --recursive --jobs 8", None),
    ("DLR-AMR/t8code", 6): Preparation(
        "configure_command",
        "set -e; repo=$PWD; dep='{cache}/deps'; printf '2.0.0\\n' > .tarball-version; ",
        "git submodule update --init sc p4est", ("sc", "p4est")),
    ("argotorg/solidity", 34): Preparation(
        "configure_command", "set -e; ",
        "git submodule update --init deps/nlohmann-json deps/range-v3 deps/fmtlib",
        ("deps/nlohmann-json", "deps/range-v3", "deps/fmtlib")),
    **{("google/bigwheels", index): Preparation(
        "build_command", "set -e; ",
        "git submodule update --init --depth 1 --jobs 1 "
        "third_party/imgui third_party/glm third_party/glfw third_party/pcg32 third_party/gli",
        ("third_party/imgui", "third_party/glm", "third_party/glfw",
         "third_party/pcg32", "third_party/gli")) for index in (62, 63)},
    **{("hyrise/hyrise", index): Preparation(
        "configure_command", "",
        "git submodule update --init --recursive --jobs 1 && "
        "git submodule foreach --recursive 'git reset --hard && git clean -ffd'",
        None) for index in (70, 71)},
    ("uliegecsm/kokkos-execution", 126): Preparation(
        "build_command", "set -e; ",
        "git submodule update --init external/stdexec", ("external/stdexec",)),
}


def selected_paths(task, plan, links):
    """Validate the complete, literal preparation prefix without executing it."""
    profile = PROFILES.get((task["repo"], plan["input_index"]))
    if profile is None:
        return None
    command = plan.get(profile.field, "")
    prefix = profile.prefix + profile.operation
    if not command.startswith(prefix) or not command[len(prefix):].startswith(("; ", " && ")):
        raise ContractError("native pinned-submodule preparation differs from the literal supported prefix")
    remainder = command[len(prefix):]
    other = plan.get("build_command" if profile.field == "configure_command" else "configure_command", "") or ""
    if "git submodule" in remainder + other:
        raise ContractError("native pinned-submodule preparation has an additional unsupported Git operation")
    pins = {item["path"]: item["commit"] for item in links}
    paths = tuple(pins) if profile.paths is None else profile.paths
    if not paths or not set(paths) <= pins.keys():
        raise ContractError("native required submodule is absent from the exact superproject Git tree")
    return paths


def module_declarations(repository, commit):
    """Use Git's own parser on the committed .gitmodules, never the checkout."""
    with object_view(repository) as view:
        entry = git(view, "ls-tree", commit, "--", ".gitmodules")
        if not entry.startswith((b"100644 blob ", b"100755 blob ")):
            raise ContractError(f"native pinned submodule declarations missing at {commit} in {repository}")
        raw = git(view, "config", "--blob", commit + ":.gitmodules", "--null", "--list")
    modules = {}
    for item in raw.split(b"\0"):
        if not item:
            continue
        key, separator, value = item.partition(b"\n")
        match = re.fullmatch(r"submodule\.(.+)\.(path|url)", key.decode())
        if not match:
            continue
        name, field = match.groups()
        repo_path(name)
        record = modules.setdefault(name, {})
        if not separator or field in record:
            raise ContractError("native .gitmodules contains a duplicate or empty source declaration")
        record[field] = value.decode()
    result = {}
    for name, record in modules.items():
        path = repo_path(record.get("path", ""))
        if path in result or not record.get("url"):
            raise ContractError("native .gitmodules paths must have unique public source URLs")
        result[path] = {"name": name, "url": record["url"]}
    return result


def check_repository(repository, commit):
    repository = Path(repository)
    if (not repository.is_absolute() or not repository.is_dir()
            or not ((repository / ".git").exists()
                    or ((repository / "HEAD").is_file() and (repository / "objects").is_dir()))):
        raise ContractError(f"native pinned source must be an absolute local Git repository: {repository}")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ContractError("native submodule pin must be a full Git commit ID")
    with object_view(repository) as view:
        if git(view, "cat-file", "-t", commit).strip() != b"commit":
            raise ContractError(f"native submodule pin is not a commit: {commit}")
        entries = [row for row in git(view, "ls-tree", "-r", "-z", commit).split(b"\0") if row]
        if len(entries) > MAX_FILES:
            raise ContractError("native pinned submodule source exceeds the file bound")
        blobs = {row.split(b"\t", 1)[0].split()[2] for row in entries
                 if row.split(b"\t", 1)[0].split()[1] == b"blob"}
        if blobs:
            checked = git(view, "cat-file", "--batch-check", input_bytes=b"\n".join(sorted(blobs)) + b"\n")
            records = [row.split() for row in checked.splitlines()]
            if (len(records) != len(blobs)
                    or any(len(row) != 3 or row[1] != b"blob" or not row[2].isdigit() for row in records)):
                raise ContractError(f"native source missing local blobs at {commit}; use a private pinned fetch")
    return repository


def local_repository(parent, declaration, relative, absolute, commit, configured):
    """Read-only resolver; network acquisition belongs to the gated preparer."""
    if absolute in configured:
        location = configured[absolute]
        if isinstance(location, dict):
            location = location.get(commit)
        if not isinstance(location, str):
            raise ContractError(f"native configured source lacks the exact submodule pin: {absolute}@{commit}")
        return check_repository(location, commit)
    candidates = [Path(parent) / relative]
    module_dir = git(parent, "rev-parse", "--path-format=absolute", "--git-path",
                     "modules/" + declaration["name"]).decode().strip()
    candidates.append(Path(module_dir))
    for candidate in candidates:
        try:
            return check_repository(candidate, commit)
        except ContractError:
            continue
    raise ContractError(
        f"native pinned submodule source missing: {absolute}@{commit}; "
        "configure migration.native_submodule_repositories with this exact public Git pin")


def closure(repository, commit, paths, configured, *, resolver=local_repository):
    """Enumerate all nested pins below requested roots with hard size bounds."""
    records = []
    totals = {"files": 0, "input_bytes": 0}

    def visit(parent, parent_commit, relative, prefix, active, depth):
        if depth > MAX_DEPTH or len(records) >= MAX_MODULES:
            raise ContractError("native pinned submodule closure exceeds its depth/module bound")
        links = {item["path"]: item["commit"] for item in readonly_links(parent, parent_commit)}
        declarations = module_declarations(parent, parent_commit)
        if relative not in links or relative not in declarations:
            raise ContractError("native submodule declaration differs from its parent's exact Git pin")
        absolute = prefix + relative
        repo_path(absolute)
        pin = links[relative]
        declaration = declarations[relative]
        source = resolver(parent, declaration, relative, absolute, pin, configured)
        source = check_repository(source, pin)
        tree = readonly_git(source, "rev-parse", pin + "^{tree}").decode().strip()
        identity = (tree, declaration["url"])
        if identity in active:
            raise ContractError("native pinned submodule closure contains a recursive source cycle")
        entries = readonly_git(source, "ls-tree", "-r", "-l", "-z", pin).split(b"\0")
        files, size = 0, 0
        for entry in entries:
            if not entry:
                continue
            metadata, name = entry.split(b"\t", 1)
            repo_path(name.decode())
            mode, kind, _, amount = metadata.split()
            if mode == b"160000":
                continue
            if kind != b"blob" or mode not in (b"100644", b"100755", b"120000"):
                raise ContractError("native dependency has an unsupported Git tree object")
            files += 1
            size += int(amount) + 1024
        totals["files"] += files
        totals["input_bytes"] += size
        if totals["files"] > MAX_FILES or totals["input_bytes"] > MAX_INPUT_BYTES:
            raise ContractError("native pinned submodule source exceeds the file/scratch bound")
        record = {
            "path": absolute, "commit": pin, "parent_commit": parent_commit,
            "repository": str(source), "declared_url": declaration["url"],
            "tree_id": tree, "files": files, "input_bytes": size, "depth": depth,
        }
        records.append(record)
        for nested in readonly_links(source, pin):
            visit(source, pin, nested["path"], absolute + "/", active | {identity}, depth + 1)

    for path in paths:
        visit(repository, commit, path, "", set(), 1)
    return records, totals


def task_local_cache(task, plan):
    """t8code builds its own dependencies; no historical install is reused."""
    return (
        (task["repo"], plan["input_index"]) == ("DLR-AMR/t8code", 6)
        and all((plan.get(field) or "").count("{cache}") == 1
                and "dep='{cache}/deps'" in plan[field]
                for field in ("configure_command", "build_command"))
        and "{root}/cache" not in str(plan)
        and not plan.get("env")
    )


def adapt(ctx, task, plan):
    """Materialize exact sources and replace only the declared preparation."""
    if (task["repo"], plan["input_index"]) not in PROFILES:
        return None
    repository = ctx.repo(task)
    links = readonly_links(repository, task["start_sha"])
    paths = selected_paths(task, plan, links)
    modules = ctx.modules(task)
    pins = {item["path"]: item["commit"] for item in links}
    if (len(modules) != len(pins)
            or {item["path"]: item["commit"] for item in modules} != pins):
        raise ContractError("native source catalog submodules differ from the exact superproject Git pins")
    configured = ctx.config.get("migration", {}).get("native_submodule_repositories", {}).get(task["repo"], {})
    records, totals = closure(repository, task["start_sha"], paths, configured)
    from .native_prepared_sources import reuse
    archived = reuse(ctx, task, paths, pins, records)
    assets = archived or {}
    for root in (() if archived is not None else paths):
        with tempfile.TemporaryDirectory(prefix="native-pinned-submodule-", dir=ctx.scratch) as temporary:
            destination = Path(temporary) / "source"
            subtree = [item for item in records if item["path"] == root or item["path"].startswith(root + "/")]
            for item in subtree:
                target = destination if item["path"] == root else destination / item["path"][len(root) + 1:]
                # Git archive may emit an empty directory for a gitlink.
                # Never replace files, traverse a link, or merge a checkout.
                if target.is_symlink():
                    raise ContractError("native nested submodule destination is linked")
                if target.exists():
                    try:
                        target.rmdir()
                    except OSError as exc:
                        raise ContractError("native nested submodule destination is not empty") from exc
                with start_archive(Path(item["repository"]), item["commit"], timeout=300) as stream:
                    extract_tar(stream, target)
            assets[root] = ctx.bundle(destination, role="submodule", git_commit=pins[root])
    result = []
    for item in modules:
        path = item["path"]
        if path in assets:
            result.append({"path": path, "commit": pins[path], "asset": assets[path]})
        else:
            result.append({**item})
    profile = PROFILES[(task["repo"], plan["input_index"])]
    original = plan[profile.field]
    prefix = profile.prefix + profile.operation
    guard = " && ".join("test -d " + shlex.quote(path) for path in paths)
    replacement = profile.prefix + guard + original[len(prefix):]
    modified = {**plan, profile.field: replacement}
    atomic_json(ctx.output / "native-public-submodules" / (component(task["id"]) + ".json"), {
        "id": task["id"], "start_sha": task["start_sha"], "roots": list(paths),
        "closure": records, "totals": totals, "assets": assets,
        "changed_field": profile.field, "original_command": original,
        "modified_command": replacement, "replaced_operation": profile.operation,
        "nested_gitlinks_complete": True, "source_changes": [], "test_changes": [],
        "method": "exact public Git objects including all nested pins and tracked tests; "
                  "fresh archives, no checkout/build outputs, no export transformations",
    })
    return modified, result
