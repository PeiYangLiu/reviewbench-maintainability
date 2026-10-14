"""Go lock closure from manifest bytes, never historical production sources."""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shlex
import subprocess
import tempfile
import zipfile

from reviewbench_maintainability.agentic.contracts import ContractError, file_hash, repo_path
from reviewbench_maintainability.agentic.snapshots import git

METADATA_NAMES = {"go.mod", "go.sum", "go.work", "go.work.sum"}
MAX_MODULES = 12000


@dataclass
class GoResolution:
    """One start/golden main-module context, not a replacement Go resolver."""
    main_modules: set[str] = field(default_factory=set)
    replacements: dict[tuple[str, str | None], tuple[str, str | None]] = field(default_factory=dict)

    def replacement(self, name, version):
        return self.replacements.get((name, version), self.replacements.get((name, None)))

    def dependency(self, name, version):
        replaced = self.replacement(name, version)
        if replaced:
            # Local replacements remove only the matching identity/version.
            # A remote RHS remains remote even if its path is also a use module.
            return (replaced, True) if replaced[1] is not None else None
        # Workspaces use current candidate source, but Go may still read the
        # .mod graph of explicitly required versions of those same module paths.
        return (name, version), name not in self.main_modules


@dataclass
class ModuleInput:
    zip_required: bool = False
    sums: dict[str, str] = field(default_factory=dict)
    origins: list[str] = field(default_factory=list)
    graph_scopes: set[str] = field(default_factory=set)


@dataclass
class GoInputs:
    modules: dict[tuple[str, str], ModuleInput] = field(default_factory=dict)
    manifests: list[dict] = field(default_factory=list)
    local_modules: set[str] = field(default_factory=set)
    scopes: dict[str, GoResolution] = field(default_factory=dict)
    # Extra requires only expand exact-version metadata, not @latest queries.
    expand: set[tuple[str, str]] = field(default_factory=set)
    download_all: bool = False

    def add(self, name, version, *, zipped=False, checksum=None, suffix=None, origin=""):
        repo_path(name)
        repo_path(version)
        if not re.fullmatch(r"v[0-9][^/\s]*", version):
            raise ContractError("go_dependency_version_not_exact: " + name + "@" + version)
        key = (name, version)
        item = self.modules.setdefault(key, ModuleInput())
        item.zip_required |= zipped
        if checksum:
            if not re.fullmatch(r"h1:[A-Za-z0-9+/]{43}=", checksum):
                raise ContractError("go_invalid_frozen_checksum: " + name)
            if suffix in item.sums and item.sums[suffix] != checksum:
                raise ContractError("go_frozen_checksum_conflict: " + name + "@" + version)
            item.sums[suffix] = checksum
        if origin and origin not in item.origins:
            item.origins.append(origin)
        if len(self.modules) > MAX_MODULES:
            raise ContractError("go_dependency_closure_exceeds_bound")

    def require(self, name, version, *, scope="", expand=False, origin=""):
        resolved = self.scopes.get(scope, GoResolution()).dependency(name, version)
        if resolved is None:
            return None
        key, zipped = resolved
        self.add(*key, zipped=zipped, origin=origin)
        self.modules[key].graph_scopes.add(scope)
        if expand or not zipped:
            self.expand.add(key)
        return key


def parse_mod(text: str) -> tuple[list[tuple[str, str]], list[tuple[str, str | None, str, str | None]]]:
    """Read require/replace directives; Go remains the actual module resolver."""
    requires, replacements = [], []
    block = None
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        try:
            tokens = shlex.split(line)
        except ValueError as exc:
            raise ContractError("go_manifest_syntax: " + str(exc)) from exc
        if tokens == [")"]:
            block = None
            continue
        directive = block
        if block is None:
            directive, tokens = tokens[0], tokens[1:]
            if tokens == ["("]:
                block = directive
                continue
        if directive == "require":
            if len(tokens) != 2:
                raise ContractError("go_require_syntax_pending")
            requires.append((tokens[0], tokens[1]))
        elif directive == "replace":
            if "=>" not in tokens:
                raise ContractError("go_replace_syntax_pending")
            split = tokens.index("=>")
            left, right = tokens[:split], tokens[split + 1:]
            if len(left) not in {1, 2} or len(right) not in {1, 2}:
                raise ContractError("go_replace_syntax_pending")
            replacements.append((left[0], left[1] if len(left) == 2 else None,
                                 right[0], right[1] if len(right) == 2 else None))
    return requires, replacements


