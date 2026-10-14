"""Bounded classifier requests, receipt reuse, and download-only import."""
from contextlib import contextmanager, nullcontext
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import prepare_agentic_sbt_dependencies as prepare
from agentic_runtime_migration import jvm_managed
from reviewbench_maintainability.agentic.contracts import canonical_hash, component, file_hash
from reviewbench_maintainability.agentic.resources import CURRENT_JOB, GiB


COMMAND = (
    "/oracle-tools/sbt -batch 'set Global / enableWasmEverywhere := true' "
    "'testSuite2_13/Test/testOnly *ExportsTest *StackTraceTest *LinkingInfoTest'"
)
PROJECTS = ["linker2_12", "scalalibInternal2_12"]
CLASSIFIERS = ["scalalibInternal2_12"]
ORIGINAL_ARGUMENTS = [
    "-batch", "set Global / enableWasmEverywhere := true", "testSuite2_13/Test/update",
    "linker2_12/update", "scalalibInternal2_12/update",
]
CLASSIFIER_ARGUMENTS = [*ORIGINAL_ARGUMENTS, "scalalibInternal2_12/updateClassifiers"]
DOWNLOAD = ".cache/coursier/v1/https/repo1.maven.org/maven2/example/library/1.0/"


def put(root, name, contents=b"downloaded bytes"):
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(contents)
    return target


def test_classifiers_are_opt_in_and_preserve_original_scopes():
    assert prepare.dependency_arguments(COMMAND, PROJECTS) == ORIGINAL_ARGUMENTS
    assert prepare.dependency_arguments(
        COMMAND, PROJECTS, classifier_projects=CLASSIFIERS,
    ) == CLASSIFIER_ARGUMENTS
    assert not any("testOnly" in arg or "retrieve" in arg for arg in CLASSIFIER_ARGUMENTS)


def test_classifier_targets_are_deduplicated_without_losing_binary_updates():
    assert prepare.dependency_arguments(
        COMMAND, PROJECTS + PROJECTS, classifier_projects=CLASSIFIERS + CLASSIFIERS,
    ) == CLASSIFIER_ARGUMENTS


@pytest.mark.parametrize("project", ["p", "_p", "1", "some-public.project_2-12", "p" * 128])
def test_classifier_ids_are_literal(project):
    assert prepare.dependency_arguments(
        "/oracle-tools/sbt -batch test", classifier_projects=[project],
    ) == ["-batch", "Test/update", project + "/updateClassifiers"]


@pytest.mark.parametrize("project", [
    "", "../private", ".", "-project", "p/updateClassifiers", "p/Test",
    "p;reload", "p; runMain Harness", "p\nreload", "p\rreload", "p update",
    "$PROJECT", "$(touch output)", "`id`", "{https://example.org/build}root",
    "p:compile", "p*", "p\\reload", "p'quoted", "p\"quoted", "p" * 129, "projéct",
])
def test_classifier_ids_reject_shell_sbt_expressions_and_unbounded_names(project):
    with pytest.raises(prepare.ContractError, match="classifier project"):
        prepare.dependency_arguments(COMMAND, classifier_projects=[project])


def test_classifier_request_count_is_bounded_even_for_duplicate_ids():
    with pytest.raises(prepare.ContractError, match="at most 16"):
        prepare.dependency_arguments(COMMAND, classifier_projects=["p"] * 17)
    assert len(prepare.dependency_arguments(
        "/oracle-tools/sbt -batch test", classifier_projects=[f"p{i}" for i in range(16)],
    )) == 18


def test_classifier_request_needs_an_original_test_configuration():
    with pytest.raises(prepare.ContractError, match="original test command"):
        prepare.dependency_arguments(
            "/oracle-tools/sbt -batch root/compile", classifier_projects=CLASSIFIERS,
        )
    with pytest.raises(prepare.ContractError):
        prepare.dependency_arguments(
            "/oracle-tools/sbt -batch 'root/runMain Harness'", classifier_projects=CLASSIFIERS,
        )


