"""Seed original checksum-bound CMake downloads without altering build commands."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import shlex

TASK = "github__argotorg__solidity__pull_14612__comment_1603608333__complexity"
START = "cdcd57f30bd2727f4ca82aeb23b26a8de57c65d2"
PLAN = "06655baa493a87a55b51ab02e541b65e58ebd9f186f6fecb4fc5025b8b2c2346"
SCHEMA = "reviewbench-solidity-original-downloads/v1"
SOURCE = "/opt/reviewbench-assets/solidity-downloads"
TOOLS = "/opt/reviewbench-assets/solidity-tools"
SPEC = "/opt/reviewbench-assets/solidity-downloads.json"
FILES = {
    "fmt-9.1.0.tar.gz": {
        "sha256": "5dea48d1fcddc3ec571ce2058e13910a0d4a6bab4cc09a809d8b1dd1c88ae6f2",
        "destination": "deps/downloads/fmt-9.1.0.tar.gz",
        "declaration": "cmake/fmtlib.cmake",
        "declaration_sha256": "74e6c354f29d1e45b226d2abb920bd5ebe43e0fef228a6d0ae157c07136623da",
    },
    "range-v3-0.12.0.tar.gz": {
        "sha256": "015adb2300a98edfceaf0725beec3337f542af4915cec4d0b89fa0886f4ba9cb",
        "destination": "deps/downloads/range-v3-0.12.0.tar.gz",
        "declaration": "cmake/range-v3.cmake",
        "declaration_sha256": "2825ff72eb34af0fb2cc82f62f5689f8165905a7c21d60cd8e2fb20ec1467d43",
    },
    "json.hpp": {
        "sha256": "9bea4c8066ef4a1c206b2be5a36302f8926f7fdc6087af5d20b417d0cf103ea6",
        "destination": "deps/nlohmann/nlohmann/json.hpp",
        "declaration": "cmake/nlohmann-json.cmake",
        "declaration_sha256": "076f54782120eb32d2c8d4a3d1a11aa28d9d2458bd162471aebab8c2c3ab76aa",
    },
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def seed(spec, workspace):
    if set(spec) != {"schema_version", "source"} or spec["schema_version"] != SCHEMA:
        raise ValueError("invalid original Solidity download specification")
    root = Path(workspace).resolve()
    source = Path(spec["source"])
    if not source.is_absolute() or source.is_symlink() or not source.is_dir():
        raise ValueError("original Solidity download source is missing or linked")
    validated = []
    for name, item in FILES.items():
        declaration = root / item["declaration"]
        incoming = source / name
        destination = root / item["destination"]
        if (declaration.is_symlink() or not declaration.is_file()
                or not declaration.resolve().is_relative_to(root)
                or digest(declaration) != item["declaration_sha256"]):
            raise ValueError("current Solidity dependency declaration changed: " + item["declaration"])
        if incoming.is_symlink() or not incoming.is_file() or digest(incoming) != item["sha256"]:
            raise ValueError("original Solidity dependency checksum differs: " + name)
        if destination.is_symlink() or any(p.is_symlink() for p in destination.parents if p.is_relative_to(root)):
            raise ValueError("Solidity dependency destination is linked")
        if destination.exists() and (not destination.is_file() or digest(destination) != item["sha256"]):
            raise ValueError("candidate-owned Solidity dependency differs: " + name)
        validated.append((incoming, destination, item["sha256"]))
    for incoming, destination, checksum in validated:
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(incoming, destination)
        assert digest(destination) == checksum
    return [str(destination.relative_to(root)) for _, destination, _ in validated]


def adapt(ctx, task, plan):
    if (task["repo"], plan.get("input_index")) != ("argotorg/solidity", 33):
        return None
    from reviewbench_maintainability.agentic.assets import asset_descriptor
    from reviewbench_maintainability.agentic.contracts import ContractError, atomic_json, canonical_hash, file_hash
    from reviewbench_maintainability.agentic.snapshots import git

    if task["id"] != TASK or task["start_sha"] != START or canonical_hash(plan) != PLAN:
        raise ContractError("original Solidity task/build-plan binding differs")
    reference = ctx.config.get("migration", {}).get("solidity_download_asset")
    if not isinstance(reference, dict):
        raise ContractError("Solidity requires its original checksum-bound CMake downloads")
    descriptor = asset_descriptor(reference, ctx.output)
    if descriptor.get("source_kind") != "dependency" or descriptor.get("files") != 3:
        raise ContractError("Solidity download archive contains unexpected inputs")
    for item in FILES.values():
        original = git(ctx.repo(task), "show", START + ":" + item["declaration"])
        if hashlib.sha256(original).hexdigest() != item["declaration_sha256"]:
            raise ContractError("original Solidity CMake dependency declaration differs")
    tools = ctx.bundle(Path(__file__).parent, ["native_solidity.py"], role="toolchain")
    path = ctx.output / "solidity-downloads.json"
    atomic_json(path, {"schema_version": SCHEMA, "source": SOURCE})
    command = shlex.join(["python3", TOOLS + "/native_solidity.py",
                         "--spec", SPEC, "--workspace", "/workspace"])
    assert plan["configure_command"].startswith("set -e; ")
    public, separator, private = plan["build_command"].partition("; g++-11 ")
    if not separator or "/harnesses/" not in private:
        raise ContractError("original Solidity private harness build is not accounted for")
    return {
        "configure": "set -e; " + command + "; " + plan["configure_command"][len("set -e; "):],
        "build": public,
        "grading_bootstrap": [plan["build_command"]],
        "scratch_paths": ["deps/downloads", "deps/nlohmann"],
        "build_scratch_mb_extra": (descriptor["archive"]["input_bytes"] + 1024**2 - 1) // 1024**2,
        "assets": [
            {**reference, "kind": "archive", "visibility": "developer", "target": SOURCE},
            {**tools, "kind": "archive", "visibility": "developer", "target": TOOLS},
            {"path": str(path), "sha256": file_hash(path), "visibility": "developer", "target": SPEC},
        ],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    paths = seed(json.loads(args.spec.read_text()), args.workspace)
    print("SOLIDITY_ORIGINAL_DOWNLOADS " + json.dumps({"files": paths, "checksums": "original"}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
