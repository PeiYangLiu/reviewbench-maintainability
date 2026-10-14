"""Focused standalone Kirby controller tests; optional PHP fixtures use the shared gate.

Set KIRBY_PREP_CONFIG/KIRBY_TOOLCHAINS/KIRBY_LEGACY_ROOT to enable tiny,
network-disabled PHP runtime fixtures. The upstream PHPUnit resolution happens
only in the later explicitly frozen real preparation, never in these fixtures.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tarfile
from types import SimpleNamespace

import pytest

WORK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORK / "scripts"))
if os.environ.get("REVIEWBENCH_ENGINE_SRC"):
    sys.path.insert(0, os.environ["REVIEWBENCH_ENGINE_SRC"])

import test_agentic_composer_test_tools as fixture
from agentic_runtime_migration import composer_candidate as cc
from agentic_runtime_migration import composer_test_tools as tools
from agentic_runtime_migration import composer_test_tools_runtime as runtime
from agentic_runtime_migration import composer_test_tools_lane as lane
import prepare_agentic_composer_test_tools as prep
from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash, component


def _bundle(root, resolver=None):
    """Synthetic package metadata and a clearly labelled fixture version program."""
    resolver = copy.deepcopy(resolver or fixture._resolver())
    original_manifest, original_lock = map(json.loads, fixture._inputs())
    original_manifest["require"]["composer/semver"] = "3.4.4"
    original_lock["packages"].append(fixture._package("composer/semver", "3.4.4"))
    original_lock["content-hash"] = tools.composer_content_hash(original_manifest)
    inputs = fixture._bytes(original_manifest), fixture._bytes(original_lock)
    plan = tools.plan_test_tools(fixture._task(), *inputs)
    stage = tools.stage_test_tools(root / "install", plan, *inputs)
    _inventory_with_composer_namespace(stage, inputs[1])
    fixture._put(stage, "vendor/fixture/prod/src/Removed.php", "<?php class RemovedUpstream {}\n")
    original = tools.record_original_install(plan, stage, resolver=resolver,
                                             execution=fixture._execution("original_install"))
    derived = fixture._derived(inputs)
    (stage / "composer.json").write_bytes(derived[0])
    (stage / "composer.lock").write_bytes(derived[1])
    resolved = tools.record_resolution(plan, stage, original_install=original, resolver=resolver,
                                       execution=fixture._execution("resolve_tools"))
    _inventory_with_composer_namespace(stage, derived[1])
    fixture._put(stage, "vendor/phpunit/phpunit/phpunit", '<?php echo "PHPUnit 11.5.8 UNIT-FIXTURE\\n";\n')
    fixture._put(stage, "vendor/bin/phpunit", "<?php require __DIR__.'/../phpunit/phpunit/phpunit';\n")
    sealed = root / "sealed"
    reference = tools.seal_prepared_tools(
        sealed, plan, stage, original_manifest=inputs[0], original_lock=inputs[1],
        original_install=original, resolution=resolved, resolver=resolver,
        execution=fixture._execution("derived_install"),
    )
    protected = ["vendor/fixture/prod/src/Fixture.php", "vendor/fixture/prod/src/Removed.php",
                 "vendor/composer/semver/src/Current.php"]
    payload = root / "payload"
    exported = runtime.export_payload(stage, payload, [*protected, *cc.METADATA], sealed=sealed,
                                      expected_sha256=reference["record_sha256"], image=resolver["image"])
    return SimpleNamespace(inputs=inputs, stage=stage, sealed=sealed, payload=payload, reference=reference,
                           resolver=resolver, protected=protected, exported=exported)


def _inventory_with_composer_namespace(stage, lock):
    fixture._inventory(stage, lock)
    path = stage / "vendor/composer/installed.json"
    inventory = json.loads(path.read_bytes())
    for package in inventory["packages"]:
        if package["name"] == "composer/semver":
            package["install-path"] = "semver"
    path.write_bytes(fixture._bytes(inventory))


@pytest.mark.parametrize("relative,accepted", [
    ("semver", True), ("./semver", True), ("../composer/semver", True),
    ("../fixture/prod", False), ("/outside", False), ("../../../outside", False),
])
def test_composer_namespace_install_paths_resolve_only_to_the_exact_package(tmp_path, relative, accepted):
    data = _bundle(tmp_path)
    path = data.stage / "vendor/composer/installed.json"
    inventory = json.loads(path.read_bytes())
    next(p for p in inventory["packages"] if p["name"] == "composer/semver")["install-path"] = relative
    path.write_bytes(fixture._bytes(inventory))
    lock = json.loads((data.stage / "composer.lock").read_bytes())
    if accepted:
        assert tools._inventory(data.stage, lock)
    else:
        with pytest.raises(tools.TestToolsError, match="unexpected installed package path"):
            tools._inventory(data.stage, lock)


def _candidate(root, inputs):
    manifest = json.loads(inputs[0])
    manifest["autoload"]["classmap"].append("new-helpers/")
    fixture._put(root, "composer.json", fixture._bytes(manifest))
    fixture._put(root, "composer.lock", inputs[1] + b"\n").chmod(0o444)
    fixture._put(root, "config/setup.php", "<?php // current setup\n")
    fixture._put(root, "dependencies/Empty.php", "<?php // current root classmap\n")
    fixture._put(root, "src/Cms/Responder.php",
                 "<?php\nnamespace Kirby\\Cms;\nclass Responder {\n public static function label() {\n"
                 "  return 'current-source';\n }\n}\n")
    fixture._put(root, "new-helpers/NewHelper.php", "<?php class NewHelper { static function value() { return 'new-helper'; } }\n")
    fixture._put(root, "vendor/fixture/prod/src/Fixture.php",
                 "<?php class VendorCurrent { static function value() { return 'current-vendor'; } }\n")
    fixture._put(root, "vendor/composer/semver/src/Current.php", "<?php // not Composer metadata\n")
    fixture._put(root, "vendor/autoload.php", "<?php // original tracked generated file\n")
    fixture._put(root, "vendor/composer/ClassLoader.php", "<?php // original tool source\n")
    fixture._put(root, ".git/config", "history is excluded from mirrors\n")
    (root / "empty-dir").mkdir()
    fixture._put(root, "check.php", r'''<?php
require __DIR__.'/vendor/autoload.php';
echo json_encode([
    'source'=>Kirby\Cms\Responder::label(),
    'vendor'=>VendorCurrent::value(),
    'helper'=>NewHelper::value(),
    'removed'=>class_exists('RemovedUpstream'),
    'cwd'=>getcwd(), 'home'=>getenv('HOME')
], JSON_UNESCAPED_SLASHES), "\n";
''')
    return root


def _source_snapshot(root):
    return {name: value for name, value in fixture._snapshot(root).items()
            if not name.startswith((".reviewbench/", ".git/"))}


def _case():
    env = "-e HOME=/work -e XDEBUG_MODE=coverage -e ORACLE_TARGET=/work/src/Cms/Responder.php "
    env += "-e ORACLE_COVERAGE_JSON=/oracle-task/artifacts/coverage-{mode}-{run}.json "
    return {
        "setup_host_command": "mkdir -p {task_dir}/artifacts && docker run --rm -w /work "
                              + lane.IMAGE_ALIAS + " bash -lc " + shlex.quote(tools.ORIGINAL_SETUP),
        "test_host_command": "rm -f {task_dir}/artifacts/coverage-{mode}-{run}.json; docker run --rm "
                             + env + "-w /work " + lane.IMAGE_ALIAS + " bash -lc " + shlex.quote(lane.TEST_COMMAND),
        "environment": {"docker": lane.IMAGE_ALIAS, "coverage": "Xdebug"},
        "test_selectors": ["tests/Cms/ResponderTest.php::testFromArray"],
        "outcome_regex": r"OK \((\d+) tests?, (\d+) assertions?\)", "timeout": 900,
    }


def test_exporter_preserves_original_plugin_type_and_never_ships_original_candidate_vendor(tmp_path):
    data = _bundle(tmp_path)
    assert not set(data.protected) & data.exported["files"].keys()
    assert not (data.payload / "vendor/autoload.php").exists()
    assert (data.payload / "vendor/phpunit/phpunit/phpunit").is_file()
    assert (data.payload / "vendor/bin/phpunit").is_file()
    assert all((data.payload / name).is_file() for name in cc.SEEDS)
    for package in data.exported["locked"]["packages"]:
        if package["name"] == tools.INERT_PLUGIN:
            assert package["type"] == "composer-plugin" and package["version"] == "1.2.1"
    assert runtime.validate_payload(data.payload, expected_sha256=data.reference["record_sha256"],
                                    image=data.resolver["image"]) == data.exported
    with pytest.raises(cc.ComposerCandidateError, match="installer/path"):
        cc._packages(data.exported["locked"])


@pytest.mark.parametrize("mutation", ["file", "mode", "sealed", "graph", "image"])
def test_exported_payload_integrity_and_graph_are_bound(tmp_path, mutation):
    data = _bundle(tmp_path)
    image = data.resolver["image"]
    if mutation == "file":
        (data.payload / "vendor/phpunit/phpunit/phpunit").write_text("changed")
    elif mutation == "mode":
        (data.payload / "vendor/phpunit/phpunit/phpunit").chmod(0o755)
    elif mutation == "sealed":
        path = data.payload / runtime.BUILD_INPUTS / tools.RECORD
        path.chmod(0o644); path.write_bytes(path.read_bytes() + b"\n")
    elif mutation == "graph":
        path = data.payload / runtime.RECORD
        meta = json.loads(path.read_text())
        meta["dependency_manifest"]["fields"]["require-dev"][tools.PHPUNIT] = "^12"
        path.write_bytes(tools._encoded(meta))
    else:
        image = "sha256:" + "9" * 64
    with pytest.raises(tools.TestToolsError):
        runtime.validate_payload(data.payload, expected_sha256=data.reference["record_sha256"], image=image)


def test_runtime_is_independent_and_privatebin_emission_is_byte_identical(tmp_path):
    before = cc.php_runtime_source()
    assert hashlib.sha256(Path(cc.__file__).read_bytes()).hexdigest() == "1645d9d01071f0de2c7a166f7f52460d2ae7eee7d368c53f6d66b088d3a19069"
    entry = runtime.write_php_runtime(tmp_path / "kirby.php")
    assert entry.read_text() == runtime.php_runtime_source()
    assert "function kt_build" in entry.read_text()
    assert "function cc_build" not in entry.read_text()
    assert cc.php_runtime_source() == before
    command = shlex.split(runtime.runtime_command("/entry", "/deps", "/mirror", "sha256:" + "1" * 64,
                                                   "2" * 64, lane.TEST_COMMAND,
                                                   env={"ORACLE_TARGET": "/workspace/src/Cms/Responder.php"}))
    assert command[1] == "ORACLE_TARGET=/workspace/src/Cms/Responder.php"
    assert command[-3:] == ["bash", "-c", lane.TEST_COMMAND]


def test_controller_manifest_rejects_mutable_source_failed_tests_and_wrong_engine(tmp_path):
    root = tmp_path / "frozen"
    sources = {}
    for name in prep.REQUIRED_FILES:
        path = fixture._put(root, name, "# fixture controller\n")
        sources[name] = prep.sha(path)
    log = fixture._put(root, "focused-tests.log", "passed fixture\n")
    engine = tmp_path / "engine"; engine.mkdir()
    pinned = {"status": "ready", "image": "sha256:" + "1" * 64}
    manifest = {
        "schema": prep.CONTROLLER_SCHEMA, "files": sources, "engine_src": str(engine.resolve()),
        "toolchain_entry_sha256": tools._fingerprint(pinned),
        "tests": {"exit_status": 0, "command": "pytest focused", "log": log.name, "log_sha256": prep.sha(log)},
    }
    path = fixture._put(root, "controller.json", fixture._bytes(manifest))
    verified = prep.verify_controller(path, engine, pinned, running_script=root / prep.REQUIRED_FILES[0])
    assert verified["sha256"] == prep.sha(path)
    with pytest.raises(tools.TestToolsError, match="mutable workspace"):
        prep.verify_controller(path, engine, pinned, running_script=tmp_path / "mutable.py")
    fixture._put(root, prep.REQUIRED_FILES[1], "# changed\n")
    with pytest.raises(tools.TestToolsError, match="source changed"):
        prep.verify_controller(path, engine, pinned, running_script=root / prep.REQUIRED_FILES[0])


def test_runner_caps_marks_and_rejects_ungated_container_start(tmp_path):
    services = SimpleNamespace(scratch=tmp_path, gate=SimpleNamespace(limits=SimpleNamespace(min_free_disk_bytes=80 * prep.GIB)))
    runner = prep.ContainerRunner(services=services, lease={"lease": "fixture-lease"}, image="sha256:" + "1" * 64,
                                  stage=tmp_path, evidence=tmp_path / "commands", timeout_seconds=10)
    argv = runner.argv("fixture", ["composer", "--version"], runner.environment())
    assert argv[argv.index("--cpus") + 1] == "2"
    assert argv[argv.index("--memory") + 1] == "4096m"
    assert argv[argv.index("--pids-limit") + 1] == "256"
    assert "--read-only" in argv and "reviewbench.resource-lease=fixture-lease" in argv
    audit_argv = runner.argv("audit", ["find", "/"], {}, rootfs_audit=True)
    assert "--mount" not in audit_argv and "--tmpfs" not in audit_argv
    assert "--cap-add=DAC_OVERRIDE" in audit_argv and audit_argv[audit_argv.index("-w") + 1] == "/"
    assert audit_argv[audit_argv.index("--memory") + 1] == "4096m"
    assert runner.floor >= 80 * prep.GIB
    with pytest.raises(tools.TestToolsError, match="shared resource lease|disk floor"):
        runner.run("ungated", ["php", "--version"])
    record = json.loads((tmp_path / "commands/ungated/execution.json").read_text())
    assert record["exit_status"] is None and record["error"]


def test_prepared_reuse_never_reenters_resolution_or_overwrites_published_payload(tmp_path, monkeypatch):
    record = {"status": "prepared", "runtime_php_sha256": hashlib.sha256(runtime.php_runtime_source().encode()).hexdigest()}
    fixture._put(tmp_path, "preparation.json", fixture._bytes(record))
    called = []
    monkeypatch.setattr(lane, "validate_prepared", lambda *args, **kwargs: called.append(args))
    assert prep.reuse_prepared(tmp_path, tmp_path / "repo", fixture._task(), _case(), "image") == record
    assert len(called) == 1
    monkeypatch.setattr(lane, "validate_prepared", lambda *args, **kwargs: (_ for _ in ()).throw(ContractError("tampered")))
    with pytest.raises(ContractError, match="tampered"):
        prep.reuse_prepared(tmp_path, tmp_path / "repo", fixture._task(), _case(), "image")
    assert json.loads((tmp_path / "preparation.json").read_text()) == record
    fixture._put(tmp_path, "preparation.json", '{"status":"pending"}')
    (tmp_path / "payload").mkdir()
    with pytest.raises(tools.TestToolsError, match="another resolution"):
        prep.reuse_prepared(tmp_path, tmp_path / "repo", fixture._task(), _case(), "image")


def test_lane_emits_original_fixed_command_full_summary_and_derived_mirror_only(tmp_path, monkeypatch):
    data = _bundle(tmp_path / "fixture")
    task = {**fixture._task(), "repo": "getkirby/kirby", "locality": {"path": "src/Cms/Responder.php"}}
    case = _case()
    legacy = tmp_path / "legacy"
    fixture._put(legacy, "scripted/oracle_config.json", fixture._bytes({"tasks": {"42": case}}))
    fixture._put(legacy, "scripted/tools/php_coverage_prepend.php", "<?php // fixed\n")
    oracle = {"index": 42, "artifacts": "task_artifacts/42", "runs": {"start": [], "golden": []}}
    for variant in oracle["runs"]:
        for index in range(3):
            name = f"task_artifacts/42/{variant}-{index}.log"
            fixture._put(legacy, "scripted/" + name, "OK (1 test, 8 assertions)\n")
            oracle["runs"][variant].append({"exit_code": 0, "log": name})
    root = tmp_path / "prepared"
    target = root / component(task["id"])
    target.mkdir(parents=True)
    shutil.copytree(data.payload, target / "payload")
    audit = fixture._put(target, "image-audit.json", fixture._bytes({
        "image": data.resolver["image"], "no_repository_history": True, "no_reference_assets": True,
    }))
    prepared = tools.load_prepared_tools(data.sealed, expected_sha256=data.reference["record_sha256"], image=data.resolver["image"])
    record = {
        "id": task["id"], "repo": task["repo"], "start_sha": task["start_sha"], "status": "prepared",
        "image": data.resolver["image"], "config_fingerprint": canonical_hash(case),
        "manifest_sha256": hashlib.sha256(data.inputs[0]).hexdigest(),
        "lock_sha256": hashlib.sha256(data.inputs[1]).hexdigest(), "historical_lock": False,
        "scratch_paths": [], "test_tools": data.reference,
        "payload_tree_sha256": runtime.tree_fingerprint(target / "payload"),
        "build_probe": {"exit_status": 0, "source_unchanged": True, "phpunit_version": prepared["derived"]["phpunit_version"]},
        "image_audit": {"path": str(audit), "sha256": prep.sha(audit)},
    }
    fixture._put(target, "preparation.json", fixture._bytes(record))
    registry = fixture._put(tmp_path, "registry.json", fixture._bytes({
        lane.IMAGE_ALIAS: {"status": "ready", "image": data.resolver["image"]},
    }))
    assets = []
    def bundle(path, names=None, **kwargs):
        assets.append((Path(path), names, kwargs))
        return {"path": "/fixture/asset.json", "sha256": "a" * 64}
    monkeypatch.setattr(lane, "git", lambda repo, operation, argument: data.inputs[0 if argument.endswith("composer.json") else 1])
    ctx = SimpleNamespace(
        legacy=legacy, scratch=tmp_path,
        config={"migration": {lane.DEPENDENCY_ROOT_KEY: str(root), "toolchain_registry": str(registry)}},
        repo=lambda task: tmp_path / "repo", bundle=bundle, modules=lambda task: [],
        task_directory=lambda *args: legacy / "scripted/task_artifacts/42",
        immutable_tests=lambda task: ["tests/Cms/ResponderTest.php"],
        golden=lambda *args: {"path": "/fixture/fixed.patch", "sha256": "b" * 64},
    )
    recipe = lane.migrate(ctx, task, oracle)
    assert recipe.cpus == 2 and recipe.memory_mb == 4096 and recipe.scratch_paths == []
    assert "phpunit.dist.xml" in recipe.grading["immutable_paths"]
    assert recipe.grading["commands"][0]["success_pattern"] == r"^OK\ \(1\ test,\ 8\ assertions\)\s*$"
    command = shlex.split(recipe.grading["commands"][0]["command"])
    assert command[-1] == lane.normalize(lane.TEST_COMMAND)
    assert any(p == "ORACLE_TARGET=/workspace/src/Cms/Responder.php" for p in command)
    assert any(p.startswith("ORACLE_COVERAGE_JSON=/workspace/.reviewbench/scripted/artifacts/") for p in command)
    assert recipe.grading["commands"][0]["expected_tests"] == [task["id"] + "::tests/Cms/ResponderTest.php::testFromArray"]
    assert all("--no-dev" not in c and "composer require" not in c for c in recipe.bootstrap + recipe.dev_checks)
    assert assets[-1][2] == {"role": "grader"}
    fixture._put(legacy, "scripted/" + oracle["runs"]["golden"][0]["log"], "OK (1 test, 9 assertions)\n")
    with pytest.raises(ContractError, match="counts differ"):
        lane.completion_pattern(ctx, oracle, case)


@pytest.fixture(scope="module")
def gated_php(tmp_path_factory):
    config_path, registry_path = os.environ.get("KIRBY_PREP_CONFIG"), os.environ.get("KIRBY_TOOLCHAINS")
    if not config_path or not registry_path:
        pytest.skip("set the explicit shared-gate config and PHP84 registry for bounded PHP fixtures")
    from reviewbench_maintainability.agentic.cli import load_config
    from reviewbench_maintainability.agentic.execution import ExecutionServices
    from reviewbench_maintainability.agentic.resources import ResourceRequest
    root = tmp_path_factory.getbasetemp().resolve()
    stage = root / "php-fixture-stage"; stage.mkdir()
    services = ExecutionServices(load_config(Path(config_path)))
    image = json.loads(Path(registry_path).read_text())[lane.IMAGE_ALIAS]["image"]
    with services.gate.acquire(ResourceRequest("kirby-php-runtime-focused-fixtures", cpus=2,
                                               memory_bytes=4 * prep.GIB, scratch_bytes=512 * 1024**2),
                               uses_containers=True, timeout=600) as lease:
        runner = prep.ContainerRunner(
            services=services, lease=lease, image=image, stage=stage, evidence=root / "gated-php",
            network="none", timeout_seconds=240, extra_mounts=[(root, str(root), False)])
        audit = prep.image_audit(runner, root / "fixture-image-audit.json")
        assert audit["sha256"] == prep.sha(root / "fixture-image-audit.json")
        resolver = prep.resolver_identity(runner)
        entry = runtime.write_php_runtime(root / "kirby-entry.php")
        lint = runner.run("php-lint", ["php", "-l", str(entry)], network="none")
        assert lint["exit_status"] == 0, lint
        yield SimpleNamespace(root=root, runner=runner, resolver=resolver, entry=entry)


def _invoke(gated, phase, data, candidate, mirror, command=None, env=None):
    text = runtime.runtime_command(str(gated.entry), str(data.payload), str(mirror),
                                   data.resolver["image"], data.reference["record_sha256"],
                                   command, source=str(candidate), env=env)
    return gated.runner.run(phase, ["bash", "-c", text], network="none")


def test_gated_php_builds_current_candidate_and_only_derived_mirror_inputs(gated_php, tmp_path):
    root = tmp_path.resolve()
    data = _bundle(root / "fixture", gated_php.resolver)
    candidate = _candidate(root / "candidate", data.inputs)
    mirror = candidate / ".reviewbench/kirby-current"
    before = _source_snapshot(candidate)
    result = _invoke(gated_php, "current-candidate", data, candidate, mirror, "php check.php", {"HOME": str(candidate)})
    assert result["exit_status"] == 0, result["stdout"] + result["stderr"]
    actual = json.loads(result["stdout"].splitlines()[-1])
    assert actual == {"source": "current-source", "vendor": "current-vendor", "helper": "new-helper",
                      "removed": False, "cwd": str(mirror), "home": str(mirror)}
    assert _source_snapshot(candidate) == before
    assert (mirror / "composer.lock").read_bytes() == (data.sealed / "derived/composer.lock").read_bytes()
    assert (candidate / "composer.lock").read_bytes() == data.inputs[1] + b"\n"
    assert tools.PHPUNIT in json.loads((mirror / "composer.json").read_bytes())["require-dev"]
    assert tools.PHPUNIT not in json.loads((candidate / "composer.json").read_bytes())["require-dev"]
    assert (mirror / "empty-dir").is_dir()
    assert not (mirror / ".git").exists()
    assert (mirror / "vendor/composer/semver/src/Current.php").read_bytes() == (candidate / "vendor/composer/semver/src/Current.php").read_bytes()
    (candidate / "new-helpers/NewHelper.php").unlink()
    fixture._put(candidate, "new-helpers/Replacement.php", "<?php class Replacement {}\n")
    fixture._put(candidate, "refresh.php", "<?php require __DIR__.'/vendor/autoload.php'; echo json_encode([class_exists('NewHelper'),class_exists('Replacement')]),\"\\n\";\n")
    fixture._put(mirror, "stale.php", "old mirror")
    again = _invoke(gated_php, "refresh-current", data, candidate, mirror, "php refresh.php")
    assert again["exit_status"] == 0, again["stdout"] + again["stderr"]
    assert again["stdout"].endswith("[false,true]\n")
    assert not (mirror / "stale.php").exists() and not (mirror / "new-helpers/NewHelper.php").exists()


def test_gated_php_accepts_real_asset_permission_normalization_but_checks_execute_bits(gated_php, tmp_path):
    from reviewbench_maintainability.agentic.snapshots import extract_tar
    data = _bundle(tmp_path / "fixture", gated_php.resolver)
    original = data.payload
    archive = tmp_path / "payload.tar"
    with tarfile.open(archive, "w") as stream:
        for name, path in cc._tree(original).items():
            stream.add(path, arcname=name, recursive=False)
    restored = tmp_path / "restored"
    with archive.open("rb") as stream:
        extract_tar(stream, restored)
    sealed_input = tools.RECORD
    assert (original / runtime.BUILD_INPUTS / sealed_input).stat().st_mode & 0o777 == 0o444
    assert (restored / runtime.BUILD_INPUTS / sealed_input).stat().st_mode & 0o777 == 0o644
    data.payload = restored
    candidate = _candidate(tmp_path / "candidate", data.inputs)
    before = _source_snapshot(candidate)
    result = _invoke(gated_php, "archive-mode-roundtrip", data, candidate, tmp_path / "mirror", "php check.php")
    assert result["exit_status"] == 0, result["stdout"] + result["stderr"]
    assert _source_snapshot(candidate) == before
    (restored / "vendor/phpunit/phpunit/phpunit").chmod(0o755)
    rejected = _invoke(gated_php, "archive-mode-tamper", data, candidate, tmp_path / "bad-mirror", "touch TEST_RAN")
    assert rejected["exit_status"] == 2 and "executable modes changed" in rejected["stderr"]
    assert not (tmp_path / "bad-mirror/TEST_RAN").exists()
    assert _source_snapshot(candidate) == before


def test_gated_php_coverage_path_and_nonzero_test_exit_are_preserved(gated_php, tmp_path):
    root = tmp_path.resolve()
    data = _bundle(root / "fixture", gated_php.resolver)
    candidate = _candidate(root / "candidate", data.inputs)
    mirror = root / "mirror"
    helper = Path(os.environ["KIRBY_LEGACY_ROOT"]) / "scripted/tools/php_coverage_prepend.php"
    local_helper = fixture._put(root, "coverage.php", helper.read_bytes())
    output = root / "coverage.json"
    output.write_text('{"target":"stale","covered_lines":[999]}')
    result = _invoke(
        gated_php, "coverage", data, candidate, mirror,
        "php -d auto_prepend_file=" + shlex.quote(str(local_helper)) + " check.php",
        {"XDEBUG_MODE": "coverage", "ORACLE_TARGET": str(candidate / "src/Cms/Responder.php"),
         "ORACLE_COVERAGE_JSON": str(output)},
    )
    assert result["exit_status"] == 0, result["stdout"] + result["stderr"]
    coverage = json.loads(output.read_text())
    assert coverage["target"] == str(candidate / "src/Cms/Responder.php")
    assert 5 in coverage["covered_lines"] and 999 not in coverage["covered_lines"]
    assert coverage["coverage_backend"] == "xdebug"
    failure = _invoke(gated_php, "test-exit-status", data, candidate, mirror, "printf 'FIXED_FAILURE\\n'; exit 23")
    assert failure["exit_status"] == 23 and failure["stdout"].endswith("FIXED_FAILURE\n")


def test_gated_php_rejects_source_graph_changes_before_test_or_input_mutation(gated_php, tmp_path):
    root = tmp_path.resolve()
    data = _bundle(root / "fixture", gated_php.resolver)
    candidate = _candidate(root / "candidate", data.inputs)
    manifest = json.loads((candidate / "composer.json").read_bytes())
    manifest["require"]["fixture/prod"] = "^9"
    (candidate / "composer.json").write_bytes(fixture._bytes(manifest))
    before = _source_snapshot(candidate)
    result = _invoke(gated_php, "changed-source-graph", data, candidate, root / "mirror", "touch TEST_RAN")
    assert result["exit_status"] == 2 and "original dependency graph changed" in result["stderr"]
    assert _source_snapshot(candidate) == before
    assert not (root / "mirror/TEST_RAN").exists()