@pytest.mark.parametrize("command,expected", [
    ("/oracle-tools/sbt -batch '++2.12.20' 'p/Test/testOnly *One' 'p/testQuick *Two'",
     ["-batch", "++2.12.20", "p/Test/update", "p/updateClassifiers"]),
    ("/oracle-tools/sbt -batch 'p/testOnly *One' 'q/Test/testOnly *Two'",
     ["-batch", "p/Test/update", "q/Test/update", "p/updateClassifiers"]),
])
def test_classifiers_keep_cross_version_flags_and_each_original_test_scope(command, expected):
    assert prepare.dependency_arguments(command, classifier_projects=["p"]) == expected


@pytest.mark.parametrize("resolve_only", [False, True])
def test_both_preparation_modes_execute_classifiers_without_replacing_existing_operations(resolve_only):
    actual = prepare.preparation_arguments(COMMAND, CLASSIFIER_ARGUMENTS, resolve_only=resolve_only)
    if resolve_only:
        assert actual == CLASSIFIER_ARGUMENTS
    else:
        assert actual == [
            "-batch", "set Global / enableWasmEverywhere := true",
            "testSuite2_13/Compile/compile", *CLASSIFIER_ARGUMENTS[2:],
        ]
    assert not any("transitiveClassifiers" in argument or "testOnly" in argument for argument in actual)
    assert shlex.split(shlex.join(actual)) == actual


def test_request_identity_keeps_all_raw_requested_lists_and_effective_arguments():
    projects, classifiers = PROJECTS.copy(), CLASSIFIERS.copy()
    request = prepare.dependency_request(COMMAND, projects, classifiers, resolve_only=True)
    assert request == {
        "dependency_projects": PROJECTS, "classifier_projects": CLASSIFIERS,
        "resolve_only": True, "arguments": CLASSIFIER_ARGUMENTS,
    }
    projects.append("later")
    classifiers.append("later")
    assert request["dependency_projects"] == PROJECTS
    assert request["classifier_projects"] == CLASSIFIERS
    changed = [
        prepare.dependency_request(COMMAND, PROJECTS, [], resolve_only=True),
        prepare.dependency_request(COMMAND, PROJECTS, ["other"], resolve_only=True),
        prepare.dependency_request(COMMAND, PROJECTS, CLASSIFIERS + CLASSIFIERS, resolve_only=True),
        prepare.dependency_request(COMMAND, PROJECTS[::-1], CLASSIFIERS, resolve_only=True),
        prepare.dependency_request(COMMAND, PROJECTS + PROJECTS, CLASSIFIERS, resolve_only=True),
        prepare.dependency_request(COMMAND, PROJECTS, CLASSIFIERS, resolve_only=False),
    ]
    assert all(canonical_hash(other) != canonical_hash(request) for other in changed)


@pytest.mark.parametrize("layout", ["https", "v1/https"])
def test_overlay_exports_sources_and_javadoc_but_not_classifier_failures_or_seed_links(tmp_path, layout):
    seed, cache, output = tmp_path / "seed", tmp_path / "cache", tmp_path / "overlay"
    prefix = f".cache/coursier/{layout}/repo1.maven.org/maven2/example/library/1.0/"
    binary = prefix + "library-1.0.jar"
    put(seed, binary)
    prepare.seed_thin_cache(seed, cache, [binary])
    expected = {}
    for classifier in ("sources", "javadoc"):
        name = prefix + f"library-1.0-{classifier}.jar"
        expected[name] = file_hash(put(cache, name, (classifier + " bytes").encode()))
        expected[name + ".sha1"] = file_hash(put(cache, name + ".sha1", b"checksum"))
        put(cache, name + ".part")
        put(cache, prefix + "." + name.rsplit("/", 1)[1] + ".error", b"")
    put(cache, ".ivy2/local/answer.jar")
    put(cache, ".cache/coursier/file/workspace/target/answer.jar")
    changed, removed = prepare.dependency_overlay(seed, cache, [binary], output)
    assert changed == expected and not removed
    assert os.readlink(cache / binary) == prepare.SBT_BASE + "/" + binary
    assert not (output / binary).exists()
    assert {name: file_hash(output / name) for name in changed} == expected


