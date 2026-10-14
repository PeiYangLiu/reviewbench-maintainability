"""Transport an explicitly frozen original install, rebinding local source packages."""
from __future__ import annotations

import json
from pathlib import Path

from reviewbench_maintainability.agentic.contracts import ContractError, file_hash
from .tsjs_pnpm_payload import SOURCE as ORIGINAL_TRANSPORT, export_payload

SCHEMA = "reviewbench-pnpm-original-fixed-install/v1"
_INPUT_CHECK = """  for (const [name, digest] of Object.entries(layout.input_hashes)) {
    relative(name); parentsSafe(workspace, name);
    const p = path.join(workspace, name);
    if (fs.lstatSync(p).isSymbolicLink() || hash(p) !== digest) fail('frozen pnpm input changed: ' + name);
  }
"""
if ORIGINAL_TRANSPORT.count(_INPUT_CHECK) != 1:
    raise RuntimeError("original pnpm input validation boundary changed")


def build_payload(source, destination, plan):
    """Do not select a different graph from target/candidate metadata.

    This mode requires a separately bound original no-reinstall workflow. Source
    manifests remain current and untouched; only the original command routing and
    local-source identity must remain fixed. No target metadata enters this data.
    """
    source = Path(source)
    if plan["importers"] != ["."]:
        raise ContractError("original fixed-install transport requires its root importer")
    layout, index = export_payload(source, destination, plan)
    manifest = json.loads((source / "package.json").read_text())
    local = {}
    for item in plan.get("file_sources", []):
        name = item["directory"] + "/package.json"
        local[name] = json.loads((source / name).read_text())
    data = {
        "schema_version": SCHEMA,
        "policy": "same-original-install-for-every-current-source-candidate",
        "scripts": manifest.get("scripts", {}),
        "pnpm_settings": manifest.get("pnpm", {}),
        "local_manifests": local,
        "exact_config_inputs": {
            name: digest for name, digest in plan["input_hashes"].items()
            if name not in {"package.json", "pnpm-lock.yaml", *local}
        },
        "original_root_manifest_sha256": file_hash(source / "package.json"),
        "original_lock_sha256": file_hash(source / "pnpm-lock.yaml"),
        "source_manifest_or_lock_rewritten": False,
        "dependency_selection_uses_candidate_or_reference_metadata": False,
    }
    (Path(destination) / "fixed-install.json").write_text(
        json.dumps(data, sort_keys=True, indent=2) + "\n")
    return layout, index


SOURCE = ORIGINAL_TRANSPORT.replace(
    _INPUT_CHECK, "  validateFixedInstall(payload, workspace, layout);\n",
) + r"""

function validateFixedInstall(payload, workspace, layout) {
  const same = require('util').isDeepStrictEqual;
  const base = JSON.parse(fs.readFileSync(path.join(payload, 'fixed-install.json'), 'utf8'));
  if (base.schema_version !== 'reviewbench-pnpm-original-fixed-install/v1' ||
      base.policy !== 'same-original-install-for-every-current-source-candidate' ||
      base.dependency_selection_uses_candidate_or_reference_metadata !== false ||
      base.source_manifest_or_lock_rewritten !== false)
    fail('unsupported original fixed-install contract');
  function read(name) {
    relative(name); parentsSafe(workspace, name);
    const p = path.join(workspace, name);
    const st = fs.lstatSync(p);
    if (!st.isFile() || st.isSymbolicLink()) fail('candidate input is redirected: ' + name);
    return fs.readFileSync(p, 'utf8');
  }
  const current = JSON.parse(read('package.json'));
  if (!same(current.scripts || {}, base.scripts) ||
      !same(current.pnpm || {}, base.pnpm_settings))
    fail('original package command routing/settings changed');
  // A no-reinstall workflow intentionally keeps its original installed graph.
  // Current package/lock bytes are not replaced or used to choose another graph.
  read('pnpm-lock.yaml');
  if (layout.input_hashes['package.json'] !== base.original_root_manifest_sha256 ||
      layout.input_hashes['pnpm-lock.yaml'] !== base.original_lock_sha256)
    fail('original fixed-install baseline binding changed');
  const expectedLocal = (layout.file_sources || []).map(
    row => relative(row.directory) + '/package.json').sort();
  if (!same(expectedLocal, Object.keys(base.local_manifests).sort()))
    fail('incomplete local source manifest binding');
  for (const [name, definition] of Object.entries(base.local_manifests)) {
    if (!same(JSON.parse(read(name)), definition))
      fail('current local source package identity changed: ' + name);
  }
  for (const [name, digest] of Object.entries(base.exact_config_inputs)) {
    read(name);
    if (hash(path.join(workspace, name)) !== digest)
      fail('original package execution configuration changed: ' + name);
  }
  const boundInputs = ['package.json', 'pnpm-lock.yaml',
    ...expectedLocal, ...Object.keys(base.exact_config_inputs)].sort();
  if (!same(boundInputs, Object.keys(layout.input_hashes).sort()))
    fail('incomplete original fixed-install input binding');
  console.log('PNPM_ORIGINAL_FIXED_INSTALL ' + JSON.stringify({
    graph: 'original-install', current_source: true,
    local_source_packages: expectedLocal.length, candidate_metadata_rewritten: false
  }));
}
"""
