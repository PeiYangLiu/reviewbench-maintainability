"""Pure bounded fixtures: execution records are SYNTHETIC, not real resolver proof.

No Composer/PHP/container/network calls occur. The optional original-input test
reads only copied Git manifest/lock bytes through COMPOSER_TEST_TOOLS_INPUTS.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import composer_candidate as cc
from agentic_runtime_migration import composer_test_tools as tools


def _bytes(data):
    return (json.dumps(data, indent=2) + "\n").encode()


def _task():
    return {"id": tools.TASK_ID, "start_sha": tools.START_SHA}


def _package(name, version="1.0.0", kind="library", **extra):
    return {
        "name": name, "version": version, "type": kind,
        "source": {"type": "git", "url": "https://example.invalid/" + name, "reference": "a" * 40},
        "dist": {"type": "zip", "url": "https://example.invalid/" + name + ".zip", "reference": "a" * 40},
        "require": {"php": ">=8.2"}, "autoload": {"classmap": ["src/"]}, **extra,
    }


def _inputs():
    manifest = {
        "name": "getkirby/cms", "type": "kirby-cms", "version": "5.2.0-rc.1",
        "require": {"fixture/prod": "1.0.0", tools.INERT_PLUGIN: "^1.2.1"},
        "require-dev": {"fixture/old-dev": "1.0.0"},
        "autoload": {"psr-4": {"Kirby\\": "src/"}, "classmap": ["dependencies/"],
                     "files": ["config/setup.php"]},
        "config": {"platform": {"php": "8.3.0"}, "platform-check": False,
                   "optimize-autoloader": True, "allow-plugins": {tools.INERT_PLUGIN: True}},
        "scripts": {"post-update-cmd": "curl -o cacert.pem https://example.invalid/cacert.pem"},
    }
    lock = {
        "packages": [_package("fixture/prod"), _package(
            tools.INERT_PLUGIN, "1.2.1", "composer-plugin",
            require={"composer-plugin-api": "^1.0 || ^2.0"}, extra={"class": "Fixture\\Installer"},
        )],
        "packages-dev": [_package("fixture/old-dev")], "aliases": [], "stability-flags": {},
        "minimum-stability": "stable", "prefer-stable": False, "prefer-lowest": False,
        "platform": {"php": ">=8.2"}, "platform-dev": {}, "platform-overrides": {"php": "8.3.0"},
        "plugin-api-version": "2.9.0", "content-hash": tools.composer_content_hash(manifest),
    }
    return _bytes(manifest), _bytes(lock)


def _derived(inputs):
    manifest, lock = map(json.loads, inputs)
    manifest["require-dev"][tools.PHPUNIT] = tools.CONSTRAINT
    lock["packages-dev"] += [
        _package(tools.PHPUNIT, "11.5.8", bin=["phpunit"],
                 require={"php": ">=8.3", "fixture/test-support": "^2.0"}),
        _package("fixture/test-support", "2.1.0"),
    ]
    lock["content-hash"] = tools.composer_content_hash(manifest)
    return _bytes(manifest), _bytes(lock)


def _resolver():
    return {
        "image": "sha256:" + "1" * 64,
        "composer_path": "/usr/local/bin/composer", "composer_sha256": "2" * 64,
        "version_argv": ["/usr/local/bin/composer", "--version"], "version_exit_status": 0,
        "version_stdout": "Composer version 2.9.0 UNIT-TEST-RESOLVER\n", "version_stderr": "",
    }


def _execution(phase):
    return {
        "argv": list(tools.COMMANDS[phase]), "environment": dict(tools.ENVIRONMENT),
        "exit_status": 0, "stdout": "UNIT-FIXTURE " + phase + "\n",
        "stderr": "", "workdir": "/workspace",
    }


def _put(root, name, data):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    return path


def _inventory(stage, lock_bytes):
    """Write intentionally synthetic installed metadata, never executable tooling."""
    lock = json.loads(lock_bytes)
    installed = []
    for package in lock["packages"] + lock["packages-dev"]:
        name = package["name"]
        _put(stage, "vendor/" + name + "/src/Fixture.php", "<?php // UNIT ONLY " + name + "\n")
        for binary in package.get("bin", []):
            _put(stage, "vendor/" + name + "/" + binary, "<?php // UNIT ONLY binary metadata fixture\n")
        installed.append({**package, "installation-source": "dist", "install-path": "../" + name})
    _put(stage, "vendor/composer/installed.json", _bytes({
        "packages": installed, "dev": True, "dev-package-names": sorted(p["name"] for p in lock["packages-dev"]),
    }))
    _put(stage, "vendor/composer/installed.php", "<?php return []; // UNIT ONLY\n")
    _put(stage, "vendor/composer/InstalledVersions.php", "<?php // UNIT ONLY\n")


def _snapshot(root):
    return {p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mode & 0o777)
            for p in root.rglob("*") if p.is_file()}


def _original_stage(root):
    inputs = _inputs()
    plan = tools.plan_test_tools(_task(), *inputs)
    stage = tools.stage_test_tools(root / "stage", plan, *inputs)
    _inventory(stage, inputs[1])
    checkpoint = tools.record_original_install(
        plan, stage, resolver=_resolver(), execution=_execution("original_install"),
    )
    return inputs, plan, stage, checkpoint


def _resolution_stage(root):
    inputs, plan, stage, original = _original_stage(root)
    derived = _derived(inputs)
    (stage / "composer.json").write_bytes(derived[0])
    (stage / "composer.lock").write_bytes(derived[1])
    resolution = tools.record_resolution(
        plan, stage, original_install=original, resolver=_resolver(), execution=_execution("resolve_tools"),
    )
    return inputs, plan, stage, original, derived, resolution


def _sealed(root):
    inputs, plan, stage, original, derived, resolution = _resolution_stage(root)
    _inventory(stage, derived[1])
    directory = root / "sealed"
    reference = tools.seal_prepared_tools(
        directory, plan, stage, original_manifest=inputs[0], original_lock=inputs[1],
        original_install=original, resolution=resolution, resolver=_resolver(),
        execution=_execution("derived_install"),
    )
    return directory, reference, stage, inputs


def _load(directory, reference):
    return tools.load_prepared_tools(
        directory, expected_sha256=reference["record_sha256"], image=reference["image"],
    )


def test_original_kirby_git_inputs_and_plugin_are_bound_without_resolution():
    location = os.environ.get("COMPOSER_TEST_TOOLS_INPUTS")
    if not location:
        pytest.skip("set COMPOSER_TEST_TOOLS_INPUTS to copied original Kirby Composer inputs")
    inputs = [(Path(location) / name).read_bytes() for name in ("composer.json", "composer.lock")]
    plan = tools.plan_test_tools(_task(), *inputs)
    assert plan["original"]["manifest_sha256"] == "1a243d7a423f5bc65f6e1cbad62187e81ef0756981b165c3df493cfe50ea5434"
    assert plan["original"]["lock_sha256"] == "cb19032804c07583d7aba943056ba516009a22f421c9a8df002373762b2a0c3d"
    lock = plan["original"]["lock"]
    assert len(lock["packages"]) == 16 and lock["packages-dev"] == []
    assert tools.composer_content_hash(plan["original"]["manifest"]) == lock["content-hash"]
    plugin = next(p for p in lock["packages"] if p["name"] == tools.INERT_PLUGIN)
    assert plugin["type"] == "composer-plugin" and plugin["version"] == "1.2.1"
    with pytest.raises(cc.ComposerCandidateError, match="installer/path"):
        cc._packages(lock)  # Old adapter is deliberately NOT silently broadened.
    assert not cc.is_composer_metadata("vendor/composer/semver/src/Semver.php")


def test_exact_plan_preserves_original_literal_and_executes_all_dependency_phases():
    inputs = _inputs()
    plan = tools.plan_test_tools(_task(), *inputs)
    assert plan["status"] == "planned" and plan["original_setup"] == tools.ORIGINAL_SETUP
    assert plan["requirement"] == "phpunit/phpunit:^11.5"
    commands = [shlex.split(command) for command in tools.preparation_commands(plan)]
    assert len(commands) == 3 and "--no-dev" not in sum(commands, [])
    assert "--no-install" in commands[1] and "--no-autoloader" not in commands[1]
    assert "--with-all-dependencies" in commands[1] and tools.REQUIREMENT in commands[1]
    assert all("--no-scripts" in c and "--no-plugins" in c for c in commands)
    assert all("--ignore-platform-req=ext-gd" in c and "--ignore-platform-reqs" not in c for c in commands)
    assert all("COMPOSER_NO_DEV=0" in c for c in commands)
    assert "--no-autoloader" in commands[0] and "--no-autoloader" in commands[2]


@pytest.mark.parametrize("mutation", ["task", "sha", "requirement", "no-dev", "suffix"])
def test_plan_does_not_generalize_the_literal_tool_request(mutation):
    task, setup = _task(), tools.ORIGINAL_SETUP
    if mutation == "task":
        task["id"] = "other"
    elif mutation == "sha":
        task["start_sha"] = "f" * 40
    elif mutation == "requirement":
        setup = setup.replace("^11.5", "^12")
    elif mutation == "no-dev":
        setup = setup.replace("install ", "install --no-dev ")
    else:
        setup += " && composer update"
    assert not tools.applies_to(task, setup)
    with pytest.raises(tools.TestToolsError, match="exact frozen"):
        tools.plan_test_tools(task, *_inputs(), setup)


def test_staging_only_copies_inputs_and_never_mutates_original_source(tmp_path):
    manifest, lock = _inputs()
    source = tmp_path / "candidate"
    _put(source, "composer.json", manifest)
    _put(source, "composer.lock", lock)
    _put(source, "src/Cms/Responder.php", "<?php // current candidate, not dependency data\n")
    before = _snapshot(source)
    plan = tools.plan_test_tools(_task(), manifest, lock)
    stage = tools.stage_test_tools(tmp_path / "stage", plan, manifest, lock)
    assert sorted(p.name for p in stage.iterdir()) == ["composer.json", "composer.lock"]
    assert (stage / "composer.json").read_bytes() == manifest
    assert _snapshot(source) == before
    with pytest.raises(FileExistsError):
        tools.stage_test_tools(source, plan, manifest, lock)
    with pytest.raises(tools.TestToolsError, match="bytes changed"):
        tools.stage_test_tools(tmp_path / "bad", plan, manifest + b"\n", lock)
    assert not (tmp_path / "bad").exists()


@pytest.mark.parametrize("mutation,match", [
    ("exit", "exit zero"), ("no-dev", "actual execution"), ("wrong-requirement", "actual execution"),
    ("no-dev-env", "all-dev"), ("platform-env", "platform exception"),
    ("image-alias", "digest"), ("missing-binary-hash", "SHA256"), ("missing-version", "version probe"),
    ("missing-output", "literal command output"),
])
def test_actual_execution_and_resolver_evidence_are_mandatory(tmp_path, mutation, match):
    inputs = _inputs()
    plan = tools.plan_test_tools(_task(), *inputs)
    stage = tools.stage_test_tools(tmp_path / "stage", plan, *inputs)
    _inventory(stage, inputs[1])
    resolver, evidence = _resolver(), _execution("original_install")
    if mutation == "exit":
        evidence["exit_status"] = 7
    elif mutation == "no-dev":
        evidence["argv"].append("--no-dev")
    elif mutation == "wrong-requirement":
        evidence["argv"] = list(tools.RESOLVE)
    elif mutation == "no-dev-env":
        evidence["environment"]["COMPOSER_NO_DEV"] = "1"
    elif mutation == "platform-env":
        evidence["environment"]["COMPOSER_IGNORE_PLATFORM_REQS"] = "1"
    elif mutation == "image-alias":
        resolver["image"] = "rb-scripted-php84-cov"
    elif mutation == "missing-binary-hash":
        del resolver["composer_sha256"]
    elif mutation == "missing-version":
        resolver["version_exit_status"] = 1
    else:
        del evidence["stdout"]
    with pytest.raises(tools.TestToolsError, match=match):
        tools.record_original_install(plan, stage, resolver=resolver, execution=evidence)


@pytest.mark.parametrize("version,compatible", [
    ("11.5.0", True), ("v11.5.99", True), ("11.6.0", True),
    ("11.4.99", False), ("12.0.0", False), ("10.5.0", False),
    ("11.5.x-dev", False), ("11.5.0-RC1", False), ("dev-main", False),
])
def test_phpunit_constraint_is_caret_11_5_not_old_vendored_or_latest_major(version, compatible):
    assert tools.phpunit_compatible(version) is compatible


@pytest.mark.parametrize("mutation,match", [
    ("drop-prod", "production package set"), ("add-prod", "production package set"),
    ("drop-dev", "original dev"), ("prod-version", "identity drift"), ("dev-version", "identity drift"),
    ("source-ref", "identity drift"), ("autoload", "beyond require-dev"),
    ("scripts", "beyond require-dev"), ("platform", "platform/stability"),
    ("old-phpunit", "PHPUnit"), ("bad-content-hash", "content-hash"),
    ("new-plugin", "installer adapter"), ("changed-plugin", "identity drift"),
])
def test_resolution_retains_all_original_dependencies_and_only_authorized_manifest_edit(tmp_path, mutation, match):
    inputs, plan, stage, checkpoint = _original_stage(tmp_path)
    manifest, lock = map(json.loads, _derived(inputs))
    if mutation == "drop-prod":
        lock["packages"] = lock["packages"][1:]
    elif mutation == "add-prod":
        lock["packages"].append(_package("fixture/unrelated"))
    elif mutation == "drop-dev":
        lock["packages-dev"] = lock["packages-dev"][1:]
    elif mutation == "prod-version":
        lock["packages"][0]["version"] = "2.0.0"
    elif mutation == "dev-version":
        lock["packages-dev"][0]["version"] = "2.0.0"
    elif mutation == "source-ref":
        lock["packages"][0]["source"]["reference"] = "b" * 40
    elif mutation == "autoload":
        manifest["autoload"]["classmap"] = ["historical-source/"]
    elif mutation == "scripts":
        manifest["scripts"] = {}
    elif mutation == "platform":
        lock["platform-overrides"]["php"] = "8.4.0"
    elif mutation == "old-phpunit":
        lock["packages-dev"][1]["version"] = "10.5.0"
    elif mutation == "bad-content-hash":
        lock["content-hash"] = "stale"
    elif mutation == "new-plugin":
        lock["packages-dev"].append(_package("fixture/plugin", kind="composer-plugin"))
    else:
        lock["packages"][1]["source"]["reference"] = "b" * 40
    (stage / "composer.json").write_bytes(_bytes(manifest))
    (stage / "composer.lock").write_bytes(_bytes(lock))
    with pytest.raises(tools.TestToolsError, match=match):
        tools.record_resolution(plan, stage, original_install=checkpoint,
                                resolver=_resolver(), execution=_execution("resolve_tools"))


def test_resolution_checkpoint_is_after_original_install_and_does_not_install_or_rewrite_vendor(tmp_path):
    inputs, plan, stage, checkpoint = _original_stage(tmp_path)
    derived = _derived(inputs)
    (stage / "composer.json").write_bytes(derived[0])
    (stage / "composer.lock").write_bytes(derived[1])
    _put(stage, "vendor/fixture/prod/src/Fixture.php", "<?php // forbidden resolver-side source change\n")
    with pytest.raises(tools.TestToolsError, match="resolve-only"):
        tools.record_resolution(plan, stage, original_install=checkpoint,
                                resolver=_resolver(), execution=_execution("resolve_tools"))


def test_seal_binds_original_derived_resolver_and_literal_evidence_and_never_claims_historical_lock(tmp_path):
    directory, reference, stage, inputs = _sealed(tmp_path)
    record = _load(directory, reference)
    assert reference["historical_lock"] is record["historical_lock"] is record["derived"]["historical_lock"] is False
    assert record["status"] == "prepared" and record["requirement"] == tools.REQUIREMENT
    assert record["resolver"] == _resolver() and record["original_setup"] == tools.ORIGINAL_SETUP
    assert record["derived"]["original_package_identities_preserved"] is True
    assert record["derived"]["added_dev_packages"] == ["fixture/test-support", "phpunit/phpunit"]
    assert reference["original_lock_sha256"] != reference["derived_lock_sha256"]
    for phase in ("original_install", "resolution", "derived_install"):
        assert record[phase]["execution"]["exit_status"] == 0
        assert record[phase]["execution"]["stdout"].startswith("UNIT-FIXTURE ")
    assert (directory / "original/composer.json").read_bytes() == inputs[0]
    assert (directory / "original/composer.lock").read_bytes() == inputs[1]
    assert (directory / "derived/composer.json").read_bytes() == (stage / "composer.json").read_bytes()
    assert all(not (p.stat().st_mode & 0o222) for p in directory.rglob("*") if p.is_file())
    assert tools.verify_sealed_install(directory, stage, expected_sha256=reference["record_sha256"],
                                      image=reference["image"]) == record
    with pytest.raises(tools.TestToolsError, match="unsealed"):
        tools.preparation_commands(record)


@pytest.mark.parametrize("mutation,match", [
    ("no-derived-install", "not all locked"), ("re-resolution", "re-resolved"),
    ("image-switch", "resolver binding"), ("missing-phpunit-file", "regular input"),
    ("source-checkout", "manifest-only"),
])
def test_seal_requires_complete_frozen_derived_install_not_only_successful_resolution(tmp_path, mutation, match):
    inputs, plan, stage, original, derived, resolution = _resolution_stage(tmp_path)
    resolver = _resolver()
    if mutation != "no-derived-install":
        _inventory(stage, derived[1])
    if mutation == "re-resolution":
        with (stage / "composer.lock").open("ab") as output:
            output.write(b"\n")
    elif mutation == "image-switch":
        resolver["image"] = "sha256:" + "9" * 64
    elif mutation == "missing-phpunit-file":
        (stage / "vendor/phpunit/phpunit/phpunit").unlink()
    elif mutation == "source-checkout":
        _put(stage, "src/Old.php", "<?php // historical candidate\n")
    with pytest.raises(tools.TestToolsError, match=match):
        tools.seal_prepared_tools(
            tmp_path / "sealed", plan, stage, original_manifest=inputs[0], original_lock=inputs[1],
            original_install=original, resolution=resolution, resolver=resolver,
            execution=_execution("derived_install"),
        )
    assert not (tmp_path / "sealed").exists()


@pytest.mark.parametrize("mutation,match", [
    ("original", "build input bytes"), ("derived", "build input bytes"), ("record", "record changed"),
    ("image", "runtime image"), ("digest", "record changed"), ("vendor", "dependencies changed after sealing"),
])
def test_prepared_reuse_authenticates_external_digest_image_and_exact_bytes_without_resolution(tmp_path, mutation, match):
    directory, reference, stage, _ = _sealed(tmp_path)
    if mutation in {"original", "derived"}:
        path = directory / mutation / "composer.lock"
        path.chmod(0o644)
        path.write_bytes(path.read_bytes() + b"\n")
    elif mutation == "record":
        path = directory / tools.RECORD
        path.chmod(0o644)
        path.write_bytes(path.read_bytes() + b"\n")
    elif mutation == "image":
        reference["image"] = "sha256:" + "9" * 64
    elif mutation == "digest":
        reference["record_sha256"] = "0" * 64
    else:
        _put(stage, "vendor/phpunit/phpunit/phpunit", "<?php // not the prepared PHPUnit\n")
    with pytest.raises(tools.TestToolsError, match=match):
        tools.verify_sealed_install(directory, stage, expected_sha256=reference["record_sha256"],
                                    image=reference["image"])


def test_candidate_projection_only_returns_explicit_mirror_inputs_preserving_current_source_and_autoload(tmp_path):
    directory, reference, _, inputs = _sealed(tmp_path)
    candidate = tmp_path / "candidate"
    manifest = json.loads(inputs[0])
    manifest["autoload"]["classmap"].append("new-helpers/")
    manifest["autoload-dev"] = {"psr-4": {"CurrentTests\\": "tests/new-helpers/"}}
    manifest["description"] = "current candidate, not historical dependency code"
    _put(candidate, "composer.json", _bytes(manifest))
    _put(candidate, "composer.lock", inputs[1] + b"\n")
    _put(candidate, "src/Cms/Responder.php", "<?php // current code\n")
    _put(candidate, "new-helpers/Headers.php", "<?php // current extracted helper\n")
    _put(candidate, "vendor/composer/semver/src/Semver.php", "<?php // current vendor edit\n")
    before = _snapshot(candidate)
    projected = tools.candidate_build_inputs(
        directory, expected_sha256=reference["record_sha256"], image=reference["image"],
        candidate_manifest=(candidate / "composer.json").read_bytes(),
        candidate_lock=(candidate / "composer.lock").read_bytes(),
    )
    assert set(projected["files"]) == {"composer.json", "composer.lock"}
    assert projected["changed_manifest_fields"] == [["require-dev", "phpunit/phpunit"]]
    assert projected["historical_lock"] is False
    rebuilt = json.loads(projected["files"]["composer.json"])
    expected = copy.deepcopy(manifest)
    expected["require-dev"]["phpunit/phpunit"] = "^11.5"
    assert rebuilt == expected
    assert projected["files"]["composer.lock"] == (directory / "derived/composer.lock").read_bytes()
    assert _snapshot(candidate) == before
    prepared = _load(directory, reference)
    contract = tools.composer_candidate_contract(
        prepared, (directory / "derived/composer.json").read_bytes(), projected["files"]["composer.lock"],
    )
    with pytest.raises(cc.ComposerCandidateError, match="dependency graph"):
        cc.validate_candidate_inputs(inputs[0], inputs[1], contract)
    cc.validate_candidate_inputs(projected["files"]["composer.json"], projected["files"]["composer.lock"], contract)


@pytest.mark.parametrize("mutation", ["require", "lock-version", "extra"])
def test_current_candidate_graph_changes_fail_without_reverting_candidate_inputs(tmp_path, mutation):
    directory, reference, _, inputs = _sealed(tmp_path)
    manifest, lock = map(json.loads, inputs)
    if mutation == "require":
        manifest["require"]["fixture/prod"] = "^2.0"
    elif mutation == "lock-version":
        lock["packages"][0]["version"] = "2.0.0"
    else:
        manifest["extra"] = {"changed-resolver-input": True}
    current = _bytes(manifest), _bytes(lock)
    with pytest.raises((tools.TestToolsError, cc.ComposerCandidateError)):
        tools.candidate_build_inputs(directory, expected_sha256=reference["record_sha256"],
                                    image=reference["image"], candidate_manifest=current[0], candidate_lock=current[1])
    assert current == (_bytes(manifest), _bytes(lock))


def test_parent_plugin_hook_is_exact_inert_and_preserves_real_plugin_type(tmp_path):
    directory, reference, _, _ = _sealed(tmp_path)
    prepared = _load(directory, reference)
    plugin = copy.deepcopy(prepared["plan"]["original"]["lock"]["packages"][1])
    before = copy.deepcopy(plugin)
    tools.validate_export_package(plugin, prepared)
    assert plugin == before and plugin["type"] == "composer-plugin"
    tools.validate_export_package(_package("fixture/ordinary"), prepared)
    for changed in ({**plugin, "name": "other/plugin"}, {**plugin, "version": "2.0.0"},
                    {**plugin, "extra": {"class": "Other\\Installer"}}):
        with pytest.raises(tools.TestToolsError, match="unchanged inert"):
            tools.validate_export_package(changed, prepared)


def test_runtime_contract_has_no_resolution_or_test_substitution_and_cli_only_stages(tmp_path, capsys):
    runtime = tools.runtime_contract()
    assert runtime["allow_resolution"] is False
    assert not {"require", "install", "update", "--no-dev"} & set(runtime["composer_argv"])
    assert runtime["environment"]["COMPOSER_DISABLE_NETWORK"] == "1"
    assert "--dev" in runtime["composer_argv"]
    assert runtime["phpunit_entry"] == "vendor/phpunit/phpunit/phpunit"
    assert runtime["fixed_selector"] == "tests/Cms/ResponderTest.php::testFromArray"
    assert runtime["coverage_target"] == "src/Cms/Responder.php"
    manifest, lock = _inputs()
    _put(tmp_path, "composer.json", manifest)
    _put(tmp_path, "composer.lock", lock)
    result = tools.main(["stage", "--manifest", str(tmp_path / "composer.json"), "--lock", str(tmp_path / "composer.lock"),
                         "--task-id", tools.TASK_ID, "--start-sha", tools.START_SHA, "--out", str(tmp_path / "stage")])
    assert result == 0
    assert json.loads(capsys.readouterr().out)["status"] == "planned"
    assert sorted(p.name for p in (tmp_path / "stage").iterdir()) == ["composer.json", "composer.lock"]
    assert tools.main(["runtime-contract"]) == 0
    assert json.loads(capsys.readouterr().out) == runtime


def test_pure_hooks_never_start_processes_or_containers(tmp_path, monkeypatch):
    import subprocess
    import socket
    def forbidden(*args, **kwargs):
        raise AssertionError("pure test-tool hooks must not execute or use the network")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    directory, reference, _, inputs = _sealed(tmp_path)
    assert tools.candidate_build_inputs(
        directory, expected_sha256=reference["record_sha256"], image=reference["image"],
        candidate_manifest=inputs[0], candidate_lock=inputs[1],
    )["files"]["composer.lock"]
