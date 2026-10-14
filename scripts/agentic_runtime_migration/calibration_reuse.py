"""Evidence-bound reuse of original start runs after a read-only predicate change."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import stat

from reviewbench_maintainability.agentic.assets import asset_descriptor
from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash, file_hash
from reviewbench_maintainability.agentic.evaluation import parse_tests
from reviewbench_maintainability.agentic.snapshots import tree_files, tree_hash
from .tsjs_pnpm9_transport import SOURCE as ORIGINAL_PREDICATE_SOURCE
from .tsjs_pnpm_fixed_workspaces import CONFIG_NAME, COPY_ROUTINE_SHA256, SOURCE, fixed_contract

SCHEMA = "reviewbench-input-specific-start-bootstrap-equivalence/v1"
TRANSPORT_TARGET = "/opt/reviewbench-assets/blaze-transport"


def checked_json(pointer):
    if not isinstance(pointer, dict) or set(pointer) != {"path", "sha256"}:
        raise ContractError("calibration reuse needs a hash-bound evidence file")
    path = Path(pointer["path"])
    if (not path.is_absolute() or path.is_symlink() or not path.is_file()
            or path.stat().st_size > 64 * 1024**2 or file_hash(path) != pointer["sha256"]):
        raise ContractError("calibration reuse evidence changed or is absent")
    return json.loads(path.read_text())


def snapshot_workspace(root):
    """All nodes, including empty directories and private Git bytes; no link traversal."""
    root = Path(root)
    rows = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in sorted([*dirs, *files]):
            path = Path(directory) / name
            info = path.lstat()
            row = {"mode": stat.S_IMODE(info.st_mode)}
            if path.is_symlink():
                row.update(type="symlink", target=os.readlink(path))
            elif path.is_file():
                row.update(type="file", sha256=file_hash(path), bytes=info.st_size)
            elif path.is_dir():
                row.update(type="directory")
            else:
                raise ContractError("bootstrap equivalence encountered a special node")
            rows[path.relative_to(root).as_posix()] = row
    return {"nodes": rows, "node_count": len(rows), "state_sha256": canonical_hash(rows)}


def validate_transport_change(old_recipe, new_recipe, old_root, new_root, layout, original_files):
    old, new = asdict(old_recipe), asdict(new_recipe)
    if {key for key in old if old[key] != new[key]} != {"assets"}:
        raise ContractError("calibration reuse changed source/build/tests/environment/resources")
    if len(old["assets"]) != len(new["assets"]):
        raise ContractError("calibration reuse changed the asset inventory")
    changed = [index for index in range(len(old["assets"])) if old["assets"][index] != new["assets"][index]]
    if len(changed) != 1:
        raise ContractError("calibration reuse needs exactly one transport archive change")
    before, after = [assets[changed[0]] for assets in (old["assets"], new["assets"])]
    expected_properties = {"kind": "archive", "visibility": "developer", "target": TRANSPORT_TARGET}
    if ({key: value for key, value in before.items() if key not in {"path", "sha256"}} != expected_properties
            or {key: value for key, value in after.items() if key not in {"path", "sha256"}} != expected_properties):
        raise ContractError("calibration reuse transport role or target differs")
    old_root, new_root = Path(old_root), Path(new_root)
    for asset, root in ((before, old_root), (after, new_root)):
        descriptor = asset_descriptor(asset, root)
        if descriptor["source_kind"] != "toolchain" or descriptor["tree_sha256"] != tree_hash(root):
            raise ContractError("calibration reuse tool snapshot is not the actual recipe archive")
    old_files, new_files = tree_files(old_root), tree_files(new_root)
    if set(new_files) != {*old_files, CONFIG_NAME} or "transport.cjs" not in old_files:
        raise ContractError("calibration reuse changed unrelated transport files")
    for name in old_files:
        if name == "transport.cjs":
            continue
        if (old_files[name].is_symlink() or new_files[name].is_symlink()
                or old_files[name].read_bytes() != new_files[name].read_bytes()
                or stat.S_IMODE(old_files[name].stat().st_mode) != stat.S_IMODE(new_files[name].stat().st_mode)):
            raise ContractError("calibration reuse altered an unrelated tool or configuration")
    if ((old_root / "transport.cjs").read_text() != ORIGINAL_PREDICATE_SOURCE
            or (new_root / "transport.cjs").read_text() != SOURCE):
        raise ContractError("calibration reuse input predicate is not the reviewed read-only implementation")
    expected = fixed_contract(layout, original_files)
    if json.loads((new_root / CONFIG_NAME).read_text()) != expected:
        raise ContractError("calibration reuse does not bind the exact original fixed installation")
    return {
        "old_runtime_fingerprint": old_recipe.fingerprint,
        "new_runtime_fingerprint": new_recipe.fingerprint,
        "changed_transport_target": TRANSPORT_TARGET, "copy_routine_sha256": COPY_ROUTINE_SHA256,
        "old_transport_source_sha256": hashlib.sha256(ORIGINAL_PREDICATE_SOURCE.encode()).hexdigest(),
        "new_transport_source_sha256": hashlib.sha256(SOURCE.encode()).hexdigest(),
        "fixed_contract_sha256": canonical_hash(expected),
        "all_other_recipe_fields_and_assets_identical": True,
    }


def audit_commands(directory, expected_commands, test_specs=()):
    directory = Path(directory)
    records = [json.loads(path.read_text()) for path in directory.glob("*.json")]
    records = [row for row in records if "stdout_file" in row]
    if Counter(row["command"] for row in records) != Counter(expected_commands):
        raise ContractError("calibration command set differs from its actual original recipe")
    for row in records:
        if (row["status"] != "completed" or row["returncode"] != 0
                or row["timed_out"] or row["log_limit_exceeded"]):
            raise ContractError("calibration contains an unfinished or unsuccessful command")
        for stream in ("stdout", "stderr"):
            path = directory / row[stream + "_file"]
            if path.is_symlink() or file_hash(path) != row[stream + "_sha256"]:
                raise ContractError("original calibration output bytes changed")
    tests = {}
    for index, spec in enumerate(test_specs):
        matching = [row for row in records if row["command"] == spec["command"]]
        if len(matching) != 1 or spec.get("report_path"):
            raise ContractError("retained calibration needs one original log-parsed test command")
        row = matching[0]
        text = "\n".join((directory / row[stream + "_file"]).read_text() for stream in ("stdout", "stderr"))
        parsed = parse_tests(spec["parser"], text, command_ok=True, expected=spec["expected_tests"],
                             success_marker=spec.get("success_marker"), success_pattern=spec.get("success_pattern"))
        tests.update({f"{index}::{name}": value for name, value in parsed.items()})
    if test_specs and (not tests or not all(tests.values())):
        raise ContractError("retained calibration no longer proves all original assertions")
    return {"commands": len(records), "tests": tests,
            "records": [{"path": str(directory / (row["id"] + ".json")),
                         "sha256": file_hash(directory / (row["id"] + ".json"))} for row in records]}


def validate_equivalence(pointer, task, oracle, old_recipe, new_recipe, expected_engine):
    certificate = checked_json(pointer)
    if (certificate.get("schema_version") != SCHEMA
            or certificate.get("status") != "input_specific_start_bootstrap_equivalence_verified"
            or certificate.get("task_fingerprint") != canonical_hash(task)
            or certificate.get("oracle_fingerprint") != canonical_hash(oracle)
            or certificate.get("engine_fingerprint") != expected_engine
            or certificate.get("start_sha") != task["start_sha"]
            or certificate.get("full_scope") != 402
            or certificate.get("new_SUT_build_test_model_judge_calls") != 0):
        raise ContractError("start equivalence certificate identity differs")
    old_root, new_root = [Path(certificate[key]) for key in ("old_tool_snapshot", "new_tool_snapshot")]
    layout = checked_json(certificate["original_layout"])
    encoded = checked_json(certificate["original_input_texts"])
    original_files = {name: text.encode() for name, text in encoded.items()}
    expected = validate_transport_change(old_recipe, new_recipe, old_root, new_root, layout, original_files)
    if certificate.get("transport_delta") != expected:
        raise ContractError("start equivalence transport comparison differs")
    old_state, new_state = [checked_json(certificate[key]) for key in ("old_state", "new_state")]
    if (not old_state.get("nodes") or old_state != new_state
            or old_state.get("state_sha256") != canonical_hash(old_state["nodes"])
            or old_state.get("node_count") != len(old_state["nodes"])):
        raise ContractError("old and new complete start bootstrap states differ")
    if not certificate.get("identical_complete_source_and_git_seed"):
        raise ContractError("bootstrap equivalence lacks the shared exact source/Git seed")
    prefix = old_recipe.bootstrap[:-1]
    if not prefix or old_recipe.bootstrap[-1] != certificate.get("original_build_command_not_executed"):
        raise ContractError("equivalence bootstrap prefix may execute the SUT build")
    for key in ("old_commands", "new_commands"):
        observed = audit_commands(certificate[key], prefix)
        if observed["tests"]:
            raise ContractError("equivalence unexpectedly executed tests")
    return certificate


def validate_original_starts(proof_pointer, directory, task, oracle, recipe, expected_engine):
    proof = checked_json(proof_pointer)
    if (proof.get("id") != task["id"] or proof.get("runtime_fingerprint") != recipe.fingerprint
            or proof.get("task_fingerprint") != canonical_hash(task)
            or proof.get("oracle_fingerprint") != canonical_hash(oracle)
            or proof.get("engine_fingerprint") != expected_engine
            or Counter((row["variant"], row["repetition"]) for row in proof.get("runs", []))
               != Counter(("start", number) for number in range(3))):
        raise ContractError("retained original three-start identity differs")
    directory = Path(directory)
    audit_commands(directory / "developer", [*recipe.bootstrap, *recipe.dev_checks])
    commands = [*recipe.bootstrap, *recipe.grading.get("bootstrap", []),
                *[spec["command"] for spec in recipe.grading["commands"]]]
    for row in proof["runs"]:
        audit = audit_commands(directory / ("start-" + str(row["repetition"])), commands,
                               recipe.grading["commands"])
        if audit["tests"] != row["tests"]:
            raise ContractError("retained original test values differ from actual command outputs")
    return proof


def compose_readiness(old_pointer, old_directory, golden_pointer, golden_directory,
                      equivalence_pointer, task, oracle, old_recipe, new_recipe, expected_engine):
    validate_equivalence(equivalence_pointer, task, oracle, old_recipe, new_recipe, expected_engine)
    old = validate_original_starts(old_pointer, old_directory, task, oracle, old_recipe, expected_engine)
    golden = checked_json(golden_pointer)
    if (golden.get("status") != "original_golden_calibrations_complete"
            or golden.get("id") != task["id"]
            or golden.get("runtime_fingerprint") != new_recipe.fingerprint
            or golden.get("task_fingerprint") != canonical_hash(task)
            or golden.get("oracle_fingerprint") != canonical_hash(oracle)
            or golden.get("engine_fingerprint") != expected_engine
            or Counter((row["variant"], row["repetition"]) for row in golden.get("runs", []))
               != Counter(("golden", number) for number in range(3))):
        raise ContractError("fresh golden calibration identities are incomplete")
    commands = [*new_recipe.bootstrap, *new_recipe.grading.get("bootstrap", []),
                *[spec["command"] for spec in new_recipe.grading["commands"]]]
    for row in golden["runs"]:
        audit = audit_commands(Path(golden_directory) / ("golden-" + str(row["repetition"])),
                               commands, new_recipe.grading["commands"])
        if audit["tests"] != row["tests"]:
            raise ContractError("fresh golden results differ from actual command outputs")
    runs = []
    for source, runtime, pointer, kind in (
            (old, old_recipe, old_pointer, "retained-equivalent-original-start"),
            (golden, new_recipe, golden_pointer, "fresh-original-golden")):
        for original in source["runs"]:
            row = deepcopy(original)
            row["execution_provenance"] = {
                "kind": kind, "actual_runtime_fingerprint": runtime.fingerprint, "original_proof": pointer,
            }
            runs.append(row)
    return {
        "id": task["id"], "status": "ready", "runtime_fingerprint": new_recipe.fingerprint,
        "task_fingerprint": canonical_hash(task), "oracle_fingerprint": canonical_hash(oracle),
        "engine_fingerprint": expected_engine, "runs": runs,
        "calibration_provenance": {
            "schema_version": "reviewbench-readiness-with-explicit-input-equivalence/v1",
            "retained_original_start": old_pointer, "fresh_original_golden": golden_pointer,
            "start_bootstrap_equivalence": equivalence_pointer,
            "actual_start_runtime_fingerprint": old_recipe.fingerprint,
            "actual_golden_runtime_fingerprint": new_recipe.fingerprint,
            "replayed_successful_start_calibrations": 0,
        },
    }
