"""The pinned Serial Kokkos dependency and public header build for native 126."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shlex
import tempfile

from reviewbench_maintainability.agentic.contracts import (
    ContractError, atomic_json, canonical_hash, component, file_hash,
)
from .native_submodules import readonly_git


SOURCE_COMMIT = "1f5f4b6d82c07939f036263e4651d27a543c2049"
SOURCE_TREE = "8e07fa919bdc843af6576aaac1eb75d950d5d5be"
SOURCE_VERSION = "5.0.99"
CACHE_SHA256 = "0f809cd2882f9a93d9da39bb0398a3754a52ed266ad53c7084e4a5aaa2b2acd1"
HARNESS_CMAKE_SHA256 = "65a16a1d6c2f6df9aa0bb9b5d23bdf47afcd4c070893a9c5f9ecdcf8569720a9"
POLICY = "native-kokkos-source-install/v1"
INSTALL = "/opt/reviewbench-assets/kokkos-install"
PUBLIC = "/opt/reviewbench-assets/native-kokkos-public"
BUILD = "build-native-kokkos-public"
OLD_PREFIX = "{root}/cache/kokkos-" + SOURCE_COMMIT + "/install-oracle-gcc14"
HEADERS = (
    "kokkos-execution/execution_space.hpp",
    "kokkos-execution/parallel_for.hpp",
    "kokkos-execution/stdexec.hpp",
)


def cache_values(text):
    result = {}
    for line in text.splitlines():
        if not line or line.startswith(("#", "//")):
            continue
        match = re.fullmatch(r"([A-Za-z_][A-Za-z_0-9-]*):([A-Z]+)=(.*)", line)
        if not match:
            raise ContractError("Kokkos cache contains a nonliteral entry")
        if match[1] in result:
            raise ContractError("Kokkos cache contains duplicate entries")
        result[match[1]] = {"type": match[2], "value": match[3]}
    return result


def build_options(cache):
    """Preserve public feature/architecture/compiler flags, not old paths."""
    required = {
        "BUILD_SHARED_LIBS": "OFF", "BUILD_TESTING": "ON", "CMAKE_BUILD_TYPE": "Debug",
        "CMAKE_CXX_COMPILER": "/usr/bin/g++-14", "CMAKE_CXX_STANDARD": "20",
        "Kokkos_ENABLE_SERIAL": "ON", "Kokkos_ENABLE_TESTS": "OFF",
        "Kokkos_ENABLE_EXAMPLES": "OFF", "Kokkos_ENABLE_BENCHMARKS": "OFF",
        "Kokkos_ENABLE_CUDA": "OFF", "Kokkos_ENABLE_HIP": "OFF",
        "Kokkos_ENABLE_SYCL": "OFF", "Kokkos_ENABLE_OPENMP": "OFF",
        "Kokkos_ENABLE_OPENACC": "OFF", "Kokkos_ENABLE_THREADS": "OFF",
        "Kokkos_ENABLE_HPX": "OFF",
    }
    for key, value in required.items():
        if cache.get(key, {}).get("value") != value:
            raise ContractError("Kokkos original Serial build configuration differs: " + key)
    result = {}
    for key, record in cache.items():
        kind, value = record["type"], record["value"]
        if kind in {"INTERNAL", "STATIC"}:
            continue
        selected = (
            key.startswith("Kokkos_") or key in required
            or re.fullmatch(r"CMAKE_(?:CXX|EXE_LINKER|SHARED_LINKER|MODULE_LINKER|STATIC_LINKER)_FLAGS"
                            r"(?:_(?:DEBUG|RELEASE|RELWITHDEBINFO|MINSIZEREL))?", key)
            or key in {"CMAKE_INSTALL_BINDIR", "CMAKE_INSTALL_LIBDIR", "CMAKE_INSTALL_INCLUDEDIR",
                       "CMAKE_INSTALL_DATAROOTDIR", "CMAKE_SKIP_RPATH", "CMAKE_SKIP_INSTALL_RPATH"}
        )
        if not selected:
            continue
        if kind not in {"BOOL", "STRING", "PATH", "UNINITIALIZED"}:
            raise ContractError("Kokkos source build has an unsupported cache type")
        if kind == "BOOL" and value.upper() not in {"ON", "OFF", "YES", "NO", "TRUE", "FALSE", "0", "1"}:
            raise ContractError("Kokkos source build has a nonliteral boolean")
        if key.startswith("Kokkos_") and kind == "PATH" and value:
            raise ContractError("Kokkos source build would use an external historical dependency")
        if key.startswith("Kokkos_ARCH_") and value.upper() != "OFF":
            raise ContractError("Kokkos source build requires the original generic CPU architecture")
        if any(c in value for c in ("\n", "\r", ";", "$", "`")):
            raise ContractError("Kokkos source build has an executable or multiline cache value")
        result[key] = dict(record)
    if len(result) > 192:
        raise ContractError("Kokkos cache projection exceeds its argument bound")
    return result


def configure_argv(options):
    arguments = ["cmake", "-S", "/source", "-B", "/build", "-GNinja",
                 "-DCMAKE_INSTALL_PREFIX=" + INSTALL]
    for key, row in sorted(options.items()):
        suffix = "" if row["type"] == "UNINITIALIZED" else ":" + row["type"]
        arguments.append("-D" + key + suffix + "=" + row["value"])
    return arguments


def same_options(expected, actual):
    """Compare CMake booleans semantically; all other values stay exact."""
    if set(expected) != set(actual):
        return False
    truth = {"ON", "YES", "TRUE", "1"}
    for key, row in expected.items():
        other = actual[key]
        if row["type"] == "BOOL":
            if other["type"] != "BOOL" or ((row["value"].upper() in truth) !=
                                           (other["value"].upper() in truth)):
                return False
        elif row["value"] != other["value"]:
            return False
    return True


def _pinned_json(reference):
    if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
        raise ContractError("Kokkos preparation requires a path and its immutable SHA-256")
    path = Path(reference["path"])
    if (not path.is_absolute() or path.is_symlink() or not path.is_file()
            or file_hash(path) != reference["sha256"]):
        raise ContractError("Kokkos preparation input is missing or changed")
    return json.loads(path.read_text())


def load_dependency(ctx, image):
    reference = ctx.config.get("migration", {}).get("native_kokkos_preparation")
    if not reference:
        raise ContractError("native Kokkos requires a fresh source build in migration.native_kokkos_preparation")
    value = _pinned_json(reference)
    request = value.get("request", {})
    if (value.get("policy") != POLICY or value.get("status") != "prepared"
            or value.get("request_sha256") != canonical_hash(request)
            or request.get("source_commit") != SOURCE_COMMIT or request.get("source_tree") != SOURCE_TREE
            or request.get("source_version") != SOURCE_VERSION or request.get("cache_sha256") != CACHE_SHA256
            or request.get("image") != image
            or request.get("producer_sha256") != file_hash(Path(__file__).with_name("native_kokkos_prepare.py"))
            or value.get("checks") != {"source_unchanged": True, "configuration_matches": True,
                                      "git_metadata_matches": True, "cpu_only": True}):
        raise ContractError("Kokkos preparation does not bind this source/configuration/toolchain")
    for label in ("toolchain", "configure", "build", "install"):
        receipt = _pinned_json(value.get("commands", {}).get(label))
        if receipt.get("exit_status") != 0 or receipt.get("timed_out"):
            raise ContractError("Kokkos source build command did not complete: " + label)
        for stream in ("stdout", "stderr"):
            record = receipt.get(stream, {})
            path = Path(record.get("path", ""))
            if not path.is_absolute() or path.is_symlink() or not path.is_file() or file_hash(path) != record.get("sha256"):
                raise ContractError("Kokkos source build command output changed: " + label)
    source = _pinned_json(value["source_asset"])
    install = _pinned_json(value["install_asset"])
    if (source.get("git_commit") != SOURCE_COMMIT
            or install.get("git_commit") is not None or install.get("source_kind") != "dependency"
            or install.get("tree_sha256") != value.get("install_tree_sha256")
            or not 0 < install.get("files", 0) <= 4096
            or install.get("archive", {}).get("input_bytes", 2**64) > 256 * 1024**2):
        raise ContractError("Kokkos install asset lacks a bounded fresh-build source binding")
    return value, reference


def checked_build(plan):
    try:
        parts = [shlex.split(part) for part in plan["build_command"].split(";")]
    except ValueError as exc:
        raise ContractError("Kokkos original build is not literal shell") from exc
    expected = [
        ["set", "-e"], ["test", "-d", "external/stdexec"],
        ["rm", "-rf", "oracle-kokkos", "oracle-kokkos-cmake"], ["mkdir", "oracle-kokkos-cmake"],
        ["cp", "{root}/harnesses/CMakeLists.kokkos126.txt", "oracle-kokkos-cmake/CMakeLists.txt"],
        ["cmake", "-S", "oracle-kokkos-cmake", "-B", "oracle-kokkos", "-GNinja",
         "-DCMAKE_BUILD_TYPE=Debug", "-DCMAKE_CXX_COMPILER=g++-14", "-DCMAKE_PREFIX_PATH=" + OLD_PREFIX,
         "-DORACLE_SOURCE={root}/harnesses/kokkos_execution_nested.cpp", "-DPRODUCTION_ROOT=$PWD"],
        ["cmake", "--build", "oracle-kokkos", "-j2"],
    ]
    if parts != expected or plan.get("configure_command") or plan.get("env"):
        raise ContractError("Kokkos source adapter requires the complete original build and flags")
    needle = "-DCMAKE_PREFIX_PATH='" + OLD_PREFIX + "'"
    if plan["build_command"].count(needle) != 1:
        raise ContractError("Kokkos historical installation prefix is not uniquely literal")
    return plan["build_command"].replace(needle, "-DCMAKE_PREFIX_PATH=" + INSTALL)


def adapt(ctx, task, plan, image):
    if (task["repo"], plan["input_index"]) != ("uliegecsm/kokkos-execution", 126):
        return None
    build = checked_build(plan)
    cmake = ctx.legacy / "native/harnesses/CMakeLists.kokkos126.txt"
    if cmake.is_symlink() or not cmake.is_file() or file_hash(cmake) != HARNESS_CMAKE_SHA256:
        raise ContractError("Kokkos original private CMake flags changed")
    receipt, reference = load_dependency(ctx, image)
    entries = readonly_git(ctx.repo(task), "ls-tree", "-r", "-z", task["start_sha"], "--", *HEADERS)
    found = {}
    for row in entries.split(b"\0"):
        if row:
            metadata, name = row.split(b"\t", 1)
            if metadata.startswith((b"100644 blob ", b"100755 blob ")):
                found[name.decode()] = metadata.split()[2].decode()
    if set(found) != set(HEADERS):
        raise ContractError("Kokkos public compile headers are not tracked candidate sources")
    with tempfile.TemporaryDirectory(prefix="native-kokkos-public-", dir=ctx.scratch) as temporary:
        public = Path(temporary)
        # Only public production headers, no private program/body/assertions.
        (public / "public.cpp").write_text("".join('#include "' + name + '"\n' for name in HEADERS)
                                           + '#include "Kokkos_Core.hpp"\n')
        (public / "CMakeLists.txt").write_text(
            "cmake_minimum_required(VERSION 3.22)\nproject(KokkosExecutionPublic LANGUAGES CXX)\n"
            "find_package(Kokkos REQUIRED CONFIG)\n"
            "add_library(kokkos_execution_public OBJECT public.cpp)\n"
            "target_compile_features(kokkos_execution_public PRIVATE cxx_std_20)\n"
            "target_compile_options(kokkos_execution_public PRIVATE -O0 -g --coverage)\n"
            "target_compile_definitions(kokkos_execution_public PRIVATE KOKKOS_IMPL_PUBLIC_INCLUDE)\n"
            'target_include_directories(kokkos_execution_public PRIVATE "${PRODUCTION_ROOT}" '
            '"${PRODUCTION_ROOT}/external/stdexec/include")\n'
            "target_link_libraries(kokkos_execution_public PRIVATE Kokkos::kokkos)\n")
        atomic_json(public / "dependency-binding.json", {
            "preparation_sha256": reference["sha256"], "source_commit": SOURCE_COMMIT,
            "source_tree": SOURCE_TREE, "image": image, "install_tree_sha256": receipt["install_tree_sha256"],
        })
        public_asset = ctx.bundle(public, role="dependency")
    atomic_json(ctx.output / "native-kokkos" / (component(task["id"]) + ".json"), {
        "id": task["id"], "start_sha": task["start_sha"], "preparation": reference,
        "original_build_command": plan["build_command"], "modified_build_command": build,
        "changed_field": "build_command: literal CMAKE_PREFIX_PATH only",
        "public_headers": found, "harness_cmake_sha256": HARNESS_CMAKE_SHA256,
        "source_changes": [], "test_changes": [], "private_harness_exposed": False,
    })
    return {
        "plan": {**plan, "build_command": build},
        "configure": "cmake -S " + PUBLIC + " -B " + BUILD + " -GNinja -DCMAKE_BUILD_TYPE=Debug "
                     "-DCMAKE_CXX_COMPILER=g++-14 -DCMAKE_PREFIX_PATH=" + INSTALL + " -DPRODUCTION_ROOT=/workspace",
        "build": "cmake --build " + BUILD + " --target kokkos_execution_public -j2",
        "grading_bootstrap": [build], "scratch_paths": [BUILD],
        "assets": [
            {**receipt["install_asset"], "kind": "archive", "visibility": "developer", "target": INSTALL},
            {**public_asset, "kind": "archive", "visibility": "developer", "target": PUBLIC},
        ],
    }
