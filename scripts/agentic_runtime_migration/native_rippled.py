"""Four evidence-bound Rippled builds over current, complete candidate trees."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shlex
import shutil
import tempfile

from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, atomic_json, canonical_hash, component, file_hash,
)
from .native_rippled_conan import graph_from_log
from .native_submodules import readonly_links


POLICY = "native-rippled-exact-conan/v1"
PUBLIC = "/opt/reviewbench-assets/rippled-public"
DEPENDENCIES = "/opt/reviewbench-assets/rippled-dependencies"
PRIVATE = "/opt/reviewbench-assets/rippled-private"
RUNTIME = "/workspace/.reviewbench/native"
SPECS = {
    "directory": {
        "id": "github__XRPLF__rippled__pull_4751__comment_1479091579__readability",
        "start_sha": "b5828422a175cd6fece3bf553e5537996f99e541", "index": 27,
        "plan_sha256": "202054bbc02e959fa351986c3014acea50b35495429d87fc47d1a3e140ebfea4",
        "helper": "configure_rippled_previous_txn.sh",
        "helper_sha256": "54adc6a08850d3ed07adf6a1feeb9450b4a9d37edcb8134f414777803c4acce9",
        "target": "rippled", "cases": 5, "checks": 2442,
    },
    "stobject": {
        "id": "github__XRPLF__rippled__pull_5143__comment_1786919023__duplication",
        "start_sha": "ea887bfc93b22f6a1225f2843a11131e04f0dfd2", "index": None,
        "commands_sha256": "d5736750cc13062600e26cf9fa2ca284d4d4618b1f667ebdffb1c04b3f9e80b8",
        "target": "rippled", "cases": 6, "checks": 2175,
    },
    "confidential": {
        "id": "github__XRPLF__rippled__pull_5860__comment_2974420059__modularity",
        "start_sha": "3f749ecf7640aab34fb073fd3a6fa1a81ba1037d", "index": 28,
        "plan_sha256": "174ac1ecba2fe49240952e8cb3403fcd4efccb83251067bd70f86e8e6a7cebfd",
        "helper": "configure_rippled_confidential_transfer.sh",
        "helper_sha256": "688fdd8004ba134c2ac62b7f7d80e759b4831375e6ba0c53807b148cf9dbd0df",
        "target": "xrpld", "cases": 37, "checks": 7640,
    },
    "view": {
        "id": "github__XRPLF__rippled__pull_5996__comment_2635153432__dead_code",
        "start_sha": "46d9937ffa401011a874b24117ffe5fd793fa67e", "index": 29,
        "plan_sha256": "587aa3ac1dd5235ca7a5e4522358524a44e5d21d0ec5441de19fa501a17f1636",
        "helper": "configure_rippled_view_oracle.sh",
        "helper_sha256": "dee697a7fb92f61ff5978ce5c0f02118af39290824cf9d001e0e60f324198303",
        "target": "xrpl.libxrpl", "cases": None, "checks": None,
    },
}
VIEW_FILES = {
    "rippled-view-oracle/CMakeLists.txt": "dd199a4d26d54c88a29b3cd96d2f239f0f0f10ae4e990485988405a8fe3af57b",
    "rippled_view_balance.cpp": "0c058aafdacd5417ff80787af39a74e1a2c7c9d7d88c10c3b14d21ca7599c583",
}


def pinned_file(path, expected=None):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ContractError("Rippled original input is absent or linked: " + str(path))
    digest = file_hash(path)
    if expected is not None and digest != expected:
        raise ContractError("Rippled original input changed: " + str(path))
    return {"path": str(path), "sha256": digest}


def pinned_json(reference):
    if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
        raise ContractError("Rippled input needs an absolute path and independent SHA-256")
    return json.loads(Path(pinned_file(reference["path"], reference["sha256"])["path"]).read_text())


def public_spec(key):
    if key not in SPECS:
        raise ContractError("unsupported Rippled public dependency profile")
    legacy = key == "stobject"
    base = ("[settings]\narch=x86_64\nbuild_type=" + ("Debug" if legacy else "Release")
            + "\ncompiler=gcc\ncompiler.cppstd=" + ("20" if legacy else "gnu17")
            + "\ncompiler.libcxx=libstdc++11\ncompiler.version=" + ("11" if legacy else "13")
            + "\nos=Linux\n")
    if legacy:
        base += "arch_build=x86_64\nos_build=Linux\n"
        host = base + "[options]\nxrpl:coverage=True\nxrpl:tests=True\n"
    else:
        options = {"rocksdb": key == "confidential", "tests": key != "view", "unity": False}
        if key != "directory":
            options["xrpld"] = key == "confidential"
        host = base.replace("compiler.cppstd=gnu17", "compiler.cppstd=20") + "[options]\n"
        host += "".join("&:" + name + "=" + str(value) + "\n" for name, value in sorted(options.items()))
    return {"conan_version": "1.60.2" if legacy else "2.17.1", "build_profile": base,
            "host_profile": host, "build_directory": "build" if legacy else "build-oracle",
            "recipe_mode": "conan2-historical" if key == "directory" else "current"}


def profile_entries(text):
    result, section = {}, None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if re.fullmatch(r"\[[a-z_]+\]", line):
            section = line
            continue
        if section is None or "=" not in line:
            raise ContractError("Rippled original Conan profile contains an unsupported statement")
        key, value = line.split("=", 1)
        index = (section, key)
        if index in result:
            raise ContractError("Rippled original Conan profile contains a duplicate setting")
        result[index] = value
    return result


def check_original_profiles(text, public):
    try:
        if public["conan_version"].startswith("2."):
            host, rest = text.split("Profile host:\n", 1)[1].split("Profile build:\n", 1)
            build = rest.split("========", 1)[0]
        else:
            host = text.split("Configuration:\n", 1)[1].split("\n\n", 1)[0]
            build = None  # Original Conan 1 invocation had one profile.
    except (IndexError, ValueError) as exc:
        raise ContractError("Rippled original Conan profile evidence is missing") from exc
    if profile_entries(host) != profile_entries(public["host_profile"]):
        raise ContractError("Rippled host compiler/options differ from the original Conan input")
    if build is not None and profile_entries(build) != profile_entries(public["build_profile"]):
        raise ContractError("Rippled build-tool profile differs from the original Conan input")


def original_inputs(ctx, task, oracle):
    key = next((key for key, row in SPECS.items() if row["id"] == task["id"]), None)
    if key is None:
        return None
    spec = SPECS[key]
    if (task["repo"] != "XRPLF/rippled" or task["start_sha"] != spec["start_sha"]
            or oracle.get("id") != task["id"] or oracle.get("start_sha") != task["start_sha"]):
        raise ContractError("Rippled task/original Oracle identity differs")
    if readonly_links(ctx.repo(task), task["start_sha"]):
        raise ContractError("Rippled current source requires an explicit unexpected gitlink closure")
    if key == "stobject":
        if oracle.get("input_index") is not None or oracle.get("lane_results_source"):
            raise ContractError("published Rippled input must not acquire a synthetic lane index")
        root = ctx.legacy.parent / "strict_screen_20260726"
        directory = root / oracle["artifacts"]["directory"]
        commands = pinned_file(directory / "build-commands.txt", spec["commands_sha256"])
        log = pinned_file(directory / "start-conan.log")
        golden = pinned_file(root / oracle["golden_scope"]["patch"], oracle["golden_scope"]["patch_sha256"])
        plan = {"test_command": "./build/rippled --unittest=STObject",
                "build_command": "cmake --build build --target rippled -j16",
                "docker_image": "oracle-rippled:real", "timeout_seconds": 7200}
        run_rows = {v: oracle[v + "_runs"] for v in ("start", "golden")}
        helpers = []
    else:
        root = ctx.legacy / "native"
        plans = [json.loads(line) for line in (root / "plans.jsonl").read_text().splitlines()]
        plan = next((p for p in plans if p["input_index"] == spec["index"]), None)
        if (oracle.get("input_index") != spec["index"] or not plan
                or canonical_hash(plan) != spec["plan_sha256"]):
            raise ContractError("Rippled complete original plan/flags/assertions differ")
        helpers = [pinned_file(root / "harnesses" / spec["helper"], spec["helper_sha256"])]
        if key == "view":
            helpers += [pinned_file(root / "harnesses" / name, digest) for name, digest in VIEW_FILES.items()]
        directory = root / oracle["artifacts"]["directory"]
        commands = pinned_file(root / oracle["artifacts"]["commands"])
        log = pinned_file(directory / "logs/start-configure-attempt1.log")
        golden = pinned_file(root / oracle["artifacts"]["patch"], oracle["golden_patch_sha256"])
        run_rows = oracle["runs"]
    runs, texts = [], []
    for variant in ("start", "golden"):
        values = run_rows[variant]
        if len(values) != 3 or {row["run"] for row in values} != {1, 2, 3}:
            raise ContractError("Rippled original three-plus-three run identities are incomplete")
        for row in values:
            if row.get("ok") is not True or row.get("failed") != 0 or row.get("passed", 0) <= 0:
                raise ContractError("Rippled original replay is not successful")
            record = pinned_file(root / row["log"])
            text = Path(record["path"]).read_text()
            runs.append({"variant": variant, "run": row["run"], **record})
            texts.append(text)
    stable = [line for line in texts[0].splitlines() if line and not line.startswith("ORACLE_")
              and all(line in text.splitlines() for text in texts[1:])]
    if key != "view":
        summary = ", 1 suite, " + str(spec["cases"]) + " cases, " + str(spec["checks"]) + " tests total, 0 failures"
        if any(not re.search(re.escape(summary) + r"$", text, re.M) for text in texts):
            raise ContractError("Rippled original suite has different cases/checks/failures")
        completed = [line for line in stable if line.startswith(("ripple.", "xrpl."))]
        if len(completed) < spec["cases"]:
            raise ContractError("Rippled original completed case identities are absent")
        success = "(?ms)" + "".join(r"(?=.*^" + re.escape(line) + r"$)" for line in dict.fromkeys(completed))
        success += r".*" + re.escape(summary) + r"$"
    else:
        if len(stable) != 3:
            raise ContractError("Rippled original view assertion output is incomplete")
        success = "(?ms)" + "".join(r"(?=.*^" + re.escape(line) + r"$)" for line in stable) + ".*"
    if not oracle.get("pass_to_pass_tests"):
        raise ContractError("Rippled original fixed test IDs are missing")
    log_text = Path(log["path"]).read_text()
    check_original_profiles(log_text, public_spec(key))
    graph = graph_from_log(log_text, 1 if key == "stobject" else 2)
    return {"key": key, "plan": plan, "graph": graph, "log": log, "commands": commands,
            "golden": golden, "helpers": helpers, "runs": runs, "success_pattern": success,
            "expected_tests": list(oracle["pass_to_pass_tests"]), "public": public_spec(key)}


def dependency_request(task, inputs, image):
    return {
        "task_id": task["id"], "start_sha": task["start_sha"], "image": image,
        "public": inputs["public"], "graph": inputs["graph"], "original_conan_log": inputs["log"],
        "original_commands": inputs["commands"], "original_helpers": inputs["helpers"],
        "producer_sha256": file_hash(Path(__file__).with_name("native_rippled_prepare.py")),
        "cache_adapter_sha256": file_hash(Path(__file__).with_name("native_rippled_conan.py")),
        "entry_sha256": file_hash(Path(__file__).with_name("native_rippled_entry.py")),
    }


def load_preparation(ctx, task, inputs, image):
    reference = ctx.config.get("migration", {}).get("native_rippled_preparations", {}).get(task["id"])
    if not reference:
        raise ContractError("Rippled needs its exact cache/resolve receipt in migration.native_rippled_preparations")
    value = pinned_json(reference)
    request = dependency_request(task, inputs, image)
    if (value.get("policy") != POLICY or value.get("status") != "dependencies_resolved"
            or value.get("request") != request or value.get("request_sha256") != canonical_hash(request)
            or value.get("source_unchanged") is not True or value.get("old_candidate_artifacts_copied") is not False):
        raise ContractError("Rippled preparation differs from the original inputs or frozen producer")
    for label in ("cache_projection_command", "resolve_command"):
        command = pinned_json(value.get(label))
        if command.get("exit_status") != 0 or command.get("timed_out"):
            raise ContractError("Rippled offline dependency preparation did not complete: " + label)
        for stream in ("stdout", "stderr"):
            record = command[stream]
            pinned_file(record["path"], record["sha256"])
    payload = pinned_json(value["cache_projection"])
    if (payload.get("graph") != request["graph"] or payload.get("source_cache_modified") is not False
            or payload.get("dependency_builds") != 0 or payload.get("old_candidate_artifacts_copied") is not False):
        raise ContractError("Rippled cache projection lacks exact original graph/payload evidence")
    descriptor = pinned_json(value["dependency_asset"])
    if (descriptor.get("source_kind") != "dependency" or descriptor.get("git_commit") is not None
            or not 0 < descriptor.get("files", 0) <= 120000
            or descriptor.get("archive", {}).get("input_bytes", 2**64) > 12 * 1024**3):
        raise ContractError("Rippled dependency asset exceeds its bounded external-package closure")
    return value


def write_public(directory, public, *, view=False):
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(__file__).with_name("native_rippled_entry.py"), directory / "entry.py")
    atomic_json(directory / "public.json", public)
    if view:
        (directory / "view").mkdir()
        (directory / "view/CMakeLists.txt").write_text(
            "cmake_minimum_required(VERSION 3.22)\nproject(rippled_public LANGUAGES C CXX)\n"
            'set(tests OFF CACHE BOOL "" FORCE)\nset(xrpld OFF CACHE BOOL "" FORCE)\n'
            'set(rocksdb OFF CACHE BOOL "" FORCE)\nset(unity OFF CACHE BOOL "" FORCE)\n'
            'add_subdirectory("${XRPL_SOURCE_DIR}" xrpl)\n')


def configure_command(key, *, private=False):
    if key == "stobject":
        # The original rippled/STObject contract needs this public target.
        # Restore its retained effective flag, not the Conan dependency profile.
        return shlex.join(["cmake", "-S", ".", "-B", "build", "-G", "Ninja",
                           "-DCMAKE_TOOLCHAIN_FILE=/workspace/build/generators/conan_toolchain.cmake",
                           "-DCMAKE_CXX_FLAGS=-O0 -g --coverage", "-DCMAKE_EXE_LINKER_FLAGS=--coverage",
                           "-Dxrpld=ON"])
    source = PRIVATE + "/harnesses/rippled-view-oracle" if private else PUBLIC + "/view"
    build = "build-oracle" if key != "view" or private else "build-native-rippled-public"
    args = ["cmake", "-S", source if key == "view" else ".", "-B", build, "-G", "Ninja",
            "-DCMAKE_BUILD_TYPE=Release",
            "-DCMAKE_TOOLCHAIN_FILE=/workspace/build-oracle/conan/build/generators/conan_toolchain.cmake",
            "-DCMAKE_C_COMPILER_LAUNCHER=ccache", "-DCMAKE_CXX_COMPILER_LAUNCHER=ccache",
            "-DCMAKE_C_FLAGS_RELEASE=-O0 -g --coverage -fprofile-abs-path -DNDEBUG",
            "-DCMAKE_CXX_FLAGS_RELEASE=-O0 -g --coverage -fprofile-abs-path -DNDEBUG",
            "-DCMAKE_EXE_LINKER_FLAGS=--coverage"]
    if key == "view":
        args += ["-DXRPL_SOURCE_DIR=/workspace"]
        if private:
            args += ["-DORACLE_ROOT=" + PRIVATE]
    else:
        args += ["-Drocksdb=" + ("ON" if key == "confidential" else "OFF"),
                 "-Dtests=ON", "-Dunity=OFF"]
        if key == "confidential":
            args += ["-Dxrpld=ON"]
    return shlex.join(args)


def migrate(ctx, task, oracle):
    inputs = original_inputs(ctx, task, oracle)
    if inputs is None:
        return None
    key, plan = inputs["key"], inputs["plan"]
    image, image_audit = ctx.image(plan["docker_image"])
    prepared = load_preparation(ctx, task, inputs, image)
    with tempfile.TemporaryDirectory(prefix="rippled-public-", dir=ctx.scratch) as temporary:
        public = Path(temporary) / "public"
        write_public(public, inputs["public"], view=key == "view")
        public_asset = ctx.bundle(public, role="dependency")
        assets = [
            {**public_asset, "kind": "archive", "visibility": "developer", "target": PUBLIC},
            {**prepared["dependency_asset"], "kind": "archive", "visibility": "developer", "target": DEPENDENCIES},
        ]
        if key == "view":
            private = Path(temporary) / "private"
            for name in VIEW_FILES:
                target = private / "harnesses" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ctx.legacy / "native/harnesses" / name, target)
            asset = ctx.bundle(private, role="grader")
            assets.append({**asset, "kind": "archive", "visibility": "grader", "target": PRIVATE})
    from .native_lane import limit_jobs
    bootstrap = ["mkdir -p /workspace/.reviewbench/tmp " + RUNTIME + "/artifacts/task",
                 "python3 " + PUBLIC + "/entry.py --public " + PUBLIC + " --dependencies " + DEPENDENCIES,
                 configure_command(key)]
    grade_bootstrap = ([configure_command(key, private=True)] if key == "view" else [])
    grade_bootstrap.append(limit_jobs(plan["build_command"]))
    build_dir = "build-native-rippled-public" if key == "view" else inputs["public"]["build_directory"]
    golden = ctx.copy_asset(Path(inputs["golden"]["path"]), "golden")
    recipe = RuntimeRecipe(
        task_id=task["id"], repo=task["repo"], start_sha=task["start_sha"], lane="native",
        image=image, image_audit=image_audit, cpus=4, memory_mb=8192, build_scratch_mb=28672,
        scratch_paths=["build", "build-oracle", "build-native-rippled-public", "build-rippled-dependencies"],
        submodules=[], cache_mounts={"ccache": "/opt/reviewbench-cache/ccache"},
        command_timeout_seconds=plan["timeout_seconds"],
        env={"ORACLE_ROOT": RUNTIME, "ORACLE_ARTIFACT_DIR": RUNTIME + "/artifacts/task",
             "ORACLE_VARIANT": "candidate", "TMPDIR": "/workspace/.reviewbench/tmp",
             "CMAKE_BUILD_PARALLEL_LEVEL": "4", "CCACHE_DIR": "/opt/reviewbench-cache/ccache",
             "CCACHE_BASEDIR": "/workspace", "CCACHE_COMPRESS": "1", "CCACHE_MAXSIZE": "1G",
             "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "SOURCE_DATE_EPOCH": "0",
             "PYTHONDONTWRITEBYTECODE": "1"},
        bootstrap=bootstrap,
        dev_checks=["cmake --build " + build_dir + " --target " + SPECS[key]["target"] + " -j4"],
        assets=assets,
        grading={"multifile_build": True, "immutable_paths": ctx.immutable_tests(task),
                 "golden_patch": golden, "bootstrap": grade_bootstrap,
                 "commands": [{"command": plan["test_command"], "parser": "regex",
                               "expected_tests": inputs["expected_tests"], "success_pattern": inputs["success_pattern"]}]},
    )
    recipe.validate()
    atomic_json(ctx.output / "native-rippled" / (component(task["id"]) + ".json"), {
        "id": task["id"], "start_sha": task["start_sha"], "policy": POLICY, "inputs": inputs,
        "image": image, "preparation_request_sha256": prepared["request_sha256"],
        "source_changes": [], "assertion_changes": [], "test_id_changes": [],
        "candidate_production_build": "complete current CMake target; no imported xrpl library or executable",
        "changed_fields": ["dependency preparation: exact external Conan cache, offline install, task-local metadata",
                           "public configure/developer build separated from original private view harness",
                           "build worker cap: -j4"],
        "original_network": plan.get("docker_network", "unspecified"),
        "runtime_network": "frozen engine's default isolated network=none; requires real replay verification",
        "runtime_verified": False,
    })
    return recipe
