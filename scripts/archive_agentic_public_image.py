#!/usr/bin/env python3
"""Archive a new public-input image in the independent archive pool and verify loadability."""
import argparse
from contextlib import contextmanager
from dataclasses import asdict
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import time

sys.path.insert(0, os.environ.get("REVIEWBENCH_ENGINE_SRC", str(Path(__file__).resolve().parents[1] / "src")))
from reviewbench_maintainability.agentic.cli import load_config
from reviewbench_maintainability.agentic.contracts import atomic_json, file_hash
from reviewbench_maintainability.agentic.execution import ExecutionServices
from reviewbench_maintainability.agentic.storage import describe_file
from agentic_runtime_migration import archive_spool


def save_image(image, target, stderr_path, maximum=None):
    command = ["docker", "save", image]
    with stderr_path.open("xb") as stderr, target.open("xb") as stream:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=stderr)
        try:
            destination = archive_spool.BoundedWriter(stream, maximum) if maximum else stream
            with gzip.GzipFile(filename="", mode="wb", fileobj=destination, compresslevel=1, mtime=0) as compressed:
                shutil.copyfileobj(process.stdout, compressed, 1024**2)
            code = process.wait()
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
    assert code == 0
    return command, code


@contextmanager
def disk_spool(output):
    yield output / "image.tar.gz", {"kind": "disk", "buffer_removed": False}


def archive(config_path, preparation_path, output, *, memory_spool_mb=None, tmpfs_root=None):
    existed = output.exists()
    try:
        if tmpfs_root is not None and memory_spool_mb is None:
            raise ValueError("tmpfs-root requires an explicit memory-spool-mb bound")
        return _archive(config_path, preparation_path, output,
                        memory_spool_mb=memory_spool_mb, tmpfs_root=tmpfs_root)
    except Exception as error:
        path = output / "publication.json"
        if not existed and path.is_file():
            record = json.loads(path.read_text())
            record.update(status="failed", exit_status=1, completed_at=time.time(),
                          error=type(error).__name__ + ": " + str(error))
            atomic_json(path, record)
        raise


