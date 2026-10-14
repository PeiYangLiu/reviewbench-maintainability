"""Retained executions keep their actual identities and need strict equivalence."""
from copy import deepcopy
from dataclasses import dataclass, field, asdict
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import calibration_reuse as reuse
from agentic_runtime_migration.tsjs_pnpm_fixed_workspaces import CONFIG_NAME, SOURCE, fixed_contract
from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash, file_hash
from reviewbench_maintainability.agentic.snapshots import tree_hash


@dataclass
class Recipe:
    assets: list
    image: str = "original-image"
    env: dict = field(default_factory=lambda: {"ORIGINAL": "true"})
    bootstrap: list = field(default_factory=lambda: ["setup", "copy-original", "original-build"])
    dev_checks: list = field(default_factory=lambda: ["original-public-check"])
    grading: dict = field(default_factory=lambda: {
        "bootstrap": [], "golden_patch": {"sha256": "b" * 64},
        "commands": [{"command": "original-test", "parser": "regex",
                      "expected_tests": ["original-15"], "success_pattern": "^15 passed$"}],
    })
    cpus: int = 4
    memory_mb: int = 8192

    @property
    def fingerprint(self):
        return canonical_hash(asdict(self))


def pointer(path):
    return {"path": str(path), "sha256": file_hash(path)}


def write(path, value):
    path.write_text(json.dumps(value))
    return pointer(path)


def asset(root, path):
    # No remote is accessed by these synthetic descriptor/schema unit controls.
    write(path, {
        "schema_version": "reviewbench-runtime-asset-tree/v1", "source_kind": "toolchain",
        "tree_sha256": tree_hash(root), "files": len(list(root.glob("*"))),
        "archive": {"schema_version": "reviewbench-chunked-archive/v1", "codec": "gzip",
                    "input_bytes": 1024, "compressed_bytes": 8,
                    "parts": [{"sha256": "f" * 64, "size_bytes": 8, "md5_base64": "AAAAAAAAAAAAAAAAAAAAAA=="}]},
    })
    return {**pointer(path), "kind": "archive", "visibility": "developer", "target": reuse.TRANSPORT_TARGET}


def commands(directory, values):
    directory.mkdir(parents=True)
    for number, command in enumerate(values):
        ident = str(number)
        out, err = directory / (ident + ".stdout"), directory / (ident + ".stderr")
        out.write_text("15 passed\n" if command == "original-test" else "original-command-output\n")
        err.write_text("")
        write(directory / (ident + ".json"), {
            "id": ident, "command": command, "status": "completed", "returncode": 0,
            "timed_out": False, "log_limit_exceeded": False,
            "stdout_file": out.name, "stderr_file": err.name,
            "stdout_sha256": file_hash(out), "stderr_sha256": file_hash(err),
        })


