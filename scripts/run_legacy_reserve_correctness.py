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
import concurrent.futures
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
import uuid
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

# The exact 17 published-hard task IDs sourced from the 20260728 reserve: the
# 14 this adapter originally covered plus the three JVM/TS-JS tasks
# (angular/components, FluidFramework, sniffy) whose recovered contracts were
# added later. Any drift between this list and what --tasks/--oracles
# currently contain is treated as a preflight blocker rather than silently
# adapting scope.
RESERVE_TASK_IDS: tuple[str, ...] = (
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

# Native/rust published-legacy tasks that were never part of the 20260728
# reserve and therefore have no historical harness under ``--legacy-root`` at
# all. They are only replayable through a *recovered* contract supplied by
# ``--asset-root native-rust=...`` (see
# scripts/recover_legacy_native_rust.py); without one
# they are reported as explicit blockers, never skipped and never scored.
RECOVERED_ONLY_TASK_IDS: tuple[str, ...] = (
    "github__apache__tsfile__pull_823__comment_3418007322__modularity",
    "github__grpc__grpc__pull_32970__comment_1195793486__duplication",
    "github__otter-sec__anchor__pull_2488__comment_1193854703__duplication",
    "github__python__cpython__pull_143314__comment_2656278401__complexity",
)

TASK_IDS: tuple[str, ...] = RESERVE_TASK_IDS + RECOVERED_ONLY_TASK_IDS
EXPECTED_LANE = {
    "github__apache__tsfile__pull_823__comment_3418007322__modularity": "native",
    "github__grpc__grpc__pull_32970__comment_1195793486__duplication": "native",
    "github__otter-sec__anchor__pull_2488__comment_1193854703__duplication": "rust",
    "github__python__cpython__pull_143314__comment_2656278401__complexity": "native",
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
# Pinned SHA-256 of every reconstructed ``/oracle-cache`` blob a published JVM
# Oracle command can reference. A token the recovery has no pinned digest for
# is a preflight blocker: an unpinned blob could silently change the replayed
# behavior, and "the file happens to exist" is not provenance.
# Pinned *whole-tree* SHA-256 of every reconstructed ``/oracle-cache``
# directory a published JVM Oracle depends on. These are validated with
# ``full_tree_digest`` -- every file's relative path, mode/symlink target and
# complete content. No bounded or sampling digest is used anywhere on this
# path: a mirror of thousands of artifacts would otherwise let a tampered or
# removed artifact past a file-count sampling cap or a per-file byte prefix,
# and a resolution that silently picked up different bytes would be scored as a
# candidate failure.
ORACLE_CACHE_DIR_SHA256: dict[str, str] = {
    "m2-mirror": (
        "1dd6ee41cc10766c83c9cebb93813abc1b7ed1c27a0f959545648c9e4f1845d2"
    ),
}

ORACLE_CACHE_ASSET_SHA256: dict[str, str] = {
    "jacocoagent.jar": (
        "47e700ccb0fdb9e27c5241353f8161938f4e53c3561dd35e063c5fe88dc3349b"
    ),
    "jacococli.jar": (
        "8f748683833d4dc4d72cea5d6b43f49344687b831e0582c97bcb9b984e3de0a3"
    ),
    "oracle.init.gradle": (
        "d1e1ffee7439e1144bcf571918be80ba5d6798ff9b530466056089229e501f65"
    ),
}


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
        "required_cache_dirs": ("m2-mirror",),
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
        "required_cache_dirs": ("m2-mirror",),
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
        # The digest below is that recorded value at this exact start commit,
        # pinned here so preflight can require and hash-validate the asset
        # before anything runs instead of discovering it mid-replay.
        "needs_gradle_wrapper_jar": True,
        "gradle_wrapper_jar_sha256": (
            "2db75c40782f5e8ba1fc278a5574bab070adccb2d21ca5a6e5ed840888448046"
        ),
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
        "required_cache_dirs": ("m2-mirror",),
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
        "required_cache_dirs": ("m2-mirror",),
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
#   asset root, copied (copy-on-write where supported) per replay, never hardlinked.
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
#
# ``required_runtime_executables`` / ``required_dependency_executables`` /
# ``required_dependency_files`` name the *exact* binaries and package roots the
# recovered ``test_command`` actually executes. Checking only that a
# ``node_modules`` parent directory exists is not enough: a truncated,
# partially pruned or half-copied dependency tree still has the parent
# directory, and the resulting "cannot find vitest/config" style failure would
# be recorded as a candidate failure. Each of these paths is required in
# preflight, checked for the executable bit where applicable, and folded into
# ``asset_state`` so tampering with any of them invalidates recorded rows.
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
        "required_runtime_executables": ("node",),
        "required_dependency_executables": (".bin/vitest",),
        # The session vitest.config.js imports "vitest/config" and the Oracle
        # command runs it with --coverage against the v8 provider. These are the
        # concrete modules that are actually loaded, not just the launcher shim
        # and its package.json.
        "required_dependency_files": (
            "vitest/package.json",
            "vitest/vitest.mjs",
            "vitest/dist/cli.js",
            "vitest/dist/config.js",
            "vitest/dist/config.cjs",
            "@vitest/coverage-v8/package.json",
            "@vitest/coverage-v8/dist/index.js",
            "@vitest/coverage-v8/dist/provider.js",
            "vite/package.json",
        ),
        # Whole-payload validation: the retained shared Node tools tree must be
        # byte-identical to the one the frozen evidence was produced with,
        # apart from Vitest's own scratch cache directory, which the runner
        # legitimately rewrites and which is not part of the resolved payload.
        "dependency_tree_sha256": (
            "e2d7ee52b90ef8c7d27b5c1337ee8203e5cc67b3fc589fabe74487ba61012474"
        ),
        "dependency_tree_exclusions": (".vite",),
        "runtime_tree_sha256": (
            "54dde7a39d83a58c93eb4aae30515179a8e4264a1e5cecbd055061ef294ab629"
        ),
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
        "required_runtime_executables": ("node",),
        # The recovered command literally invokes node_modules/.bin/react-scripts.
        "required_dependency_executables": (".bin/react-scripts",),
        # react-scripts' shim dispatches to scripts/test.js, which runs Jest
        # through jest-cli; these are the concrete entrypoints that execute.
        "required_dependency_files": (
            "react-scripts/package.json",
            "react-scripts/bin/react-scripts.js",
            "react-scripts/scripts/test.js",
            "react-scripts/scripts/utils/createJestConfig.js",
            "jest/package.json",
            "jest-cli/bin/jest.js",
            "jest-cli/package.json",
        ),
        "dependency_tree_sha256": (
            "6457c2cc2f860c4bd55630229abd25b921ee09e282babb6e81df17a0f0358953"
        ),
        "runtime_tree_sha256": (
            "d8480948b951a2b80f77e50183eb9085ef5c6bda08a34e4f190974c785683dc3"
        ),
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
        # This workspace's dependency tree is materialized by pnpm from the
        # pinned store, so there is no pre-existing node_modules to check;
        # what must exist is the pinned package manager itself. The bare
        # ``pnpm`` shim is load-bearing rather than cosmetic: FluidFramework's
        # test-version-utils postinstall shells out to plain ``pnpm``, and
        # without the shim the offline install fails.
        "required_runtime_executables": ("node", "corepack", "pnpm"),
        # The bare ``pnpm``/``corepack`` entries in the runtime bin directory
        # are symlinks into corepack's dist; the concrete dispatched modules
        # are what actually execute, so they are required and hashed too.
        "required_runtime_files": (
            "../lib/node_modules/corepack/dist/pnpm.js",
            "../lib/node_modules/corepack/dist/corepack.js",
            "../lib/node_modules/corepack/package.json",
        ),
        "runtime_tree_sha256": (
            "b48c9d72dde2de2c62c578210367c81c5fd0d7b182988b06621dc8004ff0f939"
        ),
        # This contract materializes node_modules from the pinned pnpm store,
        # so the store *is* its dependency payload and is validated as a whole.
        # ``v10/index`` and ``v10/projects`` are pnpm's own bookkeeping, which
        # the install rewrites and prunes on every run; only ``v10/files`` --
        # the content-addressed package payload that is actually linked into
        # the workspace, and which is immutable by construction because each
        # file is named by its own hash -- stays inside the digest. Excluding
        # mutable bookkeeping is what keeps the pin stable across replays
        # instead of self-invalidating after the first one.
        "store_tree_sha256": (
            "386fbe0b5aa17e98e002261d79eeb1712a89d3a3a6ec7adada16ecd13c5e76f1"
        ),
        "store_tree_exclusions": ("v10/index", "v10/projects"),
    },
}


INFRA_RETRY = "infra_retry"
CANDIDATE_MISSING = "candidate_missing"

# ---------------------------------------------------------------------------
# Recovered replay contracts (--asset-root)
# ---------------------------------------------------------------------------
# The historical native/rust harnesses were only partially retained: every
# ``.../tasks/<case>/start`` workspace subtree and the recorded rust
# ``cache/cargo`` CARGO_HOME were deleted, and four in-scope tasks never had a
# retained harness on this machine at all. ``scripts/recover_legacy_native_rust
# .py`` re-materializes those assets under an assets root and records each
# proven contract in ``<root>/recovery-manifest.json``. This adapter
# consumes that manifest as an *additional, verified* source of replay
# contracts; it never invents one, and every recovered contract is re-verified
# against live disk/Docker state on each preflight and replay.
RECOVERY_MANIFEST_NAME = "recovery-manifest.json"
RECOVERY_STATE: dict[str, Any] = {"assets_root": None, "tasks": {}}


def load_recovery_assets(assets_root: str | Path | None) -> dict[str, Any]:
    """Load ``<root>/recovery-manifest.json`` into module state.

    Passing ``None`` (the default) clears the state, so behaviour is exactly
    the historical one. A malformed/missing manifest is an error rather than a
    silent fallback: pointing a ``native-rust`` asset root somewhere that has
    no manifest must fail loudly instead of quietly downgrading tasks to
    "blocked".
    """
    if assets_root in (None, ""):
        RECOVERY_STATE["assets_root"] = None
        RECOVERY_STATE["tasks"] = {}
        return RECOVERY_STATE
    root = Path(assets_root).expanduser().resolve()
    manifest_path = root / RECOVERY_MANIFEST_NAME
    if not manifest_path.is_file():
        raise ValueError(
            f"--asset-root native-rust={root} does not contain "
            f"{RECOVERY_MANIFEST_NAME}; run "
            "scripts/recover_legacy_native_rust.py manifest first"
        )
    payload = json.loads(manifest_path.read_text())
    tasks = {entry["id"]: entry for entry in payload.get("tasks", [])}
    RECOVERY_STATE["assets_root"] = root
    RECOVERY_STATE["tasks"] = tasks
    return RECOVERY_STATE


def recovered_entry(task_id: str) -> dict[str, Any] | None:
    return RECOVERY_STATE["tasks"].get(task_id)


# ---------------------------------------------------------------------------
# Durable recovery-asset roots (--asset-root)
# ---------------------------------------------------------------------------
# Two independent recoveries produced two *different kinds* of durable asset
# root, and a single scored run has to consume both at once:
#
#   native-rust  <root>/recovery-manifest.json      -- whole recovered replay
#                (schema reviewbench-legacy-           contracts (workspaces,
#                native-rust-recovery/v1)             caches, recipes, proofs)
#                                                     that *supersede* the
#                                                     historical ones.
#   jvm-tsjs     <root>/manifest.json               -- pinned assets the
#                (schema reviewbench-legacy-           retained session root no
#                recovery-assets/v1)                  longer carries, resolved
#                                                     under the *same* recorded
#                                                     relative path when the
#                                                     retained root lacks them.
#
# They are not interchangeable, so a single unlabelled root would be ambiguous:
# pointing the jvm/tsjs root at ``load_recovery_assets`` would abort the run,
# and pointing the native/rust bundle at ``resolve_legacy_relative`` would
# silently resolve nothing. ``--asset-root`` is therefore repeatable and takes
# either ``LABEL=PATH`` (explicit, always unambiguous) or a bare ``PATH`` that
# is classified by the manifest it actually contains. Bare paths that carry
# neither or both manifests are a hard error rather than a guess, and the same
# label may not be given twice.
ASSET_ROOT_LABELS = ("jvm-tsjs", "native-rust")
JVM_TSJS_MANIFEST_NAME = "manifest.json"
NATIVE_RUST_MANIFEST_SCHEMA = "reviewbench-legacy-native-rust-recovery/"
JVM_TSJS_MANIFEST_SCHEMA = "reviewbench-legacy-recovery-assets/"


def _manifest_schema(path: Path) -> str | None:
    """``schema_version`` of a manifest file, or ``None`` if it is absent or
    not a readable JSON object."""
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    version = payload.get("schema_version")
    return version if isinstance(version, str) else None


