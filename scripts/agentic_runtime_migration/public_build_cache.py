"""Capture only external Cargo dependency artifacts, never workspace/grader outputs."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shlex
import shutil
import stat
import subprocess

from reviewbench_maintainability.agentic.contracts import ContractError, RuntimeRecipe, canonical_hash, file_hash
from . import cache_seed_entry as entry

TARGET = "/opt/reviewbench-assets/public-build-cache"
TOOL = "/opt/reviewbench-assets/public-cache-seed.py"
GRAPH_SCHEMA = "reviewbench-public-cache-graph-groups/v1"


def image_input_name(index, asset):
    """Keep distinct fixed-mtime payloads in distinct Docker context namespaces.

    Context transfer may reuse same-path/same-size/same-mtime files before COPY
    computes a layer. A hash-pinned asset therefore owns its context path, not
    merely its destination or a positional input-N name.
    """
    digest = asset.get("sha256", "")
    if not re.fullmatch(r"[0-9a-f]{64}", digest) or type(index) is not int or index < 0:
        raise ContractError("public image input needs an immutable asset identity")
    return f"input-{index}-{digest}"


def validate_public_image_inputs(recipe):
    """Reject direct role/path conflicts before materializing large archives."""
    private_hashes = {a["sha256"] for a in recipe.assets if a["visibility"] == "grader"}
    if recipe.grading.get("golden_patch"):
        private_hashes.add(recipe.grading["golden_patch"]["sha256"])
    for asset in recipe.assets:
        if asset["visibility"] != "developer":
            continue
        target = Path(asset["target"])
        if not target.is_relative_to("/opt/reviewbench-assets") or ".." in target.parts:
            raise ContractError("only public /opt/reviewbench-assets inputs may be baked")
        if asset["sha256"] in private_hashes:
            raise ContractError("private artifact appeared among public image inputs: " + str(target))


def bake_public_inputs(recipe, pool, output):
    """Give immutable compiler/dependency inputs stable mtimes across fresh jobs.

    The frozen asset extractor intentionally creates fresh timestamps. That is
    correct for editable source, but otherwise invalidates transplanted Cargo
    dependency fingerprints. Put only the already-declared public inputs into
    a read-only image layer; workspace and grader inputs are never included.
    """
    from reviewbench_maintainability.agentic.environment import docker, resolve_asset, validate_assets
    from reviewbench_maintainability.agentic.snapshots import tree_files
    validate_public_image_inputs(recipe)
    validate_assets(recipe, pool.asset_root)
    public = [asset for asset in recipe.assets if asset["visibility"] == "developer"]
    if not public:
        return recipe, {"status": "already_image_resident"}
    output = Path(output)
    context = output / "public-image-context"
    context.mkdir()
    base_alias = "reviewbench-cache-base:" + recipe.image.removeprefix("sha256:")[:24]
    docker("tag", recipe.image, base_alias)
    instructions = ["FROM " + base_alias]
    files, roots = [], []
    private_hashes = {a["sha256"] for a in recipe.assets if a["visibility"] == "grader"}
    if recipe.grading.get("golden_patch"):
        private_hashes.add(recipe.grading["golden_patch"]["sha256"])
    for index, asset in enumerate(public):
        target = Path(asset["target"])
        if not target.is_relative_to("/opt/reviewbench-assets") or ".." in target.parts:
            raise ContractError("only public /opt/reviewbench-assets inputs may be baked")
        source = (pool.materialize(asset) if asset.get("kind") == "archive"
                  else resolve_asset(asset, pool.asset_root))
        copied = context / image_input_name(index, asset)
        roots.append({"path": str(target), "directory": source.is_dir()})
        if source.is_dir():
            shutil.copytree(source, copied, symlinks=True)
            if any(p.name == ".git" for p in copied.rglob("*")):
                raise ContractError("public image input contains repository history")
            pairs = [(name, path, target / name) for name, path in tree_files(copied).items()]
            instructions.append("COPY " + json.dumps([copied.name + "/", str(target) + "/"]))
        else:
            shutil.copy2(source, copied)
            pairs = [("", copied, target)]
            instructions.append("COPY " + json.dumps([copied.name, str(target)]))
        for _, path, installed in pairs:
            if ".git" in path.parts:
                raise ContractError("public image input contains repository history")
            if path.is_symlink():
                files.append({"path": str(installed), "link": os.readlink(path)})
            else:
                digest = file_hash(path)
                if digest in private_hashes:
                    raise ContractError("private artifact appeared among public image inputs")
                files.append({"path": str(installed), "sha256": digest,
                              "mode": stat.S_IMODE(path.stat().st_mode)})
            os.utime(path, ns=(0, 0), follow_symlinks=False)
        if copied.is_dir():
            for directory in sorted((p for p in copied.rglob("*") if p.is_dir() and not p.is_symlink()),
                                    reverse=True):
                os.utime(directory, ns=(0, 0))
            os.utime(copied, ns=(0, 0))
    (context / "Dockerfile").write_text("\n".join(instructions) + "\n")
    (output / "public-image-context-inputs.json").write_text(json.dumps([
        {"context_path": image_input_name(i, a), "target": a["target"], "asset_sha256": a["sha256"]}
        for i, a in enumerate(public)], indent=2, sort_keys=True) + "\n")
    command = ["docker", "build", "--network", "none", "--cpu-quota", str(recipe.cpus * 100000),
               "--memory", str(recipe.memory_mb) + "m", "--iidfile", str(output / "public-image.id"), str(context)]
    built = subprocess.run(command, capture_output=True, text=True, timeout=900)
    (output / "public-image-build.stdout").write_text(built.stdout)
    (output / "public-image-build.stderr").write_text(built.stderr)
    if built.returncode:
        raise ContractError("public immutable input image build failed: " + built.stderr[-1500:])
    image = (output / "public-image.id").read_text().strip()
    expected = output / "public-image-files.json"
    expected.write_text(json.dumps(files, sort_keys=True) + "\n")
    probe = """import hashlib,json,os,stat
