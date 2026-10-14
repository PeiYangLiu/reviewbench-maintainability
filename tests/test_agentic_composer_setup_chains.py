"""Bounded literal setups and explicitly new, never historical, Composer locks."""
import copy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import scripted_lane as lane
import prepare_agentic_scripted_dependencies as prepare
from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash, component
from reviewbench_maintainability.agentic.snapshots import collect_patch, git, initialize_git, tree_files


INSTALL = "composer install --no-interaction --prefer-dist --no-scripts --ignore-platform-reqs"
SULU_INSTALL = ("composer install --no-interaction --prefer-dist --no-scripts "
                "--ignore-platform-req=ext-vips --ignore-platform-req=ext-ffi")
CASES = {
    "contao": (
        "github__contao__contao__pull_8838__comment_2459042154__modularity", "contao/contao", 32,
        INSTALL + " && ln -sfn ../vendor core-bundle/vendor", None,
        "core-bundle/contao/library/Contao/Model.php", "OracleModelVirtualFieldsTest.php",
        "vendor/bin/phpunit -c core-bundle/phpunit.xml.dist OracleModelVirtualFieldsTest.php "
        "--filter testSetRowExpandsVirtualFields",
        "Model::setRow public virtual-field access behavior"),
    "sulu": (
        "github__sulu__sulu__pull_6872__comment_2059330528__api_design", "sulu/sulu", 73,
        SULU_INSTALL, None, "src/Sulu/Bundle/AdminBundle/Controller/IconController.php",
        "OracleIconControllerTest.php",
        "vendor/bin/phpunit --no-configuration --bootstrap vendor/autoload.php OracleIconControllerTest.php",
        "IconController public Icomoon search response behavior"),
    "shopware13722": (
        "github__shopware__shopware__pull_13722__comment_2567973929__modularity", "shopware/shopware", 64,
        "composer config audit.block-insecure false && " + INSTALL, "6.7.13.0",
        "src/Core/Content/MailTemplate/Api/MailActionController.php", "OracleMailActionControllerTest.php",
        "vendor/bin/phpunit --no-configuration --bootstrap vendor/autoload.php OracleMailActionControllerTest.php",
        "MailActionController public send payload behavior"),
    "shopware17596": (
        "github__shopware__shopware__pull_17596__comment_3452238525__modularity", "shopware/shopware", 65,
        "composer config audit.block-insecure false && " + INSTALL, "6.7.13.0",
        "src/Core/Content/Seo/SeoUrlRoute/EntityRouteResolver.php", None,
        "vendor/bin/phpunit --no-configuration --bootstrap vendor/autoload.php "
        "--filter testGetRouteNameReturnsRegisteredRoute "
        "tests/unit/Core/Content/Seo/SeoUrlRoute/EntityRouteResolverTest.php",
        "tests/unit/Core/Content/Seo/SeoUrlRoute/EntityRouteResolverTest.php::testGetRouteNameReturnsRegisteredRoute"),
}
IMAGE = "sha256:" + "a" * 64
LOCK = b'{"content-hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","packages":[],"packages-dev":[]}\n'


def digest(value):
    return hashlib.sha256(value).hexdigest()


def case(name="sulu", *, command=None, cwd="/work"):
    row = CASES[name]
    image = "rb-scripted-php82-cov"
    root = " -e COMPOSER_ROOT_VERSION=" + row[4] if row[4] else ""
    setup = (
        'mkdir -p {task_dir}/artifacts {root}/cache/composer && docker run --rm --user "$(id -u):$(id -g)" '
        "-e HOME=/work -e COMPOSER_CACHE_DIR=/composer-cache" + root +
        " -v {work}:/work -v {repo}:{repo}:ro -v {root}/cache/composer:/composer-cache -w " +
        cwd + " " + image + " bash -lc " + shlex.quote(command if command is not None else row[3]))
    test = (
        'rm -f {task_dir}/artifacts/coverage-{mode}-{run}.json; docker run --rm --user "$(id -u):$(id -g)" '
        "-e HOME=/work -e XDEBUG_MODE=coverage -e ORACLE_TARGET=/work/" + row[5] +
        " -e ORACLE_COVERAGE_JSON=/oracle-task/artifacts/coverage-{mode}-{run}.json "
        "-v {work}:/work -v {repo}:{repo}:ro -v {root}/tools:/oracle-tools:ro "
        "-v {task_dir}:/oracle-task -w /work " + image + " bash -lc " +
        shlex.quote("php -d auto_prepend_file=/oracle-tools/php_coverage_prepend.php " + row[7]))
    return {
        "environment": {"docker": image, "coverage": "Xdebug"}, "setup_host_command": setup,
        "test_host_command": test, "test_selectors": [row[8]],
        "session_files": [row[6]] if row[6] else [], "timeout": 1200,
        "outcome_regex": r"Tests:\s+(\d+), Assertions:\s+(\d+)" if name == "contao" else r"OK \((\d+) tests?,",
    }


