"""Original root manifest/lock pairs; current source and original test argv survive."""
import copy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_manifest_variants_entry as entry
from test_agentic_rust_image import ref, output


def fixture(root, variant="start"):
    source = root / "source"
    for name in ("src", "member/src", "tests"):
        (source / name).mkdir(parents=True, exist_ok=True)
    (source / "src/lib.rs").write_text("pub fn actual_candidate()->u8{42}\n")
    (source / "member/src/lib.rs").write_text("pub fn preserved()->u8{99}\n")
    dependency = "1.0.0" if variant == "start" else "2.0.0"
    (source / "member/Cargo.toml").write_text(
        '[package]\nname="member"\nversion="0.1.0"\n[dependencies]\nindexmap="=' + dependency + '"\n')
    if variant == "golden":
        (source / "extra/src").mkdir(parents=True)
        (source / "extra/src/lib.rs").write_text("pub fn extra(){}\n")
        (source / "extra/Cargo.toml").write_text('[package]\nname="extra"\nversion="0.1.0"\n')
    forms = []
    for name, version in (("start", "1.0.0"), ("golden", "2.0.0")):
        doc = {"package": {"name": "vector", "version": "0.1.0"},
               "workspace": {"members": [".", "member"] + (["extra"] if name == "golden" else [])},
               "dependencies": {"indexmap": "=" + version},
               "features": {"selected": [], "default": []}}
        if name == "golden":
            doc["dependencies"]["extra"] = {"path": "extra"}
        path = root / (name + ".toml")
        path.write_text("# exact original " + name + "\n" + entry.tomli_w.dumps(doc))
        packages = [{"name": "vector", "version": "0.1.0"}, {"name": "member", "version": "0.1.0"},
                    {"name": "indexmap", "version": version, "source": "git+https://example.invalid/map#" + "a" * 40}]
        if name == "golden":
            packages.append({"name": "extra", "version": "0.1.0"})
        lock = root / (name + ".lock")
        lock.write_text(entry.tomli_w.dumps({"version": 3, "package": packages}))
        forms.append({"name": name, "manifests": [{"path": "Cargo.toml", "asset": ref(path)}], "lock": ref(lock)})
    (source / "Cargo.toml").write_bytes(Path(forms[0]["manifests"][0]["asset"]["path"]).read_bytes())
    (source / "Cargo.lock").write_bytes(Path(forms[0]["lock"]["path"]).read_bytes())
    (source / "tests/original.rs").write_text("#[test] fn original_behavior() { assert!(true); }\n")
    for name in ("cargo", "rustc", "sources.toml"):
        (root / name).write_text("pinned " + name)
    features = ["--no-default-features", "--features", "selected"]
    public = {"schema_version": entry.ROOT_SCHEMA, "task_id": "root-fixture", "selection": entry.SELECTION,
              "package": "vector", "package_manifest": "Cargo.toml", "forms": forms,
              "manifests": [{"path": "Cargo.toml", "reference": forms[0]["manifests"][0]["asset"]}],
              "feature_args": features,
              "public_command": ["build", "--locked", *features, "--lib", "--message-format=json"],
              "reference_seed_sha256": forms[0]["lock"]["sha256"],
              "cargo": ref(root / "cargo"), "rustc": ref(root / "rustc"),
              "source_config": ref(root / "sources.toml"), "cpus": 4, "timeout_seconds": 3600,
              "metadata_timeout_seconds": 180, "max_log_bytes": 32 * 1024**2}
    h = {"path": "tests/original.rs", "sha256": ref(source / "tests/original.rs")["sha256"]}
    private = {"task_id": public["task_id"], "package_manifest": "Cargo.toml", "harness": h,
               "session_files": [h], "test_filter": "original_behavior", "expected_tests": ["original_behavior"],
               "command": ["test", "--locked", *features, "--test", "original", "original_behavior", "--", "--nocapture"]}
    return source, public, private