def _archive(config_path, preparation_path, output, *, memory_spool_mb=None, tmpfs_root=None):
    preparation = json.loads(preparation_path.read_text())
    assert preparation["status"] == "prepared_needs_original_calibration"
    binding = preparation["immutable_input_image"]
    assert binding["status"] == "built_and_verified"
    image = binding["image"]
    audit = Path(binding["audit"]["path"])
    assert file_hash(audit) == binding["audit"]["sha256"]
    inspect = json.loads(subprocess.check_output(["docker", "image", "inspect", image]))[0]
    assert inspect["Id"] == image
    output.mkdir(parents=True)
    config = load_config(config_path)
    services = ExecutionServices(config)
    request = archive_spool.archive_request(inspect["Size"], memory_spool_mb)
    root, reclaimed = archive_spool.prepare_root(tmpfs_root) if memory_spool_mb is not None else (None, [])
    started = time.time()
    record = {"status": "running", "image": image, "producer_record_sha256": file_hash(preparation_path),
              "model_calls": 0, "original_SUT_calls": 0, "started_at": started,
              "resource_request": asdict(request), "reclaimed_dead_spools": reclaimed}
    atomic_json(output / "publication.json", record)
    with services.gate.acquire(request, uses_containers=False, timeout=1800) as lease:
        os.sched_setaffinity(0, set(sorted(os.sched_getaffinity(0))[-2:]))
        maximum = memory_spool_mb * 1024**2 if memory_spool_mb is not None else None
        spool = (archive_spool.memory_spool(root, maximum, image, lease["lease"])
                 if maximum is not None else disk_spool(output))
        with spool as (target, spool_record):
            record.update(resource_lease=lease["lease"], admission_wait_seconds=lease["wait_seconds"],
                          spool=spool_record)
            atomic_json(output / "publication.json", record)
            command, code = save_image(image, target, output / "save.stderr", maximum)
            if maximum is None:
                spec, receipt = services.cache.publish(target)
                download_receipt = None
            else:
                # The daemon image is already local. Archive bodies are cold
                # backups, not dependency hot-cache entries: avoid retaining a
                # second disk copy of the compressed image.
                spec = describe_file(target)
                assert spec.size_bytes <= maximum
                with services.cache.transfers.acquire():
                    receipt = services.cache.remote.put(target, spec)
                atomic_json(output / "archive-object.json", {"object": asdict(spec), "receipt": receipt,
                                                            "save_command": command, "save_exit_status": code,
                                                            "readback_receipt": None, "hot_cache_body_seeded": False})
                target.unlink()  # committed remote object exists; reuse one RAM buffer
                with services.cache.transfers.acquire():
                    # Azure get checks declared size BEFORE each write, then
                    # verifies SHA256 and MD5. No unbounded download buffer.
                    download_receipt = services.cache.remote.get(spec, target)
                assert describe_file(target) == spec
            atomic_json(output / "archive-object.json", {"object": asdict(spec), "receipt": receipt,
                                                        "save_command": command, "save_exit_status": code,
                                                        "readback_receipt": download_receipt,
                                                        "hot_cache_body_seeded": maximum is None})
            # Hold the image pin through docker load. Its layers remain live,
            # so loading this same image does not require a second layer copy.
            if maximum is None:
                with services.cache.acquire(spec) as local:
                    verification = verify_and_load(image, inspect, local)
            else:
                verification = verify_and_load(image, inspect, target, request.scratch_bytes)
        actual_layers, load, staging_bytes = verification
        after = json.loads(subprocess.check_output(["docker", "image", "inspect", image]))[0]
        assert after["Id"] == image and after["RootFS"]["Layers"] == inspect["RootFS"]["Layers"]
        record.update(status="archived_and_loaded", exit_status=0, save_command=command, save_exit_status=code,
                      archive_object=asdict(spec), receipt=receipt, layer_diff_ids=actual_layers,
                      all_layers_match=True, load_command=load.args, load_stdout=load.stdout,
                      load_stderr=load.stderr, load_exit_status=load.returncode,
                      completed_at=time.time(), seconds=time.time()-started, spool=spool_record,
                      outer_tar_staging_bytes=staging_bytes, readback_receipt=download_receipt,
                      hot_cache_body_seeded=maximum is None,
                      disk_reservation_basis="daemon tar staging estimate, checked before load" if maximum else "original disk-spool estimate")
        atomic_json(output / "publication.json", record)
    print("PUBLIC_INPUT_IMAGE_ARCHIVED_AND_LOADED bytes=" + str(spec.size_bytes)
          + " layers=" + str(len(actual_layers)) + " new_API_SUT_calls=0", flush=True)
    return record


def verify_and_load(image, inspect, local, staging_limit=None):
    hashes, documents, staging_bytes = {}, {}, 0
    with tarfile.open(local, "r:gz") as saved:
        for member in saved:
            # Each outer tar file/directory can occupy its rounded blocks in
            # Docker's import staging tree. Inner layer tars remain opaque.
            staging_bytes += ((member.size + 4095) // 4096 * 4096 if member.isfile() else 4096)
            if not member.isfile():
                continue
            digest = hashlib.sha256()
            data = bytearray() if member.size <= 1024**2 else None
            source = saved.extractfile(member)
            while chunk := source.read(1024**2):
                digest.update(chunk)
                if data is not None:
                    data.extend(chunk)
            hashes[member.name] = digest.hexdigest()
            if data is not None:
                try:
                    documents[member.name] = json.loads(data)
                except (ValueError, UnicodeDecodeError):
                    pass
    manifest = documents["manifest.json"]
    assert len(manifest) == 1
    config_name, layers = manifest[0]["Config"], manifest[0]["Layers"]
    assert hashes[config_name] == image.removeprefix("sha256:")
    actual_layers = ["sha256:" + hashes[name] for name in layers]
    assert actual_layers == documents[config_name]["rootfs"]["diff_ids"] == inspect["RootFS"]["Layers"]
    if staging_limit is not None and staging_bytes + 64 * 1024**2 > staging_limit:
        raise ValueError("actual image tar staging exceeds its declared disk reservation")
    load = subprocess.run(["docker", "load", "-i", str(local)], capture_output=True, text=True, timeout=600)
    assert load.returncode == 0, load.stderr
    return actual_layers, load, staging_bytes


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--preparation", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--memory-spool-mb", type=int, help="bounded tmpfs gzip buffer, charged to the shared RAM limit")
    parser.add_argument("--tmpfs-root", type=Path)
    args = parser.parse_args()
    archive(args.config, args.preparation, args.out_dir,
            memory_spool_mb=args.memory_spool_mb, tmpfs_root=args.tmpfs_root)