def fixture(tmp_path):
    task = {"id": "original-task", "start_sha": "a" * 40}
    oracle = {"id": task["id"], "original_tests": 15}
    engine = "e" * 64
    original = {"package.json": b'{"name":"original-root"}\n',
                "pnpm-lock.yaml": b"original-lock\n", "pnpm-workspace.yaml": b"packages: []\n"}
    layout = {
        "schema_version": "reviewbench-pnpm-workspace-dependencies/v1",
        "workspace_production_files_exported": 0,
        "input_hashes": {k: hashlib.sha256(v).hexdigest() for k, v in original.items()},
        "module_roots": ["node_modules"], "workspace_links": [], "external_index_sha256": "c" * 64,
    }
    old_root, new_root = tmp_path / "old-tool", tmp_path / "new-tool"
    for root in (old_root, new_root):
        root.mkdir()
        (root / "unchanged-helper").write_text("original tool input")
    (old_root / "transport.cjs").write_text(reuse.ORIGINAL_PREDICATE_SOURCE)
    (new_root / "transport.cjs").write_text(SOURCE)
    write(new_root / CONFIG_NAME, fixed_contract(layout, original))
    old = Recipe([asset(old_root, tmp_path / "old-asset.json")])
    new = Recipe([asset(new_root, tmp_path / "new-asset.json")])
    delta = reuse.validate_transport_change(old, new, old_root, new_root, layout, original)
    workspace = tmp_path / "state"
    workspace.mkdir()
    (workspace / "current-source").write_text("exact original starting source")
    state = reuse.snapshot_workspace(workspace)
    equivalence = {
        "schema_version": reuse.SCHEMA, "status": "input_specific_start_bootstrap_equivalence_verified",
        "task_fingerprint": canonical_hash(task), "oracle_fingerprint": canonical_hash(oracle),
        "engine_fingerprint": engine, "start_sha": task["start_sha"], "full_scope": 402,
        "new_SUT_build_test_model_judge_calls": 0,
        "old_tool_snapshot": str(old_root), "new_tool_snapshot": str(new_root),
        "original_layout": write(tmp_path / "layout.json", layout),
        "original_input_texts": write(tmp_path / "input-texts.json", {k: v.decode() for k, v in original.items()}),
        "transport_delta": delta,
        "old_state": write(tmp_path / "old-state.json", state),
        "new_state": write(tmp_path / "new-state.json", state),
        "identical_complete_source_and_git_seed": True,
        "original_build_command_not_executed": "original-build",
        "old_commands": str(tmp_path / "old-prefix"), "new_commands": str(tmp_path / "new-prefix"),
    }
    for path in ("old-prefix", "new-prefix"):
        commands(tmp_path / path, old.bootstrap[:-1])
    equivalence_pointer = write(tmp_path / "equivalence.json", equivalence)
    tests = {"0::original-15": True}
    base = {"id": task["id"], "task_fingerprint": canonical_hash(task),
            "oracle_fingerprint": canonical_hash(oracle), "engine_fingerprint": engine}
    old_proof = {**base, "status": "blocked", "runtime_fingerprint": old.fingerprint,
                 "runs": [{"variant": "start", "repetition": i, "tests": tests} for i in range(3)]}
    golden_proof = {**base, "status": "original_golden_calibrations_complete",
                    "runtime_fingerprint": new.fingerprint,
                    "runs": [{"variant": "golden", "repetition": i, "tests": tests} for i in range(3)]}
    old_dir, golden_dir = tmp_path / "original-runs", tmp_path / "new-runs"
    commands(old_dir / "developer", [*old.bootstrap, *old.dev_checks])
    for i in range(3):
        commands(old_dir / ("start-" + str(i)), [*old.bootstrap, "original-test"])
        commands(golden_dir / ("golden-" + str(i)), [*new.bootstrap, "original-test"])
    return {
        "task": task, "oracle": oracle, "old": old, "new": new, "engine": engine,
        "old_root": old_root, "new_root": new_root, "layout": layout, "files": original,
        "equivalence": equivalence, "equivalence_pointer": equivalence_pointer,
        "old_pointer": write(tmp_path / "old-proof.json", old_proof),
        "golden_pointer": write(tmp_path / "golden-proof.json", golden_proof),
        "old_dir": old_dir, "golden_dir": golden_dir,
    }


def compose(f):
    return reuse.compose_readiness(
        f["old_pointer"], f["old_dir"], f["golden_pointer"], f["golden_dir"],
        f["equivalence_pointer"], f["task"], f["oracle"], f["old"], f["new"], f["engine"])


def test_combined_readiness_preserves_actual_old_and_new_runtime_provenance(tmp_path):
    f = fixture(tmp_path)
    original = Path(f["old_pointer"]["path"]).read_bytes()
    proof = compose(f)
    assert proof["status"] == "ready" and len(proof["runs"]) == 6
    assert proof["runtime_fingerprint"] == f["new"].fingerprint
    assert all(row["execution_provenance"]["actual_runtime_fingerprint"] == f["old"].fingerprint
               for row in proof["runs"][:3])
    assert all(row["execution_provenance"]["actual_runtime_fingerprint"] == f["new"].fingerprint
               for row in proof["runs"][3:])
    assert proof["calibration_provenance"]["replayed_successful_start_calibrations"] == 0
    assert Path(f["old_pointer"]["path"]).read_bytes() == original


