"""Bounded projections of frozen native API and public-production build plans.

The hashes attest the original build/assertion inputs, never a candidate
implementation. Runtime selection compiles the CURRENT candidate's public API.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import shlex
import tempfile

from reviewbench_maintainability.agentic.contracts import (
    ContractError, atomic_json, canonical_hash, component, file_hash,
)
from reviewbench_maintainability.agentic.snapshots import extract_tar, start_archive
from .native_submodules import closure, readonly_links


ROOT = "/workspace/.reviewbench/native"
ENTRY = "/opt/reviewbench-assets/native-api-entry.py"
PUBLIC = "/opt/reviewbench-assets/native-api-public.json"
PRIVATE = "/opt/reviewbench-assets/native-api-grader.json"
DEPS = "/opt/reviewbench-assets/native-api-dependencies"
LINK_PROBE = "/opt/reviewbench-assets/native-api-lmms-link.cpp"
SCHEMA = "reviewbench-native-api/v1"
SPDLOG_ZIP_SHA256 = "209ef3624588113c92cdaa827c0ea3c3e6a6c56381a994050dcc1ad017a7ad87"
LMMS_LINK_PROBE = (
    '#include "Sample.h"\n'
    "int main() { lmms::Sample::PlaybackState value(false, SRC_LINEAR); "
    "return static_cast<int>(value.frameIndex()); }\n"
)

PROFILES = {
    ("AutoPas/AutoPas", 1): ("autopas-traversal", "cfa484083af541450732a296bdd45f3fbc8fa82a42ed3428feb00af42970f40e"),
    ("AutoPas/AutoPas", 2): ("autopas-properties", "b33d500b4f54da0fa1d89bee9a19e58df367f1e4666964595e880f5d6ebce053"),
    ("LMMS/lmms", 17): ("lmms-playback", "66f617f1f75391d6c2cc87f6249f44785f1183fdf23940e1b2ba3575ba69c32b"),
    ("dosbox-staging/dosbox-staging", 48): ("dosbox-vmm", "37575c7e1d6e0b706d7851d63d50285e2f9c157f65b14e383c5058f293a37d07"),
    ("microsoft/vcpkg-tool", 91): ("vcpkg-binary-info", "e4510b470fb2a1633c3ffba71180408347af02c78652b5dc1ebc6a9514ed2c96"),
    ("microsoft/vcpkg-tool", 92): ("vcpkg-message", "41bc884d748e7f8b524521dd15f1db4e0307760b65557ddec6bdf497f6560a18"),
    ("munich-quantum-toolkit/core", 94): ("mqt-qasm", "91db445d7f95c1dc38d4a65f0992bccf5fa4f26f90e236663aaede72e516ff62"),
    ("numere-org/NumeRe", 100): ("numere-formats", "dbcdbc0ad3b92a4fc0aeb8bb42b7e4d294c4f2dcc2424a1f9bc17189cb9469dc"),
}
HARNESS_HASHES = {
    "CMakeLists.autopas1.txt": "77cda2968e9cd4a72b5339be74f02b08ba87b136c434afd69be6295fe3da088b",
    "autopas_traversal_selector.cpp": "d8a37dae0e026a442944621a95aec912000367ec213496696b8536b597a111d8",
    "autopas_particle_properties.cpp": "a2f179f85bd558b6c191c376eddc13384b5f2d7d158fa0a7f6985bfe02a34316",
    "lmms_playback_state.cpp": "81140608cb5a345f1dd9e38b0c93f863964a4f55a1017e033d638db06b050430",
    "dosbox_mouse_vmm.cpp": "b3d3935717ce046addf602ff8685bc21367643efdb74005a000adcc4409c63f9",
    "vcpkg_binary_package_info.cpp": "c494c5ee0dde8e95a1b6613758752b11ae35f4f02912842ee3d20fa5f536dc3c",
    "vcpkg_message_sink.cpp": "4f9e3fbf755d7b7aae017e968d15ffca8ef735d0b09f3621fa71f3ddf5ffc6b7",
    "numere_number_formats.cpp": "29d6f6e5e0862d4e51a18a1f9ee6130c714467c3916f684b572bab5fd7c7edf1",
    "mqt_standard_operation_qasm.cpp": "4412ac01549d696359254364606ac874b7989ac27a7293b24ecbf01d51eb35d4",
}
# Exact public release archive digests from the repository CMake declarations.
FMT9_SHA512 = "a18442042722dd48e20714ec034a12fcc0576c9af7be5188586970e2edf47529825bdc99af366b1d5891630c8dbf6f63bfa9f012e77ab3d3ed80d1a118e3b2be"
CMRC_SHA512 = "cb69ff4545065a1a89e3a966e931a58c3f07d468d88ecec8f00da9e6ce3768a41735a46fc71af56e0753926371d3ca5e7a3f2221211b4b1cf634df860c2c997f"


def normalized_plan(plan, legacy):
    fields = ("repo", "input_index", "configure_command", "build_command", "test_command",
              "real_translation_units", "docker_image", "timeout_seconds", "build_jobs")
    return {key: value.replace(str(legacy / "native"), "{root}") if isinstance(value, str) else value
            for key in fields for value in (plan.get(key),)}


def attested(ctx, task, plan):
    profile = PROFILES.get((task["repo"], plan["input_index"]))
    if profile is None:
        return None
    normalized = normalized_plan(plan, ctx.legacy)
    if canonical_hash(normalized) != profile[1]:
        raise ContractError("native API compatibility adapter requires the complete original build/configure/assertion plan")
    names = sorted(set(re.findall(r"/harnesses/([A-Za-z0-9_.-]+)", normalized["build_command"])))
    hashes = {}
    for name in names:
        source = ctx.legacy / "native/harnesses" / name
        if source.is_symlink() or not source.is_file() or file_hash(source) != HARNESS_HASHES.get(name):
            raise ContractError("native API original assertion/CMake harness changed: " + name)
        hashes[name] = file_hash(source)
    return profile[0], normalized, hashes


def _replace_once(text, before, after):
    if text.count(before) != 1:
        raise ContractError("native API literal compatibility branch is missing or ambiguous")
    return text.replace(before, after)


def _variant(command, definition):
    branch = ("variant_flag=''; if test \"$ORACLE_VARIANT\" = golden; then variant_flag="
              + definition + "; fi;")
    return [_replace_once(command, branch, "variant_flag=" + shlex.quote(value) + ";")
            for value in ("", definition)]


def _syntax(command, executable):
    # These attested plans have exactly one C++ link invocation. The driver
    # receives the SAME flags and source files, but only performs syntax checks.
    marker = " -o " + executable
    return _replace_once(command, marker, " -fsyntax-only")


def _spec(kind, timeout):
    return {"schema_version": SCHEMA, "kind": kind, "state_directory": "build-native-api-" + kind,
            "timeout_seconds": timeout, "max_log_bytes": 16 * 1024**2, "setup": [], "forms": []}


def source_dependencies(ctx, task, profile):
    """Attach explicitly prepared source assets; never import an old build."""
    from reviewbench_maintainability.agentic.assets import asset_descriptor

    required = {"autopas-properties": (), "vcpkg-message": ("fmt9", "fmt11", "cmrc"),
                "vcpkg-binary-info": ("fmt9", "cmrc"),
                "numere-formats": ("fast-float",)}.get(profile, ())
    configured = ctx.config.get("migration", {}).get("native_api_sources", {})
    assets, copies = [], []
    for name in required:
        binding = configured.get(name)
        if not isinstance(binding, dict) or not binding.get("source_pin"):
            raise ContractError("native API requires prepared exact public source: " + name)
        descriptor = asset_descriptor(binding["asset"], ctx.output)
        if (descriptor.get("source_kind") != "dependency" or not 0 < descriptor.get("files", 0) <= 20000
                or descriptor.get("archive", {}).get("input_bytes", 256 * 1024**2 + 1) > 256 * 1024**2):
            raise ContractError("native API source asset is not a complete source-only dependency")
        evidence = binding.get("receipt", {})
        evidence_path = Path(evidence.get("path", ""))
        if (not evidence_path.is_absolute() or evidence_path.is_symlink() or not evidence_path.is_file()
                or file_hash(evidence_path) != evidence.get("sha256")):
            raise ContractError("native API source asset lacks its hash-bound preparation receipt")
        receipt = json.loads(evidence_path.read_text())
        if (receipt.get("status") != "source_ready" or receipt.get("asset") != binding["asset"]
                or receipt.get("source_pin") != binding["source_pin"]
                or receipt.get("source_tree_sha256") != descriptor.get("tree_sha256")
                or binding.get("source_tree_sha256") != descriptor.get("tree_sha256")):
            raise ContractError("native API source pin is not bound to the prepared archive tree")
        pin = binding["source_pin"]
        if name == "fmt9" and pin != "sha512:" + FMT9_SHA512:
            raise ContractError("native API fmt9 archive differs from the original CMake pin")
        if name == "cmrc" and pin != "sha512:" + CMRC_SHA512:
            raise ContractError("native API CMakeRC archive differs from the original CMake pin")
        if name == "fast-float" and pin != "git:00c8c7b0d5c722d2212568d915a39ea73b08b973":
            raise ContractError("native API fast_float source differs from the retained v6.1.6 Git pin")
        if name == "fast-float" and descriptor.get("git_commit") != pin.removeprefix("git:"):
            raise ContractError("native API fast_float descriptor lacks its exact Git identity")
        if name == "fmt11" and not (
                re.fullmatch(r"sha512:[0-9a-f]{128}", pin) and receipt.get("original_headers_verified")
                and len(receipt.get("original_headers", {})) >= 5):
            raise ContractError("native API fmt11 source lacks its original header-closure attestation")
        target = DEPS + "/" + name
        assets.append({**binding["asset"], "kind": "archive", "visibility": "developer", "target": target})
        if profile in {"vcpkg-message", "vcpkg-binary-info"}:
            copies.append({"source": target, "destination": "build-native-api-deps/" + name})
    return assets, copies


def date_submodule(ctx, task):
    """Reuse the exact root/nested Git closure implementation without extending its profiles."""
    repository = ctx.repo(task)
    pins = {item["path"]: item["commit"] for item in readonly_links(repository, task["start_sha"])}
    if pins.get("externals/date") != "8ee6118e14a30be5512294b3829b09a47a63f06e":
        raise ContractError("native API original date gitlink changed")
    configured = ctx.config.get("migration", {}).get("native_submodule_repositories", {}).get(task["repo"], {})
    records, totals = closure(repository, task["start_sha"], ("externals/date",), configured)
    modules = ctx.modules(task)
    if {r["path"]: r["commit"] for r in modules} != pins:
        raise ContractError("native API source catalog differs from the superproject gitlinks")
    with tempfile.TemporaryDirectory(prefix="native-api-date-", dir=ctx.scratch) as temporary:
        destination = Path(temporary) / "source"
        for row in records:
            suffix = row["path"][len("externals/date"):].lstrip("/")
            target = destination / suffix
            if target.exists():
                target.rmdir()
            with start_archive(Path(row["repository"]), row["commit"], timeout=300) as stream:
                extract_tar(stream, target)
        asset = ctx.bundle(destination, role="submodule", git_commit=pins["externals/date"])
    atomic_json(ctx.output / "native-api-date" / (component(task["id"]) + ".json"),
                {"closure": records, "totals": totals, "asset": asset, "source_only": True})
    return [{**r} if r["path"] != "externals/date" else
            {"path": r["path"], "commit": r["commit"], "asset": asset} for r in modules]


def specifications(profile, plan, hashes, *, source_copies=()):
    """Project only known build branches; every original assertion stays opaque."""
    public, grader = (_spec(kind, plan["timeout_seconds"]) for kind in ("public", "grader"))
    grader["test_command"] = plan["test_command"].replace("{root}", ROOT)
    grader["file_bindings"] = [{"path": ROOT + "/harnesses/" + name, "sha256": sha}
                                for name, sha in hashes.items()]
    command = plan["build_command"].replace("{root}", ROOT)
    scratch = {"build-native-api-public", "build-native-api-grader", "build-native-api-deps",
               "build-native-public"}
    if profile == "autopas-traversal":
        builds = _variant(command, "-DAUTOPAS_ORACLE_METHOD_INTERACTION=ON")
        names = ("class-interaction", "method-interaction")
        executable = "oracle-selector/autopas_traversal_oracle"
        for name, build in zip(names, builds):
            setup, separator, _ = build.rpartition("; cmake --build ")
            if not separator:
                raise ContractError("native AutoPas API build lacks its original CMake target")
            setup = _replace_once(setup, "-GNinja", "-GNinja -DCMAKE_EXPORT_COMPILE_COMMANDS=ON")
            grader["forms"].append({
                "name": name, "setup": [setup],
                "probe": {"compile_database": "oracle-selector/compile_commands.json",
                          "source": ROOT + "/harnesses/autopas_traversal_selector.cpp"},
                "build": [build], "executable": executable,
            })
        flags = ["-DAUTOPAS_BUILD_TESTS=OFF", "-DAUTOPAS_BUILD_EXAMPLES=OFF",
                 "-DAUTOPAS_FORMATTING_TARGETS=OFF", "-DAUTOPAS_OPENMP=OFF",
                 "-DEigen3_ForceBundled=OFF", "-Dspdlog_ForceBundled=ON",
                 "-DAUTOPAS_STANDALONE_BUILD=OFF", "-DCMAKE_C_FLAGS=-O0 -g --coverage",
                 "-DCMAKE_CXX_FLAGS=-O0 -g --coverage", "-DCMAKE_EXE_LINKER_FLAGS=--coverage",
                 "-DCMAKE_SHARED_LINKER_FLAGS=--coverage", "-DCMAKE_MODULE_LINKER_FLAGS=--coverage"]
        configure = shlex.join(["cmake", "-S", ".", "-B", "build-native-public", "-GNinja",
                                "-DCMAKE_BUILD_TYPE=Debug", "-DCMAKE_CXX_COMPILER=g++-11", *flags])
        public["forms"] = [{"name": "production", "probe": {"command": configure +
                            " && cmake --build build-native-public --target molecularDynamicsLibrary -j2"}}]
        scratch.update(("oracle-selector", "oracle-selector-cmake"))
    elif profile == "autopas-properties":
        command = command.replace(ROOT + "/cache/spdlog-e86be93b4a193e78a78ff894b7b3d454307241bb",
                                  "build-native-api-deps/spdlog")
        for spec in (public, grader):
            spec["source_zips"] = [{"path": "libs/spdlog-1.x.zip", "sha256": SPDLOG_ZIP_SHA256,
                                    "destination": "build-native-api-deps/spdlog"}]
        for name, build in zip(("combined-site", "separate-parameters"),
                               _variant(command, "-DAUTOPAS_ORACLE_SEPARATE_SITE_PARAMETERS")):
            grader["forms"].append({"name": name, "probe": {"command": _syntax(build, "oracle-ppl/oracle")},
                                     "build": [build], "executable": "oracle-ppl/oracle"})
        flags = ("g++-11 -std=c++17 -O0 -g --coverage -IapplicationLibrary/molecularDynamics -Isrc "
                 "-Ibuild-native-api-deps/spdlog/include -DFMT_HEADER_ONLY")
        public["forms"] = [{"name": "production", "probe": {"command":
            "mkdir -p build-native-public && " + flags +
            " -x c++ -fsyntax-only applicationLibrary/molecularDynamics/molecularDynamicsLibrary/"
            "ParticlePropertiesLibrary.h && " + flags +
            " -c src/autopas/utils/ExceptionHandler.cpp -o build-native-public/ExceptionHandler.o"}}]
        scratch.add("oracle-ppl")
    elif profile == "mqt-qasm":
        # The two original API forms use exactly the same assertions. Select
        # every compilable form without inspecting a reference label or output.
        for name, build in zip(("separate-qasm", "consolidated-qasm"),
                               _variant(command, "-DMQT_ORACLE_CONSOLIDATED_QASM")):
            grader["forms"].append({
                "name": name, "probe": {"command": _syntax(build, "oracle-qasm/oracle")},
                "build": [build], "executable": "oracle-qasm/oracle",
            })
        flags = "g++-11 -std=c++17 -O0 -g --coverage -Iinclude"
        public["forms"] = [{
            "name": "production", "probe": {"command": "set -e; mkdir -p build-native-public; " +
                "; ".join(flags + " -c " + shlex.quote(source) +
                          " -o build-native-public/production-" + str(i) + ".o"
                          for i, source in enumerate(plan["real_translation_units"]))},
        }]
        scratch.add("oracle-qasm")
    elif profile == "lmms-playback":
        branch = 'extra_source=\'\'; if [ "$ORACLE_VARIANT" = golden ]; then extra_source=src/core/AudioResampler.cpp; fi;'
        for name, extra in (("inline-resampler", ""), ("separate-resampler", "src/core/AudioResampler.cpp")):
            build = _replace_once(command, branch, "extra_source=" + shlex.quote(extra) + ";")
            probe = build.replace(ROOT + "/harnesses/lmms_playback_state.cpp", LINK_PROBE)
            probe = _replace_once(probe, "-o oracle-playback/oracle", "-o oracle-playback/api-link-probe")
            # The probe is a neutral public API use linked but NEVER executed.
            # Link compatibility is necessary for the out-of-line resampler.
            public["forms"].append({"name": name, "probe": {"command": probe}})
            grader["forms"].append({"name": name, "probe": {"command": probe},
                                     "build": [build], "executable": "oracle-playback/oracle"})
        scratch.add("oracle-playback")
    elif profile in {"dosbox-vmm", "numere-formats"}:
        if profile == "dosbox-vmm":
            definition, directory, harness = "-DDOSBOX_UNIFIED_VMM_API", "build-oracle", "dosbox_mouse_vmm.cpp"
            sources = ["src/hardware/input/mouseif_virtual_machines.cpp", "src/hardware/input/mouse_common.cpp"]
            names = ("separate-vmm", "unified-vmm")
        else:
            definition, directory, harness = "-DNUMERE_ORACLE_FORMAT_VOTER", "oracle-number", "numere_number_formats.cpp"
            command = _replace_once(command, "git submodule update --init --depth 1 externals/date;",
                                    "test -d externals/date/include;")
            command = command.replace(ROOT + "/cache/fast_float-v6.1.6", DEPS + "/fast-float")
            sources, names = ["kernel/core/utils/stringtools.cpp"], ("global-votes", "voter-object")
        builds = _variant(command, definition)
        for name, build in zip(names, builds):
            prefix = build.split("; g++-11 ", 1)[0]
            probe = prefix + "; g++-11 $flags -fsyntax-only " + " ".join([
                *map(shlex.quote, sources), shlex.quote(ROOT + "/harnesses/" + harness)])
            grader["forms"].append({"name": name, "probe": {"command": probe}, "build": [build],
                                     "executable": directory + "/oracle"})
        prefix = builds[0].split("; g++-11 ", 1)[0]
        public_command = prefix + "; mkdir -p build-native-public; "
        public_command += "; ".join("g++-11 $flags -c " + shlex.quote(source) +
                                    " -o build-native-public/production-" + str(i) + ".o"
                                    for i, source in enumerate(sources))
        public["forms"] = [{"name": "production", "probe": {"command": public_command}}]
        scratch.add(directory)
    elif profile == "vcpkg-binary-info":
        # This original plan compiles the public production units and its
        # private harness in one driver invocation. Project all eleven units
        # into a separate public object build, not a private-harness prefix.
        configure = plan["configure_command"]
        configure += (" -DFETCHCONTENT_SOURCE_DIR_FMT=$PWD/build-native-api-deps/fmt9"
                      " -DFETCHCONTENT_SOURCE_DIR_CMAKERC=$PWD/build-native-api-deps/cmrc")
        for spec in (public, grader):
            spec["source_copies"] = list(source_copies)
            spec["source_guards"] = [
                {"path": "cmake/Findfmt.cmake", "required_literals": ["9.1.0.tar.gz", FMT9_SHA512]},
                {"path": "cmake/FindCMakeRC.cmake", "required_literals": ["2.0.1.tar.gz", CMRC_SHA512]},
            ]
            spec["setup"] = [configure]
        # Only the prepared dependency's filesystem location changes. Compiler,
        # flags, SUT inputs, private harness, linker options and assertion stay.
        build = command.replace("build-oracle/_deps/fmt-src", "build-native-api-deps/fmt9")
        marker = ROOT + "/harnesses/vcpkg_binary_package_info.cpp"
        if build.count(marker) != 1:
            raise ContractError("native vcpkg binary-info original harness boundary changed")
        prefix, tail = build.split(marker)
        compiler = prefix.split("; ")[-1].strip()
        sources = [*plan["real_translation_units"], "build-native-api-deps/fmt9/src/format.cc"]
        suffix = " -Wl,--gc-sections --coverage -pthread -o oracle-info/oracle"
        if (not compiler.startswith("g++-11 ") or len(plan["real_translation_units"]) != 11
                or tail != " " + " ".join(sources) + suffix):
            raise ContractError("native vcpkg binary-info original production/link inputs changed")
        public["setup"].append("mkdir -p build-native-public")
        object_builds = [compiler + " -c " + shlex.quote(source)
                         + " -o build-native-public/production-" + str(i) + ".o"
                         for i, source in enumerate(sources)]
        # There is one original interface, not an API-variant choice. Compile
        # once per phase; never repeat a syntax pass and then the same build.
        public["forms"] = [{
            "name": "production", "probe": {"command": "set -e; " + "; ".join(object_builds)},
        }]
        grader["forms"] = [{
            "name": "original-binary-info", "probe": {"command": build},
            "executable": "oracle-info/oracle",
        }]
        scratch.add("oracle-info")
    elif profile == "vcpkg-message":
        configure = plan["configure_command"].split("; if [", 1)[0]
        configure += (" -DFETCHCONTENT_SOURCE_DIR_FMT=$PWD/build-native-api-deps/fmt9"
                      " -DFETCHCONTENT_SOURCE_DIR_CMAKERC=$PWD/build-native-api-deps/cmrc")
        for spec in (public, grader):
            spec["source_copies"] = list(source_copies)
            spec["source_guards"] = [
                {"path": "cmake/Findfmt.cmake", "required_literals": ["9.1.0.tar.gz", FMT9_SHA512]},
                {"path": "cmake/FindCMakeRC.cmake", "required_literals": ["2.0.1.tar.gz", CMRC_SHA512]},
            ]
            spec["setup"] = [configure]
        branch = ('if [ "$ORACLE_VARIANT" = golden ]; then fmt_inc="$HOME/fmt-11.0.2/include"; '
                  'else fmt_inc="$PWD/build-oracle/_deps/fmt-src/include"; fi;')
        for name, dependency in (("fmt-9-1", "fmt9"), ("fmt-11-0", "fmt11")):
            build = _replace_once(command, branch,
                                  'fmt_inc="$PWD/build-native-api-deps/' + dependency + '/include";')
            grader["forms"].append({"name": name, "probe": {"command": _syntax(build, "oracle-message/oracle")},
                                     "build": [build], "executable": "oracle-message/oracle"})
            prefix = build.split(ROOT + "/harnesses/vcpkg_message_sink.cpp", 1)[0]
            sources = plan["real_translation_units"]
            public["forms"].append({
                "name": name, "probe": {"command": prefix + " -fsyntax-only " + shlex.join(sources)},
                "build": [prefix + " -c " + shlex.quote(source) +
                          " -o build-native-public/production-" + str(i) + ".o"
                          for i, source in enumerate(sources)],
            })
        public["setup"].append("mkdir -p build-native-public")
        scratch.add("oracle-message")
    else:
        raise ContractError("unsupported native API compatibility profile")
    return public, grader, sorted(scratch)


def adapt(ctx, task, plan):
    admitted = attested(ctx, task, plan)
    if admitted is None:
        return None
    profile, original, hashes = admitted
    assets, copies = source_dependencies(ctx, task, profile)
    public, grader, scratch = specifications(profile, original, hashes, source_copies=copies)
    from .native_api_entry import validate
    validate(public)
    validate(grader)
    modules = date_submodule(ctx, task) if profile == "numere-formats" else ctx.modules(task)
    directory = ctx.output / "native-api-specs" / component(task["id"])
    directory.mkdir(parents=True, exist_ok=True)
    for kind, spec in (("public", public), ("grader", grader)):
        path = directory / (kind + ".json")
        atomic_json(path, spec)
        assets.append({**ctx.copy_asset(path, "native-api-specs"), "kind": "file", "visibility":
                       "developer" if kind == "public" else "grader",
                       "target": PUBLIC if kind == "public" else PRIVATE})
    assets.append({**ctx.copy_asset(Path(__file__).with_name("native_api_entry.py"), "native-api-tools"),
                   "kind": "file", "visibility": "developer", "target": ENTRY})
    if profile == "lmms-playback":
        path = directory / "lmms-public-link.cpp"
        path.write_text(LMMS_LINK_PROBE)
        assets.append({**ctx.copy_asset(path, "native-api-tools"), "kind": "file",
                       "visibility": "developer", "target": LINK_PROBE})
    def entry(spec, phase):
        return shlex.join(["python3", ENTRY, "--spec", spec, "--phase", phase])
    prepare, test = entry(PRIVATE, "prepare"), entry(PRIVATE, "run")
    atomic_json(ctx.output / "native-api-admission" / (component(task["id"]) + ".json"), {
        "id": task["id"], "profile": profile, "original_plan_sha256": canonical_hash(original),
        "original_harness_sha256": hashes, "original_test_command": original["test_command"],
        "selection": "current candidate compile-only; every compatible form; conjunctive original assertions",
        "public_spec_sha256": canonical_hash(public), "grader_spec_sha256": canonical_hash(grader),
        "source_changes": [], "test_changes": [], "golden_changes": [],
    })
    return {"plan": {**plan, "configure_command": None, "build_command": prepare},
            "configure": None, "build": entry(PUBLIC, "prepare"), "grading_bootstrap": [prepare],
            "test_command": test, "scratch_paths": scratch, "submodules": modules, "assets": assets}
