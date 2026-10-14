"""Reuse archived native source roots after checking every current pinned blob."""
import hashlib
import json
import os
from pathlib import Path
import stat

from reviewbench_maintainability.agentic.assets import asset_descriptor
from reviewbench_maintainability.agentic.contracts import (
    ContractError, atomic_json, component, file_hash, repo_path,
)
from reviewbench_maintainability.agentic.snapshots import tree_files, tree_hash

OPTION = "native_prepared_submodule_archives"


def require(value, message):
    if not value:
        raise ContractError("prepared native source reuse: " + message)


def referenced(pointer):
    require(isinstance(pointer, dict) and set(pointer) == {"path", "sha256"},
            "an explicit archive-record pointer is required")
    path = Path(pointer["path"])
    require(path.is_absolute() and path.is_file() and not path.is_symlink()
            and path.stat().st_size <= 16 * 1024**2
            and file_hash(path) == pointer["sha256"], "record path/hash changed")
    return json.loads(path.read_text())


def pinned_tree(root, name, records):
    """No export, checkout or compilation: compare real Git blobs to restored bytes."""
    from .native_submodules import readonly_git
    require(root.is_absolute() and root.is_dir() and not root.is_symlink(),
            "physical restoration root is absent or linked")
    expected = set()
    for record in records:
        if record["path"] != name and not record["path"].startswith(name + "/"):
            continue
        prefix = "" if record["path"] == name else record["path"][len(name)+1:] + "/"
        entries = readonly_git(Path(record["repository"]), "ls-tree", "-r", "-z", record["commit"])
        for entry in entries.split(b"\0"):
            if not entry:
                continue
            metadata, raw_name = entry.split(b"\t", 1)
            mode, kind, blob = metadata.decode().split()
            if kind == "commit":
                continue
            require(kind == "blob" and mode in {"100644", "100755", "120000"},
                    "Git source node type differs")
            relative = repo_path(prefix + raw_name.decode())
            expected.add(relative)
            path = root
            parts = relative.split("/")
            for part in parts[:-1]:
                path /= part
                require(path.is_dir() and not path.is_symlink(), "source parent is linked or absent")
            path /= parts[-1]
            try:
                info = path.lstat()
            except FileNotFoundError as exc:
                raise ContractError("prepared native source reuse: original blob missing: " + relative) from exc
            if mode == "120000":
                require(stat.S_ISLNK(info.st_mode), "original symbolic link changed")
                data = os.readlink(path).encode()
            else:
                require(stat.S_ISREG(info.st_mode) and not path.is_symlink(),
                        "original regular source became another node type")
                require(stat.S_IMODE(info.st_mode) == int(mode, 8) & 0o777,
                        "original source permissions changed")
                data = path.read_bytes()
            observed = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
            require(observed == blob, "restored source differs from its exact Git blob: " + relative)
    require(expected and set(tree_files(root)) == expected,
            "archived root omits/adds tracked source or recursive dependencies")
    return expected


def reuse(ctx, task, paths, pins, records):
    settings = ctx.config.get("migration", {}).get(OPTION, {})
    require(isinstance(settings, dict), "archive configuration is not a map")
    pointer = settings.get(task["repo"])
    if pointer is None:
        return None
    publication = referenced(pointer)
    require(publication.get("status") ==
            "all_original_recursive_source_roots_archived_and_independently_restored"
            and publication.get("id") == task["id"] and publication.get("full_scope") == 402
            and publication.get("new_project_build_test_model_judge_calls") == 0,
            "archive publication is not the original task's complete source record")
    prepared = referenced(publication["source_preparation"])
    require(prepared["status"] == "exact_recursive_original_git_inputs_prepared"
            and prepared["id"] == task["id"] and prepared["full_scope"] == 402
            and prepared["closure"] == records and prepared["new_project_build_test_model_judge_calls"] == 0,
            "current exact recursive Git closure differs from the archived preparation")
    require(set(publication["roots"]) == set(paths)
            and publication["modules"] == len(records)
            and publication["original_files"] == sum(row["files"] for row in records),
            "archive root/module/file scope differs")
    receipts = {row["sha256"]: row for row in publication["independent_get_receipts"]}
    assets, observations = {}, {}
    for name in paths:
        row = publication["roots"][name]
        descriptor = asset_descriptor(row["asset"], ctx.output)
        require(row["commit"] == pins[name] and descriptor.get("git_commit") == pins[name]
                and descriptor["source_kind"] == "submodule"
                and descriptor["tree_sha256"] == row["tree_sha256"]
                and descriptor["files"] == row["files"], "archive commit/tree identity changed")
        for part in descriptor["archive"]["parts"]:
            receipt = receipts.get(part["sha256"], {})
            require(all(receipt.get(key) == part[key] for key in ("sha256", "size_bytes", "md5_base64")),
                    "source archive lacks its independent complete GET receipt")
        physical = Path(row["physical_restoration"])
        names = pinned_tree(physical, name, records)
        require(len(names) == descriptor["files"] and tree_hash(physical) == descriptor["tree_sha256"],
                "physical restoration differs from the complete archived tree")
        assets[name] = row["asset"]
        observations[name] = {"files": len(names), "commit": pins[name],
                              "tree_sha256": descriptor["tree_sha256"],
                              "asset": row["asset"], "actual_Git_blobs_compared": True}
    atomic_json(ctx.output / "native-source-reuse" / (component(task["id"]) + ".json"), {
        "status": "exact_archived_sources_reused_without_export",
        "id": task["id"], "full_scope": 402, "publication": pointer,
        "roots": observations, "new_fetch_export_upload_build_test_model_judge_calls": 0,
    })
    return assets
