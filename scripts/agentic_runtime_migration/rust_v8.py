"""Stage the original rusty_v8 release library for offline native builds.

Only the build script's existing local-archive input changes. The library is
the original URL/version/platform, newly hash-pinned from the official asset;
the pin is not represented as a checksum recorded by the historical Oracle.
"""
from __future__ import annotations

from pathlib import Path
import shlex
import shutil
import tempfile

from reviewbench_maintainability.agentic.contracts import ContractError, atomic_json, file_hash
from reviewbench_maintainability.agentic.resources import file_lock

TASK = "github__phylum-dev__cli__pull_407__comment_892231201__api_design"
REPO = "phylum-dev/cli"
START = "aa11c52f35e355f9e75fa4cca86878b46db32187"
VERSION = "0.42.1"
TARGET_TRIPLE = "x86_64-unknown-linux-gnu"
NAME = "librusty_v8_release_x86_64-unknown-linux-gnu.a"
SHA256 = "ac0a88909f39979792a234e8c15665e7068afdf934d092387c8d40635b9efe2e"
SIZE = 78632406
URL = "https://github.com/denoland/rusty_v8/releases/download/v0.42.1/" + NAME
TARGET = "/opt/reviewbench-assets/rusty-v8/0.42.1-linux-x86_64"


def checked_input(ctx):
    value = ctx.config.get("rust_v8_archive")
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ContractError("original V8 archive requires an explicit absolute local path")
    source = Path(value)
    if (source.is_symlink() or not source.is_file()
            or source.stat().st_size != SIZE or file_hash(source) != SHA256):
        raise ContractError("original V8 version/platform archive is absent or has changed")
    with source.open("rb") as stream:
        if stream.read(8) != b"!<arch>\n":
            raise ContractError("original V8 input is not a self-contained static archive")
    return source


def binding(ctx, task):
    if task.get("id") != TASK:
        return [], {}, []
    if task.get("repo") != REPO or task.get("start_sha") != START:
        raise ContractError("original V8 task/source binding changed")
    source = checked_input(ctx)
    with file_lock(ctx.output / "locks" / "rusty-v8-0.42.1-linux-x86_64"):
        with tempfile.TemporaryDirectory(prefix="rusty-v8-original-", dir=ctx.scratch) as temporary:
            root = Path(temporary)
            archive = root / NAME
            shutil.copyfile(source, archive)
            archive.chmod(0o444)
            if archive.stat().st_size != SIZE or file_hash(archive) != SHA256:
                raise ContractError("staged original V8 static library differs from its pin")
            pins = {
                "version": VERSION, "target": TARGET_TRIPLE, "profile": "release",
                "sha256": SHA256, "bytes": SIZE, "original_url": URL,
                "input_kind": "official original-version build input, newly checksum-pinned",
            }
            atomic_json(root / "pins.json", pins)
            asset = ctx.bundle(root, role="toolchain")
            checked_input(ctx)
    atomic_json(ctx.output / "rust-v8" / "asset-binding.json", {
        "task_id": TASK, "input": str(source), **pins, "asset": asset,
        "source_builds": 0, "SUT_calls": 0, "model_calls": 0,
    })
    location = TARGET + "/" + NAME
    # In v8 0.42.1 non-HTTP inputs are direct filesystem paths, not file:// URLs.
    environment = {"RUSTY_V8_ARCHIVE": location}
    check = "printf '%s  %s\\n' " + shlex.quote(SHA256) + " " + shlex.quote(location) + " | sha256sum -c -"
    return [{**asset, "kind": "archive", "visibility": "developer", "target": TARGET}], environment, [check]