def classify_asset_root(root: Path) -> str:
    """Label of an unlabelled ``--asset-root`` PATH, by the manifest it holds.

    Classification is by ``schema_version``, never by directory name, and an
    ambiguous or unrecognised root raises instead of defaulting -- silently
    picking the wrong role would either abort a run that should have proceeded
    or, worse, leave a lane resolving against nothing while still reporting
    "ready".
    """
    native = _manifest_schema(root / RECOVERY_MANIFEST_NAME)
    jvm_tsjs = _manifest_schema(root / JVM_TSJS_MANIFEST_NAME)
    labels = []
    if native is not None and native.startswith(NATIVE_RUST_MANIFEST_SCHEMA):
        labels.append("native-rust")
    if jvm_tsjs is not None and jvm_tsjs.startswith(JVM_TSJS_MANIFEST_SCHEMA):
        labels.append("jvm-tsjs")
    if len(labels) == 1:
        return labels[0]
    if not labels:
        raise ValueError(
            f"--asset-root {root} holds neither a {RECOVERY_MANIFEST_NAME} "
            f"({NATIVE_RUST_MANIFEST_SCHEMA}*) nor a "
            f"{JVM_TSJS_MANIFEST_NAME} ({JVM_TSJS_MANIFEST_SCHEMA}*); pass "
            "--asset-root LABEL=PATH with LABEL in "
            f"{{{', '.join(ASSET_ROOT_LABELS)}}} to say which it is"
        )
    raise ValueError(
        f"--asset-root {root} holds both recovery manifests and is ambiguous; "
        "pass --asset-root LABEL=PATH to disambiguate"
    )


def parse_asset_roots(values: list[str] | str | None) -> dict[str, Path]:
    """Resolve repeatable ``--asset-root`` values into ``{label: path}``.

    Accepts ``LABEL=PATH`` and bare ``PATH`` (classified by
    ``classify_asset_root``) so the pre-existing single-root reserve
    invocations keep working verbatim, while a combined run passes one
    ``--asset-root`` per lane group. A repeated label, an unknown label or a
    non-existent path is an error: the whole point of these roots is that
    provenance resolves somewhere real, so a typo must not degrade to
    "resolved nowhere, everything blocked".
    """
    if values is None:
        values = []
    if isinstance(values, (str, Path)):
        values = [str(values)]
    roots: dict[str, Path] = {}
    for value in values:
        if not value:
            continue
        label: str | None = None
        raw = value
        head, separator, tail = value.partition("=")
        if separator and head in ASSET_ROOT_LABELS:
            label, raw = head, tail
        elif separator and head and not Path(value).exists():
            raise ValueError(
                f"unknown --asset-root label {head!r}; expected one of "
                f"{{{', '.join(ASSET_ROOT_LABELS)}}}"
            )
        root = Path(raw).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"--asset-root {root} is not a directory")
        if label is None:
            label = classify_asset_root(root)
        if label in roots and roots[label] != root:
            raise ValueError(
                f"--asset-root {label} given twice ({roots[label]} and {root})"
            )
        roots[label] = root
    return roots


def resolve_asset_roots(args: argparse.Namespace) -> tuple[Path | None, Path | None]:
    """``(pinned_asset_root, recovery_assets_root)`` for a parsed CLI.

    ``pinned_asset_root`` (jvm-tsjs) is threaded through
    ``resolve_legacy_relative`` and the jvm/tsjs required-asset helpers;
    ``recovery_assets_root`` (native-rust) is loaded into ``RECOVERY_STATE``.
    Loading always happens -- including with no roots at all -- so a process
    that replays twice never inherits the previous scope's recovered
    contracts.
    """
    values = list(getattr(args, "asset_root", None) or [])
    if not values:
        # Backward-compatible single-root reserve usage: the pre-existing
        # native/rust recovery exported this variable and passed no flag.
        values = [
            item
            for item in os.environ.get("REVIEWBENCH_LEGACY_ASSETS", "").split(
                os.pathsep
            )
            if item
        ]
    roots = parse_asset_roots(values)
    load_recovery_assets(roots.get("native-rust"))
    return roots.get("jvm-tsjs"), roots.get("native-rust")


# Lanes whose durable assets live in the ``native-rust`` recovery bundle
# rather than in the ``jvm-tsjs`` pinned-asset root.
NATIVE_RUST_LANES = ("native", "rust")


def lane_asset_root(
    lane: str, task_id: str, asset_root: Path | None = None
) -> Path | None:
    """The durable asset root that actually participates in *this* row.

    Provenance must be lane-specific and stable. A combined invocation passes
    both roots, but a native/rust row's replay never reads the ``jvm-tsjs``
    pinned-asset root and a jvm/tsjs row's replay never reads the
    ``native-rust`` recovery bundle. Folding "whichever root the CLI happened
    to be given" into a row's ``replay_contract_fingerprint`` would therefore
    make every native/rust row stale the moment a jvm/tsjs root was added to
    the command line (and vice versa), forcing pointless full reruns of work
    that nothing about the row actually changed.

    So each row binds exactly one root:

    * a task covered by a recovered contract, and every ``native``/``rust``
      task, binds the ``native-rust`` bundle (``RECOVERY_STATE``), because
      that bundle is what decides whether a recovered contract exists and
      supplies the workspace/caches the replay mounts;
    * every ``jvm``/``tsjs`` task binds the ``jvm-tsjs`` pinned-asset root,
      which is where ``resolve_legacy_relative`` and the pinned-asset helpers
      look when the retained session root no longer has an asset.

    Changing a lane's *own* root still invalidates that lane's rows, which is
    the invalidation this fingerprint exists to provide.
    """
    if recovered_entry(task_id) is not None or lane in NATIVE_RUST_LANES:
        return RECOVERY_STATE["assets_root"]
    return asset_root


def workspace_git_state(start: Path) -> dict[str, Any]:
    """Live HEAD plus tracked/untracked/submodule state of a workspace.

    Mirrors ``recover_legacy_native_rust.workspace_state`` field for field, so
    preflight/replay compare exactly what the manifest recorded. Untracked
    entries are collapsed by git at directory granularity, which keeps this
    cheap even for a 52 GB workspace containing a full build tree.
    """
    def _git(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(start), *args],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        return result.stdout if result.returncode == 0 else ""

    return {
        "head": _git("rev-parse", "HEAD").strip() or None,
        "tracked": sorted(
            line[3:]
            for line in _git(
                "status", "--porcelain", "--untracked-files=no"
            ).splitlines()
            if line.strip()
        ),
        "untracked": sorted(
            line[3:]
            for line in _git(
                "status", "--porcelain", "--untracked-files=normal"
            ).splitlines()
            if line.startswith("?? ")
        ),
        "submodules": sorted(
            line.strip()
            for line in _git("submodule", "status").splitlines()
            if line.strip()
        ),
        # Authoritative: a full lstat+content digest of the copied tree with
        # only ``.git`` metadata excluded, so ``.gitignore``d build residue
        # (build/, target/, node_modules/ ...) -- which git status hides and
        # which is nevertheless copied into every replay scratch -- blocks.
        "tree": _tree_digest_impl(start, {".git"}),
    }


# Deliberately *not* memoized. A mounted cache is a live, writable directory
# (cargo and conan both write into their cache homes), so any process-lifetime
# memo could hand a later blocker check a digest that predates a mutation --
# precisely the window in which a drifted cache would be waved through. Every
# call re-walks the tree; the walk costs well under a second per gigabyte and
# runs at most twice per (task, model) replay.
# Deliberately *not* memoized. A mounted cache is a live, writable directory,
# so any process-lifetime memo could hand a later blocker check a digest that
# predates a mutation -- precisely the window in which a drifted cache would be
# waved through. Every call re-walks and re-hashes the tree.
_CACHE_TREE_DIGESTS: dict[str, dict[str, Any] | None] = {}
_HASH_WORKERS = 16
_HASH_CHUNK = 4 * 1024 * 1024


def invalidate_cache_digests() -> None:
    """Drop *every* memoized digest in this module.

    ``cache_tree_digest``/``content_tree_digest`` are never memoized at all
    (their trees are live and writable). ``full_file_digest``/
    ``full_tree_digest`` do memoize, because the jvm/tsjs payloads they cover
    are pinned and mounted read-only -- but their keys deliberately do not
    include an mtime for trees, so a post-run drift check that reused a
    pre-run memo would report "no drift" for a tree that had in fact been
    rewritten. Every place that has to observe *current* disk state (each
    scoped preflight, each replay's post-run revalidation) calls this first,
    so no provenance decision anywhere can be answered from a stale memo.
    """
    _CACHE_TREE_DIGESTS.clear()
    _FULL_DIGEST_CACHE.clear()
    _TREE_DIGEST_CACHE.clear()


def file_content_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_file_sha256(path: Path) -> str:
    try:
        return file_content_sha256(path)
    except OSError:
        return "unreadable"


def file_input_digest(path: Path) -> dict[str, Any] | None:
    """Content identity of a single mounted file."""
    if not path.is_file():
        return None
    stat = path.lstat()
    return {
        "kind": "file",
        "size": stat.st_size,
        "mode": stat.st_mode,
        "sha256": file_content_sha256(path),
    }



def _enumerate_tree(
    path: Path, exclude: set[str]
) -> tuple[list[tuple[str, str, int, int, str]], list[tuple[str, Path]]]:
    """lstat-based enumeration of **every** path entry under ``path``.

    Yields one record per entry -- regular files, symlinks (to files *or*
    directories), real directories (including empty ones) and anything else --
    as ``(relative_path, kind, mode, size, detail)``. Symlinks are never
    followed: a symlinked directory is recorded by its target string, so
    retargeting or removing it changes the digest, and a real directory is
    recorded in its own right, so adding or removing an empty directory does
    too. ``os.walk`` could do neither: it drops directory entries from the
    record entirely, silently leaving symlinked and empty directories unbound.

    Returns the entry list plus the list of regular files whose bytes still
    need hashing.
    """
    entries: list[tuple[str, str, int, int, str]] = []
    files: list[tuple[str, Path]] = []
    stack: list[Path] = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as scanner:
                items = sorted(scanner, key=lambda item: item.name)
        except OSError:
            entries.append(
                (str(current.relative_to(path)), "unreadable-dir", -1, -1, "")
            )
            continue
        for item in items:
            item_path = Path(item.path)
            relative = str(item_path.relative_to(path))
            if current == path and item.name in exclude:
                continue
            try:
                info = item.stat(follow_symlinks=False)
            except OSError:
                entries.append((relative, "unreadable", -1, -1, ""))
                continue
            if item.is_symlink():
                try:
                    target = os.readlink(item_path)
                except OSError:
                    target = "unreadable"
                entries.append(
                    (relative, "symlink", info.st_mode, info.st_size, target)
                )
            elif stat.S_ISDIR(info.st_mode):
                entries.append((relative, "dir", info.st_mode, 0, ""))
                stack.append(item_path)
            elif stat.S_ISREG(info.st_mode):
                entries.append((relative, "file", info.st_mode, info.st_size, ""))
                files.append((relative, item_path))
            else:
                entries.append((relative, "other", info.st_mode, info.st_size, ""))
    return entries, files


def _tree_digest_impl(
    path: Path, exclude: set[str] | None = None
) -> dict[str, Any] | None:
    """Full-content digest of every entry under ``path``.

    Identical implementation and semantics in the recovery tool and the replay
    adapter: every regular file contributes its bytes' SHA-256 plus path, mode
    and size; every symlink contributes its link target (never followed);
    every real directory, including empty ones, contributes its own record.
    """
    if not path.is_dir():
        return None
    entries, files = _enumerate_tree(path, exclude or set())
    hashes: dict[str, str] = {}
    if files:
        with concurrent.futures.ThreadPoolExecutor(_HASH_WORKERS) as pool:
            futures = {
                pool.submit(_safe_file_sha256, item): relative
                for relative, item in files
            }
            for future in concurrent.futures.as_completed(futures):
                hashes[futures[future]] = future.result()
    final = [
        (
            relative,
            kind,
            mode,
            size,
            hashes.get(relative, "unreadable") if kind == "file" else detail,
        )
        for relative, kind, mode, size, detail in entries
    ]
    return {
        "kind": "tree",
        "entry_count": len(final),
        "file_count": sum(1 for row in final if row[1] == "file"),
        "dir_count": sum(1 for row in final if row[1] == "dir"),
        "symlink_count": sum(1 for row in final if row[1] == "symlink"),
        "digest": hashlib.sha256(
            json.dumps(sorted(final), sort_keys=True).encode("utf-8")
        ).hexdigest(),
    }


def cache_tree_digest(
    path: Path, *, exclude: set[str] | None = None
) -> dict[str, Any] | None:
    """See ``_tree_digest_impl``; never memoized (mounted trees are writable)."""
    return _tree_digest_impl(path, exclude)


def content_tree_digest(
    path: Path | None, *, exclude: set[str] | None = None
) -> dict[str, Any] | None:
    """Full lstat+content digest of any replay-mounted tree, legacy or
    recovered (alias of the shared implementation)."""
    if path is None:
        return None
    return _tree_digest_impl(path, exclude)


def content_file_digest(path: Path | None) -> dict[str, Any] | None:
    """Full-content digest of any single replay-mounted file."""
    if path is None:
        return None
    return file_input_digest(path)


