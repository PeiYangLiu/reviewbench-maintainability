"""The three released automatic Python Oracles, without synthetic session specs.

The automatic shards retained counts, not node IDs. Wave-001 later recovered
explicit selectors and retained all three discovery logs. Those *recovery*
artifacts are independently pinned below; they are not original six-run proof.
Only the frozen engine's subsequent start/source-only-golden replays can issue
runtime readiness. Migration neither discovers a new passing subset nor runs a
historical checkout instead of the complete current candidate.
"""
from __future__ import annotations

from dataclasses import dataclass
from email.parser import Parser
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile

from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, atomic_json, canonical_hash, file_hash, repo_path,
)
from reviewbench_maintainability.agentic.environment import resolve_asset
from reviewbench_maintainability.agentic.snapshots import validate_link
from .python_dependencies import dependency_tree_files

RECOVERY_SCHEMA = "reviewbench-python-automatic-recovery/v1"
DEPENDENCY_SCHEMA = "reviewbench-python-automatic-dependencies/v1"
ENTRY_SCHEMA = "reviewbench-python-automatic-entry/v1"
WAVE = "wave-001/correctness"
RECOVERED = WAVE + "/remote-artifacts-MSBJ04/gopy/_automatic-oracles"
REPLAY = WAVE + "/gopy_candidate_replay.py"
REPLAY_SHA256 = "fb7e1b0e2156f88883e1a81d3f8bb368e16dbbad77381f123997cc33f53b3728"
ASSET_ROOT = "/opt/reviewbench-assets/python-automatic"
SITE_TARGET = "/opt/reviewbench-assets/python-automatic-site"
REPORT_PATH = ".reviewbench-python-automatic/results.json"