@pytest.fixture
def preparation_run(tmp_path, monkeypatch):
    """Exercise the controller with a fake container, never a model or replay."""
    source, legacy, output = tmp_path / "source", tmp_path / "legacy", tmp_path / "prepared"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    home = legacy / "jvm/cache/home"
    source_inputs = {
        "build.sbt": b'libraryDependencies += "example" %% "library" % "1.0"\n',
        "project/build.properties": b"sbt.version=1.10.7\n",
        ".sbtopts": b"-batch\n",
        ".jvmopts": b"-Xmx3g\n",
    }
    for name, contents in source_inputs.items():
        put(source, name, contents)
    put(legacy, "jvm/cache/tools/sbt", b"#!/bin/sh\n")
    seed = DOWNLOAD + "seed-1.0.jar"
    put(home, seed)
    put(home, ".sbt/launchers/1.10.7/sbt-launch.jar")
    task = {"id": "classifier-task", "repo": "example/project", "start_sha": "a" * 40}
    oracle = {"command": COMMAND, "environment": {"container": "fixture-image", "build_system": "sbt"}}
    target = output / component(task["id"])
    target.mkdir(parents=True)
    old_jar = DOWNLOAD + "old-1.0.jar"
    put(target / "thin-home", old_jar)
    put(target / "overlay", old_jar)
    install = put(target, "install.log", b"[success] original public compilation\n")
    put(target, "resolve-test-dependencies.log", b"[success] original Test/update\n")
    base = {"path": "/frozen/sbt-base.json", "sha256": "b" * 64}
    previous = {
        **task, "status": "prepared", "exit_status": 0, "seconds": 10,
        "expected_tasks": 402, "model_calls": 0, "scoring_result": False,
        "oracle_fingerprint": canonical_hash(oracle), "base_asset": base,
        "preparation_policy": "sbt-download-delta/test-configurations-v4",
        "command": "original public compilation", "log": str(install), "log_sha256": file_hash(install),
        "input_hashes": {name: file_hash(source / name) for name in source_inputs},
        "overlay_files": {old_jar: file_hash(target / "overlay" / old_jar)}, "removed_files": [],
        "test_dependency_resolution": {"status": "resolved", "arguments": ORIGINAL_ARGUMENTS},
    }
    prepare.atomic_json(target / "preparation.json", previous)
    tasks = {task["id"]: task, **{f"other-{i}": {} for i in range(401)}}
    ids = put(tmp_path, "one-task.ids", (task["id"] + "\n").encode())
    config = {"tasks": "tasks.jsonl", "scope_ids": "full402.ids", "oracles": "oracles.jsonl"}
    state = SimpleNamespace(
        source=source, legacy=legacy, home=home, target=target, output=output, task=task, oracle=oracle,
        previous=previous, base=base, old_jar=old_jar, seed=seed, requests=[], processes=[],
        returncode=0, on_process=lambda workspace: None, marked=0, bundled=[],
    )

    class Gate:
        @contextmanager
        def acquire(self, request, **kwargs):
            state.requests.append((request, kwargs))
            def mark():
                assert kwargs.get("uses_containers") is True
                state.marked += 1
            token = CURRENT_JOB.set({"mark_container_use": mark})
            try:
                yield {"lease": "c" * 32}
            finally:
                CURRENT_JOB.reset(token)

        def status(self):
            return {"fixture_gate": True}

    services = SimpleNamespace(gate=Gate(), scratch=scratch, source_bytes=lambda *args: 1234)
    context = SimpleNamespace(
        repo=lambda task: source, image=lambda image: ("sha256:fixture", {"fixture": True}),
    )

    class Process:
        def __init__(self, argv, stdout, stderr):
            state.processes.append(argv)
            assert argv[0:2] == ["docker", "run"]
            assert stderr == prepare.subprocess.STDOUT
            assert state.marked == len(state.processes)
            stdout.write("[success] classifier resolution\n" if not state.returncode
                         else "[error] missing required javadoc.jar\n")
            for classifier in ("sources", "javadoc"):
                put(target / "thin-home", DOWNLOAD + f"library-1.0-{classifier}.jar",
                    (classifier + " downloaded bytes").encode())
            mount = next(arg for arg in argv if arg.startswith("type=bind,source=")
                         and arg.endswith(",target=/workspace"))
            workspace = Path(mount.split("source=", 1)[1].rsplit(",target=", 1)[0])
            state.on_process(workspace)
            self.returncode = state.returncode

        def poll(self):
            return self.returncode

    monkeypatch.setattr(prepare, "load_config", lambda path: config.copy())
    monkeypatch.setattr(prepare, "selected_tasks", lambda *args: tasks)
    monkeypatch.setattr(prepare, "read_jsonl", lambda *args: {task["id"]: oracle})
    monkeypatch.setattr(prepare, "ExecutionServices", lambda config: services)
    monkeypatch.setattr(prepare, "MigrationContext", lambda *args: context)
    monkeypatch.setattr(prepare, "shared_archive", lambda *args: base.copy())
    monkeypatch.setattr(prepare, "start_archive", lambda *args: nullcontext(None))
    monkeypatch.setattr(prepare, "extract_tar", lambda stream, workspace: shutil.copytree(source, workspace))
    monkeypatch.setattr(prepare, "initialize_git", lambda workspace: None)
    monkeypatch.setattr(prepare.subprocess, "Popen", Process)

    def run(*, resolve_only=True, classifiers=CLASSIFIERS, extra=()):
        argv = [
            "prepare", "--config", str(tmp_path / "config.yaml"), "--legacy-root", str(legacy),
            "--out-dir", str(output), "--task-ids", str(ids), "--toolchains", str(tmp_path / "tools.json"),
            "--workers", "1", "--timeout-seconds", "1200",
        ]
        if resolve_only:
            argv.append("--resolve-only")
        for project in PROJECTS:
            argv.extend(["--dependency-project", project])
        for project in classifiers:
            argv.extend(["--classifier-project", project])
        monkeypatch.setattr(sys, "argv", [*argv, *extra])
        result = prepare.main()
        return result, json.loads((target / "preparation.json").read_text())

    state.run = run
    return state