def work_uses(text: str) -> list[str]:
    uses, in_use = [], False
    for raw in text.splitlines():
        tokens = shlex.split(raw.split("//", 1)[0].strip())
        if not tokens:
            continue
        if tokens == [")"]:
            in_use = False
        elif tokens == ["use", "("]:
            in_use = True
        elif tokens[0] == "use" or in_use:
            values = tokens[1:] if tokens[0] == "use" else tokens
            if len(values) != 1:
                raise ContractError("go_workspace_use_syntax_pending")
            uses.append(values[0])
    return uses


def candidate_relative(directory: str, target: str) -> str:
    if target != "." and not target.startswith(("./", "../")):
        raise ContractError("go_external_local_replacement_pending: " + target)
    relative = posixpath.normpath(posixpath.join(directory, target))
    if relative == ".":
        relative = ""
    repo_path(relative, allow_root=True)
    return relative


def main_manifests(files, cwd):
    workspace = next((name for name in files if Path(name).name == "go.work"), None)
    directories = ([candidate_relative(str(PurePosixPath(workspace).parent), path)
                    for path in work_uses(files[workspace].decode())] if workspace else [cwd])
    return workspace, [str(PurePosixPath(path) / "go.mod") for path in directories]


def module_resolution(files, cwd):
    """Only main go.mod/go.work replacements apply, including during expansion."""
    workspace, mains = main_manifests(files, cwd)
    result = GoResolution()
    work_rules = {(old, version): (target, pinned)
                  for old, version, target, pinned in parse_mod(files[workspace].decode())[1]} if workspace else {}

    def target(name, path, version):
        return (candidate_relative(str(PurePosixPath(name).parent), path), None) if version is None else (path, version)

    for name in mains:
        text = files[name].decode()
        module = re.search(r'^module\s+"?([^\s"]+)"?', text, re.M)
        if not module:
            raise ContractError("go_workspace_module_name_missing: " + name)
        result.main_modules.add(module[1])
        for old, version, path, pinned in parse_mod(text)[1]:
            key = old, version
            # A go.work wildcard also overrides version-specific go.mod rules.
            if key in work_rules or (old, None) in work_rules:
                continue
            value = target(name, path, pinned)
            if key in result.replacements and result.replacements[key] != value:
                raise ContractError("go_conflicting_workspace_replacements: " + old)
            result.replacements[key] = value
    for key, (path, pinned) in work_rules.items():
        result.replacements[key] = target(workspace, path, pinned)
    return result


def original_golden(ctx, oracle: dict, directory: Path) -> Path:
    source = oracle.get("source_only_golden", {})
    candidates = []
    if isinstance(source, dict) and source.get("patch"):
        candidates.append(ctx.legacy / "gopy" / repo_path(source["patch"]))
    if oracle.get("source_only_patch", {}).get("artifact"):
        candidates.append(ctx.legacy / "gopy" / repo_path(oracle["source_only_patch"]["artifact"]))
    candidates += [directory / name for name in (
        "dependency-closed-source-only.patch", "accepted-source-only-golden.patch", "source-only-golden.patch",
    )]
    path = next((p for p in candidates if p.is_file() and not p.is_symlink()), None)
    if path is None:
        raise ContractError("go_frozen_golden_missing")
    expected = oracle.get("golden_patch_sha256") or (source.get("patch_sha256") if isinstance(source, dict) else None)
    if not expected or file_hash(path) != expected:
        raise ContractError("go_frozen_golden_digest_differs")
    return path


