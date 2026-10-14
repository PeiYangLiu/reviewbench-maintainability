"""Standalone Ruby CLI/lane integration with the mandatory frozen engine."""
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import prepare_agentic_ruby_candidates as prepare
from agentic_runtime_migration import ruby_candidate as ruby, ruby_candidate_lane as lane
from reviewbench_maintainability.agentic import contracts
from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash, component
from reviewbench_maintainability.agentic.snapshots import git
from test_agentic_ruby_candidate import gem_repository, local_ruby, source_fixture, shell, IMAGE


def engine_src():
    return Path(contracts.__file__).resolve().parents[2]


def case(task_id):
    index = 10 if task_id == ruby.KNAPSACK else 37
    target = "lib/knapsack_pro/runners/queue/rspec_runner.rb" if index == 10 else "lib/prometheus_exporter/middleware.rb"
    command = ("bundle exec rspec spec/integration/runners/queue/rspec_runner_spec.rb:159 --format progress"
               if index == 10 else "bundle exec ruby -Itest test/middleware_test.rb --name test_redis_5_patching")
    selector = ("spec/integration/runners/queue/rspec_runner_spec.rb:159"
                if index == 10 else "test/middleware_test.rb::test_redis_5_patching")
    image = lane.IMAGE_ALIAS
    setup = ("mkdir -p {task_dir}/artifacts {root}/cache/bundle && docker run --rm "
             '-u "$(id -u):$(id -g)" -e HOME=/work -e BUNDLE_USER_CACHE=/bundle-cache '
             "-v {work}:/work -v {root}/cache/bundle:/bundle-cache -w /work " + image +
             " bash -lc " + shlex.quote(ruby.TARGETS[task_id][2]))
    test = ("rm -f {task_dir}/artifacts/coverage-{mode}-{run}.json; docker run --rm "
            '-u "$(id -u):$(id -g)" -e HOME=/work -e BUNDLE_PATH=/work/.bundle '
            "-e RUBYOPT=-r/oracle-tools/ruby_coverage.rb -e ORACLE_TARGET=/work/" + target +
            " -e ORACLE_COVERAGE_JSON=/oracle-task/artifacts/coverage-{mode}-{run}.json "
            "-v {work}:/work -v {root}/tools:/oracle-tools:ro -v {task_dir}:/oracle-task "
            "-w /work " + image + " bash -lc " + shlex.quote(command))
    return index, {
        "environment": {"docker": image, "coverage": "Ruby Coverage"}, "setup_host_command": setup,
        "test_host_command": test, "test_selectors": [selector], "timeout": 1200 if index == 10 else 900,
        "outcome_regex": r"(\d+) examples?, (\d+) failures?" if index == 10 else r"(\d+) runs, (\d+) assertions, 0 failures, 0 errors",
    }