def test_resolve_only_retains_receipt_inputs_scopes_and_both_classifiers(preparation_run):
    state = preparation_run
    result, record = state.run()
    assert result == 0 and record["status"] == "prepared"
    assert record["expected_tasks"] == 402 and record["model_calls"] == 0 and not record["scoring_result"]
    assert record["preparation_policy"] == "sbt-download-delta/test-classifiers-v5"
    assert record["dependency_request_sha256"] == canonical_hash(record["dependency_request"])
    assert record["test_dependency_resolution"]["arguments"] == CLASSIFIER_ARGUMENTS
    assert record["test_dependency_resolution"]["exit_status"] == 0
    assert record["test_dependency_resolution"]["resolve_only"] is True
    for key in ("command", "exit_status", "seconds", "log_sha256", "input_hashes"):
        assert record[key] == state.previous[key]
        assert record["prior_public_build"][key] == state.previous[key]
    assert record["prior_preparation_sha256"] == canonical_hash(state.previous)
    assert file_hash(state.target / "install.log") == state.previous["log_sha256"]
    assert (state.target / "resolve-test-dependencies.log").read_bytes() == b"[success] original Test/update\n"
    assert record["test_dependency_resolution"]["log"] != str(state.target / "install.log")
    assert state.previous["overlay_files"].items() <= record["overlay_files"].items()
    for classifier in ("sources", "javadoc"):
        name = DOWNLOAD + f"library-1.0-{classifier}.jar"
        assert record["overlay_files"][name] == file_hash(state.target / "overlay" / name)
    attempts = list((state.target / "attempts").glob("*/preparation.json"))
    assert len(attempts) == 1 and json.loads(attempts[0].read_text()) == state.previous
    assert len(state.processes) == 1
    command = shlex.split(state.processes[0][-1])
    assert command[-len(CLASSIFIER_ARGUMENTS):] == CLASSIFIER_ARGUMENTS
    assert not any("Compile/compile" in arg or "testOnly" in arg for arg in command)
    assert {name: file_hash(state.source / name) for name in record["input_hashes"]} == record["input_hashes"]


