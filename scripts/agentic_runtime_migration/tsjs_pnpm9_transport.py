"""Start-only, removal-compatible dependency transport for pnpm9 workspaces.

No target commit, reference patch, phase or test result participates in this
predicate. The candidate's declaration removals must agree with its lockfile;
all external resolutions, workspace membership and other settings stay fixed.
"""
from __future__ import annotations

import json
from pathlib import Path
import re

from reviewbench_maintainability.agentic.contracts import ContractError, file_hash
from .tsjs_pnpm_payload import SOURCE as ORIGINAL_TRANSPORT, export_payload

SCHEMA = "reviewbench-pnpm9-removal-compatible/v1"


def yaml_parser(payload, packages):
    """Select an already locked external parser, never a candidate implementation."""
    choices = []
    for manifest in payload.glob("node_modules/.pnpm/yaml@*/node_modules/yaml/package.json"):
        if manifest.is_symlink():
            raise ContractError("pnpm9 parser manifest is a link")
        data = json.loads(manifest.read_text())
        version = data.get("version", "")
        if data.get("name") != "yaml" or not re.fullmatch(r"2\.\d+\.\d+", version):
            continue
        package = packages.get("yaml@" + version)
        if not package or not package.get("resolution", {}).get("integrity"):
            raise ContractError("pnpm9 YAML parser lacks an original lock integrity")
        entry = manifest.parent / data["main"]
        if entry.is_symlink() or not entry.is_file() or not entry.resolve().is_relative_to(payload.resolve()):
            raise ContractError("pnpm9 YAML parser entry escapes its dependency payload")
        choices.append((tuple(map(int, version.split("."))), {
            "path": entry.relative_to(payload).as_posix(), "version": version,
            "manifest_path": manifest.relative_to(payload).as_posix(),
            "manifest_sha256": file_hash(manifest), "entry_sha256": file_hash(entry),
            "integrity": package["resolution"]["integrity"],
        }))
    if not choices:
        raise ContractError("pnpm9 requires an already locked YAML2 parser")
    return max(choices, key=lambda item: item[0])[1]


def build_payload(source, destination, plan, *, registry_test_tools=None):
    """Export unchanged external bytes plus public *start-only* compatibility data."""
    layout, index = export_payload(source, destination, plan, registry_test_tools=registry_test_tools)
    manifests = {
        name: (source / name).read_text()
        for name in plan["input_hashes"] if name.endswith("package.json")
    }
    data = {
        "schema_version": SCHEMA,
        "workspace_text": (source / "pnpm-workspace.yaml").read_text(),
        "lock_text": (source / "pnpm-lock.yaml").read_text(),
        "manifest_texts": manifests,
        "exact_other_inputs": {
            name: digest for name, digest in plan["input_hashes"].items()
            if name not in manifests and name not in {"pnpm-lock.yaml", "pnpm-workspace.yaml"}
        },
        "yaml_parser": yaml_parser(destination, plan["packages"]),
    }
    (destination / "compatibility-base.json").write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    return layout, index, data["yaml_parser"]


_ORIGINAL_HASH_CHECK = """  for (const [name, digest] of Object.entries(layout.input_hashes)) {
    relative(name); parentsSafe(workspace, name);
    const p = path.join(workspace, name);
    if (fs.lstatSync(p).isSymbolicLink() || hash(p) !== digest) fail('frozen pnpm input changed: ' + name);
  }
"""
if ORIGINAL_TRANSPORT.count(_ORIGINAL_HASH_CHECK) != 1:
    raise RuntimeError("reviewed pnpm transport input-check boundary changed")

