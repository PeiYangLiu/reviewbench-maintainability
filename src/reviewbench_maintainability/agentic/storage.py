"""Content-addressed blob objects and a bounded, lease-protected local cache.

Only archive parts cross the network. Git, compilers, checkpoints, and SQLite
or other small-file workloads belong on a local filesystem, never BlobFuse.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import datetime
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import threading
import time
from typing import Callable
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .contracts import ContractError, InfrastructureError, atomic_json
from .resources import file_lock, process_identity

MiB = 1024**2
GiB = 1024**3


@dataclass(frozen=True)
class ObjectSpec:
    sha256: str
    size_bytes: int
    md5_base64: str

    def __post_init__(self):
        if not re.fullmatch(r"[0-9a-f]{64}", self.sha256) or type(self.size_bytes) is not int or self.size_bytes < 0:
            raise ContractError("invalid content-addressed object")
        try:
            if len(base64.b64decode(self.md5_base64, validate=True)) != 16:
                raise ValueError("wrong length")
        except (ValueError, TypeError) as exc:
            raise ContractError("invalid object transport checksum") from exc

    @classmethod
    def from_dict(cls, value: dict) -> "ObjectSpec":
        return cls(**{key: value[key] for key in ("sha256", "size_bytes", "md5_base64")})


def describe_file(path: Path) -> ObjectSpec:
    if not path.is_file() or path.is_symlink():
        raise ContractError(f"object must be a regular file: {path}")
    sha, md5, size = hashlib.sha256(), hashlib.md5(usedforsecurity=False), 0
    with path.open("rb") as stream:
        for data in iter(lambda: stream.read(4 * MiB), b""):
            sha.update(data)
            md5.update(data)
            size += len(data)
    return ObjectSpec(sha.hexdigest(), size, base64.b64encode(md5.digest()).decode("ascii"))


def require_local_filesystem(path: Path) -> None:
    existing = path.resolve()
    while not existing.exists():
        existing = existing.parent
    probe = subprocess.run(["findmnt", "-no", "FSTYPE", "--target", str(existing)],
                           capture_output=True, text=True, timeout=10)
    if probe.returncode:
        raise ContractError(f"could not identify local filesystem for {path}")
    kind = probe.stdout.strip().lower()
    if kind.startswith(("fuse", "nfs", "cifs", "smb")):
        raise ContractError(f"hot cache/scratch must be local, not {kind}: {path}")


class AzureCliCredential:
    """Keep the existing mount's Azure CLI credential in memory, never in files."""

    def __init__(self):
        self._lock = threading.Lock()
        self._token = ""
        self._until = 0.

    def __call__(self, *, refresh: bool = False) -> str:
        with self._lock:
            if refresh or time.monotonic() >= self._until:
                result = subprocess.run(
                    ["az", "account", "get-access-token", "--resource", "https://storage.azure.com/", "-o", "json"],
                    capture_output=True, text=True, timeout=60,
                )
                if result.returncode:
                    raise InfrastructureError("Azure CLI storage authentication failed")
                data = json.loads(result.stdout)
                self._token = data["accessToken"]
                expires = float(data.get("expires_on") or (time.time() + 180))
                self._until = time.monotonic() + max(0., min(600., expires - time.time() - 120.))
            return self._token

    def __repr__(self):
        return "AzureCliCredential(<in-memory>)"


class TransferSlots:
    """Limit I/O across cooperating processes as well as worker threads."""

    def __init__(self, root: Path, count: int = 2):
        if type(count) is not int or count < 1:
            raise ContractError("transfer slot count must be positive")
        self.root, self.count = root, count
        root.mkdir(parents=True, exist_ok=True)
        with file_lock(root / "config.lock"):
            config = root / "config.json"
            if config.exists() and json.loads(config.read_text())["count"] != count:
                raise ContractError("transfer pool configuration changed")
            atomic_json(config, {"count": count})

    @contextmanager
    def acquire(self):
        held = None
        while held is None:
            for number in range(self.count):
                stream = (self.root / f"slot-{number}.lock").open("a+b")
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    held = stream
                    break
                except BlockingIOError:
                    stream.close()
            if held is None:
                time.sleep(.1)
        try:
            yield
        finally:
            fcntl.flock(held, fcntl.LOCK_UN)
            held.close()


