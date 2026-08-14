#!/usr/bin/env python3
"""Recovery tooling for the 11 ``published_legacy`` native/rust tasks.

The 20260728 reserve adapter (``run_legacy_reserve_correctness.py``) can only
replay a legacy task when the historical harness assets still exist under the
retained session root. For the native and rust lanes those assets were partly
lost: every ``.../tasks/<case>/start`` workspace subtree was deleted, the
recorded ``oracle_recovery/rust/cache/cargo`` CARGO_HOME never survived, and
four of the eleven tasks (apache/tsfile, grpc/grpc, python/cpython,
otter-sec/anchor) never had a retained harness on this machine at all.

This module owns the *recovered* side of that contract:

* it re-materializes each exact-start workspace from retained full repo
  objects (``git clone --shared`` + ``checkout --detach <start_sha>``) plus the
  small retained per-case harness files, under an assets root that is separate
  from (and never writes into) the retained session root;
* it records every recovered replay contract in a single machine-readable
  ``recovery-manifest.json`` with a ``SHA256SUMS`` sidecar over every small
  authored/retained file (harness sources, golden patches, recipes, proof
  logs) plus git provenance for the multi-gigabyte workspaces that cannot be
  hashed file-by-file;
* it re-runs each recovered contract end to end (``smoke``) against the
  pristine start tree, and, where a source-only golden patch was recovered,
  against the golden tree too, so that "ready" always means *proven here*,
  never *assumed*.

Anything that is not proven is recorded as a blocker and stays out of the
manifest's ready set: ``run_legacy_reserve_correctness.py`` fails closed on it.

Usage
-----
    # Verify the on-disk assets and (re)write manifest + SHA256SUMS
    python3 scripts/recover_legacy_native_rust.py manifest \
      --assets-root /data/.../legacy-recovery-assets/native-rust \
      --legacy-root /data/.../reviewbench-session-<id>

    # Re-prove one (or every) recovered contract on the pristine start tree
    python3 scripts/recover_legacy_native_rust.py smoke \
      --assets-root ... --legacy-root ... --case 19-memilio-history
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

MANIFEST_NAME = "recovery-manifest.json"
CHECKSUM_NAME = "SHA256SUMS"
MANIFEST_SCHEMA = "reviewbench-legacy-native-rust-recovery/v1"

# Retained session-root-relative location of the historical native recovery.
NATIVE_REAL = "strict_screen_20260726/oracle_recovery/native-real/tasks"


def _native(**kwargs: Any) -> dict[str, Any]:
    entry = {
        "lane": "native",
        "shell": ["sh", "-lc"],
        "network": None,
        "user": None,
        "env": {},
        "harness_copies": [],
        "harness_mounts": [],
        "golden_patch": None,
        # Some historical recoveries proved the golden tree with a *different*
        # build recipe than the start tree (the golden's own source-only
        # changes restructure the build). ``golden_command`` records that
        # recorded recipe; it is used only by the golden proof, never by a
        # candidate replay, which always runs ``command``.
        "golden_command": None,
        "expected_stdout_sha256": None,
    }
    entry.update(kwargs)
    return entry


def _rust(**kwargs: Any) -> dict[str, Any]:
    entry = {
        "lane": "rust",
        # The historical rust runner used ``/bin/sh -c``. A *login* shell
        # (``sh -lc``) re-sources /etc/profile and drops the image's
        # CARGO_HOME/bin from PATH, which makes ``cargo`` unresolvable; this
        # is why the rust lane must not reuse the native lane's shell.
        "shell": ["/bin/sh", "-c"],
        "network": "host",
        "user": None,
        "env": {
            "CARGO_TERM_COLOR": "never",
            "RUST_BACKTRACE": "1",
            "CARGO_BUILD_JOBS": "32",
            "RUSTFLAGS": "--cap-lints=allow",
        },
        "harness_copies": [],
        "harness_mounts": [],
        "golden_patch": None,
        "golden_command": None,
        "expected_stdout_sha256": None,
    }
    entry.update(kwargs)
    return entry


# ---------------------------------------------------------------------------
# Recovered replay contracts
# ---------------------------------------------------------------------------
# ``workspace``      assets-root-relative directory that plays the role of the
#                    historical ``task_dir``; it holds ``start/`` plus any
#                    retained harness files.
# ``patch_subdir``   subdirectory of ``workspace`` that receives the candidate
#                    patch (always the exact-start checkout).
# ``mount``          {host_subpath: container_path} bind mounts of the copied
#                    workspace; ``.`` means the workspace directory itself.
# ``workdir``        container cwd.
# ``command``        shell command replayed verbatim after the patch applies.
# ``provenance``     how the exact-start tree is (re)materialized.
RECIPES: dict[str, dict[str, Any]] = {
    "github__ClickHouse__ClickHouse__pull_74193__comment_2024566878__duplication": _native(
        case="05-clickhouse",
        repo="ClickHouse/ClickHouse",
        start_sha="9ba917834bc6042bd9d46903b054e62a110713f9",
        workspace="workspaces/05-clickhouse",
        golden_patch="workspaces/05-clickhouse/source-only.patch",
        patch_subdir="start",
        docker_image="oracle-clickhouse:real",
        mount={"start": "/src", "query.sql": "/work/query.sql"},
        readonly_mounts=["query.sql"],
        workdir="/src",
        command=(
            'ninja -C build-real programs/clickhouse && '
            'build-real/programs/clickhouse-local --query "$(cat /work/query.sql)"'
        ),
        provenance={
            "kind": "git-clone-shared",
            "source": f"{NATIVE_REAL}/05-clickhouse/repo",
            "submodules": "git submodule update --init --recursive",
            "prebuild": (
                "cmake -S /src -B /src/build-real -G Ninja -DCMAKE_BUILD_TYPE=Debug "
                "-DWITH_COVERAGE=1 -DCMAKE_C_COMPILER=clang-19 "
                "-DCMAKE_CXX_COMPILER=clang++-19 && "
                "git config --global --add safe.directory /src && "
                "ninja -C build-real programs/clickhouse"
            ),
            "retained_files": ["query.sql", "checkpoint.json", "source-only.patch"],
        },
    ),
    "github__Desbordante__desbordante-core__pull_154__comment_1371183985__modularity": _native(
        case="11-desbordante",
        repo="Desbordante/desbordante-core",
        start_sha="e963ad8cdf492859a4632e729a4bcfddefe959a6",
        workspace="workspaces/11-desbordante",
        golden_patch="workspaces/11-desbordante/source-only.patch",
        patch_subdir="start",
        docker_image="reviewbench-native-real-desbordante",
        mount={".": "/work"},
        workdir="/work/start",
        # ``-Ilib/easyloggingpp/src -Ilib/better-enums`` are the exact include
        # directories the project's own CMakeLists adds
        # (``include_directories(SYSTEM "lib/easyloggingpp/src"
        # "lib/better-enums/")``); the historical CASE_CONFIGS string omitted
        # them and therefore never compiled. Everything else is verbatim.
        command=(
            "cmake --build build-real --target Desbordante_lib -j8 && "
            "g++ -std=c++17 --coverage -O0 -g /work/oracle.cpp -Isrc/core -Isrc "
            "-Ibuild-real/src/core -Ilib/easyloggingpp/src -Ilib/better-enums "
            "build-real/src/core/libDesbordante_lib.a "
            "build-real/lib/easyloggingpp/libeasyloggingpp.a -lboost_container "
            "-lboost_program_options -lboost_thread -lboost_graph -pthread "
            "-o oracle-real && ./oracle-real /work/start"
        ),
        # Byte-identical stdout to the historical proven start run
        # (11-desbordante/start-runs.log).
        expected_stdout_sha256=(
            "50d9c3cd931938b97b3fa05670b63cd2131873fc572286fa7f5443d9b058ca59"
        ),
        # The golden's own source-only CMakeLists change removes the
        # ``Desbordante_lib`` target, so the historical recovery proved the
        # golden tree by building the individual production objects and
        # archiving them (build-commands.txt: "GOLDEN PRODUCTION"/"GOLDEN
        # ORACLE LINK"). That recorded recipe is reproduced verbatim here and
        # is used only for the golden proof.
        golden_command=(
            # The golden's source-only CMakeLists change removes the
            # ``Desbordante_lib`` target, and its algorithms.h references
            # unrelated-branch headers that do not exist at this commit, so
            # the whole library cannot link. The historical recovery
            # (build-commands.txt "GOLDEN PRODUCTION"/"GOLDEN ORACLE LINK")
            # therefore built the real CMake production objects the oracle
            # needs, archived them, and linked against that archive with
            # -std=c++20. Reproduced verbatim here, plus balancer.cpp.o which
            # the golden's GfdValidation now calls. Used only by the golden
            # proof; candidate replays always run ``command``.
            "cmake -S /work/start -B /work/start/build-real -G Ninja "
            "-DCOMPILE_TESTS=OFF -DUNPACK_DATASETS=OFF -DASAN=OFF "
            "-DCMAKE_BUILD_TYPE=Debug -DCMAKE_CXX_FLAGS=--coverage "
            "-DCMAKE_EXE_LINKER_FLAGS=--coverage && "
            "OBJS=\"src/core/CMakeFiles/Desbordante.dir/algorithms/gfd/gfd_validation.cpp.o "
            "src/core/CMakeFiles/Desbordante.dir/algorithms/gfd/gfd_handler.cpp.o "
            "src/core/CMakeFiles/Desbordante.dir/algorithms/gfd/balancer.cpp.o "
            "src/core/CMakeFiles/Desbordante.dir/parser/graph_parser/graph_parser.cpp.o "
            "src/core/CMakeFiles/Desbordante.dir/algorithms/algorithm.cpp.o "
            "src/core/CMakeFiles/Desbordante.dir/util/progress.cpp.o "
            "src/core/CMakeFiles/Desbordante.dir/config/thread_number/option.cpp.o\" && "
            "ninja -C build-real $OBJS && "
            "rm -f libDesbordante-minimal.a && "
            "ar rcs libDesbordante-minimal.a "
            "$(for o in $OBJS; do echo build-real/$o; done) && "
            "g++ -std=c++20 --coverage -O0 -g /work/oracle.cpp -Isrc/core -Isrc "
            "-Ibuild-real/src/core -Ilib/easyloggingpp/src -Ilib/better-enums "
            "libDesbordante-minimal.a build-real/lib/easyloggingpp/libeasyloggingpp.a "
            "-lboost_container -lboost_program_options -lboost_thread "
            "-lboost_graph -lboost_regex -pthread -o oracle-real && "
            "./oracle-real /work/start"
        ),
        provenance={
            "kind": "git-clone-shared",
            "source": "repos/Desbordante__desbordante-core",
            "vendored": (
                "cp -a /opt/deps/easyloggingpp start/lib/easyloggingpp; "
                "cp -a /opt/deps/better-enums start/lib/better-enums "
                "(exact tags baked into Dockerfile.desbordante)"
            ),
            "prebuild": (
                "cmake -S /work/start -B /work/start/build-real -G Ninja "
                "-DCOMPILE_TESTS=OFF -DUNPACK_DATASETS=OFF -DASAN=OFF "
                "-DCMAKE_BUILD_TYPE=Debug -DCMAKE_CXX_FLAGS=--coverage "
                "-DCMAKE_EXE_LINKER_FLAGS=--coverage && "
                "cmake --build /work/start/build-real --target Desbordante_lib"
            ),
            "retained_files": [
                "oracle.cpp", "checkpoint.json", "build-commands.txt",
                "source-only.patch",
            ],
        },
    ),
    "github__IntelPython__dpnp__pull_2201__comment_1886256998__modularity": _native(
        case="14-dpnp",
        repo="IntelPython/dpnp",
        start_sha="efa7e1c208f33287edc54ba870214d093a43d643",
        workspace="workspaces/14-dpnp",
        golden_patch="workspaces/14-dpnp/source-only.patch",
        patch_subdir="start",
        docker_image="oracle-dpnp:real",
        mount={"start": "/src", "behavior_test.py": "/oracle/behavior_test.py"},
        readonly_mounts=["behavior_test.py"],
        workdir="/src",
        command=(
            'CC=icx CXX=icpx python3 setup.py build_ext --inplace -- '
            '-DCMAKE_BUILD_TYPE=Debug "-DCMAKE_CXX_FLAGS=-O0 -g --coverage" '
            '-DCMAKE_SHARED_LINKER_FLAGS=--coverage && '
            'PYTHONPATH=/src python3 /oracle/behavior_test.py'
        ),
        provenance={
            "kind": "git-clone-shared",
            "source": f"{NATIVE_REAL}/13-14-dpnp/repo",
            "start_patches": ["dependency-compatibility.patch"],
            "start_patch_note": (
                "the retained dpctl-0.19 API compatibility patch is applied "
                "identically to start and golden by the historical recovery; "
                "it is part of the exact-start workspace, not a candidate edit"
            ),
            "retained_files": [
                "behavior_test.py", "checkpoint.json", "build-commands.txt",
                "dependency-compatibility.patch", "source-only.patch",
            ],
        },
    ),
    "github__SciCompMod__memilio__pull_690__comment_1394151706__complexity": _native(
        case="19-memilio-history",
        repo="SciCompMod/memilio",
        start_sha="ce15427d0294e5cd2c862704515df707323d25f6",
        workspace="workspaces/19-memilio-history",
        golden_patch="workspaces/19-memilio-history/source-only-golden.patch",
        patch_subdir="start",
        docker_image="reviewbench-native-real-eventuals",
        mount={".": "/work"},
        workdir="/work/start",
        command=(
            "g++-11 -std=c++17 -O0 -g -Icpp /work/oracle.cpp -o "
            "correctness-oracle && ./correctness-oracle"
        ),
        provenance={
            "kind": "git-clone-shared",
            "source": "repos/SciCompMod__memilio",
            "retained_files": [
                "oracle.cpp", "checkpoint.json", "source-only-golden.patch",
            ],
        },
    ),
    "github__XRPLF__rippled__pull_5143__comment_1786919023__duplication": _native(
        case="22-rippled",
        repo="XRPLF/rippled",
        start_sha="ea887bfc93b22f6a1225f2843a11131e04f0dfd2",
        workspace="workspaces/22-rippled",
        golden_patch="workspaces/22-rippled/source-only.patch",
        patch_subdir="start",
        docker_image="oracle-rippled:real",
        mount={"start": "/src"},
        cache_mounts={"caches/conan": "/root/.conan"},
        workdir="/src",
        command=(
            # Differences from the recorded historical string, each forced by
            # replaying against a *freshly materialized* workspace instead of
            # the deleted pre-configured one:
            #  * ``-o xrpld=True -o tests=True``: the in-repo conan recipe
            #    defaults both to False, which removes the ``rippled``
            #    executable target (and its --unittest support) entirely.
            #  * ``-DCMAKE_BUILD_TYPE=Debug``: conan generates Debug-config
            #    dependency data; without a matching build type the
            #    OpenSSL::SSL interface include dirs are dropped and the
            #    bundled ed25519-donna target fails on <openssl/rand.h>.
            #  * the historical ``sed``/``touch`` protoc bypass is gone: it
            #    only worked because generated *.pb.cc/*.pb.h already existed
            #    in the prebuilt tree; here protoc runs for real.
            "conan install . --output-folder=. --build=missing "
            "-o xrpld=True -o tests=True -s build_type=Debug "
            "2>/dev/null 1>/dev/null && "
            "cmake -S . -B build -G Ninja "
            "-DCMAKE_TOOLCHAIN_FILE=generators/conan_toolchain.cmake "
            "-DCMAKE_BUILD_TYPE=Debug "
            '"-DCMAKE_CXX_FLAGS=-O0 -g --coverage" '
            "-DCMAKE_EXE_LINKER_FLAGS=--coverage 2>/dev/null 1>/dev/null && "
            "ninja -C build -j16 rippled && "
            "./build/rippled --unittest=STObject"
        ),
        provenance={
            "kind": "git-clone-shared",
            "source": f"{NATIVE_REAL}/22-rippled/repo",
            "cache": (
                "caches/conan is a reconstructed persistent Conan 1.60 cache "
                "(the recovery image ships an empty /root/.conan); it is "
                "produced by the recipe's own `conan install --build=missing` "
                "and mounted read-write so replays do not re-download or "
                "re-build the historical package graph"
            ),
            "retained_files": [
                "checkpoint.json", "build-commands.txt", "source-only.patch",
            ],
        },
    ),
    "github__lanl__singularity-eos__pull_75__comment_792881520__duplication": _native(
        case="28-singularity-eos",
        repo="lanl/singularity-eos",
        start_sha="af8d8b4e9fad603f4f6aae99945823cfbc5c7f9a",
        workspace="workspaces/28-singularity-eos",
        golden_patch="workspaces/28-singularity-eos/source-only-golden.patch",
        patch_subdir="start",
        docker_image="reviewbench-native-real-eventuals",
        mount={".": "/work"},
        workdir="/work/start",
        # ``-include typeinfo`` is added to the *shared* command (start and
        # golden alike): the reference source-only golden introduces a
        # ``typeid(CRTP)`` helper in eos_base.hpp without including
        # <typeinfo> (upstream's target commit does not include it either),
        # so the historical environment must have provided it transitively.
        # Adding it to only the golden proof would unfairly fail any candidate
        # that mirrors the reference fix, which is exactly the kind of
        # infrastructure artefact that must never be scored as a model error.
        command=(
            "g++-11 -std=c++17 -O0 -g -include typeinfo -I. -Iutils "
            "/work/oracle.cpp -o correctness-oracle && ./correctness-oracle"
        ),
        provenance={
            "kind": "git-clone-shared",
            "source": "repos/lanl__singularity-eos",
            "submodule_sources": {
                "start/utils/spiner": f"{NATIVE_REAL}/28-singularity-eos/spiner",
                "start/utils/variant": f"{NATIVE_REAL}/28-singularity-eos/variant",
            },
            "submodule_note": (
                "utils/ports-of-call is an in-repo symlink to "
                "utils/spiner/ports-of-call, so restoring the retained spiner "
                "checkout also restores it; the historical gcov artifacts "
                "confirm /work/start/utils/variant/include/mpark/variant.hpp"
            ),
            "retained_files": [
                "oracle.cpp", "checkpoint.json", "source-only-golden.patch",
            ],
        },
    ),
    "github__apache__tsfile__pull_823__comment_3418007322__modularity": _native(
        case="tsfile-823",
        repo="apache/tsfile",
        start_sha="60a91f3ad508da57802febea42e3aa6a105fc1e0",
        workspace="workspaces/tsfile-823",
        patch_subdir="start",
        docker_image="reviewbench-increment5-tsfile",
        mount={"start": "/work"},
        harness_mounts=[{"host": "harness/tsfile-823", "container": "/harness"}],
        workdir="/work",
        command=(
            "cp /harness/oracle_aligned_batch_test.cc "
            "cpp/test/reader/oracle_aligned_batch_test.cc && "
            # A retained cpp/build-oracle cache makes the bundled
            # third_party/google_snappy sub-project's project() call fail
            # ("CMAKE_C_COMPILER ... is not a full path"), so the build dir is
            # always reconfigured from scratch: deterministic, ~3 min.
            "rm -rf cpp/build-oracle && "
            "cmake -S cpp -B cpp/build-oracle -GNinja -DBUILD_TEST=ON "
            "-DCOV_ENABLED=ON -DENABLE_ANTLR4=OFF "
            "-DCMAKE_C_COMPILER=/usr/bin/gcc-13 "
            "-DCMAKE_CXX_COMPILER=/usr/bin/g++-13 && "
            "cmake --build cpp/build-oracle --target TsFile_Test && "
            "cd cpp/build-oracle/test/lib && ./TsFile_Test "
            "--gtest_filter=OracleAlignedBatch.DoubleValuesPreserveRowsAndValues"
        ),
        harness_files=["harness/tsfile-823/oracle_aligned_batch_test.cc"],
        golden_patch="harness/tsfile-823/source-only-golden.patch",
        provenance={
            "kind": "git-clone-shared",
            "source": "repos/apache__tsfile",
            "harness": (
                "re-authored public-API gtest (the recorded harness file "
                "cpp/test/reader/oracle_aligned_batch_test.cc was never "
                "retained on this machine); auto-registered by the project's "
                "own file(GLOB_RECURSE ... reader/*_test.cc) and copied in at "
                "replay time so the workspace stays pristine"
            ),
            "golden_patch": (
                "git diff <start_sha> <target_sha> -- "
                "cpp/src/reader/aligned_chunk_reader.cc "
                "cpp/src/reader/aligned_chunk_reader.h "
                "(exactly the Oracle's golden_scope.paths)"
            ),
        },
    ),
    "github__python__cpython__pull_143314__comment_2656278401__complexity": _native(
        case="cpython-143314",
        repo="python/cpython",
        start_sha="243b880d12a867dc64551254931ca92181233da4",
        workspace="workspaces/cpython-143314",
        patch_subdir="start",
        docker_image="reviewbench-increment5-cpython:latest",
        mount={"start": "/src"},
        workdir="/src",
        shell=["bash", "-c"],
        user="current",
        env={"HOME": "/src"},
        command=(
            "make -j16 && ./python -m unittest -v "
            "test.test_os.test_os.ExecTests.test_execve_invalid_env "
            "test.test_os.test_os.SpawnTests.test_spawnve_bytes"
        ),
        golden_patch="harness/cpython-143314/source-only-golden.patch",
        provenance={
            "kind": "git-clone-shared",
            "source": "repos/python__cpython",
            "prebuild": "./configure --with-ensurepip=no && make -j16",
            "image_recipe": "recipes/Dockerfile.cpython-143314",
            "harness": (
                "the Oracle records only pass_to_pass=2 and the covered line "
                "Modules/posixmodule.c:7383; the two selected tests are the "
                "project's own unchanged tests (byte-identical between start "
                "and target) that drive parse_envlist() through os.execve / "
                "os.spawnve, verified with a throwaway printf probe"
            ),
            "golden_patch": (
                "git diff <start_sha> <target_sha> -- Modules/posixmodule.c "
                "(source-only; no test changes)"
            ),
        },
        pass_to_pass_tests=[
            "test.test_os.test_os.ExecTests.test_execve_invalid_env",
            "test.test_os.test_os.SpawnTests.test_spawnve_bytes",
        ],
    ),
    "github__FuelLabs__fuel-tx__pull_197__comment_1003742605__complexity": _rust(
        case="fuel-tx-197",
        repo="FuelLabs/fuel-tx",
        start_sha="30d88423ac0e00f2d0a54c314b4dc59ef38238c1",
        workspace="workspaces/fuel-tx-197",
        golden_patch="harness/fuel-tx-197/source-only-golden.patch",
        patch_subdir="start",
        docker_image="reviewbench-strict-rust:20260726",
        # The workspace's test-helpers manifest resolves the crate under test
        # as ``fuel-tx = { path = "../../fuel-tx" }``, so the checkout must be
        # mounted under a directory literally named ``fuel-tx``.
        mount={"start": "/work/fuel-tx"},
        cargo_cache="caches/cargo",
        workdir="/work/fuel-tx",
        command="cargo +1.93.0 test -p fuel-tx --all-features -- --nocapture",
        provenance={
            "kind": "git-clone-shared",
            "source": "repos/FuelLabs__fuel-tx",
            "cache": (
                "caches/cargo is the reconstructed CARGO_HOME the Oracle "
                "records as oracle_recovery/rust/cache/cargo (never retained): "
                "seeded from the pinned image's own $CARGO_HOME (toolchain "
                "1.93.0 + rustup shims) and warmed by one networked run; "
                "proven complete by a --network=none rerun after deleting "
                "target/"
            ),
        },
    ),
    "github__otter-sec__anchor__pull_2488__comment_1193854703__duplication": _rust(
        case="anchor-2488",
        repo="otter-sec/anchor",
        start_sha="4f32cb5cae922f087870a28fc1683fafd8594bc3",
        workspace="workspaces/anchor-2488",
        patch_subdir="start",
        docker_image="reviewbench-increment5-rust:latest",
        mount={"start": "/work/anchor"},
        cargo_cache="caches/cargo-anchor",
        harness_copies=[
            {
                "host": "harness/anchor-2488/oracle_nonblocking.rs",
                "workspace_path": "client/tests/oracle_nonblocking.rs",
            }
        ],
        harness_files=["harness/anchor-2488/oracle_nonblocking.rs"],
        golden_patch="harness/anchor-2488/source-only-golden.patch",
        workdir="/work/anchor",
        command=(
            "cargo +1.70.0 test -p anchor-client --features async "
            "--test oracle_nonblocking -- --nocapture"
        ),
        provenance={
            "kind": "git-clone-shared",
            "source": "repos/otter-sec__anchor",
            "harness": (
                "re-authored client/tests/oracle_nonblocking.rs (the recorded "
                "increment5 round2_test.rs was never retained): loopback TCP "
                "JSON-RPC server answering getVersion/getAccountInfo driven "
                "through the public async client API, with a compile-time "
                "IntoProgram trait normalizing the start (Program) vs golden "
                "(Result<Program>) public return type -- no source inspection "
                "and no candidate-symbol branching"
            ),
            "golden_patch": (
                "git diff <start_sha> <target_sha> -- client/src/blocking.rs "
                "client/src/lib.rs client/src/nonblocking.rs (the Oracle's "
                "scope_expansion.paths)"
            ),
        },
        pass_to_pass_tests=[
            "async_account_fetches_and_deserializes_real_rpc_payload",
            "async_account_reports_missing_account",
        ],
    ),
}

# Tasks in scope that are deliberately *not* recovered. They are listed so the
# manifest is exhaustive over the 11 in-scope task IDs and so the adapter can
# surface an exact, non-fabricated blocker instead of silently skipping them.
UNRECOVERED: dict[str, dict[str, Any]] = {
    "github__grpc__grpc__pull_32970__comment_1195793486__duplication": {
        "lane": "native",
        "repo": "grpc/grpc",
        "start_sha": "803e4da2c7133712e9645738c7805b7fee8ced7d",
        "blockers": [
            "no retained replay contract: the release Oracle carries only "
            "native_oracle_result/pass_to_pass/coverage (source_artifact "
            "legacy159_oracle_native_normalized.jsonl) with no command, image, "
            "or harness, and no legacy159 recovery tree exists under the "
            "retained session root",
            "retained repo checkout repos/grpc__grpc does not contain the task "
            "start_sha 803e4da2c7133712e9645738c7805b7fee8ced7d as a commit, so "
            "the exact-start workspace cannot be materialized offline",
            "the recorded pass_to_pass set (6 tests, with "
            "XdsAuditLoggerRegistryTest.NoSupportedType explicitly excluded as "
            "solution-coupled) is a Bazel test target; no pinned Bazel "
            "toolchain/output-base cache for the 2023 gRPC tree was retained",
        ],
    },
}

ALL_TASK_IDS = tuple(sorted(set(RECIPES) | set(UNRECOVERED)))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    return subprocess.run(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, **kwargs
    )


def git_head(path: Path) -> str | None:
    result = run(["git", "-C", str(path), "rev-parse", "HEAD"])
    return result.stdout.strip() if result.returncode == 0 else None


def docker_image_id(image: str) -> str | None:
    if shutil.which("docker") is None:
        return None
    result = run(["docker", "image", "inspect", "--format", "{{.Id}}", image])
    return result.stdout.strip() if result.returncode == 0 else None


def workspace_state(start: Path) -> dict[str, Any]:
    """Exact, replay-relevant state of a recovered exact-start workspace.

    A recovered workspace is only trustworthy if it is *still* the tree the
    contract was proven against, so this captures everything that can drift
    underneath a replay:

    * ``head`` -- the checked-out commit;
    * ``tracked`` -- every modified/deleted tracked path (dpnp legitimately
      carries the retained dpctl-0.19 compatibility patch; anything else is
      drift);
    * ``untracked`` -- untracked entries, with directories collapsed by git
      (``--untracked-files=normal``), which is what makes this cheap even for
      a 52 GB tree with a full build directory in it;
    * ``submodules`` -- ``git submodule status`` lines, so an initialized
      submodule that moves or goes dirty (ClickHouse ships 123 of them) is
      caught instead of silently changing what gets compiled.

    The manifest records this verbatim, and both preflight and replay compare
    the live value against it: any difference is a blocker, never a candidate
    failure.
    """
    if not start.is_dir():
        return {"exists": False}
    tracked = run(
        ["git", "-C", str(start), "status", "--porcelain", "--untracked-files=no"]
    )
    untracked = run(
        [
            "git", "-C", str(start), "status", "--porcelain",
            "--untracked-files=normal",
        ]
    )
    submodules = run(["git", "-C", str(start), "submodule", "status"])
    return {
        "exists": True,
        "head": git_head(start),
        "tracked": sorted(
            line[3:] for line in tracked.stdout.splitlines() if line.strip()
        ),
        "untracked": sorted(
            line[3:] for line in untracked.stdout.splitlines()
            if line.startswith("?? ")
        ),
        "submodules": sorted(
            line.strip() for line in submodules.stdout.splitlines() if line.strip()
        ),
    }


# Backwards-compatible alias: the manifest field is now ``workspace_state``.
workspace_provenance = workspace_state


_TREE_DIGEST_CACHE: dict[str, dict[str, Any]] = {}


def tree_digest(path: Path, *, use_cache: bool = True) -> dict[str, Any] | None:
    """Complete, order-independent structural digest of a mounted directory.

    Every file under ``path`` contributes its repository-relative path, size,
    mode and symlink target -- with **no sampling and no truncation**, unlike
    the adapter's historical bounded ``directory_identity``. That is what lets
    a reconstructed cache (a 7.8 GB Conan tree, a CARGO_HOME registry) be
    bound to the manifest as a whole: adding, removing, truncating, growing or
    re-permissioning any file inside it changes the digest, so a tampered or
    partially-restored cache blocks instead of silently changing what a replay
    compiles against.

    File *contents* are deliberately not read: the caches are multi-gigabyte
    and are mounted read-write by design (cargo/conan write lock files), so a
    full content hash would be both prohibitively slow and unstable. Small
    authored inputs (harness sources, golden patches, recipes) are covered by
    real SHA-256 content hashes in ``SHA256SUMS`` instead.
    """
    key = str(path)
    if use_cache and key in _TREE_DIGEST_CACHE:
        return _TREE_DIGEST_CACHE[key]
    if not path.is_dir():
        return None
    entries: list[tuple[str, int, int, str]] = []
    for root, dirnames, filenames in os.walk(path):
        dirnames.sort()
        for name in sorted(filenames):
            item = Path(root) / name
            relative = str(item.relative_to(path))
            try:
                stat = item.lstat()
            except OSError:
                entries.append((relative, -1, -1, "unreadable"))
                continue
            target = ""
            if os.path.islink(item):
                try:
                    target = os.readlink(item)
                except OSError:
                    target = "unreadable"
            entries.append((relative, stat.st_size, stat.st_mode, target))
    digest = hashlib.sha256(
        json.dumps(sorted(entries), sort_keys=True).encode("utf-8")
    ).hexdigest()
    value = {"file_count": len(entries), "digest": digest}
    if use_cache:
        _TREE_DIGEST_CACHE[key] = value
    return value


def mounted_cache_digests(
    recipe: dict[str, Any], assets_root: Path
) -> dict[str, Any]:
    """``tree_digest`` for every cache directory a replay actually mounts.

    Covers both ``cache_mounts`` (rippled's reconstructed Conan cache) and
    ``cargo_cache`` (the rebuilt CARGO_HOMEs), so no mounted build input is
    left unbound.
    """
    digests: dict[str, Any] = {}
    for host in sorted(recipe.get("cache_mounts", {})):
        digests[host] = tree_digest(assets_root / host)
    if recipe.get("cargo_cache"):
        digests[recipe["cargo_cache"]] = tree_digest(
            assets_root / recipe["cargo_cache"]
        )
    return digests


def contract_blockers(
    task_id: str,
    recipe: dict[str, Any],
    assets_root: Path,
    *,
    expected_state: dict[str, Any] | None = None,
) -> list[str]:
    """Fail-closed asset check for one recovered contract.

    When ``expected_state`` (the manifest's recorded ``workspace_state``) is
    supplied, the live workspace must match it exactly -- same HEAD, same
    modified tracked paths, same untracked entries, same submodule states --
    so a workspace that has drifted (a stray edit, a half-deleted build tree,
    a submodule moved or gone dirty) blocks instead of quietly changing what a
    replay compiles.
    """
    blockers: list[str] = []
    workspace = assets_root / recipe["workspace"]
    start = workspace / recipe["patch_subdir"]
    if not workspace.is_dir():
        blockers.append(f"missing recovered workspace: {workspace}")
    if not start.is_dir():
        blockers.append(f"missing exact-start subtree: {start}")
    else:
        head = git_head(start)
        if head != recipe["start_sha"]:
            blockers.append(
                f"exact-start subtree {start} is at {head}, not the task "
                f"start_sha {recipe['start_sha']}"
            )
        if expected_state is not None:
            live = workspace_state(start)
            for field in ("tracked", "untracked", "submodules"):
                if live.get(field) != expected_state.get(field):
                    added = sorted(
                        set(live.get(field) or []) - set(expected_state.get(field) or [])
                    )[:5]
                    removed = sorted(
                        set(expected_state.get(field) or []) - set(live.get(field) or [])
                    )[:5]
                    blockers.append(
                        f"recovered workspace {start} has drifted: {field} "
                        f"unexpected={added} missing={removed}"
                    )
    for relative in recipe.get("harness_files", []):
        if not (assets_root / relative).is_file():
            blockers.append(f"missing recovered harness file: {assets_root / relative}")
    if recipe.get("golden_patch") and not (assets_root / recipe["golden_patch"]).is_file():
        blockers.append(
            f"missing recovered golden patch: {assets_root / recipe['golden_patch']}"
        )
    for host in recipe.get("mount", {}):
        if host == ".":
            continue
        if not (workspace / host).exists():
            blockers.append(f"missing mount source: {workspace / host}")
    for host in recipe.get("cache_mounts", {}):
        if not (assets_root / host).is_dir():
            blockers.append(f"missing mounted cache: {assets_root / host}")
    for mount in recipe.get("harness_mounts", []):
        if not (assets_root / mount["host"]).exists():
            blockers.append(
                f"missing mounted harness directory: {assets_root / mount['host']}"
            )
    for copy in recipe.get("harness_copies", []):
        if not (assets_root / copy["host"]).is_file():
            blockers.append(f"missing harness copy source: {assets_root / copy['host']}")
    if recipe.get("cargo_cache"):
        cache = assets_root / recipe["cargo_cache"]
        if not (cache / "bin" / "cargo").exists():
            blockers.append(f"missing reconstructed CARGO_HOME: {cache}")
    image_id = docker_image_id(recipe["docker_image"])
    if image_id is None:
        blockers.append(f"missing Docker image: {recipe['docker_image']}")
    return blockers


def docker_argv(
    task_id: str,
    recipe: dict[str, Any],
    assets_root: Path,
    workspace: Path,
    command: str | None = None,
) -> list[str]:
    """Exact ``docker run`` argv for one recovered contract."""
    argv = ["docker", "run", "--rm"]
    if recipe.get("network"):
        argv += [f"--network={recipe['network']}"]
    if recipe.get("user") == "current":
        argv += ["--user", f"{os.getuid()}:{os.getgid()}"]
    for host, container in recipe.get("mount", {}).items():
        source = workspace if host == "." else workspace / host
        suffix = ":ro" if host in recipe.get("readonly_mounts", []) else ""
        argv += ["-v", f"{source}:{container}{suffix}"]
    for host, container in recipe.get("cache_mounts", {}).items():
        argv += ["-v", f"{assets_root / host}:{container}"]
    for mount in recipe.get("harness_mounts", []):
        argv += ["-v", f"{assets_root / mount['host']}:{mount['container']}:ro"]
    if recipe.get("cargo_cache"):
        argv += ["-v", f"{assets_root / recipe['cargo_cache']}:/cargo-cache"]
        argv += ["-e", "CARGO_HOME=/cargo-cache"]
    for key, value in recipe.get("env", {}).items():
        argv += ["-e", f"{key}={value}"]
    argv += ["-w", recipe["workdir"], recipe["docker_image"]]
    argv += list(recipe["shell"]) + [command or recipe["command"]]
    return argv


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def load_proof_reports(assets_root: Path) -> dict[str, dict[str, Any]]:
    """Proof runs recorded by ``smoke`` (and by harness recovery reports)."""
    proofs: dict[str, dict[str, Any]] = {}
    proof_dir = assets_root / "proofs"
    if proof_dir.is_dir():
        for path in sorted(proof_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if payload.get("id"):
                proofs[payload["id"]] = payload
    return proofs


def cmd_manifest(args: argparse.Namespace) -> int:
    assets_root = Path(args.assets_root).resolve()
    legacy_root = Path(args.legacy_root).resolve()
    proofs = load_proof_reports(assets_root)

    entries: list[dict[str, Any]] = []
    checksums: list[tuple[str, str]] = []
    ready = 0
    for task_id in ALL_TASK_IDS:
        if task_id in UNRECOVERED:
            record = dict(UNRECOVERED[task_id])
            record.update({"id": task_id, "status": "blocked", "recovered": False})
            entries.append(record)
            continue
        recipe = RECIPES[task_id]
        workspace = assets_root / recipe["workspace"]
        blockers = contract_blockers(task_id, recipe, assets_root)
        proof = proofs.get(task_id, {})
        proof_runs = proof.get("runs", [])
        blockers.extend(
            f"{text} (run `recover_legacy_native_rust.py smoke"
            f"{' --golden' if recipe.get('golden_patch') else ''}` first)"
            for text in proof_blockers(recipe, proof_runs)
        )
        hashed: list[dict[str, str]] = []
        extra_hashed = list(recipe.get("harness_files", []))
        if recipe.get("golden_patch"):
            extra_hashed.append(recipe["golden_patch"])
        for copy in recipe.get("harness_copies", []):
            extra_hashed.append(copy["host"])
        if recipe.get("provenance", {}).get("image_recipe"):
            extra_hashed.append(recipe["provenance"]["image_recipe"])
        for relative in dict.fromkeys(extra_hashed):
            path = assets_root / relative
            if path.is_file():
                digest = sha256_file(path)
                hashed.append({"path": relative, "sha256": digest})
                checksums.append((digest, relative))
        record = {
            "id": task_id,
            "status": "ready" if not blockers else "blocked",
            "recovered": True,
            "lane": recipe["lane"],
            "repo": recipe["repo"],
            "case": recipe["case"],
            "start_sha": recipe["start_sha"],
            "workspace": recipe["workspace"],
            "workspace_state": workspace_state(workspace / recipe["patch_subdir"]),
            "mounted_cache_digests": mounted_cache_digests(recipe, assets_root),
            "patch_subdir": recipe["patch_subdir"],
            "docker_image": recipe["docker_image"],
            "docker_image_id": docker_image_id(recipe["docker_image"]),
            "shell": recipe["shell"],
            "user": recipe.get("user"),
            "network": recipe.get("network"),
            "env": recipe.get("env", {}),
            "mount": recipe.get("mount", {}),
            "readonly_mounts": recipe.get("readonly_mounts", []),
            "cache_mounts": recipe.get("cache_mounts", {}),
            "cargo_cache": recipe.get("cargo_cache"),
            "harness_mounts": recipe.get("harness_mounts", []),
            "harness_copies": recipe.get("harness_copies", []),
            "workdir": recipe["workdir"],
            "command": recipe["command"],
            "golden_patch": recipe.get("golden_patch"),
            "golden_command": recipe.get("golden_command"),
            "expected_stdout_sha256": recipe.get("expected_stdout_sha256"),
            "pass_to_pass_tests": recipe.get("pass_to_pass_tests", []),
            "provenance": recipe["provenance"],
            "hashed_files": hashed,
            "proof_runs": proof.get("runs", []),
            "blockers": blockers,
            "replay_argv_example": docker_argv(
                task_id, recipe, assets_root, workspace
            ),
        }
        if not blockers:
            ready += 1
        entries.append(record)

    # Hash every other small authored/retained file in the assets root
    # (recipes, proofs, logs) so the bundle is fully checksummed; the
    # multi-gigabyte workspaces and caches are covered by git/provenance
    # metadata instead of a file-by-file hash.
    for subdir in ("harness", "proofs", "logs", "recipes"):
        base = assets_root / subdir
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            relative = str(path.relative_to(assets_root))
            if any(relative == entry[1] for entry in checksums):
                continue
            checksums.append((sha256_file(path), relative))

    manifest = {
        "schema_version": MANIFEST_SCHEMA,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "assets_root": str(assets_root),
        "legacy_root": str(legacy_root),
        "scope": {
            "task_count": len(ALL_TASK_IDS),
            "ready": ready,
            "blocked": len(ALL_TASK_IDS) - ready,
        },
        "tasks": entries,
    }
    (assets_root).mkdir(parents=True, exist_ok=True)
    manifest_path = assets_root / MANIFEST_NAME
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    checksum_path = assets_root / CHECKSUM_NAME
    checksum_path.write_text(
        "".join(f"{digest}  {relative}\n" for digest, relative in sorted(
            checksums, key=lambda item: item[1]
        ))
    )
    print(json.dumps(
        {
            "manifest": str(manifest_path),
            "manifest_sha256": sha256_file(manifest_path),
            "checksums": str(checksum_path),
            "checksums_sha256": sha256_file(checksum_path),
            "ready": ready,
            "blocked": len(ALL_TASK_IDS) - ready,
            "blocked_ids": [
                entry["id"] for entry in entries if entry["status"] != "ready"
            ],
        },
        indent=2,
    ))
    return 0


def purge_directory(path: Path) -> None:
    """Delete a disposable tree that may contain root-owned container state."""
    if not path.exists():
        return
    subprocess.run(
        ["docker", "run", "--rm", "-v", f"{path}:/w", "alpine",
         "sh", "-c", "rm -rf /w/* /w/.[!.]* 2>/dev/null; true"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    shutil.rmtree(path, ignore_errors=True)


def proof_blockers(recipe: dict[str, Any], runs: list[dict[str, Any]]) -> list[str]:
    """Why a contract's recorded proof runs are insufficient.

    A start-tree proof alone is never enough for a contract that declares a
    source-only golden: the Oracle is PASS_TO_PASS, so the golden must be
    shown to apply *and* to pass. Any recorded failing run (apply or execute)
    also keeps the contract out of the ready set.
    """
    blockers: list[str] = []
    if not any(
        row.get("variant") == "start" and row.get("exit_code") == 0 for row in runs
    ):
        blockers.append("no successful start-tree proof run")
    if recipe.get("golden_patch"):
        golden = [row for row in runs if row.get("variant") == "golden"]
        if not any(row.get("exit_code") == 0 for row in golden):
            failed = [row for row in golden if row.get("exit_code") != 0]
            detail = (
                f" (last failure: {failed[-1].get('stage')} -> "
                f"{failed[-1].get('error')})" if failed else ""
            )
            blockers.append(
                "contract declares a source-only golden patch but has no "
                f"successful golden proof run{detail}"
            )
    return blockers


def proof_status(recipe: dict[str, Any], runs: list[dict[str, Any]]) -> str:
    if proof_blockers(recipe, runs):
        return "failed" if runs else "blocked"
    return "ready"


def _apply_patch(tree: Path, patch: Path) -> tuple[bool, str]:
    result = run(
        ["git", "apply", "--whitespace=nowarn", "-p1", str(patch)], cwd=tree
    )
    if result.returncode == 0:
        return True, result.stdout
    fallback = run(["patch", "-p1", "--fuzz=0", "-i", str(patch)], cwd=tree)
    return fallback.returncode == 0, result.stdout + fallback.stdout


def cmd_smoke(args: argparse.Namespace) -> int:
    """Replay a recovered contract on the pristine start tree (and, with
    ``--golden``, on a disposable copy carrying the recovered source-only
    golden patch). This is the proof that "ready" is real."""
    assets_root = Path(args.assets_root).resolve()
    proof_dir = assets_root / "proofs"
    proof_dir.mkdir(parents=True, exist_ok=True)
    log_dir = assets_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    selected = [
        (task_id, recipe)
        for task_id, recipe in RECIPES.items()
        if not args.case or recipe["case"] in args.case
    ]
    if not selected:
        print(f"error: no recipe matches --case {args.case}", file=sys.stderr)
        return 2

    overall = 0
    for task_id, recipe in selected:
        workspace = assets_root / recipe["workspace"]
        blockers = contract_blockers(task_id, recipe, assets_root)
        proof_path = proof_dir / f"{recipe['case']}.json"
        existing = {}
        if proof_path.is_file():
            try:
                existing = json.loads(proof_path.read_text())
            except (OSError, json.JSONDecodeError):
                existing = {}
        runs: list[dict[str, Any]] = [] if args.reset else list(
            existing.get("runs", [])
        )
        if blockers:
            payload = {
                "id": task_id,
                "case": recipe["case"],
                "status": "blocked",
                "blockers": blockers,
                "runs": runs,
            }
            proof_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            print(json.dumps(payload, indent=2))
            overall = 1
            continue

        variants = ["start"] + (["golden"] if args.golden and recipe.get("golden_patch") else [])
        for variant in variants:
            if variant == "start":
                target = workspace
                cleanup = None
            else:
                target = workspace.parent / f"{recipe['case']}-golden-smoke"
                shutil.rmtree(target, ignore_errors=True)
                shutil.copytree(workspace, target, symlinks=True)
                ok, detail = _apply_patch(
                    target / recipe["patch_subdir"],
                    assets_root / recipe["golden_patch"],
                )
                if not ok:
                    # A golden patch that will not apply is a *recorded
                    # failure*, not a silently skipped variant: without it the
                    # contract can never satisfy the "golden must pass" rule,
                    # so the proof file must show why.
                    log_path = log_dir / f"{recipe['case']}-smoke-golden-apply.log"
                    log_path.write_text(detail, encoding="utf-8")
                    runs.append(
                        {
                            "variant": "golden",
                            "run": 1 + sum(
                                1 for r in runs if r.get("variant") == "golden"
                            ),
                            "exit_code": 1,
                            "stage": "apply",
                            "error": "golden patch failed to apply",
                            "duration_seconds": 0.0,
                            "log": str(log_path),
                            "command": f"apply {recipe['golden_patch']}",
                        }
                    )
                    print(f"{recipe['case']}: golden patch failed to apply:\n{detail}")
                    overall = 1
                    purge_directory(target)
                    continue
                cleanup = target
            for copy in recipe.get("harness_copies", []):
                destination = target / recipe["patch_subdir"] / copy["workspace_path"]
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(assets_root / copy["host"], destination)
            argv = docker_argv(
                task_id, recipe, assets_root, target,
                command=(
                    recipe.get("golden_command")
                    if variant == "golden" else None
                ),
            )
            log_path = log_dir / f"{recipe['case']}-smoke-{variant}.log"
            started = time.time()
            with log_path.open("w") as stream:
                completed = subprocess.run(
                    argv, stdout=stream, stderr=subprocess.STDOUT,
                    timeout=args.timeout,
                )
            row = {
                "variant": variant,
                "run": 1 + sum(1 for r in runs if r.get("variant") == variant),
                "exit_code": completed.returncode,
                "stage": "execute",
                "duration_seconds": round(time.time() - started, 1),
                "log": str(log_path),
                "command": " ".join(argv),
            }
            if completed.returncode != 0:
                row["error"] = f"{variant} proof run exited {completed.returncode}"
            runs.append(row)
            if completed.returncode != 0:
                overall = 1
            if cleanup is not None:
                purge_directory(cleanup)
            print(json.dumps({"case": recipe["case"], **row}))
        payload = {
            "id": task_id,
            "case": recipe["case"],
            "status": proof_status(recipe, runs),
            "blockers": proof_blockers(recipe, runs),
            "runs": runs,
        }
        proof_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return overall


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    manifest = sub.add_parser("manifest", help="write recovery-manifest.json + SHA256SUMS")
    manifest.add_argument("--assets-root", required=True)
    manifest.add_argument("--legacy-root", required=True)
    manifest.set_defaults(func=cmd_manifest)

    smoke = sub.add_parser("smoke", help="replay recovered contracts on the start tree")
    smoke.add_argument("--assets-root", required=True)
    smoke.add_argument("--legacy-root", required=False, default="")
    smoke.add_argument("--case", action="append")
    smoke.add_argument("--golden", action="store_true")
    smoke.add_argument(
        "--reset",
        action="store_true",
        help="Drop previously recorded proof runs for the selected cases "
        "before running (use after changing a recipe, so the manifest never "
        "carries proof runs produced by a superseded command).",
    )
    smoke.add_argument("--timeout", type=int, default=14400)
    smoke.set_defaults(func=cmd_smoke)

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
