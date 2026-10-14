"""Unchanged Mfscreener tests with real task-isolated Docker services."""
from dataclasses import asdict
import json
from pathlib import Path
import shlex

from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, atomic_json, canonical_hash, file_hash,
)

PROFILE = "mfscreener133-135-original-testcontainers-v1"
CLIENT = "mfscreener-testcontainers-java-1.19.7"
OPTION = "jvm_docker_lifecycle_inputs"
HTTPS_HOSTS = ["api.mfapi.in", "portal.amfiindia.com", "www.amfiindia.com"]
TARGET = "/opt/reviewbench-assets/docker-transport-java.env.sh"
CONTRACT_TARGET = "/opt/reviewbench-assets/docker-lifecycle-contract.json"
PREFIX = "github__rajadilipkolli__mfscreener__pull_333__comment_"
STARTS = {
    PREFIX + "1564727091__complexity": "a4498ce82af1b5529ffcefeab92cdd5eb902a664",
    PREFIX + "1564787473__complexity": "ddee8b2509e4604924d8d2e0220c7ac9967e24c9",
    PREFIX + "1564796379__complexity": "f8c12cd5a00daa4e6b5a52c005080bcda50388f2",
}
TESTS = [
    "getPortfolio()", "getPortfolioForAfterDate()", "getPortfolioWithOutDate()", "uploadFile()",
    "uploadFileWithNewFolio()", "uploadFileWithNewFolioAndSchemeAndTransaction()",
    "uploadFileWithNewScheme()", "uploadFileWithNewTransaction()", "uploadFileWithNoChanges()",
]
PING = """import os, urllib.request
endpoint=os.environ['DOCKER_HOST'].replace('tcp://','http://')
with urllib.request.urlopen(endpoint+'/_ping',timeout=10) as response:
 assert response.status==200 and response.read().strip()==b'OK'
print('ORIGINAL_JAVA_DOCKER_TRANSPORT_READY')"""


def require(value, message):
    if not value:
        raise ContractError("original Java Docker services: " + message)


def referenced(pointer):
    require(isinstance(pointer, dict) and set(pointer) == {"path", "sha256"},
            "complete file binding required")
    path = Path(pointer["path"])
    require(path.is_absolute() and path.is_file() and not path.is_symlink()
            and path.stat().st_size <= 16 * 1024**2 and file_hash(path) == pointer["sha256"],
            "retained input bytes/path changed")
    return json.loads(path.read_text())


def with_transport(original, preload, port, contract, *, https=False):
    require(original.task_id in STARTS and STARTS[original.task_id] == original.start_sha
            and original.repo == "rajadilipkolli/mfscreener" and original.lane == "jvm",
            "original task/start/repository differs")
    require(original.workdir == "/workspace" and original.cpus == 2 and original.memory_mb == 8192
            and not original.sidecars, "original resources or workspace differs")
    commands = original.grading["commands"]
    expected = {"com.learning.mfscreener.web.controllers.PortfolioControllerIT#" + name for name in TESTS}
    require(len(commands) == 1 and commands[0]["parser"] == "json"
            and len(commands[0]["expected_tests"]) == 9
            and set(commands[0]["expected_tests"]) == expected,
            "original nine test identities differ")
    require(type(port) is int and 1024 <= port <= 65535, "broker port is invalid")
    for asset, target in [(preload, TARGET), (contract, CONTRACT_TARGET)]:
        require(isinstance(asset, dict) and asset.get("kind") == "file"
                and asset.get("visibility") == "developer" and asset.get("target") == target,
                "immutable transport/controller asset role differs")
    require(not any(key in original.env for key in [
        "BASH_ENV", "DOCKER_HOST", "DOCKER_TLS_VERIFY", "TESTCONTAINERS_HOST_OVERRIDE",
        "TESTCONTAINERS_RYUK_DISABLED", "TESTCONTAINERS_RYUK_PRIVILEGED", "TESTCONTAINERS_CHECKS_DISABLE",
        "REVIEWBENCH_DOCKER_PROFILE", "REVIEWBENCH_DOCKER_BROKER_PORT",
        "REVIEWBENCH_ORIGINAL_HTTPS_PROXY", "REVIEWBENCH_BASE_JAVA_TOOL_OPTIONS",
    ]), "original environment already overrides transport or library checks")
    value = asdict(original)
    value["assets"].extend([preload, contract])
    value["sidecars"] = [{"name": "network-anchor", "image": original.image,
                          "command": ["sleep", "7200"]}]
    value["env"].update({"BASH_ENV": TARGET, "REVIEWBENCH_DOCKER_PROFILE": PROFILE,
                         "REVIEWBENCH_DOCKER_BROKER_PORT": str(port)})
    require(type(https) is bool, "HTTPS transport selection must be boolean")
    if https:
        value["env"].update({
            "REVIEWBENCH_ORIGINAL_HTTPS_PROXY": "1",
            "REVIEWBENCH_BASE_JAVA_TOOL_OPTIONS": original.env.get("JAVA_TOOL_OPTIONS", ""),
        })
    value["bootstrap"].append("python3 -c " + shlex.quote(PING))
    result = RuntimeRecipe.from_dict(value)
    require(result.grading == original.grading and result.dev_checks == original.dev_checks
            and result.bootstrap[:-1] == original.bootstrap and result.assets[:-2] == original.assets
            and result.submodules == original.submodules and result.scratch_paths == original.scratch_paths
            and result.cache_mounts == original.cache_mounts,
            "transport construction changed original commands/tests/assets")
    return result


