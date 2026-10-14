"""Verified cold retirement of terminal pytest fixtures, not benchmark caches."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import uuid

sys.path.insert(0, os.environ.get("REVIEWBENCH_ENGINE_SRC", str(Path(__file__).resolve().parents[2] / "src")))
from reviewbench_maintainability.agentic.contracts import ContractError, atomic_json, canonical_hash, file_hash

SCHEMA = "reviewbench-terminal-test-fixture/v1"
DEFAULT_LIMITS = {
    "max_nodes": 200000, "max_file_bytes": 64 * 1024**2,
    "max_manifest_bytes": 128 * 1024**2, "max_archive_bytes": 512 * 1024**2,
}
SHAPE = ("path", "type", "mode", "mtime_ns", "size", "sha256", "link", "group")

# sd-pam/sshd may be non-dumpable even for the owner UID. The authorized
# system-Python probe reads only kernel process/FD metadata for those exact
# already-observed owner processes. It never reads a workspace file or mutates it.
PROC_METADATA_SOURCE = r"""
import json,os,pathlib,sys
value=json.load(sys.stdin)
assert int(os.environ.get('SUDO_UID','-1')) == value['owner_uid']
assert 0 < len(value['pids']) <= 64 and len(value['inodes']) <= 200000
root=value['source_root']; assert root.startswith('/') and root != '/'
inodes={tuple(row) for row in value['inodes']}
refs=[];errors=[];checked=[]
for name,expected in sorted(value['pids'].items()):
 assert name.isdigit() and int(name)>0
 proc=pathlib.Path('/proc')/name
 try:
  start=(proc/'stat').read_text().rsplit(')',1)[1].split()[19]
  if start != expected: raise ValueError('process identity changed during metadata probe')
  uid=next(line for line in (proc/'status').read_text().splitlines() if line.startswith('Uid:'))
  if int(uid.split()[2]) != value['owner_uid']: raise ValueError('process UID changed during metadata probe')
  for entry in [proc/'cwd',proc/'root',*list((proc/'fd').iterdir())]:
   try:
    target=os.readlink(entry);st=entry.stat()
    if target==root or target.startswith(root+'/') or (st.st_dev,st.st_ino) in inodes:
     refs.append({'pid':int(name),'handle':str(entry),'target':target})
   except (FileNotFoundError,ProcessLookupError): pass
  command=(proc/'cmdline').read_bytes().replace(b'\0',b' ').decode(errors='replace')
  if root in command: refs.append({'pid':int(name),'handle':'cmdline','target':root})
 except (FileNotFoundError,ProcessLookupError): pass
 except Exception as error: errors.append(type(error).__name__+': '+str(error))
 checked.append(int(name))
print(json.dumps({'checked_pids':checked,'references':refs,'observation_errors':errors,
                  'workspace_file_contents_read':0,'modifications':0}))
"""

DIRECTORY_ACCESS_SOURCE = r"""
import json,os,pathlib,re,stat,sys
v=json.load(sys.stdin);actor=int(os.environ.get('SUDO_UID','-1'))
assert actor==v['owner_uid'] and actor>0
root=pathlib.Path(v['root'])
assert root.is_absolute() and root.resolve()==root
assert re.fullmatch(r'\.retiring-.+-[0-9a-f]{32}',root.name)
assert 0<len(v['directories'])<=512
base=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
opened=[];changed=[]
try:
 st=os.fstat(base)
 assert (st.st_dev,st.st_ino,st.st_uid)==(v['root_dev'],v['root_ino'],actor)
 for row in v['directories']:
  name=row['path'];parts=name.split('/')
  assert name and not name.startswith('/') and all(p not in ('','.','..') for p in parts)
  fd=os.dup(base)
  try:
   for part in parts:
    nxt=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
    os.close(fd);fd=nxt
   info=os.fstat(fd)
   assert (info.st_dev,info.st_ino,info.st_uid,info.st_gid,stat.S_IMODE(info.st_mode))==(
       row['dev'],row['ino'],row['uid'],row['gid'],row['mode'])
   assert info.st_uid!=actor
   opened.append((row,fd));fd=None
  finally:
   if fd is not None:os.close(fd)
 try:
  for row,fd in opened:
   changed.append((row,fd))
   os.fchown(fd,actor,-1)
   os.fchmod(fd,row['mode']|0o700)
 except Exception:
  for row,fd in reversed(changed):
   os.fchown(fd,row['uid'],row['gid']);os.fchmod(fd,row['mode'])
  raise
 print(json.dumps({'directories_changed':[row['path'] for row,fd in opened],
                   'root_dev':st.st_dev,'root_ino':st.st_ino,
                   'file_content_changes':0,'outside_root_changes':0}))
