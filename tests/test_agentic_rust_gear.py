"""Original Gear pairs retain full graphs, public selection, private fixtures and caches."""
import copy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration import rust_gear_entry as entry
import prepare_agentic_build_cache as cache


def write(root, name, data):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"path": str(path), "sha256": entry.core.digest(data)}


def lock(version):
    return entry.tomli_w.dumps({"version": 3, "package": [
        {"name": "gtest", "version": version},
        {"name": "parity-scale-codec", "version": "3.6.4", "source": entry.core.CRATES_IO},
        {"name": entry.OMITTED[0], "version": entry.OMITTED[1], "source": entry.OMITTED[2],
         "checksum": entry.OMITTED_CHECKSUM},
    ]}).encode()


def fixture(tmp_path):
    source = tmp_path / "candidate"
    reference = b'[workspace.package]\nversion="1.7.1"\n[workspace]\nmembers=["gtest"]\n'
    write(source, "Cargo.toml", reference)
    write(source, "Cargo.lock", lock("1.7.1"))
    write(source, "gtest/Cargo.toml", b'[package]\nname="gtest"\nversion.workspace=true\n')
    write(source, "gtest/src/lib.rs", b"pub fn current() {}\n")
    write(source, "other/unchanged", b"unrelated source retained")
    public = {"schema_version": entry.SCHEMA, "task_id": entry.TASK, "selection": entry.forms.SELECTION,
              "package": "gtest", "package_manifest": "Cargo.toml", "public_command": entry.PUBLIC,
              "reference_seed_sha256": entry.core.digest(lock("1.7.1")),
              "manifests": [{"path": "Cargo.toml", "reference": write(tmp_path, "assets/reference", reference)}],
              "forms": [], "cpus": 4, "timeout_seconds": 60, "metadata_timeout_seconds": 30,
              "max_log_bytes": 1024 * 1024}
    private = {"schema_version": entry.PRIVATE_SCHEMA, "task_id": entry.TASK, "workdir": entry.SESSION,
               "command": entry.COMMAND, "expected_tests": [entry.TEST], "forms": []}
    for index, name in enumerate(entry.NAMES):
        manifest = reference.replace(b"1.7.1", b"1.8.1") if index else reference
        tool = tmp_path / ("compiler-" + name)
        form = {"name": name, "toolchain": entry.TOOLCHAINS[index],
                "cargo": write(tool, "bin/cargo", b"cargo:" + name.encode()),
                "rustc": write(tool, "bin/rustc", b"rustc:" + name.encode()),
                "source_config": write(tmp_path, "assets/config-" + name, name.encode()),
                "manifests": [{"path": "Cargo.toml", "asset": write(tmp_path, "assets/manifest-" + name, manifest)}],
                "lock": write(tmp_path, "assets/lock-" + name, lock("1.8.1" if index else "1.7.1"))}
        public["forms"].append(form)
        pmanifest = manifest.replace(b'members=["gtest"]', b'members=["tests/oracle-gear","gtest"]')
        plock = entry.tomllib.loads(Path(form["lock"]["path"]).read_text())
        plock["package"].append({"name": "gear-oracle-session", "version": "0.0.0",
                                "dependencies": ["gtest", "parity-scale-codec"]})
        private["forms"].append({
            "name": name, "manifest": write(tmp_path, "private/manifest-" + name, pmanifest),
            "lock": write(tmp_path, "private/lock-" + name, entry.tomli_w.dumps(plock).encode()),
            "session_files": [{"path": n, "asset": write(
                tmp_path, "private/" + name + "/" + n,
                ((b"original-" + name.encode()) if n.endswith(".wasm") else
                 b'[package]\nname="gear-oracle-session"\nversion="0.0.0"\n'
                 if n.endswith("Cargo.toml") else b"original session bytes"),
            )} for n in entry.SESSION_PATHS]})
    public["private_fingerprint"] = entry.fingerprint(private)
    return source, public, private


def graph(mirror, *, omitted=True):
    lockdoc = entry.tomllib.loads((mirror / "Cargo.lock").read_text())
    packages = []
    for row in lockdoc["package"]:
        if omitted and (row["name"], row["version"], row.get("source")) == entry.OMITTED:
            continue
        name = row["name"]
        manifest = mirror / (entry.SESSION if name == "gear-oracle-session" else name) / "Cargo.toml"
        package = {"id": name + ":" + row["version"], "name": name, "version": row["version"],
                   "source": row.get("source"), "manifest_path": str(manifest), "targets": []}
        if name == "gear-oracle-session":
            package["targets"] = [{"name": "oracle_session", "kind": ["test"],
                                   "src_path": str(mirror / entry.HARNESS)}]
        packages.append(package)
    return {"workspace_root": str(mirror), "packages": packages,
            "workspace_members": [p["id"] for p in packages if p["source"] is None],
            "resolve": {"nodes": [{"id": p["id"]} for p in packages]}}