def runner(calls, *, passed=True, both=False, fault=None):
    def run(command, cwd, env, folder, label, deadline, limit):
        root = entry.tomllib.loads((cwd / "Cargo.toml").read_text())
        lock = entry.tomllib.loads((cwd / "Cargo.lock").read_text())
        version = root["dependencies"]["indexmap"].lstrip("=")
        wanted = entry.tomllib.loads((cwd / "member/Cargo.toml").read_text())["dependencies"]["indexmap"].lstrip("=")
        calls.append({"command": command, "root": (cwd / "Cargo.toml").read_bytes(),
                      "lock": (cwd / "Cargo.lock").read_bytes(), "cache": env["CARGO_TARGET_DIR"]})
        assert (cwd / "src/lib.rs").read_text() == "pub fn actual_candidate()->u8{42}\n"
        if "metadata" in command:
            if fault:
                return output(command, cwd, code=101, stderr=fault)
            if "extra" in root["dependencies"] and not (cwd / "extra/Cargo.toml").exists():
                return output(command, cwd, code=101, stderr=(
                    f"error: failed to load manifest for workspace member `{cwd}/.`\n\n"
                    "Caused by:\n  failed to load manifest for dependency `extra`\n\n"
                    f"Caused by:\n  failed to read `{cwd}/extra/Cargo.toml`\n\n"
                    "Caused by:\n  No such file or directory (os error 2)\n"))
            if version != wanted and not both:
                return output(command, cwd, code=101, stderr=(
                    "error: failed to select a version for `indexmap`.\n"
                    f"    ... required by package `member v0.1.0 ({cwd}/member)`\n"
                    "versions that meet the requirements are: 2.0.0\n"
                    "previously selected package `indexmap v1.0.0`\n"
                    "failed to select a version for `indexmap` which could resolve this conflict\n"))
            if next(p["version"] for p in lock["package"] if p["name"] == "indexmap") != version:
                return output(command, cwd, code=101, stderr=(
                    f"error: the lock file {cwd}/Cargo.lock needs to be updated but --locked was passed to prevent this\n"
                    "If you want to try to generate the lock file without accessing the network, "
                    "remove the --locked flag and use --offline instead.\n"))
            packages = []
            for p in lock["package"]:
                row = {**p, "id": p["name"] + "@" + p["version"], "targets": []}
                if not p.get("source"):
                    relative = "Cargo.toml" if p["name"] == "vector" else p["name"] + "/Cargo.toml"
                    row["manifest_path"] = str(cwd / relative)
                    if p["name"] == "vector":
                        row["targets"] = [{"name": "original", "kind": ["test"], "src_path": str(cwd / "tests/original.rs")}]
                packages.append(row)
            return output(command, cwd, stdout=json.dumps({
                "workspace_root": str(cwd), "packages": packages,
                "workspace_members": [p["id"] for p in packages if not p.get("source")],
                "resolve": {"nodes": [{"id": p["id"]} for p in packages]}}))
        if "build" in command:
            return output(command, cwd, stdout='{"reason":"build-finished","success":true}\n')
        return output(command, cwd, code=0 if passed else 101, stdout=(
            "test original_behavior ... " + ("ok" if passed else "FAILED") +
            "\ntest result: " + ("ok." if passed else "FAILED.") + "\n"))
    return run


@pytest.mark.parametrize("variant", ["start", "golden"])
@pytest.mark.parametrize("grading", [False, True])
def test_complete_current_source_uses_original_paired_inputs_and_flags(tmp_path, variant, grading):
    source, public, private = fixture(tmp_path, variant)
    before, calls = entry.core.snapshot(source), []
    record = entry.run_candidate(public, source, tmp_path / "cache",
                                 private if grading else None, run=runner(calls))
    assert record["exit_status"] == 0 and record["compatible_forms"] == [variant]
    assert entry.core.snapshot(source) == record["source_after"] == record["source_before"] == before
    assert len(record["probes"]) == 2  # Pairs, not a cross-product of unrelated original locks.
    selected = next(f for f in public["forms"] if f["name"] == variant)
    compiled = next(call for call in calls if "build" in call["command"])
    assert compiled["root"] == Path(selected["manifests"][0]["asset"]["path"]).read_bytes()
    assert compiled["lock"] == Path(selected["lock"]["path"]).read_bytes()
    assert "--no-default-features" in compiled["command"]
    if grading:
        assert calls[-1]["command"] == [public["cargo"]["path"], *private["command"]]
        assert record["test_results"] == {"original_behavior": True}


def test_failed_original_test_never_selects_or_repairs_another_form(tmp_path):
    source, public, private = fixture(tmp_path, "golden")
    calls = []
    result = entry.run_candidate(public, source, tmp_path / "cache", private, run=runner(calls, passed=False))
    assert result["exit_status"] == 1 and result["test_results"] == {"original_behavior": False}
    assert sum("test" in row["command"] for row in calls) == 1


