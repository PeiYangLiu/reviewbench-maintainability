"""Whole-candidate MQT95/96 builds with exact original public dependency inputs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shlex

from reviewbench_maintainability.agentic.assets import asset_descriptor
from reviewbench_maintainability.agentic.contracts import ContractError, atomic_json, canonical_hash, component, file_hash
from reviewbench_maintainability.agentic.snapshots import git
from .native_api import normalized_plan
from .native_submodules import readonly_links

REPO = "munich-quantum-toolkit/core"
PROFILES = {
    95: {
        "id": "github__munich-quantum-toolkit__core__pull_371__comment_1267712690__modularity",
        "start": "016a7e5efa67fc8df3dfc9b9315681e6cb705ee3",
        "plan": "57fee069dbaa3be340f9bdacb8cff9b86fbe379de95722d7e4e62eda313d3ff9",
    },
    96: {
        "id": "github__munich-quantum-toolkit__core__pull_752__comment_1880208272__complexity",
        "start": "165f26bd556bc2e9ce23b08961d15c0839b5edc3",
        "plan": "beaa30f0b79cdccb1803956c9f8fcada64862be497b7ed2cd9fea24bf16639d6",
    },
}
PINS95 = {
    "extern/boost/config": "a1cf5d531405e62927b0257b5cbecc66a545b508",
    "extern/boost/multiprecision": "5f81a78cdb38ea35f98ef7a655d2535773c1261b",
    "extern/googletest": "06f44bc951046150f1348598854b211afdcf37fc",
    "extern/json": "6af826d0bdb55e4b69e3ad817576745335f243ca",
    "extern/pybind11": "d0232b119f5f0cf2bf6cecacaf099c301c811ca8",
    "extern/pybind11_json": "b02a2ad597d224c3faee1f05a56d81d4c4453092",
}
DECLARATION96 = "3d580fbabe5bf51dae0ee109c17e4641827418687357a80d49b7de46b3d1a378"
HARNESS96 = {
    "CMakeLists.mqt96.txt": "be8869c9946cfd331af366cd01aa8a283917ae08243c1e34ab7b0f01a87c49d7",
    "mqt_dcx_inverse.cpp": "27259d83a3d786aa088a2946ff0b2b7348a6d66e7de02519dc5649f43e606365",
}
DOWNLOAD96 = {
    "json": ("d6c65aca6b1ed68e7a182f4757257b107ae403032760ed6ef121c9d55e81757d",
             "https://github.com/nlohmann/json/releases/download/v3.11.3/json.tar.xz"),
    "boost_mp": ("5339fb083184499c86c30fbc8c5bb5f812f989806c72656f3dc26765e1a2e1ef",
                 "https://github.com/boostorg/multiprecision/archive/refs/tags/Boost_1_84_0.tar.gz"),
}
SOURCE96 = "/opt/reviewbench-assets/mqt96-source"
TOOLS96 = "/opt/reviewbench-assets/native-mqt"
DEST96 = "build-native-dependencies"
PUBLIC_SOURCE96 = "build-native-public-source"
PUBLIC_BUILD96 = "build-native-public"


def attested(ctx, task, plan, index):
    if (task["repo"], plan.get("input_index")) != (REPO, index):
        return False
    profile = PROFILES[index]
    if (task.get("id") != profile["id"] or task.get("start_sha") != profile["start"]
            or canonical_hash(normalized_plan(plan, ctx.legacy)) != profile["plan"]):
        raise ContractError("MQT adapter requires the complete original task/build/test plan")
    return True


def receipt(ctx, key):
    ref = ctx.config.get("migration", {}).get(key)
    if not isinstance(ref, dict) or set(ref) != {"path", "sha256"}:
        raise ContractError("MQT original input receipt is required: " + key)
    path = Path(ref["path"])
    if (not path.is_absolute() or path.is_symlink() or not path.is_file()
            or path.stat().st_size > 2 * 1024**2 or file_hash(path) != ref["sha256"]):
        raise ContractError("MQT original input receipt changed: " + key)
    return json.loads(path.read_text()), ref


def mqt95_submodules(ctx, task, plan):
    if not attested(ctx, task, plan, 95):
        return None
    original, evidence = receipt(ctx, "mqt95_git_inputs")
    readback, rb_evidence = receipt(ctx, "mqt95_git_readback")
    if (original.get("status") != "original_MQT95_git_inputs_archived" or original.get("id") != task["id"]
            or original.get("pins") != PINS95 or set(original.get("assets", {})) != set(PINS95)
            or readback.get("status") != "next_original_inputs_independently_verified"
            or readback.get("id") != task["id"] or set(readback.get("receipts", {})) != set(PINS95)):
        raise ContractError("MQT95 source closure does not bind all original Git links")
    observed = {r["path"]: r["commit"] for r in readonly_links(ctx.repo(task), task["start_sha"])}
    declared = {r["path"]: r["commit"] for r in ctx.modules(task)}
    if observed != PINS95 or declared != PINS95:
        raise ContractError("MQT95 source catalog differs from the original superproject pins")
    modules, total = [], 0
    for path, pin in PINS95.items():
        asset = original["assets"][path]
        descriptor = asset_descriptor(asset, ctx.output)
        if descriptor["source_kind"] != "submodule" or descriptor.get("git_commit") != pin:
            raise ContractError("MQT95 source archive has a different Git identity")
        check = readback["receipts"][path]
        report = Path(check["path"])
        if (not report.is_absolute() or report.is_symlink() or not report.is_file()
                or file_hash(report) != check["sha256"]):
            raise ContractError("MQT95 independent readback reference changed")
        proof = json.loads(report.read_text())
        if (proof.get("status") != "independent_Blob_component_readback_verified"
                or proof.get("asset") != asset or not proof.get("all_paths_bytes_full_modes_verified")
                or not proof.get("gzip_crc_and_eof_verified")):
            raise ContractError("MQT95 input has no complete independent Blob readback")
        total += descriptor["archive"]["input_bytes"]
        modules.append({"path": path, "commit": pin, "asset": asset})
    if total > 384 * 1024**2:
        raise ContractError("MQT95 source closure exceeds its declared input bound")
    prefix = "set -e; git submodule update --init --recursive;"
    if not plan["build_command"].startswith(prefix):
        raise ContractError("MQT95 original submodule command boundary changed")
    guard = "set -e; " + " && ".join("test -d " + shlex.quote(p) for p in PINS95) + ";"
    updated = {**plan, "build_command": guard + plan["build_command"][len(prefix):]}
    atomic_json(ctx.output / "mqt-original-inputs" / (component(task["id"]) + ".json"), {
        "id": task["id"], "input_receipt": evidence, "readback": rb_evidence,
        "original_build_command": plan["build_command"], "current_build_command": updated["build_command"],
        "method": "reuse all original source-only Git archives; remove only the network checkout operation",
        "source_changes": [], "test_changes": [], "golden_changes": [], "new_archives": 0,
    })
    return updated, modules


def mqt96_specification(wrapper, tree_sha256):
    # Original add_subdirectory builds the whole candidate; retain all original
    # option values. The public target depends on the same MQT::CoreDD alias,
    # so renaming its internal target does not narrow candidate refactor scope.
    before, separator, after = wrapper.partition("\nadd_executable(")
    if not separator or "target_link_libraries(mqt-dcx-oracle PRIVATE MQT::CoreDD)" not in after:
        raise ContractError("MQT96 original wrapper graph boundary changed")
    public = before.replace("project(mqt_dcx_oracle LANGUAGES CXX)",
                            "project(reviewbench_mqt_public LANGUAGES CXX)", 1)
    public += ("\nadd_custom_target(reviewbench-public-mqt)\n"
               "add_dependencies(reviewbench-public-mqt MQT::CoreDD)\n")
    if "ORACLE_SOURCE" in public or "add_executable" in public:
        raise ContractError("MQT96 public wrapper contains a private harness")
    return {
        "schema_version": "reviewbench-mqt96-source/v1", "source": SOURCE96,
        "source_tree_sha256": tree_sha256, "destination": DEST96,
        "declaration": "cmake/ExternalDependencies.cmake", "declaration_sha256": DECLARATION96,
        "public_wrapper_path": PUBLIC_SOURCE96 + "/CMakeLists.txt", "public_wrapper": public,
    }


def mqt96(ctx, task, plan):
    if not attested(ctx, task, plan, 96):
        return None
    for name, expected in HARNESS96.items():
        path = ctx.legacy / "native/harnesses" / name
        if path.is_symlink() or not path.is_file() or file_hash(path) != expected:
            raise ContractError("MQT96 original harness/wrapper changed: " + name)
    original, evidence = receipt(ctx, "mqt96_sources")
    rb, rb_evidence = receipt(ctx, "mqt96_sources_readback")
    if original.get("status") != "original_MQT96_sources_archived":
        raise ContractError("MQT96 needs both complete original source archives")
    if set(original.get("components", {})) != set(DOWNLOAD96):
        raise ContractError("MQT96 original dependency set differs")
    for name, (digest, url) in DOWNLOAD96.items():
        row = original["components"][name]
        if row.get("archive_sha256") != digest or row.get("url") != url:
            raise ContractError("MQT96 source archive differs from its original captured input")
    asset = original["asset"]
    descriptor = asset_descriptor(asset, ctx.output)
    if (descriptor["source_kind"] != "dependency" or not 1 <= descriptor["files"] <= 20000
            or descriptor["archive"]["input_bytes"] > 256 * 1024**2):
        raise ContractError("MQT96 source asset exceeds its complete-dependency bound")
    if (rb.get("status") != "independent_Blob_component_readback_verified" or rb.get("asset") != asset
            or not rb.get("all_paths_bytes_full_modes_verified") or not rb.get("gzip_crc_and_eof_verified")):
        raise ContractError("MQT96 original source archive has no independent readback")
    raw = git(ctx.repo(task), "show", task["start_sha"] + ":cmake/ExternalDependencies.cmake")
    if hashlib.sha256(raw).hexdigest() != DECLARATION96:
        raise ContractError("MQT96 original dependency declaration changed")
    wrapper = (ctx.legacy / "native/harnesses/CMakeLists.mqt96.txt").read_text()
    spec = mqt96_specification(wrapper, descriptor["tree_sha256"])
    spec_file = ctx.output / "mqt-original-inputs" / (component(task["id"]) + "-public.json")
    atomic_json(spec_file, spec)
    assets = [
        {**asset, "kind": "archive", "visibility": "developer", "target": SOURCE96},
        {**ctx.copy_asset(Path(__file__).with_name("native_mqt_entry.py"), "mqt-tools"),
         "kind": "file", "visibility": "developer", "target": TOOLS96 + "/entry.py"},
        {**ctx.copy_asset(spec_file, "mqt-tools"), "kind": "file", "visibility": "developer", "target": TOOLS96 + "/spec.json"},
    ]
    seed = shlex.join(["python3", TOOLS96 + "/entry.py", "--spec", TOOLS96 + "/spec.json", "--workspace", "/workspace"])
    locations = ("-DFETCHCONTENT_SOURCE_DIR_NLOHMANN_JSON=$PWD/" + DEST96 + "/json"
                 " -DFETCHCONTENT_SOURCE_DIR_BOOST_MP=$PWD/" + DEST96 + "/boost_mp")
    flags = ("-GNinja -DPRODUCTION_ROOT=$PWD -DCMAKE_BUILD_TYPE=Debug -DCMAKE_CXX_FLAGS='-O0 -g --coverage' "
             "-DCMAKE_EXE_LINKER_FLAGS=--coverage -DCMAKE_SHARED_LINKER_FLAGS=--coverage")
    public = ("cmake -S " + PUBLIC_SOURCE96 + " -B " + PUBLIC_BUILD96 + " " + flags + " " + locations +
              "; cmake --build " + PUBLIC_BUILD96 + " --target reviewbench-public-mqt -j2")
    private = plan["build_command"].replace("; cmake --build", " " + locations + "; cmake --build", 1)
    if private == plan["build_command"]:
        raise ContractError("MQT96 original configure/build boundary is absent")
    atomic_json(ctx.output / "mqt-original-inputs" / (component(task["id"]) + ".json"), {
        "id": task["id"], "source_receipt": evidence, "readback": rb_evidence,
        "original_build_command": plan["build_command"], "current_build_command": private,
        "original_test_command": plan["test_command"], "public_build_command": public,
        "original_harnesses": HARNESS96, "source_changes": [], "test_changes": [], "golden_changes": [],
    })
    return {"configure": seed, "build": "set -e; " + public, "grading_bootstrap": [private],
            "scratch_paths": [DEST96, PUBLIC_SOURCE96, PUBLIC_BUILD96],
            "assets": assets}
