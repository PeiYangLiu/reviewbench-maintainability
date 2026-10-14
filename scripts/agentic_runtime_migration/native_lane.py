"""Replay frozen native assertions against the whole isolated candidate tree."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shlex
import shutil
import tempfile

from reviewbench_maintainability.agentic.contracts import ContractError, RuntimeRecipe, atomic_json, component, file_hash
from reviewbench_maintainability.agentic.snapshots import extract_tar, git, gitlinks, start_archive
from .common import relocate
from .native_public_wrappers import public_wrapper
from .native_submodules import adapt as pinned_submodules, task_local_cache
from .native_kokkos import adapt as kokkos_dependency
from .native_api import adapt as public_api_compatibility
from .native_rippled import migrate as rippled_dependencies
from .native_ripsim import adapt as ripsim_dependency
from .native_solidity import adapt as solidity_dependencies
from .native_mqt import mqt95_submodules, mqt96 as mqt96_dependencies
from .native_conan import migrate as conan_dependencies


def limit_jobs(text):
    return re.sub(r"-j\s*(\d+)", lambda match: "-j" + str(min(int(match[1]) or 4, 4)), text)


def production_prefix(command):
    """Keep only the production build before a separate private-harness compile."""
    markers = [i for token in ("/harnesses/", "$ORACLE_ARTIFACT_DIR") if (i := command.find(token)) >= 0]
    if not markers:
        return command
    at = min(markers)
    # Shell command boundaries, including a continued compiler line.
    start = max(command.rfind(";", 0, at), command.rfind("&&", 0, at))
    line = command.rfind("\n", 0, at)
    while line > 0 and command[:line].rstrip().endswith("\\"):
        line = command.rfind("\n", 0, line)
    start = max(start, line)
    if start < 0:
        raise ContractError("native public production-build segment needs an explicit adapter")
    result = command[:start].strip().rstrip("&;")
    if not re.search(
            r"(?:^|;|&&|\n)\s*(?:g\+\+(?:-\d+)?|gcc(?:-\d+)?|clang\+\+(?:-\d+)?|clang(?:-\d+)?|"
            r"cmake\s+--build|make|protoc)(?=\s|$)", result):
        raise ContractError("native public build prefix does not compile production code")
    return result


def public_configure_command(text, *, source_directory=".", build_directory="build-oracle"):
    """Expose a literal repository CMake configuration, never a private helper."""
    if source_directory not in {".", "Development"} or build_directory not in {"build-oracle", "build-native-public"}:
        raise ContractError("native public CMake configuration has unsupported declared paths")
    lines = [line.strip() for line in text.splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    if lines and lines[0] in {"set -euo pipefail", "set -eu", "set -e"}:
        lines = lines[1:]
    command = "\n".join(lines).replace("\\\n", " ")
    if (any(token in command for token in ("$", "`", ";", "&&", "||", "|", "{root}", "/harnesses/"))
            or "\n" in command):
        raise ContractError("native configure helper is not a literal public CMake configuration")
    try:
        arguments = shlex.split(command)
    except ValueError as exc:
        raise ContractError("native configure helper has invalid shell syntax") from exc
    if not arguments or arguments[0] != "cmake":
        raise ContractError("native configure helper needs a public dependency/build adapter")
    if arguments.count("-S") != 1 or arguments[arguments.index("-S") + 1:arguments.index("-S") + 2] != [source_directory]:
        raise ContractError("native public CMake configuration must use the current candidate")
    if arguments.count("-B") != 1 or arguments[arguments.index("-B") + 1:arguments.index("-B") + 2] != [build_directory]:
        raise ContractError("native public CMake configuration needs its declared build directory")
    if any("ORACLE" in word.upper() and not word == "build-oracle" or
           ("CMAKE_PROJECT" in word and "INCLUDE" in word) or word in {"-P", "-E", "--build", "--install"}
           for word in arguments[1:]):
        raise ContractError("native public configuration contains a private or executable helper")
    return shlex.join(arguments)


def configure_helper(ctx, task, command):
    arguments = shlex.split(command)
    if len(arguments) != 2 or arguments[0] != "bash":
        raise ContractError("native configure helper needs a public dependency/build adapter")
    match = re.fullmatch(r"\{root\}/harnesses/([A-Za-z0-9_.-]+\.sh)", arguments[1])
    if not match:
        raise ContractError("native configure helper has an unsupported source location")
    source = ctx.legacy / "native/harnesses" / match[1]
    if source.is_symlink() or not source.is_file():
        raise ContractError("native original configure helper is missing or linked")
    result = public_configure_command(source.read_text())
    atomic_json(ctx.output / "native-public-configure" / (component(task["id"]) + ".json"), {
        "id": task["id"], "start_sha": task["start_sha"], "source": str(source),
        "source_sha256": file_hash(source), "public_command": result,
        "method": "literal public CMake argv; original flags preserved; comments/private helper path not exposed",
        "source_changes": [], "test_changes": [],
    })
    return result


def autopas_public_build(ctx, task, plan):
    """Extract only the literal public library build from the input-3 wrapper.

    This is not a CMake interpreter or a general shell-prefix heuristic. Every
    statement in the small frozen wrapper must be accounted for; executable
    helpers, extra targets and implicit test-switch changes require an adapter.
    """
    error = "native AutoPas public build needs a literal, dependency-closed subproject adapter"
    try:
        stages = [shlex.split(part) for part in plan["build_command"].split(";")]
    except ValueError as exc:
        raise ContractError(error) from exc
    if len(stages) != 6 or stages[0] != ["set", "-e"] or len(stages[1]) != 4:
        raise ContractError(error)
    build_dir, project_dir = stages[1][2:]
    if (stages[1][:2] != ["rm", "-rf"]
            or any(not re.fullmatch(r"oracle-[A-Za-z0-9_-]+", name) for name in (build_dir, project_dir))
            or stages[2] != ["mkdir", project_dir] or len(stages[3]) != 3):
        raise ContractError(error)
    helper = re.fullmatch(r"\{root\}/harnesses/([A-Za-z0-9_.-]+\.txt)", stages[3][1])
    if (not helper or stages[3][0] != "cp" or stages[3][2] != project_dir + "/CMakeLists.txt"
            or stages[4][:5] != ["cmake", "-S", project_dir, "-B", build_dir]
            or len(stages[5]) != 6 or stages[5][:4] != ["cmake", "--build", build_dir, "--target"]
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", stages[5][4])
            or not re.fullmatch(r"-j[1-9]\d*", stages[5][5])):
        raise ContractError(error)
    options = stages[4][5:]
    if (len(options) != 5 or options[0] != "-GNinja"
            or not re.fullmatch(r"-DCMAKE_BUILD_TYPE=(Debug|Release|RelWithDebInfo|MinSizeRel)", options[1])
            or not re.fullmatch(r"-DCMAKE_CXX_COMPILER=(g\+\+|clang\+\+)(-\d+)?", options[2])
            or options[3] != "-DAUTOPAS_REPOSITORY=$PWD"
            or not re.fullmatch(r"-DAUTOPAS_ORACLE_HARNESS=\{root\}/harnesses/[A-Za-z0-9_.-]+\.cpp", options[4])):
        raise ContractError(error)
    source = ctx.legacy / "native/harnesses" / helper[1]
    if source.is_symlink() or not source.is_file():
        raise ContractError("native original CMake wrapper is missing or linked")
    statements = []
    for line in source.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([a-z_]+)\((.*)\)", line)
        if not match:
            raise ContractError(error)
        try:
            statements.append((match[1], shlex.split(match[2])))
        except ValueError as exc:
            raise ContractError(error) from exc
    cache_types = {
        "AUTOPAS_BUILD_TESTS": "BOOL", "AUTOPAS_BUILD_EXAMPLES": "BOOL",
        "AUTOPAS_FORMATTING_TARGETS": "BOOL", "AUTOPAS_OPENMP": "BOOL",
        "AUTOPAS_MIN_LOG_LVL": "STRING", "Eigen3_ForceBundled": "BOOL", "spdlog_ForceBundled": "BOOL",
    }
    target = stages[5][4]
    if (len(statements) != len(cache_types) + 7
            or statements[0] != ("cmake_minimum_required", ["VERSION", "3.14"])
            or statements[1][0] != "project" or len(statements[1][1]) != 3
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", statements[1][1][0])
            or statements[1][1][1:] != ["LANGUAGES", "CXX"]
            or statements[-3:] != [
                ("add_subdirectory", ["${AUTOPAS_REPOSITORY}", "autopas"]),
                ("add_executable", [target, "${AUTOPAS_ORACLE_HARNESS}"]),
                ("target_link_libraries", [target, "PRIVATE", "molecularDynamicsLibrary"])]):
        raise ContractError(error)
    cache = {}
    for name, words in statements[2:-5]:
        if (name != "set" or len(words) != 6 or words[0] not in cache_types or words[0] in cache
                or words[2:] != ["CACHE", cache_types[words[0]], "", "FORCE"]
                or not re.fullmatch(r"[A-Za-z0-9_]+", words[1])
                or (cache_types[words[0]] == "BOOL" and words[1] not in {"ON", "OFF", "TRUE", "FALSE", "0", "1"})):
            raise ContractError(error)
        cache[words[0]] = words[1]
    if set(cache) != set(cache_types):
        raise ContractError(error)
    compile_name, compile_flags = statements[-5]
    link_name, link_flags = statements[-4]
    if (compile_name != "add_compile_options" or not compile_flags
            or any(not re.fullmatch(r"-O[0-3sg]|-g[0-3]?|--coverage", word) for word in compile_flags)
            or link_name != "add_link_options" or link_flags != ["--coverage"]):
        raise ContractError(error)
    public_files = [
        "CMakeLists.txt", "src/CMakeLists.txt", "src/autopas/CMakeLists.txt", "applicationLibrary/CMakeLists.txt",
        "src/autopas/LogicHandler.h", "src/autopas/tuning/AutoTuner.cpp", "src/autopas/utils/ExceptionHandler.cpp",
        "applicationLibrary/molecularDynamics/molecularDynamicsLibrary/MoleculeLJ.cpp",
    ]
    entries = {}
    for entry in git(ctx.repo(task), "ls-tree", "-r", "-z", task["start_sha"], "--", *public_files).split(b"\0"):
        if entry:
            metadata, path = entry.split(b"\t", 1)
            if metadata.startswith((b"100644 blob ", b"100755 blob ")):
                entries[path.decode()] = metadata.split()[2].decode()
    if set(entries) != set(public_files):
        raise ContractError("native AutoPas public production inputs are not tracked regular source files")
    for path, library in (("src/autopas/CMakeLists.txt", "autopas"),
                          ("applicationLibrary/CMakeLists.txt", "molecularDynamicsLibrary")):
        text = git(ctx.repo(task), "show", task["start_sha"] + ":" + path).decode()
        if not re.search(r"(?im)^\s*add_library\s*\(\s*" + library + r"\s+STATIC\b", text):
            raise ContractError("native AutoPas public production target is missing")
    arguments = ["cmake", "-S", ".", "-B", "build-oracle", *options[:3]]
    arguments += [f"-D{name}:{cache_types[name]}={value}" for name, value in cache.items()]
    # Reproduce the original add_subdirectory context, not standalone defaults.
    arguments += ["-DAUTOPAS_STANDALONE_BUILD=OFF"]
    arguments += [f"-DCMAKE_{language}_FLAGS=" + " ".join(compile_flags) for language in ("C", "CXX")]
    arguments += [f"-DCMAKE_{kind}_LINKER_FLAGS=" + " ".join(link_flags) for kind in ("EXE", "SHARED", "MODULE")]
    configure = public_configure_command(shlex.join(arguments))
    build = "cmake --build build-oracle --target molecularDynamicsLibrary " + limit_jobs(stages[5][5])
    atomic_json(ctx.output / "native-public-build" / (component(task["id"]) + ".json"), {
        "id": task["id"], "start_sha": task["start_sha"], "source": str(source), "source_sha256": file_hash(source),
        "public_configure": configure, "public_build": build, "public_inputs": entries,
        "method": "literal public cache/compiler options and tracked repository library targets; wrapper stays grader-only",
        "source_changes": [], "test_changes": [],
    })
    return configure, build


def mariadb_submodules(ctx, task, configure, modules):
    """Close the client-only build over Connector/C's exact source gitlink."""
    arguments = shlex.split(configure or "")
    if not {"-DWITHOUT_SERVER=ON", "-DWITH_SSL=system"}.issubset(arguments):
        raise ContractError("native MariaDB submodule adapter requires the public client-only/system-SSL configuration")
    pins = {item["path"]: item["commit"] for item in gitlinks(ctx.repo(task), task["start_sha"])}
    declared = [item for item in modules if item["path"] == "libmariadb"]
    if len(declared) != 1 or declared[0]["commit"] != pins.get("libmariadb"):
        raise ContractError("native MariaDB Connector/C source policy differs from the Git pin")
    configured = ctx.config.get("migration", {}).get("native_submodule_repositories", {})
    location = configured.get(task["repo"], {}).get("libmariadb")
    if not location:
        raise ContractError("native libmariadb requires local pinned source in migration.native_submodule_repositories")
    repository = Path(location)
    if (not repository.is_absolute() or not repository.is_dir()
            or not ((repository / ".git").exists()
                    or ((repository / "HEAD").is_file() and (repository / "objects").is_dir()))):
        raise ContractError("native libmariadb source must be an absolute local Git repository")
    commit = pins["libmariadb"]
    if git(repository, "cat-file", "-t", commit).strip() != b"commit":
        raise ContractError("native libmariadb Git pin is not a commit")
    if gitlinks(repository, commit):
        raise ContractError("native libmariadb requires explicit closure for nested gitlinks")
    entries = [entry for entry in git(repository, "ls-tree", "-r", "-l", "-z", commit).split(b"\0") if entry]
    cmake = git(repository, "ls-tree", commit, "--", "CMakeLists.txt")
    if not cmake.startswith((b"100644 blob ", b"100755 blob ")):
        raise ContractError("native libmariadb public CMake input is missing")
    if len(entries) > 10000 or sum(int(entry.split(b"\t", 1)[0].split()[3]) for entry in entries) > 128 * 1024**2:
        raise ContractError("native libmariadb source exceeds the bounded dependency adapter")
    with tempfile.TemporaryDirectory(prefix="native-connector-", dir=ctx.scratch) as temporary:
        curated = Path(temporary) / "source"
        # Never copy a checkout, run submodule update, honor export-ignore, or
        # reuse compiled objects from the frozen run.
        with start_archive(repository, commit, timeout=60) as stream:
            extract_tar(stream, curated)
        asset = ctx.bundle(curated, role="submodule", git_commit=commit)
    result = [{**item} if item["path"] != "libmariadb"
              else {"path": "libmariadb", "commit": commit, "asset": asset} for item in modules]
    atomic_json(ctx.output / "native-public-submodules" / (component(task["id"]) + ".json"), {
        "id": task["id"], "start_sha": task["start_sha"], "path": "libmariadb", "git_commit": commit,
        "source_repository": str(repository), "asset": asset, "files": len(entries),
        "method": "exact public Git dependency tree including tests; no checkout or export transformations",
        "source_changes": [], "test_changes": [],
    })
    return result