def cli_fixture(tmp_path, monkeypatch, gem_repository=None):
    fixtures = {}
    for task_id in ruby.TARGETS:
        parent = tmp_path / component(task_id)
        parent.mkdir()
        fixtures[task_id] = source_fixture(parent, task_id=task_id, gem_repository=gem_repository)
    journal = tmp_path / "J"
    journal.mkdir()
    engine_file = journal / "engine-src.txt"
    engine_file.write_text(str(engine_src()) + "\n")
    config_file = journal / "experiment.yaml"
    config_file.write_text("fixture\n")
    toolchains = journal / "scripted-toolchains.json"
    toolchains.write_text(json.dumps({lane.IMAGE_ALIAS: {"status": "ready", "image": IMAGE}}))
    ids = journal / "ruby.ids"
    ids.write_text("\n".join(ruby.TARGETS) + "\n")
    legacy = tmp_path / "legacy"
    (legacy / "scripted").mkdir(parents=True)
    cases = {str(case(task_id)[0]): case(task_id)[1] for task_id in ruby.TARGETS}
    (legacy / "scripted/oracle_config.json").write_text(json.dumps({"tasks": cases}))
    output = journal / "prepared-ruby-candidates-v1"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    config = {"root": str(tmp_path), "tasks": "fixture", "oracles": "fixture"}
    tasks = {task_id: fixture.task for task_id, fixture in fixtures.items()}
    tasks.update({f"unselected-{i}": {"id": f"unselected-{i}"} for i in range(400)})
    oracles = {task_id: {"index": case(task_id)[0]} for task_id in fixtures}
    events, requests, invocations = [], [], []

    @contextmanager
    def acquire(request, **options):
        requests.append((request, options))
        events.append("acquire")
        try:
            yield {"lease": "ruby-fixture-lease"}
        finally:
            events.append("release")

    services = SimpleNamespace(scratch=scratch, source_bytes=lambda *args: 1024,
                               gate=SimpleNamespace(acquire=acquire, status=lambda: {"fixture": True}))
    ctx = SimpleNamespace(repo=lambda task: fixtures[task["id"]].source, output=output / "toolchains")
    api = prepare._api()
    api.load_config = lambda path: copy.deepcopy(config)
    api.selected_tasks = lambda *args: tasks
    api.read_jsonl = lambda path: oracles
    api.ExecutionServices = lambda config: services
    api.MigrationContext = lambda *args: ctx
    api.completed_jobs = lambda fn, batch, workers: ((ident, fn(ident)) for ident in batch)
    api.bounded_cpu_set = lambda *args: "0,1"
    monkeypatch.setattr(prepare, "_api", lambda: api)
    monkeypatch.setattr(prepare, "audit_image", lambda *args: {"path": "/fixture/audit", "sha256": "b" * 64})
    monkeypatch.setattr(prepare.shutil, "disk_usage", lambda path: SimpleNamespace(free=1000 * 1024**3))

    def install(argv, container, log, *, scratch, timeout):
        events.append("install")
        invocations.append(argv)
        assert events.count("acquire") > events.count("release")
        assert list((output / "code-snapshots").glob("*/code.tar")), "snapshot must precede resolver launch"
        mounts = [dict(part.split("=", 1) for part in argv[i + 1].split(",") if "=" in part)
                  for i, value in enumerate(argv) if value == "--mount"]
        source = Path(next(item["source"] for item in mounts if item["target"] == "/workspace"))
        payload = Path(next(item["source"] for item in mounts if item["target"] == lane.CACHE))
        spec = json.loads(shlex.split(argv[-1])[-1])
        request = spec["request"]
        assert not (source / "Gemfile.lock").exists()
        native = ruby.resolution_command(
            request, source=source, state=source / lane.STATE.removeprefix("/workspace/"),
            bundle_path=payload / "gems", bootstrap_path=payload / "bootstrap")
        result = shell(native)
        Path(log).write_text(result.stdout)
        return result.returncode, 0.25

    monkeypatch.setattr(prepare, "run_install", install)
    args = ["--config", str(config_file), "--legacy-root", str(legacy), "--out-dir", str(output),
            "--task-ids", str(ids), "--toolchains", str(toolchains), "--workers", "1", "--timeout-seconds", "30"]
    return SimpleNamespace(**locals())


def test_cli_requires_opt_in_before_gate_and_uses_current_engine(tmp_path, monkeypatch):
    fixture = cli_fixture(tmp_path, monkeypatch)
    assert prepare.main(fixture.args) == 2
    assert fixture.invocations == fixture.requests == []
    summary = json.loads((fixture.output / "preparation-summary.json").read_text())
    assert summary["expected_tasks"] == 402 and summary["batch_tasks"] == 2 and summary["model_calls"] == 0
    assert summary["engine"]["src"] == str(engine_src())
    assert all("explicit --resolve-unlocked" in item["error"] for item in summary["records"].values())