def test_all_publicly_compatible_forms_are_tested_without_assertion_selection(tmp_path):
    source, public, private = fixture(tmp_path, "golden")
    calls = []
    result = entry.run_candidate(public, source, tmp_path / "cache", private, run=runner(calls, both=True))
    assert result["compatible_forms"] == ["start", "golden"]
    assert sum("test" in row["command"] for row in calls) == 2
    assert len({row["cache"] for row in calls}) == 2


@pytest.mark.parametrize("change", ["unrelated", "controlled-version", "controlled-members", "owned-lock"])
def test_candidate_owned_manifest_and_lock_changes_survive(tmp_path, change):
    source, public, _ = fixture(tmp_path, "golden")
    doc = entry.tomllib.loads((source / "Cargo.toml").read_text())
    if change == "unrelated":
        doc["package"]["description"] = "candidate-owned"
    elif change == "controlled-version":
        doc["dependencies"]["indexmap"] = "=3.0.0"
    elif change == "controlled-members":
        doc["workspace"]["members"].append("candidate-member")
    else:
        (source / "Cargo.lock").write_bytes((source / "Cargo.lock").read_bytes() + b"# owned\n")
    (source / "Cargo.toml").write_text(entry.tomli_w.dumps(doc))
    before = entry.core.snapshot(source)
    forms = entry.build_forms(public, source, before)
    assert len(forms) == (2 if change == "unrelated" else 1)
    for form in forms:
        observed = entry.tomllib.loads(form["contents"]["Cargo.toml"].decode())
        if change == "unrelated":
            assert observed["package"]["description"] == "candidate-owned"
        else:
            assert form["candidate_owned"] and form["contents"]["Cargo.toml"] == (source / "Cargo.toml").read_bytes()
    assert entry.core.snapshot(source) == before


@pytest.mark.parametrize("message", ["network failed\n", "No space left on device\n",
                                     "error: no matching package named `missing`\n"])
def test_transport_and_missing_archive_errors_do_not_trigger_form_fallback(tmp_path, message):
    source, public, private = fixture(tmp_path)
    calls = []
    with pytest.raises(entry.core.VariantError, match="outside graph compatibility"):
        entry.run_candidate(public, source, tmp_path / "cache", private, run=runner(calls, fault=message))
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["defaults", "features", "filter", "unlocked", "old-schema"])
def test_original_root_contract_is_not_weakened(tmp_path, change):
    _, public, private = fixture(tmp_path)
    if change == "defaults":
        private["command"].remove("--no-default-features")
    elif change == "features":
        private["command"][private["command"].index("--features") + 1] = "other"
    elif change == "filter":
        private["command"].remove("original_behavior")
    elif change == "unlocked":
        private["command"].remove("--locked")
    else:
        public["schema_version"] = entry.SCHEMA
    with pytest.raises((entry.core.VariantError, KeyError)):
        entry.validate(public, private)


def test_root_public_graphs_can_seed_each_effective_dependency_namespace(tmp_path):
    from prepare_agentic_build_cache import metadata_from_records
    source, public, _ = fixture(tmp_path, "golden")
    result = entry.run_candidate(public, source, tmp_path / "cache", run=runner([], both=True))
    (tmp_path / "stdout").write_text(entry.EVIDENCE_PREFIX + json.dumps(result) + "\n")
    groups = metadata_from_records(tmp_path, [{"stdout_file": "stdout"}])["cache_groups"]
    assert len(groups) == 2 and sorted(len(graph["packages"]) for graph in groups.values()) == [3, 4]


def test_same_test_path_in_another_workspace_package_is_not_the_original_root_test(tmp_path):
    source, public, private = fixture(tmp_path)
    calls = []
    original = runner(calls)

    def redirected(command, cwd, env, folder, label, deadline, limit):
        row = original(command, cwd, env, folder, label, deadline, limit)
        if "metadata" in command and row["returncode"] == 0:
            graph = json.loads(row["stdout"])
            root = next(p for p in graph["packages"] if p["name"] == "vector")
            member = next(p for p in graph["packages"] if p["name"] == "member")
            member["targets"], root["targets"] = root["targets"], []
            return output(command, cwd, stdout=json.dumps(graph))
        return row

    with pytest.raises(entry.core.VariantError, match="another workspace package"):
        entry.run_candidate(public, source, tmp_path / "cache", private, run=redirected)
    assert all("metadata" in call["command"] for call in calls)
