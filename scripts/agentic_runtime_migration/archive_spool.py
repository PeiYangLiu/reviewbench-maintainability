"""Bounded tmpfs image spooling; RAM and the one disk cache copy are both reserved."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import uuid

from reviewbench_maintainability.agentic.contracts import ContractError, atomic_json
from reviewbench_maintainability.agentic.resources import GiB, ResourceRequest, file_lock, process_identity

SCHEMA = "reviewbench-bounded-image-spool/v1"
MiB = 1024**2
MAX_MEMORY_SPOOL_MB = 8192


def archive_request(image_size, memory_spool_mb=None):
    if memory_spool_mb is None:
        return ResourceRequest("public-input-image-archive", cpus=2, memory_bytes=2 * GiB,
                               scratch_bytes=max(4 * GiB, image_size * 2))
    if type(memory_spool_mb) is not int or not 1 <= memory_spool_mb <= MAX_MEMORY_SPOOL_MB:
        raise ContractError("memory image spool must be an integer from 1 to 8192 MiB")
    limit = memory_spool_mb * MiB
    # The gzip buffer and remote readback share one tmpfs file, charged to RAM.
    # Docker's classic exporter/importer still stages uncompressed tar blobs.
    # Reserve their image-size estimate plus explicit tar/metadata headroom;
    # verify the actual outer-tar staging size before loading. This is not a
    # claim that Docker save/load use no data-disk temporary storage.
    staging = image_size + max(256 * MiB, (image_size + 3) // 4)
    return ResourceRequest("public-input-image-archive", cpus=2, memory_bytes=2 * GiB + limit,
                           scratch_bytes=staging)


def docker(*args):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise ContractError("image archive Docker operation failed: " + result.stderr[-2000:])
    return result.stdout.strip()


def remove_pin(pin):
    """Remove only our unstarted image reference, never a running task container."""
    if (not isinstance(pin.get("token"), str) or not re.fullmatch(r"[0-9a-f]{32}", pin["token"])
            or pin.get("name") != "rb-archive-pin-" + pin["token"]
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", pin.get("image", ""))):
        raise ContractError("image pin descriptor changed")
    result = subprocess.run(["docker", "container", "inspect", pin["name"]],
                            capture_output=True, text=True, timeout=30)
    if result.returncode and "no such" in result.stderr.lower():
        return "absent"
    if result.returncode:
        raise ContractError("image pin observation failed: " + result.stderr[-1000:])
    rows = json.loads(result.stdout)
    if (len(rows) != 1 or rows[0]["Image"] != pin["image"]
            or rows[0]["Config"].get("Labels", {}).get("reviewbench.archive-spool") != pin["token"]
            or rows[0]["State"]["Status"] != "created" or rows[0]["State"]["Running"]):
        raise ContractError("image pin identity or unstarted state changed")
    docker("rm", rows[0]["Id"])
    return "removed"


def prepare_root(root=None):
    root = Path(root or ("/dev/shm/reviewbench-image-archives-" + str(os.getuid())))
    if not root.is_absolute() or root.is_symlink():
        raise ContractError("memory spool root must be absolute and not redirected")
    parent = root if root.exists() else root.parent
    observed = subprocess.run(["findmnt", "-no", "FSTYPE", "--target", str(parent)],
                              capture_output=True, text=True, timeout=10)
    if observed.returncode or observed.stdout.strip() != "tmpfs":
        raise ContractError("memory image spool requires an actual tmpfs filesystem")
    root.mkdir(mode=0o700, exist_ok=True)
    if root.stat().st_uid != os.getuid() or stat.S_IMODE(root.stat().st_mode) != 0o700:
        raise ContractError("memory image spool root is not private to its owner")
    lock = root / ".lock"
    if lock.is_symlink() or (lock.exists() and not lock.is_file()):
        raise ContractError("memory image spool lock redirected")
    with file_lock(root / ".lock"):
        marker = root / "OWNER.json"
        if marker.is_symlink():
            raise ContractError("memory image spool marker redirected")
        if not marker.exists():
            if {p.name for p in root.iterdir()} != {".lock"}:
                raise ContractError("memory image spool root was not empty")
            atomic_json(marker, {"schema_version": SCHEMA, "uid": os.getuid()})
        if json.loads(marker.read_text()) != {"schema_version": SCHEMA, "uid": os.getuid()}:
            raise ContractError("memory image spool marker changed")
        reclaimed = []
        for folder in sorted(root.glob("job-*")):
            if not re.fullmatch(r"job-[0-9a-f]{32}", folder.name) or folder.is_symlink() or not folder.is_dir():
                raise ContractError("unknown memory image spool entry")
            owner_file = folder / "owner.json"
            if owner_file.is_symlink() or not owner_file.is_file():
                raise ContractError("memory image spool has no original owner record")
            owner = json.loads(owner_file.read_text())
            if (owner["schema_version"] != SCHEMA or owner["uid"] != os.getuid()
                    or type(owner.get("pid")) is not int or owner["pid"] <= 0
                    or not isinstance(owner.get("process_start"), str)
                    or not re.fullmatch(r"\d+", owner["process_start"])
                    or type(owner.get("maximum_bytes")) is not int
                    or not 0 < owner["maximum_bytes"] <= MAX_MEMORY_SPOOL_MB * MiB):
                raise ContractError("memory image spool owner changed")
            if owner.get("pin") and owner["pin"]["token"] != folder.name.removeprefix("job-"):
                raise ContractError("memory image spool pin belongs to another entry")
            actual = process_identity(owner["pid"])
            if actual == "unknown" or actual == owner["process_start"]:
                continue
            if actual is not None and owner["process_start"] in (None, "unknown"):
                continue
            # A missing process or a verified reused PID is terminal. An
            # ambiguous process observation above retains the buffer.
            entries = list(folder.iterdir())
            if any(p.is_symlink() or not p.is_file() or p.name not in {"owner.json", "image.tar.gz"} for p in entries):
                raise ContractError("memory image spool contains an unbound file")
            if owner.get("pin"):
                remove_pin(owner["pin"])
            size = sum(p.stat().st_size for p in entries)
            shutil.rmtree(folder)
            reclaimed.append({"path": str(folder), "bytes": size, "pid": owner["pid"]})
    return root, reclaimed


class BoundedWriter:
    def __init__(self, stream, maximum):
        self.stream, self.maximum, self.bytes_written = stream, maximum, 0

    def write(self, data):
        if self.bytes_written + len(data) > self.maximum:
            raise ContractError("memory image archive exceeds its declared byte limit")
        count = self.stream.write(data)
        if count != len(data):
            raise ContractError("short image archive write")
        self.bytes_written += count
        return count

    def flush(self):
        self.stream.flush()

    def tell(self):
        return self.bytes_written


@contextmanager
def memory_spool(root, maximum, image, lease):
    if shutil.disk_usage(root).free < maximum + MiB:
        raise ContractError("tmpfs has insufficient reserved image spool capacity")
    token = uuid.uuid4().hex
    folder = root / ("job-" + token)
    pin = {"name": "rb-archive-pin-" + token, "image": image, "token": token}
    owner = {"schema_version": SCHEMA, "uid": os.getuid(), "pid": os.getpid(),
             "process_start": process_identity(os.getpid()), "maximum_bytes": maximum, "pin": pin}
    if owner["process_start"] in (None, "unknown"):
        raise ContractError("memory image spool needs a verified owner process")
    with file_lock(root / ".lock"):
        folder.mkdir(mode=0o700)
        atomic_json(folder / "owner.json", owner)
    record = {"kind": "tmpfs", "path": str(folder / "image.tar.gz"), "byte_limit": maximum,
              "pin": pin, "pin_started": False, "buffer_removed": False}
    try:
        container = docker("create", "--name", pin["name"], "--read-only", "--network", "none",
                           "--label", "reviewbench.archive-spool=" + token,
                           "--label", "reviewbench.resource-lease=" + lease,
                           "--entrypoint", "/__reviewbench_archive_pin_never_run__", image)
        rows = json.loads(docker("container", "inspect", container))
        if (len(rows) != 1 or rows[0]["Image"] != image
                or rows[0]["State"]["Status"] != "created"
                or not rows[0]["HostConfig"]["ReadonlyRootfs"]):
            raise ContractError("existing image was not retained by an unstarted read-only pin")
        record["pin_id"] = container
        yield Path(record["path"]), record
    finally:
        # Keep the lease until both the image reference and RAM buffer close.
        # If observation fails, the durable owner record remains for explicit
        # inspection or a later verified dead-owner cleanup.
        record["pin_cleanup"] = remove_pin(pin)
        with file_lock(root / ".lock"):
            assert json.loads((folder / "owner.json").read_text()) == owner
            shutil.rmtree(folder)
        record["buffer_removed"] = True
