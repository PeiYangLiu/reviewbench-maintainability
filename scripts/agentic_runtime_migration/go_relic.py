"""Explicit, hash-bound external Relic assets for whole-candidate cgo builds.

Selection depends on frozen module inputs and an audited dependency descriptor,
never on a start/golden label or a test result. No production file is overlaid.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from reviewbench_maintainability.agentic.assets import ASSET_SCHEMA
from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash
from reviewbench_maintainability.agentic.snapshots import git
from .go_dependencies import parse_mod
from .go_public_inputs import pinned_file

SCHEMA = "reviewbench-go-relic-dependency/v1"
MODULE = "github.com/onflow/flow-go/crypto"
VERSION = "v0.24.9"
ZIP_SHA256 = "c2e076737747e0a6bb3a7375636a438d9f3ea9369f89a1f852b326f6bc49c97a"
ZIP_H1 = "h1:0EQp+kSZYJepMIiSypfJVe7tzsPcb6UXOdOtsTCDhBs="
RELIC_COMMIT = "7d885d1ba34be61bf22190943a73549a910c1714"
RELIC_TREE = "51a106511826bbc172b9a371519c3d2ec1cd1d09"
BUILD_SCRIPTS = {
    "build_dependency.sh": "2dcba30f5b6c3989b808e5aad6148cca2401301077dc1f901397e0f863436ee0",
    "relic_build.sh": "536a9a8e9fb8cb0a9acc0523aea8ee441df01257ad17e9022b7ec20cf109153e",
}
MOUNT = "/opt/reviewbench-assets/relic"
MAX_RUNTIME_BYTES = 64 * 1024**2


def cmake_definitions() -> list[str]:
    """Verbatim configuration from crypto v0.24.9 relic_build.sh, x86_64 branch."""
    return [
        "-DCFLAGS=-O3 -funroll-loops -fomit-frame-pointer -march=westmere -mtune=native",
        "-DTIMER=CYCLE", "-DCHECK=OFF", "-DVERBS=OFF",
        "-DSHLIB=OFF", "-DSTLIB=ON", "-DRAND=HASHD", "-DSEED=",
        "-DALLOC=AUTO", "-DALIGN=1", "-DWSIZE=64", "-DBN_PRECI=1024",
        "-DBN_MAGNI=DOUBLE", "-DARITH=EASY", "-DFP_PRIME=381",
        "-DFP_PMERS=OFF", "-DFP_QNRES=ON",
        "-DEP_MIXED=ON", "-DEP_PLAIN=OFF", "-DEP_ENDOM=ON", "-DEP_SUPER=OFF",
        "-DEP_CTMAP=ON", "-DEP_METHD=JACOB;LWNAF;COMBS;INTER",
        "-DBN_KARAT=0", "-DBN_METHD=COMBA;COMBA;MONTY;SLIDE;BINAR;BASIC",
        "-DFP_KARAT=0", "-DFP_METHD=INTEG;INTEG;INTEG;MONTY;MONTY;JMPDS;SLIDE",
        "-DFPX_METHD=INTEG;INTEG;LAZYR", "-DPP_METHD=LAZYR;OATEP",
    ]


def frozen_crypto_binding(ctx, task: dict, module_cwd: str) -> dict:
    prefix = module_cwd + "/" if module_cwd else ""
    mod = git(ctx.repo(task), "show", task["start_sha"] + ":" + prefix + "go.mod")
    sums = git(ctx.repo(task), "show", task["start_sha"] + ":" + prefix + "go.sum")
    requires, replacements = parse_mod(mod.decode())
    if (requires.count((MODULE, VERSION)) != 1
            or any(row[0] == MODULE for row in replacements)):
        raise ContractError("go_relic_frozen_crypto_requirement_differs")
    values = [row.split()[2] for row in sums.decode().splitlines()
              if len(row.split()) == 3 and row.split()[:2] == [MODULE, VERSION]]
    if values != [ZIP_H1]:
        raise ContractError("go_relic_frozen_crypto_checksum_differs")
    return {
        "module": MODULE, "version": VERSION, "zip_sha256": ZIP_SHA256, "h1": ZIP_H1,
        "build_scripts": dict(BUILD_SCRIPTS),
        "go_mod_sha256": hashlib.sha256(mod).hexdigest(),
        "go_sum_sha256": hashlib.sha256(sums).hexdigest(),
    }


def has_relic_tag(flags: list[str]) -> bool:
    values = []
    for index, arg in enumerate(flags):
        if arg == "-tags" and index + 1 < len(flags):
            values.append(flags[index + 1])
        elif arg.startswith("-tags="):
            values.append(arg[len("-tags="):])
    return any("relic" in value.replace(",", " ").split() for value in values)


def runtime_file_records(value) -> list[tuple[str, str, bool]]:
    """Permit only the external library/header/license closure, not SUT outputs."""
    if not isinstance(value, list) or not 3 <= len(value) <= 256:
        raise ContractError("go_relic_runtime_file_manifest_invalid")
    records, total, seen = [], 0, set()
    for row in value:
        if not isinstance(row, dict):
            raise ContractError("go_relic_runtime_file_invalid")
        name, digest, size = row.get("path"), row.get("sha256"), row.get("bytes")
        allowed = isinstance(name, str) and (
            re.fullmatch(r"include/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.h", name)
            or name in {"generated/relic_conf.h", "lib/librelic_s.a",
                        "LICENSE", "LICENSE.Apache-2.0", "LICENSE.LGPL-2.1"}
        )
        if (not allowed or name in seen or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or type(size) is not int or not 0 < size <= MAX_RUNTIME_BYTES):
            raise ContractError("go_relic_runtime_file_invalid")
        seen.add(name)
        total += size
        records.append((name, digest, False))
    if (total > MAX_RUNTIME_BYTES
            or not {"include/relic.h", "generated/relic_conf.h", "lib/librelic_s.a"} <= seen):
        raise ContractError("go_relic_runtime_file_closure_invalid")
    return sorted(records)


def dependency(ctx, task: dict, contract):
    """Validate an explicitly selected preparation; leave other Go tasks unchanged."""
    migration = ctx.config.get("migration", {})
    if not isinstance(migration, dict):
        raise ContractError("go_relic_migration_configuration_invalid")
    selections = migration.get("go_relic_dependencies", {})
    if not isinstance(selections, dict):
        raise ContractError("go_relic_dependency_selection_invalid")
    if task["id"] not in selections:
        return None
    if not has_relic_tag(contract.build_flags):
        raise ContractError("go_relic_original_build_tag_missing")
    original = frozen_crypto_binding(ctx, task, contract.module_cwd)
    path, provenance = pinned_file(selections[task["id"]], "external-relic-dependency")
    value = json.loads(path.read_text())
    if (value.get("schema_version") != SCHEMA
            or value.get("platform") != "linux/amd64"
            or value.get("crypto") != original
            or value.get("relic_commit") != RELIC_COMMIT
            or value.get("relic_tree") != RELIC_TREE
            or value.get("cmake_definitions") != cmake_definitions()
            or value.get("build_status") != "built"
            or value.get("candidate_inputs_used") is not False):
        raise ContractError("go_relic_dependency_binding_differs")
    tree = canonical_hash(runtime_file_records(value.get("runtime_files")))
    asset_path, _ = pinned_file(value.get("runtime_asset"), "external-relic-runtime-archive")
    asset = json.loads(asset_path.read_text())
    if (asset.get("schema_version") != ASSET_SCHEMA
            or asset.get("source_kind") != "dependency"
            or asset.get("git_commit") != RELIC_COMMIT
            or asset.get("tree_sha256") != tree
            or asset.get("files") != len(value["runtime_files"])):
        raise ContractError("go_relic_runtime_archive_binding_differs")
    return {
        "asset": {**value["runtime_asset"], "kind": "archive", "visibility": "developer",
                  "target": MOUNT},
        "provenance": provenance,
    }


def cgo_environment(environment: dict[str, str], mount: str = MOUNT) -> dict[str, str]:
    """Append fixed search paths; retain caller flags and Go's default link flags."""
    if not re.fullmatch(r"/[A-Za-z0-9_./@-]+", mount) or ".." in Path(mount).parts:
        raise ContractError("go_relic_mount_invalid")
    if environment.get("CGO_ENABLED", "1") != "1":
        raise ContractError("go_relic_cgo_disabled")
    result = dict(environment)
    result["CGO_CPPFLAGS"] = " ".join(filter(None, [
        environment.get("CGO_CPPFLAGS", ""),
        f"-I{mount}/generated -I{mount}/include -I{mount}/include/low",
    ]))
    result["CGO_LDFLAGS"] = " ".join(filter(None, [
        environment.get("CGO_LDFLAGS", "-O2 -g"), f"-L{mount}/lib",
    ]))
    return result