def completed_signature(ctx, oracle, plan):
    root = ctx.legacy / "native"
    outputs = []
    for variant in ("start", "golden"):
        for run in oracle.get("runs", {}).get(variant, []):
            log = root / run["log"]
            text = re.sub(r"\x1b\[[0-9;]*m", "", log.read_text(errors="replace"))
            outputs.append([line for line in text.splitlines() if line.strip() and not line.startswith("ORACLE_")])
    if not outputs:
        raise ContractError("native frozen execution output is missing")
    if plan.get("test_parser") == "gtest":
        names = set(re.findall(r"^\[\s+OK\s+\]\s+(\S+)\s+\(", "\n".join(outputs[0]), re.M))
        for output in outputs[1:]:
            names &= set(re.findall(r"^\[\s+OK\s+\]\s+(\S+)\s+\(", "\n".join(output), re.M))
        if len(names) < plan["expected_min_passed"]:
            raise ContractError("frozen GoogleTest identities are incomplete")
        return "(?s)" + "".join(r"(?=.*^\[\s+OK\s+\]\s+" + re.escape(name) + r"\s+\()"
                               for name in sorted(names)) + ".*"
    if plan.get("test_parser") == "ctest":
        return r"^100% tests passed, 0 tests failed out of " + str(plan["expected_min_passed"]) + r"\s*$"
    stable = [line for line in outputs[0] if all(line in other for other in outputs[1:])]
    stable = [line for line in dict.fromkeys(stable)
              if not re.match(r"(?i)^(warning|note|debug|trace|info|\[.*\]|time|elapsed)", line)]
    if not stable:
        raise ContractError("native harness needs an observable assertion-completion adapter")
    # All stable completed-harness output lines must be observed; a successful
    # process exit with empty stdout never suffices.
    return "(?s)" + "".join(r"(?=.*^" + re.escape(line) + r"$)" for line in stable) + ".*"


