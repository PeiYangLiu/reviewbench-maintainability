"""Build fresh generated Python inputs beside an unchanged, complete candidate.

Developer-visible transport code, not an Oracle. No reference revision, passing
test selector, replacement production implementation, or cached SUT is used.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import stat
import sys
import tomllib
import traceback

SCRATCH = ".reviewbench-python-candidate"
SCHEMA = "reviewbench-python-candidate-build/v1"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def relative(value: str) -> str:
    path = Path(value)
    if (not value or path.is_absolute() or path.as_posix() != value
            or any(p in {"..", ".git", ".reviewbench", SCRATCH} for p in path.parts)
            or any(c in value for c in "\0\r\n\\")):
        raise ValueError("invalid candidate-relative path")
    return value


def inventory(root: Path, *, workspace=False) -> dict:
    records = {}
    for directory, directories, files in os.walk(root, followlinks=False):
        if Path(directory) == root and workspace:
            directories[:] = [n for n in directories if n not in {".git", ".reviewbench", SCRATCH}]
            files = [n for n in files if n not in {".git", ".reviewbench", SCRATCH}]
        for name in sorted(files + [n for n in directories if (Path(directory) / n).is_symlink()]):
            path = Path(directory) / name
            key = path.relative_to(root).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                target = os.readlink(path)
                if Path(target).is_absolute() or not path.resolve().is_relative_to(root.resolve()):
                    raise ValueError("candidate symlink escapes source tree: " + key)
                records[key] = {"link": target}
            elif stat.S_ISREG(mode):
                records[key] = {"sha256": digest(path), "executable": bool(mode & 0o111)}
            else:
                raise ValueError("non-regular candidate source: " + key)
    return records


def copy_candidate(workspace: Path) -> tuple[Path, dict]:
    """Preserve all supplied files, including helper modules and public tests."""
    scratch = workspace / SCRATCH
    if scratch.is_symlink():
        raise ValueError("candidate build scratch is a symlink")
    scratch.mkdir(exist_ok=True)
    source = scratch / "source"
    if source.is_symlink():
        raise ValueError("candidate stage is a symlink")
    if source.exists():
        shutil.rmtree(source)
    records = inventory(workspace, workspace=True)
    source.mkdir()
    for name, record in records.items():
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if "link" in record:
            target.symlink_to(record["link"])
        else:
            shutil.copy2(workspace / name, target)
    if inventory(source) != records:
        raise ValueError("complete candidate staging changed input bytes")
    return source, records


def project_metadata(source: Path, project_relative: str, metadata: Path) -> dict:
    """Expose static distribution identity from *current* pyproject.toml only.

    importlib.metadata.version() does not require installing a historic wheel.
    Dynamic metadata is deliberately not guessed. Build/compile remains the
    candidate's own script; this projection supplies no Python implementation.
    """
    project = source / relative(project_relative)
    declaration = project / "pyproject.toml"
    value = tomllib.loads(declaration.read_text())
    fields = value["project"]
    name, version = fields["name"], fields["version"]
    if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name)
            or not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.!+_-]*", version)
            or {"name", "version", "dependencies"} & set(fields.get("dynamic", []))):
        raise ValueError("candidate distribution needs explicit static metadata")
    requirements = fields.get("dependencies", [])
    if (not isinstance(requirements, list)
            or any(not isinstance(s, str) or not s or any(c in s for c in "\r\n\0") for s in requirements)):
        raise ValueError("invalid candidate dependency metadata")
    if metadata.exists():
        if metadata.is_symlink():
            raise ValueError("candidate metadata directory is a symlink")
        shutil.rmtree(metadata)
    dist = metadata / (re.sub(r"[-_.]+", "_", name) + "-" + version + ".dist-info")
    dist.mkdir(parents=True)
    lines = ["Metadata-Version: 2.1", "Name: " + name, "Version: " + version]
    if fields.get("requires-python"):
        requirement = fields["requires-python"]
        if not isinstance(requirement, str) or any(c in requirement for c in "\r\n\0"):
            raise ValueError("invalid candidate interpreter requirement")
        lines.append("Requires-Python: " + requirement)
    lines += ["Requires-Dist: " + s for s in requirements]
    (dist / "METADATA").write_text("\n".join(lines) + "\n")
    return {"project_file": declaration.relative_to(source).as_posix(),
            "project_sha256": digest(declaration), "name": name, "version": version,
            "requirements": requirements, "metadata_sha256": digest(dist / "METADATA")}


def configure(source: Path, project: str, metadata: Path, site: Path) -> None:
    site = site.resolve()
    if not site.is_dir() or site.is_relative_to(source.parent.parent):
        raise ValueError("fixed dependencies must be outside candidate workspace")
    roots = [source / relative(project), source, source / "src", metadata, site]
    if any(not p.resolve().is_relative_to(source) for p in roots[:3]):
        raise ValueError("candidate import root escapes staged source")
    sys.path[:0] = [str(p) for p in roots]
    os.environ["SOURCE_ROOT"] = str(source)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True


def check_origins(source: Path, packages: list[str]) -> dict:
    origins = {}
    for name, module in tuple(sys.modules.items()):
        if not any(name == p or name.startswith(p + ".") for p in packages):
            continue
        path = getattr(module, "__file__", None)
        paths = [path] if path else list(getattr(module, "__path__", []))
        if not paths or any(not Path(p).resolve().is_relative_to(source) for p in paths):
            raise ValueError("production import did not originate in current candidate: " + name)
        origins[name] = [str(Path(p).resolve().relative_to(source)) for p in paths]
    return origins


def check_originals(source: Path, originals: dict) -> None:
    current = inventory(source)
    if any(current.get(name) != value for name, value in originals.items()):
        raise ValueError("candidate compiler overwrote supplied production/test bytes")


def build(workspace: Path, site: Path, project: str, compiler: str, packages: list[str]) -> dict:
    source, originals = copy_candidate(workspace)
    scratch = workspace / SCRATCH
    identity = project_metadata(source, project, scratch / "metadata")
    configure(source, project, scratch / "metadata", site)
    script = source / relative(compiler)
    if not script.is_file() or not script.resolve().is_relative_to(source):
        raise ValueError("candidate compiler is absent or escapes source")
    os.chdir(source / project)
    sys.argv = [str(script)]
    runpy.run_path(str(script), run_name="__main__")
    check_originals(source, originals)
    if inventory(workspace, workspace=True) != originals:
        raise ValueError("candidate workspace changed during private build")
    current = inventory(source)
    record = {"schema_version": SCHEMA, "source": str(source), "inputs": originals,
              "metadata": identity, "compiler": compiler, "compiler_sha256": digest(script),
              "generated": {n: v for n, v in current.items() if n not in originals},
              "import_origins": check_origins(source, packages)}
    (scratch / "build.json").write_text(json.dumps(record, sort_keys=True, indent=2) + "\n")
    # Successful replay scratch is deleted by the unmodified engine. Keep
    # compact, source-free build attestation in its immutable command stdout.
    print("CANDIDATE_BUILD_EVIDENCE=" + json.dumps({
        "schema_version": SCHEMA, "source_files": len(originals),
        "source_tree_sha256": tree_digest(originals), "compiler": compiler,
        "compiler_sha256": record["compiler_sha256"], "metadata": identity,
        "generated": record["generated"],
    }, sort_keys=True), flush=True)
    print("CURRENT_CANDIDATE_BUILD: PASS", flush=True)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["build", "check", "run"])
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--site", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--compiler")
    parser.add_argument("--packages", nargs="+", required=True)
    parser.add_argument("--imports", nargs="*", default=[])
    parser.add_argument("--script", type=Path)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    try:
        if args.operation == "build":
            if not args.compiler:
                raise ValueError("candidate build requires its own compiler")
            build(workspace, args.site, args.project, args.compiler, args.packages)
            return 0
        scratch = workspace / SCRATCH
        record = json.loads((scratch / "build.json").read_text())
        source = scratch / "source"
        if (record.get("schema_version") != SCHEMA or record["source"] != str(source)
                or inventory(workspace, workspace=True) != record["inputs"]):
            raise ValueError("candidate changed since private build")
        check_originals(source, record["inputs"])
        configure(source, args.project, scratch / "metadata", args.site)
        os.chdir(source / args.project)
        if args.operation == "run":
            if not args.script or not args.script.is_file() or args.script.is_symlink():
                raise ValueError("fixed Oracle script is absent")
            # The unchanged Oracle imports its pinned sibling oracle_support.
            sys.path.insert(0, str(args.script.resolve().parent))
            sys.argv = [str(args.script)]
            runpy.run_path(str(args.script), run_name="__main__")
        else:
            for name in args.imports:
                importlib.import_module(name)
        origins = check_origins(source, args.packages)
        if not origins:
            raise ValueError("no current candidate production module was exercised")
        check_originals(source, record["inputs"])
        if inventory(workspace, workspace=True) != record["inputs"]:
            raise ValueError("candidate workspace changed during execution")
        (scratch / (args.operation + "-origins.json")).write_text(json.dumps(origins, sort_keys=True, indent=2) + "\n")
        print("CANDIDATE_IMPORT_EVIDENCE=" + json.dumps({
            "source_tree_sha256": tree_digest(record["inputs"]), "operation": args.operation,
            "origins": origins,
        }, sort_keys=True), flush=True)
        print("CURRENT_CANDIDATE_IMPORTS: PASS", flush=True)
        return 0
    except Exception as exc:
        traceback.print_exc()
        print(f"CURRENT_CANDIDATE_ERROR: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
