"""Read-only first-lock admission controls; no VFS Cargo/build/test invocation."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_overlay, rust_vfs_resolution as resolution
from agentic_runtime_migration import rust_vfs_resolution_entry as entry
from reviewbench_maintainability.agentic.contracts import ContractError


def ref(path):
    data = path.read_bytes()
    return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def dump(path, value):
    path.write_text(json.dumps(value, sort_keys=True) + "\n")
    return ref(path)


@pytest.fixture
def original_config():
    name = os.environ.get("VFS_FIRST_LOCK_OPTIN")
    if not name:
        pytest.skip("explicit retained first-resolution evidence required; never resolve/download")
    return json.loads(Path(name).read_text())


@pytest.fixture
def capsule(tmp_path, original_config):
    config = copy.deepcopy(original_config)
    option = config["migration"][resolution.OPTION]
    receipt = json.loads(Path(option["receipt"]).read_text())
    def save():
        record = dump(tmp_path / "resolution.json", receipt)
        option.update(receipt=record["path"], receipt_sha256=record["sha256"])
        return config
    save()
    return config, receipt, save


def context(tmp_path, config):
    option = config["migration"][resolution.OPTION]
    receipt = json.loads(Path(option["receipt"]).read_text())
    data = json.loads(Path(receipt["original_task_input_json"]["path"]).read_text())
    toolchain = json.loads(Path(receipt["toolchain_binding"]["path"]).read_text())
    config = {**config, "rust_toolchains": toolchain["registry_path"]}
    output = tmp_path / "output"
    output.mkdir(exist_ok=True)
    legacy = Path(data["golden"]["path"]).parents[3]
    def copy_asset(path, category):
        target = output / category / path.name
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(path, target)
        return ref(target)
    ctx = SimpleNamespace(config=config, legacy=legacy, output=output, scratch=tmp_path,
                          repo=lambda _: legacy.parent / "repos/manuel-woelker__rust-vfs",
                          copy_asset=copy_asset)
    return ctx, data


@pytest.mark.parametrize("task", ["foreign", "frost106", "wash569", ""])
def test_configured_single_task_never_enters_other_rust_profiles(task):
    ctx = SimpleNamespace(config={"migration": {resolution.OPTION: {"enabled": True, "task_id": entry.TASK}}})
    assert not resolution.requested(ctx, {"id": task})


@pytest.mark.parametrize("config", [{}, {"migration": {}}, {"migration": {
    resolution.OPTION: {"enabled": False}}}])
def test_absent_or_disabled_optin_keeps_original_prepare_rejection(config):
    ctx = SimpleNamespace(config=config)
    assert not resolution.requested(ctx, {"id": entry.TASK})
    with pytest.raises(ContractError, match="non-lock build adapter"):
        rust_overlay.build_specification(ctx, {"id": entry.TASK}, {"prepare_command": entry.PREPARE}, {}, {})


@pytest.mark.parametrize("enabled", [None, 1, "true", [], {}])
def test_optin_is_an_explicit_boolean(enabled):
    with pytest.raises(ContractError):
        resolution.requested(SimpleNamespace(config={"migration": {
            resolution.OPTION: {"enabled": enabled}}}), {"id": entry.TASK})


def test_complete_retained_receipt_recomputes_both_graphs_and_all_archive_hashes(original_config):
    receipt, marker, archives = resolution.read_receipt(original_config)
    assert len(archives) == 101 and receipt["lock_packages"] == 102
    assert marker["historical_restoration"] is False
    assert marker["lock_sha256"] == entry.LOCK and marker["graph_sha256"] == entry.GRAPH
    assert marker["original_prepare_command"] == entry.PREPARE
    assert all(path.is_file() and entry.sha(path) == digest for (_, _, digest), path in archives.items())
    output = os.environ.get("VFS_FIRST_LOCK_EVIDENCE")
    if output:
        path = Path(output)
        path.mkdir(exist_ok=True)
        dump(path / "retained-receipt-audit.json", {
            "receipt_sha256": marker["receipt_sha256"], "first_new_lock": entry.LOCK,
            "complete_graphs": 2, "packages_per_graph": 102, "archive_files_verified": 101,
            "archive_bytes_read_only": sum(p.stat().st_size for p in archives.values()),
            "historical_restoration": False, "new_cargo_commands": 0, "downloads": 0,
            "model_calls": 0, "sut_or_public_runs": 0,
        })


def test_actual_original_overlay_has_only_missing_lock_and_keeps_original_plan(tmp_path, original_config):
    ctx, data = context(tmp_path, original_config)
    before = copy.deepcopy(data)
    spec, locks = rust_overlay.build_specification(
        ctx, data["task"], data["resolved_plan"], data["oracle"], data["golden"])
    entry.validate(spec)
    assert spec["manifests"] == spec["private_members"] == []
    assert spec["locks"][0]["replace_if_sha256"] == []
    assert spec["locks"][0]["allow_missing"] is True
    assert entry.sha(locks["Cargo.lock"]["path"]) == entry.LOCK
    assert data == before and data["resolved_plan"]["prepare_command"] == entry.PREPARE
    public = json.dumps(spec)
    assert "oracle_start" not in public and "oracle_golden" not in public
    assert "in_memory_async_path_round_trips_file_contents" not in public
    assert not any(k in spec for k in ("frozen_lock_recovery", "selection", "golden"))


@pytest.mark.parametrize("field,value", [
    ("task_id", "other"), ("input_index", 62), ("status", "first_success_only"),
    ("manifest_sha256", "0" * 64), ("original_golden_sha256", "0" * 64),
    ("original_plan_sha256", "0" * 64), ("original_start_sha", "0" * 40),
    ("original_prepare_command_verbatim", "cargo update"),
    ("historical_restoration", True), ("historical_lock_absence_verified", False),
    ("new_update_invocations", 2), ("selection_uses_test_results", True),
    ("model_calls", 1), ("sut_builds_checks_tests", 1), ("public_checks", 1),
    ("metadata_has_full_lock_graph", False), ("required_tokio_stream", "0.1.17"),
    ("start_golden_normalized_graph_sha256", "0" * 64),
])
def test_changed_receipt_fields_fail_even_with_a_new_valid_envelope_hash(capsule, field, value):
    _, receipt, save = capsule
    receipt[field] = value
    with pytest.raises(ContractError):
        resolution.read_receipt(save())


@pytest.mark.parametrize("change", ["task", "disabled", "missing_receipt", "receipt_hash",
                                  "decision_hash", "extra_outcome"])
def test_wrong_unbound_or_outcome_dependent_optin_is_rejected(capsule, change):
    config, _, _ = capsule
    opt = config["migration"][resolution.OPTION]
    if change == "task":
        opt["task_id"] = "other"
    elif change == "disabled":
        opt["enabled"] = False
    elif change == "missing_receipt":
        opt["receipt"] += ".absent"
    elif change in {"receipt_hash", "decision_hash"}:
        opt[change.replace("_hash", "_sha256")] = "0" * 64
    else:
        opt["choose_if_tests_pass"] = True
    with pytest.raises(ContractError):
        resolution.read_receipt(config)


@pytest.mark.parametrize("change", [
    "first_unsuccessful", "first_hash", "pin", "repeat_prepare", "raw_prepare_failed",
    "raw_prepare_signal", "raw_prepare_old_lock", "raw_prepare_changed_source",
    "graph_missing", "graph_empty", "graph_nodes", "graph_stream_hash",
    "old_present_lock", "source_manifest", "archive_missing", "archive_bad_hash",
])
def test_nested_raw_evidence_not_ready_booleans_controls_admission(tmp_path, capsule, change):
    _, receipt, save = capsule
    if change in {"first_unsuccessful", "first_hash", "pin"}:
        d = resolution.reference(receipt["first_success"])
        key, value = {"first_unsuccessful": ("update_exit_status", 1),
                      "first_hash": ("lock_sha256", "0" * 64),
                      "pin": ("tokio_stream_versions", ["0.1.17"])}[change]
        d[key] = value
        receipt["first_success"] = dump(tmp_path / "first.json", d)
    elif change == "repeat_prepare":
        receipt["cargo_runs"].append(copy.deepcopy(receipt["cargo_runs"][0]))
    elif change.startswith("raw_prepare"):
        row = receipt["cargo_runs"][0]
        d = resolution.reference(row["result"])
        key, value = {"raw_prepare_failed": ("exit_status", 1), "raw_prepare_signal": ("exit_status", -9),
                      "raw_prepare_old_lock": ("lock_before_sha256", entry.LOCK),
                      "raw_prepare_changed_source": ("source_after_matches_original", False)}[change]
        d[key] = value
        row["result"] = dump(tmp_path / "command.json", d)
    elif change in {"old_present_lock", "source_manifest"}:
        d = resolution.reference(receipt["original_input_proof"])
        if change == "old_present_lock":
            d["historical_lock_present"] = True
        else:
            d["complete_pre_harness_source_files"]["start"]["Cargo.toml"]["sha256"] = "0" * 64
        receipt["original_input_proof"] = dump(tmp_path / "original.json", d)
    elif change in {"archive_missing", "archive_bad_hash"}:
        d = resolution.reference(receipt["archive_checksums"])
        if change == "archive_missing":
            d["rows"].pop()
        else:
            d["rows"][0]["sha256"] = "0" * 64
        receipt["archive_checksums"] = dump(tmp_path / "archives.json", d)
    elif change in {"graph_missing", "graph_empty"}:
        if change == "graph_missing":
            receipt["metadata"]["path"] += ".absent"
        else:
            d = resolution.reference(receipt["metadata"])
            d["proofs"] = []
            receipt["metadata"] = dump(tmp_path / "metadata.json", d)
    elif change == "graph_stream_hash":
        receipt["cargo_runs"][1]["stdout"]["sha256"] = "0" * 64
    else:
        row = receipt["cargo_runs"][1]
        graph = resolution.reference(row["stdout"])
        graph["resolve"]["nodes"].pop()
        row["stdout"] = dump(tmp_path / "raw-graph.json", graph)
        raw = resolution.reference(row["result"])
        raw.update(stdout=row["stdout"]["path"], stdout_sha256=row["stdout"]["sha256"])
        row["result"] = dump(tmp_path / "raw-result.json", raw)
    with pytest.raises((ContractError, entry.OverlayError)):
        resolution.read_receipt(save())


@pytest.mark.parametrize("change", ["task", "oracle", "harness", "toolchain", "plan", "golden"])
def test_actual_original_contract_guards_remain_before_new_resolution(tmp_path, original_config, change):
    ctx, data = context(tmp_path, original_config)
    if change == "task":
        data["task"]["repo"] = "other/repo"
    elif change == "oracle":
        data["oracle"]["pass_to_pass_tests"] = ["substitute"]
    elif change == "harness":
        data["resolved_plan"]["session_files"][0]["content"] += "// not the original\n"
    elif change == "toolchain":
        data["resolved_plan"]["toolchain"] = "1.85.1"
    elif change == "plan":
        data["resolved_plan"]["prepare_command"] = ""
    else:
        data["golden"]["sha256"] = "0" * 64
    with pytest.raises(ContractError):
        resolution.build_specification(ctx, data["task"], data["resolved_plan"], data["oracle"], data["golden"])


@pytest.mark.parametrize("variant", ["start", "golden"])
def test_original_present_lock_is_not_replaced_by_new_lock(tmp_path, original_config, monkeypatch, variant):
    ctx, data = context(tmp_path, original_config)
    original = rust_overlay.reference_inputs(ctx, data["task"], data["golden"], ["Cargo.lock", "Cargo.toml"])
    original[variant]["Cargo.lock"] = b"an original present lock\n"
    monkeypatch.setattr(rust_overlay, "reference_inputs", lambda *a: original)
    with pytest.raises(ContractError, match="old lock"):
        resolution.build_specification(ctx, data["task"], data["resolved_plan"], data["oracle"], data["golden"])


def test_no_cargo_or_network_api_in_receipt_admission_module():
    import ast
    tree = ast.parse(Path(resolution.__file__).read_text())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not names & {"subprocess", "requests", "urllib", "urlopen", "evaluate", "judge"}


def test_copied_lock_cannot_drift_before_vendor_even_if_graph_is_unchanged(tmp_path, original_config):
    ctx, data = context(tmp_path, original_config)
    spec, locks = resolution.build_specification(
        ctx, data["task"], data["resolved_plan"], data["oracle"], data["golden"])
    path = Path(locks["Cargo.lock"]["path"])
    path.write_bytes(path.read_bytes() + b"\n# altered copy\n")
    with pytest.raises(ContractError, match="copied first-lock asset changed"):
        resolution.retained_archives(ctx, spec, locks)


def test_normal_migrate_dispatch_consumes_receipt_without_resolution_or_original_command_execution(
        tmp_path, original_config, monkeypatch):
    from agentic_runtime_migration import rust_api, rust_lane, rust_protoc
    ctx, data = context(tmp_path, original_config)
    bundled = []
    def bundle(root, names=None, *, role="dependency"):
        names = names or [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()]
        bundled.append({"role": role, "names": names})
        path = ctx.output / (root.name + "-descriptor.json")
        path.write_text(json.dumps({"role": role, "names": names}))
        return ref(path)
    ctx.bundle = bundle
    ctx.task_directory = lambda *_: ctx.output
    ctx.golden = lambda *_: data["golden"]
    ctx.modules = lambda *_: []
    ctx.immutable_tests = lambda *_: ["src/test_macros.rs", "src/async_vfs/test_macros.rs"]
    ctx.image = lambda _: ("sha256:" + "a" * 64, {"path": "/fixture/image-audit.json", "sha256": "b" * 64})
    monkeypatch.setattr(rust_lane, "compiler", lambda *_: {"path": "/fixture/rust.json", "sha256": "c" * 64})
    vendored = []
    def vendor(ctx, task, plan, cwd, locks, **kwargs):
        assert plan == data["resolved_plan"] and cwd == ""
        assert len(resolution.retained_archives(ctx, kwargs["build_spec"], locks)) == 101
        vendored.append(entry.LOCK)
        return {"path": "/fixture/vendor.json", "sha256": "d" * 64}, None
    monkeypatch.setattr(rust_lane, "vendor", vendor)
    monkeypatch.setattr(rust_overlay, "cargo_cli_prefix", lambda *_: [])
    monkeypatch.setattr(rust_protoc, "binding", lambda *_: ([], {}, []))
    def build_tools(ctx):
        root = ctx.output / "rust-build-tools"
        root.mkdir(exist_ok=True)
        (root / "build.py").write_text("# existing overlay asset fixture\n")
        return ctx.bundle(root, role="toolchain")
    monkeypatch.setattr(rust_overlay, "build_tools", build_tools)
    recipe = rust_lane.migrate(ctx, data["task"], data["oracle"])
    assert vendored == [entry.LOCK]
    assert recipe.cpus == 4 and recipe.memory_mb == 8192 and recipe.build_scratch_mb == 24576
    assert recipe.command_timeout_seconds == 1800 and recipe.env["CARGO_BUILD_JOBS"] == "4"
    build_spec = json.loads(next((ctx.output / "rust-build-specs").glob("*.json")).read_text())
    assert build_spec["cpus"] == data["resolved_plan"]["build_jobs"] == 4
    assert recipe.grading["golden_patch"]["sha256"] == data["golden"]["sha256"]
    assert recipe.grading["commands"][0]["expected_tests"] == data["oracle"]["pass_to_pass_tests"]
    assert len(recipe.dev_checks) == 1 and entry.TOOLS in recipe.dev_checks[0]
    assert "rust_vfs_entry.py" in recipe.grading["commands"][0]["command"]
    assert all("update" not in c for c in recipe.bootstrap + recipe.dev_checks
               + recipe.grading["bootstrap"] + [recipe.grading["commands"][0]["command"]])
    assert recipe.scratch_paths == []  # No lock injection into original /workspace.
    assert len([a for a in recipe.assets if a["target"].endswith("rust-lock-0")]) == 1
    assert all(a["visibility"] == "grader" for a in recipe.assets
               if a["target"] in {"/opt/reviewbench-assets/rust-vfs-spec.json",
                                  "/opt/reviewbench-assets/rust-vfs-tools",
                                  "/opt/reviewbench-assets/rust-tests"})
    assert any(b["role"] == "grader" for b in bundled)


def test_task_only_vendor_hook_uses_retained_archive_without_network_or_cargo(tmp_path, monkeypatch):
    import io
    import tarfile
    from agentic_runtime_migration import rust_lane
    archive = tmp_path / "dependency-1.0.0.crate"
    with tarfile.open(archive, "w:gz") as t:
        data = b"pub fn dependency() -> bool { true }\n"
        info = tarfile.TarInfo("dependency-1.0.0/src/lib.rs")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    checksum = ref(archive)["sha256"]
    lock = tmp_path / "Cargo.lock"
    lock.write_text('version=4\n[[package]]\nname="dependency"\nversion="1.0.0"\n'
                    'source="registry+https://github.com/rust-lang/crates.io-index"\n'
                    'checksum="' + checksum + '"\n')
    calls = []
    def archives(ctx, spec, locks):
        calls.append((spec, locks))
        return {("dependency", "1.0.0", checksum): archive}
    monkeypatch.setattr(resolution, "retained_archives", archives)
    monkeypatch.setattr(rust_lane, "crate_archive", lambda *_: pytest.fail("new archive lookup/download"))
    monkeypatch.setattr(rust_lane, "cargo_vendor", lambda *a, **k: pytest.fail("Cargo resolution"))
    def bundle(root):
        source = root / "dependency-1.0.0/src/lib.rs"
        assert source.read_bytes() == b"pub fn dependency() -> bool { true }\n"
        hashes = json.loads((root / "dependency-1.0.0/.cargo-checksum.json").read_text())
        assert hashes["package"] == checksum
        assert hashes["files"]["src/lib.rs"] == ref(source)["sha256"]
        return {"fixture": "dependency-only-source"}
    ctx = SimpleNamespace(scratch=tmp_path, bundle=bundle, output=tmp_path)
    spec = {entry.MARKER: {"fixture": True}, "locks": []}
    result = rust_lane.vendor(ctx, {"id": entry.TASK}, {}, "", {"Cargo.lock": ref(lock)},
                              build_spec=spec, oracle={})
    assert result == ({"fixture": "dependency-only-source"}, None) and len(calls) == 1
