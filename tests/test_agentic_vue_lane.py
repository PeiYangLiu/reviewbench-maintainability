import copy
import hashlib
import json
from pathlib import Path
import shlex
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import scripted_lane, vue_lane as vue
from reviewbench_maintainability.agentic.contracts import ContractError


def pinned(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def case():
    return {
        "environment": {"node": "25.6.1", "vue": "3.3.13", "coverage": "c8/Istanbul"},
        "test_kind": "session_observable_behavior", "test_selectors": ["original renderer"],
        "setup_host_command": "mkdir -p {task_dir}/artifacts",
        "pass_to_pass": 1, "timeout": 300,
        "test_host_command": (
            "node {root}/tools/compile_vue_sfc.mjs {work}/src/Example.vue "
            "{task_dir}/artifacts/component-{mode}-{run}.cjs && "
            "{root}/cache/vue-oracle/node_modules/.bin/c8 --reporter=json "
            "node {root}/tools/render_vue_sfc.mjs {task_dir}/artifacts/component-{mode}-{run}.cjs "
            "'{{\"label\":\"value\"}}' 'Exact text'; rc=$?; if [ $rc -eq 0 ]; then "
            "python {root}/tools/istanbul_to_json.py INPUT {work}/src/Example.vue OUTPUT; fi; exit $rc"
        ),
    }


def task_oracle():
    task = {"id": "example", "repo": "example/source", "start_sha": "a" * 40,
            "locality": {"path": "src/Example.vue"}}
    oracle = {"index": 1, "anchor_path": "src/Example.vue",
              "pass_to_pass": 1, "test_selectors": ["original renderer"]}
    return task, oracle


def test_scripted_entry_routes_vue_before_rejecting_non_docker_original(tmp_path, monkeypatch):
    path = tmp_path / "scripted/oracle_config.json"
    pinned(path, {"tasks": {"1": case()}})
    ctx = SimpleNamespace(legacy=tmp_path)
    task, oracle = task_oracle()
    seen, expected = [], object()
    monkeypatch.setattr(vue, "migrate", lambda *args: seen.append(args) or expected)
    assert scripted_lane.migrate(ctx, task, oracle) is expected
    assert seen == [(ctx, task, oracle)]


def test_original_versions_target_selectors_and_script_only_flag():
    task, oracle = task_oracle()
    config = case()
    assert vue.case_target(task, oracle, config) == (
        "src/Example.vue", False, ["compile_vue_sfc.mjs", "istanbul_to_json.py", "render_vue_sfc.mjs"])
    config["test_host_command"] = config["test_host_command"].replace(".cjs && ", ".cjs --script-only && ")
    assert vue.case_target(task, oracle, config)[1] is True
    oracle["pass_to_pass_tests"] = oracle.pop("test_selectors")
    assert vue.case_target(task, oracle, config)[0] == "src/Example.vue"


@pytest.mark.parametrize("field,value", [
    ("timeout", 600), ("pass_to_pass", 2), ("setup_host_command", "npm install"),
    ("test_selectors", ["replacement renderer"]), ("session_files", ["test.js"]),
    ("test_kind", "source_inspection"), ("environment", {"node": "26", "vue": "3.3.13"}),
])
def test_original_case_changes_do_not_silently_gain_a_new_adapter(field, value):
    task, oracle = task_oracle()
    config = case()
    config[field] = value
    with pytest.raises(ContractError):
        vue.case_target(task, oracle, config)


@pytest.mark.parametrize("before,after", [
    ("src/Example.vue", "../Example.vue"),
    ("src/Example.vue", "src/Other.vue"),
    ("render_vue_sfc.mjs", "new_easy_renderer.mjs"),
    ("compile_vue_sfc.mjs", "source_scan.mjs"),
    ("istanbul_to_json.py", "skip_coverage.py"),
])
def test_changed_target_or_original_tools_fail_closed(before, after):
    task, oracle = task_oracle()
    config = case()
    config["test_host_command"] = config["test_host_command"].replace(before, after)
    with pytest.raises(ContractError):
        vue.case_target(task, oracle, config)


def test_relocation_changes_only_declared_paths_and_format_escapes():
    config = case()
    command = shlex.split(vue.relocated_command(config))
    assert command[:2] == ["bash", "-c"]
    expected = config["test_host_command"].format(
        root=vue.ROOT, work="/workspace", task_dir=vue.STATE, mode="candidate", run="1")
    assert command[2] == expected
    assert "'{\"label\":\"value\"}' 'Exact text'; rc=$?" in command[2]
    assert "if [ $rc -eq 0 ]" in command[2]
    assert command[2].endswith("fi; exit $rc")


def test_host_path_is_not_carried_into_container():
    config = case()
    config["test_host_command"] += "; cat /home/host/file"
    with pytest.raises(ContractError, match="host-only"):
        vue.relocated_command(config)


def test_input_pins_reject_missing_relative_redirected_and_changed_bytes(tmp_path):
    ref = pinned(tmp_path / "manifest.json", {"value": 1})
    assert vue.read_pin(ref) == {"value": 1}
    for bad in (None, {}, {**ref, "sha256": "bad"}, {**ref, "path": "relative"}):
        with pytest.raises(ContractError):
            vue.read_pin(bad)
    link = tmp_path / "link.json"
    link.symlink_to(ref["path"])
    with pytest.raises(ContractError):
        vue.read_pin({**ref, "path": str(link)})
    Path(ref["path"]).write_text("{}")
    with pytest.raises(ContractError):
        vue.read_pin(ref)


def test_pending_original_source_does_not_trigger_source_rewriting(tmp_path):
    task, oracle = task_oracle()
    capture = pinned(tmp_path / "capture.json", {
        "status": "all_eight_original_Vue_input_routes_inspected", "tasks": {},
        "pending": {"1": {"error": "original escaping link"}},
    })
    document = pinned(tmp_path / "manifest.json", {"schema_version": vue.SCHEMA, "capture": capture})
    ctx = SimpleNamespace(config={"migration": {"vue_original_inputs": document}})
    with pytest.raises(ContractError, match="pending; no source link is rewritten"):
        vue.original_inputs(ctx, task, oracle, case())


def test_prepared_original_task_drift_is_not_accepted(tmp_path):
    task, oracle = task_oracle()
    row = {
        "task_id": task["id"], "task": pinned(tmp_path / "task.json", task),
        "oracle": pinned(tmp_path / "oracle.json", oracle),
        "plan": pinned(tmp_path / "plan.json", case()),
    }
    capture = pinned(tmp_path / "capture.json", {
        "status": "all_eight_original_Vue_input_routes_inspected", "tasks": {"1": row},
    })
    document = pinned(tmp_path / "manifest.json", {"schema_version": vue.SCHEMA, "capture": capture})
    ctx = SimpleNamespace(config={"migration": {"vue_original_inputs": document}})
    changed = copy.deepcopy(task)
    changed["start_sha"] = "b" * 40
    with pytest.raises(ContractError, match="original release"):
        vue.original_inputs(ctx, changed, oracle, case())


@pytest.mark.parametrize("field,value", [
    ("status", "uploaded"), ("asset", {"path": "/other", "sha256": "b" * 64}),
    ("all_paths_bytes_full_modes_verified", False), ("gzip_crc_and_eof_verified", False),
])
def test_head_or_local_archive_presence_does_not_substitute_independent_blob_get(field, value):
    asset = {"path": "/original", "sha256": "a" * 64}
    preparation = {"status": "prepared", "asset": asset}
    readback = {
        "status": "independent_Blob_component_readback_verified", "asset": asset,
        "all_paths_bytes_full_modes_verified": True, "gzip_crc_and_eof_verified": True,
    }
    readback[field] = value
    with pytest.raises(ContractError, match="independent Blob"):
        vue._component(object(), preparation, readback, status="prepared", role="dependency")


def test_public_checker_contains_no_original_grader_boundary_implementation():
    text = Path(vue.__file__).with_name("vue_public_check.mjs").read_text()
    for token in ("compile_vue_sfc.mjs", "render_vue_sfc.mjs", "oracleCallable",
                  "ORACLE_PASS", "expected", "createSSRApp", "importedNames"):
        assert token not in text
    assert "compileScript(descriptor" in text and "compileTemplate({" in text