def mount_input_digests(entry: dict[str, Any], assets_root: Path) -> dict[str, Any]:
    """Live content digests of every replay-mounted input except the
    exact-start subtree (which is verified through git ``workspace_state``).

    Mirrors ``recover_legacy_native_rust.mount_input_digests`` key for key, so
    the manifest's recorded map can be compared entry by entry.
    """
    workspace = assets_root / entry["workspace"]
    digests: dict[str, Any] = {}
    for host in sorted(entry.get("mount") or {}):
        if host == entry["patch_subdir"]:
            continue
        source = workspace if host == "." else workspace / host
        key = f"mount:{host}"
        if host == ".":
            digests[key] = cache_tree_digest(
                source, exclude={entry["patch_subdir"]}
            )
        elif source.is_dir():
            digests[key] = cache_tree_digest(source)
        else:
            digests[key] = file_input_digest(source)
    for mount in entry.get("harness_mounts") or []:
        source = assets_root / mount["host"]
        digests[f"harness_mount:{mount['host']}"] = (
            cache_tree_digest(source) if source.is_dir()
            else file_input_digest(source)
        )
    for copy in entry.get("harness_copies") or []:
        digests[f"harness_copy:{copy['host']}"] = file_input_digest(
            assets_root / copy["host"]
        )
    return digests


def _proof_run_label(run_row: dict[str, Any]) -> str:
    """Short human label for one recorded proof run."""
    return (
        f"({run_row.get('variant')} run {run_row.get('run')}, stage "
        f"{run_row.get('stage')}, exit {run_row.get('exit_code')})"
    )


def _latest_proof_run(
    proof_runs: list[dict[str, Any]], variant: str
) -> dict[str, Any] | None:
    """The most recently recorded run of ``variant`` (list order is
    execution order)."""
    matching = [
        run_row for run_row in proof_runs if run_row.get("variant") == variant
    ]
    return matching[-1] if matching else None


def recovered_blockers(task_id: str, entry: dict[str, Any]) -> list[str]:
    """Re-verify a recovered contract against live disk/Docker state.

    The manifest is evidence, not authority: a contract only stays "ready" if
    its exact-start workspace is still present at the recorded ``start_sha``,
    its authored harness files still hash to the recorded digests, its Docker
    image is still loaded with the recorded image id, and any reconstructed
    cargo cache is still there.
    """
    root: Path | None = RECOVERY_STATE["assets_root"]
    if root is None:  # pragma: no cover - defensive
        return ["recovered contract present without a native-rust asset root"]
    blockers: list[str] = list(entry.get("blockers") or [])
    if not entry.get("recovered"):
        return blockers or [f"no recovered replay contract for {task_id}"]
    workspace = root / entry["workspace"]
    start = workspace / entry["patch_subdir"]
    if not start.is_dir():
        blockers.append(f"missing recovered exact-start subtree: {start}")
    else:
        live_state = workspace_git_state(start)
        if live_state.get("head") != entry["start_sha"]:
            blockers.append(
                f"recovered workspace {start} is at {live_state.get('head')}, "
                f"not the task start_sha {entry['start_sha']}"
            )
        recorded_state = entry.get("workspace_state") or {}
        for field in ("tracked", "untracked", "submodules", "tree"):
            if field not in recorded_state:
                # An older manifest without recorded workspace state cannot be
                # verified, and an unverifiable workspace must not run.
                blockers.append(
                    "recovered contract manifest does not record "
                    f"workspace_state.{field}; regenerate it with "
                    "scripts/recover_legacy_native_rust.py manifest"
                )
                continue
            if live_state.get(field) != recorded_state.get(field):
                if field == "tree":
                    live_tree = live_state.get("tree") or {}
                    recorded_tree = recorded_state.get("tree") or {}
                    blockers.append(
                        f"recovered workspace {start} has drifted since the "
                        "manifest was written: tree digest "
                        f"{str(recorded_tree.get('digest'))[:12]} -> "
                        f"{str(live_tree.get('digest'))[:12]} "
                        f"({recorded_tree.get('entry_count')} -> "
                        f"{live_tree.get('entry_count')} entries; this covers "
                        "gitignored build residue)"
                    )
                    continue
                unexpected = sorted(
                    set(live_state.get(field) or [])
                    - set(recorded_state.get(field) or [])
                )[:5]
                missing = sorted(
                    set(recorded_state.get(field) or [])
                    - set(live_state.get(field) or [])
                )[:5]
                blockers.append(
                    f"recovered workspace {start} has drifted since the "
                    f"manifest was written: {field} unexpected={unexpected} "
                    f"missing={missing}"
                )
    for hashed in entry.get("hashed_files", []):
        path = root / hashed["path"]
        if not path.is_file():
            blockers.append(f"missing recovered asset: {path}")
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != hashed["sha256"]:
            blockers.append(
                f"recovered asset {path} hash {digest} does not match the "
                f"manifest digest {hashed['sha256']}"
            )
    image = entry.get("docker_image")
    live_image_id = docker_image_id(image) if image else None
    if live_image_id is None:
        blockers.append(f"missing Docker image: {image}")
    elif entry.get("docker_image_id") and live_image_id != entry["docker_image_id"]:
        blockers.append(
            f"Docker image {image} is now {live_image_id}, not the recovered "
            f"{entry['docker_image_id']}"
        )
    # Every *non-start* input the replay mounts -- single harness files
    # (query.sql, behavior_test.py), whole workspace directories exposed by a
    # ``mount: "."`` recipe (oracle.cpp, retained patches), read-only harness
    # directories and harness copy sources -- must be present and match its
    # recorded content digest. Tampering with any one of them blocks here,
    # both at preflight and in the pre-execution check.
    recorded_mounts = entry.get("mount_input_digests")
    if recorded_mounts is None:
        blockers.append(
            "recovered contract manifest does not record "
            "mount_input_digests; regenerate it with "
            "scripts/recover_legacy_native_rust.py manifest"
        )
    else:
        live_mounts = mount_input_digests(entry, root)
        for key in sorted(set(recorded_mounts) | set(live_mounts)):
            recorded_value = recorded_mounts.get(key)
            live_value = live_mounts.get(key)
            if key not in recorded_mounts:
                blockers.append(
                    f"mounted input {key} is not bound to the recovery "
                    "manifest (no recorded content digest)"
                )
            elif live_value is None:
                blockers.append(f"missing mounted input: {key}")
            elif live_value != recorded_value:
                blockers.append(
                    f"mounted input {key} has drifted from its recorded "
                    "content digest"
                )

    # Every directory a replay actually mounts must be present *and* bound to
    # the manifest, not merely exist: a mounted cache silently swapped,
    # truncated or partially restored changes what the Oracle compiles.
    recorded_caches = entry.get("mounted_cache_digests")
    if recorded_caches is None:
        blockers.append(
            "recovered contract manifest does not record "
            "mounted_cache_digests; regenerate it with "
            "scripts/recover_legacy_native_rust.py manifest"
        )
        recorded_caches = {}
    mounted_caches = list((entry.get("cache_mounts") or {}))
    if entry.get("cargo_cache"):
        mounted_caches.append(entry["cargo_cache"])
    for relative in mounted_caches:
        cache_dir = root / relative
        if not cache_dir.is_dir():
            blockers.append(f"missing mounted cache: {cache_dir}")
            continue
        recorded = recorded_caches.get(relative)
        if recorded is None:
            blockers.append(
                f"mounted cache {cache_dir} is not bound to the recovery "
                "manifest (no recorded tree digest)"
            )
            continue
        live = cache_tree_digest(cache_dir)
        if live != recorded:
            blockers.append(
                f"mounted cache {cache_dir} has drifted: recorded "
                f"{recorded.get('file_count')} files / "
                f"{str(recorded.get('digest'))[:12]}, live "
                f"{live.get('file_count') if live else None} files / "
                f"{str((live or {}).get('digest'))[:12]}"
            )
    if entry.get("cargo_cache") and not (root / entry["cargo_cache"] / "bin" / "cargo").exists():
        blockers.append(
            f"incomplete reconstructed CARGO_HOME (no bin/cargo): "
            f"{root / entry['cargo_cache']}"
        )
    for mount in entry.get("harness_mounts") or []:
        if not (root / mount["host"]).exists():
            blockers.append(
                f"missing mounted harness directory: {root / mount['host']}"
            )
    for copy in entry.get("harness_copies") or []:
        if not (root / copy["host"]).is_file():
            blockers.append(f"missing harness copy source: {root / copy['host']}")
    proof_runs = entry.get("proof_runs", [])
    recorded_fingerprint = entry.get("recipe_fingerprint")
    if recorded_fingerprint is None:
        blockers.append(
            "recovered contract manifest does not record a "
            "recipe_fingerprint; regenerate it with "
            "scripts/recover_legacy_native_rust.py manifest"
        )
    else:
        stale = [
            run_row for run_row in proof_runs
            if run_row.get("recipe_fingerprint") != recorded_fingerprint
        ]
        if stale:
            blockers.append(
                f"{len(stale)} recorded proof run(s) were produced under a "
                "different recipe fingerprint than the manifest entry "
                "(command/image/mount/workspace/cache/harness drift); re-run "
                "scripts/recover_legacy_native_rust.py smoke --golden"
            )
        proof_runs = [
            run_row for run_row in proof_runs
            if run_row.get("recipe_fingerprint") == recorded_fingerprint
        ]
    # Fail closed on the runs that are still current, exactly as
    # ``recover_legacy_native_rust.proof_blockers`` does. An old success
    # followed by a later failure at the *same* recipe fingerprint means the
    # contract does not currently work, so "some run passed" is not a
    # sufficient test: any failure blocks, and the latest start (and latest
    # golden, where a source-only golden is declared) must both have
    # succeeded. ``proof_runs`` is append-only in execution order, so the last
    # matching entry is the latest attempt.
    failed = [
        run_row for run_row in proof_runs if run_row.get("exit_code") != 0
    ]
    if failed:
        blockers.append(
            f"{len(failed)} recorded proof run(s) at the manifest's recipe "
            "fingerprint failed "
            f"{[_proof_run_label(run_row) for run_row in failed]}; an earlier "
            "success does not clear a later failure -- re-run "
            "scripts/recover_legacy_native_rust.py smoke --golden --reset"
        )
    latest_start = _latest_proof_run(proof_runs, "start")
    if latest_start is None:
        blockers.append(
            "recovered contract has no successful start-tree proof run "
            "recorded in recovery-manifest.json"
        )
    elif latest_start.get("exit_code") != 0:
        blockers.append(
            "the latest recorded start-tree proof run failed "
            f"{_proof_run_label(latest_start)}"
        )
    if entry.get("golden_patch"):
        # PASS_TO_PASS: a contract that declares a source-only golden is only
        # trustworthy once that golden has been shown to apply and pass.
        latest_golden = _latest_proof_run(proof_runs, "golden")
        if latest_golden is None:
            blockers.append(
                "recovered contract declares a source-only golden patch but "
                "has no successful golden proof run recorded in "
                "recovery-manifest.json"
            )
        elif latest_golden.get("exit_code") != 0:
            blockers.append(
                "the latest recorded golden proof run failed "
                f"{_proof_run_label(latest_golden)}"
            )
    return blockers


def recovered_asset_state(entry: dict[str, Any]) -> dict[str, Any]:
    root: Path | None = RECOVERY_STATE["assets_root"]
    if root is None:  # pragma: no cover - defensive
        return {}
    workspace = root / entry["workspace"]
    mounted_caches = list(entry.get("cache_mounts") or {})
    if entry.get("cargo_cache"):
        mounted_caches.append(entry["cargo_cache"])
    return {
        "assets_root": str(root),
        # the exact tree replay copies and patches, including any drift
        "workspace_state": workspace_git_state(workspace / entry["patch_subdir"]),
        "docker_image_id": docker_image_id(entry.get("docker_image") or ""),
        # content hashes of every authored harness/patch input
        "hashed_files": entry.get("hashed_files", []),
        "command": entry.get("command"),
        "golden_command": entry.get("golden_command"),
        "shell": entry.get("shell"),
        "mount": entry.get("mount"),
        "recipe_fingerprint": entry.get("recipe_fingerprint"),
        "harness_mounts": entry.get("harness_mounts"),
        "harness_copies": entry.get("harness_copies"),
        # every cache directory the replay mounts, digested in full
        "mounted_caches": {
            relative: cache_tree_digest(root / relative)
            for relative in sorted(mounted_caches)
        },
        # every other mounted input (files, workspace siblings, harness dirs)
        "mounted_inputs": mount_input_digests(entry, root),
    }


class _PrimeFailure(Exception):
    """Raised when a mandatory ``prime_command`` did not succeed.

    Carries the already-built ``infra_retry`` row so ``replay_jvm`` can persist
    its evidence and return without ever reaching the scored command.
    """

    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__(result.get("error", "prime_command failed"))
        self.result = result


