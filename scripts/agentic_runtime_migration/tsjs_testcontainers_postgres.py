"""Original Hiero tests using actual lease-isolated Docker/PostgreSQL/Ryuk."""
from dataclasses import asdict
import json
from pathlib import Path
import shlex

from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, atomic_json, canonical_hash, file_hash,
)
from reviewbench_maintainability.agentic.snapshots import git

TASK = "github__hiero-ledger__hiero-mirror-node__pull_6034__comment_1207163967__duplication"
PROFILE = "hiero42-original-testcontainers-v1"
OPTION = "tsjs_docker_lifecycle_inputs"
TARGET = "/opt/reviewbench-assets/docker-transport.cjs"
CONTRACT_TARGET = "/opt/reviewbench-assets/docker-lifecycle-contract.json"
SETUP = "hedera-mirror-rest/__tests__/globalSetup.js"
SETUP_SHA256 = "dc71d4e09ad5695e44ce4e205be8852cf75735b76324a4b4294cf1a2d3e32866"
ORIGINAL_COMMAND = (
    "node --experimental-vm-modules node_modules/jest/bin/jest.js "
    "__tests__/accounts.test.js --runInBand --coverage=false"
)
PING = """const http=require('http');
const url=process.env.DOCKER_HOST.replace('tcp:','http:')+'/_ping';
const r=http.get(url,res=>{let body='';res.on('data',x=>body+=x);
res.on('end',()=>{if(res.statusCode!==200||body.trim()!=='OK')process.exit(1);
console.log('ORIGINAL_DOCKER_TRANSPORT_READY');});});
r.setTimeout(10000,()=>r.destroy(new Error('broker readiness timeout')));
r.on('error',e=>{console.error(e.message);process.exit(1)});"""


def require(value, message):
    if not value:
        raise ContractError("original Testcontainers transport: " + message)


def referenced(pointer, *, document=True):
    require(isinstance(pointer, dict) and set(pointer) == {"path", "sha256"},
            "input requires an explicit path and digest")
    path = Path(pointer["path"])
    require(path.is_absolute() and path.is_file() and not path.is_symlink()
            and path.stat().st_size <= 16 * 1024**2
            and file_hash(path) == pointer["sha256"], "input path/hash differs")
    raw = path.read_bytes()
    return json.loads(raw) if document else raw


def with_transport(original: RuntimeRecipe, preload: dict, port: int,
                   contract: dict | None = None) -> RuntimeRecipe:
    """Construct a provisional runtime; actual readiness still needs controls."""
    require(original.task_id == TASK and original.repo == "hiero-ledger/hiero-mirror-node"
            and original.start_sha == "ea1e57ca66914d56c434bc6b7bfdc4f13cc3e647",
            "task or original source identity differs")
    require(original.workdir == "/workspace/hedera-mirror-rest"
            and not original.sidecars and original.cpus == 2 and original.memory_mb == 2048
            and original.grading["commands"][0]["command"] == ORIGINAL_COMMAND
            and len(original.grading["commands"]) == 1,
            "original command/resource/environment binding differs")
    require(type(port) is int and 1024 <= port <= 65535, "broker port is invalid")
    require(preload.get("kind", "file") == "file" and preload.get("target") == TARGET
            and preload.get("visibility") == "developer", "preload asset role/path differs")
    require(not any(k in original.env for k in (
        "DOCKER_HOST", "TESTCONTAINERS_HOST_OVERRIDE", "TESTCONTAINERS_RYUK_DISABLED",
        "TESTCONTAINERS_RYUK_PRIVILEGED", "REVIEWBENCH_DOCKER_PROFILE",
        "REVIEWBENCH_DOCKER_BROKER_PORT")), "original runtime already selects a Docker transport")
    require(isinstance(contract, dict) and contract.get("kind") == "file"
            and contract.get("target") == CONTRACT_TARGET
            and contract.get("visibility") == "developer",
            "live broker code/config need an immutable runtime contract")
    data = asdict(original)
    data["assets"].extend([preload, contract])
    data["sidecars"] = [{"name": "network-anchor", "image": original.image,
                         "command": ["sleep", "7200"]}]
    data["env"].update({
        "REVIEWBENCH_DOCKER_PROFILE": PROFILE,
        "REVIEWBENCH_DOCKER_BROKER_PORT": str(port),
        "NODE_OPTIONS": (original.env.get("NODE_OPTIONS", "") + " --require=" + TARGET).strip(),
    })
    data["bootstrap"].append("node -e " + shlex.quote(PING))
    result = RuntimeRecipe.from_dict(data)
    require(result.grading == original.grading and result.dev_checks == original.dev_checks
            and result.assets[:-2] == original.assets
            and result.bootstrap[:-1] == original.bootstrap
            and result.cache_mounts == original.cache_mounts
            and result.scratch_paths == original.scratch_paths
            and result.submodules == original.submodules,
            "transport construction changed candidate/test/dependency behavior")
    return result


