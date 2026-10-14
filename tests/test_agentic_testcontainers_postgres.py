from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from agentic_runtime_migration import tsjs_testcontainers_postgres as transport
from reviewbench_maintainability.agentic.contracts import ContractError, RuntimeRecipe


def recipe():
    return RuntimeRecipe(
        task_id=transport.TASK, repo="hiero-ledger/hiero-mirror-node",
        start_sha="ea1e57ca66914d56c434bc6b7bfdc4f13cc3e647", lane="tsjs",
        image="sha256:" + "1" * 64,
        image_audit={"path": "/fixture/audit.json", "sha256": "2" * 64},
        dev_checks=["node --input-type=module --check < /workspace/hedera-mirror-rest/accounts.js"],
        bootstrap=["original fixed dependency installation copy"],
        cpus=2, memory_mb=2048, workdir="/workspace/hedera-mirror-rest",
        assets=[{"path": "/fixture/dependencies.json", "sha256": "3" * 64,
                 "target": "/opt/reviewbench-assets/node_modules", "visibility": "developer",
                 "kind": "archive"}],
        cache_mounts={"node-dependencies": "/opt/reviewbench-cache/node-dependencies"},
        scratch_paths=["hedera-mirror-rest/node_modules"],
        env={"PATH": "/original/node/bin", "CI": "true"},
        grading={"immutable_paths": ["hedera-mirror-rest/__tests__/accounts.test.js"],
                 "golden_patch": {"path": "/fixture/original.patch", "sha256": "4" * 64},
                 "commands": [{"command": transport.ORIGINAL_COMMAND,
                               "expected_tests": ["original118"], "parser": "regex",
                               "success_pattern": "118 passed"}]},
    )


def preload():
    return {"path": "/fixture/transport.cjs", "sha256": "5" * 64,
            "target": transport.TARGET, "visibility": "developer", "kind": "file"}


def contract():
    return {"path": "/fixture/controller-contract.json", "sha256": "6" * 64,
            "target": transport.CONTRACT_TARGET, "visibility": "developer", "kind": "file"}


def test_transport_preserves_every_original_assertion_and_asset():
    old = recipe()
    before = asdict(old)
    new = transport.with_transport(old, preload(), 39741, contract())
    assert asdict(old) == before
    assert new.grading == old.grading
    assert new.dev_checks == old.dev_checks
    assert new.assets[:-2] == old.assets
    assert new.bootstrap[:-1] == old.bootstrap
    assert new.cache_mounts == old.cache_mounts
    assert new.scratch_paths == old.scratch_paths
    assert new.env["PATH"] == old.env["PATH"]
    assert new.sidecars == [{"name": "network-anchor", "image": old.image, "command": ["sleep", "7200"]}]
    assert new.env["NODE_OPTIONS"] == "--require=" + transport.TARGET
    assert "TESTCONTAINERS_RYUK_DISABLED" not in new.env
    assert "TESTCONTAINERS_RYUK_PRIVILEGED" not in new.env


@pytest.mark.parametrize("field,value", [
    ("task_id", "another"), ("repo", "another/repo"), ("start_sha", "6" * 40),
    ("workdir", "/workspace/other"), ("cpus", 8), ("memory_mb", 8192),
    ("sidecars", [{"name": "foreign", "image": "sha256:" + "7" * 64}]),
])
def test_transport_rejects_changed_original_fields(field, value):
    old = replace(recipe(), **{field: value})
    with pytest.raises(ContractError):
        transport.with_transport(old, preload(), 39741)


@pytest.mark.parametrize("port", [0, 1023, 65536, True, "39741"])
def test_transport_requires_a_valid_typed_port(port):
    with pytest.raises(ContractError):
        transport.with_transport(recipe(), preload(), port)


@pytest.mark.parametrize("field", [
    "DOCKER_HOST", "TESTCONTAINERS_HOST_OVERRIDE", "TESTCONTAINERS_RYUK_DISABLED",
    "TESTCONTAINERS_RYUK_PRIVILEGED", "REVIEWBENCH_DOCKER_PROFILE", "REVIEWBENCH_DOCKER_BROKER_PORT",
])
def test_transport_rejects_existing_service_override(field):
    old = recipe()
    old.env[field] = "unexpected"
    with pytest.raises(ContractError):
        transport.with_transport(old, preload(), 39741)


