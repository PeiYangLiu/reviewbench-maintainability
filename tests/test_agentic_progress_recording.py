from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import os
import subprocess
import sys

import pytest

FILE = Path(__file__).resolve().parents[1] / "scripts/record_agentic_progress.py"
SPEC = importlib.util.spec_from_file_location("progress_observer_under_test", FILE)
observer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(observer)
ENGINE = "a" * 64


def report(generations=192, resolved=91, missing=226, complete=False):
    return {
        "task_count": 402, "expected_pairs": 402, "complete": complete,
        "measured_generations": generations, "generation_engine_fingerprint": ENGINE,
        "leaderboard": {"model": {"tasks": 402, "complete": complete, "resolved": resolved,
                                  "missing_measurements": missing, "incomplete_gate_rows": missing,
                                  "generation_failed": 1, "votes": 442}},
    }


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


@pytest.fixture
def config(tmp_path):
    config = {
        "report": str(tmp_path / "report.json"), "source_report": str(tmp_path / "source.json"),
        "document": str(tmp_path / "progress.md"), "state": str(tmp_path / "state/state.json"),
        "timezone": "America/Los_Angeles", "expected_tasks": 402, "interval_seconds": 1800,
        "disk_path": str(tmp_path), "services": [], "extra_packages": [],
    }
    write(Path(config["report"]), report())
    write(Path(config["source_report"]), {"task_count": 402, "runtime_bound_tasks": 194})
    return config


def entries(config):
    path = Path(config["document"])
    return path.read_text().count("<!-- reviewbench-progress:") if path.exists() else 0


def proof(path, *, ident="task", engine=ENGINE, broken=False):
    row = {
        "id": ident, "engine_fingerprint": engine, "runtime_fingerprint": "b" * 64,
        "status": "ready",
        "runs": [{"variant": variant, "repetition": repetition, "tests": {"assertion": not broken}}
                 for variant in ("start", "golden") for repetition in range(3)],
    }
    write(path, row)
    return row


def test_periodic_record_is_immediate_then_bounded_to_half_hour_slots(config):
    assert observer.record(config, now=3600)["status"] == "recorded"
    for now in (3601, 3660, 5399):
        assert observer.record(config, now=now)["status"] == "unchanged"
    assert observer.record(config, now=5400)["status"] == "recorded"
    assert entries(config) == 2


def test_report_and_source_bytes_are_not_modified(config):
    paths = [Path(config["report"]), Path(config["source_report"])]
    before = [path.read_bytes() for path in paths]
    observer.record(config, now=3600)
    assert [path.read_bytes() for path in paths] == before
    text = Path(config["document"]).read_text()
    assert "192 / 402" in text and "complete=false" in text
    assert "| model | 91 | 226 | 226 | 1 | 442 |" in text


def test_human_notes_are_preserved_and_manual_events_are_deduplicated(config, tmp_path):
    path = Path(config["document"])
    path.write_text("# Human notes\nkeep this paragraph\n")
    evidence = tmp_path / "proof.json"
    write(evidence, {"observed": True})
    first = observer.record(config, now=3600, events=["verified milestone"], evidence=[evidence])
    second = observer.record(config, now=3660, events=["verified milestone"], evidence=[evidence])
    assert first["status"] == "recorded" and second["status"] == "unchanged"
    assert path.read_text().startswith("# Human notes\nkeep this paragraph\n")
    assert path.read_text().count("verified milestone") == 1


def test_changed_manual_evidence_creates_a_new_record(config, tmp_path):
    evidence = tmp_path / "proof.json"
    write(evidence, {"version": 1})
    observer.record(config, now=3600, events=["runtime"], evidence=[evidence])
    write(evidence, {"version": 2})
    observer.record(config, now=3660, events=["runtime"], evidence=[evidence])
    assert entries(config) == 2


def test_generation_and_resolved_milestones_are_not_delayed_until_periodic_slot(config):
    observer.record(config, now=3600)
    write(Path(config["report"]), report(generations=200, resolved=100))
    result = observer.record(config, now=3660)
    assert result["events"] == 2 and entries(config) == 2


def test_votes_only_change_does_not_write_a_record_every_minute(config):
    observer.record(config, now=3600)
    value = report()
    value["leaderboard"]["model"]["votes"] += 1
    write(Path(config["report"]), value)
    assert observer.record(config, now=3660)["status"] == "unchanged"


def test_generation_counter_regression_is_explicit(config):
    observer.record(config, now=3600)
    write(Path(config["report"]), report(generations=190))
    observer.record(config, now=3660)
    assert "模型生成计数回退" in Path(config["document"]).read_text()


