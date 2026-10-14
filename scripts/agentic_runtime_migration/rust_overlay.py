"""Attest symmetric Cargo build overlays without replacing candidate source."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import tomllib
import urllib.request
import zipfile

from reviewbench_maintainability.agentic.contracts import (
    ContractError, atomic_json, canonical_hash, file_hash, repo_path,
)
from reviewbench_maintainability.agentic.resources import file_lock
from reviewbench_maintainability.agentic.snapshots import git
from .rust_build_entry import manifest_operations

TOOLS = "/opt/reviewbench-assets/rust-build-tools"
SPEC = "/opt/reviewbench-assets/rust-build-spec.json"
BUILD_CACHE = "/opt/reviewbench-cache/rust-build"
WHEELS = {
    "tomli": {
        "filename": "tomli-2.2.1-py3-none-any.whl",
        "url": "https://files.pythonhosted.org/packages/6e/c2/61d3e0f47e2b74ef40a68b9e6ad5984f6241a942f7cd3bbfbdbd03861ea9/tomli-2.2.1-py3-none-any.whl",
        "sha256": "cb55c73c5f4408779d0cf3eef9f762b9c9f147a77de7b258bef0a5628adc85cc",
    },
    "tomli_w": {
        "filename": "tomli_w-1.2.0-py3-none-any.whl",
        "url": "https://files.pythonhosted.org/packages/c7/18/c86eb8e0202e32dd3df50d43d7ff9854f8e0603945ff398974c1d91ac1ef/tomli_w-1.2.0-py3-none-any.whl",
        "sha256": "188306098d013b691fcadc011abd66727d3c414c571bb01b1a174ba8c983cf90",
    },
}


def session_bytes(ctx, plan, oracle, entry):
    if any(key in entry for key in ("start_content", "golden_content", "start_source", "golden_source")):
        raise ContractError("Rust variant session inputs require a compatibility selector")
    if "content" in entry:
        data = entry["content"].encode()
    elif entry.get("source"):
        data = (ctx.legacy / "rust" / repo_path(entry["source"])).read_bytes()
    else:
        raise ContractError("Rust frozen session input is absent")
    digest = hashlib.sha256(data).hexdigest()
    for variant in ("start", "golden"):
        rows = [row for row in oracle.get("session_files", {}).get(variant, [])
                if row["path"] == entry["path"]]
        if len(rows) != 1 or rows[0]["sha256"] != digest:
            raise ContractError("Rust session input differs from frozen start/golden evidence")
    return data


def lock_prepare(command):
    """Recognize the historical checked-copy operation, not an arbitrary shell."""
    if not command:
        return None
    words = shlex.split(command)
    if (len(words) != 13 or words[:2] != ["printf", "%s  %s\\n"]
            or words[4:11] != ["|", "sha256sum", "-c", "-", ">/dev/null", "&&", "cp"]
            or words[3] != words[11]):
        raise ContractError("Rust preparation needs an explicit non-lock build adapter")
    digest, source, destination = words[2], repo_path(words[3]), repo_path(words[-1])
    if not re.fullmatch(r"[0-9a-f]{64}", digest) or Path(destination).name != "Cargo.lock":
        raise ContractError("Rust checked-copy preparation is not a pinned Cargo lock")
    return {"source": source, "path": destination, "sha256": digest}


def reference_inputs(ctx, task, patch, names):
    start = {}
    for name in names:
        try:
            start[name] = git(ctx.repo(task), "show", task["start_sha"] + ":" + name)
        except ContractError:
            start[name] = None
    with tempfile.TemporaryDirectory(prefix="rust-overlay-reference-", dir=ctx.scratch) as temporary:
        private = Path(temporary)
        git(private, "init", "--bare", "--quiet")
        objects = git(ctx.repo(task), "rev-parse", "--path-format=absolute", "--git-path", "objects").decode().strip()
        (private / "objects/info/alternates").write_text(objects + "\n")
        git(private, "read-tree", task["start_sha"])
        git(private, "apply", "--cached", "--binary", "--whitespace=nowarn",
            input_bytes=Path(patch["path"]).read_bytes())
        golden = {}
        for name in names:
            try:
                golden[name] = git(private, "show", ":" + name)
            except ContractError:
                golden[name] = None
    return {"start": start, "golden": golden}


def build_specification(ctx, task, plan, oracle, patch):
    from . import rust_vfs_resolution
    if rust_vfs_resolution.requested(ctx, task):
        return rust_vfs_resolution.build_specification(ctx, task, plan, oracle, patch)
    if plan.get("test_compatibility_replacements"):
        from .rust_test_compat import validate_profile
        validate_profile(task, plan, oracle)
        from . import rust_wash_compat
        if task.get("id") == rust_wash_compat.TASK:
            return rust_wash_compat.build_specification(ctx, task, plan, oracle, patch)
    if any(plan.get(key) for key in ("start_prepare_command", "golden_prepare_command")):
        raise ContractError("Rust variant dependency preparation needs a candidate graph selector")
    text = plan.get("environment_replacements", [])
    replacements = plan.get("environment_file_replacements", [])
    for item in text:
        if Path(repo_path(item["path"])).name != "Cargo.toml" and item["path"] != ".cargo/config.toml":
            raise ContractError("Rust build overlay must not replace production/test source")
        if any(key.startswith(("start_", "golden_")) for key in item):
            raise ContractError("Rust variant manifest changes need a candidate dependency selector")
    for item in replacements:
        if Path(repo_path(item["path"])).name != "Cargo.lock":
            raise ContractError("whole-file Rust environment defaults must be dependency locks")
        if any(key.startswith(("start_", "golden_")) for key in item):
            raise ContractError("Rust variant lockfiles need a candidate dependency selector")
    prepared = lock_prepare(plan.get("prepare_command"))
    names = {repo_path(item["path"]) for item in [*text, *replacements]}
    if prepared:
        names.add(prepared["path"])
    inputs = reference_inputs(ctx, task, patch, sorted(names))
    outputs = {variant: dict(values) for variant, values in inputs.items()}
    for variant in ("start", "golden"):
        evidence = oracle.get("environment_replacements", {}).get(variant, [])
        for index, item in enumerate(text):
            name, old, new = repo_path(item["path"]), item["old"], item["new"]
            count = item.get("count", 1)
            if not old or type(count) is not int or count < 1 or outputs[variant][name] is None:
                raise ContractError("Rust manifest replacement is not a bounded existing input")
            value = outputs[variant][name].decode()
            if value.count(old) != count:
                raise ContractError("Rust manifest replacement differs from its frozen occurrence count")
            updated = value.replace(old, new, count).encode()
            if (index >= len(evidence) or evidence[index]["path"] != name
                    or evidence[index]["sha256"] != hashlib.sha256(updated).hexdigest()):
                raise ContractError("Rust manifest overlay differs from frozen intermediate evidence")
            outputs[variant][name] = updated
    manifests = []
    for name in sorted({item["path"] for item in text}):
        operations = {}
        for variant in ("start", "golden"):
            before = tomllib.loads(inputs[variant][name].decode())
            after = tomllib.loads(outputs[variant][name].decode())
            operations[variant] = manifest_operations(before, after)
        if canonical_hash(operations["start"]) != canonical_hash(operations["golden"]):
            raise ContractError("Rust manifest overlay is not semantically symmetric")
        if name == ".cargo/config.toml" and any(
                operation["kind"] not in {"remove", "prune_empty"}
                or not operation["key"] or operation["key"][0] not in {"include", "unstable"}
                for operation in operations["start"]):
            raise ContractError("Rust config overlay may only remove its attested nightly include/unstable inputs")
        manifests.append({"path": name, "operations": operations["start"]})
    defaults = {}
    for index, item in enumerate(replacements, start=len(text)):
        name = repo_path(item["path"])
        source = ctx.legacy / "rust" / repo_path(item["source"])
        digest = file_hash(source)
        if item.get("source_sha256") and item["source_sha256"] != digest:
            raise ContractError("Rust default dependency lock checksum changed")
        for variant in ("start", "golden"):
            evidence = oracle.get("environment_replacements", {}).get(variant, [])
            if (index >= len(evidence) or evidence[index]["path"] != name or evidence[index]["sha256"] != digest):
                raise ContractError("Rust default lock differs from frozen environment evidence")
            before = inputs[variant][name]
            if before is None and not item.get("allow_missing"):
                raise ContractError("Rust lock default has no permitted missing-file case")
            if item.get("old_sha256") and (before is None or hashlib.sha256(before).hexdigest() != item["old_sha256"]):
                raise ContractError("Rust lock default differs from its recorded original input")
        defaults[name] = ctx.copy_asset(source, "rust-locks")
    if prepared:
        entries = [item for item in plan.get("session_files", []) if item["path"] == prepared["source"]]
        if len(entries) != 1:
            raise ContractError("checked-copy Rust lock has no unambiguous session input")
        data = session_bytes(ctx, plan, oracle, entries[0])
        if hashlib.sha256(data).hexdigest() != prepared["sha256"]:
            raise ContractError("checked-copy Rust lock differs from the frozen command")
        source = ctx.output / "rust-prepare-locks" / (prepared["sha256"] + "-Cargo.lock")
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(data)
        defaults[prepared["path"]] = ctx.copy_asset(source, "rust-locks")
    locks = []
    for index, (name, asset) in enumerate(sorted(defaults.items())):
        locks.append({
            "path": name, "asset_target": "/opt/reviewbench-assets/rust-lock-" + str(index),
            "sha256": asset["sha256"],
            "replace_if_sha256": sorted({hashlib.sha256(inputs[v][name]).hexdigest()
                                        for v in ("start", "golden") if inputs[v][name] is not None}),
        })
    private_members = [str(Path(item["path"]).parent) for item in plan.get("session_files", [])
                       if Path(item["path"]).name == "Cargo.toml"]
    spec = {"schema_version": "reviewbench-cargo-build-overlay/v1", "task_id": task["id"],
            "manifests": manifests, "locks": locks, "private_members": private_members, "cpus": 4}
    from .rust_resolution import add_recorded_resolution
    add_recorded_resolution(ctx, task, plan, oracle, patch, spec, defaults)
    from .rust_retained_resolution import add_retained_resolution
    add_retained_resolution(ctx, task, plan, oracle, patch, spec, defaults)
    return spec, defaults


def build_tools(ctx, *, extra_entries=None):
    """Ship pure Python TOML support for the unchanged Python-3.10 base image."""
    if importlib.metadata.version("tomli-w") != "1.2.0":
        raise ContractError("Cargo overlay preparation requires pinned tomli-w 1.2.0")
    extra_entries = extra_entries or {}
    for name, source in extra_entries.items():
        if (Path(name).name != name or not name.endswith(".py")
                or name in {"build.py", "locks.py"} or Path(source).is_symlink()
                or Path(source).parent != Path(__file__).parent):
            raise ContractError("portable Cargo entry must be a sibling preparation module")
    # Task-specific companions must not change a later ordinary caller's bundle.
    suffix = ("-" + canonical_hash({name: file_hash(Path(source))
                                   for name, source in extra_entries.items()})[:20]) if extra_entries else ""
    root = ctx.output / ("rust-build-tools" + suffix)
    with file_lock(ctx.output / "locks" / root.name):
        root.mkdir(exist_ok=True)
        shutil.copyfile(Path(__file__).with_name("rust_build_entry.py"), root / "build.py")
        shutil.copyfile(Path(__file__).with_name("rust_lock_entry.py"), root / "locks.py")
        for name, source in extra_entries.items():
            shutil.copyfile(source, root / name)
        for package, record in WHEELS.items():
            wheel = ctx.output / "python-tool-wheels" / record["filename"]
            if not wheel.is_file() or file_hash(wheel) != record["sha256"]:
                wheel.parent.mkdir(exist_ok=True)
                with ctx.cache.transfers.acquire():
                    with urllib.request.urlopen(record["url"], timeout=60) as response:
                        data = response.read(1024 * 1024 + 1)
                if len(data) > 1024 * 1024 or hashlib.sha256(data).hexdigest() != record["sha256"]:
                    raise ContractError("portable Cargo TOML dependency differs from its pinned wheel")
                wheel.write_bytes(data)
            with zipfile.ZipFile(wheel) as archive:
                for name in archive.namelist():
                    if name.startswith(package + "/") and name.endswith((".py", "py.typed")):
                        destination = root / repo_path(name)
                    elif name.endswith("/LICENSE"):
                        destination = root / "licenses" / (package + "-LICENSE")
                    else:
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(archive.read(name))
        atomic_json(root / "dependency-pins.json", WHEELS)
        return ctx.bundle(root, role="toolchain")


def command_wrapper(command, cwd):
    return shlex.join(["python3", TOOLS + "/build.py", "--spec", SPEC, "--cwd", cwd or ".", "--", *command])


def cargo_cli_prefix(ctx, version):
    """Probe the pinned Cargo instead of silently upgrading an old compiler."""
    registry = Path(ctx.config.get("rust_toolchains", ctx.output.parent / "rust-toolchains/toolchains.json"))
    tool = Path(json.loads(registry.read_text())[version]["root"])
    with tempfile.TemporaryDirectory(prefix="cargo-cli-probe-", dir=ctx.scratch) as temporary:
        root = Path(temporary)
        manifest = root / "Cargo.toml"
        manifest.write_text('[package]\nname="reviewbench-cli-probe"\nversion="0.0.0"\n')
        command = [str(tool / "bin/cargo"), "--offline", "--config", "net.offline=true",
                   "locate-project", "--manifest-path", str(manifest)]
        first = subprocess.run(command, capture_output=True, text=True, timeout=30)
        prefix = []
        probes = [{"command": command, "stdout": first.stdout, "stderr": first.stderr, "exit_status": first.returncode}]
        if first.returncode and "the `--config` flag is unstable" in first.stderr and version.startswith("nightly-"):
            prefix = ["-Z", "unstable-options"]
            command = [command[0], *prefix, *command[1:]]
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            probes.append({"command": command, "stdout": result.stdout,
                           "stderr": result.stderr, "exit_status": result.returncode})
        else:
            result = first
        if result.returncode:
            raise ContractError("pinned Cargo has no verified source-config command syntax: " + result.stderr[-1000:])
        atomic_json(ctx.output / "rust-cli-probes" / (version + ".json"), {"toolchain": version, "probes": probes})
        return prefix


def portable_environment(ctx, plan, base):
    additions, bootstrap = {}, []
    for key, value in plan.get("env", {}).items():
        if key == "RUSTUP_HOME":
            old = ctx.legacy / "rust/cache"
            path = Path(value)
            if not path.is_absolute() or not path.is_relative_to(old) or not path.name.startswith("rustup-"):
                raise ContractError("Rust version-manager home has no known isolated toolchain binding")
            home = "/workspace/.reviewbench/rustup"
            toolchain = plan["toolchain"] + "-x86_64-unknown-linux-gnu"
            settings = ('version = "12"\ndefault_host_triple = "x86_64-unknown-linux-gnu"\n'
                        'default_toolchain = ' + json.dumps(toolchain) + '\nprofile = "minimal"\n[overrides]\n')
            additions["RUSTUP_HOME"] = home
            bootstrap.extend([
                "mkdir -p " + home + "/toolchains",
                "ln -s /opt/reviewbench-assets/rust " + shlex.quote(home + "/toolchains/" + toolchain),
                "printf %s " + shlex.quote(settings) + " > " + home + "/settings.toml",
            ])
        elif (key in base or not isinstance(value, str)
              or any(prefix in value for prefix in ("/data/", "/home/", "{root}", "/work/"))):
            raise ContractError("Rust environment input needs a portable explicit binding: " + key)
        else:
            additions[key] = value
    return additions, bootstrap


def production_package(ctx, task):
    anchor = Path(repo_path(task["locality"]["path"]))
    for parent in [anchor.parent, *anchor.parent.parents]:
        name = (parent / "Cargo.toml").as_posix()
        try:
            data = tomllib.loads(git(ctx.repo(task), "show", task["start_sha"] + ":" + name).decode())
        except ContractError:
            continue
        package = data.get("package", {}).get("name")
        if isinstance(package, str):
            return package
    raise ContractError("Rust public build has no candidate production package")