def migrate(ctx, task, oracle, pointer):
    binding = referenced(pointer)
    require(binding.get("schema_version") == "reviewbench-original-java-service-binding/v1"
            and binding.get("full_scope") == 402
            and binding.get("task_fingerprint") == canonical_hash(task)
            and binding.get("oracle_fingerprint") == canonical_hash(oracle),
            "task/Oracle/full scope binding differs")
    original = RuntimeRecipe.from_dict(referenced(binding["original_runtime"]))
    require((original.task_id, original.repo, original.start_sha) ==
            (task["id"], task["repo"], task["start_sha"]), "original runtime identity differs")
    source = referenced(binding["original_inputs"])
    records = [row for row in source["records"] if row["id"] == task["id"]]
    require(source["full_scope"] == 402 and len(records) == 1, "original source record missing")
    record = records[0]
    require(record["start_sha"] == task["start_sha"] and oracle["pass_to_pass"] == 9
            and record["original_command"] == oracle["command"]
            and record["original_test_ids"] == oracle["pass_to_pass_tests"]
            and record["original_reference"]["sha256"] == oracle["source_only_patch"]["sha256"]
            == original.grading["golden_patch"]["sha256"],
            "original command/test/reference differs")
    import hashlib
    from reviewbench_maintainability.agentic.snapshots import git
    for name, digest in record["source_files"].items():
        require(hashlib.sha256(git(ctx.repo(task), "show", task["start_sha"] + ":" + name)).hexdigest() == digest,
                "original source declaration changed")
    broker, contract, controls = [referenced(binding[name]) for name in
                                  ("broker", "execution_contract", "controls")]
    require(broker["client"] == CLIENT and broker["profile"] == PROFILE
            and broker["developer_image"] == original.image and broker["max_parents"] == 2
            and broker["preload_target"] == TARGET and broker["contract_target"] == CONTRACT_TARGET,
            "broker profile/developer/transport binding differs")
    roles = {"postgres:16-alpine": ("postgres", 5432), "redis:7.2.4-alpine": ("redis", 6379),
             "openzipkin/zipkin:latest": ("zipkin", 9411), "testcontainers/ryuk:0.6.0": ("ryuk", 8080)}
    require(set(broker["images"]) == set(roles), "original service image tags differ")
    archives = referenced(binding["service_images"])
    require(archives["status"] == "four_original_service_images_archived_and_restoration_verified"
            and set(archives["images"]) == set(roles), "original service image archives missing")
    for tag, (role, port) in roles.items():
        image = broker["images"][tag]
        archived = referenced(archives["images"][tag])
        require((image["role"], image["port"]) == (role, port)
                and archived["image"] == image["id"] and archived["declared_tag"] == tag
                and archived["config_and_all_layer_hashes_verified"] is True
                and archived["gzip_crc_verified"] is True,
                "actual original-tag image body differs")
    require(contract["schema_version"] == "reviewbench-real-docker-controller-contract/v1"
            and contract["profile"] == PROFILE
            and contract["broker_config_sha256"] == binding["broker"]["sha256"]
            and contract["preload_sha256"] == broker["preload_sha256"],
            "live broker execution contract differs")
    require(controls["status"] == "two_original_Java_service_lifecycles_verified"
            and controls["errors"] == [] and controls["java_exit_statuses"] == [0, 0]
            and controls["broker_config_sha256"] == binding["broker"]["sha256"]
            and controls["broker_executable_sha256"] == contract["broker_executable_sha256"]
            and controls["original_testcontainers"] == "1.19.7"
            and controls["original_ryuk_privileged_request"] is True
            and controls["actual_ryuk_privileged"] is False
            and controls["real_ryuk_cleanup_verified"] is True
            and controls["unrelated_containers_unchanged"] is True
            and controls["raw_host_socket_exposed"] is False
            and controls["owned_services_remaining"] == 0
            and controls["original_project_tests_model_judge_calls"] == 0,
            "two real original Java lifecycles/isolation/cleanup are not verified")
    preload = Path(__file__).with_name("docker_transport_java.env.sh")
    require(file_hash(preload) == broker["preload_sha256"], "immutable preload differs from live broker")
    asset = {**ctx.copy_asset(preload, "developer"), "kind": "file",
             "visibility": "developer", "target": TARGET}
    contract_asset = {**ctx.copy_asset(Path(binding["execution_contract"]["path"]), "developer"),
                      "kind": "file", "visibility": "developer", "target": CONTRACT_TARGET}
    https = bool(broker.get("https_hosts"))
    if https:
        require(sorted(broker["https_hosts"]) == HTTPS_HOSTS, "original HTTPS host list differs")
        evidence = controls.get("original_https")
        require(isinstance(evidence, dict)
                and evidence.get("both_original_Spring_clients") is True
                and evidence.get("all_three_original_hosts") is True
                and evidence.get("tls_terminated") is False
                and evidence.get("responses_fabricated") is False
                and evidence.get("foreign_host_rejected") is True,
                "actual original HTTPS client/host/TLS controls are incomplete")
    recipe = with_transport(original, asset, int(broker["listen"].rsplit(":", 1)[1]),
                            contract_asset, https=https)
    atomic_json(ctx.output / ("original-java-services-" + task["id"] + ".json"), {
        "id": task["id"], "full_scope": 402, "binding": pointer, "runtime_fingerprint": recipe.fingerprint,
        "original_commands_tests_reference_dependency_assets_unchanged": True,
        "real_original_testcontainers_and_ryuk_verified": True,
        "original_reaper_privilege_declaration_retained": True,
        "actual_reaper_uses_scoped_proxy_without_host_privileges": True,
        "historical_service_image_digests_attested": False,
        "original_project_tests_model_judge_calls_by_migration": 0,
    })
    return recipe