def output(command, cwd, stdout, code=0, stderr=""):
    row = {"command": command, "cwd": str(cwd), "returncode": code, "limit": None}
    for name, text in (("stdout", stdout), ("stderr", stderr)):
        row.update({name: text, name + "_utf8_valid": True, name + "_sha256": entry.core.digest(text.encode()),
                    name + "_captured_bytes": len(text.encode()), name + "_total_bytes": len(text.encode())})
    return row


def runner(calls, *, compatible=("start", "golden"), failures=(), fault=None):
    def run(command, cwd, env, folder, label, deadline, limit):
        original = next(name for name in entry.NAMES if "compiler-" + name in command[0])
        private = "metadata-private" == label or label == "original-test"
        mirror = cwd.parent.parent if label == "original-test" else cwd
        calls.append({"form": original, "label": label, "command": command, "env": env,
                      "private": private, "root": mirror,
                      "lock": (mirror / "Cargo.lock").read_bytes(),
                      "manifest": (mirror / "Cargo.toml").read_bytes()})
        if not private:
            assert not (mirror / entry.SESSION).exists()
        if "metadata" in command:
            value = graph(mirror)
            if fault == "graph":
                value["packages"] = value["packages"][:-1]
            return output(command, cwd, json.dumps(value))
        if label == "public-build":
            if fault == "limit":
                row = output(command, cwd, "")
                row["limit"] = "deadline"
                return row
            if fault == "infrastructure":
                return output(command, cwd, "", 101, "disk full\n")
            if original in compatible:
                return output(command, cwd, '{"reason":"build-finished","success":true}\n')
            diagnostic = {"reason": "compiler-message", "manifest_path": str(mirror / "gtest/Cargo.toml"),
                          "target": {"kind": ["lib"], "src_path": str(mirror / "gtest/src/lib.rs")},
                          "message": {"level": "error", "code": {"code": "E0308"},
                                      "message": "current production API mismatch",
                                      "spans": [{"is_primary": True, "file_name": str(mirror / "gtest/src/lib.rs")}]}}
            return output(command, cwd, json.dumps(diagnostic) + '\n{"reason":"build-finished","success":false}\n', 101)
        assert label == "original-test"
        assert (mirror / entry.SESSION_PATHS[2]).read_bytes() == b"original-" + original.encode()
        failed = original in failures
        return output(command, cwd, "test " + entry.TEST + " ... " + ("FAILED" if failed else "ok")
                      + "\ntest result: " + ("FAILED." if failed else "ok.") + "\n", int(failed))
    return run


@pytest.mark.parametrize("compatible", [("start",), ("golden",), ("start", "golden")])
def test_all_public_selection_precedes_any_original_private_form(tmp_path, compatible):
    source, public, private = fixture(tmp_path)
    before, calls = entry.core.snapshot(source), []
    evidence = entry.run_candidate(public, source, tmp_path / "cache", private,
                                   run=runner(calls, compatible=compatible))
    labels = [r["label"] for r in calls]
    assert labels[:4] == ["metadata-public", "public-build", "metadata-public", "public-build"]
    tests = [r for r in calls if r["label"] == "original-test"]
    assert [r["form"] for r in tests] == list(compatible)
    assert all(r["command"][1:] == entry.COMMAND and "--locked" not in r["command"] for r in tests)
    assert evidence["exit_status"] == 0 and evidence["test_results"] == {entry.TEST: True}
    assert entry.core.snapshot(source) == before and evidence["mirrors_restored"]
    assert all(entry.core.snapshot(r["root"]) == before for r in calls)
    builds = [r for r in calls if r["label"] == "public-build"]
    for name in ("CARGO_HOME", "RUSTC", "CARGO_TARGET_DIR", "RUSTDOC"):
        assert builds[0]["env"][name] != builds[1]["env"][name]


def test_failing_original_test_is_conjunctive_not_a_selection_signal(tmp_path):
    source, public, private = fixture(tmp_path)
    calls = []
    evidence = entry.run_candidate(public, source, tmp_path / "cache", private,
                                   run=runner(calls, failures=("start",)))
    assert evidence["exit_status"] == 1 and evidence["test_results"] == {entry.TEST: False}
    assert [r["form"] for r in calls if r["label"] == "original-test"] == ["start", "golden"]