def test_service_transition_is_a_key_event(config, monkeypatch):
    monkeypatch.setattr(observer, "service_states", lambda _: {"bench.service": {"ActiveState": "active"}})
    observer.record(config, now=3600)
    monkeypatch.setattr(observer, "service_states", lambda _: {"bench.service": {"ActiveState": "failed"}})
    observer.record(config, now=3660)
    assert "服务状态发生变化" in Path(config["document"]).read_text()


def test_repeated_service_failures_and_recovery_are_separate_events(config, monkeypatch):
    for index, state in enumerate(("active", "failed", "active", "failed")):
        monkeypatch.setattr(observer, "service_states",
                            lambda _, state=state: {"bench.service": {"ActiveState": state}})
        observer.record(config, now=3600 + index * 60)
    assert entries(config) == 4


def test_ready_proof_event_requires_exact_six_successful_runs(config, tmp_path):
    package = tmp_path / "package"
    config["extra_packages"] = [str(package)]
    observer.record(config, now=3600)
    path = package / "execution/preparation/verified/task/verification.json"
    proof(path)
    assert observer.record(config, now=3660)["events"] == 1
    assert "新增当前引擎六轮成功回放报告：task" in Path(config["document"]).read_text()


@pytest.mark.parametrize("mutation", ["old-engine", "failed-assertion", "five-runs", "duplicate-run"])
def test_incomplete_or_old_engine_proofs_are_not_successes(config, tmp_path, mutation):
    package = tmp_path / "package"
    config["extra_packages"] = [str(package)]
    path = package / "execution/preparation/verified/task/verification.json"
    row = proof(path)
    if mutation == "old-engine":
        row["engine_fingerprint"] = "c" * 64
    elif mutation == "failed-assertion":
        row["runs"][0]["tests"]["assertion"] = False
    elif mutation == "five-runs":
        row["runs"].pop()
    else:
        row["runs"][-1] = row["runs"][0]
    write(path, row)
    snapshot = observer.collect(config, 3600)
    assert snapshot["proofs"] == {}


def test_duplicate_proofs_count_one_task(config, tmp_path):
    for name in ("one", "two"):
        package = tmp_path / name
        config["extra_packages"].append(str(package))
        proof(package / "execution/preparation/verified/task/verification.json")
    observer.record(config, now=3600)
    assert "按任务去重）：1" in Path(config["document"]).read_text()


def test_missing_report_is_logged_as_unknown_not_zero_or_complete(config):
    observer.record(config, now=3600)
    Path(config["report"]).unlink()
    result = observer.record(config, now=3660)
    text = Path(config["document"]).read_text()
    assert result["observation_errors"] == 1 and entries(config) == 2
    assert "本条不推算任务进度或成功率" in text and "0 / 402" not in text


@pytest.mark.parametrize("field,value", [("task_count", 401), ("expected_pairs", 401),
                                        ("measured_generations", True)])
def test_invalid_denominator_and_boolean_counts_are_not_reported_as_valid(config, field, value):
    data = report()
    data[field] = value
    write(Path(config["report"]), data)
    snapshot = observer.collect(config, 3600)
    assert snapshot["metrics"] is None and snapshot["errors"]


def test_premature_complete_flag_is_not_trusted(config):
    write(Path(config["report"]), report(complete=True))
    snapshot = observer.collect(config, 3600)
    assert snapshot["metrics"] is None and "measurement gaps" in snapshot["errors"][0]


@pytest.mark.parametrize("value", [[], None, "text", {"runs": "text"}])
def test_malformed_proof_json_is_reported_without_crashing(config, tmp_path, value):
    package = tmp_path / "package"
    config["extra_packages"] = [str(package)]
    write(package / "execution/preparation/verified/task/verification.json", value)
    snapshot = observer.collect(config, 3600)
    assert snapshot["proofs"] == {} and snapshot["errors"]


@pytest.mark.parametrize("output,source", [("document", "report"), ("state", "source_report"),
                                         ("document", "state")])
def test_writer_outputs_never_alias_observation_inputs(config, output, source):
    config[output] = config[source]
    before = Path(config["report"]).read_bytes()
    with pytest.raises(ValueError, match="must differ"):
        observer.record(config, now=3600)
    assert Path(config["report"]).read_bytes() == before


def test_journal_symlink_is_not_followed(config, tmp_path):
    original = tmp_path / "original"
    original.write_text("retained")
    Path(config["document"]).symlink_to(original)
    with pytest.raises(ValueError, match="regular files"):
        observer.record(config, now=3600)
    assert original.read_text() == "retained"