@pytest.mark.parametrize("field", ["image", "env", "bootstrap", "grading", "cpus"])
def test_equivalence_never_covers_unrelated_recipe_changes(tmp_path, field):
    f = fixture(tmp_path)
    recipe = deepcopy(f["new"])
    setattr(recipe, field, {"changed": True} if field in {"env", "grading"} else
            ["different-build"] if field == "bootstrap" else 8 if field == "cpus" else "different-image")
    with pytest.raises(ContractError):
        reuse.validate_transport_change(f["old"], recipe, f["old_root"], f["new_root"], f["layout"], f["files"])


@pytest.mark.parametrize("change", ["script", "helper", "extra-file", "contract"])
def test_transport_change_must_be_the_exact_reviewed_readonly_predicate(tmp_path, change):
    f = fixture(tmp_path)
    if change == "script":
        (f["new_root"] / "transport.cjs").write_text(SOURCE + "\nfs.writeFileSync('/workspace/source','forged');\n")
    elif change == "helper":
        (f["new_root"] / "unchanged-helper").write_text("modified build tool")
    elif change == "extra-file":
        (f["new_root"] / "new-build-script").write_text("replacement")
    else:
        p = f["new_root"] / CONFIG_NAME
        value = json.loads(p.read_text())
        value["original_input_hashes"] = {}
        write(p, value)
    f["new"].assets[0] = asset(f["new_root"], tmp_path / "new-asset.json")
    with pytest.raises(ContractError):
        reuse.validate_transport_change(f["old"], f["new"], f["old_root"], f["new_root"], f["layout"], f["files"])


@pytest.mark.parametrize("change", ["state", "scope", "calls", "engine", "prefix"])
def test_input_specific_state_and_no_SUT_execution_are_required(tmp_path, change):
    f = fixture(tmp_path)
    e = f["equivalence"]
    if change == "state":
        p = Path(e["new_state"]["path"])
        value = json.loads(p.read_text())
        value["nodes"]["current-source"]["sha256"] = "0" * 64
        e["new_state"] = write(p, value)
    elif change == "scope":
        e["full_scope"] = 1
    elif change == "calls":
        e["new_SUT_build_test_model_judge_calls"] = 1
    elif change == "engine":
        e["engine_fingerprint"] = "0" * 64
    else:
        commands(tmp_path / "extra-prefix", [*f["old"].bootstrap])
        e["new_commands"] = str(tmp_path / "extra-prefix")
    f["equivalence_pointer"] = write(Path(f["equivalence_pointer"]["path"]), e)
    with pytest.raises(ContractError):
        compose(f)


@pytest.mark.parametrize("change", ["missing-start", "start-runtime", "old-output", "missing-golden", "golden-runtime", "golden-output"])
def test_all_original_and_fresh_executions_require_their_actual_logs_and_identities(tmp_path, change):
    f = fixture(tmp_path)
    if change in {"old-output", "golden-output"}:
        directory = f["old_dir"] / "start-0" if change == "old-output" else f["golden_dir"] / "golden-0"
        (directory / "3.stdout").write_text("0 passed\n")
    else:
        which = "old_pointer" if change.startswith(("missing-start", "start-")) else "golden_pointer"
        path = Path(f[which]["path"])
        proof = json.loads(path.read_text())
        if change.startswith("missing"):
            proof["runs"].pop()
        else:
            proof["runtime_fingerprint"] = "0" * 64
        f[which] = write(path, proof)
    with pytest.raises(ContractError):
        compose(f)
