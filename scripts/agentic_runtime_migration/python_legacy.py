"""Evidence-bound Python legacy closure; never synthesize replacement Oracles."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shlex

from reviewbench_maintainability.agentic.contracts import ContractError, RuntimeRecipe, canonical_hash
from reviewbench_maintainability.agentic.environment import resolve_asset

QDK = "github__microsoft__qdk-ec__pull_105__comment_3553632084__complexity"
MIND = "github__dimiturtrz__mindscape__pull_2__comment_3505662522__modularity"
DEPENDENCY_SCHEMA = "reviewbench-python-legacy-dependencies/v1"
ASSET_ROOT = "/opt/reviewbench-assets/python-legacy"
SITE = ASSET_ROOT + "/site"
QDK_DIRECTORY = "gopy/recovery/092-qdk-ec"
QDK_ORACLE = "58f00c08e69fd9ffbb77386100c66b1a38166f0084dbd0c56463c6509b02f8ce"
QDK_TASK = "7f6b8dd972ffd84e692221d24d2e522e212ddcd61f4287cb8789689bb5640aa6"
QDK_FILES = {
    "oracle.py": "9a91085bfa9fca41a42fd6450188c2bbcfb4466984775a425d11ad03cce8d61f",
    "run_oracle.sh": "bff960bd44ad2de75abd0b4755f8919c6931db0ab75292661e893ab788fb356a",
    "session-spec.json": "a9104051ab0461f1a00b6915a18414706adf83f933675ab8b0144bb9ddeec639",
    "dependency-closed-source-only.patch": "fa749cdc6797f01566018ea770f8cb8f49091f56ecf92bab41119761f6803529",
}
QDK_SUPPORT = "2dcb4a4bf3c995159776c213de461b40682fe2bf64ffc5f83b0ced01a4b935f3"
QDK_LOG = "330c081f704b0a70598cdd42e7476d12398400e5b25e610531cafa92aecc5b30"
QDK_REQUIRED = {"stim": "1.15.0", "binar": "0.1.3", "protobuf": "6.33.5",
                "grpcio": "1.76.0", "grpcio-tools": "1.76.0"}

# These are the *released* paths, not the independently authored 20260813
# replacement tests. An absent original stays absent; no fallback root exists.
LEGACY_INPUTS = {
    "github__OpenFreeEnergy__alchemiscale__pull_421__comment_2294333734__modularity": (
        "increment5-recovery/gopy4-baseline/01-alchemiscale",
        ("test_recovery.py", "source-only-golden.patch", "start-1.log", "golden-1.log")),
    "github__Pyomo__pyomo__pull_3698__comment_2761545989__duplication": (
        "recovery/wave2-coverage-oracle",
        ("test_pyomo_gurobi_loader_recovery.py", "pyomo-source-only-golden.patch",
         "pyomo-start-1.log", "pyomo-golden-1.log")),
    "github__codecov__shared__pull_90__comment_1412095367__complexity": (
        "recovery/wave2-baseline/codecov-90",
        ("recovery_oracle_test.py", "start-1.log", "golden-1.log")),
    "github__labscript-suite__lyse__pull_119__comment_2042996306__testability": (
        "recovery/incremental-python-2/lyse",
        ("test_recovery.py", "golden.patch", "start-run1.log", "golden-run1.log")),
    "github__open-formulieren__open-forms__pull_3125__comment_1224260642__complexity": (
        "increment5-recovery/gopy2-env/openforms-3125",
        ("test_behavior.py", "Dockerfile", "start-run1.log", "golden-run1.log")),
    "github__redis__redis-py__pull_3350__comment_1723391627__api_design": (
        "increment5-recovery/gopy2-baseline/redis-3350",
        ("oracle.py", "source-only-golden.patch", "start-run1.log", "golden-run1.log")),
    "github__scikit-learn__scikit-learn__pull_18359__comment_501886754__modularity": (
        "recovery/scientific-python/sklearn-18359",
        ("golden.patch", "Dockerfile", "docker-run-attempt2.log")),
}
COHORT = tuple(sorted([*LEGACY_INPUTS, QDK, MIND]))


def pinned(path: Path, expected: str) -> bytes:
    if not path.is_file() or path.is_symlink():
        raise ContractError("original Python input is absent: " + str(path))
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected:
        raise ContractError("original Python input hash differs: " + str(path))
    return data


def legacy_preflight(task: dict, oracle: dict, legacy: Path) -> dict:
    """No outcome-based selectors and no replacement-harness inference."""
    directory, names = LEGACY_INPUTS[task["id"]]
    root = legacy.parent / directory
    observations = []
    for name in names:
        path = root / name
        regular = path.is_file() and not path.is_symlink()
        observations.append({"path": str(path), "present": regular,
                             "sha256": hashlib.sha256(path.read_bytes()).hexdigest() if regular else None})
    expected = oracle.get("pass_to_pass_tests")
    return {"task_id": task["id"], "status": "blocked", "phase": "original-input-binding",
            "task_fingerprint": canonical_hash(task), "oracle_fingerprint": canonical_hash(oracle),
            "inputs": observations, "declared_tests": expected,
            "test_identities_explicit": isinstance(expected, list) and bool(expected)
                                       and all(isinstance(x, str) for x in expected),
            "cause": "original_harness_or_golden_missing" if any(not r["present"] for r in observations)
                     else "original_inputs_need_explicit_test_and_golden_attestation",
            "runtime_verified": False,
            "next": "restore the exact released test/golden inputs with provenance; do not substitute "
                    "the later engineered session Oracle or old candidate binaries"}


def mindscape_boundary(ctx, task, oracle, prior_package: Path) -> dict:
    """Verify, but do not rerun, the already measured missing-source failure."""
    from .python_automatic import CASES, git, pinned_bytes
    from reviewbench_maintainability.agentic.contracts import component, file_hash
    case = CASES[MIND]
    patch = ctx.legacy / "gopy" / case.directory / "source-only-golden.patch"
    patch_bytes = pinned_bytes(patch, case.golden_sha256)
    listed = git(ctx.repo(task), "ls-tree", "-r", "--name-only", task["start_sha"], "--", "baselines")
    present = set(listed.decode().splitlines())
    missing = "baselines/base.py"
    path = prior_package / "execution/preparation/verified" / component(MIND)
    proof_path = path / "verification.json"
    proof = json.loads(proof_path.read_text())
    log = path / "golden-0/4000b20b324a449292b3a0c7ba085ffa.stdout"
    log_bytes = pinned(log, "934ff23ad9402171c22202cbdfc2eb383aa416f948f8891112c95e8e8f620e42")
    if (task["id"] != MIND or task["start_sha"] != case.start_sha
            or missing in present or "baselines/base/__init__.py" in present
            or b"+from baselines.base import Baseline" not in patch_bytes
            or b"diff --git a/baselines/base" in patch_bytes
            or proof.get("task_fingerprint") != canonical_hash(task)
            or proof.get("oracle_fingerprint") != canonical_hash(oracle)
            or proof.get("status") != "blocked"
            or b"ModuleNotFoundError: No module named 'baselines.base'" not in log_bytes):
        raise ContractError("retained Mindscape source-boundary evidence differs; review before execution")
    return {"task_id": MIND, "status": "blocked", "phase": "golden-source-closure",
            "cause": "frozen golden imports baselines.base but supplies neither that module nor its package",
            "missing_candidate_path": missing, "golden_sha256": case.golden_sha256,
            "task_fingerprint": canonical_hash(task), "oracle_fingerprint": canonical_hash(oracle),
            "prior_proof": {"path": str(proof_path), "sha256": file_hash(proof_path),
                            "engine_fingerprint": proof["engine_fingerprint"],
                            "completed_start_replays": sum(r["variant"] == "start" and all(r["tests"].values())
                                                          for r in proof["runs"])},
            "failure_log": {"path": str(log), "sha256": file_hash(log)},
            "runtime_verified": False, "rerun": False,
            "next": "retain source failure; an external wheel or helper copied from target would replace candidate code"}


def qdk_contract(ctx, task: dict, oracle: dict) -> dict:
    if (task["id"] != QDK or canonical_hash(task) != QDK_TASK
            or canonical_hash(oracle) != QDK_ORACLE):
        raise ContractError("QDK released task/Oracle identity differs")
    directory = ctx.legacy / QDK_DIRECTORY
    for name, digest in QDK_FILES.items():
        pinned(directory / name, digest)
    pinned(ctx.legacy / "gopy/oracle_support.py", QDK_SUPPORT)
    spec = json.loads((directory / "session-spec.json").read_text())
    if (spec["command"] != ["bash", "{task_dir}/run_oracle.sh"]
            or spec["pass_to_pass_tests"] != oracle["pass_to_pass_tests"]):
        raise ContractError("QDK original command/identities differ")
    observations = []
    signatures = []
    for phase in ("start", "golden"):
        runs = oracle["runs"][phase]
        if len(runs) != 3:
            raise ContractError("QDK original three-run binding missing")
        for number, run in enumerate(runs, 1):
            path = directory / f"{phase}-{number}.log"
            text = pinned(path, QDK_LOG).decode()
            signature = run.get("signature")
            if (run.get("run") != number or run.get("exit_code") != 0 or run.get("ok") is not True
                    or not signature or text.splitlines().count("P2P_SIGNATURE=" + signature) != 1):
                raise ContractError("QDK retained behavior/log binding differs")
            signatures.append(signature)
            observations.append({"phase": phase, "repetition": number, "path": str(path), "sha256": QDK_LOG})
    if len(set(signatures)) != 1:
        raise ContractError("QDK retained behavior signatures differ")
    return {"task_id": QDK, "task_fingerprint": canonical_hash(task),
            "oracle_fingerprint": canonical_hash(oracle), "expected_tests": oracle["pass_to_pass_tests"],
            "signature": signatures[0], "inputs": QDK_FILES, "oracle_support_sha256": QDK_SUPPORT,
            "retained_runs": observations, "runtime_verified": False}


def prepared_qdk(ctx, task, contract):
    root = ctx.config.get("migration", {}).get("python_legacy_dependency_root")
    if not root or not Path(root).is_absolute():
        raise ContractError("QDK requires prepared, source-free Python dependencies")
    path = Path(root) / "qdk-ec105/preparation.json"
    value = json.loads(path.read_text())
    if (value.get("schema_version") != DEPENDENCY_SCHEMA or value.get("status") != "prepared"
            or value.get("task_id") != task["id"] or value.get("start_sha") != task["start_sha"]
            or value.get("contract_sha256") != canonical_hash(contract)
            or value.get("owned_packages") != ["deq"]
            or value.get("owned_distributions") != ["deq"] or value.get("runtime_verified") is not False):
        raise ContractError("QDK prepared dependencies are not bound to current contract")
    site = json.loads(resolve_asset(value["site_asset"], ctx.output).read_text())
    audit = json.loads(resolve_asset(value["image_audit"], ctx.output).read_text())
    if (site.get("source_kind") != "dependency" or site.get("tree_sha256") != value.get("site_tree_sha256")
            or audit.get("image") != value["image"] or audit.get("no_reference_assets") is not True
            or audit.get("no_repository_history") is not True
            or audit.get("python_version") != value.get("python_version")):
        raise ContractError("QDK prepared dependency archive/image binding differs")
    return value, ctx.copy_asset(path, "grader")


def qdk_migrate(ctx, task, oracle):
    contract = qdk_contract(ctx, task, oracle)
    dependencies, preparation = prepared_qdk(ctx, task, contract)
    directory = ctx.legacy / QDK_DIRECTORY
    runner = ctx.copy_asset(Path(__file__).with_name("python_candidate_entry.py"), "developer")
    helper = ctx.copy_asset(ctx.legacy / "gopy/oracle_support.py", "grader")
    script = ctx.copy_asset(directory / "oracle.py", "grader")
    original_shell = ctx.copy_asset(directory / "run_oracle.sh", "grader")
    spec = ctx.copy_asset(directory / "session-spec.json", "grader")
    base = ["python3", "-I", "-B", ASSET_ROOT + "/candidate_entry.py"]
    options = ["--workspace", "/workspace", "--site", SITE, "--project", "deq", "--packages", "deq"]
    build = shlex.join([*base, "build", *options, "--compiler", "deq/deq/proto/compile.py"])
    check = shlex.join([*base, "check", *options, "--imports",
                       "deq.circuit.parser", "deq.transpiler.jit_library_builder"])
    execute = shlex.join([*base, "run", *options, "--script", ASSET_ROOT + "/oracle.py"])
    recipe = RuntimeRecipe(
        task_id=task["id"], repo=task["repo"], start_sha=task["start_sha"], lane="gopy",
        image=dependencies["image"], image_audit=dependencies["image_audit"],
        cpus=2, memory_mb=4096, build_scratch_mb=4096, command_timeout_seconds=300,
        scratch_paths=[".reviewbench-python-candidate"],
        bootstrap=[build], dev_checks=[check],
        env={"PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
             "COVERAGE_OUT": "/workspace/.reviewbench-python-candidate/coverage.json",
             "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"},
        submodules=ctx.modules(task),
        assets=[{**dependencies["site_asset"], "kind": "archive", "visibility": "developer", "target": SITE},
                {**runner, "visibility": "developer", "target": ASSET_ROOT + "/candidate_entry.py"},
                *[{**asset, "visibility": "grader", "target": ASSET_ROOT + "/" + name}
                  for name, asset in (("oracle_support.py", helper), ("oracle.py", script),
                                      ("original-run_oracle.sh", original_shell), ("session-spec.json", spec),
                                      ("preparation.json", preparation))]],
        grading={"multifile_build": True, "immutable_paths": ctx.immutable_tests(task),
                 "golden_patch": ctx.golden(task, oracle, "gopy", directory),
                 "commands": [{"command": execute, "parser": "regex",
                               "expected_tests": contract["expected_tests"],
                               "success_pattern": "^P2P_SIGNATURE=" + re.escape(contract["signature"]) + "$"}]},
    )
    recipe.validate()
    return recipe
