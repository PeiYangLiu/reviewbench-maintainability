"""Read-only, exact-revision projection of the retained Rippled Conan caches.

Only recipe exports and manifested external packages are transferable. Conan
source/build folders, credentials, consumers and historical task binaries are
not cache inputs. This module is also usable by the bounded volume exporter.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3


REF = r"[a-z0-9][a-z0-9+_.-]*/[A-Za-z0-9+_.-]+"
REV = r"[0-9a-f]{32}"
PID = r"[0-9a-f]{40}"
MAX_RECIPES = 48
MAX_FILES = 120000
MAX_BYTES = 12 * 1024**3
EXTERNAL = frozenset((
    "abseil", "b2", "boost", "bzip2", "c-ares", "cmake", "date", "doctest",
    "ed25519", "grpc", "gtest", "libarchive", "libbacktrace", "lz4", "mpt-crypto",
    "nudb", "openssl", "protobuf", "re2", "rocksdb", "secp256k1", "snappy",
    "soci", "sqlite3", "xxhash", "zlib",
))


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def relative(name):
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or ".." in path.parts or str(path) != name
            or "\\" in name or "\0" in name):
        raise ValueError("Conan input contains a noncanonical relative path: " + repr(name))
    return path


def external(ref):
    if not re.fullmatch(REF, ref) or ref.split("/")[0] not in EXTERNAL:
        raise ValueError("Conan closure contains an undeclared external package or consumer: " + ref)
    return ref


def _one(mapping, key, value, label):
    if key in mapping and mapping[key] != value:
        raise ValueError("ambiguous original Conan " + label + ": " + str(key))
    mapping[key] = value


def graph_from_log(text, major):
    """Use completed package records, never 'main binary missing' guesses."""
    if len(text) > 32 * 1024**2 or major not in (1, 2):
        raise ValueError("unsupported Conan log format or bound")
    recipes, packages, created = {}, {}, {}
    context = None
    for line in text.splitlines():
        if major == 2:
            if line in ("Requirements", "Test requirements", "Build requirements"):
                context = {"Requirements": "host", "Test requirements": "test",
                           "Build requirements": "build"}[line]
                continue
            match = re.fullmatch(rf"    ({REF})#({REV})(?::({PID})(?:#({REV}))?)? - (\w+)(?: .*)?", line)
            if match:
                ref, rrev, pkgid, prev, action = match.groups()
                external(ref)
                _one(recipes, ref, rrev, "recipe revision")
                if pkgid:
                    if context is None or action not in {"Build", "Cache", "Download"}:
                        raise ValueError("unsupported original Conan package status")
                    key = (ref, rrev, pkgid)
                    row = packages.setdefault(key, {"prev": prev, "contexts": []})
                    if row["prev"] and prev and row["prev"] != prev:
                        raise ValueError("ambiguous original Conan package revision")
                    row["prev"] = row["prev"] or prev
                    if context not in row["contexts"]:
                        row["contexts"].append(context)
                continue
            match = re.fullmatch(rf"({REF}): Full package reference: ({REF})#({REV}):({PID})#({REV})", line)
            if match:
                if match[1] != match[2]:
                    raise ValueError("Conan completed package reference disagrees with its owner")
                _one(created, (match[2], match[3], match[4]), match[5], "built package revision")
        else:
            match = re.fullmatch(rf"({REF}): Downloaded recipe revision ({REV})", line)
            if match:
                _one(recipes, external(match[1]), match[2], "recipe revision")
                continue
            if line in ("Packages", "Build requirements packages"):
                context = "host" if line == "Packages" else "build"
                continue
            match = re.fullmatch(rf"    ({REF}):({PID}) - (Build|Download|Cache)", line)
            if match:
                if context is None:
                    raise ValueError("Conan 1 package has no original context")
                key = (external(match[1]), recipes.get(match[1]), match[2])
                if not key[1]:
                    raise ValueError("Conan 1 log lacks the original recipe revision")
                row = packages.setdefault(key, {"prev": None, "contexts": []})
                if context not in row["contexts"]:
                    row["contexts"].append(context)
                continue
            match = re.fullmatch(rf"({REF}): (?:Created|Downloaded) package revision ({REV})", line)
            if match:
                keys = [key for key in packages if key[0] == match[1]]
                if len(keys) != 1:
                    raise ValueError("Conan 1 revision lacks a unique original package ID")
                _one(created, keys[0], match[2], "package revision")
    if not recipes or len(recipes) > MAX_RECIPES or not packages or len(packages) > MAX_RECIPES * 2:
        raise ValueError("original Conan graph is empty or exceeds the package bound")
    for key, row in packages.items():
        if key in created:
            if row["prev"] and row["prev"] != created[key]:
                raise ValueError("Conan install changed a previously selected revision")
            row["prev"] = created[key]
        if not row["prev"]:
            raise ValueError("original Conan package has no completed revision: " + str(key))
    if set(created) - set(packages):
        raise ValueError("original Conan log built packages outside the selected graph")
    return {
        "major": major,
        "recipes": [{"ref": ref, "rrev": rev} for ref, rev in sorted(recipes.items())],
        "packages": [{"ref": key[0], "rrev": key[1], "package_id": key[2],
                      "prev": row["prev"], "contexts": sorted(row["contexts"])}
                     for key, row in sorted(packages.items())],
    }


def manifest(text):
    lines = text.splitlines()
    if not lines or not lines[0].isdigit():
        raise ValueError("Conan manifest lacks its timestamp")
    result = {}
    for line in lines[1:]:
        if not line:
            continue
        try:
            name, digest = line.rsplit(": ", 1)
        except ValueError as exc:
            raise ValueError("malformed Conan manifest record") from exc
        relative(name)
        if name in result or not re.fullmatch(REV, digest):
            raise ValueError("duplicate or malformed Conan manifest entry")
        result[name] = digest
    if not result or len(result) > MAX_FILES:
        raise ValueError("Conan manifest is empty or exceeds its bound")
    encoded = "".join(name + ": " + result[name] + "\n" for name in sorted(result)).encode()
    return result, hashlib.md5(encoded).hexdigest()


def _files(root):
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Conan input directory is absent or linked: " + str(root))
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in [*dirs, *files]:
            path = Path(directory) / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                link = os.readlink(path)
                if Path(link).is_absolute() or not path.resolve().is_relative_to(root.resolve()):
                    raise ValueError("Conan package link escapes its own export/package: " + rel)
                result[rel] = path
            elif path.is_file():
                result[rel] = path
            elif not path.is_dir():
                raise ValueError("Conan package contains a special file: " + rel)
        dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()]
        if len(result) > MAX_FILES:
            raise ValueError("Conan file count exceeds its bound")
    return result


def checked_payload(folder, revision, export_source=None, *, missing_exports=None, conan_major=2):
    """Attest every copied byte to the original RREV/PREV, including symlinks."""
    marker = folder / "conanmanifest.txt"
    if marker.is_symlink() or not marker.is_file():
        raise ValueError("Conan package manifest is missing or linked")
    expected, digest = manifest(marker.read_text())
    if digest != revision:
        raise ValueError("Conan manifest does not match the original revision: " + str(folder))
    files = _files(folder)
    if export_source and export_source.exists():
        files.update({"export_source/" + name: path for name, path in _files(export_source).items()})
    ignored = {"conanmanifest.txt", "conan_package.tgz", "conan_export.tgz", "conan_sources.tgz"}
    # Python cache files are interpreter byproducts, not exports in either Conan.
    extra = set(files) - set(expected) - ignored
    extra = {name for name in extra if "__pycache__" not in PurePosixPath(name).parts
             and not name.endswith(".pyc") and not files[name].is_symlink()}
    if extra:
        raise ValueError("Conan package contains unmanifested input files: " + repr(sorted(extra)[:5]))
    records, total = [], 0
    for name in sorted(set(expected) | {"conanmanifest.txt"}):
        path = files.get(name)
        if path is None:
            if name.startswith("export_source/") and missing_exports is not None:
                # Conan does not download export_source for cached binaries.
                # Its declared MD5 remains bound by RREV; it is not an input to
                # --build=never. Record absence rather than inventing source.
                missing_exports.append({"path": name, "md5": expected[name],
                                        "reason": "unmaterialized source-build-only export; binary-only cache"})
                continue
            raise ValueError("Conan manifested input is absent: " + name)
        if path.is_symlink():
            data = os.readlink(path).encode()
            md5, sha, size = hashlib.md5(data).hexdigest(), hashlib.sha256(data).hexdigest(), len(data)
            if conan_major == 1:
                # Conan 1.60 FileTreeManifest follows internal file links;
                # Conan 2 hashes link text. Always SHA-bind the actual link too.
                with path.open("rb") as stream:
                    md5 = hashlib.file_digest(stream, "md5").hexdigest()
            kind = "symlink"
        else:
            md5_hash, sha_hash, size = hashlib.md5(), hashlib.sha256(), 0
            with path.open("rb") as stream:
                while chunk := stream.read(1024**2):
                    md5_hash.update(chunk)
                    sha_hash.update(chunk)
                    size += len(chunk)
            md5, sha, kind = md5_hash.hexdigest(), sha_hash.hexdigest(), "file"
        total += size
        if total > MAX_BYTES:
            raise ValueError("Conan payload exceeds its byte bound")
        if name in expected and md5 != expected[name]:
            raise ValueError("Conan file differs from the original manifest: " + str(path))
        records.append({"path": name, "source": str(path), "sha256": sha, "kind": kind,
                        "bytes": size, "executable": bool(path.stat().st_mode & 0o111) if kind == "file" else False})
    # Conan omits directory links from its MD5 manifest. Keep internal directory
    # links, separately SHA-bound; do not silently drop part of a public package.
    for name in sorted(set(files) - set(expected) - ignored):
        path = files[name]
        if path.is_symlink():
            if not path.is_dir():
                raise ValueError("unmanifested Conan file link: " + name)
            data = os.readlink(path).encode()
            records.append({"path": name, "source": str(path), "sha256": hashlib.sha256(data).hexdigest(),
                            "kind": "symlink", "bytes": len(data), "executable": False})
    return records


def copy_payload(records, target, *, export_source=None):
    for row in records:
        name = row["path"]
        if name.startswith("export_source/"):
            if export_source is None:
                raise ValueError("recipe export_source destination is missing")
            destination = export_source / name.removeprefix("export_source/")
        else:
            destination = target / relative(name)
        source = Path(row["source"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        if row["kind"] == "symlink":
            value = os.readlink(source)
            if hashlib.sha256(value.encode()).hexdigest() != row["sha256"]:
                raise ValueError("Conan link changed during projection")
            destination.symlink_to(value)
        else:
            shutil.copyfile(source, destination)
            destination.chmod(0o755 if row["executable"] else 0o644)
            if sha256(destination) != row["sha256"]:
                raise ValueError("Conan bytes changed during projection")


def modern_cache(source, graph, target=None):
    """Project exact SQLite rows into a new cache; never open the old DB RW."""
    if graph["major"] != 2 or target is not None and target.exists():
        raise ValueError("Conan 2 projection needs a new destination")
    database = source / "p/cache.sqlite3"
    before = sha256(database)
    original = sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)
    original.row_factory = sqlite3.Row
    out = None
    if target is not None:
        target.mkdir(parents=True)
        (target / "p").mkdir()
        out = sqlite3.connect(target / "p/cache.sqlite3")
    columns = {
        "recipes": ("reference", "rrev", "path", "timestamp", "lru"),
        "packages": ("reference", "rrev", "pkgid", "prev", "path", "timestamp", "build_id", "lru"),
    }
    definitions = {
        "recipes": "reference text NOT NULL, rrev text NOT NULL, path text NOT NULL UNIQUE, "
                   "timestamp real NOT NULL, lru integer NOT NULL, UNIQUE(reference, rrev)",
        "packages": "reference text NOT NULL, rrev text NOT NULL, pkgid text, prev text, path text NOT NULL UNIQUE, "
                    "timestamp real NOT NULL, build_id text, lru integer NOT NULL, UNIQUE(reference, rrev, pkgid, prev)",
    }
    for table in columns:
        if tuple(row["name"] for row in original.execute("pragma table_info(" + table + ")")) != columns[table]:
            raise ValueError("retained Conan 2 database schema differs from 2.17")
        if out:
            out.execute("CREATE TABLE " + table + " (" + definitions[table] + ")")
    evidence, payloads, total = [], [], 0
    for table, values in (("recipes", graph["recipes"]), ("packages", graph["packages"])):
        for value in values:
            fields = ["reference", "rrev"] + (["pkgid", "prev"] if table == "packages" else [])
            args = [value["ref"], value["rrev"]] + (
                [value["package_id"], value["prev"]] if table == "packages" else [])
            rows = list(original.execute("SELECT * FROM " + table + " WHERE "
                                         + " AND ".join(k + "=?" for k in fields), args))
            if len(rows) != 1:
                raise ValueError("exact retained Conan record is missing: " + repr(value))
            row = dict(rows[0])
            relative(row["path"])
            directory = source / "p" / row["path"]
            missing_exports = []
            if table == "recipes":
                records = checked_payload(directory / "e", value["rrev"], directory / "es",
                                          missing_exports=missing_exports)
                if out:
                    copy_payload(records, target / "p" / row["path"] / "e",
                                 export_source=target / "p" / row["path"] / "es")
            else:
                records = checked_payload(directory / "p", value["prev"])
                if out:
                    copy_payload(records, target / "p" / row["path"] / "p")
            total += sum(item["bytes"] for item in records)
            if total > MAX_BYTES:
                raise ValueError("Conan cache projection exceeds its byte bound")
            if out:
                out.execute("INSERT INTO " + table + " VALUES (" + ",".join("?" for _ in columns[table]) + ")",
                            [row[column] for column in columns[table]])
            evidence.append({**value, "path": row["path"], "files": len(records),
                             "bytes": sum(item["bytes"] for item in records),
                             "unmaterialized_source_exports": missing_exports})
            payloads.extend({**item, "cache_entry": row["path"]} for item in records)
            if len(payloads) > MAX_FILES:
                raise ValueError("Conan cache projection exceeds its file bound")
    if out:
        out.commit()
        out.close()
    original.close()
    if sha256(database) != before:
        raise ValueError("retained Conan database changed during its read-only projection")
    return {"database_sha256": before, "entries": evidence, "files": payloads, "bytes": total}


def legacy_cache(source, graph, target=None):
    """Conan 1.60 metadata projection, including original recipe_revision ties."""
    if graph["major"] != 1 or target is not None and target.exists():
        raise ValueError("Conan 1 projection needs a new destination")
    if target is not None:
        target.mkdir(parents=True)
    evidence, payloads, total = [], [], 0
    for recipe in graph["recipes"]:
        name, version = external(recipe["ref"]).split("/")
        prefix = Path("data") / name / version / "_" / "_"
        directory = source / prefix
        metadata_file = directory / "metadata.json"
        if metadata_file.is_symlink() or not metadata_file.is_file():
            raise ValueError("Conan 1 original metadata is missing")
        metadata = json.loads(metadata_file.read_text())
        if metadata.get("recipe", {}).get("revision") != recipe["rrev"]:
            raise ValueError("Conan 1 retained recipe revision differs")
        missing_exports = []
        records = checked_payload(directory / "export", recipe["rrev"], directory / "export_source",
                                  missing_exports=missing_exports, conan_major=1)
        if target is not None:
            copy_payload(records, target / prefix / "export", export_source=target / prefix / "export_source")
        evidence.append({**recipe, "path": str(prefix / "export"), "files": len(records),
                         "unmaterialized_source_exports": missing_exports})
        selected = {}
        for package in [row for row in graph["packages"] if row["ref"] == recipe["ref"]]:
            pid = package["package_id"]
            record = metadata.get("packages", {}).get(pid, {})
            if record.get("revision") != package["prev"] or record.get("recipe_revision") != package["rrev"]:
                raise ValueError("Conan 1 retained binary revision/configuration differs: " + recipe["ref"])
            items = checked_payload(directory / "package" / pid, package["prev"], conan_major=1)
            if target is not None:
                copy_payload(items, target / prefix / "package" / pid)
            selected[pid] = record
            total += sum(item["bytes"] for item in items)
            evidence.append({**package, "path": str(prefix / "package" / pid), "files": len(items)})
            payloads.extend({**item, "cache_entry": str(prefix / "package" / pid)} for item in items)
        metadata = {"recipe": metadata["recipe"], "packages": selected}
        if target is not None:
            (target / prefix / "metadata.json").write_text(json.dumps(metadata, sort_keys=True) + "\n")
        total += sum(item["bytes"] for item in records)
        payloads.extend({**item, "cache_entry": str(prefix / "export")} for item in records)
        if total > MAX_BYTES or len(payloads) > MAX_FILES:
            raise ValueError("Conan 1 projection exceeds its payload bound")
    return {"entries": evidence, "files": payloads, "bytes": total}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--graph", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    args = parser.parse_args(argv)
    graph = json.loads(args.graph.read_text())
    method = modern_cache if graph["major"] == 2 else legacy_cache
    value = method(args.source, graph, args.target)
    for name in ("settings.yml", "version.txt"):
        source = args.source / name
        if source.is_symlink() or not source.is_file():
            raise ValueError("original Conan configuration is missing: " + name)
        shutil.copyfile(source, args.target / name)
    value.update(graph=graph, graph_sha256=sha256(args.graph), old_candidate_artifacts_copied=False,
                 source_cache_modified=False, dependency_builds=0)
    args.audit.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    print(json.dumps({"status": "external_payloads_verified", "files": len(value["files"]),
                      "bytes": value["bytes"], "audit": str(args.audit)}, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
