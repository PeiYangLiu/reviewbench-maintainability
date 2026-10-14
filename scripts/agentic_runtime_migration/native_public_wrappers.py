"""Bounded public projections of selected frozen native wrapper shapes.

The wrappers are parsed, never executed during migration. Only repository build
inputs enter public commands; private targets and compatibility switches remain
in the grader. Unaccounted shell, CMake or Make instructions reject extraction.
"""
from __future__ import annotations

import hashlib
import inspect
import re
import shlex

from reviewbench_maintainability.agentic.contracts import ContractError, atomic_json, component, file_hash
from reviewbench_maintainability.agentic.snapshots import git


PUBLIC_BUILD = "build-native-public"
STREAM_HEADER = "google/cloud/pubsublite/internal/resumable_async_streaming_read_write_rpc.h"
FLUTTER_SOURCES = (
    "flutter/shell/platform/common/client_wrapper/core_implementations.cc",
    "flutter/shell/platform/common/client_wrapper/standard_codec.cc",
    "flutter/shell/platform/common/json_message_codec.cc",
    "flutter/shell/platform/tizen/channels/key_mapping.cc",
    "flutter/shell/platform/tizen/channels/keyboard_channel.cc",
    "flutter/shell/platform/tizen/logger.cc",
)
# Pin the opaque grader-only program, not its generated corpus or Python source.
_DISKANN_CORPUS_SHA256 = "5823ca2aa8d01364ca087c753b5e5b1b2264e3d79edb07a5855a1a9f90f627bb"


def _require(condition, message="native public wrapper contains unsupported instructions"):
    if not condition:
        raise ContractError(message)