def test_cli_prepares_both_once_with_bootstrap_and_never_resolves_published_again(tmp_path, monkeypatch, gem_repository):
    fixture = cli_fixture(tmp_path, monkeypatch, gem_repository)
    assert prepare.main([*fixture.args, "--resolve-unlocked"]) == 0
    assert len(fixture.invocations) == len(fixture.requests) == 2
    originals = {}
    for ident, source_fixture in fixture.fixtures.items():
        target = fixture.output / component(ident)
        originals[ident] = (target / "preparation.json").read_bytes()
        record = json.loads(originals[ident])
        assert record["status"] == "prepared" and record["lock_origin"] == ruby.ORIGIN
        assert record["expected_tasks"] == 402 and record["exit_status"] == 0
        assert record["ruby_candidate"]["bootstrap_sha256"] == ruby.tree_fingerprint(target / "payload/bootstrap")
        assert Path(record["code_snapshot"]["archive"]).is_file()
        assert not (source_fixture.source / "Gemfile.lock").exists()
        assert ruby.tree_fingerprint(source_fixture.source, source=True) == source_fixture.start["source_sha256"]
    for request, options in fixture.requests:
        assert request.cpus == 2 and request.memory_bytes == 4 * 1024**3
        assert options == {"uses_containers": True, "timeout": 30}
    for argv in fixture.invocations:
        assert argv[argv.index("--cpus") + 1] == "2"
        assert argv[argv.index("--memory") + 1] == "4096m"
        assert argv[argv.index("--pids-limit") + 1] == "256"
        assert argv[argv.index("--cpuset-cpus") + 1] == "0,1"
        assert "reviewbench.resource-lease=ruby-fixture-lease" in argv
        assert "GEM_HOME=" + lane.CACHE + "/bootstrap" in argv
        assert "BUNDLE_FROZEN=false" in argv
        assert "gem install bundler --version 2.4.19 --no-document --install-dir" in argv[-1]
    assert prepare.main(fixture.args) == 0
    assert len(fixture.invocations) == len(fixture.requests) == 2
    # A broken literal setup must not overwrite an already-prepared manifest.
    config_path = fixture.legacy / "scripted/oracle_config.json"
    changed = json.loads(config_path.read_text())
    changed["tasks"]["10"]["setup_host_command"] += " && true"
    config_path.write_text(json.dumps(changed))
    assert prepare.main([*fixture.args, "--resolve-unlocked"]) == 2
    assert len(fixture.invocations) == len(fixture.requests) == 2
    for ident, original in originals.items():
        assert (fixture.output / component(ident) / "preparation.json").read_bytes() == original


@pytest.mark.parametrize("flag,value", [("--workers", "3"), ("--timeout-seconds", "0")])
def test_cli_rejects_unbounded_execution_arguments(tmp_path, monkeypatch, flag, value):
    fixture = cli_fixture(tmp_path, monkeypatch)
    with pytest.raises(ContractError):
        prepare.main([*fixture.args, flag, value, "--resolve-unlocked"])
    assert fixture.invocations == []


def test_cli_rejects_engine_drift_and_non_target_ids(tmp_path, monkeypatch):
    fixture = cli_fixture(tmp_path, monkeypatch)
    monkeypatch.setenv("REVIEWBENCH_ENGINE_SRC", str(tmp_path / "wrong-engine"))
    with pytest.raises(RuntimeError, match="mandatory engine-src"):
        prepare.main(fixture.args)
    monkeypatch.setenv("REVIEWBENCH_ENGINE_SRC", str(engine_src()))
    fixture.ids.write_text("another-task\n")
    with pytest.raises(ContractError, match="two explicit task IDs"):
        prepare.main([*fixture.args, "--resolve-unlocked"])
    assert fixture.requests == []