def test_classifier_preparation_uses_shared_resolve_only_resource_bounds(preparation_run):
    state = preparation_run
    assert state.run()[0] == 0
    request, options = next((request, options) for request, options in state.requests
                            if request.name.startswith("sbt-prepare:"))
    assert (request.cpus, request.memory_bytes, request.scratch_bytes, request.heavy) == (
        2, 4 * GiB, 6 * GiB + 2 * 1234, False,
    )
    assert options == {"uses_containers": True}
    argv = state.processes[0]
    assert argv[argv.index("--cpus") + 1] == "2"
    assert len(argv[argv.index("--cpuset-cpus") + 1].split(",")) <= 2
    assert argv[argv.index("--memory") + 1] == "4096m"
    assert argv[argv.index("--pids-limit") + 1] == "512"
    assert "COURSIER_PARALLEL_DOWNLOAD_COUNT=2" in argv
    assert any(arg.endswith(f"target={prepare.SBT_BASE},readonly") for arg in argv)


def test_public_build_mode_does_not_drop_requested_classifiers(preparation_run):
    state = preparation_run
    result, record = state.run(resolve_only=False)
    assert result == 0 and record["status"] == "prepared"
    command = shlex.split(state.processes[0][-1])
    assert "testSuite2_13/Compile/compile" in command
    assert command[-4:] == CLASSIFIER_ARGUMENTS[-4:]
    assert record["test_dependency_resolution"]["arguments"] == CLASSIFIER_ARGUMENTS


def test_identical_request_reuses_cache_but_changed_requested_arguments_do_not(preparation_run):
    state = preparation_run
    assert state.run()[0] == 0
    assert state.run()[0] == 0
    assert len(state.processes) == 1
    assert state.run(classifiers=CLASSIFIERS + CLASSIFIERS)[0] == 0
    assert len(state.processes) == 2
    assert state.run(classifiers=[*CLASSIFIERS, "scalalibInternal2_13"])[0] == 0
    assert len(state.processes) == 3
    assert state.run(classifiers=[])[0] == 0
    assert len(state.processes) == 4


def test_unchanged_legacy_v4_binary_closure_is_reused_without_rebuilding_or_rewriting(preparation_run):
    state = preparation_run
    log = state.target / "resolve-test-dependencies.log"
    state.previous["test_dependency_resolution"].update(
        exit_status=0, log=str(log), log_sha256=file_hash(log))
    manifest = state.target / "preparation.json"
    prepare.atomic_json(manifest, state.previous)
    before = manifest.read_bytes(), manifest.stat().st_mtime_ns, log.read_bytes(), log.stat().st_mtime_ns
    result, record = state.run(classifiers=[])
    assert result == 0 and record == state.previous and not state.processes
    assert (manifest.read_bytes(), manifest.stat().st_mtime_ns, log.read_bytes(), log.stat().st_mtime_ns) == before


def test_legacy_v4_record_never_claims_an_unprepared_classifier_request():
    old = {"preparation_policy": "sbt-download-delta/test-configurations-v4",
           "test_dependency_resolution": {"arguments": ORIGINAL_ARGUMENTS}}
    assert prepare.cached_request_matches(old, prepare.dependency_request(COMMAND, PROJECTS))
    assert not prepare.cached_request_matches(
        old, prepare.dependency_request(COMMAND, PROJECTS, CLASSIFIERS))


@pytest.mark.parametrize("kind", ["log", "overlay"])
def test_cached_request_checks_bound_resolution_log_and_download_bytes(preparation_run, kind):
    state = preparation_run
    result, record = state.run()
    assert result == 0
    path = (Path(record["test_dependency_resolution"]["log"]) if kind == "log"
            else state.target / "overlay" / (DOWNLOAD + "library-1.0-javadoc.jar"))
    path.write_bytes(b"changed cached evidence")
    result, record = state.run()
    assert result == 2 and record["status"] == "pending"
    assert "cached dependency" in record["error"]
    assert len(state.processes) == 1


