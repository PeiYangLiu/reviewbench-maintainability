"""Current-source topology transport for an explicitly original Yarn1 install."""
from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath

from reviewbench_maintainability.agentic.contracts import ContractError, repo_path
from .tsjs_fixed_install import _INPUT_CHECK
from .tsjs_pnpm_payload import SOURCE as TOPOLOGY_TRANSPORT

SCHEMA = "reviewbench-yarn1-original-fixed-install/v1"
MANIFEST_FIELDS = ("name", "scripts", "workspaces", "bin")


def original_contract(record, files):
    """Bind exact-start inputs; never select dependencies from a candidate/target."""
    if (record.get("workspace_production_files_exported") != 0
            or record.get("source_manifest_or_lock_rewritten") is not False
            or record.get("yarn_version") != "1.22.22"):
        raise ContractError("Yarn1 transport needs its original source-free fixed install")
    expected = record["input_hashes"]
    if set(files) != set(expected) or not {"package.json", "yarn.lock"} <= set(files):
        raise ContractError("Yarn1 original install inputs are incomplete")
    manifests, config = {}, {}
    for name, data in files.items():
        repo_path(name)
        if hashlib.sha256(data).hexdigest() != expected[name]:
            raise ContractError("Yarn1 dependency input differs from exact start: " + name)
        if name == "package.json" or name.endswith("/package.json"):
            definition = json.loads(data)
            manifests[name] = {field: definition.get(field) for field in MANIFEST_FIELDS}
        elif name != "yarn.lock":
            config[name] = expected[name]
    importers = {str(PurePosixPath(name).parent) for name in manifests}
    allowed_roots = {"node_modules" if name == "." else name + "/node_modules" for name in importers}
    if ("node_modules" not in record["module_roots"]
            or len(set(record["module_roots"])) != len(record["module_roots"])
            or not set(record["module_roots"]) <= allowed_roots):
        raise ContractError("Yarn1 module roots differ from the original workspace layout")
    for row in record["workspace_links"]:
        repo_path(row["path"])
        if row["workspace_target"] not in importers:
            raise ContractError("Yarn1 source link is not an original workspace root")
    return {
        "schema_version": SCHEMA,
        "policy": "same-original-install-for-every-current-source-candidate",
        "original_input_hashes": expected,
        "manifest_fields": list(MANIFEST_FIELDS),
        "manifests": manifests,
        "exact_config_inputs": config,
        "absent_config_inputs": [name for name in (".yarnrc", ".npmrc") if name not in expected],
        "module_roots": record["module_roots"],
        "workspace_links": record["workspace_links"],
        "source_manifest_or_lock_rewritten": False,
        "dependency_selection_uses_candidate_or_reference_metadata": False,
    }


if TOPOLOGY_TRANSPORT.count(_INPUT_CHECK) != 1:
    raise RuntimeError("shared topology validation boundary changed")

SOURCE = TOPOLOGY_TRANSPORT.replace(
    _INPUT_CHECK, "  validateOriginalYarnInstall(workspace, layout);\n",
).replace("PNPM_CURRENT_WORKSPACE ", "YARN1_CURRENT_WORKSPACE ").replace(
    "PNPM_TRANSPORT_ERROR ", "YARN1_TRANSPORT_ERROR ",
) + r"""

function validateOriginalYarnInstall(workspace, layout) {
  const same = require('util').isDeepStrictEqual;
  const base = JSON.parse(fs.readFileSync(path.join(__dirname, 'original-install.json'), 'utf8'));
  if (base.schema_version !== 'reviewbench-yarn1-original-fixed-install/v1' ||
      base.policy !== 'same-original-install-for-every-current-source-candidate' ||
      base.source_manifest_or_lock_rewritten !== false ||
      base.dependency_selection_uses_candidate_or_reference_metadata !== false)
    fail('unsupported original Yarn1 fixed-install contract');
  if (!same(base.original_input_hashes, layout.input_hashes) ||
      !same(base.module_roots, layout.module_roots) ||
      !same(base.workspace_links, layout.workspace_links))
    fail('original Yarn1 dependency layout binding changed');
  if (!same(base.manifest_fields, ['name', 'scripts', 'workspaces', 'bin']))
    fail('original Yarn1 manifest-field policy changed');
  function read(name) {
    relative(name); parentsSafe(workspace, name);
    const p = path.join(workspace, name), stat = fs.lstatSync(p);
    if (!stat.isFile() || stat.isSymbolicLink()) fail('candidate input is redirected: ' + name);
    return fs.readFileSync(p, 'utf8');
  }
  for (const [name, definition] of Object.entries(base.manifests)) {
    const current = JSON.parse(read(name));
    for (const field of base.manifest_fields)
      if (!same(current[field] === undefined ? null : current[field], definition[field]))
        fail('original Yarn1 command routing/workspace identity changed: ' + name + ':' + field);
  }
  // The original workflow deliberately does not reinstall after source changes.
  // Require current metadata to remain ordinary files, but neither replace those
  // bytes nor use them to select a different dependency graph.
  read('yarn.lock');
  for (const [name, digest] of Object.entries(base.exact_config_inputs)) {
    read(name);
    if (hash(path.join(workspace, name)) !== digest)
      fail('original Yarn1 execution configuration changed: ' + name);
  }
  for (const name of base.absent_config_inputs) {
    relative(name); parentsSafe(workspace, name);
    let exists = true;
    try { fs.lstatSync(path.join(workspace, name)); }
    catch (error) { if (error.code === 'ENOENT') exists = false; else throw error; }
    if (exists) fail('candidate added package-manager execution configuration: ' + name);
  }
  const all = ['yarn.lock', ...Object.keys(base.manifests), ...Object.keys(base.exact_config_inputs)].sort();
  if (!same(all, Object.keys(layout.input_hashes).sort()))
    fail('incomplete original Yarn1 input binding');
  console.log('YARN1_ORIGINAL_FIXED_INSTALL ' + JSON.stringify({
    graph: 'original-install', current_source: true, source_manifests: Object.keys(base.manifests).length,
    candidate_metadata_rewritten: false
  }));
}
"""
