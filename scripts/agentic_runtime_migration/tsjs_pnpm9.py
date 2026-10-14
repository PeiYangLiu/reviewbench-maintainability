"""Private original-session pnpm9 adapter with full-current-project public checks."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shlex
import tempfile

from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, atomic_json, canonical_hash, component, file_hash, repo_path,
)
from reviewbench_maintainability.agentic.snapshots import git
from .tsjs_pnpm import NODE, MANAGER, PAYLOAD, TRANSPORT, bind_oracle
from .tsjs_pnpm9_transport import SCHEMA, SOURCE

PRIVATE = "/opt/reviewbench-assets/pnpm9-session"

SESSION_SOURCE = r"""'use strict';
// Grader-only byte transport. No test implementation is generated here.
const fs = require('fs'), path = require('path'), crypto = require('crypto');
const fail = message => { throw new Error(message); };
const hash = p => crypto.createHash('sha256').update(fs.readFileSync(p)).digest('hex');
function local(name) {
  if (typeof name !== 'string' || path.isAbsolute(name) || name.includes('\\') ||
      name.split('/').some(p => p === '..' || p === '.' || p === ''))
    fail('invalid private session relative path');
  return name;
}
try {
  if (process.argv.length !== 4) fail('expected PRIVATE CURRENT_WORKSPACE');
  const [input, workspace] = process.argv.slice(2).map(p => path.resolve(p));
  if (fs.lstatSync(workspace).isSymbolicLink() || !fs.statSync(workspace).isDirectory())
    fail('private session workspace is not a real directory');
  const rows = JSON.parse(fs.readFileSync(path.join(input, 'session-inputs.json'), 'utf8'));
  if (!Array.isArray(rows) || rows.length === 0) fail('missing private input binding');
  const seen = new Set();
  for (const row of rows) {
    const name = local(row.destination);
    if (seen.has(name)) fail('duplicate private session destination');
    seen.add(name);
    const source = path.join(input, name), dest = path.join(workspace, name);
    const parts = name.split('/');
    let parent = workspace;
    for (const part of parts.slice(0, -1)) {
      parent = path.join(parent, part);
      const stat = fs.lstatSync(parent, {throwIfNoEntry: false});
      if (stat && (stat.isSymbolicLink() || !stat.isDirectory()))
        fail('private session parent is not a real candidate directory');
    }
    if (fs.lstatSync(dest, {throwIfNoEntry: false}))
      fail('private session would overwrite candidate input');
    if (!fs.lstatSync(source).isFile() || fs.lstatSync(source).isSymbolicLink() ||
        hash(source) !== row.sha256) fail('original private session bytes differ');
  }
  for (const row of rows) {
    const dest = path.join(workspace, row.destination);
    fs.mkdirSync(path.dirname(dest), {recursive: true});
    fs.copyFileSync(path.join(input, row.destination), dest, fs.constants.COPYFILE_EXCL);
    if (hash(dest) !== row.sha256) fail('private session copy differs');
  }
  console.log('PNPM9_PRIVATE_SESSION ' + JSON.stringify({files: rows.length, original_bytes: true}));
} catch (error) {
  console.error('PNPM9_PRIVATE_SESSION_ERROR ' + error.message);
  process.exitCode = 2;
}
"""


def validate_case(case, oracle):
    manager = case.get("package_manager", "")
    if not re.fullmatch(r"pnpm@9\.\d+\.\d+", manager):
        raise ContractError("pnpm9 session requires an exact manager version")
    if (case.get("status") != "ready" or case.get("build") not in (None, "SKIP")
            or case.get("install_cwd") != "." or case.get("reinstall_golden") is not False):
        raise ContractError("pnpm9 session setup differs from the supported fixed case")
    if case.get("install") != ["corepack", manager, "install", "--frozen-lockfile", "--ignore-scripts"]:
        raise ContractError("pnpm9 original frozen install flags changed")
    raw = case.get("test")
    if not isinstance(raw, list) or raw[:4] != ["corepack", manager, "exec", "vitest"] or raw[4:5] != ["run"]:
        raise ContractError("pnpm9 session needs the original Vitest run argv")
    names = bind_oracle(case, oracle)
    fixed = oracle.get("session_files", [])
    declared = case.get("session_files", [])
    if not fixed or len(fixed) != len(declared):
        raise ContractError("pnpm9 original session input binding is missing")
    seen = set()
    for before, item in zip(fixed, declared):
        if (before.get("source") != item.get("source") or before.get("destination") != item.get("destination")
                or not re.fullmatch(r"[0-9a-f]{64}", before.get("sha256", ""))):
            raise ContractError("pnpm9 original session source/hash differs")
        destination = repo_path(item["destination"])
        repo_path(item["source"])
        if (destination not in case.get("test_paths", []) or not destination.endswith((".spec.ts", ".test.ts"))
                or destination in seen):
            raise ContractError("pnpm9 session must be an original distinct test destination")
        seen.add(destination)
    if not re.fullmatch(r"vitest:files=[1-9]\d*,tests=[1-9]\d*", oracle.get("start_signature", "")):
        raise ContractError("pnpm9 original complete Vitest signature is missing")
    if oracle["start_signature"] != oracle.get("golden_signature"):
        raise ContractError("pnpm9 original replay signatures differ")
    repo_path(case["workspace"])
    return manager.split("@", 1)[1], names


def public_command():
    """Compile the complete current project-reference closure, not one anchor."""
    return shlex.join(["node", MANAGER + "/bin/pnpm.cjs", "exec", "tsc",
                       "--build", "tsconfig.lib.json", "--force", "--pretty", "false"])


def session_command(case):
    return shlex.join(["node", MANAGER + "/bin/pnpm.cjs", *case["test"][2:]])


def descriptor(ctx, task, oracle, case, pointer):
    version, logs = validate_case(case, oracle)
    if set(pointer) != {"path", "sha256"} or not Path(pointer["path"]).is_absolute():
        raise ContractError("pnpm9 requires an absolute hash-bound opt-in descriptor")
    p = Path(pointer["path"])
    if p.is_symlink() or not p.is_file() or file_hash(p) != pointer["sha256"]:
        raise ContractError("pnpm9 prepared descriptor changed")
    record = json.loads(p.read_text())
    if (record.get("schema_version") != SCHEMA or record.get("status") != "prepared"
            or record.get("id") != task["id"] or record.get("start_sha") != task["start_sha"]
            or record.get("case_fingerprint") != canonical_hash(case)
            or record.get("node_version") != case["node"] or record.get("pnpm_version") != version
            or record.get("workspace_production_files_exported") != 0):
        raise ContractError("pnpm9 prepared task/case/version binding differs")
    if ([row.get("name") for row in record.get("original_logs", [])] != logs
            or any(not re.fullmatch(r"[0-9a-f]{64}", row.get("sha256", ""))
                   for row in record.get("original_logs", []))):
        raise ContractError("pnpm9 original six-log binding differs")
    for name in ("node", "manager", "dependencies"):
        asset = record[name]
        source = Path(asset["path"])
        if not source.is_absolute() or source.is_symlink() or file_hash(source) != asset["sha256"]:
            raise ContractError("pnpm9 asset descriptor changed: " + name)
        role = json.loads(source.read_text()).get("source_kind")
        if role != ("dependency" if name == "dependencies" else "toolchain"):
            raise ContractError("pnpm9 asset content role differs: " + name)
    for name, digest in record["input_hashes"].items():
        data = git(ctx.repo(task), "show", task["start_sha"] + ":" + repo_path(name))
        if __import__("hashlib").sha256(data).hexdigest() != digest:
            raise ContractError("pnpm9 public baseline differs from exact start: " + name)
    return record


def migrate(ctx, task, oracle, case, pointer):
    from .tsjs_lane import IMAGE, node_worker_environment, reference_patch, success_pattern
    record = descriptor(ctx, task, oracle, case, pointer)
    directory = ctx.legacy / "tsjs" / oracle["recovery_artifact"]
    for row in record["original_logs"]:
        path = directory / row["name"]
        if path.is_symlink() or not path.is_file() or file_hash(path) != row["sha256"]:
            raise ContractError("pnpm9 original replay log changed")
    project = repo_path(case["workspace"]) + "/tsconfig.lib.json"
    # Presence only; the public compiler reads the candidate's current project.
    if not git(ctx.repo(task), "show", task["start_sha"] + ":" + project):
        raise ContractError("pnpm9 original complete production project is absent")
    image, audit = ctx.image(IMAGE)
    with tempfile.TemporaryDirectory(prefix="pnpm9-transport-", dir=ctx.scratch) as temporary:
        root = Path(temporary)
        (root / "transport.cjs").write_text(SOURCE)
        transport = ctx.bundle(root, role="dependency")
    with tempfile.TemporaryDirectory(prefix="pnpm9-private-", dir=ctx.scratch) as temporary:
        root = Path(temporary)
        copied = []
        for item in oracle["session_files"]:
            source = ctx.legacy / "tsjs" / repo_path(item["source"])
            if source.is_symlink() or not source.is_file() or file_hash(source) != item["sha256"]:
                raise ContractError("pnpm9 original private session bytes changed")
            destination = repo_path(item["destination"])
            if destination == task["locality"]["path"]:
                raise ContractError("pnpm9 private input would overwrite production")
            original_paths = git(ctx.repo(task), "ls-tree", "-r", "--name-only", task["start_sha"], "--", destination)
            if original_paths.strip():
                raise ContractError("pnpm9 injected session must not overwrite exact-start source")
            target = root / destination
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            copied.append({"destination": destination, "sha256": file_hash(target)})
        (root / "inject.cjs").write_text(SESSION_SOURCE)
        atomic_json(root / "session-inputs.json", copied)
        private = ctx.bundle(root, role="grader")
    assets = [
        {**record[name], "kind": "archive", "visibility": "developer", "target": target}
        for name, target in (("node", NODE), ("manager", MANAGER), ("dependencies", PAYLOAD))
    ] + [{**transport, "kind": "archive", "visibility": "developer", "target": TRANSPORT},
         {**private, "kind": "archive", "visibility": "grader", "target": PRIVATE}]
    grade = ["node " + PRIVATE + "/inject.cjs " + PRIVATE + " /workspace"]
    recipe = RuntimeRecipe(
        task_id=task["id"], repo=task["repo"], start_sha=task["start_sha"], lane="tsjs",
        image=image, image_audit=audit, cpus=2, memory_mb=2048, build_scratch_mb=12288,
        command_timeout_seconds=case.get("test_timeout") or 600,
        workdir="/workspace/" + repo_path(case["workspace"]),
        env={"PATH": NODE + "/bin:" + MANAGER + "/bin:/usr/local/bin:/usr/bin:/bin",
             "HOME": "/workspace/.reviewbench/home", "TMPDIR": "/workspace/.reviewbench/tmp",
             "NODE_OPTIONS": "--max-old-space-size=1536", "CI": "true", "HUSKY": "0", "FORCE_COLOR": "0",
             "COREPACK_ENABLE_NETWORK": "0", "COREPACK_ENABLE_PROJECT_SPEC": "0",
             "npm_config_offline": "true", "SOURCE_ROOT": "/workspace", **node_worker_environment(2)},
        assets=assets, bootstrap=[
            "mkdir -p /workspace/.reviewbench/tmp /workspace/.reviewbench/home",
            "test \"$(node --version)\" = " + shlex.quote("v" + record["node_version"]) +
            " && test \"$(node " + MANAGER + "/bin/pnpm.cjs --version)\" = " + shlex.quote(record["pnpm_version"]),
            "node " + TRANSPORT + "/transport.cjs " + PAYLOAD + " /workspace",
        ], dev_checks=[public_command()], submodules=ctx.modules(task),
        scratch_paths=record["module_roots"],
        grading={"multifile_build": True, "immutable_paths": ctx.immutable_tests(task),
                 "golden_apply_ignore_space_change": True, "golden_patch": reference_patch(ctx, task, oracle),
                 "bootstrap": grade, "commands": [{
                     "command": session_command(case), "parser": "regex",
                     "expected_tests": oracle.get("pass_to_pass_tests") or
                                       [task["id"] + "::frozen-" + oracle["start_signature"]],
                     "success_pattern": success_pattern(oracle["start_signature"]),
                 }]},
    )
    recipe.validate()
    atomic_json(ctx.output / "pnpm9-plans" / (component(task["id"]) + ".json"), {
        "id": task["id"], "descriptor": pointer, "runtime_fingerprint": recipe.fingerprint,
        "public_check": public_command(), "original_test_argv": case["test"],
        "private_session_inputs": copied, "original_session_bytes_preserved": True,
        "compatibility": "start-only unchanged external graph; consistent declaration removals",
        "public_assets_contain_reference_data": False,
        "readiness": "requires original current-engine public/start3/golden3 execution",
    })
    return recipe