def _metadata_paths(patch: Path) -> set[str]:
    paths = set()
    for line in patch.read_text().splitlines():
        if not line.startswith(("--- a/", "+++ b/")):
            continue
        name = line[6:]
        if PurePosixPath(name).name in METADATA_NAMES:
            paths.add(repo_path(name))
    return paths


def metadata_patch_environment(root: Path) -> dict[str, str]:
    """Make --no-index patching independent of any enclosing Git repository.

    --no-index alone still discovers an enclosing worktree and can silently
    skip paths outside its current prefix. These temporary files are a plain
    manifest projection, not a repository or the caller's Git worktree.
    """
    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    environment.update(
        GIT_CEILING_DIRECTORIES=str(root.resolve().parent),
        GIT_DISCOVERY_ACROSS_FILESYSTEM="0",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
    )
    return environment


def manifest_snapshots(ctx, task: dict, cwd: str, golden: Path | None, *, gowork=None):
    """Apply only module/workspace metadata hunks in a metadata-only directory."""
    repository = ctx.repo(task)
    names = set(git(repository, "ls-tree", "-r", "--name-only", task["start_sha"]).decode().splitlines())
    base = {}

    def read(name):
        if name not in base and name in names:
            base[name] = git(repository, "show", task["start_sha"] + ":" + name)

    touched = _metadata_paths(golden) if golden else set()
    for name in touched:
        read(name)

    def closure(files, *, deleted=()):
        def ensure(name, required=False):
            if name not in files and name not in deleted:
                read(name)
                if name in base:
                    files[name] = base[name]
            if required and name not in files:
                raise ContractError("go_local_replacement_manifest_missing: " + name)
            return name in files

        directories, selected = [cwd], set()
        workspace = None
        if gowork not in (None, "", "off"):
            if not gowork.startswith("{repo}/"):
                raise ContractError("go_explicit_workspace_path_pending: " + gowork)
            workspace = repo_path(gowork.removeprefix("{repo}/"))
            if Path(workspace).name != "go.work" or not ensure(workspace):
                raise ContractError("go_explicit_workspace_missing: " + workspace)
        elif gowork != "off":
            for parent in [PurePosixPath(cwd or "."), *PurePosixPath(cwd or ".").parents]:
                name = str(parent / "go.work")
                if ensure(name):
                    workspace = name
                    break
        if workspace:
            selected.add(workspace)
            if ensure(workspace + ".sum"):
                selected.add(workspace + ".sum")
            workdir = str(PurePosixPath(workspace).parent)
            uses = [candidate_relative(workdir, path) for path in work_uses(files[workspace].decode())]
            if cwd not in uses:
                raise ContractError("go_workspace_does_not_include_command_module: " + cwd)
            directories = uses

        def select_module(here):
            name = str(PurePosixPath(here) / "go.mod")
            ensure(name, required=True)
            selected.add(name)
            checksum = str(PurePosixPath(here) / "go.sum")
            if ensure(checksum):
                selected.add(checksum)

        for here in directories:
            select_module(here)
        resolution = module_resolution({name: files[name] for name in selected}, cwd)
        for target, version in resolution.replacements.values():
            if version is None:
                select_module(target)
        return selected

    selected_start = closure(base)
    start = dict(base)
    final = dict(base)
    if touched:
        with tempfile.TemporaryDirectory(prefix="go-lock-metadata-", dir=ctx.scratch) as temporary:
            root = Path(temporary)
            for name, content in base.items():
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            for name in touched:
                (root / name).parent.mkdir(parents=True, exist_ok=True)
            command = ["git", "apply", "--no-index", *("--include=" + name for name in sorted(touched)), str(golden)]
            result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=30,
                                    env=metadata_patch_environment(root))
            if result.returncode:
                raise ContractError("go_golden_metadata_patch_failed: " + result.stderr[-1000:])
            final = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    selected_golden = closure(final, deleted=touched - final.keys())
    phases = [("start", start, selected_start)]
    if golden:
        phases.append(("golden", final, selected_golden))
    return [(phase, {name: data for name, data in files.items() if name in selected})
            for phase, files, selected in phases]