from pathlib import Path
rows=json.loads(Path('/tmp/expected.json').read_text())
roots=json.loads(ROOTS)
expected={r['path'] for r in rows}
assert len(expected)==len(rows), 'overlapping public input files'
observed=set()
for root in roots:
    path=Path(root['path'])
    if not root['directory']:
        observed.add(str(path))
        continue
    assert path.is_dir() and not path.is_symlink(), 'public input directory redirected'
    for folder,dirs,files in os.walk(path,followlinks=False):
        observed.update(str(Path(folder)/n) for n in files)
        observed.update(str(Path(folder)/n) for n in dirs if (Path(folder)/n).is_symlink())
assert observed==expected, 'public image inventory mismatch: '+repr(sorted(observed^expected)[:16])
errors=[]
for r in rows:
    p=Path(r['path'])
    if 'link' in r:
        ok=p.is_symlink() and os.readlink(p)==r['link']
    else:
        ok=(p.is_file() and not p.is_symlink()
            and hashlib.sha256(p.read_bytes()).hexdigest()==r['sha256']
            and stat.S_IMODE(p.stat().st_mode)==r['mode'] and p.stat().st_mtime_ns==0)
    if not ok:
        errors.append(r['path'])
assert not errors, 'immutable public input mismatch: '+repr(errors[:16])
print('PUBLIC_IMAGE_INPUTS_VERIFIED files='+str(len(rows)))
""".replace("ROOTS", repr(json.dumps(roots)))
    inspected = docker("run", "--rm", "--read-only", "--network", "none", "--cpus", str(recipe.cpus),
                       "--memory", str(recipe.memory_mb) + "m", "--mount",
                       "type=bind,source=" + str(expected) + ",target=/tmp/expected.json,readonly",
                       "--entrypoint", "python3", image, "-c", probe, timeout=300)
    audit = output / "public-image-audit.json"
    audit.write_text(json.dumps({
        "schema_version": "reviewbench-developer-image-audit/v1", "image": image,
        "expected_image_id": image, "no_reference_assets": True, "no_repository_history": True,
        "basis": "verified audited base plus byte/mode-verified declared public inputs only; no RUN or source checkout",
        "base_image": recipe.image, "base_image_audit": recipe.image_audit,
        "public_input_assets": public, "input_inventory_sha256": file_hash(expected),
        "context_identity": "content-addressed-asset-paths/v1",
        "context_inputs_sha256": file_hash(output / "public-image-context-inputs.json"),
        "exact_target_inventory_verified": True,
        "stable_input_mtime_ns": 0, "verification_stdout": inspected,
        "build_command": command, "build_exit_status": built.returncode,
    }, indent=2, sort_keys=True) + "\n")
    value = asdict(recipe)
    value["image"], value["image_audit"] = image, {"path": str(audit), "sha256": file_hash(audit)}
    value["assets"] = [asset for asset in value["assets"] if asset["visibility"] != "developer"]
    result = RuntimeRecipe.from_dict(value)
    result.validate()
    # The exact image is retained locally; image archival can run in the
    # independent archive pool instead of holding up preparation/verification.
    return result, {"status": "built_and_verified", "image": image, "public_files": len(files),
                    "audit": {"path": str(audit), "sha256": file_hash(audit)},
                    "remote_image_archive_pending": True}


def public_binding(recipe):
    value = asdict(recipe)
    # The seed is produced without grader assets or the reference patch.
    value.pop("grading")
    value["image_audit"] = {"sha256": value["image_audit"]["sha256"]}
    value["assets"] = [{k: v for k, v in a.items() if k != "path"}
                       for a in value["assets"] if a["visibility"] == "developer"]
    for module in value["submodules"]:
        if "asset" in module:
            module["asset"] = {"sha256": module["asset"]["sha256"]}
    return canonical_hash(value)


def metadata_groups(metadata):
    """Keep each complete public graph bound to its own target namespace."""
    if "cache_groups" not in metadata:
        if metadata.get("schema_version") == GRAPH_SCHEMA:
            raise ContractError("public cache graph groups are missing")
        return [("", metadata)]
    groups = metadata["cache_groups"]
    if metadata.get("schema_version") != GRAPH_SCHEMA or not isinstance(groups, dict) or not 1 <= len(groups) <= 16:
        raise ContractError("invalid public cache graph groups")
    result = []
    for namespace, graph in sorted(groups.items()):
        if not isinstance(namespace, str) or not re.fullmatch(r"form-[0-9a-f]{20}", namespace):
            raise ContractError("invalid public cache graph namespace")
        if not isinstance(graph, dict) or not isinstance(graph.get("packages"), list) or not graph["packages"]:
            raise ContractError("public cache group has no complete package graph")
        result.append((namespace, graph))
    return result


def external_artifacts(target, metadata):
    groups = metadata_groups(metadata)
    if groups[0][0] == "":
        return _external_artifacts(target, groups[0][1])
    names, local, external = [], set(), set()
    for namespace, graph in groups:
        directory = Path(target) / namespace
        if directory.is_symlink() or not directory.is_dir():
            raise ContractError("public cache namespace missing or redirected")
        selected, excluded, dependencies = _external_artifacts(directory, graph)
        names.extend(namespace + "/" + name for name in selected)
        local.update(excluded)
        external.update(dependencies)
    return sorted(names), sorted(local), sorted(external)


def _external_artifacts(target, metadata):
    packages = metadata["packages"]
    local = {p["name"] for p in packages if p.get("source") is None}
    external = {p["name"] for p in packages if p.get("source") is not None} - local
    if not local or not external:
        raise ContractError("dependency seed needs both workspace and external package identities")
    names = []
    for path in sorted(Path(target).rglob("*")):
        if path.is_dir() and not path.is_symlink():
            continue
        name = path.relative_to(target).as_posix()
        parts = Path(name).parts
        if name in {".rustc_info.json", "CACHEDIR.TAG"}:
            names.append(name)
            continue
        offset = 0 if parts[0] in {"debug", "release"} else 1
        if len(parts) < offset + 3 or parts[offset] not in {"debug", "release"}:
            continue
        category, member = parts[offset + 1:offset + 3]
        if category in {"build", ".fingerprint"}:
            match = re.fullmatch(r"(.+)-[0-9a-f]{16}", member)
            allowed = match and match[1] in external
        elif category == "deps":
            match = re.fullmatch(r"(.+?)-[0-9a-f]{16}(?:[.-].+)?", member)
            forbidden = {n.replace("-", "_") for n in local}
            candidates = {match[1], match[1].removeprefix("lib")} if match else set()
            allowed = candidates & {n.replace("-", "_") for n in external} and not candidates & forbidden
        else:
            allowed = False
        if allowed:
            names.append(name)
    if not names:
        raise ContractError("no external dependency artifacts were produced")
    return names, sorted(local), sorted(external)


def portable_artifacts(target, names, external, runtime_target=None):
    """Keep a closed seed; normalize only links within its declared cache mount.

    Artifacts belonging to an external package with a nonportable link are
    omitted together. Cargo rebuilds that package instead of importing an
    incomplete cache unit. Other already compiled dependency artifacts remain
    usable. No absolute link is read or admitted by the seed importer.
    """
    if runtime_target is not None:
        runtime_target = PurePosixPath(runtime_target)
        if not runtime_target.is_absolute() or ".." in runtime_target.parts or len(runtime_target.parts) < 3:
            raise ContractError("invalid runtime cache mount")
    normalized_names = {}
    for package in external:
        normalized_names.setdefault(package.replace("-", "_"), set()).add(package)

    def owners(name):
        result = set()
        for part in PurePosixPath(name).parts:
            match = re.fullmatch(r"(.+?)-[0-9a-f]{16}(?:[.-].+)?", part)
            if match:
                stem = match[1]
                if stem in external:
                    result.add(stem)
                for candidate in (stem, stem.removeprefix("lib")):
                    result.update(normalized_names.get(candidate, set()))
        return result

    ownership = {name: owners(name) for name in names}
    links, destinations = {}, {}
    for name in names:
        source = Path(target) / name
        if not source.is_symlink():
            continue
        original = os.readlink(source)
        link = PurePosixPath(original)
        if link.is_absolute():
            if runtime_target is None or not link.is_relative_to(runtime_target):
                destinations[name] = None
                continue
            destination = link.relative_to(runtime_target).as_posix()
        else:
            destination = posixpath.join(posixpath.dirname(name), original)
        destination = posixpath.normpath(destination)
        if destination == ".." or destination.startswith("../") or destination.startswith("/"):
            destinations[name] = None
            continue
        destinations[name] = destination
        links[name] = posixpath.relpath(destination, posixpath.dirname(name))
    selected = set(names)
    excluded = set()
    while True:
        bad = set()
        for name, destination in destinations.items():
            if name not in selected:
                continue
            if destination is None or not (
                    destination in selected or any(p.startswith(destination + "/") for p in selected)):
                bad.add(name)
                continue
            visited = {name}
            while destination in destinations:
                if destination in visited or destination not in selected:
                    bad.add(name)
                    break
                visited.add(destination)
                destination = destinations[destination]
        if not bad:
            break
        excluded.update(package for name in bad for package in ownership[name])
        remaining = {name for name in selected if name not in bad and not ownership[name] & excluded}
        if remaining == selected:
            raise ContractError("nonportable cache link filtering did not converge")
        selected = remaining
    if not selected or not any(ownership[name] for name in selected):
        raise ContractError("no portable external dependency artifacts were produced")
    changes = [{"path": name, "original_target": os.readlink(Path(target) / name), "target": links[name]}
               for name in sorted(selected & links.keys()) if os.readlink(Path(target) / name) != links[name]]
    return sorted(selected), links, sorted(excluded), changes


def capture(target, metadata, output, binding, *, producer_receipt, runtime_target=None):
    if producer_receipt.get("producer") != "clean-start-public-check" or producer_receipt.get(
            "grader_assets_visible") is not False or producer_receipt.get("public_check_exit_status") != 0:
        raise ContractError("only successful clean public checks may produce reusable dependency artifacts")
    target, output = Path(target), Path(output)
    if output.exists() or target.is_symlink():
        raise ContractError("cache capture paths are not independent/new")
    names, local, external = external_artifacts(target, metadata)
    names, links, omitted, normalized = portable_artifacts(target, names, external, runtime_target)
    payload = output / "payload"
    payload.mkdir(parents=True)
    rows, total = [], 0
    for name in names:
        source, destination = target / name, payload / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        item = {"path": name, "mtime_ns": source.lstat().st_mtime_ns}
        if source.is_symlink():
            destination.symlink_to(links[name])
            item.update(kind="symlink", target=links[name])
        else:
            if not source.is_file():
                raise ContractError("special cache file")
            shutil.copy2(source, destination)
            item.update(kind="file", sha256=file_hash(source), bytes=source.stat().st_size,
                        mode=stat.S_IMODE(source.stat().st_mode))
            total += item["bytes"]
        rows.append(item)
    manifest = {
        "schema_version": entry.SCHEMA, "role": "public-dependencies",
        "producer": "clean-start-public-check", "grader_assets_visible": False,
        "binding_sha256": binding, "files": rows, "bytes": total,
        "excluded_workspace_packages": local, "external_packages": external,
        "metadata_sha256": canonical_hash(metadata), "producer_receipt": producer_receipt,
        "omitted_nonportable_packages": omitted, "normalized_cache_links": normalized,
    }
    (output / entry.MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    entry.validate(output, binding)
    return manifest


def bind(recipe, descriptor, importer):
    """Attach a public readonly seed and its importer before the first model call."""
    if descriptor["public_runtime_binding"] != public_binding(recipe):
        raise ContractError("public cache was built with different source/toolchain/environment inputs")
    if "target" not in recipe.cache_mounts:
        raise ContractError("this dependency seed requires the declared target cache mount")
    if any(a["target"] in {TARGET, TOOL} for a in recipe.assets):
        raise ContractError("public cache seed already attached")
    value = asdict(recipe)
    value["assets"] += [
        {**descriptor["asset"], "kind": "archive", "visibility": "developer", "target": TARGET},
        {**importer, "kind": "file", "visibility": "developer", "target": TOOL},
    ]
    command = shlex.join(["python3", TOOL, "--source", TARGET, "--target", recipe.cache_mounts["target"],
                          "--binding-sha256", descriptor["public_runtime_binding"], "--canonical-archive-modes"])
    value["bootstrap"] = [command, *value["bootstrap"]]
    result = RuntimeRecipe.from_dict(value)
    if result.grading != recipe.grading or result.dev_checks != recipe.dev_checks:
        raise ContractError("cache attachment changed tests or public commands")
    result.validate()
    return result