def test_output_written_before_state_failure_is_not_duplicated_on_retry(config, monkeypatch):
    real = observer.atomic_json
    monkeypatch.setattr(observer, "atomic_json", lambda *args: (_ for _ in ()).throw(OSError("fixture failure")))
    with pytest.raises(OSError):
        observer.record(config, now=3600)
    monkeypatch.setattr(observer, "atomic_json", real)
    assert observer.record(config, now=3601)["status"] == "unchanged"
    assert entries(config) == 1


def test_two_writers_do_not_duplicate_a_periodic_entry(config):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: observer.record(config, now=3600), range(2)))
    assert sorted(row["status"] for row in results) == ["recorded", "unchanged"]
    assert entries(config) == 1


def test_state_binding_prevents_accidental_experiment_mixing(config, tmp_path):
    observer.record(config, now=3600)
    other = copy.deepcopy(config)
    other["report"] = str(tmp_path / "other-report.json")
    write(Path(other["report"]), report())
    with pytest.raises(ValueError, match="different experiment"):
        observer.record(other, now=3660)


def test_file_size_limits_are_enforced(config, monkeypatch):
    monkeypatch.setattr(observer, "MAX_JSON_BYTES", 10)
    assert observer.collect(config, 3600)["metrics"] is None


def test_journal_size_bound_preserves_existing_content(config, monkeypatch):
    Path(config["document"]).write_text("do not truncate these notes")
    monkeypatch.setattr(observer, "MAX_JOURNAL_BYTES", 10)
    with pytest.raises(ValueError, match="archive"):
        observer.record(config, now=3600)
    assert Path(config["document"]).read_text() == "do not truncate these notes"