@pytest.mark.parametrize("reason", ["timeout", "disk"])
def test_monitor_stops_container_on_timeout_or_disk_floor(tmp_path, monkeypatch, reason):
    class Process:
        returncode = None
        def poll(self):
            return self.returncode
        def wait(self, timeout):
            self.returncode = 143
        def kill(self):
            self.returncode = -9
    process, stopped = Process(), []
    monkeypatch.setattr(prepare.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(prepare.subprocess, "run", lambda argv, **kwargs: stopped.append(argv))
    ticks = iter([0, 2, 3, 4])
    monkeypatch.setattr(prepare.time, "monotonic", lambda: next(ticks))
    free = iter([1000 * 1024**3, 1] if reason == "disk" else [1000 * 1024**3] * 3)
    monkeypatch.setattr(prepare.shutil, "disk_usage", lambda path: SimpleNamespace(free=next(free)))
    with pytest.raises((TimeoutError, RuntimeError)) as caught:
        prepare.run_install(["docker", "run"], "fixture-container", tmp_path / "log",
                            scratch=tmp_path, timeout=1 if reason == "timeout" else 10)
    assert stopped == [["docker", "stop", "-t", "5", "fixture-container"]]
    assert caught.value.exit_status == 143


def test_disk_floor_prevents_launch(tmp_path, monkeypatch):
    monkeypatch.setattr(prepare.shutil, "disk_usage", lambda path: SimpleNamespace(free=1))
    monkeypatch.setattr(prepare.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("launched below disk floor"))
    with pytest.raises(RuntimeError, match="before launch"):
        prepare.run_install(["docker", "run"], "fixture-container", tmp_path / "log", scratch=tmp_path, timeout=1)


def test_image_audit_uses_the_admitted_limits_and_rejects_corrupt_cached_audit(tmp_path, monkeypatch):
    from reviewbench_maintainability.agentic import environment
    calls = []
    def docker(*args, **kwargs):
        calls.append(args)
        if args[0] == "image":
            return json.dumps([{"Id": IMAGE}])
        return ""
    monkeypatch.setattr(environment, "docker", docker)
    api = prepare._api()
    api.bounded_cpu_set = lambda *args: "0,1"
    ctx = SimpleNamespace(output=tmp_path)
    audit = prepare.audit_image(api, ctx, IMAGE, {"lease": "audit-lease"})
    scan = next(args for args in calls if args[0] == "run")
    assert scan[scan.index("--cpus") + 1] == "2"
    assert scan[scan.index("--memory") + 1] == "4096m"
    assert scan[scan.index("--network") + 1] == "none"
    assert "reviewbench.resource-lease=audit-lease" in scan
    path = Path(audit["path"])
    assert json.loads(path.read_text())["no_reference_assets"] is True
    path.write_text("{}")
    with pytest.raises(ContractError, match="retained Ruby image audit"):
        prepare.audit_image(api, ctx, IMAGE, {"lease": "audit-lease"})


def test_missing_bundler_bootstrap_is_explicit_private_and_exact(tmp_path, monkeypatch):
    api = prepare._api()
    api = SimpleNamespace(**vars(api))
    api.lane = SimpleNamespace(CACHE=str(tmp_path / "payload"), STATE="/workspace/.reviewbench/ruby/build")
    source = source_fixture(tmp_path)
    request = ruby.prepare_or_reuse(source.plan, source.start, IMAGE, resolve_unlocked=True)
    tools = tmp_path / "tools"
    tools.mkdir()
    calls = tmp_path / "calls.jsonl"
    for name in ("ruby", "gem"):
        file = tools / name
        file.write_text(f"#!{sys.executable}\n" + r'''
import json, os, pathlib, sys
tool = pathlib.Path(sys.argv[0]).name
path = pathlib.Path(os.environ["BOOTSTRAP_CALLS"])
with path.open("a") as stream: stream.write(json.dumps([tool, *sys.argv[1:]]) + "\n")
if tool == "gem":
    target = pathlib.Path(sys.argv[sys.argv.index("--install-dir") + 1])
    target.mkdir(parents=True, exist_ok=True)
    (target / "installed-2.4.19").write_text("pinned bootstrap fixture")
    raise SystemExit(0)
if sys.argv[2].startswith('gem "bundler"'):
    raise SystemExit(1)
print("bounded Ruby entry reached")
''')
        file.chmod(0o755)
    result = shell(prepare.bootstrap_command(api, request),
                   env={**os.environ, "PATH": str(tools) + ":" + os.environ["PATH"], "BOOTSTRAP_CALLS": str(calls)})
    assert result.returncode == 0, result.stdout
    observed = [json.loads(line) for line in calls.read_text().splitlines()]
    assert observed[1] == ["gem", "install", "bundler", "--version", "2.4.19", "--no-document",
                           "--install-dir", str(tmp_path / "payload/bootstrap")]
    assert "bounded Ruby entry reached" in result.stdout
    assert (tmp_path / "payload/bootstrap/installed-2.4.19").is_file()
    assert not (source.source / "Gemfile.lock").exists()


def test_lane_import_does_not_import_scripted_lane_at_module_load():
    script = ("import sys; import agentic_runtime_migration.ruby_candidate_lane; "
              "assert 'agentic_runtime_migration.scripted_lane' not in sys.modules")
    env = {**os.environ, "PYTHONPATH": str(engine_src()) + os.pathsep + str(Path(__file__).resolve().parents[1] / "scripts")}
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("task_id", [ruby.KNAPSACK, ruby.PROMETHEUS])
def test_standalone_lane_binds_original_wrappers_fixed_tests_and_current_candidate(tmp_path, monkeypatch, gem_repository, task_id):
    fixture = cli_fixture(tmp_path, monkeypatch, gem_repository)
    fixture.ids.write_text(task_id + "\n")
    assert prepare.main([*fixture.args, "--resolve-unlocked"]) == 0
    selected = fixture.fixtures[task_id]
    index, original = case(task_id)
    task = {**selected.task, "locality": {"path": "lib/" + selected.gem_name + ".rb"}}
    task_dir = fixture.legacy / "scripted/task"
    task_dir.mkdir()
    summary = "1 example, 0 failures" if task_id == ruby.KNAPSACK else "1 runs, 2 assertions, 0 failures, 0 errors"
    (fixture.legacy / "scripted/complete.log").write_text(summary + "\n")
    oracle = {"index": index, "runs": {variant: [
        {"log": "complete.log", "exit_code": 0} for _ in range(3)] for variant in ("start", "golden")}}
    bundles = []
    def bundle(root, names=None, *, role="dependency"):
        bundles.append((root, names, role))
        return {"path": "/fixture/tree.json", "sha256": "b" * 64}
    ctx = SimpleNamespace(
        config={"root": str(tmp_path), "migration": {"ruby_candidate_dependency_root": str(fixture.output)}},
        legacy=fixture.legacy, scratch=fixture.scratch, repo=lambda task: selected.source,
        bundle=bundle, image=lambda image: (IMAGE, {"path": "/fixture/audit.json", "sha256": "c" * 64}),
        task_directory=lambda *args: task_dir, immutable_tests=lambda task: ["test/fixed.rb"],
        golden=lambda *args: {"path": "/fixture/golden.patch", "sha256": "d" * 64}, modules=lambda task: [])
    before = ruby.tree_fingerprint(selected.source, source=True)
    recipe = lane.migrate(ctx, task, oracle)
    recipe.validate()
    assert recipe.cpus == 2 and recipe.memory_mb == 4096 and recipe.pids_limit == 256
    assert recipe.scratch_paths == [] and recipe.workdir == "/workspace"
    assert recipe.cache_mounts == {"ruby-candidates": lane.CACHE}
    assert recipe.env["BUNDLE_FROZEN"] == "true"
    assert recipe.env["BUNDLE_GEMFILE"] == lane.STATE + "/source/Gemfile"
    assert recipe.env["GEM_HOME"] == lane.CACHE + "/bootstrap"
    assert recipe.bootstrap[-1] == recipe.dev_checks[-1] == recipe.grading["bootstrap"][-1]
    assert set(recipe.grading["immutable_paths"]) == {"test/fixed.rb", "spec/fixed_spec.rb"}
    command = shlex.split(recipe.grading["commands"][0]["command"])
    assert command[-3:-1] == ["bash", "-c"]
    assert command[-1] == ("bundle exec rspec spec/integration/runners/queue/rspec_runner_spec.rb:159 --format progress"
                           if task_id == ruby.KNAPSACK else
                           "bundle exec ruby -Itest test/middleware_test.rb --name test_redis_5_patching")
    assert "RUBYOPT=-r" + lane.TOOLS + "/ruby_coverage.rb" in command
    assert "BUNDLE_PATH=" + lane.CACHE + "/gems" in command
    assert "ORACLE_COVERAGE_JSON=" + lane.RUNTIME + "/artifacts/coverage-candidate-1.json" in command
    if task_id == ruby.PROMETHEUS:
        assert recipe.env["MT_CPU"] == "2" and "MT_CPU=2" in command
    else:
        assert "MT_CPU" not in recipe.env and not any(word.startswith("MT_CPU=") for word in command)
    assert recipe.grading["commands"][0]["expected_tests"] == [task_id + "::" + original["test_selectors"][0]]
    assert ruby.tree_fingerprint(selected.source, source=True) == before
    assert not (selected.source / "Gemfile.lock").exists()
    assert (fixture.output / component(task_id) / "payload", None, "dependency") in bundles
    record = fixture.output / component(task_id) / "preparation.json"
    changed = json.loads(record.read_text())
    changed["image"] = "sha256:" + "e" * 64
    record.write_text(json.dumps(changed))
    with pytest.raises(ContractError, match="never re-resolve"):
        lane.migrate(ctx, task, oracle)