@pytest.mark.parametrize("name", CASES)
def test_four_original_shapes_preserve_every_install_option_and_root_version(name):
    plan = lane.dependency_plan(case(name))
    original = SULU_INSTALL if name == "sulu" else INSTALL
    assert plan["install_flags"] == shlex.split(original)[2:]
    assert plan["platform_flags"] == [flag for flag in shlex.split(original) if flag.startswith("--ignore-platform-req")]
    assert plan["root_version"] == CASES[name][4]
    assert plan["workspace"] == "." and plan["no_dev"] is False
    command = prepare.install_command(plan, "")
    assert command.count(original) == 1
    assert "composer config audit.block-insecure false" not in command
    assert "ln -sfn" not in command
    if name == "contao":
        assert plan["vendor_alias"] == {"path": "core-bundle/vendor", "target": "../vendor"}
        assert "ln -sT -- ../vendor core-bundle/vendor" in command
    if name.startswith("shopware"):
        assert plan["audit_block_insecure"] is False
        assert "/tmp/composer" in command
    assert set(CASES[key][0] for key in CASES) | {
        "github__googleapis__google-cloud-php__pull_6708__comment_1397564001__api_design",
        "github__spryker-sdk__integrator__pull_34__comment_957304473__complexity",
    } == lane.UNLOCKED_COMPOSER_TASKS


@pytest.mark.parametrize("name", CASES)
@pytest.mark.parametrize("suffix", [" && true", "; true", "\ntrue", " || true", " | cat", " > output"])
def test_extra_shell_is_rejected_for_each_original_shape(name, suffix):
    with pytest.raises(ContractError):
        lane.dependency_plan(case(name, command=CASES[name][3] + suffix))


@pytest.mark.parametrize("command", [
    INSTALL + " && ln -sfn ../outside core-bundle/vendor",
    INSTALL + " && ln -sfn ../vendor /tmp/vendor",
    INSTALL + " && ln -sfn ../vendor core-bundle/vendor/",
    INSTALL + " && ln -sf ../vendor core-bundle/vendor",
    "composer config audit.block-insecure true && " + INSTALL,
    "composer config audit.abandoned ignore && " + INSTALL,
    "composer config audit.block-insecure false && " + CASES["contao"][3],
    "composer install --ignore-platform-req=ext-other",
    "composer install --ignore-platform-req=ext-*",
    "composer install --ignore-platform-req=$(printf php)",
    "composer\ninstall --no-scripts",
    'composer install "--no-scripts"',
    INSTALL + " # ignored",
])
def test_nearby_but_nonliteral_commands_do_not_expand_the_adapter(command):
    with pytest.raises(ContractError):
        lane.dependency_plan(case(command=command))


@pytest.mark.parametrize("name", ["contao", "shopware13722"])
def test_setup_chains_are_not_retargeted_to_another_workspace(name):
    with pytest.raises(ContractError, match="candidate root"):
        lane.dependency_plan(case(name, cwd="/work/nested"))


@pytest.mark.parametrize("manifest", [
    {}, {"config": {"audit": {"abandoned": "report"}}},
    {"config": {"audit": {"block-insecure": False}, "allow-plugins": {"fixture/plugin": True}}},
    {"config": {"policy": {"abandoned": {"audit": "report"}}}},
])
def test_isolated_policy_preserves_candidate_settings_without_new_exemptions(manifest):
    before = copy.deepcopy(manifest)
    result = lane.composer_home_config(lane.dependency_plan(case("shopware13722")), manifest)
    assert result == {"config": {"audit": {"block-insecure": False}}}
    assert manifest == before
    assert lane.composer_home_config(lane.dependency_plan(case("sulu")), manifest) == {}


@pytest.mark.parametrize("config", [
    None, [], {"audit": None}, {"audit": []},
    {"audit": {"block-insecure": True}}, {"audit": {"block-insecure": 0}},
    {"audit": {"block-insecure": None}}, {"audit": {"block-insecure": "false"}},
    {"policy": False}, {"policy": {"advisories": False}},
    {"policy": {"advisories": {"block": False}}}, {"policy": {"advisories": {"block": True}}},
])
def test_root_policy_override_is_explicitly_rejected(config):
    manifest = {"config": config}
    before = json.dumps(manifest)
    with pytest.raises(ContractError, match="overrides Composer audit policy"):
        lane.composer_home_config(lane.dependency_plan(case("shopware13722")), manifest)
    assert json.dumps(manifest) == before


def test_runtime_policy_program_uses_same_rules_and_writes_only_isolated_home():
    command = lane.composer_policy_command(lane.dependency_plan(case("shopware13722")), "/tmp/isolated-home")
    words = shlex.split(command)
    assert words[:2] == ["php", "-r"]
    assert json.loads(words[3]) == [[path, kind] for path, kind in lane.COMPOSER_POLICY_RULES]
    assert words[4] == "/tmp/isolated-home"
    assert json.loads(words[5]) == {"config": {"audit": {"block-insecure": False}}}
    assert '$file = $home . "/config.json";' in words[2]
    assert 'getenv("COMPOSER_HOME") !== $home' in words[2]
    assert "file_put_contents(\"composer.json\"" not in words[2]


