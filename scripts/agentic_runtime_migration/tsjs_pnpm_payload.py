"""Export only isolated pnpm dependencies; keep workspace code in the candidate."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil

import yaml

from reviewbench_maintainability.agentic.contracts import (
    ContractError, canonical_hash, file_hash, repo_path,
)

SCHEMA = "reviewbench-pnpm-workspace-dependencies/v1"


def under(path, root):
    return path == root or root in path.parents


def input_plan(source, *, file_sources=None):
    """Read fixed dependency declarations, rejecting source-copy/injected layouts."""
    source = Path(source).resolve()
    file_sources = {} if file_sources is None else file_sources
    if not isinstance(file_sources, dict):
        raise ContractError("pnpm file source declarations must be an explicit mapping")
    lock_path = source / "pnpm-lock.yaml"
    workspace_path = source / "pnpm-workspace.yaml"
    if not lock_path.is_file() or not workspace_path.is_file():
        raise ContractError("pnpm requires the original root lock and workspace manifest")
    lock = yaml.safe_load(lock_path.read_text())
    workspace = yaml.safe_load(workspace_path.read_text())
    if str(lock.get("lockfileVersion")) != "9.0":
        raise ContractError("unsupported pnpm lockfile version")
    importers = lock.get("importers", {})
    if "." not in importers:
        raise ContractError("pnpm lock lacks the root importer")
    if (workspace.get("injectWorkspacePackages") or lock.get("settings", {}).get("injectWorkspacePackages")
            or workspace.get("nodeLinker", "isolated") != "isolated"
            or workspace.get("virtualStoreDir") not in (None, "node_modules/.pnpm")
            or workspace.get("enableGlobalVirtualStore") or workspace.get("sharedWorkspaceLockfile") is False):
        raise ContractError("unsupported injected/shared-store pnpm workspace layout")
    for name in (".pnpmfile.cjs", ".pnpmfile.js", "pnpmfile.cjs", "pnpmfile.js"):
        if (source / name).exists():
            raise ContractError("pnpm dependency hooks require a separate explicit adapter")
    files = ["package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml"]
    names = {}
    roots = []
    for importer in sorted(importers):
        importer = repo_path(importer, allow_root=True)
        path = source / importer / "package.json"
        if path.is_symlink() or not path.is_file():
            raise ContractError("pnpm importer manifest is not a fixed regular file: " + importer)
        manifest = json.loads(path.read_text())
        package_name = manifest.get("name")
        if not isinstance(package_name, str) or not package_name or package_name in names:
            raise ContractError("pnpm workspace package names must be unique and explicit")
        names[package_name] = importer
        if any(value.get("injected") for value in manifest.get("dependenciesMeta", {}).values()):
            raise ContractError("injected workspace production must not become a dependency payload")
        config = manifest.get("pnpm", {})
        if (config.get("injectWorkspacePackages") or config.get("patchedDependencies")
                or config.get("pnpmfile") or config.get("nodeLinker", "isolated") != "isolated"):
            raise ContractError("unsupported source-copy/patch pnpm package configuration")
        relative = path.relative_to(source).as_posix()
        if relative not in files:
            files.append(relative)
        roots.append(("node_modules" if importer == "." else importer + "/node_modules"))
        npmrc = source / importer / ".npmrc"
        if npmrc.exists():
            if npmrc.is_symlink():
                raise ContractError("pnpm configuration must be a regular input")
            # Unknown transport/linkage settings are not silently coerced to an
            # isolated layout. Registry/auth data are neither printed nor exported.
            text = npmrc.read_text()
            for line in text.splitlines():
                key = line.split("=", 1)[0].strip().lower()
                if any(word in key for word in ("inject", "node-linker", "virtual-store", "pnpmfile",
                                                "shared-workspace-lockfile", "symlink")):
                    raise ContractError("pnpm .npmrc layout override requires an explicit adapter")
            files.append(npmrc.relative_to(source).as_posix())
    local = []
    for name, declaration in file_sources.items():
        if not isinstance(declaration, dict) or set(declaration) != {"directory", "installed_root"}:
            raise ContractError("pnpm file source needs exact directory and installed-root bindings")
        directory = repo_path(declaration["directory"])
        installed = repo_path(declaration["installed_root"])
        if (directory == "node_modules" or "/node_modules/" in "/" + directory + "/"
                or not installed.startswith("node_modules/.pnpm/")):
            raise ContractError("pnpm local source and dependency-copy boundaries overlap")
        for relative in (directory, installed):
            current = source
            for part in Path(relative).parts:
                current /= part
                if current.is_symlink():
                    raise ContractError("pnpm file-source input is redirected: " + relative)
        manifest_path = source / directory / "package.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise ContractError("pnpm file source package manifest is missing")
        manifest = json.loads(manifest_path.read_text())
        package_name = manifest.get("name")
        if (not isinstance(package_name, str) or not package_name or package_name in names
                or name != package_name + "@file:" + directory
                or not installed.endswith("/node_modules/" + package_name)):
            raise ContractError("pnpm file source package name/path binding differs")
        resolution = lock.get("packages", {}).get(name, {}).get("resolution")
        if resolution != {"directory": directory, "type": "directory"}:
            raise ContractError("pnpm file source differs from the original lock directory")
        names[package_name] = directory
        files.append(directory + "/package.json")
        local.append({"package_key": name, "name": package_name, "directory": directory,
                      "installed_root": installed, "manifest_sha256": file_hash(manifest_path)})
    for name, package in lock.get("packages", {}).items():
        if name in file_sources:
            continue
        resolution = package.get("resolution", {})
        if (any(protocol in name for protocol in ("file:", "link:", "workspace:"))
                or resolution.get("directory") or resolution.get("type") == "directory"
                or not resolution.get("integrity")):
            raise ContractError("pnpm external package lacks a fixed registry integrity: " + name)
    workspace_paths = {str((source / name).resolve()): name for name in importers}
    for importer, row in importers.items():
        for kind in ("dependencies", "devDependencies", "optionalDependencies"):
            for name, dependency in row.get(kind, {}).items():
                value = dependency.get("version", "")
                if value.startswith("link:"):
                    target = (source / importer / value[5:]).resolve()
                    if str(target) not in workspace_paths:
                        raise ContractError("pnpm link does not bind a declared current workspace: " + name)
                elif (value.startswith("file:") or "@file:" in value) and file_sources:
                    matches = [
                        item for item in local
                        if value in {item["package_key"], "file:" + item["directory"]}
                        and dependency.get("specifier") == "file:" + item["directory"]
                        and (source / importer / item["directory"]).resolve() == source / item["directory"]
                    ]
                    if len(matches) != 1:
                        raise ContractError("pnpm importer file dependency is not explicitly bound")
                elif value.startswith(("file:", "workspace:")) or "@file:" in value:
                    raise ContractError("unsupported unresolved/source-copy pnpm local dependency")
    hashes = {}
    for name in sorted(set(files)):
        path = source / name
        if path.is_symlink() or not path.is_file():
            raise ContractError("pnpm input must be a regular file: " + name)
        hashes[name] = file_hash(path)
    plan = {"input_hashes": hashes, "workspace_names": names, "module_roots": roots,
            "lock_sha256": hashes["pnpm-lock.yaml"], "importers": sorted(importers),
            "packages": lock.get("packages", {})}
    if local:
        plan["file_sources"] = local
    return plan


def check_inputs(source, expected):
    for name, digest in expected.items():
        path = Path(source) / repo_path(name)
        if path.is_symlink() or not path.is_file() or file_hash(path) != digest:
            raise ContractError("frozen pnpm dependency input changed: " + name)


def walk_entries(root, *, excluded=()):
    for directory, directories, files in os.walk(root, followlinks=False):
        directories[:] = [name for name in directories if Path(directory) / name not in excluded]
        directories.sort()
        files.sort()
        for name in [*directories, *files]:
            path = Path(directory) / name
            if path not in excluded:
                yield path


def file_source_links(source, plan, roots):
    """Drop copied local code and bind its package root to current candidate source."""
    links, excluded = [], set()
    for item in plan.get("file_sources", []):
        original = source / repo_path(item["directory"])
        copied = source / repo_path(item["installed_root"])
        if (original.is_symlink() or not original.is_dir() or copied.is_symlink() or not copied.is_dir()
                or not any(under(copied, root) for root in roots)):
            raise ContractError("pnpm local package copy/source is missing or redirected")
        def inventory(root):
            result = {}
            for path in walk_entries(root):
                relative = path.relative_to(root).as_posix()
                if path.is_symlink():
                    result[relative] = ("link", os.readlink(path))
                elif path.is_file():
                    result[relative] = ("file", file_hash(path), bool(path.stat().st_mode & 0o111))
                elif not path.is_dir():
                    raise ContractError("pnpm local source has an unsupported object")
            return result
        if inventory(original) != inventory(copied):
            raise ContractError("pnpm installed local package differs from its bound original source")
        excluded.add(copied)
        links.append({
            "path": item["installed_root"],
            "link": os.path.relpath(original, copied.parent),
            "workspace_target": item["directory"], "kind": "directory",
            "origin": "current-file-source",
        })
    return links, excluded


def export_payload(source, destination, plan, *, registry_test_tools=None):
    """Preserve all dependency-relative topology, removing workspace-code links.

    All original workspace symlink text is recorded and restored at the same
    relative location inside the *current* /workspace. No workspace source file
    or injected workspace copy is eligible for the external dependency archive.
    """
    source, destination = Path(source).resolve(), Path(destination)
    if destination.exists():
        raise ContractError("pnpm export requires a new output directory")
    check_inputs(source, plan["input_hashes"])
    from .node_registry_tools import verify_registry_test_tools
    approved_tools = verify_registry_test_tools(source, plan, registry_test_tools)
    used_tools = set()
    roots = [name for name in plan["module_roots"] if (source / name).exists()]
    if "node_modules" not in roots:
        raise ContractError("pnpm root node_modules is absent")
    absolute_roots = [source / name for name in roots]
    for root in absolute_roots:
        if root.is_symlink() or not root.is_dir():
            raise ContractError("pnpm module root must be an isolated directory")
    metadata = source / "node_modules/.modules.yaml"
    if metadata.is_file():
        value = yaml.safe_load(metadata.read_text())
        if value.get("injectedDeps"):
            raise ContractError("pnpm injected workspace copies are not external dependencies")
        if value.get("nodeLinker", "isolated") != "isolated":
            raise ContractError("pnpm output is not an isolated layout")
    links, excluded = file_source_links(source, plan, absolute_roots)
    records = []
    for root in absolute_roots:
        for path in walk_entries(root, excluded=excluded):
            relative = path.relative_to(source).as_posix()
            if path.is_symlink():
                text = os.readlink(path)
                if os.path.isabs(text):
                    raise ContractError("pnpm dependency symlink must be relative: " + relative)
                lexical = Path(os.path.abspath(path.parent / text))
                if not under(lexical, source):
                    raise ContractError("pnpm dependency link escapes the source tree: " + relative)
                resolved = path.resolve()
                if not any(under(resolved, item) for item in absolute_roots):
                    workspace = resolved.relative_to(source).as_posix() if under(resolved, source) else None
                    known = [source / name for name in plan["importers"] if name != "."]
                    if resolved != source and not any(under(resolved, item) for item in known):
                        raise ContractError("pnpm source link is not a declared workspace: " + relative)
                    if not path.exists():
                        raise ContractError("pnpm workspace link is dangling: " + relative)
                    links.append({"path": relative, "link": text, "workspace_target": workspace,
                                  "kind": "directory" if resolved.is_dir() else "file"})
                    continue
                if not path.exists():
                    raise ContractError("pnpm external link is dangling: " + relative)
                records.append({"path": relative, "link": text})
            elif path.is_file():
                if path.name == "package.json":
                    try:
                        name = json.loads(path.read_text()).get("name")
                    except (ValueError, UnicodeDecodeError):
                        name = None
                    if name in plan["workspace_names"]:
                        package_root = path.parent.relative_to(source).as_posix()
                        if package_root not in approved_tools:
                            raise ContractError("copied workspace production found in dependency payload: " + relative)
                        used_tools.add(package_root)
                records.append({"path": relative, "sha256": file_hash(path),
                                "bytes": path.stat().st_size, "executable": bool(path.stat().st_mode & 0o111)})
            elif not path.is_dir():
                raise ContractError("unsupported pnpm dependency file type: " + relative)
    if used_tools != set(approved_tools):
        raise ContractError("declared registry test tool was not encountered at its exact original root")
    destination.mkdir(parents=True)
    for name in roots:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source / name, target, symlinks=True, ignore=lambda parent, names: [
            name for name in names if Path(parent) / name in excluded
        ])
    for row in links:
        if row.get("origin") == "current-file-source":
            if (destination / row["path"]).exists() or (destination / row["path"]).is_symlink():
                raise ContractError("pnpm copied local production leaked into the dependency payload")
        else:
            (destination / row["path"]).unlink()
    for row in records:
        path = destination / row["path"]
        if "link" in row:
            if not path.is_symlink() or os.readlink(path) != row["link"]:
                raise ContractError("pnpm external link changed during export")
        elif file_hash(path) != row["sha256"]:
            raise ContractError("pnpm external bytes changed during export")
    layout = {"schema_version": SCHEMA, "module_roots": roots, "workspace_links": links,
              "input_hashes": plan["input_hashes"], "external_entries": len(records),
              "external_bytes": sum(row.get("bytes", 0) for row in records),
              "external_index_sha256": canonical_hash(records),
              "workspace_production_files_exported": 0}
    if plan.get("file_sources"):
        layout["file_sources"] = plan["file_sources"]
    if approved_tools:
        layout["registry_test_tools"] = approved_tools
    (destination / "transport.json").write_text(json.dumps(layout, indent=2, sort_keys=True) + "\n")
    return layout, records


SOURCE = r"""'use strict';
// Generic transport only: no benchmark implementation, test, or variant.
const fs = require('fs'), path = require('path'), crypto = require('crypto');
const [payloadArg, workspaceArg] = process.argv.slice(2);
const fail = message => { throw new Error(message); };
const hash = p => crypto.createHash('sha256').update(fs.readFileSync(p)).digest('hex');
const inside = (p, root) => p === root || p.startsWith(root + path.sep);
function relative(s, rootAllowed = false) {
  if (typeof s !== 'string' || path.isAbsolute(s) || s.includes('\\') ||
      s.split('/').includes('..') || (!rootAllowed && (s === '.' || s === '')))
    fail('invalid transport-relative path');
  return s;
}
function parentsSafe(root, name) {
  const parts = name.split('/');
  let current = root;
  for (const part of parts.slice(0, -1)) {
    current = path.join(current, part);
    if (fs.existsSync(current) && fs.lstatSync(current).isSymbolicLink())
      fail('candidate dependency parent is a link: ' + current);
  }
}
try {
  if (!payloadArg || !workspaceArg || process.argv.length !== 4) fail('expected PAYLOAD CURRENT_WORKSPACE');
  const payload = path.resolve(payloadArg), workspace = path.resolve(workspaceArg);
  if (fs.lstatSync(workspace).isSymbolicLink() || !fs.statSync(workspace).isDirectory())
    fail('candidate workspace must be a real directory');
  const layout = JSON.parse(fs.readFileSync(path.join(payload, 'transport.json')));
  if (layout.schema_version !== 'reviewbench-pnpm-workspace-dependencies/v1' ||
      layout.workspace_production_files_exported !== 0) fail('unsupported pnpm payload');
  const roots = layout.module_roots.map(p => relative(p));
  if (!roots.includes('node_modules') || new Set(roots).size !== roots.length ||
      roots.some(p => p.split('/').at(-1) !== 'node_modules')) fail('invalid pnpm module roots');
  for (const [name, digest] of Object.entries(layout.input_hashes)) {
    relative(name); parentsSafe(workspace, name);
    const p = path.join(workspace, name);
    if (fs.lstatSync(p).isSymbolicLink() || hash(p) !== digest) fail('frozen pnpm input changed: ' + name);
  }
  for (const name of roots) {
    parentsSafe(workspace, name);
    const dest = path.join(workspace, name), src = path.join(payload, name);
    if (fs.existsSync(dest) || (() => { try { return fs.lstatSync(dest).isSymbolicLink(); } catch { return false; } })())
      fail('dependency destination already exists: ' + name);
    if (fs.lstatSync(src).isSymbolicLink()) fail('payload module root is a link');
  }
  function copy(src, dest) {
    fs.mkdirSync(dest, {recursive: true});
    for (const entry of fs.readdirSync(src, {withFileTypes: true})) {
      const a = path.join(src, entry.name), b = path.join(dest, entry.name);
      const stat = fs.lstatSync(a);
      if (stat.isSymbolicLink()) {
        const link = fs.readlinkSync(a), target = path.resolve(path.dirname(a), link);
        if (path.isAbsolute(link) || !inside(target, payload)) fail('payload link escapes');
        fs.symlinkSync(link, b);
      } else if (stat.isDirectory()) copy(a, b);
      else if (stat.isFile()) { fs.copyFileSync(a, b); fs.chmodSync(b, stat.mode & 0o777); }
      else fail('unsupported payload entry');
    }
  }
  for (const name of roots) copy(path.join(payload, name), path.join(workspace, name));
  for (const row of layout.workspace_links) {
    const name = relative(row.path), targetName = relative(row.workspace_target, true);
    if (!roots.some(root => name.startsWith(root + '/'))) fail('workspace link outside dependencies');
    parentsSafe(workspace, name);
    const dest = path.join(workspace, name), lexical = path.resolve(path.dirname(dest), row.link);
    const target = path.resolve(workspace, targetName);
    if (path.isAbsolute(row.link) || !inside(lexical, workspace) || !inside(target, workspace))
      fail('workspace link escapes current candidate');
    fs.mkdirSync(path.dirname(dest), {recursive: true});
    fs.symlinkSync(row.link, dest);
  }
  for (const row of layout.workspace_links) {
    const actual = fs.realpathSync(path.join(workspace, row.path));
    const target = fs.realpathSync(path.join(workspace, row.workspace_target));
    if (!inside(actual, workspace) || actual !== target) fail('workspace link does not bind current candidate');
  }
  console.log('PNPM_CURRENT_WORKSPACE ' + JSON.stringify({
    module_roots: roots.length, workspace_links: layout.workspace_links.length,
    workspace_production_files_exported: 0, external_index_sha256: layout.external_index_sha256
  }));
} catch (error) {
  console.error('PNPM_TRANSPORT_ERROR ' + error.message);
  process.exitCode = 2;
}
"""
