"""Original two-graph binding and real local transport-selection controls."""
import copy
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import tsjs_legacy_yarn as legacy
from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash, file_hash


def pointer(path):
    return {"path": str(path), "sha256": file_hash(path)}


def write(path, value):
    path.write_text(json.dumps(value))
    return pointer(path)


def fixture(tmp_path):
    task = {"id": legacy.OPOSSUM, "repo": "opossum-tool/OpossumUI",
            "start_sha": "a" * 40, "target_sha": "b" * 40}
    oracle = {"id": task["id"], "historical_node": "16.20.2"}
    script = tmp_path / "runner.py"
    script.write_text("CONFIG = {23: ('18.20.8', 'yarn1', '')}\nPM = {'yarn1': ('yarn', '1.22.22')}\n")
    runner = write(tmp_path / "runner.json", {"runner": pointer(script)})
    logs, installs = [], {}
    for phase in ("start", "golden"):
        for number in (1, 2, 3):
            path = tmp_path / (phase + str(number) + ".log")
            path.write_text("$ " + shlex.join(["/original/node_modules/.bin/react-scripts", *legacy.ARGV[1:]])
                            + "\nTest Suites: 1 passed, 1 total\nTests: 2 passed, 2 total\n")
            logs.append({**pointer(path), "phase": phase, "run": number})
        path = tmp_path / (phase + "-install.log")
        path.write_text("$ " + shlex.join(legacy.INSTALL) + "\nexit=0 duration=1.0s\n")
        installs[phase] = pointer(path)
    patch = tmp_path / "reference.patch"
    patch.write_text("original-source-only-patch")
    hashes = {"start": {"package.json": "1" * 64, "yarn.lock": "2" * 64},
              "golden": {"package.json": "3" * 64, "yarn.lock": "4" * 64}}
    execution = {
        "status": "original_runner_accepted_argv_and_two_install_graphs_bound",
        "id": task["id"], "full_scope": 402, "original_node_version": "18.20.8",
        "published_node_version": "16.20.2", "accepted_test_argv": legacy.ARGV,
        "signature": legacy.SIGNATURE, "original_install_argv": legacy.INSTALL,
        "original_reinstall_golden": True, "new_install_calibration_model_judge_calls": 0,
        "runner_binding": runner, "original_logs": logs, "original_installations": installs,
        "input_hashes": hashes, "source_only_reference": pointer(patch),
    }
    preparations = {}
    for phase in ("start", "golden"):
        prepared = {
            "id": task["id"], "full_scope": 402,
            "status": "original_" + phase + "_dependencies_Blob_verified",
            "original_node": "18.20.8", "original_manager": "yarn@1.22.22",
            "original_install_argv": legacy.INSTALL,
            "install": {"original_install_argv": legacy.INSTALL, "exit_status": 0},
            "original_sources_unchanged": True, "workspace_production_files_exported": 0,
            "calibration_model_judge_calls": 0, "input_hashes": hashes[phase],
            "module_roots": ["node_modules"], "workspace_links": [],
            "node": {"path": "original-node"}, "manager": {"path": "original-yarn"},
        }
        preparations[phase] = write(tmp_path / (phase + "-prepared.json"), prepared)
    value = {
        "id": task["id"], "full_scope": 402, "schema_version": "reviewbench-legacy-yarn-two-graphs/v1",
        "status": "both_original_dependency_graphs_ready",
        "base_inputs": write(tmp_path / "base.json", {
            "task_fingerprint": canonical_hash(task), "oracle_fingerprint": canonical_hash(oracle),
            "node_version": "16.20.2",
        }),
        "execution_binding": write(tmp_path / "execution.json", execution),
        "preparations": preparations,
    }
    path = tmp_path / "inputs.json"
    write(path, value)
    return task, oracle, value, path


def test_two_original_graphs_bind_without_changing_published_node(tmp_path):
    task, oracle, value, path = fixture(tmp_path)
    result, execution, preparations = legacy.read_inputs(task, oracle, pointer(path))
    assert result == value and oracle["historical_node"] == "16.20.2"
    assert execution["accepted_test_argv"] == legacy.ARGV
    assert set(preparations) == {"start", "golden"}
    assert preparations["start"]["input_hashes"] != preparations["golden"]["input_hashes"]


@pytest.mark.parametrize("field,value", [
    ("original_node_version", "16.20.2"), ("published_node_version", "18.20.8"),
    ("original_reinstall_golden", False), ("full_scope", 1),
    ("accepted_test_argv", ["react-scripts", "test", "other-test"]),
    ("signature", "jest:suites=1,tests=1"), ("new_install_calibration_model_judge_calls", 1),
    ("original_install_argv", [*legacy.INSTALL, "--ignore-engines"]),
])
def test_relabelled_version_selector_install_and_test_rules_rejected(tmp_path, field, value):
    task, oracle, inputs, path = fixture(tmp_path)
    p = Path(inputs["execution_binding"]["path"])
    execution = json.loads(p.read_text())
    execution[field] = value
    inputs["execution_binding"] = write(p, execution)
    write(path, inputs)
    with pytest.raises(ContractError):
        legacy.read_inputs(task, oracle, pointer(path))


@pytest.mark.parametrize("field,value", [
    ("status", "prepared"), ("original_node", "20.19.5"),
    ("original_sources_unchanged", False), ("workspace_production_files_exported", 1),
    ("calibration_model_judge_calls", 1), ("input_hashes", {}),
    ("workspace_links", [{"path": "node_modules/source", "workspace_target": "frozen-source"}]),
    ("module_roots", ["node_modules", "target-sources"]),
])
def test_unverified_or_source_contaminated_graph_is_rejected(tmp_path, field, value):
    task, oracle, inputs, path = fixture(tmp_path)
    p = Path(inputs["preparations"]["golden"]["path"])
    preparation = json.loads(p.read_text())
    preparation[field] = value
    inputs["preparations"]["golden"] = write(p, preparation)
    write(path, inputs)
    with pytest.raises(ContractError):
        legacy.read_inputs(task, oracle, pointer(path))