def test_emitted_policy_program_with_real_php_when_available(tmp_path):
    if shutil.which("php") is None:
        pytest.skip("PHP interpreter is supplied by the parent-owned tool image")
    manifest, lock = tmp_path / "composer.json", tmp_path / "composer.lock"
    manifest.write_text('{"config":{"audit":{"abandoned":"report"}}}\n')
    lock.write_bytes(LOCK)
    before = (manifest.read_bytes(), lock.read_bytes())
    home = tmp_path / "home"
    command = lane.composer_policy_command(lane.dependency_plan(case("shopware13722")), str(home))
    result = subprocess.run(["bash", "-c", command], cwd=tmp_path,
                            env={**os.environ, "COMPOSER_HOME": str(home)}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads((home / "config.json").read_text()) == {"config": {"audit": {"block-insecure": False}}}
    assert (manifest.read_bytes(), lock.read_bytes()) == before
    manifest.write_text('{"config":{"audit":{"block-insecure":true}}}')
    rejected = subprocess.run(["bash", "-c", command], cwd=tmp_path,
                              env={**os.environ, "COMPOSER_HOME": str(home)}, capture_output=True, text=True)
    assert rejected.returncode == 2 and "overrides Composer audit policy" in rejected.stderr
    assert manifest.read_text() == '{"config":{"audit":{"block-insecure":true}}}'


def alias_fixture(tmp_path):
    work = tmp_path / "candidate"
    (work / "core-bundle/src").mkdir(parents=True)
    (work / "core-bundle/src/Model.php").write_text("current candidate")
    (work / "vendor/library").mkdir(parents=True)
    (work / "vendor/library/api.php").write_text("dependency")
    (work / "composer.json").write_text("{}\n")
    return work, work / ".reviewbench/scripted", lane.dependency_plan(case("contao"))


def bash(command, *, env=None):
    return subprocess.run(["bash", "-c", command], env=env, capture_output=True, text=True, timeout=20)


def test_alias_is_owned_idempotent_and_bound_to_the_current_candidate(tmp_path):
    work, state, plan = alias_fixture(tmp_path)
    before = {name: path.read_bytes() for name, path in tree_files(work).items()}
    check = lane.composer_alias_command(plan, str(work), str(state), create=False)
    assert bash(check).returncode == 0
    alias = work / "core-bundle/vendor"
    assert not alias.is_symlink()
    command = lane.composer_alias_command(plan, str(work), str(state), create=True)
    for _ in range(2):
        result = bash(command)
        assert result.returncode == 0, result.stderr
        assert alias.readlink().as_posix() == "../vendor"
    assert (state / "composer-vendor-alias").is_file()
    assert all((work / name).read_bytes() == value for name, value in before.items())
    (work / "core-bundle/src/NewHelper.php").write_text("new live source")
    assert bash(command).returncode == 0
    assert (work / "core-bundle/src/NewHelper.php").read_text() == "new live source"
    alias.unlink()
    alias.write_text("candidate-owned replacement")
    assert bash(command).returncode == 2
    assert alias.read_text() == "candidate-owned replacement"


@pytest.mark.parametrize("change", [
    "parent-link", "missing-parent", "parent-file", "vendor-link", "vendor-file",
    "alias-file", "alias-directory", "unowned-link", "escaping-link", "dangling-link",
    "marker-link", "marker-directory", "wrong-owner", "state-link", "workspace-link",
])
def test_alias_boundaries_fail_before_any_candidate_overwrite(tmp_path, change):
    work, state, plan = alias_fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("unchanged")
    alias = work / "core-bundle/vendor"
    if change in {"parent-link", "missing-parent", "parent-file"}:
        shutil.rmtree(work / "core-bundle")
        if change == "parent-link":
            (work / "core-bundle").symlink_to(outside, target_is_directory=True)
        elif change == "parent-file":
            (work / "core-bundle").write_text("candidate source")
    elif change in {"vendor-link", "vendor-file"}:
        shutil.rmtree(work / "vendor")
        if change == "vendor-link":
            (work / "vendor").symlink_to(outside, target_is_directory=True)
        else:
            (work / "vendor").write_text("candidate source")
    elif change == "alias-file":
        alias.write_text("candidate source")
    elif change == "alias-directory":
        alias.mkdir()
        (alias / "source.php").write_text("candidate source")
    elif change in {"unowned-link", "escaping-link", "dangling-link"}:
        alias.symlink_to({"unowned-link": "../vendor", "escaping-link": str(outside),
                         "dangling-link": "../missing"}[change])
    elif change == "state-link":
        state.parent.mkdir()
        state.symlink_to(outside, target_is_directory=True)
    elif change == "workspace-link":
        linked = tmp_path / "linked-candidate"
        linked.symlink_to(work, target_is_directory=True)
        work = linked
    else:
        state.mkdir(parents=True)
        marker = state / "composer-vendor-alias"
        if change == "marker-link":
            marker.symlink_to(sentinel)
        elif change == "marker-directory":
            marker.mkdir()
        else:
            marker.write_text("another owner")
    for create in (False, True):
        command = lane.composer_alias_command(plan, str(work), str(state), create=create)
        result = bash(command + " && printf 'unexpected continuation'")
        assert result.returncode == 2, (change, result.stdout, result.stderr)
        assert result.stdout == ""
    assert sentinel.read_text() == "unchanged"
    if change == "alias-file":
        assert alias.read_text() == "candidate source"
    if change == "alias-directory":
        assert (alias / "source.php").read_text() == "candidate source"


def repository_fixture(tmp_path, name="sulu", *, locked=False, historical=False):
    repository = tmp_path / "repo"
    (repository / "src").mkdir(parents=True)
    (repository / "src/Source.php").write_text("<?php // original source\n")
    (repository / "core-bundle").mkdir()
    (repository / "core-bundle/.keep").write_text("")
    (repository / "composer.json").write_text('{"name":"fixture/project","autoload":{"psr-4":{"Fixture\\\\":"src/"}}}\n')
    if locked or historical:
        (repository / "composer.lock").write_bytes(LOCK)
    initialize_git(repository)
    if historical and not locked:
        (repository / "composer.lock").unlink()
        git(repository, "add", "-A")
        git(repository, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "commit", "-m", "lockless frozen start")
    row = CASES[name]
    task = {"id": row[0], "repo": row[1], "start_sha": git(repository, "rev-parse", "HEAD").decode().strip(),
            "locality": {"path": row[5]}}
    return repository, task, case(name), lane.dependency_plan(case(name))


@pytest.mark.parametrize("name", CASES)
def test_absent_start_lock_requires_explicit_opt_in_and_never_uses_history(tmp_path, name):
    repository, task, config, plan = repository_fixture(tmp_path, name, historical=True)
    with pytest.raises(ContractError, match="explicit --resolve-unlocked"):
        lane.dependency_start_inputs(repository, task, plan)
    inputs = lane.dependency_start_inputs(repository, task, plan, resolve_unlocked=True)
    assert inputs == {"manifest": (repository / "composer.json").read_bytes(), "lock": None}
    assert not (repository / "composer.lock").exists()


@pytest.mark.parametrize("opt_in", [False, True])
def test_existing_tracked_lock_is_always_the_exact_start_input(tmp_path, opt_in):
    repository, task, config, plan = repository_fixture(tmp_path, locked=True)
    inputs = lane.dependency_start_inputs(repository, task, plan, resolve_unlocked=opt_in)
    assert inputs["lock"] == LOCK
    assert (repository / "composer.lock").read_bytes() == LOCK


def test_unlocked_opt_in_is_not_a_generic_missing_input_fallback(tmp_path):
    repository, task, config, plan = repository_fixture(tmp_path)
    with pytest.raises(ContractError, match="allowlisted"):
        lane.dependency_start_inputs(repository, {**task, "id": "unassigned-task"}, plan, resolve_unlocked=True)
    with pytest.raises(ContractError, match="manifest is absent"):
        lane.dependency_start_inputs(repository, task, {**plan, "manifest": "missing.json"}, resolve_unlocked=True)
    (repository / "composer.lock").symlink_to("composer.json")
    git(repository, "add", "-A")
    git(repository, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-m", "non-regular lock")
    task["start_sha"] = git(repository, "rev-parse", "HEAD").decode().strip()
    with pytest.raises(ContractError, match="not a regular file"):
        lane.dependency_start_inputs(repository, task, plan, resolve_unlocked=True)


def test_untracked_or_substituted_lock_is_not_an_original_start_lock(tmp_path):
    repository, task, config, plan = repository_fixture(tmp_path)
    (repository / "composer.lock").write_bytes(LOCK)
    inputs = lane.dependency_start_inputs(repository, task, plan, resolve_unlocked=True)
    assert inputs["lock"] is None
    with pytest.raises(ContractError, match="substituted start lock"):
        prepare.validate_install_inputs(repository, plan, inputs)
    (repository / "composer.lock").unlink()
    prepare.validate_install_inputs(repository, plan, inputs)
    with pytest.raises(ContractError, match="regular lock"):
        prepare.validate_install_inputs(repository, plan, inputs, installed=True)


@pytest.mark.parametrize("config", [{"lock": False}, {"vendor-dir": "elsewhere"}])
def test_preparation_rejects_settings_that_bypass_its_pinned_input_contract(tmp_path, config):
    repository, task, _, plan = repository_fixture(tmp_path)
    manifest = json.dumps({"config": config}).encode()
    (repository / "composer.json").write_bytes(manifest)
    with pytest.raises(ContractError, match="requires lock usage"):
        prepare.validate_install_inputs(repository, plan, {"manifest": manifest, "lock": None})


@pytest.mark.parametrize("field", ["manifest", "lock"])
@pytest.mark.parametrize("change", ["bytes", "symlink"])
def test_install_checks_keep_manifests_and_tracked_locks_immutable(tmp_path, field, change):
    repository, task, config, plan = repository_fixture(tmp_path, locked=True)
    inputs = lane.dependency_start_inputs(repository, task, plan)
    work = tmp_path / "isolated"
    shutil.copytree(repository, work)
    prepare.validate_install_inputs(work, plan, inputs)
    path = work / plan[field]
    if change == "bytes":
        path.write_text("changed")
    else:
        path.unlink()
        path.symlink_to(repository / plan[field])
    with pytest.raises(ContractError, match="frozen|regular"):
        prepare.validate_install_inputs(work, plan, inputs, installed=True)
    assert (repository / plan[field]).read_bytes() == inputs[field]


def prepared_fixture(tmp_path, name="sulu"):
    repository, task, config, plan = repository_fixture(tmp_path, name)
    inputs = lane.dependency_start_inputs(repository, task, plan, resolve_unlocked=True)
    work, target = tmp_path / "isolated", tmp_path / "prepared" / component(task["id"])
    shutil.copytree(repository, work)
    prepare.validate_install_inputs(work, plan, inputs)
    source_fingerprint = lane.dependency_tree_fingerprint(work)
    payload = target / "payload"
    resolution = payload / "resolution"
    resolution.mkdir(parents=True)
    (payload / "vendor/library").mkdir(parents=True)
    (payload / "vendor/library/api.php").write_text("fixture dependency")
    (resolution / "resolver-version.txt").write_text("Composer version fixture-2.9.3\n")
    (resolution / "resolver-sha256.txt").write_text("b" * 64 + "  /usr/local/bin/composer\n")
    (work / "composer.lock").write_bytes(LOCK)
    prepare.validate_install_inputs(work, plan, inputs, installed=True)
    record = {
        "id": task["id"], "repo": task["repo"], "start_sha": task["start_sha"], "status": "prepared",
        "plan": plan, "config_fingerprint": canonical_hash(config), "manifest_sha256": digest(inputs["manifest"]),
        "image": IMAGE, "source_git_tree": git(repository, "rev-parse", task["start_sha"] + "^{tree}").decode().strip(),
        "source_tree_sha256": source_fingerprint, "environment_sha256": canonical_hash({"CI": "true"}),
        "command": "fixture resolver command, not a real dependency replay",
        "scratch_paths": ["vendor/library", *lane.composer_alias_paths(plan)],
    }
    prepare.capture_composer_resolution(work, payload, plan, record)
    record["payload_tree_sha256"] = lane.dependency_tree_fingerprint(payload)
    (target / "preparation.json").write_text(json.dumps(record))
    return repository, task, config, plan, target, payload, record


def test_new_lock_is_archived_with_nonhistorical_provenance_and_actual_input_hashes(tmp_path):
    repository, task, config, plan, target, payload, record = prepared_fixture(tmp_path)
    assert record["lock_origin"] == lane.RESOLVED_COMPOSER_LOCK
    assert record["lock_sha256"] == digest(LOCK)
    provenance = json.loads((payload / "resolution/provenance.json").read_text())
    assert provenance["kind"] == "resolved-from-start-manifest"
    assert provenance["historical_lock"] is False and provenance["start_lock_present"] is False
    assert provenance["image"] == IMAGE
    assert provenance["resolver"] == {"version": "Composer version fixture-2.9.3",
                                      "path": "/usr/local/bin/composer", "sha256": "b" * 64}
    assert provenance["manifest_sha256"] == digest((repository / "composer.json").read_bytes())
    assert provenance["plan_sha256"] == canonical_hash(plan)
    assert provenance["command_sha256"] == digest(record["command"].encode())
    with tarfile.open(payload / "resolution.tar") as stream:
        assert stream.getnames() == ["composer.lock", "provenance.json"]
        assert stream.extractfile("composer.lock").read() == LOCK
        assert json.loads(stream.extractfile("provenance.json").read()) == provenance
    lane.validate_prepared_dependencies(repository, task, config, plan, record, payload, image=IMAGE)
    assert not (repository / "composer.lock").exists()
    assert "composer.lock" not in record["scratch_paths"]


@pytest.mark.parametrize("change", [
    "lock", "archive", "provenance", "manifest-hash", "resolver", "image", "environment", "command",
])
def test_generated_lock_is_bound_to_bytes_provenance_and_resolver_inputs(tmp_path, change):
    repository, task, config, plan, target, payload, record = prepared_fixture(tmp_path)
    if change in {"lock", "archive", "provenance"}:
        path = payload / {"lock": "resolution/composer.lock", "archive": "resolution.tar",
                          "provenance": "resolution/provenance.json"}[change]
        path.write_bytes(path.read_bytes() + b"\nchanged")
    elif change == "manifest-hash":
        record["manifest_sha256"] = "f" * 64
    elif change == "resolver":
        record["resolver"]["sha256"] = "f" * 64
    elif change == "image":
        record["image"] = "sha256:" + "f" * 64
    elif change == "environment":
        record["environment_sha256"] = "f" * 64
    else:
        record["command"] += " changed resolver invocation"
    with pytest.raises(ContractError):
        lane.validate_prepared_dependencies(repository, task, config, plan, record, payload, image=IMAGE)


def test_prepared_resolution_is_reused_without_opt_in_and_stale_packages_do_not_float(tmp_path):
    repository, task, config, plan, target, payload, record = prepared_fixture(tmp_path)
    before = (target / "preparation.json").read_bytes()
    assert prepare.reuse_prepared_dependencies(target, repository, task, config, plan) == record
    assert (target / "preparation.json").read_bytes() == before
    (payload / "resolution/composer.lock").write_bytes(LOCK + b"tampered")
    with pytest.raises(ContractError, match="bytes changed"):
        prepare.reuse_prepared_dependencies(target, repository, task, config, plan)
    assert (target / "preparation.json").read_bytes() == before
    (target / "preparation.json").write_text('{"status":"pending"}')
    with pytest.raises(ContractError, match="not a new resolution"):
        prepare.reuse_prepared_dependencies(target, repository, task, config, plan)


def runtime_fixture(tmp_path, *, supplied=False):
    work, state, _ = alias_fixture(tmp_path)
    (work / "src").mkdir()
    (work / "src/NewHelper.php").write_text("current helper")
    (work / ".new-source.php").write_text("hidden current source")
    (work / "NewRootHelper.php").write_text("new root source")
    candidate_lock = LOCK.replace(b'"packages":[]', b'"packages":[{"name":"candidate/package"}]')
    if supplied:
        (work / "composer.lock").write_bytes(candidate_lock)
    assets = tmp_path / "assets"
    (assets / "resolution").mkdir(parents=True)
    (assets / "resolution/composer.lock").write_bytes(LOCK)
    tools = tmp_path / "tools"
    tools.mkdir()
    shim = tools / "composer"
    shim.write_text(f"#!{sys.executable}\n" + r'''
import json, os, pathlib, sys
root = pathlib.Path.cwd()
config = json.loads((root / "composer.json").read_text()).get("config", {})
if "config" in sys.argv:
    key = sys.argv[-1]
    value = config.get(key, {"lock": True, "vendor-dir": "vendor"}[key])
    print(json.dumps(value) if not isinstance(value, str) else value)
    raise SystemExit(0)
assert sys.argv[1] == "install", sys.argv
assert os.environ["COMPOSER_DISABLE_NETWORK"] == "1"
assert os.environ["COMPOSER"] == "composer.json"
assert (root / "composer.lock").is_file(), "floating resolution attempted"
observed = {
    "flags": sys.argv[2:], "lock": (root / "composer.lock").read_text(),
    "manifest": (root / "composer.json").read_text(),
    "helper": (root / "src/NewHelper.php").read_text(),
    "hidden": (root / ".new-source.php").read_text(),
    "root_helper": (root / "NewRootHelper.php").read_text(),
}
pathlib.Path(os.environ["COMPOSER_TEST_OBSERVATION"]).write_text(json.dumps(observed))
(root / "generated-setup.json").write_text("generated only in scratch")
if os.environ.get("COMPOSER_TEST_MUTATE"):
    (root / os.environ["COMPOSER_TEST_MUTATE"]).write_text("unexpected install mutation")
raise SystemExit(int(os.environ.get("COMPOSER_TEST_EXIT", "0")))
''')
    shim.chmod(0o755)
    observation = tmp_path / "observed.json"
    env = {**os.environ, "PATH": str(tools) + ":" + os.environ["PATH"],
           "COMPOSER_TEST_OBSERVATION": str(observation)}
    plan = lane.dependency_plan(case("sulu"))
    record = {"lock_origin": lane.RESOLVED_COMPOSER_LOCK, "lock_sha256": digest(LOCK)}
    command = lane.composer_locked_install_command(plan, record, str(work), str(state), assets=str(assets))
    return SimpleNamespace(work=work, state=state, assets=assets, observation=observation,
                           env=env, command=command, plan=plan, candidate_lock=candidate_lock)


@pytest.mark.parametrize("supplied", [False, True])
def test_runtime_selects_only_absent_lock_default_and_keeps_all_current_sources(tmp_path, supplied):
    fixture = runtime_fixture(tmp_path, supplied=supplied)
    original = {name: path.read_bytes() for name, path in tree_files(fixture.work).items()}
    if supplied:
        # An inactive default must not replace or even be read for a candidate lock.
        (fixture.assets / "resolution/composer.lock").unlink()
    for text in ("current helper", "edited helper with a new dependency"):
        (fixture.work / "src/NewHelper.php").write_text(text)
        result = bash(fixture.command, env=fixture.env)
        assert result.returncode == 0, result.stderr
        observed = json.loads(fixture.observation.read_text())
        assert observed["lock"].encode() == (fixture.candidate_lock if supplied else LOCK)
        assert observed["flags"] == fixture.plan["install_flags"]
        assert observed["helper"] == text and observed["hidden"] == "hidden current source"
        assert observed["root_helper"] == "new root source"
    assert (fixture.work / "composer.lock").exists() == supplied
    assert not (fixture.work / "generated-setup.json").exists()
    assert (fixture.state / "composer-source/generated-setup.json").read_text() == "generated only in scratch"
    assert all((fixture.work / name).read_bytes() == value for name, value in original.items()
               if name != "src/NewHelper.php")
    # No generated default/setup files enter the engine's production-file walk.
    files = tree_files(fixture.work)
    assert ("composer.lock" in files) == supplied
    assert all("composer-source" not in name and "generated-setup" not in name for name in files)


def test_candidate_lock_addition_remains_a_production_patch_not_declared_scratch(tmp_path):
    fixture = runtime_fixture(tmp_path)
    base = tmp_path / "base"
    shutil.copytree(fixture.work, base)
    assert bash(fixture.command, env=fixture.env).returncode == 0
    default_patch = tmp_path / "default.patch"
    collect_patch(base, fixture.work, default_patch)
    assert default_patch.read_text() == ""
    (fixture.work / "composer.lock").write_bytes(fixture.candidate_lock)
    assert bash(fixture.command, env=fixture.env).returncode == 0
    candidate_patch = tmp_path / "candidate.patch"
    collect_patch(base, fixture.work, candidate_patch)
    assert "diff --git a/composer.lock b/composer.lock" in candidate_patch.read_text()
    assert "candidate/package" in candidate_patch.read_text()
    assert "resolved-from-start-manifest" not in candidate_patch.read_text()


@pytest.mark.parametrize("change", [
    "default-lock", "lock-link", "lock-directory", "manifest-link", "view-link", "owner-link",
    "unowned-view", "lock-disabled", "vendor-redirect",
])
def test_runtime_default_boundary_rejects_tampering_and_floating_resolution(tmp_path, change):
    fixture = runtime_fixture(tmp_path)
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(LOCK)
    if change == "default-lock":
        (fixture.assets / "resolution/composer.lock").write_bytes(b"changed")
    elif change == "lock-link":
        (fixture.work / "composer.lock").symlink_to(sentinel)
    elif change == "lock-directory":
        (fixture.work / "composer.lock").mkdir()
    elif change == "manifest-link":
        (fixture.work / "composer.json").unlink()
        (fixture.work / "composer.json").symlink_to(sentinel)
    elif change in {"lock-disabled", "vendor-redirect"}:
        config = {"lock": False} if change == "lock-disabled" else {"vendor-dir": "src"}
        (fixture.work / "composer.json").write_text(json.dumps({"config": config}))
    else:
        fixture.state.mkdir(parents=True)
        if change == "view-link":
            (fixture.state / "composer-source").symlink_to(fixture.work, target_is_directory=True)
        elif change == "owner-link":
            (fixture.state / "composer-source-owner").symlink_to(sentinel)
        else:
            (fixture.state / "composer-source").mkdir()
    result = bash(fixture.command + " && printf 'unexpected continuation'", env=fixture.env)
    assert result.returncode == 2, result.stderr
    assert not fixture.observation.exists()
    assert sentinel.read_bytes() == LOCK
    assert (fixture.work / "src/NewHelper.php").read_text() == "current helper"


@pytest.mark.parametrize("mutate", ["composer.json", "composer.lock"])
def test_install_mutation_is_confined_to_scratch_and_never_changes_candidate_inputs(tmp_path, mutate):
    fixture = runtime_fixture(tmp_path, supplied=True)
    before = {name: (fixture.work / name).read_bytes() for name in ("composer.json", "composer.lock")}
    result = bash(fixture.command, env={**fixture.env, "COMPOSER_TEST_MUTATE": mutate})
    assert result.returncode == 2
    assert all((fixture.work / name).read_bytes() == data for name, data in before.items())


def test_runtime_install_failure_cannot_continue_or_publish_a_generated_lock(tmp_path):
    fixture = runtime_fixture(tmp_path)
    result = bash(fixture.command + " && printf 'unexpected continuation'",
                  env={**fixture.env, "COMPOSER_TEST_EXIT": "17"})
    assert result.returncode == 17 and result.stdout == ""
    assert not (fixture.work / "composer.lock").exists()


def preparation_cli_fixture(tmp_path, monkeypatch, *, locked=False, mutation=None):
    repository, task, config, plan = repository_fixture(tmp_path, locked=locked)
    legacy, output, scratch = tmp_path / "legacy", tmp_path / "output", tmp_path / "scratch"
    (legacy / "scripted").mkdir(parents=True)
    (legacy / "scripted/oracle_config.json").write_text(json.dumps({"tasks": {"73": config}}))
    scratch.mkdir()
    ids = tmp_path / "ids"
    ids.write_text(task["id"] + "\n")
    calls = []

    @contextmanager
    def acquire(*args, **kwargs):
        yield {"lease": "fixture-lease"}

    services = SimpleNamespace(scratch=scratch, source_bytes=lambda *args: 1024,
                               gate=SimpleNamespace(acquire=acquire, status=lambda: {}))
    monkeypatch.setattr(prepare, "load_config", lambda path: {"tasks": "fixture", "oracles": "fixture"})
    monkeypatch.setattr(prepare, "selected_tasks", lambda *args: {task["id"]: task})
    monkeypatch.setattr(prepare, "read_jsonl", lambda path: {task["id"]: {"index": 73}})
    monkeypatch.setattr(prepare, "ExecutionServices", lambda config: services)
    monkeypatch.setattr(prepare, "MigrationContext", lambda *args: SimpleNamespace(
        repo=lambda task: repository, image=lambda name: (IMAGE, {"fixture": True})))
    monkeypatch.setattr(prepare, "bounded_cpu_set", lambda *args: "0,1")
    monkeypatch.setattr(prepare, "completed_jobs", lambda fn, batch, workers: ((ident, fn(ident)) for ident in batch))

    def resolver(argv, stdout, stderr):
        calls.append(argv)
        mounts = [dict(part.split("=", 1) for part in argv[i + 1].split(",") if "=" in part)
                  for i, arg in enumerate(argv) if arg == "--mount"]
        source = Path(next(m["source"] for m in mounts if m["target"] == "/workspace"))
        payload = Path(next(m["source"] for m in mounts if m["target"] == lane.DEPENDENCY_CACHE))
        assert (source / "composer.json").read_bytes() == (repository / "composer.json").read_bytes()
        assert (source / "composer.lock").exists() == locked
        assert SULU_INSTALL in argv[-1] and "composer update" not in argv[-1]
        (source / "vendor/library").mkdir(parents=True)
        (source / "vendor/library/api.php").write_text("fixture dependency")
        if not locked:
            (source / "composer.lock").write_bytes(LOCK)
            (payload / "resolution").mkdir()
            (payload / "resolution/resolver-version.txt").write_text("Composer version fixture-2.9.3\n")
            (payload / "resolution/resolver-sha256.txt").write_text("b" * 64 + "  /usr/local/bin/composer\n")
        if mutation:
            (source / mutation).write_text("changed by fixture install")
        stdout.write("fixture Composer install; no external resolver executed\n")
        return SimpleNamespace(returncode=0, poll=lambda: 0)

    return SimpleNamespace(repository=repository, task=task, config=config, plan=plan, output=output,
                           legacy=legacy, ids=ids, calls=calls, resolver=resolver)


def test_cli_opt_in_resolves_once_then_reuses_or_rejects_without_floating(tmp_path, monkeypatch):
    # Intercept only the explicit preparation Docker process, not local Git.
    real_popen = subprocess.Popen
    fixture = preparation_cli_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(prepare.subprocess, "Popen",
                        lambda argv, *args, **kwargs: fixture.resolver(argv, *args, **kwargs)
                        if argv[0] == "docker" else real_popen(argv, *args, **kwargs))
    argv = ["prepare", "--config", "fixture", "--legacy-root", str(fixture.legacy),
            "--out-dir", str(fixture.output), "--task-ids", str(fixture.ids), "--workers", "1"]
    monkeypatch.setattr(sys, "argv", argv)
    assert prepare.main() == 2 and fixture.calls == []
    monkeypatch.setattr(sys, "argv", [*argv, "--resolve-unlocked"])
    assert prepare.main() == 0 and len(fixture.calls) == 1
    target = fixture.output / component(fixture.task["id"])
    original = (target / "preparation.json").read_bytes()
    record = json.loads(original)
    assert record["lock_origin"] == lane.RESOLVED_COMPOSER_LOCK
    assert record["lock_sha256"] == digest(LOCK)
    monkeypatch.setattr(sys, "argv", argv)
    assert prepare.main() == 0 and len(fixture.calls) == 1
    assert (target / "preparation.json").read_bytes() == original
    (target / "payload/resolution/composer.lock").write_bytes(b"changed")
    monkeypatch.setattr(sys, "argv", [*argv, "--resolve-unlocked"])
    assert prepare.main() == 2 and len(fixture.calls) == 1
    assert (target / "preparation.json").read_bytes() == original
    assert not (fixture.repository / "composer.lock").exists()


@pytest.mark.parametrize("mutation", [None, "composer.json", "composer.lock"])
def test_cli_existing_locks_stay_immutable_even_with_unlocked_opt_in(tmp_path, monkeypatch, mutation):
    real_popen = subprocess.Popen
    fixture = preparation_cli_fixture(tmp_path, monkeypatch, locked=True, mutation=mutation)
    monkeypatch.setattr(prepare.subprocess, "Popen",
                        lambda argv, *args, **kwargs: fixture.resolver(argv, *args, **kwargs)
                        if argv[0] == "docker" else real_popen(argv, *args, **kwargs))
    monkeypatch.setattr(sys, "argv", [
        "prepare", "--config", "fixture", "--legacy-root", str(fixture.legacy),
        "--out-dir", str(fixture.output), "--task-ids", str(fixture.ids), "--workers", "1", "--resolve-unlocked"])
    assert prepare.main() == (2 if mutation else 0)
    assert len(fixture.calls) == 1
    assert (fixture.repository / "composer.lock").read_bytes() == LOCK
    target = fixture.output / component(fixture.task["id"])
    record = json.loads((target / "preparation.json").read_text())
    if mutation:
        assert record["status"] == "pending" and "changed the frozen" in record["error"]
        assert not (target / "payload").exists()
    else:
        assert record["lock_origin"] == "tracked-start-lock" and record["lock_sha256"] == digest(LOCK)
        assert not (target / "payload/resolution.tar").exists()


@pytest.mark.parametrize("name", CASES)
def test_migration_keeps_fixed_selectors_wrappers_coverage_and_current_candidate_inputs(tmp_path, name):
    repository, task, config, plan, target, payload, record = prepared_fixture(tmp_path, name)
    legacy, scratch = tmp_path / "legacy", tmp_path / "scratch"
    directory = legacy / "scripted/task"
    directory.mkdir(parents=True)
    scratch.mkdir()
    (legacy / "scripted/oracle_config.json").write_text(json.dumps({"tasks": {str(CASES[name][2]): config}}))
    for filename in config["session_files"]:
        (directory / filename).write_text("<?php require '/work/vendor/autoload.php'; // fixed assertion\n")
    completed = "Tests: 1, Assertions: 2" if name == "contao" else "OK (1 test, 2 assertions)"
    log = legacy / "scripted/completed.log"
    log.write_text(completed + "\n")
    oracle = {"index": CASES[name][2], "runs": {
        mode: [{"exit_code": 0, "log": "completed.log"} for _ in range(3)] for mode in ("start", "golden")}}
    bundles = []

    def bundle(root, names=None, *, role="dependency"):
        bundles.append((root, names, role))
        return {"path": "/fixture/tree.json", "sha256": "b" * 64}

    ctx = SimpleNamespace(
        legacy=legacy, scratch=scratch, repo=lambda task: repository,
        config={"migration": {"scripted_dependency_root": str(target.parent)}},
        image=lambda name: (IMAGE, {"path": "/fixture/image.json", "sha256": "c" * 64}),
        task_directory=lambda *args: directory, bundle=bundle, modules=lambda task: [],
        immutable_tests=lambda task: ["tests/fixed.php"],
        golden=lambda *args: {"path": "/fixture/golden.patch", "sha256": "d" * 64})
    before = {name: path.read_bytes() for name, path in tree_files(repository).items()}
    recipe = lane.migrate(ctx, task, oracle)
    expected = [task["id"] + "::" + config["test_selectors"][0]]
    assert recipe.grading["commands"][0]["expected_tests"] == expected
    assert recipe.grading["immutable_paths"] == ["tests/fixed.php"]
    raw, env, cwd = lane.container_payload(config["test_host_command"], config["environment"]["docker"])
    command = shlex.split(recipe.grading["commands"][0]["command"])
    assert command[-3:-1] == ["bash", "-c"]
    assert command[-1] == raw.replace("/oracle-tools", lane.TOOLS)
    assert "XDEBUG_MODE=coverage" in command
    assert "ORACLE_TARGET=/workspace/" + CASES[name][5] in command
    assert "ORACLE_COVERAGE_JSON=" + lane.RUNTIME + "/artifacts/coverage-candidate-1.json" in command
    assert recipe.workdir == "/workspace" and recipe.env["COMPOSER_DISABLE_NETWORK"] == "1"
    assert recipe.env.get("COMPOSER_ROOT_VERSION") == CASES[name][4]
    assert recipe.grading["bootstrap"][-1] == recipe.dev_checks[-1] == recipe.bootstrap[-1]
    assert "composer install " in recipe.bootstrap[-1] and "composer update" not in recipe.bootstrap[-1]
    assert "composer.lock" not in recipe.scratch_paths
    assert ("core-bundle/vendor" in recipe.scratch_paths) == (name == "contao")
    assert (payload, None, "dependency") in bundles
    assert {name: path.read_bytes() for name, path in tree_files(repository).items()} == before
    assert re.search(recipe.grading["commands"][0]["success_pattern"], completed)
    assert not re.search(recipe.grading["commands"][0]["success_pattern"], "OK (0 tests,")
    if name == "contao":
        for scratch_paths in (["vendor/library"], ["vendor/library", "core-bundle/vendor", "core-bundle"]):
            (target / "preparation.json").write_text(json.dumps({**record, "scratch_paths": scratch_paths}))
            with pytest.raises(ContractError, match="scratch"):
                lane.migrate(ctx, task, oracle)