# Reuse the reviewed copy/link checks without editing their original source file.
# Only the start-only compatibility predicate replaces raw package/lock hashing.
SOURCE = ORIGINAL_TRANSPORT.replace(
    _ORIGINAL_HASH_CHECK, "  compatibleInputs(payload, workspace, layout);\n",
) + r"""

function compatibleInputs(payload, workspace, layout) {
  const base = JSON.parse(fs.readFileSync(path.join(payload, 'compatibility-base.json'), 'utf8'));
  if (base.schema_version !== 'reviewbench-pnpm9-removal-compatible/v1')
    fail('unsupported pnpm9 compatibility data');
  const parserPath = path.join(payload, relative(base.yaml_parser.path));
  if (!inside(fs.realpathSync(parserPath), payload) || hash(parserPath) !== base.yaml_parser.entry_sha256)
    fail('immutable pnpm9 YAML parser differs');
  const manifestPath = path.join(payload, relative(base.yaml_parser.manifest_path));
  if (!inside(fs.realpathSync(manifestPath), payload) ||
      hash(manifestPath) !== base.yaml_parser.manifest_sha256)
    fail('immutable pnpm9 YAML parser manifest differs');
  const YAML = require(parserPath);
  function object(value, label) {
    if (value === null || typeof value !== 'object' || Array.isArray(value))
      fail('expected mapping: ' + label);
    return value;
  }
  function canonical(value, active = new Set()) {
    if (typeof value === 'number' && !Number.isFinite(value))
      fail('non-finite dependency metadata');
    if (value === null || ['string', 'number', 'boolean'].includes(typeof value))
      return JSON.stringify(value);
    if (typeof value !== 'object') fail('unsupported dependency metadata scalar');
    if (!Array.isArray(value) && ![Object.prototype, null].includes(Object.getPrototypeOf(value)))
      fail('unsupported dependency metadata mapping');
    if (active.has(value)) fail('cyclic dependency metadata');
    active.add(value);
    const result = Array.isArray(value)
      ? '[' + value.map(v => canonical(v, active)).join(',') + ']'
      : '{' + Object.keys(value).sort().map(k => JSON.stringify(k) + ':' + canonical(value[k], active)).join(',') + '}';
    active.delete(value);
    return result;
  }
  const same = (a, b) => canonical(a) === canonical(b);
  function read(name) {
    relative(name); parentsSafe(workspace, name);
    const p = path.join(workspace, name);
    if (!fs.lstatSync(p).isFile() || fs.lstatSync(p).isSymbolicLink())
      fail('dependency input is not a regular candidate file: ' + name);
    return fs.readFileSync(p, 'utf8');
  }
  function parse(text, label) {
    const result = YAML.parse(text, {uniqueKeys: true, maxAliasCount: 50});
    return object(result, label);
  }
  function without(value, keys) {
    return Object.fromEntries(Object.entries(value).filter(([k]) => !keys.includes(k)));
  }
  const sections = ['dependencies', 'devDependencies', 'optionalDependencies'];
  function removals(original, current, label) {
    object(original, label); object(current, label);
    if (!same(without(original, sections), without(current, sections)))
      fail('non-dependency metadata changed: ' + label);
    const result = {};
    for (const section of sections) {
      const a = object(Object.hasOwn(original, section) ? original[section] : {}, label + '.' + section);
      const b = object(Object.hasOwn(current, section) ? current[section] : {}, label + '.' + section);
      for (const key of Object.keys(b)) {
        if (!Object.hasOwn(a, key) || !same(a[key], b[key]))
          fail('new or changed dependency: ' + label + '.' + section + '.' + key);
      }
      result[section] = Object.keys(a).filter(k => !Object.hasOwn(b, k)).sort();
    }
    return result;
  }
  const previousLock = parse(base.lock_text, 'start lock');
  const currentLock = parse(read('pnpm-lock.yaml'), 'current lock');
  for (const [name, text] of Object.entries({
    ...base.manifest_texts, 'pnpm-lock.yaml': base.lock_text, 'pnpm-workspace.yaml': base.workspace_text
  })) {
    if (crypto.createHash('sha256').update(text).digest('hex') !== layout.input_hashes[name])
      fail('compatibility baseline differs from original start hash: ' + name);
  }
  if (!same(without(previousLock, ['importers']), without(currentLock, ['importers'])))
    fail('external dependency graph/settings changed');
  if (!same(parse(base.workspace_text, 'start workspace'),
            parse(read('pnpm-workspace.yaml'), 'current workspace')))
    fail('workspace membership/configuration changed');
  const previousImporters = object(previousLock.importers, 'start importers');
  const currentImporters = object(currentLock.importers, 'current importers');
  if (!same(Object.keys(previousImporters).sort(), Object.keys(currentImporters).sort()))
    fail('workspace importer set changed');
  const expectedManifests = Object.keys(previousImporters).map(
    key => (key === '.' ? '' : relative(key) + '/') + 'package.json').sort();
  if (!same(expectedManifests, Object.keys(base.manifest_texts).sort()))
    fail('incomplete start manifest binding');
  let count = 0;
  for (const importer of Object.keys(previousImporters)) {
    const name = (importer === '.' ? '' : relative(importer) + '/') + 'package.json';
    const a = removals(JSON.parse(base.manifest_texts[name]), JSON.parse(read(name)), name);
    const b = removals(previousImporters[importer], currentImporters[importer], 'lock.' + importer);
    if (!same(a, b)) fail('manifest/lock dependency removals disagree: ' + importer);
    count += Object.values(a).reduce((n, names) => n + names.length, 0);
  }
  for (const [name, digest] of Object.entries(base.exact_other_inputs)) {
    read(name);
    if (hash(path.join(workspace, name)) !== digest) fail('other dependency input changed: ' + name);
  }
  const expectedInputs = [...expectedManifests, 'pnpm-lock.yaml', 'pnpm-workspace.yaml',
                          ...Object.keys(base.exact_other_inputs)].sort();
  if (!same(expectedInputs, Object.keys(layout.input_hashes).sort()))
    fail('compatibility input set is incomplete');
  console.log('PNPM9_COMPATIBILITY ' + JSON.stringify({
    policy: 'unchanged-external-graph-consistent-removals-only',
    importers: expectedManifests.length, dependency_declarations_removed: count
  }));
}
"""
