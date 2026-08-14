#!/usr/bin/env python3
"""Provenance-preserving correctness replay adapter for the 17 published-hard
tasks whose Oracles were recovered under the 20260728 reserve (and the older
increment5/legacy159 recoveries) and therefore lack a modern
``lane_results_source`` replay contract (``source_kind == "published_legacy"``
in ``run_open_model_correctness.py``'s task plan).

This script does NOT reinvent normalization or finalization. It only
populates the extension point that ``run_open_model_correctness.py``
already reads but that nothing currently writes:

    <output>/legacy/correctness/<lane>.jsonl

Those raw rows are merged by ``run_open_model_correctness.py finalize``
alongside ``<output>/modern/correctness/<lane>.jsonl`` and normalized with
the very same ``normalized_record()`` function used for the 374 tasks that
do have a modern contract. See docs/OPEN_MODEL_EVALUATION.md section 7.1.

Design summary
--------------
* Native (6 tasks), rust (1 task): the historical recovery already produced
  a fully materialized, patchable ``start/`` checkout under the retained
  session root; a fresh disposable copy of that checkout is made per
  (task, model) pair, the staged candidate patch is applied there, and the
  historical, previously-*proven* Docker command is replayed unmodified.
* JVM (6 tasks), TS/JS (4 tasks): the exact start commit is materialized
  the way the historical harness did (``git archive | tar`` plus ``git init``
  and one empty commit -- a self-contained tree, not a ``git worktree``
  whose ``.git`` file would dangle inside the Oracle container), the staged
  candidate patch is applied there, and the Oracle's own command is replayed
  with the historical toolchain (Docker image / pinned Node runtime) against
  *pinned, hashed durable assets* under ``--asset-root``, with no network.

  ``JVM_MANIFEST``/``TSJS_MANIFEST`` carry the parts of the historical
  execution recipe that were never published with the Oracle records and
  had to be recovered from the retained harness and run logs themselves:
  the exact per-task Gradle cache partition, the session-authored
  ``oracle.*`` behavior tests, RepoSense's pre-repeat priming command, the
  Discourse ``node_modules`` symlink into the retained shared Node tools,
  the proven OpossumUI ``react-scripts`` invocation (the published
  ``test_command`` string is stale and cannot reproduce the published
  signature), and the FluidFramework pnpm/mocha recipe recovered from the
  pre-release ``legacy159_oracle_typescript.jsonl`` record that the
  published Oracle names as its own source artifact.

  ``verify-contracts`` replays every one of them at its exact *unpatched*
  start, through the same code path scoring uses, and requires the frozen
  ``pass_to_pass``/signature evidence to be reproduced before the task may be
  treated as runner-ready.

Never fabricate: any task whose required runner/harness/image/cache asset
cannot be found under ``--legacy-root`` is reported as a preflight blocker
and is simply left out of the raw output (finalize's own
``--allow-incomplete`` reporting will show it as a missing pair) rather
than being scored.

Resume, concurrency, and evidence hardening
--------------------------------------------
* **Resume validates provenance, not just presence.** Every raw row embeds
  ``task_fingerprint``/``oracle_fingerprint``/``replay_contract_fingerprint``
  (plus the generation's ``patch_sha256``/``apply_ok``). On resume,
  ``cmd_replay`` recomputes these for the *current* task/Oracle/adapter
  recipe/staged generation and reruns any row whose recorded fingerprints
  no longer match (``row_is_stale``), instead of silently trusting a row
  that predates a task/Oracle update, a regenerated candidate patch, or a
  revision to this adapter's own replay recipe
  (``ADAPTER_CONTRACT_VERSION``). These fields are additive; the existing
  ``normalized_record()`` embeds the full raw record verbatim, so they are
  accepted and preserved by the finalizer without any change to it.
* **Concurrent replay writers cannot lose rows.** ``append_raw_row`` holds
  an exclusive, per-lane, cross-process ``flock`` (``lane_lock``) around
  its read-merge-write critical section and *rereads the file from disk*
  under that lock rather than trusting any in-memory snapshot -- so two
  ``replay`` invocations racing on the same lane (different models, or
  several task IDs) can never clobber each other's already-flushed rows.
  The lock file's name ends in ``.lock``, matching
  ``run_open_model_correctness.py``'s existing hashing-exclusion
  convention. ``BASE.atomic_jsonl`` already writes to a PID-suffixed temp
  file before an atomic rename, so there is no cross-process temp-file
  collision risk either.
* **Raw rows never reference an unhashed external log.** Each replay
  writes its run/install/apply-error logs into a disposable ``--work-root``
  scratch copy (default ``<output>/legacy/work``, may point anywhere,
  including outside ``--output``), then ``persist_evidence`` copies just
  those small text logs into a permanent ``<output>/legacy/evidence/...``
  subtree and deletes the (potentially gigabyte-sized) scratch checkout.
  Raw rows reference only the copied, output-relative path, which
  ``run_open_model_correctness.py``'s ``write_artifact_set`` already walks
  and hashes into ``SHA256SUMS`` -- never the ephemeral/external work root.

Typical usage
-------------
    # 1) Stage every model's generation results (arbitrary model set) with
    #    the existing orchestrator, allowing the 40 published-legacy tasks
    #    to remain unresolved for now:
    python3 scripts/run_open_model_correctness.py prepare \\
      --tasks data/releases/hard/20260808/tasks.jsonl \\
      --oracles data/releases/hard/20260808/oracles.jsonl \\
      --repo-cache <repo-cache> --output <output> \\
      --model 'my-model=/path/to/generation_results.jsonl' \\
      --allow-partial-modern

    # 2) Preflight the 14 retained reserve contracts (fails closed on any
    #    missing asset without executing anything):
    python3 scripts/run_legacy_reserve_correctness.py preflight \\
      --tasks data/releases/hard/20260808/tasks.jsonl \\
      --oracles data/releases/hard/20260808/oracles.jsonl \\
      --legacy-root /data/v-peiyangliu/reviewbench-session-<id> \\
      --output <output>

    # 3) Replay only the tasks that preflight reported ready, for a given
    #    already-staged model:
    python3 scripts/run_legacy_reserve_correctness.py replay \\
      --tasks data/releases/hard/20260808/tasks.jsonl \\
      --oracles data/releases/hard/20260808/oracles.jsonl \\
      --legacy-root /data/v-peiyangliu/reviewbench-session-<id> \\
      --output <output> --model my-model

    # 4) Finalize as usual; missing/blocked pairs show up in
    #    correctness-manifest.json's "missing_pairs" until their assets are
    #    recovered.
    python3 scripts/run_open_model_correctness.py finalize \\
      --tasks data/releases/hard/20260808/tasks.jsonl \\
      --oracles data/releases/hard/20260808/oracles.jsonl \\
      --output <output> --allow-incomplete
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import Any


def _load_sibling_module():
    path = Path(__file__).resolve().parent / "run_open_model_correctness.py"
    spec = importlib.util.spec_from_file_location(
        "run_open_model_correctness",
        path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BASE = _load_sibling_module()

# The exact 14 published-hard task IDs sourced from the 20260728 reserve.
# Any drift between this list and what --tasks/--oracles currently contain
# is treated as a preflight blocker rather than silently adapting scope.
TASK_IDS: tuple[str, ...] = (
    "github__ClickHouse__ClickHouse__pull_74193__comment_2024566878__duplication",
    "github__Consensys__web3signer__pull_489__comment_789312375__modularity",
    "github__Desbordante__desbordante-core__pull_154__comment_1371183985__modularity",
    "github__FuelLabs__fuel-tx__pull_197__comment_1003742605__complexity",
    "github__IntelPython__dpnp__pull_2201__comment_1886256998__modularity",
    "github__SciCompMod__memilio__pull_690__comment_1394151706__complexity",
    "github__SkriptLang__Skript__pull_7136__comment_1835154932__complexity",
    "github__XRPLF__rippled__pull_5143__comment_1786919023__duplication",
    "github__angular__components__pull_24601__comment_830247812__complexity",
    "github__apache__solr__pull_3433__comment_2231064740__modularity",
    "github__discourse__discourse__pull_37336__comment_2752215814__complexity",
    "github__hibernate__hibernate-orm__pull_4938__comment_844341285__duplication",
    "github__lanl__singularity-eos__pull_75__comment_792881520__duplication",
    "github__microsoft__FluidFramework__pull_27466__comment_3350352130__complexity",
    "github__opossum-tool__OpossumUI__pull_987__comment_972768731__duplication",
    "github__reposense__RepoSense__pull_2078__comment_1472205280__duplication",
    "github__sniffy__sniffy__pull_554__comment_3568493577__modularity",
)
EXPECTED_LANE = {
    "github__ClickHouse__ClickHouse__pull_74193__comment_2024566878__duplication": "native",
    "github__Consensys__web3signer__pull_489__comment_789312375__modularity": "jvm",
    "github__Desbordante__desbordante-core__pull_154__comment_1371183985__modularity": "native",
    "github__FuelLabs__fuel-tx__pull_197__comment_1003742605__complexity": "rust",
    "github__IntelPython__dpnp__pull_2201__comment_1886256998__modularity": "native",
    "github__SciCompMod__memilio__pull_690__comment_1394151706__complexity": "native",
    "github__SkriptLang__Skript__pull_7136__comment_1835154932__complexity": "jvm",
    "github__XRPLF__rippled__pull_5143__comment_1786919023__duplication": "native",
    "github__angular__components__pull_24601__comment_830247812__complexity": "tsjs",
    "github__apache__solr__pull_3433__comment_2231064740__modularity": "jvm",
    "github__discourse__discourse__pull_37336__comment_2752215814__complexity": "tsjs",
    "github__hibernate__hibernate-orm__pull_4938__comment_844341285__duplication": "jvm",
    "github__lanl__singularity-eos__pull_75__comment_792881520__duplication": "native",
    "github__microsoft__FluidFramework__pull_27466__comment_3350352130__complexity": "tsjs",
    "github__opossum-tool__OpossumUI__pull_987__comment_972768731__duplication": "tsjs",
    "github__reposense__RepoSense__pull_2078__comment_1472205280__duplication": "jvm",
    "github__sniffy__sniffy__pull_554__comment_3568493577__modularity": "jvm",
}

# Historical, previously-*proven* Docker replay recipes for the 6 native
# tasks, recovered verbatim from
# artifacts/model_eval_20260728_agentic/correctness/native.jsonl (the
# recorded "command" field of a successful historical replay) except for
# Desbordante, whose recipe is reconstructed from
# run_native_batch.py's CASE_CONFIGS because every historical model patch
# failed to apply through this harness (no proven execution exists;
# encoded as proven_historical=False so preflight/replay can flag it).
#
# "workspace": the task_dir-relative subtree that gets freshly copied per
#   (task, model) replay and receives the candidate patch.
# "workspace_mount": container path the copied workspace is bound to.
# "workdir": container cwd the shell command runs from.
# "harness_mounts": task_dir-relative *files* bind-mounted read-only
#   straight from the retained (unmodified) task_dir -- never copied,
#   never patched.
NATIVE_MANIFEST: dict[str, dict[str, Any]] = {
    "github__ClickHouse__ClickHouse__pull_74193__comment_2024566878__duplication": {
        "task_dir": "oracle_recovery/native-real/tasks/05-clickhouse",
        "docker_image": "oracle-clickhouse:real",
        "workspace": "start",
        "workspace_mount": "/src",
        "workdir": "/src",
        "harness_mounts": [{"host": "query.sql", "container": "/work/query.sql"}],
        "shell_command": (
            'ninja -C build-real programs/clickhouse && '
            'build-real/programs/clickhouse-local --query "$(cat /work/query.sql)"'
        ),
        "proven_historical": True,
    },
    "github__Desbordante__desbordante-core__pull_154__comment_1371183985__modularity": {
        "task_dir": "oracle_recovery/native-real/tasks/11-desbordante",
        "docker_image": "reviewbench-native-real-desbordante",
        "workspace": ".",
        "workspace_mount": "/work",
        "workdir": "/work/start",
        "harness_mounts": [],
        "shell_command": (
            "cmake --build build-real --target Desbordante_lib -j8 && "
            "g++ -std=c++17 --coverage -O0 -g /work/oracle.cpp -Isrc/core -Isrc "
            "-Ibuild-real/src/core build-real/src/core/libDesbordante_lib.a "
            "build-real/lib/easyloggingpp/libeasyloggingpp.a -lboost_container "
            "-lboost_program_options -lboost_thread -lboost_graph -pthread "
            "-o oracle-real && ./oracle-real /work/start"
        ),
        "proven_historical": False,
    },
    "github__IntelPython__dpnp__pull_2201__comment_1886256998__modularity": {
        "task_dir": "oracle_recovery/native-real/tasks/14-dpnp",
        "docker_image": "oracle-dpnp:real",
        "workspace": "start",
        "workspace_mount": "/src",
        "workdir": "/src",
        "harness_mounts": [
            {"host": "behavior_test.py", "container": "/oracle/behavior_test.py"}
        ],
        "shell_command": (
            'CC=icx CXX=icpx python3 setup.py build_ext --inplace -- '
            '-DCMAKE_BUILD_TYPE=Debug "-DCMAKE_CXX_FLAGS=-O0 -g --coverage" '
            '-DCMAKE_SHARED_LINKER_FLAGS=--coverage && '
            'PYTHONPATH=/src python3 /oracle/behavior_test.py'
        ),
        "proven_historical": True,
    },
    "github__SciCompMod__memilio__pull_690__comment_1394151706__complexity": {
        "task_dir": "oracle_recovery/native-real/tasks/19-memilio-history",
        "docker_image": "reviewbench-native-real-eventuals",
        "workspace": ".",
        "workspace_mount": "/work",
        "workdir": "/work/start",
        "harness_mounts": [],
        "shell_command": (
            "g++-11 -std=c++17 -O0 -g -Icpp /work/oracle.cpp -o "
            "correctness-oracle && ./correctness-oracle"
        ),
        "proven_historical": True,
    },
    "github__XRPLF__rippled__pull_5143__comment_1786919023__duplication": {
        "task_dir": "oracle_recovery/native-real/tasks/22-rippled",
        "docker_image": "oracle-rippled:real",
        "workspace": "start",
        "workspace_mount": "/src",
        "workdir": "/src",
        "harness_mounts": [],
        "shell_command": (
            "conan install . --output-folder=. --build=missing "
            "-s build_type=Debug 2>/dev/null 1>/dev/null && "
            "cmake -S . -B build -G Ninja "
            "-DCMAKE_TOOLCHAIN_FILE=generators/conan_toolchain.cmake "
            '"-DCMAKE_CXX_FLAGS=-O0 -g --coverage" '
            "-DCMAKE_EXE_LINKER_FLAGS=--coverage 2>/dev/null 1>/dev/null && "
            'sed -i "s|/root/.conan/data/protobuf/3.21.9/_/_/package/'
            '[a-f0-9]*/bin/protoc|/usr/bin/true|g" build/build.ninja && '
            'find build -name "*.pb.h" -exec touch {} \\; 2>/dev/null && '
            'find build -name "*.pb.cc" -exec touch {} \\; 2>/dev/null && '
            "ninja -C build -j16 rippled && ./build/rippled --unittest=STObject"
        ),
        "proven_historical": True,
    },
    "github__lanl__singularity-eos__pull_75__comment_792881520__duplication": {
        "task_dir": "oracle_recovery/native-real/tasks/28-singularity-eos",
        "docker_image": "reviewbench-native-real-eventuals",
        "workspace": ".",
        "workspace_mount": "/work",
        "workdir": "/work/start",
        "harness_mounts": [],
        "shell_command": (
            "g++-11 -std=c++17 -O0 -g -I. -Iutils /work/oracle.cpp -o "
            "correctness-oracle && ./correctness-oracle"
        ),
        "proven_historical": True,
    },
}

TSJS_NODE_OVERRIDES = {
    "github__opossum-tool__OpossumUI__pull_987__comment_972768731__duplication": "18.20.8",
}

# ---------------------------------------------------------------------------
# Recovered JVM replay manifest (20260728 reserve)
# ---------------------------------------------------------------------------
#
# The published JVM Oracles carry their own ``command``/``environment``, but
# three pieces of the historical execution recipe were *not* published with
# them and had to be recovered from the retained harness itself
# (strict_screen_20260726/oracle_recovery/java/execute.py and the per-task
# artifact directories):
#
# * ``cache_index`` -- the historical GRADLE_USER_HOME was
#   ``/cachehome/gradle-task-<index>`` where <index> is the numeric prefix of
#   the Oracle's own ``artifacts.directory``. Replaying with a different
#   per-task cache home would resolve dependencies into a cold cache and
#   diverge; this pins the exact historical cache partition.
# * ``prime_command`` -- RepoSense's published command carries
#   ``-x compileTestJava`` because the historical harness ran it as a *repeat*
#   inside a workspace an earlier run had already compiled. In a fresh
#   materialization the scored command alone would find no test classes, so the
#   recorded pre-repeat command (Oracle ``attempts[0].command`` plus the same
#   init-script instrumentation) is replayed first, exactly reproducing the
#   historical workspace state, and only the scored command's result counts.
# * ``session_tests`` -- Skript and RepoSense are scored against session-authored
#   behavior tests in the ``oracle`` package that the harness copied into the
#   workspace from ``<artifacts.directory>/session-tests``.
#
# ``expected_tests`` is the frozen ``pass_to_pass`` evidence and is what
# ``verify-contracts`` requires a fresh unpatched start replay to reproduce
# before the task may be treated as runner-ready.
JVM_MANIFEST: dict[str, dict[str, Any]] = {
    "github__Consensys__web3signer__pull_489__comment_789312375__modularity": {
        "cache_index": "8",
        "session_tests": False,
        "prime_command": None,
        "test_results_globs": ("acceptance-tests/build/test-results/**/*.xml",),
        "expected_tests": 6,
        # This Oracle's own acceptance-test harness downloads HashiCorp Vault
        # at build time (:acceptance-tests:downloadVault, a conditional GET
        # against releases.hashicorp.com), exactly as the historical run did.
        # It therefore keeps a network namespace, but the downloaded archive is
        # pre-placed from a pinned, hashed copy so the conditional request is a
        # no-op and the Vault binary under test is byte-identical to the
        # historical one; dependency resolution is served by the pinned cache
        # and its file:// mirror, which oracle.init.gradle declares first.
        "network": "bridge",
        "prefetch": (
            {
                "asset": "prefetch/web3signer-hashicorp.zip",
                "workspace_path": "acceptance-tests/build/hashicorp.zip",
                "sha256": (
                    "f486da4f6d08e42eb2c17e95cf36cc7f9d9c0e7cc8ced06ce3fca7c3"
                    "abd7db3d"
                ),
            },
        ),
    },
    "github__SkriptLang__Skript__pull_7136__comment_1835154932__complexity": {
        "cache_index": "17",
        "session_tests": True,
        "prime_command": None,
        "test_results_globs": ("build/test-results/**/*.xml",),
        "expected_tests": 1,
    },
    "github__apache__solr__pull_3433__comment_2231064740__modularity": {
        "cache_index": "31",
        "session_tests": False,
        "prime_command": None,
        "test_results_globs": ("solr/modules/ltr/build/test-results/**/*.xml",),
        "expected_tests": 215,
        # Solr's gradlew downloads its own wrapper jar from raw.githubusercontent
        # when it is absent, which a network-isolated replay cannot do. The jar
        # is pinned as a durable asset and installed from it after verifying the
        # repository's *own* recorded gradle-wrapper.jar.sha256, so the
        # installed bytes are exactly what the checked-out revision demands.
        "needs_gradle_wrapper_jar": True,
    },
    "github__hibernate__hibernate-orm__pull_4938__comment_844341285__duplication": {
        "cache_index": "35",
        "session_tests": False,
        "prime_command": None,
        "test_results_globs": (
            "hibernate-spatial/target/test-results/**/*.xml",
            "hibernate-spatial/build/test-results/**/*.xml",
        ),
        "expected_tests": 4,
    },
    "github__reposense__RepoSense__pull_2078__comment_1472205280__duplication": {
        "cache_index": "41",
        "session_tests": True,
        "prime_command": (
            "bash ./gradlew --no-daemon --console=plain cleanTest test "
            "--tests 'oracle.RepoConfigurationBehaviorOracleTest' "
            "-I /oracle-cache/oracle.init.gradle"
        ),
        "test_results_globs": ("build/test-results/**/*.xml",),
        "expected_tests": 1,
    },
    # sniffy's published Oracle predates the JVM ``environment`` block: it
    # records only ``command`` plus ``toolchain``
    # ("Maven 3.9.9; Eclipse Temurin JDK 17; JaCoCo 0.8.8"). The container and
    # cache home below are the exact, unambiguous encoding of that recorded
    # toolchain string; no other field is invented.
    "github__sniffy__sniffy__pull_554__comment_3568493577__modularity": {
        "cache_index": "sniffy",
        "session_tests": False,
        "prime_command": None,
        "container": "maven:3.9.9-eclipse-temurin-17",
        "cache": "strict_screen_20260726/oracle_recovery/java/cache/home",
        # Only the scored reactor module counts: `-am` also builds and tests
        # sniffy-core, whose 374 tests are not this Oracle's anchor.
        "test_results_globs": ("sniffy-module-nio/**/surefire-reports/*.xml",),
        "expected_tests": 131,
        # This Oracle's behavior anchor *is* network behavior (Sniffy's socket
        # and NIO channel monitoring binds loopback sockets and resolves the
        # local host name), so running it in an isolated network namespace
        # would change the behavior under test rather than isolate it.
        # Dependency resolution is still pinned and reproducible: Maven is put
        # in offline mode through MAVEN_ARGS, so every artifact must come from
        # the pinned .m2 in the durable cache home.
        "network": "bridge",
        "offline_env": {"MAVEN_ARGS": "-o"},
    },
}


def jvm_container(oracle: dict[str, Any], task_id: str) -> str:
    environment = oracle.get("environment") or {}
    return str(
        environment.get("container")
        or JVM_MANIFEST.get(task_id, {}).get("container")
        or ""
    )


def jvm_cache(oracle: dict[str, Any], task_id: str) -> str:
    environment = oracle.get("environment") or {}
    return str(
        environment.get("cache")
        or JVM_MANIFEST.get(task_id, {}).get("cache")
        or ""
    )


# ---------------------------------------------------------------------------
# Recovered TS/JS replay manifest (20260728 reserve)
# ---------------------------------------------------------------------------
#
# ``node_modules`` describes how the historical run made a runnable dependency
# tree available; it is never re-resolved from the network at replay time:
#
# * ``shared_node_tools`` -- the retained
#   ``oracle_recovery/tsjs/_node_tools/node_modules`` tree is symlinked into the
#   workspace exactly as the historical ``_work/discourse__discourse`` checkout
#   did (the symlink is still present in the retained tree), which is what makes
#   the session Vitest config resolve ``vitest/config``.
# * ``pinned`` -- a durable, hashed ``node_modules`` snapshot under the recovery
#   asset root, hardlink-copied per replay.
#
# ``test_command`` is the *executed* historical command recovered from the
# retained run logs, which for OpossumUI differs from the (stale) published
# ``test_command`` field: the retained probe4/recovery-*-run logs show the
# proven invocation targets ``ProjectStatisticsPopup.test.tsx`` through
# ``node_modules/.bin/react-scripts`` and is the one whose output matches the
# published ``start_signature``/``golden_signature``
# ("Test Suites: 1 passed, 1 total|Tests: 2 passed, 2 total"). Going through the
# published ``yarn test:local ...`` string instead expands to the whole ``src/``
# suite and can never reproduce that signature, so it would turn recovered
# infrastructure into model failure.
TSJS_MANIFEST: dict[str, dict[str, Any]] = {
    "github__discourse__discourse__pull_37336__comment_2752215814__complexity": {
        "node": "20.19.5",
        "node_modules": "shared_node_tools",
        "session_from_recovery": True,
        "workspace": ".",
        "test_command": "vitest run --config session/vitest.config.js --coverage",
        "expected_signature": "Test Files  1 passed (1)",
        # Vitest reports suites ("Test Files") and cases ("Tests") separately;
        # the frozen signature is "Test Files: 1 passed (1)|Tests: 2 passed (2)"
        # so the executed-case count must be read from the "Tests" line.
        "test_count_patterns": (r"\bTests\s+(\d+) passed",),
        "expected_tests": 2,
        "runtime_root": "legacy",
    },
    "github__opossum-tool__OpossumUI__pull_987__comment_972768731__duplication": {
        "node": "18.20.8",
        "node_modules": "pinned",
        "pinned_node_modules": "tsjs/node_modules/opossum-tool__OpossumUI",
        "session_from_recovery": False,
        "workspace": ".",
        "test_command": (
            "node_modules/.bin/react-scripts test --watchAll=false "
            "src/Frontend/Components/ProjectStatisticsPopup/__tests__/"
            "ProjectStatisticsPopup.test.tsx --runInBand "
            "--setupFilesAfterEnv=./setupTests.ts"
        ),
        "expected_signature": "Test Suites: 1 passed, 1 total",
        "test_count_patterns": (r"\bTests:\s+(\d+) passed",),
        "expected_tests": 2,
        "runtime_root": "legacy",
    },
    # Recovered verbatim from the retained pre-release oracle record
    # legacy159_oracle_typescript.jsonl ("commands"/"environment_notes"), the
    # declared source artifact of the published FluidFramework Oracle
    # (release_oracle_provenance.source_artifact ==
    # "legacy159_oracle_typescript_normalized.jsonl"), which is the only place
    # the executable recipe survives: the published record carries coverage and
    # pass_to_pass only. Replayed at the *released task* start commit.
    "github__microsoft__FluidFramework__pull_27466__comment_3350352130__complexity": {
        "node": "22.22.2",
        "node_modules": "pnpm_workspace",
        "package_manager": "pnpm@10.33.0",
        "install_command": "pnpm install --frozen-lockfile",
        "build_command": "pnpm --dir packages/dds/tree build",
        "session_from_recovery": False,
        "workspace": "packages/dds/tree",
        "test_env": {
            "MOCHA_SPEC": (
                "lib/test/feature-libraries/default-field-kinds/"
                "defaultChangeFamily.spec.js"
            )
        },
        "test_command": "pnpm exec mocha lib/test/shared-tree/editing.spec.js --reporter dot",
        "expected_signature": None,
        "test_count_patterns": (r"(\d+) passing",),
        # The published Oracle's pass_to_pass (151) was measured at the PR
        # *base* commit (legacy159 start_sha 4b1a8bec...); the released task's
        # exact start (55842a0e..., the PR head at the review comment) carries
        # the PR's own earlier spec edits, and a fresh unpatched start replay
        # of the very same Oracle command reproduces 152 passing / 2 pending.
        # The validated-at-exact-start count is what a candidate must preserve.
        "expected_tests": 152,
        "runtime_root": "assets",
    },
}


INFRA_RETRY = "infra_retry"
CANDIDATE_MISSING = "candidate_missing"

# Bump whenever the *behavior* of a replay_* function, NATIVE_MANIFEST entry,
# or judging rule changes in a way that could change the outcome for
# already-recorded rows. It is folded into every raw row's
# ``replay_contract_fingerprint`` so ``cmd_replay``'s resume logic reruns
# rows that were produced under a since-changed contract instead of
# silently treating them as fresh (code-review finding: "resume must
# validate current task/oracle/replay-contract fingerprints and rerun
# stale rows").
ADAPTER_CONTRACT_VERSION = "v5-2026-08-13-jvm-tsjs-asset-recovery"


def replay_contract_fingerprint(
    task_id: str,
    lane: str,
    oracle: dict[str, Any],
    legacy_root: Path,
    blockers: list[str],
    asset_root: Path | None = None,
) -> str:
    """Fingerprint of *this adapter's* replay recipe for ``task_id`` as
    resolved against the *current* ``--legacy-root``.

    Combined with the task fingerprint (``BASE.task_fingerprint``) and the
    Oracle fingerprint (``BASE.canonical_hash(oracle)``), this lets resume
    logic tell a genuinely-already-replayed row apart from one that was
    produced under a task/Oracle/adapter-recipe revision that has since
    changed underneath it.

    ``legacy_root``, the live ``blockers`` list, and a cheap non-content
    ``asset_state()`` snapshot are folded in too (code-review finding:
    "cmd_replay must evaluate current task_blockers()/required asset
    provenance before resume skip ... bind required asset fingerprints
    into replay contract provenance") so that pointing at a different
    ``--legacy-root``, removing a required asset, or silently
    replacing/retagging one at the same path all invalidate any
    previously-recorded row instead of being resumed past.
    """
    payload: dict[str, Any] = {
        "adapter_contract_version": ADAPTER_CONTRACT_VERSION,
        "legacy_root": str(legacy_root),
        "asset_root": str(asset_root) if asset_root else None,
        "blockers": sorted(blockers),
        "asset_state": asset_state(
            task_id, lane, oracle, legacy_root, asset_root
        ),
    }
    if lane == "native":
        payload["native_manifest"] = NATIVE_MANIFEST[task_id]
    return BASE.canonical_hash(payload)


def row_provenance_fields(
    task: dict[str, Any],
    oracle: dict[str, Any],
    task_id: str,
    lane: str,
    generation: dict[str, Any],
    legacy_root: Path,
    blockers: list[str],
    asset_root: Path | None = None,
) -> dict[str, Any]:
    """Strong provenance fields folded into every raw row.

    These are additive fields alongside the ones ``normalized_record()``
    already reads by name (``id``, ``model``, ``lane``, ``start_sha``,
    ``patch_sha256``/``patch_apply``/``correctness_ok`` etc.); since
    ``normalized_record()`` embeds the full raw record verbatim under
    ``raw_record`` and hashes it into ``raw_record_sha256``, these fields
    are accepted and preserved by the existing finalizer without any
    change to it.
    """
    return {
        "task_fingerprint": BASE.task_fingerprint(task),
        "oracle_fingerprint": BASE.canonical_hash(oracle),
        "replay_contract_fingerprint": replay_contract_fingerprint(
            task_id, lane, oracle, legacy_root, blockers, asset_root
        ),
        "generation_apply_ok": generation.get("apply_ok"),
    }


def row_is_stale(
    existing_row: dict[str, Any],
    task: dict[str, Any],
    oracle: dict[str, Any],
    task_id: str,
    lane: str,
    generation: dict[str, Any],
    legacy_root: Path,
    blockers: list[str],
    asset_root: Path | None = None,
) -> bool:
    """True if a previously-recorded raw row no longer matches the current
    task, Oracle, adapter replay contract, live asset provenance, or staged
    generation, and must therefore be rerun (or, if now blocked, retracted)
    rather than treated as already-done on resume.
    """
    current = row_provenance_fields(
        task, oracle, task_id, lane, generation, legacy_root, blockers, asset_root
    )
    if any(existing_row.get(field) != value for field, value in current.items()):
        return True
    return existing_row.get("patch_sha256") != generation.get("patch_sha256")


# ---------------------------------------------------------------------------
# Concurrency-safe raw-row persistence
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def lane_lock(legacy_dir: Path, lane: str):
    """Exclusive, per-lane, cross-process advisory lock.

    Guards the read-merge-write critical section in ``append_raw_row`` so
    that concurrent ``replay`` invocations (e.g. one process per model, or
    several models sharing one process) never lose a row: whichever
    process is inside the lock rereads the current on-disk state, so the
    other process's already-flushed row can never be silently clobbered by
    a write based on a stale in-memory snapshot. The lock file's name ends
    in ``.lock`` so it is already excluded from
    ``run_open_model_correctness.py``'s ``write_artifact_set`` hashing.
    """
    legacy_dir.mkdir(parents=True, exist_ok=True)
    lock_path = legacy_dir / f".{lane}.lock"
    with open(lock_path, "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Small generic helpers
# ---------------------------------------------------------------------------


def resolve_legacy_relative(
    value: str,
    legacy_root: Path,
    asset_root: Path | None = None,
) -> Path:
    """Resolve a path recorded by a historical Oracle against the retained
    session root, falling back to the recovery asset root.

    JVM Oracle path fields (``artifacts.directory``, ``environment.cache``)
    are recorded already prefixed with ``strict_screen_20260726/`` (i.e.
    relative to the session root itself); every other lane's fields
    (``recovery_artifact``, ``artifacts.directory``, ``environment.
    cargo_cache``) are recorded relative to ``strict_screen_20260726``.
    Both conventions are handled explicitly rather than assumed, since
    silently guessing wrong would resolve to the wrong (or a nonexistent)
    directory.

    ``asset_root`` is the durable recovery-asset root
    (``--asset-root``). Assets that no longer exist under the retained
    session root (most importantly the JVM Oracles'
    ``environment.cache`` Gradle/Maven cache home and its
    ``/oracle-cache`` blobs) are reconstructed there under the *same*
    recorded relative path, so resolution stays a pure function of the
    Oracle's own recorded value. The retained root always wins when it
    still has the asset, so recovery can never shadow retained evidence.
    """
    if value.startswith("strict_screen_20260726/"):
        retained = (legacy_root / value).resolve()
        relative = value
    else:
        retained = (legacy_root / "strict_screen_20260726" / value).resolve()
        relative = f"strict_screen_20260726/{value}"
    if retained.exists() or asset_root is None:
        return retained
    recovered = (asset_root / relative).resolve()
    return recovered if recovered.exists() else retained


def artifacts_directory(
    oracle: dict[str, Any],
    legacy_root: Path,
    asset_root: Path | None = None,
) -> Path:
    """Resolve an Oracle's ``artifacts.directory``/``recovery_artifact``."""
    value = str(
        oracle.get("recovery_artifact")
        or (oracle.get("artifacts") or {}).get("directory")
        or ""
    )
    if not value:
        raise ValueError("Oracle is missing an artifacts/recovery directory")
    return resolve_legacy_relative(value, legacy_root, asset_root)



def oracle_recovery_root(legacy_root: Path) -> Path:
    return legacy_root / "strict_screen_20260726" / "oracle_recovery"


def repos_root(legacy_root: Path) -> Path:
    return legacy_root / "repos"


def repo_checkout_dir(legacy_root: Path, repo: str) -> Path:
    return repos_root(legacy_root) / repo.replace("/", "__")


def docker_available() -> bool:
    return shutil.which("docker") is not None


def docker_image_present(image: str) -> bool:
    if not docker_available():
        return False
    result = subprocess.run(
        ["docker", "image", "inspect", image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def docker_image_id(image: str) -> str | None:
    """Content-identity of a locally loaded Docker image, or ``None`` if it
    is not present. Used (in addition to ``docker_image_present``'s plain
    existence check) so a retagged/rebuilt image with the same name still
    invalidates any previously-recorded row referencing it.
    """
    if not docker_available():
        return None
    result = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def path_identity(path: Path) -> dict[str, Any] | None:
    """Cheap (stat-only, never content-hashing) identity of a required
    asset path, so a missing/replaced file or directory under
    ``--legacy-root`` is detected without rehashing potentially huge
    checkouts on every resume decision.

    This alone is *not* sufficient for a directory that is an actual
    replay source tree (e.g. Rust's ``start/``, a JVM/TS-JS repo
    checkout, a recovered ``session/`` tree): an in-place content
    replacement that reuses the directory's own size/mtime (a common
    outcome of ``rsync``/``cp -a``/container-mount refreshes) would be
    invisible to a parent-only stat. Use ``directory_identity`` for
    those; this remains appropriate for single files and directories
    that are not themselves walked (e.g. a container image mount point).
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    return {"is_dir": path.is_dir(), "size": stat.st_size, "mtime": stat.st_mtime}


_DIRECTORY_IDENTITY_MAX_FILES = 500
_DIRECTORY_IDENTITY_MAX_BYTES_PER_FILE = 65536  # 64 KiB read cap per file


def _bounded_file_digest(path: Path) -> str:
    """Bounded *content* digest of a single file: hashes the file's exact
    size plus up to the first 64 KiB of its bytes.

    Never reads a huge cached artifact (Gradle/cargo cache blob, build
    output, etc.) in full, but any change to its size or to that bounded
    prefix -- which is what actually distinguishes a same-path in-place
    replacement in practice -- still changes the digest.
    """
    size = path.stat().st_size
    hasher = hashlib.sha256()
    hasher.update(str(size).encode("utf-8"))
    with path.open("rb") as handle:
        hasher.update(handle.read(_DIRECTORY_IDENTITY_MAX_BYTES_PER_FILE))
    return hasher.hexdigest()


def safe_file_digest(path: Path | None) -> str | None:
    """``_bounded_file_digest`` that degrades to ``None`` for a missing
    path/unreadable file instead of raising -- asset-state fingerprinting
    must never itself fail closed with an exception (that is
    ``task_blockers()``'s job); a ``None`` digest simply differs from any
    real digest, so a file that disappears still invalidates the
    fingerprint.
    """
    if path is None:
        return None
    try:
        return _bounded_file_digest(path)
    except OSError:
        return None


def directory_identity(
    path: Path, *, max_files: int = _DIRECTORY_IDENTITY_MAX_FILES
) -> dict[str, Any] | None:
    """Bounded, content-sensitive identity of a directory tree.

    Unlike ``path_identity`` (a single stat() of the directory itself),
    this walks the tree -- in a stable, deterministic (sorted at every
    level) order -- and folds in a bounded content digest
    (``_bounded_file_digest``) of every file it visits, so replacing the
    same source tree in-place (e.g. Oracle recovery re-run, repo
    re-checkout, retained cache refresh) changes the fingerprint even
    when the top-level directory's own mtime/size is unaffected or
    reused by the replacement.

    Both the *traversal* and the *hashing* are bounded to ``max_files``:
    a multi-gigabyte retained JVM/cargo cache or full source checkout is
    never fully enumerated or read, so every resume decision stays cheap
    regardless of how large ``--legacy-root`` is. This is a deliberate
    trade-off (explicitly requested): content changes strictly beyond
    the bounded file/byte caps are not guaranteed to be detected, but any
    change among the first ``max_files`` files (sorted by path) is.
    """
    if not path.is_dir():
        return None
    digests: list[tuple[str, str]] = []
    truncated = False
    for root, dirnames, filenames in os.walk(path):
        dirnames.sort()
        for filename in sorted(filenames):
            if len(digests) >= max_files:
                truncated = True
                break
            file_path = Path(root) / filename
            try:
                digest = _bounded_file_digest(file_path)
            except OSError:
                digest = "unreadable"
            digests.append((str(file_path.relative_to(path)), digest))
        if truncated:
            break
    return {
        "sample_file_count": len(digests),
        "truncated": truncated,
        "digest": BASE.canonical_hash(digests),
    }


def oracle_cache_tokens(command: str) -> list[str]:
    """Return the ``/oracle-cache/<name>`` assets a jvm command requires."""
    return sorted(set(re.findall(r"/oracle-cache/([\w.\-]+)", command)))


def patch_changed_paths(patch_text: str) -> list[str]:
    return list(
        dict.fromkeys(
            right
            for _, right in re.findall(
                r"^diff --git a/(.+?) b/(.+?)$", patch_text, re.M
            )
        )
    )


def scope_is_safe(paths: list[str]) -> bool:
    for path in paths:
        if path.startswith("/") or ".." in Path(path).parts:
            return False
    return True


def apply_patch(workdir: Path, patch_path: Path) -> tuple[bool, str]:
    """Apply an exact-start candidate patch to a freshly materialized tree.

    Tries ``git apply`` first (works even outside a git repository as long
    as the path prefixes line up) and falls back to POSIX ``patch -p1``.
    """
    git_result = subprocess.run(
        ["git", "apply", "--whitespace=nowarn", "-p1", str(patch_path)],
        cwd=workdir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if git_result.returncode == 0:
        return True, git_result.stdout
    patch_result = subprocess.run(
        ["patch", "-p1", "--fuzz=0", "-i", str(patch_path)],
        cwd=workdir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if patch_result.returncode == 0:
        return True, patch_result.stdout
    return False, (
        f"git apply: {git_result.stdout}\npatch -p1: {patch_result.stdout}"
    )


def git_worktree_checkout(repo_source: Path, start_sha: str, dest: Path) -> None:
    subprocess.run(
        ["git", "-C", str(repo_source), "worktree", "remove", "--force", str(dest)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    shutil.rmtree(dest, ignore_errors=True)
    subprocess.run(
        ["git", "-C", str(repo_source), "worktree", "prune"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            "git", "-C", str(repo_source), "worktree", "add", "--force",
            "--detach", str(dest), start_sha,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"exact-start materialization failed: {result.stdout}")
    head = subprocess.check_output(
        ["git", "-C", str(dest), "rev-parse", "HEAD"], text=True
    ).strip()
    if head != start_sha:
        raise RuntimeError(
            f"materialized HEAD {head} is not the exact start {start_sha}"
        )


def materialize_start(repo_source: Path, start_sha: str, dest: Path) -> None:
    """Materialize the exact start commit the way the historical harness did.

    ``strict_screen_20260726/oracle_recovery/java/execute.py``'s
    ``materialize()`` streamed ``git archive <sha> | tar -x`` into a fresh
    directory and then ran ``git init`` plus one empty commit, producing a
    *self-contained* working tree. That detail is load-bearing rather than
    cosmetic: a ``git worktree`` checkout's ``.git`` file points at an
    absolute path inside the retained bare clone that is not mounted into
    the Oracle container, so repository-introspecting builds break (e.g.
    RepoSense's ``versionDetails()`` fails to evaluate build.gradle at all),
    and the resulting failure would be scored as a model failure. This
    reproduces the historical materialization exactly, and the fresh
    single-commit repository is also what makes ``git apply`` of a candidate
    patch behave identically to the historical harness.
    """
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    archive = subprocess.Popen(
        ["git", "-C", str(repo_source), "archive", "--format=tar", start_sha],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    extract = subprocess.Popen(
        ["tar", "-x", "-C", str(dest)],
        stdin=archive.stdout,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    assert archive.stdout is not None
    archive.stdout.close()
    extract_rc = extract.wait()
    archive_rc = archive.wait()
    if extract_rc or archive_rc:
        detail = (archive.stderr.read() if archive.stderr else b"").decode(
            "utf-8", "replace"
        )
        raise RuntimeError(
            f"exact-start materialization failed for {start_sha}: {detail[-2000:]}"
        )
    subprocess.run(
        ["git", "init", "-q"],
        cwd=dest,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )
    subprocess.run(
        [
            "git", "-c", "user.name=Oracle", "-c", "user.email=oracle@invalid",
            "commit", "-q", "--allow-empty", "-m", "start-anchor",
        ],
        cwd=dest,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )


def link_tree(source: Path, dest: Path) -> None:
    """Hardlink-copy a pinned dependency tree into a disposable workspace.

    Used for the pinned ``node_modules`` snapshots under the recovery asset
    root: the replay gets a private, writable directory tree without copying
    (or re-resolving) hundreds of megabytes per (task, model) pair, and the
    pinned asset itself is never mutated.
    """
    shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["cp", "-al", f"{source}/.", str(dest)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if result.returncode != 0:
        shutil.copytree(source, dest, symlinks=True, dirs_exist_ok=True)


def node_runtime_bin(
    node: str,
    legacy_root: Path,
    asset_root: Path | None,
    runtime_root: str = "legacy",
) -> Path | None:
    """Resolve the pinned Node runtime for a tsjs task.

    ``runtime_root == "legacy"`` are the runtimes retained with the historical
    session; ``"assets"`` are runtimes pinned into the recovery asset root
    (downloaded once from nodejs.org, hashed into the asset manifest, and
    reused offline afterwards) for versions the retained session never held.
    Both roots are searched regardless of the declared preference so a runtime
    is never re-downloaded when it is already retained.
    """
    if not node:
        return None
    relative = Path("tsjs/_runtimes") / f"node-v{node}-linux-x64" / "bin"
    candidates = []
    if runtime_root == "assets" and asset_root is not None:
        candidates.append(asset_root / relative)
    candidates.append(oracle_recovery_root(legacy_root) / relative)
    if asset_root is not None:
        candidates.append(asset_root / relative)
    for candidate in candidates:
        if (candidate / "node").is_file():
            return candidate
    return None


def commit_present(repo_dir: Path, sha: str) -> bool:
    """True if ``sha`` resolves to an actual commit object reachable in
    the retained repo checkout.

    Used by ``tsjs_blockers`` so preflight fails closed if the retained
    checkout does not actually contain the exact task ``start_sha``,
    rather than only discovering this later when ``git_worktree_checkout``
    fails mid-replay.
    """
    if not sha or not repo_dir.is_dir():
        return False
    result = subprocess.run(
        ["git", "-C", str(repo_dir), "cat-file", "-e", f"{sha}^{{commit}}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def tsjs_node_version(task_id: str, oracle: dict[str, Any]) -> str:
    """Pinned Node version for a tsjs task.

    Prefers the Oracle's own ``historical_node``; falls back to the recovered
    ``TSJS_MANIFEST`` entry for Oracles published without it (FluidFramework's
    published record carries only coverage/pass_to_pass -- its Node version is
    recovered from the pre-release record it names as its source artifact).
    """
    return str(
        TSJS_NODE_OVERRIDES.get(task_id)
        or oracle.get("historical_node")
        or TSJS_MANIFEST.get(task_id, {}).get("node")
        or ""
    )


def resolve_node_command(
    command: str,
    cwd: Path,
    fallback_bin: Path | None = None,
) -> list[str]:
    parts = shlex.split(command)
    if parts and "/" not in parts[0]:
        local = cwd / "node_modules/.bin" / parts[0]
        if local.is_file():
            parts[0] = str(local)
        elif fallback_bin is not None:
            fallback = fallback_bin / parts[0]
            if fallback.is_file():
                parts[0] = str(fallback)
    return parts


def remove_worktree(repo_source: Path, dest: Path) -> None:
    subprocess.run(
        ["git", "-C", str(repo_source), "worktree", "remove", "--force", str(dest)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    shutil.rmtree(dest, ignore_errors=True)
    subprocess.run(
        ["git", "-C", str(repo_source), "worktree", "prune"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_count(text: str, patterns: tuple[str, ...]) -> int:
    cleaned = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
    for pattern in patterns:
        match = re.search(pattern, cleaned, re.I)
        if match:
            return int(match.group(1))
    return 0


GENERIC_TEST_COUNT_PATTERNS = (
    r"Test Files:?\s+(\d+) passed",
    r"Test Suites:?\s+(\d+) passed",
    r"# pass\s+(\d+)",
    r"TOTAL:\s+(\d+) SUCCESS",
    r"(\d+) passing(?:\s|\(|$)",
    r"test result: ok\.\s+(\d+) passed",
)


def run_subprocess(
    command: list[str],
    *,
    cwd: Path | None,
    log_path: Path,
    env: dict[str, str] | None = None,
    timeout: int = 1800,
) -> tuple[int, float, str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    with open(log_path, "w") as stream:
        try:
            proc = subprocess.run(
                command,
                cwd=cwd,
                env=env,
                stdout=stream,
                stderr=subprocess.STDOUT,
                timeout=timeout,
            )
            return proc.returncode, round(time.time() - started, 3), ""
        except subprocess.TimeoutExpired:
            return 124, round(time.time() - started, 3), f"timeout after {timeout}s"
        except Exception as error:  # pragma: no cover - defensive
            return 125, round(time.time() - started, 3), repr(error)


def model_component(model: str) -> str:
    """Collision-free filesystem component for a ``--model`` value, for
    every scratch/evidence directory this adapter itself owns (the
    ``work_root/<lane>/<model_component>/<task_id>`` disposable scratch
    trees, and ``output/legacy/evidence/<lane>/<model_component>/
    <task_id>/`` hashed logs).

    ``BASE.safe_component(model)`` alone is a many-to-one slug -- it
    collapses every run of disallowed characters (including ``/``) to a
    single ``-``, so e.g. both ``"a/b"`` and ``"a-b"`` collapse to the
    identical slug ``"a-b"`` even though both are valid, distinct model
    IDs per ``validate_model_id``. Two such models would then share (and
    concurrently overwrite or delete) the same scratch workspace and
    hashed evidence subtree. Appending a short SHA-256 suffix of the
    *exact*, pre-slugging model string makes the resulting component
    collision-free: no two distinct model strings can ever produce the
    same ``model_component()`` output (short of a SHA-256 collision).

    Deliberately *not* used for ``staged_generation_path``: the
    read-only ``inputs/models/<safe_component(model)>/...`` convention
    there is owned by ``run_open_model_correctness.py``'s own
    ``prepare``/``finalize`` and must match it byte-for-byte to locate
    staged generation results. That read-only path's own collision risk
    is instead closed by ``load_staged_generation``'s per-row embedded
    ``model`` field cross-check, since we cannot rename a directory we
    do not own.
    """
    slug = BASE.safe_component(model)
    digest = hashlib.sha256(model.encode("utf-8")).hexdigest()[:16]
    return f"{slug}-{digest}"


def evidence_dir(output: Path, lane: str, model: str, task_id: str) -> Path:
    return (
        output
        / "legacy/evidence"
        / lane
        / model_component(model)
        / BASE.safe_component(task_id)
    )


def persist_evidence(
    output: Path,
    lane: str,
    model: str,
    task_id: str,
    scratch: Path,
) -> dict[str, str]:
    """Copy every top-level log file out of a disposable ``scratch``
    replay workspace into a permanent, hashed subtree under ``output``,
    then delete ``scratch`` entirely.

    ``scratch`` holds a full disposable copy of a repo checkout (and, for
    native/rust, its build output) -- potentially gigabytes -- which must
    never linger under ``output`` where ``run_open_model_correctness.py
    finalize``'s ``write_artifact_set`` would otherwise hash it as if it
    were evidence. Only the small text log files
    (``run.log``/``install.log``/``apply-error.log``) are evidence; they
    are copied into ``output/legacy/evidence/<lane>/<model>/<task_id>/``
    (a normal, non-excluded, non-``.lock``/``.new`` subtree that
    ``write_artifact_set`` already walks and hashes) and referenced from
    raw rows as paths *relative to output*, never as absolute paths into
    an ephemeral/external work root. This satisfies the review requirement
    that raw rows never reference unhashed external logs, regardless of
    where ``--work-root`` points.
    """
    destination = evidence_dir(output, lane, model, task_id)
    preserved: dict[str, str] = {}
    if scratch.is_dir():
        destination.mkdir(parents=True, exist_ok=True)
        for log_file in sorted(scratch.glob("*.log")):
            target = destination / log_file.name
            shutil.copy2(log_file, target)
            preserved[log_file.name] = str(target.relative_to(output))
    shutil.rmtree(scratch, ignore_errors=True)
    return preserved


# ---------------------------------------------------------------------------
# Release / plan loading
# ---------------------------------------------------------------------------


def load_release_and_plan(
    tasks_path: Path,
    oracles_path: Path,
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    list[dict[str, Any]],
]:
    tasks, oracles, tasks_by_id, oracles_by_id = BASE.load_release(
        tasks_path, oracles_path
    )
    plan = BASE.build_task_plan(tasks, oracles_by_id)
    return tasks_by_id, oracles_by_id, plan


def select_scope(
    plan: list[dict[str, Any]],
    task_ids: list[str] | None,
) -> list[dict[str, Any]]:
    wanted = set(task_ids) if task_ids else set(TASK_IDS)
    unknown = wanted - set(TASK_IDS)
    if unknown:
        raise ValueError(
            f"--task-id values outside the 14-task reserve scope: "
            f"{sorted(unknown)}"
        )
    rows = [row for row in plan if row["id"] in wanted]
    found = {row["id"] for row in rows}
    missing = wanted - found
    if missing:
        raise ValueError(
            f"task IDs absent from --tasks/--oracles: {sorted(missing)}"
        )
    drift = [
        row["id"]
        for row in rows
        if row["source_kind"] != "published_legacy"
    ]
    if drift:
        raise ValueError(
            "one or more reserve task IDs already have a modern "
            f"lane_results_source and are no longer published_legacy: {drift}"
        )
    lane_drift = [
        row["id"] for row in rows if row["lane"] != EXPECTED_LANE[row["id"]]
    ]
    if lane_drift:
        raise ValueError(
            f"lane classification drifted for: {lane_drift}"
        )
    return rows


# ---------------------------------------------------------------------------
# Per-lane blocker detection (shared by preflight and replay)
# ---------------------------------------------------------------------------


def native_blockers(task_id: str, legacy_root: Path) -> list[str]:
    manifest = NATIVE_MANIFEST[task_id]
    blockers = []
    task_dir = legacy_root / "strict_screen_20260726" / manifest["task_dir"]
    if not task_dir.is_dir():
        blockers.append(f"missing task directory: {task_dir}")
        return blockers
    workspace = task_dir if manifest["workspace"] == "." else task_dir / manifest["workspace"]
    if not workspace.is_dir():
        blockers.append(f"missing workspace subtree: {workspace}")
    for mount in manifest["harness_mounts"]:
        host = task_dir / mount["host"]
        if not host.is_file():
            blockers.append(f"missing harness file: {host}")
    if not docker_image_present(manifest["docker_image"]):
        blockers.append(
            f"missing Docker image: {manifest['docker_image']} "
            "(not loaded locally; build/import must happen out of band, "
            "never fabricated)"
        )
    if not manifest["proven_historical"]:
        blockers.append(
            "harness recipe was never proven end-to-end historically "
            "(every historical candidate patch failed to apply); "
            "encoded from run_native_batch.py CASE_CONFIGS only"
        )
    return blockers


def rust_blockers(
    task_id: str,
    oracle: dict[str, Any],
    legacy_root: Path,
) -> list[str]:
    blockers = []
    try:
        task_dir = artifacts_directory(oracle, legacy_root)
    except ValueError as error:
        return [str(error)]
    if not (task_dir / "start").is_dir():
        blockers.append(f"missing workspace subtree: {task_dir / 'start'}")
    environment = oracle.get("environment") or {}
    image = environment.get("docker_image")
    if not image:
        blockers.append("Oracle is missing environment.docker_image")
    elif not docker_image_present(image):
        blockers.append(f"missing Docker image: {image}")
    cargo_cache = environment.get("cargo_cache")
    if cargo_cache:
        cache_dir = resolve_legacy_relative(cargo_cache, legacy_root)
        if not cache_dir.is_dir():
            blockers.append(
                f"missing recorded cargo cache: {cache_dir} "
                "(environment declares it as required; network fallback "
                "would diverge from the recovered environment and is not "
                "attempted)"
            )
    if not oracle.get("workspace_command"):
        blockers.append("Oracle is missing workspace_command")
    return blockers


def jvm_blockers(
    task_id: str,
    oracle: dict[str, Any],
    legacy_root: Path,
    asset_root: Path | None = None,
) -> list[str]:
    blockers = []
    command = oracle.get("command")
    if not command:
        blockers.append("Oracle is missing command")
        return blockers
    manifest = JVM_MANIFEST.get(task_id)
    if manifest is None:
        blockers.append(f"no recovered jvm replay manifest for {task_id}")
        return blockers
    image = jvm_container(oracle, task_id)
    if not image:
        blockers.append("Oracle is missing environment.container")
    elif not docker_image_present(image):
        blockers.append(f"missing Docker image: {image}")
    repo = oracle.get("repo") or ""
    checkout = repo_checkout_dir(legacy_root, repo)
    if not checkout.is_dir():
        blockers.append(f"missing retained repo checkout: {checkout}")
    cache = jvm_cache(oracle, task_id)
    if cache:
        cache_home = resolve_legacy_relative(cache, legacy_root, asset_root)
        cache_root = cache_home.parent
        if not cache_home.is_dir():
            blockers.append(
                f"missing recorded Gradle cache home: {cache_home} "
                "(environment.cache is required to avoid a network-"
                "dependent dependency resolution divergence)"
            )
        for token in oracle_cache_tokens(command):
            asset = cache_root / token
            if not asset.is_file():
                blockers.append(f"missing /oracle-cache asset: {asset}")
    else:
        blockers.append("Oracle is missing environment.cache")
    if manifest.get("session_tests"):
        try:
            recovery_dir = artifacts_directory(oracle, legacy_root, asset_root)
        except ValueError as error:
            blockers.append(str(error))
        else:
            session_tests = recovery_dir / "session-tests"
            if not session_tests.is_dir():
                blockers.append(
                    "missing retained session behavior tests: "
                    f"{session_tests} (the Oracle scores a session-authored "
                    "oracle.* test that must be installed into the workspace)"
                )
    return blockers


def tsjs_blockers(
    task_id: str,
    oracle: dict[str, Any],
    legacy_root: Path,
    start_sha: str,
    asset_root: Path | None = None,
) -> list[str]:
    blockers = []
    manifest = TSJS_MANIFEST.get(task_id)
    if manifest is None:
        blockers.append(
            f"no recovered tsjs replay manifest for {task_id}: the historical "
            "recovery artifacts that carried this Oracle's executable harness "
            "are not present under --legacy-root or --asset-root"
        )
        return blockers
    repo = oracle.get("repo") or ""
    checkout = repo_checkout_dir(legacy_root, repo)
    if not checkout.is_dir():
        blockers.append(f"missing retained repo checkout: {checkout}")
    elif not start_sha:
        # Real Discourse/Opossum oracles omit start_sha entirely (the
        # exact start commit lives on the task/task-plan, not the
        # Oracle); replay must never fall back to a missing/wrong field,
        # so this fails closed instead.
        blockers.append(
            "task is missing start_sha (tsjs replay requires the exact "
            "task start_sha, since Oracle.start_sha is not reliably "
            "present for this lane)"
        )
    elif not commit_present(checkout, start_sha):
        blockers.append(
            f"retained repo checkout {checkout} does not contain the "
            f"exact task start_sha {start_sha} as a commit (cannot "
            "materialize the correct starting revision)"
        )
    node = tsjs_node_version(task_id, oracle)
    if not node:
        blockers.append("Oracle is missing historical_node")
    elif node_runtime_bin(
        node, legacy_root, asset_root, manifest.get("runtime_root", "legacy")
    ) is None:
        blockers.append(f"missing pinned Node {node} runtime")

    provisioning = manifest["node_modules"]
    if provisioning == "shared_node_tools":
        tools = (
            oracle_recovery_root(legacy_root) / "tsjs/_node_tools/node_modules"
        )
        if not tools.is_dir():
            blockers.append(f"missing retained shared Node tools: {tools}")
    elif provisioning == "pinned":
        if asset_root is None:
            blockers.append(
                "--asset-root is required for a pinned node_modules snapshot"
            )
        else:
            pinned = asset_root / manifest["pinned_node_modules"]
            if not (pinned / ".bin").is_dir() and not pinned.is_dir():
                blockers.append(f"missing pinned node_modules asset: {pinned}")
    elif provisioning == "pnpm_workspace":
        if asset_root is None:
            blockers.append(
                "--asset-root is required for a pinned pnpm workspace store"
            )
        else:
            store = asset_root / "tsjs/pnpm-store"
            if not store.is_dir():
                blockers.append(f"missing pinned pnpm store: {store}")
    else:  # pragma: no cover - defensive
        blockers.append(f"unsupported node_modules provisioning: {provisioning}")

    if manifest.get("session_from_recovery"):
        try:
            recovery_dir = artifacts_directory(oracle, legacy_root, asset_root)
        except ValueError as error:
            blockers.append(str(error))
        else:
            session_dir = recovery_dir / "session"
            if not session_dir.is_dir():
                blockers.append(
                    f"missing recovered session harness directory: {session_dir}"
                )
    return blockers


def task_blockers(
    task_id: str,
    lane: str,
    oracle: dict[str, Any],
    legacy_root: Path,
    start_sha: str,
    asset_root: Path | None = None,
) -> list[str]:
    if lane == "native":
        return native_blockers(task_id, legacy_root)
    if lane == "rust":
        return rust_blockers(task_id, oracle, legacy_root)
    if lane == "jvm":
        return jvm_blockers(task_id, oracle, legacy_root, asset_root)
    if lane == "tsjs":
        return tsjs_blockers(task_id, oracle, legacy_root, start_sha, asset_root)
    return [f"unsupported lane: {lane}"]  # pragma: no cover - defensive


def asset_state(
    task_id: str,
    lane: str,
    oracle: dict[str, Any],
    legacy_root: Path,
    asset_root: Path | None = None,
) -> dict[str, Any]:
    """Identity of the actual replay source trees and other required
    assets a lane's replay recipe depends on under the *current*
    ``--legacy-root``.

    This exists to make ``replay_contract_fingerprint`` sensitive to a
    missing or changed ``--legacy-root`` even when the corresponding
    ``task_blockers()`` text happens to read the same (e.g. an image was
    retagged/rebuilt with the same name, or a source tree/cache blob was
    replaced with different content at the same path). Every lane's
    *actual execution input tree* -- the exact directory each
    ``replay_*`` function copies/checks out from -- is fingerprinted with
    ``directory_identity`` (bounded, content-sensitive, so a same-path
    in-place replacement is detected and a huge cache/checkout is never
    fully read); single required files use ``safe_file_digest``.
    """
    if lane == "native":
        manifest = NATIVE_MANIFEST[task_id]
        task_dir = legacy_root / "strict_screen_20260726" / manifest["task_dir"]
        workspace_source = (
            task_dir if manifest["workspace"] == "."
            else task_dir / manifest["workspace"]
        )
        harness_digests = {
            mount["host"]: safe_file_digest(task_dir / mount["host"])
            for mount in manifest["harness_mounts"]
        }
        return {
            "docker_image_id": docker_image_id(manifest["docker_image"]),
            # the exact tree replay_native copies into scratch and patches
            "workspace": directory_identity(workspace_source),
            "harness_mounts": harness_digests,
        }
    if lane == "rust":
        environment = oracle.get("environment") or {}
        image = environment.get("docker_image")
        cargo_cache = environment.get("cargo_cache")
        try:
            task_dir = artifacts_directory(oracle, legacy_root)
            start_tree = directory_identity(task_dir / "start")
        except ValueError:
            start_tree = None
        return {
            "docker_image_id": docker_image_id(image) if image else None,
            # the exact "start/" tree replay_rust copies into scratch
            "start": start_tree,
            "cargo_cache": (
                directory_identity(resolve_legacy_relative(cargo_cache, legacy_root))
                if cargo_cache
                else None
            ),
        }
    if lane == "jvm":
        image = jvm_container(oracle, task_id)
        cache = jvm_cache(oracle, task_id)
        repo = oracle.get("repo") or ""
        checkout = repo_checkout_dir(legacy_root, repo) if repo else None
        cache_blobs: dict[str, str | None] = {}
        if cache:
            cache_root = resolve_legacy_relative(
                cache, legacy_root, asset_root
            ).parent
            for token in oracle_cache_tokens(oracle.get("command") or ""):
                cache_blobs[token] = safe_file_digest(cache_root / token)
        session_tests = None
        if JVM_MANIFEST.get(task_id, {}).get("session_tests"):
            try:
                session_tests = directory_identity(
                    artifacts_directory(oracle, legacy_root, asset_root)
                    / "session-tests"
                )
            except ValueError:
                session_tests = None
        return {
            "docker_image_id": docker_image_id(image) if image else None,
            # the exact repo checkout replay_jvm materializes the start from
            "repo_checkout": directory_identity(checkout) if checkout else None,
            # the exact /oracle-cache blobs the recorded command requires
            "oracle_cache_blobs": cache_blobs,
            "cache_home": (
                directory_identity(
                    resolve_legacy_relative(cache, legacy_root, asset_root)
                )
                if cache
                else None
            ),
            # the session-authored behavior tests installed into the workspace
            "session_tests": session_tests,
            "manifest": JVM_MANIFEST.get(task_id),
        }
    if lane == "tsjs":
        manifest = TSJS_MANIFEST.get(task_id)
        if manifest is None:
            return {"manifest": None}
        node = tsjs_node_version(task_id, oracle)
        runtime = node_runtime_bin(
            node, legacy_root, asset_root, manifest.get("runtime_root", "legacy")
        )
        repo = oracle.get("repo") or ""
        checkout = repo_checkout_dir(legacy_root, repo) if repo else None
        session_tree = None
        if manifest.get("session_from_recovery"):
            try:
                session_tree = directory_identity(
                    artifacts_directory(oracle, legacy_root, asset_root) / "session"
                )
            except ValueError:
                session_tree = None
        dependencies: dict[str, Any] | None = None
        provisioning = manifest["node_modules"]
        if provisioning == "shared_node_tools":
            dependencies = directory_identity(
                oracle_recovery_root(legacy_root) / "tsjs/_node_tools/node_modules"
            )
        elif provisioning == "pinned" and asset_root is not None:
            dependencies = directory_identity(
                asset_root / manifest["pinned_node_modules"]
            )
        elif provisioning == "pnpm_workspace" and asset_root is not None:
            dependencies = directory_identity(asset_root / "tsjs/pnpm-store")
        return {
            "node_runtime": (
                safe_file_digest(runtime / "node") if runtime else None
            ),
            "node_modules": dependencies,
            # the exact repo checkout replay_tsjs materializes the start from
            "repo_checkout": directory_identity(checkout) if checkout else None,
            # the exact recovered session/ tree copied into the workspace
            "session": session_tree,
            "manifest": manifest,
        }
    return {}  # pragma: no cover - defensive



# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


def cmd_preflight(args: argparse.Namespace) -> int:
    legacy_root = Path(args.legacy_root).expanduser().resolve()
    asset_root = (
        Path(args.asset_root).expanduser().resolve() if args.asset_root else None
    )
    tasks_path = Path(args.tasks).resolve()
    oracles_path = Path(args.oracles).resolve()
    tasks_by_id, oracles_by_id, plan = load_release_and_plan(
        tasks_path, oracles_path
    )
    rows = select_scope(plan, args.task_id or None)

    report_rows = []
    ready = 0
    for row in rows:
        task_id = row["id"]
        oracle = oracles_by_id[task_id]
        blockers = task_blockers(
            task_id, row["lane"], oracle, legacy_root, row["start_sha"], asset_root
        )
        status = "ready" if not blockers else "blocked"
        if status == "ready":
            ready += 1
        report_rows.append(
            {
                "id": task_id,
                "repo": row["repo"],
                "lane": row["lane"],
                "start_sha": row["start_sha"],
                "status": status,
                "blockers": blockers,
            }
        )
    report = {
        "schema_version": "reviewbench-legacy-reserve-preflight/v1",
        "created_at": BASE.utc_now(),
        "legacy_root": str(legacy_root),
        "asset_root": str(asset_root) if asset_root else None,
        "tasks_sha256": BASE.sha256(tasks_path),
        "oracles_sha256": BASE.sha256(oracles_path),
        "reserve_task_count": len(TASK_IDS),
        "scoped_task_count": len(rows),
        "ready_task_count": ready,
        "blocked_task_count": len(rows) - ready,
        "tasks": sorted(report_rows, key=lambda entry: entry["id"]),
    }
    if args.output:
        output = Path(args.output).resolve()
        output.mkdir(parents=True, exist_ok=True)
        BASE.atomic_json(output / "legacy-reserve-preflight.json", report)
    print(json.dumps(report, indent=2))
    if report["blocked_task_count"] and not args.allow_blocked:
        return 1
    return 0


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


def staged_generation_path(output: Path, model: str) -> Path:
    return (
        output
        / "inputs/models"
        / BASE.safe_component(model)
        / "generation_results.normalized.jsonl"
    )


def load_staged_generation(
    output: Path,
    model: str,
    task_ids: set[str],
) -> dict[str, dict[str, Any]]:
    path = staged_generation_path(output, model)
    if not path.is_file():
        raise ValueError(
            f"staged generation results not found for {model!r}: {path}. "
            "Run `run_open_model_correctness.py prepare "
            "--allow-partial-modern` first."
        )
    rows = BASE.read_jsonl(path)
    by_id: dict[str, dict[str, Any]] = {}
    mislabeled: list[str] = []
    for row in rows:
        row_id = row.get("id")
        if row_id not in task_ids:
            continue
        # ``staged_generation_path`` keys the on-disk directory by
        # ``BASE.safe_component(model)``, which is a many-to-one slug (it
        # collapses disallowed characters to ``-``); two distinct model
        # IDs can collide onto the same directory. Trusting the directory
        # alone would let a mismatched/mislabeled generation file be
        # silently consumed under the wrong model name, so every row's
        # own embedded ``model`` field must also match the requested
        # ``--model`` exactly (code-review finding: require every staged
        # row's embedded model to match, to prevent safe_component slug
        # collisions/mislabeling).
        if row.get("model") != model:
            mislabeled.append(f"{row_id}: embedded model {row.get('model')!r}")
            continue
        by_id[row_id] = row
    if mislabeled:
        raise ValueError(
            f"staged generation results at {path} contain rows whose "
            f"embedded model does not match the requested --model {model!r} "
            f"(possible safe_component slug collision or mislabeled "
            f"staging directory): {mislabeled}"
        )
    missing = task_ids - set(by_id)
    if missing:
        raise ValueError(
            f"staged generation results for {model!r} are missing IDs: "
            f"{sorted(missing)}"
        )
    return by_id


def existing_raw_rows(legacy_dir: Path, lane: str) -> dict[tuple[str, str], dict[str, Any]]:
    path = legacy_dir / f"{lane}.jsonl"
    if not path.is_file():
        return {}
    rows = BASE.read_jsonl(path)
    return {(row["id"], row["model"]): row for row in rows}


def append_raw_row(
    legacy_dir: Path,
    lane: str,
    row: dict[str, Any],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Merge ``row`` into ``<legacy_dir>/<lane>.jsonl`` safely under
    concurrent writers.

    The read-merge-write critical section runs entirely inside
    ``lane_lock``, and *rereads the file from disk* rather than trusting
    any snapshot the caller may already hold -- this is what makes
    concurrent ``replay`` invocations for the same lane (different
    models, or several task IDs racing within one lane) safe: whichever
    process's row was flushed last is always merged on top of every
    other process's already-flushed rows, never on top of a stale
    in-memory copy that would silently drop them. ``BASE.atomic_jsonl``
    additionally writes to a PID-suffixed temp file before ``os.replace``,
    so even without the lock there would be no cross-process temp-file
    collisions; the lock exists specifically to prevent *lost rows*.
    """
    key = (row["id"], row["model"])
    with lane_lock(legacy_dir, lane):
        existing = existing_raw_rows(legacy_dir, lane)
        existing[key] = row
        ordered = sorted(
            existing.values(), key=lambda entry: (entry["id"], entry["model"])
        )
        BASE.atomic_jsonl(legacy_dir / f"{lane}.jsonl", ordered)
        return existing


def retract_raw_row(
    legacy_dir: Path,
    lane: str,
    task_id: str,
    model: str,
) -> bool:
    """Remove any previously-recorded row for ``(task_id, model)`` from
    ``<legacy_dir>/<lane>.jsonl``.

    Used when a task that previously had a recorded row (necessarily
    written while its required assets were present, since blocked tasks
    never reach ``append_raw_row``) is discovered on a later invocation
    to be blocked again -- e.g. ``--legacy-root`` was pointed elsewhere,
    or a required asset was removed/replaced. Resume logic alone (not
    treating the row as fresh) is not enough to satisfy "an old row
    cannot survive": without this, the stale successful row would still
    be physically present in the lane file for a later ``finalize`` to
    read as if it were still valid. Uses the identical lock+reread+write
    pattern as ``append_raw_row`` so it is safe against concurrent
    writers too. Returns ``True`` if a row was actually removed.
    """
    key = (task_id, model)
    with lane_lock(legacy_dir, lane):
        existing = existing_raw_rows(legacy_dir, lane)
        if key not in existing:
            return False
        del existing[key]
        ordered = sorted(
            existing.values(), key=lambda entry: (entry["id"], entry["model"])
        )
        BASE.atomic_jsonl(legacy_dir / f"{lane}.jsonl", ordered)
        return True


def missing_row(
    task_id: str,
    model: str,
    lane: str,
    start_sha: str,
    task: dict[str, Any],
    oracle: dict[str, Any],
    generation: dict[str, Any],
    legacy_root: Path,
    blockers: list[str],
    asset_root: Path | None = None,
) -> dict[str, Any]:
    row = {
        "id": task_id,
        "model": model,
        "lane": lane,
        "source_kind": "published_legacy",
        "start_sha": start_sha,
        "status": CANDIDATE_MISSING,
        "candidate_result": CANDIDATE_MISSING,
        "correctness_ok": None,
        "patch_apply": False,
        "patch_sha256": generation.get("patch_sha256"),
        "scope_ok": None,
        "reserve": "20260728",
    }
    row.update(
        row_provenance_fields(
            task, oracle, task_id, lane, generation, legacy_root, blockers,
            asset_root,
        )
    )
    return row



def replay_native(
    task_id: str,
    model: str,
    task: dict[str, Any],
    oracle: dict[str, Any],
    generation: dict[str, Any],
    legacy_root: Path,
    work_root: Path,
    output: Path,
    timeout: int,
) -> dict[str, Any]:
    manifest = NATIVE_MANIFEST[task_id]
    task_dir = legacy_root / "strict_screen_20260726" / manifest["task_dir"]
    scratch = work_root / "native" / model_component(model) / task_id
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)
    provenance = row_provenance_fields(
        task, oracle, task_id, "native", generation, legacy_root, []
    )

    workspace_source = task_dir if manifest["workspace"] == "." else task_dir / manifest["workspace"]
    workspace_scratch = scratch / "workspace"
    shutil.copytree(workspace_source, workspace_scratch, symlinks=True)
    patch_target = workspace_scratch if manifest["workspace"] == "." else workspace_scratch

    patch_path = Path(generation["patch_file"])
    patch_workdir = (
        workspace_scratch / "start" if manifest["workspace"] == "." else workspace_scratch
    )
    ok, detail = apply_patch(patch_workdir, patch_path)
    if not ok:
        (scratch / "apply-error.log").write_text(detail, encoding="utf-8")
        evidence = persist_evidence(output, "native", model, task_id, scratch)
        row = {
            "id": task_id,
            "model": model,
            "lane": "native",
            "source_kind": "published_legacy",
            "start_sha": oracle["start_sha"],
            "patch_sha256": generation.get("patch_sha256"),
            "status": "apply_error",
            "candidate_result": "apply_error",
            "correctness_ok": False,
            "patch_apply": False,
            "scope_ok": None,
            "error": detail[-4000:],
            "error_log": evidence.get("apply-error.log"),
        }
        row.update(provenance)
        return row
    patch_text = patch_path.read_text(errors="replace")
    changed = patch_changed_paths(patch_text)
    scope_ok = scope_is_safe(changed)

    mounts = ["-v", f"{workspace_scratch}:{manifest['workspace_mount']}"]
    for mount in manifest["harness_mounts"]:
        host = task_dir / mount["host"]
        mounts += ["-v", f"{host}:{mount['container']}:ro"]
    docker_cmd = [
        "docker", "run", "--rm",
        *mounts,
        "-w", manifest["workdir"],
        manifest["docker_image"],
        "sh", "-lc", manifest["shell_command"],
    ]
    log_path = scratch / "run.log"
    exit_code, duration, error = run_subprocess(
        docker_cmd, cwd=None, log_path=log_path, timeout=timeout
    )
    correctness_ok = exit_code == 0
    evidence = persist_evidence(output, "native", model, task_id, scratch)
    row = {
        "id": task_id,
        "model": model,
        "lane": "native",
        "source_kind": "published_legacy",
        "start_sha": oracle["start_sha"],
        "patch_sha256": generation.get("patch_sha256"),
        "status": "pass" if correctness_ok else "fail",
        "candidate_result": "pass" if correctness_ok else "fail",
        "correctness_ok": correctness_ok,
        "patch_apply": True,
        "scope_ok": scope_ok,
        "changed_lines": len(changed),
        "exit_code": exit_code,
        "duration_seconds": duration,
        "error": error,
        "log": evidence.get("run.log"),
        "pass_to_pass_total": BASE.pass_to_pass_count(oracle.get("pass_to_pass")),
        "pass_to_pass_passed": (
            BASE.pass_to_pass_count(oracle.get("pass_to_pass")) if correctness_ok else 0
        ),
        "failed_tests": [] if correctness_ok else ["oracle-run"],
    }
    row.update(provenance)
    return row


def replay_rust(
    task_id: str,
    model: str,
    task: dict[str, Any],
    oracle: dict[str, Any],
    generation: dict[str, Any],
    legacy_root: Path,
    work_root: Path,
    output: Path,
    timeout: int,
) -> dict[str, Any]:
    task_dir = artifacts_directory(oracle, legacy_root)
    environment = oracle.get("environment") or {}
    scratch = work_root / "rust" / model_component(model) / task_id
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)
    provenance = row_provenance_fields(
        task, oracle, task_id, "rust", generation, legacy_root, []
    )
    workspace_scratch = scratch / "workspace"
    shutil.copytree(task_dir / "start", workspace_scratch, symlinks=True)

    patch_path = Path(generation["patch_file"])
    ok, detail = apply_patch(workspace_scratch, patch_path)
    if not ok:
        (scratch / "apply-error.log").write_text(detail, encoding="utf-8")
        evidence = persist_evidence(output, "rust", model, task_id, scratch)
        row = {
            "id": task_id,
            "model": model,
            "lane": "rust",
            "source_kind": "published_legacy",
            "start_sha": oracle["start_sha"],
            "patch_sha256": generation.get("patch_sha256"),
            "status": "apply_error",
            "correctness_ok": False,
            "patch_apply": False,
            "scope_ok": None,
            "error": detail[-4000:],
            "error_log": evidence.get("apply-error.log"),
        }
        row.update(provenance)
        return row
    patch_text = patch_path.read_text(errors="replace")
    changed = patch_changed_paths(patch_text)
    scope_ok = scope_is_safe(changed)

    cargo_cache = environment.get("cargo_cache")
    mounts = ["-v", f"{workspace_scratch}:/work"]
    if cargo_cache:
        cache_dir = resolve_legacy_relative(cargo_cache, legacy_root)
        mounts += ["-v", f"{cache_dir}:/cargo-cache", "-e", "CARGO_HOME=/cargo-cache"]
    docker_cmd = [
        "docker", "run", "--rm", "--network=host",
        *mounts, "-w", "/work",
        environment["docker_image"],
        "sh", "-lc", oracle["workspace_command"],
    ]
    log_path = scratch / "run.log"
    exit_code, duration, error = run_subprocess(
        docker_cmd, cwd=None, log_path=log_path, timeout=timeout
    )
    text = log_path.read_text(errors="replace") if log_path.is_file() else ""
    passed = test_count(text, GENERIC_TEST_COUNT_PATTERNS)
    expected = BASE.pass_to_pass_count(oracle.get("pass_to_pass"))
    correctness_ok = exit_code == 0 and (not expected or passed >= expected)
    evidence = persist_evidence(output, "rust", model, task_id, scratch)
    row = {
        "id": task_id,
        "model": model,
        "lane": "rust",
        "source_kind": "published_legacy",
        "start_sha": oracle["start_sha"],
        "patch_sha256": generation.get("patch_sha256"),
        "status": "pass" if correctness_ok else "fail",
        "correctness_ok": correctness_ok,
        "patch_apply": True,
        "scope_ok": scope_ok,
        "changed_lines": len(changed),
        "exit_code": exit_code,
        "duration_seconds": duration,
        "error": error,
        "log": evidence.get("run.log"),
        "pass_to_pass_total": expected,
        "pass_to_pass_passed": passed,
        "failed_tests": [] if correctness_ok else ["cargo-test"],
    }
    row.update(provenance)
    return row


def jvm_test_count(workspace: Path, globs: tuple[str, ...]) -> tuple[int, int]:
    """Count executed/failed tests from the build's own JUnit XML reports.

    Mirrors ``execute.py``'s ``test_count`` (which scored the historical
    evidence off the very same focused report globs) so a "green" exit code
    that executed zero tests can never be scored as a pass.
    """
    total = failed = 0
    for pattern in globs:
        for report in workspace.glob(pattern):
            try:
                root = ElementTree.parse(report).getroot()
            except Exception:  # pragma: no cover - malformed report
                continue
            suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
            for suite in suites:
                total += int(suite.attrib.get("tests", 0) or 0)
                failed += int(suite.attrib.get("failures", 0) or 0)
                failed += int(suite.attrib.get("errors", 0) or 0)
    return total, failed


def jvm_docker_command(
    oracle: dict[str, Any],
    task_id: str,
    legacy_root: Path,
    asset_root: Path | None,
    workspace: Path,
    task_dir: Path,
    shell_command: str,
    offline: bool,
) -> list[str]:
    """Build the historical JVM Oracle container invocation.

    Reproduces ``execute.py``'s docker recipe: the workspace at /workspace,
    a per-replay /task scratch (the Oracle commands write
    ``/task/start.exec``, so it must exist and be writable or the JaCoCo
    agent aborts the JVM), the /oracle-cache asset directory, the pinned
    cache home at /cachehome, and the exact historical per-task
    ``GRADLE_USER_HOME=/cachehome/gradle-task-<index>`` partition. Note that
    ``CI`` is deliberately *not* set: the historical harness did not set it,
    and Solr's build turns error-prone's ``-Werror`` on inside CI, which
    fails the unpatched start compile outright.
    """
    manifest = JVM_MANIFEST[task_id]
    cache_home = resolve_legacy_relative(
        jvm_cache(oracle, task_id), legacy_root, asset_root
    )
    cache_root = cache_home.parent
    passwd = cache_root / "container.passwd"
    if not passwd.is_file():
        passwd.parent.mkdir(parents=True, exist_ok=True)
        passwd.write_text(
            "root:x:0:0:root:/root:/bin/bash\n"
            f"oracle:x:{os.getuid()}:{os.getgid()}:Oracle:/cachehome:/bin/bash\n",
            encoding="utf-8",
        )
    command = [
        "docker", "run", "--rm",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-e", "HOME=/cachehome",
        "-e", f"GRADLE_USER_HOME=/cachehome/gradle-task-{manifest['cache_index']}",
        "-e", "MAVEN_CONFIG=/cachehome/.m2",
        "-e", "TERM=dumb",
    ]
    if offline:
        # Scored replays are executed with no network at all: every artifact
        # must already be in the pinned, hashed cache home. This is what makes
        # the replay reproducible rather than dependent on live repositories.
        # The single exception is an Oracle whose behavior anchor *is* network
        # behavior; it keeps a network namespace but pins its package manager
        # into offline mode so dependency resolution stays cache-only.
        if manifest.get("network") == "bridge":
            command += ["--hostname", "oracle", "--add-host", "oracle:127.0.0.1"]
        else:
            command += ["--network", "none"]
        for key, value in (manifest.get("offline_env") or {}).items():
            command += ["-e", f"{key}={value}"]
    else:
        mirror = os.environ.get("ORACLE_JCENTER_MIRROR", "")
        if mirror:
            command += ["-e", f"ORACLE_JCENTER_MIRROR={mirror}"]
    command += [
        "-v", f"{workspace}:/workspace",
        "-v", f"{task_dir}:/task",
        "-v", f"{cache_root}:/oracle-cache",
        "-v", f"{cache_home}:/cachehome",
        "-v", f"{passwd}:/etc/passwd:ro",
        "-w", "/workspace",
        jvm_container(oracle, task_id),
        "bash", "-lc", shell_command,
    ]
    return command


def install_pinned_gradle_wrapper(
    workspace: Path,
    legacy_root: Path,
    asset_root: Path | None,
    cache_home: Path,
) -> str | None:
    """Install the repository's own Gradle wrapper jar from the pinned store.

    Solr's checked-in ``gradlew`` fetches ``gradle-wrapper.jar`` from
    raw.githubusercontent.com when it is absent, which a network-isolated
    replay cannot do -- and failing there would be scored as a model failure.
    The jar is pinned as a durable asset keyed by *the repository's own*
    recorded ``gradle-wrapper.jar.sha256``, and the pinned bytes are verified
    against that recorded digest before being installed, so this can only ever
    supply exactly what the checked-out revision itself demands.
    """
    marker = workspace / "gradle/wrapper/gradle-wrapper.jar.sha256"
    jar = workspace / "gradle/wrapper/gradle-wrapper.jar"
    if jar.is_file() or not marker.is_file():
        return None
    expected = marker.read_text(encoding="utf-8", errors="replace").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        return None
    pinned = cache_home.parent / "gradle-wrapper" / f"{expected}.jar"
    if not pinned.is_file():
        return None
    digest = hashlib.sha256(pinned.read_bytes()).hexdigest()
    if digest != expected:
        return None
    jar.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(pinned, jar)
    return expected


def replay_jvm(
    task_id: str,
    model: str,
    task: dict[str, Any],
    oracle: dict[str, Any],
    generation: dict[str, Any],
    legacy_root: Path,
    work_root: Path,
    output: Path,
    timeout: int,
    asset_root: Path | None = None,
    offline: bool = True,
) -> dict[str, Any]:
    manifest = JVM_MANIFEST[task_id]
    repo = oracle["repo"]
    repo_source = repo_checkout_dir(legacy_root, repo)
    start_sha = oracle.get("start_sha") or task["start_sha"]
    scratch = work_root / "jvm" / model_component(model) / task_id
    workspace_scratch = scratch / "workspace"
    task_scratch = scratch / "task"
    provenance = row_provenance_fields(
        task, oracle, task_id, "jvm", generation, legacy_root, [], asset_root
    )
    shutil.rmtree(scratch, ignore_errors=True)
    task_scratch.mkdir(parents=True, exist_ok=True)
    materialize_start(repo_source, start_sha, workspace_scratch)
    result: dict[str, Any]
    log_kind = "run.log"
    try:
        if manifest.get("session_tests"):
            session_tests = (
                artifacts_directory(oracle, legacy_root, asset_root) / "session-tests"
            )
            shutil.copytree(session_tests, workspace_scratch, dirs_exist_ok=True)

        prefetched = []
        for entry in manifest.get("prefetch") or ():
            source = (
                resolve_legacy_relative(
                    jvm_cache(oracle, task_id), legacy_root, asset_root
                ).parent
                / entry["asset"]
            )
            if not source.is_file():
                continue
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            if digest != entry["sha256"]:
                continue
            destination = workspace_scratch / entry["workspace_path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            prefetched.append(entry["workspace_path"])

        wrapper_sha256 = None
        if manifest.get("needs_gradle_wrapper_jar"):
            wrapper_sha256 = install_pinned_gradle_wrapper(
                workspace_scratch,
                legacy_root,
                asset_root,
                resolve_legacy_relative(
                    jvm_cache(oracle, task_id), legacy_root, asset_root
                ),
            )

        patch_path = Path(generation["patch_file"])
        ok, detail = apply_patch(workspace_scratch, patch_path)
        if not ok:
            (scratch / "apply-error.log").write_text(detail, encoding="utf-8")
            log_kind = "apply-error.log"
            result = {
                "id": task_id,
                "model": model,
                "lane": "jvm",
                "source_kind": "published_legacy",
                "start_sha": start_sha,
                "patch_sha256": generation.get("patch_sha256"),
                "status": "apply_error",
                "candidate_result": "apply_error",
                "correctness_ok": False,
                "patch_apply": False,
                "scope_ok": None,
                "error": detail[-4000:],
            }
        else:
            patch_text = patch_path.read_text(errors="replace")
            changed = patch_changed_paths(patch_text)
            scope_ok = scope_is_safe(changed)

            prime_command = manifest.get("prime_command")
            if prime_command:
                run_subprocess(
                    jvm_docker_command(
                        oracle, task_id, legacy_root, asset_root,
                        workspace_scratch, task_scratch, prime_command, offline,
                    ),
                    cwd=None,
                    log_path=scratch / "prime.log",
                    timeout=timeout,
                )
            log_path = scratch / "run.log"
            exit_code, duration, error = run_subprocess(
                jvm_docker_command(
                    oracle, task_id, legacy_root, asset_root,
                    workspace_scratch, task_scratch, str(oracle["command"]), offline,
                ),
                cwd=None,
                log_path=log_path,
                timeout=timeout,
            )
            executed, failures = jvm_test_count(
                workspace_scratch, manifest["test_results_globs"]
            )
            expected = (
                BASE.pass_to_pass_count(oracle.get("pass_to_pass"))
                or manifest["expected_tests"]
            )
            # A zero-test "success" is never a pass: the historical evidence
            # recorded a concrete executed-test count for every one of these
            # Oracles, so an empty run means the harness did not actually
            # execute the behavior anchor.
            correctness_ok = (
                exit_code == 0 and failures == 0 and executed >= expected
            )
            result = {
                "id": task_id,
                "model": model,
                "lane": "jvm",
                "source_kind": "published_legacy",
                "start_sha": start_sha,
                "patch_sha256": generation.get("patch_sha256"),
                "status": "pass" if correctness_ok else "fail",
                "candidate_result": "pass" if correctness_ok else "fail",
                "correctness_ok": correctness_ok,
                "patch_apply": True,
                "scope_ok": scope_ok,
                "changed_lines": len(changed),
                "exit_code": exit_code,
                "duration_seconds": duration,
                "error": error,
                "pass_to_pass_total": expected,
                "pass_to_pass_passed": max(executed - failures, 0),
                "failed_tests": [] if correctness_ok else ["gradle-test"],
                "offline_replay": offline,
                "container_network": (
                    manifest.get("network", "none") if offline else "bridge"
                ),
                "pinned_gradle_wrapper_sha256": wrapper_sha256,
                "prefetched_assets": prefetched,
            }
    finally:
        shutil.rmtree(workspace_scratch, ignore_errors=True)
    evidence = persist_evidence(output, "jvm", model, task_id, scratch)
    if log_kind == "apply-error.log":
        result["error_log"] = evidence.get("apply-error.log")
    else:
        result["log"] = evidence.get("run.log")
        if evidence.get("prime.log"):
            result["prime_log"] = evidence["prime.log"]
    result.update(provenance)
    return result


def provision_tsjs_dependencies(
    task_id: str,
    manifest: dict[str, Any],
    legacy_root: Path,
    asset_root: Path | None,
    workspace: Path,
    env: dict[str, str],
    scratch: Path,
    timeout: int,
    offline: bool = True,
) -> tuple[bool, str]:
    """Make the historical dependency tree available without re-resolving it.

    Returns ``(ok, detail)``; ``ok is False`` is an *infrastructure* failure
    and is never scored as a candidate failure.
    """
    provisioning = manifest["node_modules"]
    destination = workspace / "node_modules"
    if provisioning == "shared_node_tools":
        # The retained historical workspace
        # (oracle_recovery/tsjs/_work/discourse__discourse) still carries this
        # exact symlink; without it the session Vitest config cannot resolve
        # "vitest/config" and every candidate fails for infrastructure reasons.
        shutil.rmtree(destination, ignore_errors=True)
        destination.symlink_to(
            oracle_recovery_root(legacy_root) / "tsjs/_node_tools/node_modules"
        )
        return True, "symlinked retained shared node tools"
    if provisioning == "pinned":
        assert asset_root is not None
        link_tree(asset_root / manifest["pinned_node_modules"], destination)
        return True, "hardlink-copied pinned node_modules snapshot"
    if provisioning == "pnpm_workspace":
        assert asset_root is not None
        store = asset_root / "tsjs/pnpm-store"
        package_manager = manifest["package_manager"]
        install = shlex.split(manifest["install_command"])
        # A scored replay must be a pure function of the pinned store, so the
        # package manager is put in offline mode rather than trusted to happen
        # to hit only cached metadata.
        offline_flags = ["--offline"] if offline else []
        exit_code, _, detail = run_subprocess(
            ["corepack", package_manager, *install[1:],
             f"--config.store-dir={store}", *offline_flags],
            cwd=workspace,
            log_path=scratch / "install.log",
            env=env,
            timeout=timeout,
        )
        if exit_code != 0:
            return False, f"install_command failed: {detail}"
        build = manifest.get("build_command")
        if build:
            build_parts = shlex.split(build)
            exit_code, _, detail = run_subprocess(
                ["corepack", package_manager, *build_parts[1:]],
                cwd=workspace,
                log_path=scratch / "build.log",
                env=env,
                timeout=timeout,
            )
            if exit_code != 0:
                return False, f"build_command failed: {detail}"
        return True, "installed from pinned pnpm store"
    return False, f"unsupported node_modules provisioning: {provisioning}"


def replay_tsjs(
    task_id: str,
    model: str,
    task: dict[str, Any],
    oracle: dict[str, Any],
    generation: dict[str, Any],
    legacy_root: Path,
    work_root: Path,
    output: Path,
    timeout: int,
    asset_root: Path | None = None,
    offline: bool = True,
) -> dict[str, Any]:
    manifest = TSJS_MANIFEST[task_id]
    repo = oracle["repo"]
    repo_source = repo_checkout_dir(legacy_root, repo)
    scratch = work_root / "tsjs" / model_component(model) / task_id
    workspace_scratch = scratch / "workspace"
    provenance = row_provenance_fields(
        task, oracle, task_id, "tsjs", generation, legacy_root, [], asset_root
    )
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)
    materialize_start(repo_source, task["start_sha"], workspace_scratch)
    result: dict[str, Any]
    log_kind = "run.log"
    try:
        if manifest.get("session_from_recovery"):
            session_dir = (
                artifacts_directory(oracle, legacy_root, asset_root) / "session"
            )
            destination = workspace_scratch / "session"
            shutil.rmtree(destination, ignore_errors=True)
            shutil.copytree(session_dir, destination, symlinks=True)

        patch_path = Path(generation["patch_file"])
        ok, detail = apply_patch(workspace_scratch, patch_path)
        if not ok:
            (scratch / "apply-error.log").write_text(detail, encoding="utf-8")
            log_kind = "apply-error.log"
            result = {
                "id": task_id,
                "model": model,
                "lane": "tsjs",
                "source_kind": "published_legacy",
                "start_sha": task["start_sha"],
                "patch_sha256": generation.get("patch_sha256"),
                "status": "apply_error",
                "candidate_result": "apply_error",
                "correctness_ok": False,
                "patch_apply": False,
                "scope_ok": None,
                "error": detail[-4000:],
            }
        else:
            patch_text = patch_path.read_text(errors="replace")
            changed = patch_changed_paths(patch_text)
            scope_ok = scope_is_safe(changed)

            node = tsjs_node_version(task_id, oracle)
            runtime_bin = node_runtime_bin(
                node, legacy_root, asset_root, manifest.get("runtime_root", "legacy")
            )
            env = dict(os.environ)
            env["PATH"] = f"{runtime_bin}:{env.get('PATH', '')}"
            env["CI"] = "true"
            env["HUSKY"] = "0"
            env["NOYARNPOSTINSTALL"] = "1"
            env["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] = "1"
            env["PUPPETEER_SKIP_DOWNLOAD"] = "true"
            env["COREPACK_ENABLE_DOWNLOAD_PROMPT"] = "0"
            env.update(
                {str(key): str(value) for key, value in (manifest.get("test_env") or {}).items()}
            )

            provisioned, provision_detail = provision_tsjs_dependencies(
                task_id, manifest, legacy_root, asset_root, workspace_scratch,
                env, scratch, timeout, offline,
            )
            if not provisioned:
                log_kind = "install.log"
                result = {
                    "id": task_id,
                    "model": model,
                    "lane": "tsjs",
                    "source_kind": "published_legacy",
                    "start_sha": task["start_sha"],
                    "patch_sha256": generation.get("patch_sha256"),
                    "status": INFRA_RETRY,
                    "candidate_result": INFRA_RETRY,
                    "correctness_ok": None,
                    "patch_apply": True,
                    "scope_ok": scope_ok,
                    "error": provision_detail,
                }
            else:
                workspace = manifest.get("workspace") or "."
                cwd = (
                    workspace_scratch
                    if workspace == "."
                    else workspace_scratch / workspace
                )
                run_log = scratch / "run.log"
                exit_code, duration, error = run_subprocess(
                    resolve_node_command(
                        str(manifest["test_command"]),
                        cwd,
                        oracle_recovery_root(legacy_root)
                        / "tsjs/_node_tools/node_modules/.bin",
                    ),
                    cwd=cwd,
                    log_path=run_log,
                    env=env,
                    timeout=timeout,
                )
                text = run_log.read_text(errors="replace") if run_log.is_file() else ""
                passed = test_count(
                    text,
                    tuple(manifest.get("test_count_patterns") or ())
                    or GENERIC_TEST_COUNT_PATTERNS,
                )
                expected = manifest["expected_tests"]
                signature = manifest.get("expected_signature")
                signature_ok = not signature or signature in re.sub(
                    r"\x1b\[[0-9;]*[A-Za-z]", "", text
                )
                correctness_ok = (
                    exit_code == 0 and passed >= expected and signature_ok
                )
                result = {
                    "id": task_id,
                    "model": model,
                    "lane": "tsjs",
                    "source_kind": "published_legacy",
                    "start_sha": task["start_sha"],
                    "patch_sha256": generation.get("patch_sha256"),
                    "status": "pass" if correctness_ok else "fail",
                    "candidate_result": "pass" if correctness_ok else "fail",
                    "correctness_ok": correctness_ok,
                    "patch_apply": True,
                    "scope_ok": scope_ok,
                    "changed_lines": len(changed),
                    "exit_code": exit_code,
                    "duration_seconds": duration,
                    "error": error,
                    "pass_to_pass_total": expected,
                    "pass_to_pass_passed": passed,
                    "failed_tests": [] if correctness_ok else ["tsjs-test"],
                    "dependency_provisioning": provision_detail,
                }
    finally:
        # node_modules may be a symlink into a retained shared tree; never
        # follow it while cleaning up the disposable workspace.
        link = workspace_scratch / "node_modules"
        if link.is_symlink():
            link.unlink()
        shutil.rmtree(workspace_scratch, ignore_errors=True)
    evidence = persist_evidence(output, "tsjs", model, task_id, scratch)
    if log_kind == "apply-error.log":
        result["error_log"] = evidence.get("apply-error.log")
    else:
        result["log"] = evidence.get(log_kind)
        for extra in ("install.log", "build.log"):
            if extra != log_kind and evidence.get(extra):
                result[extra.replace(".", "_")] = evidence[extra]
    result.update(provenance)
    return result


REPLAY_FUNCTIONS = {
    "native": replay_native,
    "rust": replay_rust,
    "jvm": replay_jvm,
    "tsjs": replay_tsjs,
}


def cmd_verify_contracts(args: argparse.Namespace) -> int:
    """Replay each recovered Oracle at its exact *unpatched* start commit and
    require the frozen historical evidence to be reproduced.

    This is what makes the recovery auditable rather than asserted: an Oracle
    whose pinned assets were reconstructed (the JVM ``/oracle-cache``
    init script and Gradle/Maven cache home, the pinned ``node_modules``
    snapshots, the pinned Node/pnpm runtimes) only becomes runner-ready once a
    fresh, network-isolated start replay reproduces the published
    ``pass_to_pass``/signature evidence. It runs the *same* code path as a
    scored replay, with an empty candidate patch, so nothing can be validated
    through a path that scoring does not use.
    """
    legacy_root = Path(args.legacy_root).expanduser().resolve()
    asset_root = (
        Path(args.asset_root).expanduser().resolve() if args.asset_root else None
    )
    tasks_path = Path(args.tasks).resolve()
    oracles_path = Path(args.oracles).resolve()
    output = Path(args.output).resolve()
    tasks_by_id, oracles_by_id, plan = load_release_and_plan(tasks_path, oracles_path)
    rows = [
        row
        for row in select_scope(plan, args.task_id or None)
        if row["lane"] in ("jvm", "tsjs")
    ]

    work_root = (
        Path(args.work_root).resolve() if args.work_root else output / "legacy/verify-work"
    )
    work_root.mkdir(parents=True, exist_ok=True)
    empty_patch = work_root / "unpatched-start.patch"
    empty_patch.write_text("", encoding="utf-8")
    generation = {
        "patch_file": str(empty_patch),
        "patch_sha256": BASE.sha256(empty_patch),
        "apply_ok": True,
    }

    results = []
    verified = 0
    for row in rows:
        task_id = row["id"]
        lane = row["lane"]
        oracle = oracles_by_id[task_id]
        task = tasks_by_id[task_id]
        blockers = task_blockers(
            task_id, lane, oracle, legacy_root, task["start_sha"], asset_root
        )
        if blockers:
            results.append(
                {
                    "id": task_id,
                    "lane": lane,
                    "status": "blocked",
                    "blockers": blockers,
                }
            )
            continue
        manifest = (JVM_MANIFEST if lane == "jvm" else TSJS_MANIFEST)[task_id]
        expected = manifest["expected_tests"]
        raw = REPLAY_FUNCTIONS[lane](
            task_id, "unpatched-start", task, oracle, generation, legacy_root,
            work_root, output, args.timeout,
            asset_root=asset_root, offline=not args.allow_network,
        )
        observed = raw.get("pass_to_pass_passed")
        ok = bool(raw.get("correctness_ok")) and (observed or 0) >= expected
        if ok:
            verified += 1
        results.append(
            {
                "id": task_id,
                "lane": lane,
                "status": "verified" if ok else "evidence_mismatch",
                "start_sha": raw.get("start_sha"),
                "exit_code": raw.get("exit_code"),
                "expected_tests": expected,
                "observed_tests": observed,
                "duration_seconds": raw.get("duration_seconds"),
                "log": raw.get("log"),
                "offline": not args.allow_network,
            }
        )

    report = {
        "schema_version": "reviewbench-legacy-reserve-contract-verification/v1",
        "created_at": BASE.utc_now(),
        "legacy_root": str(legacy_root),
        "asset_root": str(asset_root) if asset_root else None,
        "adapter_contract_version": ADAPTER_CONTRACT_VERSION,
        "tasks_sha256": BASE.sha256(tasks_path),
        "oracles_sha256": BASE.sha256(oracles_path),
        "scoped_task_count": len(rows),
        "verified_task_count": verified,
        "tasks": sorted(results, key=lambda entry: entry["id"]),
    }
    output.mkdir(parents=True, exist_ok=True)
    BASE.atomic_json(output / "legacy-reserve-contract-verification.json", report)
    print(json.dumps(report, indent=2))
    return 0 if verified == len(rows) or args.allow_blocked else 1


def cmd_replay(args: argparse.Namespace) -> int:
    legacy_root = Path(args.legacy_root).expanduser().resolve()
    asset_root = (
        Path(args.asset_root).expanduser().resolve() if args.asset_root else None
    )
    tasks_path = Path(args.tasks).resolve()
    oracles_path = Path(args.oracles).resolve()
    output = Path(args.output).resolve()
    # Fail closed on an unsafe/malformed model ID before it is ever used to
    # build a filesystem path (code-review finding: validate --model with
    # the existing strict validator).
    model = BASE.validate_model_id(args.model)

    tasks_by_id, oracles_by_id, plan = load_release_and_plan(
        tasks_path, oracles_path
    )
    rows = select_scope(plan, args.task_id or None)
    generations = load_staged_generation(output, model, {row["id"] for row in rows})

    legacy_dir = output / "legacy/correctness"
    work_root = Path(args.work_root).resolve() if args.work_root else output / "legacy/work"

    summary = {
        "ready": 0,
        "skipped_resume": 0,
        "stale_reran": 0,
        "candidate_missing": 0,
        "blocked": 0,
        "retracted_stale_rows": 0,
        "executed": 0,
        "errors": [],
    }
    by_lane: dict[str, dict[tuple[str, str], dict[str, Any]]] = {
        lane: existing_raw_rows(legacy_dir, lane) for lane in ("native", "rust", "jvm", "tsjs")
    }

    for row in rows:
        task_id = row["id"]
        lane = row["lane"]
        task = tasks_by_id[task_id]
        oracle = oracles_by_id[task_id]
        generation = generations[task_id]
        key = (task_id, model)

        # Blockers/asset provenance must be (re)computed unconditionally,
        # here, *before* any resume-skip decision -- not lazily inside the
        # non-skip branch -- so that a row recorded while required assets
        # were present cannot survive a since-removed/changed
        # --legacy-root merely by matching an in-row fingerprint that was
        # never sensitive to live asset state (code-review finding).
        # ``row_is_stale`` folds ``blockers``/``legacy_root`` into the
        # replay-contract fingerprint it compares against, so any change
        # here forces a fall-through instead of a resume-skip.
        blockers = task_blockers(
            task_id, lane, oracle, legacy_root, task["start_sha"], asset_root
        )

        existing_row = by_lane[lane].get(key)
        if existing_row is not None and not args.force:
            if not row_is_stale(
                existing_row, task, oracle, task_id, lane, generation,
                legacy_root, blockers, asset_root,
            ):
                summary["skipped_resume"] += 1
                continue
            # Task/Oracle/adapter-contract, live asset provenance, or
            # staged patch drifted since this row was recorded: fall
            # through and rerun instead of silently trusting a stale
            # result (code-review finding: resume must validate
            # fingerprints and rerun stale rows).
            summary["stale_reran"] += 1

        if generation.get("apply_ok") is not True:
            by_lane[lane] = append_raw_row(
                legacy_dir, lane,
                missing_row(
                    task_id, model, lane, row["start_sha"], task, oracle,
                    generation, legacy_root, blockers, asset_root,
                ),
            )
            summary["candidate_missing"] += 1
            continue

        if blockers:
            if existing_row is not None:
                # This can only happen for a row that was actually
                # executed while assets were present (missing_row rows
                # already fold blockers into their fingerprint and are
                # simply rewritten above). An old row must not survive a
                # since-missing/changed asset: retract it from disk, not
                # merely bypass it in-memory, so a later `finalize` can
                # never read it as still valid.
                retract_raw_row(legacy_dir, lane, task_id, model)
                by_lane[lane] = existing_raw_rows(legacy_dir, lane)
                summary["retracted_stale_rows"] += 1
            summary["blocked"] += 1
            summary["errors"].append({"id": task_id, "blockers": blockers})
            continue

        if args.dry_run:
            summary["ready"] += 1
            continue

        try:
            raw_row = REPLAY_FUNCTIONS[lane](
                task_id, model, task, oracle, generation, legacy_root, work_root,
                output, args.timeout,
                **(
                    {"asset_root": asset_root, "offline": not args.allow_network}
                    if lane in ("jvm", "tsjs")
                    else {}
                ),
            )
        except LookupError as error:
            # A missing-key/index error (e.g. reading a field an Oracle
            # does not actually carry -- the exact bug class this
            # adapter previously had for tsjs start_sha) indicates a
            # code/data-contract bug that would never succeed on retry.
            # It must propagate and fail the run loudly instead of being
            # silently mislabeled as a transient INFRA_RETRY (code-review
            # finding: the tsjs start_sha KeyError must not merely be
            # converted into infra_retry).
            raise RuntimeError(
                f"contract bug replaying {lane} task {task_id}: {error!r} "
                "(a required field was missing; this is not a transient "
                "infra failure and will not be recorded as infra_retry)"
            ) from error
        except Exception as error:  # pragma: no cover - defensive
            raw_row = {
                "id": task_id,
                "model": model,
                "lane": lane,
                "source_kind": "published_legacy",
                "start_sha": row["start_sha"],
                "patch_sha256": generation.get("patch_sha256"),
                "status": INFRA_RETRY,
                "candidate_result": INFRA_RETRY,
                "correctness_ok": None,
                "patch_apply": None,
                "scope_ok": None,
                "error": repr(error),
            }
            raw_row.update(
                row_provenance_fields(
                    task, oracle, task_id, lane, generation, legacy_root,
                    blockers, asset_root,
                )
            )
        by_lane[lane] = append_raw_row(legacy_dir, lane, raw_row)
        summary["executed"] += 1

    print(json.dumps(summary, indent=2))
    return 0 if not summary["errors"] or args.allow_blocked else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def add_release_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--tasks", required=True)
    parser.add_argument("--oracles", required=True)
    parser.add_argument("--legacy-root", required=True)
    parser.add_argument(
        "--asset-root",
        help="Durable recovery-asset root holding the reconstructed pinned "
        "assets the retained session root no longer carries (JVM "
        "/oracle-cache + Gradle/Maven cache home, pinned node_modules "
        "snapshots, pinned pnpm store and Node runtimes). Recorded Oracle "
        "paths are resolved here only when the retained root does not have "
        "them, so retained evidence always wins.",
    )
    parser.add_argument(
        "--task-id",
        action="append",
        help="Restrict to a subset of the 14 reserve task IDs (repeatable)",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    preflight = sub.add_parser(
        "preflight",
        help="Fail-closed check of the 14 reserve tasks' harness assets",
    )
    add_release_args(preflight)
    preflight.add_argument("--output")
    preflight.add_argument(
        "--allow-blocked",
        action="store_true",
        help="Exit 0 even if some tasks are blocked (report only)",
    )
    preflight.set_defaults(func=cmd_preflight)

    replay = sub.add_parser(
        "replay",
        help="Replay the 14 reserve tasks for a staged model and emit "
        "output/legacy/correctness/<lane>.jsonl rows",
    )
    add_release_args(replay)
    replay.add_argument("--output", required=True)
    replay.add_argument("--model", required=True)
    replay.add_argument(
        "--work-root",
        help="Disposable scratch root for per-(task,model) checkouts/builds "
        "(default: <output>/legacy/work). May point anywhere, including "
        "outside --output: every replay always copies its run/install/"
        "apply-error logs into a hashed <output>/legacy/evidence subtree "
        "and deletes the scratch copy afterward, so raw rows never "
        "reference an unhashed external log path regardless of where "
        "this points.",
    )
    replay.add_argument("--timeout", type=int, default=1800)
    replay.add_argument(
        "--allow-network",
        action="store_true",
        help="Permit network access during jvm/tsjs replay. Off by default: "
        "scored replays run with no network so the result is a pure function "
        "of the pinned, hashed durable assets. Use only to (re)warm those "
        "assets, never to score.",
    )
    replay.add_argument("--force", action="store_true")
    replay.add_argument("--dry-run", action="store_true")
    replay.add_argument(
        "--allow-blocked",
        action="store_true",
        help="Exit 0 even if some tasks are blocked (report only)",
    )
    replay.set_defaults(func=cmd_replay)

    verify = sub.add_parser(
        "verify-contracts",
        help="Replay every recovered jvm/tsjs Oracle at its exact unpatched "
        "start and require the frozen historical evidence to be reproduced",
    )
    add_release_args(verify)
    verify.add_argument("--output", required=True)
    verify.add_argument("--work-root")
    verify.add_argument("--timeout", type=int, default=7200)
    verify.add_argument(
        "--allow-network",
        action="store_true",
        help="Permit network access (use only to warm the pinned assets)",
    )
    verify.add_argument(
        "--allow-blocked",
        action="store_true",
        help="Exit 0 even if some contracts are blocked/mismatched",
    )
    verify.set_defaults(func=cmd_verify_contracts)

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return args.func(args)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
