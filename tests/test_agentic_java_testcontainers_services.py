from dataclasses import asdict, replace
import hashlib
import json

import pytest

from agentic_runtime_migration import jvm_testcontainers_services as service
from reviewbench_maintainability.agentic.contracts import ContractError, RuntimeRecipe


def recipe(ident=None):
    ident = ident or next(iter(service.STARTS))
    return RuntimeRecipe(
        task_id=ident, repo="rajadilipkolli/mfscreener", start_sha=service.STARTS[ident], lane="jvm",
        image="sha256:" + "1" * 64, image_audit={"path": "/original/audit.json", "sha256": "2" * 64},
        cpus=2, memory_mb=8192, workdir="/workspace", build_scratch_mb=16384,
        bootstrap=["original dependency seed"], dev_checks=["original Gradle classes command"],
        cache_mounts={"gradle": "/opt/reviewbench-cache/gradle"}, env={"JAVA_TOOL_OPTIONS": "original options"},
        assets=[{"kind": "archive", "visibility": "developer", "path": "/original/gradle.json",
                 "sha256": "3" * 64, "target": "/opt/reviewbench-assets/gradle-downloads"}],
        grading={"golden_patch": {"path": "/original/reference.patch", "sha256": "4" * 64},
                 "commands": [{"command": "original immutable runner", "parser": "json",
                    "expected_tests": ["com.learning.mfscreener.web.controllers.PortfolioControllerIT#" + value
                                       for value in service.TESTS]}]},
    )


def asset(target, digest="5"):
    return {"path": "/transport/" + digest, "sha256": digest * 64, "kind": "file",
            "visibility": "developer", "target": target}


def make(original, port=39742, preload=None, contract=None):
    return service.with_transport(original, preload or asset(service.TARGET), port,
                                  contract or asset(service.CONTRACT_TARGET, "6"))


@pytest.mark.parametrize("ident", list(service.STARTS))
def test_each_original_start_reference_and_nine_tests_remain_unchanged(ident):
    old = recipe(ident)
    before = asdict(old)
    new = make(old)
    assert asdict(old) == before
    assert new.grading == old.grading
    assert new.dev_checks == old.dev_checks
    assert new.bootstrap[:-1] == old.bootstrap
    assert new.assets[:-2] == old.assets
    assert new.cache_mounts == old.cache_mounts
    assert new.scratch_paths == old.scratch_paths
    assert new.submodules == old.submodules
    assert new.cpus == old.cpus and new.memory_mb == old.memory_mb
    assert new.sidecars == [{"name": "network-anchor", "image": old.image, "command": ["sleep", "7200"]}]
    assert new.env["BASH_ENV"] == service.TARGET
    assert new.env["JAVA_TOOL_OPTIONS"] == old.env["JAVA_TOOL_OPTIONS"]
    assert "TESTCONTAINERS_RYUK_DISABLED" not in new.env
    assert "TESTCONTAINERS_RYUK_PRIVILEGED" not in new.env
    assert "TESTCONTAINERS_CHECKS_DISABLE" not in new.env


@pytest.mark.parametrize("key,value", [
    ("task_id", "another"), ("repo", "another/repo"), ("start_sha", "7" * 40),
    ("lane", "tsjs"), ("workdir", "/workspace/other"), ("cpus", 4),
    ("memory_mb", 4096), ("sidecars", [{"name": "other", "image": "sha256:" + "8" * 64}]),
])
def test_changed_original_inputs_are_rejected(key, value):
    with pytest.raises(ContractError):
        make(replace(recipe(), **{key: value}))


@pytest.mark.parametrize("key", [
    "BASH_ENV", "DOCKER_HOST", "TESTCONTAINERS_RYUK_DISABLED", "TESTCONTAINERS_RYUK_PRIVILEGED",
    "TESTCONTAINERS_CHECKS_DISABLE", "DOCKER_TLS_VERIFY", "REVIEWBENCH_DOCKER_PROFILE",
])
def test_existing_library_or_transport_overrides_are_rejected(key):
    original = recipe()
    original.env[key] = "changed"
    with pytest.raises(ContractError):
        make(original)


@pytest.mark.parametrize("port", [True, 0, 1023, 65536, "39742"])
def test_transport_port_is_typed_and_bounded(port):
    with pytest.raises(ContractError):
        make(recipe(), port)


@pytest.mark.parametrize("change", ["missing", "duplicated", "different", "second-command", "parser"])
def test_exact_original_test_identities_and_parser_are_required(change):
    original = recipe()
    command = original.grading["commands"][0]
    if change == "missing":
        command["expected_tests"].pop()
    elif change == "duplicated":
        command["expected_tests"][-1] = command["expected_tests"][0]
    elif change == "different":
        command["expected_tests"][-1] = "different"
    elif change == "second-command":
        original.grading["commands"].append(dict(command))
    else:
        command["parser"] = "regex"
    with pytest.raises(ContractError):
        make(original)


@pytest.mark.parametrize("target", [service.TARGET, service.CONTRACT_TARGET])
@pytest.mark.parametrize("key,value", [
    ("kind", "archive"), ("visibility", "grader"), ("target", "/workspace/mutable"),
])
def test_controller_and_preload_assets_are_immutable_developer_inputs(target, key, value):
    changed = {**asset(target), key: value}
    with pytest.raises(ContractError):
        if target == service.TARGET:
            make(recipe(), preload=changed)
        else:
            make(recipe(), contract=changed)


def test_actual_controller_changes_are_visible_in_the_runtime_fingerprint():
    first = make(recipe(), contract=asset(service.CONTRACT_TARGET, "6"))
    second = make(recipe(), contract=asset(service.CONTRACT_TARGET, "7"))
    assert first.fingerprint != second.fingerprint
    assert first.grading == second.grading


def test_input_file_bytes_and_complete_pointer_are_required(tmp_path):
    path = tmp_path / "input.json"
    path.write_text(json.dumps({"original": True}))
    pointer = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    assert service.referenced(pointer) == {"original": True}
    path.write_text(json.dumps({"original": False}))
    with pytest.raises(ContractError):
        service.referenced(pointer)
    with pytest.raises(ContractError):
        service.referenced({"path": str(path)})


def test_https_changes_only_bound_transport_configuration():
    old = recipe()
    new = service.with_transport(old, asset(service.TARGET), 39742,
                                 asset(service.CONTRACT_TARGET, "6"), https=True)
    assert new.env["REVIEWBENCH_ORIGINAL_HTTPS_PROXY"] == "1"
    assert new.env["REVIEWBENCH_BASE_JAVA_TOOL_OPTIONS"] == old.env["JAVA_TOOL_OPTIONS"]
    assert new.env["JAVA_TOOL_OPTIONS"] == old.env["JAVA_TOOL_OPTIONS"]
    assert new.grading == old.grading and new.dev_checks == old.dev_checks
    assert new.fingerprint != make(old).fingerprint
    assert "REVIEWBENCH_ORIGINAL_HTTPS_PROXY" not in old.env


@pytest.mark.parametrize("value", [1, "true", None])
def test_https_selection_does_not_accept_untyped_flags(value):
    with pytest.raises(ContractError):
        service.with_transport(recipe(), asset(service.TARGET), 39742,
                               asset(service.CONTRACT_TARGET, "6"), https=value)