def test_cli_creates_and_reopens_real_document(config, tmp_path):
    config_path = tmp_path / "config.json"
    write(config_path, config)
    result = subprocess.run([sys.executable, str(FILE), "--config", str(config_path),
                             "--event", "fixture milestone"], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["status"] == "recorded" and output["model_calls"] == 0
    assert "fixture milestone" in Path(config["document"]).read_text()


def bind_manifest(config, tmp_path, *, engine=ENGINE):
    path = tmp_path / "source/experiment-manifest.json"
    value = {
        "schema_version": "reviewbench-progressive-experiment/v1",
        "protocol": "reviewbench-agentic-exec/v1",
        "expected_pairs": 402, "models": ["model"],
        "task_ids": ["task"] + ["task-" + str(index) for index in range(401)],
        "engine_fingerprint": engine,
    }
    write(path, value)
    config["generation_manifest"] = str(path)
    config["generation_manifest_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    scope = tmp_path / "scope.ids"
    scope.write_text("\n".join(value["task_ids"]) + "\n")
    config["scope_ids"] = str(scope)
    return path


def omit_report_engine(config):
    value = json.loads(Path(config["report"]).read_text())
    value.pop("generation_engine_fingerprint")
    write(Path(config["report"]), value)


def test_overlay_missing_engine_uses_pinned_manifest_without_changing_report(config, tmp_path):
    omit_report_engine(config)
    manifest = bind_manifest(config, tmp_path)
    package = tmp_path / "package"
    config["extra_packages"] = [str(package)]
    proof(package / "execution/preparation/verified/task/verification.json")
    proof(package / "execution/preparation/verified/old/verification.json", engine="c" * 64)
    inputs = [Path(config["report"]), manifest]
    before = [path.read_bytes() for path in inputs]
    snapshot = observer.collect(config, 3600)
    assert snapshot["proof_engine"] == ENGINE and snapshot["metrics"]["engine"] is None
    assert snapshot["proof_inventory_status"] == "complete"
    assert len(snapshot["proofs"]) == 1 and not snapshot["errors"]
    assert any(row["path"] == str(manifest) for row in snapshot["evidence"])
    assert [path.read_bytes() for path in inputs] == before
    observer.record(config, now=3600)
    assert "按任务去重）：1" in Path(config["document"]).read_text()


def test_unknown_calibration_binding_is_not_reported_as_zero(config):
    omit_report_engine(config)
    result = observer.record(config, now=3600)
    text = Path(config["document"]).read_text()
    assert result["observation_errors"] == 1
    assert "按任务去重）：未知" in text and "按任务去重）：0" not in text
    assert "192 / 402" in text


def test_binding_restoration_does_not_claim_fresh_calibrations(config, tmp_path):
    omit_report_engine(config)
    package = tmp_path / "package"
    config["extra_packages"] = [str(package)]
    proof(package / "execution/preparation/verified/task/verification.json")
    observer.record(config, now=3600)
    bind_manifest(config, tmp_path)
    observer.record(config, now=3660)
    text = Path(config["document"]).read_text()
    assert "重读存量证明，未重跑校准" in text
    assert "新增当前引擎六轮成功回放报告" not in text


@pytest.mark.parametrize("mutation", [
    "missing-pin", "wrong-pin", "wrong-schema", "wrong-protocol", "wrong-engine",
    "wrong-models", "duplicate-models", "wrong-pairs", "wrong-scope", "duplicate-task",
    "malformed", "report-conflict",
])
def test_manifest_binding_errors_produce_unknown_counts(config, tmp_path, mutation):
    omit_report_engine(config)
    path = bind_manifest(config, tmp_path)
    value = json.loads(path.read_text())
    if mutation == "missing-pin":
        config.pop("generation_manifest_sha256")
    elif mutation == "wrong-pin":
        config["generation_manifest_sha256"] = "0" * 64
    elif mutation == "report-conflict":
        write(Path(config["report"]), report())
        value["engine_fingerprint"] = "c" * 64
    elif mutation == "wrong-schema":
        value["schema_version"] = "unrelated"
    elif mutation == "wrong-protocol":
        value["protocol"] = "other"
    elif mutation == "wrong-engine":
        value["engine_fingerprint"] = "not-a-fingerprint"
    elif mutation == "wrong-models":
        value["models"] = ["other"]
    elif mutation == "duplicate-models":
        value["models"] = ["model", "model"]
    elif mutation == "wrong-pairs":
        value["expected_pairs"] = 401
    elif mutation == "wrong-scope":
        value["task_ids"][-1] = "not-in-selected-scope"
    elif mutation == "duplicate-task":
        value["task_ids"][-1] = "task"
    else:
        value = []
    if mutation not in ("missing-pin", "wrong-pin"):
        write(path, value)
        config["generation_manifest_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    snapshot = observer.collect(config, 3600)
    assert snapshot["metrics"]["generations"] == 192
    assert snapshot["proofs"] == {} and snapshot["proof_engine"] is None
    assert snapshot["proof_inventory_status"] == "unknown" and snapshot["errors"]


@pytest.mark.parametrize("output", ["document", "state"])
def test_outputs_never_alias_generation_manifest(config, tmp_path, output):
    path = bind_manifest(config, tmp_path)
    config[output] = str(path)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="must differ"):
        observer.record(config, now=3600)
    assert path.read_bytes() == before


def test_partial_proof_read_is_explicit_instead_of_an_exact_zero(config, tmp_path):
    package = tmp_path / "package"
    config["extra_packages"] = [str(package)]
    write(package / "execution/preparation/verified/broken/verification.json", [])
    observer.record(config, now=3600)
    assert "至少 0（部分证明读取异常）" in Path(config["document"]).read_text()


def test_duplicate_scope_ids_are_rejected(config, tmp_path):
    bind_manifest(config, tmp_path)
    scope = Path(config["scope_ids"])
    scope.write_text(scope.read_text() + "task\n")
    snapshot = observer.collect(config, 3600)
    assert snapshot["proof_inventory_status"] == "unknown" and snapshot["errors"]


def rotating_report(config, tmp_path):
    owner = tmp_path / "owner"
    owner.mkdir()
    manifest = owner / "experiment-manifest.json"
    write(manifest, {"schema_version": "fixture-immutable-run/v1", "expected_pairs": 402})
    first = owner / "publications/one"
    second = owner / "publications/two"
    write(first / "report.json", report())
    write(second / "report.json", report(generations=200, resolved=100))
    (owner / "current").symlink_to("publications/one", target_is_directory=True)
    (owner / "report.json").symlink_to("current/report.json")
    config.update(report=str(owner / "report.json"), report_manifest=str(manifest),
                  report_manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest())
    return owner, first, second, manifest


def swap_current(owner, target="publications/two"):
    temporary = owner / ".next-current"
    temporary.symlink_to(target, target_is_directory=True)
    os.replace(temporary, owner / "current")


def test_pinned_report_identity_survives_epoch_swap_preserving_history(config, tmp_path):
    owner, first, second, manifest = rotating_report(config, tmp_path)
    observer.record(config, now=3600, events=["original note"])
    initial = json.loads(Path(config["state"]).read_text())
    assert initial["binding"]["report"] == str(owner / "report.json")
    swap_current(owner)
    observer.record(config, now=3660, events=["original note"])
    after = json.loads(Path(config["state"]).read_text())
    assert after["binding"] == initial["binding"]
    assert after["manual_seen"] == initial["manual_seen"]
    assert after["record_sequence"] == 2 and entries(config) == 2
    assert Path(config["document"]).read_text().count("original note") == 1
    assert after["observation"]["metrics"]["generations"] == 200
    assert {"path": str(manifest), "sha256": config["report_manifest_sha256"]} in after["observation"]["evidence"]
    assert after["observation"]["evidence"][0]["path"] == str(second / "report.json")


def test_pinned_report_reader_uses_one_target_during_pointer_swap(config, tmp_path, monkeypatch):
    owner, first, second, _ = rotating_report(config, tmp_path)
    collect = observer.collect
    def changed_after_binding(bound, now):
        swap_current(owner)
        return collect(bound, now)
    monkeypatch.setattr(observer, "collect", changed_after_binding)
    observer.record(config, now=3600)
    state = json.loads(Path(config["state"]).read_text())
    assert state["observation"]["metrics"]["generations"] == 192
    assert state["observation"]["evidence"][0]["path"] == str(first / "report.json")


@pytest.mark.parametrize("mutation", ["missing-path", "missing-hash", "bad-hash", "relative-path", "hash-change", "non-object"])
def test_stable_identity_requires_exact_regular_manifest(config, tmp_path, mutation):
    _, _, _, manifest = rotating_report(config, tmp_path)
    if mutation == "missing-path":
        config.pop("report_manifest")
    elif mutation == "missing-hash":
        config.pop("report_manifest_sha256")
    elif mutation == "bad-hash":
        config["report_manifest_sha256"] = "not-a-sha"
    elif mutation == "relative-path":
        config["report_manifest"] = "relative.json"
    elif mutation == "hash-change":
        manifest.write_text(manifest.read_text() + " ")
    else:
        write(manifest, [])
        config["report_manifest_sha256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="manifest"):
        observer.record(config, now=3600)
    assert not Path(config["state"]).exists() and not Path(config["document"]).exists()


def test_manifest_change_cannot_rebind_an_existing_observer_state(config, tmp_path):
    _, _, _, manifest = rotating_report(config, tmp_path)
    observer.record(config, now=3600)
    before = Path(config["document"]).read_bytes()
    write(manifest, {"schema_version": "another-run/v1"})
    config["report_manifest_sha256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="different experiment"):
        observer.record(config, now=3660)
    assert Path(config["document"]).read_bytes() == before


@pytest.mark.parametrize("target", ["manifest", "report", "parent"])
def test_stable_identity_rejects_foreign_symlinks(config, tmp_path, target):
    owner, _, _, manifest = rotating_report(config, tmp_path)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    if target == "manifest":
        (foreign / "manifest.json").write_bytes(manifest.read_bytes())
        manifest.unlink()
        manifest.symlink_to(foreign / "manifest.json")
    elif target == "report":
        write(foreign / "report.json", report())
        (owner / "report.json").unlink()
        (owner / "report.json").symlink_to(foreign / "report.json")
    else:
        config["report"] = str(owner / "current/report.json")
    with pytest.raises(ValueError, match="manifest"):
        observer.record(config, now=3600)


@pytest.mark.parametrize("output", ["document", "state"])
def test_observer_outputs_cannot_alias_pinned_report_manifest(config, tmp_path, output):
    _, _, _, manifest = rotating_report(config, tmp_path)
    config[output] = str(manifest)
    raw = manifest.read_bytes()
    with pytest.raises(ValueError, match="must differ"):
        observer.record(config, now=3600)
    assert manifest.read_bytes() == raw


def test_pinned_report_missing_epoch_is_unknown_not_another_experiment(config, tmp_path):
    owner, _, _, _ = rotating_report(config, tmp_path)
    observer.record(config, now=3600)
    swap_current(owner, "publications/not-yet-present")
    result = observer.record(config, now=3660)
    assert result["observation_errors"] == 1
    assert "本条不推算任务进度或成功率" in Path(config["document"]).read_text()


def test_legacy_symlink_identity_is_not_silently_migrated(config, tmp_path):
    owner, _, _, _ = rotating_report(config, tmp_path)
    config.pop("report_manifest")
    config.pop("report_manifest_sha256")
    observer.record(config, now=3600)
    swap_current(owner)
    with pytest.raises(ValueError, match="different experiment"):
        observer.record(config, now=3660)
