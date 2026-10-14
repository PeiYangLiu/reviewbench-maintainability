"""Use RipSim's exact public GoogleTest source without changing its declarations."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess

try:
    from .native_fetchcontent import seed_source
except ImportError:
    if __package__:
        raise
    from native_fetchcontent import seed_source

TASK = "github__Reservoir-In-Processor__rip-sim__pull_53__comment_1359146014__readability"
START = "20ef9867eefcbb91cc3fa8bdfdc672550d8f66f5"
PLAN_SHA256 = "308272e4801fe41025582342077a6d6db5dd60f071709a6aac93d7863c04a72d"
COMMIT = "03597a01ee50ed33e9dfd640b249b4be3799d395"
URL = "https://github.com/google/googletest/archive/" + COMMIT + ".zip"
SCHEMA = "reviewbench-ripsim-public-dependency/v1"
SOURCE = "/opt/reviewbench-assets/ripsim-googletest"
TOOLS = "/opt/reviewbench-assets/ripsim-tools"
SPEC = "/opt/reviewbench-assets/ripsim-googletest.json"
CACHE = "/opt/reviewbench-cache/ripsim-dependencies"
FLAGS = [
    "-GNinja", "-DSANITIZE=OFF", "-DCMAKE_BUILD_TYPE=Debug",
    "-DCMAKE_CXX_COMPILER_LAUNCHER=ccache", "-DCMAKE_CXX_FLAGS=-O0 -g --coverage",
    "-DCMAKE_SHARED_LINKER_FLAGS=--coverage", "-DCMAKE_EXE_LINKER_FLAGS=--coverage",
]


def declarations(text):
    """Both original CMake-version branches declare the same immutable URL."""
    text = re.sub(r"#[^\n]*", "", text)
    matches = []
    for body in re.findall(r"\bFetchContent_Declare\s*\(([^()]*)\)", text, re.S | re.I):
        words = shlex.split(body)
        if not words or words[0].lower() != "googletest":
            continue
        if words not in (
            ["googletest", "URL", URL],
            ["googletest", "URL", URL, "DOWNLOAD_EXTRACT_TIMESTAMP", "true"],
        ):
            raise ValueError("RipSim GoogleTest declaration differs from its original commit")
        matches.append(words)
    if not 1 <= len(matches) <= 2:
        raise ValueError("RipSim original GoogleTest declarations are missing or ambiguous")
    return matches


def configuration(spec, workspace, mode, *, cache_root=None):
    if (set(spec) != {"schema_version", "asset_sha256", "git_commit", "source"}
            or spec["schema_version"] != SCHEMA or spec["git_commit"] != COMMIT
            or not re.fullmatch(r"[0-9a-f]{64}", spec["asset_sha256"])
            or mode not in {"developer", "grader"}):
        raise ValueError("invalid RipSim original dependency specification")
    workspace = Path(workspace).resolve()
    cmake = workspace / "CMakeLists.txt"
    if not cmake.is_file() or cmake.is_symlink() or cmake.stat().st_size > 1024**2:
        raise ValueError("RipSim CMake declaration is missing, linked or overlarge")
    declarations(cmake.read_text())
    source = Path(spec["source"])
    if (not source.is_absolute() or not source.is_dir() or source.is_symlink()
            or not (source / "CMakeLists.txt").is_file()):
        raise ValueError("RipSim original GoogleTest source is absent")
    cache = Path(cache_root or CACHE)
    copied = seed_source(source, cache / mode / ("googletest-" + spec["asset_sha256"]),
                         spec["asset_sha256"], cache)
    build = "build-native-public" if mode == "developer" else "build-oracle"
    return ["cmake", "-S", ".", "-B", build, *FLAGS,
            "-DFETCHCONTENT_SOURCE_DIR_GOOGLETEST=" + str(copied)]


def adapt(ctx, task, plan):
    if (task["repo"], plan.get("input_index")) != ("Reservoir-In-Processor/rip-sim", 23):
        return None
    from reviewbench_maintainability.agentic.assets import asset_descriptor
    from reviewbench_maintainability.agentic.contracts import ContractError, atomic_json, canonical_hash, file_hash
    from reviewbench_maintainability.agentic.snapshots import git

    if task["id"] != TASK or task["start_sha"] != START or canonical_hash(plan) != PLAN_SHA256:
        raise ContractError("RipSim original task/build plan binding differs")
    reference = ctx.config.get("migration", {}).get("ripsim_googletest_asset")
    if not isinstance(reference, dict):
        raise ContractError("RipSim requires its exact public GoogleTest dependency archive")
    descriptor = asset_descriptor(reference, ctx.output)
    if descriptor.get("source_kind") != "dependency" or descriptor.get("git_commit") != COMMIT:
        raise ContractError("RipSim GoogleTest source is not bound to the original commit")
    declarations(git(ctx.repo(task), "show", START + ":CMakeLists.txt").decode())
    source_bytes = descriptor["archive"]["input_bytes"]
    if type(source_bytes) is not int or not 0 < source_bytes <= 64 * 1024**2:
        raise ContractError("RipSim original GoogleTest source exceeds its copy budget")
    tools = ctx.bundle(Path(__file__).parent, ["native_ripsim.py", "native_fetchcontent.py"], role="toolchain")
    path = ctx.output / "ripsim-googletest.json"
    atomic_json(path, {"schema_version": SCHEMA, "asset_sha256": reference["sha256"],
                      "git_commit": COMMIT, "source": SOURCE})

    def configure(mode):
        return shlex.join(["python3", TOOLS + "/native_ripsim.py", "--spec", SPEC,
                           "--workspace", "/workspace", "--mode", mode])

    return {
        "configure": configure("developer"),
        "build": "cmake --build build-native-public --target rip-sim asmkheiv -j4",
        "grading_bootstrap": [
            configure("grader"), "cmake --build build-oracle --target rip-sim asmkheiv -j4",
        ],
        "scratch_paths": ["build-native-public"],
        "cache_mounts": {"ripsim-dependencies": CACHE},
        "build_scratch_mb_extra": (2 * source_bytes + 1024**2 - 1) // 1024**2,
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
    parser.add_argument("--mode", choices=("developer", "grader"), required=True)
    args = parser.parse_args()
    command = configuration(json.loads(args.spec.read_text()), args.workspace, args.mode)
    environment = {k: v for k, v in os.environ.items() if k != "ORACLE_VARIANT"}
    print("RIPSIM_PUBLIC_DEPENDENCY " + json.dumps({"git_commit": COMMIT, "command": command}), flush=True)
    return subprocess.run(command, cwd=args.workspace, env=environment).returncode


if __name__ == "__main__":
    raise SystemExit(main())
