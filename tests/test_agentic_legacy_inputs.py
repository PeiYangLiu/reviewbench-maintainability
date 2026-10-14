"""Small on-disk fixtures only: no containers, builds, caches, or network."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import legacy_inputs as legacy


def sha(data):
    return hashlib.sha256(data).hexdigest()


def put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    return path


def jsonl(path, *rows):
    return put(path, "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))


class Sample:
    def __init__(self, tmp):
        self.tmp = tmp
        self.release = tmp / "release"
        self.session = tmp / "session"
        self.oracle_root = self.session / "hard_oracle"
        self.home = tmp / "home"
        for directory in (self.release, self.oracle_root, self.home):
            directory.mkdir(parents=True)
        self.roots = {"release": self.release, "session": self.session, "oracle": self.oracle_root}
        self.task = {"id": "legacy-task", "repo": "org/repo", "start_sha": "a" * 40,
                     "target_sha": "b" * 40, "inferred_language": "Java"}
        self.relative_dir = "strict_screen_20260726/oracle_recovery/java/task"
        self.directory = self.session / self.relative_dir
        self.patch = put(self.directory / "source-only.patch", b"original patch bytes\n")
        self.harness = put(self.directory / "session-tests/Test.java", b"assertEquals(7, publicAPI());\n")
        self.source = {k: self.task[k] for k in ("id", "repo", "start_sha", "target_sha")}
        self.source.update({
            "behavior_assertions": ["public API returns exactly 7"],
            "pass_to_pass_tests": ["Test::publicAPI"], "pass_to_pass": 1,
            "artifacts": {"directory": self.relative_dir},
            "source_only_patch": {"artifact": f"{self.relative_dir}/source-only.patch",
                                  "sha256": sha(self.patch.read_bytes())},
            "session_behavior_tests": [f"{self.relative_dir}/session-tests/Test.java"],
            "command": "bash ./gradlew test --tests 'Test.publicAPI'",
            "build_system": "gradle", "environment": {"container": "oracle-java-tools:17"},
        })
        for phase in ("start", "golden"):
            self.source[phase + "_runs"] = []
            for number in (1, 2, 3):
                relative = f"{self.relative_dir}/{phase}-{number}.log"
                put(self.session / relative, f"Test::publicAPI PASS ({phase} {number})\n")
                self.source[phase + "_runs"].append({"run": number, "exit_code": 0, "passed": 1, "log": relative})
        self.source_file = self.session / "strict_screen_20260726/oracle_results/java.jsonl"
        self.provenance = {"file": "oracle_results/java.jsonl", "line": 1, "lane": "java", "paths_sanitized": True}
        self.sources = {}
        self.secondary = None
        self.sync()

    def sync(self, *, write_source=True):
        self.oracle = {**deepcopy(self.source), "release_oracle_provenance": deepcopy(self.provenance),
                       "golden_correctness_ok": True, "resolved": True}
        self.save(write_source=write_source)

    def save(self, *, write_source=True):
        self.tasks = jsonl(self.release / "tasks.jsonl", self.task)
        self.oracles = jsonl(self.release / "oracles.jsonl", self.oracle)
        self.scope = put(self.release / "scope.ids", self.task["id"] + "\n")
        if write_source:
            jsonl(self.source_file, self.source)

    def capture(self):
        return legacy.capture_inventory(
            tasks=self.tasks, oracles=self.oracles, scope_ids=self.scope, roots=self.roots,
            source_files=self.sources, secondary_contract=self.secondary,
        )

    def pin(self):
        document = self.capture()
        path = put(self.tmp / "inventory.json", json.dumps(document, sort_keys=True, indent=2) + "\n")
        return path, sha(path.read_bytes()), document


@pytest.fixture
def sample(tmp_path):
    return Sample(tmp_path)


def test_migration_entry_keeps_full_scope_and_does_not_promote_input_only_records(sample):
    from migrate_agentic_runtimes import legacy_inventory_for_scope
    path, digest, _ = sample.pin()
    tasks, oracles = {sample.task["id"]: sample.task}, {sample.oracle["id"]: sample.oracle}
    inventory = legacy_inventory_for_scope(tasks, oracles, path, digest)
    with pytest.raises(legacy.LegacyInputError, match="legacy_lane_adapter_required"):
        inventory.for_task(sample.task, sample.oracle).require_existing_lane()
    with pytest.raises(legacy.LegacyInputError, match="migration_scope_mismatch"):
        legacy_inventory_for_scope({}, {}, path, digest)
    changed = deepcopy(sample.oracle)
    changed["pass_to_pass_tests"] = ["Different::test"]
    with pytest.raises(legacy.LegacyInputError, match="task_or_oracle_changed"):
        legacy_inventory_for_scope(tasks, {sample.task["id"]: changed}, path, digest)


def test_capture_load_and_read_original_bytes_preserve_the_contract(sample):
    before_task, before_oracle = deepcopy(sample.task), deepcopy(sample.oracle)
    path, digest, document = sample.pin()
    inventory = legacy.load_inventory(path, expected_sha256=digest)
    loaded = legacy.load_legacy_inputs(sample.task, sample.oracle, inventory=inventory)
    row = loaded.row
    assert row["source"]["status"] == "original_record_bound"
    assert row["source"]["reference"]["line"] == 1
    assert row["source"]["reference"]["sha256"] == sha(sample.source_file.read_bytes())
    assert row["source"]["reference"]["record_sha256"] == legacy.canonical_sha256(sample.source)
    assert row["runtime_status"] == "not_verified"
    assert row["source_kind"] == "published_legacy"
    assert "lane_results_source" not in row["normalized_contract"]
    assert row["normalized_contract"]["behavior_assertions"] == sample.source["behavior_assertions"]
    assert row["normalized_contract"]["pass_to_pass_tests"] == sample.source["pass_to_pass_tests"]
    assert row["replay"]["six_original_signals_bound"] is True
    assert [(s["phase"], s["repetition"]) for s in row["replay"]["signals"]] == [
        (phase, n) for phase in ("start", "golden") for n in (1, 2, 3)]
    for signal in row["replay"]["signals"]:
        assert signal["raw"] == sample.source[signal["phase"] + "_runs"][signal["repetition"] - 1]
        assert loaded.read_evidence(signal["logs"][0]).endswith(b"\n")
    harness = next(r for r in row["evidence"] if r["role"] == "harness")
    assert loaded.read_evidence(harness) == sample.harness.read_bytes()
    assert sample.task == before_task and sample.oracle == before_oracle
    assert document["summary"]["runtime_verified"] == 0
    with pytest.raises(legacy.LegacyInputError, match="legacy_lane_adapter_required"):
        loaded.require_existing_lane()


@pytest.mark.parametrize("spelling,relative", [
    ("oracle_recovery/native/task/log", "strict_screen_20260726/oracle_recovery/native/task/log"),
    ("oracle_results/java.jsonl", "strict_screen_20260726/oracle_results/java.jsonl"),
    ("strict_screen_20260726/oracle_recovery/java/task/log", "strict_screen_20260726/oracle_recovery/java/task/log"),
    ("recovery/task/log", "recovery/task/log"),
    ("increment5-recovery/task/log", "increment5-recovery/task/log"),
    ("${REVIEWBENCH_SESSION_FILES}/recovery/task/log", "recovery/task/log"),
    ("${SESSION_FILES}/increment5-recovery/task/log", "increment5-recovery/task/log"),
    ("session files/increment5-recovery/task/log", "increment5-recovery/task/log"),
])
def test_documented_path_spellings_have_one_explicit_root(sample, spelling, relative):
    roots = legacy.Roots(sample.roots)
    assert roots.legacy_path(spelling) == sample.session / relative


def test_explicit_root_alias_is_canonicalized_but_child_links_still_fail(sample):
    alias = sample.tmp / "release-alias"
    alias.symlink_to(sample.release, target_is_directory=True)
    sample.roots["release"] = alias
    sample.tasks = alias / "tasks.jsonl"
    sample.oracles = alias / "oracles.jsonl"
    sample.scope = alias / "scope.ids"
    path, digest, document = sample.pin()
    assert document["roots"]["release"] == str(sample.release)
    assert document["capture_parameters"]["tasks"] == str(sample.release / "tasks.jsonl")
    assert legacy.load_inventory(path, expected_sha256=digest).document == document
    (sample.release / "child").symlink_to(sample.directory, target_is_directory=True)
    with pytest.raises(legacy.LegacyInputError, match="unsafe_file"):
        legacy.Roots(sample.roots).read("release", "child/source-only.patch")


def test_home_is_explicit_not_environment_or_session_fallback(sample, monkeypatch):
    monkeypatch.setenv("HOME", str(sample.home))
    roots = legacy.Roots(sample.roots)
    with pytest.raises(legacy.LegacyInputError, match="unbound_path_alias"):
        roots.legacy_path("${HOME}/recovery/task/log")
    roots = legacy.Roots({**sample.roots, "home": sample.home})
    assert roots.legacy_path("${HOME}/recovery/task/log") == sample.home / "recovery/task/log"
    assert roots.legacy_path("recovery/task/log") == sample.session / "recovery/task/log"


@pytest.mark.parametrize("path", [
    "../outside", "recovery/../../outside", "/etc/passwd", "recovery//test",
    "recovery/./test", r"recovery\..\outside", "C:/outside", "${UNBOUND}/file",
    "recovery/test\0log", "recovery/test\nlog", "~/recovery/test", "recovery/*.log",
    "recovery/{1..3}.log", "recovery/{1,2}.log",
])
def test_paths_reject_traversal_unbound_tokens_and_patterns(sample, path):
    with pytest.raises(legacy.LegacyInputError):
        legacy.Roots(sample.roots).legacy_path(path)


def test_sibling_with_root_prefix_is_outside_the_boundary(sample):
    with pytest.raises(legacy.LegacyInputError, match="path_boundary"):
        legacy.Roots(sample.roots).legacy_path(str(sample.session) + "-other/file")


@pytest.mark.parametrize("parent,external", [(False, False), (False, True), (True, False), (True, True)])
def test_symlinks_are_rejected_even_when_the_target_is_inside(sample, parent, external):
    roots = legacy.Roots(sample.roots)
    target_dir = sample.tmp / "outside" if external else sample.session / "real"
    target = put(target_dir / "value", b"do not follow\n")
    if parent:
        (sample.session / "link").symlink_to(target_dir, target_is_directory=True)
        relative = "link/value"
    else:
        (sample.session / "link").symlink_to(target)
        relative = "link"
    with pytest.raises(legacy.LegacyInputError, match="unsafe_file"):
        roots.read("session", relative)


def test_fifo_is_rejected_without_opening_a_blocking_stream(sample):
    os.mkfifo(sample.session / "fifo")
    with pytest.raises(legacy.LegacyInputError, match="not_regular_file"):
        legacy.Roots(sample.roots).read("session", "fifo")


def test_directory_is_not_a_file_or_a_content_hash(sample):
    with pytest.raises(legacy.LegacyInputError, match="not_regular_file"):
        legacy.Roots(sample.roots).read("session", "strict_screen_20260726")
    row = sample.capture()["rows"][0]
    directory = next(e for e in row["evidence"] if e["role"] == "directory")
    assert directory["state"] == "directory_present" and directory["sha256"] is None


@pytest.mark.parametrize("bad", ["", "a" * 63, "A" * 64, "sha256:" + "a" * 64, False, 123])
def test_inventory_requires_an_external_well_typed_digest(sample, bad):
    path, _, _ = sample.pin()
    with pytest.raises(legacy.LegacyInputError, match="inventory_sha256_required"):
        legacy.load_inventory(path, expected_sha256=bad)


def test_full_file_sha_covers_tail_and_not_mtime_or_size(sample):
    roots = legacy.Roots(sample.roots)
    data = b"x" * 10000 + b"original tail"
    path = put(sample.session / "payload", data)
    old = path.stat()
    assert roots.read("session", "payload", sha(data)) == data
    path.write_bytes(b"x" * 10000 + b"modified tail")
    os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
    with pytest.raises(legacy.LegacyInputError, match="sha256_mismatch"):
        roots.read("session", "payload", sha(data))


def test_size_limit_is_a_blocker_not_a_prefix_hash(sample, monkeypatch):
    put(sample.session / "large", b"x" * 129)
    monkeypatch.setattr(legacy, "MAX_FILE_BYTES", 128)
    with pytest.raises(legacy.LegacyInputError, match="file_too_large"):
        legacy.Roots(sample.roots).read("session", "large")


@pytest.mark.parametrize("line", [0, -1, True, "1", 900])
def test_provenance_lines_are_physical_one_based_and_never_guessed(sample, line):
    sample.provenance["line"] = line
    sample.sync()
    row = sample.capture()["rows"][0]
    assert row["source"]["status"] in {"invalid_provenance_line", "source_record_missing"}


def test_wrong_provenance_line_does_not_search_for_a_convenient_matching_id(sample):
    other = {**sample.source, "id": "another-task"}
    jsonl(sample.source_file, other, sample.source)
    assert sample.capture()["rows"][0]["source"]["status"] == "source_identity_mismatch"


def test_blank_lines_do_not_shift_physical_provenance_line_numbers(sample):
    sample.provenance["line"] = 2
    sample.sync()
    put(sample.source_file, b"\n" + sample.source_file.read_bytes())
    assert sample.capture()["rows"][0]["source"]["reference"]["line"] == 2


@pytest.mark.parametrize("key,value", [
    ("repo", "other/repo"), ("start_sha", "c" * 40), ("target_sha", "c" * 40),
    ("behavior_assertions", ["weaker assertion"]), ("pass_to_pass_tests", ["another-test"]),
    ("command", "true"), ("pass_to_pass", True),
])
def test_source_identity_and_all_source_owned_fields_are_checked(sample, key, value):
    original = deepcopy(sample.source)
    original[key] = value
    jsonl(sample.source_file, original)
    row = sample.capture()["rows"][0]
    assert row["source"]["status"] in {"source_identity_mismatch", "source_release_mismatch"}
    assert row["normalized_contract"].get(key) == sample.oracle.get(key)
    assert row["runtime_status"] == "not_verified"


def test_duplicate_json_keys_and_task_ids_are_not_last_wins(sample):
    put(sample.source_file, '{"id":"legacy-task","id":"another-task"}\n')
    assert sample.capture()["rows"][0]["source"]["status"] == "duplicate_key"
    jsonl(sample.source_file, sample.source, sample.source)
    assert sample.capture()["rows"][0]["source"]["status"] == "duplicate_id"
    jsonl(sample.tasks, sample.task, sample.task)
    with pytest.raises(legacy.LegacyInputError, match="duplicate_id"):
        sample.capture()


def test_named_source_artifact_requires_an_explicit_location_and_exact_identity(sample):
    sample.provenance = {"source_artifact": "old.jsonl", "paths_sanitized": True}
    sample.sync()
    row = sample.capture()["rows"][0]
    assert row["source"]["status"] == "missing_file"
    assert row["source"]["reference"]["absolute_path"] == str(sample.session / "old.jsonl")
    sample.sources = {"old.jsonl": sample.source_file}
    row = sample.capture()["rows"][0]
    assert row["source"]["status"] == "original_record_bound"


def test_sanitized_home_paths_compare_to_only_the_explicit_original_root(sample):
    sample.roots["home"] = sample.home
    sample.source["artifacts"]["directory"] = str(sample.home / "recovery/task")
    sample.provenance = {"source_artifact": "old.jsonl", "paths_sanitized": True}
    sample.sources = {"old.jsonl": sample.source_file}
    sample.sync()
    sample.oracle["artifacts"]["directory"] = "${HOME}/recovery/task"
    sample.save()
    assert sample.capture()["rows"][0]["source"]["status"] == "original_record_bound"


@pytest.mark.parametrize("where", ["source_only_patch", "provenance"])
def test_original_declared_sha256_is_not_replaced_by_an_observed_hash(sample, where):
    if where == "source_only_patch":
        sample.source[where]["sha256"] = "0" * 64
    else:
        sample.provenance["sha256"] = "0" * 64
    sample.sync()
    row = sample.capture()["rows"][0]
    if where == "source_only_patch":
        patch = next(r for r in row["evidence"] if r["role"] == "patch")
        assert patch["state"] == "sha256_mismatch"
        assert patch["sha256"] != patch["declared_sha256"]
    else:
        assert row["source"]["status"] == "sha256_mismatch"


@pytest.mark.parametrize("target", ["source", "patch", "harness", "log", "release", "scope"])
def test_pinned_inventory_reopens_each_original_input(sample, target):
    path, digest, _ = sample.pin()
    changed = {
        "source": sample.source_file, "patch": sample.patch, "harness": sample.harness,
        "log": sample.directory / "start-1.log", "release": sample.oracles, "scope": sample.scope,
    }[target]
    put(changed, changed.read_bytes() + b"\n")
    with pytest.raises(legacy.LegacyInputError):
        legacy.load_inventory(path, expected_sha256=digest)


def test_tampered_inventory_fails_even_if_attacker_rehashes_its_derived_claims(sample):
    path, digest, document = sample.pin()
    document["rows"][0]["runtime_status"] = "ready"
    put(path, json.dumps(document))
    with pytest.raises(legacy.LegacyInputError, match="inventory_sha256_mismatch"):
        legacy.load_inventory(path, expected_sha256=digest)
    with pytest.raises(legacy.LegacyInputError, match="inventory_evidence_changed"):
        legacy.load_inventory(path, expected_sha256=sha(path.read_bytes()))


def test_inventory_symlink_parents_are_rejected(sample):
    path, digest, _ = sample.pin()
    linked = sample.tmp / "alias"
    linked.symlink_to(path.parent, target_is_directory=True)
    with pytest.raises(legacy.LegacyInputError, match="unsafe_file"):
        legacy.load_inventory(linked / path.name, expected_sha256=digest)


def test_reappearing_or_disappearing_evidence_invalidates_old_blockers(sample):
    sample.harness.unlink()
    path, digest, _ = sample.pin()
    put(sample.harness, b"restored original assertions\n")
    with pytest.raises(legacy.LegacyInputError, match="inventory_evidence_changed"):
        legacy.load_inventory(path, expected_sha256=digest)
    path, digest, _ = sample.pin()
    sample.harness.unlink()
    with pytest.raises(legacy.LegacyInputError, match="inventory_evidence_changed"):
        legacy.load_inventory(path, expected_sha256=digest)


def test_assets_are_rechecked_immediately_before_consumption(sample):
    path, digest, _ = sample.pin()
    inventory = legacy.load_inventory(path, expected_sha256=digest)
    loaded = inventory.for_task(sample.task, sample.oracle)
    ref = next(r for r in loaded.row["evidence"] if r["role"] == "harness")
    sample.harness.write_bytes(b"changed assertions\n")
    with pytest.raises(legacy.LegacyInputError, match="sha256_mismatch"):
        loaded.read_evidence(ref)
    with pytest.raises(legacy.LegacyInputError, match="unbound_evidence"):
        loaded.read_evidence({**ref, "path": "unbound-file"})
    with pytest.raises(legacy.LegacyInputError, match="task_or_oracle_changed"):
        inventory.for_task(sample.task, {**sample.oracle, "command": "true"})


def test_numeric_jvm_counts_remain_numeric_not_six_test_ids(sample):
    sample.source["pass_to_pass_tests"] = [6, 6, 6, 6, 6, 6]
    sample.source["pass_to_pass"] = 6
    sample.sync()
    row = sample.capture()["rows"][0]
    assert row["normalized_contract"]["pass_to_pass_tests"] == [6] * 6
    assert "fixed_unique_string_test_identities" in row["required_lane_adapter"]


def test_six_exit_codes_without_logs_are_not_six_bound_replays(sample):
    sample.source["start_runs"] = [0, 0, 0]
    sample.source.pop("golden_runs")
    sample.source["source_only_golden_runs"] = [0, 0, 0]
    sample.source["start_signature"] = sample.source["golden_signature"] = "2 passed"
    sample.sync()
    row = sample.capture()["rows"][0]
    assert row["replay"]["six_original_signals_bound"] is False
    assert [s["raw"] for s in row["replay"]["signals"]] == [0] * 6
    assert row["normalized_contract"]["source_only_golden_runs"] == [0] * 3
    assert row["runtime_status"] == "not_verified"


def test_metadata_signature_must_be_present_in_its_original_log(sample):
    for phase in ("start", "golden"):
        for run in sample.source[phase + "_runs"]:
            run["signature"] = "claimed-but-not-printed"
    sample.sync()
    row = sample.capture()["rows"][0]
    assert not row["replay"]["six_original_signals_bound"]
    assert len([i for i in row["replay"]["issues"] if i.endswith("_signature_not_in_log")]) == 6


def test_aggregate_counts_and_log_patterns_do_not_invent_statuses(sample):
    sample.source["start_runs"] = sample.source["golden_runs"] = 3
    sample.source["logs"] = {p: f"{sample.relative_dir}/{p}-{{1,2,3}}.log" for p in ("start", "golden")}
    sample.sync()
    row = sample.capture()["rows"][0]
    assert len(row["replay"]["signals"]) == 6
    for s in row["replay"]["signals"]:
        assert s["raw"] is None and s["recorded_success"] is None
        assert len(s["logs"]) == 1
        assert s["logs"][0]["state"] == "present"
    assert not row["replay"]["six_original_signals_bound"]


@pytest.mark.parametrize("change", ["one_run", "four_runs", "run_number", "conflicting_alias", "same_log"])
def test_missing_conflicting_or_reused_replay_evidence_is_detected(sample, change):
    runs = sample.source["start_runs"]
    if change == "one_run":
        sample.source["start_runs"] = runs[:1]
    elif change == "four_runs":
        sample.source["start_runs"] = runs + [deepcopy(runs[-1])]
    elif change == "run_number":
        runs[0]["run"] = True
    elif change == "conflicting_alias":
        sample.source["runs"] = {"start": deepcopy(runs)}
        sample.source["runs"]["start"][0]["exit_code"] = 1
    else:
        runs[1]["log"] = runs[0]["log"]
    sample.sync()
    row = sample.capture()["rows"][0]
    assert row["replay"]["issues"]
    assert not row["replay"]["six_original_signals_bound"]


@pytest.mark.parametrize("raw", [True, {"exit_code": False}, {"ok": "true"}, {}, {"exit_code": 0, "failed": 1}])
def test_success_requires_typed_explicit_per_run_observations(sample, raw):
    sample.source["start_runs"][0] = raw
    sample.sync()
    row = sample.capture()["rows"][0]
    assert row["replay"]["signals"][0]["recorded_success"] is not True
    assert not row["replay"]["six_original_signals_bound"]


def test_no_provenance_or_published_status_is_not_validation(sample):
    sample.oracle.pop("release_oracle_provenance")
    sample.oracle["published"] = sample.oracle["validated"] = True
    sample.save()
    row = sample.capture()["rows"][0]
    assert row["source"]["status"] == "unattributed_release_record"
    assert row["runtime_status"] == "not_verified"
    assert row["migration_route"] == "new_adapter"


def test_engineered_secondary_contract_is_diagnostic_only(sample):
    entry = {"status": "recovered", "recipe": {"expected_tests": ["new weaker assertion"]},
             "provenance": {"oracle_kind": "engineered session Oracle (historical harness lost)"}}
    sample.secondary = put(sample.release / "secondary.json", json.dumps({"tasks": {sample.task["id"]: entry}}))
    row = sample.capture()["rows"][0]
    assert row["secondary_contract"]["declared_status"] == "recovered"
    assert row["secondary_contract"]["use"] == "diagnostic_only_never_a_replacement_for_original_assertions"
    assert row["normalized_contract"]["pass_to_pass_tests"] == ["Test::publicAPI"]
    assert row["runtime_status"] == "not_verified"


def test_legacy_input_index_is_not_joined_to_an_unrelated_modern_plan(sample):
    sample.task["inferred_language"] = "Rust"
    sample.source["input_index"] = 4
    sample.source["workspace_command"] = "cargo test"
    sample.sync()
    modern = {**sample.task, "id": "another-rust-task"}
    jsonl(sample.oracle_root / "rust/tasks.jsonl", modern)
    put(sample.oracle_root / "rust/plans.jsonl", '{"input_index":4,"command":"cargo test"}\n')
    row = sample.capture()["rows"][0]
    assert row["existing_lane_catalog"]["task_membership"] is False
    assert "task_absent_from_existing_lane_catalog" in row["required_lane_adapter"]
    assert row["existing_lane_catalog"]["specification"]["state"] == "present"
    assert row["migration_route"] == "new_adapter"


def test_compatible_maven_input_loads_without_becoming_an_execution_proof(sample):
    import shutil
    old = sample.relative_dir
    directory = sample.oracle_root / "jvm/task"
    shutil.copytree(sample.directory, directory)
    # This synthetic fixture already has the existing lane's exact layout,
    # identities, command/environment, fixed harness, patch pin and six logs.
    sample.source = json.loads(json.dumps(sample.source).replace(old, str(directory)))
    sample.source["build_system"] = "maven"
    sample.source["environment"]["build_system"] = "maven"
    sample.source["command"] = "/oracle-tools/maven/bin/mvn -Dtest=Test#publicAPI test"
    sample.sync()
    jsonl(sample.oracle_root / "jvm/tasks.jsonl", sample.task)
    path, digest, document = sample.pin()
    assert document["summary"]["migration_route"] == {"existing_lane": 1}
    inventory = legacy.load_inventory(path, expected_sha256=digest)
    inputs = inventory.for_task(sample.task, sample.oracle)
    assert inputs.require_existing_lane() == sample.oracle
    assert document["summary"]["runtime_verified"] == 0
    # Public inspection views are copies, not writable verification state.
    inputs.row["migration_route"] = "not-the-original"
    view = inputs.row
    view["normalized_contract"]["pass_to_pass_tests"].append("forged")
    inventory.document["rows"].clear()
    assert inputs.require_existing_lane()["pass_to_pass_tests"] == ["Test::publicAPI"]
    assert inventory.for_task(sample.task, sample.oracle).row["runtime_status"] == "not_verified"


def test_discovered_session_spec_is_not_fabricated_source_metadata(sample):
    sample.task["inferred_language"] = "Python"
    put(sample.directory / "session-spec.json", json.dumps({
        "command": ["python", "-c", "print('P2P_SIGNATURE=forged')"],
        "pass_to_pass_tests": sample.source["pass_to_pass_tests"],
    }))
    sample.sync()
    row = sample.capture()["rows"][0]
    assert "original_session_spec_reference_and_sha256" in row["required_lane_adapter"]
    assert row["existing_lane_catalog"]["specification"]["binding"] == "convention_discovery_only"
    assert "session_spec" not in row["normalized_contract"]
    assert row["migration_route"] == "new_adapter"


def test_source_declared_session_spec_sha_is_checked_before_loading_it(sample):
    sample.task["inferred_language"] = "Python"
    put(sample.directory / "session-spec.json", '{"command":["python","oracle.py"]}')
    sample.source["session_spec"] = {"artifact": sample.relative_dir + "/session-spec.json", "sha256": "0" * 64}
    sample.sync()
    row = sample.capture()["rows"][0]
    assert row["existing_lane_catalog"]["specification"]["state"] == "sha256_mismatch"
    assert "original_session_spec_command" in row["required_lane_adapter"]


def test_target_commit_status_does_not_supply_a_source_only_golden(sample):
    sample.source["golden_mode"] = "target-commit"
    sample.source["source_only_golden"] = False
    sample.sync()
    row = sample.capture()["rows"][0]
    assert "target_commit_is_not_a_source_only_golden" in row["required_lane_adapter"]
    assert row["normalized_contract"]["source_only_golden"] is False


def test_repository_harness_reference_does_not_hide_path_traversal(sample):
    sample.source["harness"] = {"files": ["../outside.cpp (repository test)"]}
    sample.sync()
    row = sample.capture()["rows"][0]
    reference = next(r for r in row["evidence"] if r.get("field") == "/harness/files/0")
    assert reference["state"] == "path_boundary"


def test_modern_rows_are_left_out_without_changing_the_declared_scope(sample):
    modern_task = {**sample.task, "id": "modern-task"}
    modern_oracle = {**sample.oracle, "id": "modern-task", "lane_results_source": "jvm/results.jsonl"}
    jsonl(sample.tasks, sample.task, modern_task)
    jsonl(sample.oracles, sample.oracle, modern_oracle)
    put(sample.scope, "legacy-task\nmodern-task\n")
    result = sample.capture()
    assert result["scope_size"] == 2 and result["legacy_count"] == 1
    assert [r["id"] for r in result["rows"]] == ["legacy-task"]


def test_capture_has_no_execution_or_input_mutation(sample, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("the input loader attempted to execute a command")
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    before = {str(p): sha(p.read_bytes()) for p in sample.tmp.rglob("*") if p.is_file()}
    sample.capture()
    after = {str(p): sha(p.read_bytes()) for p in sample.tmp.rglob("*") if p.is_file()}
    assert before == after


def test_cli_capture_verify_and_exclusive_output(sample, capsys):
    config = put(sample.release / "experiment.yaml", "\n".join([
        f"root: {sample.release}", "tasks: tasks.jsonl", "oracles: oracles.jsonl",
        "scope_ids: scope.ids", "budget: {total_output_tokens: 128000}",
        "models: {unchanged: {model_name: unchanged}}",
    ]))
    before = config.read_bytes()
    output = sample.tmp / "cli-inventory.json"
    args = ["capture", "--config", str(config), "--oracle-root", str(sample.oracle_root), "--output", str(output)]
    assert legacy.main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["legacy_count"] == 1 and result["runtime_verified"] == 0
    saved = output.read_bytes()
    assert legacy.main(args) == 2
    assert output.read_bytes() == saved
    capsys.readouterr()
    assert legacy.main(["verify", "--inventory", str(output), "--sha256", sha(saved)]) == 0
    assert json.loads(capsys.readouterr().out)["runtime_verified"] == 0
    assert config.read_bytes() == before