class AzureBlobStore:
    """Direct REST access to the same container as the mounted BlobFuse path.

    Immutable CAS PUTs carry Content-MD5 (verified by Azure during transfer).
    A subsequent server HEAD must agree on SHA metadata, length and MD5 before
    upload is acknowledged. Reads verify the actual streamed SHA256 and MD5.
    This avoids mistaking a reread of BlobFuse's local cache for remote proof.
    """

    def __init__(self, endpoint: str, container: str, prefix: str, *,
                 credential: Callable | None = None, timeout: float = 300,
                 retries: int = 2, allow_loopback_http: bool = False):
        parsed = urllib.parse.urlsplit(endpoint)
        if (parsed.scheme != "https" and not (allow_loopback_http and parsed.scheme == "http"
                                             and parsed.hostname in {"127.0.0.1", "localhost"})):
            raise ContractError("blob endpoint must use HTTPS")
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise ContractError("blob endpoint must not contain credentials or object paths")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", container):
            raise ContractError("invalid Azure container")
        path = PurePosixPath(prefix)
        if not prefix or path.is_absolute() or ".." in path.parts or "\\" in prefix:
            raise ContractError("invalid blob object prefix")
        if timeout <= 0 or retries < 0:
            raise ContractError("invalid blob transport limits")
        self.base = endpoint.rstrip("/") + "/" + container + "/" + urllib.parse.quote(prefix.strip("/"), safe="/")
        self.credential = credential or AzureCliCredential()
        self.timeout, self.retries = timeout, retries
        self._lock = threading.Lock()
        self.stats = {"requests": 0, "uploads": 0, "reused_uploads": 0, "downloads": 0,
                      "uploaded_bytes": 0, "downloaded_bytes": 0}

    def _count(self, **values):
        with self._lock:
            for key, value in values.items():
                self.stats[key] += value

    def uri(self, spec: ObjectSpec) -> str:
        return self.base + "/objects/sha256/" + spec.sha256[:2] + "/" + spec.sha256

    def _request(self, method: str, spec: ObjectSpec, *, data=None, headers: dict | None = None):
        for attempt in range(self.retries + 1):
            if data is not None:
                data.seek(0)
            auth = self.credential(refresh=attempt > 0)
            request = urllib.request.Request(self.uri(spec), data=data, method=method, headers={
                "Authorization": "Bearer " + auth, "x-ms-version": "2023-11-03",
                "x-ms-date": datetime.datetime.now(datetime.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT"),
                **(headers or {}),
            })
            self._count(requests=1)
            try:
                return urllib.request.urlopen(request, timeout=self.timeout)
            except urllib.error.HTTPError as exc:
                if exc.code in {404, 409, 412}:
                    raise
                retryable = exc.code in {401, 408, 429, 500, 502, 503, 504}
                code = exc.code
                exc.close()
                if not retryable or attempt >= self.retries:
                    raise InfrastructureError(f"blob {method} failed with HTTP {code}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if attempt >= self.retries:
                    raise InfrastructureError(f"blob {method} transport failed: {type(exc).__name__}") from None
            time.sleep(min(2 ** attempt, 8))
        raise AssertionError("unreachable")

    def head(self, spec: ObjectSpec) -> dict | None:
        try:
            with self._request("HEAD", spec) as response:
                headers = response.headers
                proof = {"sha256": headers.get("x-ms-meta-sha256"), "size_bytes": int(headers["Content-Length"]),
                         "md5_base64": headers.get("Content-MD5"), "etag": headers.get("ETag"),
                         "uri": self.uri(spec), "verification": "azure-server-head-and-content-md5"}
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            if code == 404:
                return None
            raise InfrastructureError(f"blob HEAD failed with HTTP {code}") from None
        if any(proof[key] != value for key, value in asdict(spec).items()) or not proof["etag"]:
            raise ContractError("remote CAS object identity/checksum does not match")
        return proof

    def put(self, source: Path, spec: ObjectSpec) -> dict:
        if describe_file(source) != spec:
            raise ContractError("upload source changed or object description is incorrect")
        old = self.head(spec)
        if old is not None:
            self._count(reused_uploads=1)
            return old
        with source.open("rb") as stream:
            try:
                with self._request("PUT", spec, data=stream, headers={
                    "x-ms-blob-type": "BlockBlob", "Content-Length": str(spec.size_bytes),
                    "Content-MD5": spec.md5_base64, "x-ms-meta-sha256": spec.sha256,
                    "If-None-Match": "*", "Content-Type": "application/octet-stream",
                }) as response:
                    response.read()
            except urllib.error.HTTPError as exc:
                code = exc.code
                exc.close()
                if code not in {409, 412}:
                    raise InfrastructureError(f"blob PUT failed with HTTP {code}") from None
        proof = self.head(spec)
        if proof is None:
            raise InfrastructureError("blob PUT did not produce a committed object")
        self._count(uploads=1, uploaded_bytes=spec.size_bytes)
        return proof

    def get(self, spec: ObjectSpec, destination: Path) -> dict:
        if destination.exists() or destination.is_symlink():
            raise ContractError("download destination must be new")
        for attempt in range(self.retries + 1):
            sha, md5, size = hashlib.sha256(), hashlib.md5(usedforsecurity=False), 0
            try:
                with self._request("GET", spec) as response, destination.open("xb") as output:
                    etag = response.headers.get("ETag")
                    while data := response.read(4 * MiB):
                        size += len(data)
                        if size > spec.size_bytes:
                            raise ContractError("download exceeded the declared object size")
                        sha.update(data)
                        md5.update(data)
                        output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                if size < spec.size_bytes:
                    raise http.client.IncompleteRead(b"", spec.size_bytes - size)
                actual = ObjectSpec(sha.hexdigest(), size, base64.b64encode(md5.digest()).decode("ascii"))
                if actual != spec:
                    raise ContractError("downloaded object failed SHA256/MD5 verification")
                break
            except (TimeoutError, ConnectionError, http.client.IncompleteRead) as exc:
                destination.unlink(missing_ok=True)
                if attempt >= self.retries:
                    raise InfrastructureError(f"blob GET stream failed: {type(exc).__name__}") from None
                time.sleep(min(2 ** attempt, 8))
            except BaseException:
                destination.unlink(missing_ok=True)
                raise
        self._count(downloads=1, downloaded_bytes=size)
        return {**asdict(spec), "etag": etag, "uri": self.uri(spec), "verification": "streamed-sha256-and-md5"}


class DirectoryObjectStore:
    """A local immutable backing store for deterministic cache tests/offline use."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.stats = {"uploads": 0, "downloads": 0}

    def put(self, source: Path, spec: ObjectSpec) -> dict:
        if describe_file(source) != spec:
            raise ContractError("upload description differs from source")
        target = self.root / spec.sha256
        if not target.exists():
            pending = target.with_name(target.name + "." + uuid.uuid4().hex)
            shutil.copyfile(source, pending)
            pending.replace(target)
            self.stats["uploads"] += 1
        if describe_file(target) != spec:
            raise ContractError("backing object is corrupted")
        return {**asdict(spec), "uri": str(target), "verification": "local-filesystem-sha256"}

    def get(self, spec: ObjectSpec, destination: Path) -> dict:
        source = self.root / spec.sha256
        if describe_file(source) != spec:
            raise ContractError("backing object is corrupted")
        if destination.exists():
            raise ContractError("download target exists")
        shutil.copyfile(source, destination)
        self.stats["downloads"] += 1
        return {**asdict(spec), "verification": "local-filesystem-sha256"}


class LocalObjectCache:
    """Bounded LRU cache with leases, immutable objects and miss coalescing."""

    def __init__(self, root: Path, remote, *, max_bytes: int = 64 * GiB,
                 min_free_bytes: int = 80 * GiB, transfers: int = 2,
                 wait_seconds: float = 300):
        if max_bytes <= 0 or min_free_bytes < 0 or wait_seconds <= 0:
            raise ContractError("invalid local cache limits")
        require_local_filesystem(root)
        self.root, self.remote = root.resolve(), remote
        self.max_bytes, self.min_free_bytes, self.wait_seconds = max_bytes, min_free_bytes, wait_seconds
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"
        self.lock_path = self.root / "index.lock"
        settings = {"max_bytes": max_bytes, "min_free_bytes": min_free_bytes, "transfers": transfers}
        with file_lock(self.lock_path):
            marker = self.root / "CACHE_OWNER.json"
            if marker.is_symlink():
                raise ContractError("cache ownership marker must not be symlinked")
            if not marker.exists():
                if any(p.name != "index.lock" for p in self.root.iterdir()):
                    raise ContractError("refusing to manage a nonempty unmarked cache directory")
                atomic_json(marker, {"schema_version": "reviewbench-local-object-cache/v1",
                                     "instance": uuid.uuid4().hex, "settings": settings})
            else:
                owner = json.loads(marker.read_text())
                if (owner.get("schema_version") != "reviewbench-local-object-cache/v1"
                        or owner.get("settings") != settings):
                    raise ContractError("cache ownership marker/configuration is invalid")
        for name in ("objects", "locks", "staging", "transfer-slots"):
            if (self.root / name).is_symlink():
                raise ContractError("cache internals must not be symlinked")
            (self.root / name).mkdir(exist_ok=True)
        self.transfers = TransferSlots(self.root / "transfer-slots", transfers)

    def _index(self) -> dict:
        if self.index_path.is_symlink():
            raise ContractError("cache index must not be symlinked")
        index = json.loads(self.index_path.read_text()) if self.index_path.exists() else {}
        if not isinstance(index, dict):
            raise ContractError("invalid cache index")
        for key, entry in index.items():
            if not re.fullmatch(r"[0-9a-f]{64}", key) or not isinstance(entry, dict):
                raise ContractError("invalid cache object key")
            if ObjectSpec.from_dict(entry["spec"]).sha256 != key:
                raise ContractError("cache index object identity mismatch")
        return index

    def _path(self, spec: ObjectSpec) -> Path:
        return self.root / "objects" / spec.sha256

    def _valid(self, spec: ObjectSpec, entry: dict | None) -> bool:
        path = self._path(spec)
        if not entry or entry.get("state") != "verified" or path.is_symlink() or not path.is_file():
            return False
        st = path.stat()
        return (entry["spec"] == asdict(spec) and st.st_size == spec.size_bytes
                and entry.get("stamp") == [st.st_ino, st.st_mtime_ns, st.st_ctime_ns])

    def _reserve(self, spec: ObjectSpec, temporary: Path) -> None:
        if spec.size_bytes > self.max_bytes:
            raise ContractError("object part is larger than the configured local cache")
        started = time.monotonic()
        while True:
            with file_lock(self.lock_path):
                index = self._index()
                # A pending reservation from a dead owner is not a live writer.
                for key, entry in list(index.items()):
                    if entry.get("state") == "pending":
                        owner = process_identity(entry["pid"])
                        if owner is None or (owner != "unknown" and entry["process_start"] not in {None, "unknown"}
                                             and owner != entry["process_start"]):
                            old_name = entry.get("temporary", "")
                            if re.fullmatch(r"[0-9a-f]{32}\.part", old_name):
                                (self.root / "staging" / old_name).unlink(missing_ok=True)
                            # Do not wait while holding the metadata lock; an
                            # active reader/fetcher has priority over eviction.
                            if key != spec.sha256:
                                try:
                                    with file_lock(self.root / "locks" / key, nonblocking=True):
                                        (self.root / "objects" / key).unlink(missing_ok=True)
                                except BlockingIOError:
                                    continue
                            index.pop(key)
                required = spec.size_bytes
                used = sum(e["spec"]["size_bytes"] for key, e in index.items() if key != spec.sha256)
                pending = sum(e["spec"]["size_bytes"] for key, e in index.items()
                              if key != spec.sha256 and e.get("state") == "pending")
                free = shutil.disk_usage(self.root).free - pending
                for key, entry in sorted(index.items(), key=lambda item: item[1].get("last_used", 0)):
                    if used + required <= self.max_bytes and free - required >= self.min_free_bytes:
                        break
                    if key == spec.sha256 or entry.get("state") != "verified":
                        continue
                    try:
                        with file_lock(self.root / "locks" / key, nonblocking=True):
                            path = self.root / "objects" / key
                            size = path.stat().st_size if path.is_file() and not path.is_symlink() else 0
                            path.unlink(missing_ok=True)
                            used -= entry["spec"]["size_bytes"]
                            free += size
                            index.pop(key)
                    except BlockingIOError:
                        continue  # an active container/extractor still holds it
                if used + required <= self.max_bytes and free - required >= self.min_free_bytes:
                    index[spec.sha256] = {"state": "pending", "spec": asdict(spec), "pid": os.getpid(),
                                          "process_start": process_identity(os.getpid()), "last_used": time.time(),
                                          "temporary": temporary.name}
                    atomic_json(self.index_path, index)
                    return
                atomic_json(self.index_path, index)
            if time.monotonic() - started >= self.wait_seconds:
                raise InfrastructureError("local cache is full/pinned or host disk headroom is low")
            time.sleep(.2)

    def _promote(self, spec: ObjectSpec, temporary: Path, receipt: dict) -> None:
        if describe_file(temporary) != spec:
            raise ContractError("cache import checksum mismatch")
        target = self._path(spec)
        temporary.chmod(0o444)
        temporary.replace(target)
        st = target.stat()
        with file_lock(self.lock_path):
            index = self._index()
            index[spec.sha256] = {"state": "verified", "spec": asdict(spec), "receipt": receipt,
                                  "last_used": time.time(), "stamp": [st.st_ino, st.st_mtime_ns, st.st_ctime_ns]}
            atomic_json(self.index_path, index)

    def _abandon(self, spec: ObjectSpec) -> None:
        with file_lock(self.lock_path):
            index = self._index()
            if index.get(spec.sha256, {}).get("state") == "pending":
                index.pop(spec.sha256)
                atomic_json(self.index_path, index)

    @contextmanager
    def acquire(self, spec: ObjectSpec):
        handle = (self.root / "locks" / spec.sha256).open("a+b")
        temporary = self.root / "staging" / (uuid.uuid4().hex + ".part")
        try:
            fcntl.flock(handle, fcntl.LOCK_SH)
            with file_lock(self.lock_path):
                valid = self._valid(spec, self._index().get(spec.sha256))
            if not valid:
                fcntl.flock(handle, fcntl.LOCK_UN)
                fcntl.flock(handle, fcntl.LOCK_EX)
                with file_lock(self.lock_path):
                    valid = self._valid(spec, self._index().get(spec.sha256))
                if not valid:
                    self._path(spec).unlink(missing_ok=True)
                    self._reserve(spec, temporary)
                    try:
                        with self.transfers.acquire():
                            receipt = self.remote.get(spec, temporary)
                        self._promote(spec, temporary, receipt)
                    except BaseException:
                        self._abandon(spec)
                        raise
                # Eviction uses the same index lock, closing the downgrade gap.
                with file_lock(self.lock_path):
                    fcntl.flock(handle, fcntl.LOCK_SH)
            with file_lock(self.lock_path):
                index = self._index()
                index[spec.sha256]["last_used"] = time.time()
                atomic_json(self.index_path, index)
            yield self._path(spec)
        finally:
            temporary.unlink(missing_ok=True)
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()

    def publish(self, source: Path) -> tuple[ObjectSpec, dict]:
        """A source is never deleted here; callers retain it until receipt."""
        spec = describe_file(source)
        with self.transfers.acquire():
            receipt = self.remote.put(source, spec)
        # Seed local hot reads without paying a download of the just-uploaded object.
        with file_lock(self.root / "locks" / spec.sha256):
            with file_lock(self.lock_path):
                valid = self._valid(spec, self._index().get(spec.sha256))
            if not valid:
                temporary = self.root / "staging" / (uuid.uuid4().hex + ".part")
                self._path(spec).unlink(missing_ok=True)
                self._reserve(spec, temporary)
                try:
                    shutil.copyfile(source, temporary)
                    self._promote(spec, temporary, receipt)
                except BaseException:
                    self._abandon(spec)
                    raise
                finally:
                    temporary.unlink(missing_ok=True)
        return spec, receipt

    def status(self) -> dict:
        with file_lock(self.lock_path):
            index = self._index()
            return {"objects": len(index), "bytes": sum(e["spec"]["size_bytes"] for e in index.values()),
                    "max_bytes": self.max_bytes, "min_free_bytes": self.min_free_bytes,
                    "free_disk_bytes": shutil.disk_usage(self.root).free,
                    "pending": sum(e["state"] == "pending" for e in index.values())}


def open_cache(settings: dict) -> LocalObjectCache:
    """Explicit, portable storage configuration; credentials stay out of YAML."""
    remote = settings["remote"]
    if remote.get("kind") == "azure":
        store = AzureBlobStore(remote["endpoint"], remote["container"], remote["prefix"],
                               timeout=remote.get("timeout_seconds", 300), retries=remote.get("retries", 2))
    elif remote.get("kind") == "directory":
        store = DirectoryObjectStore(Path(remote["path"]))
    else:
        raise ContractError("storage.remote.kind must be azure or directory")
    return LocalObjectCache(Path(settings["cache_dir"]), store,
                            max_bytes=settings.get("max_cache_bytes", 64 * GiB),
                            min_free_bytes=settings.get("min_free_bytes", 80 * GiB),
                            transfers=settings.get("transfers", 2),
                            wait_seconds=settings.get("wait_seconds", 300))