def test_transport_preserves_existing_node_options():
    old = recipe()
    old.env["NODE_OPTIONS"] = "--max-old-space-size=1536"
    new = transport.with_transport(old, preload(), 39741, contract())
    assert new.env["NODE_OPTIONS"] == "--max-old-space-size=1536 --require=" + transport.TARGET


def test_transport_rejects_a_different_test_or_multiple_test_commands():
    for change in ("replace", "append"):
        old = recipe()
        if change == "replace":
            old.grading["commands"][0]["command"] = "echo 118 passed"
        else:
            old.grading["commands"].append(dict(old.grading["commands"][0]))
        with pytest.raises(ContractError):
            transport.with_transport(old, preload(), 39741)


@pytest.mark.parametrize("field,value", [
    ("visibility", "grader"), ("kind", "archive"), ("target", "/workspace/preload.cjs"),
])
def test_preload_role_is_fixed(field, value):
    asset = {**preload(), field: value}
    with pytest.raises(ContractError):
        transport.with_transport(recipe(), asset, 39741)


def test_referenced_files_are_hash_bound_and_no_directory_fallback(tmp_path):
    path = tmp_path / "input.json"
    path.write_text('{"original":true}')
    pointer = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    assert transport.referenced(pointer) == {"original": True}
    assert transport.referenced(pointer, document=False) == path.read_bytes()
    path.write_text('{"original":false}')
    with pytest.raises(ContractError):
        transport.referenced(pointer)
    linked = tmp_path / "linked.json"
    linked.symlink_to(path)
    with pytest.raises(ContractError):
        transport.referenced({"path": str(linked), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})


@pytest.mark.parametrize("asset", [None, {}, {"kind": "archive"},
    {"kind": "file", "target": "/workspace/controller.json", "visibility": "developer"},
    {"kind": "file", "target": transport.CONTRACT_TARGET, "visibility": "grader"}])
def test_live_controller_must_be_bound_into_the_runtime(asset):
    with pytest.raises(ContractError):
        transport.with_transport(recipe(), preload(), 39741, asset)


def test_changing_controller_code_changes_runtime_fingerprint():
    first = transport.with_transport(recipe(), preload(), 39741, contract())
    second = transport.with_transport(recipe(), preload(), 39741, {**contract(), "sha256": "7" * 64})
    assert first.fingerprint != second.fingerprint
    assert first.grading == second.grading


def test_configuration_only_preload_uses_exact_bridge_and_keeps_ryuk():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for configuration-only transport unit test")
    path = Path(transport.__file__).with_name("docker_transport.cjs")
    code = r"""
const assert = require('assert');
const {gatewayFor, configure} = require(process.argv[1]);
const nic = {lo:[{internal:true,family:'IPv4',address:'127.0.0.1',netmask:'255.0.0.0'}],
             eth0:[{internal:false,family:'IPv4',address:'172.29.0.2',netmask:'255.255.0.0'}]};
assert.equal(gatewayFor(nic), '172.29.0.1');
const env = {REVIEWBENCH_DOCKER_PROFILE:'hiero42-original-testcontainers-v1',
             REVIEWBENCH_DOCKER_BROKER_PORT:'39741', KEEP_ORIGINAL:'yes'};
configure(env,nic);
assert.equal(env.DOCKER_HOST,'tcp://172.29.0.1:39741');
assert.equal(env.TESTCONTAINERS_HOST_OVERRIDE,'172.29.0.1');
assert.equal(env.TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE,'/var/run/docker.sock');
assert.equal(env.KEEP_ORIGINAL,'yes');
assert.equal(env.TESTCONTAINERS_RYUK_DISABLED,undefined);
assert.throws(()=>configure({...env,TESTCONTAINERS_RYUK_DISABLED:'true'},nic));
assert.throws(()=>configure({...env,TESTCONTAINERS_RYUK_PRIVILEGED:'true'},nic));
assert.throws(()=>configure({...env,REVIEWBENCH_DOCKER_BROKER_PORT:'80'},nic));
assert.throws(()=>configure({...env,REVIEWBENCH_DOCKER_PROFILE:'foreign'},nic));
assert.throws(()=>gatewayFor({}));
assert.throws(()=>gatewayFor({...nic,eth1:[{internal:false,family:'IPv4',address:'172.30.0.2',netmask:'255.255.0.0'}]}));
console.log('DOCKER_TRANSPORT_CONFIGURATION_ASSERTIONS_PASSED');
"""
    result = subprocess.run([node, "-e", code, str(path)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "DOCKER_TRANSPORT_CONFIGURATION_ASSERTIONS_PASSED\n"