@pytest.mark.parametrize("fault", ["graph", "infrastructure", "limit"])
def test_infrastructure_graph_and_limit_errors_never_license_private_tests(tmp_path, fault):
    source, public, private = fixture(tmp_path)
    before, calls = entry.core.snapshot(source), []
    with pytest.raises(entry.core.VariantError) as caught:
        entry.run_candidate(public, source, tmp_path / "cache", private, run=runner(calls, fault=fault))
    assert not any(r["private"] for r in calls)
    assert caught.value.evidence["mirrors_restored"]
    assert entry.core.snapshot(source) == before


def test_unrelated_candidate_edits_survive_each_manifest_and_private_overlay(tmp_path):
    source, public, private = fixture(tmp_path)
    with (source / "Cargo.toml").open("ab") as stream:
        stream.write(b'\n[workspace.metadata.candidate]\nretain = "yes"\n')
    write(source, "gtest/src/new.rs", b"new candidate source")
    (source / "other/unchanged").unlink()
    calls = []
    entry.run_candidate(public, source, tmp_path / "cache", private, run=runner(calls))
    assert all(entry.tomllib.loads(r["manifest"].decode())["workspace"]["metadata"]["candidate"]["retain"]
               == "yes" for r in calls)
    assert not (source / "other/unchanged").exists()


@pytest.mark.parametrize("owner", ["manifest", "lock"])
def test_candidate_owned_configuration_is_not_repaired_from_originals(tmp_path, owner):
    source, public, private = fixture(tmp_path)
    if owner == "manifest":
        (source / "Cargo.toml").write_text((source / "Cargo.toml").read_text().replace("1.7.1", "1.7.9"))
    else:
        with (source / "Cargo.lock").open("ab") as stream:
            stream.write(b"\n# candidate-owned lock bytes\n")
    before, calls = entry.core.snapshot(source), []
    evidence = entry.run_candidate(public, source, tmp_path / "cache", private, run=runner(calls))
    public_calls = [r for r in calls if not r["private"]]
    assert all(r["lock"] == (source / "Cargo.lock").read_bytes() for r in public_calls)
    assert all(r["manifest"] == (source / "Cargo.toml").read_bytes() for r in public_calls)
    assert evidence["candidate_configuration_preserved"] and entry.core.snapshot(source) == before
    for row in calls:
        if row["private"]:
            assert row["lock"].startswith((source / "Cargo.lock").read_bytes())
            document = entry.tomllib.loads(row["lock"].decode())
            assert len(document["package"]) == 4  # Only the original session package was appended.


@pytest.mark.parametrize("compatible", [("start",), ("golden",), ("start", "golden")])
def test_cache_prewarmer_recognizes_both_isolated_public_namespaces(tmp_path, compatible):
    source, public, _ = fixture(tmp_path)
    evidence = entry.run_candidate(public, source, tmp_path / "cache", run=runner([], compatible=compatible))
    (tmp_path / "stdout").write_text(entry.EVIDENCE_PREFIX + json.dumps(evidence) + "\n")
    metadata = cache.metadata_from_records(tmp_path, [{"stdout_file": "stdout"}])
    assert len(metadata["cache_groups"]) == 2
    assert all(not any(p["name"] == "gear-oracle-session" for p in g["packages"])
               for g in metadata["cache_groups"].values())


def test_unused_registry_entry_is_retained_in_the_original_lock(tmp_path):
    source, _, _ = fixture(tmp_path)
    raw = (source / "Cargo.lock").read_bytes()
    with pytest.raises(entry.core.VariantError, match="differs"):
        entry.core.graph_check(json.dumps(graph(source)), source, entry.core.snapshot(source), raw)
    result = entry.graph_check(json.dumps(graph(source)), source, entry.core.snapshot(source), raw)
    assert result["unresolved_locked_registry_packages"] == [list(entry.OMITTED)]
    assert (source / "Cargo.lock").read_bytes() == raw
    result = entry.graph_check(json.dumps(graph(source, omitted=False)), source, entry.core.snapshot(source), raw)
    assert result["unresolved_locked_registry_packages"] == []


@pytest.mark.parametrize("fault", ["different-package", "different-checksum", "local-package", "observed", "duplicate"])
def test_no_other_graph_omission_is_licensed(tmp_path, fault):
    source, _, _ = fixture(tmp_path)
    raw = (source / "Cargo.lock").read_bytes()
    value = graph(source)
    if fault == "different-checksum":
        raw = raw.replace(entry.OMITTED_CHECKSUM.encode(), b"0" * 64)
        with pytest.raises(entry.core.VariantError):
            entry.graph_check(json.dumps(value), source, entry.core.snapshot(source), raw)
        return
    omissions = (entry.OMITTED,)
    if fault == "different-package":
        value["packages"] = [p for p in value["packages"] if p["name"] != "parity-scale-codec"]
    elif fault == "local-package":
        omissions = (("gtest", "1.7.1", None),)
    elif fault == "observed":
        value = graph(source, omitted=False)
    else:
        omissions = (entry.OMITTED, entry.OMITTED)
    with pytest.raises(entry.core.VariantError):
        entry.core.graph_check(json.dumps(value), source, entry.core.snapshot(source), raw,
                               omitted_locked_registry_packages=omissions)


