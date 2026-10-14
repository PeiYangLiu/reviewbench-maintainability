"""Original no-reinstall pnpm workspaces: current manifests, fixed installed bytes."""
from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath

from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash, repo_path
from .tsjs_fixed_install import _INPUT_CHECK
from .tsjs_pnpm_payload import SOURCE as TOPOLOGY_SOURCE

SCHEMA = "reviewbench-pnpm-original-fixed-workspaces/v1"
CONFIG_NAME = "fixed-workspaces.json"


def fixed_contract(layout, original_files):
    if (layout.get("schema_version") != "reviewbench-pnpm-workspace-dependencies/v1"
            or layout.get("workspace_production_files_exported") != 0):
        raise ContractError("fixed workspace install requires the original source-free layout")
    expected = layout["input_hashes"]
    if set(original_files) != set(expected) or not {
            "package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml"} <= set(expected):
        raise ContractError("original fixed workspace input inventory is incomplete")
    manifests, exact = {}, {}
    for name, data in original_files.items():
        repo_path(name)
        if hashlib.sha256(data).hexdigest() != expected[name]:
            raise ContractError("original fixed workspace input bytes differ: " + name)
        if name == "package.json" or name.endswith("/package.json"):
            value = json.loads(data)
            if not isinstance(value.get("name"), str) or not value["name"]:
                raise ContractError("original workspace package identity is missing")
            manifests[name] = value["name"]
        elif name != "pnpm-lock.yaml":
            exact[name] = expected[name]
    roots = {str(PurePosixPath(name).parent) for name in manifests}
    allowed = {"node_modules" if root == "." else root + "/node_modules" for root in roots}
    if ("node_modules" not in layout["module_roots"]
            or len(set(layout["module_roots"])) != len(layout["module_roots"])
            or not set(layout["module_roots"]) <= allowed):
        raise ContractError("original installed module roots differ from original workspaces")
    for row in layout["workspace_links"]:
        repo_path(row["path"])
        if row["workspace_target"] not in roots:
            raise ContractError("original source link is not a declared current workspace")
    return {
        "schema_version": SCHEMA, "policy": "original-install-with-current-workspace-manifests",
        "original_input_hashes": expected, "manifest_names": manifests,
        "exact_configuration": exact, "module_roots": layout["module_roots"],
        "workspace_links": layout["workspace_links"],
        "external_index_sha256": layout["external_index_sha256"],
        "source_manifest_or_lock_rewritten": False, "candidate_metadata_selects_dependencies": False,
    }


VALIDATION_CALL = "  validateOriginalFixedWorkspaces(payload, workspace, layout);\n"
VALIDATOR = r"""

function validateOriginalFixedWorkspaces(payload, workspace, layout) {
  const same = require('util').isDeepStrictEqual;
  const base = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixed-workspaces.json'), 'utf8'));
  if (base.schema_version !== 'reviewbench-pnpm-original-fixed-workspaces/v1' ||
      base.policy !== 'original-install-with-current-workspace-manifests' ||
      base.source_manifest_or_lock_rewritten !== false ||
      base.candidate_metadata_selects_dependencies !== false)
    fail('unsupported original fixed-workspace installation');
  if (!same(base.original_input_hashes, layout.input_hashes) ||
      !same(base.module_roots, layout.module_roots) ||
      !same(base.workspace_links, layout.workspace_links) ||
      base.external_index_sha256 !== layout.external_index_sha256)
    fail('original fixed installation/layout binding changed');
  function readCurrent(name) {
    relative(name); parentsSafe(workspace, name);
    const filename = path.join(workspace, name), st = fs.lstatSync(filename);
    if (!st.isFile() || st.isSymbolicLink()) fail('current workspace input is redirected: ' + name);
    return fs.readFileSync(filename, 'utf8');
  }
  for (const [name, identity] of Object.entries(base.manifest_names)) {
    const current = JSON.parse(readCurrent(name));
    if (current.name !== identity) fail('current workspace package identity changed: ' + name);
  }
  // The original workflow performs no reinstall after source changes. Version,
  // dependency and script fields remain current; no resolver uses them here.
  readCurrent('pnpm-lock.yaml');
  for (const [name, digest] of Object.entries(base.exact_configuration)) {
    readCurrent(name);
    if (hash(path.join(workspace, name)) !== digest)
      fail('original workspace topology/configuration changed: ' + name);
  }
  const all = ['pnpm-lock.yaml', ...Object.keys(base.manifest_names),
               ...Object.keys(base.exact_configuration)].sort();
  if (!same(all, Object.keys(layout.input_hashes).sort())) fail('incomplete fixed workspace binding');
  console.log('PNPM_ORIGINAL_FIXED_WORKSPACES ' + JSON.stringify({
    original_install: true, current_metadata_untouched: true, source_manifests: Object.keys(base.manifest_names).length
  }));
}
"""

if TOPOLOGY_SOURCE.count(_INPUT_CHECK) != 1:
    raise RuntimeError("reviewed pnpm transport input-check boundary changed")
SOURCE = TOPOLOGY_SOURCE.replace(_INPUT_CHECK, VALIDATION_CALL) + VALIDATOR
COPY_ROUTINE_SHA256 = hashlib.sha256(TOPOLOGY_SOURCE.replace(_INPUT_CHECK, "").encode()).hexdigest()


def contract_fingerprint(contract):
    return canonical_hash(contract)