def migrate(ctx, task, oracle):
    conan = conan_dependencies(ctx, task, oracle)
    if conan is not None:
        return conan
    rippled = rippled_dependencies(ctx, task, oracle)
    if rippled is not None:
        return rippled
    plans = {p["input_index"]: p for p in
             (json.loads(line) for line in (ctx.legacy / "native/plans.jsonl").read_text().splitlines())}
    plan = plans[oracle["input_index"]]
    if plan.get("repo") != task["repo"]:
        raise ContractError("native plan index belongs to a different repository")
    image, audit = ctx.image(plan["docker_image"])
    api = public_api_compatibility(ctx, task, plan)
    if "ORACLE_VARIANT" in plan["build_command"] and "golden" in plan["build_command"] and not api:
        raise ContractError("native public-interface compatibility adapter is required")
    if api:
        plan = api["plan"]
    submodule_adapter = mqt95_submodules(ctx, task, plan) or pinned_submodules(ctx, task, plan)
    if submodule_adapter:
        plan, pinned_modules = submodule_adapter
    kokkos = kokkos_dependency(ctx, task, plan, image)
    if kokkos:
        plan = kokkos["plan"]
    all_command = str(plan.get("configure_command", "")) + "\n" + plan["build_command"]
    if "git submodule update" in all_command:
        raise ContractError("native build requires pinned submodule sources")
    if (("{cache}" in all_command or "{root}/cache" in all_command or plan.get("env"))
            and not (submodule_adapter and task_local_cache(task, plan))):
        raise ContractError("native build requires a curated dependency-cache bundle")
    lane_root = ctx.legacy / "native"
    directory = ctx.task_directory(task, oracle, "native")
    runtime_root = "/workspace/.reviewbench/native"
    context = {"root": runtime_root, "artifact": runtime_root + "/artifacts/task",
               "cache": runtime_root + "/cache", "variant": "candidate"}
    normalize = lambda text: limit_jobs(relocate(text.replace(str(lane_root), runtime_root), context))
    raw_build = plan["build_command"]
    configure = plan.get("configure_command")
    wrapper = (api or kokkos or ripsim_dependency(ctx, task, plan)
               or solidity_dependencies(ctx, task, plan)
               or mqt96_dependencies(ctx, task, plan)
               or public_wrapper(ctx, task, plan, public_configure_command))
    match = re.fullmatch(r"bash ['\"]?\{root\}/harnesses/([^'\"\s]+)['\"]?", raw_build.strip())
    if wrapper:
        configure, public = wrapper["configure"], wrapper["build"]
    elif task["repo"] == "AutoPas/AutoPas" and plan["input_index"] == 3:
        if configure:
            raise ContractError("native AutoPas subproject has an unaccounted configure helper")
        configure, public = autopas_public_build(ctx, task, plan)
    elif task["repo"] == "3rdparty/stout":
        public = ("mkdir -p build-oracle/generated && "
                  "protoc -Iinclude --cpp_out=build-oracle/generated "
                  "include/stout/flags/v1/flag.proto include/stout/flags/v1/positional_argument.proto "
                  "include/stout/flags/v1/subcommand.proto && "
                  "g++-11 -std=c++17 -O0 -g --coverage $(pkg-config --cflags protobuf absl_time absl_strings) "
                  "-Iinclude -Ibuild-oracle/generated -c include/stout/flags/flags.cc -o build-oracle/public-flags.o")
    elif match:
        public = production_prefix((lane_root / "harnesses" / match[1]).read_text())
    elif task["repo"] == "bitcoin/bitcoin":
        public = "g++-11 -std=c++20 -Isrc -fsyntax-only -x c++ " + shlex.quote(task["locality"]["path"])
    else:
        public = production_prefix(raw_build)
    if "/harnesses/" in public or "$ORACLE_ARTIFACT_DIR" in public:
        raise ContractError("private harness cannot be included in the public developer build")
    if configure and "/harnesses/" in configure:
        configure = configure_helper(ctx, task, configure)
    modules = api["submodules"] if api else pinned_modules if submodule_adapter else ctx.modules(task)
    public_setup = []
    if task["repo"] == "MariaDB/server" and plan["input_index"] in {18, 19}:
        if not re.fullmatch(r"cmake --build build-oracle --target mariadb-binlog -j\d+", raw_build):
            raise ContractError("native MariaDB adapter requires the original public client target")
        modules = mariadb_submodules(ctx, task, configure, modules)
        # Keep the original CMake flags AND its Connector/C existence check.
        # All required sources are already pinned; optional gitlinks must not
        # trigger CMake's recursive network checkout in the candidate sandbox.
        public_setup.append("git config --local cmake.update-submodules no")
    # Only relocate build/test filesystem paths; the frozen assertions and
    # production sources are not edited or replaced with reference files.
    with tempfile.TemporaryDirectory(prefix="native-harnesses-", dir=ctx.scratch) as temporary:
        curated = Path(temporary)
        target = curated / "harnesses"
        shutil.copytree(lane_root / "harnesses", target, symlinks=True)
        for path in target.rglob("*"):
            if path.is_file() and not path.is_symlink() and path.suffix in {".sh", ".py"}:
                text = path.read_text()
                text = text.replace(str(lane_root), runtime_root)
                path.write_text(limit_jobs(text) if path.name.startswith(("build_", "configure_")) else text)
        harness = ctx.bundle(curated, role="grader")
    bootstrap = ["mkdir -p /workspace/.reviewbench/tmp " + runtime_root + "/artifacts/task " + runtime_root + "/cache"]
    bootstrap.extend(public_setup)
    if configure:
        bootstrap.append(normalize(configure))
    grade_bootstrap = [
        "ln -s /opt/reviewbench-assets/native/harnesses " + runtime_root + "/harnesses",
        *[normalize(command) for command in (wrapper["grading_bootstrap"] if wrapper else [raw_build])],
    ]
    scratch = {"build-oracle"}
    if wrapper:
        scratch.update(wrapper["scratch_paths"])
    scratch.update(re.findall(r"(?:mkdir(?:\s+-p)?|rm\s+-rf|-B)\s+(oracle-[A-Za-z0-9_-]+)", all_command))
    expected = oracle.get("pass_to_pass_tests") or plan.get("pass_to_pass_tests")
    if not expected:
        raise ContractError("native fixed test identities are missing")
    recipe = RuntimeRecipe(
        task_id=task["id"], repo=task["repo"], start_sha=task["start_sha"], lane="native",
        image=image, image_audit=audit, cpus=4, memory_mb=8192,
        build_scratch_mb=12288 + (wrapper.get("build_scratch_mb_extra", 0) if wrapper else 0),
        scratch_paths=sorted(scratch), submodules=modules,
        cache_mounts={"ccache": "/opt/reviewbench-cache/ccache",
                      **(wrapper.get("cache_mounts", {}) if wrapper else {})},
        command_timeout_seconds=plan.get("timeout_seconds", 1800),
        env={"ORACLE_ROOT": runtime_root, "ORACLE_ARTIFACT_DIR": runtime_root + "/artifacts/task",
             "ORACLE_VARIANT": "candidate", "TMPDIR": "/workspace/.reviewbench/tmp",
             "CMAKE_BUILD_PARALLEL_LEVEL": "4", "CCACHE_DIR": "/opt/reviewbench-cache/ccache",
             "CCACHE_BASEDIR": "/workspace", "CCACHE_COMPRESS": "1", "CCACHE_MAXSIZE": "1G",
             "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "SOURCE_DATE_EPOCH": "0"},
        bootstrap=bootstrap, dev_checks=[normalize(public)],
        assets=[{**harness, "kind": "archive", "visibility": "grader", "target": "/opt/reviewbench-assets/native"},
                *(wrapper.get("assets", []) if wrapper else [])],
        grading={"multifile_build": True, "immutable_paths": ctx.immutable_tests(task),
                 "golden_patch": ctx.golden(task, oracle, "native", directory),
                 "bootstrap": grade_bootstrap,
                 "commands": [{"command": normalize(api["test_command"] if api else plan["test_command"]), "parser": "regex",
                               "expected_tests": expected, "success_pattern": completed_signature(ctx, oracle, plan)}]},
    )
    recipe.validate()
    return recipe