# Bump whenever the *behavior* of a replay_* function, NATIVE_MANIFEST entry,
# or judging rule changes in a way that could change the outcome for
# already-recorded rows. It is folded into every raw row's
# ``replay_contract_fingerprint`` so ``cmd_replay``'s resume logic reruns
# rows that were produced under a since-changed contract instead of
# silently treating them as fresh (code-review finding: "resume must
# validate current task/oracle/replay-contract fingerprints and rerun
# stale rows").
ADAPTER_CONTRACT_VERSION = "v6-2026-08-14-integrated-legacy-asset-recovery"


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
    # Normalize to the root this lane actually replays against, whatever the
    # caller passed. A combined invocation supplies both roots, and binding an
    # unrelated one here would make every row of the other lane group stale
    # (see ``lane_asset_root``). Doing it inside the fingerprint -- not only at
    # the call sites -- means no caller can reintroduce that drift.
    lane_root = lane_asset_root(lane, task_id, asset_root)
    payload: dict[str, Any] = {
        "adapter_contract_version": ADAPTER_CONTRACT_VERSION,
        "legacy_root": str(legacy_root),
        "asset_root": str(lane_root) if lane_root else None,
        "blockers": sorted(blockers),
        "asset_state": asset_state(
            task_id, lane, oracle, legacy_root, lane_root
        ),
    }
    entry = recovered_entry(task_id)
    if entry is not None:
        # The recovered contract (command/mounts/harness digests/image id) is
        # the recipe that will actually run, so it -- not the historical
        # manifest -- must be what the fingerprint binds.
        payload["recovered_contract"] = {
            key: entry.get(key)
            for key in (
                "command", "shell", "user", "network", "env", "mount",
                "readonly_mounts", "cache_mounts", "cargo_cache",
                "harness_mounts", "harness_copies", "workdir", "workspace",
                "patch_subdir", "start_sha", "docker_image",
                "docker_image_id", "hashed_files",
            )
        }
    elif lane == "native":
        # ``.get``: a native published-legacy task outside the 20260728
        # reserve has no historical manifest entry at all; it is blocked
        # unless a recovered contract covers it, and blockers are already
        # folded into this payload.
        payload["native_manifest"] = NATIVE_MANIFEST.get(task_id)
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
def execution_lock(legacy_dir: Path, lane: str, model: str, task_id: str):
    """Exclusive, cross-process lock for one (lane, model, task) execution.

    Held from scratch creation through evidence persistence and cleanup, so
    two concurrent invocations that happen to be replaying the *same* pair
    (a re-run started before the first finished, a retry loop, two shards
    given overlapping task lists) cannot interleave: without it, the second
    invocation's ``shutil.rmtree(scratch)`` would delete the first's
    workspace mid-build, and its ``persist_evidence`` would overwrite the
    first's logs while the first was still writing them.

    Scratch directories are *also* made unique per invocation
    (``invocation_scratch``) and evidence files are published atomically, so
    even a caller that bypasses this lock cannot destroy another invocation's
    state -- defence in depth rather than a single choke point. The lock file
    name ends in ``.lock``, which ``write_artifact_set`` already excludes from
    hashing.
    """
    legacy_dir.mkdir(parents=True, exist_ok=True)
    name = f".exec-{lane}-{model_component(model)}-{BASE.safe_component(task_id)}.lock"
    lock_path = legacy_dir / name
    with open(lock_path, "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield lock_path
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def invocation_scratch(
    work_root: Path, lane: str, model: str, task_id: str
) -> Path:
    """Per-invocation scratch directory for one (lane, model, task).

    The suffix makes it unique across processes *and* across retries in one
    process, so a concurrent duplicate replay can never remove or write into
    another invocation's workspace.
    """
    unique = f"{os.getpid()}-{uuid.uuid4().hex[:12]}"
    return (
        work_root / lane / model_component(model)
        / BASE.safe_component(task_id) / unique
    )


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
    invisible to a parent-only stat. Use ``content_tree_digest`` for
    those; this remains appropriate for a cheap existence probe only, and
    is never used for provenance or blocker decisions.
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    return {"is_dir": path.is_dir(), "size": stat.st_size, "mtime": stat.st_mtime}


# The bounded ``directory_identity``/``safe_file_digest``/``_bounded_file_digest``
# helpers that used to live here (500-file traversal cap, 64 KiB-per-file read
# cap) have been removed outright: they were used for exactly the provenance
# decisions that must not be approximate. Any tree beyond the cap, any change
# past a file's first 64 KiB, and every symlink or empty directory were
# invisible to them. Every lane now uses only complete, lstat-based identity --
# ``content_tree_digest``/``file_input_digest`` for live, writable replay mounts
# (recovered native/rust caches and workspaces) and
# ``full_tree_digest``/``full_entry_digest``/``full_file_digest`` for the pinned,
# read-only jvm/tsjs payloads whose digests are frozen in the manifests above --
# so a historical JVM Gradle cache, a TS/JS node_modules tree, a rust ``start/``
# checkout and a recovered cache are all bound the same way.
_FULL_DIGEST_CACHE: dict[tuple[str, int, int], str] = {}
_TREE_DIGEST_CACHE: dict[tuple[str, tuple[str, ...]], str | None] = {}


def full_file_digest(path: Path | None) -> str | None:
    """Complete SHA-256 of a pinned asset, or ``None`` if unreadable.

    Pinned assets are validated by their *whole* content, not by a bounded
    prefix: a same-size in-place tamper anywhere in a multi-megabyte archive
    must be rejected, not merely be unlikely to be noticed. Results are memoised
    on (path, size, mtime_ns) so repeated preflight/fingerprint calls across
    many models do not rehash the same asset over and over.
    """
    if path is None:
        return None
    try:
        status = path.stat()
    except OSError:
        return None
    key = (str(path), status.st_size, status.st_mtime_ns)
    cached = _FULL_DIGEST_CACHE.get(key)
    if cached is not None:
        return cached
    hasher = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
    except OSError:
        return None
    digest = hasher.hexdigest()
    _FULL_DIGEST_CACHE[key] = digest
    return digest


def full_entry_digest(path: Path | None) -> str | None:
    """Complete, symlink-aware identity of a single pinned filesystem entry.

    Unlike ``full_file_digest`` -- which follows symlinks and therefore reports
    the *target's* content -- this uses ``lstat`` semantics: a symlink is
    identified by its literal target string, so retargeting ``.bin/vitest`` at a
    different (even byte-identical) file changes the identity. Regular files are
    identified by their complete SHA-256, directories and anything else by kind.
    ``None`` means the entry does not exist, which differs from every real
    identity and so still invalidates a fingerprint.
    """
    if path is None:
        return None
    try:
        status = path.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(status.st_mode):
        try:
            return f"L:{os.readlink(path)}"
        except OSError:  # pragma: no cover - defensive
            return None
    if stat.S_ISDIR(status.st_mode):
        return "D:"
    if stat.S_ISREG(status.st_mode):
        digest = full_file_digest(path)
        return None if digest is None else f"F:{digest}"
    return f"O:{stat.S_IFMT(status.st_mode)}"


def full_tree_digest(
    root: Path | None, exclude: tuple[str, ...] = ()
) -> str | None:
    """Deterministic, complete content digest of a pinned directory tree.

    A full ``lstat`` traversal: it binds **every** entry the tree contains --
    regular files by their complete SHA-256 plus size and executable bit,
    symlinks (to files *or* directories) by their literal target without ever
    following them, directories by their relative path so an added, removed or
    emptied directory is visible, and any other node type by its kind. Nothing
    is sampled: there is no file-count cap and no per-file byte cap, so a tail
    edit past any prefix, a same-size in-place swap, an artifact removed from a
    tree of thousands, a retargeted symlink and a vanished empty directory all
    change the digest.

    This is one of only two identity functions that feed provenance: this one
    for the pinned, read-only jvm/tsjs payloads whose digests are frozen in the
    manifests above, and ``content_tree_digest``/``file_input_digest`` for the
    live, writable native/rust replay mounts. Every bounded or sampling helper
    has been deleted from this module, so no provenance decision anywhere can
    fall back on a file-count cap, a per-file byte cap or symlink blindness.

    ``exclude`` lists relative path prefixes that are *runtime scratch*, not
    payload -- a test runner's own cache directory, or a package store's
    per-project bookkeeping. They are excluded because a tool legitimately
    rewrites them while running, so including them would make the pinned digest
    self-invalidating after the first replay; everything that is actually
    resolved and executed stays inside the digest.

    Memoised per process by (root path, exclusions): preflight and every
    per-model fingerprint call in one run share a single pass over each tree.
    """
    if root is None:
        return None
    exclusions = tuple(sorted(exclude))
    key = (str(root), exclusions)
    if key in _TREE_DIGEST_CACHE:
        return _TREE_DIGEST_CACHE[key]
    digest: str | None
    if not root.is_dir() or root.is_symlink():
        digest = None
    else:
        hasher = hashlib.sha256()
        try:
            for current, directories, names in os.walk(root, followlinks=False):
                relative_dir = Path(current).relative_to(root)
                directories.sort()
                kept: list[str] = []
                for name in directories:
                    relative = str(relative_dir / name)
                    if relative in exclusions:
                        continue
                    path = Path(current) / name
                    if path.is_symlink():
                        # A symlink to a directory is never descended into and
                        # is bound by its literal target.
                        hasher.update(
                            f"L\0{relative}\0{os.readlink(path)}\0".encode("utf-8")
                        )
                        continue
                    # Bind the directory itself, so an added, removed or
                    # emptied directory is visible even when it holds no files.
                    hasher.update(f"D\0{relative}\0".encode("utf-8"))
                    kept.append(name)
                directories[:] = kept
                for name in sorted(names):
                    path = Path(current) / name
                    relative = str(path.relative_to(root))
                    if relative in exclusions:
                        continue
                    status = path.lstat()
                    if stat.S_ISLNK(status.st_mode):
                        hasher.update(
                            f"L\0{relative}\0{os.readlink(path)}\0".encode("utf-8")
                        )
                        continue
                    if not stat.S_ISREG(status.st_mode):
                        hasher.update(
                            f"O\0{relative}\0{stat.S_IFMT(status.st_mode)}\0"
                            .encode("utf-8")
                        )
                        continue
                    inner = hashlib.sha256()
                    with path.open("rb") as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            inner.update(chunk)
                    executable = 1 if status.st_mode & 0o111 else 0
                    hasher.update(
                        f"F\0{relative}\0{status.st_size}\0{executable}\0"
                        f"{inner.hexdigest()}\0".encode("utf-8")
                    )
        except OSError:
            digest = None
        else:
            digest = hasher.hexdigest()
    _TREE_DIGEST_CACHE[key] = digest
    return digest


def jvm_cache_root(
    oracle: dict[str, Any],
    task_id: str,
    legacy_root: Path,
    asset_root: Path | None,
) -> Path | None:
    """Directory bind-mounted at ``/oracle-cache`` for a JVM replay."""
    cache = jvm_cache(oracle, task_id)
    if not cache:
        return None
    return resolve_legacy_relative(cache, legacy_root, asset_root).parent


def jvm_required_assets(
    task_id: str,
    oracle: dict[str, Any],
    legacy_root: Path,
    asset_root: Path | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Every pinned file and directory a JVM replay is allowed to depend on.

    Returns ``(files, directories)`` where each file entry carries the pinned
    ``sha256`` it must hash to (``None`` when the recovery has no pinned digest
    for it, which is itself a blocker). This is the single source of truth
    shared by ``jvm_blockers`` (fail closed) and ``asset_state`` (fingerprint),
    so an asset can never be required by one and ignored by the other.
    """
    manifest = JVM_MANIFEST.get(task_id) or {}
    cache_root = jvm_cache_root(oracle, task_id, legacy_root, asset_root)
    files: list[dict[str, Any]] = []
    directories: list[dict[str, Any]] = []
    if cache_root is None:
        return files, directories
    for token in oracle_cache_tokens(str(oracle.get("command") or "")):
        files.append(
            {
                "kind": "oracle_cache",
                "path": cache_root / token,
                "relative": token,
                "sha256": ORACLE_CACHE_ASSET_SHA256.get(token),
            }
        )
    wrapper_sha256 = manifest.get("gradle_wrapper_jar_sha256")
    if manifest.get("needs_gradle_wrapper_jar"):
        files.append(
            {
                "kind": "gradle_wrapper",
                "path": (
                    cache_root / "gradle-wrapper" / f"{wrapper_sha256}.jar"
                    if wrapper_sha256
                    else cache_root / "gradle-wrapper"
                ),
                "relative": f"gradle-wrapper/{wrapper_sha256}.jar",
                "sha256": wrapper_sha256,
            }
        )
    for entry in manifest.get("prefetch") or ():
        files.append(
            {
                "kind": "prefetch",
                "path": cache_root / entry["asset"],
                "relative": entry["asset"],
                "sha256": entry["sha256"],
                "workspace_path": entry["workspace_path"],
            }
        )
    for name in manifest.get("required_cache_dirs") or ():
        directories.append(
            {
                "kind": "cache_dir",
                "path": cache_root / name,
                "relative": name,
                "sha256": ORACLE_CACHE_DIR_SHA256.get(name),
                "exclude": (),
            }
        )
    return files, directories


def tsjs_dependency_root(
    task_id: str,
    manifest: dict[str, Any],
    legacy_root: Path,
    asset_root: Path | None,
) -> Path | None:
    """Root of the pinned dependency tree a tsjs replay provisions from.

    ``None`` for the pnpm-workspace contract, whose tree is materialized by the
    pinned package manager at replay time rather than pre-existing.
    """
    provisioning = manifest.get("node_modules")
    if provisioning == "shared_node_tools":
        return oracle_recovery_root(legacy_root) / "tsjs/_node_tools/node_modules"
    if provisioning == "pinned" and asset_root is not None:
        return asset_root / manifest["pinned_node_modules"]
    return None


def tsjs_required_executables(
    task_id: str,
    oracle: dict[str, Any],
    manifest: dict[str, Any],
    legacy_root: Path,
    asset_root: Path | None,
) -> list[dict[str, Any]]:
    """The exact binaries/package roots this Oracle's command executes.

    Each entry has ``path``, a human ``label``, and ``executable`` telling
    whether the exec bit is required. Shared by ``tsjs_blockers`` and
    ``asset_state`` so a required executable is always also fingerprinted.
    """
    entries: list[dict[str, Any]] = []
    node = tsjs_node_version(task_id, oracle)
    runtime_bin = node_runtime_bin(
        node, legacy_root, asset_root, manifest.get("runtime_root", "legacy")
    )
    for name in manifest.get("required_runtime_executables") or ("node",):
        entries.append(
            {
                "label": f"node runtime executable {name}",
                "path": (runtime_bin / name) if runtime_bin else None,
                "executable": True,
            }
        )
    dependency_root = tsjs_dependency_root(
        task_id, manifest, legacy_root, asset_root
    )
    for relative in manifest.get("required_dependency_executables") or ():
        entries.append(
            {
                "label": f"dependency executable {relative}",
                "path": (dependency_root / relative) if dependency_root else None,
                "executable": True,
            }
        )
    for relative in manifest.get("required_dependency_files") or ():
        entries.append(
            {
                "label": f"dependency file {relative}",
                "path": (dependency_root / relative) if dependency_root else None,
                "executable": False,
            }
        )
    for relative in manifest.get("required_runtime_files") or ():
        entries.append(
            {
                "label": f"node runtime file {relative}",
                "path": (
                    (runtime_bin / relative).resolve() if runtime_bin else None
                ),
                "executable": False,
            }
        )
    return entries


def tsjs_required_trees(
    task_id: str,
    oracle: dict[str, Any],
    manifest: dict[str, Any],
    legacy_root: Path,
    asset_root: Path | None,
) -> list[dict[str, Any]]:
    """Whole pinned payload trees this Oracle's runtime resolves through.

    Each entry carries the pinned ``sha256`` the tree must digest to. Shared by
    ``tsjs_blockers`` (fail closed) and ``asset_state`` (fingerprint), so the
    payload that is required is exactly the payload that is fingerprinted.
    """
    entries: list[dict[str, Any]] = []
    node = tsjs_node_version(task_id, oracle)
    runtime_bin = node_runtime_bin(
        node, legacy_root, asset_root, manifest.get("runtime_root", "legacy")
    )
    if manifest.get("runtime_tree_sha256"):
        entries.append(
            {
                "label": f"pinned Node {node} runtime tree",
                "path": runtime_bin.parent if runtime_bin else None,
                "sha256": manifest["runtime_tree_sha256"],
                "exclude": (),
            }
        )
    if manifest.get("dependency_tree_sha256"):
        entries.append(
            {
                "label": "pinned dependency tree",
                "path": tsjs_dependency_root(
                    task_id, manifest, legacy_root, asset_root
                ),
                "sha256": manifest["dependency_tree_sha256"],
                "exclude": tuple(manifest.get("dependency_tree_exclusions") or ()),
            }
        )
    if manifest.get("store_tree_sha256"):
        entries.append(
            {
                "label": "pinned pnpm store tree",
                "path": (asset_root / "tsjs/pnpm-store") if asset_root else None,
                "sha256": manifest["store_tree_sha256"],
                "exclude": tuple(manifest.get("store_tree_exclusions") or ()),
            }
        )
    return entries


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


def copy_tree(source: Path, dest: Path) -> None:
    """Materialize a private, writable copy of a pinned tree.

    Deliberately **not** a hardlink farm. ``cp -al`` shares inodes with the
    pinned source, so any tool that rewrites a file in place -- rather than
    replacing it -- writes straight through into the pinned asset and silently
    corrupts frozen evidence for every later replay. ``--reflink=auto`` takes a
    copy-on-write clone where the filesystem supports it and falls back to a
    real byte copy where it does not (ext4); either way the destination has its
    own inodes and no write can reach the source.
    """
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["cp", "-a", "--reflink=auto", f"{source}/.", str(dest)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if result.returncode != 0:
        shutil.copytree(source, dest, symlinks=True, dirs_exist_ok=True)


# Entries of the pinned Gradle/Maven cache home that are seeded into a
# per-replay writable scratch. Only the current task's own Gradle partition is
# copied; the other tasks' partitions are irrelevant to it and copying them
# would multiply the seeding cost for nothing.
JVM_CACHE_HOME_SHARED_ENTRIES = (".m2", ".cache", ".config")


def seed_jvm_cache_home(
    cache_home: Path, cache_index: str, destination: Path
) -> tuple[list[str], list[str]]:
    """Seed a disposable writable ``HOME``/``GRADLE_USER_HOME`` for one replay.

    Gradle and Maven genuinely need to write into their cache home (lock files,
    the wrapper distribution's own bookkeeping, journal and metadata updates),
    so it cannot be mounted read-only -- but the *pinned* cache home must never
    be the thing they write to, or the durable asset drifts out from under its
    own pinned digest. The pinned home is therefore copied into per-replay
    scratch and only that copy is mounted read-write; every immutable asset
    (jars, the init script, the file:// mirror, prefetched archives, the
    wrapper jar store) stays in the read-only ``/oracle-cache`` mount.

    Returns ``(seeded, faults)``.
    """
    destination.mkdir(parents=True, exist_ok=True)
    seeded: list[str] = []
    faults: list[str] = []
    wanted = list(JVM_CACHE_HOME_SHARED_ENTRIES) + [f"gradle-task-{cache_index}"]
    for name in wanted:
        source = cache_home / name
        if not source.is_dir():
            continue
        try:
            copy_tree(source, destination / name)
        except OSError as error:  # pragma: no cover - defensive
            faults.append(f"could not seed cache-home entry {name}: {error!r}")
            continue
        seeded.append(name)
    return seeded, faults


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
            # Publish atomically: copy to a PID-unique temp name inside the
            # same directory, then rename over the final path. A concurrent
            # duplicate replay can therefore never observe (or leave behind) a
            # half-written evidence log, and neither invocation can truncate
            # the other's file. The temp name ends in ``.new``, which
            # ``write_artifact_set`` already excludes from hashing.
            staging = destination / f"{log_file.name}.{os.getpid()}.new"
            shutil.copy2(log_file, staging)
            os.replace(staging, target)
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
            "--task-id values outside the reserve/recovered native-rust "
            f"scope ({len(TASK_IDS)} tasks): {sorted(unknown)}"
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
    manifest = NATIVE_MANIFEST.get(task_id)
    if manifest is None:
        return [
            f"no historical native harness manifest for {task_id} (task is "
            "outside the 20260728 reserve; a recovered contract via "
            "--asset-root native-rust=<path> is required)"
        ]
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
        if not cache_home.is_dir():
            blockers.append(
                f"missing recorded Gradle cache home: {cache_home} "
                "(environment.cache is required to avoid a network-"
                "dependent dependency resolution divergence)"
            )
        # Every pinned asset this replay may depend on is required *and*
        # hash-validated here, before anything executes. A missing or tampered
        # asset is a hard blocker: it is never silently skipped and never
        # substituted from the live network -- including for the two Oracles
        # that keep a network namespace, whose runs simply do not happen.
        files, directories = jvm_required_assets(
            task_id, oracle, legacy_root, asset_root
        )
        for entry in files:
            path = entry["path"]
            if entry["sha256"] is None:
                blockers.append(
                    f"unpinned {entry['kind']} asset {entry['relative']}: the "
                    "recovery has no pinned SHA-256 for it, so its content "
                    "cannot be validated"
                )
                continue
            if not path.is_file():
                blockers.append(
                    f"missing pinned {entry['kind']} asset: {path}"
                )
                continue
            digest = full_file_digest(path)
            if digest != entry["sha256"]:
                blockers.append(
                    f"pinned {entry['kind']} asset {path} has SHA-256 "
                    f"{digest}, expected {entry['sha256']}"
                )
        for entry in directories:
            path = entry["path"]
            if not path.is_dir() or not any(path.iterdir()):
                blockers.append(
                    f"missing pinned {entry['kind']}: {path} (required by the "
                    "reconstructed /oracle-cache/oracle.init.gradle, which "
                    "declares it as the first repository so a network-isolated "
                    "replay can resolve retired coordinates)"
                )
                continue
            if entry["sha256"] is None:
                blockers.append(
                    f"unpinned {entry['kind']} {entry['relative']}: the "
                    "recovery has no pinned whole-tree SHA-256 for it, so its "
                    "content cannot be validated"
                )
                continue
            digest = full_tree_digest(path, entry["exclude"])
            if digest != entry["sha256"]:
                blockers.append(
                    f"pinned {entry['kind']} {path} has content digest "
                    f"{digest}, expected {entry['sha256']}"
                )
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
            if not pinned.is_dir():
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

    # Requiring only the dependency *root* is not enough: a truncated or
    # partially pruned tree still has the root, and the resulting
    # "cannot find module" failure would be scored as a candidate failure.
    # Check the exact binaries/package roots the recovered command executes.
    for entry in tsjs_required_executables(
        task_id, oracle, manifest, legacy_root, asset_root
    ):
        path = entry["path"]
        if path is None:
            blockers.append(f"cannot resolve required {entry['label']}")
            continue
        if not path.is_file():
            blockers.append(f"missing required {entry['label']}: {path}")
            continue
        if entry["executable"] and not os.access(path, os.X_OK):
            blockers.append(
                f"required {entry['label']} is not executable: {path}"
            )

    # Whole-payload validation. Requiring a launcher shim plus a package.json is
    # not provenance: a synthetic tree containing only those still satisfies a
    # per-file existence check while none of the code that actually executes is
    # present. The pinned dependency tree, the pinned Node runtime and (for the
    # pnpm-workspace contract) the pinned store must digest to exactly the value
    # the frozen evidence was produced with.
    for entry in tsjs_required_trees(
        task_id, oracle, manifest, legacy_root, asset_root
    ):
        path = entry["path"]
        if path is None or not path.is_dir():
            blockers.append(f"missing {entry['label']}: {path}")
            continue
        digest = full_tree_digest(path, entry["exclude"])
        if digest != entry["sha256"]:
            blockers.append(
                f"{entry['label']} {path} has content digest {digest}, "
                f"expected {entry['sha256']}"
            )

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
    entry = recovered_entry(task_id)
    if entry is not None:
        # A recovered contract supersedes the historical one: for every
        # native/rust task the historical ``start/`` workspace was deleted, so
        # the recovered assets are the only executable route.
        return recovered_blockers(task_id, entry)
    if task_id in RECOVERED_ONLY_TASK_IDS:
        return [
            "no recovered replay contract available: this published-legacy "
            "task was never part of the 20260728 reserve and has no harness "
            "under --legacy-root; supply --asset-root native-rust=<path> "
            f"with a {RECOVERY_MANIFEST_NAME} entry for it (see "
            "scripts/recover_legacy_native_rust.py)"
        ]
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
    a complete, lstat-based content walk in every lane. Live, writable
    native/rust replay mounts use ``content_tree_digest`` (and
    ``content_file_digest`` for single files); pinned, read-only jvm/tsjs
    payloads use ``full_tree_digest`` (and ``full_entry_digest`` for single
    required entries). Both record every regular file's complete bytes, every
    symlink's literal target without following it, and every directory
    including empty ones. Nothing here is sampled or truncated -- every
    bounded helper has been deleted from this module -- so a tail edit, a
    same-size in-place swap, a removal from a tree of thousands, a retargeted
    launcher shim and a vanished empty directory all change the fingerprint,
    in a legacy JVM Gradle cache, a TS/JS node_modules tree and a recovered
    native/rust cache alike.
    """
    entry = recovered_entry(task_id)
    if entry is not None:
        return recovered_asset_state(entry)
    # Same normalization as ``replay_contract_fingerprint``: a native/rust
    # asset state must never resolve through the jvm/tsjs pinned root, and
    # vice versa. ``lane_asset_root`` is idempotent, so re-normalizing an
    # already-correct root is a no-op.
    asset_root = lane_asset_root(lane, task_id, asset_root)
    if lane == "native":
        manifest = NATIVE_MANIFEST.get(task_id)
        if manifest is None:
            # Native published-legacy task outside the 20260728 reserve with
            # no recovered contract: there is no asset state to fingerprint,
            # and task_blockers() already fails it closed.
            return {"historical_manifest": None}
        task_dir = legacy_root / "strict_screen_20260726" / manifest["task_dir"]
        workspace_source = (
            task_dir if manifest["workspace"] == "."
            else task_dir / manifest["workspace"]
        )
        harness_digests = {
            mount["host"]: content_file_digest(task_dir / mount["host"])
            for mount in manifest["harness_mounts"]
        }
        return {
            "docker_image_id": docker_image_id(manifest["docker_image"]),
            # the exact tree replay_native copies into scratch and patches
            "workspace": content_tree_digest(workspace_source),
            "harness_mounts": harness_digests,
        }
    if lane == "rust":
        environment = oracle.get("environment") or {}
        image = environment.get("docker_image")
        cargo_cache = environment.get("cargo_cache")
        try:
            task_dir = artifacts_directory(oracle, legacy_root)
            start_tree = content_tree_digest(task_dir / "start")
        except ValueError:
            start_tree = None
        return {
            "docker_image_id": docker_image_id(image) if image else None,
            # the exact "start/" tree replay_rust copies into scratch
            "start": start_tree,
            "cargo_cache": (
                content_tree_digest(
                    resolve_legacy_relative(cargo_cache, legacy_root, asset_root)
                )
                if cargo_cache
                else None
            ),
        }
    if lane == "jvm":
        image = jvm_container(oracle, task_id)
        cache = jvm_cache(oracle, task_id)
        repo = oracle.get("repo") or ""
        checkout = repo_checkout_dir(legacy_root, repo) if repo else None
        # Every pinned file jvm_required_assets() makes mandatory is bound by
        # its *full* SHA-256 (never a bounded prefix), and every required cache
        # directory by its complete tree digest, so a same-size in-place tamper
        # anywhere in a pinned archive still invalidates recorded rows.
        required_files, required_dirs = jvm_required_assets(
            task_id, oracle, legacy_root, asset_root
        )
        pinned_assets = {
            f"{entry['kind']}:{entry['relative']}": {
                "expected_sha256": entry["sha256"],
                "observed_sha256": full_file_digest(entry["path"]),
            }
            for entry in required_files
        }
        # Bound by *complete* tree content -- the same function preflight and
        # the post-run revalidation use.
        pinned_directories = {
            f"{entry['kind']}:{entry['relative']}": {
                "expected_sha256": entry["sha256"],
                "observed_sha256": full_tree_digest(
                    entry["path"], entry["exclude"]
                ),
            }
            for entry in required_dirs
        }
        session_tests = None
        if JVM_MANIFEST.get(task_id, {}).get("session_tests"):
            try:
                session_tests = full_tree_digest(
                    artifacts_directory(oracle, legacy_root, asset_root)
                    / "session-tests"
                )
            except ValueError:
                session_tests = None
        return {
            "docker_image_id": docker_image_id(image) if image else None,
            # the exact repo checkout replay_jvm materializes the start from
            "repo_checkout": full_tree_digest(checkout) if checkout else None,
            # every pinned /oracle-cache blob, prefetched archive and pinned
            # Gradle wrapper jar the recorded command requires
            "pinned_assets": pinned_assets,
            # the pinned cache directories oracle.init.gradle declares
            "pinned_directories": pinned_directories,
            "cache_home": (
                full_tree_digest(
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
                session_tree = full_tree_digest(
                    artifacts_directory(oracle, legacy_root, asset_root) / "session"
                )
            except ValueError:
                session_tree = None
        dependencies: dict[str, Any] | None = None
        provisioning = manifest["node_modules"]
        dependency_root = tsjs_dependency_root(
            task_id, manifest, legacy_root, asset_root
        )
        if dependency_root is not None:
            dependencies = full_tree_digest(dependency_root)
        elif provisioning == "pnpm_workspace" and asset_root is not None:
            dependencies = full_tree_digest(asset_root / "tsjs/pnpm-store")
        # Bind the *exact* binaries/entrypoints the recovered command runs, by
        # full content digest, so replacing or truncating any one of them
        # invalidates every row recorded against the old tree.
        required_executables = {
            entry["label"]: full_file_digest(entry["path"])
            for entry in tsjs_required_executables(
                task_id, oracle, manifest, legacy_root, asset_root
            )
        }
        # Bind the whole pinned payload trees too, so a change anywhere inside
        # the resolved runtime -- not just at a named entrypoint -- invalidates
        # recorded rows. This is the same set tsjs_blockers() requires.
        required_trees = {
            entry["label"]: {
                "expected_sha256": entry["sha256"],
                "observed_sha256": full_tree_digest(
                    entry["path"], entry["exclude"]
                ),
            }
            for entry in tsjs_required_trees(
                task_id, oracle, manifest, legacy_root, asset_root
            )
        }
        return {
            "node_runtime": (
                full_entry_digest(runtime / "node") if runtime else None
            ),
            "node_modules": dependencies,
            "required_executables": required_executables,
            "required_trees": required_trees,
            # the exact repo checkout replay_tsjs materializes the start from
            "repo_checkout": full_tree_digest(checkout) if checkout else None,
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
    asset_root, recovery_root = resolve_asset_roots(args)
    # Preflight reports *current* disk state, so it may never answer from a
    # memo left behind by an earlier scope in the same process.
    invalidate_cache_digests()
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
                "replay_contract": (
                    "recovered" if recovered_entry(task_id) else "historical"
                ),
                "blockers": blockers,
            }
        )
    report = {
        "schema_version": "reviewbench-legacy-reserve-preflight/v1",
        "created_at": BASE.utc_now(),
        "legacy_root": str(legacy_root),
        "asset_root": str(asset_root) if asset_root else None,
        "assets_root": str(recovery_root) if recovery_root else None,
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
    *,
    allow_missing: bool = False,
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
    if missing and not allow_missing:
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



def rust_test_count(text: str) -> int:
    """Total ``cargo test`` passes across *every* target in one run.

    ``cargo test`` prints one ``test result: ok. N passed`` line per test
    binary (lib, each integration test, doc-tests). The generic first-match
    counter would return only the first binary's count, which for e.g.
    fuel-tx's 77 recorded pass-to-pass tests reports 40 and would misjudge a
    fully passing replay as a failure. Summing every line matches the
    historical rust evaluator's own parser.
    """
    cleaned = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
    return sum(
        int(value)
        for value in re.findall(r"test result: ok\.\s+(\d+) passed", cleaned)
    )


def copy_cache_overlay(source: Path, destination: Path) -> Path:
    """Disposable writable copy of a mounted cache directory."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("cp"):
        result = subprocess.run(
            ["cp", "-a", "--reflink=auto", f"{source}/.", str(destination)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"cache copy of {source} failed: {result.stdout[-2000:]}"
            )
        return destination
    shutil.copytree(source, destination, symlinks=True)
    return destination


def recovered_docker_argv(
    entry: dict[str, Any],
    assets_root: Path,
    workspace: Path,
    cache_overlay_root: Path | None = None,
) -> list[str]:
    """Exact ``docker run`` argv for a recovered contract, built from the
    manifest fields (never from a hardcoded per-task string).

    With ``cache_overlay_root``, every mounted cache is first copied there and
    the copy is mounted read-write, leaving the canonical (digest-bound)
    caches untouched.
    """
    argv = ["docker", "run", "--rm"]
    if entry.get("network"):
        argv.append(f"--network={entry['network']}")
    if entry.get("user") == "current":
        argv += ["--user", f"{os.getuid()}:{os.getgid()}"]
    for host, container in (entry.get("mount") or {}).items():
        source = workspace if host == "." else workspace / host
        suffix = ":ro" if host in (entry.get("readonly_mounts") or []) else ""
        argv += ["-v", f"{source}:{container}{suffix}"]
    def cache_source(relative: str) -> Path:
        canonical = assets_root / relative
        if cache_overlay_root is None:
            return canonical
        overlay = cache_overlay_root / relative
        if not overlay.exists():
            copy_cache_overlay(canonical, overlay)
        return overlay

    for host, container in (entry.get("cache_mounts") or {}).items():
        argv += ["-v", f"{cache_source(host)}:{container}"]
    for mount in entry.get("harness_mounts") or []:
        argv += ["-v", f"{assets_root / mount['host']}:{mount['container']}:ro"]
    if entry.get("cargo_cache"):
        argv += [
            "-v", f"{cache_source(entry['cargo_cache'])}:/cargo-cache",
            "-e", "CARGO_HOME=/cargo-cache",
        ]
    for key, value in (entry.get("env") or {}).items():
        argv += ["-e", f"{key}={value}"]
    argv += ["-w", entry["workdir"], entry["docker_image"]]
    argv += list(entry["shell"]) + [entry["command"]]
    return argv


def purge_scratch(scratch: Path) -> None:
    """Delete a disposable replay workspace, including root-owned build state.

    A container build writes as root into the bind-mounted scratch copy, so a
    plain ``rmtree`` fails with EPERM; a throwaway cleanup container removes
    the root-owned tree first. Shared caches are never touched -- only this
    (task, model) scratch directory.
    """
    if not scratch.exists():
        return
    subprocess.run(
        ["docker", "run", "--rm", "-v", f"{scratch}:/w", "alpine",
         "sh", "-c", "rm -rf /w/* /w/.[!.]* 2>/dev/null; true"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    shutil.rmtree(scratch, ignore_errors=True)


def replay_recovered(
    task_id: str,
    model: str,
    task: dict[str, Any],
    oracle: dict[str, Any],
    generation: dict[str, Any],
    legacy_root: Path,
    work_root: Path,
    output: Path,
    timeout: int,
    lane: str,
) -> dict[str, Any]:
    """Replay one recovered contract for a (task, model) pair.

    Identical in shape to ``replay_native``/``replay_rust``: a disposable copy
    of the recovered workspace receives the staged candidate patch, the
    recovered harness files are copied in, and the recovered command is run
    verbatim in the recovered image. The recovered workspace itself is never
    mutated.
    """
    entry = recovered_entry(task_id)
    assert entry is not None
    assets_root: Path = RECOVERY_STATE["assets_root"]
    provenance = row_provenance_fields(
        task, oracle, task_id, lane, generation, legacy_root, [],
        lane_asset_root(lane, task_id),
    )

    # Re-verify every mounted input immediately before doing any work, not
    # just at preflight: a workspace or cache that went missing, drifted or
    # was tampered with between preflight and here must surface as
    # infrastructure, never as a candidate failure. Any digest state computed
    # earlier in this process is dropped first, so a cache mutated by an
    # earlier replay in the same process cannot be waved through.
    invalidate_cache_digests()
    live_blockers = recovered_blockers(task_id, entry)
    if live_blockers:
        row = {
            "id": task_id,
            "model": model,
            "lane": lane,
            "source_kind": "published_legacy",
            "start_sha": entry["start_sha"],
            "patch_sha256": generation.get("patch_sha256"),
            "status": INFRA_RETRY,
            "candidate_result": INFRA_RETRY,
            "correctness_ok": None,
            "patch_apply": None,
            "scope_ok": None,
            "replay_contract": "recovered",
            "error": "recovered asset verification failed before execution: "
                     + "; ".join(live_blockers),
            "asset_blockers": live_blockers,
        }
        row.update(provenance)
        return row

    scratch = invocation_scratch(work_root, lane, model, task_id)
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)
    workspace_scratch = scratch / "workspace"
    try:
        shutil.copytree(
            assets_root / entry["workspace"], workspace_scratch, symlinks=True
        )
    except (OSError, shutil.Error) as error:
        # An unreadable/root-owned leftover in the recovered workspace is an
        # infrastructure fault, not a model failure.
        purge_scratch(scratch)
        row = {
            "id": task_id,
            "model": model,
            "lane": lane,
            "source_kind": "published_legacy",
            "start_sha": entry["start_sha"],
            "patch_sha256": generation.get("patch_sha256"),
            "status": INFRA_RETRY,
            "candidate_result": INFRA_RETRY,
            "correctness_ok": None,
            "patch_apply": None,
            "scope_ok": None,
            "replay_contract": "recovered",
            "error": f"could not materialize recovered workspace: {error!r}"[:4000],
        }
        row.update(provenance)
        return row
    patch_workdir = workspace_scratch / entry["patch_subdir"]

    patch_path = Path(generation["patch_file"])
    ok, detail = apply_patch(patch_workdir, patch_path)
    if not ok:
        (scratch / "apply-error.log").write_text(detail, encoding="utf-8")
        evidence = persist_evidence(output, lane, model, task_id, scratch)
        row = {
            "id": task_id,
            "model": model,
            "lane": lane,
            "source_kind": "published_legacy",
            "start_sha": entry["start_sha"],
            "patch_sha256": generation.get("patch_sha256"),
            "status": "apply_error",
            "candidate_result": "apply_error",
            "correctness_ok": False,
            "patch_apply": False,
            "scope_ok": None,
            "replay_contract": "recovered",
            "error": detail[-4000:],
            "error_log": evidence.get("apply-error.log"),
        }
        row.update(provenance)
        return row

    # Harness files are copied in *after* the candidate patch so a candidate
    # can never overwrite or delete the oracle harness.
    for copy in entry.get("harness_copies") or []:
        destination = patch_workdir / copy["workspace_path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(assets_root / copy["host"], destination)

    patch_text = patch_path.read_text(errors="replace")
    changed = patch_changed_paths(patch_text)
    scope_ok = scope_is_safe(changed)

    # Mounted caches are copied into the disposable scratch and the *copies*
    # are mounted: cargo/conan write into their cache homes, and mutating the
    # canonical cache would both break the manifest's digest binding and let
    # one replay silently change the inputs of the next.
    cache_overlay = scratch / "caches"
    try:
        argv = recovered_docker_argv(
            entry, assets_root, workspace_scratch,
            cache_overlay_root=cache_overlay,
        )
    except (OSError, RuntimeError, shutil.Error) as error:
        purge_scratch(scratch)
        row = {
            "id": task_id,
            "model": model,
            "lane": lane,
            "source_kind": "published_legacy",
            "start_sha": entry["start_sha"],
            "patch_sha256": generation.get("patch_sha256"),
            "status": INFRA_RETRY,
            "candidate_result": INFRA_RETRY,
            "correctness_ok": None,
            "patch_apply": None,
            "scope_ok": None,
            "replay_contract": "recovered",
            "error": f"could not materialize disposable cache copy: {error!r}"[:4000],
        }
        row.update(provenance)
        return row
    log_path = scratch / "run.log"
    exit_code, duration, error = run_subprocess(
        argv, cwd=None, log_path=log_path, timeout=timeout
    )
    text = log_path.read_text(errors="replace") if log_path.is_file() else ""
    expected = BASE.pass_to_pass_count(oracle.get("pass_to_pass"))
    if lane == "rust":
        passed = rust_test_count(text)
        correctness_ok = exit_code == 0 and (not expected or passed >= expected)
    else:
        passed = expected if exit_code == 0 else 0
        correctness_ok = exit_code == 0
    evidence = persist_evidence(output, lane, model, task_id, scratch)
    purge_scratch(scratch)
    # The next blocker check in this process must re-read every cache from
    # disk rather than reuse anything computed around this run.
    invalidate_cache_digests()
    row = {
        "id": task_id,
        "model": model,
        "lane": lane,
        "source_kind": "published_legacy",
        "start_sha": entry["start_sha"],
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
        "replay_contract": "recovered",
        "pass_to_pass_total": expected,
        "pass_to_pass_passed": passed if correctness_ok else min(passed, expected),
        "failed_tests": [] if correctness_ok else ["oracle-run"],
    }
    row.update(provenance)
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
    scratch = invocation_scratch(work_root, "native", model, task_id)
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)
    provenance = row_provenance_fields(
        task, oracle, task_id, "native", generation, legacy_root, [],
        lane_asset_root("native", task_id),
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
    scratch = invocation_scratch(work_root, "rust", model, task_id)
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)
    provenance = row_provenance_fields(
        task, oracle, task_id, "rust", generation, legacy_root, [],
        lane_asset_root("rust", task_id),
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

    # Same immutability contract as the JVM lane: the *retained* cargo cache is
    # a pinned durable asset and is never mounted writable. Cargo writes into
    # CARGO_HOME (registry index locks, .package-cache), so a disposable copy
    # is seeded per replay and mounted read-write while the retained cache is
    # additionally exposed read-only for reference. A seeding failure is an
    # infrastructure fault, never a candidate verdict.
    cargo_cache = environment.get("cargo_cache")
    mounts = ["-v", f"{workspace_scratch}:/work"]
    cache_dir = None
    cargo_home_scratch = None
    asset_faults: list[str] = []
    if cargo_cache:
        cache_dir = resolve_legacy_relative(cargo_cache, legacy_root)
        cargo_home_scratch = scratch / "cargo-home"
        try:
            copy_tree(cache_dir, cargo_home_scratch)
        except OSError as error:  # pragma: no cover - defensive
            asset_faults.append(f"could not seed cargo home: {error!r}")
        mounts += [
            "-v", f"{cargo_home_scratch}:/cargo-cache",
            "-v", f"{cache_dir}:/cargo-cache-pinned:ro",
            "-e", "CARGO_HOME=/cargo-cache",
        ]
    if asset_faults:
        (scratch / "asset-error.log").write_text(
            "\n".join(asset_faults) + "\n", encoding="utf-8"
        )
        evidence = persist_evidence(output, "rust", model, task_id, scratch)
        row = {
            "id": task_id,
            "model": model,
            "lane": "rust",
            "source_kind": "published_legacy",
            "start_sha": oracle["start_sha"],
            "patch_sha256": generation.get("patch_sha256"),
            "status": INFRA_RETRY,
            "candidate_result": INFRA_RETRY,
            "correctness_ok": None,
            "patch_apply": True,
            "scope_ok": scope_ok,
            "error": "; ".join(asset_faults)[-4000:],
            "pinned_asset_faults": asset_faults,
            "error_log": evidence.get("asset-error.log"),
        }
        row.update(provenance)
        return row
    pinned_cargo_cache_digest = full_tree_digest(cache_dir) if cache_dir else None
    docker_cmd = [
        "docker", "run", "--rm", "--network=host",
        *mounts,
        # Same environment the historical rust evaluator exported.
        "-e", "CARGO_TERM_COLOR=never", "-e", "RUST_BACKTRACE=1",
        "-e", "CARGO_BUILD_JOBS=16", "-e", "RUSTFLAGS=--cap-lints=allow",
        "-w", "/work",
        environment["docker_image"],
        # ``/bin/sh -c``, not a *login* shell: sourcing /etc/profile resets
        # PATH and drops the image's CARGO_HOME/bin, making ``cargo``
        # unresolvable (observed as "sh: 1: cargo: not found").
        "/bin/sh", "-c", oracle["workspace_command"],
    ]
    log_path = scratch / "run.log"
    exit_code, duration, error = run_subprocess(
        docker_cmd, cwd=None, log_path=log_path, timeout=timeout
    )
    # Defensive post-run revalidation of the pinned cargo cache: it is only
    # ever mounted read-only, so drift is impossible by construction; if it is
    # observed anyway the row is infrastructure, never a candidate verdict.
    invalidate_cache_digests()
    drifted_assets = (
        [str(cache_dir)]
        if cache_dir is not None
        and full_tree_digest(cache_dir) != pinned_cargo_cache_digest
        else []
    )
    text = log_path.read_text(errors="replace") if log_path.is_file() else ""
    passed = rust_test_count(text)
    expected = BASE.pass_to_pass_count(oracle.get("pass_to_pass"))
    correctness_ok = (
        exit_code == 0
        and (not expected or passed >= expected)
        and not drifted_assets
    )
    status = "pass" if correctness_ok else "fail"
    if drifted_assets:
        status = INFRA_RETRY
    evidence = persist_evidence(output, "rust", model, task_id, scratch)
    row = {
        "id": task_id,
        "model": model,
        "lane": "rust",
        "source_kind": "published_legacy",
        "start_sha": oracle["start_sha"],
        "patch_sha256": generation.get("patch_sha256"),
        "status": status,
        "candidate_result": status,
        "correctness_ok": None if drifted_assets else correctness_ok,
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
        "pinned_assets_drifted_during_run": drifted_assets,
        "pinned_cargo_cache_mounted_readonly": cache_dir is not None,
        "seeded_cargo_home": cargo_home_scratch is not None,
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
    cache_home_scratch: Path | None = None,
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
    # The pinned cache home is *never* the thing the build writes to; a
    # disposable, already-seeded copy is. Callers always pass one -- the
    # fallback only exists so the pinned home can still be located.
    writable_home = cache_home_scratch if cache_home_scratch is not None else cache_home
    # The container passwd file encodes the *current* uid/gid, so it is host
    # state rather than a pinned artifact: it is written into the disposable
    # per-replay scratch instead of the pinned asset root, which therefore
    # stays read-only and hash-stable across replays.
    task_dir.mkdir(parents=True, exist_ok=True)
    passwd = task_dir / "container.passwd"
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
        # /workspace and /task are disposable per-replay scratch, so they are
        # the only writable bind mounts besides the seeded cache home.
        "-v", f"{workspace}:/workspace",
        "-v", f"{task_dir}:/task",
        # Every immutable, digest-pinned asset the recorded command references
        # -- jacocoagent.jar, oracle.init.gradle, the file:// m2-mirror, the
        # pinned Gradle wrapper store and the prefetched archives -- is mounted
        # strictly read-only, so no build can mutate a durable asset out from
        # under its own pinned digest.
        "-v", f"{cache_root}:/oracle-cache:ro",
        "-v", f"{writable_home}:/cachehome",
        "-v", f"{passwd}:/etc/passwd:ro",
        "-w", "/workspace",
        jvm_container(oracle, task_id),
        "bash", "-lc", shell_command,
    ]
    return command


def install_pinned_gradle_wrapper(
    workspace: Path,
    cache_root: Path,
    pinned_sha256: str | None,
) -> tuple[str, str | None]:
    """Install the repository's own Gradle wrapper jar from the pinned store.

    Solr's checked-in ``gradlew`` fetches ``gradle-wrapper.jar`` from
    raw.githubusercontent.com when it is absent, which a network-isolated
    replay cannot do -- and failing there would be scored as a model failure.

    The jar is pinned as a durable asset keyed by *the repository's own*
    recorded ``gradle-wrapper.jar.sha256``, and three things must agree before
    anything is installed: the digest the checked-out revision records, the
    digest the recovery manifest pins, and the actual bytes in the durable
    store. Any disagreement returns a failure status so the caller can stop
    the replay instead of letting the build fall back to the live network.

    Returns ``(status, sha256)`` where status is one of ``installed``,
    ``already-present``, ``no-marker``, ``bad-marker``, ``manifest-mismatch``,
    ``missing-pinned-jar`` or ``digest-mismatch``.
    """
    marker = workspace / "gradle/wrapper/gradle-wrapper.jar.sha256"
    jar = workspace / "gradle/wrapper/gradle-wrapper.jar"
    if jar.is_file():
        return "already-present", None
    if not marker.is_file():
        return "no-marker", None
    expected = marker.read_text(encoding="utf-8", errors="replace").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        return "bad-marker", None
    if pinned_sha256 is not None and pinned_sha256 != expected:
        return "manifest-mismatch", None
    pinned = cache_root / "gradle-wrapper" / f"{expected}.jar"
    if not pinned.is_file():
        return "missing-pinned-jar", None
    if full_file_digest(pinned) != expected:
        return "digest-mismatch", None
    jar.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(pinned, jar)
    return "installed", expected


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
    cache_home = resolve_legacy_relative(
        jvm_cache(oracle, task_id), legacy_root, asset_root
    )
    cache_root = cache_home.parent
    scratch = invocation_scratch(work_root, "jvm", model, task_id)
    workspace_scratch = scratch / "workspace"
    task_scratch = scratch / "task"
    cache_home_scratch = scratch / "cachehome"
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

        # Pinned assets are installed strictly from the validated durable
        # store. Anything missing or mismatched is an *infrastructure* fault
        # that stops the replay outright -- it is never quietly skipped so the
        # build can fetch a live substitute, which is the only reason the two
        # network-namespace Oracles could otherwise drift.
        prefetched: list[dict[str, str]] = []
        asset_faults: list[str] = []
        for entry in manifest.get("prefetch") or ():
            source = cache_root / entry["asset"]
            digest = full_file_digest(source)
            if digest is None:
                asset_faults.append(f"missing pinned prefetch asset: {source}")
                continue
            if digest != entry["sha256"]:
                asset_faults.append(
                    f"pinned prefetch asset {source} has SHA-256 {digest}, "
                    f"expected {entry['sha256']}"
                )
                continue
            destination = workspace_scratch / entry["workspace_path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            prefetched.append(
                {"workspace_path": entry["workspace_path"], "sha256": digest}
            )

        # Seed the disposable writable HOME/GRADLE_USER_HOME from the pinned
        # cache home. The pinned home itself is never mounted, so a build
        # cannot write into a durable asset even by accident.
        seeded_cache_entries, seed_faults = seed_jvm_cache_home(
            cache_home, manifest["cache_index"], cache_home_scratch
        )
        asset_faults.extend(seed_faults)

        wrapper_sha256 = None
        if manifest.get("needs_gradle_wrapper_jar"):
            wrapper_status, wrapper_sha256 = install_pinned_gradle_wrapper(
                workspace_scratch, cache_root,
                manifest.get("gradle_wrapper_jar_sha256"),
            )
            if wrapper_status not in ("installed", "already-present"):
                asset_faults.append(
                    f"pinned Gradle wrapper jar unusable ({wrapper_status})"
                )

        patch_path = Path(generation["patch_file"])
        ok, detail = apply_patch(workspace_scratch, patch_path)
        if asset_faults:
            # Fail closed: a pinned asset the recovery declares mandatory is
            # missing or no longer matches its pinned digest. The Oracle is not
            # executed at all, so no candidate can be scored against a
            # degraded harness and no run can silently substitute a live
            # download for the pinned bytes.
            (scratch / "asset-error.log").write_text(
                "\n".join(asset_faults) + "\n", encoding="utf-8"
            )
            log_kind = "asset-error.log"
            result = {
                "id": task_id,
                "model": model,
                "lane": "jvm",
                "source_kind": "published_legacy",
                "start_sha": start_sha,
                "patch_sha256": generation.get("patch_sha256"),
                "status": INFRA_RETRY,
                "candidate_result": INFRA_RETRY,
                "correctness_ok": None,
                "patch_apply": None,
                "scope_ok": None,
                "error": "; ".join(asset_faults)[-4000:],
                "pinned_asset_faults": asset_faults,
            }
        elif not ok:
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

            # A declared prime_command is *mandatory*, not best effort. It is
            # what reproduces the historical workspace state the scored command
            # depends on (RepoSense's published command carries
            # -x compileTestJava and therefore finds no test classes at all in
            # a workspace nothing has compiled yet). If priming fails, times
            # out, or errors, the scored command must not run: the resulting
            # "no tests found" build failure is infrastructure, and scoring it
            # would turn missing infrastructure into a model failure.
            prime_command = manifest.get("prime_command")
            prime_result: dict[str, Any] | None = None
            if prime_command:
                prime_exit, prime_duration, prime_error = run_subprocess(
                    jvm_docker_command(
                        oracle, task_id, legacy_root, asset_root,
                        workspace_scratch, task_scratch, prime_command, offline,
                        cache_home_scratch,
                    ),
                    cwd=None,
                    log_path=scratch / "prime.log",
                    timeout=timeout,
                )
                prime_result = {
                    "command": prime_command,
                    "exit_code": prime_exit,
                    "duration_seconds": prime_duration,
                    "error": prime_error,
                    "ok": prime_exit == 0 and not prime_error,
                }
            if prime_result is not None and not prime_result["ok"]:
                log_kind = "prime.log"
                result = {
                    "id": task_id,
                    "model": model,
                    "lane": "jvm",
                    "source_kind": "published_legacy",
                    "start_sha": start_sha,
                    "patch_sha256": generation.get("patch_sha256"),
                    "status": INFRA_RETRY,
                    "candidate_result": INFRA_RETRY,
                    "correctness_ok": None,
                    "patch_apply": True,
                    "scope_ok": scope_ok,
                    "error": (
                        "mandatory prime_command failed with exit "
                        f"{prime_result['exit_code']}"
                        + (f": {prime_result['error']}" if prime_result["error"] else "")
                    ),
                    "prime_result": prime_result,
                    "scored_command_executed": False,
                }
                raise _PrimeFailure(result)
            log_path = scratch / "run.log"
            exit_code, duration, error = run_subprocess(
                jvm_docker_command(
                    oracle, task_id, legacy_root, asset_root,
                    workspace_scratch, task_scratch, str(oracle["command"]), offline,
                    cache_home_scratch,
                ),
                cwd=None,
                log_path=log_path,
                timeout=timeout,
            )
            # The pinned inputs must still be the pinned inputs *after* the
            # run: an Oracle that keeps a network namespace could otherwise
            # have replaced a pre-placed archive with a freshly downloaded one
            # mid-build, which would quietly reintroduce a live dependency.
            tampered = [
                entry["workspace_path"]
                for entry in prefetched
                if full_file_digest(workspace_scratch / entry["workspace_path"])
                != entry["sha256"]
            ]
            # Defensive post-run re-validation of the durable assets
            # themselves. /oracle-cache is mounted read-only and the cache home
            # is a disposable copy, so this must always hold; if it ever does
            # not, something impossible happened and the row is infrastructure,
            # never a candidate verdict.
            invalidate_cache_digests()
            required_files_now, required_dirs_now = jvm_required_assets(
                task_id, oracle, legacy_root, asset_root
            )
            drifted_assets = [
                str(entry["path"])
                for entry in required_files_now
                if entry["sha256"] is not None
                and full_file_digest(entry["path"]) != entry["sha256"]
            ]
            # Required /oracle-cache directories are revalidated the same way:
            # whole-tree content, every file, after the run.
            drifted_assets += [
                str(entry["path"])
                for entry in required_dirs_now
                if entry["sha256"] is not None
                and full_tree_digest(entry["path"], entry["exclude"])
                != entry["sha256"]
            ]
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
                exit_code == 0
                and failures == 0
                and executed >= expected
                and not tampered
                and not drifted_assets
            )
            status = "pass" if correctness_ok else "fail"
            if tampered or drifted_assets:
                # A replaced pinned input is an infrastructure fault, never a
                # candidate failure.
                status = INFRA_RETRY
            result = {
                "id": task_id,
                "model": model,
                "lane": "jvm",
                "source_kind": "published_legacy",
                "start_sha": start_sha,
                "patch_sha256": generation.get("patch_sha256"),
                "status": status,
                "candidate_result": status,
                "correctness_ok": (
                    None if (tampered or drifted_assets) else correctness_ok
                ),
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
                "prefetched_assets_replaced_during_run": tampered,
                "pinned_assets_drifted_during_run": drifted_assets,
                "seeded_cache_home_entries": seeded_cache_entries,
                "pinned_cache_root_mounted_readonly": True,
                "prime_result": prime_result,
                "scored_command_executed": True,
            }
    except _PrimeFailure as failure:
        result = failure.result
    finally:
        shutil.rmtree(workspace_scratch, ignore_errors=True)
    evidence = persist_evidence(output, "jvm", model, task_id, scratch)
    if log_kind in ("apply-error.log", "asset-error.log"):
        result["error_log"] = evidence.get(log_kind)
    elif log_kind == "prime.log":
        # A failed mandatory prime never produced a scored run, so its own log
        # is the evidence for this row.
        result["error_log"] = evidence.get("prime.log")
        result["prime_log"] = evidence.get("prime.log")
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
    log_prefix: str = "",
) -> tuple[bool, str]:
    """Make the historical dependency tree available without re-resolving it.

    Returns ``(ok, detail)``. A failed install is infrastructure; a failed
    candidate build is classified only after the same build runs at exact
    start.
    """
    provisioning = manifest["node_modules"]
    destination = workspace / "node_modules"
    if provisioning == "shared_node_tools":
        # The retained historical workspace
        # (oracle_recovery/tsjs/_work/discourse__discourse) carries a
        # node_modules *symlink* into this tree, and without that resolution
        # root the session Vitest config cannot resolve "vitest/config" and
        # every candidate fails for infrastructure reasons. A symlink would let
        # the runner write its scratch cache straight back into the retained
        # tree, mutating frozen evidence and invalidating its pinned digest for
        # every later replay, so the tree is copied into the workspace instead
        # -- identical module resolution, zero writes to the retained asset.
        # The copy is a real (or copy-on-write) copy, never a hardlink farm: a
        # shared inode would let an in-place rewrite reach straight back into
        # the pinned source. The runner's own cache directory is additionally
        # pruned from the copy so it is recreated rather than reused.
        source = oracle_recovery_root(legacy_root) / "tsjs/_node_tools/node_modules"
        copy_tree(source, destination)
        for scratch_name in manifest.get("dependency_tree_exclusions") or ():
            shutil.rmtree(destination / scratch_name, ignore_errors=True)
        return True, "copied retained shared node tools"
    if provisioning == "pinned":
        assert asset_root is not None
        copy_tree(asset_root / manifest["pinned_node_modules"], destination)
        return True, "copied pinned node_modules snapshot"
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
            log_path=scratch / f"{log_prefix}install.log",
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
                log_path=scratch / f"{log_prefix}build.log",
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
    scratch = invocation_scratch(work_root, "tsjs", model, task_id)
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
                log_kind = (
                    "build.log"
                    if provision_detail.startswith("build_command failed")
                    else "install.log"
                )
                baseline_ok = False
                baseline_detail = ""
                if log_kind == "build.log":
                    baseline_workspace = scratch / "baseline-workspace"
                    materialize_start(
                        repo_source,
                        task["start_sha"],
                        baseline_workspace,
                    )
                    try:
                        if manifest.get("session_from_recovery"):
                            session_dir = (
                                artifacts_directory(
                                    oracle, legacy_root, asset_root
                                )
                                / "session"
                            )
                            shutil.copytree(
                                session_dir,
                                baseline_workspace / "session",
                                symlinks=True,
                            )
                        baseline_ok, baseline_detail = (
                            provision_tsjs_dependencies(
                                task_id,
                                manifest,
                                legacy_root,
                                asset_root,
                                baseline_workspace,
                                env,
                                scratch,
                                timeout,
                                offline,
                                log_prefix="baseline-",
                            )
                        )
                    finally:
                        shutil.rmtree(
                            baseline_workspace,
                            ignore_errors=True,
                        )
                if baseline_ok:
                    result = {
                        "id": task_id,
                        "model": model,
                        "lane": "tsjs",
                        "source_kind": "published_legacy",
                        "start_sha": task["start_sha"],
                        "patch_sha256": generation.get("patch_sha256"),
                        "status": "fail",
                        "candidate_result": "fail",
                        "correctness_ok": False,
                        "patch_apply": True,
                        "scope_ok": scope_ok,
                        "error": provision_detail,
                        "failure_validated_by_baseline": True,
                        "pass_to_pass_total": manifest["expected_tests"],
                        "pass_to_pass_passed": 0,
                        "failed_tests": ["tsjs-build"],
                        "baseline_dependency_provisioning": baseline_detail,
                    }
                else:
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
                        "error": (
                            provision_detail
                            + (
                                f"; exact-start baseline: {baseline_detail}"
                                if baseline_detail
                                else ""
                            )
                        ),
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
                # Defensive post-run re-validation: the pinned payload trees
                # are copied, never shared by inode or symlink, so a replay
                # cannot reach them -- if one drifted anyway, the row is
                # infrastructure rather than a candidate verdict.
                invalidate_cache_digests()
                drifted_trees = [
                    entry["label"]
                    for entry in tsjs_required_trees(
                        task_id, oracle, manifest, legacy_root, asset_root
                    )
                    if full_tree_digest(entry["path"], entry["exclude"])
                    != entry["sha256"]
                ]
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
                    exit_code == 0
                    and passed >= expected
                    and signature_ok
                    and not drifted_trees
                )
                status = "pass" if correctness_ok else "fail"
                if drifted_trees:
                    status = INFRA_RETRY
                result = {
                    "id": task_id,
                    "model": model,
                    "lane": "tsjs",
                    "source_kind": "published_legacy",
                    "start_sha": task["start_sha"],
                    "patch_sha256": generation.get("patch_sha256"),
                    "status": status,
                    "candidate_result": status,
                    "correctness_ok": None if drifted_trees else correctness_ok,
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
                    "pinned_trees_drifted_during_run": drifted_trees,
                }
    finally:
        # Defensive: never follow a symlink out of the disposable workspace
        # while cleaning it up.
        link = workspace_scratch / "node_modules"
        if link.is_symlink():
            link.unlink()
        shutil.rmtree(workspace_scratch, ignore_errors=True)
    evidence = persist_evidence(output, "tsjs", model, task_id, scratch)
    if log_kind == "apply-error.log":
        result["error_log"] = evidence.get("apply-error.log")
    else:
        result["log"] = evidence.get(log_kind)
        for extra in (
            "install.log",
            "build.log",
            "baseline-install.log",
            "baseline-build.log",
        ):
            if extra != log_kind and evidence.get(extra):
                result[extra.replace("-", "_").replace(".", "_")] = evidence[extra]
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
    asset_root, _recovery_root = resolve_asset_roots(args)
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
    asset_root, recovery_root = resolve_asset_roots(args)
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
    generations = load_staged_generation(
        output,
        model,
        {row["id"] for row in rows},
        allow_missing=True,
    )

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
        # The one root this row's replay actually reads. Every provenance and
        # replay call below is given *this* value rather than the raw CLI
        # jvm/tsjs root, so a combined invocation that also supplies the other
        # lane group's root cannot make this row look stale.
        lane_root = lane_asset_root(lane, task_id, asset_root)
        generation = generations.get(task_id)
        if generation is None:
            if blockers:
                existing_row = by_lane[lane].get(key)
                if existing_row is not None:
                    retract_raw_row(legacy_dir, lane, task_id, model)
                    by_lane[lane] = existing_raw_rows(legacy_dir, lane)
                    summary["retracted_stale_rows"] += 1
                summary["blocked"] += 1
                summary["errors"].append(
                    {"id": task_id, "blockers": blockers}
                )
                continue
            raise ValueError(
                f"ready legacy task {task_id} has no staged generation "
                f"row for {model!r}"
            )

        existing_row = by_lane[lane].get(key)
        if existing_row is not None and not args.force:
            if not row_is_stale(
                existing_row, task, oracle, task_id, lane, generation,
                legacy_root, blockers, lane_root,
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
                    generation, legacy_root, blockers, lane_root,
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
            # One (lane, model, task) execution at a time across processes:
            # covers scratch creation, the container run, evidence
            # persistence and scratch cleanup.
            with execution_lock(legacy_dir, lane, model, task_id):
                if recovered_entry(task_id) is not None:
                    raw_row = replay_recovered(
                        task_id, model, task, oracle, generation, legacy_root,
                        work_root, output, args.timeout, lane,
                    )
                else:
                    raw_row = REPLAY_FUNCTIONS[lane](
                        task_id, model, task, oracle, generation, legacy_root,
                        work_root, output, args.timeout,
                        **(
                            {
                                "asset_root": lane_root,
                                "offline": not args.allow_network,
                            }
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
                    blockers, lane_root,
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
        action="append",
        default=None,
        metavar="[LABEL=]PATH",
        help="Durable recovery-asset root, repeatable. Two kinds exist and a "
        "combined run needs both, so each value is either LABEL=PATH with "
        f"LABEL in {{{', '.join(ASSET_ROOT_LABELS)}}} or a bare PATH that is "
        "classified by the manifest it actually holds (never by its name). "
        f"A 'jvm-tsjs' root ({JVM_TSJS_MANIFEST_NAME}) supplies the pinned "
        "assets the retained session root no longer carries (JVM "
        "/oracle-cache + Gradle/Maven cache home, pinned node_modules "
        "snapshots, pinned pnpm store and Node runtimes); recorded Oracle "
        "paths resolve there only when the retained root lacks them, so "
        f"retained evidence always wins. A 'native-rust' root "
        f"({RECOVERY_MANIFEST_NAME}, scripts/recover_legacy_native_rust.py) "
        "supplies whole recovered replay contracts that supersede the "
        "historical ones for the tasks they cover and are re-verified "
        "against live disk/Docker state; without it, native/rust tasks whose "
        "historical start/ workspace was deleted stay blocked. Repeating a "
        "label, or a bare PATH that matches neither or both manifests, is an "
        "error rather than a guess. Defaults to $REVIEWBENCH_LEGACY_ASSETS "
        "when set, which keeps the pre-existing single-root reserve "
        "invocations working verbatim.",
    )
    parser.add_argument(
        "--task-id",
        action="append",
        help="Restrict to a subset of the reserve/recovered task IDs (repeatable)",
    )
    parser.add_argument(
        "--assets-root",
        dest="asset_root",
        action="append",
        help=argparse.SUPPRESS,
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