@pytest.mark.parametrize("change", ["missing", "duplicate", "bytes", "selector", "count", "runner"])
def test_original_replay_and_runner_bytes_are_bound(tmp_path, change):
    task, oracle, inputs, path = fixture(tmp_path)
    p = Path(inputs["execution_binding"]["path"])
    execution = json.loads(p.read_text())
    if change == "missing":
        execution["original_logs"].pop()
    elif change == "duplicate":
        execution["original_logs"][-1] = copy.deepcopy(execution["original_logs"][0])
    elif change == "runner":
        script = tmp_path / "runner.py"
        script.write_text("CONFIG = {23: ('16.20.2', 'yarn1', '')}\nPM = {'yarn1': ('yarn', '1.22.22')}\n")
        execution["runner_binding"] = write(tmp_path / "runner.json", {"runner": pointer(script)})
    else:
        row = execution["original_logs"][0]
        log = Path(row["path"])
        text = log.read_text()
        log.write_text("changed" if change == "bytes" else text.replace(
            "ProjectStatisticsPopup.test.tsx" if change == "selector" else "2 passed, 2 total",
            "another.test.tsx" if change == "selector" else "1 passed, 1 total"))
        if change != "bytes":
            row["sha256"] = file_hash(log)
    inputs["execution_binding"] = write(p, execution)
    write(path, inputs)
    with pytest.raises(ContractError):
        legacy.read_inputs(task, oracle, pointer(path))


@pytest.mark.parametrize("change", ["files", "bytes", "part", "role"])
def test_independent_asset_readback_must_cover_the_original_graph(tmp_path, monkeypatch, change):
    part = {"sha256": "a" * 64, "size_bytes": 10, "md5_base64": "b"}
    descriptor = {"source_kind": "dependency", "files": 2,
                  "archive": {"compressed_bytes": 10, "parts": [part]}}
    prepared = {
        "node": "node", "manager": "manager", "dependencies": "dependencies",
        "independent_readback": {"status": "independent_Blob_bytes_modes_links_crc_verified",
                                 "files": 2, "bytes": 10, "receipts": [dict(part)]},
    }
    if change == "files":
        prepared["independent_readback"]["files"] = 1
    elif change == "bytes":
        prepared["independent_readback"]["bytes"] = 9
    elif change == "part":
        prepared["independent_readback"]["receipts"][0]["sha256"] = "0" * 64
    else:
        descriptor["source_kind"] = "source"
    monkeypatch.setattr(legacy, "asset_descriptor", lambda pointer, root:
                        descriptor if pointer == "dependencies" else {"source_kind": "toolchain"})
    class Context:
        output = tmp_path
    with pytest.raises(ContractError):
        legacy.check_assets(Context(), prepared)


@pytest.mark.parametrize("variant,phase,code", [
    ("start", "start", 0), ("golden", "golden", 0), ("partial", "start", 0),
    ("other", "start", 0), ("symlink", None, 2), ("transport-failure", "golden", 7),
])
def test_real_local_selector_preserves_inputs_and_exact_exit_status(tmp_path, variant, phase, code):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is not installed on this unit-test host")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original = {"package.json": b'{"name":"original","dependencies":{"x":"2"}}\n',
                "yarn.lock": b"original-target-lock\n"}
    write(tmp_path / "golden-inputs.json", {
        name: hashlib.sha256(data).hexdigest() for name, data in original.items()
    })
    for name, data in original.items():
        (workspace / name).write_bytes(data)
    if variant in {"start", "other"}:
        (workspace / "package.json").write_bytes(b'{"name":"original","dependencies":{"x":"1"}}\n')
        (workspace / "yarn.lock").write_bytes(b"original-start-lock\n")
    elif variant == "partial":
        (workspace / "package.json").write_bytes(b'{"name":"original","dependencies":{"x":"1"}}\n')
    elif variant == "symlink":
        (workspace / "yarn.lock").unlink()
        (tmp_path / "external.lock").write_bytes(original["yarn.lock"])
        (workspace / "yarn.lock").symlink_to(tmp_path / "external.lock")
    start = tmp_path / "start.cjs"
    golden = tmp_path / "golden.cjs"
    start.write_text("console.log('START_TRANSPORT'); process.exit(0);\n")
    golden.write_text("console.log('GOLDEN_TRANSPORT'); process.exit("
                      + ("7" if variant == "transport-failure" else "0") + ");\n")
    selector = tmp_path / "select.cjs"
    selector.write_text(legacy.SELECTOR_SOURCE)
    before = {name: (workspace / name).read_bytes() for name in original}
    process = subprocess.run(
        [node, str(selector), str(start), "start-payload", str(golden), "golden-payload", str(workspace)],
        capture_output=True, text=True, timeout=15)
    assert process.returncode == code, process.stderr
    if phase:
        assert "LEGACY_YARN_GRAPH phase=" + phase + " source_metadata_rewritten=false\n" in process.stdout
        assert phase.upper() + "_TRANSPORT\n" in process.stdout
    else:
        assert "LEGACY_YARN_GRAPH_ERROR" in process.stderr
        assert "_TRANSPORT" not in process.stdout
    assert {name: (workspace / name).read_bytes() for name in original} == before