@pytest.mark.parametrize("kind", ["public-log", "prior-inputs", "source", "positive-dependency", "seed-deletion"])
def test_resolve_only_rejects_changed_receipt_source_or_original_downloads(preparation_run, kind):
    state = preparation_run
    if kind == "public-log":
        (state.target / "install.log").write_bytes(b"changed original public build")
    elif kind == "prior-inputs":
        state.previous["input_hashes"]["build.sbt"] = "0" * 64
        prepare.atomic_json(state.target / "preparation.json", state.previous)
    elif kind == "source":
        state.on_process = lambda workspace: (workspace / "build.sbt").write_bytes(b"removed classifiers")
    elif kind == "positive-dependency":
        state.on_process = lambda workspace: (state.target / "thin-home" / state.old_jar).write_bytes(b"changed")
    else:
        state.on_process = lambda workspace: (state.target / "thin-home" / state.seed).unlink()
    result, record = state.run()
    assert result == 2 and record["status"] == "pending"
    assert record.get("error")
    if kind in {"public-log", "prior-inputs", "source"}:
        assert "test_dependency_resolution" not in record
    assert len(state.processes) == (0 if kind in {"public-log", "prior-inputs"} else 1)


def test_classifier_failure_is_not_a_prepared_cache_or_a_rerun_public_build(preparation_run):
    state = preparation_run
    state.returncode = 1
    result, record = state.run()
    assert result == 2 and record["status"] == "pending" and record["exit_status"] == 1
    assert "test_dependency_resolution" not in record
    assert file_hash(state.target / "install.log") == state.previous["log_sha256"]
    assert "javadoc.jar" in Path(record["log"]).read_text()
    assert len(state.processes) == 1


@pytest.mark.parametrize("flag", ["--refresh-overlay-only", "--refresh-remote-ivy"])
def test_classifier_operations_are_not_silently_replaced_by_metadata_refresh(preparation_run, flag):
    state = preparation_run
    with pytest.raises(SystemExit) as error:
        state.run(resolve_only=False, extra=[flag])
    assert error.value.code == 2 and not state.processes and not state.requests


@pytest.mark.parametrize("tamper", [False, True])
def test_existing_managed_importer_accepts_classifier_overlay_without_changing_frozen_tests(
    preparation_run, monkeypatch, tamper,
):
    state = preparation_run
    assert state.run()[0] == 0
    if tamper:
        (state.target / "overlay" / (DOWNLOAD + "library-1.0-javadoc.jar")).write_bytes(b"changed")
    monkeypatch.setattr(jvm_managed, "source_files", lambda *args: ["build.sbt", "project/build.properties"])
    monkeypatch.setattr(jvm_managed, "git", lambda *args: b"sbt.version=1.10.7\n")
    monkeypatch.setattr(jvm_managed, "shared_archive", lambda *args: state.base)
    monkeypatch.setattr(jvm_managed, "build_tools", lambda *args, **kwargs: {
        "path": "/frozen/tools.json", "sha256": "d" * 64,
    })
    def bundle(root, names, **kwargs):
        state.bundled.append((root, names))
        return {"path": "/frozen/overlay.json", "sha256": canonical_hash(names)}
    ctx = SimpleNamespace(
        config={"migration": {"sbt_dependency_root": str(state.output)}},
        legacy=state.legacy, repo=lambda task: state.source, bundle=bundle,
    )
    if tamper:
        with pytest.raises(prepare.ContractError, match="overlay bytes changed"):
            jvm_managed.managed_runtime(ctx, state.task, state.oracle, COMMAND)
        assert not state.bundled
    else:
        runtime = jvm_managed.managed_runtime(ctx, state.task, state.oracle, COMMAND)
        assert runtime["command"] == COMMAND.replace("/oracle-tools/sbt", jvm_managed.sbt_prefix())
        assert "testOnly *ExportsTest *StackTraceTest *LinkingInfoTest" in runtime["command"]
        assert len(state.bundled) == 1
        assert state.bundled[0][0] == state.target / "overlay"
        for classifier in ("sources", "javadoc"):
            assert DOWNLOAD + f"library-1.0-{classifier}.jar" in state.bundled[0][1]