finally:
 for row,fd in opened:os.close(fd)
 os.close(base)
"""


def now():
    return datetime.now(timezone.utc).isoformat()


def relative(name):
    if (not isinstance(name, str) or not name or "\0" in name or name.startswith("/")
            or any(part in {"", ".", ".."} for part in name.split("/"))):
        raise ContractError("invalid fixture member name")
    return name


def node_type(mode):
    for predicate, kind in ((stat.S_ISDIR, "directory"), (stat.S_ISREG, "file"),
                            (stat.S_ISLNK, "symlink"), (stat.S_ISFIFO, "fifo"),
                            (stat.S_ISSOCK, "socket")):
        if predicate(mode):
            return kind
    raise ContractError("fixture contains a device or unsupported node type")


def identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_nlink)


def hash_fd(descriptor):
    digest = hashlib.sha256()
    while True:
        data = os.read(descriptor, 1024**2)
        if not data:
            return digest.hexdigest()
        digest.update(data)


def traversal_error(error):
    raise error


def open_beneath(root, name):
    parts = relative(name).split("/")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    finally:
        os.close(fd)


def scan_tree(root, limits=None):
    """Walk using directory descriptors; never open a FIFO or follow a link."""
    limits = {**DEFAULT_LIMITS, **(limits or {})}
    root = Path(root)
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    rows, by_inode = [], {}
    try:
        first = os.fstat(descriptor)
        rows.append({"path": ".", "type": "directory", "mode": stat.S_IMODE(first.st_mode),
                     "mtime_ns": first.st_mtime_ns, "dev": first.st_dev, "ino": first.st_ino,
                     "allocated": first.st_blocks * 512, "uid": first.st_uid, "gid": first.st_gid})
        for directory, dirs, files, fd in os.fwalk(
                ".", dir_fd=descriptor, follow_symlinks=False, onerror=traversal_error):
            dirs.sort()
            files.sort()
            prefix = "" if directory == "." else directory.removeprefix("./") + "/"
            for name in sorted([*dirs, *files]):
                if len(rows) >= limits["max_nodes"]:
                    raise ContractError("fixture node bound exceeded")
                path = relative(prefix + name)
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                kind = node_type(info.st_mode)
                row = {"path": path, "type": kind, "mode": stat.S_IMODE(info.st_mode),
                       "mtime_ns": info.st_mtime_ns, "dev": info.st_dev, "ino": info.st_ino,
                       "allocated": info.st_blocks * 512, "uid": info.st_uid, "gid": info.st_gid}
                if kind == "file":
                    if info.st_size > limits["max_file_bytes"]:
                        raise ContractError("fixture regular-file bound exceeded")
                    stream = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                    try:
                        if identity(os.fstat(stream)) != identity(info):
                            raise ContractError("fixture file changed before hashing")
                        digest = hash_fd(stream)
                        if identity(os.fstat(stream)) != identity(info):
                            raise ContractError("fixture file changed while hashing")
                    finally:
                        os.close(stream)
                    key = (info.st_dev, info.st_ino)
                    row.update(size=info.st_size, sha256=digest, nlink=info.st_nlink)
                    by_inode.setdefault(key, []).append(row)
                elif kind == "symlink":
                    row["link"] = os.readlink(name, dir_fd=fd)
                    if row["mode"] != 0o777:
                        raise ContractError("fixture symlink mode is not reproducible on this host")
                rows.append(row)
        if identity(os.fstat(descriptor)) != identity(first):
            raise ContractError("fixture root changed during scan")
    finally:
        os.close(descriptor)
    for group in by_inode.values():
        representative = min(row["path"] for row in group)
        for row in group:
            row["group"] = representative
    rows.sort(key=lambda row: row["path"])
    unique = {(row["dev"], row["ino"]): row for row in rows}
    exclusive = sum(row["allocated"] for row in unique.values()
                    if row["type"] != "file" or row["nlink"] == len(by_inode[(row["dev"], row["ino"])]))
    result = {
        "schema_version": SCHEMA, "source_root": str(root.absolute()), "entries": rows,
        "counts": dict(Counter(row["type"] for row in rows)),
        "logical_bytes": sum(row.get("size", 0) for row in rows),
        "allocated_bytes": sum(row["allocated"] for row in unique.values()),
        "exclusive_allocated_bytes": exclusive,
        "preserves": ["bytes", "modes", "mtime", "node_types", "literal_symlink_targets", "in_tree_hardlinks"],
    }
    validate_manifest(result, limits)
    return result


def validate_manifest(manifest, limits=None):
    limits = {**DEFAULT_LIMITS, **(limits or {})}
    if manifest.get("schema_version") != SCHEMA:
        raise ContractError("unknown fixture archive schema")
    rows = manifest.get("entries")
    if not isinstance(rows, list) or not 1 <= len(rows) <= limits["max_nodes"]:
        raise ContractError("invalid fixture entry count")
    by_name, groups = {}, {}
    for row in rows:
        name = row.get("path")
        if name != ".":
            relative(name)
        if name in by_name or row.get("type") not in {"directory", "file", "symlink", "fifo", "socket"}:
            raise ContractError("duplicate or unsupported fixture member")
        if type(row.get("mode")) is not int or not 0 <= row["mode"] <= 0o7777:
            raise ContractError("invalid fixture mode")
        if type(row.get("mtime_ns")) is not int:
            raise ContractError("invalid fixture modification time")
        if row["type"] == "file":
            if (type(row.get("size")) is not int or not 0 <= row["size"] <= limits["max_file_bytes"]
                    or not re.fullmatch("[0-9a-f]{64}", str(row.get("sha256", "")))):
                raise ContractError("invalid fixture payload descriptor")
            relative(row["group"])
            shape = (row["sha256"], row["size"], row["mode"], row["mtime_ns"])
            if row["group"] in groups and groups[row["group"]] != shape:
                raise ContractError("inconsistent fixture hardlink group")
            groups[row["group"]] = shape
        elif row["type"] == "symlink":
            if not isinstance(row.get("link"), str) or "\0" in row["link"] or row["mode"] != 0o777:
                raise ContractError("invalid literal fixture symlink")
        by_name[name] = row
    if by_name.get(".", {}).get("type") != "directory":
        raise ContractError("fixture archive lacks its directory root")
    for name, row in by_name.items():
        if name == ".":
            continue
        parent = str(PurePosixPath(name).parent)
        if by_name.get(parent, {}).get("type") != "directory":
            raise ContractError("fixture path has a non-directory or absent parent")
        if row["type"] == "file":
            target = by_name.get(row["group"], {})
            if target.get("type") != "file" or target.get("group") != row["group"]:
                raise ContractError("fixture hardlink representative is absent")
    return by_name


def shape(rows):
    return [{key: row[key] for key in SHAPE if key in row} for row in rows]


class LimitedWriter:
    def __init__(self, stream, maximum):
        self.stream, self.maximum, self.written = stream, maximum, 0

    def write(self, data):
        if self.written + len(data) > self.maximum:
            raise ContractError("fixture compressed archive bound exceeded")
        count = self.stream.write(data)
        self.written += count
        return count

    def flush(self):
        self.stream.flush()


class HashReader:
    def __init__(self, stream):
        self.stream, self.digest, self.count = stream, hashlib.sha256(), 0

    def read(self, size=-1):
        data = self.stream.read(size)
        self.digest.update(data)
        self.count += len(data)
        return data


def create_archive(root, manifest, destination, limits=None):
    """Store each content digest once; unrelated equal files stay separate on restore."""
    limits = {**DEFAULT_LIMITS, **(limits or {})}
    validate_manifest(manifest, limits)
    data = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    if len(data) > limits["max_manifest_bytes"]:
        raise ContractError("fixture manifest bound exceeded")
    representatives = {}
    for row in manifest["entries"]:
        if row["type"] == "file":
            representatives.setdefault(row["sha256"], row)
    with Path(destination).open("xb") as stream:
        bounded = LimitedWriter(stream, limits["max_archive_bytes"])
        with gzip.GzipFile(fileobj=bounded, mode="wb", filename="", mtime=0, compresslevel=3) as zipped:
            with tarfile.open(fileobj=zipped, mode="w|", format=tarfile.PAX_FORMAT) as archive:
                member = tarfile.TarInfo("manifest.json")
                member.size, member.mode = len(data), 0o600
                archive.addfile(member, io.BytesIO(data))
                for digest, row in sorted(representatives.items()):
                    fd = open_beneath(root, row["path"])
                    with os.fdopen(fd, "rb") as incoming:
                        info = os.fstat(incoming.fileno())
                        if (not stat.S_ISREG(info.st_mode) or info.st_dev != row["dev"]
                                or info.st_ino != row["ino"] or info.st_size != row["size"]):
                            raise ContractError("fixture payload identity changed")
                        reader = HashReader(incoming)
                        member = tarfile.TarInfo("payload/" + digest)
                        member.size, member.mode = row["size"], 0o600
                        archive.addfile(member, reader)
                        if reader.count != row["size"] or reader.digest.hexdigest() != digest:
                            raise ContractError("fixture payload bytes changed")
        stream.flush()
        os.fsync(stream.fileno())
    if scan_tree(root, limits)["entries"] != manifest["entries"]:
        raise ContractError("fixture changed while its archive was written")
    return {"unique_payloads": len(representatives),
            "unique_payload_bytes": sum(row["size"] for row in representatives.values()),
            "archive_bytes": Path(destination).stat().st_size}


def _socket_node(path):
    program = "import socket,sys; s=socket.socket(socket.AF_UNIX); s.bind(sys.argv[1]); s.close()"
    result = subprocess.run([sys.executable, "-I", "-B", "-c", program, path.name],
                            cwd=path.parent, capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise ContractError("inactive fixture socket restoration failed: " + result.stderr[-1000:])


def privileged_proc_metadata(root, inodes, pids, owner_uid):
    value = {"source_root": str(root), "inodes": sorted(inodes),
             "pids": pids, "owner_uid": owner_uid}
    command = ["sudo", "-n", "/usr/bin/python3", "-I", "-B", "-c", PROC_METADATA_SOURCE]
    result = subprocess.run(command, input=json.dumps(value), capture_output=True,
                            text=True, timeout=30)
    if result.returncode:
        raise ContractError("read-only owner-process metadata probe did not complete")
    observed = json.loads(result.stdout)
    if (set(observed.get("checked_pids", [])) != {int(pid) for pid in pids}
            or observed.get("workspace_file_contents_read") != 0
            or observed.get("modifications") != 0):
        raise ContractError("read-only process metadata probe did not cover every requested owner")
    return observed


def prepare_quarantine_directory_access(root, manifest, directories):
    """Only verified, unreachable quarantine directory inodes can change owner."""
    if not directories:
        return {"directories_changed": [], "file_content_changes": 0, "outside_root_changes": 0}
    root = Path(root)
    if not re.fullmatch(r"\.retiring-.+-[0-9a-f]{32}", root.name) or root.is_symlink():
        raise ContractError("directory access preparation requires a unique quarantine")
    base = next(row for row in manifest["entries"] if row["path"] == ".")
    value = {"root": str(root), "root_dev": base["dev"], "root_ino": base["ino"],
             "owner_uid": os.geteuid(), "directories": directories}
    command = ["sudo", "-n", "/usr/bin/python3", "-I", "-B", "-c", DIRECTORY_ACCESS_SOURCE]
    result = subprocess.run(command, input=json.dumps(value), capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ContractError("verified quarantine directory access preparation did not complete")
    observed = json.loads(result.stdout)
    if (set(observed.get("directories_changed", [])) != {row["path"] for row in directories}
            or (observed.get("root_dev"), observed.get("root_ino")) != (base["dev"], base["ino"])
            or observed.get("file_content_changes") != 0 or observed.get("outside_root_changes") != 0):
        raise ContractError("quarantine access preparation did not match its exact directory set")
    return observed


def restore_archive(body, destination, expected=None, limits=None, expected_digest=None):
    """Materialize data first and links last; never extract archive-chosen paths."""
    limits = {**DEFAULT_LIMITS, **(limits or {})}
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise ContractError("fixture restoration destination already exists")
    with tarfile.open(body, "r|gz") as archive:
        first = archive.next()
        if (first is None or first.name != "manifest.json" or not first.isfile()
                or not 0 < first.size <= limits["max_manifest_bytes"]):
            raise ContractError("fixture archive manifest is absent or oversized")
        manifest = json.load(archive.extractfile(first))
        rows = validate_manifest(manifest, limits)
        if expected is not None and manifest != expected:
            raise ContractError("fixture archive differs from its pinned source manifest")
        if expected_digest is not None and canonical_hash(manifest) != expected_digest:
            raise ContractError("fixture archive manifest digest differs")
        destination.mkdir(mode=0o700, parents=False)
        for name, row in sorted(rows.items(), key=lambda item: (item[0].count("/"), item[0])):
            if name != "." and row["type"] == "directory":
                (destination / name).mkdir(mode=0o700)
        recipients = defaultdict(list)
        for row in rows.values():
            if row["type"] == "file":
                recipients[row["sha256"]].append(row)
        seen = set()
        for member in archive:
            if member.name == "manifest.json" and member.offset == first.offset:
                continue
            digest = member.name.removeprefix("payload/")
            if (member.name != "payload/" + digest or digest not in recipients
                    or digest in seen or not member.isfile()):
                raise ContractError("unexpected, duplicate, or special fixture payload")
            group = sorted(recipients[digest], key=lambda row: row["path"])
            if member.size != group[0]["size"]:
                raise ContractError("fixture payload size differs")
            seen.add(digest)
            primary = destination / group[0]["path"]
            reader = HashReader(archive.extractfile(member))
            with primary.open("xb") as out:
                shutil.copyfileobj(reader, out, length=1024**2)
            if reader.count != member.size or reader.digest.hexdigest() != digest:
                raise ContractError("fixture payload checksum differs")
            representatives = {group[0]["group"]: primary}
            for row in group[1:]:
                target = destination / row["path"]
                if row["group"] in representatives:
                    os.link(representatives[row["group"]], target, follow_symlinks=False)
                else:
                    with primary.open("rb") as source, target.open("xb") as out:
                        shutil.copyfileobj(source, out, length=1024**2)
                    representatives[row["group"]] = target
        if seen != set(recipients):
            raise ContractError("fixture archive is missing payloads")
    # Force gzip trailer/CRC validation as tar readers may stop at tar's end marker.
    with gzip.open(body, "rb") as stream:
        while stream.read(1024**2):
            pass
    for name, row in rows.items():
        target = destination / name
        if row["type"] == "fifo":
            os.mkfifo(target, 0o600)
        elif row["type"] == "socket":
            _socket_node(target)
        elif row["type"] == "symlink":
            os.symlink(row["link"], target)
    for name, row in sorted(rows.items(), key=lambda item: (-item[0].count("/"), item[0])):
        target = destination / name
        if row["type"] != "symlink":
            os.chmod(target, row["mode"], follow_symlinks=False)
        os.utime(target, ns=(row["mtime_ns"], row["mtime_ns"]), follow_symlinks=False)
    restored = scan_tree(destination, limits)
    if shape(restored["entries"]) != shape(manifest["entries"]):
        raise ContractError("restored fixture bytes, modes, links, times, or types differ")
    return {"status": "all_fixture_nodes_independently_restored",
            "manifest_sha256": canonical_hash(manifest), "entries": len(rows),
            "counts": manifest["counts"], "archive_sha256": file_hash(Path(body)),
            "destination": str(destination), "gzip_crc_verified": True,
            "links_followed": 0, "socket_state": "inactive_filesystem_nodes_only"}


def live_references(root, manifest, *, inspect_docker=True):
    """Check owner-UID processes, socket names, and all active Docker mounts."""
    root = Path(root).absolute()
    prefix, references, errors = str(root) + "/", [], []
    inode_set = {(row["dev"], row["ino"]) for row in manifest["entries"]}
    own_uid = os.geteuid()
    starts, deferred = {}, {}
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            uid = next(line for line in (proc / "status").read_text().splitlines() if line.startswith("Uid:"))
            if int(uid.split()[2]) != own_uid:
                continue
            starts[proc.name] = (proc / "stat").read_text().rsplit(")", 1)[1].split()[19]
            for entry in [proc / "cwd", proc / "root", *list((proc / "fd").iterdir())]:
                try:
                    target = os.readlink(entry)
                    info = entry.stat()
                    if target == str(root) or target.startswith(prefix) or (info.st_dev, info.st_ino) in inode_set:
                        references.append({"pid": int(proc.name), "handle": str(entry), "target": target})
                except (FileNotFoundError, ProcessLookupError):
                    pass
                except PermissionError:
                    deferred[proc.name] = starts[proc.name]
            command = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            if int(proc.name) != os.getpid() and str(root) in command:
                references.append({"pid": int(proc.name), "handle": "cmdline", "target": str(root)})
        except (FileNotFoundError, ProcessLookupError):
            pass
        except PermissionError as error:
            if proc.name in starts:
                deferred[proc.name] = starts[proc.name]
            else:
                errors.append(str(error))
        except (StopIteration, ValueError, IndexError) as error:
            errors.append(str(error))
    privileged = None
    if deferred:
        try:
            privileged = privileged_proc_metadata(root, inode_set, deferred, own_uid)
            references.extend(privileged["references"])
            errors.extend(privileged["observation_errors"])
        except (OSError, ValueError, ContractError, subprocess.TimeoutExpired) as error:
            errors.append(type(error).__name__ + ": " + str(error))
    for line in Path("/proc/net/unix").read_text().splitlines()[1:]:
        fields = line.split(maxsplit=7)
        if len(fields) == 8 and (fields[7] == str(root) or fields[7].startswith(prefix)):
            references.append({"handle": "/proc/net/unix", "target": fields[7]})
    if inspect_docker:
        process = subprocess.run(["docker", "ps", "-q"], capture_output=True, text=True, timeout=30)
        if process.returncode:
            errors.append("Docker liveness query failed")
        elif process.stdout.split():
            inspected = subprocess.run(["docker", "inspect", *process.stdout.split()],
                                       capture_output=True, text=True, timeout=30)
            if inspected.returncode:
                errors.append("Docker mount query failed")
            else:
                for container in json.loads(inspected.stdout):
                    for mount in container.get("Mounts", []):
                        source = Path(mount["Source"])
                        if source == root or source.is_relative_to(root) or root.is_relative_to(source):
                            references.append({"handle": container["Id"], "target": mount["Source"]})
    return {"references": references, "observation_errors": errors, "owner_uid": own_uid,
            "read_only_privileged_metadata": privileged}


def retire_tree(root, manifest, restoration, *, inspect_docker=True, journal=None):
    root = Path(root)
    if (restoration.get("status") != "all_fixture_nodes_independently_restored"
            or restoration.get("manifest_sha256") != canonical_hash(manifest)
            or restoration.get("entries") != len(manifest["entries"])
            or restoration.get("gzip_crc_verified") is not True):
        raise ContractError("fixture retirement lacks an independent complete restoration")
    if scan_tree(root)["entries"] != manifest["entries"]:
        raise ContractError("fixture changed since its archived source snapshot")
    foreign_directories = [
        row for row in manifest["entries"]
        if row["type"] == "directory" and row["uid"] != os.geteuid()
        and not os.access(root / row["path"], os.W_OK | os.X_OK, effective_ids=True)
    ]
    observed = live_references(root, manifest, inspect_docker=inspect_docker)
    if observed["references"] or observed["observation_errors"]:
        raise ContractError("fixture has a live or unverified owner reference")
    quarantine = root.with_name(".retiring-" + root.name + "-" + uuid.uuid4().hex)
    root.rename(quarantine)
    event = {"status": "quarantined_not_deleted", "source_root": str(root),
             "quarantine": str(quarantine), "recorded_at": now()}
    if journal:
        atomic_json(Path(journal), event)
    try:
        if scan_tree(quarantine)["entries"] != manifest["entries"]:
            raise ContractError("fixture changed during quarantine")
        observed = live_references(quarantine, manifest, inspect_docker=inspect_docker)
        if observed["references"] or observed["observation_errors"]:
            raise ContractError("quarantined fixture has a live or unverified owner reference")
        access = prepare_quarantine_directory_access(quarantine, manifest, foreign_directories)
        if journal:
            atomic_json(Path(journal), {**event, "status": "verified_quarantine_access_prepared",
                                      "directory_access": access})
    except Exception:
        if not root.exists() and not root.is_symlink():
            quarantine.rename(root)
            if journal:
                atomic_json(Path(journal), {**event, "status": "quarantine_restored_without_deletion"})
        raise
    if not shutil.rmtree.avoids_symlink_attacks:
        quarantine.rename(root)
        raise ContractError("fixture retirement requires descriptor-safe rmtree")
    for row in sorted(manifest["entries"], key=lambda row: row["path"].count("/")):
        if row["type"] == "directory" and row["uid"] == os.geteuid():
            os.chmod(quarantine / row["path"], row["mode"] | 0o700, follow_symlinks=False)
    if journal:
        atomic_json(Path(journal), {**event, "status": "verified_quarantine_deletion_started"})
    shutil.rmtree(quarantine)
    result = {"status": "terminal_fixture_retired", "source_root": str(root),
              "removed_entries": len(manifest["entries"]),
              "exclusive_allocated_bytes": manifest["exclusive_allocated_bytes"],
              "original_path_absent": not root.exists() and not root.is_symlink(),
              "quarantine_absent": not quarantine.exists(), "live_reference_check": observed,
              "quarantine_directory_access": access}
    if journal:
        atomic_json(Path(journal), {**event, **result})
    return result


def validate_selection(selection, scratch):
    root = Path(selection["root"])
    match = re.fullmatch(r"test-tmp-[A-Za-z0-9_-]+-(\d+)", root.name)
    if (selection.get("schema_version") != "reviewbench-terminal-pytest-fixture-selection/v1"
            or not match or int(match[1]) != selection["producer_pid"]
            or not root.is_absolute() or root.is_symlink() or root.resolve() != root
            or root.parent != Path(selection["parent"]) or root.parent != Path(scratch).parent):
        raise ContractError("fixture selection does not bind a direct terminal pytest temporary tree")
    if Path("/proc", str(selection["producer_pid"])).exists():
        raise ContractError("fixture producer PID is still present")
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != (
            selection["root_dev"], selection["root_ino"]):
        raise ContractError("fixture root identity changed")
    ref = selection["producer_receipt"]
    receipt = Path(ref["path"])
    if receipt.is_symlink() or file_hash(receipt) != ref["sha256"]:
        raise ContractError("original pytest terminal receipt changed")
    text = receipt.read_text()
    marker = "COMMAND: " + selection["producer_command"] + "\n"
    if marker not in text:
        raise ContractError("original pytest producer command is absent")
    tail = text.split(marker, 1)[1]
    output = next((line.removeprefix("LITERAL OUTPUT: ") for line in tail.splitlines()
                   if line.startswith("LITERAL OUTPUT: ")), None)
    status = next((line.removeprefix("EXIT STATUS: ") for line in tail.splitlines()
                   if line.startswith("EXIT STATUS: ")), None)
    args = shlex.split(selection["producer_command"])
    has_root = "--basetemp=" + str(root) in args or any(
        a == "--basetemp" and b == str(root) for a, b in zip(args, args[1:]))
    if (not has_root or "pytest" not in args or output != selection["producer_literal_output"]
            or status != "0" or selection.get("producer_exit_status") != 0):
        raise ContractError("fixture selection lacks its successful original pytest completion")
    return root


def run_archive(config, selection, output, restored, retire=False):
    from reviewbench_maintainability.agentic.execution import ExecutionServices
    from reviewbench_maintainability.agentic.resources import GiB, ResourceRequest, file_lock
    from reviewbench_maintainability.agentic.storage import describe_file, require_local_filesystem
    services = ExecutionServices(config)
    root = validate_selection(selection, services.scratch)
    output = Path(output).parent.resolve() / Path(output).name
    restored = Path(restored).parent.resolve() / Path(restored).name
    paths = [root, output, restored]
    if any(a == b or a.is_relative_to(b) or b.is_relative_to(a)
           for index, a in enumerate(paths) for b in paths[index + 1:]):
        raise ContractError("fixture source/output/restoration roots overlap")
    require_local_filesystem(output.parent)
    require_local_filesystem(restored.parent)
    limits = {key: selection[key] for key in DEFAULT_LIMITS}
    required = (selection["restore_allocated_limit_bytes"]
                + 2 * limits["max_archive_bytes"] + 2 * limits["max_manifest_bytes"])
    if selection["reserved_scratch_bytes"] < required:
        raise ContractError("fixture archive/GET/restore exceeds its admitted scratch bound")
    request = ResourceRequest("terminal-pytest-fixture-retirement:" + root.name, cpus=2,
                              memory_bytes=2 * GiB, scratch_bytes=selection["reserved_scratch_bytes"])
    lock = services.gate.root / ("fixture-" + hashlib.sha256(str(root).encode()).hexdigest() + ".lock")
    with services.gate.acquire(request, timeout=7200), file_lock(lock):
        output.mkdir()
        atomic_json(output / "selection.json", selection)
        status = {"status": "scanning", "source_root": str(root), "started_at": now(),
                  "full_scope": 402, "new_SUT_model_judge_calls": 0}
        atomic_json(output / "status.json", status)
        manifest = scan_tree(root, limits)
        file_groups = {row["group"]: row for row in manifest["entries"] if row["type"] == "file"}
        restore_bound = sum((row["size"] + 4095) // 4096 * 4096 for row in file_groups.values())
        restore_bound += sum(max(row["allocated"], 4096) * 2
                             for row in manifest["entries"] if row["type"] != "file")
        if max(manifest["allocated_bytes"], restore_bound) > selection["restore_allocated_limit_bytes"]:
            raise ContractError("fixture restoration exceeds its reserved allocation")
        observed = live_references(root, manifest)
        if observed["references"] or observed["observation_errors"]:
            atomic_json(output / "live-reference-observation.json", observed)
            raise ContractError("fixture producer has a live or unverified reference")
        body = output / "fixture.tar.gz"
        compact = create_archive(root, manifest, body, limits)
        status.update(status="archive_created", **compact)
        atomic_json(output / "status.json", status)
        obj = describe_file(body)
        with services.cache.transfers.acquire():
            put = services.cache.remote.put(body, obj)
        copy = output / "independent-readback.tar.gz"
        with services.cache.transfers.acquire():
            get = services.cache.remote.get(obj, copy)
        if describe_file(copy) != obj:
            raise ContractError("independent fixture Blob GET differs")
        proof = restore_archive(copy, restored, expected=manifest, limits=limits)
        atomic_json(output / "restoration.json", proof)
        publication = {
            "status": "fixture_archive_independently_restored", "source_root": str(root),
            "object": asdict(obj), "receipt": put, "readback": get,
            "manifest_sha256": canonical_hash(manifest), "counts": manifest["counts"],
            "entries": len(manifest["entries"]), "logical_bytes": manifest["logical_bytes"],
            "allocated_bytes": manifest["allocated_bytes"],
            "exclusive_allocated_bytes": manifest["exclusive_allocated_bytes"],
            "restoration": {"path": str(output / "restoration.json"), "sha256": file_hash(output / "restoration.json")},
            "limits": limits, "reserved_scratch_bytes": selection["reserved_scratch_bytes"],
            "recorded_at": now(), "new_SUT_model_judge_calls": 0, **compact,
        }
        atomic_json(output / "publication.json", publication)
        if retire:
            validate_selection(selection, services.scratch)
            retired = retire_tree(root, manifest, proof, journal=output / "retirement-state.json")
            atomic_json(output / "retirement.json", retired)
            status.update(**retired)
        else:
            status.update(status="archived_original_retained")
        status.update(recorded_at=now(), publication=str(output / "publication.json"))
        atomic_json(output / "status.json", status)
        return status


def run_restore(config, publication, destination):
    from reviewbench_maintainability.agentic.execution import ExecutionServices
    from reviewbench_maintainability.agentic.resources import GiB, ResourceRequest
    from reviewbench_maintainability.agentic.storage import ObjectSpec, describe_file, require_local_filesystem
    if publication.get("status") != "fixture_archive_independently_restored":
        raise ContractError("fixture restoration needs a verified archive publication")
    services = ExecutionServices(config)
    destination = Path(destination).absolute()
    require_local_filesystem(destination.parent)
    request = ResourceRequest("restore-terminal-fixture:" + destination.name, cpus=2,
                              memory_bytes=2 * GiB, scratch_bytes=publication["reserved_scratch_bytes"])
    with services.gate.acquire(request, timeout=7200):
        with tempfile.TemporaryDirectory(prefix=".fixture-get-", dir=destination.parent) as temporary:
            body = Path(temporary) / "fixture.tar.gz"
            obj = ObjectSpec.from_dict(publication["object"])
            with services.cache.transfers.acquire():
                services.cache.remote.get(obj, body)
            if describe_file(body) != obj:
                raise ContractError("fixture restoration Blob object differs")
            proof = restore_archive(body, destination, limits=publication["limits"],
                                    expected_digest=publication["manifest_sha256"])
            if proof["manifest_sha256"] != publication["manifest_sha256"]:
                raise ContractError("fixture restored manifest differs from publication")
            return proof


def main():
    from reviewbench_maintainability.agentic.cli import load_config
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["archive", "restore"])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--selection", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--restored", type=Path)
    parser.add_argument("--retire", action="store_true")
    parser.add_argument("--publication", type=Path)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    config = load_config(args.config)
    try:
        if args.action == "archive":
            if not all((args.selection, args.output, args.restored)):
                parser.error("archive needs selection, output, and independent restored paths")
            result = run_archive(config, json.loads(args.selection.read_text()),
                                 args.output, args.restored, retire=args.retire)
        else:
            if not args.publication:
                parser.error("restore needs a publication")
            publication = json.loads(args.publication.read_text())
            result = run_restore(config, publication, args.destination or publication["source_root"])
    except Exception as error:
        if args.output and (args.output / "status.json").is_file():
            previous = json.loads((args.output / "status.json").read_text())
            atomic_json(args.output / "status.json", {
                **previous, "status": "failed", "last_phase": previous["status"],
                "error": type(error).__name__ + ": " + str(error), "recorded_at": now(),
            })
        raise
    print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
