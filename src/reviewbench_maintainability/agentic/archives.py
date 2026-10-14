"""Compressed, immutable archive parts; only one part is staged/leased at a time."""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import asdict
import gzip
import io
from pathlib import Path
import tempfile

from .contracts import ContractError
from .snapshots import extract_tar
from .storage import LocalObjectCache, MiB, ObjectSpec, require_local_filesystem

ARCHIVE_SCHEMA = "reviewbench-chunked-archive/v1"


class PartWriter:
    """Back-pressure reaches the producer while a full part is uploaded.

    Upload acknowledgments precede scratch deletion. Failed PUTs preserve the
    pending part for diagnosis/retry; cache.publish never removes input files.
    The enclosing managed job decides whether it may discard that scratch.
    """

    def __init__(self, cache: LocalObjectCache, scratch: Path, part_bytes: int):
        if type(part_bytes) is not int or part_bytes < 1024:
            raise ContractError("archive part size must be at least 1024 bytes")
        require_local_filesystem(scratch)
        self.cache, self.scratch, self.part_bytes = cache, scratch, part_bytes
        scratch.mkdir(parents=True, exist_ok=True)
        self.parts, self.receipts = [], []
        self.stream = None
        self.path = None
        self.size = self.total = 0

    def _open(self):
        self.stream = tempfile.NamedTemporaryFile(dir=self.scratch, prefix="part-", suffix=".pending", delete=False)
        self.path = Path(self.stream.name)
        self.size = 0

    def write(self, data) -> int:
        length = len(data)
        view = memoryview(data)
        while view:
            if self.stream is None:
                self._open()
            count = min(len(view), self.part_bytes - self.size)
            self.stream.write(view[:count])
            self.size += count
            self.total += count
            view = view[count:]
            if self.size == self.part_bytes:
                self.commit_part()
        return length

    def flush(self):
        if self.stream:
            self.stream.flush()

    def commit_part(self):
        if self.stream is None:
            return
        self.stream.close()
        self.stream = None
        spec, receipt = self.cache.publish(self.path)
        self.parts.append(asdict(spec))
        self.receipts.append(receipt)
        self.path.unlink()  # verified remote receipt, not merely a FUSE close
        self.path = None

    def close(self):
        if self.stream:
            self.stream.close()
            self.stream = None


def archive_stream(stream, cache: LocalObjectCache, scratch: Path, *,
                   part_bytes: int = 256 * MiB) -> tuple[dict, list[dict]]:
    writer = PartWriter(cache, scratch, part_bytes)
    input_bytes = 0
    try:
        with gzip.GzipFile(fileobj=writer, mode="wb", compresslevel=1, mtime=0, filename="") as compressed:
            while data := stream.read(4 * MiB):
                input_bytes += len(data)
                compressed.write(data)
        writer.commit_part()
    finally:
        writer.close()
    return {"schema_version": ARCHIVE_SCHEMA, "codec": "gzip", "parts": writer.parts,
            "compressed_bytes": writer.total, "input_bytes": input_bytes}, writer.receipts


def validate_archive(value: dict) -> list[ObjectSpec]:
    if (not isinstance(value, dict) or value.get("schema_version") != ARCHIVE_SCHEMA
            or value.get("codec") != "gzip" or not value.get("parts")
            or type(value.get("input_bytes")) is not int or value["input_bytes"] < 0):
        raise ContractError("invalid chunked archive descriptor")
    parts = [ObjectSpec.from_dict(item) for item in value["parts"]]
    if sum(p.size_bytes for p in parts) != value.get("compressed_bytes"):
        raise ContractError("archive part sizes do not match descriptor")
    return parts


class PartReader(io.RawIOBase):
    def __init__(self, cache: LocalObjectCache, parts: list[ObjectSpec]):
        super().__init__()
        self.cache, self.parts = cache, iter(parts)
        self.lease = self.stream = None

    def readable(self):
        return True

    def readinto(self, buffer):
        while True:
            if self.stream is None:
                spec = next(self.parts, None)
                if spec is None:
                    return 0
                self.lease = ExitStack()
                try:
                    path = self.lease.enter_context(self.cache.acquire(spec))
                    self.stream = self.lease.enter_context(path.open("rb"))
                except BaseException:
                    self.lease.close()
                    self.lease = None
                    raise
            size = self.stream.readinto(buffer)
            if size:
                return size
            self.lease.close()
            self.lease = self.stream = None

    def close(self):
        if self.lease:
            self.lease.close()
            self.lease = self.stream = None
        super().close()


class LimitedReader:
    def __init__(self, stream, expected: int):
        self.stream, self.expected, self.count = stream, expected, 0

    def read(self, size: int = -1):
        # Never let a malformed descriptor turn a gzip bomb into an unbounded
        # allocation or disk write, including through a caller's read(-1).
        maximum = min(4 * MiB if size < 0 else size, self.expected - self.count + 1)
        data = self.stream.read(maximum)
        self.count += len(data)
        if self.count > self.expected:
            raise ContractError("archive expanded beyond its declared size")
        return data


def extract_archive(archive: dict, cache: LocalObjectCache, destination: Path) -> None:
    """Stream archive parts through a bounded reader, including gzip CRC/EOF."""
    import shutil
    parts = validate_archive(archive)
    if destination.exists() or destination.is_symlink():
        raise ContractError("archive destination must be new")
    require_local_filesystem(destination)
    try:
        with io.BufferedReader(PartReader(cache, parts), buffer_size=MiB) as source:
            with gzip.GzipFile(fileobj=source, mode="rb") as gzip_stream:
                stream = LimitedReader(gzip_stream, archive["input_bytes"])
                extract_tar(stream, destination)
                # Tar parsers stop at an end block; still verify gzip CRC and
                # consume all parts instead of trusting an unobserved suffix.
                while stream.read(MiB):
                    pass
                if stream.count != archive["input_bytes"]:
                    raise ContractError("archive expanded size differs from descriptor")
    except BaseException:
        if destination.is_dir() and not destination.is_symlink():
            shutil.rmtree(destination)
        raise