def frozen_inputs(ctx, task: dict, directory: Path, cwd: str, *, oracle=None, contract=None) -> GoInputs:
    golden = original_golden(ctx, oracle, directory) if oracle else None
    gowork = contract.spec.get("environment", {}).get("GOWORK") if contract else None
    snapshots = manifest_snapshots(ctx, task, cwd, golden, gowork=gowork)
    result = GoInputs(download_all=bool(contract and contract.kind == "module-pin-test"))
    for phase, files in snapshots:
        scope = module_resolution(files, cwd)
        if contract and contract.kind == "local-replace-test":
            scope.replacements[("github.com/weaveworks/weave-gitops-enterprise-credentials", None)] = ("", None)
        result.scopes[phase] = scope
        # This is an inventory of candidate source, not a path-wide proxy denylist.
        result.local_modules.update(scope.main_modules)
        result.local_modules.update(old for (old, _), (_, version) in scope.replacements.items() if version is None)
    remote_targets = {target for scope in result.scopes.values()
                      for target in scope.replacements.values() if target[1] is not None}
    checksum_scopes = {}
    for phase, files in snapshots:
        for name, data in sorted(files.items()):
            origin = phase + ":" + name
            result.manifests.append({"phase": phase, "path": name, "sha256": hashlib.sha256(data).hexdigest()})
            if Path(name).name in {"go.sum", "go.work.sum"}:
                for line in data.decode().splitlines():
                    if not line.strip():
                        continue
                    fields = line.split()
                    if len(fields) != 3:
                        raise ContractError("go_invalid_sum_line: " + origin)
                    module, version, checksum = fields
                    mod_only = version.endswith("/go.mod")
                    version = version.removesuffix("/go.mod")
                    key = module, version
                    # Checksums describe physical objects, not replacement LHS
                    # aliases. Retain the lock union whenever either phase can
                    # use that remote identity, including a replacement RHS.
                    uses = [scope.dependency(*key) for scope in result.scopes.values()]
                    if key not in remote_targets and not any(use and use[0] == key for use in uses):
                        continue
                    zipped = not mod_only and (key in remote_targets or any(
                        use and use[0] == key and use[1] for use in uses))
                    result.add(module, version, zipped=zipped,
                               checksum=checksum, suffix=".mod" if mod_only else ".zip", origin=origin)
                    checksum_scopes.setdefault(key, set()).add(phase)
            else:
                for module, version in parse_mod(data.decode())[0]:
                    result.require(module, version, scope=phase, expand=result.download_all, origin=origin)
        for pin in contract.module_pins if contract else []:
            module, version = pin.rsplit("@", 1)
            result.require(module, version, scope=phase, expand=True, origin="original-wrapper:require")
    for key, item in result.modules.items():
        if item.zip_required and ".mod" not in item.sums:
            result.expand.add(key)
            if not item.graph_scopes:
                item.graph_scopes.update(checksum_scopes.get(key, set()))
    return result


def content_sum(path: Path, suffix: str) -> str:
    """Go's dirhash.Hash1, including module zip prefixes (not a ZIP byte hash)."""
    summary = hashlib.sha256()
    if suffix == ".mod":
        summary.update((file_hash(path) + "  go.mod\n").encode())
    elif suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            names = sorted(archive.namelist())
            if len(names) != len(set(names)) or sum(p.file_size for p in archive.infolist()) > 2 * 1024**3:
                raise ContractError("go_module_zip_layout_or_size_invalid")
            for name in names:
                if "\n" in name:
                    raise ContractError("go_module_zip_name_invalid")
                with archive.open(name) as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                summary.update((digest + "  " + name + "\n").encode())
    else:
        raise ContractError("go_unsupported_checksum_object")
    return "h1:" + base64.b64encode(summary.digest()).decode()