@pytest.mark.parametrize("field", ["toolchain", "command", "wasm", "collision"])
def test_changed_original_compiler_command_fixture_or_collision_is_rejected(tmp_path, field):
    source, public, private = fixture(tmp_path)
    if field == "toolchain":
        public["forms"][1]["toolchain"] = entry.TOOLCHAINS[0]
    elif field == "command":
        private["command"] = [*entry.COMMAND, "--ignored"]
    elif field == "wasm":
        Path(private["forms"][0]["session_files"][2]["asset"]["path"]).write_bytes(b"wrong WASM")
    else:
        write(source, entry.HARNESS, b"candidate-authored replacement test")
    with pytest.raises(entry.core.VariantError):
        entry.run_candidate(public, source, tmp_path / "cache", private, run=runner([]))


def test_only_exact_explicit_gear_profile_routes_to_new_migration():
    from types import SimpleNamespace
    from agentic_runtime_migration import rust_gear as host, rust_lane
    context = SimpleNamespace(config={"migration": {host.OPTION: {entry.TASK: {}}}})
    assert host.requested(context, {"id": entry.TASK})
    assert not host.requested(context, {"id": "another-original-task"})
    assert rust_lane.migration_scratch_bytes(context, {"id": entry.TASK}) == 2 * 1024**3
    context.config["migration"][host.OPTION] = {"not-original": {}}
    with pytest.raises(host.ContractError):
        host.requested(context, {"id": entry.TASK})


def test_no_compatible_public_form_runs_no_assertions(tmp_path):
    source, public, private = fixture(tmp_path)
    calls = []
    with pytest.raises(entry.core.VariantError, match="no original environment"):
        entry.run_candidate(public, source, tmp_path / "cache", private, run=runner(calls, compatible=()))
    assert len(calls) == 4 and not any(row["private"] for row in calls)


def test_git_config_reconstruction_keeps_registry_root_net_policy_and_commit_isolation():
    from agentic_runtime_migration import rust_gear as host
    first = {"name": "git-source", "version": "1.0", "source": "git+https://example.org/repo?branch=fixed#" + "a" * 40}
    second = {**first, "source": first["source"].replace("a" * 40, "b" * 40)}
    a, b = [host.isolated_sources({"package": [r]}) for r in (first, second)]
    assert a != b and a["net"] == b["net"] == {"offline": True, "retry": 0}
    source = a["source"]
    assert source[source["crates-io"]["replace-with"]]["directory"].startswith(
        "/opt/reviewbench-assets/vendor-registry/")
    assert any(v.get("directory", "").startswith("/opt/reviewbench-assets/vendor-git/") for v in source.values())
    with pytest.raises(host.ContractError, match="multiple commits"):
        host.isolated_sources({"package": [first, second]})


@pytest.mark.parametrize("fault", [None, "commit", "repository", "selector", "kind"])
def test_metadata_branch_slash_spelling_preserves_full_git_identity(tmp_path, fault):
    from agentic_runtime_migration import rust_vendor
    source, _, _ = fixture(tmp_path)
    encoded = "git+https://example.org/repo?branch=release%2Fstable#" + "a" * 40
    literal = encoded.replace("%2F", "/")
    assert entry.git_source_key(encoded) == rust_vendor.git_source_identity(encoded)
    assert entry.git_source_key(literal) == rust_vendor.git_source_identity(literal)
    raw = (source / "Cargo.lock").read_bytes()
    document = entry.tomllib.loads(raw.decode())
    document["package"].append({"name": "locked-git", "version": "1.0", "source": encoded})
    data = entry.tomli_w.dumps(document).encode()
    value = graph(source)
    if fault == "commit":
        literal = literal.replace("a" * 40, "b" * 40)
    elif fault == "repository":
        literal = literal.replace("example.org/repo", "other.example.org/repo")
    elif fault == "selector":
        literal = literal.replace("release/stable", "release/other")
    elif fault == "kind":
        literal = literal.replace("?branch=", "?tag=")
    value["packages"].append({"id": "locked-git", "name": "locked-git", "version": "1.0", "source": literal})
    value["resolve"]["nodes"].append({"id": "locked-git"})
    if fault:
        with pytest.raises(entry.core.VariantError, match="differs"):
            entry.graph_check(json.dumps(value), source, entry.core.snapshot(source), data)
    else:
        assert entry.graph_check(json.dumps(value), source, entry.core.snapshot(source), data)["packages"] == 3
    assert (source / "Cargo.lock").read_bytes() == raw