def git(repository: Path, *args: str) -> bytes:
    """No worktree/index update, automatic maintenance, or promisor-cache fetch."""
    result = subprocess.run(
        ["git", "-c", f"safe.directory={repository.resolve()}", "-c", "gc.auto=0",
         "-c", "maintenance.auto=false", "-C", str(repository), *args],
        capture_output=True, timeout=300,
        env={**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_OPTIONAL_LOCKS": "0",
             "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode:
        raise ContractError("read-only automatic Python Git input missing: "
                            + result.stderr.decode(errors="replace")[-1200:])
    return result.stdout


@dataclass(frozen=True)
class AutomaticCase:
    repo: str
    start_sha: str
    directory: str
    recovered_slug: str
    project: str
    config_file: str
    venv: str
    distribution: str
    source_packages: tuple[str, ...]
    imports: tuple[str, ...]
    count: int
    golden_sha256: str
    automatic_sha256: str
    metadata_sha256: str
    log_sha256: str


CASES = {
    "github__airbytehq__airbyte__pull_30111__comment_1317338233__api_design": AutomaticCase(
        "airbytehq/airbyte", "f0dc3ff8ad25d1ba058f6fe61767f530761b76a8",
        "recovery/025-airbyte", "025-airbytehq-airbyte", "airbyte-cdk/python",
        "airbyte-cdk/python/pytest.ini", "airbyte-cdk/python/.venv_bench", "airbyte-cdk",
        ("airbyte_cdk",), ("pytest", "airbyte_protocol", "pydantic", "yaml", "requests_cache"),
        3, "f1544ab324420b686efcd027ca0b028e21e70b3a2f2b8302151d712048949811",
        "b1d46f3aa4d951f7cd0cf44d78e36b2c6fee09f303a1fa9a14cad95db00004e8",
        "fe12f51c76835662e829379b90915de8159bea06b6927cbf68b2ab685932f0a1",
        "3cbaf6a210059d9782b64070ded62369e479b4e0985fbbda47e1320b75599682",
    ),
    "github__canonical__operator__pull_843__comment_1091253290__readability": AutomaticCase(
        "canonical/operator", "ba3c9eeadf3ddac7de9ae8d00b0717d0120f40cf",
        "recovery/037-operator", "037-canonical-operator", ".", "pyproject.toml",
        ".venv_bench", "ops", ("ops",), ("pytest", "yaml", "websocket"),
        19, "97915083fe35b99c88c4070553547649eafc6c79ac92bc8760d04d5031a7c3cd",
        "ba95c23465a922ba7758cf97cb082a70ca0f611570a263f0c47c27f4cb13f25e",
        "127c0c132332c5e29668dd36592356d1125ea062a6d2dd7426a73568e62ff009",
        "579710f009924822e3f38800c71c6d0e8ed424a4a54e937d237918585a7db872",
    ),
    "github__dimiturtrz__mindscape__pull_2__comment_3505662522__modularity": AutomaticCase(
        "dimiturtrz/mindscape", "0c9287d402cf9531433d41f260fd425851137013",
        "recovery/047-mindscape", "047-dimiturtrz-mindscape", ".", "pyproject.toml",
        ".venv", "mindscape", ("baselines", "core", "neuroscan", "neuroviz"),
        ("pytest", "numpy", "scipy", "sklearn", "pyriemann", "pydantic", "braindecode"),
        18, "f5007aae39a72ca682c3f0e30e279e2c6671f8582aa019ca82ea30b3d2b16430",
        "01cffaced5552f40ba478b035462417f9b7787c33eee9f469767f17fa09542a6",
        "f7ce6f9785549e954b9c1659233efc652dc5a6b16fc0cceea32d6d9eca72aa6f",
        "bd96cdb5d5cf38622528b19cebc127975bf7aeeec505715f2fb9bc931aefaa42",
    ),
}


def pinned_bytes(path: Path, digest: str) -> bytes:
    if not path.is_file() or path.is_symlink():
        raise ContractError(f"retained automatic Python evidence missing: {path}")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != digest:
        raise ContractError(f"retained automatic Python evidence changed: {path}")
    return data


def node_groups(nodes: list[str]) -> list[list[str]]:
    if not nodes or len(nodes) != len(set(nodes)):
        raise ContractError("automatic Python selectors must be nonempty and unique")
    groups: dict[str, list[str]] = {}
    for node in nodes:
        if (not isinstance(node, str) or "::" not in node or any(c in node for c in "\n\r\0")
                or not node.split("::", 1)[1]):
            raise ContractError("automatic Python selector is not a pytest node ID")
        path = repo_path(node.split("::", 1)[0])
        if not path.endswith(".py"):
            raise ContractError("automatic Python selector is not a Python test")
        groups.setdefault(path, []).append(node)
    return list(groups.values())


def audit_discovery(text: str, metadata: dict) -> dict:
    """Verify identities/statuses in retained logs, not a summary/count marker."""
    header = re.compile(r"^=== run ([123]) target (.+?) rc=(-?\d+) ===\s*$", re.M)
    matches = list(header.finditer(text))
    targets = metadata["targets"]
    if (not targets or len(targets) != len(set(targets))
            or len(matches) != 3 * len(targets)):
        raise ContractError("automatic Python recovery lacks three complete discovery runs")
    observed = {}
    versions = set()
    selected = set(metadata["p2p"])
    for index, match in enumerate(matches):
        number, target, exit_code = int(match[1]), match[2], int(match[3])
        key = (number, target)
        if target not in targets or key in observed:
            raise ContractError("duplicate/foreign automatic Python discovery group")
        block = text[match.end():matches[index + 1].start() if index + 1 < len(matches) else len(text)]
        statuses: dict[str, list[str]] = {}
        # A selected node must have an actual verbose pytest status line in
        # every run. Summary lines such as '18 passed' never supply identities.
        for node, status in re.findall(
                r"^(\S+::\S+) (PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?:\s|$)", block, re.M):
            statuses.setdefault(node, []).append(status)
        for node in selected:
            if node.split("::", 1)[0] == target and statuses.get(node) != ["PASSED"]:
                raise ContractError("selected automatic Python node lacks three retained passes: " + node)
        versions.update(re.findall(r"^platform linux -- Python ([^,]+), pytest-([^,]+),", block, re.M))
        observed[key] = {"run": number, "target": target, "exit_code": exit_code,
                         "passed_nodes": sorted(n for n, states in statuses.items() if states == ["PASSED"])}
    for node in selected:
        if node.split("::", 1)[0] not in targets:
            raise ContractError("automatic Python selected node has no discovery target")
    if len(versions) != 1:
        raise ContractError("automatic Python discovery interpreter/pytest identity is unstable")
    python_version, pytest_version = versions.pop()
    return {"python_version": python_version, "pytest_version": pytest_version,
            "runs": [observed[k] for k in sorted(observed)]}


def frozen_test_inputs(ctx, task: dict, case: AutomaticCase, nodes: list[str]) -> dict:
    """Read public assertion/fixture bytes at start_sha; never check out target."""
    repository = ctx.repo(task)
    rows = git(repository, "ls-tree", "-r", "-z", task["start_sha"]).split(b"\0")
    tree = {}
    for row in rows:
        if row:
            info, name = row.split(b"\t", 1)
            tree[name.decode()] = info.split()[0].decode()
    roots = set()
    for group in node_groups(nodes):
        name = group[0].split("::", 1)[0]
        parts = Path(name).parts
        test_index = next((i for i, p in enumerate(parts[:-1])
                           if p in {"test", "tests", "unit_tests"}), None)
        if test_index is None:
            raise ContractError("automatic Python test needs an explicit public-test root")
        roots.add(Path(*parts[:test_index + 1]).as_posix())
        if name not in tree:
            raise ContractError("recovered Python selector absent at frozen start_sha: " + name)
    names = sorted(n for n in tree if any(n == r or n.startswith(r + "/") for r in roots)
                   or Path(n).name == "conftest.py" and (
                       Path(n).parent == Path(".") or any(Path(r).is_relative_to(Path(n).parent) for r in roots)))
    hashes, links = {}, {}
    for name in names:
        if tree[name] == "120000":
            target = git(repository, "show", task["start_sha"] + ":" + name).decode()
            validate_link(name, target)
            links[name] = target
            continue
        if tree[name] not in {"100644", "100755"}:
            raise ContractError("fixed Python test inputs must be regular files: " + name)
        hashes[name] = hashlib.sha256(git(repository, "show", task["start_sha"] + ":" + name)).hexdigest()
    config = git(repository, "show", task["start_sha"] + ":" + case.config_file)
    return {"test_roots": sorted(roots), "test_files": hashes, "test_symlinks": links, "config_file": case.config_file,
            "config_sha256": hashlib.sha256(config).hexdigest()}


def recover_contract(ctx, task: dict, oracle: dict) -> dict:
    case = CASES.get(task["id"])
    if case is None:
        raise ContractError("automatic Python task has no pinned retained selector recovery")
    source = oracle.get("source_only_golden", {})
    if (task.get("inferred_language", "").lower() != "python"
            or task["repo"] != case.repo or task["start_sha"] != case.start_sha
            or any(oracle.get(k) != task.get(k) for k in ("id", "repo", "start_sha", "target_sha"))
            or oracle.get("reviewed_path") != task["locality"]["path"]
            or oracle.get("artifacts", {}).get("directory") != case.directory
            or oracle.get("result_source") != "automatic_strict_validation"
            or not oracle.get("lane_results_source")
            or oracle.get("pass_to_pass") != case.count
            or oracle.get("start_runs") != 3 or oracle.get("golden_runs") != 3
            or any(oracle.get(k) is not True for k in (
                "release_eligible", "triple_run_stable", "golden_correctness_ok", "changed_lines_covered"))
            or source.get("patch_sha256") != case.golden_sha256
            or source.get("target_tests_included") is not False):
        raise ContractError("automatic Python task/released Oracle binding differs")
    directory = ctx.task_directory(task, oracle, "gopy")
    automatic_path = directory / "automatic.json"
    automatic = json.loads(pinned_bytes(automatic_path, case.automatic_sha256))
    if automatic != oracle.get("automatic_result"):
        raise ContractError("automatic Python recovery differs from released automatic result")
    golden = ctx.legacy / "gopy" / repo_path(source["patch"])
    pinned_bytes(golden, case.golden_sha256)
    recovered = ctx.legacy / RECOVERED / case.recovered_slug
    meta_path, log_path = recovered / "oracle.json", recovered / "discovery.log"
    metadata = json.loads(pinned_bytes(meta_path, case.metadata_sha256))
    log = pinned_bytes(log_path, case.log_sha256).decode()
    pinned_bytes(ctx.legacy / REPLAY, REPLAY_SHA256)
    if (metadata.get("ok") is not True or metadata.get("kind") != "python"
            or metadata.get("project_relative") != case.project):
        raise ContractError("automatic Python retained selector recovery is not usable")
    nodes = metadata.get("p2p", [])
    groups = node_groups(nodes)
    if len(nodes) != case.count:
        raise ContractError("automatic Python recovered selector cardinality differs")
    declared = oracle.get("pass_to_pass_tests")
    if declared is not None and declared != nodes:
        raise ContractError("released automatic Python identities differ from recovered selectors")
    discovery = audit_discovery(log, metadata)
    runner = metadata["runner"]
    original_commands = [[runner, "-m", "pytest", "-v", "-p", "no:cacheprovider", *g] for g in groups]
    expected_discovery = " && ".join(shlex.join(
        [runner, "-m", "pytest", "-v", "-p", "no:cacheprovider", target]) for target in metadata["targets"])
    if metadata.get("command") != expected_discovery:
        raise ContractError("automatic Python discovery command/selector binding differs")
    inputs = frozen_test_inputs(ctx, task, case, nodes)
    return {
        "schema_version": RECOVERY_SCHEMA, "task_id": task["id"], "repo": task["repo"],
        "start_sha": task["start_sha"], "task_sha256": canonical_hash(task),
        "released_oracle_sha256": canonical_hash(oracle), "source_only_golden_sha256": case.golden_sha256,
        "nodes": nodes, "groups": groups, "project_relative": case.project, **inputs,
        "original_commands": original_commands, "original_discovery_command": metadata["command"],
        "recovered_discovery": discovery,
        "evidence": [{"path": str(p.relative_to(ctx.legacy)), "sha256": h} for p, h in (
            (automatic_path, case.automatic_sha256), (meta_path, case.metadata_sha256),
            (log_path, case.log_sha256), (ctx.legacy / REPLAY, REPLAY_SHA256),
            (golden, case.golden_sha256))],
        "limitations": [
            "Original automatic shards retained a P2P count, not original per-node/six-run logs.",
            "Wave-001 reconstructed selectors using three start runs and the frozen count; "
            "these retained recovery records are not an original-node-identity attestation.",
            "The wave-001 dependency lock/install snapshot is not retained here. The separately "
            "prepared retained-venv snapshot is new migration evidence, not historical replay proof.",
        ],
        "runtime_verified": False,
    }


def normalized_distribution(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def dependency_inventory(repository: Path, case: AutomaticCase) -> dict:
    """Inspect retained metadata only; do not import/install/edit its venv."""
    venv = repository / case.venv
    config = venv / "pyvenv.cfg"
    if not config.is_file():
        raise ContractError("retained automatic Python dependency venv missing: " + str(venv))
    text = config.read_text()
    if re.search(r"^include-system-site-packages\s*=\s*true\s*$", text, re.M | re.I):
        raise ContractError("retained automatic Python venv inherits unbundled system dependencies")
    version = re.search(r"^(?:version|version_info)\s*=\s*(\d+\.\d+\.\d+)\s*$", text, re.M)
    if not version:
        raise ContractError("retained automatic Python dependency interpreter version missing")
    sites = sorted((venv / "lib").glob("python*/site-packages"))
    expected = "python" + ".".join(version[1].split(".")[:2])
    if len(sites) != 1 or sites[0].parent.name != expected:
        raise ContractError("retained automatic Python dependency ABI is ambiguous")
    site = sites[0]
    distributions, excluded = [], []
    for path in sorted(site.glob("*.dist-info")):
        metadata_path = path / "METADATA"
        metadata = Parser().parsestr(metadata_path.read_text())
        name = normalized_distribution(metadata["Name"] or "")
        if name == normalized_distribution(case.distribution):
            excluded.append(path.name)
            continue
        direct = path / "direct_url.json"
        if direct.is_file() and json.loads(direct.read_text()).get("dir_info", {}).get("editable"):
            raise ContractError("foreign editable dependency would reference a historical checkout: " + name)
        if not name or not metadata["Version"]:
            raise ContractError("retained automatic Python distribution identity missing")
        distributions.append({"name": name, "version": metadata["Version"],
                              "metadata_sha256": file_hash(metadata_path)})
    if "pytest" not in {d["name"] for d in distributions}:
        raise ContractError("retained automatic Python dependency site lacks pytest")
    return {
        "basis": "read-only retained automatic venv; separately captured migration dependency snapshot",
        "venv": str(venv), "site": str(site), "pyvenv_sha256": file_hash(config),
        "python_version": version[1], "image_reference": "python:" + version[1] + "-bookworm",
        "distributions": distributions, "excluded_distribution_dirs": excluded,
        "excluded_source_packages": list(case.source_packages),
        "historical_wave001_lock": False,
    }


def dependency_files(site: Path, case: AutomaticCase, inventory: dict) -> list[str]:
    """Exclude all project code and executable editable/path hooks, not just .pth."""
    excluded = set(inventory["excluded_distribution_dirs"])
    names = []
    for name, path in sorted(dependency_tree_files(site).items()):
        parts = Path(name).parts
        if (parts[0] in excluded or parts[0] in case.source_packages
                or parts[0].removesuffix(".py") in case.source_packages
                or "__pycache__" in parts or name.endswith((".pyc", ".pyo", ".pth", ".egg-link"))
                or parts[0].startswith(("__editable__", "_virtualenv"))
                or parts[0].endswith(".egg-info")):
            continue
        if path.is_symlink():
            # Archive-relative symlinks can bypass filtering of an owned package.
            # A retained site requiring such a link needs a separately flattened copy.
            raise ContractError("automatic Python dependency site needs explicit symlink flattening: " + name)
        if path.is_file():
            names.append(name)
    if not names:
        raise ContractError("automatic Python dependency snapshot is empty")
    return names


def prepared_dependencies(ctx, task: dict, recovery: dict) -> tuple[dict, dict]:
    root = ctx.config.get("migration", {}).get("python_automatic_dependency_root")
    if not root or not Path(root).is_absolute():
        raise ContractError("automatic Python requires prepared dependencies: set migration.python_automatic_dependency_root")
    path = Path(root) / Path(CASES[task["id"]].directory).name / "preparation.json"
    if not path.is_file() or path.is_symlink():
        raise ContractError("automatic Python dependency preparation missing: " + str(path))
    manifest = json.loads(path.read_text())
    if (manifest.get("schema_version") != DEPENDENCY_SCHEMA or manifest.get("status") != "prepared"
            or manifest.get("task_id") != task["id"] or manifest.get("start_sha") != task["start_sha"]
            or manifest.get("recovery_sha256") != canonical_hash(recovery)
            or manifest.get("excluded_source_packages") != list(CASES[task["id"]].source_packages)
            or manifest.get("runtime_verified") is not False):
        raise ContractError("automatic Python dependency preparation is not bound to this recovered contract")
    audit = json.loads(resolve_asset(manifest["image_audit"], ctx.output).read_text())
    if (audit.get("schema_version") != "reviewbench-developer-image-audit/v1"
            or audit.get("image") != manifest["image"] or audit.get("no_reference_assets") is not True
            or audit.get("no_repository_history") is not True
            or audit.get("python_version") != manifest.get("python_version")):
        raise ContractError("automatic Python dependency image audit/ABI differs")
    site = json.loads(resolve_asset(manifest["site_asset"], ctx.output).read_text())
    if site.get("source_kind") != "dependency" or site.get("tree_sha256") != manifest.get("site_tree_sha256"):
        raise ContractError("automatic Python dependency archive is not the prepared source-free site")
    return manifest, ctx.copy_asset(path, "grader")


def entry_contract(recovery: dict, python_version: str) -> dict:
    case = CASES[recovery["task_id"]]
    return {
        "schema_version": ENTRY_SCHEMA, "nodes": recovery["nodes"], "groups": recovery["groups"],
        "project_relative": case.project, "pytest_root_relative": case.project,
        "test_files": recovery["test_files"], "test_roots": recovery["test_roots"],
        "test_symlinks": recovery["test_symlinks"],
        "config_path": ASSET_ROOT + "/" + Path(case.config_file).name, "config_sha256": recovery["config_sha256"],
        "dependency_site": SITE_TARGET, "source_packages": list(case.source_packages),
        "python_version": python_version, "timeout_seconds": 1800,
    }


def grading_command(recovery: dict) -> dict:
    command = shlex.join(["python3", "-I", "-B", ASSET_ROOT + "/entry.py",
                          "--contract", ASSET_ROOT + "/contract.json", "--workspace", "/workspace",
                          "--report", "/workspace/" + REPORT_PATH])
    return {"command": command, "parser": "json", "report_path": REPORT_PATH,
            "expected_tests": recovery["nodes"]}


def runtime_limits(recovery: dict) -> dict:
    return {"cpus": 2, "memory_mb": 4096, "build_scratch_mb": 4096,
            "scratch_paths": [str(Path(REPORT_PATH).parent)],
            "command_timeout_seconds": 1800 * len(recovery["groups"]) + 120}


def migrate(ctx, task: dict, oracle: dict) -> RuntimeRecipe:
    recovery = recover_contract(ctx, task, oracle)
    dependencies, preparation_asset = prepared_dependencies(ctx, task, recovery)
    case = CASES[task["id"]]
    config_bytes = git(ctx.repo(task), "show", task["start_sha"] + ":" + case.config_file)
    config_name = Path(case.config_file).name
    contract = entry_contract(recovery, dependencies["python_version"])
    with tempfile.TemporaryDirectory(prefix="python-automatic-contract-", dir=ctx.scratch) as temporary:
        root = Path(temporary)
        (root / config_name).write_bytes(config_bytes)
        atomic_json(root / "contract.json", contract)
        atomic_json(root / "recovery.json", recovery)
        files = {name: ctx.copy_asset(root / name, "grader")
                 for name in (config_name, "contract.json", "recovery.json")}
    entry = ctx.copy_asset(Path(__file__).with_name("python_automatic_entry.py"), "grader")
    workspace = Path("/workspace")
    roots = list(dict.fromkeys(str(p) for p in (
        workspace, workspace / "src", workspace / case.project, workspace / case.project / "src")))
    anchor = task["locality"]["path"]
    compile_command = "python3 -I -B -c " + shlex.quote(
        f"from pathlib import Path; p=Path({anchor!r}); compile(p.read_bytes(),str(p),'exec'); "
        "print('Public production-module compilation: PASS')")
    recipe = RuntimeRecipe(
        task_id=task["id"], repo=task["repo"], start_sha=task["start_sha"], lane="gopy",
        image=dependencies["image"], image_audit=dependencies["image_audit"],
        **runtime_limits(recovery),
        dev_checks=[compile_command],
        bootstrap=["mkdir -p /workspace/.reviewbench/tmp /workspace/.reviewbench-python-automatic"],
        env={"SOURCE_ROOT": "/workspace", "PYTHONPATH": ":".join([*roots, SITE_TARGET]),
             "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
             "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": "",
             "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
             "NUMEXPR_NUM_THREADS": "1", "LOKY_MAX_CPU_COUNT": "2",
             "MPLCONFIGDIR": "/workspace/.reviewbench/tmp/matplotlib",
             "TMPDIR": "/workspace/.reviewbench/tmp"},
        submodules=ctx.modules(task),
        assets=[
            {**dependencies["site_asset"], "kind": "archive", "visibility": "developer", "target": SITE_TARGET},
            {**entry, "visibility": "grader", "target": ASSET_ROOT + "/entry.py"},
            {**preparation_asset, "visibility": "grader", "target": ASSET_ROOT + "/preparation.json"},
            *[{**asset, "visibility": "grader", "target": ASSET_ROOT + "/" + name}
              for name, asset in files.items()],
        ],
        grading={"multifile_build": True,
                 "immutable_paths": sorted(set(ctx.immutable_tests(task)) | set(recovery["test_files"])
                                           | set(recovery["test_symlinks"])),
                 "golden_patch": ctx.golden(task, oracle, "gopy", ctx.legacy / "gopy" / case.directory),
                 "automatic_python_recovery": files["recovery.json"],
                 "commands": [grading_command(recovery)]},
    )
    recipe.validate()
    return recipe