def _lines(text):
    return [line.strip() for line in text.replace("\\\n", " ").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def _words(text):
    try:
        return shlex.split(text)
    except ValueError as exc:
        raise ContractError("native public wrapper has invalid literal syntax") from exc


def _cmake_calls(text):
    text = "\n".join(_lines(text))
    calls = []
    while text:
        match = re.match(r"([a-z_]+)\s*\(([^()]*)\)\s*", text)
        _require(match is not None, "native public wrapper has unsupported CMake statements")
        calls.append((match[1], _words(match[2])))
        text = text[match.end():]
    return calls


def _source(ctx, name, hashes):
    path = ctx.legacy / "native/harnesses" / name
    _require(path.is_file() and not path.is_symlink(), "native original wrapper is missing or linked")
    hashes[name] = file_hash(path)
    return path.read_text()


def _public_inputs(ctx, task, names, target=None, *, target_kind="add_library"):
    entries = {}
    for entry in git(ctx.repo(task), "ls-tree", "-r", "-z", task["start_sha"], "--", *names).split(b"\0"):
        if entry:
            metadata, name = entry.split(b"\t", 1)
            if metadata.startswith((b"100644 blob ", b"100755 blob ")):
                entries[name.decode()] = metadata.split()[2].decode()
    _require(set(entries) == set(names), "native public compile inputs must be tracked regular repository files")
    if target:
        _require(target_kind in {"add_library", "add_executable"})
        path, name = target
        text = git(ctx.repo(task), "show", task["start_sha"] + ":" + path).decode()
        _require(re.search(r"(?m)^\s*" + target_kind + r"\s*\(\s*" + re.escape(name) + r"(?=\s|\))", text),
                 "native public production " + ("library" if target_kind == "add_library" else "executable")
                 + " target is missing")
    return entries


def google_cloud_variant(text, index):
    """Select an EXISTING frozen harness interface from current public types.

    This standalone function also runs in the grader. It neither identifies a
    revision nor changes a source/assertion; unknown interfaces fail closed.
    """
    import re

    text = re.sub(r"/\*.*?\*/|//[^\n]*", "", text, flags=re.S)
    aliases = {}
    for name in ("AsyncSleeper", "StreamInitializer"):
        matches = re.findall(r"\busing\s+" + name + r"\s*=\s*([^;]+);", text)
        if len(matches) != 1:
            raise ValueError("ambiguous current Google Cloud public interface")
        aliases[name] = re.sub(r"\s+", "", matches[0])
    unique = "std::unique_ptr<AsyncStreamingReadWriteRpc<RequestType,ResponseType>>"
    shared = "std::shared_ptr<" + unique + ">"
    initializer = lambda arg: "std::function<future<StatusOr<" + arg + ">>(" + arg + ")>"
    millis = "std::function<future<void>(std::chrono::milliseconds)>"
    duration = "std::function<future<void>(std::chrono::duration<double>)>"
    methods = re.findall(r"\bvirtual\s+future<void>\s+(Finish|Shutdown)\s*\(\s*\)\s*=\s*0\s*;", text)
    if index == 65 and aliases == {"AsyncSleeper": duration, "StreamInitializer": initializer(unique)} and methods == ["Finish"]:
        return "start"
    if index == 67 and aliases == {"AsyncSleeper": millis, "StreamInitializer": initializer(shared)} and methods == ["Shutdown"]:
        return "start"
    if index in (65, 66, 67) and aliases == {"AsyncSleeper": millis, "StreamInitializer": initializer(unique)} and methods == ["Shutdown"]:
        return "candidate"
    raise ValueError("unsupported current Google Cloud public interface")


def _google(ctx, task, plan, configure_parser, hashes):
    index = plan["input_index"]
    original = f"bash '{{root}}/harnesses/build_google_cloud_resumable_stream.sh' {index}"
    _require(_words(plan["build_command"]) == _words(original) and not plan.get("configure_command"))
    lines = _lines(_source(ctx, "build_google_cloud_resumable_stream.sh", hashes))
    prelude = [
        "set -euo pipefail", "task=$1", "definition=",
        'if [[ "${ORACLE_VARIANT}" == start ]]; then', "rm -rf build-oracle",
        'if [[ "${task}" == 65 ]]; then', "definition=GOOGLE_CLOUD_ORACLE_TASK65_EXACT",
        'elif [[ "${task}" == 67 ]]; then', "definition=GOOGLE_CLOUD_ORACLE_TASK67_EXACT",
        "fi", "fi", "export CCACHE_BASEDIR=$PWD",
    ]
    _require(len(lines) == len(prelude) + 2 and lines[:-2] == prelude)
    arguments = _words(lines[-2])
    _require(arguments[:6] == ["cmake", "-S", ".", "-B", "build-oracle", "-GNinja"])
    private = [
        "-DCMAKE_PROJECT_INCLUDE=$(dirname $0)/google_cloud_resumable_stream.cmake",
        "-DGOOGLE_CLOUD_ORACLE_HARNESS=$(dirname $0)/google_cloud_resumable_stream.cpp",
        "-DGOOGLE_CLOUD_ORACLE_DEFINITION:STRING=${definition}", ">/dev/null",
    ]
    _require(len(arguments) == 15 and arguments[-4:] == private)
    public = arguments[6:-4]
    keys = ["BUILD_TESTING", "GOOGLE_CLOUD_CPP_ENABLE:STRING", "GOOGLE_CLOUD_CPP_ENABLE_GRPC",
            "GOOGLE_CLOUD_CPP_ENABLE_REST", "GOOGLE_CLOUD_CPP_ENABLE_EXAMPLES"]
    for key, argument in zip(keys, public):
        pattern = r"-D" + re.escape(key) + "=" + ("" if key.endswith(":STRING") else "(ON|OFF)")
        _require(re.fullmatch(pattern, argument))
    _require(_words(lines[-1]) == [
        "cmake", "--build", "build-oracle", "--target", "google_cloud_resumable_stream_oracle", "-j4"])
    target = "google_cloud_resumable_stream_oracle"
    callback = "add_google_cloud_resumable_stream_oracle"
    _require(_cmake_calls(_source(ctx, "google_cloud_resumable_stream.cmake", hashes)) == [
        ("function", [callback]),
        ("add_executable", [target, "${GOOGLE_CLOUD_ORACLE_HARNESS}"]),
        ("target_link_libraries", [target, "PRIVATE", "google-cloud-cpp::common"]),
        ("target_compile_options", [target, "PRIVATE", "-O0", "-g", "--coverage"]),
        ("target_link_options", [target, "PRIVATE", "--coverage"]),
        ("if", ["GOOGLE_CLOUD_ORACLE_DEFINITION"]),
        ("target_compile_definitions", [target, "PRIVATE", "${GOOGLE_CLOUD_ORACLE_DEFINITION}"]),
        ("endif", []), ("endfunction", []), ("cmake_language", ["DEFER", "CALL", callback]),
    ])
    inputs = _public_inputs(ctx, task, [
        "CMakeLists.txt", "google/cloud/CMakeLists.txt", STREAM_HEADER,
        "google/cloud/internal/future_impl.cc", "google/cloud/status.cc", "google/cloud/internal/log_impl.cc",
    ], ("google/cloud/CMakeLists.txt", "google_cloud_cpp_common"))
    try:
        google_cloud_variant(git(ctx.repo(task), "show", task["start_sha"] + ":" + STREAM_HEADER).decode(), index)
    except ValueError as exc:
        raise ContractError(str(exc)) from exc
    configure = configure_parser(shlex.join([
        "cmake", "-S", ".", "-B", PUBLIC_BUILD, "-GNinja", *public]), build_directory=PUBLIC_BUILD)
    # The original export CCACHE_BASEDIR=$PWD is already represented by the
    # native recipe's CCACHE_BASEDIR=/workspace and workdir=/workspace.
    probe = (inspect.getsource(google_cloud_variant) +
             "\nfrom pathlib import Path\nimport sys\n"
             "print(google_cloud_variant(Path(sys.argv[1]).read_text(), int(sys.argv[2])))\n")
    grade = ("native_variant=$(python3 -c " + shlex.quote(probe) + " " + shlex.quote(STREAM_HEADER) +
             " " + str(index) + ') && ORACLE_VARIANT="$native_variant" ' + plan["build_command"])
    return {"configure": configure, "build": f"cmake --build {PUBLIC_BUILD} --target google_cloud_cpp_common -j4",
            "grading_bootstrap": [grade], "public_inputs": inputs, "required_dependency_inputs": []}


def _flutter(ctx, task, plan, hashes):
    _require(_words(plan["build_command"]) == [
        "make", "-f", "{root}/harnesses/flutter_tizen_keyboard.mk", "-j2"])
    _require(_words(plan.get("configure_command", "")) == [
        "bash", "{root}/harnesses/configure_flutter_tizen_keyboard.sh"])
    _require(_lines(_source(ctx, "configure_flutter_tizen_keyboard.sh", hashes)) == [
        "set -euo pipefail", "rm -rf build-oracle", "mkdir -p build-oracle/obj"])
    lines = _lines(_source(ctx, "flutter_tizen_keyboard.mk", hashes))
    names = ["CXX", "BUILD", "OBJ", "HARNESS_DIR", "CPPFLAGS", "CXXFLAGS", "LDLIBS", "SOURCES", "OBJECTS"]
    _require(len(lines) > len(names))
    values = {}
    for name, line in zip(names, lines):
        match = re.fullmatch(re.escape(name) + r"\s*:=\s*(.*)", line)
        _require(match is not None)
        values[name] = match[1]
    compiler = _words(values["CXX"])
    _require(re.fullmatch(r"(?:ccache )?(?:g\+\+|clang\+\+)(?:-\d+)?", " ".join(compiler)))
    _require(values["BUILD"] == "build-oracle" and values["OBJ"] == "$(BUILD)/obj")
    _require(values["HARNESS_DIR"] == "$(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))")
    cppflags = _words(values["CPPFLAGS"])
    _require(cppflags == ["-I.", "-Iflutter/shell/platform/common/public", "-I$(HARNESS_DIR)/flutter_tizen_stubs"])
    flags = _words(values["CXXFLAGS"])
    _require(flags == ["-std=gnu++17", "-O0", "-g", "--coverage", "-ffunction-sections",
                       "-fdata-sections", "-MMD", "-MP"])
    _require(_words(values["LDLIBS"]) == ["--coverage", "-Wl,--gc-sections", "-pthread"])
    sources = ["$(HARNESS_DIR)/flutter_tizen_keyboard_oracle.cc", *FLUTTER_SOURCES]
    objects = ["$(OBJ)/" + name + ".o" for name in (
        "oracle", "core_implementations", "standard_codec", "json_message_codec", "key_mapping", "keyboard_channel", "logger")]
    _require(_words(values["SOURCES"]) == sources and _words(values["OBJECTS"]) == objects)
    rules = [
        "all: $(BUILD)/flutter_tizen_keyboard_oracle",
        "$(BUILD)/flutter_tizen_keyboard_oracle: $(OBJECTS)",
        "$(CXX) $(OBJECTS) $(LDLIBS) -o $@",
    ]
    for obj, source in zip(objects, sources):
        rules += [obj + ": " + source, "@mkdir -p $(OBJ)",
                  "$(CXX) $(CPPFLAGS) $(CXXFLAGS) -c $< -o $@"]
    rules += ["-include $(OBJECTS:.o=.d)", ".PHONY: all"]
    _require(lines[len(names):] == rules)
    inputs = _public_inputs(ctx, task, list(FLUTTER_SOURCES))
    public_objects = [obj.replace("$(OBJ)", PUBLIC_BUILD + "/obj") for obj in objects[1:]]
    commands = ["mkdir -p " + PUBLIC_BUILD + "/obj"]
    # dlog.h must come from the public SDK/toolchain include path. The private
    # grader stub is deliberately not republished or replaced by injected code.
    for source, obj in zip(FLUTTER_SOURCES, public_objects):
        commands.append(shlex.join([*compiler, *cppflags[:2], *flags, "-c", source, "-o", obj]))
    commands.append(shlex.join(["ar", "rcs", PUBLIC_BUILD + "/libflutter_keyboard.a", *public_objects]))
    return {"configure": "mkdir -p " + PUBLIC_BUILD + "/obj", "build": " && ".join(commands),
            "grading_bootstrap": [plan["configure_command"], plan["build_command"]],
            "public_inputs": inputs, "required_dependency_inputs": ["public Tizen dlog.h in the compiler include path"]}


def _nmos(ctx, task, plan, configure_parser, hashes):
    _require(_words(plan.get("configure_command", "")) == [
        "bash", "{root}/harnesses/configure_nmos_ocsp.sh", "{root}"])
    _require(_words(plan["build_command"]) == ["bash", "{root}/harnesses/build_nmos_ocsp.sh"])
    lines = _lines(_source(ctx, "configure_nmos_ocsp.sh", hashes))
    _require(len(lines) == 3 and lines[:2] == ["set -euo pipefail", "oracle_root=$1"])
    arguments = _words(lines[2])
    _require(arguments[:8] == ["cmake", "-S", "$oracle_root/harnesses/nmos_ocsp", "-B", "build-oracle",
                               "-GNinja", "-DNMOS_SOURCE=$PWD", "-DORACLE_ROOT=$oracle_root"])
    _require(arguments[8:] == ["-DCMAKE_BUILD_TYPE=Debug", "-DCMAKE_CXX_COMPILER_LAUNCHER=ccache",
                               "-DCMAKE_CXX_FLAGS=--coverage -O0 -g", "-DCMAKE_EXE_LINKER_FLAGS=--coverage"])
    calls = _cmake_calls(_source(ctx, "nmos_ocsp/CMakeLists.txt", hashes))
    _require(len(calls) == 9 and calls[:2] == [
        ("cmake_minimum_required", ["VERSION", "3.17"]), ("project", ["nmos-ocsp-oracle", "LANGUAGES", "CXX"])])
    cache = []
    for key, (name, words) in zip(
            ("NMOS_CPP_BUILD_EXAMPLES", "NMOS_CPP_BUILD_TESTS", "NMOS_CPP_BUILD_LLDP", "NMOS_CPP_USE_CONAN"), calls[2:6]):
        _require(name == "set" and len(words) == 6 and words[0] == key
                 and words[1] in {"ON", "OFF"} and words[2:] == ["CACHE", "BOOL", "", "FORCE"])
        cache.append("-D" + key + ":BOOL=" + words[1])
    _require(calls[6:] == [
        ("add_subdirectory", ["${NMOS_SOURCE}/Development", "nmos-cpp"]),
        ("add_executable", ["nmos_ocsp_oracle", "${ORACLE_ROOT}/harnesses/nmos_ocsp_oracle.cpp"]),
        ("target_link_libraries", ["nmos_ocsp_oracle", "PRIVATE", "nmos-cpp::nmos-cpp"]),
    ])
    build = _lines(_source(ctx, "build_nmos_ocsp.sh", hashes))
    _require(len(build) == 5 and build[:4] == [
        "set -euo pipefail", "jobs=${CMAKE_BUILD_PARALLEL_LEVEL:-12}",
        'cmake --build build-oracle --target nmos_ocsp_oracle -j"$jobs"',
        "mkdir -p build-oracle/oracle-data",
    ])
    _require(_words(build[-1]) == [
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", "build-oracle/oracle-data/key.pem",
        "-out", "build-oracle/oracle-data/cert.pem", "-sha256", "-days", "1", "-nodes", "-subj", "/CN=localhost",
        ">/dev/null", "2>&1",
    ])
    inputs = _public_inputs(ctx, task, [
        "Development/CMakeLists.txt", "Development/cmake/NmosCppLibraries.cmake",
        "Development/nmos/server_utils.cpp", "Development/nmos/ocsp_utils.cpp", "Development/ssl/ssl_utils.cpp",
    ], ("Development/cmake/NmosCppLibraries.cmake", "nmos-cpp"))
    configure = configure_parser(shlex.join([
        "cmake", "-S", "Development", "-B", PUBLIC_BUILD, "-GNinja", *arguments[8:], *cache]),
        source_directory="Development", build_directory=PUBLIC_BUILD)
    return {"configure": configure, "build": f"cmake --build {PUBLIC_BUILD} --target nmos-cpp -j4",
            "grading_bootstrap": [plan["configure_command"], plan["build_command"]],
            "public_inputs": inputs, "required_dependency_inputs": []}


def _prefix_configure(ctx, plan, configure_parser, hashes, name, flags):
    """Retain the literal CMake argv, relocating only its public build directory."""
    _require(_words(plan.get("configure_command", "")) == [
        "bash", "{root}/harnesses/configure_" + name + ".sh"])
    _require(_words(plan["build_command"]) == [
        "bash", "{root}/harnesses/build_" + name + ".sh", "{root}"])
    arguments = _words(configure_parser(_source(ctx, "configure_" + name + ".sh", hashes)))
    _require(arguments[:6] == ["cmake", "-S", ".", "-B", "build-oracle", "-GNinja"])
    _require(len(arguments[6:]) == len(flags))
    for actual, expected in zip(arguments[6:], flags):
        # A required repository test switch is preserved, never forced OFF.
        _require(re.fullmatch(r"-DWITH_TESTS=(ON|OFF)", actual) if expected == "-DWITH_TESTS=OFF"
                 else actual == expected)
    arguments[4] = PUBLIC_BUILD
    return configure_parser(shlex.join(arguments), build_directory=PUBLIC_BUILD)


def _diskann(ctx, task, plan, configure_parser, hashes):
    configure = _prefix_configure(ctx, plan, configure_parser, hashes, "diskann_public_oracle", [
        "-DCMAKE_BUILD_TYPE=Debug", "-DCMAKE_CXX_COMPILER_LAUNCHER=ccache",
        "-DCMAKE_CXX_FLAGS=--coverage -O0 -g",
        "-DCMAKE_EXE_LINKER_FLAGS=-L/opt/diskann-mkl-compat --coverage",
        "-DCMAKE_SHARED_LINKER_FLAGS=-L/opt/diskann-mkl-compat --coverage",
    ])
    text = _source(ctx, "build_diskann_public_oracle.sh", hashes)
    prefix, marker, rest = text.partition("python3 - <<'PY'\n")
    corpus, end, suffix = rest.partition("\nPY\n")
    _require(marker and end, "native DiskANN wrapper needs its original quoted corpus heredoc")
    _require(hashlib.sha256(corpus.encode()).hexdigest() == _DISKANN_CORPUS_SHA256,
             "native DiskANN private corpus program differs from the frozen original")
    lines = _lines(prefix)
    environment = "export LD_LIBRARY_PATH=/opt/diskann-mkl-compat:/usr/lib/x86_64-linux-gnu"
    _require(len(lines) == 9 and lines[:5] == [
        "set -euo pipefail", "oracle_root=$1", "jobs=${CMAKE_BUILD_PARALLEL_LEVEL:-12}", environment,
        'cmake --build build-oracle --target build_disk_index -j"$jobs"',
    ])
    _require(_words(lines[5]) == [
        "ccache", "g++", "-std=gnu++14", "-O0", "-g", "--coverage", "-fopenmp", "-march=native",
        "-DUSE_AVX2", "-DMKL_ILP64", "-Iinclude", "-I/usr/include/mkl",
        "$oracle_root/harnesses/diskann_public_oracle.cpp", "build-oracle/src/libdiskann.a",
        "-L/opt/diskann-mkl-compat", "-L/usr/lib/x86_64-linux-gnu/mkl",
        "-lmkl_intel_ilp64", "-lmkl_intel_thread", "-lmkl_core", "-liomp5",
        "-ltcmalloc", "-laio", "-lpthread", "-lm", "-ldl", "-o", "build-oracle/diskann_public_oracle",
    ])
    _require([_words(line) for line in lines[6:]] == [
        ["find", "build-oracle", "-name", "*.gcda", "-delete"],
        ["rm", "-rf", "build-oracle/oracle-data"], ["mkdir", "-p", "build-oracle/oracle-data"],
    ])
    after = _lines(suffix)
    _require(len(after) == 1 and _words(after[0]) == [
        "build-oracle/tests/build_disk_index", "--data_type", "float", "--dist_fn", "l2",
        "--data_path", "build-oracle/oracle-data/vectors.bin",
        "--index_path_prefix", "build-oracle/oracle-data/index", "-R", "16", "-L", "32",
        "-B", "0.1", "-M", "0.5", "-T", "2",
    ])
    inputs = _public_inputs(ctx, task, [
        "CMakeLists.txt", "src/CMakeLists.txt", "tests/CMakeLists.txt", "tests/build_disk_index.cpp",
        "src/pq_flash_index.cpp", "src/linux_aligned_file_reader.cpp", "src/disk_utils.cpp",
    ], ("tests/CMakeLists.txt", "build_disk_index"), target_kind="add_executable")
    return {"configure": configure,
            "build": environment + f" && cmake --build {PUBLIC_BUILD} --target build_disk_index -j4",
            "grading_bootstrap": [plan["configure_command"], plan["build_command"]],
            "public_inputs": inputs, "required_dependency_inputs": [],
            "private_corpus_sha256": _DISKANN_CORPUS_SHA256}


def _halide(ctx, task, plan, configure_parser, hashes):
    configure = _prefix_configure(ctx, plan, configure_parser, hashes, "halide_shuffle", [
        "-DCMAKE_BUILD_TYPE=RelWithDebInfo", "-DHalide_LLVM_ROOT=/usr/lib/llvm-20", "-DHalide_CCACHE_BUILD=ON",
        "-DBUILD_SHARED_LIBS=OFF", "-DWITH_TESTS=OFF", "-DWITH_PYTHON_BINDINGS=OFF", "-DWITH_TUTORIALS=OFF",
        "-DWITH_UTILS=OFF", "-DCMAKE_CXX_FLAGS=--coverage -O0 -g", "-DCMAKE_EXE_LINKER_FLAGS=--coverage",
    ])
    lines = _lines(_source(ctx, "build_halide_shuffle.sh", hashes))
    _require(len(lines) == 5 and lines[:4] == [
        "set -euo pipefail", "oracle_root=$1", "jobs=${CMAKE_BUILD_PARALLEL_LEVEL:-24}",
        'cmake --build build-oracle --target Halide -j"$jobs"',
    ])
    _require(_words(lines[4]) == [
        "g++-11", "--coverage", "-O0", "-g", "-std=c++17", "-Ibuild-oracle/include",
        "$oracle_root/harnesses/halide_shuffle_oracle.cpp", "build-oracle/src/libHalide.a",
        "/usr/lib/llvm-20/lib/liblldWasm.a", "/usr/lib/llvm-20/lib/liblldCommon.a",
        "/usr/lib/llvm-20/lib/libLLVM.so.1", "-ldl", "-pthread", "-o", "build-oracle/halide_shuffle_oracle",
    ])
    inputs = _public_inputs(ctx, task, [
        "CMakeLists.txt", "src/CMakeLists.txt", "src/DecomposeVectorShuffle.cpp",
        "src/DecomposeVectorShuffle.h", "src/CodeGen_ARM.cpp",
    ], ("src/CMakeLists.txt", "Halide"))
    return {"configure": configure, "build": f"cmake --build {PUBLIC_BUILD} --target Halide -j4",
            "grading_bootstrap": [plan["configure_command"], plan["build_command"]],
            "public_inputs": inputs, "required_dependency_inputs": []}


def public_wrapper(ctx, task, plan, configure_parser):
    key = (task["repo"], plan["input_index"])
    hashes = {}
    if key[0] == "googleapis/google-cloud-cpp" and key[1] in {65, 66, 67}:
        result = _google(ctx, task, plan, configure_parser, hashes)
    elif key == ("flutter-tizen/embedder", 58):
        result = _flutter(ctx, task, plan, hashes)
    elif key == ("sony/nmos-cpp", 121):
        result = _nmos(ctx, task, plan, configure_parser, hashes)
    elif key == ("microsoft/DiskANN", 86):
        result = _diskann(ctx, task, plan, configure_parser, hashes)
    elif key == ("halide/Halide", 68):
        result = _halide(ctx, task, plan, configure_parser, hashes)
        from .native_fetchcontent import attach
        result = attach(ctx, task, result)
    else:
        return None
    result["scratch_paths"] = [PUBLIC_BUILD]
    atomic_json(ctx.output / "native-public-wrappers" / (component(task["id"]) + ".json"), {
        "id": task["id"], "start_sha": task["start_sha"], "wrapper_sha256": hashes,
        **result, "source_changes": [], "test_changes": [],
        "method": "fully accounted wrapper; current-candidate production build; private assertions remain grader-only",
    })
    return result