def migrate(ctx, task, oracle, pointer):
    binding = referenced(pointer)
    require(set(binding) == {"schema_version", "inputs", "broker", "controls", "execution_contract"}
            and binding["schema_version"] == "reviewbench-hiero42-docker-lifecycle-binding/v1",
            "unbound service configuration")
    original_inputs = referenced(binding["inputs"])
    require(original_inputs["schema_version"] == "reviewbench-original-hiero42-inputs/v1"
            and original_inputs["status"] == "original_inputs_reopened_without_reinstall"
            and original_inputs["full_scope"] == 402
            and original_inputs["new_installs_builds_tests_model_judge_calls"] == 0
            and original_inputs["node_version"] == "v18.20.8"
            and original_inputs["testcontainers_version"] == "9.8.0"
            and original_inputs["task_fingerprint"] == canonical_hash(task)
            and original_inputs["oracle_fingerprint"] == canonical_hash(oracle)
            and referenced(original_inputs["task"]) == task
            and referenced(original_inputs["oracle"]) == oracle,
            "original Node/testcontainers/task/test inputs differ")
    case = referenced(original_inputs["case"])
    require(case["node"] == "18.20.8" and case["reinstall_golden"] is False
            and oracle["pass_to_pass"] == 118
            and shlex.join(case["test"]) == ORIGINAL_COMMAND,
            "original installed graph or test command differs")
    original = RuntimeRecipe.from_dict(referenced(original_inputs["original_runtime"]))
    import hashlib
    require(hashlib.sha256(git(ctx.repo(task), "show", task["start_sha"] + ":" + SETUP)).hexdigest()
            == SETUP_SHA256, "original PostgreSQL global setup changed")
    settings = referenced(binding["broker"])
    execution_contract = referenced(binding["execution_contract"])
    require(execution_contract["schema_version"] == "reviewbench-real-docker-controller-contract/v1"
            and execution_contract["profile"] == PROFILE
            and execution_contract["broker_config_sha256"] == binding["broker"]["sha256"]
            and execution_contract["preload_sha256"] == settings["preload_sha256"],
            "immutable execution contract differs from broker configuration")
    require(settings["profile"] == PROFILE and settings["developer_image"] == original.image
            and settings["preload_target"] == TARGET and settings["max_parents"] == 2
            and set(settings["images"]) == {"postgres:14-alpine", "testcontainers/ryuk:0.4.0"},
            "broker service/parent profile changed")
    require(settings["images"]["postgres:14-alpine"]["role"] == "postgres"
            and settings["images"]["postgres:14-alpine"]["port"] == 5432
            and settings["images"]["testcontainers/ryuk:0.4.0"]["role"] == "ryuk"
            and settings["images"]["testcontainers/ryuk:0.4.0"]["port"] == 8080,
            "original service ports or lifecycle changed")
    controls = referenced(binding["controls"])
    require(controls["status"] == "two_independent_real_docker_lifecycles_verified"
            and controls["broker_config_sha256"] == binding["broker"]["sha256"]
            and controls["broker_executable_sha256"] == execution_contract["broker_executable_sha256"]
            and controls["new_project_build_test_model_judge_calls"] == 0
            and controls["unrelated_containers_unchanged"] is True
            and controls["real_ryuk_enabled"] is True
            and controls["raw_host_socket_exposed"] is False
            and controls["owned_services_remaining"] == 0,
            "real Docker lifecycle/isolation/cleanup controls are incomplete")
    tool = Path(__file__).with_name("docker_transport.cjs")
    require(file_hash(tool) == settings["preload_sha256"], "broker and runtime preload bytes differ")
    asset = {**ctx.copy_asset(tool, "developer"), "kind": "file",
             "visibility": "developer", "target": TARGET}
    contract_asset = {**ctx.copy_asset(Path(binding["execution_contract"]["path"]), "developer"),
                      "kind": "file", "visibility": "developer", "target": CONTRACT_TARGET}
    recipe = with_transport(original, asset, int(settings["listen"].rsplit(":", 1)[1]), contract_asset)
    require(recipe.grading["golden_patch"] == original_inputs["original_reference"],
            "original reference differs")
    atomic_json(ctx.output / "original-docker-lifecycle.json", {
        "id": TASK, "full_scope": 402, "binding": pointer,
        "runtime_fingerprint": recipe.fingerprint,
        "source_tests_reference_dependency_assets_unchanged": True,
        "original_testcontainers_and_ryuk_executed": True,
        "service_backend": "real Docker containers on this task's internal network",
        "port_transport": "actual parent-only gateway TCP listeners; only inspect NetworkSettings.Ports is augmented",
        "historical_service_image_digest_attested": False,
        "service_image_identity": "original declared image tags resolved to recorded current immutable local digests",
        "host_socket": "broker only; original Ryuk receives a task-scoped proxy socket directory",
        "resource_accounting": "existing four-CPU/four-GiB two-container reservation; child caps bound the unused anchor headroom",
        "new_project_build_test_model_judge_calls_by_migration": 0,
    })
    return recipe
